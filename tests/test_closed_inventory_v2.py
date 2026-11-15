"""闭集盘点 v2：分维度 + 分批 + 检查点复用；只有「成功且通过校验」的批次才可复用。

main 侧原本还有若干 `run_diagnostic_task` / `experimental_consent` 级别的用例，
它们依赖后续批次才搬的运行件，这里只搬**协议自身**的断言：
- 结构化判定 → 结论的确定性推导（模型不再直接下「违例」结论）；
- 同一实体自相矛盾的裁决要降级为 unknown，不能当违例；
- 提及必须逐字可定位，模型说错段落/字面词时做**确定性重锚**（不放松契约）；
- 检查点复用要过指纹与校验；改正文 → 指纹变化 → 失效重问；
- 83 段真实正文在 650 字预算下分批且一段不丢。
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

from app.agents import closed_inventory  # noqa: E402
from app.agents.body_evidence import paragraph_sources  # noqa: E402
from app.agents.closed_inventory_v2 import (  # noqa: E402
    authorized_names,
    converge_candidates,
    derive_status,
    inconsistent_verdicts,
    inspect_inventory,
    locate_mention,
    reanchor_mention,
)

_FIXTURES = Path(__file__).parent / "fixtures"


# ====================================================================
# 纯规则
# ====================================================================


def test_derive_status_is_deterministic_from_entity_kind():
    closed = {"mode": "closed"}
    open_ = {"mode": "open"}
    assert derive_status("part_of_authorized", closed) == "allowed"
    assert derive_status("in_authority_context", closed) == "allowed"
    assert derive_status("not_an_entity", closed) == "allowed"
    assert derive_status("unclear", closed) == "unknown"
    assert derive_status("independent", closed) == "violated"
    assert derive_status("contained_in_authorized", closed) == "violated"
    # 清单不封闭 ⇒ 不在清单内也不构成违例
    assert derive_status("independent", open_) == "allowed"
    assert derive_status("contained_in_authorized", open_) == "allowed"


def _cand(cid, canonical, surface, status):
    return {"id": cid,
            "mention": {"canonical": canonical, "surface": surface},
            "verdict": {"status": status}}


def test_inconsistent_verdicts_catches_canonical_and_surface_level_conflicts():
    # 规范名相同、原词不同
    by_canonical = [_cand("N001", "焊枪", "焊枪", "violated"),
                    _cand("N002", "焊枪", "它", "allowed")]
    # 规范名不同、原词相同
    by_surface = [_cand("N003", "电焊设备", "焊枪", "violated"),
                  _cand("N004", "工具", "焊枪", "allowed")]
    # 毫无关系的两个实体，各自一致 ⇒ 不算矛盾
    unrelated = [_cand("N005", "焊枪", "焊枪", "violated"),
                 _cand("N006", "扳手", "扳手", "allowed")]

    assert set(inconsistent_verdicts(by_canonical)) == {"N001"}
    assert set(inconsistent_verdicts(by_surface)) == {"N003"}
    assert inconsistent_verdicts(unrelated) == {}
    # 只有违例方降级，被允许的那条不动
    assert "N002" not in inconsistent_verdicts(by_canonical)


def test_authorized_names_only_counts_closed_lists():
    constraints = [
        {"mode": "closed", "entities": [{"canonical": "扳手", "aliases": ["扳子"]}]},
        {"mode": "open", "entities": [{"canonical": "焊枪", "aliases": []}]},
    ]
    assert authorized_names(constraints) == {"扳手", "扳子"}


# ====================================================================
# 提及定位与重锚
# ====================================================================


def _sources():
    return {
        "G1P001": {"source_id": "generation:1", "quote": "她拿起一把扳手。", "start": 0, "end": 9},
        "G1P002": {"source_id": "generation:1", "quote": "桌上还有一个扳手。", "start": 9, "end": 19},
    }


def test_locate_mention_keeps_every_occurrence_unless_selected():
    m = {"source_id": "G1P001", "surface": "扳手", "canonical": "扳手",
         "state": "actual", "relation": "持有"}
    found = locate_mention(m, _sources())
    assert found["locations"] == [{"start": 5, "end": 7}]
    m2 = dict(m, occurrence=1)
    assert locate_mention(m2, _sources())["locations"] == [{"start": 5, "end": 7}]


def test_locate_mention_rejects_rewritten_surface():
    m = {"source_id": "G1P001", "surface": "焊接工具", "canonical": "焊枪",
         "state": "actual", "relation": "持有"}
    with pytest.raises(ValueError, match="inventory_source_mismatch"):
        locate_mention(m, _sources())


def test_reanchor_moves_to_nearest_paragraph_that_actually_contains_the_token():
    sources = _sources()
    body_ids = list(sources)
    m = {"source_id": "G1P002", "surface": "扳手", "canonical": "活动扳手",
         "state": "actual", "relation": "持有"}
    fixed, info = reanchor_mention(m, sources, body_ids)
    # canonical「活动扳手」正文里没有 → 退到 surface「扳手」，就近锚到 G1P002
    assert fixed["source_id"] == "G1P002" and fixed["surface"] == "扳手"
    assert info == {"from_source_id": "G1P002", "to_source_id": "G1P002", "token": "扳手"}


def test_reanchor_refuses_pronouns_and_unknown_tokens():
    sources = _sources()
    pronoun = {"source_id": "G1P001", "surface": "她", "canonical": "她",
               "state": "actual", "relation": "持有"}
    assert reanchor_mention(pronoun, sources, list(sources)) == (None, None)
    ghost = {"source_id": "G1P001", "surface": "焊枪", "canonical": "焊枪",
             "state": "actual", "relation": "持有"}
    assert reanchor_mention(ghost, sources, list(sources)) == (None, None)


def test_converge_candidates_merges_only_identical_and_indexes_candidates():
    constraint = {"segment_no": 1, "kind": "object", "mode": "closed",
                  "entities": [{"canonical": "扳手", "aliases": []}]}
    base = {"segment_no": 1, "kind": "object", "source_id": "G1P001",
            "canonical": "焊枪", "state": "actual"}
    mentions = [dict(base), dict(base), dict(base, source_id="G1P002", canonical="扳手")]
    out = converge_candidates(mentions, [constraint])
    assert [c["id"] for c in out["candidates"]] == ["N001"]
    assert len(out["authorized"]) == 1
    assert out["counts"] == {"mentions": 3, "independent_entities": 2,
                             "authorized_entities": 1, "duplicate_mentions_merged": 1,
                             "pending_entities": 1}


# ====================================================================
# 批次预算（main 原用例）
# ====================================================================


def test_character_budget_covers_all_83_sources_without_loss():
    sources = {f"G1P{i:03}": {"quote": "正文物件。" * (i % 11 + 1)} for i in range(1, 84)}
    batches = closed_inventory.mention_batches(sources, char_budget=420)
    assert [sid for b in batches for sid in b] == list(sources)
    assert all(sum(len(v["quote"]) + len(k) + 8 for k, v in b.items()) <= 420 for b in batches)
    assert len(batches) > 3
    with pytest.raises(ValueError, match="inventory_paragraph_too_large"):
        closed_inventory.mention_batches({"G1P001": {"quote": "长" * 421}}, char_budget=420)


class _Scoped:
    """回放用的假 provider：按 stage 给答案（协议 2 口径）。"""

    def __init__(self, omit=False, calls=None):
        self.calls = calls if calls is not None else []
        self.omit = omit

    def chat(self, system, user):
        d = json.loads(user)
        self.calls.append(d)
        kind = d["dimension"]
        if d["stage"] == "constraints":
            return json.dumps({"constraints": [] if self.omit else [{
                "kind": kind, "mode": "closed",
                "entities": [{"canonical": "扳手", "aliases": []}],
                "source_id": next(iter(d["sources"])), "quote": "室内仅有扳手。"}]})
        if d["stage"] == "mentions":
            return json.dumps({"covered_source_ids": list(d["sources"]), "mentions": [
                {"source_id": sid, "canonical": "焊枪", "quote": "焊枪", "state": "actual"}
                for sid in d["sources"]]})
        return json.dumps({"verdicts": [
            {"candidate_id": c["id"], "status": "violated", "reason": "仅有扳手，新增焊枪"}
            for c in d["candidates"]]})


def test_real_existing_83_paragraphs_budgeted_and_validated():
    text = json.loads((_FIXTURES / "closed_inventory_existing_body.json")
                      .read_text(encoding="utf-8"))["text"]
    body = paragraph_sources(text, "generation:10", prefix="G1P")
    assert len(body) == 83
    authority = {"C1S096": {"source_id": "contract:1", "path": "/authority/premise",
                            "quote": "室内仅有扳手。", "start": 0, "end": 8}}

    class EmptyLedger(_Scoped):
        def chat(self, system, user):
            d = json.loads(user)
            if d["stage"] == "mentions":
                self.calls.append(d)
                assert sum(len(v) + len(k) + 8 for k, v in d["sources"].items()) <= 650
                return json.dumps({"covered_source_ids": list(d["sources"]), "mentions": []})
            return super().chat(system, user)

    report = {"unresolved": []}
    inspect_inventory(EmptyLedger(), {"data": {
        "segments": [{"segment_no": 1, "generation_id": 10}],
        "sources": dict(authority, **body)}}, report, kinds=["object"])
    assert report["inventory"]["dimensions"]["object"]["validated_source_ids"] == list(body)
    assert report["inventory"]["coverage_complete"] is True


def test_real_v2_bad_quote_is_rejected_with_replayable_details():
    data = json.loads((_FIXTURES / "closed_inventory_v2_source_mismatch.json")
                      .read_text(encoding="utf-8"))
    sources = {sid: {"source_id": "generation:10", "quote": text, "start": 0, "end": len(text)}
               for sid, text in data["request"]["sources"].items()}
    sources["C1S096"] = {"source_id": "contract:1", "path": "/authority/premise",
                         "quote": "室内仅有扳手。", "start": 0, "end": 8}

    class Replay(_Scoped):
        def chat(self, system, user):
            d = json.loads(user)
            if d["stage"] == "mentions":
                self.calls.append(d)
                return json.dumps(data["response"], ensure_ascii=False)
            return super().chat(system, user)

    report = {"unresolved": []}
    with pytest.raises(ValueError, match="inventory_source_mismatch"):
        inspect_inventory(Replay(), {"data": {
            "segments": [{"segment_no": 1, "generation_id": 10}],
            "sources": sources}}, report, kinds=["object"])
    assert report["inventory"]["calls"][-1]["rejected_citation"] == {
        "source_id": "G1P025",
        "quote": "她手里的核对表",
        "source_quote": data["request"]["sources"]["G1P025"],
    }
    assert report["inventory"]["dimensions"]["object"]["validated_source_ids"] == []


# ====================================================================
# 检查点：只有成功且过校验的批次可复用
# ====================================================================


def _scoped_observation(texts=("她拿起焊枪。", "她放下焊枪。")):
    body = {}
    for i, text in enumerate(texts, start=1):
        body.update(paragraph_sources(text, "generation:1", prefix=f"G1P{i}"))
    return {"data": {"segments": [{"segment_no": 1, "generation_id": 1}],
                     "sources": dict({
                         "C1S001": {"source_id": "contract:1", "path": "/authority/premise",
                                    "quote": "室内仅有扳手。", "start": 0, "end": 8},
                     }, **body)}}


def test_validated_batches_are_reused_and_body_change_invalidates_them(tmp_path):
    observation = _scoped_observation()
    kwargs = dict(kinds=["object"], checkpoint_dir=str(tmp_path), protocol=2)

    first, report1 = _Scoped(), {"unresolved": []}
    inspect_inventory(first, observation, report1, **kwargs)
    assert [c["stage"] for c in first.calls] == ["constraints", "mentions", "verdicts"]
    assert report1["checks"][0]["status"] == "contradicted"

    # 同一份输入再跑：一个模型调用都不该发生（全部命中已校验的检查点）
    second, report2 = _Scoped(), {"unresolved": []}
    inspect_inventory(second, observation, report2, **kwargs)
    assert second.calls == []
    assert all(call["cache_hit"] for call in report2["inventory"]["calls"])
    assert report2["checks"] == report1["checks"]

    # 改正文 → 指纹变化 → 旧检查点失效，必须重新问
    changed, report3 = _scoped_observation(texts=("她拿起焊枪。", "她放下焊枪又拿起焊枪。")), {"unresolved": []}
    third = _Scoped()
    inspect_inventory(third, changed, report3, **kwargs)
    assert len(third.calls) == 3
    assert report3["inventory"]["input_fingerprint"] != report2["inventory"]["input_fingerprint"]


def test_tampered_checkpoint_is_revalidated_and_refused(tmp_path):
    observation = _scoped_observation()
    kwargs = dict(kinds=["object"], checkpoint_dir=str(tmp_path), protocol=2)
    inspect_inventory(_Scoped(), observation, {"unresolved": []}, **kwargs)

    target = next(p for p in tmp_path.glob("*.json")
                  if json.loads(p.read_text(encoding="utf-8"))["stage"] == "mentions")
    saved = json.loads(target.read_text(encoding="utf-8"))
    saved["answer"]["covered_source_ids"] = []
    target.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="inventory_incomplete_coverage"):
        inspect_inventory(_Scoped(), observation, {"unresolved": []}, **kwargs)


def test_missing_dimension_constraint_fails_closed():
    observation = _scoped_observation()
    provider = _Scoped(omit=True)
    with pytest.raises(ValueError, match="inventory_missing_dimension"):
        inspect_inventory(provider, observation, {"unresolved": []},
                          kinds=["object"], protocol=2)
    assert len(provider.calls) == 1


def test_invalid_dimension_list_is_refused_before_any_call():
    provider = _Scoped()
    for bad in ([], ["object", "object"], ["camera"]):
        with pytest.raises(ValueError, match="invalid_inventory_dimensions"):
            inspect_inventory(provider, _scoped_observation(), {"unresolved": []}, kinds=bad)
    assert provider.calls == []
