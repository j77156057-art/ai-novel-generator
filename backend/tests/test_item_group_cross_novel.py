# -*- coding: utf-8 -*-
"""跨书同名组：读写两侧都必须按书解析（2026-11-11「切读侧」的验收测试）。

真实库只有一本书建了组，「行为不变」证明不了切换成立。这里在**临时库**里造
同一个项目下的第二本书，让它和第一本书有同名组 ``镇国剑``，锁死四件事：

1. ``resolve_group_id`` 按书解析命中各自那本；不传书时**不猜**（返回 ``None``），
   不许再用 ``ORDER BY pri, gid LIMIT 1`` 抽到另一本；
2. ``current_item_holdings`` 按书查询不返回另一本的持有行；
3. ``novel_id IS NULL`` 的项目级泛称组在任一本书下都可见，且不算跨书冲突；
4. 单书场景下「传书」与「不传书」结果逐字一致。

真实库一行不动：全程 ``tmp_path``。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg import generated_store as store  # noqa: E402
from app.kg import item_holding_read as read  # noqa: E402

#: 迁移后的登记表形态（含扩到书维度的两个唯一键）。
REGISTRY_DDL = """
CREATE TABLE kg_item_group (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'item',
    canonical_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    generic_members TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    novel_id INTEGER
);
CREATE UNIQUE INDEX ux_item_group_project_novel_name
    ON kg_item_group (project_id, kind, COALESCE(novel_id, -1), canonical_name);
CREATE TABLE kg_item_surface (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    surface TEXT NOT NULL,
    group_id INTEGER NOT NULL REFERENCES kg_item_group(id),
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
    novel_id INTEGER
);
CREATE UNIQUE INDEX ux_item_surface_project_novel_surface
    ON kg_item_surface (project_id, COALESCE(novel_id, -1), surface);
