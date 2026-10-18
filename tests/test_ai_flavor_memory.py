"""AI 腔反馈闭环的持久化基座回归测试：`ai_flavor_memories` 表 + 读写层。

覆盖任务要求的四类断言：
1. 新表能创建、字段可读写（含 create_all 对已存在库的补建能力）；
2. `upsert_memory` 同 key 累加而非插新行；`examples` 不超过上限；
3. `load_memories` 能合并 global + project、去重、按预算裁剪；
4. 软停用（active=False）的记忆不出现在 load 结果里，且不被 upsert 复活。

第 5 类（方案库升级）覆盖：
5. `_PATTERN_TYPES` 与引擎 `AI_FLAVOR_PATTERNS` 的 32 个模式集合相等；
6. `match_memories` 项目行胜出 / include_global / 空入参；
7. `plan_solution` 三条来源路径（template / assistant_llm / fallback）与
   「绝不抛异常」；
8. `resolve_tuning` 的 planned → reused → flagged → replanned 完整循环、
   llm 只在冷门模式被调用、失败整体回滚返空；
9. `note_recurrence` 打标；`load_memories(exclude_flagged=True)` 过滤；
10. 渲染优先 solution、无 solution 回落 summary；
11. 老库（只有旧列）经 ensure_schema 升级后新列可读写。

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


# ====================================================================
# 5. 方案库：模板覆盖 / 匹配 / 规划 / 复用与有效性反馈
# ====================================================================


class _ScriptedLLM:
    """按脚本依次返回的假 llm；同时记录调用次数与每次收到的 (system, user)。

    约定与 app.llm.provider.LLMProvider.chat 一致：chat(system, user) -> str。
    """

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self._responses:
            return self._responses.pop(0)
        return ""


class _RaisingLLM:
    """一调就抛错的假 llm（模拟 LLM 渠道故障）。"""

    def chat(self, system: str, user: str) -> str:  # noqa: ARG002
        raise RuntimeError("llm 渠道故障")


def test_pattern_types_covers_all_engine_patterns():
    """模式映射必须与引擎 AI_FLAVOR_PATTERNS 集合相等（32/32）。

    这是「模板优先」路线的护栏：漏一个模式，那个模式就会次次走 LLM 或
    兜底，零成本路径名不副实。引擎加新模式时本测试红，逼着同步补模板。
    """
    from app.generation.engine import AI_FLAVOR_PATTERNS

    assert len(AI_FLAVOR_PATTERNS) == 32
    assert set(afm._PATTERN_TYPES) == set(AI_FLAVOR_PATTERNS)
    # 每个类别都要有文案模板，否则渲染时静默回落兜底
    assert set(afm._PATTERN_TYPES.values()) <= set(afm._CATEGORY_TEMPLATES)


# —— match_memories ——


def test_match_memories_project_row_wins_over_global(db_session):
    """同一 issue_key 项目行胜出全局行；item 带方案库六字段。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="全局版：仿佛过密", hits_delta=9,
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="项目版：本文禁用仿佛", hits_delta=1,
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="不禁", summary="项目版：不禁过密", hits_delta=1,
    )
    db_session.commit()

    res = afm.match_memories(db_session, proj.id, ["仿佛", "不禁", "没见过的"])
    assert [it["issue_key"] for it in res["matched"]] == ["仿佛", "不禁"]
    assert res["unmatched"] == ["没见过的"]
    by_key = {it["issue_key"]: it for it in res["matched"]}
    assert by_key["仿佛"]["summary"] == "项目版：本文禁用仿佛"
    assert by_key["仿佛"]["scope"] == "project"
    item = by_key["仿佛"]
    for field in (
        "solution", "solution_origin", "times_reused",
        "last_reused_at", "recurrences", "flagged_ineffective",
    ):
        assert field in item
    assert item["times_reused"] == 0
    assert item["recurrences"] == 0
    assert item["flagged_ineffective"] is False
    assert item["last_reused_at"] is None


