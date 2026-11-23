"""助理对话里的「一步步生成」引导接口。

这一层只做三件事：
1. 把 `app.wizard_flow` 的纯状态机接到项目数据上（语料列表 / 提炼档位 /
   角色列表 / 已有段号）；
2. 把用户选中的答案**映射到现有参数**：项目字段、现有提炼后台任务、
   现有 `build_bible`、现有 `generate_loop`——不新造生成执行层；
3. 把会话落库，支持刷新/关页后续跑。

「角色对话」是仓库里原本没有的链路：这里只读 KG 角色卡 + Assistant 模型
回答，不写正文、不写业务表、不改 KG 任何数据。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app import wizard_actions as actions
from app import wizard_chat as chat
from app import wizard_facts as facts
from app import wizard_flow as flow
from app import wizard_story as story
from app.assistant_store import list_messages, record_message
from app.db import get_db
from app.models import (
    AssistantWizardSession,
    DistillRun,
    Generation,
    KgCharacterState,
    KgEntity,
    KgNode,
    KgRelation,
    NovelChunk,
    Project,
    RawNovel,
    WizardNotice,
)
from app.schemas import StrictRequest
from app.style.jobs import (
    ACTIVE_STATUSES,
    HEALTH_STALLED,
    distill_health,
    distill_run_to_dict,
    emit_notice,
)
from app.style.narrative import DEFAULT_TIER, list_tiers

router = APIRouter(prefix="/api/assistant/wizard", tags=["assistant-wizard"])

logger = logging.getLogger(__name__)

#: 长文本至少落进 project.premise 的步骤（方向/角色名/情境有自己的语义，
#: 不在这里兜底）。premise 是既有字段，已被生成链路读取，不需要改表。
_PREMISE_STEPS = {"corpus", "genre", "style", "length", "chapters", "outline", "book_scope"}
_LONG_TEXT_NOTE = "（这段我先记进项目简介了，之后生成会带上。）"


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------


class WizardStartIn(StrictRequest):
    project_id: int
    #: True = 放弃当前会话重新开始（旧会话标记 aborted，不删行）。
    restart: bool = False


class WizardAnswerIn(StrictRequest):
    step_key: str
    value: Any = None
    #: True = 跳过本题，直接采用卡片默认值（只对有默认值的步骤有效）。
    skip: bool = False


class RoleChatIn(StrictRequest):
    message: str
    #: 最近几轮对话（前端持有），用于维持角色口吻的连续性。
    history: list[dict[str, str]] = []


class WizardGotoIn(StrictRequest):
    #: 从汇总卡片里直接跳回某一步修改。
    step_key: str


class WizardSayIn(StrictRequest):
    """用户直接打字说的话：可能是作答，也可能是追问，也可能是「帮我改 X」。"""

    text: str


class WizardUndoIn(StrictRequest):
    """撤销一次写操作；不传 audit_id 就撤销最近一次可撤销的。"""

    audit_id: int | None = None


class CardsRegenerateIn(StrictRequest):
    """重新生成角色卡；names 为空 = 全部角色。"""

    names: list[str] = []


class CharacterCardIn(StrictRequest):
    """手工编辑后的角色卡（分组结构：身份/声纹/能力/关系）。"""

    card: dict[str, Any]


# ---------------------------------------------------------------------------
# 会话与上下文
# ---------------------------------------------------------------------------


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _capture_premise(db: Session, project: Project, text: str) -> bool:
    """把长文本追加进项目简介（既有字段），返回是否真的写了。

    只追加、不覆盖：用户之前写过的简介保留；同一段重复发送不重复落库。
    这里不新建字段、不改表结构。
    """
    t = (text or "").strip()
    old = (project.premise or "").strip()
    if not t or t in old:
        return False
    project.premise = f"{old}\n\n{t}".strip() if old else t
    db.commit()
    return True


def _story_prefill(
    step: str,
    text: str,
    answers: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """从用户一句话里抽出**其他步骤**的信息（当前步骤交给 parse 判）。

    这是「只说一句就尽量别重复问」的入口：在 branch 说「写个科幻悬疑，主角叫
    林深，第一人称，30 万字日更」，题材/写法/主角/篇幅会一次性预填，后面
    对应的卡片直接跳过，回执里告诉用户「已记下什么、还差什么」。
    """
    if step not in story.STORY_STEPS and step not in ("branch", "corpus"):
        return {}, []
    sig = story.read_signals(text, tuple(flow.GENRE_HINTS), tuple(flow.STYLE_HINTS))
    if not sig:
        return {}, []
    prefilled = story.prefill(answers, sig)
    tags = list(prefilled.pop("genre_tags", None) or [])
    prefilled.pop(step, None)
    return prefilled, tags


def _apply_prefill(
    answers: dict[str, Any],
    prefill: dict[str, Any],
    tags: list[str],
    current_step: str,
) -> dict[str, Any]:
    """把预填写进 answers，并记进 `_auto`（next_step 会跳过这些已说过的项）。"""
    auto = [s for s in (answers.get("_auto") or []) if s != current_step]
    for key, value in prefill.items():
        if key == current_step or key in answers:
            continue
        answers[key] = value
        auto.append(key)
    if tags:
        answers["genre_tags"] = list(tags)
        if not answers.get("genre"):
            answers["genre"] = tags[0]
            auto.append("genre")
    if auto:
        answers["_auto"] = sorted(set(auto))
    return answers


def _fill_defaults(
    answers: dict[str, Any],
    ctx: dict[str, Any],
    project: Project,
    start_step: str,
) -> list[str]:
    """「按默认来 / 别问了」：把当前及后续步骤全部按默认补齐，直达确认页。"""
    order = flow.step_order(answers.get("branch"), answers, ctx)
    start = order.index(start_step) if start_step in order else 0
    auto = list(answers.get("_auto") or [])
    filled: list[str] = []
    keys = tuple(flow.GENRE_HINTS)
    for key in order[start:]:
        if key == "confirm" or key in answers:
            continue
        value = flow.default_for(key, answers, ctx)
        if value is None or value == "":
            value = story.default_for(key, answers, keys, project.genre)
        if value is None or value == "":
            # 风格卡没有默认值，但「别问了」要求不再追问：取第一个平台调性。
            if key == "style":
                value = next(iter(flow.STYLE_HINTS), "")
        if value is None or value == "":
            continue
        answers[key] = value
        auto.append(key)
        filled.append(key)
    if filled and not answers.get("genre_tags") and answers.get("genre"):
        answers["genre_tags"] = [answers["genre"]]
    if auto:
        answers["_auto"] = sorted(set(auto))
    return filled


def _story_brief(answers: dict[str, Any]) -> dict[str, str]:
    """把故事问答压成「问题 → 答案」，既写进 premise 也传给大纲生成。"""
    labels = story.STORY_LABELS
    out: dict[str, str] = {}
    tags = answers.get("genre_tags") or []
    if answers.get("genre"):
        extra = ""
        if len(tags) > 1:
            extra = "（副标签：" + "、".join(str(t) for t in tags if t != answers["genre"]) + "）"
        out["题材"] = f"{answers['genre']}{extra}"
    for key in ("pov", "premise", "protagonist", "world", "power", "scale"):
        value = answers.get(key)
        if value in (None, ""):
            continue
        out[labels.get(key, key)] = str(value)
    return out


def _active_session(db: Session, project_id: int) -> AssistantWizardSession | None:
    return (
        db.query(AssistantWizardSession)
        .filter(
            AssistantWizardSession.project_id == project_id,
            AssistantWizardSession.status == "active",
        )
        .order_by(desc(AssistantWizardSession.id))
        .first()
    )


def _any_session(db: Session, project_id: int) -> AssistantWizardSession | None:
    return (
        db.query(AssistantWizardSession)
        .filter(AssistantWizardSession.project_id == project_id)
        .order_by(desc(AssistantWizardSession.id))
        .first()
    )


def _context(
    db: Session,
    project: Project,
    history: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把项目现状压成状态机需要的 ctx。

    只做只读查询；`needs_distill` 用「有参考语料但项目没有档案」近似，
    不在这里跑指纹/全量哈希——那会把每次提问拖成秒级 IO。真的命中
    文风档案库时，多问一次档位也只是返回一次 `from_cache: True`。

    讲解需要的**硬事实**（语料章数/字数、已有档案、各档预计耗时、维度清单）
    全部来自 `wizard_facts`，不在这里手写数字。
    """
    novels = (
        db.query(RawNovel)
        .order_by(desc(RawNovel.id))
        .limit(50)
        .all()
    )
    segments_rows = (
        db.query(Generation.segment_no)
        .filter(Generation.project_id == project.id)
        .all()
    )
    segments = sorted({int(row[0]) for row in segments_rows if row[0] is not None})

    chars = (
        db.query(KgEntity)
        .filter(KgEntity.project_id == project.id, KgEntity.kind == "character")
        .order_by(desc(KgEntity.mention_count))
        .limit(20)
        .all()
    )
    characters = []
    for c in chars:
        attrs = c.attrs or {}
        characters.append(
            {
                "name": c.canonical_name,
                "mention_count": c.mention_count or 0,
                "has_secrets": bool(attrs.get("secrets")),
            }
        )
    character_source = "kg_v3"
    if not characters:
        # v3 图谱还没抽角色时，退回旧图谱的 character 节点（只读）。
        # 旧图噪声高（家里/石块这类也会被标成 character），所以要求提及 ≥3
        # 并在卡片上明确标注「旧图谱」，不假装是高质量角色卡。
        legacy: list[tuple[int, str]] = []
        for node in (
            db.query(KgNode)
            .filter(KgNode.project_id == project.id, KgNode.type == "character")
            .all()
        ):
            props = node.props or {}
            name = str(props.get("name") or "").strip()
            mentions = int(props.get("mentions") or 0)
            if name and mentions >= 3:
                legacy.append((mentions, name))
        legacy.sort(reverse=True)
        seen: set[str] = set()
        for mentions, name in legacy:
            if name in seen:
                continue
            seen.add(name)
            characters.append(
                {"name": name, "mention_count": mentions, "has_secrets": False}
            )
            if len(characters) >= 20:
                break
        if characters:
            character_source = "legacy"

    corpus = [int(i) for i in (project.reference_novel_ids or [])]
    archive_exists = bool(project.style_narrative)
    stats = {int(b["id"]): b for b in facts.novel_stats(db, [n.id for n in novels])}
    empty = {"chapters": 0, "chars": 0}
    ctx = {
        "history": history or [],
        "overrides": overrides or {},
        "novels": [
            {
                "id": n.id,
                "title": n.title,
                "author": n.author,
                "genre": n.genre,
                **{
                    k: stats.get(int(n.id), empty)[k]
                    for k in ("chapters", "chars")
                },
                # 单本跑完整档（30% 采样）的预计耗时：按字数外推，不是实测。
                "full_minutes": facts.estimate_minutes(
                    facts.sampled_chars_for(
                        facts.tier_by_key("full"),
                        stats.get(int(n.id), empty)["chapters"],
                        stats.get(int(n.id), empty)["chars"],
                    )
                )["minutes"],
            }
            for n in novels
        ],
        "tiers": list_tiers(),
        "default_tier": project.narrative_tier or DEFAULT_TIER,
        "archive_exists": archive_exists,
        "project_has_corpus": bool(corpus),
        # 续写分支不重选语料，书级事实步骤（book_scope）的选项要按项目已有语料出。
        "project_corpus_ids": corpus,
        # 现有书级事实：作为 book_scope 的默认值，避免用户「跳过」把已设好的书清掉。
        "active_novel_id": project.active_novel_id,
        "continuation_novel_id": project.continuation_novel_id,
        "project_has_style": archive_exists,
        "needs_distill": bool(corpus) and not archive_exists,
        "segments": segments,
        "characters": characters,
        "character_source": character_source,
        # 剧情圣经里的角色：角色卡步骤要列出「谁、什么角色、有没有卡」。
        # 只读投影，不参与任何写入。
        "bible_characters": _bible_characters(project),
        "generation_length": project.generation_length or 3000,
        "project_genre": project.genre,
    }
    # 语料真实章数/字数、已有档案、各档预计耗时、维度清单、模型可用事实清单。
    ctx.update(facts.distill_context(db, project))
    ctx["overrides"] = overrides or {}
    return ctx


