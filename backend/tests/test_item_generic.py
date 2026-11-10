# -*- coding: utf-8 -*-
"""泛称判据、写入口拦截、子串匹配。"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.kg import canon_source, generated_store as store, item_generic as ig  # noqa: E402
from app.kg import item_holding_read as read  # noqa: E402


def _load_apply_item_group():
    path = BACKEND / "migrations" / "2026-11-11-item-group-v1" / "apply_item_group.py"
    spec = importlib.util.spec_from_file_location("apply_item_group", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 判据本身
# ---------------------------------------------------------------------------
def test_shape_high_for_category_and_bare_material():
    assert ig.classify_name("神兵").level == ig.HIGH
    assert ig.classify_name("神兵").rule == ig.R4
    assert ig.classify_name("铜剑").level == ig.HIGH
    assert ig.classify_name("刀").level == ig.HIGH
    assert ig.classify_name("法宝").level == ig.HIGH


def test_shape_medium_for_alloy_but_not_blocked():
    # 黄铜剑形态上像类名，但这本书里它是唯一指代，只报告不拦。
    assert ig.classify_name("黄铜剑").level == ig.MEDIUM
    assert ig.classify_name("黄铜剑").flagged
    assert ig.alias_verdict("黄铜剑", "神剑", {"神剑", "黄铜剑"}, {"黄铜剑": {"神剑"}})["blocked"] is False


def test_shape_none_for_proper_names():
    assert ig.classify_name("镇国剑").level == ig.NONE
    assert ig.classify_name("太平刀").level == ig.NONE
    assert ig.classify_name("玄武神剑").level == ig.NONE


def test_own_abbreviation_is_not_generic():
    assert ig.is_own_abbreviation("黑金刀", "黑金长刀")
    assert ig.alias_verdict("黑金刀", "黑金长刀", {"黑金长刀", "黑金刀"},
                            {"黑金刀": {"黑金长刀"}})["blocked"] is False


# ---------------------------------------------------------------------------
# 写法登记入口：泛称登不成 approved
# ---------------------------------------------------------------------------
def test_registration_blocks_generic_aliases():
    mod = _load_apply_item_group()
    plan = mod.build_plan([
        {"id": 26, "canonical_name": "神剑",
         "aliases": ["镇国剑", "神兵", "铜剑", "黄铜剑"]},
    ])
    by_surface = {s["surface"]: s for s in plan["surfaces"]}
    assert by_surface["神兵"]["status"] == "pending"
    assert by_surface["铜剑"]["status"] == "pending"
    assert "疑似泛称" in by_surface["神兵"]["note"]
    # 专名与「唯一指代的材质写法」不受影响
    assert by_surface["镇国剑"]["status"] == "approved"
    assert by_surface["黄铜剑"]["status"] == "approved"
    assert {s["surface"] for s in plan["suspected"]} == {"神兵", "铜剑"}


# ---------------------------------------------------------------------------
# count_forms：短写法不再吃更长名字
# ---------------------------------------------------------------------------
def test_count_forms_longest_match_wins():
    canon_source.reset_cache()
    assert canon_source.count_forms("他握着一柄黄铜剑", ["铜剑"]) == 0
    assert canon_source.count_forms("一柄青铜剑", ["铜剑"]) == 0
    assert canon_source.count_forms("一柄铜剑", ["铜剑"]) == 1
    assert canon_source.count_forms("镇国神剑出鞘", ["神剑"]) == 0
    assert canon_source.count_forms("神剑出鞘", ["神剑"]) == 1


def test_guard_injection_is_optional_and_opt_in():
    """守卫只由调用方注入；不注入时行为与注入前完全一致。"""
    canon_source.reset_cache()
    text = "地书碎片亮了，地书也在"
    # 无注入：归一层不读 generated.db，「地书碎片」不在内置守卫里，照旧数 2
    assert canon_source.count_forms(text, ["地书"]) == 2
    # 注入登记层名字：短写法被更长写法遮挡，只数独立出现的 1 次
    assert canon_source.count_forms(text, ["地书"], guards={"地书碎片"}) == 1
    # 也接受 provider 回调（batch_worker 用回调可以按需取）
    assert canon_source.count_forms(
        text, ["地书"], guards=lambda req: {"地书碎片", "地书聊天群"}) == 1
    # 注入无关名字不影响结果
    assert canon_source.count_forms(text, ["地书"], guards={"镇国剑"}) == 2


def test_text_mention_passes_guards_through():
    canon_source.reset_cache()
    assert canon_source.text_mention("地书碎片亮了", "地书") == "地书"
    assert canon_source.text_mention("地书碎片亮了", "地书",
                                     guards={"地书碎片"}) is None


# ---------------------------------------------------------------------------
# 写入口：泛称不返回组，新写入 group_id 为 NULL
# ---------------------------------------------------------------------------
def _gen_con(tmp_path, generic=True):
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    con.executescript(
        """
        CREATE TABLE kg_item_group (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'item',
            canonical_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            generic_members TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, UNIQUE (project_id, kind, canonical_name));
        CREATE TABLE kg_item_surface (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
            surface TEXT NOT NULL, group_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', source TEXT NOT NULL DEFAULT 'human',
            confidence REAL NOT NULL DEFAULT 1.0, instance_label TEXT,
            evidence_chapter INTEGER, evidence TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '', reviewer TEXT, reviewed_at TEXT,
            created_at TEXT NOT NULL, UNIQUE (project_id, surface));
        """
    )
    con.execute("INSERT INTO kg_item_group (id, project_id, canonical_name, status,"
                " generic_members, note, created_at) VALUES (24, 5, '神剑', 'active', '[]', '', 'x')")
    if generic:
        con.execute("INSERT INTO kg_item_group (id, project_id, canonical_name, status,"
                    " generic_members, note, created_at) VALUES (90, 5, '神兵', 'generic',"
                    " '[]', '泛称：类名，不指单件', 'x')")
        con.execute("INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
                    " source, confidence, created_at) VALUES (5, '神兵', 90, 'approved',"
                    " 'migration', 0.9, 'x')")
    con.execute("INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
                " source, confidence, created_at) VALUES (5, '神剑', 24, 'approved',"
                " 'migration', 1.0, 'x')")
    con.commit()
    return con


def test_resolve_group_id_skips_generic():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        con = _gen_con(pathlib.Path(td))
        assert store.resolve_group_id(con, "神剑") == 24
        assert store.resolve_group_id(con, "神兵") is None
        con.close()


def test_record_holding_generic_keeps_group_id_null():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        con = _gen_con(pathlib.Path(td))
        row = store.record_holding(con, "神兵", "own", "元景帝", 98,
                                   owner_kind="character")
        con.commit()
        got = con.execute("SELECT group_id FROM kg_item_holding WHERE id=?",
                          (row["id"],)).fetchone()[0]
        assert got is None
        con.close()


# ---------------------------------------------------------------------------
# 读通道：泛称如实回答「类名，不指单件」
# ---------------------------------------------------------------------------
def test_read_generic_name_returns_no_single_item(tmp_path):
    app_path = tmp_path / "app.db"
    app = sqlite3.connect(app_path)
    app.execute("CREATE TABLE kg_entities (id INTEGER PRIMARY KEY, project_id INTEGER,"
                " kind TEXT, canonical_name TEXT, aliases TEXT)")
    app.execute("INSERT INTO kg_entities VALUES (26, 5, 'item', '神剑',"
                " '[\"镇国剑\", \"神兵\", \"铜剑\", \"黄铜剑\"]')")
    app.commit()
    app.close()

    con = _gen_con(tmp_path)
    store.record_holding(con, "神剑", "own", "元景帝", 106, owner_kind="character")
    con.commit()
    con.close()

    db = Session(create_engine(f"sqlite:///{app_path}"))
    try:
        facts = read.current_item_holdings(["神兵"], db=db, project_id=5,
                                           generated_db=tmp_path / "gen.db")
    finally:
        db.close()
    assert len(facts) == 1
    assert facts[0]["status"] == read.GENERIC
    assert facts[0]["lines"] == []
    assert facts[0]["item_name"] == "神兵"
    text = read.format_holding_facts(facts)
    assert "类名" in text and "没有登记候选" in text
