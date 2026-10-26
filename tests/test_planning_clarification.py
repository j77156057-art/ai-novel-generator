import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def test_unknown_planning_settings_become_structured_clarification():
    from app.generation.planning import build_clarification_report

    nodes = [{
        "title": "核对登记",
        "premise": "unknown",
        "motivation": "澄清责任",
        "action": "核对原件",
        "result": "unknown",
        "cost": "NA",
        "time": "21:00",
        "location": "桥下",
        "participants": ["沈砚"],
        "state_before": "unknown",
        "state_after": "记录已核对",
    }]
    report = build_clarification_report(nodes, "fp-v1")
    assert report["status"] == "clarification_required"
    assert report["verified"] is False
    assert report["source_fingerprint"] == "fp-v1"
    assert report["clarifications"]
    item = report["clarifications"][0]
    assert {"question", "why_required", "dependent_nodes", "existing_evidence", "authorized_fabrication"} <= set(item)
    assert item["authorized_fabrication"] is False
    assert report["unknown_fields"]


def test_planning_candidate_rejects_stale_fingerprint_and_keeps_unknown_unverified():
    from app.generation.planning import validate_planning_candidate

    node = {
        "title": "节点", "premise": "unknown", "motivation": "调查",
        "action": "查验", "result": "unknown", "cost": "NA",
        "time": "21:00", "location": "桥下", "participants": ["甲"],
        "state_before": "unknown", "state_after": "待复核",
    }
    candidate = {
        "status": "outline_draft", "source_fingerprint": "new-fp",
        "nodes": [node, {**node, "title": "节点2"}, {**node, "title": "节点3"}],
    }
    checked = validate_planning_candidate(candidate, "new-fp")
    assert checked["status"] == "clarification_required"
    assert checked["verified"] is False
    assert checked["unknown_fields"]
    try:
        validate_planning_candidate(candidate, "old-fp")
    except ValueError as exc:
        assert "fingerprint" in str(exc)
    else:
        raise AssertionError("stale candidate was accepted")


def test_structured_planning_state_is_consumed_by_node_prompt():
    from app.generation.planning import timeline_for
    from app.generation.timeline import build_node_prompt, state_from_dict

    nodes = [{
        "title": "桥下核验", "premise": "记录待核", "motivation": "澄清责任",
        "action": "核对原件", "result": "差异可复核", "cost": "失去返程时间",
        "time": "21:00", "location": "旧桥下", "participants": ["沈砚", "陆禾"],
        "state_before": "封条未验", "state_after": "页码差异已登记",
    }] * 3
    raw = timeline_for(nodes, {"source_fingerprint": "fp"}, 3)
    state = state_from_dict(raw, total_segments=3)
    prompt = build_node_prompt(state.nodes["n1"], 0, {"main_progress": 0, "status": "active"})
    for value in ("21:00", "旧桥下", "沈砚、陆禾", "封条未验", "页码差异已登记"):
        assert value in prompt
