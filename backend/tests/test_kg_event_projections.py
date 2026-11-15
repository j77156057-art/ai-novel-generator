# -*- coding: utf-8 -*-
"""事件派生表投影（2026-11-20）。

背景：``kg_character_state`` / ``kg_foreshadows`` / ``kg_entity_knowledge`` /
``kg_relation_evolution`` 四张表此前只依赖抽取器原样返回的字段，模型几乎不产出，
线上长期 0 行（读侧 judge / retrieval / bible 永远查空）。本测试锁住：

1. 事件自带 location/subject → 位置状态行；death → alive=false；
2. foreshadow_plant / payoff 事件 → 伏笔状态机（同一标签只一行，前后事件都挂上）；
3. revelation 事件 → 主体得知客体；
4. 关系演化：新建边落基线行，章号取「同时涉及两端」的事件；
5. 同一地点重复出现不重复落行。
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
from app.kg import batch_worker as bw  # noqa: E402
from app.kg.extractor_v3 import ExtractionStats  # noqa: E402
from app.models import (  # noqa: E402
    KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow,
    KgRelation, KgRelationEvolution, Project,
)


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _fixture(db):
    proj = Project(title="事件投影测试")
    db.add(proj)
    db.commit()
    pid = proj.id
    hero = KgEntity(project_id=pid, kind="character", canonical_name="许七安")
    place = KgEntity(project_id=pid, kind="location", canonical_name="云鹿书院")
    other = KgEntity(project_id=pid, kind="character", canonical_name="李玉春")
    db.add_all([hero, place, other])
    db.commit()
    events = [
        KgEvent(project_id=pid, sequence=1, chapter=3, segment_no=11,
                event_type="incident", summary="许七安抵达云鹿书院",
                subject_id=hero.id, location_id=place.id),
        KgEvent(project_id=pid, sequence=2, chapter=3, segment_no=11,
                event_type="foreshadow_plant", summary="地书碎片来历不明",
                subject_id=hero.id),
        KgEvent(project_id=pid, sequence=3, chapter=4, segment_no=12,
                event_type="foreshadow_payoff", summary="地书碎片来历不明"),
        KgEvent(project_id=pid, sequence=4, chapter=4, segment_no=12,
                event_type="revelation", summary="许七安得知李玉春身份",
                subject_id=hero.id, object_id=other.id),
        KgEvent(project_id=pid, sequence=5, chapter=5, segment_no=13,
                event_type="death", summary="李玉春身死", subject_id=other.id),
    ]
    db.add_all(events)
    rel = KgRelation(project_id=pid, from_id=hero.id, to_id=other.id,
                     rel_type="ally", strength=0.6)
    db.add(rel)
    db.commit()
    return pid, hero, place, other, events, rel


def _project(db, pid, events, rel, stats=None):
    stats = stats or ExtractionStats()
    bw._project_event_facts(
        db, project_id=pid, events=[(None, e) for e in events],
        name_of=lambda eid: (db.get(KgEntity, eid).canonical_name
                             if eid is not None else None),
        rel_evo_pending=[(rel, None, 0.6, "批抽建立")], stats=stats)
    db.commit()
    return stats


def test_projection_writes_state_foreshadow_knowledge_and_evolution():
    db = _db()
    pid, hero, place, other, events, rel = _fixture(db)
    stats = _project(db, pid, events, rel)

    states = db.scalars(select(KgCharacterState)).all()
    by_type = {(s.character_id, s.state_type): s for s in states}
    loc = by_type[(hero.id, "location")]
    assert loc.new_value == "云鹿书院"
    assert loc.cause_event_id == events[0].id
    assert loc.chapter == 3
    alive = by_type[(other.id, "alive")]
    assert alive.new_value == "false"
    assert alive.cause_event_id == events[4].id

    fs = db.scalar(select(KgForeshadow).where(KgForeshadow.project_id == pid))
    assert fs is not None
    assert fs.name == "地书碎片来历不明"
    assert fs.status == "paid_off"
    assert fs.planted_event_id == events[1].id
    assert fs.payoff_event_id == events[2].id
    assert fs.planted_chapter == 3 and fs.actual_payoff_chapter == 4

    kn = db.scalars(select(KgEntityKnowledge)).all()
    assert len(kn) == 1
    assert kn[0].entity_id == hero.id and kn[0].known_entity_id == other.id
    assert kn[0].learned_at_event_id == events[3].id

    evo = db.scalars(select(KgRelationEvolution)).all()
    assert len(evo) == 1
    assert evo[0].edge_id == rel.id
    assert evo[0].strength == 0.6
    # 章号取「同时涉及两端」的 revelation 事件（第 4 章）
    assert evo[0].chapter == 4
    assert stats.state_changes >= 2 and stats.foreshadows >= 2


def test_projection_does_not_duplicate_unchanged_location():
    db = _db()
    pid, hero, place, other, events, rel = _fixture(db)
    _project(db, pid, events, rel)
    n1 = len(db.scalars(select(KgCharacterState)).all())
    # 同一事件再投影一次：位置没变 → 不新增状态行
    _project(db, pid, events, rel)
    n2 = len(db.scalars(select(KgCharacterState)).all())
    assert n2 == n1
