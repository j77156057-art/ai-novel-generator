"""正文一致性诊断的确定性部分：finding 校验、候选构造（含边界回显）、读取正文。

只钉「搬进来的地基」能做什么，不接调用方：
- `validate_findings` 的证据纪律（矛盾必须两端、id 必须在 sources 里、每项 ≤12）；
- `build_body_candidate_multi` 的区间校验（有序不重叠、长度上限、删除需显式允许）
  与边界回显防护；
- `read_body` 按段号读取，段不存在时 **拒绝**而不是猜一段。
"""

from __future__ import annotations

import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.body_consistency import (  # noqa: E402
    MAX_REPAIR_RANGES,
    build_body_candidate,
    build_body_candidate_multi,
    candidate_ranges,
    changed_body_sources,
    dimension_of,
    experiment_outcome,
    read_body,
    supported_citations,
    validate_findings,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Generation, Project  # noqa: E402


# ====================================================================
# fixtures（只建本文件用到的两张表）
# ====================================================================


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


def _project(db, **kw):
    proj = Project(title="一致性", target_length=5000, mode="quick",
                   status="created", generation_length=3000, **kw)
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _source(sid, text, start=0):
    return {sid: {"source_id": "generation:1", "quote": text,
                  "start": start, "end": start + len(text)}}


# ====================================================================
# finding 校验
# ====================================================================


def test_supported_finding_needs_at_least_one_citation():
    sources = _source("P001", "主角在雨夜登场")
    answer = {"findings": [{"dimension": "participants", "assessment": "supported",
                            "reason": "原文写了", "source_ids": ["P001"]}]}
    checks = validate_findings(answer, sources)
    assert checks[0]["id"] == "participants_1"
    assert checks[0]["citations"][0]["location_verified"] is True


def test_contradicted_finding_needs_two_distinct_sources():
    sources = {**_source("P001", "他带着伞"), **_source("P002", "他两手空空", 10)}
    answer = {"findings": [{"dimension": "object_state", "assessment": "contradicted",
                            "reason": "前后相反", "source_ids": ["P001"]}]}
    with pytest.raises(ValueError, match="invalid_body_evidence"):
        validate_findings(answer, sources)
    answer["findings"][0]["source_ids"] = ["P001", "P002"]
    assert len(validate_findings(answer, sources)[0]["citations"]) == 2


def test_unknown_source_id_or_bad_shape_is_refused():
    sources = _source("P001", "正文")
    bad = [
        {"findings": []},
        {"findings": [{"dimension": "time", "assessment": "supported",
                       "reason": "x", "source_ids": ["P999"]}]},
        {"findings": [{"dimension": "time", "assessment": "maybe",
                       "reason": "x", "source_ids": ["P001"]}]},
        {"findings": [{"dimension": "time", "assessment": "supported",
                       "reason": "  ", "source_ids": ["P001"]}]},
        {"findings": [{"dimension": "time", "assessment": "supported",
                       "reason": "x", "source_ids": ["P001", "P001"]}]},
    ]
    for answer in bad:
        with pytest.raises(ValueError, match="invalid_body_evidence"):
            validate_findings(answer, sources)


def test_experiment_outcome_requires_semantic_acceptance():
    report = {"source_unchanged": True, "original_body_unchanged": True, "stages": {
        "diagnosis": {"status": "completed"},
        "writer": {"status": "pending", "outside_range_unchanged": True},
        "verification": {"status": "completed", "unresolved": []},
    }}
    # 没有 checks → 语义未验收，pipeline 过也不算成功
    assert experiment_outcome(report)["pipeline_completed"] is True
    assert experiment_outcome(report)["success"] is False
    report["stages"]["verification"]["checks"] = [{"status": "supported"}]
    assert experiment_outcome(report)["success"] is True
    report["stages"]["verification"]["unresolved"] = ["missing_saved_contract"]
    assert experiment_outcome(report)["success"] is False


# ====================================================================
# 候选构造
# ====================================================================


def test_single_candidate_replaces_only_the_range():
    text = "开头。中间。结尾。"
    c = build_body_candidate(text, 3, 5, "中央")
    assert c["after_text"] == "开头。中央。结尾。"
    assert c["base_content_hash"] and c["after_hash"] != c["base_content_hash"]
    assert c["apply_state"] == "candidate_only" and c["verified"] is False
    assert c["fixed_range"]["range_hash"] == c["fixed_ranges"][0]["range_hash"]


def test_multi_candidate_applies_in_order_and_is_ordered_disjoint():
    text = "AAAABBBBCCCC"
    c = build_body_candidate_multi(text, [
        {"start": 0, "end": 4, "replacement": "aa"},
        {"start": 8, "end": 12, "replacement": "cc"},
    ])
    assert c["after_text"] == "aaBBBBcc"
    assert c["fixed_range"] is None and len(c["fixed_ranges"]) == 2
    with pytest.raises(ValueError, match="invalid_candidate_range"):
        build_body_candidate_multi(text, [
            {"start": 0, "end": 6, "replacement": "x"},
            {"start": 4, "end": 8, "replacement": "y"},
        ])


def test_range_limits_and_delete_permission():
    text = "正文" * 20
    with pytest.raises(ValueError, match="invalid_candidate_range"):
        build_body_candidate_multi(text, [
            {"start": i * 2, "end": i * 2 + 1, "replacement": "x"}
            for i in range(MAX_REPAIR_RANGES + 1)
        ])
    with pytest.raises(ValueError, match="invalid_candidate_range"):
        build_body_candidate(text, 0, 2, "")
    assert build_body_candidate(text, 0, 2, "", allow_delete=True)["after_text"] == text[2:]


def test_boundary_echo_is_refused_but_adjacent_selected_range_is_allowed():
    text = "前文八个字以上的上下文在这里。要改的句子。后文八个字以上的上下文也在这里。"
    start = text.index("要改的句子")
    end = start + len("要改的句子")
    # 复制后文上下文 → 边界回显
    with pytest.raises(ValueError, match="candidate_boundary_echo"):
        build_body_candidate(text, start, end, "改完了。" + text[end:end + 12])
    # 复制**同一候选里另一个被选区间** → 合法（那段本来就会被替换）
    tail = text[end:]
    c = build_body_candidate_multi(text, [
        {"start": start, "end": end, "replacement": "改完了。" + tail},
        {"start": end, "end": len(text), "replacement": "收束。"},
    ])
    assert c["after_text"].startswith("前文八个字以上的上下文在这里。改完了。")


def test_candidate_ranges_accepts_legacy_single_shape():
    c = build_body_candidate("ABCDE", 1, 3, "x")
    legacy = {"fixed_range": c["fixed_range"]}
    assert candidate_ranges(legacy) == [{"start": 1, "end": 3, "replacement": "x"}]
    assert candidate_ranges(c) == [{"start": 1, "end": 3, "replacement": "x"}]


def test_changed_sources_and_supported_citations():
    sources = {
        "P001": {"source_id": "generation:1", "start": 0, "end": 5},
        "P002": {"source_id": "generation:1", "start": 5, "end": 10},
        "P003": {"source_id": "generation:9", "start": 0, "end": 5},
    }
    changed = changed_body_sources(sources, 1, [{"start": 4, "end": 6, "replacement": "x"}])
    assert changed == {"P001", "P002"}
    checks = [
        {"status": "supported", "citations": [{"source_id": "P001"}, {"source_id": "P002"}]},
        {"status": "unknown", "citations": [{"source_id": "P003"}]},
    ]
    assert supported_citations(checks) == {"P001", "P002"}


def test_dimension_of_maps_inventory_kinds():
    assert dimension_of("object_N001") == "object_state"
    assert dimension_of("person_3") == "participants"
    assert dimension_of("place_2") == "place"
    assert dimension_of("time_1") == "time"


# ====================================================================
# 读取正文
# ====================================================================


def test_read_body_reads_exact_requested_segments(db):
    proj = _project(db)
    db.add_all([
        Generation(project_id=proj.id, segment_no=1, text="第一段\n第二段"),
        Generation(project_id=proj.id, segment_no=2, text="第三段"),
    ])
    db.commit()

    data, fingerprint, coverage = read_body(db, proj, {"segment_nos": [1, 2]})
    assert [s["segment_no"] for s in data["segments"]] == [1, 2]
    assert set(data["body_texts"]) == {str(g.id) for g in db.query(Generation).all()}
    assert len(fingerprint) == 64
    assert coverage["complete"] is True
    # 没有保存契约 → 必须显式 unknown，不能当成「没矛盾」
    assert data["segments"][0]["unknown"] == "missing_saved_contract"


def test_read_body_refuses_missing_or_ambiguous_segments(db):
    proj = _project(db)
    db.add(Generation(project_id=proj.id, segment_no=1, text="正文"))
    db.commit()
    with pytest.raises(PermissionError, match="segment_not_found_or_ambiguous"):
        read_body(db, proj, {"segment_nos": [1, 2]})
    # 同一段号两行 → 也拒绝（不能猜是哪一段）
    db.add(Generation(project_id=proj.id, segment_no=1, text="重复"))
    db.commit()
    with pytest.raises(PermissionError, match="segment_not_found_or_ambiguous"):
        read_body(db, proj, {"segment_nos": [1]})


def test_read_body_requires_bounded_scope(db):
    proj = _project(db)
    with pytest.raises(ValueError, match="segment_scope_required"):
        read_body(db, proj, {})
    with pytest.raises(ValueError, match="segment_scope_required"):
        read_body(db, proj, {"segment_nos": list(range(1, 12))})


def test_diagnostic_module_imports_standalone():
    """`diagnostic.py` 只有 models + diagnostic_skills 两个顶层依赖，应能独立导入。"""
    import importlib

    module = importlib.import_module("app.agents.diagnostic")
    assert callable(module.digest) and callable(module.read_capability)
    assert "diagnostic" in module.ROLE_SYSTEM_PROMPTS
