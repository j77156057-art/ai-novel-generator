"""M4 生成环路由：续写单段 / 续写多段 / 流式续写 / 生成前预览(plan) / 查看已生成段。

端点：
- POST /api/generate            续写一段（同步，等整段返回）
- POST /api/generate-loop       连续续写多段
- POST /api/generate-stream     流式续写（SSE，长文首字即显）
- POST /api/generate-plan       仅组装上下文与提示词（不调 LLM），供生成前漂移预检
- GET  /api/generations/{pid}   列出某项目已生成的全部段落
- PATCH /api/generations/{pid}/{generation_id}  手动改一段正文并重算总结/图谱
- GET  /api/projects/{pid}/generation-status  轻量进度（段数/最新段·章/是否在生成）
- GET  /api/projects/{pid}/segments/{no}/snapshot  取某段生成时的提示词/权威快照
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel  # noqa: F401 - 供类型标注/未来扩展保留
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import json as _json
import logging

from app.config import settings
from app.db import get_db
from app.generation import chapter_labels
from app.generation import (
    build_context,
    generate_loop,
    generate_next_segment,
)
from app.generation.engine import _apply_gate_limits, _catalog_skeleton, _style_info, _style_narrative, _style_profile, _style_source_ids, _next_segment_no, current_node_prompt, preflight_timeline_gate, _timeline_progress, _chapter_ordinal
from app.generation.length_guard import LengthBlocked
from app.generation.outline_notice import build_notice
from app.generation.prompts import build_system_prompt, build_user_prompt
from app.generation.repetition import parse_quality
from app.generation.story_cards import render_for_project as _render_story_cards
from app.models import Generation, Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api", tags=["generation"])

_logger = logging.getLogger(__name__)


class GenerateBody(StrictRequest):
    project_id: int
    novel_ids: list[int] | None = None  # 缺省用 project.reference_novel_ids
    max_chars: int | None = None  # 缺省用 project.generation_length
    # 章号/章区间：`"44"` 或 `"44-46"`。缺省由 `chapter_labels.resolve` 推导
    # （库内最大章号 +1），前端不改也能出章。
    chapter_range: str | None = None


class GenerateLoopBody(StrictRequest):
    project_id: int
    novel_ids: list[int] | None = None
    # 默认 1（历史值为 3，此处刻意下调并留痕）：引擎的设计是「一次长段」而非多段短循环
    # ——短循环会把前情/大纲反复塞进上下文，既压低提示词缓存命中率又易触发会话压缩
    #（见 engine.generate_next_segment 的 docstring）。要连写多段请调用方显式给数，
    # 缺省不该悄悄替用户连生成 3 段、还让他以为只写了一段。
    segments: int = 1
    max_chars: int | None = None
    chapter_range: str | None = None


class GeneratePlanBody(StrictRequest):
    project_id: int
    novel_ids: list[int] | None = None


class RegenerateBody(StrictRequest):
    project_id: int
    segment_no: int
    novel_ids: list[int] | None = None
    max_chars: int | None = None
    # 只是「改标签」：重写不改章号，所以这里允许指向已存在的章。
    chapter_range: str | None = None


class UpdateGenerationTextBody(StrictRequest):
    """手动改正文：只收 text，段号/章身份不允许经这里改。"""

    text: str


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _begin_generation(project_id: int) -> None:
    """同一项目禁止并发生成（B4）：拿不到租约就 409，不静默排队。

    段模式没有章号可去重，双击「生成」会各起一次模型调用并各落一段；
    流式入口的请求早已返回 200，更拦不住。租约在 finally 里释放，
    残留租约按 TTL 过期（见 `app.generation.inflight`）。
    """
    from app.generation.inflight import try_begin

    if not try_begin(project_id):
        raise HTTPException(
            status_code=409,
            detail="该项目已有生成在进行中；请等当前生成结束后再试"
                   "（同一项目不允许并发生成）",
        )


def _end_generation(project_id: int) -> None:
    from app.generation.inflight import end

    end(project_id)


def _resolve_novel_ids(proj: Project, override: list[int] | None) -> list[int]:
    if override:
        return override
    return list(proj.reference_novel_ids or [])


def _planning_notice(proj: Project) -> dict | None:
    """预检用的规划缺口（不受 `planning_guard_mode` 影响，始终只报告）。"""
    from app.generation.planning_guard import planning_block

    return planning_block(proj)


def _require_planning_ready(proj: Project) -> None:
    """规划就绪闸（P0）：`block` 模式下规划不完整直接 409。

    `report`（默认）不拦，缺口通过 `/generate-plan` 的 `planning` 字段暴露。
    """
    mode = str(getattr(settings, "planning_guard_mode", "report") or "report").lower()
    if mode != "block":
        return
    from app.generation.planning_guard import planning_block

    block = planning_block(proj)
    if block:
        raise HTTPException(status_code=409, detail=block)


def _require_workflow_ready(db: Session, proj: Project) -> None:
    """生成前的**工序链闸门**：流程没过就别生成（别绕过流程）。

    拦的是具体缺口（不是「项目没资料」那种自由生成）：

    - 有待批的写回建议 → 正文要改完才能继续生成；
    - 文风档案没真进到提示词里（提示词体检有 error）→ 生成出来也是白写。

    自由生成（没选参考语料、没有文风档案）一律放行 ——
    闸门拦的是「流程没走完」，不是「用户没准备」。
    """
    _require_planning_ready(proj)
    from app.agents.workflow import generation_block
    block = generation_block(db, proj)
    if block:
        raise HTTPException(status_code=409, detail=block)


def _chapter_plan(
    db: Session, proj: Project, raw: str | None, *, allow_existing: bool = False
) -> dict:
    """章号入参 → 章标签；非法/撞章一律 400，不静默改数。"""
    try:
        return chapter_labels.resolve(db, proj, raw, allow_existing=allow_existing)
    except chapter_labels.ChapterRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _to_generation_out(gen: Generation) -> dict:
    return {
        "id": gen.id,
        "project_id": gen.project_id,
        "segment_no": gen.segment_no,
        "chapter_range": gen.chapter_range,
        "text": gen.text,
        "summary": gen.summary,
        # 复读闸门结论。`NULL` → verdict="undetected"（未检测），**不是**「正常」。
        "quality": parse_quality(gen.quality_json),
    }


@router.post("/generate")
def generate(body: GenerateBody, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, body.project_id)
    _require_workflow_ready(db, proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 章号：入参优先，缺省推导；冲突/越界在调模型前就 400（不烧额度）
    plan = _chapter_plan(db, proj, body.chapter_range)
    _begin_generation(proj.id)
    try:
        result = generate_next_segment(
            db, proj, novel_ids=novel_ids, max_chars=body.max_chars,
            chapter_range=plan["label"],
        )
    except (LengthBlocked, chapter_labels.ChapterIdentityError) as exc:
        # 两条生成侧硬判据：章身份解析失败 / 压缩后仍超篇幅上限 → 不落正文。
        # 422 带 blocked 明细（哪一段、原文、实测与上限），不是 500。
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except ValueError as exc:
        # 档案预检失败（未蒸馏）等可操作错误：409 + 明确指引，
        # 而不是 500 或把请求挂死在同步蒸馏上
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        _end_generation(proj.id)
    return {
        **_to_generation_out(_last_generation(db, proj.id)),
        # 落库仍是 200：复读只标记不拒绝（D7）。
        "repetition": result.get("repetition"),
        "length": result.get("length"),
        "summary_style": result.get("summary_style"),
        "warnings": result.get("warnings") or [],
        # 本章实际收到的章纲：缺纲/校验跳过显式回执，blocking 恒为 False。
        "outline": build_notice(db, proj, plan["chapter"], plan["chapter_end"]),
        "debug": {
            "system_prompt": result["system_prompt"],
            "user_prompt": result["user_prompt"],
            "constraints": result["constraints"],
            "rag_context": result["rag_context"],
        },
    }


@router.post("/generate-stream")
def generate_stream(body: GenerateBody, db: Session = Depends(get_db)):
    """流式续写：SSE(text/event-stream) 逐块下发，长文首字即显、不干等。

    生成器内部持有独立 session——StreamingResponse 跑在线程池，避免跨线程复用
    依赖注入的 session（SQLAlchemy Session 非线程安全）。
    """
    proj = _require_project(db, body.project_id)
    _require_workflow_ready(db, proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    plan = _chapter_plan(db, proj, body.chapter_range)
    # 开流前预检时间线写前闸门：一旦返回 StreamingResponse，生成器里抛的 409
    # 只能被吞成 SSE error 事件，前端拿不到状态码与结构化 detail，三种一键
    # 修复按钮就不会出现（浏览器实测：HTTP 200 + data:{"type":"error",...}）。
    timeline_written, chapter_based = _timeline_progress(db, proj)
    chapter = _chapter_ordinal(plan["label"])
    if chapter is None and chapter_based:
        chapter = timeline_written + 1
    from app.llm.provider import get_llm_provider

    preflight_timeline_gate(
        proj, timeline_written, get_llm_provider(), db,
        chapter=chapter, chapter_based=chapter_based,
    )
    # 在请求线程里先算好：SSE 生成器跑在线程池，不跨线程复用这个 session。
    outline_notice = build_notice(db, proj, plan["chapter"], plan["chapter_end"])
    # 生成器在依赖会话关闭后才迭代；commit 会 expire 全部字段，连 proj.id
    # 都会触发 DetachedInstanceError，所以这里先取出纯值。
    project_id = proj.id
    _begin_generation(project_id)

    def event_stream():
        from app.db import SessionLocal
        from app.generation.engine import generate_next_segment_stream

        sdb = SessionLocal()
        try:
            p = sdb.get(Project, project_id)
            for ev in generate_next_segment_stream(
                sdb, p, novel_ids=novel_ids, max_chars=body.max_chars,
                chapter_range=plan["label"],
            ):
                if ev.get("type") == "done":
                    ev = {**ev, "outline": outline_notice}
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except (LengthBlocked, chapter_labels.ChapterIdentityError) as exc:
            # 阻断也按 SSE 事件下发：前端能拿到 blocked 明细，而不是只看到断流。
            yield f"data: {_json.dumps({'type': 'error', 'message': str(exc), **exc.to_dict()}, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 把异常也作为 SSE 事件下发，前端可提示
            yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            sdb.close()
            _end_generation(project_id)

    try:
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    except Exception:
        _end_generation(project_id)
        raise


@router.post("/regenerate-stream")
def regenerate_stream(body: RegenerateBody, db: Session = Depends(get_db)):
    """重生成单段（流式 SSE）：与 /generate-stream 同协议，额外带 segment_no。

    章标签：**重写不推导新章号**。不传时保留目标段原标签（原本是 `段N` 就仍是
    `段N`）——重写不产生新内容，「本次章号 = 库内最大章号 +1」会把这个旧段悄悄
    挪到未来章节上，破坏按章号的趋势与台账；传了才改，且允许指向已存在的章
    （重写本身就是声明重写）。
    """
    proj = _require_project(db, body.project_id)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    target = _generation_by_no(db, proj.id, body.segment_no)
    if target is None:
        raise HTTPException(status_code=404, detail="段不存在")
    if body.chapter_range:
        plan = _chapter_plan(db, proj, body.chapter_range, allow_existing=True)
        label = plan["label"]
        span = (plan["chapter"], plan["chapter_end"])
    else:
        label = None  # 引擎保留原标签
        span = chapter_labels.label_span(target.chapter_range)
    # 没有章号（标签是「段N」）就不涉及章纲，回执整个省略，前端按缺省处理。
    outline_notice = build_notice(db, proj, span[0], span[1]) if span else None
    # 流式响应期间依赖会话已关闭：先取出纯值，生成器里绝不再读 ORM 属性
    # （commit 会 expire 全部字段，访问 proj.id 也会触发 DetachedInstanceError）。
    project_id = proj.id
    _begin_generation(project_id)

    # 两段式写入第一段：调模型前先写 pending 快照（与助手重写同一条链路）。
    # 工作台的重写此前不建快照，导致「撤销/回滚」只对助手重写可用。
    from app.agents.dialog.snapshot import SnapshotBusy, begin_snapshot

    try:
        snapshot = begin_snapshot(db, proj, target)
    except SnapshotBusy as exc:
        _end_generation(project_id)
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    # 生成器里不再碰 ORM 对象：流式响应期间依赖会话可能已关闭，先取出纯值。
    snapshot_id = snapshot.id
    expected_before = snapshot.before_text

    def event_stream():
        from app.db import SessionLocal
        from app.generation.engine import regenerate_segment_stream

        sdb = SessionLocal()
        try:
            p = sdb.get(Project, project_id)
            for ev in regenerate_segment_stream(
                sdb, p, body.segment_no, novel_ids=novel_ids, max_chars=body.max_chars,
                chapter_label=label,
                snapshot_id=snapshot_id,
                expected_before=expected_before,
            ):
                if outline_notice is not None and ev.get("type") == "done":
                    ev = {**ev, "outline": outline_notice}
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 异常也作为 SSE 事件下发
            from app.agents.dialog.snapshot import fail_snapshot

            fail_snapshot(sdb, snapshot_id, type(exc).__name__)
            yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            sdb.close()
            _end_generation(project_id)

    try:
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
    except Exception:
        _end_generation(project_id)
        raise


@router.post("/generate-loop")
def generate_loop_endpoint(body: GenerateLoopBody, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, body.project_id)
    _require_workflow_ready(db, proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 逐段章标签：一次解析完整段（区间入参按段展开），引擎只收标签不推导章号
    try:
        plans = chapter_labels.resolve_many(db, proj, body.chapter_range, body.segments)
    except chapter_labels.ChapterRangeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 在写库前算：回执描述的是「每段生成时实际拿到的章纲」，不是生成后的状态。
    outline_notices = [
        build_notice(db, proj, plan["chapter"], plan["chapter_end"]) for plan in plans
    ]
    _begin_generation(proj.id)
    try:
        results = generate_loop(
            db, proj, novel_ids=novel_ids, segments=body.segments, max_chars=body.max_chars,
            chapter_labels=[p["label"] for p in plans],
        )
    except (LengthBlocked, chapter_labels.ChapterIdentityError) as exc:
        # 前几段已按序落库，阻断段不落；回执里带 blocked 明细与段号。
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    except ValueError as exc:
        # 档案预检失败（未蒸馏）→ 409 + 指引，不挂死、不 500
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        _end_generation(proj.id)
    return {
        "segments": [
            {**_to_generation_out(_generation_by_no(db, proj.id, r["segment_no"])),
             "summary": r["summary"],
             "repetition": r.get("repetition"),
             "length": r.get("length"),
             "summary_style": r.get("summary_style"),
             "warnings": r.get("warnings") or [],
             "outline": outline_notices[index]}
            for index, r in enumerate(results)
        ],
        "last_system_prompt": results[-1]["system_prompt"],
        "constraints": results[-1]["constraints"],
    }


@router.post("/generate-plan")
def generate_plan(body: GeneratePlanBody, db: Session = Depends(get_db)) -> dict:
    """只组装上下文与提示词，不调用 LLM——生成前漂移预检与人工审阅。"""
    proj = _require_project(db, body.project_id)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 预检页展示「下一次生成会拿到哪一章的章纲」；推导规则与真实生成同源。
    plan = _chapter_plan(db, proj, None)
    ctx = build_context(db, proj, novel_ids=novel_ids)
    profile = _style_profile(db, proj)
    # 闸门带必须在拼提示**之前**写入：对白字占比的硬下限（v23）与极短纯对白段
    # 上限都写在 system 里。不写这一句，预检页会把常量表的 6% 当成真实下限，
    # 与实际下发的那份提示不一致。
    _apply_gate_limits(db, proj, seg_no=_next_segment_no(db, proj.id))
    system = build_system_prompt(
        proj.title, ctx["reminders"], _style_info(proj), proj.plot_bible,
        profile, _style_narrative(db, proj),
        style_source_ids=_style_source_ids(proj),
        # 预检必须与真实生成同口径：首段裁剪/角色卡遮蔽/压缩趋势降权都在这里生效。
        is_opening=not ctx["prior_blocks"] and not ctx["prior_summaries"],
        catalog_skeleton=_catalog_skeleton(db, proj),
        max_chars=proj.generation_length or 4000,
    )
    user = build_user_prompt(
        ctx["prior_summaries"], ctx["rag_context"], proj.outline,
        proj.generation_length or 4000, proj.plot_bible, proj.premise,
        prior_blocks=ctx["prior_blocks"],
        style_exemplars=ctx.get("style_exemplars"),
        rag_has_forbidden=ctx.get("rag_has_forbidden", False),
        # 预检页必须与真实生成同源：漏了这块，人工审阅看到的 prompt 就不是实际下发的那份
        kg_slice=ctx.get("kg_slice") or None,
        item_holding_block=ctx.get("item_holding_block") or None,
        # 预检是**只读**路径：不传 db/provider，既不落库也不因缺计划调模型。
        # 缺计划/预算耗尽时把状态写进提示块（由 build_node_prompt 渲染），
        # 真正的生成入口才用 409 暂停。
        node_prompt=current_node_prompt(
            proj, _next_segment_no(db, body.project_id) - 1
        ),
        # 预检必须与真实生成同源：设定卡的增删改要能在这里直接看到效果。
        story_cards_block=_render_story_cards(db, proj.id),
        foreshadow_block=ctx.get("foreshadow_block"),
    )
    return {
        "reminders": ctx["reminders"],
        "constraints": ctx["constraints"],
        "prior_summaries": ctx["prior_summaries"],
        # 预检页要能看见「真正下发的那份前情」有多长、丢了哪些卷 —— 否则前情被裁了
        # 用户完全无从察觉，只会在读到后面时觉得「它怎么忘了」
        "prior_blocks": ctx["prior_blocks"],
        "prior_report": ctx["prior_report"],
        "rag_context": ctx["rag_context"],
        # Phase 0.5：回传这次检索实际用的查询词（目标侧 + 衔接侧）。
        # 零成本（串本就在内存里），让「新查询词 vs 上一段摘要」的 A/B 不必跑生成。
        "rag_query": ctx.get("rag_query", ""),
        "style_exemplars": ctx["style_exemplars"],
        "system_prompt": system,
        "user_prompt": user,
        # 文风档案的预检摘要。这里刻意**不回传整份档案**（含 20 个特征的分布，
        # JSON 有 6.8KB）：前端要回答的问题是「下发的是哪份档案、它可不可信」，
        # 而不是复现统计。完整档案在 raw_novels.style_profile / projects.style_profile。
        "style_profile": _style_profile_digest(proj, profile),
        # 规则哨兵的预检摘要：哪几条硬冲突规则**当前真的会响**。
        # 必须显式说明 `item_owner_conflict` 默认沉默 —— 否则界面看上去像四条都在跑，
        # 用户会以为「没报错=没问题」，而实际是那条规则根本没参与。
        "sentinel": _sentinel_digest(proj),
        # 先看章纲：缺纲、空条目与被跳过的检查都会在这里，不藏在 prompt 里。
        "outline": build_notice(db, proj, plan["chapter"], plan["chapter_end"]),
        # 预检页同时暴露规划缺口（占位节点/上次规划失败），
        # 即使 `planning_guard_mode=report` 也能提前看到「这一章没有真实剧情点」。
        "planning": _planning_notice(proj),
    }


def _sentinel_digest(project: Project) -> dict:
    """哨兵的预检摘要：规则清单 + 本书的境界阶梯来源。"""
    from app.kg.sentinel import _active_rules, realm_order_from_project

    ladder = realm_order_from_project(project)
    # 未配置 = 不注入、不判定（与 power_prompt._realm_order_line 同口径）
    from_bible = bool(ladder)
    return {
        "rules": _active_rules(),
        "realm_systems": len(ladder),
        "realm_words": sum(len(g) for g in ladder),
        "realm_from_project": from_bible,
        "note": (
            "境界阶梯取自 plot_bible.realm_order"
            if from_bible else
            "未配 plot_bible.realm_order，不做境界判定（不注入、不抽境界事件）；"
            "要启用请在项目设定里给 realm_order"
        ),
    }


def _style_profile_digest(project: Project, profile: dict | None) -> dict | None:
    """文体档案的预检摘要（够判断可信度，不含逐特征分布）。"""
    if not profile:
        return {
            "available": False,
            "reason": (
                "未选参考语料，自由生成——不注入文风档案"
                if not (project.reference_novel_ids or [])
                else "参考语料不足或拟合失败，本次已降级为不注入"
            ),
        }
    from app.style.stylometry import describe

    from app.style import health_summary
    from app.style.pipeline import DIGEST_MARK_FIELDS

    health = profile.get("corpus_health") or {}
    # 逐字段挑，不做 `**m` 展开：档案是落库的 JSON，手工改过或来自更早的
    # PIPELINE_SCHEMA 时字段可能缺，缺字段不能让预检接口整个 500。
    # 形状正确性由指纹里的 schema 保证（见 pipeline.PIPELINE_SCHEMA）。
    marks = [
        {f: (k if f == "novel" else m.get(f)) for f in DIGEST_MARK_FIELDS}
        for k, v in health.items()
        for m in (v.get("marks") or [])
    ]
    return {
        "available": True,
        "label": profile.get("label") or "",
        "unit_chars": profile.get("unit_chars"),
        "n_units": profile.get("n_units"),
        "n_train": profile.get("n_train"),
        "n_holdout": profile.get("n_holdout"),
        "cleaned": bool(profile.get("clean")),
        "source_chars": profile.get("source_chars"),
        "removed_chars": sum(v.get("removed", 0) for v in health.values()),
        "health_line": health_summary(profile),
        "marks": marks,
        "holdout_p90": (profile.get("holdout") or {}).get("mean_abs_z", {}).get("p90"),
        "text": describe(profile),
    }


@router.get("/alerts/{project_id}")
def list_alerts(project_id: int, db: Session = Depends(get_db)) -> dict:
    """规则哨兵的历史告警（按段号倒序）。

    告警**落库**才谈得上复核 —— 只放在单次生成响应里的告警，
    用户当时没看就永远丢了，也就没法回答「上次那条到底是不是误报」。
    这里只读；哨兵的判定在生成时完成，本端点不做任何重新判定。
    """
    from app.kg.sentinel import _active_rules, load_findings

    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    items = load_findings(db, project_id)
    return {
        "items": items,
        "count": len(items),
        "by_severity": {
            s: sum(1 for i in items if i["severity"] == s) for s in ("err", "warn")
        },
        # 规则清单随告警一起下发：只看告警列表会以为「没告警=没问题」，
        # 而实际有规则是默认沉默的（见 _active_rules 的说明）
        "rules": _active_rules(),
    }


@router.get("/generations/{project_id}")
def list_generations(project_id: int, db: Session = Depends(get_db)) -> list[dict]:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no)
    ).all()
    return [_to_generation_out(g) for g in gens]


@router.patch("/generations/{project_id}/{generation_id}")
def update_generation_text(
    project_id: int,
    generation_id: int,
    body: UpdateGenerationTextBody,
    db: Session = Depends(get_db),
) -> dict:
    """用户手动编辑一段正文（不调模型改字，但会重算配套数据）。

    语义边界：
    - 只改 `text`。段号 / 章身份都不动：改字不应改变「这是第几段、对应
      哪一章」的身份。
    - `quality_json` 清空：复读、文风、结构结论都是对**旧正文**算的，
      改完必须回到「未检测」，不能把旧结论冒充成新正文的结论。
    - 正文落库后立即做配套重算（见 `_resync_after_manual_edit`）：
      剧情总结、L1/L2/L3 摘要树、v2.6 KG replay、v3 批抽重入队。
      任何一步失败都不回滚正文，但会把失败原因随响应带回，界面可见。
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    gen = db.get(Generation, generation_id)
    if gen is None or gen.project_id != project_id:
        raise HTTPException(status_code=404, detail="generation 不存在")
    text = (body.text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise HTTPException(status_code=400, detail="正文不能为空")
    if len(text) > 200_000:
        raise HTTPException(status_code=400, detail="正文过长（上限 200000 字）")
    old_text = (gen.text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if text == old_text:
        # 没改动就不重算：省一次摘要调用，也不把已有 quality 结论清掉
        return {
            **_to_generation_out(gen),
            "cascade": {
                "skipped": True,
                "summary": True,
                "tree": True,
                "kg": True,
                "v3_enqueued": False,
                "errors": [],
            },
        }
    gen.text = text
    gen.quality_json = None
    db.commit()
    db.refresh(gen)

    cascade = _resync_after_manual_edit(db, project, gen, text)
    return {**_to_generation_out(gen), "cascade": cascade}


def _resync_after_manual_edit(db: Session, project: Project, gen: Generation, text: str) -> dict:
    """手动改字后的配套重算：剧情总结 → 摘要树 → KG（v2.6 + v3）。

    顺序不能换：KG replay 与摘要树都从 `Generation` 表读正文/摘要，
    所以正文必须先 commit（本函数只在 commit 之后调用）。

    每一步独立兜底：正文已经落库，任何配套步骤失败都不能把保存变成失败。
    失败原因收进 `errors[]` 随响应下发，前端如实展示。
    """
    out: dict = {
        "skipped": False,
        "summary": False,
        "tree": False,
        "kg": False,
        "kg_stats": None,
        "v3_enqueued": False,
        "v3_pending": None,
        "errors": [],
    }

    # —— 1. 剧情总结（L1）——
    # 摘要由模型重写；超长段先做首尾截断（模型上下文/超时都不该被一段吃掉）。
    # 模型失败时退回确定性的截断摘要，保证新正文至少有一份对应的摘要；
    # 只有在连兜底都失败时才保留旧摘要并跳过摘要树重建。
    provider = None
    try:
        from app.llm.provider import LLMProvider, get_llm_provider

        provider = get_llm_provider()
        #: 送给摘要模型的最大字符数（首尾各一半，中间省略）。
        limit = 24_000
        if len(text) > limit:
            half = limit // 2
            summary_input = text[:half] + "\n……（中间省略）……\n" + text[-half:]
        else:
            summary_input = text
        try:
            summary_text = (provider.summarize(summary_input) or "").strip()
        except Exception as exc:  # noqa: BLE001 —— 模型超时/限流：退回本地截断
            summary_text = (LLMProvider.summarize(provider, summary_input) or "").strip()
            out["errors"].append(f"剧情总结模型调用失败，已用截断兜底：{str(exc)[:80]}")
            _logger.warning("手动改正文后摘要模型失败，改用截断兜底: %s", exc)
        if not summary_text:
            raise ValueError("模型返回了空摘要")
        merged = dict(gen.summary or {})
        merged["text"] = summary_text
        gen.summary = merged
        db.commit()
        db.refresh(gen)
        out["summary"] = True
    except Exception as exc:  # noqa: BLE001 —— 配套失败不阻断正文保存
        db.rollback()
        provider = None
        out["errors"].append(f"剧情总结重算失败：{str(exc)[:120]}")
        _logger.warning("手动改正文后重算摘要失败: %s", exc)

    # —— 2. 摘要树（L2 卷 / L3 主线都基于 L1，必须整棵重建）——
    if provider is not None:
        try:
            from app.generation.engine import rebuild_summary_tree

            rebuild_summary_tree(db, project, provider)
            out["tree"] = True
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out["errors"].append(f"摘要树重建失败：{str(exc)[:120]}")
            _logger.warning("手动改正文后重建摘要树失败: %s", exc)

    # —— 3. v2.6 KG：清空 generated 侧 + 按段 replay ——
    # 增量不可行（mentions 是累加值、坑状态是覆盖值），只能全量重放，
    # 与「重新生成」走同一个入口，避免两套口径。
    try:
        from app.kg.build import rebuild_generated_kg

        kg_stats = rebuild_generated_kg(db, project.id)
        out["kg"] = bool(kg_stats.get("complete", True))
        out["kg_stats"] = {
            "segments": kg_stats.get("segments"),
            "replayed": kg_stats.get("replayed"),
            "complete": kg_stats.get("complete"),
            "failed": kg_stats.get("failed") or [],
        }
        if not out["kg"]:
            out["errors"].append("图谱重建存在失败段")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        out["errors"].append(f"图谱重建失败：{str(exc)[:120]}")
        _logger.warning("手动改正文后重建图谱失败: %s", exc)

    # —— 4. v3 KG：本段按新正文重新入队（异步 worker 落库，旧版本保留可回溯）——
    try:
        from app.generation.engine import _chapter_ordinal, _enqueue_v3_extraction

        queued = _enqueue_v3_extraction(
            db,
            project,
            gen.segment_no,
            text,
            chapter=_chapter_ordinal(gen.chapter_range),
            rebuild=True,
        )
        out["v3_enqueued"] = bool(queued.get("enqueued"))
        out["v3_pending"] = queued.get("pending")
        if not out["v3_enqueued"]:
            out["errors"].append(f"v3 批抽入队失败：{queued.get('error') or '未知原因'}")
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        out["errors"].append(f"v3 批抽入队失败：{str(exc)[:120]}")
        _logger.warning("手动改正文后 v3 批抽入队失败: %s", exc)

    return out


@router.get("/projects/{project_id}/generation-status")
def generation_status(project_id: int, db: Session = Depends(get_db)) -> dict:
    """生成进度（前端轮询用）：段数 / 最新段与章 / 是否正在生成。

    为什么单独开一个端点而不是复用 `/generations/{pid}`：后者会把全部段落
    正文一起返回（长篇 40+ 段、几百 KB），每 2 秒轮询一次等于反复搬整本
    书。这里只回对账需要的四个字段，让卡片能显示「已确认 → 已落库 → 完成」
    的轨迹，而不是干等一句「执行中…」。

    `running` 来自进程内租约（`app.generation.inflight`），只反映
    `/generate` 等**走租约**的入口；助手同步调用没有租约，前端不能把它当成
    「没在写」的判据。
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")

    from app.generation.inflight import is_in_flight

    last = _last_generation(db, project_id)
    count = db.scalar(
        select(func.count())
        .select_from(Generation)
        .where(Generation.project_id == project_id)
    )
    total_chars = db.scalar(
        select(func.coalesce(func.sum(func.length(Generation.text)), 0))
        .where(Generation.project_id == project_id)
    )
    return {
        "project_id": project_id,
        "count": int(count or 0),
        "total_chars": int(total_chars or 0),
        "last_segment_no": last.segment_no if last is not None else None,
        "last_chapter": last.chapter_range if last is not None else None,
        "running": is_in_flight(project_id),
    }


# --- 内部小工具：从库中取回刚写入的 Generation，保证响应与落库一致 ---

def _last_generation(db: Session, project_id: int) -> Generation:
    return db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no.desc())
    ).first()


def _generation_by_no(db: Session, project_id: int, segment_no: int) -> Generation:
    return db.scalars(
        select(Generation).where(
            Generation.project_id == project_id, Generation.segment_no == segment_no
        )
    ).first()


def _load_snapshot(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = _json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@router.get("/projects/{project_id}/segments/{segment_no}/snapshot")
def get_segment_snapshot(
    project_id: int, segment_no: int, db: Session = Depends(get_db)
) -> dict:
    """返回某段生成时的提示词与权威快照（B 子系统第二刀）。

    历史行（快照列落地前生成）返回 `prompt: null` 并在 `note` 说明原因，
    不伪造内容、不拿当前项目状态回填。
    """
    gen = _generation_by_no(db, project_id, segment_no)
    if gen is None:
        raise HTTPException(status_code=404, detail="该段不存在")

    prompt = _load_snapshot(gen.prompt_json)
    return {
        "project_id": project_id,
        "segment_no": segment_no,
        "chapter_range": gen.chapter_range,
        "prompt": prompt,
        "authority": _load_snapshot(gen.authority_json),
        "prompt_sha256": (prompt or {}).get("sha256"),
        "note": None if prompt else "该段生成于快照列落地前，没有提示词快照",
    }
