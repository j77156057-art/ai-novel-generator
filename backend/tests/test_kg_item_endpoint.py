"""belongs_to 的物品端点：不建正式边，落候选（v2.6 + v3 两条链路同口径）。

判定：belongs_to 只用于人物↔组织、组织↔组织；人物/组织 ↔ 物品是「持有」，
该走 owns。写入侧硬拒物品端点（prompt 只教模型别这么写，这里是兜底），
只拒 belongs_to，owns / located_at 不受影响。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.kg import batch_worker as bw
from app.kg import canon_source
from app.kg import generated_extract as ge
from app.models import KgEntity, KgRelation


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _legacy_source_gate_off(monkeypatch):
    monkeypatch.setattr(settings, "kg_canon_source", "legacy")
    # 本文件只验证物品端点硬规则，把集合门关掉，避免两条判据互相盖住。
    monkeypatch.setattr(settings, "kg_collection_gate", False)
    canon_source.reset_cache()
    yield
    canon_source.reset_cache()


def _result(rel_type: str, to_name: str) -> dict:
    return {
        "entities": [
            {"kind": "character", "canonical_name": "许七安", "aliases": [], "attrs": {}},
            {"kind": "item", "canonical_name": "神剑", "aliases": [], "attrs": {}},
            {"kind": "faction", "canonical_name": "打更人", "aliases": [], "attrs": {}},
        ],
        "relations": [{"from": "许七安", "to": to_name, "rel_type": rel_type,
                       "strength": 0.6}],
        "events": [], "event_links": [], "entity_knowledge": [],
        "state_changes": [], "foreshadows": [],
    }


def _batch(txt: str):
    return [bw._QueueItem(project_id=1, segment_no=1, text=txt, chapter=1)]


def _rel(db, from_name: str, to_name: str, rel: str) -> KgRelation | None:
    ids = {e.canonical_name: e.id for e in db.scalars(select(KgEntity)).all()}
    return db.scalar(select(KgRelation).where(
        KgRelation.project_id == 1,
        KgRelation.from_id == ids[from_name], KgRelation.to_id == ids[to_name],
        KgRelation.rel_type == rel,
    ))


def test_v26_belongs_to_item_endpoint_goes_candidate():
    """原著层：端点为物品的 belongs_to 不落正式边，落 candidate 且 reason 正确。"""
    canon_nodes = [
        {"name": "许七安", "kind": "character", "aliases": []},
        {"name": "神剑", "kind": "item", "aliases": []},
    ]
    canon_edges = [{"from_name": "许七安", "to_name": "神剑", "rel_type": "belongs_to",
                    "status": "resolved", "strength": 0.6}]
    mentioned = {"许七安": {}, "神剑": {}}

    final, cands, rejected = ge._collect_edges(
        "许七安拿起神剑，此剑归他所有。", canon_edges, mentioned, canon_nodes)

    assert final == []
    assert [(c["from"], c["rel"], c["to"], c["reason"]) for c in cands] == [
        ("许七安", "belongs_to", "神剑", "item_endpoint")]
    assert rejected == []


def test_v3_belongs_to_item_endpoint_goes_candidate(db):
    """v3 写入：端点为物品的 belongs_to 落 candidate，reason 记在 description。"""
    bw._persist_extraction(db, 1, _result("belongs_to", "神剑"), _batch("许七安拿起神剑。"))

    row = _rel(db, "许七安", "神剑", "belongs_to")
    assert row is not None
    assert row.status == "candidate"
    assert row.description == "item_endpoint"


def test_v3_owns_item_endpoint_stays_active(db):
    """只拒 belongs_to：同样的物品端点，owns 照常写正式边。"""
    bw._persist_extraction(db, 1, _result("owns", "神剑"), _batch("许七安拿起神剑。"))

    row = _rel(db, "许七安", "神剑", "owns")
    assert row is not None
    assert row.status == "active"
    assert row.description is None


def test_v3_belongs_to_without_item_endpoint_stays_active(db):
    """人物 → 组织 的 belongs_to 不受影响（防止硬拒扩大化）。"""
    bw._persist_extraction(db, 1, _result("belongs_to", "打更人"), _batch("许七安加入打更人。"))

    row = _rel(db, "许七安", "打更人", "belongs_to")
    assert row is not None
    assert row.status == "active"
    assert row.description is None
