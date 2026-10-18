"""AI 腔反馈闭环的持久化基座回归测试：`ai_flavor_memories` 表 + 读写层。

覆盖任务要求的四类断言：
1. 新表能创建、字段可读写（含 create_all 对已存在库的补建能力）；
2. `upsert_memory` 同 key 累加而非插新行；`examples` 不超过上限；
3. `load_memories` 能合并 global + project、去重、按预算裁剪；
4. 软停用（active=False）的记忆不出现在 load 结果里，且不被 upsert 复活。

库隔离由 tests/conftest.py 强制（临时文件库）；本文件的 autouse fixture
只做本文件关心的表重建，参照 test_regenerate_ai_flavor_alert.py 的用法。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.agents import ai_flavor_memory as afm  # noqa: E402
from app.db import Base, SessionLocal, engine, ensure_schema  # noqa: E402
from app.models import AIFlavorMemory, Project  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    """每个用例一张干净的表（跑在 conftest 强制的临时库上，不碰真实库）。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db_session():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_project(db, title: str = "AI 腔记忆测试项目") -> Project:
    proj = Project(title=title, target_length=5000, mode="quick", status="created")
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


# ====================================================================
# 1. 表结构与可创建性
# ====================================================================


def test_table_created_and_all_fields_roundtrip(db_session):
    """新表能建、全部字段可读写（examples 走 JSON 列）。"""
    proj = _make_project(db_session)
    row = AIFlavorMemory(
        scope="project",
        project_id=proj.id,
        issue_key="仿佛",
        summary="机械比喻：仿佛/宛如 使用过密",
        hit_count=3,
        examples=[{"segment_no": 2, "offset": 17, "snippet": "仿佛一只受惊的兽"}],
        last_segment_no=2,
        severity="high",
        active=True,
    )
    db_session.add(row)
    db_session.commit()

    got = db_session.query(AIFlavorMemory).one()
    assert got.scope == "project"
    assert got.project_id == proj.id
    assert got.issue_key == "仿佛"
    assert got.summary == "机械比喻：仿佛/宛如 使用过密"
    assert got.hit_count == 3
    assert got.examples == [
        {"segment_no": 2, "offset": 17, "snippet": "仿佛一只受惊的兽"}
    ]
    assert got.last_segment_no == 2
    assert got.severity == "high"
    assert got.active is True
    assert got.created_at is not None and got.updated_at is not None


def test_defaults_on_minimal_insert(db_session):
    """不显式给的字段有保守默认：active=True、hit_count=0、examples=[]。"""
    db_session.add(AIFlavorMemory(scope="global", issue_key="宛如"))
    db_session.commit()
    got = db_session.query(AIFlavorMemory).one()
    assert got.project_id is None
    assert got.hit_count == 0
    assert got.examples == []
    assert got.severity == "mid"
    assert got.active is True


def test_create_all_recreates_missing_table_on_existing_db(db_session):
    """老库升级路径：表整体缺失时 `create_all` 能补建（不需要迁移清单）。

    这正是 schema_baseline.json 之后**新建表**的升级语义——create_all
    只补缺失的表，列级补丁才需要 db.py 的 migrations 清单。
    """
    # 造一个「没有 ai_flavor_memories 表」的老库：先建全量，再只 drop 新表
    Base.metadata.create_all(bind=engine)
    AIFlavorMemory.__table__.drop(bind=engine)
    from sqlalchemy import inspect as _sa_inspect

    assert not _sa_inspect(engine).has_table("ai_flavor_memories")

    ensure_schema(target_engine=engine)  # 模拟服务启动/离线脚本的初始化

    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session,
        scope="project",
        project_id=proj.id,
        issue_key="不禁",
        summary="禁字夸张：不禁/忍不住 使用过密",
    )
    db_session.commit()
    assert db_session.query(AIFlavorMemory).count() == 1


# ====================================================================
# 2. upsert：同 key 累加，examples 有上限
# ====================================================================


