# -*- coding: utf-8 -*-
"""关系章归属：批内「两端实体名同段共现」定 since_chapter。

锁死 2026-11-15 的修复：旧实现一律取批内最小章，ch19/ch20 并批时 ch20 的
7 条关系全被记到 ch19（质量表「关系」列 ch20=0），且 since_chapter 只降不升，
后几章重抽也拿不回来。

规则：
- 批内恰好一章同时出现两端名字 → 记该章
- 零命中 / 跨章多命中 → 退回批内最小章（保守）
- 同批内改判：老行落在本批另一章且证据指向另一章 → 允许把边还给它自己的章
- 证据指向批外的更早章 → 保留原值（不把真正的早期边往后推）

真实库一行不动：全程内存库。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base  # noqa: E402
from app.kg.batch_worker import (  # noqa: E402
    _QueueItem, _persist_extraction, _relation_chapter, _upsert_entity,
)
from app.models import KgRelation, Project  # noqa: E402


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _project(db) -> Project:
    proj = Project(title="关系章归属测试")
    db.add(proj)
    db.commit()
    return proj


def _batch(seg_texts: list[tuple[int, int, str]]) -> list[_QueueItem]:
    return [_QueueItem(project_id=1, segment_no=seg, chapter=ch, text=txt)
            for seg, ch, txt in seg_texts]


def _entities():
    return [{"kind": "character", "canonical_name": "陈小满"},
            {"kind": "item", "canonical_name": "铜钥匙"}]


def _relation():
    return [{"from": "陈小满", "to": "铜钥匙", "rel_type": "owns", "strength": 0.8}]


def _only_relation(db) -> KgRelation:
    return db.scalars(select(KgRelation)).one()


# ---------------------------------------------------------------------------
# 1. 纯函数
# ---------------------------------------------------------------------------
def test_relation_chapter_unique_hit_wins():
    seg_items = [(36, 19, "陈小满走进楼道。"), (37, 20, "陈小满把铜钥匙揣进兜里。")]
    assert _relation_chapter(("陈小满",), ("铜钥匙",), seg_items, 19) == 20


def test_relation_chapter_zero_hit_falls_back_to_min():
    seg_items = [(36, 19, "楼道里没有人。"), (37, 20, "钥匙在桌上。")]
    assert _relation_chapter(("陈小满",), ("铜钥匙",), seg_items, 19) == 19


def test_relation_chapter_multi_chapter_hit_falls_back_to_min():
    seg_items = [(36, 19, "陈小满拿着铜钥匙。"), (37, 20, "陈小满又看了看铜钥匙。")]
    assert _relation_chapter(("陈小满",), ("铜钥匙",), seg_items, 19) == 19


def test_relation_chapter_same_chapter_multi_segment_is_unique():
    seg_items = [(36, 19, "陈小满拿着铜钥匙。"), (37, 19, "铜钥匙又回到陈小满手里。")]
    assert _relation_chapter(("陈小满",), ("铜钥匙",), seg_items, 19) == 19


# ---------------------------------------------------------------------------
# 2. 落库
# ---------------------------------------------------------------------------
def test_persist_relation_uses_cooccurrence_chapter():
    db = _db()
    proj = _project(db)
    result = {"entities": _entities(), "relations": _relation()}
    _persist_extraction(db, proj.id, result, _batch([
        (36, 19, "陈小满走进楼道，楼道里没有人。"),
        (37, 20, "陈小满把铜钥匙揣进兜里，转身离开。"),
    ]))
    db.commit()
    assert _only_relation(db).since_chapter == 20


def test_persist_relation_without_cooccurrence_keeps_min_chapter():
    db = _db()
    proj = _project(db)
    result = {"entities": _entities(), "relations": _relation()}
    _persist_extraction(db, proj.id, result, _batch([
        (36, 19, "楼道里没有人。"),
        (37, 20, "桌上放着一把钥匙。"),
    ]))
    db.commit()
    assert _only_relation(db).since_chapter == 19


def test_persist_relation_alias_form_counts_as_cooccurrence():
    db = _db()
    proj = _project(db)
    _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "陈小满",
                                 "aliases": ["小满"]})
    _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "铜钥匙"})
    db.commit()
    result = {"entities": [], "relations": _relation()}
    _persist_extraction(db, proj.id, result, _batch([
        (36, 19, "楼道里没有人。"),
        (37, 20, "小满把铜钥匙揣进兜里。"),
    ]))
    db.commit()
    assert _only_relation(db).since_chapter == 20


def test_persist_relation_reassigns_within_same_batch():
    """被上一轮 min() 吸附到 ch19 的边，同批重抽证据只在 ch20 → 改回 ch20。"""
    db = _db()
    proj = _project(db)
    a = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "陈小满"})
    b = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "铜钥匙"})
    db.add(KgRelation(project_id=proj.id, from_id=a.id, to_id=b.id,
                      rel_type="owns", strength=0.8, status="active",
                      since_chapter=19))
    db.commit()

    result = {"entities": [], "relations": _relation()}
    _persist_extraction(db, proj.id, result, _batch([
        (36, 19, "楼道里没有人。"),
        (37, 20, "陈小满把铜钥匙揣进兜里。"),
    ]))
    db.commit()
    assert _only_relation(db).since_chapter == 20


def test_persist_relation_keeps_earlier_chapter_outside_batch():
    """真正的早期边（ch5）不会被后批证据推后。"""
    db = _db()
    proj = _project(db)
    a = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "陈小满"})
    b = _upsert_entity(db, proj.id, {"kind": "item", "canonical_name": "铜钥匙"})
    db.add(KgRelation(project_id=proj.id, from_id=a.id, to_id=b.id,
                      rel_type="owns", strength=0.8, status="active",
                      since_chapter=5))
    db.commit()

    result = {"entities": [], "relations": _relation()}
    _persist_extraction(db, proj.id, result, _batch([
        (36, 19, "楼道里没有人。"),
        (37, 20, "陈小满把铜钥匙揣进兜里。"),
    ]))
    db.commit()
    assert _only_relation(db).since_chapter == 5