def _bible_characters(project: Project) -> list[dict[str, Any]]:
    """剧情圣经里的角色摘要（角色卡步骤的清单/校验来源）。"""
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    out: list[dict[str, Any]] = []
    for item in bible.get("characters") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        card = item.get("card")
        out.append(
            {
                "name": name,
                "role": str(item.get("role") or ""),
                "has_card": isinstance(card, dict) and bool(card.get("声纹")),
            }
        )
    return out


def _card_stats_from_bible(bible: dict) -> dict[str, Any]:
    """由已有 `characters[].card` 反算补卡统计（提交复用时的回执口径）。"""
    chars = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    names: list[str] = []
    missing: list[str] = []
    for c in chars:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        card = c.get("card")
        if isinstance(card, dict) and card.get("声纹"):
            names.append(name)
        else:
            missing.append(name)
    return {
        "generated": len(names),
        "names": names,
        "missing": missing,
        "collisions": 0,
        "reason": "reused" if names else ("no_characters" if not chars else "no_cards"),
    }


def _session_view(session: AssistantWizardSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "kind": session.kind,
        "step_key": session.step_key,
        "status": session.status,
        "answers": session.answers or {},
        "overrides": session.overrides or {},
        "result": session.result or None,
        "can_back": bool(flow.prev_step(session.history or [], session.step_key)),
    }


# ---------------------------------------------------------------------------
# 蒸馏进度与看护（引导卡片里的一句话进度 + 完成/卡住通知）
# ---------------------------------------------------------------------------


def _latest_run(db: Session, project_id: int) -> DistillRun | None:
    return db.scalar(
        select(DistillRun)
        .where(DistillRun.project_id == project_id)
        .order_by(desc(DistillRun.id))
    )


def _notice_view(row: WizardNotice) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "job_id": row.job_id,
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _materialize_stalled(db: Session, run: DistillRun | None) -> None:
    """把「疑似卡住」落成一条持久化通知（同一任务只落一次）。

    为什么是**读时物化**而不是后台定时器：卡住的典型形态就是「后台线程已经
    不干活了」，指望它自己发提醒是不现实的。真正的观察者只能是前端轮询——
    每次轮询顺带问一句「这个任务按现在的判据算不算卡住」，算就落一条通知，
    用户下次（哪怕隔一天）打开助手仍能看到这条提醒。

    `emit_notice` 按 (job_id, kind) 去重，所以重复轮询不会刷屏。
    """
    if run is None or run.status not in ACTIVE_STATUSES:
        return
    health, note = distill_health(run)
    if health != HEALTH_STALLED or not note:
        return
    emit_notice(
        db,
        run,
        "distill_stalled",
        {
            "job_id": run.id,
            "tier": run.tier,
            "status": run.status,
            "stage": run.stage,
            "health": health,
            "message": f"提炼疑似卡住：{note}",
            "progress_current": int(run.progress_current or 0),
            "progress_total": int(run.progress_total or 0),
            "expected_minutes": run.expected_minutes,
            "elapsed_minutes": distill_run_to_dict(run).get("elapsed_minutes"),
            # 这条路**不给「继续蒸馏」按钮**：卡住的任务仍是 running，点续跑会被
            # find_active_run 复用成同一个任务（等于没点）。真正的出口是重启后端：
            # 重启后 reap_stale_runs 把它转成「中断」回执，那条上才有按钮。
            "next": (
                "建议：先按上面的分钟数再等一会。若仍无进度，重启后端——重启会把这条"
                "任务转成「中断」回执，点那条回执上的「继续提炼」接着跑（已完成的块不会重跑）。"
            ),
        },
    )


def _heal_step(
    db: Session,
    session: AssistantWizardSession,
    answers: dict[str, Any],
    ctx: dict[str, Any],
) -> None:
    """旧会话自愈：步骤序列变动后，退回序列里第一个未答步骤。

    语料前置（2026-11-20）后，旧会话可能停在 `genre` 而 `corpus` 还没答。
    只靠 `_state()` 的自愈不够——`answer/say` 会直接用旧 `step_key` 推进，
    从而永久跳过 corpus。所有「读取当前题」的入口都必须先调用本函数。
    """
    if session.status != "active":
        return
    order = flow.step_order(answers.get("branch"), answers, ctx)
    pending = next((s for s in order if s not in answers), None)
    # 只修「结构性插入」的跳过：corpus 前置、book_scope 因多本语料插入。
    # 不做通用回退——用户可能有意跳过剧情追问，硬退回去会打断输入。
    if pending not in ("corpus", "book_scope"):
        return
    if (
        session.step_key in order
        and order.index(pending) < order.index(session.step_key)
    ):
        session.step_key = pending
        db.commit()
        db.refresh(session)


def _state(
    db: Session,
    project: Project,
    session: AssistantWizardSession,
    ack: str | None = None,
) -> dict[str, Any]:
    """当前会话的可渲染状态。

    `ack` 是「答完上一题」的回执：拼在新问题正文前面，让用户先看到
    「好，平衡档…」再读下一题，而不是答完立刻被弹一张新卡。
    """
    ctx = _context(db, project, session.history or [], session.overrides or {})
    answers = session.answers or {}
    question = None
    if session.status == "active":
        # 步骤序列是动态的（例如本轮把 corpus 前置、book_scope 因语料 ≥2 本
        # 插入）。旧会话的 step_key 可能越过一个从未答过的步骤，这里自愈。
        _heal_step(db, session, answers, ctx)
        question = flow.build_question(session.step_key, answers, ctx)
        if ack:
            question["message"] = ack + "\n\n" + str(question.get("message") or "")
    run = _latest_run(db, project.id)
    out = {
        "session": _session_view(session),
        "question": question,
        # 最近一条蒸馏任务（含 health / 已运行分钟 / 预计分钟）。
        # 卡片里只展示「一句话 + 百分比」，数据全在这里，刷新/断线重连后拿到的
        # 就是同一份 DB 事实，不靠前端内存续命。
        "distill": distill_run_to_dict(run) if run is not None else None,
    }
    if ack:
        out["ack"] = ack
    # 角色卡步骤：把刚生成（或用户改过）的卡作为预览交给前端，
    # 复用结果气泡里已有的编辑/重生成/移除 UI。
    if session.status == "active" and session.step_key == "cards":
        bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
        if bible.get("characters"):
            out["cards_preview"] = {
                "kind": flow.KIND_ORIGINAL,
                "stage": "cards",
                "applied": [],
                "message": "",
                "bible": bible,
                "character_cards": _card_stats_from_bible(bible),
            }
    return out


# ---------------------------------------------------------------------------
# 接口
# ---------------------------------------------------------------------------


@router.get("/{project_id}")
def get_state(project_id: int, db: Session = Depends(get_db)) -> dict:
    """恢复引导：没有会话时返回 session=null，前端展示干净的空对话。"""
    project = _require_project(db, project_id)
    session = _any_session(db, project_id)
    run = _latest_run(db, project.id)
    distill = distill_run_to_dict(run) if run is not None else None
    if session is None or session.status == "aborted":
        return {
            "session": None,
            "question": None,
            "distill": distill,
            "project_hint": _project_hint(project),
        }
    return _state(db, project, session)


