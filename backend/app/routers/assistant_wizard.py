"""助理对话里的「一步步生成」引导接口。

这一层只做三件事：
1. 把 `app.wizard_flow` 的纯状态机接到项目数据上（语料列表 / 蒸馏档位 /
   角色列表 / 已有段号）；
2. 把用户选中的答案**映射到现有参数**：项目字段、现有蒸馏后台任务、
   现有 `build_bible`、现有 `generate_loop`——不新造生成执行层；
3. 把会话落库，支持刷新/关页后续跑。

「角色对话」是仓库里原本没有的链路：这里只读 KG 角色卡 + Assistant 模型
回答，不写正文、不写业务表、不改 KG 任何数据。
"""

from __future__ import annotations

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
    if step not in story.STORY_STEPS and step != "branch":
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
    蒸馏库时，多问一次档位也只是返回一次 `from_cache: True`。

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
        "generation_length": project.generation_length or 3000,
        "project_genre": project.genre,
    }
    # 语料真实章数/字数、已有档案、各档预计耗时、维度清单、模型可用事实清单。
    ctx.update(facts.distill_context(db, project))
    ctx["overrides"] = overrides or {}
    return ctx


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
            "message": f"蒸馏疑似卡住：{note}",
            "progress_current": int(run.progress_current or 0),
            "progress_total": int(run.progress_total or 0),
            "expected_minutes": run.expected_minutes,
            "elapsed_minutes": distill_run_to_dict(run).get("elapsed_minutes"),
            # 这条路**不给「继续蒸馏」按钮**：卡住的任务仍是 running，点续跑会被
            # find_active_run 复用成同一个任务（等于没点）。真正的出口是重启后端：
            # 重启后 reap_stale_runs 把它转成「中断」回执，那条上才有按钮。
            "next": (
                "建议：先按上面的分钟数再等一会。若仍无进度，重启后端——重启会把这条"
                "任务转成「中断」回执，点那条回执上的「继续蒸馏」接着跑（已完成的块不会重跑）。"
            ),
        },
    )


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
        # 步骤序列是动态的（例如本轮新增的 book_scope 会因为「在场语料 ≥2 本」
        # 插在 corpus 之后）。部署前就停在后面的旧会话，step_key 会「越过」
        # 一个从未答过的步骤——进度写着 10 步、实际只走 9 步，新步骤对它永不
        # 可见。这里退回到序列里第一个未答的步骤，自愈一次即可。
        order = flow.step_order(answers.get("branch"), answers, ctx)
        pending = next((s for s in order if s not in answers), None)
        if (
            pending
            and session.step_key in order
            and order.index(pending) < order.index(session.step_key)
        ):
            session.step_key = pending
            db.commit()
            db.refresh(session)
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


@router.get("/{project_id}/notices")
def list_notices(project_id: int, db: Session = Depends(get_db)) -> dict:
    """未消费的蒸馏通知 + 最近一条任务状态（一次轮询同时更新进度和回执）。

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

    if body.step_key != session.step_key:
        # 页面停留在旧题（另一个标签页已经答过）：不覆盖，直接把当前题推回去。
        raise HTTPException(
            status_code=409,
            detail=f"当前问题已变为「{session.step_key}」，请刷新后重答",
        )

    answers = dict(session.answers or {})
    ctx = _context(db, project, session.history or [], session.overrides or {})
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
    nxt = flow.next_step(session.kind, step, answers, ctx)
    if nxt is None:
        # 理论上只有 confirm 会走到末尾，而 confirm 已在上面处理。
        raise HTTPException(status_code=500, detail="引导流程没有下一步")
    session.step_key = nxt
    db.commit()
    db.refresh(session)
    out = _state(db, project, session, ack=ack)
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

    step = session.step_key
    answers = dict(session.answers or {})
    ctx = _context(db, project, session.history or [], session.overrides or {})
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
        session.step_key = "confirm"
        db.commit()
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

    `novel_chunks` 是蒸馏与 RAG 的唯一语料源；为空时蒸馏会整批失败，
    用户只会看到「蒸馏完成但 0 条规则」。所以在写字段前拦下，并把
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
            result["bible"] = _build_bible(db, project, answers)
        result["distill"] = distill
        result["message"] = _commit_message(kind, distill, result["stage"])

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
        result["message"] = _commit_message(kind, distill, "ready")

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


def _commit_message(kind: str, distill: dict | None, stage: str) -> str:
    parts = []
    if kind == flow.KIND_CONTINUATION:
        parts.append("已按你的选择保存续写参数。")
    else:
        parts.append("已按你的选择保存项目参数。")
    if distill:
        if distill.get("from_cache"):
            parts.append("文风档案直接命中缓存，没有重新蒸馏。")
        else:
            parts.append(
                f"蒸馏任务已提交（job #{distill.get('job_id')}），"
                "后台跑完才能开始生成；可以在项目工作台看进度。"
            )
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


def _build_bible(
    db: Session,
    project: Project,
    answers: dict[str, Any] | None = None,
) -> dict:
    """复用现有 build-bible（含无 LLM 时的离线兜底）。

    把引导里的故事问答作为「创作者问答」传进去：大纲要按用户刚说的题材、
    写法、核心冲突、主角、世界约束、篇幅来构思，而不是只按 genre/style 猜。
    """
    from app.routers.wizard import ExpertAnswersIn, build_bible

    qa = _story_brief(answers or {})
    body = ExpertAnswersIn(answers=qa) if qa else None
    bible = build_bible(project.id, body, db)
    return bible if isinstance(bible, dict) else dict(bible)


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

    from app.generation.engine import generate_loop

    try:
        rows = generate_loop(
            db,
            project,
            novel_ids=None,
            segments=segments,
            max_chars=max_chars,
        )
    except ValueError as exc:  # 未蒸馏等可操作错误：如实返回，不吞成 500
        raise HTTPException(status_code=409, detail=str(exc)) from exc

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
