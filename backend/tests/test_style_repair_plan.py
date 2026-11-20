"""定向返修计划器（`app.style.repair.build_repair_plan`）的单元测试。

这里不连库、不调模型：只验证「指标偏离 / 反向禁忌 → 可执行返修指令」
这条翻译链，以及指令不会误触发局部改写关键词。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Generation, Project
from app.routers.style_repair import get_repair_plan
from app.style.repair import build_repair_plan


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


def _profile() -> dict:
    return {
        "version": 1,
        "unit_chars": 300,
        "feat": {
            "句长均值": {
                "mean": 24.0, "std": 6.0,
                "p10": 18.0, "p50": 24.0, "p90": 36.0,
                "min": 10.0, "max": 50.0,
            },
            "句号密度": {
                "mean": 14.0, "std": 3.0,
                "p10": 8.0, "p50": 14.0, "p90": 20.0,
                "min": 4.0, "max": 30.0,
            },
        },
    }


def test_metric_deviation_becomes_repair_instruction():
    # 40 个 8 字短句：句长远低于 p10，句号密度远高于 p90。
    text = "他推开门走出去。" * 40
    plan = build_repair_plan(
        text,
        _profile(),
        {"forbidden": {"禁用转折词": ["突然", "就在这时"]}},
    )

    assert plan["ok"] is True
    features = {issue.get("feature") for issue in plan["issues"]}
    assert "句长均值" in features
    assert "句号密度" in features
    assert "定向返修" in plan["instruction"]
    assert "句长均值" in plan["instruction"]
    assert "句号密度" in plan["instruction"]


def test_forbidden_hits_work_without_profile():
    text = "他推开门。突然，他停住了。就在这时，灯灭了。"
    plan = build_repair_plan(
        text,
        None,
        {"forbidden": {"禁用转折词": ["突然", "就在这时"]}},
    )

    assert plan["ok"] is True
    forbidden = [i for i in plan["issues"] if i["kind"] == "forbidden"]
    assert forbidden
    assert "禁用转折词" in plan["instruction"]
    assert "突然" in plan["instruction"]
    assert "就在这时" in plan["instruction"]


def test_instruction_avoids_local_scope_trigger_words():
    # 反向禁忌维度原始名里带「段尾」，必须被换成不会触发局部改写的说法。
    plan = build_repair_plan(
        "他知道，事情已经结束了。",
        None,
        {"forbidden": {"禁用段尾拔高": ["他知道"]}},
    )

    instruction = plan["instruction"]
    assert "禁用拔高式收束" in instruction
    for trigger in ("段尾", "结尾", "局部", "保留", "只", "仅", "第一句"):
        assert trigger not in instruction


def test_clean_text_has_no_issues():
    plan = build_repair_plan(
        "他推开门，屋里的灯还亮着。",
        None,
        {"forbidden": {"禁用转折词": ["突然"]}},
    )

    assert plan["ok"] is False
    assert plan["reason"] == "clean"
    assert plan["instruction"] == ""


def test_no_archive_reason():
    plan = build_repair_plan("他推开门。", None, None)

    assert plan["ok"] is False
    assert plan["reason"] == "no_archive"
    assert plan["archive"] == {"has_profile": False, "has_rules": False}


def test_endpoint_returns_plan(db):
    project = Project(
        title="定向返修测试",
        style_profile=_profile(),
        style_narrative={"forbidden": {"禁用转折词": ["突然"]}},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add(
        Generation(
            project_id=project.id,
            segment_no=1,
            chapter_range="第1章",
            text="突然，他推开门走出去。" * 40,
        )
    )
    db.commit()

    out = get_repair_plan(project.id, 1, db)

    assert out["project_id"] == project.id
    assert out["segment_no"] == 1
    assert out["ok"] is True
    assert out["issues"]
    assert out["instruction"].startswith("这是定向返修")


def test_endpoint_missing_segment_is_404(db):
    project = Project(title="没有这一段的项目")
    db.add(project)
    db.commit()
    db.refresh(project)

    with pytest.raises(HTTPException) as exc:
        get_repair_plan(project.id, 9, db)
    assert exc.value.status_code == 404