def test_match_memories_include_global_false_only_project_rows(db_session):
    """include_global=False 时全局行不参与匹配，直接进 unmatched。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="全局版",
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="不禁", summary="项目版",
    )
    db_session.commit()

    res = afm.match_memories(
        db_session, proj.id, ["仿佛", "不禁"], include_global=False
    )
    assert [it["issue_key"] for it in res["matched"]] == ["不禁"]
    assert res["unmatched"] == ["仿佛"]


def test_match_memories_without_project_id_only_global(db_session):
    """project_id=None（无项目上下文）只匹配全局行，不串读项目行。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="全局版",
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="不禁", summary="项目版",
    )
    db_session.commit()

    res = afm.match_memories(db_session, None, ["仿佛", "不禁"])
    assert [it["issue_key"] for it in res["matched"]] == ["仿佛"]
    assert res["unmatched"] == ["不禁"]


def test_match_memories_empty_keys_returns_empty(db_session):
    """空 issue_keys（含 None / 全空白）返回空结果，不查库。"""
    proj = _make_project(db_session)
    assert afm.match_memories(db_session, proj.id, []) == {
        "matched": [], "unmatched": []
    }
    assert afm.match_memories(db_session, proj.id, None) == {
        "matched": [], "unmatched": []
    }
    assert afm.match_memories(db_session, proj.id, ["  "]) == {
        "matched": [], "unmatched": []
    }


# —— plan_solution ——


def test_plan_solution_known_pattern_uses_template():
    """已知模式 → 类别模板；文案含模式名与命中次数，origin=template。"""
    text, origin = afm.plan_solution("仿佛", count=3)
    assert origin == "template"
    assert "仿佛" in text
    assert "3" in text


def test_plan_solution_unknown_pattern_with_llm():
    """冷门模式 + 假 llm → origin=assistant_llm，文案来自 llm（strip 后）。"""
    llm = _ScriptedLLM(["冷门模式的方案：把描写写具体。"])
    text, origin = afm.plan_solution(
        "冷门套路",
        count=2,
        examples=[{"segment_no": 1, "offset": 0, "snippet": "某种冷门写法"}],
        llm=llm,
    )
    assert origin == "assistant_llm"
    assert text == "冷门模式的方案：把描写写具体。"
    assert len(llm.calls) == 1
    _system, user = llm.calls[0]
    # user 提示要带模式名、命中次数、例句 snippet
    assert "冷门套路" in user
    assert "2" in user
    assert "某种冷门写法" in user


def test_plan_solution_llm_raises_falls_back_without_raising():
    """llm 调用抛错 → 兜底文案，本函数绝不抛异常。"""
    text, origin = afm.plan_solution("冷门套路", count=1, llm=_RaisingLLM())
    assert origin == "fallback"
    assert "冷门套路" in text
    assert "1" in text


def test_plan_solution_llm_none_unknown_pattern_fallback():
    """llm=None（零成本路径）+ 未知模式 → 兜底，不炸。"""
    text, origin = afm.plan_solution("冷门套路", count=1)
    assert origin == "fallback"
    assert "冷门套路" in text


def test_plan_solution_llm_empty_text_falls_back():
    """llm 返回空白 → 视为没有方案，走兜底。"""
    llm = _ScriptedLLM(["   "])
    text, origin = afm.plan_solution("冷门套路", count=1, llm=llm)
    assert origin == "fallback"
    assert "冷门套路" in text


class _UnavailableLLMHandle:
    """模拟闭环的 _LazyLLMHandle：provider 取不到时 getattr 抛 RuntimeError。

    真实句柄（app.routers.assistant_generation._LazyLLMHandle）刻意抛
    RuntimeError 而非 AttributeError——「LLM 不可用」的事实不该被
    `getattr(x, "chat", None)` 的默认值静默吞掉。本假件钉死同一行为。
    """

    def __getattr__(self, name: str):
        raise RuntimeError(f"Assistant provider 不可用，无法代理 {name!r}")


def test_plan_solution_unavailable_llm_handle_falls_back():
    """llm 句柄在可用性探测阶段就抛（provider 不可用）→ 兜底，绝不外抛。"""
    text, origin = afm.plan_solution("冷门套路", count=1, llm=_UnavailableLLMHandle())
    assert origin == "fallback"
    assert "冷门套路" in text


