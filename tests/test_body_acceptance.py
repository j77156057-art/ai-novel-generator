"""整段验收：覆盖证据不足就不发模型请求；通过才把全局义务关闭到这一版正文。

main 原用例依赖 `test_causal_timeline.fixture` / `read_capability`（未搬或运行时依赖缺口），
这里用等价的最小夹具：直接构造 `read_closed_inventory` 观测（`body_texts` + `sources`），
其余断言逐条保留。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.body_acceptance import (  # noqa: E402
    body_fingerprint,
    run_body_acceptance,
)
from app.agents.diagnostic import digest  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import AgentRun, Generation, Project  # noqa: E402

BODY_BEFORE = "她戴着手套。\n账册未动。"
BODY_AFTER = "她看着账册。\n账册未动。"
AUTHORITY = "手套是未授权物件。"


class Reply:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def chat(self, system, user):
        self.calls.append(json.loads(user))
        return json.dumps(self.answer, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _inventory_run(db, project, generation, *, complete=True):
    """造一条 closed_inventory 审计（覆盖证据的来源）。"""
    body = generation.text or ""
    data = {
        "segments": [{"segment_no": generation.segment_no,
                      "generation_id": generation.id}],
        "body_texts": {str(generation.id): body},
        "sources": {
            "C1S001": {"source_id": "contract:1", "path": "/authority/premise",
                       "quote": AUTHORITY, "start": 0, "end": len(AUTHORITY)},
            "G1P001": {"source_id": f"generation:{generation.id}",
                       "quote": body.split("\n")[0], "start": 0,
                       "end": len(body.split("\n")[0])},
        },
    }
    obs = {"capability": "read_closed_inventory", "data": data,
           "input_fingerprint": digest(data)}
    report = {
        "project_id": project.id, "status": "completed", "agent_role": "diagnostic",
        "provider_role": "assistant", "model_participated": True,
        "skill": {"id": "closed_inventory", "version": 4, "status": "experimental"},
        "scope": {"segment_nos": [generation.segment_no], "inventory_kinds": ["object"]},
        "observations": [obs], "input_fingerprint": digest([obs]),
        "acceptance": [{"id": "explicit_closure", "requires_evidence": True},
                       {"id": "full_scope_coverage", "requires_evidence": True}],
        "inventory": {"input_fingerprint": obs["input_fingerprint"],
                      "coverage_complete": complete,
                      "global_quality_verified": complete,
                      "verdict_scope": {"source_ids": ["G1P001"], "complete": complete},
                      "unreviewed_dimensions": [] if complete else ["person", "place"],
                      "mentions": [{"source_id": "G1P001", "quote": "她"}]},
        "checks": [] if complete else [{"id": "object_N011", "status": "contradicted",
                                        "reason": "小凳子未授权", "citations": []}],
        "unresolved": ["inventory_partial_coverage"],
    }
    run = AgentRun(project_id=project.id, role="assistant", status="success",
                   request_hash="test")
    db.add(run)
    db.flush()
    report["run_id"] = run.id
    run.note = json.dumps(report, ensure_ascii=False)
    db.commit()
    return run, report


def _acceptance_fixture(db, *, current_inventory=True, complete=True):
    project = Project(title="整段验收", target_length=5000, mode="quick",
                      status="created", generation_length=3000)
    db.add(project)
    db.commit()
    db.refresh(project)
    generation = Generation(project_id=project.id, segment_no=1, text=BODY_BEFORE)
    db.add(generation)
    db.commit()
    db.refresh(generation)

    origin, _ = _inventory_run(db, project, generation, complete=complete)
    # 模拟一次已落地的修复：正文变了，原审计的证据随之失效。
    generation.text = BODY_AFTER
    db.commit()
    if current_inventory:
        _inventory_run(db, project, generation, complete=complete)

    rows = list(db.query(Generation).filter_by(project_id=project.id).all())
    timeline = dict(project.plot_timeline or {})
    timeline["pending_acceptance"] = {
        "state": "carried_forward",
        "unresolved": ["inventory_partial_coverage", "original_check:object_N011"],
        "closed_check_ids": [],
        "acceptance": [{"id": "explicit_closure", "requires_evidence": True},
                       {"id": "full_scope_coverage", "requires_evidence": True}],
        "inventory_fingerprint": "fp", "source_run_id": origin.id,
        "diagnostic_run_id": origin.id,
        "bound_body_hash": body_fingerprint(rows),
    }
    project.plot_timeline = timeline
    db.commit()
    return project, generation, body_fingerprint(rows)


def _good_answer():
    return {
        "assessment": "observed", "reason": "整段核对完成",
        "dimensions": [{"dimension": d, "assessment": "supported", "reason": "一致",
                        "source_ids": ["G1P001"]}
                       for d in ("time", "place", "participants", "object_state")],
        "check_decisions": [{"check_id": "object_N011", "status": "resolved",
                             "reason": "小凳子已不在正文中", "source_ids": ["G1P001"]}],
        "coverage": {"examined_source_ids": ["G1P001", "G1P002"], "complete": True,
                     "reason": "逐段核对"},
    }


def test_acceptance_failure_records_the_error_message(db):
    class Boom:
        model = "fake-minimax"

        def chat(self, *args, **kwargs):
            raise RuntimeError("MiniMax chat 调用失败: 429 已达到 Token Plan 用量上限")

    project, _, fingerprint = _acceptance_fixture(db)
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=Boom())
    assert report["status"] == "blocked"
    assert report["global_semantic_accepted"] is False
    assert report["model_error_category"] == "RuntimeError"
    assert "429" in report["model_error_message"]
    assert "Token Plan" in report["model_error_message"]
    assert "acceptance_failed" in report["unresolved"]
    db.refresh(project)
    assert project.plot_timeline["pending_acceptance"]["state"] != "accepted"


def test_missing_current_inventory_returns_coverage_incomplete_without_model_call(db):
    project, _, fingerprint = _acceptance_fixture(db, current_inventory=False)
    provider = Reply(_good_answer())
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert report["status"] == "coverage_incomplete"
    assert report["global_semantic_accepted"] is False
    assert provider.calls == []
    db.refresh(project)
    assert project.plot_timeline["pending_acceptance"]["state"] == "carried_forward"


def test_incomplete_inventory_coverage_returns_coverage_incomplete(db):
    project, _, fingerprint = _acceptance_fixture(db, complete=False)
    provider = Reply(_good_answer())
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert report["status"] == "coverage_incomplete"
    assert provider.calls == []


def test_unreviewed_dimension_keeps_coverage_open(db):
    project, generation, fingerprint = _acceptance_fixture(db)
    run, report = _inventory_run(db, project, generation, complete=True)
    note = json.loads(run.note)
    note["inventory"]["unreviewed_dimensions"] = ["place"]
    run.note = json.dumps(note, ensure_ascii=False)
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert result["status"] == "coverage_incomplete"
    assert "place" in result["coverage"]["unreviewed_dimensions"]
    assert provider.calls == []


def test_audit_violation_blocks_the_acceptance(db):
    project, generation, fingerprint = _acceptance_fixture(db)
    run, report = _inventory_run(db, project, generation, complete=True)
    note = json.loads(run.note)
    note["checks"] = [{"id": "object_N021", "status": "contradicted",
                       "reason": "仍有未授权物件", "citations": []}]
    run.note = json.dumps(note, ensure_ascii=False)
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert result["status"] == "blocked"
    assert "audit_violation:object_N021" in result["unresolved"]


def test_audit_unknown_verdict_is_kept_open_not_hidden(db):
    project, generation, fingerprint = _acceptance_fixture(db)
    run, report = _inventory_run(db, project, generation, complete=True)
    note = json.loads(run.note)
    note["checks"] = [{"id": "object_N017", "status": "unknown",
                       "reason": "所指实体无法确认", "citations": []}]
    run.note = json.dumps(note, ensure_ascii=False)
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert result["status"] == "accepted"
    assert [item["id"] for item in result["open_items"]] == ["object_N017"]
    db.refresh(project)
    assert project.plot_timeline["pending_acceptance"]["open_items"]


def test_full_acceptance_closes_the_global_obligation(db):
    project, _, fingerprint = _acceptance_fixture(db)
    provider = Reply(_good_answer())
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert report["status"] == "accepted"
    assert report["global_semantic_accepted"] is True
    assert report["unresolved"] == []
    assert len(provider.calls) == 1
    db.refresh(project)
    pending = project.plot_timeline["pending_acceptance"]
    assert pending["state"] == "accepted"
    assert pending["unresolved"] == []
    assert pending["bound_body_hash"] == fingerprint
    assert pending["acceptance_run_id"] == report["run_id"]


def test_unresolved_check_keeps_the_obligation_open(db):
    project, _, fingerprint = _acceptance_fixture(db)
    answer = _good_answer()
    answer["check_decisions"][0]["status"] = "still_violated"
    provider = Reply(answer)
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert report["status"] == "blocked"
    assert report["global_semantic_accepted"] is False
    assert "original_check:object_N011" in report["unresolved"]
    db.refresh(project)
    assert project.plot_timeline["pending_acceptance"]["state"] == "carried_forward"


def test_incomplete_coverage_claim_does_not_pass(db):
    project, _, fingerprint = _acceptance_fixture(db)
    answer = _good_answer()
    answer["coverage"]["examined_source_ids"] = ["G1P001"]
    answer["coverage"]["complete"] = True
    provider = Reply(answer)
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert report["status"] == "blocked"
    assert "coverage_incomplete" in report["unresolved"]


def test_top_level_assessment_must_be_observed(db):
    project, _, fingerprint = _acceptance_fixture(db)
    answer = _good_answer()
    answer["assessment"] = "supported"
    provider = Reply(answer)
    report = run_body_acceptance(db, project.id, base_content_hash=fingerprint,
                                 provider=provider)
    assert report["status"] == "blocked"
    assert "invalid_top_level_assessment" in report["unresolved"]


def test_stale_body_fingerprint_is_rejected(db):
    project, _, _ = _acceptance_fixture(db)
    provider = Reply(_good_answer())
    with pytest.raises(ValueError) as error:
        run_body_acceptance(db, project.id, base_content_hash="stale", provider=provider)
    assert str(error.value) == "stale_body_fingerprint"
    assert provider.calls == []


def test_missing_pending_acceptance_is_rejected(db):
    project, _, fingerprint = _acceptance_fixture(db)
    project.plot_timeline = {k: v for k, v in (project.plot_timeline or {}).items()
                             if k != "pending_acceptance"}
    db.commit()
    provider = Reply(_good_answer())
    with pytest.raises(ValueError) as error:
        run_body_acceptance(db, project.id, base_content_hash=fingerprint, provider=provider)
    assert str(error.value) == "no_pending_acceptance"
    assert provider.calls == []
