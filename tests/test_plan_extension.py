"""plan_extension：段模式下追加剧情点并预写契约，失败状态可解释。"""
import json
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
import app.agents.plan_extension as pe  # noqa: E402
from app.agents.plan_extension import extend_plan  # noqa: E402
from app.generation.timeline import plan_timeline, state_to_dict  # noqa: E402
from app.models import Generation, Project  # noqa: E402

_NODES = {"nodes": [{
    "title": "新剧情点", "emotion": "悬疑",
    "entry_condition": "主角回到旧院区",
    "exit_condition": "在档案室找到缺失的一页",
    "beats": ["进楼", "翻档案"],
}]}


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


class Provider:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or _NODES
        self.calls = 0

    def chat(self, system, user, constraints=None):
        self.calls += 1
        return json.dumps(self.payload, ensure_ascii=False)


def _project(db, *, exhausted: bool = True, premise: str | None = "前提") -> Project:
    st = plan_timeline(total_segments=4)
    if exhausted:
        st.done = set(st.order)
        st.current_node = None
    proj = Project(title="t", premise=premise, plot_timeline=state_to_dict(st))
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def test_requires_saved_authority(db):
    proj = _project(db, premise=None)

    result = extend_plan(db, proj, Provider())

    assert result["status"] == "no_saved_authority"


def test_requires_exhausted_plan(db):
    proj = _project(db, exhausted=False)

    result = extend_plan(db, proj, Provider())

    assert result["status"] == "plan_not_exhausted"
    assert "未写" in result["message"]


def test_chapter_mode_is_rejected_with_next_step(db):
    proj = _project(db)
    db.add(Generation(project_id=proj.id, segment_no=1,
                      chapter_range="第1章", text="正文"))
    db.commit()

    result = extend_plan(db, proj, Provider())

    assert result["status"] == "chapter_mode_requires_outline"
    assert "章纲" in result["message"]


def test_extend_appends_node_and_contract(db, monkeypatch):
    proj = _project(db)
    monkeypatch.setattr(pe, "_out_of_authority", lambda *a, **k: [])
    before_order = list(proj.plot_timeline["order"])
    provider = Provider()

    result = extend_plan(db, proj, provider)

    assert result["status"] == "extended"
    assert provider.calls == 1
    db.refresh(proj)
    timeline = proj.plot_timeline
    assert len(timeline["order"]) == len(before_order) + 1
    nid = timeline["order"][-1]
    node = timeline["nodes"][nid]
    assert node["title"] == "新剧情点"
    assert node["entry_condition"] == "主角回到旧院区"
    assert node["exit_condition"] == "在档案室找到缺失的一页"
    assert node["beats"] == ["进楼", "翻档案"]
    assert timeline["current_node"] == nid
    contract = timeline["planning"]["segment_contracts"]["1"]
    assert contract["node_id"] == nid
    assert contract["authority"]["premise"] == "前提"


def test_out_of_authority_is_rejected_and_not_written(db, monkeypatch):
    proj = _project(db)
    before = json.dumps(proj.plot_timeline, ensure_ascii=False, sort_keys=True)
    monkeypatch.setattr(
        pe, "_out_of_authority",
        lambda *a, **k: [{"entity": "镊子", "kind": "object", "reason": "权威里没有"}],
    )

    result = extend_plan(db, proj, Provider())

    assert result["status"] == "out_of_authority_items"
    assert "镊子" in result["message"]
    db.refresh(proj)
    after = json.dumps(proj.plot_timeline, ensure_ascii=False, sort_keys=True)
    assert after == before


def test_plan_extension_endpoint_exists_and_maps_failures_to_400(db):
    """动作卡的 `extend_plan` 端点必须真实存在：无规划 → 可读 400。"""
    from fastapi import HTTPException

    from app.routers.projects import PlanExtensionRequest, extend_project_plan

    proj = Project(title="no-plan")
    db.add(proj)
    db.commit()
    db.refresh(proj)

    with pytest.raises(HTTPException) as err:
        extend_project_plan(proj.id, PlanExtensionRequest(count=1), db)

    assert err.value.status_code == 400
    assert "没有时间线状态" in str(err.value.detail)