# —— resolve_tuning ——


def test_resolve_tuning_first_seen_plans_new_row(db_session):
    """①首见：未命中 → 规划并 upsert 新行（scope=project），times_reused=1。"""
    proj = _make_project(db_session)
    result = afm.resolve_tuning(
        db_session,
        proj.id,
        {
            "仿佛": {
                "count": 2,
                "examples": [{"segment_no": 1, "offset": 0, "snippet": "仿佛定了格"}],
            }
        },
    )
    assert [len(result["reused"]), len(result["replanned"]), len(result["planned"])] == [0, 0, 1]
    item = result["planned"][0]
    assert item["issue_key"] == "仿佛"
    assert item["solution_origin"] == "template"
    assert "仿佛" in item["solution"] and "2" in item["solution"]
    assert item["times_reused"] == 1
    assert item["recurrences"] == 0
    assert item["flagged_ineffective"] is False
    row = db_session.query(AIFlavorMemory).one()
    assert row.scope == "project" and row.project_id == proj.id
    # 问题账本口径与旧闭环逐字节一致：hit_count 记「多少次检查命中过它」
    # （每轮 +1，不记本段密度），密度写进 severity 与 summary
    assert row.hit_count == 1
    assert row.severity == "mid"  # 本段命中 2 次 → mid 档
    assert row.last_segment_no == 1
    assert "2" in row.summary


def test_resolve_tuning_second_hit_reuses_unchanged(db_session):
    """②二次同模式：solution 一字不改，times_reused=2、recurrences=1。"""
    proj = _make_project(db_session)
    evidence = {
        "仿佛": {
            "count": 1,
            "examples": [{"segment_no": 1, "offset": 0, "snippet": "仿佛定了格"}],
        }
    }
    r1 = afm.resolve_tuning(db_session, proj.id, evidence)
    first_solution = r1["planned"][0]["solution"]

    r2 = afm.resolve_tuning(db_session, proj.id, evidence)
    assert [len(r2["reused"]), len(r2["replanned"]), len(r2["planned"])] == [1, 0, 0]
    item = r2["reused"][0]
    assert item["solution"] == first_solution  # 一字不改
    assert item["times_reused"] == 2
    assert item["recurrences"] == 1
    assert item["last_reused_at"] is not None


def test_resolve_tuning_fills_solution_on_old_row(db_session):
    """命中但没方案的老行 → 规划并补上，不插新行、不擦掉老总结。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="机械比喻：仿佛过密", hits_delta=3,
    )
    db_session.commit()

    result = afm.resolve_tuning(db_session, proj.id, {"仿佛": {"count": 1}})
    assert len(result["planned"]) == 1
    item = result["planned"][0]
    assert item["solution_origin"] == "template"
    assert "仿佛" in item["solution"]
    assert item["times_reused"] == 1
    assert db_session.query(AIFlavorMemory).count() == 1
    row = db_session.query(AIFlavorMemory).one()
    # 老总结被本段密度刷新（与旧闭环每轮 upsert 同款：summary 记最新一轮）
    assert "1" in row.summary
    # 命中分支也记问题账本：改造前闭环每轮 upsert，改造后由 resolve_tuning
    # 的 _refresh_ledger 扛同款口径（hit_count +1、severity 定档）
    assert row.hit_count == 4
    assert row.severity == "low"  # 本段命中 1 次 → low 档


def test_resolve_tuning_preserves_ledger_semantics(db_session):
    """账本口径与旧闭环逐字节一致：每轮 hit_count +1、severity 按本段密度
    定档、last_segment_no 刷新、summary 写密度。

    resolve_tuning 是改造后闭环唯一的记账写入路径（旧闭环逐模式 upsert 的
    接班者）——口径漂移会悄悄废掉 load_memories 的优先级/最近性排序，故
    用本测试钉死。旧闭环测试（test_assistant_ai_flavor_feedback.py 等）从
    端点侧锁同一件事。
    """
    proj = _make_project(db_session)
    evidence = {
        "仿佛": {
            "count": 9,  # >= _SEVERITY_HIGH_HITS(4) → high
            "examples": [{"segment_no": 3, "offset": 0, "snippet": "仿佛"}],
        }
    }
    afm.resolve_tuning(db_session, proj.id, evidence)
    row = db_session.query(AIFlavorMemory).one()
    assert row.hit_count == 1  # 每轮 +1，密度不并进 hit_count
    assert row.severity == "high"
    assert row.last_segment_no == 3
    assert "9" in row.summary

    # 第二轮同模式：hit_count 累加为 2、段号刷新、severity 重定档
    evidence["仿佛"]["examples"] = [{"segment_no": 5, "offset": 0, "snippet": "仿佛"}]
    r2 = afm.resolve_tuning(db_session, proj.id, evidence)
    assert len(r2["reused"]) == 1
    row = db_session.query(AIFlavorMemory).one()
    assert row.hit_count == 2
    assert row.last_segment_no == 5
    assert row.severity == "high"


def test_resolve_tuning_reuses_global_solution(db_session):
    """全局行的方案可被本项目复用（跨项目复用），计数落在全局行上。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="全局教训",
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    row.solution = "全局方案：写具体感官细节"
    row.solution_origin = "template"
    row.times_reused = 1
    db_session.commit()

    result = afm.resolve_tuning(db_session, proj.id, {"仿佛": {"count": 1}})
    assert len(result["reused"]) == 1
    item = result["reused"][0]
    assert item["scope"] == "global"
    assert item["solution"] == "全局方案：写具体感官细节"
    assert item["times_reused"] == 2
    assert item["recurrences"] == 1


