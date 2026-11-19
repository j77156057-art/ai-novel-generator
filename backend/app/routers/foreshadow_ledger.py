"""伏笔台账 CRUD：/api/projects/{project_id}/foreshadows

与设定卡同一套纪律：

1. **未知字段键直接拒绝**（`StrictRequest` 的 extra=forbid）；
2. **删除是真删**；想结案请把 `status` 改成 `resolved` / `dropped`；
3. **非法值 422**，不静默截断（标题/章号/文本上限见 `app.foreshadow_ledger`）。

台账是作者意图层；KG 的 `kg_foreshadows` / `plothole` 是事实层，本路由不碰。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.foreshadow_ledger import (
    STATUSES,
    entries_of,
    import_from_bible,
    import_from_kg,
    kg_linkage,
    legacy_titles,
    normalize_entry,
)
from app.models import ForeshadowLedger, Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["foreshadow-ledger"])


class ForeshadowCreate(StrictRequest):
    title: str
    status: str = "open"
    planted_chapter: int | None = None
    planned_chapter: int | None = None
    resolved_chapter: int | None = None
    evidence: str = ""
    resolution: str = ""


class ForeshadowPatch(StrictRequest):
    """部分更新：只改传了的字段。章号传 null 表示清空。"""

    title: str | None = None
    status: str | None = None
    planted_chapter: int | None = None
    planned_chapter: int | None = None
    resolved_chapter: int | None = None
    evidence: str | None = None
    resolution: str | None = None


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


def _entry_or_404(db: Session, project_id: int, entry_id: int) -> ForeshadowLedger:
    entry = db.get(ForeshadowLedger, entry_id)
    if entry is None or entry.project_id != project_id:
        raise HTTPException(status_code=404, detail="伏笔条目不存在")
    return entry


def _serialize(entry: ForeshadowLedger) -> dict:
    return {
        "id": entry.id,
        "project_id": entry.project_id,
        "title": entry.title,
        "status": entry.status,
        "planted_chapter": entry.planted_chapter,
        "planned_chapter": entry.planned_chapter,
        "resolved_chapter": entry.resolved_chapter,
        "evidence": entry.evidence or "",
        "resolution": entry.resolution or "",
        "source": entry.source,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def _payload(
    *,
    title: str,
    status: str,
    planted_chapter: int | None,
    planned_chapter: int | None,
    resolved_chapter: int | None,
    evidence: str,
    resolution: str,
) -> dict:
    try:
        return normalize_entry(
            title,
            status=status,
            planted_chapter=planted_chapter,
            planned_chapter=planned_chapter,
            resolved_chapter=resolved_chapter,
            evidence=evidence,
            resolution=resolution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _list_payload(db: Session, project: Project) -> dict:
    """台账列表的统一响应体（GET / 两种导入共用），附带 KG 联动清单。"""
    entries = entries_of(db, project.id)
    summary = {status: 0 for status in STATUSES}
    for entry in entries:
        summary[entry.status] = summary.get(entry.status, 0) + 1
    return {
        "project_id": project.id,
        "entries": [_serialize(entry) for entry in entries],
        "summary": summary,
        "legacy": legacy_titles(project, entries),
        "kg": kg_linkage(db, project.id, entries),
    }


@router.get("/{project_id}/foreshadows")
def list_foreshadows(project_id: int, db: Session = Depends(get_db)) -> dict:
    """返回全部台账 + 三态计数 + 剧情圣经里尚未导入的旧伏笔标题 + KG 联动清单。"""
    project = _project_or_404(db, project_id)
    return _list_payload(db, project)


@router.post("/{project_id}/foreshadows/import")
def import_foreshadows(project_id: int, db: Session = Depends(get_db)) -> dict:
    """把 `plot_bible.foreshadowing` 导入台账；按标题去重，只新增不覆盖。"""
    project = _project_or_404(db, project_id)
    result = import_from_bible(db, project)
    db.commit()
    return {"created": result["created"], "titles": result["titles"], **_list_payload(db, project)}


@router.post("/{project_id}/foreshadows/import-kg")
def import_foreshadows_from_kg(project_id: int, db: Session = Depends(get_db)) -> dict:
    """把 KG 里仍 open 的未填坑登记进台账；按标题去重，只新增不覆盖。"""
    project = _project_or_404(db, project_id)
    result = import_from_kg(db, project_id, entries_of(db, project_id))
    db.commit()
    return {"created": result["created"], "titles": result["titles"], **_list_payload(db, project)}


@router.post("/{project_id}/foreshadows")
def create_foreshadow(
    project_id: int, body: ForeshadowCreate, db: Session = Depends(get_db)
) -> dict:
    _project_or_404(db, project_id)
    payload = _payload(
        title=body.title,
        status=body.status,
        planted_chapter=body.planted_chapter,
        planned_chapter=body.planned_chapter,
        resolved_chapter=body.resolved_chapter,
        evidence=body.evidence,
        resolution=body.resolution,
    )
    entry = ForeshadowLedger(project_id=project_id, source="manual", **payload)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _serialize(entry)


@router.patch("/{project_id}/foreshadows/{entry_id}")
def update_foreshadow(
    project_id: int,
    entry_id: int,
    body: ForeshadowPatch,
    db: Session = Depends(get_db),
) -> dict:
    _project_or_404(db, project_id)
    entry = _entry_or_404(db, project_id, entry_id)

    # 只覆盖显式传入的字段：PATCH 不传 ≠ 清空。
    provided = body.model_fields_set
    payload = _payload(
        title=body.title if "title" in provided else entry.title,
        status=body.status if "status" in provided else entry.status,
        planted_chapter=(
            body.planted_chapter if "planted_chapter" in provided
            else entry.planted_chapter
        ),
        planned_chapter=(
            body.planned_chapter if "planned_chapter" in provided
            else entry.planned_chapter
        ),
        resolved_chapter=(
            body.resolved_chapter if "resolved_chapter" in provided
            else entry.resolved_chapter
        ),
        evidence=body.evidence if "evidence" in provided else (entry.evidence or ""),
        resolution=(
            body.resolution if "resolution" in provided else (entry.resolution or "")
        ),
    )

    entry.title = payload["title"]
    entry.status = payload["status"]
    entry.planted_chapter = payload["planted_chapter"]
    entry.planned_chapter = payload["planned_chapter"]
    entry.resolved_chapter = payload["resolved_chapter"]
    entry.evidence = payload["evidence"] or None
    entry.resolution = payload["resolution"] or None
    db.commit()
    db.refresh(entry)
    return _serialize(entry)


@router.delete("/{project_id}/foreshadows/{entry_id}")
def delete_foreshadow(
    project_id: int, entry_id: int, db: Session = Depends(get_db)
) -> dict:
    _project_or_404(db, project_id)
    entry = _entry_or_404(db, project_id, entry_id)
    deleted_id = entry.id
    db.delete(entry)
    db.commit()
    return {"ok": True, "deleted_id": deleted_id}
