"""伏笔节奏规划器：规则单测（不调模型、不写生产库）。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.foreshadow_planner import apply_plan, build_plan, plan_entry
from app.models import ForeshadowLedger, Project
from app.routers.foreshadow_ledger import create_foreshadow, ForeshadowCreate


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


def _seed_project(db, chapters: int = 100) -> Project:
    project = Project(
        title="节奏测试",
        outline=[f"第{i}章《标题{i}》：事件{i}。" for i in range(1, chapters + 1)],
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_plan_entry_main_uses_percent_window():
    entry = {
        "title": "主线",
        "status": "open",
        "tier": "main",
        "planted_chapter": 1,
        "total_chapters": 100,
        "reveal_ratio": 0.9,
        "reveal_span_ratio": 0.1,
        "truth": "背景。第一层反转：A。第二层反转：B。第三层反转：C。",
        "hints": "暗示一",
    }
    result = plan_entry(entry, 100)
    plan = result["plan"]
    assert plan["plant_chapter"] == 1
    assert [layer["chapter"] for layer in plan["reveal_layers"]] == [90, 94, 97]
    assert len(plan["beats"]) == 4
    assert result["issues"] == []


def test_plan_entry_arc_and_chapter_leads():
    arc = {
        "title": "卷级",
        "status": "open",
        "tier": "arc",
        "planned_chapter": 35,
    }
    chapter = {
        "title": "小节",
        "status": "open",
        "tier": "chapter",
        "planned_chapter": 8,
    }
    assert plan_entry(arc, 100)["plan"]["plant_chapter"] == 15
    assert plan_entry(chapter, 100)["plan"]["plant_chapter"] == 4
    assert plan_entry(chapter, 100)["plan"]["beats"] == [
        {"chapter": 6, "kind": "hint"}
    ]


def test_plan_entry_clamps_payoff_beyond_book():
    entry = {
        "title": "越界",
        "status": "open",
        "tier": "chapter",
        "planned_chapter": 500,
    }
    result = plan_entry(entry, 100)
    assert result["plan"]["payoff_chapter"] == 100
    assert any("超出全书" in issue for issue in result["issues"])


def test_build_and_apply_plan_writes_columns(db):
    project = _seed_project(db, 100)
    main = create_foreshadow(
        project.id,
        ForeshadowCreate(
            title="账本暗语",
            tier="main",
            planted_chapter=1,
            truth="背景。第一层反转：A。第二层反转：B。第三层反转：C。",
            hints="暗示一",
        ),
        db,
    )
    arc = create_foreshadow(
        project.id,
        ForeshadowCreate(title="楼灵脉", tier="arc", planned_chapter=35),
        db,
    )

    report = build_plan(db, project)
    assert report["total_chapters"] == 100
    assert report["summary"]["counts"]["main"] == 1

    applied = apply_plan(db, project)
    assert applied["applied"] == 2

    rows = {
        row.id: row
        for row in db.query(ForeshadowLedger)
        .filter(ForeshadowLedger.project_id == project.id)
        .all()
    }
    assert rows[main["id"]].total_chapters == 100
    assert rows[main["id"]].reveal_ratio == 0.9
    assert rows[main["id"]].beat_plan
    assert rows[arc["id"]].planted_chapter == 15
    assert rows[arc["id"]].planned_chapter == 35


def test_foreshadow_mode_off_auto_manual(db):
    """生成开关三种模式：off 不注入、auto 按规划器、manual 按台账。"""
    from app.generation.engine import _foreshadow_ledger_block

    project = _seed_project(db, 100)
    create_foreshadow(
        project.id,
        ForeshadowCreate(title="账本暗语", tier="main", planted_chapter=1,
                         truth="背景。第一层反转：A。", hints="暗示一"),
        db,
    )
    # 台账里故意留空回收章，auto 会补成规划器算出的值。
    create_foreshadow(
        project.id,
        ForeshadowCreate(title="楼灵脉", tier="arc"),
        db,
    )

    off_block, off_lines = _foreshadow_ledger_block(db, project.id, mode="off")
    assert off_block == "" and off_lines == []

    manual_block, manual_lines = _foreshadow_ledger_block(
        db, project.id, mode="manual"
    )
    assert "楼灵脉" in manual_block

    auto_block, auto_lines = _foreshadow_ledger_block(db, project.id, mode="auto")
    assert "楼灵脉" in auto_block
    # auto 给缺回收章的卷级伏笔补了计划章（100 章的 35% = 35）。
    assert any("计划第35章回收" in line for line in auto_lines)
