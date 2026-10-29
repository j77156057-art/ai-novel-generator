"""项目 + 知识图谱路由：建项目 → 分步更新 → 建图谱 → 查询角色/坑 → 生成前提醒 → 导出txt。"""

import re
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.engine import _style_info
from app.kg.build import build_graph
from app.kg.queries import get_character, get_graph, get_reminders
from app.models import DistillRun, Generation, Project, StyleDistill
from app.schemas import ProjectCreate, ProjectOut, ProjectPatch, StrictRequest
from app.style.jobs import (
    create_distill_run,
    distill_run_to_dict,
    find_active_run,
    run_distill_job,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


class DistillStartRequest(StrictRequest):
    """`POST /api/projects/{id}/distill` 的请求体。

    继承 `StrictRequest`（未知字段 422）：拼错 `tier` 写成 `tiers` 会被立刻拒掉，
    而不是静默按默认档位跑一整套 40 分钟的蒸馏——那是最贵的一种「静默无效」。
    """

    #: 档位 key（full/balanced/fast）。不给则沿用项目上已存的 `narrative_tier`，
    #: 那也没有则用默认完整档。
    tier: str | None = None
    #: True = 同档位也强制重算（忽略指纹缓存）。
    force: bool = False


class StyleDistillApplyRequest(StrictRequest):
    """`POST /api/projects/{id}/style-distills/apply` 的请求体。

    把蒸馏库里某条已蒸馏档案**应用到本项目**。继承 `StrictRequest`：拼错字段名会被
    立刻 422，而不是静默当成「空 body」把 distill_id 当 0 去查。
    """

    #: 要应用的蒸馏库条目 id（来自 `GET /api/projects/style-distills` 列表）。
    distill_id: int


class PlanExtensionRequest(StrictRequest):
    """`POST /api/projects/{id}/plan-extension` 的请求体。

    继承 `StrictRequest`：多写一个字段就 422，而不是静默按默认 count=1 跑一整轮
    （这一轮要调模型，不该被拼错的字段悄悄改掉规模）。
    """

    #: 追加几个剧情点（1..3）。
    count: int = 1


def _validate_visibility(db, values):
    from app.models import NovelChunk
    if values.get("story_authority") not in ("generated_branch", "reference_anchor"):
        raise HTTPException(400, "story_authority 必须为 generated_branch 或 reference_anchor，不能为 null")
    start = values.get("continuation_start_chapter")
    if start is not None and start < 1:
        raise HTTPException(400, "continuation_start_chapter 必须 >= 1")
    selected = set(values.get("reference_novel_ids") or [])
    seen = set()
    for item in values.get("reference_cutoffs") or []:
        nid, ch = item.get("novel_id"), item.get("chapter")
        if set(item) != {"novel_id", "chapter"} or type(nid) is not int or type(ch) is not int or ch < 1 or nid not in selected or nid in seen:
            raise HTTPException(400, "reference_cutoffs 必须为已选语料的唯一 novel_id + 正整数 chapter")
        if db.scalar(select(NovelChunk.id).where(NovelChunk.novel_id == nid, NovelChunk.chapter_no == ch).limit(1)) is None:
            raise HTTPException(400, "reference_cutoffs 章节不存在")
        seen.add(nid)


@router.post("", response_model=ProjectOut)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    _validate_visibility(db, body.model_dump())
    proj = Project(
        story_authority=body.story_authority,
        reference_cutoffs=body.reference_cutoffs,
        title=body.title,
        target_length=body.target_length,
        mode=body.mode,
        style_dims=body.style_dims,
        outline=body.outline,
        status="created",
        genre=body.genre,
        style=body.style,
        premise=body.premise,
        reference_novel_ids=body.reference_novel_ids,
        continuation_start_chapter=body.continuation_start_chapter,
        generation_length=body.generation_length,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    """列出全部项目（服务端为单一事实来源；前端不再依赖 localStorage 缓存）。"""
    projects = list(db.scalars(select(Project).order_by(Project.id.desc())).all())
    for p in projects:
        p.segment_count = db.scalar(
            select(func.count(Generation.id)).where(Generation.project_id == p.id)
        ) or 0
    return projects


@router.get("/narrative-tiers")
def narrative_tiers() -> dict:
    """列出全部蒸馏档位（前端选档用）。

    静态元数据，不碰 DB。

    **必须声明在 `/{project_id}` 之前**：FastAPI 按声明顺序匹配路由，
    `project_id: int` 会把 "narrative-tiers" 拒成 422，静态段就永远匹配不到。
    """
    from app.style.narrative import DEFAULT_TIER, list_tiers

    return {"default": DEFAULT_TIER, "tiers": list_tiers()}


# ====================================================================
# 文风蒸馏后台任务（进度条的三条端点）
# ====================================================================
#
# 为什么整块声明在 `/{project_id}` 之前：FastAPI 按**声明顺序**匹配路由。
# `narrative-tiers` 是踩过的实例（被 `project_id: int` 拒成 422），这三条的
# 路径里虽然带 `{project_id}`，但 `/{project_id}` 是**静态一截**的 GET，
# 一旦将来有人在它前面/后面改了路径参数类型，遮蔽关系就会变。统一把带
# 静态子段的路由压在裸 `/{project_id}` 之前，是最省心的排法。
#
# 为什么蒸馏必须有后台任务：833 章语料跑完整档实测 42 分钟，HTTP 请求挂不住。
# 这里只登记 `DistillRun` 并立刻返回 job_id，真正的蒸馏由 BackgroundTasks
# 在线程池里跑（见 app/style/jobs.py）。


@router.post("/{project_id}/distill")
def start_distill(
    project_id: int,
    body: DistillStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """提交一次文风蒸馏：**立即返回** job_id，蒸馏在后台跑。

    前端拿 job_id 轮询 `GET .../distill/jobs/{job_id}` 看进度。这样即便用户
    关掉页面、刷新、切到别的步骤，蒸馏也不中断、进度也不丢。

    并发防重：项目上已有 pending/running 的**同档位**任务且不 force 时，
    直接复用那条（返回它的 job_id，`reused: True`），不重复烧一遍 LLM。

    缓存命中预测：不 force 时若本次会直接复用「项目已存档案」或「蒸馏库」，
    返回 `from_cache: True` 与 `cache_source`（"project" / "library"）。
    前端据此如实提示「秒级复用、未重新调用模型」，避免「一秒蒸馏完却没解释」。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")

    from app.style.narrative import resolve_tier

    # 档位优先级：本次显式传的 > 项目已存的偏好 > 默认完整档。
    # 非法档位在这里就 400 —— 不让它变成一个跑 40 分钟才失败的后台任务。
    try:
        cfg = resolve_tier(body.tier or getattr(proj, "narrative_tier", None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 选档是项目级偏好：存下来，之后不带 tier 的调用（含生成链路）也按它来。
    if proj.narrative_tier != cfg.key:
        proj.narrative_tier = cfg.key
        db.commit()

    # —— 缓存命中预测（不调模型，秒级返回）——
    # 在登记任务之前就判断：本次蒸馏会不会直接复用「项目已存档案」或「蒸馏库」，
    # 把这件事如实回给前端。否则前端只会看到 reused:False → 误以为「已提交后台运行中」，
    # 实际后台 0.3s 就 replay 完缓存，用户感知就是「一秒蒸馏完了却没解释」。
    from_cache = False
    cache_source = None
    if not body.force:
        ids = [int(i) for i in (proj.reference_novel_ids or [])]
        if ids:
            from app.style.narrative import (
                corpus_content_hash,
                find_style_distill,
                narrative_fingerprint,
            )

            content_hash = corpus_content_hash(ids, db)
            want = narrative_fingerprint(ids, cfg, corpus_hash=content_hash)
            cached = proj.style_narrative or {}
            if isinstance(cached, dict) and cached.get("fingerprint") == want:
                from_cache = True
                cache_source = "project"
            else:
                content_hash = corpus_content_hash(ids, db)
                existing = find_style_distill(
                    db, ids, cfg.key, want["tier_signature"], content_hash
                )
                if existing is not None:
                    from_cache = True
                    cache_source = "library"

    if not body.force:
        # 同档位才复用：换档位的指纹不同，必须重算，复用会静默拿旧档结果。
        active = find_active_run(db, project_id)
        if active is not None and active.tier == cfg.key:
            return {
                "job_id": active.id,
                "status": active.status,
                "tier": active.tier,
                "reused": True,
                "from_cache": False,
                "cache_source": None,
            }

    run = create_distill_run(db, project_id, cfg.key)
    background_tasks.add_task(
        run_distill_job, run.id, project_id, cfg.key, body.force
    )
    return {
        "job_id": run.id,
        "status": run.status,
        "tier": run.tier,
        "reused": False,
        "from_cache": from_cache,
        "cache_source": cache_source,
    }


@router.get("/{project_id}/distill/jobs")
def list_distill_jobs(
    project_id: int,
    limit: int = Query(default=5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """列出该项目最近的蒸馏任务（id 降序），供前端「回到页面续上进度」。

    前端进工作台时拉最近一条：若它还是 pending/running，就接着轮询，
    用户于是看到「刚才那次蒸馏还在跑、跑到第几块」，而不是一条空白。
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    rows = (
        db.scalars(
            select(DistillRun)
            .where(DistillRun.project_id == project_id)
            .order_by(DistillRun.id.desc())
            .limit(limit)
        )
        .all()
    )
    return {"items": [distill_run_to_dict(r) for r in rows]}


@router.get("/{project_id}/distill/jobs/{job_id}")
def get_distill_job(
    project_id: int, job_id: int, db: Session = Depends(get_db)
) -> dict:
    """单条蒸馏任务的状态（进度条轮询的就是它）。

    `project_id` 参与校验：job_id 猜错不该看到别的项目在跑什么。
    """
    run = db.get(DistillRun, job_id)
    if run is None or run.project_id != project_id:
        raise HTTPException(status_code=404, detail="蒸馏任务不存在")
    return distill_run_to_dict(run)


# ====================================================================
# 文风蒸馏库（跨项目复用）
# ====================================================================
#
# 为什么整块声明在 `/{project_id}` 之前：与上面的蒸馏三端点同理由 ——
# FastAPI 按声明顺序匹配，静态子段路由压在裸 `/{project_id}` 之前最省心，
# 不会出现「被 int 路径参数吞掉」的遮蔽。
#
# 复用判据见 app.style.narrative.find_style_distill / _persist_style_distill 的注释：
# 四键 (source_ids_hash, tier, tier_signature, content_hash) + active 态。


def _tier_name(key: str | None) -> str | None:
    """档位 key → 可读名（完整 / 平衡 / 快速），用于蒸馏库里「标清楚蒸馏程度」。

    库行只存 tier key，展示时再解析；解析失败（未知 key）回落原 key，不报错。
    """
    if not key:
        return None
    try:
        from app.style.narrative import resolve_tier

        return resolve_tier(key).name
    except Exception:
        return key


def _sd_summary(row: StyleDistill) -> dict:
    """蒸馏库条目 → 列表用的摘要（**不含** style_narrative 全文与 rules）。"""
    return {
        "id": row.id,
        # 可读的档位键（`full:9a3f1166f4e6:1f2a3b4c`）—— 与人物塑造模板
        # 的 id 同一套格式：哪一档、哪次配置、哪一版语料，一眼就看出来。
        "product_key": row.product_key,
        "source_ids": row.source_ids,
        "tier": row.tier,
        # 蒸馏程度的可读名：完整 / 平衡 / 快速。前端直接展示，不必再映射。
        "tier_name": _tier_name(row.tier),
        "tier_signature": row.tier_signature,
        "content_hash": row.content_hash,
        "narrative_version": row.narrative_version,
        "status": row.status,
        "n_chapters": row.n_chapters,
        "rule_count": row.rule_count,
        "rejected": row.rejected,
        "created_by_project_id": row.created_by_project_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "applied_at": row.applied_at.isoformat() if row.applied_at else None,
    }


def _sd_detail(row: StyleDistill) -> dict:
    """蒸馏库条目 → 详情（含 style_narrative 全文）。"""
    d = _sd_summary(row)
    d["style_narrative"] = row.style_narrative
    return d


# ====================================================================
# 人物塑造模板（独立产物，按档位分开存放）
# ====================================================================


class CharacterTemplateRequest(StrictRequest):
    """`POST /api/projects/{id}/character-template` 的请求体。"""

    #: 档位 key（full/balanced/fast）。不给则沿用项目上已存的 `narrative_tier`。
    tier: str | None = None
    #: True = 同档位也强制重抽（忽略「库里已有」的复用）。
    force: bool = False


class CharacterTemplateApplyRequest(StrictRequest):
    """`POST /api/projects/{id}/character-templates/apply` 的请求体。"""

    #: 要应用的模板产物 id（形如 `full:9a3f1166f4e6:1f2a3b4c`，**自带档位**）。
    template_id: str


@router.get("/character-templates")
def list_character_templates(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """列出库里的**人物塑造模板**产物（新的在前）。

    每条产物的 `id` 自带档位（`full:9a3f1166f4e6:1f2a3b4c`）：
    哪一档、哪次档位配置、哪一版语料 —— 一眼就看出来，不用再翻表。
    """
    from app.style.character_template import list_templates
    items = []
    for row in list_templates(db, limit=limit):
        items.append({
            "id": row.id,
            "tier": row.tier,
            "tier_signature": row.tier_signature,
            "source_ids": row.source_ids,
            "content_hash": row.content_hash,
            "rule_count": len(row.rules or []),
            "n_chunks": row.n_chunks,
            "rejected": row.rejected,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "applied_at": row.applied_at.isoformat() if row.applied_at else None,
        })
    return {"items": items}


@router.post("/{project_id}/character-template")
def start_character_template(
    project_id: int,
    body: CharacterTemplateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """提炼**人物塑造模板**：**立即返回** job_id，抽取在后台跑。

    与叙事蒸馏分开的原因：叙事档案一次完整档要 42 分钟，而模板只要每块
    一次调用（约 1/8 的量）。**抽模板不必重蒸整本档案** —— 旧档案照旧有效。

    库里已有同一档的模板产物时直接复用（`from_cache: True`），不重抽。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    if not (proj.reference_novel_ids or []):
        raise HTTPException(status_code=409, detail="项目未选参考语料，无法提炼模板")

    from app.style.narrative import resolve_tier
    try:
        cfg = resolve_tier(body.tier or getattr(proj, "narrative_tier", None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 复用预测：库里已有这一档的模板吗？
    from_cache = False
    if not body.force:
        from app.style.narrative import (_source_ids_hash, _tier_signature,
                                        corpus_content_hash)
        from app.style.character_template import find_template
        ids = [int(i) for i in (proj.reference_novel_ids or [])]
        content_hash = corpus_content_hash(ids, db)
        existing = find_template(
            db, tier=cfg.key, tier_signature=_tier_signature(cfg),
            content_hash=content_hash)
        from_cache = existing is not None

    if not body.force:
        active = find_active_run(db, project_id, kind="character_template")
        if active is not None and active.tier == cfg.key:
            return {"job_id": active.id, "status": active.status, "tier": active.tier,
                    "kind": "character_template", "reused": True,
                    "from_cache": from_cache, "cache_source": "library" if from_cache else None}

    run = create_distill_run(db, project_id, cfg.key, kind="character_template")
    background_tasks.add_task(run_distill_job, run.id, project_id, cfg.key, body.force)
    return {
        "job_id": run.id,
        "status": run.status,
        "tier": run.tier,
        "kind": "character_template",
        "reused": False,
        "from_cache": from_cache,
        "cache_source": "library" if from_cache else None,
    }


@router.post("/{project_id}/character-templates/apply", response_model=ProjectOut)
def apply_character_template(
    project_id: int,
    body: CharacterTemplateApplyRequest,
    db: Session = Depends(get_db),
) -> Project:
    """把库里某条**人物塑造模板**应用到本项目（只记引用，不改正文）。"""
    from app.models import CharacterTemplate
    from app.style.character_template import apply_template
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    row = db.get(CharacterTemplate, body.template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板产物不存在")
    apply_template(db, proj, row)
    return proj


@router.get("/style-distills")
def list_style_distills(
    status: str = Query(default="active", pattern="^(active|archived|all)$"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """列出蒸馏库里**可复用**的文风档案（默认只给 active）。

    列表**省略** style_narrative 全文与 rules：那份 JSON 有几万字符，列表场景用不到，
    只在 `GET /style-distills/{id}` 详情里返回。前端据此渲染「已有蒸馏档案」列表 + 应用按钮。
    """
    stmt = select(StyleDistill)
    if status == "active":
        stmt = stmt.where(StyleDistill.status == "active")
    elif status == "archived":
        stmt = stmt.where(StyleDistill.status == "archived")
    rows = db.scalars(
        stmt.order_by(StyleDistill.id.desc()).limit(limit)
    ).all()
    return {"items": [_sd_summary(r) for r in rows]}


@router.get("/style-distills/{distill_id}")
def get_style_distill(distill_id: int, db: Session = Depends(get_db)) -> dict:
    """单条蒸馏库条目详情（含 style_narrative 全文）。"""
    row = db.get(StyleDistill, distill_id)
    if row is None:
        raise HTTPException(status_code=404, detail="蒸馏库条目不存在")
    return _sd_detail(row)


@router.post("/style-distills/{distill_id}/archive")
def archive_style_distill(distill_id: int, db: Session = Depends(get_db)) -> dict:
    """归档一条蒸馏库条目：不再出现在「可复用」列表，但保留供历史查阅。

    归档后同输入再蒸馏会新建一行（而非悄悄覆盖），保证用户「我想重新蒸馏」的意图生效。
    """
    row = db.get(StyleDistill, distill_id)
    if row is None:
        raise HTTPException(status_code=404, detail="蒸馏库条目不存在")
    if row.status == "archived":
        return _sd_summary(row)  # 幂等：已是 archived 直接返回
    row.status = "archived"
    db.commit()
    return _sd_summary(row)


@router.post("/{project_id}/style-distills/apply", response_model=ProjectOut)
def apply_style_distill(
    project_id: int,
    body: StyleDistillApplyRequest,
    db: Session = Depends(get_db),
) -> Project:
    """把蒸馏库里某条已蒸馏档案**应用到本项目**。

    应用 = 把条目的 style_narrative 复制进 `project.style_narrative`（生成链路用的单槽缓存），
    并把项目的 `narrative_tier` 同步成该条目所用档位，使「指纹」对齐、应用后不会立刻被
    `project_narrative` 当成失效而重写。条目的 `applied_at` 同步刷新（命中复用也算一次使用）。

    这是「蒸馏一次、多个项目复用」的落点：项目 A 蒸馏完入库，项目 B 在「蒸馏库」里点一下
    应用，立刻拿到 A 的文风档案，省掉 B 自己的 42 分钟 LLM。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    entry = db.get(StyleDistill, body.distill_id)
    if entry is None or entry.status != "active":
        raise HTTPException(status_code=404, detail="蒸馏库条目不存在或已归档")

    from app.style.narrative import narrative_fingerprint, resolve_tier

    cfg = resolve_tier(entry.tier)
    ids = [int(i) for i in (proj.reference_novel_ids or [])]
    # 指纹按「本项目当前语料 + 该条目档位」算，确保应用后对本项目保持 sticky，
    # 不会在下次生成时被 project_narrative 判失效而重算。
    fp = narrative_fingerprint(ids, cfg) if ids else {
        "source_ids": [], "narrative_version": entry.narrative_version,
        "tier": cfg.key, "tier_signature": entry.tier_signature,
    }
    applied = dict(entry.style_narrative) if isinstance(entry.style_narrative, dict) else {}
    applied["fingerprint"] = fp
    proj.style_narrative = applied
    proj.narrative_tier = cfg.key
    entry.applied_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(proj)
    return proj


class StyleNarrativeRepairRequest(StrictRequest):
    """`POST /api/projects/{id}/style-narrative/repair` 的请求体。

    体检说「档案的禁用词与档案正文自相矛盾」，产品里却没地方让人动手 ——
    这个请求体就是那个地方。

    继承 `StrictRequest`：拼错字段名立刻 422，而不是静默按默认值改档案。
    """

    #: 要处理的禁用词（必须是档案禁用清单里的词）。
    word: str
    #: 修法：`remove_word`（删词，默认）/ `drop_rule`（删掉那条规则）/ `unban`（从清单里去掉）。
    mode: str = "remove_word"
    #: True = 只返回「将要改什么」，**不落库**（前端先给人看）。
    preview: bool = False


# ====================================================================
# 角色个性：人写 + AI 帮着完善（2026-10-28）
# ====================================================================


class PersonaSaveRequest(StrictRequest):
    """`POST /api/projects/{id}/style-narrative/personas` 的请求体。"""

    #: 角色名（要改的那个角色）。
    name: str
    #: 用户写的个性档案（四个字段的子集；给空串表示清空这个角色的个性）。
    persona: dict


class PersonaCompleteRequest(StrictRequest):
    """`POST /api/projects/{id}/style-narrative/personas/complete` 的请求体。"""

    #: 角色名。
    name: str
    #: 用户已经写好的部分（可选）——AI 只能在它**后面**补充。
    persona: dict = {}
    #: 用户的额外要求（可选，如「我想让他更冷一点」）。
    note: str = ""


@router.get("/{project_id}/style-narrative/personas")
def list_personas(project_id: int, db: Session = Depends(get_db)) -> dict:
    """列出本项目所有角色的个性档案（含来源标记）。

    来源：`model`=合成圣经时模型给的；`derived`=从已写正文反推的；
    `user`=用户自己写的（**受保护**：反推不会覆盖它）。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    bible = proj.plot_bible
    if isinstance(bible, str):
        try:
            import json as _json

            bible = _json.loads(bible)
        except Exception:  # noqa: BLE001
            bible = {}
    if not isinstance(bible, dict):
        bible = {}
    out = []
    for c in (bible.get("characters") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        persona = c.get("persona") if isinstance(c.get("persona"), dict) else {}
        out.append({
            "name": c.get("name"),
            "role": c.get("role"),
            "motivation": c.get("motivation"),
            "persona": persona,
            "source": persona.get("来源") or "",
        })
    return {"items": out, "has_bible": bool(bible)}


@router.post("/{project_id}/style-narrative/personas")
def save_persona(
    project_id: int, body: PersonaSaveRequest, db: Session = Depends(get_db)
) -> dict:
    """保存**用户写的**角色个性（只动 persona，不碰 name/role/motivation）。

    落库后标 `来源=user` ⇒ 之后的「从正文反推」不会覆盖它。
    """
    from app.generation.persona import save_persona as _save

    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    saved = _save(db, proj, body.name, body.persona or {})
    if not saved:
        raise HTTPException(status_code=404, detail="角色不存在，或没有可保存的内容")
    return {"ok": True, "name": body.name, "persona": saved}


@router.post("/{project_id}/style-narrative/personas/complete")
def complete_persona(
    project_id: int, body: PersonaCompleteRequest, db: Session = Depends(get_db)
) -> dict:
    """AI 帮着完善角色个性 —— **只返回建议，不落库**。

    人写的内容优先：模型被要求只能在用户已写的内容**后面**补充，不得推翻；
    要不要采纳由用户在界面上点。采纳时调 `POST .../style-narrative/personas` 落库。
    """
    from app.generation.persona import complete_persona as _complete
    from app.llm.provider import get_llm_provider

    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    bible = proj.plot_bible
    if isinstance(bible, str):
        try:
            import json as _json

            bible = _json.loads(bible)
        except Exception:  # noqa: BLE001
            bible = {}
    if not isinstance(bible, dict):
        bible = {}
    character = None
    for c in (bible.get("characters") or []):
        if isinstance(c, dict) and str(c.get("name") or "") == str(body.name or ""):
            character = c
            break
    if character is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    provider = get_llm_provider()
    result = _complete(provider, character, body.persona or {}, body.note or "")
    if not result:
        return {"ok": False, "note": "没能给出建议（模型不可用或限流）—— 你写的内容不受影响"}
    return {"ok": True, "name": body.name, **result}

@router.get("/{project_id}/style-narrative/conflicts")
def style_narrative_conflicts(project_id: int, db: Session = Depends(get_db)) -> dict:
    """查这个项目的文风档案里有没有「禁用词与正文自相矛盾」（**只读**）。

    给界面用的轻量版：不调模型、不写库，只把冲突列出来。
    （体检本身走 `POST /api/assistant/diagnose` 或助理的 `audit_prompt`，
    那两条都会调模型解释；这里只给「有哪些冲突、修不修」用。）

    返回里的 `can_generate` = 现在生成会不会被体检挡住
    （挡住的条件：有 **error 级**的冲突 —— 名词性用法的 warn 不挡）。
    """
    from app.style.forbidden_usage import analyze
    from app.style.narrative import FORBIDDEN_SECTION_MARKER
    from app.generation.prompts import _narrative_block

    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    narrative = proj.style_narrative
    if not isinstance(narrative, dict) or not narrative:
        return {
            "has_archive": False,
            "conflicts": [],
            "attributive": [],
            "can_generate": True,
            "note": "这个项目还没有文风档案 —— 先蒸馏一份，或从蒸馏库应用一份。",
        }

    block = _narrative_block(narrative, include_voice=False)
    rules_text = block.split(FORBIDDEN_SECTION_MARKER, 1)[0]
    analysis = analyze(narrative, rules_text)

    def _dump(usages: list) -> list[dict]:
        out = []
        for u in usages:
            out.append({
                "word": u.word,
                "dimension": u.dimension,
                "rule_dimension": u.rule_dimension,
                "context": u.context,
                "rule_text": u.rule_text,
            })
        return out

    conflicts = _dump(analysis.conflicts)
    attributive = _dump(analysis.attributive)
    return {
        "has_archive": True,
        "conflicts": conflicts,
        "attributive": attributive,
        "can_generate": not conflicts,
        "note": ("档案正文在教模型用禁用清单里的转折词 —— 生成会被挡住。"
                 if conflicts else
                 ("档案正文里出现了禁用词，但用的是名词性写法 —— 不挡生成。"
                  if attributive else "档案干净，没有禁用词冲突。")),
    }


@router.post("/{project_id}/style-narrative/repair")
def repair_style_narrative(
    project_id: int,
    body: StyleNarrativeRepairRequest,
    db: Session = Depends(get_db),
) -> dict:
    """修文风档案里的禁用词冲突：体检报了，就能当场改。

    ## 为什么要有这个入口

    体检（`agents/prompt_audit.py`）发现「档案的禁用词与档案正文自相矛盾」时，
    原来的建议是「从档案规则里删掉这些词，或从禁用清单里去掉」——
    但产品里**没有改档案规则的接口**：只能重蒸馏（一轮 ≈ 42 分钟 LLM）或换档案。
    「被告知了原因，却没有地方自己动手」—— 这就是「开箱即用」最大的实际卡点。

    ## 改的是谁

    改**项目当前应用的档案**（`projects.style_narrative`），
    **不动蒸馏库条目** —— 库条目是全局共享的，替别人改档案不是用户该做的事。

    ## 留痕

    改动记进档案的 `repair_history`（模式、词、维度、**改前原文**）。
    改动是「删除」，所以改前的原文就是回退所需的全部信息 ——
    不必另存整份档案（几十万字）。

    ## 自证

    落库后**重跑一次体检**并把结果一起返回：修好了就没有 conflict 了，
    没修好看得出来。不想改只想看 ⇒ `preview=true`（不落库）。
    """
    from app.style.forbidden_usage import REPAIR_MODES, plan_repair

    if body.mode not in REPAIR_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode 只能是 {' / '.join(REPAIR_MODES)}",
        )
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    narrative = proj.style_narrative
    if not isinstance(narrative, dict) or not narrative.get("rules"):
        raise HTTPException(
            status_code=400,
            detail="这个项目还没有文风档案（或档案里没有规则）—— 先蒸馏或从蒸馏库应用一份",
        )

    plan = plan_repair(narrative, body.word, body.mode)
    if plan is None:
        raise HTTPException(
            status_code=404,
            detail=f"档案里找不到「{body.word}」—— 没什么可修（换个词，或先看体检报告）",
        )

    payload: dict = {
        "ok": True,
        "mode": plan.mode,
        "word": plan.word,
        "applied": False,
        "changes": plan.changes,
    }

    if body.preview:
        payload["preview"] = True
        payload["message"] = f"将要改动 {plan.affected} 处（还没落库）"
        return payload

    repaired = plan.narrative
    history = repaired.get("repair_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "mode": plan.mode,
        "word": plan.word,
        "changes": [{"dimension": c.get("dimension", ""), "before": c.get("before", "")}
                    for c in plan.changes],
    })
    repaired["repair_history"] = history[-20:]  # 只留最近 20 次
    proj.style_narrative = repaired
    db.commit()
    db.refresh(proj)

    payload["applied"] = True
    payload["message"] = f"已改动 {plan.affected} 处，并重新体检"

    # —— 自证：重跑体检，把结果一起返回 ——
    try:
        from app.agents.prompt_audit import run_prompt_audit

        report, _stats = run_prompt_audit(db, proj)
        issues = []
        for i in report.issues:
            issues.append({
                "id": getattr(i, "id", ""),
                "severity": getattr(i, "severity", ""),
                "title": getattr(i, "title", ""),
                "rule": getattr(i, "rule", ""),
                "suggestion": getattr(i, "suggestion", ""),
            })
        payload["audit"] = {
            "issues": issues,
            "errors": sum(1 for i in issues if i["severity"] == "error"),
            "warnings": sum(1 for i in issues if i["severity"] == "warn"),
        }
    except Exception as exc:  # noqa: BLE001 —— 体检失败不该把已落库的修复说成失败
        payload["audit"] = {"issues": [], "errors": 0, "warnings": 0,
                            "error": f"体检没跑成：{type(exc).__name__}"}
    return payload


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _build_reference_graphs_bg(project_id: int, novel_ids: list[int]) -> None:
    """后台给参考语料建图（零 LLM 规则抽取，幂等；**失败只记日志**）。

    为什么要提前建：`project_narrative` 开头会同步补建这张图，
   实测 1177 章的语料要 **48.2 秒**（纯 CPU、不调模型），而它只服务
   「参考书角色」相关的产物 —— 也就是说用户在「准备参考语料」上白等 50 秒。
    代价：这一步要在用户选完语料时就付掉（后台付），而不是在点「开始蒸馏」时。

    口径不变：每本语料在每个项目下**各自成图**（source=f"novel:{id}"，不混）。
    """
    import logging

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        from sqlalchemy import func, select

        from app.kg.build import build_graph
        from app.models import KgNode

        for nid in novel_ids:
            try:
                existing = db.scalar(
                    select(func.count()).select_from(KgNode).where(
                        KgNode.project_id == project_id,
                        KgNode.type == "character",
                        KgNode.source == f"novel:{nid}",
                    )
                )
                if existing:
                    continue
                built = build_graph(db, project_id, novel_id=nid)
                logging.getLogger(__name__).info(
                    "后台建参考图谱：项目 %s 语料 %s → %s 个角色",
                    project_id, nid, built.get("characters"))
            except Exception as exc:  # noqa: BLE001 —— 建图失败不阻断任何主流程
                logging.getLogger(__name__).warning(
                    "后台建参考图谱失败（项目 %s 语料 %s）: %s", project_id, nid, exc)
    finally:
        db.close()


@router.patch("/{project_id}", response_model=ProjectOut)
def patch_project(
    project_id: int, body: ProjectPatch,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Project:
    """分步更新项目字段（向导每步调一次）。

    `narrative_tier` 在这里做校验（而不是靠 pydantic），有两个原因：
    1. 想回**400 + 中文可读文案**（"未知蒸馏档位 'xxx'，可选：…"），
       pydantic 的 Literal/validator 只会给 422 + 英文结构化错误；
    2. 这是**唯一会写入非法档位的入口**（`build-narrative` 那条路自己已经校验过）。
       漏在这里的后果：项目被存成不存在的档位，当场不报错，要等到之后
       蒸馏时才炸，那时已经无从追溯是谁写进去的。

    校验放在 setattr **之前**，所以非法档位时整条 patch 都不落库——不部分写入。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    updates = body.model_dump(exclude_unset=True)
    _validate_visibility(db, {**{k: getattr(proj, k) for k in ("story_authority", "reference_cutoffs", "reference_novel_ids", "continuation_start_chapter")}, **updates})
    if "continuation_start_chapter" in updates:
        start = updates["continuation_start_chapter"]
        if start is not None and start < 1:
            raise HTTPException(status_code=400, detail="continuation_start_chapter 必须 >= 1")
    if "narrative_tier" in updates:
        from app.style.narrative import resolve_tier

        tier = updates["narrative_tier"]
        # null = 清空偏好（回到默认完整档），不是非法值，必须放行。
        if tier is not None:
            try:
                # 归一化后写回：大写/带空白的输入也存成规范 key，
                # 免得库里出现 'FAST' 这种 resolve_tier 认不出的值。
                updates["narrative_tier"] = resolve_tier(tier).key
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "outline" in updates and updates["outline"] != proj.outline:
        proj.plot_timeline = None
        updates["status"] = "needs_planning"
    # 参考语料变了 ⇒ 后台把它们的图谱建起来（零 LLM、幂等）。
    # 建图只服务「参考书角色」相关产物，跟蒸馏结果无关，不该压在蒸馏的关键路径上：
    # 实测 1177 章要 48.2 秒，用户在「准备参考语料」上白等。
    _ids_before = set(getattr(proj, "reference_novel_ids", None) or [])
    for field, value in updates.items():
        setattr(proj, field, value)
    db.commit()
    db.refresh(proj)
    _ids_after = set(proj.reference_novel_ids or [])
    _new_ids = sorted(int(i) for i in (_ids_after - _ids_before))
    if _new_ids:
        background_tasks.add_task(_build_reference_graphs_bg, int(proj.id), _new_ids)
    return proj


# --------------------------------------------------------------------------- #
# plot_bible 级联协议（kg-cascade-protocol-design，2026-10-15）
# --------------------------------------------------------------------------- #
# PATCH /projects/{id} 走老路径（兼容性）；新增 POST /projects/{id}/bible/apply
# 走新路径——写 revision + 算 diff + 扫 KG 冲突 + 覆盖 bible。前端从
# apply 响应里直接拿到 conflicts（toast 一次性展开，不需新 tab）。
# 理由：Pydantic 响应模型 ProjectOut 序列化对前端已稳定，新增字段会破契约。

class BibleApplyRequest(BaseModel):
    """plot_bible 改写请求（带审计与冲突扫描的新路径）。"""

    bible: dict
    note: str | None = None  # 备注（"为什么改"留痕）


@router.post("/{project_id}/bible/apply")
def apply_bible_revision(
    project_id: int, body: BibleApplyRequest, db: Session = Depends(get_db)
) -> dict:
    """改 plot_bible（级联协议新路径）：写 revision + 算 diff + 扫 KG 冲突 + 覆盖。

    与 PATCH /projects/{id} 的区别：
    - 走新路径会**留下版本快照**（kg_bible_revisions），事后可查
    - 改完立即扫 KG，把"bible 声明 vs KG 已有事实"的字面矛盾**一起返回**，
      前端在 toast 里展开给用户判断（忽略 / 去豁免 / 撤回）
    - 不自动改 KG——已 commit 段是历史事实，不能重写

    老项目无 revision 行（迁移不补历史）：首次 apply 记 rev_no=1。
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    from app.kg.bible import apply_bible_revision as _apply

    try:
        r = _apply(db, project_id, body.bible, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return r


class WorkflowLogRequest(BaseModel):
    """POST /api/projects/{id}/workflow/log 请求体。"""

    step_id: str
    action: str
    detail: str = ""
    proposal_id: int | None = None


@router.get("/{project_id}/workflow")
def get_workflow(project_id: int, db: Session = Depends(get_db)) -> dict:
    """工序链状态：助理 agent 现在到哪一步、该做什么、卡在哪儿。

    状态是**每次重新评估**出来的（查库 + 跑只读规则），不是日志里的一句话 ——
    正文改过、蒸馏重跑过、提案批了，状态都会跟着变。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    from app.agents.workflow import get_or_create_run, state_to_dict
    run = get_or_create_run(db, proj)
    return state_to_dict(db, proj, run)


@router.post("/{project_id}/workflow/log")
def log_workflow(
    project_id: int,
    body: WorkflowLogRequest,
    db: Session = Depends(get_db),
) -> dict:
    """记一笔工序推进（agent 做完一步读工序、或提出了一条待批提案时调用）。

    **它不执行任何写操作** —— 写动作一律走 proposal 审批闸门；
    这里只留痕，好让「走到哪一步」可追溯、可断点续跑。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    from app.agents.workflow import STEP_BY_ID, record, state_to_dict
    if body.step_id not in STEP_BY_ID:
        raise HTTPException(status_code=422, detail=f"未知工序：{body.step_id}")
    run = record(db, proj, step_id=body.step_id, action=body.action,
                 detail=body.detail, proposal_id=body.proposal_id)
    return state_to_dict(db, proj, run)


@router.post("/{project_id}/plan-extension")
def extend_plan(
    project_id: int,
    body: PlanExtensionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """扩展规划：在**保存权威允许的范围内**追加剧情点，并写成新的段契约。

    什么时候用：生成被拒（409 `plan_exhausted`，规划里的剧情点都写完了），
    而作者还想继续写 —— 这条路径把新剧情点写进契约，让生成继续。

    什么时候**不该**用：权威本身已经写到边界（时间范围用满、段数预算用完）。
    这时返回 409 `no_authority_headroom` 并说清差在哪 —— **不替作者编剧情**。
    先扩权威，再扩规划。

    失败一律 409（可恢复、看得懂原因），不吞异常。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    from app.agents.plan_extension import extend_plan as _extend
    from app.llm.registry import get_provider_registry
    provider = get_provider_registry().get('assistant')
    result = _extend(db, proj, provider, count=body.count)
    status = result.get('status')
    if status != 'extended':
        raise HTTPException(status_code=409, detail={
            'status': status,
            'message': result.get('message') or '扩展规划失败。',
            'headroom': result.get('headroom'),
        })
    return result


@router.get("/{project_id}/bible/revisions")
def list_bible_revisions(    project_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """plot_bible 改写历史（按 revision_no 倒序，新的在前）。

    列表不返回 bible_json 全量（按 KB 级别，列表会变慢）；要看具体某版
    的全量 bible 走 PATCH 后端点的 proj 响应，或后端新加的 detail 端点
    （本轮不做——审计先于回看）。"""
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    from app.kg.bible import list_bible_revisions as _list

    return {"revisions": _list(db, project_id, limit)}


@router.post("/{project_id}/build-kg")
def build_kg(
    project_id: int,
    novel_id: int | None = Query(default=None),
    all_corpora: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return build_graph(db, project_id, novel_id, all_corpora=all_corpora)


@router.post("/{project_id}/build-narrative")
def build_narrative(
    project_id: int,
    force: bool = Query(default=False),
    max_chapters: int = Query(default=30),
    tier: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """提炼叙事档案（LLM 从参考语料提炼 L2/L3叙事规则，附证据账本）。

    幂等：已存且指纹一致（source_ids + narrative_version + **tier**）则直接返回缓存。

    Args:
        tier: 蒸馏档位（full/balanced/fast）。不给则沿用项目上存的
            `narrative_tier`；那也没有则用默认完整档。
            显式传入合法档位会**顺带存进项目**（项目级偏好，见下）。

    tier 与 force 的区别：force 是「同档位重算一遍」，tier 是「换档位重算」。
    换档必然重算——指纹里含tier（见 narrative_fingerprint），不重算就会静默
    返回上一档的缓存。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    ids = [int(i) for i in (proj.reference_novel_ids or [])]
    if not ids:
        raise HTTPException(status_code=400, detail="项目未选参考语料，无法提炼叙事档案")

    from app.style.narrative import project_narrative, resolve_tier

    if tier:
        try:
            cfg = resolve_tier(tier)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 选档是项目级偏好：存下来，后续不带 tier 参数的调用也按它来。
        if proj.narrative_tier != cfg.key:
            proj.narrative_tier = cfg.key
            db.commit()

    profile = project_narrative(db, proj, force=force, max_chapters=max_chapters)
    if profile is None:
        raise HTTPException(status_code=500, detail="叙事档案提炼失败")
    return {
        "version": profile.get("version"),
        "source_ids": profile.get("source_ids"),
        "tier": profile.get("tier"),
        "tier_name": profile.get("tier_name"),
        "n_chapters": profile.get("n_chapters"),
        "rejected": profile.get("rejected"),
        "rule_count": sum(len(v) for v in (profile.get("rules") or {}).values()),
        "dimensions": {
            dim: len(items)
            for dim, items in (profile.get("rules") or {}).items()
            if items
        },
    }


@router.get("/{project_id}/kg")
def kg(
    project_id: int,
    type_: str = Query(default="all"),
    source: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return get_graph(db, project_id, type_, source=source)


@router.get("/{project_id}/kg/character/{name}")
def character(project_id: int, name: str, db: Session = Depends(get_db)) -> dict:
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    result = get_character(db, project_id, name)
    if result is None:
        raise HTTPException(status_code=404, detail="角色不存在")
    return result


@router.get("/{project_id}/reminders")
def reminders(
    project_id: int,
    source: str = Query(default="generated"),
    db: Session = Depends(get_db),
) -> dict:
    """生成前提醒（未填坑 / 角色约束）。

    `source` 与 /kg 对齐，但**默认值刻意不同**：/kg 默认 `None`＝全量图谱（供浏览），
    这里默认 `"generated"`＝本次创作自己的图谱。原因是这两个端点服务的目标相反：

    - 提醒会注入提示词去约束**接下来写什么**，参考语料的角色/悬念与之无关，
      混进来就是污染（P0-1 修的就是这个：默认不限 source 时林黛玉会进生成约束）；
    - 图谱是给人看的，默认看全部才符合「先建图再浏览」的心智。

    要查参考语料那一侧的提醒，显式传 `?source=novel:{id}`。
    """
    if db.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    # top_k=None：人工查阅的 API 要完整列出全部角色/伏笔；封顶只发生在生成路径
    # （app.kg.queries.get_reminders 的默认 REMINDER_TOP_K），避免每轮注入随账本膨胀。
    return get_reminders(db, project_id, source=source, top_k=None)


@router.get("/{project_id}/export-txt")
def export_txt(project_id: int, db: Session = Depends(get_db)) -> PlainTextResponse:
    """把所有已生成段落拼接成一份 txt 文档，直接触发浏览器下载。

    正文衔接靠段级总结链（短摘要）而非重复搬运全文，保证长文连贯且不被会话压缩。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no)
    ).all()
    body = "\n\n".join((g.text or "") for g in gens).strip()
    if not body:
        raise HTTPException(status_code=400, detail="尚无生成内容，无法导出")

    mode_label = "wayfinder 追问构思" if (proj.mode or "quick") == "expert" else "直接构思"
    header = (
        f"《{proj.title or '未命名'}》\n"
        f"类型：{proj.genre or '未指定'} · 风格：{proj.style or '未指定'} · "
        f"构思方式：{mode_label} · 共 {len(gens)} 段\n"
        f"{'—' * 24}\n\n"
    )
    content = header + body
    # 文件名可能含中文：ASCII 回退名 + RFC 5987 UTF-8 名，避免 HTTP 头 latin-1 编码报错
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", proj.title or "novel")
    ascii_name = re.sub(r'[^\x00-\x7f]+', "", safe) or "novel"
    utf_name = quote(f"{safe}.txt", safe="")
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}.txt"; filename*=UTF-8\'\'{utf_name}'
        },
    )

# ====================================================================
# 角色卡：身份 / 能力（武器·功法·能力）/ 声纹 / 关系（2026-10-29）
# ====================================================================
#
# 用户诉求：「没有记录角色使用的武器、功法、什么能力」+「新建角色时 AI 自动
# 添加性格、行为、关系、口头禅」+「不要千篇一律」。
#
# 纪律（与 persona 一致）：
# 1. **人写的归人**：用户保存的卡标 `来源=user`，任何自动流程不覆盖；
# 2. **AI 只建议**：生成结果只返回、不落库，采纳由人点；
# 3. **不编造**：从正文抽卡时武器/功法/能力的名称必须能在正文里查到。

class CardSaveRequest(StrictRequest):
    """`POST /api/projects/{id}/character-cards` 的请求体。"""

    name: str
    card: dict


class CardGenerateRequest(StrictRequest):
    """`POST /api/projects/{id}/character-cards/generate`（单角色补全）的请求体。"""

    name: str
    card: dict = {}
    note: str = ""


class CardsBatchRequest(StrictRequest):
    """`POST /api/projects/{id}/character-cards/batch`（批量新建）的请求体。"""

    #: 要新建的角色（名字，或「名字：一句话设定」）。
    wants: list[str]
    note: str = ""


def _bible_of(proj) -> dict:
    import json as _json

    bible = proj.plot_bible
    if isinstance(bible, str):
        try:
            bible = _json.loads(bible)
        except Exception:  # noqa: BLE001
            bible = {}
    return bible if isinstance(bible, dict) else {}


def _story_of(proj) -> dict:
    return {
        "title": proj.title or "",
        "genre": getattr(proj, "genre", "") or "",
        "style": getattr(proj, "style", "") or "",
        "premise": getattr(proj, "premise", "") or "",
    }


def _existing_voices(bible: dict) -> dict:
    """已有角色的声纹签名（批量生成时用来避让）。"""
    from app.generation.character_card import voice_signature

    out = {}
    for c in (bible.get("characters") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        card = c.get("card")
        if isinstance(card, dict):
            sig = voice_signature(card)
            if sig:
                out[str(c["name"])] = sig
    return out


@router.get("/{project_id}/character-cards")
def list_character_cards(project_id: int, db: Session = Depends(get_db)) -> dict:
    """列出本项目所有角色的卡（含 persona，方便界面一次拿全）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    bible = _bible_of(proj)
    out = []
    for c in (bible.get("characters") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        card = c.get("card") if isinstance(c.get("card"), dict) else {}
        persona = c.get("persona") if isinstance(c.get("persona"), dict) else {}
        out.append({
            "name": c.get("name"),
            "role": c.get("role") or "",
            "motivation": c.get("motivation") or "",
            "card": card,
            "card_source": card.get("来源") or "",
            "persona": persona,
            "persona_source": persona.get("来源") or "",
        })
    # 声纹撞车（界面提示「这几个人说话太像」，用来引导用户去改）
    from app.generation.character_card import voice_collisions

    cards = {item["name"]: item["card"] for item in out if item["card"]}
    return {"characters": out, "voice_collisions": voice_collisions(cards)}


@router.post("/{project_id}/character-cards")
def save_character_card(
    project_id: int, body: CardSaveRequest, db: Session = Depends(get_db)
) -> dict:
    """保存**用户编辑过**的角色卡（只动 card，不碰 name/role/motivation/persona）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")

    from app.generation.character_card import SOURCE_USER, normalize_card

    card = normalize_card(body.card or {})
    card["来源"] = SOURCE_USER  # 人写的，任何自动流程不覆盖

    bible = _bible_of(proj)
    chars = bible.get("characters")
    if not isinstance(chars, list):
        chars = []
    found = False
    for c in chars:
        if isinstance(c, dict) and str(c.get("name") or "").strip() == name:
            c["card"] = card
            found = True
            break
    if not found:
        chars.append({"name": name, "role": "", "motivation": "", "card": card})
    bible["characters"] = chars

    # JSON 列必须显式标脏（2026-10-28 踩过：赋值后 UPDATE 不一定发出）
    from sqlalchemy.orm.attributes import flag_modified

    proj.plot_bible = bible
    flag_modified(proj, "plot_bible")
    db.commit()
    return {"ok": True, "name": name, "card": card}


@router.post("/{project_id}/character-cards/generate")
def generate_character_card(
    project_id: int, body: CardGenerateRequest, db: Session = Depends(get_db)
) -> dict:
    """给**一个已有角色**补全卡（AI 只建议，不落库）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")

    from app.generation.character_card_gen import complete_card

    provider = _provider_or_none()
    if provider is None:
        raise HTTPException(status_code=503, detail="LLM 渠道未配置")

    bible = _bible_of(proj)
    char = {"name": name}
    for c in (bible.get("characters") or []):
        if isinstance(c, dict) and str(c.get("name") or "").strip() == name:
            char = dict(c)
            break
    story = _story_of(proj)
    char.setdefault("_title", story["title"])
    char.setdefault("_genre", story["genre"])
    char.setdefault("_premise", story["premise"])

    result = _model_call(
        "补全角色卡",
        lambda: complete_card(provider, char, body.card or char.get("card") or {},
                              body.note or "", raise_errors=True),
    )
    if not result:
        return {"ok": False, "detail": "AI 没给出可用结果（可重试或改写要求）"}
    return {
        "ok": True,
        "name": name,
        "补充": result.get("补充") or {},
        "说明": result.get("说明") or "",
        "voice_collisions": _collisions_with(result.get("补充") or {}, name, bible),
    }


@router.post("/{project_id}/character-cards/batch")
def generate_character_cards_batch(
    project_id: int, body: CardsBatchRequest, db: Session = Depends(get_db)
) -> dict:
    """**批量**生成新角色的卡（互相避让；撞了就重写）。

    生成结果**不落库** —— 由用户在界面上逐个采纳。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    wants = [str(w).strip() for w in (body.wants or []) if str(w).strip()]
    if not wants:
        raise HTTPException(status_code=400, detail="wants 不能为空")

    from app.generation.character_card_gen import generate_cards

    provider = _provider_or_none()
    if provider is None:
        raise HTTPException(status_code=503, detail="LLM 渠道未配置")

    bible = _bible_of(proj)
    result = _model_call(
        "批量生成角色卡",
        lambda: generate_cards(provider, _story_of(proj), wants,
                               already=_existing_voices(bible), note=body.note or "",
                               raise_errors=True),
    )
    return {
        "ok": bool(result.get("角色")),
        "角色": result.get("角色") or {},
        "冲突": result.get("冲突") or [],
        "轮次": result.get("轮次") or 0,
    }


def _collisions_with(card: dict, name: str, bible: dict) -> list:
    """新卡与既有角色的声纹撞车（只看有卡的那些）。"""
    from app.generation.character_card import voice_collisions

    cards = {}
    for c in (bible.get("characters") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        if str(c["name"]) == name:
            continue
        if isinstance(c.get("card"), dict) and c["card"]:
            cards[str(c["name"])] = c["card"]
    if not cards or not card:
        return []
    cards[name] = card
    return [x for x in voice_collisions(cards) if name in x.get("人物", [])]


def _provider_or_none():
    """取当前 LLM provider（取不到就 None —— 端点回 503，不抛栈）。

    入口与 persona 端点一致（`app.llm.provider.get_llm_provider`），
    不另造一套取法（否则提示词/渠道配置会分叉）。
    """
    try:
        from app.llm.provider import get_llm_provider

        return get_llm_provider()
    except Exception:  # noqa: BLE001 —— 渠道没配只是这个端点不可用
        return None

#: 渠道失败 → 给人看的话。**判断顺序有意义**：先认限流，再认鉴权，最后才认超时
#: （「HTTP Error 429」里没有 timeout 字样，但反过来会误伤）。
_MODEL_FAILURE_HINTS = (
    ("429", "模型渠道限流或额度已用尽"),
    ("too many requests", "模型渠道限流或额度已用尽"),
    ("freeusagelimit", "模型渠道额度已用尽"),
    ("insufficient", "模型渠道额度已用尽"),
    ("quota", "模型渠道额度已用尽"),
    ("401", "模型渠道鉴权失败"),
    ("403", "模型渠道鉴权失败"),
    ("unauthorized", "模型渠道鉴权失败"),
    ("timed out", "模型渠道超时"),
    ("timeout", "模型渠道超时"),
)


def _model_failure_detail(what: str, exc: BaseException) -> str:
    """把渠道异常翻成一句**能照着做**的话。

    界面上不放整段栈（看不懂），但也不吞掉原话 —— 原话是判断「重试还是充值」
    的唯一线索，截到 200 字放进末尾。
    """
    raw = " ".join(str(exc or "").split())
    low = raw.lower()
    why = next((text for needle, text in _MODEL_FAILURE_HINTS if needle in low), "")
    if not why:
        why = "模型渠道调用失败"
    if len(raw) > 200:
        raw = raw[:200] + "…"
    head = f"{what}没做成：{why}。"
    return head + (f"渠道原话：{raw}" if raw else "")


def _model_call(what: str, fn):
    """跑一次模型调用；渠道异常换成 502 + 可读原因。

    为什么不是 500：500 的含义是「本站写错了」，而渠道限流/超时/欠费是**外部
    状态**，人应该看到「等会儿再试」还是「去配额度」。`what` 用**这件事的名字**
    （如「生成修炼体系」），它直接进界面。
    """
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 —— 渠道故障不是本站 bug，要说清原因
        raise HTTPException(
            status_code=502, detail=_model_failure_detail(what, exc)
        ) from exc


# ====================================================================
# 修炼体系 / 档位分层 / 角色修炼档案（2026-10-29）
# ====================================================================
#
# 用户诉求：「涉及到武侠的、修仙的，还有其他乱七八糟的修炼体系的，
# 那么这个要不要做去角色卡，那么主角目前的境界，以后的境界，
# 还有中途的修炼等级，还有其他各式各样配角和路人甲的等级……
# 角色有什么修炼天赋……修炼遇到了叉，掉了境界，或者为了活命燃烧了寿命，
# 或者是去了什么洞府得到了提升这种动态的路线，怎么规划」
#
# 三条纪律（与角色卡一致）：
# 1. **体系不写死**：用户自己定义阶梯（武侠修仙都能填）；
# 2. **分层群像**：路人只记档位（「筑基期散修」），主角与主要配角才逐个精确记；
# 3. **AI 只建议**：体系/路线生成结果不落库，人点采纳才写。

class PowerSystemRequest(StrictRequest):
    """`POST /api/projects/{id}/power-system` 的请求体。"""

    #: 体系（单个或多个）。空 dict 表示清空。
    power_system: dict = {}
    #: 档位分层（群像）。
    tiers: list = []


class PowerSystemGenerateRequest(StrictRequest):
    """`POST /api/projects/{id}/power-system/generate` 的请求体。"""

    genre: str = ""
    tone: str = ""
    note: str = ""
    #: 作者指定的体系类型（细分型 / 序列型 / 粗放型）；空 = 让 AI 自己定。
    #:
    #: 为什么要有：类型一直是**模型自己说的** —— 实测要「粗放型」时，
    #: 模型给了一套细分阶梯并自判「细分型」。作者能说清要哪种时，
    #: 就该由作者说了算（不合就带原因重试）。
    system_type: str = ""


class TierGenerateRequest(StrictRequest):
    """`POST /api/projects/{id}/power-system/tiers/generate` 的请求体。"""

    note: str = ""


class CultivationSaveRequest(StrictRequest):
    """`POST /api/projects/{id}/cultivation` 的请求体。"""

    name: str
    #: 修炼档案（当前境界/天赋/瓶颈/路线/代价…）。
    profile: dict = {}


class RoutePlanRequest(StrictRequest):
    """`POST /api/projects/{id}/cultivation/plan` 的请求体。"""

    name: str
    note: str = ""


def _ps_bible(proj) -> dict:
    import json as _json

    bible = proj.plot_bible
    if isinstance(bible, str):
        try:
            bible = _json.loads(bible)
        except Exception:  # noqa: BLE001
            bible = {}
    return bible if isinstance(bible, dict) else {}


def _mark_bible_dirty(proj, bible: dict) -> None:
    """JSON 列必须显式标脏（2026-10-28 踩过：赋值后 UPDATE 不一定发出）。"""
    from sqlalchemy.orm.attributes import flag_modified

    proj.plot_bible = bible
    flag_modified(proj, "plot_bible")
    db = proj  # noqa: F841 —— 仅示意；实际 commit 由调用方做


@router.get("/{project_id}/power-system")
def get_power_system(project_id: int, db: Session = Depends(get_db)) -> dict:
    """取本项目的修炼体系与档位分层。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    bible = _ps_bible(proj)
    from app.generation.cultivation import normalize_tiers
    from app.generation.power_system import systems_of

    return {
        "power_system": {"体系": systems_of(bible)},
        "tiers": normalize_tiers(bible.get("tier_layers") or []),
    }


@router.post("/{project_id}/power-system")
def save_power_system(
    project_id: int, body: PowerSystemRequest, db: Session = Depends(get_db)
) -> dict:
    """保存**用户编辑过**的修炼体系与档位分层。

    档位层里若用了体系里没有的档位名 ⇒ **丢掉那一层**
    （否则分层群像会出现「体系里没有的档次」，写作时没法对照）。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")

    from app.generation.cultivation import normalize_tiers
    from app.generation.power_system import normalize_power_system, systems_of

    bible = _ps_bible(proj)
    if body.power_system:
        bible["power_system"] = normalize_power_system(body.power_system)
    else:
        bible.pop("power_system", None)

    tiers = normalize_tiers(body.tiers)
    systems = systems_of(bible)
    if systems:
        from app.generation.power_system_gen import filter_tiers_by_bands

        tiers = filter_tiers_by_bands(tiers, systems[0])
    if tiers:
        bible["tier_layers"] = tiers
    else:
        bible.pop("tier_layers", None)

    from sqlalchemy.orm.attributes import flag_modified

    proj.plot_bible = bible
    flag_modified(proj, "plot_bible")
    db.commit()
    return {"ok": True, "power_system": bible.get("power_system") or {},
            "tiers": bible.get("tier_layers") or []}


@router.post("/{project_id}/power-system/generate")
def generate_power_system(
    project_id: int, body: PowerSystemGenerateRequest, db: Session = Depends(get_db)
) -> dict:
    """AI 生成一套修炼体系（**只建议，不落库**）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    provider = _provider_or_none()
    if provider is None:
        raise HTTPException(status_code=503, detail="LLM 渠道未配置")

    from app.generation.power_system_gen import (
        _SYSTEM_PROMPT, build_system_prompt, parse_systems, retry_instructions,
        system_quality_issues, type_mismatch_issues,
    )

    want_type = (body.system_type or "").strip()
    prompt = build_system_prompt(body.genre or getattr(proj, "genre", "") or "",
                                 body.tone or getattr(proj, "style", "") or "",
                                 body.note or "", want_type)
    # ⚠️ system 段必须是 `_SYSTEM_PROMPT`（含三型说明、硬要求、输出 JSON 的格式）——
    # 之前这里只发了一行「你在为一本中文小说设计修炼体系。」，
    # **那份写着全部要求的提示词定义了却从没发出去** ⇒ 模型不知道要输出 JSON、
    # 不知道三型怎么选、也不知道档位怎么分层（实测因此回表格、回标题、
    # 把类型猜错、把细分摊平成 90 行）。
    raw = _model_call("生成修炼体系", lambda: provider.chat(_SYSTEM_PROMPT, prompt))
    systems = parse_systems(raw)
    issues = system_quality_issues(systems)
    issues += type_mismatch_issues(systems, want_type)
    if issues:
        # 解析不出来 **或** 解析出来的不像一套体系 ⇒ 带一句硬要求 + **具体错在哪**
        # 重试一次。
        #
        # 实测同一天多种形态：`#### 1. 开脉境` 标题 + 正文、Markdown 表格、
        # 有序列表；App 自己的 MiniMax 通道还回过**层级被摊平**的一版
        # （29 个境界，名字是「初期/中期/大圆满」反复出现）。
        # 解析器追不完，但**回灌一句硬要求 + 指出上一版错在哪**通常一次就够 ——
        # 这是「门槛 + 让执行方自己改」，比无限加形态支持稳。
        raw = _model_call(
            "生成修炼体系（重试）",
            lambda: provider.chat(system=_SYSTEM_PROMPT,
                                  user=prompt + retry_instructions(issues, want_type)),
        )
        systems = parse_systems(raw)
        issues = system_quality_issues(systems)
        issues += type_mismatch_issues(systems, want_type)
    if issues:
        # 失败时把**模型原文**带出去：只说「认不出」等于让人干瞪眼，
        # 看到原文才知道是模型写歪了、还是解析器没认出来（这两者的修法完全不同）。
        return {"ok": False,
                "detail": "AI 回了内容但认不出是一套体系：" + "；".join(issues[:3])
                          + "（已按格式要求重试一次）",
                "原文": (raw or "")[:2000]}
    return {"ok": True, "体系": systems}


@router.post("/{project_id}/power-system/tiers/generate")
def generate_tiers(
    project_id: int, body: TierGenerateRequest, db: Session = Depends(get_db)
) -> dict:
    """AI 生成档位分层（群像用；**只建议，不落库**）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    provider = _provider_or_none()
    if provider is None:
        raise HTTPException(status_code=503, detail="LLM 渠道未配置")

    from app.generation.power_system import systems_of
    from app.generation.power_system_gen import (
        build_tier_prompt, filter_tiers_by_bands, parse_tiers,
    )

    bible = _ps_bible(proj)
    systems = systems_of(bible)
    if not systems:
        raise HTTPException(status_code=400, detail="还没有修炼体系，先建体系")
    prompt = build_tier_prompt(systems[0]) + (f"\n\n{body.note}" if body.note else "")
    raw = _model_call("生成档位分层", lambda: provider.chat("你为群像设计档位分层。", prompt))
    tiers = filter_tiers_by_bands(parse_tiers(raw), systems[0])
    if not tiers:
        return {"ok": False, "detail": "AI 没给出可用分层（可重试或改要求）"}
    return {"ok": True, "档位层": tiers}


@router.post("/{project_id}/cultivation")
def save_cultivation(
    project_id: int, body: CultivationSaveRequest, db: Session = Depends(get_db)
) -> dict:
    """保存某个角色的**修炼档案**（当前境界/天赋/瓶颈/路线/代价）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")

    from app.generation.cultivation import attach_profile, normalize_profile
    from app.generation.power_system import systems_of

    bible = _ps_bible(proj)
    chars = bible.get("characters")
    if not isinstance(chars, list):
        chars = []
    found = False
    for c in chars:
        if isinstance(c, dict) and str(c.get("name") or "").strip() == name:
            c["card"] = attach_profile(c.get("card") or {}, normalize_profile(body.profile))
            found = True
            break
    if not found:
        chars.append({"name": name, "role": "", "motivation": "",
                      "card": attach_profile({}, normalize_profile(body.profile))})
    bible["characters"] = chars

    from sqlalchemy.orm.attributes import flag_modified

    proj.plot_bible = bible
    flag_modified(proj, "plot_bible")
    db.commit()
    return {"ok": True, "name": name}


@router.post("/{project_id}/cultivation/plan")
def plan_cultivation_route(
    project_id: int, body: RoutePlanRequest, db: Session = Depends(get_db)
) -> dict:
    """AI 规划某个角色的修炼路线（**只建议，不落库**）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    provider = _provider_or_none()
    if provider is None:
        raise HTTPException(status_code=503, detail="LLM 渠道未配置")

    from app.generation.cultivation import derive_situation, plan_route, profile_of
    from app.generation.power_system import systems_of

    bible = _ps_bible(proj)
    systems = systems_of(bible)
    if not systems:
        raise HTTPException(status_code=400, detail="还没有修炼体系，先建体系")

    name = (body.name or "").strip()
    profile = {}
    for c in (bible.get("characters") or []):
        if isinstance(c, dict) and str(c.get("name") or "").strip() == name:
            profile = profile_of(c.get("card") or {})
            break
    if not profile:
        raise HTTPException(status_code=400, detail="这个角色还没有修炼档案")

    route = _model_call(
        "规划修炼路线",
        lambda: plan_route(provider, profile, systems[0], raise_errors=True),
    )
    if not route:
        return {"ok": False, "detail": "AI 没给出可用路线（可重试或改要求）"}
    merged = {**profile, **route}
    return {
        "ok": True,
        "路线": route,
        "处境": derive_situation(merged, systems),
    }

class ScaleSaveRequest(StrictRequest):
    """`POST /api/projects/{id}/scale` 的请求体。"""

    name: str
    #: 量级信息（量级档 / 出手规模 / 压制）。
    scale: dict = {}


class ScaleGenerateRequest(StrictRequest):
    """`POST /api/projects/{id}/scale/generate` 的请求体。"""

    name: str
    note: str = ""


@router.get("/{project_id}/scale")
def get_scales(project_id: int, db: Session = Depends(get_db)) -> dict:
    """列出本项目所有角色的量级。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    bible = _ps_bible(proj)
    from app.generation.power_scale import resolve_scale
    from app.generation.power_system import systems_of

    systems = systems_of(bible)
    out = []
    for c in (bible.get("characters") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        card = c.get("card") if isinstance(c.get("card"), dict) else {}
        out.append({
            "name": c.get("name"),
            "role": c.get("role") or "",
            "scale": resolve_scale(card, systems),
        })
    return {"characters": out}


@router.post("/{project_id}/scale")
def save_scale(
    project_id: int, body: ScaleSaveRequest, db: Session = Depends(get_db)
) -> dict:
    """保存某个角色的**量级**（存在角色卡的 `card.量级` 下）。"""
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")

    from app.generation.power_scale import normalize_scale

    bible = _ps_bible(proj)
    chars = bible.get("characters")
    if not isinstance(chars, list):
        chars = []
    found = False
    for c in chars:
        if isinstance(c, dict) and str(c.get("name") or "").strip() == name:
            card = c.get("card") if isinstance(c.get("card"), dict) else {}
            card["量级"] = normalize_scale(body.scale)
            c["card"] = card
            found = True
            break
    if not found:
        chars.append({"name": name, "role": "", "motivation": "",
                      "card": {"量级": normalize_scale(body.scale)}})
    bible["characters"] = chars

    from sqlalchemy.orm.attributes import flag_modified

    proj.plot_bible = bible
    flag_modified(proj, "plot_bible")
    db.commit()
    return {"ok": True, "name": name}


@router.post("/{project_id}/scale/generate")
def generate_scale(
    project_id: int, body: ScaleGenerateRequest, db: Session = Depends(get_db)
) -> dict:
    """AI 帮角色定量级（**只建议，不落库**）。

    量级是**相对标尺**，不是绝对数值：判据是「他出手能造成什么后果」。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    provider = _provider_or_none()
    if provider is None:
        raise HTTPException(status_code=503, detail="LLM 渠道未配置")

    from app.generation.power_scale_gen import build_scale_prompt, parse_scale

    bible = _ps_bible(proj)
    name = (body.name or "").strip()
    char = {}
    for c in (bible.get("characters") or []):
        if isinstance(c, dict) and str(c.get("name") or "").strip() == name:
            char = dict(c)
            break
    if not char:
        raise HTTPException(status_code=404, detail="这个角色不存在")

    prompt = build_scale_prompt(char, bible, note=body.note or "")
    raw = _model_call("给角色定量级",
                      lambda: provider.chat("你在为小说角色定「量级」。", prompt))
    scale = parse_scale(raw)
    if not scale:
        return {"ok": False, "detail": "AI 没给出可用量级（可重试或改要求）"}
    return {"ok": True, "量级": scale}

