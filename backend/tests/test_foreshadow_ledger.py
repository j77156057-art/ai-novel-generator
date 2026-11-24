"""伏笔台账：纯函数校验/渲染 + 路由 CRUD + 提示词注入。

跑法（backend 目录）：`python -m pytest tests/test_foreshadow_ledger.py -q`
只用内存 SQLite，不写生产库、不调模型。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.foreshadow_ledger import (
    import_from_bible,
    import_from_kg,
    kg_linkage,
    normalize_entry,
    render_open_block,
    truth_layer,
)
from app.generation.prompts import build_user_prompt
from app.models import KgNode, Project
from app.routers.foreshadow_ledger import (
    ForeshadowCreate,
    ForeshadowPatch,
    create_foreshadow,
    delete_foreshadow,
    import_foreshadows,
    import_foreshadows_from_kg,
    list_foreshadows,
    update_foreshadow,
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


def _seed_project(db, bible: dict | None = None) -> Project:
    project = Project(title="伏笔台账测试", plot_bible=bible or {})
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


# ── normalize_entry ───────────────────────────────────────────────────


def test_normalize_rejects_empty_title_and_bad_status():
    with pytest.raises(ValueError):
        normalize_entry("   ")
    with pytest.raises(ValueError):
        normalize_entry("铜镜裂纹", status="unknown")


def test_normalize_rejects_bad_chapter_and_overlong_text():
    with pytest.raises(ValueError):
        normalize_entry("铜镜裂纹", planted_chapter=0)
    with pytest.raises(ValueError):
        normalize_entry("铜镜裂纹", planned_chapter=10000)
    with pytest.raises(ValueError):
        normalize_entry("铜镜裂纹", evidence="字" * 501)


def test_normalize_trims_and_keeps_none_chapters():
    payload = normalize_entry(
        "  铜镜裂纹 ",
        planted_chapter=" 3 ",
        planned_chapter=None,
        evidence=" 第三章结尾 ",
    )
    assert payload["title"] == "铜镜裂纹"
    assert payload["planted_chapter"] == 3
    assert payload["planned_chapter"] is None
    assert payload["evidence"] == "第三章结尾"


# ── render_open_block ─────────────────────────────────────────────────


def test_render_open_block_only_open_and_includes_planned_chapter():
    text = render_open_block([
        {"title": "铜镜裂纹", "status": "open", "planned_chapter": 5},
        {"title": "师父之死", "status": "open", "planned_chapter": None},
        {"title": "旧账本", "status": "resolved", "planned_chapter": 2},
        {"title": "失踪的船", "status": "dropped", "planned_chapter": 9},
    ])
    assert "铜镜裂纹" in text and "计划第5章回收" in text
    assert "师父之死" in text
    assert "旧账本" not in text and "失踪的船" not in text
    assert render_open_block([]) == ""


def test_main_tier_hides_title_and_truth_until_reveal():
    """主线伏笔：平时只给暗示点；到达计划揭晓章才允许给真相。"""
    entry = {
        "title": "主角其实是未来的自己",
        "status": "open",
        "tier": "main",
        "planned_chapter": 80,
        "truth": "系统是未来主角造的",
        "hints": "凌晨三点十七分的座机\n口袋里多出的便签",
    }
    early = render_open_block([entry], chapter=10)
    assert "凌晨三点十七分的座机" in early
    assert "主角其实是未来的自己" not in early
    assert "系统是未来主角造的" not in early

    late = render_open_block([entry], chapter=80)
    assert "系统是未来主角造的" in late
    assert "第 1/3 层" in late

    second = render_open_block([entry], chapter=81)
    assert "第 2/3 层" in second
    third = render_open_block([entry], chapter=82)
    assert "第 3/3 层" in third
    after = render_open_block([entry], chapter=83)
    assert "已揭晓完毕" in after


def test_main_tier_without_hints_still_hides_title():
    text = render_open_block([
        {"title": "终极真相", "status": "open", "tier": "main"},
    ])
    assert "终极真相" not in text
    assert "全书主线暗线" in text


def test_truth_layer_only_exposes_current_reversal():
    truth = (
        "背景：外婆是守楼人。第一层反转：陈经理只是执行者。"
        "第二层反转：外婆才是布局者。第三层反转：陈经理是共犯。"
    )
    first = truth_layer(truth, 1)
    assert "第一层反转" in first and "第二层反转" not in first
    second = truth_layer(truth, 2)
    assert "第二层反转" in second and "第三层反转" not in second
    assert "背景" in second
    assert truth_layer("没有分层", 1) == "没有分层"


def test_reveal_span_spaces_layers_instead_of_consecutive():
    """跨度 10 章时，三层分别从第 1、5、8 章开始，中间是余波章。"""
    entry = {
        "title": "主线",
        "status": "open",
        "tier": "main",
        "planned_chapter": 90,
        "reveal_span": 10,
        "truth": "背景。第一层反转：A。第二层反转：B。第三层反转：C。",
        "hints": "暗示一",
    }
    assert "第 1/3 层" in render_open_block([entry], chapter=90)
    assert "进行中" in render_open_block([entry], chapter=93)
    assert "第 2/3 层" in render_open_block([entry], chapter=94)
    assert "第 3/3 层" in render_open_block([entry], chapter=97)
    assert "已揭晓完毕" in render_open_block([entry], chapter=100)


def test_reveal_ratio_uses_total_chapters():
    """百分比窗口：120 章 × 85% 起、15% 跨度 → 102 起，三层在 102/108/114。"""
    entry = {
        "title": "主线",
        "status": "open",
        "tier": "main",
        "total_chapters": 120,
        "reveal_ratio": 0.85,
        "reveal_span_ratio": 0.15,
        "truth": "背景。第一层反转：A。第二层反转：B。第三层反转：C。",
        "hints": "暗示一",
    }
    assert "第 1/3 层" in render_open_block([entry], chapter=102)
    assert "进行中" in render_open_block([entry], chapter=105)
    assert "第 2/3 层" in render_open_block([entry], chapter=108)
    assert "第 3/3 层" in render_open_block([entry], chapter=114)
    assert "已揭晓完毕" in render_open_block([entry], chapter=120)


# ── 路由 CRUD ─────────────────────────────────────────────────────────


def test_router_crud_summary_and_legacy(db):
    project = _seed_project(
        db, {"foreshadowing": ["旧伏笔甲", {"name": "旧伏笔乙", "status": "planted"}]}
    )

    created = create_foreshadow(
        project.id,
        ForeshadowCreate(title="铜镜裂纹", planted_chapter=1, planned_chapter=5),
        db,
    )
    assert created["status"] == "open" and created["planned_chapter"] == 5

    listed = list_foreshadows(project.id, db)
    assert listed["summary"]["open"] == 1
    assert listed["legacy"] == ["旧伏笔甲", "旧伏笔乙"]

    updated = update_foreshadow(
        project.id,
        created["id"],
        ForeshadowPatch(status="resolved", resolved_chapter=7),
        db,
    )
    assert updated["status"] == "resolved" and updated["resolved_chapter"] == 7
    assert list_foreshadows(project.id, db)["summary"]["resolved"] == 1

    cleared = update_foreshadow(
        project.id, created["id"], ForeshadowPatch(planned_chapter=None), db,
    )
    assert cleared["planned_chapter"] is None  # 显式 null = 清空

    delete_foreshadow(project.id, created["id"], db)
    assert list_foreshadows(project.id, db)["entries"] == []


def test_router_rejects_cross_project_entry(db):
    project = _seed_project(db)
    other = _seed_project(db)
    created = create_foreshadow(project.id, ForeshadowCreate(title="铜镜裂纹"), db)

    with pytest.raises(HTTPException) as missing:
        update_foreshadow(other.id, created["id"], ForeshadowPatch(title="改名"), db)
    assert missing.value.status_code == 404


def test_router_persists_tier_truth_and_hints(db):
    project = _seed_project(db)
    created = create_foreshadow(
        project.id,
        ForeshadowCreate(
            title="未来的自己",
            tier="main",
            planned_chapter=80,
            truth="系统是未来主角造的",
            hints="凌晨三点十七分\n口袋里的便签",
        ),
        db,
    )
    assert created["tier"] == "main"
    assert created["truth"] == "系统是未来主角造的"
    assert created["hints"].startswith("凌晨三点十七分")

    updated = update_foreshadow(
        project.id, created["id"], ForeshadowPatch(tier="arc", truth=""), db,
    )
    assert updated["tier"] == "arc"
    assert updated["truth"] == ""


def test_normalize_rejects_unknown_tier():
    with pytest.raises(ValueError):
        normalize_entry("铜镜裂纹", tier="global")


def test_import_from_bible_dedupes_and_maps_status(db):
    project = _seed_project(
        db,
        {
            "foreshadowing": [
                "旧伏笔甲",
                {"name": "旧伏笔乙", "status": "paid_off"},
                "旧伏笔甲",
            ]
        },
    )
    first = import_from_bible(db, project)
    db.commit()
    assert first["created"] == 2

    second = import_from_bible(db, project)
    db.commit()
    assert second["created"] == 0

    listed = list_foreshadows(project.id, db)
    assert listed["legacy"] == []
    statuses = {entry["title"]: entry["status"] for entry in listed["entries"]}
    assert statuses == {"旧伏笔甲": "open", "旧伏笔乙": "resolved"}


def test_import_endpoint_returns_entries(db):
    project = _seed_project(db, {"foreshadowing": ["旧伏笔甲"]})
    result = import_foreshadows(project.id, db)
    assert result["created"] == 1
    assert result["entries"][0]["title"] == "旧伏笔甲"
    assert result["summary"]["open"] == 1


# ── KG 联动 ───────────────────────────────────────────────────────────


def _seed_kg(db, project: Project, holes: list[tuple[str, str, int | None]]) -> None:
    """写生成后图谱的坑，并额外写一条参考语料分图的坑（必须被隔离）。"""
    for question, status, chapter in holes:
        db.add(
            KgNode(
                project_id=project.id,
                type="plothole",
                props={"question": question, "status": status},
                first_chapter=chapter,
                source="generated",
            )
        )
    db.add(
        KgNode(
            project_id=project.id,
            type="plothole",
            props={"question": "参考语料里的坑", "status": "open"},
            first_chapter=1,
            source="novel:9",
        )
    )
    db.commit()


def test_kg_linkage_skips_dialogue_questions(db):
    """从正文对话里误抽的问句不能进台账候选（「你是谁？」不是伏笔）。"""
    project = _seed_project(db)
    _seed_kg(
        db,
        project,
        [
            ("铜镜为什么裂了？", "open", 3),
            ("“你是谁？", "open", 4),
            ("顾青梧问：“谁触发的？", "open", 5),
            ("你是谁？", "open", 6),
            ("还有呢？", "open", 7),
            ("那我们现在该怎么办？", "open", 8),
        ],
    )

    importable = list_foreshadows(project.id, db)["kg"]["importable"]
    assert [hole["question"] for hole in importable] == ["铜镜为什么裂了？"]

    created = import_from_kg(db, project.id, [])
    db.commit()
    assert created["created"] == 1
    assert created["titles"] == ["铜镜为什么裂了？"]


def test_kg_linkage_importable_only_generated_open_and_deduped(db):
    project = _seed_project(db)
    _seed_kg(
        db,
        project,
        [
            ("铜镜为什么裂了？", "open", 3),
            ("铜镜为什么裂了？", "open", 5),  # 同问题重复：按最近提出保留
            ("师父去了哪？", "resolved", 2),
        ],
    )
    created = create_foreshadow(project.id, ForeshadowCreate(title="旧账本"), db)

    listed = list_foreshadows(project.id, db)
    importable = listed["kg"]["importable"]
    assert [hole["question"] for hole in importable] == ["铜镜为什么裂了？"]
    assert importable[0]["first_chapter"] == 5  # 去重保留最近段号
    assert all("参考语料" not in hole["question"] for hole in importable)
    assert listed["kg"]["resolvable"] == []
    assert created["title"] == "旧账本"


def test_import_from_kg_creates_open_entries_and_dedupes(db):
    project = _seed_project(db)
    _seed_kg(db, project, [("铜镜为什么裂了？", "open", 3), ("师父去了哪？", "resolved", 2)])

    first = import_from_kg(db, project.id, [])
    db.commit()
    assert first["created"] == 1

    second = import_from_kg(db, project.id, [])
    db.commit()
    assert second["created"] == 0

    entries = list_foreshadows(project.id, db)["entries"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["title"] == "铜镜为什么裂了？"
    assert entry["status"] == "open"
    assert entry["source"] == "kg"
    assert entry["planted_chapter"] is None  # 段号不冒充章号
    assert "第 3 段提出" in entry["evidence"]


def test_import_kg_endpoint_returns_list_payload(db):
    project = _seed_project(db)
    _seed_kg(db, project, [("铜镜为什么裂了？", "open", 3)])

    result = import_foreshadows_from_kg(project.id, db)
    assert result["created"] == 1
    assert result["summary"]["open"] == 1
    assert result["kg"]["importable"] == []


def test_kg_linkage_suggests_resolving_recovered_holes(db):
    project = _seed_project(db)
    _seed_kg(
        db,
        project,
        [
            ("铜镜为什么裂了？", "resolved", 3),
            ("师父去了哪？", "open", 4),
        ],
    )
    recovered = create_foreshadow(project.id, ForeshadowCreate(title="铜镜为什么裂了？"), db)
    still_open = create_foreshadow(project.id, ForeshadowCreate(title="师父去了哪？"), db)
    unrelated = create_foreshadow(project.id, ForeshadowCreate(title="旧账本"), db)

    suggestions = kg_linkage(db, project.id, [recovered, still_open, unrelated])
    assert [item["entry_id"] for item in suggestions["resolvable"]] == [
        recovered["id"]
    ]
    assert suggestions["resolvable"][0]["kg_status"] == "resolved"
    # 两个同名 open 坑都已进台账，所以没有可登记项。
    assert suggestions["importable"] == []


def test_kg_linkage_ignores_resolved_ledger_entries(db):
    project = _seed_project(db)
    _seed_kg(db, project, [("铜镜为什么裂了？", "dropped", 3)])
    entry = create_foreshadow(project.id, ForeshadowCreate(title="铜镜为什么裂了？"), db)
    update_foreshadow(project.id, entry["id"], ForeshadowPatch(status="resolved"), db)

    assert kg_linkage(db, project.id, [])["resolvable"] == []


# ── 提示词注入 ────────────────────────────────────────────────────────


def _user_prompt(**kwargs) -> str:
    return build_user_prompt(
        [], [], [], 2000,
        {"foreshadowing": ["旧字段伏笔"]},
        None,
        prior_blocks=["[近情·段 1] 前情"],
        **kwargs,
    )


def test_prompt_prefers_ledger_block_and_hides_legacy():
    text = _user_prompt(foreshadow_block="未回收伏笔：铜镜裂纹（计划第5章回收）")
    assert "铜镜裂纹" in text
    assert "旧字段伏笔" not in text


def test_prompt_falls_back_to_legacy_without_block():
    text = _user_prompt()
    assert "旧字段伏笔" in text
