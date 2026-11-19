"""P0：段契约来源统一——落库时冻结节点/权威，并与时间线状态一起持久化。"""
import hashlib
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.generation.engine import _commit_segment  # noqa: E402
from app.generation.timeline import (  # noqa: E402
    assign_chapter_ranges,
    plan_timeline,
    state_from_dict,
    state_to_dict,
)
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


def _project_with_timeline(db) -> Project:
    st = plan_timeline(total_segments=4)
    st.nodes["n1"].title = "开局"
    st.nodes["n1"].entry_condition = "主角刚进城"
    st.nodes["n1"].exit_condition = "拿到第一份委托"
    st.nodes["n1"].beats = ["问路", "接单"]
    assign_chapter_ranges(st, 2)
    proj = Project(
        title="t",
        premise="前提",
        outline=["第1章 起", "第2章 承"],
        plot_timeline=state_to_dict(st),
    )
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _contracts(proj: Project) -> dict:
    return ((proj.plot_timeline or {}).get("planning") or {}).get(
        "segment_contracts"
    ) or {}


def test_commit_segment_freezes_contract(db):
    proj = _project_with_timeline(db)
    prompt = '{"system": "SYS", "user": "USER"}'
    authority = '{"version": 1}'

    gen, segment_no = _commit_segment(
        db, proj, "正文", "摘要",
        chapter_range="第1章",
        prompt_json=prompt,
        authority_json=authority,
    )

    contract = _contracts(proj)[str(segment_no)]
    assert contract["node_id"] == "n1"
    assert contract["generation_id"] == gen.id
    assert contract["chapter_range"] == "第1章"
    assert contract["authority"]["premise"] == "前提"
    node = contract["authority"]["causal_node"]
    assert node["title"] == "开局"
    assert node["entry_condition"] == "主角刚进城"
    assert node["exit_condition"] == "拿到第一份委托"
    assert node["beats"] == ["问路", "接单"]
    assert contract["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert contract["authority_sha256"] == hashlib.sha256(authority.encode()).hexdigest()


def test_contract_follows_chapter_mapping(db):
    proj = _project_with_timeline(db)

    gen, segment_no = _commit_segment(
        db, proj, "第二章正文", "摘要", chapter_range="第2章"
    )

    assert _contracts(proj)[str(segment_no)]["node_id"] == "n2"
    assert gen.chapter_range == "第2章"


def test_no_timeline_keeps_plot_timeline_empty(db):
    proj = Project(title="t")
    db.add(proj)
    db.commit()

    _commit_segment(db, proj, "正文", "摘要", prompt_json="{}")

    assert proj.plot_timeline is None


def test_contracts_survive_state_roundtrip():
    st = plan_timeline(total_segments=4)
    st.segment_contracts = {
        "1": {"node_id": "n1", "authority": {"premise": "前提"}},
    }
    st.completion = {"n1": {"verdict": "supported", "reason": "有证据"}}
    st.last_judged_segment = 3

    restored = state_from_dict(state_to_dict(st), total_segments=4)

    assert restored is not None
    assert restored.segment_contracts == st.segment_contracts
    assert restored.completion == st.completion
    assert restored.last_judged_segment == 3


def test_legacy_state_without_planning_loads_empty_contracts():
    st = plan_timeline(total_segments=4)
    data = state_to_dict(st)
    data.pop("planning")

    restored = state_from_dict(data, total_segments=4)

    assert restored is not None
    assert restored.segment_contracts == {}
