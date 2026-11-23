# -*- coding: utf-8 -*-
"""素材包 API：查看目录/条目、手动增删改、启动与取消后台调研。

写入边界：
- 用户新增/编辑的素材直接标 `usable`（不需要模型再整理）；
- 后台调研由 worker 线程执行，本层只负责排队与查询状态。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.materials import store, worker
from app.models import MaterialResearchJob, Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["materials"])


class MaterialCreate(StrictRequest):
    title: str
    summary: str = ""
    use_case: str = ""
    category: str = ""
    published_year: int | None = None
    url: str = ""
    snippet: str = ""
    tags: list[str] = []
    status: str = "usable"


class MaterialPatch(StrictRequest):
    title: str | None = None
    summary: str | None = None
    use_case: str | None = None
    category: str | None = None
    published_year: int | None = None
    url: str | None = None
    snippet: str | None = None
    tags: list[str] | None = None
    status: str | None = None


class ResearchStart(StrictRequest):
    chapter_no: int | None = None
    queries: list[str] = []


def _project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.get("/{project_id}/materials")
def list_materials(
    project_id: int,
    q: str = "",
    status: str | None = None,
    category: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    unknown_year: bool = False,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> dict:
    _project(db, project_id)
    return store.list_materials(
        db,
        project_id,
        q=q,
        status=status,
        category=category,
        year_from=year_from,
        year_to=year_to,
        unknown_year=unknown_year,
        page=page,
        page_size=page_size,
    )


@router.get("/{project_id}/materials/toc")
def material_toc(project_id: int, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    return store.toc(db, project_id)


@router.get("/{project_id}/materials/stats")
def material_stats(project_id: int, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    return store.stats(db, project_id)


@router.post("/{project_id}/materials/reparse-years")
def reparse_years(project_id: int, db: Session = Depends(get_db)) -> dict:
    """按新的年份规则重算存量素材（跳过手动素材）。"""
    _project(db, project_id)
    return store.backfill_years(db, project_id)


# —— 任务路由必须放在 `{material_id}` 之前，否则 "jobs" 会被当成 id ——

@router.get("/{project_id}/materials/jobs")
def list_jobs(project_id: int, limit: int = 20, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    return {"items": [store.job_to_dict(j) for j in store.list_jobs(db, project_id, limit)]}


@router.post("/{project_id}/materials/jobs")
def start_research(
    project_id: int,
    body: ResearchStart | None = None,
    db: Session = Depends(get_db),
) -> dict:
    project = _project(db, project_id)
    body = body or ResearchStart()
    job = worker.enqueue_research(
        db, project, body.chapter_no, trigger="manual"
    )
    if job is None:
        raise HTTPException(status_code=503, detail="素材调研未启用或入队失败")
    queries = [str(q).strip() for q in (body.queries or []) if str(q).strip()]
    if queries:
        job.queries = queries
        job.topics = queries[:6]
        db.add(job)
        db.commit()
    return store.job_to_dict(job)


@router.get("/{project_id}/materials/jobs/{job_id}")
def get_job(project_id: int, job_id: int, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    job = db.get(MaterialResearchJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail="调研任务不存在")
    return store.job_to_dict(job)


@router.post("/{project_id}/materials/jobs/{job_id}/cancel")
def cancel_job(project_id: int, job_id: int, db: Session = Depends(get_db)) -> dict:
    _project(db, project_id)
    job = db.get(MaterialResearchJob, job_id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=404, detail="调研任务不存在")
    if job.status in ("queued", "running", "consolidating"):
        job.status = "cancelled"
        job.error = "用户取消"
        db.add(job)
        db.commit()
    return store.job_to_dict(job)


# —— 单条素材 ——

@router.post("/{project_id}/materials")
def create_material(
    project_id: int,
    body: MaterialCreate,
    db: Session = Depends(get_db),
) -> dict:
    _project(db, project_id)
    title = str(body.title or "").strip()
    if not title:
        raise HTTPException(status_code=422, detail="title 不能为空")
    status = body.status if body.status in ("raw", "usable", "hidden") else "usable"
    added = store.add_many(
        db,
        project_id,
        [
            {
                "title": title,
                "summary": body.summary,
                "use_case": body.use_case,
                "category": body.category,
                "published_year": body.published_year,
                "url": body.url,
                "snippet": body.snippet,
                "tags": body.tags,
            }
        ],
        status=status,
        origin="manual",
    )
    if not added:
        raise HTTPException(status_code=409, detail="相同素材已存在")
    return store.to_dict(added[0])


@router.patch("/{project_id}/materials/{material_id}")
def patch_material(
    project_id: int,
    material_id: int,
    body: MaterialPatch,
    db: Session = Depends(get_db),
) -> dict:
    _project(db, project_id)
    row = store.update_material(
        db, project_id, material_id, body.model_dump(exclude_unset=True)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="素材不存在")
    return store.to_dict(row)


@router.delete("/{project_id}/materials/{material_id}")
def delete_material(
    project_id: int, material_id: int, db: Session = Depends(get_db)
) -> dict:
    _project(db, project_id)
    if not store.delete_material(db, project_id, material_id):
        raise HTTPException(status_code=404, detail="素材不存在")
    return {"ok": True}