CREATE INDEX ix_item_surface_group ON kg_item_surface(group_id);
"""


def _group(con, gid, name, novel_id, status="active", members="[]", note=""):
    con.execute(
        "INSERT INTO kg_item_group (id, project_id, canonical_name, status,"
        " generic_members, note, created_at, novel_id)"
        " VALUES (?, 5, ?, ?, ?, ?, '2026-11-11', ?)",
        (gid, name, status, members, note, novel_id),
    )


def _surface(con, surface, gid, novel_id, status="approved"):
    con.execute(
        "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
        " source, confidence, created_at, novel_id)"
        " VALUES (5, ?, ?, ?, 'migration', 1.0, '2026-11-11', ?)",
        (surface, gid, status, novel_id),
    )


def _path(con) -> pathlib.Path:
    return pathlib.Path(con.execute("PRAGMA database_list").fetchone()[2])


def _two_book_db(tmp_path) -> pathlib.Path:
    """项目 5 下的两本书：book5 与 book6 都有「镇国剑」，另有一个 NULL 泛称组。"""
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    con.executescript(REGISTRY_DDL)

    _group(con, 100, "镇国剑", 5)
    _group(con, 200, "镇国剑", 6)
    # 项目级通配：novel_id IS NULL，成员是 book5 的镇国剑组
    _group(con, 300, "玉石小镜", None, status="generic", members="[100]",
           note="类通称，不对应单件")
    _surface(con, "镇国剑", 100, 5)
    _surface(con, "镇国剑", 200, 6)
    _surface(con, "玉石小镜", 300, None)

    # 写侧按书解析组；item_id 不同，避开开放唯一键（持有行还没有书维度）。
    store.record_holding(con, "镇国剑", "own", "镇北王", 99, item_id=501,
                         novel_id=5)
    store.record_holding(con, "镇国剑", "own", "元景帝", 106, item_id=601,
                         novel_id=6)
    con.commit()
    path = _path(con)
    con.close()
    return path


def _one_book_db(tmp_path) -> pathlib.Path:
    """单书场景（与真实库同形）：只有 book5 有组，用来对照「行为不变」。"""
    con = store.connect(tmp_path / "one.db")
    store.ensure_schema(con)
    con.executescript(REGISTRY_DDL)
    _group(con, 100, "镇国剑", 5)
    _surface(con, "镇国剑", 100, 5)
    store.record_holding(con, "镇国剑", "own", "镇北王", 99, item_id=501,
                         novel_id=5)
    con.commit()
    path = _path(con)
    con.close()
    return path


def _read(path, name, novel_id):
    return read.current_item_holdings(
        [name], project_id=5, generated_db=path, novel_id=novel_id)


# ---------------------------------------------------------------------------
# 写侧：按书解析
# ---------------------------------------------------------------------------
def test_resolve_group_id_hits_its_own_book(tmp_path):
    path = _two_book_db(tmp_path)
    con = store.connect(path)
    try:
        assert store.resolve_group_id(con, "镇国剑", novel_id=5) == 100
        assert store.resolve_group_id(con, "镇国剑", novel_id=6) == 200
        # 不传书时横跨两本：不猜（旧实现会按 pri,gid 抽到 100）
        assert store.resolve_group_id(con, "镇国剑") is None
    finally:
        con.close()


def test_unique_key_allows_cross_book_but_blocks_same_book(tmp_path):
    path = _two_book_db(tmp_path)
    con = store.connect(path)
    try:
        _group(con, 201, "镇国剑", 6)  # 第二本再建同名：唯一键应拒
        raise AssertionError("同书同名组不该插得进去")
    except sqlite3.IntegrityError:
        pass
    finally:
        con.rollback()
        con.close()


# ---------------------------------------------------------------------------
# 读侧：按书取，不跨书
# ---------------------------------------------------------------------------
def test_read_book5_does_not_return_book6_rows(tmp_path):
    path = _two_book_db(tmp_path)
    facts = _read(path, "镇国剑", 5)
    assert len(facts) == 1
    assert facts[0]["status"] == read.HELD
    assert facts[0]["group_id"] == 100
    assert [line["owner_name"] for line in facts[0]["lines"]] == ["镇北王"]


def test_read_book6_does_not_return_book5_rows(tmp_path):
    path = _two_book_db(tmp_path)
    facts = _read(path, "镇国剑", 6)
    assert len(facts) == 1
    assert facts[0]["group_id"] == 200
    assert [line["owner_name"] for line in facts[0]["lines"]] == ["元景帝"]


def test_unscoped_read_reports_ambiguity_instead_of_picking(tmp_path):
    path = _two_book_db(tmp_path)
    facts = _read(path, "镇国剑", None)
    assert facts[0]["status"] == read.AMBIGUOUS
    assert facts[0]["group_id"] is None
    assert facts[0]["lines"] == []


# ---------------------------------------------------------------------------
# NULL 语义：项目级通配，不被书过滤掉，也不算跨书冲突
# ---------------------------------------------------------------------------
def test_null_generic_group_visible_under_every_book(tmp_path):
    path = _two_book_db(tmp_path)
    facts5 = _read(path, "玉石小镜", 5)
    facts6 = _read(path, "玉石小镜", 6)
    for facts in (facts5, facts6):
        assert len(facts) == 1
        assert facts[0]["status"] == read.GENERIC
        assert facts[0]["group_id"] == 300
        assert [c["group_id"] for c in facts[0]["candidates"]] == [100]
    assert [line["owner_name"]
            for line in facts5[0]["candidates"][0]["lines"]] == ["镇北王"]
    # 成员组是 book5 的：在 book6 下泛称组仍在，但不把 book5 的持有行带出来。
    assert facts6[0]["candidates"][0]["lines"] == []


# ---------------------------------------------------------------------------
# 单书：传书与不传书逐字一致（真实验收口径）
# ---------------------------------------------------------------------------
def test_single_book_result_identical_with_or_without_novel(tmp_path):
    path = _one_book_db(tmp_path)
    assert _read(path, "镇国剑", None) == _read(path, "镇国剑", 5)