def test_upsert_same_key_accumulates_without_duplicate_rows(db_session):
    """同一 (scope, project_id, issue_key) 只累加，不插新行。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session,
        scope="project",
        project_id=proj.id,
        issue_key="仿佛",
        summary="机械比喻：仿佛使用过密",
        hits_delta=2,
        segment_no=3,
    )
    afm.upsert_memory(
        db_session,
        scope="project",
        project_id=proj.id,
        issue_key="仿佛",
        summary="机械比喻：仿佛/宛如 使用过密",
        hits_delta=1,
        segment_no=7,
    )
    db_session.commit()

    rows = db_session.query(AIFlavorMemory).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.hit_count == 3
    assert row.last_segment_no == 7
    assert row.summary == "机械比喻：仿佛/宛如 使用过密"


def test_upsert_empty_summary_keeps_previous_description(db_session):
    """空 summary = 只报数，不把已有描述擦成空串。"""
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="宛如", summary="旧总结",
    )
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="宛如", summary="",
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    assert row.summary == "旧总结"
    assert row.hit_count == 2


def test_upsert_examples_capped_and_newest_kept(db_session):
    """examples 最多 MAX_EXAMPLES 条；超限时新的挤掉最旧的。"""
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛",
        examples=[
            {"segment_no": 1, "offset": 0, "snippet": "例1"},
            {"segment_no": 2, "offset": 0, "snippet": "例2"},
        ],
    )
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛",
        examples=[
            {"segment_no": 3, "offset": 0, "snippet": "例3"},
            {"segment_no": 4, "offset": 0, "snippet": "例4"},
            {"segment_no": 5, "offset": 0, "snippet": "例5"},
        ],
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    assert len(row.examples) == afm.MAX_EXAMPLES == 3
    # 最旧的例1 被挤掉，保留最新三条
    assert [e["snippet"] for e in row.examples] == ["例3", "例4", "例5"]


def test_upsert_same_evidence_position_not_duplicated(db_session):
    """同一处证据（segment_no+offset）重复命中只在证据列表出现一次。"""
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛",
        examples=[{"segment_no": 4, "offset": 12, "snippet": "仿佛"}],
    )
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛",
        examples=[{"segment_no": 4, "offset": 12, "snippet": "仿佛"}],
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    assert row.hit_count == 2
    assert len(row.examples) == 1


def test_upsert_snippet_truncated(db_session):
    """超长 snippet 截断到 MAX_SNIPPET_LEN，防整段正文撑库。"""
    long_text = "啊" * 5000
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="不禁",
        examples=[{"segment_no": 1, "offset": 0, "snippet": long_text}],
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    assert len(row.examples[0]["snippet"]) == afm.MAX_SNIPPET_LEN


def test_upsert_scope_and_project_are_separate_rows(db_session):
    """global 与 project、以及不同项目之间，同 issue_key 也是独立的行。"""
    p1 = _make_project(db_session, "项目一")
    p2 = _make_project(db_session, "项目二")
    afm.upsert_memory(db_session, scope="global", project_id=None, issue_key="仿佛")
    afm.upsert_memory(db_session, scope="project", project_id=p1.id, issue_key="仿佛")
    afm.upsert_memory(db_session, scope="project", project_id=p2.id, issue_key="仿佛")
    db_session.commit()
    assert db_session.query(AIFlavorMemory).count() == 3


def test_upsert_validation_errors(db_session):
    """非法输入尽早炸，而不是静默写坏去重键。"""
    proj = _make_project(db_session)
    with pytest.raises(ValueError):
        afm.upsert_memory(db_session, scope="galaxy", project_id=None, issue_key="x")
    with pytest.raises(ValueError):
        afm.upsert_memory(db_session, scope="project", project_id=None, issue_key="x")
    with pytest.raises(ValueError):
        afm.upsert_memory(db_session, scope="global", project_id=None, issue_key="  ")
    with pytest.raises(ValueError):
        afm.upsert_memory(
            db_session, scope="global", project_id=None,
            issue_key="x" * (afm.MAX_ISSUE_KEY_LEN + 1),
        )
    with pytest.raises(ValueError):
        afm.upsert_memory(
            db_session, scope="project", project_id=proj.id,
            issue_key="x", hits_delta=-1,
        )
    assert db_session.query(AIFlavorMemory).count() == 0


# ====================================================================
# 3. load：合并 global + project、去重、排序、裁剪
# ====================================================================


def test_load_merges_global_and_project_project_wins(db_session):
    """两个来源合并；同一 issue_key 项目行胜出，全局行被跳过不重复。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="全局版：仿佛过密", hits_delta=9,
    )
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="宛如", summary="全局版：宛如过密", hits_delta=1,
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="项目版：本文禁用仿佛", hits_delta=1,
    )
    db_session.commit()

    items = afm.load_memories(db_session, proj.id)
    by_key = {it["issue_key"]: it for it in items}
    assert set(by_key) == {"仿佛", "宛如"}
    # 项目行胜出：即使全局版命中次数更高，也不该复读同一 key
    assert by_key["仿佛"]["summary"] == "项目版：本文禁用仿佛"
    assert by_key["仿佛"]["scope"] == "project"


