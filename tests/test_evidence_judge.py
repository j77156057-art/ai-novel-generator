"""P0：证据化完成判定进入生成主链，并随 `plot_timeline.completion` 落库。"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.generation.engine import (  # noqa: E402
    _judge_and_advance,
    _judge_evidence,
    _judge_verdict,
    _persist_timeline_state,
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