def test_resolve_tuning_loop_flags_then_replans(db_session):
    """完整循环：planned → reused×2 → 复发达阈值打标 → 同一轮重新规划重置。

    冷门模式 + 剧本 llm：两次规划各返回一版方案，用于断言「solution 被换」。
    """
    proj = _make_project(db_session)
    llm = _ScriptedLLM(["第一版方案：删掉它。", "第二版方案：换成角色专属反应。"])
    evidence = {
        "冷门套路": {
            "count": 1,
            "examples": [{"segment_no": 1, "offset": 0, "snippet": "某种冷门写法"}],
        }
    }

    r1 = afm.resolve_tuning(db_session, proj.id, evidence, llm=llm)
    assert [len(r1["planned"]), len(r1["reused"]), len(r1["replanned"])] == [1, 0, 0]
    first_solution = r1["planned"][0]["solution"]
    assert first_solution == "第一版方案：删掉它。"

    r2 = afm.resolve_tuning(db_session, proj.id, evidence, llm=llm)
    assert len(r2["reused"]) == 1
    assert r2["reused"][0]["times_reused"] == 2
    assert r2["reused"][0]["recurrences"] == 1

    r3 = afm.resolve_tuning(db_session, proj.id, evidence, llm=llm)
    assert len(r3["reused"]) == 1
    assert r3["reused"][0]["recurrences"] == 2

    # 第 4 次：recurrences 达到 INEFFECTIVE_RECURRENCES → 打标 →
    # 分支决策紧随其后看到 flagged → 重新规划、计数重置
    r4 = afm.resolve_tuning(db_session, proj.id, evidence, llm=llm)
    assert len(r4["replanned"]) == 1
    item = r4["replanned"][0]
    assert item["solution"] == "第二版方案：换成角色专属反应。"
    assert item["solution"] != first_solution
    assert item["times_reused"] == 1
    assert item["recurrences"] == 0
    assert item["flagged_ineffective"] is False
    assert item["solution_origin"] == "assistant_llm"
    # 只有冷门模式的两次规划调了 LLM（复用轮零调用）
    assert len(llm.calls) == 2


