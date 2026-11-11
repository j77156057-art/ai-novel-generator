# -*- coding: utf-8 -*-
"""读通道按「同物组 + 写法」解析。

口径（与 reports/item-canonical-design.md 第 5 节一致）：

- 输入名字先经 ``kg_item_surface`` 找组，再按组的全部 approved 写法取持有段；
- 找不到组退回原来的规范名/别名解析，不能因为没建组就查不到；
- 一个名字命中多个组/实体时如实报 ``ambiguous``，不静默取第一条；
- 通称（``generic``）不谎称是某一件的别名，只给候选与候选的当前状态。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.kg import generated_store as store  # noqa: E402
from app.kg import item_holding_read as read  # noqa: E402

#: project 5 的真实实体形状精简版（id 与生产库一致，便于对照）。
ITEMS = [
    (9, 5, "item", "地书",
     '["三号碎片", "地书碎片", "地书碎片·三号", "叁号镜子"]'),
    (48, 5, "item", "黑金长刀", '["暗金色长刀", "刀胚"]'),
    (23, 5, "item", "监正出品的长刀", "[]"),
    # 「铜锣」既是独立规范名，又是「法器铜锣」的别名 → 撞名。
    (40, 5, "item", "法器铜锣", '["铜锣"]'),
    (41, 5, "item", "铜锣", "[]"),
]


def _app_session(tmp_path):
    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE kg_entities (id INTEGER PRIMARY KEY, project_id INTEGER, "
        "kind TEXT, canonical_name TEXT, aliases TEXT)"
    )
    con.executemany(
        "INSERT INTO kg_entities (id, project_id, kind, canonical_name, aliases) "
        "VALUES (?, ?, ?, ?, ?)", ITEMS,
    )
    con.commit()
    con.close()
    return Session(create_engine(f"sqlite:///{path}"))


def _group_schema(con):
    con.executescript(
        """
        CREATE TABLE kg_item_group (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            kind TEXT NOT NULL DEFAULT 'item',
            canonical_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            generic_members TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE (project_id, kind, canonical_name)
        );
        CREATE TABLE kg_item_surface (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            surface TEXT NOT NULL,
            group_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            source TEXT NOT NULL DEFAULT 'human',
            confidence REAL NOT NULL DEFAULT 1.0,
            instance_label TEXT,
            evidence_chapter INTEGER,
            evidence TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            reviewer TEXT,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (project_id, surface)
        );
        """
    )


def _group(con, gid, name, status="active", members="[]", note=""):
    con.execute(
        "INSERT INTO kg_item_group (id, project_id, canonical_name, status,"
        " generic_members, note, created_at) VALUES (?, 5, ?, ?, ?, ?, '2026-11-10')",
        (gid, name, status, members, note),
    )


def _surface(con, surface, gid, status="approved", label=None):
    con.execute(
        "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
        " source, confidence, instance_label, created_at)"
        " VALUES (5, ?, ?, ?, 'migration', 1.0, ?, '2026-11-10')",
        (surface, gid, status, label),
    )


def _gen_db(tmp_path, with_registry=True):
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)

    three = store.record_item_instance(con, "地书", "三号", 100)
    store.record_holding(con, "地书", "own", "天地会", 74, owner_kind="faction",
                         reason_code="transfer_event")
    store.record_holding(con, "地书", "custody", "许七安", 108,
                         instance_id=three, owner_kind="character",
                         reason_code="transfer_event")
    store.record_holding(con, "黑金长刀", "own", "许七安", 102,
                         owner_kind="character")
    store.record_holding(con, "监正出品的长刀", "own", "许七安", 227,
                         owner_kind="character")

    if with_registry:
        _group_schema(con)
        _group(con, 9, "地书")
        _group(con, 45, "黑金长刀")
        _group(con, 23, "监正出品的长刀")
        _group(con, 46, "玉石小镜", status="generic", members="[9]",
               note="九块碎片是九面同款镜子（ch596「跌出一件同款玉石小镜」），是类通称，不对应单件")
        _group(con, 47, "长刀", status="generic", members="[45, 23]",
               note="通用兵器名，不指向某一柄")

        _surface(con, "地书", 9)
        _surface(con, "三号碎片", 9, label="三号")
        _surface(con, "黑金长刀", 45)
        _surface(con, "黑金刀", 45)
        _surface(con, "监正出品的长刀", 23)
        _surface(con, "玉石小镜", 46)
        _surface(con, "玉石镜", 46)
        _surface(con, "长刀", 47)
        # 待审写法不作数：这里故意把「刀胚」挂到 23，不应改变黑金长刀的结果。
        _surface(con, "刀胚", 23, status="pending")

    con.commit()
    path = pathlib.Path(con.execute("PRAGMA database_list").fetchone()[2])
    con.close()
    return path


def _read(tmp_path, items, with_registry=True):
    app = _app_session(tmp_path)
    path = _gen_db(tmp_path, with_registry)
    with app:
        return read.current_item_holdings(items, db=app, project_id=5,
                                          generated_db=path)


# ---------------------------------------------------------------------------
# 按组解析
# ---------------------------------------------------------------------------
def test_group_lookup_returns_only_group_level(tmp_path):
    """组级查询只返回组级段：不再夹带「三号（碎片）在许七安手上」。"""
    facts = _read(tmp_path, ["地书"])
    assert [f["instance_label"] for f in facts] == [None]
    assert facts[0]["via"] == "surface" and facts[0]["group_id"] == 9
    assert [l["owner_name"] for l in facts[0]["lines"]] == ["天地会"]
    assert all(l["hold_type"] == "own" for l in facts[0]["lines"])


def test_instance_surface_narrows_to_that_instance(tmp_path):
    facts = _read(tmp_path, ["三号碎片"])
    assert [f["instance_label"] for f in facts] == ["三号"]
    custody = [l for f in facts for l in f["lines"] if l["hold_type"] == "custody"]
    assert [l["owner_name"] for l in custody] == ["许七安"]
    # 碎片级查询不夹带组级 own（天地会），也不带出别的片号
    assert all(l["hold_type"] == "custody" for f in facts for l in f["lines"])
    assert all(f["instance_label"] == "三号" for f in facts)


def _gen_timeline(tmp_path):
    """同一 hold_type 两段：镇北王 99–106（已关）+ 元景帝 106–∞（开放）。"""
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    _group_schema(con)
    _group(con, 50, "镇国剑")
    _surface(con, "镇国剑", 50)
    store.record_holding(con, "镇国剑", "own", "镇北王", 99, reason_code="gift")
    store.record_holding(con, "镇国剑", "own", "元景帝", 106,
                         reason_code="transfer")
    con.commit()
    path = pathlib.Path(con.execute("PRAGMA database_list").fetchone()[2])
    con.close()
    return path


def test_full_timeline_keeps_closed_segment(tmp_path):
    """缺陷修复：同 hold_type 只取最新一行会把「镇北王 99–106」挤掉。"""
    app = _app_session(tmp_path)
    path = _gen_timeline(tmp_path)
    with app:
        facts = read.current_item_holdings(
            ["镇国剑"], db=app, project_id=5, generated_db=path)
    assert len(facts) == 1
    assert [l["owner_name"] for l in facts[0]["lines"]] == ["镇北王", "元景帝"]
    assert facts[0]["status"] == "held"
    text = read.format_holding_facts(facts)
    assert "镇北王（99–106）" in text and "元景帝（106–∞）" in text


def test_alias_surface_maps_to_group_canonical(tmp_path):
    facts = _read(tmp_path, ["黑金刀"])
    assert len(facts) == 1
    assert facts[0]["item_name"] == "黑金长刀"
    assert facts[0]["via"] == "surface"
    assert facts[0]["lines"][0]["owner_name"] == "许七安"


# ---------------------------------------------------------------------------
# 通称：不谎称别名，只给候选
# ---------------------------------------------------------------------------
def test_generic_lists_candidates_without_claiming_alias(tmp_path):
    facts = _read(tmp_path, ["玉石小镜"])
    assert len(facts) == 1
    fact = facts[0]
    assert fact["status"] == read.GENERIC
    assert fact["lines"] == []
    assert fact["item_name"] == "玉石小镜"
    assert [c["item_name"] for c in fact["candidates"]] == ["地书"]
    cand_lines = fact["candidates"][0]["lines"]
    # 通称候选是组级摘要：只给组级 own，碎片级 custody 由「三号碎片」单独查
    assert {l["hold_type"] for l in cand_lines} == {"own"}
    # 候选里同一个 (实例, hold_type, 持有人) 只出现一次
    keys = [(l["hold_type"], l["owner_name"]) for l in cand_lines]
    assert len(keys) == len(set(keys))

    text = read.format_holding_facts(facts)
    assert "通称" in text and "不对应单件" in text
    assert "别名" not in text
    assert "地书（权属（own）归天地会）" in text


def test_generic_with_two_candidates(tmp_path):
    facts = _read(tmp_path, ["长刀"])
    fact = facts[0]
    assert fact["status"] == read.GENERIC
    names = [c["item_name"] for c in fact["candidates"]]
    assert names == ["黑金长刀", "监正出品的长刀"]
    assert all(c["lines"] for c in fact["candidates"])


def test_generic_synonym_is_not_a_single_item(tmp_path):
    """「玉石镜」是「玉石小镜」的同义写法，也不能落到地书这一个单件上。"""
    facts = _read(tmp_path, ["玉石镜"])
    assert facts[0]["status"] == read.GENERIC
    assert [c["item_name"] for c in facts[0]["candidates"]] == ["地书"]


# ---------------------------------------------------------------------------
# 撞名：报歧义，不取第一条
# ---------------------------------------------------------------------------
def test_ambiguous_name_is_reported_not_guessed(tmp_path):
    facts = _read(tmp_path, ["铜锣"])
    assert len(facts) == 1
    fact = facts[0]
    assert fact["status"] == read.AMBIGUOUS
    assert fact["lines"] == []
    assert sorted(c["item_name"] for c in fact["candidates"]) == ["法器铜锣", "铜锣"]
    text = read.format_holding_facts(facts)
    assert "不猜" in text and "法器铜锣" in text and "铜锣" in text


# ---------------------------------------------------------------------------
# 兜底：没有登记表时走原来的规范名/别名解析
# ---------------------------------------------------------------------------
def test_without_registry_falls_back_to_entity_alias(tmp_path):
    facts = _read(tmp_path, ["地书碎片"], with_registry=False)
    assert facts[0]["item_name"] == "地书"
    assert facts[0]["status"] == "held"
    assert facts[0]["via"] != "surface"


# ---------------------------------------------------------------------------
# 查询次数：与物品数量无关，generated.db 固定「登记表 1 次 + 持有段 1 次」
# ---------------------------------------------------------------------------
def test_registry_and_holding_are_constant_queries(tmp_path, monkeypatch):
    app = _app_session(tmp_path)
    path = _gen_db(tmp_path, with_registry=True)

    seen: list[str] = []
    original = read._connect_generated

    def traced(p):
        con = original(p)
        con.set_trace_callback(seen.append)
        return con

    monkeypatch.setattr(read, "_connect_generated", traced)
    with app:
        facts = read.current_item_holdings(
            ["地书", "玉石小镜", "长刀", "铜锣"], db=app, project_id=5,
            generated_db=path,
        )

    # sqlite_master / PRAGMA 的结构检查不算数据查询。
    data_q = [s for s in seen
              if "sqlite_master" not in s and not s.startswith("PRAGMA")]
    registry_q = [s for s in data_q if "FROM kg_item_group" in s]
    holding_q = [s for s in data_q if "FROM kg_item_holding" in s]
    assert len(registry_q) == 1, data_q
    assert len(holding_q) == 1, data_q
    assert len(data_q) == 2, data_q
    assert len(facts) >= 4


# ---------------------------------------------------------------------------
# 写入口：新写入的行自带 group_id，且能直接按组查到
# ---------------------------------------------------------------------------
def test_write_sets_group_id_on_holding_and_story_segment(tmp_path):
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    _group_schema(con)
    _group(con, 45, "黑金长刀")
    _surface(con, "黑金刀", 45)
    con.commit()

    holding_id = store.record_holding(con, "黑金刀", "own", "许七安", 102,
                                      owner_kind="character")["id"]
    row = con.execute("SELECT group_id FROM kg_item_holding WHERE id = ?",
                      (holding_id,)).fetchone()
    assert row["group_id"] == 45

    seg = store.record_story_segment(
        con, "黑金刀", "own", "许七安", 102, holding_id=holding_id,
        order_key=10200, order_basis="explicit")
    seg_row = con.execute(
        "SELECT group_id FROM kg_holding_story_segment WHERE id = ?",
        (seg["id"],)).fetchone()
    assert seg_row["group_id"] == 45

    # 通称组不挂单件：写「长刀」仍是 NULL。
    _group(con, 47, "长刀", status="generic", members="[45]")
    _surface(con, "长刀", 47)
    generic_id = store.record_holding(con, "长刀", "own", "许七安", 103)["id"]
    assert con.execute("SELECT group_id FROM kg_item_holding WHERE id = ?",
                       (generic_id,)).fetchone()["group_id"] is None

    con.commit()
    path = pathlib.Path(con.execute("PRAGMA database_list").fetchone()[2])
    con.close()

    app = _app_session(tmp_path)
    with app:
        facts = read.current_item_holdings(["黑金长刀"], db=app, project_id=5,
                                           generated_db=path)
    assert facts[0]["group_id"] == 45
    assert facts[0]["lines"][0]["owner_name"] == "许七安"


def test_read_by_group_id_even_when_name_is_unregistered(tmp_path):
    """回填/新写行只要带 group_id，名字不认识也能按组查到。"""
    app = _app_session(tmp_path)
    path = _gen_db(tmp_path, with_registry=True)

    con = sqlite3.connect(path)
    cur = con.execute(
        "INSERT INTO kg_item_holding (group_id, item_name, hold_type, owner_name,"
        " owner_kind, owner_state, valid_from, reason_code, evidence_chapter,"
        " evidence, confidence, created_at)"
        " VALUES (45, '没人登记过的写法', 'own', '许七安', 'character', 'held',"
        "         200, 'transfer_event', 200, '', 0.5, '2026-11-10')")
    con.commit()
    con.close()

    with app:
        facts = read.current_item_holdings(["黑金长刀"], db=app, project_id=5,
                                           generated_db=path)
    owners = {l["owner_name"] for l in facts[0]["lines"]}
    assert "许七安" in owners
    assert any(l["valid_from"] == 200 for l in facts[0]["lines"])


