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


def test_chapter_mode_extends_outline_instead_of_dead_node(db, monkeypatch):
    """章模式不能追加没有章区间的死节点，正确扩展是补章。"""
    proj = _project(db)
    proj.outline = ["第1章 夜雨"]
    db.add(Generation(project_id=proj.id, segment_no=1,
                      chapter_range="第1章", text="正文"))
    db.commit()
    seen = {}

    def fake_extend(db_, project_, provider_, count):
        seen["count"] = count
        seen["provider"] = provider_ is not None
        return {"status": "extended", "chapters": [2], "message": "已补第 2 章"}

    monkeypatch.setattr(pe, "_extend_chapters", fake_extend)

    result = extend_plan(db, proj, Provider())

    assert result["status"] == "extended"
    assert result["chapters"] == [2]
    assert seen["count"] == 1
    assert seen["provider"] is True


_NODE = {
    "title": "门后的声音", "emotion": "悬疑",
    "entry_condition": "沈砚回到旧院区", "exit_condition": "沈砚发现门后有人",
    "beats": ["靠近", "听见"],
}


def _chapter_fixture(db):
    proj = _project(db)
    proj.outline = ["第1章 夜雨"]
    db.add(Generation(project_id=proj.id, segment_no=1,
                      chapter_range="第1章", text="正文"))
    db.commit()
    return proj


def _fake_draft(project_id, body, db_):
    return {
        "entries": [{"chapter": 2, "entry": "第2章《门后的声音》：沈砚听见门后有人叫他"}],
        "validation": {"ok": True},
        "source_fingerprint": "fp-1",
    }


def test_extend_chapters_drafts_then_applies_with_node(db, monkeypatch):
    """补章 = 起草 → 校验 → 应用，并同时追加一个剧情点。"""
    import app.routers.chapter_outline as co

    proj = _chapter_fixture(db)
    seen = {}

    def fake_apply(project_id, body, db_):
        seen["apply"] = (
            body.from_chapter, body.to_chapter, body.confirm,
            body.expected_fingerprint, len(body.entries),
        )
        return {"ok": True}

    monkeypatch.setattr(co, "draft_chapter_outline", _fake_draft)
    monkeypatch.setattr(co, "apply_chapter_outline", fake_apply)
    monkeypatch.setattr(pe, "_propose", lambda *a, **k: ([dict(_NODE)], []))
    monkeypatch.setattr(pe, "_out_of_authority", lambda *a, **k: [])

    result = pe._extend_chapters(db, proj, Provider(), 1)

    assert result["status"] == "extended"
    assert result["chapters"] == [2]
    assert [n["title"] for n in result["nodes"]] == ["门后的声音"]
    assert seen["apply"] == (2, 2, True, "fp-1", 1)
    db.refresh(proj)
    assert len(proj.plot_timeline["order"]) == 4
    last_id = proj.plot_timeline["order"][-1]
    assert proj.plot_timeline["nodes"][last_id]["title"] == "门后的声音"


def test_extend_chapters_maps_provider_error_to_readable_status(db, monkeypatch):
    """渠道断连要变成可读状态，不能把 500 抛给前端。"""
    import app.routers.chapter_outline as co

    proj = _chapter_fixture(db)
    monkeypatch.setattr(co, "draft_chapter_outline", _fake_draft)

    def boom(*args, **kwargs):
        raise RuntimeError("CommandCode chat 调用失败: Remote end closed connection")

    monkeypatch.setattr(pe, "_propose", boom)

    result = pe._extend_chapters(db, proj, Provider(), 1)

    assert result["status"] == "proposal_failed"
    assert "调用模型" in result["message"]


def test_extend_chapters_does_not_write_outline_when_nodes_fail(db, monkeypatch):
    """节点不合格时整批不落库：不能出现「章纲加了、情节没加」。"""
    import app.routers.chapter_outline as co

    proj = _chapter_fixture(db)
    applied = {"count": 0}

    monkeypatch.setattr(co, "draft_chapter_outline", _fake_draft)
    monkeypatch.setattr(
        co, "apply_chapter_outline",
        lambda *a, **k: applied.__setitem__("count", applied["count"] + 1),
    )
    monkeypatch.setattr(pe, "_propose", lambda *a, **k: ([], [{"error": "bad"}]))

    result = pe._extend_chapters(db, proj, Provider(), 1)

    assert result["status"] == "proposal_failed"
    assert applied["count"] == 0


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