def test_resolve_tuning_llm_only_called_for_cold_patterns(db_session):
    """⑤llm 只在冷门模式被调用：已知模式走模板，计数 llm 断言调用次数。"""
    proj = _make_project(db_session)
    llm = _ScriptedLLM(["冷门方案的文案。"])
    evidence = {
        "仿佛": {
            "count": 2,
            "examples": [{"segment_no": 1, "offset": 0, "snippet": "仿佛定了格"}],
        },
        "冷门套路": {
            "count": 1,
            "examples": [{"segment_no": 2, "offset": 5, "snippet": "某种冷门写法"}],
        },
    }

    r1 = afm.resolve_tuning(db_session, proj.id, evidence, llm=llm)
    assert len(llm.calls) == 1  # 只有「冷门套路」调了 LLM
    _system, user = llm.calls[0]
    assert "冷门套路" in user and "某种冷门写法" in user
    by_key = {it["issue_key"]: it for it in r1["planned"]}
    assert by_key["仿佛"]["solution_origin"] == "template"
    assert by_key["冷门套路"]["solution_origin"] == "assistant_llm"

    # 第二轮全部复用：LLM 零新增调用
    r2 = afm.resolve_tuning(db_session, proj.id, evidence, llm=llm)
    assert len(llm.calls) == 1
    assert len(r2["reused"]) == 2


def test_resolve_tuning_failure_returns_empty_without_raising(db_session):
    """整体不得抛异常：project_id=None 且未命中 → upsert 必败 → 回滚返空。"""
    proj = _make_project(db_session)
    result = afm.resolve_tuning(db_session, None, {"仿佛": {"count": 1}})
    assert result == {"reused": [], "replanned": [], "planned": []}
    assert db_session.query(AIFlavorMemory).count() == 0


# —— note_recurrence / exclude_flagged / 渲染 / 迁移 ——


def test_note_recurrence_flags_after_threshold(db_session):
    """note_recurrence 累到阈值即打标；历史行不删，只是标记。"""
    proj = _make_project(db_session)
    r1 = afm.resolve_tuning(db_session, proj.id, {"仿佛": {"count": 1}})
    assert r1["planned"][0]["times_reused"] == 1

    for _ in range(afm.INEFFECTIVE_RECURRENCES):
        afm.note_recurrence(db_session, proj.id, "仿佛")

    row = db_session.query(AIFlavorMemory).one()
    assert row.recurrences == afm.INEFFECTIVE_RECURRENCES
    assert row.flagged_ineffective is True
    assert db_session.query(AIFlavorMemory).count() == 1  # 不是删除


def test_resolve_tuning_replans_flagged_row(db_session):
    """④flagged 行再命中 → replanned：solution 被换、计数重置、标记清除。"""
    proj = _make_project(db_session)
    evidence = {
        "冷门套路": {
            "count": 1,
            "examples": [{"segment_no": 1, "offset": 0, "snippet": "某种冷门写法"}],
        }
    }
    r1 = afm.resolve_tuning(
        db_session, proj.id, evidence, llm=_ScriptedLLM(["第一版方案：删掉重写。"])
    )
    assert r1["planned"][0]["solution"] == "第一版方案：删掉重写。"

    for _ in range(afm.INEFFECTIVE_RECURRENCES):
        afm.note_recurrence(db_session, proj.id, "冷门套路")
    row = db_session.query(AIFlavorMemory).one()
    assert row.flagged_ineffective is True

    r2 = afm.resolve_tuning(
        db_session, proj.id, evidence, llm=_ScriptedLLM(["第二版方案：换个写法。"])
    )
    assert len(r2["replanned"]) == 1
    item = r2["replanned"][0]
    assert item["solution"] == "第二版方案：换个写法。"
    assert item["solution_origin"] == "assistant_llm"
    assert item["times_reused"] == 1
    assert item["recurrences"] == 0
    assert item["flagged_ineffective"] is False


