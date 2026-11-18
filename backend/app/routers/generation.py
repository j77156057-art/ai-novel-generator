"""M4 生成环路由：续写单段 / 续写多段 / 流式续写 / 生成前预览(plan) / 查看已生成段。

端点：
- POST /api/generate            续写一段（同步，等整段返回）
- POST /api/generate-loop       连续续写多段
- POST /api/generate-stream     流式续写（SSE，长文首字即显）
- POST /api/generate-plan       仅组装上下文与提示词（不调 LLM），供生成前漂移预检
- GET  /api/generations/{pid}   列出某项目已生成的全部段落
- GET  /api/projects/{pid}/segments/{no}/snapshot  取某段生成时的提示词/权威快照
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel  # noqa: F401 - 供类型标注/未来扩展保留
from sqlalchemy import select
from sqlalchemy.orm import Session

import json as _json

from app.db import get_db
from app.generation import chapter_labels
from app.generation import (
    build_context,
    generate_loop,
    generate_next_segment,
)
from app.generation.engine import _catalog_skeleton, _style_info, _style_narrative, _style_profile, _style_source_ids, _next_segment_no, current_node_prompt
from app.generation.length_guard import LengthBlocked
from app.generation.outline_notice import build_notice
from app.generation.prompts import build_system_prompt, build_user_prompt
from app.generation.repetition import parse_quality
from app.models import Generation, Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api", tags=["generation"])


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


def _require_workflow_ready(db: Session, proj: Project) -> None:
    """生成前的**工序链闸门**：流程没过就别生成（别绕过流程）。

    拦的是具体缺口（不是「项目没资料」那种自由生成）：

    - 有待批的写回建议 → 正文要改完才能继续生成；
    - 文风档案没真进到提示词里（提示词体检有 error）→ 生成出来也是白写。

    自由生成（没选参考语料、没有文风档案）一律放行 ——
    闸门拦的是「流程没走完」，不是「用户没准备」。
    """
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
    # 在请求线程里先算好：SSE 生成器跑在线程池，不跨线程复用这个 session。
    outline_notice = build_notice(db, proj, plan["chapter"], plan["chapter_end"])
    _begin_generation(proj.id)

    def event_stream():
        from app.db import SessionLocal
        from app.generation.engine import generate_next_segment_stream

        sdb = SessionLocal()
        try:
            p = sdb.get(Project, proj.id)
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
            _end_generation(proj.id)

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
        _end_generation(proj.id)
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
    _begin_generation(proj.id)

    def event_stream():
        from app.db import SessionLocal
        from app.generation.engine import regenerate_segment_stream

        sdb = SessionLocal()
        try:
            p = sdb.get(Project, proj.id)
            for ev in regenerate_segment_stream(
                sdb, p, body.segment_no, novel_ids=novel_ids, max_chars=body.max_chars,
                chapter_label=label,
            ):
                if outline_notice is not None and ev.get("type") == "done":
                    ev = {**ev, "outline": outline_notice}
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 异常也作为 SSE 事件下发
            yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            sdb.close()
            _end_generation(proj.id)

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
        _end_generation(proj.id)
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
    system = build_system_prompt(
        proj.title, ctx["reminders"], _style_info(proj), proj.plot_bible,
        profile, _style_narrative(db, proj),
        style_source_ids=_style_source_ids(proj),
        # 预检必须与真实生成同口径：首段裁剪/角色卡遮蔽/压缩趋势降权都在这里生效。
        is_opening=not ctx["prior_blocks"] and not ctx["prior_summaries"],
        catalog_skeleton=_catalog_skeleton(db, proj),
        max_chars=proj.generation_length or 4000,
    )
    from app.llm.provider import get_llm_provider
    user = build_user_prompt(
        ctx["prior_summaries"], ctx["rag_context"], proj.outline,
        proj.generation_length or 4000, proj.plot_bible, proj.premise,
        prior_blocks=ctx["prior_blocks"],
        style_exemplars=ctx.get("style_exemplars"),
        rag_has_forbidden=ctx.get("rag_has_forbidden", False),
        # 预检页必须与真实生成同源：漏了这块，人工审阅看到的 prompt 就不是实际下发的那份
        kg_slice=ctx.get("kg_slice") or None,
        item_holding_block=ctx.get("item_holding_block") or None,
        node_prompt=current_node_prompt(
            proj, _next_segment_no(db, body.project_id) - 1, get_llm_provider(), db=db
        ),
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
