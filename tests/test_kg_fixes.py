"""KG 三项缺陷复现测试（ADR：先红后绿再回归）。

三个缺陷（team-lead 派单，2026-10）：

缺陷 1（P1）生成闭环里"坑"状态永不 resolved：
  增量路径 merge_graph_from_text → merge_result_into_graph 对已存在的坑按
  question 直接 continue，本段文本里的解决线索永远改不动 status；而
  extractor.resolve_plotholes 的「末现章之后」条件在单段路径里永不成立
  （extract_from_text 把所有 chunk 的 chapter_no 设成同一个 segment_no）。

缺陷 2（P1）Fact 的 status 字符串字段永不更新：
  upsert_facts 对字符串字段的规则是「k not in props 才写」，坑 fact 建成时
  status=open 后永远改不成 resolved。

缺陷 3（P2）get_character 伪造连续出场列表：
  queries.get_character 用 list(range(first, last+1)) 编造 appearances，
  角色只在第 3、50 章出场也会返回 3..50 共 48 章（KgNode 只存 first/last）。

纪律：本文件先写、先跑出**红**（证明洞真实存在），修完再跑出绿。
复现测试全部真实调用被测函数，不 mock 任何被测逻辑。
"""

from __future__ import annotations

import os
import sys

# ─── bootstrap：与仓库其它测试一致，把 backend/ 加进 sys.path ────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.kg.build import merge_graph_from_text  # noqa: E402
from app.kg.ledger import (  # noqa: E402
    T_PLOTHOLE,
    facts_from_extraction,
    upsert_facts,
)
from app.kg.queries import get_character  # noqa: E402
from app.models import Fact, KgNode, Project  # noqa: E402


# ---------------------------------------------------------------------------
# 测试语料（已用 extract_from_text 实测确认形状）：
#   SEG1：单句同时含坑线索词「秘密」与问号 → 开出 key='秘密' 的坑；
#   SEG2：含「秘密」+ 强解决短语「揭晓」→ 按 key+解决线索语义应结算该坑，
#         且自身不产生新坑（_ANSWERED/_ASSERT_COMPLETE 闸门挡掉）。
# ---------------------------------------------------------------------------
SEG1_HOLE_TEXT = "林夜始终不明白，父亲守着的秘密是否与他有关？"
SEG2_RESOLVE_TEXT = "揭晓的答案并不复杂：那秘密是母亲留下的嫁妆单子。"


