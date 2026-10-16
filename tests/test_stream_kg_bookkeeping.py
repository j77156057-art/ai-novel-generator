"""流式续写的 KG 簿记单测（先红后绿）。

缺口：`generate_next_segment`（非流式）在 `_commit_segment` 之后调
`_merge_segment_into_graph`（图谱 + 账本 + 别名归一 + 哨兵四步），
但 `generate_next_segment_stream`（流式）在 `_commit_segment` 之后
**只做 _rollup_summary_tree 和 _style_warning**——整条流式路径不做任何 KG 归档。

前端主用流式 → 流式生成的段落不进图谱、不进事实账本、不过哨兵，
于是 reminders / 诊断 / 自检全都看不到流式产出的内容。

本文件 4 项：
  T1 流式生成后，generated 图谱与事实账本里出现该段抽出的事实（修复前空 → 红）
  T2 两条路径一致：流式与非流式的簿记结果形状一致（防"只改流式"——最要紧的护栏）
  T3 簿记抛错 → 流式仍正常产出 done 事件、正文照常落库，不吞 SSE
  T4 非流式行为不变（回归护栏）

测试真实调用 generate_next_segment / generate_next_segment_stream。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import (  # noqa: E402
    generate_next_segment,
    generate_next_segment_stream,
)
from app.models import Fact, Generation, KgNode, Project  # noqa: E402


# 两个角色 + 说话归属证据 → 稳定抽出 2 个角色节点 / 2 条 character fact
SEG_TEXT = "林夜推开木门。\n林夜说道：“我回来了。”\n白光抬起头。\n白光说道：“你终于来了。”"


class StubProvider:
    """流式/非流式两用的确定性假 provider。

    stream_chat 给流式路径、chat 给非流式路径，两者返回同一份正文——
    这样 T2 比较两条路径的簿记结果时，差异只可能来自链路本身，不来自语料。
    """

    def __init__(self, text: str = SEG_TEXT) -> None:
        self.text = text

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        return self.text

    def summarize(self, text: str) -> str:
        return "测试摘要"

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        yield self.text


@pytest.fixture(autouse=True)
def _clean_db():
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


def _make_project(db, title: str = "流式簿记测试") -> Project:
    """无参考语料的项目：绕过蒸馏预检（_require_distill_ready 对无语料直接放行）。"""
    proj = Project(
        title=title, target_length=5000, mode="quick", status="created",
        reference_novel_ids=[],
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _graph_nodes(db, project_id: int) -> list[KgNode]:
    return list(
        db.scalars(
            select(KgNode).where(
                KgNode.project_id == project_id, KgNode.source == "generated"
            )
        ).all()
    )


def _ledger_facts(db, project_id: int) -> list[Fact]:
    return list(
        db.scalars(
            select(Fact).where(
                Fact.project_id == project_id, Fact.source == "generated"
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# T1：流式生成后必须留下 KG 簿记
# ---------------------------------------------------------------------------


def test_stream_generation_writes_kg_bookkeeping(db_session):
    """流式生成一段后，generated 图谱与事实账本里必须出现该段抽出的事实。

    修复前：流式路径在 _commit_segment 之后不做任何 KG 归档 → 图谱/账本全空 → 红。
    """
    proj = _make_project(db_session)
    stub = StubProvider()

    events = list(generate_next_segment_stream(db_session, proj, provider=stub))
    done = [e for e in events if e.get("type") == "done"]
    assert done, "流式未产出 done 事件"

    db_session.expire_all()
    nodes = _graph_nodes(db_session, proj.id)
    facts = _ledger_facts(db_session, proj.id)

    assert nodes, (
        "流式生成后 generated 图谱为空——流式路径没做 KG 归档（缺陷）"
    )
    assert facts, (
        "流式生成后事实账本为空——流式路径没做 KG 归档（缺陷）"
    )
    names = {(n.props or {}).get("name") for n in nodes if n.type == "character"}
    assert {"林夜", "白光"} & names, f"图谱里没抽到本段角色，实际 {names}"


# ---------------------------------------------------------------------------
# T2：两条路径簿记结果形状一致（最要紧的护栏）
# ---------------------------------------------------------------------------


def test_stream_and_nonstream_bookkeeping_consistent(db_session):
    """同一语料分别走流式与非流式，簿记结果的**形状**必须一致。

    这条是本次最要紧的护栏：只改流式而两边形状不同，等于又造出一条
    "只更一半"的路径（7416114 才刚把四步编排收归唯一入口）。
    比的是形状（节点类型集合 / fact 类型集合 / 哨兵键是否都在），
    不比具体计数——两条路径的段号不同，计数本就可以不同。
    """
    stub = StubProvider()

    # 非流式
    p1 = _make_project(db_session, title="非流式")
    r1 = generate_next_segment(db_session, p1, provider=stub)
    # 流式
    p2 = _make_project(db_session, title="流式")
    events = list(generate_next_segment_stream(db_session, p2, provider=stub))
    done = [e for e in events if e.get("type") == "done"]
    assert done, "流式未产出 done"

    db_session.expire_all()

    n1, n2 = _graph_nodes(db_session, p1.id), _graph_nodes(db_session, p2.id)
    f1, f2 = _ledger_facts(db_session, p1.id), _ledger_facts(db_session, p2.id)

    # 图谱节点类型集合一致
    assert {n.type for n in n1} == {n.type for n in n2}, (
        f"两条路径的图谱节点类型不一致：非流式 {sorted({n.type for n in n1})} vs "
        f"流式 {sorted({n.type for n in n2})}"
    )
    # 账本 fact 类型集合一致
    assert {f.type for f in f1} == {f.type for f in f2}, (
        f"两条路径的账本 fact 类型不一致：非流式 {sorted({f.type for f in f1})} vs "
        f"流式 {sorted({f.type for f in f2})}"
    )
    # 两条路径都必须做过簿记（非空）
    assert n2 and f2, "流式路径没留下簿记——与 T1 同因"
    # 统计键形状一致：非流式的 kg_update 与流式的 done.kg 应都带四步痕迹
    kg1 = r1.get("kg_update") or {}
    kg2 = done[0].get("kg") or {}
    assert kg2, "流式 done 事件缺 kg 统计键（不可观测）"
    for key in ("sentinel",):
        assert key in kg1 and key in kg2, (
            f"两条路径的簿记统计键不一致：缺 {key}（非流式 {sorted(kg1)} / 流式 {sorted(kg2)}）"
        )


# ---------------------------------------------------------------------------
# T3：簿记抛错不得吞掉 SSE
# ---------------------------------------------------------------------------


def test_bookkeeping_failure_does_not_break_stream(db_session, monkeypatch):
    """merge_segment_into_kg 抛异常时：done 事件必须照样发出、正文照常落库。

    SSE 是长连接，簿记失败绝不能让生成"看起来卡住"。
    """
    import app.kg.build as build_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("KG 簿记炸了")

    monkeypatch.setattr(build_mod, "merge_segment_into_kg", _boom)

    proj = _make_project(db_session)
    stub = StubProvider()

    # 不得抛异常
    events = list(generate_next_segment_stream(db_session, proj, provider=stub))

    done = [e for e in events if e.get("type") == "done"]
    assert done, "簿记失败把 done 事件吞了——SSE 会看起来卡死（违反硬约束）"
    assert done[0].get("segment_no") == 1

    # 正文照常落库（簿记失败不该回滚已写成的段）
    db_session.expire_all()
    gen = db_session.scalars(
        select(Generation).where(Generation.project_id == proj.id)
    ).one()
    assert gen.text == SEG_TEXT, f"正文未照常落库，实际 {gen.text!r}"


# ---------------------------------------------------------------------------
# T4：非流式行为不变（回归护栏）
# ---------------------------------------------------------------------------


def test_rewrite_failure_does_not_crash_generation(db_session):
    """重写失败（模型抛错）时生成必须照常完成，不得二次抛异常。

    本次修流式簿记时顺带发现的**既有潜伏 bug**：engine.py 两处用了
    `logging.getLogger(...)` 却从未 `import logging`——重写失败走进 except
    处理器时，处理器自己抛 NameError，把整次生成带崩（生成成功、重写失败
    这种最常见的情况反而最惨）。补 import 后本测试守住这条路径。
    """
    ai_text = "就在这时他不禁深吸一口气，心中充满了怒火，他知道，要出事了。突然之间灯灭了。"

    class BoomOnRewriteProvider(StubProvider):
        """生成正常返回（带 AI 腔以触发重写），重写那次抛错。"""

        def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
            if "你是一位资深中文网文编辑" in system:
                raise RuntimeError("重写时模型炸了")
            return ai_text

    proj = _make_project(db_session)
    # 不得抛异常（修复前这里会抛 NameError: name 'logging' is not defined）
    result = generate_next_segment(db_session, proj, provider=BoomOnRewriteProvider())

    assert result.get("text") == ai_text, "重写失败应保留原文"
    assert result.get("rewrite_info") is None, "重写失败不应产出 rewrite_info"


def test_nonstream_kg_update_unchanged(db_session):
    """非流式路径的 kg_update 与簿记结果保持现状（本次不改动它）。"""
    proj = _make_project(db_session)
    stub = StubProvider()

    result = generate_next_segment(db_session, proj, provider=stub)

    kg = result.get("kg_update") or {}
    assert kg, "非流式 kg_update 不应为空"
    assert kg.get("merged") is True, f"非流式合并标记异常：{kg}"
    db_session.expire_all()
    assert _graph_nodes(db_session, proj.id), "非流式图谱簿记应仍在工作"
    assert _ledger_facts(db_session, proj.id), "非流式账本簿记应仍在工作"
