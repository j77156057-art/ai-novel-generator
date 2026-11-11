# -*- coding: utf-8 -*-
"""实体层 / 提及层的书维度验收（2026-11-11 补写侧与提及层）。

锁死五件事：

1. **分槽规则**：实体唯一键 = ``(project_id, kind, canonical_name,
   COALESCE(novel_id, -1))``——同名不同书是两行，同书重复仍是 upsert 到一行；
2. **NULL 语义**：``novel_id IS NULL`` 是项目级通配；写侧带书写入时优先命中
   真书行、其次 NULL 行，**绝不新建重复**（单书场景行为不变的前提）；
3. **提及按书**：``_active_item_entries`` 传书时只返回「本书 + NULL」的提及，
   不传书时不过滤（与加列前逐字一致）；
4. **写侧落书**：``_persist_extraction`` / ``_index_mentions_only`` 接收调用方
   算好的 ``book``（``_run_batches_locked`` 用 ``book_for_project`` 取，唯一
   书来源）；``_index_mentions_only`` 的候选实体按书收窄，新 mention 落
   「当前书」；``_lookup_entity_id`` 跨书反查不串；
5. **组解析签名**：``resolve_group_id`` / ``record_holding`` 新增
   ``project_id`` 收窄——这是签名，不是生产链路（§I 有说明）。

真实库一行不动：全程内存库 / ``tmp_path``。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base  # noqa: E402
from app.generation import engine as gen_engine  # noqa: E402
from app.kg import generated_store as store  # noqa: E402
from app.kg.batch_worker import (  # noqa: E402
    _QueueItem,
    _index_mentions_only,
    _lookup_entity_id,
    _upsert_entity,
)
from app.models import KgEntity, KgMention, Project  # noqa: E402


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _project(db, *, active=None, continuation=None) -> Project:
    proj = Project(title="书维度测试", active_novel_id=active,
                   continuation_novel_id=continuation)
    db.add(proj)
    db.commit()
    return proj


def _entity(db, pid, name, kind="item") -> KgEntity:
    return _upsert_entity(db, pid, {"kind": kind, "canonical_name": name})


# ---------------------------------------------------------------------------
# 1. 分槽规则
# ---------------------------------------------------------------------------
def test_existing_null_row_is_reused_by_book_write():
    db = _db()
    proj = _project(db, active=5)
    a = _entity(db, proj.id, "镇国剑")
    db.flush()
    assert a.novel_id is None
    b = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=5)
    db.commit()
    # 已有 NULL 通配行时，带书写入优先复用通配行——不新建重复（行为不变）
    assert b.id == a.id
    assert db.scalar(select(KgEntity).where(KgEntity.id == a.id)).novel_id is None


def test_same_name_across_books_are_two_entities():
    db = _db()
    proj = _project(db, active=5)
    a = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=5)
    b = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=6)
    db.commit()
    assert a.id != b.id
    books = sorted(e.novel_id for e in db.scalars(select(KgEntity)))
    assert books == [5, 6]


def test_same_name_same_book_is_idempotent():
    db = _db()
    proj = _project(db, active=5)
    a = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=5)
    b = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=5)
    db.commit()
    assert a.id == b.id
    assert len(db.scalars(select(KgEntity)).all()) == 1


def test_unique_index_collapses_null_into_minus_one():
    """两行同名 NULL 通配实体必须被唯一键挡住（NULL 在唯一约束里互不相等）。"""
    db = _db()
    proj = _project(db)
    db.add(KgEntity(project_id=proj.id, kind="item", canonical_name="镇国剑",
                    novel_id=None))
    db.commit()
    db.add(KgEntity(project_id=proj.id, kind="item", canonical_name="镇国剑",
                    novel_id=None))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# 2. 跨书反查 / 写侧候选
# ---------------------------------------------------------------------------
def test_lookup_entity_id_does_not_cross_books():
    db = _db()
    proj = _project(db, active=6)
    a = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=5)
    b = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                       novel_id=6)
    db.commit()
    assert _lookup_entity_id(db, proj.id, "镇国剑", novel_id=5) == a.id
    assert _lookup_entity_id(db, proj.id, "镇国剑", novel_id=6) == b.id
    # 不传书 = 旧行为（解析到确定性的一行）
    assert _lookup_entity_id(db, proj.id, "镇国剑") in (a.id, b.id)


def test_alias_mentions_take_project_book():
    """降级 mention：候选按书收窄，新行落「当前书」（书来源由调用方算好传入）。"""
    db = _db()
    proj = _project(db, active=6)
    _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "镇国剑"},
                   novel_id=5)
    target = _upsert_entity(db, proj.id, {"kind": "item",
                                          "canonical_name": "镇国剑"},
                            novel_id=6)
    db.commit()
    batch = [_QueueItem(project_id=proj.id, segment_no=1,
                        text="他握着镇国剑，一言不发。")]
    added = _index_mentions_only(db, proj.id, batch, book=6)
    assert added == 1
    row = db.scalars(select(KgMention)).one()
    assert row.entity_id == target.id
    assert row.novel_id == 6


# ---------------------------------------------------------------------------
# 3. 读侧提及按书
# ---------------------------------------------------------------------------
def _mention(db, pid, eid, seg, novel_id):
    db.add(KgMention(project_id=pid, entity_id=eid, novel_id=novel_id,
                     segment_no=seg, context_window="x", mention_kind="llm"))


def test_active_item_entries_filters_by_book():
    db = _db()
    proj = _project(db, continuation=6)
    e5 = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "甲剑"},
                        novel_id=5)
    e6 = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "乙剑"},
                        novel_id=6)
    en = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "丙剑"})
    _mention(db, proj.id, e5.id, 1, 5)
    _mention(db, proj.id, e6.id, 1, 6)
    _mention(db, proj.id, en.id, 1, None)
    db.commit()

    names = {x["name"] for x in gen_engine._active_item_entries(db, proj, 1)}
    # 续写书 6：只看到书 6 + 项目级通配，书 5 的提及不可见
    assert names == {"乙剑", "丙剑"}


def test_active_item_entries_without_book_sees_everything():
    db = _db()
    proj = _project(db)
    e5 = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "甲剑"},
                        novel_id=5)
    e6 = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "乙剑"},
                        novel_id=6)
    _mention(db, proj.id, e5.id, 1, 5)
    _mention(db, proj.id, e6.id, 1, 6)
    db.commit()

    names = {x["name"] for x in gen_engine._active_item_entries(db, proj, 1)}
    assert names == {"甲剑", "乙剑"}


# ---------------------------------------------------------------------------
# 4. 组解析的 project_id（签名，不是链路）
# ---------------------------------------------------------------------------
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


def _two_project_db(tmp_path) -> pathlib.Path:
    path = tmp_path / "gen.db"
    con = store.connect(path)
    store.ensure_schema(con)
    con.executescript(REGISTRY_DDL)
    for gid, pid in ((100, 5), (200, 6)):
        con.execute(
            "INSERT INTO kg_item_group (id, project_id, canonical_name, status,"
            " generic_members, note, created_at, novel_id)"
            " VALUES (?, ?, '镇国剑', 'active', '[]', '', '2026-11-11', 5)",
            (gid, pid),
        )
    con.commit()
    con.close()
    return path


def test_resolve_group_id_narrows_by_project(tmp_path):
    path = _two_project_db(tmp_path)
    con = store.connect(path)
    try:
        assert store.resolve_group_id(con, "镇国剑", novel_id=5,
                                      project_id=5) == 100
        assert store.resolve_group_id(con, "镇国剑", novel_id=5,
                                      project_id=6) == 200
        # 不存在的项目不猜：返回 None 而不是抽到别人的组
        assert store.resolve_group_id(con, "镇国剑", novel_id=5,
                                      project_id=7) is None
        # 不传 project = 旧行为（同名跨项目时按 pri/gid 取到第一行）
        assert store.resolve_group_id(con, "镇国剑", novel_id=5) == 100
    finally:
        con.close()


def test_record_holding_passes_project_to_group_resolution(tmp_path):
    path = _two_project_db(tmp_path)
    con = store.connect(path)
    try:
        r = store.record_holding(
            con, "镇国剑", "own", "元景帝", 106, item_id=601,
            novel_id=5, project_id=6)
        con.commit()
        assert r["action"] in ("inserted", "confirmed")
        gid = con.execute("SELECT group_id FROM kg_item_holding WHERE id=?",
                          (r["id"],)).fetchone()[0]
        assert gid == 200
    finally:
        con.close()
