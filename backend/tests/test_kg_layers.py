"""分层 KG 的最小回归测试（pytest 可直接收集）。"""
from __future__ import annotations

import json
import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg import generated_store as store  # noqa: E402
from app.kg import layer_pipeline, shadow  # noqa: E402
from app.kg.conflict_scan import scan_text  # noqa: E402
from app.kg.generated_extract import extract_from_text  # noqa: E402
from app.kg.layer_context import build_context  # noqa: E402
from app.kg.layers import CANON, GENERATED, prefer, strip_protected, tag_attrs  # noqa: E402

CANON_NODES = [
    {"name": "许七安", "kind": "character", "aliases": ["七安"],
     "attrs": {"belongs_to": "打更人衙门", "type": "新晋铜锣"}, "mentions": 500},
    {"name": "杨千幻", "kind": "character", "aliases": [],
     "attrs": {"type": "司天监四品术士"}, "mentions": 200},
    {"name": "赵县令", "kind": "character", "aliases": [],
     "attrs": {"arc": "死亡", "current_goal": "死刑"}, "mentions": 30},
    {"name": "杨砚", "kind": "character", "aliases": [],
     "attrs": {"belongs_to": "打更人衙门", "type": "严肃男人", "secrets": ["四品武夫"]},
     "mentions": 40},
    {"name": "打更人衙门", "kind": "faction", "aliases": [], "attrs": {}, "mentions": 100},
]
CANON_EDGES = [
    {"from": "许七安", "to": "打更人衙门", "rel": "member_of", "strength": 0.9,
     "status": "resolved"},
    {"from": "许七安", "to": "杨千幻", "rel": "ally", "strength": 0.7,
     "status": "resolved"},
]


def test_tag_and_prefer_keeps_canon_identity():
    tagged = tag_attrs({"realm": "炼神", "note": "x"}, GENERATED, 230)
    assert tagged["_source"] == GENERATED
    merged = prefer({"name": "许七安", "realm": "炼神", "_source": CANON},
                    {"name": "许七安", "realm": "炼精", "note": "已出场"})
    assert merged["realm"] == "炼神"  # canon 身份字段不被覆盖
    assert merged["note"] == "已出场"
    assert strip_protected({"realm": "炼精", "note": "ok"}) == {"note": "ok"}


def test_extract_writes_generated_source_and_evidence(tmp_path):
    text = (
        "许七安与杨千幻结盟，一同走进打更人衙门。\n\n"
        "杨千幻笑道：“你回来了。”\n\n"
        "许七安是司天监的人。\n\n"
        "杨砚已是九品炼精境。"
    )
    result = extract_from_text(text, 230, CANON_NODES, CANON_EDGES)
    names = {n["name"] for n in result["nodes"]}
    assert {"许七安", "杨千幻", "杨砚"} <= names
    assert all(n["source"] == GENERATED for n in result["nodes"])
    assert all(n["source_chapter"] == 230 for n in result["nodes"])
    assert any(e["rel"] == "ally" for e in result["edges"])
    assert any(e["event_type"] == "faction_claim" for e in result["events"])
    assert any(e["event_type"] == "realm_claim" for e in result["events"])
    assert all(e["evidence"] for e in result["events"])

    db = tmp_path / "gen.db"
    con = store.connect(db)
    try:
        store.ensure_schema(con)
        for node in result["nodes"]:
            store.upsert_node(con, node["name"], node["kind"], node["attrs"],
                              230, node["mentions"])
        con.commit()
        rows = con.execute("SELECT source, COUNT(*) FROM gen_nodes GROUP BY source").fetchall()
        assert [tuple(r) for r in rows] == [("generated", len(result["nodes"]))]
    finally:
        con.close()


def test_conflict_scan_three_types():
    text = (
        "赵县令从牢里走出来，笑道：“许七安，好久不见。”\n\n"
        "许七安是司天监的人，抬手按住刀柄。\n\n"
        "杨砚已是九品炼精境，站在门口。"
    )
    conflicts = scan_text(text, 230, CANON_NODES, CANON_EDGES)
    types = {c["conflict_type"] for c in conflicts}
    assert "dead_revival" in types
    assert "faction_mismatch" in types
    assert "realm_regression" in types
    assert all(c["decision"] in {"rewrite", "mark"} for c in conflicts)
    assert all(c["evidence"] for c in conflicts)


