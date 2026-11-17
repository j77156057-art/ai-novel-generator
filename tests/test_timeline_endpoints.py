"""B2：时间线节点端点（追加 / 重算章区间 / 只读）的回归。

不启 FastAPI、不调 LLM：直接调路由函数 + 内存库，断言 `plot_timeline`
的节点集合与章区间。口径与 `app.generation.timeline` 一致。
"""
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.models import Project  # noqa: E402
from app.routers.timeline import (  # noqa: E402
    TimelineNodeIn,
    append_node,
    assign_ranges,
    get_timeline,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _project(db, chapters: int = 10) -> Project:
    proj = Project(
        title="t",
        outline=[f"第{i}章：剧情{i}" for i in range(1, chapters + 1)],
        target_length=60000,
        generation_length=3000,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def test_append_node_creates_timeline_and_covers_outline(db):
    proj = _project(db, chapters=10)

    out = append_node(proj.id, TimelineNodeIn(title="新剧情点", emotion="爽"), db)

    created = out["created"]
    assert created["id"].startswith("n") and created["title"] == "新剧情点"
    timeline = out["timeline"]
    assert timeline["exists"] is True
    assert created["id"] in timeline["order"]
    assert timeline["has_chapter_ranges"] is True
    # 区间必须从第 1 章连续覆盖到第 10 章（《balanced 分配》的验收口径）
    nodes = timeline["nodes"]
    assert nodes[0]["chapter_start"] == 1
    assert nodes[-1]["chapter_end"] == 10
    for prev, nxt in zip(nodes, nodes[1:]):
        assert prev["chapter_end"] + 1 == nxt["chapter_start"]


def test_assign_ranges_follows_outline_growth(db):
    proj = _project(db, chapters=10)
    append_node(proj.id, TimelineNodeIn(title="节点A"), db)

    # 章纲从 10 章扩到 20 章
    proj.outline = [f"第{i}章：剧情{i}" for i in range(1, 21)]
    db.add(proj)
    db.commit()

    out = assign_ranges(proj.id, db)

    assert out["outline_chapters"] == 20
    assert out["nodes"][-1]["chapter_end"] == 20


def test_get_timeline_without_archive_does_not_create(db):
    proj = _project(db, chapters=5)
    out = get_timeline(proj.id, db)
    assert out["exists"] is False and out["nodes"] == []
    assert proj.plot_timeline is None  # 只读端点不建骨架


def test_append_requires_outline(db):
    proj = Project(title="t", outline=None)
    db.add(proj)
    db.commit()
    with pytest.raises(HTTPException) as err:
        append_node(proj.id, TimelineNodeIn(title="x"), db)
    assert err.value.status_code == 400


def test_assign_without_archive_is_409(db):
    proj = _project(db, chapters=5)
    with pytest.raises(HTTPException) as err:
        assign_ranges(proj.id, db)
    assert err.value.status_code == 409


def test_append_rejects_bad_emotion(db):
    proj = _project(db, chapters=5)
    with pytest.raises(HTTPException) as err:
        append_node(proj.id, TimelineNodeIn(title="x", emotion="离谱"), db)
    assert err.value.status_code == 400
