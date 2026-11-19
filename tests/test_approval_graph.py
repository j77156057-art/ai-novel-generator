"""审批子图（LangGraph）与状态机的一致性测试。

跑法（仓库根）：
    backend/venv/Scripts/python.exe -m pytest tests/test_approval_graph.py -q

根 `tests/conftest.py` 会把库指到临时目录；这里每个用例先 drop/create 全表。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.agents import approval_graph
from app.agents import proposals as proposal_lib
from app.db import Base, SessionLocal, engine
from app.models import AgentRun, EditProposal, Generation, Project, Version


@pytest.fixture()
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def proposal(db):
    """一条 pending 的 text_edit 提案 + 它要改的那段正文。"""
    project = Project(
        title="审批子图测试", mode="quick", target_length=5000, plot_bible={}
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    gen = Generation(
        project_id=project.id,
        segment_no=1,
        chapter_range="1-1",
        text="原始正文：铜钥匙藏在门后。",
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="success",
        started_at=proposal_lib._now_utc(),
        finished_at=proposal_lib._now_utc(),
        request_hash=proposal_lib.request_hash({"test": True}),
        model="(test)",
    )
    db.add(run)
    db.flush()

    row = proposal_lib.build_proposal(
        db,
        run_id=run.id,
        project_id=project.id,
        proposal_type="text_edit",
        target_kind="generation_text",
        target_id=str(gen.id),
        after_text="改写后的正文：铜钥匙被张三取出。",
        reason="测试审批子图",
    )
    db.commit()
    db.refresh(row)
    return row, gen.id


def _version_count(db) -> int:
    return int(db.scalar(select(func.count(Version.id))) or 0)


def test_graph_nodes_and_actions_match_state_machine():
    """图与转移表同源：节点集合固定，动作集合 = 状态机动作并集。"""
    nodes = set(approval_graph.approval_graph.get_graph().nodes)
    nodes -= {"__start__", "__end__"}
    assert nodes == set(approval_graph.GRAPH_NODES)

    allowed_actions: set[str] = set()
    for actions in proposal_lib.ALLOWED_TRANSITIONS.values():
        allowed_actions |= set(actions)
    assert approval_graph.GRAPH_ACTIONS == frozenset(allowed_actions)


def test_apply_before_approve_is_rejected_by_graph(db, proposal):
    row, gen_id = proposal
    db.expire_all()

    with pytest.raises(proposal_lib.InvalidTransition):
        approval_graph.run_approval(db, proposal=row, action="apply", actor="t")

    db.expire_all()
    assert db.get(EditProposal, row.id).status == "pending"
    assert db.get(Generation, gen_id).text == "原始正文：铜钥匙藏在门后。"
    assert _version_count(db) == 0


def test_approve_then_apply_via_graph(db, proposal):
    row, gen_id = proposal

    approved = approval_graph.run_approval(
        db, proposal=row, action="approve", actor="t"
    )
    assert approved["status"] == "approved"
    db.expire_all()
    # 审批只改状态：业务表与版本表都还没动。
    assert db.get(Generation, gen_id).text == "原始正文：铜钥匙藏在门后。"
    assert _version_count(db) == 0

    applied = approval_graph.run_approval(db, proposal=row, action="apply", actor="t")
    assert applied["status"] == "applied"
    assert applied["result"]["version_id"]
    db.expire_all()
    assert db.get(Generation, gen_id).text == "改写后的正文：铜钥匙被张三取出。"
    assert _version_count(db) == 1


def test_reject_via_graph_then_approve_is_rejected(db, proposal):
    row, _ = proposal

    rejected = approval_graph.run_approval(
        db, proposal=row, action="reject", actor="t", note="不符合设定"
    )
    assert rejected["status"] == "rejected"
    db.expire_all()
    saved = db.get(EditProposal, row.id)
    assert saved.status == "rejected"
    assert "[reject] 不符合设定" in (saved.reason or "")

    with pytest.raises(proposal_lib.InvalidTransition):
        approval_graph.run_approval(db, proposal=saved, action="approve", actor="t")


def test_unknown_action_routes_to_invalid_node(db, proposal):
    row, _ = proposal
    with pytest.raises(proposal_lib.InvalidTransition):
        approval_graph.run_approval(db, proposal=row, action="delete", actor="t")

    db.expire_all()
    assert db.get(EditProposal, row.id).status == "pending"
