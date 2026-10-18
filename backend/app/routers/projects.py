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


@router.post("", response_model=ProjectOut)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    proj = Project(
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

    并发防重：项目上已有 pending/running 任务且不 force 时，直接复用那条
    （返回它的 job_id，`reused: True`），不重复烧一遍 LLM。**不分档位**——
    两个后台线程共写同一 `project.style_narrative` 会互相覆盖（后完成的
    盖掉先完成的，可能旧档盖新档）且争抢 SQLite 写锁；想换档位重算，
    等当前任务终态后再提交（或对旧任务无感知时直接重提，force 另算）。

    缓存命中预测：不 force 时若本次会直接复用「项目已存档案」或「蒸馏库」，
    返回 `from_cache: True` 与 `cache_source`（"project" / "library"）。
    前端据此如实提示「秒级复用、未重新调用模型」，避免「一秒蒸馏完却没解释」。
    预测时算出的 `content_hash` 会传给后台任务，同一条链路不重复全量加语料。
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
    # 预测里算出的 content_hash 顺手传给后台任务（P2-2）：同一条提交链路
    # 「预测算一次 + 后台再算一次」全量加载语料是重复劳动。
    from_cache = False
    cache_source = None
    content_hash = None
    if not body.force:
        ids = [int(i) for i in (proj.reference_novel_ids or [])]
        if ids:
            from app.style.narrative import (
                corpus_content_hash,
                find_style_distill,
                narrative_fingerprint,
            )

            want = narrative_fingerprint(ids, cfg)
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
        # 并发防重：项目上已有未终态任务就直接复用它的 job_id（不分档位）。
        # 同档位才复用是旧契约——换档位放行会让两个后台线程共写
        # project.style_narrative 互相覆盖 + 写锁竞争（P2-1 修复）。
        active = find_active_run(db, project_id)
        if active is not None:
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
        run_distill_job, run.id, project_id, cfg.key, body.force, content_hash
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


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db)) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


@router.patch("/{project_id}", response_model=ProjectOut)
def patch_project(
    project_id: int, body: ProjectPatch, db: Session = Depends(get_db)
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
    for field, value in updates.items():
        setattr(proj, field, value)
    db.commit()
    db.refresh(proj)
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


@router.get("/{project_id}/bible/revisions")
def list_bible_revisions(
    project_id: int,
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