@pytest.fixture
def db():
    """独立临时库（conftest 已强制隔离到临时文件）+ 一个空项目。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    project = Project(title="kg-fixes-test")
    session.add(project)
    session.commit()
    yield session
    session.close()


def _plothole_nodes(session, project_id: int) -> list[KgNode]:
    return list(
        session.scalars(
            select(KgNode).where(
                KgNode.project_id == project_id,
                KgNode.type == "plothole",
                KgNode.source == "generated",
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# 缺陷 1：跨段结算（先红）
# ---------------------------------------------------------------------------


def test_cross_segment_resolves_open_plothole(db):
    """先 merge 一段开坑文本，再 merge 一段含「检测键 + 解决线索」的文本，
    坑 status 必须从 open 变 resolved。

    修复前：第二段 merge 对已存在坑直接 continue，status 恒为 open → 红。
    """
    pid = db.scalars(select(Project)).first().id

    # 段 1：开坑
    stats1 = merge_graph_from_text(db, pid, SEG1_HOLE_TEXT, source="generated", segment_no=1)
    assert stats1.get("new_plotholes", 0) >= 1, f"段 1 应开出至少一个坑，实际 {stats1}"
    holes = _plothole_nodes(db, pid)
    assert len(holes) == 1, f"段 1 后应有且仅有 1 个坑节点，实际 {len(holes)}"
    node = holes[0]
    assert (node.props or {}).get("status") == "open", "新坑初始应为 open"

    # 段 2：本段文本含该坑检测键（秘密）+ 强解决线索（揭晓）
    merge_graph_from_text(db, pid, SEG2_RESOLVE_TEXT, source="generated", segment_no=2)

    db.expire_all()
    holes = _plothole_nodes(db, pid)
    assert len(holes) == 1, f"段 2 不应新增坑（解决句被闸门挡掉），实际 {len(holes)}"
    node = holes[0]
    assert (node.props or {}).get("status") == "resolved", (
        f"段 2 含检测键+解决线索，坑应被结算为 resolved，实际 status="
        f"{(node.props or {}).get('status')!r}（缺陷 1：增量路径永不结算）"
    )


# ---------------------------------------------------------------------------
# 缺陷 2：Fact status 单调转换（先红）
# ---------------------------------------------------------------------------


def test_upsert_facts_status_monotonic_open_to_resolved(db):
    """upsert_facts 先写 open 坑 fact，再 upsert resolved，props.status 必须变 resolved；
    再 upsert open 也不得回退（单调只进不退）。

    修复前：字符串字段「k not in props 才写」→ status 恒为 open → 红。
    """
    pid = db.scalars(select(Project)).first().id

    open_fact = {
        "type": T_PLOTHOLE,
        "key": "父亲守着的秘密是否与他有关？",
        "props": {"cue": "秘密", "status": "open", "planted_segment": 1},
        "first_segment": 1,
        "last_segment": 1,
        "confidence": "high",
    }
    upsert_facts(db, pid, [open_fact], source="generated")

    cur = db.scalars(select(Fact).where(Fact.type == T_PLOTHOLE)).one()
    assert cur.props.get("status") == "open", "建成时应为 open"

    # 段 2 的结算信息：只带 status（不重复带 planted_segment，避免 int 累加漂移）
    resolved_fact = {
        "type": T_PLOTHOLE,
        "key": "父亲守着的秘密是否与他有关？",
        "props": {"status": "resolved"},
        "first_segment": 1,
        "last_segment": 2,
        "confidence": "high",
    }
    upsert_facts(db, pid, [resolved_fact], source="generated")

    db.expire_all()
    cur = db.scalars(select(Fact).where(Fact.type == T_PLOTHOLE)).one()
    assert cur.props.get("status") == "resolved", (
        f"upsert resolved 后 status 必须变 resolved，实际 {cur.props.get('status')!r}"
        "（缺陷 2：字符串字段永不更新）"
    )
    # planted_segment 不被 resolved patch 污染（仍为首现段 1）
    assert cur.props.get("planted_segment") == 1, (
        f"planted_segment 应保持 1，实际 {cur.props.get('planted_segment')!r}"
    )

    # 单调性：再 upsert open 也不得回退成 open
    upsert_facts(db, pid, [open_fact], source="generated")
    db.expire_all()
    cur = db.scalars(select(Fact).where(Fact.type == T_PLOTHOLE)).one()
    assert cur.props.get("status") == "resolved", (
        f"resolved 绝不回退成 open，实际 {cur.props.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# 缺陷 3：get_character 不再伪造 appearances（先红）
# ---------------------------------------------------------------------------


def test_get_character_does_not_fabricate_appearances(db):
    """角色只在第 3、50 章出场时，get_character 不得返回 3..50 的伪造连续列表。

    修复前：appearances = list(range(3, 51)) → 48 项伪造数据 → 红。
    """
    pid = db.scalars(select(Project)).first().id
    db.add(
        KgNode(
            project_id=pid,
            type="character",
            props={"name": "林夜", "mentions": 4, "speaking": 1},
            first_chapter=3,
            last_chapter=50,
            source="generated",
        )
    )
    db.commit()

    result = get_character(db, pid, "林夜")
    assert result is not None, "角色应可查到"
    assert "appearances" not in result, (
        f"get_character 不得返回伪造的 appearances 字段，实际 {result.get('appearances')!r}"
        "（缺陷 3：range(first,last+1) 把 2 章出场编成 48 章）"
    )
    # 真实形状保留：首末章如实返回
    assert result["first_chapter"] == 3 and result["last_chapter"] == 50


# ---------------------------------------------------------------------------
# 协同：同一次 merge 里 KgNode 与 Fact 两侧 status 都要更新（别只修一边）
# ---------------------------------------------------------------------------


def test_generation_loop_updates_graph_and_fact_together(db):
    """走真实生成闭环入口 _merge_segment_into_graph：段 2 的解决线索必须让
    KgNode.props.status 与 Fact.props.status **同时**变 resolved。

    派单明确要求「同一次 merge 里 KgNode 和 Fact 两边的 status 都要更新，
    别只修一边」——这个测试就是钉这句的。
    """
    from app.generation.engine import _merge_segment_into_graph

    pid = db.scalars(select(Project)).first().id

    # 段 1：开坑（图谱 + 账本各落一条 open）
    _merge_segment_into_graph(db, pid, SEG1_HOLE_TEXT, 1)
    node = _plothole_nodes(db, pid)[0]
    fact = db.scalars(select(Fact).where(Fact.type == T_PLOTHOLE)).one()
    assert (node.props or {}).get("status") == "open"
    assert fact.props.get("status") == "open"

    # 段 2：含检测键 + 解决线索
    stats = _merge_segment_into_graph(db, pid, SEG2_RESOLVE_TEXT, 2)
    assert stats.get("resolved_plotholes") == 1, f"应结算 1 个坑，实际 {stats}"

    db.expire_all()
    node = _plothole_nodes(db, pid)[0]
    fact = db.scalars(select(Fact).where(Fact.type == T_PLOTHOLE)).one()
    assert (node.props or {}).get("status") == "resolved", (
        f"KgNode 侧应 resolved，实际 {(node.props or {}).get('status')!r}"
    )
    assert fact.props.get("status") == "resolved", (
        f"Fact 侧应同步 resolved（只修一边就是缺陷），实际 {fact.props.get('status')!r}"
    )
    # 首现段不被污染
    assert fact.props.get("planted_segment") == 1, (
        f"planted_segment 应保持 1，实际 {fact.props.get('planted_segment')!r}"
    )


def test_generation_loop_keeps_unresolved_hole_open(db):
    """对照：段 2 不含检测键 + 解决线索时，坑必须保持 open（不能误结算）。"""
    from app.generation.engine import _merge_segment_into_graph

    pid = db.scalars(select(Project)).first().id
    _merge_segment_into_graph(db, pid, SEG1_HOLE_TEXT, 1)

    # 无关段落：既不含「秘密」也不含任何强解决线索
    stats = _merge_segment_into_graph(db, pid, "林夜沿着河堤走了一阵，夜色渐深。", 2)
    assert stats.get("resolved_plotholes") == 0, f"不应结算任何坑，实际 {stats}"

    db.expire_all()
    node = _plothole_nodes(db, pid)[0]
    fact = db.scalars(select(Fact).where(Fact.type == T_PLOTHOLE)).one()
    assert (node.props or {}).get("status") == "open", "无解决线索时必须保持 open"
    assert fact.props.get("status") == "open", "Fact 侧同样必须保持 open"
