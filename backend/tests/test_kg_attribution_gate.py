"""减法式主宾门：四类硬拒 + 并列/角色词绑定 + 只减不增。

用户口径：
1. 否定 / 疑问 / 假设 / 转述 四类必须**拒绝**，不是降级为 candidate；
2. 主宾门任何情况下不得新增边；
3. 并列主语按 SYMMETRIC_RELS 只存一条无序边。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg import generated_extract as ge  # noqa: E402
from app.kg.generated_extract import extract_from_text  # noqa: E402

NODES = [
    {"name": name, "kind": "character", "aliases": aliases, "attrs": {}}
    for name, aliases in [
        ("许七安", []), ("宋卿", []), ("怀庆", ["怀庆公主"]),
        ("临安", ["临安公主"]), ("魏渊", []), ("神殊", []),
        ("宋廷风", []), ("朱广孝", []), ("李玉春", []), ("杨砚", []),
        ("褚采薇", []), ("浮香", []), ("元景帝", []), ("李妙真", []),
        ("金莲", []), ("张巡抚", []),
    ]
]


def _edge(a: str, b: str, rel: str) -> dict:
    return {"from": a, "to": b, "rel": rel, "strength": 0.8, "status": "resolved"}


def _run(text: str, edges: list[dict], chapter: int = 1) -> dict:
    return extract_from_text(text, chapter, NODES, edges)


# ---------------------------------------------------------------------------
# 1. 四类硬拒：不产边，也不进候选
# ---------------------------------------------------------------------------
def test_question_context_is_rejected_not_downgraded():
    out = _run("许七安的炼金术真的高深到这个地步了？能指点宋卿？",
               [_edge("许七安", "宋卿", "mentor")], 91)
    assert out["edges"] == []
    assert out["candidates"] == []
    assert [row["reason"] for row in out["rejected_edges"]] == ["question"]


def test_negated_clause_is_rejected_not_downgraded():
    out = _run("怀庆和临安两位公主站在远处，并没有和许七安并肩。",
               [_edge("怀庆", "许七安", "ally")])
    assert out["edges"] == []
    assert out["candidates"] == []
    assert [row["reason"] for row in out["rejected_edges"]] == ["negated"]


def test_hypothetical_sentence_is_rejected_not_downgraded():
    out = _run("如果世上还有谁能成为许七安的老师，非神殊莫属。",
               [_edge("许七安", "神殊", "mentor")])
    assert out["edges"] == []
    assert out["candidates"] == []
    assert [row["reason"] for row in out["rejected_edges"]] == ["hypothetical"]


def test_reported_hearsay_is_rejected_not_downgraded():
    out = _run("据说许七安是魏渊的人。", [_edge("许七安", "魏渊", "subordinate")])
    assert out["edges"] == []
    assert out["candidates"] == []
    assert [row["reason"] for row in out["rejected_edges"]] == ["reported"]


# ---------------------------------------------------------------------------
# 2. 并列主语：SYMMETRIC_RELS 只存一条无序边
# ---------------------------------------------------------------------------
def test_coordinated_subject_stores_one_symmetric_edge():
    out = _run("怀庆和许七安联手造反。",
               [_edge("怀庆", "许七安", "ally"), _edge("许七安", "怀庆", "ally")])
    assert len(out["edges"]) == 1
    edge = out["edges"][0]
    assert edge["direction_basis"] == "symmetric"
    assert {edge["from"], edge["to"]} == {"怀庆", "许七安"}


# ---------------------------------------------------------------------------
# 3. 只减不增
# ---------------------------------------------------------------------------
def test_gate_only_subtracts_edges(monkeypatch):
    text = ("宋廷风和朱广孝察觉到了同僚的异常，"
            "被许七安毫无血色的脸庞吓了一跳。")
    edges = [_edge("宋廷风", "朱广孝", "ally"), _edge("宋廷风", "许七安", "ally")]
    gated = _run(text, edges)
    monkeypatch.setattr(ge, "_attribution_gate", lambda *a, **k: (True, ""))
    ungated = _run(text, edges)

    assert len(gated["edges"]) <= len(ungated["edges"])
    assert ({(e["from"], e["to"], e["rel"]) for e in gated["edges"]}
            <= {(e["from"], e["to"], e["rel"]) for e in ungated["edges"]})


# ---------------------------------------------------------------------------
# 4. 306 章那批的实际绑定行为
# ---------------------------------------------------------------------------
def test_colleague_noun_binds_holder_to_referent():
    """「同僚」是第三人：宋、朱是持有者，许七安才是被指代的人。"""
    text = ("宋廷风和朱广孝察觉到了同僚的异常，"
            "被许七安毫无血色的脸庞吓了一跳。")
    out = _run(text, [_edge("宋廷风", "朱广孝", "ally"),
                      _edge("宋廷风", "许七安", "ally")], 97)
    assert {(e["from"], e["to"]) for e in out["edges"]} == {("宋廷风", "许七安")}


def test_copular_colleague_statement_is_kept():
    out = _run("许七安与张巡抚是同僚。", [_edge("许七安", "张巡抚", "ally")])
    assert len(out["edges"]) == 1


def test_role_verb_rejects_when_object_slot_holds_another_person():
    text = "太傅教过元景帝，教过怀庆临安，现在又要教导皇室新生代。"
    out = _run(text, [_edge("元景帝", "怀庆", "mentor")], 619)
    assert out["edges"] == []
    assert [row["reason"] for row in out["candidates"]] == ["weak_subject_object"]


def test_tutoring_binds_nearest_object_only():
    text = "许七安授业临安，以及和浮香偷偷摸摸的枯燥生活。"
    out = _run(text, [_edge("许七安", "临安", "mentor"),
                      _edge("许七安", "浮香", "mentor")], 855)
    assert {(e["from"], e["to"]) for e in out["edges"]} == {("许七安", "临安")}
    assert [row["reason"] for row in out["candidates"]] == ["endpoint_mismatch"]


def test_postposition_role_binds_owner():
    out = _run("许七安依旧在杨砚麾下。", [_edge("许七安", "杨砚", "subordinate")], 248)
    assert len(out["edges"]) == 1


def test_quote_speaker_and_pronoun_are_resolved():
    text = "许七安无奈道：“是的，临安公主非要我投靠她，给她做牛做马。”"
    out = _run(text, [_edge("许七安", "临安", "subordinate")], 127)
    assert len(out["edges"]) == 1


# ---------------------------------------------------------------------------
# 5. candidate 不能漏到下游：不进渲染、不进 conflict_scan、不升成正式边
# ---------------------------------------------------------------------------
def _canon_db(tmp_path, nodes: list[dict], edges: list[dict]):
    """建一个只含 new_nodes/new_edges 的最小原著层库。"""
    import json
    import sqlite3

    db = tmp_path / "canon.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE new_nodes (id INTEGER PRIMARY KEY, kind TEXT, canonical_name TEXT,
            aliases TEXT, attrs TEXT, first_chapter INT, last_chapter INT, chapters TEXT,
            mentions INT, source TEXT, review_status TEXT);
        CREATE TABLE new_edges (id INTEGER PRIMARY KEY, from_name TEXT, to_name TEXT,
            from_kind TEXT, to_kind TEXT, rel_type TEXT, strength REAL, chapters TEXT,
            status TEXT);
        """
    )
    for node in nodes:
        con.execute(
            "INSERT INTO new_nodes (kind, canonical_name, aliases, attrs, mentions,"
            " review_status) VALUES (?, ?, ?, ?, ?, 'active')",
            (node["kind"], node["name"], "|".join(node.get("aliases") or []),
             json.dumps(node.get("attrs") or {}, ensure_ascii=False), 1),
        )
    for edge in edges:
        con.execute(
            "INSERT INTO new_edges (from_name, to_name, rel_type, strength, status)"
            " VALUES (?, ?, ?, ?, 'resolved')",
            (edge["from"], edge["to"], edge["rel"], edge.get("strength", 0.6)),
        )
    con.commit()
    con.close()
    return db


