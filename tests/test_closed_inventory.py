"""闭集盘点 v1：先有界抽取，再独立裁定；「没说死」的清单不许当闭集。

只钉搬进来的地基（`inspect_inventory` / `read_inventory` / 批次与引用工具），
不接调用方。main 侧的 `run_diagnostic_task` / `fixture` / `experimental_consent`
属于后续批次，因此这里用假的 provider 回放同一套协议。

三类断言：
1. **封闭语义**：只有明确封闭措辞（仅有/只有/清单完整/地点始终…）才是 closed，
   普通罗列仍是 open；
2. **协议纪律**：约束只认 `/authority/premise`、引用必须逐字可定位、
   覆盖 must 完整（漏一段就整批失败，不能悄悄少读）；
3. **裁定与产物**：violated → `contradicted` + 建议动作（但不执行、不落库）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.closed_inventory import (  # noqa: E402
    KINDS,
    STATES,
    _citation,
    explicit_closure,
    inspect_inventory,
    mention_batches,
    read_inventory,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Generation, Project  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"


# ====================================================================
# fixtures
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


def _observation(sources, segments=None):
    return {"data": {"segments": segments or [{"segment_no": 1, "generation_id": 1}],
                     "sources": sources}}


class _Replay:
    """按 stage 回放的假 provider；`answers` 里没给的 stage 直接报错。"""

    def __init__(self, answers):
        self.answers = answers
        self.stages = []

    def chat(self, system, user):
        payload = json.loads(user)
        stage = payload["stage"]
        self.stages.append(stage)
        answer = self.answers[stage]
        answer = answer(payload) if callable(answer) else answer
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


# ====================================================================
# 封闭语义
# ====================================================================


def test_closure_markers_are_explicit_not_merely_enumerated():
    for quote in ["室内仅有书本。", "只有两个人。", "清单完整。", "无其他物件。", "only two items"]:
        assert explicit_closure("object", quote) is True, quote
    # 普通罗列不是闭集
    for quote in ["桌上有书本、铅笔和压板。", "她拿来焊枪。"]:
        assert explicit_closure("object", quote) is False, quote


def test_place_closure_has_its_own_phrasings():
    assert explicit_closure("place", "地点始终是社区图书室。") is True
    assert explicit_closure("place", "全程位于舰舱。") is True
    assert explicit_closure("place", "她后来去了码头。") is False
    # 这些措辞只对 place 生效，不能扩大成物件/人物的闭集证据
    assert explicit_closure("object", "地点始终是社区图书室。") is False


# ====================================================================
# 分批与引用
# ====================================================================


def test_mention_batches_never_drops_paragraphs_and_chunks_by_budget():
    sources = {f"G1P{i:03d}": {"quote": "x" * 40} for i in range(1, 11)}
    batches = mention_batches(sources, char_budget=100)
    assert [sid for batch in batches for sid in batch] == list(sources)
    assert all(len(batch) >= 1 for batch in batches)
    assert len(batches) > 1


def test_oversized_paragraph_is_refused_not_silently_omitted():
    with pytest.raises(ValueError, match="inventory_paragraph_too_large"):
        mention_batches({"G1P001": {"quote": "x" * 200}}, char_budget=100)


def test_citation_must_be_literally_locatable():
    sources = {"C1S001": {"source_id": "contract:1", "path": "/authority/premise",
                          "quote": "室内仅有书本。", "start": 10, "end": 17}}
    cited = _citation("C1S001", sources, "仅有书本")
    assert cited["start"] == 12 and cited["end"] == 16
    assert cited["body_source_id"] == "contract:1" and cited["location_verified"] is True
    with pytest.raises(ValueError, match="inventory_source_mismatch"):
        _citation("C1S001", sources, "室内有焊枪")


# ====================================================================
# read_inventory：把保存契约的 authority 变成 source
# ====================================================================


def _project(db, contract=None):
    proj = Project(title="盘点", target_length=5000, mode="quick", status="created",
                   generation_length=3000)
    if contract is not None:
        proj.plot_timeline = {"planning": {"segment_contracts": contract}}
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def test_read_inventory_collects_authority_paths_and_marks_coverage(db):
    proj = _project(db, {"1": {"authority": {"premise": "室内仅有书本。",
                                             "causal_node": {"premise": "金印不得使用。"}}}})
    db.add(Generation(project_id=proj.id, segment_no=1, text="她读书本。"))
    db.commit()

    data, fingerprint, coverage = read_inventory(db, proj, {"segment_nos": [1]})
    paths = {s["path"] for s in data["sources"].values() if s["source_id"] == "contract:1"}
    assert paths == {"/authority/premise", "/authority/causal_node/premise"}
    assert coverage["authority_complete"] is True
    assert coverage["source"] == "Generation.text+saved_segment_contract.authority"
    assert len(fingerprint) == 64


def test_read_inventory_flags_missing_authority_instead_of_claiming_complete(db):
    proj = _project(db)  # 没有保存契约
    db.add(Generation(project_id=proj.id, segment_no=1, text="她读书本。"))
    db.commit()

    _, _, coverage = read_inventory(db, proj, {"segment_nos": [1]})
    assert coverage["authority_complete"] is False


# ====================================================================
# inspect_inventory：完整协议回放
# ====================================================================


def test_full_protocol_reports_violation_without_executing_repair():
    raw = json.loads((_FIXTURES / "closed_inventory_valid_constraints.json")
                     .read_text(encoding="utf-8"))["raw"]
    constraints = json.loads(raw)["constraints"]
    authority_quote = "".join(c["quote"] for c in constraints)
    sources = {
        "C1S096": {"source_id": "contract:1", "path": "/authority/premise",
                   "quote": authority_quote, "start": 0, "end": len(authority_quote)},
        "G1P001": {"source_id": "generation:1", "quote": "图册在原处。她用扫描仪扫了一页。",
                   "start": 0, "end": 18},
    }
    report = {"unresolved": []}
    provider = _Replay({
        "constraints": raw,
        "mentions": {"covered_source_ids": ["G1P001"],
                     "mentions": [{"source_id": "G1P001", "surface": "扫描仪",
                                   "canonical": "扫描仪", "kind": "object",
                                   "state": "actual"}]},
        "verdicts": lambda payload: {"verdicts": [
            {"candidate_id": c["id"], "status": "violated",
             "reason": "起始物品清单完整且不增添工具，扫描仪不在授权清单内"}
            for c in payload["candidates"]]},
    })

    result = inspect_inventory(provider, _observation(sources), report)

    assert provider.stages == ["constraints", "mentions", "verdicts"]
    assert len(report["inventory"]["constraints"]) == 3
    assert report["inventory"]["coverage_complete"] is True
    assert result["assessment"] == "observed"
    assert report["checks"][0]["status"] == "contradicted"
    # 两端证据：契约 + 正文
    assert {c["body_source_id"].split(":")[0] for c in report["checks"][0]["citations"]} == {
        "contract", "generation"}
    action = report["proposed_actions"][0]
    assert action["kind"] == "inventory_repair_suggestion"
    assert action["requires_confirmation"] is True
    assert action["execution_available"] is False and action["verified"] is False


def test_closed_constraint_from_non_premise_path_is_refused():
    raw = json.loads((_FIXTURES / "closed_inventory_invalid_constraint.json")
                     .read_text(encoding="utf-8"))["raw"]
    quote = json.loads(raw)["constraints"][0]["quote"]
    sources = {
        "C1S090": {"source_id": "contract:1", "path": "/authority/causal_node/premise",
                   "quote": quote, "start": 0, "end": len(quote)},
        "C1S100": {"source_id": "contract:1", "path": "/authority/premise",
                   "quote": "室内仅有书本。", "start": 0, "end": 8},
        "G1P001": {"source_id": "generation:1", "quote": "她读书本。", "start": 0, "end": 6},
    }

    class _ReplayAll:
        def chat(self, *args):
            return raw

    with pytest.raises(ValueError, match="invalid_inventory_constraints"):
        inspect_inventory(_ReplayAll(), _observation(sources), {"unresolved": []})


def test_incomplete_coverage_fails_the_whole_batch():
    sources = {
        "C1S001": {"source_id": "contract:1", "path": "/authority/premise",
                   "quote": "室内仅有书本。", "start": 0, "end": 8},
        "G1P001": {"source_id": "generation:1", "quote": "书本在桌上。", "start": 0, "end": 6},
        "G1P002": {"source_id": "generation:1", "quote": "她拿起金印。", "start": 6, "end": 12},
    }
    provider = _Replay({
        "constraints": {"constraints": []},
        "mentions": {"covered_source_ids": ["G1P001"], "mentions": []},
    })
    with pytest.raises(ValueError, match="inventory_incomplete_coverage"):
        inspect_inventory(provider, _observation(sources), {"unresolved": []})


def test_missing_authority_cannot_claim_complete_coverage():
    sources = {"G1P001": {"source_id": "generation:1", "quote": "她拿起金印。",
                          "start": 0, "end": 6}}
    report = {"unresolved": []}
    provider = _Replay({})  # 没有 authority，一次模型调用都不该发生

    result = inspect_inventory(provider, _observation(sources), report)

    assert provider.stages == []
    assert report["unresolved"] == ["missing_saved_authority", "inventory_partial_coverage"]
    assert report["inventory"]["coverage_complete"] is False
    assert result["assessment"] == "unknown"
    assert report["checks"][0]["status"] == "unknown"


def test_empty_body_is_unresolved_not_violation():
    # 正文为空时 `read_body` 不产出任何段落 source（不是产出一条空 quote）
    sources = {"C1S001": {"source_id": "contract:1", "path": "/authority/premise",
                          "quote": "室内仅有书本。", "start": 0, "end": 8}}
    report = {"unresolved": []}
    provider = _Replay({"constraints": {"constraints": []}})

    result = inspect_inventory(provider, _observation(sources), report)

    # 空正文段落不算「读过」：不产生候选，也不许声称覆盖完整
    assert provider.stages == []          # 空正文不发起模型调用
    assert "inventory_empty_body" in report["unresolved"]
    assert report["inventory"]["coverage_complete"] is False
    assert result["assessment"] == "unknown"
    assert report["proposed_actions"] == []


def test_kind_and_state_enums_are_the_documented_ones():
    assert KINDS == {"object", "person", "place"}
    assert STATES == {"actual", "memory", "hypothetical", "metaphor", "negated",
                      "quoted", "unknown"}
