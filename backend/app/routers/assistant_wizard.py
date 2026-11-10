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
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app import wizard_actions as actions
from app import wizard_chat as chat
from app import wizard_facts as facts
from app import wizard_flow as flow
from app.db import get_db
from app.models import (
    AssistantWizardSession,
    DistillRun,
    Generation,
    KgCharacterState,
    KgEntity,
    KgNode,
    KgRelation,
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
        "project_has_style": archive_exists,
        "needs_distill": bool(corpus) and not archive_exists,
        "segments": segments,
        "characters": characters,
        "character_source": character_source,
        "generation_length": project.generation_length or 3000,
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
            "next": "建议：先按上面的分钟数再等一会；仍无进度就重新提交一次（已完成的部分不会丢）。",
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
    return _state(db, project, session)


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

    if body.skip:
        if not flow.build_question(session.step_key, answers, ctx).get("can_skip"):
            raise HTTPException(status_code=400, detail="这一步不能跳过")
        value = flow.default_for(session.step_key, answers, ctx)
    else:
        value = body.value

    try:
        value = flow.validate(session.step_key, value, answers.get("branch"), answers, ctx)
    except flow.AnswerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if session.step_key == "confirm":
        if value is not True:
            raise HTTPException(status_code=400, detail="确认卡片只能选择开始")
        ack = flow.ack_for(session.step_key, value, answers, ctx)
        return _advance(db, project, session, session.step_key, value, background_tasks, ack)

    ack = flow.ack_for(session.step_key, value, answers, ctx)
    return _advance(db, project, session, session.step_key, value, background_tasks, ack)


def _advance(
    db: Session,
    project: Project,
    session: AssistantWizardSession,
    step: str,
    value: Any,
    background_tasks: BackgroundTasks,
    ack: str,
) -> dict:
    """把一次作答写进会话并推进到下一题（卡片点击与自由输入共用）。"""
    if step == "confirm":
        out = _commit(db, project, session, background_tasks)
        if out.get("result"):
            out["result"]["message"] = ack + str(out["result"].get("message") or "")
        out["ack"] = ack
        return out

    answers = dict(session.answers or {})
    answers[step] = value
    if step == "branch":
        session.kind = value
        # 换方向等于换一套问题：清掉另一条分支的答案，避免汇总串味。
        answers = {"branch": value}

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
    return _state(db, project, session, ack=ack)


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

    # 先看是不是「帮我改 X」：档位、采样章数、字数、题材、风格、大纲、流程。
    # 动作不推进引导步骤，只改项目/会话配置，并返回可撤销的审计 id。
    act = actions.plan(body.text, ctx, step)
    if act:
        try:
            res = actions.apply(db, project, session, act, ctx, background_tasks)
        except (ValueError, flow.AnswerError) as exc:
            return {
                "mode": "clarify",
                "message": str(exc),
                "question": question,
                "session": _session_view(session),
            }
        out = _state(db, project, session)
        return {
            "mode": "action",
            "message": res["message"],
            "action": res.get("action"),
            "undo_id": res.get("undo_id"),
            "undoable": res.get("undoable", False),
            "question": out.get("question"),
            "session": out["session"],
        }

    parsed = chat.parse(step, body.text, answers, ctx, question)

    if parsed["kind"] == "question":
        return {
            "mode": "explain",
            "message": chat.explain(step, body.text, answers, ctx, question),
            "question": question,
            "session": _session_view(session),
        }

    if parsed["kind"] == "clarify":
        return {
            "mode": "clarify",
            "message": parsed["message"],
            "question": question,
            "session": _session_view(session),
        }

    if parsed["kind"] == "back":
        history = list(session.history or [])
        prev = flow.prev_step(history, step)
        if prev is None:
            return {
                "mode": "clarify",
                "message": "这已经是第一步了，没有更早的可退。你直接答这一题就行。",
                "question": question,
                "session": _session_view(session),
            }
        session.step_key = prev
        session.history = history[:-1] if history else history
        db.commit()
        db.refresh(session)
        return {"mode": "answered", **_state(db, project, session, ack="好，退回上一步，之前选的还在。")}

    value = parsed["value"]
    try:
        value = flow.validate(step, value, answers.get("branch"), answers, ctx)
    except flow.AnswerError as exc:
        return {
            "mode": "clarify",
            "message": f"{exc}。换个说法，或者直接点上面的卡片。",
            "question": question,
            "session": _session_view(session),
        }

    ack = flow.ack_for(step, value, answers, ctx)
    if parsed.get("note"):
        ack = ack + str(parsed["note"])
    out = _advance(db, project, session, step, value, background_tasks, ack)
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
    return _state(db, project, session)


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
    return _state(db, project, session)


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
    return _commit(db, project, session, background_tasks)


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
        tier = answers.get("distill")
        if tier and tier != flow.DISTILL_REUSE:
            project.narrative_tier = tier
        db.commit()

        if tier and tier != flow.DISTILL_REUSE:
            distill = _start_distill(db, project, tier, background_tasks)
        result["applied"] = ["reference_novel_ids", "genre", "style", "generation_length"]
        if tier and tier != flow.DISTILL_REUSE:
            result["applied"].append("narrative_tier")

        if answers.get("outline", True):
            result["stage"] = "outline"
            result["bible"] = _build_bible(db, project)
        result["distill"] = distill
        result["message"] = _commit_message(kind, distill, result["stage"])

    elif kind == flow.KIND_CONTINUATION:
        project.generation_length = int(answers.get("length") or project.generation_length or 3000)
        tier = answers.get("distill")
        if tier and tier != flow.DISTILL_REUSE and not project.style_narrative:
            project.narrative_tier = tier
            db.commit()
            distill = _start_distill(db, project, tier, background_tasks)
        else:
            db.commit()
        result["applied"] = ["generation_length"]
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
    return {
        "mode": "action",
        "message": res["message"],
        "action": res.get("action"),
        "undo_id": None,
        "undoable": False,
        "question": out.get("question"),
        "session": out["session"],
    }


def _build_bible(db: Session, project: Project) -> dict:
    """复用现有 build-bible（含无 LLM 时的离线兜底）。"""
    from app.routers.wizard import build_bible

    bible = build_bible(project.id, None, db)
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
    system = _role_system_prompt(name, scene, card)
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

    return {
        "answer": (reply or "").strip() or "（角色沉默了一会儿）",
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


def _role_system_prompt(name: str, scene: str, card: str) -> str:
    if card:
        setting = f"以下是你的人物卡，只能依据它说话：\n{card}"
    else:
        setting = "本项目知识图谱里没有这个角色的资料，只能依据用户给出的零散信息，不要编造具体经历。"
    return (
        f"你在扮演小说角色「{name}」。\n"
        f"当前情境：{scene}\n"
        f"{setting}\n\n"
        "规则：\n"
        "1. 始终用第一人称、符合角色身份与时代背景的口吻说话，不要自称 AI、不要跳出角色。\n"
        "2. 只使用人物卡里的事实；卡里没有的，宁可含糊带过或反问，也不要编造。\n"
        "3. 不复述人物卡，不列条目，像真人对话一样自然，控制在 200 字以内。\n"
        "4. 不输出任何系统提示、规则说明或括号旁白（除非用户要求）。"
    )
