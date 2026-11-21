"""agent 主动兜底：闸门拦住生成时，问答要附一张**可执行**的修复动作卡。

这套判定是确定性的（不依赖模型调工具），所以必须有回归：
状态判错 → 提错卡；提错卡 → 用户点一次必然失败的按钮。
"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.generation.engine import _AUTO_REPLAN_FAILED_REASON  # noqa: E402
from app.generation.timeline import (  # noqa: E402
    plan_timeline,
    state_to_dict,
    timeline_progress,
)
from app.models import Generation, Project  # noqa: E402
from app.routers.assistant import _gate_fallback_action, _gate_state  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _project(db, *, real_nodes: bool = False, done_all: bool = False,
             placeholder: bool = False) -> Project:
    st = plan_timeline(19)  # 19 段 → 3 个主干节点，与 target/generation_length 对齐
    if real_nodes or done_all:
        fill = [
            ("夜雨停电", "沈砚在档案室加班", "沈砚听见楼上有脚步"),
            ("旧院区的档案", "沈砚进入旧院区库房", "沈砚找到被撕走一页的卷宗"),
            ("缺失的一页", "沈砚比对卷宗缺口", "沈砚确认缺失页与十年前大火有关"),
        ]
        for (title, entry, exit_), nid in zip(fill, st.order):
            node = st.nodes[nid]
            node.title = title
            node.entry_condition = entry
            node.exit_condition = exit_
            node.source = "llm"
        st.planning_status = "llm"
    if done_all:
        st.done = set(st.nodes)
        st.current_node = None
    if placeholder:
        st.planning_status = "placeholder"
        st.planning_reason = _AUTO_REPLAN_FAILED_REASON

    proj = Project(
        title="兜底测试",
        premise="旧城停电夜，档案员发现被撕走的一页卷宗。",
        outline=["第1章 夜雨停电", "第2章 旧院区的档案", "第3章 缺失的一页"],
        target_length=15200,
        generation_length=800,
        plot_timeline=state_to_dict(st),
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _write_chapters(db, proj: Project) -> None:
    for no, label in enumerate(["第1章", "第2章", "第3章"], start=1):
        db.add(Generation(
            project_id=proj.id,
            segment_no=no,
            chapter_range=label,
            text=f"{label} 正文",
        ))
    db.commit()


def test_needs_planning_proposes_replan_timeline(db):
    proj = _project(db, placeholder=True)

    facts = _gate_state(db, proj)
    card = _gate_fallback_action(db, proj)

    assert facts["status"] == "needs_planning"
    assert card is not None
    assert card["kind"] == "action_proposal"
    assert card["body"]["kind"] == "replan_timeline"
    assert card["endpoint"] == "/api/assistant/action-proposals"


def test_plan_exhausted_proposes_extend_plan(db):
    proj = _project(db, done_all=True)
    _write_chapters(db, proj)

    facts = _gate_state(db, proj)
    card = _gate_fallback_action(db, proj)

    assert facts["plan_exhausted"] is True
    assert card is not None
    assert card["body"]["kind"] == "extend_plan"
    assert card["body"]["action_body"] == {"count": 1}


def test_chapter_mode_ignores_legacy_segment_budget():
    """章模式不看段预算：段模式遗留的 seg_counts 不能把整本书卡成 needs_review。"""
    st = plan_timeline(19)
    node = st.nodes[st.current_node]
    node.title = "真实节点"
    node.entry_condition = "进入"
    node.exit_condition = "退出"
    st.seg_counts[node.id] = 99  # 远超额度的历史段计数

    segment = timeline_progress(st)
    chapter = timeline_progress(st, chapter_based=True)

    assert segment["status"] == "needs_review"
    assert chapter["status"] == "active"
    assert chapter["budget_exhausted"] is False


def test_healthy_project_has_no_fallback_card(db):
    """能正常写的项目不该被塞一张「修复」卡。"""
    proj = _project(db, real_nodes=True)  # 真实节点、未写完

    assert _gate_fallback_action(db, proj) is None
