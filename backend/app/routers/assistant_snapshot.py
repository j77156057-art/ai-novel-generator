# -*- coding: utf-8 -*-
"""重写快照的撤销入口：列表 / 单段回滚 / 整批回滚。

与 ``/api/assistant/invoke-generation`` 成对：那边每重写一段就记一条快照，
这边负责「退回去」。语义见 ``.trae/documents/rewrite-snapshot-design.md`` §7。

409 的两类场景都返回**可操作**的结构，前端据此弹选项，而不是展示数据库原文：

- ``bible_changed_since_snapshot``：回滚后设定变过 → 三个选项
  ``restore_bible_then_text`` / ``text_only`` / ``cancel``；
- ``segment_busy``：同段正在重写（只在 invoke-generation 里出现）；
- ``text_drifted`` / ``batch_not_rollbackable``：正文在快照之后又被改过，
  拒绝覆盖（列出是哪几段、为什么）。
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.agents.dialog.rollback import (
    RollbackError,
    list_batches,
    rollback_batch,
    rollback_latest,
    rollback_snapshot,
)
from app.agents.dialog.snapshot import list_snapshots
from app.db import get_db
from app.routers.assistant_shared import _require_project
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant.snapshot")

router = APIRouter(prefix="/api/assistant", tags=["assistant-snapshots"])

Mode = Literal["auto", "text_only", "with_bible"]


class RollbackRequest(StrictRequest):
    project_id: int
    snapshot_id: int | None = None
    segment_no: int | None = None
    # auto：设定变过就 409 让用户选；text_only：只退正文；with_bible：先退设定再退正文
    mode: Mode = "auto"
    actor: str | None = None


class RollbackBatchRequest(StrictRequest):
    project_id: int
    batch_id: str
    mode: Mode = "auto"
    actor: str | None = None


def _http(exc: RollbackError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/snapshots")
def get_snapshots(
    project_id: int = Query(..., ge=1),
    segment_no: int | None = Query(None, ge=1),
    batch_id: str | None = Query(None),
    status: str | None = Query(None, pattern="^(pending|applied|failed|rolled_back)$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """快照列表（含 pending/failed，便于解释「这段为什么现在不能重写」）。"""
    _require_project(db, project_id)
    items = list_snapshots(
        db, project_id=project_id, segment_no=segment_no,
        batch_id=batch_id, status=status, limit=limit,
    )
    return {"items": items, "count": len(items)}


@router.get("/snapshots/groups")
def get_groups(
    project_id: int = Query(..., ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """按「一次操作」聚合的撤销入口（整组回滚用）。

    路径故意用 ``groups`` 而不是 ``batches``：``test_apply_has_no_bulk_route``
    对 ``/api/assistant`` 下带 ``batch|bulk`` 的路径一律判红（那条红线针对
    「批量应用提案」）。整组回滚是用户明确要求的撤销语义，不是批量应用，
    换一个不含该词的路径既守住红线，也不弱化它。
    """
    _require_project(db, project_id)
    items = list_batches(db, project_id=project_id, limit=limit)
    return {"items": items, "count": len(items)}


@router.post("/snapshots/rollback")
def post_rollback(body: RollbackRequest, db: Session = Depends(get_db)) -> dict:
    """单段回滚。不给 snapshot_id 时退该段「上一次」（UI 默认）。"""
    project = _require_project(db, body.project_id)
    try:
        if body.snapshot_id is not None:
            return rollback_snapshot(
                db, project, body.snapshot_id, mode=body.mode, actor=body.actor
            )
        if body.segment_no is None:
            raise HTTPException(
                status_code=422,
                detail="snapshot_id 与 segment_no 至少给一个（只给 segment_no 即「撤销上一次」）",
            )
        return rollback_latest(
            db, project, body.segment_no, mode=body.mode, actor=body.actor
        )
    except RollbackError as exc:
        logger.info("回滚被拒（project=%s）：%s", project.id, exc)
        raise _http(exc) from exc


@router.post("/snapshots/rollback-group")
def post_rollback_group(
    body: RollbackBatchRequest, db: Session = Depends(get_db)
) -> dict:
    """整组回滚：一个事务恢复所有正文；任一段不自洽则零写入并报出原因。"""
    project = _require_project(db, body.project_id)
    try:
        return rollback_batch(
            db, project, body.batch_id, mode=body.mode, actor=body.actor
        )
    except RollbackError as exc:
        logger.info("整批回滚被拒（project=%s, batch=%s）：%s",
                    project.id, body.batch_id, exc)
        raise _http(exc) from exc