def test_load_orders_by_severity_then_hits_then_recency(db_session):
    """排序：severity 升序 → hit_count 降序 → last_segment_no 降序。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="low_hits", summary="低档少命中", severity="low", hits_delta=99,
        segment_no=99,
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="mid_few", summary="中档少命中", severity="mid", hits_delta=1,
        segment_no=1,
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="mid_many", summary="中档多命中", severity="mid", hits_delta=8,
        segment_no=2,
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="high_one", summary="高档一次也优先", severity="high", hits_delta=1,
        segment_no=3,
    )
    db_session.commit()

    items = afm.load_memories(db_session, proj.id)
    assert [it["issue_key"] for it in items] == [
        "high_one", "mid_many", "mid_few", "low_hits",
    ]


def test_load_trims_by_limit_and_char_budget(db_session):
    """条数上限 + 字符预算双重裁剪；预算按渲染后行长精确计算。"""
    proj = _make_project(db_session)
    for i in range(10):
        afm.upsert_memory(
            db_session, scope="project", project_id=proj.id,
            issue_key=f"key{i:02d}", summary=f"总结{i:02d}", hits_delta=10 - i,
        )
    db_session.commit()

    by_limit = afm.load_memories(db_session, proj.id, limit=3)
    assert len(by_limit) == 3

    # 每条渲染行长度可算：先取全量，再逐条累计找预算截断点
    full = afm.load_memories(db_session, proj.id, limit=50, char_budget=10**9)
    assert len(full) == 10
    block = afm.render_memory_block(full)
    first_line_len = len(block.splitlines()[0])
    # 预算只够 2.5 条：应收 2 条，且渲染总长不超预算（首条必收除外）
    budget = first_line_len * 2 + first_line_len // 2
    trimmed = afm.load_memories(db_session, proj.id, limit=50, char_budget=budget)
    assert len(trimmed) == 2
    assert len(afm.render_memory_block(trimmed)) <= budget


def test_load_without_project_id_returns_global_only(db_session):
    """project_id=None（无项目上下文）只取 global，不串读项目行。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="全局教训",
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="不禁", summary="项目教训",
    )
    db_session.commit()

    items = afm.load_memories(db_session, None)
    assert [it["issue_key"] for it in items] == ["仿佛"]


def test_load_empty_returns_empty_list(db_session):
    """无记忆时返回空列表（渲染块为空串，注入链路等价原行为）。"""
    proj = _make_project(db_session)
    assert afm.load_memories(db_session, proj.id) == []
    assert afm.render_memory_block([]) == ""


def test_load_validation_errors(db_session):
    """limit / char_budget 非法输入尽早炸。"""
    with pytest.raises(ValueError):
        afm.load_memories(db_session, 1, limit=0)
    with pytest.raises(ValueError):
        afm.load_memories(db_session, 1, char_budget=-1)


# ====================================================================
# 4. 软停用
# ====================================================================


def test_inactive_memory_excluded_from_load(db_session):
    """active=False 的记忆不出现在 load 结果里。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="机械比喻",
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    row.active = False
    db_session.commit()

    assert afm.load_memories(db_session, proj.id) == []
    # 历史仍在表里（软删除，不删历史）
    assert db_session.query(AIFlavorMemory).count() == 1


def test_upsert_does_not_resurrect_disabled_memory(db_session):
    """停用是人工决策；upsert 只累加计数，不悄悄复活。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="机械比喻",
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    row.active = False
    db_session.commit()

    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", hits_delta=5,
    )
    db_session.commit()

    rows = db_session.query(AIFlavorMemory).all()
    assert len(rows) == 1
    assert rows[0].active is False
    assert rows[0].hit_count == 6  # 历史计数连续
    assert afm.load_memories(db_session, proj.id) == []


def test_render_block_contains_summary_and_example(db_session):
    """渲染块含总结与最新例句，行数等于记忆条数（有界）。"""
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="机械比喻：仿佛过密",
        hits_delta=4,
        examples=[{"segment_no": 9, "offset": 3, "snippet": "仿佛定了格"}],
    )
    db_session.commit()
    items = afm.load_memories(db_session, None)
    block = afm.render_memory_block(items)
    assert block.count("\n") == 0 and len(block.splitlines()) == 1
    assert "机械比喻：仿佛过密" in block
    assert "累计 4 次" in block
    assert "仿佛定了格" in block