def test_cooccurrence_only_becomes_candidate_not_edge():
    nodes = [
        {"name": "许七安", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
        {"name": "张巡抚", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
    ]
    edges = [{"from": "许七安", "to": "张巡抚", "rel": "ally",
              "strength": 0.6, "status": "resolved"}]
    text = "许七安把急报翻到最后一页，正文后面又附了一行小字，是张巡抚的亲笔。"
    result = extract_from_text(text, 236, nodes, edges)
    assert result["edges"] == []
    assert any(c["reason"] == "no_conflict_trigger" for c in result["candidates"])


def test_location_target_is_not_membership_edge():
    nodes = [
        {"name": "许七安", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
        {"name": "许府", "kind": "location", "aliases": [], "attrs": {}, "mentions": 1},
    ]
    belongs = [{"from": "许七安", "to": "许府", "rel": "belongs_to",
                "strength": 0.6, "status": "resolved"}]
    result = extract_from_text("许七安从许府西墙的塌口翻进去。", 236, nodes, belongs)
    assert not any(e["rel"] == "belongs_to" for e in result["edges"])
    assert any(c["reason"] == "target_is_location" for c in result["candidates"])

    located = [{"from": "许七安", "to": "许府", "rel": "located_at",
                "strength": 0.6, "status": "resolved"}]
    result = extract_from_text("许七安进了许府。", 236, nodes, located)
    assert any(e["rel"] == "located_at" for e in result["edges"])


def test_generic_subject_unresolved_and_fragment_alias_merge():
    nodes = [
        {"name": "许七安", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
        {"name": "许铃音", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
        {"name": "魏渊", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
    ]
    text = "七安走进许府。铃音在灯下画画。魏公坐在案后。大人死了。"
    result = extract_from_text(text, 236, nodes, [])
    names = {n["name"] for n in result["nodes"]}
    assert "许七安" in names and "七安" not in names
    assert "许铃音" in names and "铃音" not in names
    assert "魏渊" in names and "魏公" not in names
    assert any(e["subject_status"] == "unresolved"
               and e["raw_subject"] == "大人" for e in result["events"])
    assert any(r["raw_subject"] == "大人" for r in result["rejected_subjects"])


def test_reported_death_claim_is_visible_to_scanner():
    conflicts = scan_text("外头都说许七安死了。", 236, CANON_NODES, CANON_EDGES)
    hits = [c for c in conflicts
            if c["conflict_type"] == "death_claim" and c["subject"] == "许七安"]
    assert hits, "转述型死亡断言不能被静默漏掉"
    assert hits[0]["decision"] == "reported"


def test_explicit_colleague_and_past_role_triggers():
    nodes = [
        {"name": "许七安", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
        {"name": "张巡抚", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
        {"name": "魏渊", "kind": "character", "aliases": [], "attrs": {}, "mentions": 1},
    ]
    edges = [
        {"from": "许七安", "to": "张巡抚", "rel": "ally",
         "strength": 0.5, "status": "resolved"},
        {"from": "许七安", "to": "魏渊", "rel": "subordinate",
         "strength": 0.5, "status": "resolved"},
    ]
    result = extract_from_text("许七安与张巡抚是同僚。许七安曾是魏渊的旧部。", 236, nodes, edges)
    kept = {(e["from"], e["rel"], e["to"]): e for e in result["edges"]}
    ally = kept[("张巡抚", "ally", "许七安")]  # 对称关系按字典序归一
    assert ally["trigger"] == "同僚"
    assert ally["confidence"] >= 0.65  # 同僚不在过去时态小句里，不降置信度

    past = kept[("许七安", "subordinate", "魏渊")]
    assert past["trigger"] == "旧部"
    # 历史关系保留，但置信度下调，避免当当前关系用
    assert past["confidence"] < 0.7


def test_context_has_two_layers_and_budget(tmp_path):
    canon_db = tmp_path / "canon.db"
    import sqlite3

    con = sqlite3.connect(canon_db)
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
    for node in CANON_NODES:
        con.execute(
            "INSERT INTO new_nodes (kind, canonical_name, aliases, attrs, mentions, review_status)"
            " VALUES (?, ?, ?, ?, ?, 'active')",
            (node["kind"], node["name"], "|".join(node["aliases"]),
             json.dumps(node["attrs"], ensure_ascii=False), node["mentions"]),
        )
    for edge in CANON_EDGES:
        con.execute(
            "INSERT INTO new_edges (from_name, to_name, rel_type, strength, status)"
            " VALUES (?, ?, ?, ?, 'resolved')",
            (edge["from"], edge["to"], edge["rel"], edge["strength"]),
        )
    con.commit()
    con.close()

    gen_db = tmp_path / "gen.db"
    gen = store.connect(gen_db)
    store.ensure_schema(gen)
    store.add_event(gen, 230, "incident", "许七安回到打更人衙门", subject="许七安")
    gen.commit()
    gen.close()
    gen = store.connect(gen_db, readonly=True)
    try:
        ctx = build_context(["许七安"], canon_db, gen, chapter=231, budget_chars=800)
    finally:
        gen.close()

    assert "【原著层·实体" in ctx["text"]
    assert "【生成层·已写情节" in ctx["text"]
    assert "打更人衙门" in ctx["text"]
    assert ctx["chars"] <= 800 + 200  # 规则行允许少量超出
    assert ctx["layers"]["canon"] >= 1


def _make_canon_db(tmp_path):
    """建一个只含 new_nodes/new_edges 的最小原著层库。"""
    import sqlite3

    canon_db = tmp_path / "canon.db"
    con = sqlite3.connect(canon_db)
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
    for node in CANON_NODES:
        con.execute(
            "INSERT INTO new_nodes (kind, canonical_name, aliases, attrs, mentions, review_status)"
            " VALUES (?, ?, ?, ?, ?, 'active')",
            (node["kind"], node["name"], "|".join(node["aliases"]),
             json.dumps(node["attrs"], ensure_ascii=False), node["mentions"]),
        )
    for edge in CANON_EDGES:
        con.execute(
            "INSERT INTO new_edges (from_name, to_name, rel_type, strength, status)"
            " VALUES (?, ?, ?, ?, 'resolved')",
            (edge["from"], edge["to"], edge["rel"], edge["strength"]),
        )
    con.commit()
    con.close()
    return canon_db


def _snapshot(con):
    """生成层各表行数 + 提及总数，用于核对重跑是否可复算。"""
    tables = ("gen_nodes", "gen_edges", "gen_events", "gen_candidates",
              "gen_rejected_subjects", "gen_conflicts")
    snap = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    snap["mentions"] = con.execute("SELECT COALESCE(SUM(mentions), 0) FROM gen_nodes").fetchone()[0]
    return snap


def test_ingest_chapter_persists_and_rerun_is_reproducible(tmp_path):
    canon_db = _make_canon_db(tmp_path)
    gen = store.connect(tmp_path / "gen.db")
    store.ensure_schema(gen)
    try:
        text = (
            "赵县令从牢里走出来，笑道：“许七安，好久不见。”\n\n"
            "许七安是司天监的人，抬手按住刀柄。\n\n"
            "杨砚已是九品炼精境，站在门口。"
        )
        first = layer_pipeline.ingest_chapter(gen, canon_db, text, 230)
        snap1 = _snapshot(gen)
        second = layer_pipeline.ingest_chapter(gen, canon_db, text, 230)
        snap2 = _snapshot(gen)

        assert first["conflicts"]
        assert first["by_decision"].get("rewrite"), "死亡复生类冲突必须落到 rewrite"
        assert snap1 == snap2, "同一章重跑必须可复算"
        stored = gen.execute(
            "SELECT COUNT(*) FROM gen_conflicts WHERE chapter = 230").fetchone()[0]
        assert stored == len(second["conflicts"])
        assert gen.execute(
            "SELECT COUNT(*) FROM gen_events WHERE source = 'generated'").fetchone()[0]
    finally:
        gen.close()


def test_prepare_chapter_seeds_from_previous_chapter(tmp_path):
    canon_db = _make_canon_db(tmp_path)
    gen = store.connect(tmp_path / "gen.db")
    store.ensure_schema(gen)
    try:
        layer_pipeline.ingest_chapter(
            gen, canon_db, "许七安与杨千幻结盟，一同走进打更人衙门。", 230)
        ctx = layer_pipeline.prepare_chapter(gen, canon_db, 231, budget_chars=800)
        assert "许七安" in ctx["seeds"]
        assert "许七安" in ctx["text"]
        assert "【原著层·实体" in ctx["text"]
        assert ctx["chars"] <= 800 + 200
    finally:
        gen.close()


def test_shadow_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("KG_SHADOW_ENABLED", raising=False)
    monkeypatch.setenv("KG_SHADOW_DIR", str(tmp_path / "shadow"))
    assert shadow.enabled() is False
    assert shadow.submit(5, "许七安走进打更人衙门。", 1) == {
        "queued": False, "reason": "disabled"}
    assert not shadow.db_path(5).exists()


def test_shadow_submit_writes_separate_db(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_SHADOW_ENABLED", "1")
    monkeypatch.setenv("KG_SHADOW_DIR", str(tmp_path / "shadow"))
    monkeypatch.setenv("KG_SHADOW_CANON_DB", str(_make_canon_db(tmp_path)))
    assert shadow.enabled() is True

    out = shadow.submit(5, "许七安与杨千幻结盟，一同走进打更人衙门。", 230)
    assert out["queued"] is True
    shadow.wait_idle(timeout=10)

    db = shadow.db_path(5)
    assert db.exists()
    con = store.connect(db, readonly=True)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM gen_nodes WHERE source = 'generated'").fetchone()[0] >= 2
        assert con.execute(
            "SELECT COUNT(*) FROM gen_edges").fetchone()[0] >= 1
    finally:
        con.close()
    # 重复提交同一章必须可复算，不膨胀
    shadow.run_now(5, "许七安与杨千幻结盟，一同走进打更人衙门。", 230)
    con = store.connect(db, readonly=True)
    try:
        counts = store.load_nodes(con)
        assert len([n for n in counts if n["name"] in {"许七安", "杨千幻"}]) == 2
    finally:
        con.close()


def test_shadow_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setenv("KG_SHADOW_ENABLED", "1")
    monkeypatch.setenv("KG_SHADOW_DIR", str(tmp_path / "shadow"))
    # 把落库目录指向一个已存在的文件 → 建库必失败，但不得外抛
    bad = tmp_path / "not_a_dir"
    bad.write_text("x", encoding="utf-8")
    monkeypatch.setenv("KG_SHADOW_DIR", str(bad))
    out = shadow.run_now(5, "许七安走进打更人衙门。", 1)
    assert out["ok"] is False
    assert out["error"]