@router.get("/{project_id}/messages")
def get_messages(
    project_id: int, limit: int = 200, db: Session = Depends(get_db)
) -> dict:
    """对话记录（跨退出存活）：前端进入助手时用它重建历史气泡。

    `GET /{project_id}` 只给「当前停在哪一题」，本接口给「之前说过什么」。
    两者拼起来才是完整对话：历史（本接口，全部只读）+ 当前题卡（state）。

    只读，不改任何状态。`limit` 取尾部 N 条（`list_messages` 内部会夹到 1~500）。
    """
    _require_project(db, project_id)
    rows = list_messages(db, project_id, limit)
    return {
        "items": [
            {
                "id": r.id,
                "session_id": r.session_id,
                "role": r.role,
                "kind": r.kind,
                "text": r.text or "",
                "payload": r.payload or {},
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "count": len(rows),
    }


class AppendMessageIn(StrictRequest):
    """追加一条对话消息（目前只给「章节卡」与只读通知用）。

    正文不写进 `payload`：`chapter` 卡只存 `generation_id / segment_no /
    chapter_range / chars`，渲染时从 `generations` 读，避免同一段正文存两份。
    """

    role: str = "assistant"
    kind: str = "plain"
    text: str = ""
    payload: dict[str, Any] | None = None
    dedupe_key: str | None = None


@router.post("/{project_id}/messages")
def append_message(
    project_id: int, body: AppendMessageIn, db: Session = Depends(get_db)
) -> dict:
    """追加一条消息（幂等：同 `dedupe_key` 重复提交返回原行）。"""
    _require_project(db, project_id)
    if body.role not in ("user", "assistant"):
        raise HTTPException(status_code=422, detail="role 只接受 user/assistant")
    if body.kind not in ("plain", "chapter", "notice"):
        raise HTTPException(
            status_code=422, detail="kind 只接受 plain/chapter/notice"
        )
    row = record_message(
        db,
        project_id,
        role=body.role,
        kind=body.kind,
        text=body.text,
        payload=body.payload,
        dedupe_key=body.dedupe_key,
    )
    if row is None:
        raise HTTPException(status_code=500, detail="消息落库失败")
    return {
        "id": row.id,
        "session_id": row.session_id,
        "role": row.role,
        "kind": row.kind,
        "text": row.text or "",
        "payload": row.payload or {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/{project_id}/notices")
def list_notices(project_id: int, db: Session = Depends(get_db)) -> dict:
    """未消费的提炼通知 + 最近一条任务状态（一次轮询同时更新进度和回执）。

    这就是「完成唤醒」的**可运行机制**：任务终态时后台把回执写进
    `wizard_notices`；前端每 3 秒拉这个接口，拿到未消费的行就渲染成助手气泡，
    然后调 ack 标掉。服务重启后未消费的行仍在库里，下次进助手立刻补发。

    读时还会顺带做一次「疑似卡住」判定（`_materialize_stalled`），所以卡住提醒
    不依赖后台线程还活着。
    """
    _require_project(db, project_id)
    run = _latest_run(db, project_id)
    _materialize_stalled(db, run)
    rows = (
        db.scalars(
            select(WizardNotice)
            .where(
                WizardNotice.project_id == project_id,
                WizardNotice.consumed_at.is_(None),
            )
            .order_by(WizardNotice.id)
        )
        .all()
    )
    return {
        "items": [_notice_view(r) for r in rows],
        "distill": distill_run_to_dict(run) if run is not None else None,
    }


@router.post("/{project_id}/notices/{notice_id}/ack")
def ack_notice(
    project_id: int, notice_id: int, db: Session = Depends(get_db)
) -> dict:
    """标记通知已消费（前端渲染完就 ack，避免下次进来重复弹）。"""
    row = db.get(WizardNotice, notice_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="notice 不存在")
    if row.consumed_at is None:
        row.consumed_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True, "id": row.id}


@router.post("/start")
def start(body: WizardStartIn, db: Session = Depends(get_db)) -> dict:
    """开始/恢复一次引导。重复调用默认复用进行中的会话，restart=True 才重来。"""
    project = _require_project(db, body.project_id)
    if body.restart:
        for old in (
            db.query(AssistantWizardSession)
            .filter(
                AssistantWizardSession.project_id == project.id,
                AssistantWizardSession.status == "active",
            )
            .all()
        ):
            old.status = "aborted"
        db.commit()
    else:
        existing = _active_session(db, project.id)
        if existing is not None:
            return _state(db, project, existing)

    session = AssistantWizardSession(
        project_id=project.id,
        kind=None,
        step_key=flow.FIRST_STEP,
        answers={},
        history=[],
        status="active",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    out = _state(db, project, session)
    _record_question(db, project.id, session, out.get("question"))
    return out


@router.post("/{project_id}/answer")
def answer(
    project_id: int,
    body: WizardAnswerIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    project = _require_project(db, project_id)
    session = _active_session(db, project_id)
    if session is None:
        raise HTTPException(status_code=409, detail="没有进行中的引导，请先调用 /start")

    answers = dict(session.answers or {})
    ctx = _context(db, project, session.history or [], session.overrides or {})
    # 先自愈再校验：旧会话可能停在「题材」而新的序列把语料放在前面。
    _heal_step(db, session, answers, ctx)

    if body.step_key != session.step_key:
        # 页面停留在旧题（另一个标签页已经答过）：不覆盖，直接把当前题推回去。
        raise HTTPException(
            status_code=409,
            detail=f"当前问题已变为「{session.step_key}」，请刷新后重答",
        )

    # 只为「用户气泡显示什么」重建一次当前题（纯内存，不查库）。
    question = flow.build_question(session.step_key, answers, ctx)

    if body.skip:
        if not question.get("can_skip"):
            raise HTTPException(status_code=400, detail="这一步不能跳过")
        value = flow.default_for(session.step_key, answers, ctx)
    else:
        value = body.value

    try:
        value = flow.validate(session.step_key, value, answers.get("branch"), answers, ctx)
    except flow.AnswerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    echo = _choice_label(question, value)
    if session.step_key == "confirm":
        if value is not True:
            raise HTTPException(status_code=400, detail="确认卡片只能选择开始")
        ack = flow.ack_for(session.step_key, value, answers, ctx)
        return _advance(
            db, project, session, session.step_key, value, background_tasks, ack, echo=echo
        )

    ack = flow.ack_for(session.step_key, value, answers, ctx)
    return _advance(
        db, project, session, session.step_key, value, background_tasks, ack, echo=echo
    )


# ---------------------------------------------------------------------------
# 对话记录（跨退出存活）
# ---------------------------------------------------------------------------


def _record_question(
    db: Session,
    project_id: int,
    session: AssistantWizardSession,
    question: dict | None,
) -> None:
    """把「刚推到用户面前的那张题卡」写进对话记录。

    幂等键用 `(会话, 步骤, 已走步数)`：回退/跳回同一题时不重复记一张卡，
    但换了位置再看到同一题（步数不同）会正常记。
    """
    if not question:
        return
    record_message(
        db,
        project_id,
        role="assistant",
        kind="wizard_question",
        text=str(question.get("message") or ""),
        payload={"step": session.step_key, "question": question},
        session_id=session.id,
        dedupe_key=f"wq:{session.id}:{session.step_key}:{len(session.history or [])}",
    )


def _record_reply(
    db: Session,
    project_id: int,
    session: AssistantWizardSession,
    text: str,
    *,
    kind: str = "plain",
    payload: dict | None = None,
) -> None:
    record_message(
        db,
        project_id,
        role="assistant",
        kind=kind,
        text=text,
        payload=payload,
        session_id=session.id,
    )


def _record_user(
    db: Session,
    project_id: int,
    session: AssistantWizardSession,
    text: str,
    *,
    kind: str = "plain",
) -> None:
    record_message(
        db, project_id, role="user", kind=kind, text=text, session_id=session.id
    )


def _choice_label(question: dict | None, value: Any) -> str:
    """把用户点的选项还原成人话（用于对话记录里的用户气泡）。"""
    if isinstance(value, str) and value:
        return value
    for opt in (question or {}).get("options") or []:
        if opt.get("value") == value:
            return str(opt.get("label") or "")
    return "（已选择）"


def _advance(
    db: Session,
    project: Project,
    session: AssistantWizardSession,
    step: str,
    value: Any,
    background_tasks: BackgroundTasks,
    ack: str,
    echo: str | None = None,
    prefill: dict[str, Any] | None = None,
    prefill_tags: list[str] | None = None,
) -> dict:
    """把一次作答写进会话并推进到下一题（卡片点击与自由输入共用）。

    `echo` 是用户气泡要显示的话：点卡片时是选项文案，自由输入时由调用方
    先单独记录原话（传 None 表示「用户消息已记过，这里不再重复」）。
    `prefill` 是同一句话里带出的**其他步骤**答案，写入 `_auto`，后续跳过。
    """
    if echo is not None:
        _record_user(db, project.id, session, echo, kind="choice")

    if step == "confirm":
        out = _commit(db, project, session, background_tasks)
        if out.get("result"):
            out["result"]["message"] = ack + str(out["result"].get("message") or "")
        out["ack"] = ack
        _record_reply(
            db,
            project.id,
            session,
            str((out.get("result") or {}).get("message") or ack),
            kind="wizard_result",
            payload={"result": out.get("result")},
        )
        return out

    answers = dict(session.answers or {})
    old_genre = answers.get("genre")
    answers[step] = value
    if step == "branch":
        session.kind = value
        # 换方向等于换一套问题：清掉另一条分支的答案，避免汇总串味。
        answers = {"branch": value}
    # 用户显式答过这一步，就不再算「一句话预填」，避免回退后又被跳过。
    auto = {s for s in (answers.get("_auto") or []) if s != step}
    # 题材变了：之前由一句话预填的题材条件项（世界观/金手指等）已经过期，
    # 清掉让新题材的追问重新问一遍；用户显式答过的项不在此列。
    if step == "genre" and old_genre and old_genre != value:
        for key in story.STORY_STEPS:
            if key != "genre" and key in auto:
                answers.pop(key, None)
                auto.discard(key)
    if auto:
        answers["_auto"] = sorted(auto)
    else:
        answers.pop("_auto", None)
    if prefill:
        _apply_prefill(answers, prefill, prefill_tags or [], step)

    history = list(session.history or [])
    history.append(step)
    session.answers = answers
    session.history = history

    ctx = _context(db, project, history, session.overrides or {})
    # 条件步骤会因上游答案变化而失效（例如换了一本非仙侠的书）：清掉旧答案，
    # 避免提交时还给不适用力量体系的题材生成体系。
    if "system" in answers and "system" not in flow.step_order(session.kind, answers, ctx):
        answers.pop("system", None)
        session.answers = answers
    # 语料一选定就落库：紧随其后的 distill 步骤要立即用它启动后台提炼，
    # `start_distill` 读的是项目字段而不是会话 answers。
    if step == "corpus" and session.kind == flow.KIND_ORIGINAL:
        project.reference_novel_ids = flow._corpus_ids(answers, ctx)

    nxt = flow.next_step(session.kind, step, answers, ctx)
    if nxt is None:
        # 理论上只有 confirm 会走到末尾，而 confirm 已在上面处理。
        raise HTTPException(status_code=500, detail="引导流程没有下一步")
    session.step_key = nxt
    db.commit()
    db.refresh(session)

    # 进入角色卡步骤：先把剧情圣经和角色卡生成并落库，_state 随后把它们
    # 作为 cards_preview 交给前端（复用已有的卡编辑/重生成 UI）。
    # 已经有角色时不重建，保留用户在卡步骤里做过的修改。
    if nxt == "cards" and not _bible_characters(project):
        _build_bible(db, project, answers)
        db.refresh(project)
        db.refresh(session)

    # 蒸馏不再等到最后提交：答完档位就后台开跑，用户继续答后面的题。
    # 失败只记日志 + 回执提示，不让一次后台任务把作答打成 500。
    distill_note = ""
    if step == "distill":
        tier = str(value or "").strip()
        if tier and tier != flow.DISTILL_REUSE:
            project.narrative_tier = tier
            try:
                db.commit()
                _start_distill(db, project, tier, background_tasks)
                distill_note = "\n\n（蒸馏已在后台开始，继续答题即可。）"
            except Exception as exc:  # noqa: BLE001 —— 蒸馏启动失败不阻断引导
                logger.warning(
                    "引导内提前启动蒸馏失败 project=%s tier=%s: %s",
                    project.id, tier, exc,
                )
                distill_note = "\n\n（蒸馏没能立即启动，可稍后在工作台重新发起。）"

    out = _state(db, project, session, ack=ack + distill_note)
    _record_question(db, project.id, session, out.get("question"))
    return out


@router.post("/{project_id}/say")
def say(
    project_id: int,
    body: WizardSayIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """用户直接打字：可能是作答、追问，也可能没听懂。

    追问**不推进**流程（返回 mode=explain，当前题原样留着），
    这是「对话流」与「一问一卡」的关键差别。
    """
    project = _require_project(db, project_id)
    session = _active_session(db, project_id)
    if session is None:
        raise HTTPException(status_code=409, detail="没有进行中的引导，请先调用 /start")

    answers = dict(session.answers or {})
    ctx = _context(db, project, session.history or [], session.overrides or {})
    _heal_step(db, session, answers, ctx)
    step = session.step_key
    question = flow.build_question(step, answers, ctx)

    # 越界要求先明确拒绝：不执行、不推进，也不写进 premise。
    refused = chat.refusal(body.text)
    if refused:
        _record_user(db, project.id, session, body.text)
        _record_reply(db, project.id, session, refused)
        return {
            "mode": "clarify",
            "message": refused,
            "question": question,
            "session": _session_view(session),
        }

    # 长文本至少有落点：语料/题材/风格/字数/段数/大纲这些步骤，追加进
    # project.premise（既有字段，生成链路会读）；追问不写。
    premise_saved = (
        step in _PREMISE_STEPS
        and chat.is_long_text(body.text)
        and not chat.looks_like_question(body.text)
        and _capture_premise(db, project, body.text)
    )
    long_note = _LONG_TEXT_NOTE if premise_saved else ""

    # 先看是不是「帮我改 X」：档位、采样章数、字数、题材、风格、大纲、流程。
    # 动作不推进引导步骤，只改项目/会话配置，并返回可撤销的审计 id。
    act = actions.plan(body.text, ctx, step)
    if act:
        try:
            res = actions.apply(db, project, session, act, ctx, background_tasks)
        except (ValueError, flow.AnswerError) as exc:
            _record_user(db, project.id, session, body.text)
            _record_reply(db, project.id, session, str(exc) + long_note)
            return {
                "mode": "clarify",
                "message": str(exc) + long_note,
                "question": question,
                "session": _session_view(session),
            }
        out = _state(db, project, session)
        _record_user(db, project.id, session, body.text)
        _record_reply(
            db,
            project.id,
            session,
            res["message"] + long_note,
            kind="wizard_action",
            payload={
                "action": res.get("action"),
                "undo_id": res.get("undo_id"),
                "undoable": res.get("undoable", False),
            },
        )
        _record_question(db, project.id, session, out.get("question"))
        return {
            "mode": "action",
            "message": res["message"] + long_note,
            "action": res.get("action"),
            "undo_id": res.get("undo_id"),
            "undoable": res.get("undoable", False),
            "question": out.get("question"),
            "session": out["session"],
        }

    parsed = chat.parse(step, body.text, answers, ctx, question)

    if parsed["kind"] == "bypass":
        # 用户明确说「按默认来 / 别问了」：当前及后续步骤按默认补齐，直达确认页。
        # 同句里已经给出的信息仍然优先落库，不因为「别问了」被默认值覆盖。
        prefill, tags = _story_prefill(step, body.text, answers)
        _apply_prefill(answers, prefill, tags, step)
        filled = _fill_defaults(answers, ctx, project, step)
        history = list(session.history or [])
        history.append(step)
        session.answers = answers
        session.history = history
        order = flow.step_order(answers.get("branch"), answers, ctx)
        # 「按默认来」也不能把角色卡步骤静默跳过：先出大纲时停下来让用户过目。
        session.step_key = "cards" if "cards" in order else "confirm"
        db.commit()
        db.refresh(session)
        if session.step_key == "cards" and not _bible_characters(project):
            _build_bible(db, project, answers)
            db.refresh(project)
            db.refresh(session)
        labels = "、".join(story.STORY_LABELS.get(k, k) for k in filled) or "后续参数"
        ack = f"好，不逐题问了：{labels}按默认补齐，之后在确认页还能逐项改。"
        out = _state(db, project, session, ack=ack)
        _record_user(db, project.id, session, body.text)
        _record_reply(db, project.id, session, ack)
        _record_question(db, project.id, session, out.get("question"))
        return {"mode": "answered", **out}

    if parsed["kind"] == "question":
        message = chat.explain(step, body.text, answers, ctx, question)
        _record_user(db, project.id, session, body.text)
        _record_reply(db, project.id, session, message)
        return {
            "mode": "explain",
            "message": message,
            "question": question,
            "session": _session_view(session),
        }

    if parsed["kind"] == "clarify":
        _record_user(db, project.id, session, body.text)
        _record_reply(db, project.id, session, parsed["message"] + long_note)
        return {
            "mode": "clarify",
            "message": parsed["message"] + long_note,
            "question": question,
            "session": _session_view(session),
        }

    if parsed["kind"] == "back":
        history = list(session.history or [])
        prev = flow.prev_step(history, step)
        if prev is None:
            message = "这已经是第一步了，没有更早的可退。你直接答这一题就行。"
            _record_user(db, project.id, session, body.text)
            _record_reply(db, project.id, session, message)
            return {
                "mode": "clarify",
                "message": message,
                "question": question,
                "session": _session_view(session),
            }
        session.step_key = prev
        session.history = history[:-1] if history else history
        db.commit()
        db.refresh(session)
        out = _state(db, project, session, ack="好，退回上一步，之前选的还在。")
        _record_user(db, project.id, session, body.text)
        _record_reply(db, project.id, session, "好，退回上一步，之前选的还在。")
        _record_question(db, project.id, session, out.get("question"))
        return {"mode": "answered", **out}

    value = parsed["value"]
    try:
        value = flow.validate(step, value, answers.get("branch"), answers, ctx)
    except flow.AnswerError as exc:
        message = f"{exc}。换个说法，或者直接点上面的卡片。" + long_note
        _record_user(db, project.id, session, body.text)
        _record_reply(db, project.id, session, message)
        return {
            "mode": "clarify",
            "message": message,
            "question": question,
            "session": _session_view(session),
        }

    ack = flow.ack_for(step, value, answers, ctx)
    if parsed.get("note"):
        ack = ack + str(parsed["note"])
    # 同一句话里带出的其他步骤信息：一次性记下，并告诉用户还差什么。
    prefill, tags = _story_prefill(step, body.text, answers)
    if prefill or tags:
        preview = dict(answers)
        preview[step] = value
        _apply_prefill(preview, prefill, tags, step)
        cap = dict(prefill)
        if tags:
            cap["genre_tags"] = tags
        if step == "genre":
            cap["genre"] = value
        captured = story.captured_summary(cap)
        if captured:
            ack = ack + " " + captured
        ack = ack + " " + story.remaining_summary(preview, tuple(flow.GENRE_HINTS))
    ack = ack + long_note
    # 用户原话先落库（自由输入保留原话，比只记选项文案更有信息量）；
    # 传 echo=None 告诉 `_advance`「用户消息已经记过，别重复记」。
    _record_user(db, project.id, session, body.text)
    out = _advance(
        db,
        project,
        session,
        step,
        value,
        background_tasks,
        ack,
        echo=None,
        prefill=prefill,
        prefill_tags=tags,
    )
    out["echo"] = parsed.get("label") or ack
    return {"mode": "answered", **out}


@router.post("/{project_id}/back")
def back(project_id: int, db: Session = Depends(get_db)) -> dict:
    project = _require_project(db, project_id)
    session = _active_session(db, project_id)
    if session is None:
        raise HTTPException(status_code=409, detail="没有进行中的引导")

    history = list(session.history or [])
    prev = flow.prev_step(history, session.step_key)
    if prev is None:
        raise HTTPException(status_code=400, detail="已经是第一步")

    session.step_key = prev
    session.history = history[:-1] if history else history
    # 回退不删答案：卡片会带回上次的选择，用户可以直接改。
    db.commit()
    db.refresh(session)
    out = _state(db, project, session)
    _record_question(db, project.id, session, out.get("question"))
    return out


@router.post("/{project_id}/goto")
def goto(project_id: int, body: WizardGotoIn, db: Session = Depends(get_db)) -> dict:
    """从汇总卡片跳回某一步（只允许跳回已经走过的步骤）。"""
    project = _require_project(db, project_id)
    session = _active_session(db, project_id)
    if session is None:
        raise HTTPException(status_code=409, detail="没有进行中的引导")

    history = list(session.history or [])
    if body.step_key not in history:
        raise HTTPException(status_code=400, detail="只能跳回已经答过的步骤")
    idx = history.index(body.step_key)
    session.step_key = body.step_key
    session.history = history[:idx]
    db.commit()
    db.refresh(session)
    out = _state(db, project, session)
    _record_question(db, project.id, session, out.get("question"))
    return out


@router.post("/{project_id}/commit")
def commit(
    project_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """确认卡片的独立入口（与 answer(confirm) 等价，方便前端重试）。"""
    project = _require_project(db, project_id)
    session = _active_session(db, project_id)
    if session is None:
        raise HTTPException(status_code=409, detail="没有进行中的引导")
    out = _commit(db, project, session, background_tasks)
    _record_reply(
        db,
        project.id,
        session,
        str((out.get("result") or {}).get("message") or ""),
        kind="wizard_result",
        payload={"result": out.get("result")},
    )
    return out


def _project_hint(project: Project) -> dict:
    return {
        "id": project.id,
        "title": project.title,
        "genre": project.genre,
        "style": project.style,
        "generation_length": project.generation_length or 3000,
        "has_corpus": bool(project.reference_novel_ids),
        "has_style_archive": bool(project.style_narrative),
    }


def _require_continuation_corpus(db: Session, novel_id: int) -> None:
    """续写书必须先有正文语料，否则文风档案与续写上下文都无从谈起。

    `novel_chunks` 是提炼与 RAG 的唯一语料源；为空时提炼会整批失败，
    用户只会看到「提炼完成但 0 条规则」。所以在写字段前拦下，并把
    「先去导入语料」这一步说清楚。
    """
    novel = db.get(RawNovel, novel_id)
    chunks = db.scalar(
        select(func.count()).select_from(NovelChunk).where(NovelChunk.novel_id == novel_id)
    ) or 0
    if chunks:
        return
    title = (novel.title if novel is not None else "") or f"#{novel_id}"
    raise HTTPException(
        status_code=409,
        detail=(
            f"《{title}》还没有导入正文（novel_chunks 为空），无法生成文风档案，"
            "续写也拿不到原文上下文。请先在语料库导入这本书的章节，再回来选续写。"
        ),
    )


def _commit(
    db: Session,
    project: Project,
    session: AssistantWizardSession,
    background_tasks: BackgroundTasks,
) -> dict:
    """把选择写进现有参数，并按分支启动现有链路。"""
    answers = dict(session.answers or {})
    kind = answers.get("branch")
    result: dict[str, Any] = {"kind": kind, "stage": "ready", "applied": []}
    distill: dict | None = None

    if kind == flow.KIND_ORIGINAL:
        # 只在真的答过 corpus 时覆盖：旧会话没有这一项，不能把项目已有语料清空。
        if "corpus" in answers:
            project.reference_novel_ids = list(answers.get("corpus") or [])
        project.genre = answers.get("genre") or project.genre
        project.style = answers.get("style") or project.style
        project.generation_length = int(answers.get("length") or project.generation_length or 3000)
        # 故事问答落到既有字段：premise 追加结构化简报（生成链路会读），
        # target_length 记全书规模。不新建表字段。
        brief = _story_brief(answers)
        block = ""
        if brief:
            block = "【创作问答】\n" + "\n".join(f"{k}：{v}" for k, v in brief.items())
            old = (project.premise or "").strip()
            if block not in old:
                project.premise = f"{old}\n\n{block}".strip() if old else block
        scale_sig = story.read_signals(
            str(answers.get("scale") or ""), tuple(flow.GENRE_HINTS)
        )
        book_length = scale_sig.get("book_length")
        # 书级事实：只有真的问了这一步（≥2 本在场）才写，避免单本书时改动用户
        # 在工作台手工设过的值。原创分支声明「事实来源书」时顺手清掉续写书：
        # book_for_project 的优先级是 continuation → active，留着旧续写书会把
        # 刚选的事实来源书整个盖掉。
        if "book_scope" in answers:
            scope = int(answers.get("book_scope") or flow.BOOK_SCOPE_NONE)
            project.active_novel_id = scope or None
            project.continuation_novel_id = None
        tier = answers.get("distill")
        if tier and tier != flow.DISTILL_REUSE:
            project.narrative_tier = tier
        if book_length:
            project.target_length = int(book_length)
        db.commit()

        if tier and tier != flow.DISTILL_REUSE:
            distill = _start_distill(db, project, tier, background_tasks)
        result["applied"] = ["reference_novel_ids", "genre", "style", "generation_length"]
        if block:
            result["applied"].append("premise")
        if book_length:
            result["applied"].append("target_length")
        if "book_scope" in answers:
            result["applied"] += ["active_novel_id", "continuation_novel_id"]
        if tier and tier != flow.DISTILL_REUSE:
            result["applied"].append("narrative_tier")

        if answers.get("outline", True):
            result["stage"] = "outline"
            existing = project.plot_bible if isinstance(project.plot_bible, dict) else {}
            if existing.get("characters"):
                # 角色卡步骤已经生成过（可能含手工修改）：只补世界观背景，
                # 不再重建大纲，避免把用户确认过的卡覆盖掉。
                _apply_world_background(db, project, answers)
                bible = project.plot_bible
                card_stats = _card_stats_from_bible(bible)
            else:
                bible, card_stats = _build_bible(db, project, answers)
            result["bible"] = bible
            result["character_cards"] = card_stats
        result["distill"] = distill
        _maybe_generate_power_system(db, project, answers, result)
        result["message"] = _commit_message(
            kind, distill, result["stage"], result.get("character_cards"))
        _append_power_system_message(result)

    elif kind == flow.KIND_CONTINUATION:
        project.generation_length = int(answers.get("length") or project.generation_length or 3000)
        if "book_scope" in answers:
            scope = int(answers.get("book_scope") or flow.BOOK_SCOPE_NONE)
            if scope:
                # 续写书必须先有语料：否则蒸馏必然 0 产出、档案也读不到数据源。
                _require_continuation_corpus(db, scope)
                # 文风档案的数据源是 reference_novel_ids（见 style/pipeline.py 与
                # style/narrative.py）。续写书不并进去，续写项目就永远没有档案。
                # 只追加、不覆盖：用户在工作台手工设的其他参考书原样保留。
                refs = [int(x) for x in (project.reference_novel_ids or [])]
                if scope not in refs:
                    refs.append(scope)
                project.reference_novel_ids = refs
            project.continuation_novel_id = scope or None
        tier = answers.get("distill")
        if tier and tier != flow.DISTILL_REUSE and not project.style_narrative:
            project.narrative_tier = tier
            db.commit()
            distill = _start_distill(db, project, tier, background_tasks)
        else:
            db.commit()
        result["applied"] = ["generation_length"]
        if "book_scope" in answers:
            result["applied"].append("continuation_novel_id")
            if project.continuation_novel_id:
                result["applied"].append("reference_novel_ids")
        result["distill"] = distill
        _maybe_generate_power_system(db, project, answers, result)
        result["message"] = _commit_message(kind, distill, "ready")
        _append_power_system_message(result)

    elif kind == flow.KIND_ROLE:
        result["stage"] = "role"
        result["character"] = answers.get("character")
        result["scene"] = answers.get("scene") or "当前剧情时间线"
        result["message"] = f"已进入与「{result['character']}」的对话。直接说话即可，我会按角色设定回答。"

    else:
        raise HTTPException(status_code=400, detail="还没有选择方向")

    # 本轮自定义问题（字段已在 wizard_flow 白名单校验过）落到项目真实字段。
    for item in (session.overrides or {}).get("custom") or []:
        if item.get("field") == "premise":
            value = str(answers.get(item.get("key")) or "").strip()
            if value:
                project.premise = value
                result["applied"].append("premise")

    session.status = "committed"
    session.result = result
    db.commit()
    db.refresh(session)
    return {
        "session": _session_view(session),
        "question": None,
        "done": True,
        "result": result,
    }


def _commit_message(
    kind: str,
    distill: dict | None,
    stage: str,
    cards: dict | None = None,
) -> str:
    parts = []
    if kind == flow.KIND_CONTINUATION:
        parts.append("已按你的选择保存续写参数。")
    else:
        parts.append("已按你的选择保存项目参数。")
    if distill:
        if distill.get("from_cache"):
            parts.append("文风档案直接命中缓存，没有重新提炼。")
        else:
            parts.append(
                f"提炼任务已提交（job #{distill.get('job_id')}），"
                "后台跑完才能开始生成；可以在项目工作台看进度。"
            )
    if cards:
        generated = int(cards.get("generated") or 0)
        if cards.get("reason") == "reused":
            parts.append(f"沿用你在角色卡步骤确认过的 {generated} 张卡，没有重复生成。")
        elif generated:
            extra = ""
            if int(cards.get("collisions") or 0):
                extra = f"（有 {cards['collisions']} 处声纹撞车，已在卡里标出）"
            parts.append(
                f"已为 {generated} 个角色生成角色卡：身份/外貌/说话方式/口头禅{extra}。")
        elif cards.get("reason") == "already_have_cards":
            parts.append("角色卡已存在，未重复生成。")
        elif cards.get("reason") == "no_characters":
            pass
        else:
            parts.append("角色卡这次没生成成功（不影响大纲，可稍后重试）。")
    if stage == "outline":
        parts.append("大纲已经生成，确认后再开写。")
    elif stage == "ready":
        parts.append("点下面的按钮就开始写。")
    return "".join(parts)


def _start_distill(db: Session, project: Project, tier: str, bg: BackgroundTasks) -> dict:
    """直接复用 `POST /projects/{id}/distill` 的实现（含并发防重/缓存预测）。"""
    from app.routers.projects import DistillStartRequest, start_distill

    return start_distill(project.id, DistillStartRequest(tier=tier), bg, db)


@router.get("/{project_id}/actions")
def list_actions(project_id: int, db: Session = Depends(get_db)) -> dict:
    """本项目的写操作审计（谁、哪一步、动作、旧值、新值、时间、是否已撤销）。"""
    _require_project(db, project_id)
    return {"items": actions.history(db, project_id)}


@router.post("/{project_id}/undo")
def undo_action(
    project_id: int,
    body: WizardUndoIn,
    db: Session = Depends(get_db),
) -> dict:
    """撤销一次写操作：不传 audit_id 就撤最近一次可撤销的。"""
    project = _require_project(db, project_id)
    session = _active_session(db, project_id)
    if session is None:
        raise HTTPException(status_code=409, detail="没有进行中的引导")
    if body.audit_id is not None:
        res = actions.undo_by_id(db, project, session, body.audit_id)
    else:
        res = actions.undo_last(db, project, session)
    out = _state(db, project, session)
    _record_reply(
        db,
        project.id,
        session,
        res["message"],
        kind="wizard_action",
        payload={"action": res.get("action"), "undo_id": None, "undoable": False},
    )
    _record_question(db, project.id, session, out.get("question"))
    return {
        "mode": "action",
        "message": res["message"],
        "action": res.get("action"),
        "undo_id": None,
        "undoable": False,
        "question": out.get("question"),
        "session": out["session"],
    }


def _apply_world_background(
    db: Session, project: Project, answers: dict[str, Any]
) -> None:
    """把向导的 `world / power / scale` 答案补进 `plot_bible.background`。

    只补空、不覆盖作者手写：世界观卡一旦有「一句话世界 / 硬规则」，向导答案
    不再写入。`build_bible` 已经 commit 过一次，这里单独再提交一次。
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.generation.background import normalize_background

    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    background = normalize_background(bible.get("background"))

    world = str(answers.get("world") or "").strip()
    changed = False
    if world and not background.get("一句话世界"):
        background["一句话世界"] = world[:300]
        changed = True

    extras = []
    for key, label in (("power", "力量体系"), ("scale", "世界规模")):
        value = str(answers.get(key) or "").strip()
        if value:
            extras.append(f"{label}：{value}")
    if extras and not background.get("硬规则"):
        background["硬规则"] = extras
        changed = True

    if not changed:
        return
    bible["background"] = background
    project.plot_bible = bible
    try:
        flag_modified(project, "plot_bible")
    except Exception:  # noqa: BLE001 —— 测试里的假 project 没有 ORM 状态
        pass
    db.commit()


def _maybe_generate_power_system(
    db: Session,
    project: Project,
    answers: dict[str, Any],
    result: dict,
) -> None:
    """按向导答案生成力量体系（原创/续写共用；未勾选或失败都不阻断提交）。"""
    if not answers.get("system"):
        return
    try:
        ps = _generate_and_attach_power_system(db, project, answers)
    except Exception as exc:  # noqa: BLE001 —— 增强项，不能拖垮提交
        ps = {"ok": False, "detail": f"力量体系生成失败：{exc}"[:200]}
    result["power_system"] = ps
    if ps.get("ok"):
        result["applied"].append("power_system")


def _append_power_system_message(result: dict) -> None:
    ps = result.get("power_system")
    if not ps:
        return
    if ps.get("ok") and not ps.get("skipped"):
        result["message"] = (result.get("message") or "") + (
            f"\n力量体系：{ps.get('systems', 0)} 套体系、"
            f"{ps.get('tiers', 0)} 层档位已保存，可在项目资产里修改。"
        )
    elif not ps.get("ok"):
        result["message"] = (result.get("message") or "") + (
            f"\n力量体系未生成：{ps.get('detail') or '模型未返回可用结果'}"
            "（可稍后在项目资产 → 力量体系里补）。"
        )


def _generate_and_attach_power_system(
    db: Session,
    project: Project,
    answers: dict[str, Any],
) -> dict:
    """按向导答案生成力量体系与档位，写进 `plot_bible`（只在此处落库）。

    为什么放在提交而不是某一步：体系和档位各要一次模型调用，放在问答中途
    会把「点下一步」变成长时间等待；提交时一次跑完，失败也只降级成一条提示。
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.generation.power_system import normalize_power_system
    from app.generation.power_system_gen import (
        _SYSTEM_PROMPT, build_system_prompt, build_tier_prompt,
        filter_tiers_by_bands, parse_systems, parse_tiers, retry_instructions,
        system_quality_issues,
    )
    from app.llm.provider import get_llm_provider

    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    if bible.get("power_system"):
        systems = (bible.get("power_system") or {}).get("体系") or []
        return {"ok": True, "skipped": True,
                "systems": len(systems), "tiers": len(bible.get("tier_layers") or [])}

    note = "\n".join(
        str(x).strip() for x in (
            answers.get("world"), answers.get("power"), answers.get("premise"),
        ) if str(x or "").strip()
    )
    genre = project.genre or str(answers.get("genre") or "")
    # 续写分支没有「题材」这一步：从被续写的书里取题材与书名补进上下文。
    if project.continuation_novel_id:
        from app.models import RawNovel

        novel = db.get(RawNovel, int(project.continuation_novel_id))
        if novel is not None:
            genre = genre or (novel.genre or "")
            if not genre:
                from app.wizard_flow import GENRE_HINTS
                from app.wizard_story import detect_genres

                hits = detect_genres(str(novel.title or ""), tuple(GENRE_HINTS))
                genre = hits[0] if hits else ""
            note = f"续写《{novel.title or ''}》" + (f"\n{note}" if note else "")
    prompt = build_system_prompt(genre, project.style or "", note)
    provider = get_llm_provider()

    raw = provider.chat(_SYSTEM_PROMPT, prompt)
    systems = parse_systems(raw)
    issues = system_quality_issues(systems)
    if issues:
        raw = provider.chat(_SYSTEM_PROMPT, prompt + retry_instructions(issues, ""))
        systems = parse_systems(raw)
        issues = system_quality_issues(systems)
    if not systems:
        return {"ok": False, "detail": "模型没有给出可用体系（可重试）"}

    tiers: list[dict] = []
    try:
        tier_raw = provider.chat("你为群像设计档位分层。", build_tier_prompt(systems[0]))
        tiers = filter_tiers_by_bands(parse_tiers(tier_raw), systems[0])
    except Exception:  # noqa: BLE001 —— 档位是增强项，体系已经可用
        tiers = []

    bible = dict(bible)
    bible["power_system"] = normalize_power_system({"体系": systems})
    if tiers:
        bible["tier_layers"] = tiers
    project.plot_bible = bible
    flag_modified(project, "plot_bible")
    db.commit()
    return {"ok": True, "systems": len(systems), "tiers": len(tiers)}


def _build_bible(
    db: Session,
    project: Project,
    answers: dict[str, Any] | None = None,
) -> tuple[dict, dict]:
    """复用现有 build-bible（含无 LLM 时的离线兜底），并给角色补卡。

    把引导里的故事问答作为「创作者问答」传进去：大纲要按用户刚说的题材、
    写法、核心冲突、主角、世界约束、篇幅来构思，而不是只按 genre/style 猜。

    返回 `(bible, card_stats)`。补卡失败不影响剧情圣经本身——引导必须能走完。
    """
    from app.routers.wizard import ExpertAnswersIn, build_bible

    qa = _story_brief(answers or {})
    body = ExpertAnswersIn(answers=qa) if qa else None
    bible = build_bible(project.id, body, db)
    bible = bible if isinstance(bible, dict) else dict(bible)
    _apply_world_background(db, project, answers or {})
    card_stats = _attach_character_cards(db, project, bible)
    return bible, card_stats


#: 引导提交时一次最多补多少张角色卡（异常大的角色表也不能打爆成本）。
_MAX_WIZARD_CARDS = 12


def _story_of(project: Project) -> dict[str, str]:
    """角色卡生成用的故事背景（与 /character-cards/batch 同口径）。"""
    return {
        "title": project.title or "",
        "genre": project.genre or "",
        "style": project.style or "",
        "premise": project.premise or "",
    }


def _attach_character_cards(
    db: Session,
    project: Project,
    bible: dict,
    *,
    only_names: list[str] | None = None,
    force: bool = False,
    provider: Any = None,
) -> dict:
    """给剧情圣经里的角色批量补卡，写回 `characters[].card`。

    设计取舍：角色卡的唯一权威就是 `plot_bible.characters[].card`，
    不再镜像一份到 `story_cards`——两份同源数据一定会漂移。

    只补没有声纹卡的角色；`force=True` 时连已有卡一起重生成（用户显式要求）。
    任何失败都只返回 reason，不抛异常：补卡是增强，不能阻断引导。
    """
    chars = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    if not chars:
        return {"generated": 0, "names": [], "missing": [], "collisions": 0,
                "reason": "no_characters"}

    wanted = {str(n).strip() for n in (only_names or []) if str(n).strip()}
    known = {str(c.get("name") or "").strip() for c in chars}
    targets: list[dict] = []
    for c in chars:
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        if wanted and name not in wanted:
            continue
        card = c.get("card")
        # 用户手写的卡（来源=user）是权威：批量重生成不能覆盖，
        # 只有用户在 UI 上点名这一张时才允许重写。
        if (isinstance(card, dict) and card.get("来源") == "user"
                and name not in wanted):
            continue
        if not force and isinstance(card, dict) and card.get("声纹"):
            continue
        targets.append(c)
        if len(targets) >= _MAX_WIZARD_CARDS:
            break

    if not targets:
        reason = ("unknown_names" if wanted and not (wanted & known)
                  else "already_have_cards")
        return {"generated": 0, "names": [], "missing": [], "collisions": 0,
                "reason": reason}

    if provider is not None:
        providers = [provider]
    else:
        providers = []
        try:
            from app.llm.provider import get_llm_provider

            providers.append(get_llm_provider())
        except Exception:  # noqa: BLE001 —— 主渠道没配
            pass
        # 主渠道跑角色卡实测会偶发「远端直接断开」；抽取渠道（默认 zhipu）
        # 是同一份 JSON 生成任务的可替代通道，失败时再试一次，不改变主链路。
        try:
            from app.llm.provider import get_kg_extract_provider

            alt = get_kg_extract_provider()
            if alt is not None and all(alt is not p for p in providers):
                providers.append(alt)
        except Exception:  # noqa: BLE001
            pass
        if not providers:
            return {"generated": 0, "names": [], "missing": [], "collisions": 0,
                    "reason": "provider_unavailable"}

    from app.generation.character_card import voice_signature
    from app.generation.character_card_gen import generate_cards, style_reference

    # 生成前先记录旧 source 作为同步基线：重生成覆盖 card 后，仍能判断
    # 设定卡里的字段是「上次同步值」还是「作者手改值」。
    from app.generation.story_cards import character_fields_of

    previous_fields = {
        str(c.get("name") or "").strip().lower(): character_fields_of(c)
        for c in targets
        if str(c.get("name") or "").strip()
    }

    # 参考语料：蒸馏档案里的对白风格 / 人物声音 / 禁用表达。
    # 没有选参考书或还没蒸馏时是空串，按原逻辑生成。
    style_ref = style_reference(getattr(project, "style_narrative", None))

    target_names = {str(c.get("name") or "").strip() for c in targets}
    already: dict[str, Any] = {}
    for c in chars:
        name = str(c.get("name") or "").strip()
        card = c.get("card")
        if name and name not in target_names and isinstance(card, dict):
            sig = voice_signature(card)
            if sig:
                already[name] = sig

    wants = []
    for c in targets:
        name = str(c.get("name") or "").strip()
        role = str(c.get("role") or "").strip()
        wants.append(f"{name}：{role}" if role else name)

    result: dict = {}
    last_reason = "model_returned_no_cards"
    for p in providers:
        try:
            result = generate_cards(
                p, _story_of(project), wants, already=already,
                style_ref=style_ref,
                raise_errors=True,
            )
        except Exception as exc:  # noqa: BLE001
            last_reason = f"generate_failed: {exc}"[:160]
            continue
        if result.get("角色"):
            break
        last_reason = "model_returned_no_cards"

    cards = result.get("角色") or {}
    if not cards:
        return {"generated": 0, "names": [], "missing": [str(c.get("name") or "")
                                                         for c in targets],
                "collisions": 0, "reason": last_reason}
    attached: list[str] = []
    missing: list[str] = []
    for c in targets:
        name = str(c.get("name") or "").strip()
        card = cards.get(name)
        if not isinstance(card, dict) or not card:
            missing.append(name)
            continue
        c["card"] = card
        attached.append(name)

    # 一次批量调用后仍缺卡：只对缺失角色再补一轮（最多一轮），不整批重跑。
    collisions = result.get("冲突") or []
    retried: list[str] = []
    if missing:
        retry_wants = list(missing)
        retry_already = {
            name: (card.get("声纹") or {})
            for name, card in cards.items()
            if isinstance(card, dict)
        }
        retry_collisions: list[dict] = []
        for p in providers:
            try:
                retry = generate_cards(
                    p, _story_of(project), retry_wants,
                    already=retry_already, style_ref=style_ref,
                    raise_errors=True,
                )
            except Exception as exc:  # noqa: BLE001 —— 补建失败不改变首轮结果
                last_reason = f"retry_failed: {exc}"[:160]
                continue
            retry_cards = retry.get("角色") or {}
            if not retry_cards:
                continue
            remaining: list[str] = []
            for name in missing:
                card = retry_cards.get(name)
                if not isinstance(card, dict) or not card:
                    remaining.append(name)
                    continue
                target = next(
                    (c for c in targets
                     if str(c.get("name") or "").strip() == name),
                    None,
                )
                if target is None:
                    remaining.append(name)
                    continue
                target["card"] = card
                attached.append(name)
                retried.append(name)
            retry_collisions.extend(retry.get("冲突") or [])
            missing = remaining
            if not missing:
                break
        collisions = retry_collisions + collisions
        if retried or missing:
            logger.info(
                "角色卡缺卡二次补建 project=%s retried=%s still_missing=%s",
                getattr(project, "id", "?"), retried, missing,
            )

    if collisions:
        bible["card_collisions"] = collisions[:8]
    else:
        bible.pop("card_collisions", None)

    project.plot_bible = bible
    # JSON 列是原地改的：不标脏时 SQLAlchemy 认为「同一个对象、值相等」而不发
    # UPDATE，接口返回里有卡、库里却没有（实测）。
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "plot_bible")
    except Exception:  # noqa: BLE001 —— 测试里的假 project 没有 ORM 状态
        pass
    db.commit()

    # 卡写回后同步进设定卡视图：按快照智能合并，作者手改字段保留；
    # 同步失败不影响建卡（设定卡视图是展示层增强）。
    try:
        from app.generation.story_cards import sync_from_project

        sync_from_project(db, project, previous_fields=previous_fields)
        db.commit()
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 —— 测试里的假 db 没有 rollback
            pass

    return {
        "generated": len(attached),
        "names": attached,
        "missing": missing,
        "retried": retried,
        "collisions": len(collisions),
        "reason": "" if attached else "model_returned_no_cards",
    }


@router.post("/{project_id}/character-cards/regenerate")
def regenerate_character_cards(
    project_id: int,
    body: CardsRegenerateIn,
    db: Session = Depends(get_db),
) -> dict:
    """重新生成角色卡（names 为空 = 全部），写回剧情圣经。

    与引导提交时同一套生成器；区别只有 `force=True`：用户显式点了重生成，
    已有卡也要覆盖。生成失败返回 502，前端可以重试。
    """
    project = _require_project(db, project_id)
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    if not (bible.get("characters") or []):
        raise HTTPException(status_code=409, detail="还没有剧情圣经，请先走完创作引导")

    stats = _attach_character_cards(
        db, project, bible, only_names=body.names or None, force=True)
    if stats.get("reason") == "unknown_names":
        raise HTTPException(status_code=400, detail="指定的角色不在剧情圣经里")
    if not stats.get("generated"):
        reason = stats.get("reason") or "模型没有返回可用角色卡"
        raise HTTPException(status_code=502, detail=f"角色卡生成失败：{reason}")
    message = f"已重新生成 {stats['generated']} 张角色卡。"
    if stats.get("missing"):
        message += f"以下角色这次没生成成功：{'、'.join(stats['missing'])}。"
    return {
        "ok": True,
        "bible": project.plot_bible,
        "character_cards": stats,
        "message": message,
    }


@router.post("/{project_id}/character-cards/rebuild-with-archive")
def rebuild_cards_with_archive(
    project_id: int, db: Session = Depends(get_db)
) -> dict:
    """蒸馏完成后，用刚就绪的文风档案重建角色卡。

    只重建剧情圣经里的角色卡，**不重建剧情圣经本身**——后者会覆盖作者已改的
    世界观与人物设定。用户手写卡（来源=user）即使 force=True 也不会被覆盖。
    """
    project = _require_project(db, project_id)
    if not isinstance(project.style_narrative, dict) or not project.style_narrative:
        raise HTTPException(status_code=409, detail="文风档案还没就绪，等蒸馏完成后重试")
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    if not (bible.get("characters") or []):
        raise HTTPException(status_code=409, detail="还没有剧情圣经，请先走完创作引导")

    stats = _attach_character_cards(db, project, bible, force=True)
    if not stats.get("generated"):
        reason = stats.get("reason") or "模型没有返回可用角色卡"
        raise HTTPException(status_code=502, detail=f"角色卡重建失败：{reason}")
    message = f"已用文风档案重建 {stats['generated']} 张角色卡。"
    if stats.get("missing"):
        message += f"以下角色这次没生成成功：{'、'.join(stats['missing'])}。"
    return {"ok": True, "character_cards": stats, "message": message}


@router.delete("/{project_id}/characters/{name}")
def remove_character(
    project_id: int,
    name: str,
    db: Session = Depends(get_db),
) -> dict:
    """把角色从剧情圣经整体移除（同时清理涉及 TA 的关系）。

    只改作者意图层：已有正文、KG 抽取结果不删；后续生成不再把 TA 当锁定角色。
    """
    project = _require_project(db, project_id)
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    chars = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    target = next(
        (c for c in chars if str(c.get("name") or "").strip() == name), None)
    if target is None:
        logger.warning(
            "角色移除被拒：不在剧情圣经里 project_id=%s name=%s", project_id, name)
        raise HTTPException(status_code=404, detail="角色不在剧情圣经里")

    bible["characters"] = [c for c in chars if c is not target]
    removed_rel = 0
    rels = bible.get("relationships")
    if isinstance(rels, list):
        kept = [
            r for r in rels
            if not (isinstance(r, dict) and name in (r.get("a"), r.get("b")))
        ]
        removed_rel = len(rels) - len(kept)
        bible["relationships"] = kept
    collisions = bible.get("card_collisions")
    if isinstance(collisions, list):
        kept_c = [
            c for c in collisions
            if not (isinstance(c, dict) and name in (c.get("人物") or []))
        ]
        if kept_c:
            bible["card_collisions"] = kept_c
        else:
            bible.pop("card_collisions", None)

    project.plot_bible = bible
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "plot_bible")
    except Exception:  # noqa: BLE001
        pass
    try:
        db.commit()
    except Exception:
        logger.exception(
            "角色移除落库失败 project_id=%s name=%s", project_id, name)
        raise

    # 同步设定卡视图：未手改的镜像卡删除，手改过的停用保留。
    retired = {"deleted": False, "disabled": False}
    try:
        from app.generation.story_cards import retire_story_card

        retired = retire_story_card(db, project, name)
        db.commit()
    except Exception:  # noqa: BLE001 —— 同步失败不影响角色移除本身
        logger.warning(
            "角色移除后同步设定卡失败 project_id=%s name=%s", project_id, name)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass

    logger.info(
        "角色已从剧情圣经移除 project_id=%s name=%s removed_relationships=%s "
        "remaining_characters=%s",
        project_id, name, removed_rel, len(bible["characters"]),
    )
    message = f"已从剧情圣经移除「{name}」"
    if removed_rel:
        message += f"，并清理 {removed_rel} 条相关关系"
    message += "。正文与已有图谱不受影响，后续生成不再把 TA 当锁定角色。"
    if retired.get("deleted"):
        message += "对应设定卡已删除。"
    elif retired.get("disabled"):
        message += "对应设定卡已停用（保留手改内容）。"
    return {
        "ok": True,
        "bible": project.plot_bible,
        "removed_relationships": removed_rel,
        "story_card": retired,
        "message": message,
    }


@router.delete("/{project_id}/character-cards/{name}")
def delete_character_card(
    project_id: int,
    name: str,
    db: Session = Depends(get_db),
) -> dict:
    """删除一张角色卡（只删卡，角色仍保留在剧情圣经里）。"""
    project = _require_project(db, project_id)
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    chars = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    target = next(
        (c for c in chars if str(c.get("name") or "").strip() == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail="角色不在剧情圣经里")
    if not isinstance(target.get("card"), dict):
        raise HTTPException(status_code=409, detail="这个角色还没有角色卡")

    target.pop("card", None)
    collisions = bible.get("card_collisions")
    if isinstance(collisions, list):
        kept = [
            c for c in collisions
            if not (isinstance(c, dict) and name in (c.get("人物") or []))
        ]
        if kept:
            bible["card_collisions"] = kept
        else:
            bible.pop("card_collisions", None)

    project.plot_bible = bible
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "plot_bible")
    except Exception:  # noqa: BLE001
        pass
    db.commit()
    return {
        "ok": True,
        "bible": project.plot_bible,
        "character_cards": {
            "generated": 0, "names": [], "missing": [name],
            "collisions": len(bible.get("card_collisions") or []),
            "reason": "",
        },
        "message": f"已删除「{name}」的角色卡（角色仍在剧情圣经里，可随时重新生成）。",
    }


@router.put("/{project_id}/character-cards/{name}")
def update_character_card(
    project_id: int,
    name: str,
    body: CharacterCardIn,
    db: Session = Depends(get_db),
) -> dict:
    """手工保存一张角色卡（来源标记为 user，自动流程不再覆盖）。"""
    project = _require_project(db, project_id)
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    if not (bible.get("characters") or []):
        raise HTTPException(status_code=409, detail="还没有剧情圣经，请先走完创作引导")
    if len(json.dumps(body.card, ensure_ascii=False)) > 20000:
        raise HTTPException(status_code=413, detail="角色卡内容过长")

    from app.generation.character_card import normalize_card

    chars = [c for c in (bible.get("characters") or []) if isinstance(c, dict)]
    target = next(
        (c for c in chars if str(c.get("name") or "").strip() == name), None)
    if target is None:
        raise HTTPException(status_code=404, detail="角色不在剧情圣经里")
    card = normalize_card(body.card)
    if not card:
        raise HTTPException(status_code=422, detail="角色卡内容为空")
    # 保存前先算旧 source：同步时据此区分「上次同步值」与「作者手改值」。
    from app.generation.story_cards import character_fields_of

    previous_fields = {
        str(name).strip().lower(): character_fields_of(target),
    }
    card["来源"] = "user"
    target["card"] = card

    project.plot_bible = bible
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "plot_bible")
    except Exception:  # noqa: BLE001
        pass
    db.commit()

    # 保存角色卡后立即同步设定卡视图；失败只记日志，不影响保存本身。
    try:
        from app.generation.story_cards import sync_from_project

        sync_from_project(db, project, previous_fields=previous_fields)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("角色卡保存后同步设定卡失败 project_id=%s name=%s", project_id, name)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass

    return {
        "ok": True,
        "bible": project.plot_bible,
        "card": card,
        "character_cards": {
            "generated": 1, "names": [name], "missing": [],
            "collisions": 0, "reason": "",
        },
        "message": f"已保存「{name}」的角色卡（手写，重生成全部不会覆盖）。",
    }


@router.post("/{project_id}/run")
def run(project_id: int, db: Session = Depends(get_db)) -> dict:
    """确认后真正开始写：复用 `generate_loop`，串行逐段生成。"""
    project = _require_project(db, project_id)
    session = _any_session(db, project_id)
    if session is None or not session.result:
        raise HTTPException(status_code=409, detail="还没有完成配置，请先走完引导")

    result = dict(session.result)
    if result.get("stage") not in {"ready", "outline"}:
        raise HTTPException(status_code=409, detail="当前状态不需要再次生成")
    if result.get("kind") == flow.KIND_ROLE:
        raise HTTPException(status_code=400, detail="角色对话不产生正文")

    answers = session.answers or {}
    segments = int(answers.get("chapters") or 1)
    max_chars = int(answers.get("length") or project.generation_length or 3000)

    from app.generation import chapter_labels
    from app.generation.engine import generate_loop

    try:
        # 章身份：引导页选的是「几章」不是「几段」。推导统一走 chapter_labels
        # （库内最大章号 +1）——否则从引导直接开写会落成 `段1..段N`，之后平台
        # 续写再推 `第1章`，同一个项目里就同时存在两套编号了。
        plans = chapter_labels.resolve_many(db, project, None, segments)
        rows = generate_loop(
            db,
            project,
            novel_ids=None,
            segments=segments,
            max_chars=max_chars,
            chapter_labels=[p["label"] for p in plans],
        )
    except chapter_labels.ChapterRangeError as exc:  # 先于 ValueError（子类）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:  # 未提炼等可操作错误：如实返回，不吞成 500
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException as exc:
        # 时间线规划闸门：引擎的 409 带内部 detail，引导页只需要「为什么不能开写
        # ＋下一步去哪」。保留状态码，不降级成 500，也不把内部结构原样漏出去。
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        status = str(detail.get("status") or "")
        if status == "plan_exhausted":
            raise HTTPException(status_code=409, detail={
                "status": status,
                "message": "这一版的剧情点已经全部写完，不能再往后写了。"
                           "要继续，请先到时间线页扩展规划；已写章节不受影响。",
                "retry": f"POST /api/projects/{project.id}/plan-extension",
            }) from exc
        if status in {"needs_planning", "needs_review"}:
            raise HTTPException(status_code=409, detail={
                "status": status,
                "message": (
                    "剧情计划还没生成好，暂时不能开写。请先到时间线页重规划；"
                    "仍失败就检查模型渠道后重试。已写章节不受影响。"
                    if status == "needs_planning" else
                    "当前剧情点的篇幅预算已用尽、退出条件还没有正文证据。"
                    "请先检查已写正文，或调整计划/预算后再继续。"
                ),
                "retry": f"POST /api/projects/{project.id}/timeline/plan",
            }) from exc
        raise

    produced = []
    for row in rows:
        text = (row or {}).get("text") or ""
        produced.append(
            {
                "segment_no": (row or {}).get("segment_no"),
                "chars": len(text),
                "excerpt": text[:120],
            }
        )
    result.update({"stage": "done", "segments": produced})
    session.result = result
    session.status = "committed"
    db.commit()
    return {"done": True, "result": result, "segments": produced}


# ---------------------------------------------------------------------------
# 角色对话（只读 KG 角色卡 + Assistant 模型，不落库）
# ---------------------------------------------------------------------------


@router.post("/{project_id}/role-chat")
def role_chat(project_id: int, body: RoleChatIn, db: Session = Depends(get_db)) -> dict:
    project = _require_project(db, project_id)
    session = _any_session(db, project_id)
    answers = (session.answers if session else {}) or {}
    name = (answers.get("character") or "").strip()
    scene = (answers.get("scene") or "当前剧情时间线").strip()
    if not name:
        raise HTTPException(status_code=409, detail="还没有选定角色，请先完成引导")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")
    if len(message) > 2000:
        raise HTTPException(status_code=400, detail="单条消息最多 2000 字")

    card = _character_card(db, project.id, name)
    # 角色对话 = dialogue 用途：声音词典给足配额，文风/时间线退为辅助；
    # KG 侧只取人物近况与关系（不要整条事件序列）。
    from app.generation.engine import (
        _narrative_timeline,
        _style_narrative,
        _style_source_ids,
    )
    from app.generation.prompts import _narrative_block

    narrative_block = _narrative_block(
        _style_narrative(db, project),
        _style_source_ids(project),
        mode="dialogue",
        timeline=_narrative_timeline(db, project),
    )
    system = _role_system_prompt(name, scene, card, narrative_block=narrative_block)
    history_lines = []
    for turn in body.history[-8:]:
        role = "用户" if turn.get("role") == "user" else name
        history_lines.append(f"{role}：{turn.get('content', '')}")
    user = ("以下是此前的对话：\n" + "\n".join(history_lines) + "\n\n") if history_lines else ""
    user += f"用户说：{message}"

    from app.llm.provider import get_assistant_provider

    try:
        reply = get_assistant_provider().chat(system, user)
    except Exception as exc:  # noqa: BLE001 —— 模型侧失败要如实告诉用户
        raise HTTPException(status_code=502, detail=f"角色对话调用失败：{exc}") from exc

    sid = getattr(session, "id", None)
    answer = (reply or "").strip() or "（角色沉默了一会儿）"
    record_message(
        db, project.id, role="user", kind="plain", text=message, session_id=sid
    )
    record_message(
        db,
        project.id,
        role="assistant",
        kind="role_chat",
        text=answer,
        payload={"character": name, "scene": scene},
        session_id=sid,
    )
    return {
        "answer": answer,
        "character": name,
        "scene": scene,
    }


def _character_card(db: Session, project_id: int, name: str) -> str:
    """从 KG 拼一张紧凑角色卡；查不到时返回空串，提示词里如实说明。"""
    entity = (
        db.query(KgEntity)
        .filter(
            KgEntity.project_id == project_id,
            KgEntity.kind == "character",
            KgEntity.canonical_name == name,
        )
        .first()
    )
    if entity is None:
        # 兜底：别名里命中（别名以 JSON 数组存）。
        for cand in (
            db.query(KgEntity)
            .filter(KgEntity.project_id == project_id, KgEntity.kind == "character")
            .all()
        ):
            if name in (cand.aliases or []):
                entity = cand
                break
    if entity is None:
        # 旧图谱兜底：只有名字与提及次数，没有关系/状态，提示词里如实说明。
        for node in (
            db.query(KgNode)
            .filter(KgNode.project_id == project_id, KgNode.type == "character")
            .all()
        ):
            props = node.props or {}
            if str(props.get("name") or "").strip() == name:
                return (
                    f"姓名：{name}\n"
                    f"旧图谱提及次数：{int(props.get('mentions') or 0)}\n"
                    "（新图谱没有该角色的身份/关系/状态数据，只能依据名字与常识，"
                    "不得编造具体经历）"
                )
        return ""

    lines = [f"姓名：{entity.canonical_name}"]
    if entity.aliases:
        lines.append("别名：" + "、".join(str(a) for a in entity.aliases[:8]))
    attrs = entity.attrs or {}
    for key, label in (
        ("identity", "身份"),
        ("faction", "所属势力"),
        ("power_level", "境界/实力"),
        ("current_goal", "当前目标"),
        ("arc", "人物弧线"),
        ("secrets", "秘密（不能说破）"),
    ):
        val = attrs.get(key)
        if val:
            text = "、".join(str(x) for x in val) if isinstance(val, list) else str(val)
            lines.append(f"{label}：{text[:300]}")

    states = (
        db.query(KgCharacterState)
        .filter(
            KgCharacterState.project_id == project_id,
            KgCharacterState.character_id == entity.id,
            KgCharacterState.is_current.is_(True),  # 同章旧稿状态只留档
        )
        .order_by(desc(KgCharacterState.chapter), desc(KgCharacterState.id))
        .limit(8)
        .all()
    )
    for st in states:
        if st.new_value:
            lines.append(f"最近状态（第 {st.chapter or '?'} 章）{st.state_type}：{st.new_value}")

    rels = (
        db.query(KgRelation)
        .filter(
            KgRelation.project_id == project_id,
            (KgRelation.from_id == entity.id) | (KgRelation.to_id == entity.id),
            KgRelation.is_current.is_(True),  # 同章旧稿关系只留档
        )
        .order_by(desc(KgRelation.strength))
        .limit(10)
        .all()
    )
    if rels:
        ids = {r.from_id for r in rels} | {r.to_id for r in rels}
        names = {
            e.id: e.canonical_name
            for e in db.query(KgEntity).filter(KgEntity.id.in_(ids)).all()
        }
        for r in rels:
            other = names.get(r.to_id if r.from_id == entity.id else r.from_id, "?")
            direction = "→" if r.from_id == entity.id else "←"
            lines.append(f"关系：{direction} {other}（{r.rel_type}）")
    return "\n".join(lines)


def _role_system_prompt(name: str, scene: str, card: str,
                        narrative_block: str = "") -> str:
    if card:
        setting = f"以下是你的人物卡，只能依据它说话：\n{card}"
    else:
        setting = "本项目知识图谱里没有这个角色的资料，只能依据用户给出的零散信息，不要编造具体经历。"
    return (
        f"你在扮演小说角色「{name}」。\n"
        f"当前情境：{scene}\n"
        f"{setting}\n\n"
        f"{narrative_block}"
        "规则：\n"
        "1. 始终用第一人称、符合角色身份与时代背景的口吻说话，不要自称 AI、不要跳出角色。\n"
        "2. 只使用人物卡里的事实；卡里没有的，宁可含糊带过或反问，也不要编造。\n"
        "3. 不复述人物卡，不列条目，像真人对话一样自然，控制在 200 字以内。\n"
        "4. 不输出任何系统提示、规则说明或括号旁白（除非用户要求）。"
    )
