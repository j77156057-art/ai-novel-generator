"""P0：证据化完成判定进入生成主链，并随 `plot_timeline.completion` 落库。"""
import json
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.generation.engine import (  # noqa: E402
    _AUTO_REPLAN_FAILED_REASON,
    _judge_and_advance,
    _judge_evidence,
    _judge_verdict,
    _persist_timeline_state,
    _state_plan_exhausted,
    current_node_prompt,
)
from app.generation.timeline import plan_timeline  # noqa: E402
from app.models import Project  # noqa: E402


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


def _project_with_node(db) -> Project:
    proj = Project(
        title="t",
        outline=["a", "b", "c"],
        target_length=30000,
        generation_length=3000,
    )
    db.add(proj)
    db.commit()
    st = plan_timeline(total_segments=10)
    st.nodes["n1"].title = "开局"
    st.nodes["n1"].entry_condition = "故事开始"
    st.nodes["n1"].exit_condition = "目标达成"
    st.nodes["n1"].segment_budget = 3
    _persist_timeline_state(db, proj, st)
    db.refresh(proj)
    return proj


def test_judge_verdict_mapping():
    assert _judge_verdict({"verdict": "supported"}) is True
    assert _judge_verdict({"verdict": "contradicted"}) is False
    assert _judge_verdict({"verdict": "unknown"}) is None
    assert _judge_verdict({}) is None


def test_judge_evidence_is_lightweight():
    judge = {
        "verdict": "supported",
        "reason": "正文写明",
        "sources": [{"paragraph_id": "P001", "quote": "目标达成。"}],
        "audit": {"text_hash": "t" * 64, "plan_hash": "p" * 64,
                  "error_category": None, "response_raw": "很长"},
    }
    out = _judge_evidence(judge)
    assert out == {
        "verdict": "supported",
        "reason": "正文写明",
        "source_ids": ["P001"],
        "text_hash": "t" * 64,
        "plan_hash": "p" * 64,
        "error_category": None,
    }


def test_judge_and_advance_persists_completion(db):
    proj = _project_with_node(db)

    class Provider:
        def chat(self, system, user, constraints=None):
            return ('{"verdict": "supported", "reason": "正文已写明目标达成", '
                    '"source_ids": ["P001"]}')

    out = _judge_and_advance(db, proj, Provider(), "他终于完成了目标。", 0)

    assert out["judge"] is True
    assert out["evidence"]["source_ids"] == ["P001"]
    db.refresh(proj)
    completion = proj.plot_timeline["completion"]["n1"]
    assert completion["verdict"] == "supported"
    assert completion["audit"]["text_hash"]
    assert proj.plot_timeline["last_judged_segment"] == 1


def test_judge_runs_without_entry_condition(db):
    """entry_condition 不参与判定的提示词；老存档为空时不能被提前短路。"""
    proj = _project_with_node(db)
    timeline = dict(proj.plot_timeline)
    timeline["nodes"] = {
        **timeline["nodes"],
        "n1": {**timeline["nodes"]["n1"], "entry_condition": ""},
    }
    proj.plot_timeline = timeline
    db.commit()

    calls = {"n": 0}

    class Provider:
        def chat(self, system, user, constraints=None):
            calls["n"] += 1
            return ('{"verdict": "supported", "reason": "正文已写明", '
                    '"source_ids": ["P001"]}')

    out = _judge_and_advance(db, proj, Provider(), "目标达成。", 0)

    assert calls["n"] == 1
    assert out["judge"] is True


def test_judge_failure_falls_back_without_completion_claim(db):
    proj = _project_with_node(db)

    class Broken:
        def chat(self, *args, **kwargs):
            raise RuntimeError("LLM 挂了")

    out = _judge_and_advance(db, proj, Broken(), "正文。", 0)

    assert out["judge"] is None
    assert out["advanced"] is True
    db.refresh(proj)
    completion = proj.plot_timeline["completion"]["n1"]
    assert completion["verdict"] == "unknown"
    assert completion["audit"]["error_category"] == "request_failed"


# ---------------------------------------------------------------------------
# 存量计划迁移：半填/占位计划不能把写到一半的项目永远卡在 needs_planning
# ---------------------------------------------------------------------------


def _plan_items(count: int = 3) -> list[dict]:
    return [
        {
            "title": f"旧宅节点{i}",
            "entry_condition": "主角回到旧宅",
            "exit_condition": f"完成第{i}件事",
            "emotion": "悬疑",
        }
        for i in range(1, count + 1)
    ]


class _PlanProvider:
    def __init__(self, items: list[dict]):
        self.items = items
        self.calls: list[str] = []

    def chat(self, system, user, constraints=None, **kwargs):
        self.calls.append(user)
        return json.dumps({"nodes": self.items}, ensure_ascii=False)


def _set_current_node(db, proj, *, exit_condition: str, seg_counts: dict):
    timeline = dict(proj.plot_timeline)
    timeline["nodes"] = {
        **timeline["nodes"],
        "n1": {**timeline["nodes"]["n1"], "exit_condition": exit_condition},
    }
    timeline["seg_counts"] = dict(seg_counts)
    proj.plot_timeline = timeline
    db.commit()
    db.refresh(proj)


