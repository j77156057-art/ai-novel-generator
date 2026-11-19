"""生成进度端点（`GET /api/projects/{id}/generation-status`）的定向测试。

为什么单独测它：前端执行卡片改成显示轨迹后，「本次是不是已经落库」全靠
这里的 `count` / `last_segment_no` 判断。口径错了界面就会说谎——例如把
「全库段数」当成「本次落库」，或者在没有生成时仍报 running。

跑法：项目根 `python -m pytest backend/tests/test_generation_status.py -q`。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Generation, Project
from app.routers.generation import generation_status


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


@pytest.fixture(autouse=True)
def _clean_leases():
    """进程内租约是全局状态：每条用例前后都清掉，避免互相串味。"""
    from app.generation import inflight

    inflight.reset()
    yield
    inflight.reset()


def _seed(db, *, written: int) -> Project:
    project = Project(title="进度测试")
    db.add(project)
    db.commit()
    db.refresh(project)
    for chapter in range(1, written + 1):
        db.add(Generation(
            project_id=project.id,
            segment_no=chapter,
            chapter_range=f"第{chapter}章",
            text="正文",
        ))
    db.commit()
    return project


def test_counts_segments_and_reports_latest_chapter(db):
    project = _seed(db, written=3)

    out = generation_status(project.id, db)

    assert out["project_id"] == project.id
    assert out["count"] == 3
    assert out["total_chars"] == 6  # 3 段 × 「正文」2 字
    assert out["last_segment_no"] == 3
    assert out["last_chapter"] == "第3章"
    assert out["running"] is False


def test_empty_project_reports_null_latest(db):
    project = _seed(db, written=0)

    out = generation_status(project.id, db)

    assert out["count"] == 0
    assert out["total_chars"] == 0
    assert out["last_segment_no"] is None
    assert out["last_chapter"] is None


def test_running_follows_inflight_lease(db):
    project = _seed(db, written=1)
    from app.generation import inflight

    assert inflight.try_begin(project.id) is True
    assert generation_status(project.id, db)["running"] is True

    inflight.end(project.id)
    assert generation_status(project.id, db)["running"] is False


def test_unknown_project_is_404(db):
    with pytest.raises(HTTPException) as exc:
        generation_status(9999, db)

    assert exc.value.status_code == 404
