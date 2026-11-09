"""关系极性三态契约与冲突口径的回归测试。

口径（写死在 canon_source 与 conflict_scan）：
- same_chapter_clash = 正反两条边的**章号集合有交集**
- cross_chapter_flip  = **无交集**（剧情反转）
不用「章节区间重叠」，否则 ch1–200 为敌、ch150 为友会被误判成同章矛盾。
"""
from __future__ import annotations

from app.kg import canon_source, conflict_scan, layer_context


def _edge(a: str, b: str, rel: str, chapters, strength: float = 0.8) -> dict:
    return {"from": a, "to": b, "rel": rel, "strength": strength, "chapters": chapters}


def test_annotate_three_states():
    edges = [
        _edge("甲", "乙", "ally", [1, 2]),
        _edge("丙", "丁", "enemy", [3]),
        _edge("丙", "丁", "ally", [3, 4]),      # 同章有交集
        _edge("戊", "己", "ally", [1, 2]),
        _edge("戊", "己", "enemy", [8, 9]),     # 无交集
        _edge("庚", "辛", "mentor", [1]),
    ]
    canon_source.annotate_polarity(edges)
    assert edges[0]["polarity_state"] == canon_source.POLARITY_STABLE
    assert edges[1]["polarity_state"] == canon_source.POLARITY_CONFLICTING
    assert edges[1]["polarity_kind"] == canon_source.SAME_CHAPTER_CLASH
    assert edges[2]["polarity_kind"] == canon_source.SAME_CHAPTER_CLASH
    assert edges[3]["polarity_state"] == canon_source.POLARITY_CONFLICTING
    assert edges[3]["polarity_kind"] == canon_source.CROSS_CHAPTER_FLIP
    assert "polarity_state" not in edges[5]  # 非极性关系不写键


def test_annotate_clears_stale_state_after_filtering():
    edges = [_edge("甲", "乙", "ally", [1]), _edge("甲", "乙", "enemy", [9])]
    canon_source.annotate_polarity(edges)
    assert edges[0]["polarity_state"] == canon_source.POLARITY_CONFLICTING
    # 过滤掉 enemy 后重新标注：不能残留 conflicting
    kept = canon_source.annotate_polarity([edges[0]])
    assert kept[0]["polarity_state"] == canon_source.POLARITY_STABLE
    assert "polarity_kind" not in kept[0]


def test_chapter_list_tolerates_json_and_range():
    assert canon_source.chapter_list("[2,4,8]") == [2, 4, 8]
    assert canon_source.chapter_list([3, "3", 1]) == [1, 3]
    assert canon_source.chapter_list("1-30") == [1, 30]
    assert canon_source.chapter_list(None) == []


def test_scan_polarity_splits_by_chapter_set_overlap():
    edges = [
        _edge("甲", "乙", "ally", [2]),
        _edge("乙", "甲", "enemy", [2]),       # 反向书写 + 同章
        _edge("丙", "丁", "ally", [1, 2]),
        _edge("丙", "丁", "enemy", [7]),
    ]
    result = conflict_scan.scan_polarity(edges)
    assert result["counts"] == {
        "same_chapter_clash": 1, "cross_chapter_flip": 1, "total": 2,
    }
    assert {frozenset(x["pair"]) for x in result["same_chapter_clash"]} == \
        {frozenset({"甲", "乙"})}
    assert result["same_chapter_clash"][0]["overlap"] == [2]
    assert {frozenset(x["pair"]) for x in result["cross_chapter_flip"]} == \
        {frozenset({"丙", "丁"})}
    assert result["cross_chapter_flip"][0]["overlap"] == []


def _nodes() -> list[dict]:
    return [
        {"name": "许七安", "kind": "character", "aliases": [], "attrs": {},
         "mentions": 900, "last_chapter": 200},
        {"name": "魏渊", "kind": "character", "aliases": [], "attrs": {},
         "mentions": 400, "last_chapter": 150},
        {"name": "褚采薇", "kind": "character", "aliases": [], "attrs": {},
         "mentions": 80, "last_chapter": 100},
    ]


def test_context_keeps_conflicting_pairs_out_of_relations(monkeypatch):
    edges = [
        _edge("许七安", "魏渊", "mentor", [1]),
        _edge("许七安", "魏渊", "ally", [10, 11]),
        _edge("许七安", "魏渊", "enemy", [11]),
        _edge("许七安", "褚采薇", "ally", [5]),
    ]
    canon_source.annotate_polarity(edges)
    monkeypatch.setattr(layer_context, "read_canon", lambda _p: (_nodes(), edges))

    ctx = layer_context.build_context(["许七安"], "unused.db")
    assert ctx["canon_conflicts"] == 1
    assert ctx["layers"]["canon_conflicts"] == 1
    # 冲突边不能出现在正常关系段
    assert "许七安 —ally→ 魏渊" not in ctx["text"]
    assert "许七安 —enemy→ 魏渊" not in ctx["text"]
    # 但必须提示「极性相反」，且标明是同章并存
    assert "极性相反" in ctx["text"]
    assert "同章内正反并存" in ctx["text"]
    # 稳定的边照常渲染，并带三态字段
    assert "许七安 —ally→ 褚采薇" in ctx["text"]
    assert {e["polarity_state"] for e in ctx["edges"]} <= {
        canon_source.POLARITY_STABLE, ""
    }