def test_mid_book_placeholder_plan_is_replanned_instead_of_blocked(db):
    """已写到一半、节点缺 exit 的存量项目：生成入口应自动重规划，而不是 409。"""
    proj = _project_with_node(db)
    _set_current_node(db, proj, exit_condition="", seg_counts={"n1": 1})
    provider = _PlanProvider(_plan_items(3))

    prompt = current_node_prompt(proj, 1, provider, db=db)

    assert len(provider.calls) == 1, "半填计划没有触发自动重规划"
    assert "完成第1件事" in prompt
    db.refresh(proj)
    assert proj.plot_timeline["planning_status"] == "llm"
    assert proj.plot_timeline["seg_counts"] == {"n1": 1}, "重规划不得改动已写段计数"


def test_failed_auto_replan_is_not_retried_on_every_request(db):
    """自动重规划失败后留痕：下一次生成入口不再白调一次模型。"""
    proj = _project_with_node(db)
    _set_current_node(db, proj, exit_condition="", seg_counts={"n1": 1})
    provider = _PlanProvider([])  # 模型给不出可用计划

    with pytest.raises(HTTPException) as first:
        current_node_prompt(proj, 1, provider, db=db)
    assert first.value.status_code == 409
    assert first.value.detail["status"] == "needs_planning"
    assert len(provider.calls) == 1

    with pytest.raises(HTTPException):
        current_node_prompt(proj, 1, provider, db=db)
    assert len(provider.calls) == 1, "失败留痕后仍重复调用了模型"
    db.refresh(proj)
    assert proj.plot_timeline["planning_reason"] == _AUTO_REPLAN_FAILED_REASON


def test_rewrite_mode_is_not_blocked_by_planning_state(db):
    """重写路径（strict=False）：不抛 409、不触发重规划，只在提示里说明状态。"""
    proj = _project_with_node(db)
    _set_current_node(db, proj, exit_condition="", seg_counts={"n1": 999})

    class NoCall:
        def chat(self, *args, **kwargs):
            raise AssertionError("strict=False 不应触发自动重规划")

    prompt = current_node_prompt(proj, 1, NoCall(), db=db, strict=False)

    assert "needs_planning" in prompt


# ---------------------------------------------------------------------------
# 规划写完：不再无约束续写（main 的 plan_exhausted 口径）
# ---------------------------------------------------------------------------


def _mark_plan_done(db, proj) -> None:
    timeline = dict(proj.plot_timeline)
    timeline["done"] = ["n1", "n2", "n3"]
    timeline["current_node"] = None
    proj.plot_timeline = timeline
    db.commit()
    db.refresh(proj)


def test_completed_plan_blocks_new_generation(db):
    proj = _project_with_node(db)
    _mark_plan_done(db, proj)

    class NoCall:
        def chat(self, *args, **kwargs):
            raise AssertionError("规划写完后不应再调模型")

    with pytest.raises(HTTPException) as err:
        current_node_prompt(proj, 3, NoCall(), db=db)

    assert err.value.status_code == 409
    assert err.value.detail["status"] == "plan_exhausted"


def test_completed_plan_still_allows_rewrite(db):
    """重写旧章节不受 plan_exhausted 限制（strict=False）。"""
    proj = _project_with_node(db)
    _mark_plan_done(db, proj)

    prompt = current_node_prompt(proj, 3, None, db=db, strict=False)
    assert prompt is None


def test_free_continuation_is_not_plan_exhausted():
    """没有规划 / 只有支线的项目不能被当成「规划写完」。"""
    from app.generation.timeline import PlotNode, TimelineState

    assert _state_plan_exhausted(None) is False
    branch_only = TimelineState(
        nodes={
            "b1": PlotNode(
                id="b1", kind="branch", title="支线", emotion="甜",
                entry_condition="起点", exit_condition="终点",
            )
        },
        order=[], total_main=0,
    )
    assert _state_plan_exhausted(branch_only) is False


def test_chapter_mode_plan_exhausted_after_last_chapter(db):
    """章号模式：越过最后一个节点覆盖的章之后，同样 409 plan_exhausted。"""
    from app.generation.timeline import (
        assign_chapter_ranges,
        plan_timeline,
        state_to_dict,
    )

    proj = Project(
        title="章模式", outline=["第1章", "第2章"],
        target_length=30000, generation_length=3000,
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    st = plan_timeline(total_segments=10)
    for i, node in enumerate(st.nodes.values(), 1):
        node.title = f"节点{i}"
        node.entry_condition = "起点"
        node.exit_condition = f"完成第{i}件事"
    assign_chapter_ranges(st, 2)
    last_chapter = max(node.chapter_end for node in st.nodes.values())
    proj.plot_timeline = state_to_dict(st)
    db.commit()
    db.refresh(proj)

    # 最后一章仍在范围内 → 正常给提示
    prompt = current_node_prompt(
        proj, last_chapter, None, db=db,
        chapter=last_chapter, chapter_based=True,
    )
    assert "【当前剧情点】" in prompt

    # 越过最后一章 → 全部主干节点 done → 409
    with pytest.raises(HTTPException) as err:
        current_node_prompt(
            proj, last_chapter + 1, None, db=db,
            chapter=last_chapter + 1, chapter_based=True,
        )
    assert err.value.status_code == 409
    assert err.value.detail["status"] == "plan_exhausted"
