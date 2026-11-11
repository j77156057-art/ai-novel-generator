"""边生命周期：失效/死亡只做减法，且必须过同一套硬拒门。

用户口径：
1. 「差点反目」「并未背叛」「若不是…早就翻脸」不能减边；
2. 「差点死」「险些丧命」「九死一生」不切 historical，只有明确身亡才切；
3. 推翻时保留旧证据：两条证据（建边原句 / 失效原句）同时存在。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg import generated_store as store  # noqa: E402
from app.kg import layer_pipeline  # noqa: E402

NODES = [
    {"name": name, "kind": "character", "aliases": [], "attrs": {}}
    for name in ("许七安", "宋廷风", "朱广孝")
]


def _canon_db(tmp_path):
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
    for node in NODES:
        con.execute(
            "INSERT INTO new_nodes (kind, canonical_name, aliases, attrs, mentions,"
            " review_status) VALUES ('character', ?, '', '{}', 1, 'active')",
            (node["name"],),
        )
    for a, b in (("许七安", "宋廷风"), ("许七安", "朱广孝")):
        con.execute(
            "INSERT INTO new_edges (from_name, to_name, rel_type, strength, status)"
            " VALUES (?, ?, 'ally', 0.8, 'resolved')",
            (a, b),
        )
    con.commit()
    con.close()
    return db


def _setup(tmp_path):
    canon_db = _canon_db(tmp_path)
    gen = store.connect(tmp_path / "gen.db")
    store.ensure_schema(gen)
    # 建边章：用「结盟」把 许七安-宋廷风 立起来。
    layer_pipeline.ingest_chapter(
        gen, canon_db, "许七安与宋廷风在城头结盟，约定共进退。", 10)
    return canon_db, gen


def _edge(gen, a, b):
    for row in store.load_edges(gen):
        if {row["from_name"], row["to_name"]} == {a, b}:
            return row
    raise AssertionError(f"边不存在：{a}-{b}")


# ---------------------------------------------------------------------------
# 1. 未遂 / 否定 / 假设：不切边
# ---------------------------------------------------------------------------
def test_unrealized_conflict_does_not_end_edge(tmp_path):
    canon_db, gen = _setup(tmp_path)
    try:
        result = layer_pipeline.ingest_chapter(
            gen, canon_db, "许七安和宋廷风差点反目，最后还是压住了火气。", 11)
        assert result["invalidations"] == 0
        assert _edge(gen, "许七安", "宋廷风")["status"] == "active"
    finally:
        gen.close()


def test_negated_conflict_does_not_end_edge(tmp_path):
    canon_db, gen = _setup(tmp_path)
    try:
        result = layer_pipeline.ingest_chapter(
            gen, canon_db, "许七安并未背叛宋廷风，流言传到第三日便散了。", 11)
        assert result["invalidations"] == 0
        assert _edge(gen, "许七安", "宋廷风")["status"] == "active"
    finally:
        gen.close()


def test_hypothetical_conflict_does_not_end_edge(tmp_path):
    canon_db, gen = _setup(tmp_path)
    try:
        text = "若不是为了那封信，许七安早就和宋廷风翻脸了。"
        result = layer_pipeline.ingest_chapter(gen, canon_db, text, 11)
        assert result["invalidations"] == 0
        assert _edge(gen, "许七安", "宋廷风")["status"] == "active"
    finally:
        gen.close()


# ---------------------------------------------------------------------------
# 2. 明确推翻：关边，且旧证据不被覆盖
# ---------------------------------------------------------------------------
def test_confirmed_conflict_ends_edge_and_keeps_both_evidence(tmp_path):
    canon_db, gen = _setup(tmp_path)
    try:
        text = "许七安与宋廷风就此反目，再没同席喝过酒。"
        result = layer_pipeline.ingest_chapter(gen, canon_db, text, 11)
        assert result["invalidations"] == 1

        edge = _edge(gen, "许七安", "宋廷风")
        assert edge["status"] == "ended"
        assert edge["valid_to"] == 11
        # 建边原句不被覆盖
        assert "结盟" in edge["established_evidence"]
        assert edge["established_chapter"] == 10
        # 失效是追加记录，两条证据同时存在
        records = store.load_invalidations(gen)
        assert len(records) == 1
        assert records[0]["trigger"] == "反目"
        assert "反目" in records[0]["evidence"]
        assert edge["established_evidence"] != records[0]["evidence"]
    finally:
        gen.close()


# ---------------------------------------------------------------------------
# 3. 死亡：未遂不切，明确身亡才切
# ---------------------------------------------------------------------------
def test_near_death_does_not_mark_historical(tmp_path):
    canon_db, gen = _setup(tmp_path)
    try:
        for text in ("许七安差点死了，被朱广孝从水里拖上来。",
                     "许七安险些丧命，胸口那道伤口翻着白。",
                     "这一夜九死一生，许七安却还笑得出来。"):
            result = layer_pipeline.ingest_chapter(gen, canon_db, text, 11)
            assert result["deaths"] == 0
            assert _edge(gen, "许七安", "宋廷风")["status"] == "active"
    finally:
        gen.close()


def test_confirmed_death_marks_edges_historical(tmp_path):
    canon_db, gen = _setup(tmp_path)
    try:
        result = layer_pipeline.ingest_chapter(
            gen, canon_db, "许七安战死沙场，尸身三日后才被寻回。", 11)
        assert result["deaths"] == 1
        edge = _edge(gen, "许七安", "宋廷风")
        assert edge["status"] == "historical"
        assert edge["valid_to"] == 11
        assert "结盟" in edge["established_evidence"]
        assert store.load_invalidations(gen)[0]["reason"] == "death"
    finally:
        gen.close()
