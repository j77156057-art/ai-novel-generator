"""集合/上位端点的边级门：v3 落库链路 + 状态列迁移。

门的设计（2026-11-10 口径）：集合/上位节点**保留**为 canon 节点，只在边上判——
成员/归属类关系落在集合端点、且窗口里没有任何具体人名 → 降 status='candidate'，
不删边、不删节点。这里验证的是「v3 链路真的过门」，不是门函数本身
（纯函数用例在 test_kg_canon_source.py）。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.config import settings
from app.db import Base, _migrate_sqlite_columns
from app.kg import batch_worker as bw
from app.kg import canon_source
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
def _legacy_source(monkeypatch):
    monkeypatch.setattr(settings, "kg_canon_source", "legacy")
    # 门默认关闭，本文件验证的是「打开时链路真的过门」，所以显式打开。
    monkeypatch.setattr(settings, "kg_collection_gate", True)
    canon_source.reset_cache()
    yield
    canon_source.reset_cache()


def _result(person: str | None = None):
    entities = [
        {"kind": "character", "canonical_name": "许七安", "aliases": [], "attrs": {}},
        {"kind": "faction", "canonical_name": "佛门", "aliases": [], "attrs": {}},
        {"kind": "faction", "canonical_name": "妖族", "aliases": [], "attrs": {}},
    ]
    if person:
        entities.append({"kind": "character", "canonical_name": person,
                         "aliases": [], "attrs": {}})
    return {
        "entities": entities,
        "relations": [
            {"from": "许七安", "to": "佛门", "rel_type": "belongs_to", "strength": 0.6},
            {"from": "佛门", "to": "妖族", "rel_type": "enemy", "strength": 0.5},
        ],
        "events": [], "event_links": [], "entity_knowledge": [],
        "state_changes": [], "foreshadows": [],
    }


def _batch(txt: str):
    return [bw._QueueItem(project_id=1, segment_no=1, text=txt, chapter=1)]


def _status(db, from_name: str, to_name: str, rel: str) -> str:
    ids = {e.canonical_name: e.id for e in db.scalars(select(KgEntity)).all()}
    row = db.scalar(select(KgRelation).where(
        KgRelation.project_id == 1,
        KgRelation.from_id == ids[from_name], KgRelation.to_id == ids[to_name],
        KgRelation.rel_type == rel,
    ))
    return row.status if row else "<无行>"


def test_v3_gate_demotes_membership_without_person_in_window(db):
    """集合端点 + 成员类 + 窗口无人名 → candidate（边仍在，不删）。"""
    bw._persist_extraction(db, 1, _result(), _batch("他拜入此门，从此有了归处。"))

    assert _status(db, "许七安", "佛门", "belongs_to") == "candidate"
    # 非成员类关系（enemy）不过门，即使窗口里没有人名
    assert _status(db, "佛门", "妖族", "enemy") == "active"
    # 边没被删
    assert db.scalar(select(KgRelation).where(
        KgRelation.rel_type == "belongs_to")) is not None


def test_v3_gate_passes_when_person_in_same_sentence(db):
    """反向条件（硬条件）：窗口里出现具体人名 → 放行，status=active。"""
    bw._persist_extraction(db, 1, _result(), _batch("许七安拜入佛门。"))

    assert _status(db, "许七安", "佛门", "belongs_to") == "active"


def test_v3_gate_only_upgrades_on_later_evidence(db):
    """同一对实体：先降级、后有人名支撑 → 转正；反向不降级。"""
    bw._persist_extraction(db, 1, _result(), _batch("他拜入此门，从此有了归处。"))
    assert _status(db, "许七安", "佛门", "belongs_to") == "candidate"

    bw._persist_extraction(db, 1, _result(), _batch("许七安拜入佛门。"))
    assert _status(db, "许七安", "佛门", "belongs_to") == "active"

    # 再回到弱上下文，不把已转正的边打回候选
    bw._persist_extraction(db, 1, _result(), _batch("他拜入此门。"))
    assert _status(db, "许七安", "佛门", "belongs_to") == "active"


def test_v26_collect_edges_demotes_without_person(monkeypatch):
    """原著层链路（generated_extract._collect_edges）同样过门：降候选、不落正式边。"""
    from app.kg import generated_extract as ge

    canon_nodes = [
        {"name": "许七安", "kind": "character", "aliases": []},
        {"name": "妖族", "kind": "faction", "aliases": []},
        {"name": "朝野", "kind": "faction", "aliases": []},
    ]
    canon_edges = [{"from_name": "妖族", "to_name": "朝野", "rel_type": "member_of",
                    "status": "resolved", "strength": 0.6, "chapters": "1"}]
    mentioned = {"妖族": {}, "朝野": {}, "许七安": {}}

    # 窗口里没有任何具体人名 → 降 candidate，正式边为空
    final, cands, _rejected = ge._collect_edges(
        "妖族归于朝野，此事天下皆知。", canon_edges, mentioned, canon_nodes)
    assert final == []
    assert [(c["from"], c["rel"], c["to"], c["reason"]) for c in cands] == [
        ("妖族", "member_of", "朝野", canon_source.GATE_REASON)]

    # 同句出现具体人名 → 放行
    final, cands, _rejected = ge._collect_edges(
        "许七安见妖族归于朝野，心中冷笑。", canon_edges, mentioned, canon_nodes)
    assert [(r["from"], r["rel"], r["to"]) for r in final] == [
        ("妖族", "member_of", "朝野")]
    assert cands == []


def test_old_db_gets_status_column_with_active_default(tmp_path):
    """老库补列：已有 kg_relations 行迁移后一律 active，无需清库重灌。"""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE kg_relations (
            id INTEGER PRIMARY KEY, project_id INTEGER, from_id INTEGER,
            to_id INTEGER, rel_type VARCHAR(32)
        );
        INSERT INTO kg_relations VALUES (1, 1, 10, 11, 'belongs_to');
        """
    )
    con.commit()
    con.close()

    engine = create_engine(f"sqlite:///{path.as_posix()}", future=True)
    Base.metadata.create_all(engine)   # 其余表按新结构建；已有的 kg_relations 不动
    _migrate_sqlite_columns(engine)

    with engine.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(kg_relations)"))}
        assert "status" in cols
        assert conn.execute(text(
            "SELECT status FROM kg_relations WHERE id=1")).scalar() == "active"
    engine.dispose()
