"""语料摄取窗口状态只读端点的测试：内存 SQLite，不跑摄取、不碰生产库。

跑法：项目根 `python -m pytest backend/tests/test_kg_v3_corpus_windows.py -q`。

重点锁三件事：
1. 窗口表为空 → `ingested=False`（前端必须说「尚未摄取」，不能说「无实体」）；
2. 失败窗口计数与原因归类正确，且原始 error 原样保留；
3. 本地模板兜底的 parse_failed 被单独计数，供前端标注「疑似渠道不可用」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import KgCorpusWindow, Project
from app.routers.kg_v3 import get_corpus_windows


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _project(db) -> Project:
    p = Project(title="语料窗口状态测试")
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _window(db, project, *, novel_id=1, start=1, end=3, status="done",
            error=None, attempts=1, updated=None):
    w = KgCorpusWindow(
        project_id=project.id, novel_id=novel_id,
        window_key=f"{novel_id}:{start}-{end}",
        chapter_start=start, chapter_end=end,
        status=status, attempts=attempts, error=error,
        updated_at=updated or datetime(2026, 11, 17, 3, 0, 0),
    )
    db.add(w)
    db.commit()
    return w


def test_empty_table_is_not_ingested(db):
    """从未跑过 → ingested=False；不能把空表说成「没有实体」。"""
    project = _project(db)

    out = get_corpus_windows(project.id, None, db=db)

    assert out["ingested"] is False
    assert out["total"] == 0
    assert out["failed"] == 0
    assert out["last_run_at"] is None
    assert out["failed_reasons"] == {
        "parse_failed": 0, "channel_unavailable": 0, "other": 0,
    }


def test_failed_windows_are_counted_and_raw_error_kept(db):
    """失败计数、原因归类、离线兜底疑似计数、原始 error 保留。"""
    project = _project(db)
    _window(db, project, start=1, end=3, status="done")
    _window(db, project, start=4, end=6, status="done")
    _window(db, project, start=7, end=9, status="running")
    _window(db, project, start=10, end=12, status="pending")
    parse_error = (
        "parse_failed: 无法从模型输出解析出 JSON（前 80 字："
        "'【离线兜底·确定性续写】已锁定角色：（暂无锁定角色）…'）"
    )
    _window(db, project, start=13, end=15, status="failed",
            error=parse_error, updated=datetime(2026, 11, 17, 3, 5, 0))
    _window(db, project, start=16, end=18, status="failed",
            error="未配置可用的模型渠道：没有 API Key")

    out = get_corpus_windows(project.id, None, db=db)

    assert out["ingested"] is True
    assert out["total"] == 6
    assert out["counts"] == {"pending": 1, "running": 1, "success": 2, "failed": 2}
    assert out["failed"] == 2
    assert out["failed_reasons"] == {
        "parse_failed": 1, "channel_unavailable": 1, "other": 0,
    }
    assert out["suspected_local_fallback"] == 1
    assert len(out["failures"]) == 2
    kept = {f["window_key"]: f for f in out["failures"]}
    assert kept["1:13-15"]["error"] == parse_error
    assert kept["1:13-15"]["reason"] == "parse_failed"
    assert kept["1:16-18"]["reason"] == "channel_unavailable"
    # 最近一次运行时间取全部窗口 updated_at 的最大值。
    assert out["last_run_at"] == "2026-11-17T03:05:00"


def test_novel_filter_and_project_404(db):
    project = _project(db)
    _window(db, project, novel_id=1, start=1, end=3, status="done")
    _window(db, project, novel_id=2, start=1, end=3, status="failed",
            error="boom")

    only_book_2 = get_corpus_windows(project.id, 2, db=db)
    assert only_book_2["total"] == 1
    assert only_book_2["failed"] == 1
    assert only_book_2["failed_reasons"]["other"] == 1

    with pytest.raises(HTTPException) as exc:
        get_corpus_windows(99999, None, db=db)
    assert exc.value.status_code == 404
