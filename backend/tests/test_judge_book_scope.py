"""Judge 的书维度过滤：参考书的伏笔/事件不能算成本书的告警。

项目 6（续写《修真聊天群》）实测：Judge 面板把参考书里「楚阁主的长生之道」
等伏笔报成超期 3123 章。根因是 `_book_cond(None)` 之前返回空（不过滤），
且伏笔查询完全没带书维度条件。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.kg.judge import check_foreshadow_expiry
from app.models import KgEvent, KgForeshadow, Project


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


def _seed(db) -> Project:
    project = Project(title="续写项目", reference_novel_ids=[7])
    db.add(project)
    db.commit()
    db.refresh(project)

    # 本书（项目级）事件：当前到第 60 章
    db.add(KgEvent(
        project_id=project.id, sequence=1, segment_no=60, event_type="incident",
        summary="本书事件", chapter=60, is_current=True, version=1,
    ))
    # 参考书事件：章号巨大，不应影响本书的 current_chapter
    db.add(KgEvent(
        project_id=project.id, novel_id=7, sequence=2, segment_no=3173,
        event_type="incident", summary="参考书事件", chapter=3173,
        is_current=True, version=1,
    ))
    # 本书伏笔：第 10 章该回收，当前 60 章 → 真超期
    db.add(KgForeshadow(
        project_id=project.id, novel_id=None, name="本书伏笔",
        status="planted", planted_chapter=1, planned_payoff_chapter=10,
    ))
    # 参考书伏笔：不应被本书 Judge 报出来
    db.add(KgForeshadow(
        project_id=project.id, novel_id=7, name="楚阁主的长生之道",
        status="planted", planted_chapter=1, planned_payoff_chapter=50,
    ))
    db.commit()
    return project


def test_foreshadow_expiry_ignores_reference_book(db):
    project = _seed(db)

    findings = check_foreshadow_expiry(
        db, project.id, "", segment_no=60, novel_id=None, text_available=False,
    )

    names = {f.entity for f in findings}
    assert names == {"本书伏笔"}
    assert "楚阁主的长生之道" not in names


def test_foreshadow_expiry_keeps_current_book_plus_project_level(db):
    project = _seed(db)
    # 指定当前书为 7：此时书 7 的伏笔是本书的，应该被审到
    findings = check_foreshadow_expiry(
        db, project.id, "", segment_no=60, novel_id=7, text_available=False,
    )
    names = {f.entity for f in findings}
    assert names == {"本书伏笔", "楚阁主的长生之道"}
