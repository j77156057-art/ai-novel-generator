"""M4 生成环路由：续写单段 / 续写多段 / 流式续写 / 生成前预览(plan) / 查看已生成段。

端点：
- POST /api/generate            续写一段（同步，等整段返回）
- POST /api/generate-loop       连续续写多段
- POST /api/generate-stream     流式续写（SSE，长文首字即显）
- POST /api/generate-plan       仅组装上下文与提示词（不调 LLM），供生成前漂移预检
- GET  /api/generations/{pid}   列出某项目已生成的全部段落
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel  # noqa: F401 - 供类型标注/未来扩展保留
from sqlalchemy import select
from sqlalchemy.orm import Session

import json as _json

from app.db import get_db
from app.generation import (
    build_context,
    generate_loop,
    generate_next_segment,
)
from app.generation.engine import _style_info, _style_narrative, _style_profile, _next_segment_no, current_node_prompt, _l2_character_names
from app.generation.prompts import build_system_prompt, build_user_prompt
from app.models import Generation, Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api", tags=["generation"])


class GenerateBody(StrictRequest):
    project_id: int
    novel_ids: list[int] | None = None  # 缺省用 project.reference_novel_ids
    max_chars: int | None = None  # 缺省用 project.generation_length
    # v2.278：单次请求覆盖文风改写。None = 跟随项目/全局。
    # True = 这段一定改写、False = 这段一定不改、None = 跟随（默认走**闸门**：
    # 偏离参考书越远的那段才动，尺子 = 参考书自己的 p10/p90）。
    # 两段式改写会让句长从 ~16 提到 ~60（实测 M3 关思考），但压缩 15-25% 字数、
    # 每段多一次调用 —— 所以默认不每段都改。
    style_rewrite: bool | None = None
    # v2.276：生成模式。"original" 原创（默认，不给参考语料原文）/
    # "continuation" 续写（可见原文，锚定原书）/"roleplay" 角色聊天（只见单角色）。
    # 缺省原创：可见范围最小，出错最多是「参考资料变少」，
    # 不会把参考书的世界观悄悄灌进用户的小说。
    profile: str | None = None


class GenerateLoopBody(StrictRequest):
    project_id: int
    novel_ids: list[int] | None = None
    # 默认 1（历史值为 3，此处刻意下调并留痕）：引擎的设计是「一次长段」而非多段短循环
    # ——短循环会把前情/大纲反复塞进上下文，既压低提示词缓存命中率又易触发会话压缩
    #（见 engine.generate_next_segment 的 docstring）。要连写多段请调用方显式给数，
    # 缺省不该悄悄替用户连生成 3 段、还让他以为只写了一段。
    segments: int = 1
    # v2.278：单次请求覆盖文风改写。None = 跟随项目/全局。
    # True = 这段一定改写、False = 这段一定不改、None = 跟随（默认走**闸门**：
    # 偏离参考书越远的那段才动，尺子 = 参考书自己的 p10/p90）。
    # 两段式改写会让句长从 ~16 提到 ~60（实测 M3 关思考），但压缩 15-25% 字数、
    # 每段多一次调用 —— 所以默认不每段都改。
    style_rewrite: bool | None = None
    # v2.276：生成模式。"original" 原创（默认，不给参考语料原文）/
    # "continuation" 续写（可见原文，锚定原书）/"roleplay" 角色聊天（只见单角色）。
    # 缺省原创：可见范围最小，出错最多是「参考资料变少」，
    # 不会把参考书的世界观悄悄灌进用户的小说。
    profile: str | None = None
    max_chars: int | None = None


class GeneratePlanBody(StrictRequest):
    project_id: int
    novel_ids: list[int] | None = None
    # v2.276：生成模式。"original" 原创（默认，不给参考语料原文）/
    # "continuation" 续写（可见原文，锚定原书）/"roleplay" 角色聊天（只见单角色）。
    # 缺省原创：可见范围最小，出错最多是「参考资料变少」，
    # 不会把参考书的世界观悄悄灌进用户的小说。
    profile: str | None = None


class RegenerateBody(StrictRequest):
    project_id: int
    segment_no: int
    # v2.276：生成模式。"original" 原创（默认，不给参考语料原文）/
    # "continuation" 续写（可见原文，锚定原书）/"roleplay" 角色聊天（只见单角色）。
    # 缺省原创：可见范围最小，出错最多是「参考资料变少」，
    # 不会把参考书的世界观悄悄灌进用户的小说。
    profile: str | None = None
    novel_ids: list[int] | None = None
    max_chars: int | None = None


def _generate_guarded(fn, *args, **kwargs):
    """跑生成，**照搬门禁**的失败转成 409（其余异常照旧冒出去）。"""
    from app.generation.verbatim import VerbatimViolation

    try:
        return fn(*args, **kwargs)
    except VerbatimViolation as exc:
        raise _verbatim_conflict(exc) from exc


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _verbatim_conflict(exc: Exception) -> HTTPException:
    """照搬门禁拦下 ⇒ 转成 409 + **人话**说明。

    为什么不让异常自己冒出去：默认会变成 500 "Internal Server Error"，
    用户只看到「出错了」，不知道这一段为什么没保存、也不知道能不能重试。

    内部代号（possible_copy）只放在 `status` 里给程序看，
    `message` 是写给人看的。
    """
    spans = list(getattr(exc, "spans", None) or [])
    return HTTPException(status_code=409, detail={
        "status": "possible_copy",
        "message": ("这一段里有地方和参考语料是一字不差的，已经自动改过一遍，"
                    "还是没能换掉，所以这次没有保存。重新生成一次就好。"),
        "match_count": len(spans),
        "excerpts": [str(s.get("text") or "")[:60] for s in spans[:3]],
    })


def _require_workflow_ready(db: Session, proj: Project) -> None:
    """生成前的**工序链闸门**：流程没过就别生成（别绕过流程）。

    拦的是两种具体缺口（不是「项目没资料」那种自由生成）：

    - 有待批的写回建议 → 正文要改完才能继续生成；
    - 文风档案没真进到提示词里（提示词体检有 error）→ 生成出来也是白写。

    自由生成（没选参考语料、没有文风档案）一律放行 ——
    闸门拦的是「流程没走完」，不是「用户没准备」。
    """
    from app.agents.workflow import generation_block
    block = generation_block(db, proj)
    if block:
        raise HTTPException(status_code=409, detail=block)


def _require_story_seed(proj: Project) -> None:
    """生成前必须有「故事种子」——premise / outline / plot_bible 至少一个非空。

    ## 为什么必须在 HTTP 层拦

    v2.272 实测：一个只有 title + 参考语料、没有 premise/outline/plot_bible 的项目
    连写 5 段，**5 段全在重写参考小说的第 1 章**（第 4/5 段前 3 句几乎逐字相同）。

    原因不是模型不行，是它真的没有别的可写：项目里没有任何「接下来要发生什么」的
    信息，唯一的素材就是参考语料，于是每段都从语料开头重启。用户拿到的是 5 遍
    同一个开场，而且没有任何报错——**这种失败必须显式拦下来**，不能让用户对着
    一堆重复正文猜哪里出了问题。

    校验放在路由层而不是 engine 层：engine 的单元测试需要能在无大纲的情况下跑
    组装逻辑（测的是上下文拼装，不是故事完整性），拦在 HTTP 边界即可。
    """
    has_premise = bool((proj.premise or "").strip())
    outline = proj.outline
    has_outline = bool(outline) if not isinstance(outline, str) else bool(outline.strip())
    bible = proj.plot_bible
    has_bible = bool(bible) if not isinstance(bible, dict) else bool(
        (bible.get("characters") or bible.get("foreshadowing") or bible.get("relationships"))
    )
    if has_premise or has_outline or has_bible:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "项目缺少故事种子：premise（一句话设定）/ outline（分章大纲）/ "
            "plot_bible（人物与伏笔）三者至少填一个再生成。"
            "否则模型只能把参考语料的开篇重写一遍——实测会连写多段同一个开场。"
            "可先调 POST /api/generate-plan 看当前提示词里到底有什么。"
        ),
    )


def _resolve_novel_ids(proj: Project, override: list[int] | None) -> list[int]:
    if override:
        return override
    return list(proj.reference_novel_ids or [])


def _resolve_profile(proj: Project, profile: str | None) -> str:
    """把「这次生成走哪条创作路径」解析成确定的一条。

    请求里显式写了 profile 就尊重显式值；没写就按项目的锚定方式推导：
    锚定在参考书上的项目（`story_authority='reference_anchor'`）走续写路径，
    否则走原创路径。详见 `app/generation/profiles.py:resolve_profile`。
    """
    from app.generation.profiles import resolve_profile

    return resolve_profile(proj, profile)


def _to_generation_out(gen: Generation) -> dict:
    return {
        "id": gen.id,
        "project_id": gen.project_id,
        "segment_no": gen.segment_no,
        "chapter_range": gen.chapter_range,
        "text": gen.text,
        "summary": gen.summary,
    }


@router.post("/generate")
def generate(body: GenerateBody, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, body.project_id)
    _require_workflow_ready(db, proj)
    _require_story_seed(proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 请求体的 style_rewrite 是**单次**覆盖，不写进项目。
    # 旧实现写的是 `proj.style_rewrite = 0/1` + commit —— 那是**项目级持久设置**，
    # 与「允许单次试用而不改项目配置」的说法相反；而且 False 写进去也没用
    # （库里 0 与「从没设过」分不开，引擎会回落到全局开关）。
    # 现在直接透传给引擎：True = 这段都改、False = 这段别改、None = 跟随项目/全局。
    result = _generate_guarded(
        generate_next_segment,
        db, proj, novel_ids=novel_ids, max_chars=body.max_chars,
        profile=body.profile, style_rewrite=body.style_rewrite,
    )
    return {
        **_to_generation_out(_last_generation(db, proj.id)),
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
    _require_story_seed(proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)

    def event_stream():
        from app.db import SessionLocal
        from app.generation.engine import generate_next_segment_stream

        sdb = SessionLocal()
        try:
            p = sdb.get(Project, proj.id)
            for ev in generate_next_segment_stream(
                sdb, p, novel_ids=novel_ids, max_chars=body.max_chars,
                profile=body.profile, style_rewrite=body.style_rewrite,
            ):
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 把异常也作为 SSE 事件下发，前端可提示
            from app.generation.verbatim import VerbatimViolation
            if isinstance(exc, VerbatimViolation):
                # 照搬门禁：给**人话**，别把内部代号甩给用户
                _d = _verbatim_conflict(exc).detail
                yield f"data: {_json.dumps({'type': 'error', 'message': _d['message'], 'status': _d['status'], 'match_count': _d['match_count']}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            sdb.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/regenerate-stream")
def regenerate_stream(body: RegenerateBody, db: Session = Depends(get_db)):
    """重生成单段（流式 SSE）：与 /generate-stream 同协议，额外带 segment_no。"""
    proj = _require_project(db, body.project_id)
    _require_workflow_ready(db, proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)

    def event_stream():
        from app.db import SessionLocal
        from app.generation.engine import regenerate_segment_stream

        sdb = SessionLocal()
        try:
            p = sdb.get(Project, proj.id)
            for ev in regenerate_segment_stream(
                sdb, p, body.segment_no, novel_ids=novel_ids, max_chars=body.max_chars
            ):
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001 - 异常也作为 SSE 事件下发
            from app.generation.verbatim import VerbatimViolation
            if isinstance(exc, VerbatimViolation):
                # 照搬门禁：给**人话**，别把内部代号甩给用户
                _d = _verbatim_conflict(exc).detail
                yield f"data: {_json.dumps({'type': 'error', 'message': _d['message'], 'status': _d['status'], 'match_count': _d['match_count']}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {_json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            sdb.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/generate-loop")
def generate_loop_endpoint(body: GenerateLoopBody, db: Session = Depends(get_db)) -> dict:
    proj = _require_project(db, body.project_id)
    _require_workflow_ready(db, proj)
    _require_story_seed(proj)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 同 /generate：单次覆盖，不写进项目（见那里的说明）。
    results = _generate_guarded(
        generate_loop,
        db, proj, novel_ids=novel_ids, segments=body.segments, max_chars=body.max_chars,
        profile=body.profile, style_rewrite=body.style_rewrite,
    )
    return {
        "segments": [
            {**_to_generation_out(_generation_by_no(db, proj.id, r["segment_no"])),
             "summary": r["summary"]}
            for r in results
        ],
        "last_system_prompt": results[-1]["system_prompt"],
        "constraints": results[-1]["constraints"],
    }


@router.post("/generate-plan")
def generate_plan(body: GeneratePlanBody, db: Session = Depends(get_db)) -> dict:
    """只组装上下文与提示词，不调用 LLM——生成前漂移预检与人工审阅。"""
    proj = _require_project(db, body.project_id)
    novel_ids = _resolve_novel_ids(proj, body.novel_ids)
    # 缺省路径按项目的锚定方式推导（锚定在参考书上的项目走续写路径）——
    # 预检必须看到**和真正下发一致**的那份 prompt，路径不一致就失去意义。
    profile = _resolve_profile(proj, body.profile)
    ctx = build_context(db, proj, novel_ids=novel_ids, profile=profile)
    style_prof = proj.style_profile  # Preview reads prepared cache only; no fitting writes.
    system = build_system_prompt(
        proj.title, ctx["reminders"], _style_info(proj), proj.plot_bible,
        style_prof, ctx.get("style_narrative"),
        profile=profile,
        character_names=_l2_character_names(proj, ctx, profile),
    )
    _node = current_node_prompt(proj, _next_segment_no(db, body.project_id) - 1)
    # v2.290：预检页必须看到和真正下发一致的那份 prompt。
    # 少了 segment_no / prev_scene，预检里就没有【本节大纲目标】和场景转移约束，
    # 用户会以为「生成时也没这些东西」——预检的意义就是所见即所发。
    from app.generation.engine import _prev_scene_hint
    _seg = None if _node else _next_segment_no(db, body.project_id)
    user = build_user_prompt(
        ctx["prior_summaries"], ctx["rag_context"], proj.outline,
        proj.generation_length or 3000, proj.plot_bible, proj.premise,
        prior_blocks=ctx["prior_blocks"],
        style_exemplars=ctx.get("style_exemplars"),
        rag_has_forbidden=ctx.get("rag_has_forbidden", False),
        node_prompt=_node,
        rag_blocks=ctx.get("rag_blocks") or None,
        kg_slice=ctx.get("kg_slice") or None,
        segment_no=_seg,
        prev_scene=None if _node else _prev_scene_hint(db, proj),
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
        "style_exemplars": ctx["style_exemplars"],
        "visibility_diagnostics": ctx["visibility_diagnostics"],
        "system_prompt": system,
        "user_prompt": user,
        # 文风档案的预检摘要。这里刻意**不回传整份档案**（含 20 个特征的分布，
        # JSON 有 6.8KB）：前端要回答的问题是「下发的是哪份档案、它可不可信」，
        # 而不是复现统计。完整档案在 raw_novels.style_profile / projects.style_profile。
        "style_profile": _style_profile_digest(proj, style_prof),
        # 规则哨兵的预检摘要：哪几条硬冲突规则**当前真的会响**。
        # 必须显式说明 `item_owner_conflict` 默认沉默 —— 否则界面看上去像四条都在跑，
        # 用户会以为「没报错=没问题」，而实际是那条规则根本没参与。
        "sentinel": _sentinel_digest(proj),
        # v2.276：本次按 profile 裁掉了什么、为什么——用户要能看见
        # 「为什么这次没有检索上下文」，否则会以为功能坏了。
        "profile": ctx.get("_profile"),
        "profile_warnings": ctx.get("profile_warnings") or [],
    }


def _sentinel_digest(project: Project) -> dict:
    """哨兵的预检摘要：规则清单 + 本书的境界阶梯来源。"""
    from app.kg.sentinel import REALM_SYSTEMS, REALM_ORDER, _active_rules, realm_order_from_project

    ladder = realm_order_from_project(project)
    # 项目自己配了就明说是自配的；用默认表则说明只覆盖常见档位
    from_bible = ladder is not REALM_SYSTEMS
    return {
        "rules": _active_rules(),
        "realm_systems": len(ladder),
        "realm_words": sum(len(g) for g in ladder),
        "realm_from_project": from_bible,
        "realm_default_words": len(REALM_ORDER),
        "note": (
            "境界阶梯取自 plot_bible.realm_order"
            if from_bible else
            "未配 plot_bible.realm_order，用默认阶梯（只覆盖常见玄幻档位；"
            "本书自造档位不参与判定）"
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