def test_candidate_never_becomes_rendered_edge(tmp_path):
    """共现但无触发词 → 只落 candidate；渲染里不得出现这条生成层边。"""
    from app.kg import generated_store as store
    from app.kg.layer_context import build_context
    from app.kg.layer_pipeline import ingest_chapter

    canon_db = _canon_db(tmp_path, NODES, [_edge("许七安", "张巡抚", "ally")])
    gen = store.connect(tmp_path / "gen.db")
    store.ensure_schema(gen)
    try:
        text = "许七安把急报翻到最后一页，正文后面又附了一行小字，是张巡抚的亲笔。"
        result = ingest_chapter(gen, canon_db, text, 236)
        assert result["edges"] == 0                      # 没产正式边
        assert result["candidates"] >= 1                # 落进候选
        assert store.load_candidates(gen)                # 候选表里确实有
        assert not any(e["from_name"] == "许七安" and e["to_name"] == "张巡抚"
                       for e in store.load_edges(gen))   # 没升成正式边

        ctx = build_context(["许七安"], canon_db, gen, chapter=237, budget_chars=800)
        # 生成层边行形如「- 第 N 章写过：A —rel→ B」；候选不得出现在任何一条里。
        # 注意：原著层关系段本来就会渲染这条 canon 边（形如「- A —ally→ B（强度…）」），
        # 那是原著事实，不是候选泄漏，所以只查「章写过」的生成层边行。
        assert not any("章写过" in line and "许七安" in line and "张巡抚" in line
                       for line in ctx["text"].splitlines())
        # ctx["edges"] 只装原著层边，不含 generated 来源
        assert all(e.get("source") != "generated" for e in ctx["edges"])
    finally:
        gen.close()


def test_downstream_never_reads_candidates(tmp_path, monkeypatch):
    """渲染与 conflict_scan 都不许读 gen_candidates：读就抛错。"""
    import inspect

    from app.kg import generated_store as store
    from app.kg.conflict_scan import scan_text
    from app.kg.layer_context import build_context
    from app.kg.layer_pipeline import ingest_chapter

    # 结构性保证：conflict_scan 的入参里没有 candidates
    assert "candidates" not in inspect.signature(scan_text).parameters

    def _boom(*args, **kwargs):
        raise AssertionError("candidates 被下游读取")

    monkeypatch.setattr(store, "load_candidates", _boom)

    canon_db = _canon_db(tmp_path, NODES, [_edge("许七安", "张巡抚", "ally")])
    gen = store.connect(tmp_path / "gen.db")
    store.ensure_schema(gen)
    try:
        text = "许七安把急报翻到最后一页，正文后面又附了一行小字，是张巡抚的亲笔。"
        ingest_chapter(gen, canon_db, text, 236)        # 内含 scan_text
        build_context(["许七安"], canon_db, gen, chapter=237, budget_chars=800)
    finally:
        gen.close()