def test_load_memories_exclude_flagged_filters(db_session):
    """exclude_flagged=True 把「方案可能无效」的行滤出渲染结果（不删行）。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="机械比喻",
    )
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="不禁", summary="禁字夸张",
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).filter_by(issue_key="仿佛").one()
    row.flagged_ineffective = True
    db_session.commit()

    assert {it["issue_key"] for it in afm.load_memories(db_session, proj.id)} == {
        "仿佛", "不禁"
    }
    kept = afm.load_memories(db_session, proj.id, exclude_flagged=True)
    assert [it["issue_key"] for it in kept] == ["不禁"]
    # 历史行仍在表里（标记不是删除）
    assert db_session.query(AIFlavorMemory).count() == 2


def test_render_prefers_solution_over_summary(db_session):
    """有 solution 时渲染 solution（可执行的约束），不渲染 summary。"""
    proj = _make_project(db_session)
    afm.upsert_memory(
        db_session, scope="project", project_id=proj.id,
        issue_key="仿佛", summary="机械比喻：仿佛过密", hits_delta=4,
    )
    db_session.commit()
    row = db_session.query(AIFlavorMemory).one()
    row.solution = "删掉空泛类比，改具体感官细节"
    db_session.commit()

    line = afm.render_memory_block(afm.load_memories(db_session, proj.id))
    assert "删掉空泛类比，改具体感官细节" in line
    assert "机械比喻：仿佛过密" not in line
    assert line.startswith("- 【mid】")
    assert "累计 4 次" in line


def test_render_falls_back_to_summary_without_solution(db_session):
    """无 solution 的老行渲染时回落到 summary，保持行形状。"""
    afm.upsert_memory(
        db_session, scope="global", project_id=None,
        issue_key="仿佛", summary="机械比喻：仿佛过密", hits_delta=2,
    )
    db_session.commit()

    line = afm.render_memory_block(afm.load_memories(db_session, None))
    assert "机械比喻：仿佛过密" in line
    assert "累计 2 次" in line
    assert line.startswith("- 【mid】")


def test_migration_adds_solution_columns_to_old_db(db_session):
    """老库（只有旧列）经 ensure_schema 升级后新列可读写。"""
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    # 造老库：只有升级前的列（模拟建表那次提交之后、本次升级之前的库）
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ai_flavor_memories"))
        conn.execute(
            text(
                "CREATE TABLE ai_flavor_memories ("
                " id INTEGER NOT NULL PRIMARY KEY,"
                " scope VARCHAR(16) NOT NULL DEFAULT 'global',"
                " project_id INTEGER,"
                " issue_key VARCHAR(128) NOT NULL,"
                " summary TEXT NOT NULL DEFAULT '',"
                " hit_count INTEGER NOT NULL DEFAULT 0,"
                " examples JSON,"
                " last_segment_no INTEGER,"
                " severity VARCHAR(16) NOT NULL DEFAULT 'mid',"
                " active BOOLEAN NOT NULL DEFAULT 1,"
                " created_at DATETIME,"
                " updated_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO ai_flavor_memories (scope, issue_key, summary, hit_count)"
                " VALUES ('global', '仿佛', '老库里的一行', 7)"
            )
        )
    old_cols = {
        c["name"] for c in sa_inspect(engine).get_columns("ai_flavor_memories")
    }
    assert "solution" not in old_cols

    ensure_schema(target_engine=engine)  # 服务启动 / 离线脚本同款升级路径

    new_cols = {
        c["name"] for c in sa_inspect(engine).get_columns("ai_flavor_memories")
    }
    for col in (
        "solution", "solution_origin", "times_reused",
        "last_reused_at", "recurrences", "flagged_ineffective",
    ):
        assert col in new_cols

    # 老行的新列有服务端默认值（不是 NULL）
    row = db_session.execute(
        text(
            "SELECT solution, solution_origin, times_reused, recurrences,"
            " flagged_ineffective FROM ai_flavor_memories WHERE issue_key='仿佛'"
        )
    ).one()
    assert row[0] == "" and row[1] == ""
    assert row[2] == 0 and row[3] == 0
    assert not row[4]

    # 新列走 ORM 可读写
    orm_row = db_session.query(AIFlavorMemory).one()
    assert orm_row.summary == "老库里的一行" and orm_row.hit_count == 7
    orm_row.solution = "升级后补上的方案"
    orm_row.solution_origin = "template"
    orm_row.times_reused = 1
    orm_row.recurrences = 2
    orm_row.flagged_ineffective = True
    db_session.commit()
    db_session.expire_all()

    got = db_session.query(AIFlavorMemory).one()
    assert got.solution == "升级后补上的方案"
    assert got.solution_origin == "template"
    assert got.times_reused == 1 and got.recurrences == 2
    assert got.flagged_ineffective is True
