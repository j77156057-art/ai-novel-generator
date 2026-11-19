"""世界观端点：读取归一化 + 保存往返 + 空组清空。

跑法（backend 目录）：`python -m pytest tests/test_project_background.py -q`
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Project
from app.routers.projects import (
    BackgroundSaveRequest,
    get_background,
    save_background,
)


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


def _seed(db, bible: dict | None = None) -> Project:
    project = Project(title="世界观测试", plot_bible=bible or {})
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_get_normalizes_aliases(db):
    project = _seed(db, {"background": {"世界观": "灵气稀缺", "规则": "御剑耗气"}})
    resp = get_background(project.id, db)
    assert resp["background"]["一句话世界"] == "灵气稀缺"
    assert resp["background"]["硬规则"] == ["御剑耗气"]


def test_save_roundtrip_and_clear(db):
    project = _seed(db)
    saved = save_background(
        project.id,
        BackgroundSaveRequest(
            background={
                "一句话世界": " 灵气稀缺 ",
                "硬规则": [" 御剑耗气 ", ""],
                "势力与地点": ["青云宗——名门正派"],
            }
        ),
        db,
    )
    assert saved["ok"] is True
    assert saved["background"]["一句话世界"] == "灵气稀缺"
    assert saved["background"]["硬规则"] == ["御剑耗气"]

    cleared = save_background(
        project.id, BackgroundSaveRequest(background={}), db,
    )
    assert cleared["background"] == {}
    db.refresh(project)
    assert "background" not in (project.plot_bible or {})
