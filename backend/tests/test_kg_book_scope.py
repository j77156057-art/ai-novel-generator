# -*- coding: utf-8 -*-
"""事件表的书维度隔离（2026-11-20）。

项目挂两本书时，judge 巡检与前端事件列表都必须按 ``novel_id`` 过滤，
否则别书事件会混进本段的瞬移/年号/伏笔判定和 UI 列表。
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
from app.kg.judge import check_teleportation  # noqa: E402
from app.models import (  # noqa: E402
    KgCharacterState, KgEntity, KgEvent, KgMention, Project,
)
from app.routers.kg_v3 import get_events  # noqa: E402


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _fixture(db, active_book: int | None):
    proj = Project(title="书维度测试", active_novel_id=active_book)
    db.add(proj)
    db.commit()
    pid = proj.id
    hero = KgEntity(project_id=pid, kind="character", canonical_name="许七安")
    home = KgEntity(project_id=pid, kind="location", canonical_name="京城")
    port = KgEntity(project_id=pid, kind="location", canonical_name="云州")
    db.add_all([hero, home, port])
    db.commit()
    db.add(KgCharacterState(project_id=pid, character_id=hero.id,
                            segment_no=1, state_type="location",
                            new_value="京城"))
    db.add(KgMention(project_id=pid, entity_id=hero.id, segment_no=2,
                     context_window="许七安"))
    db.add(KgEvent(project_id=pid, sequence=1, chapter=2, segment_no=2,
                   event_type="incident", summary="许七安抵达云州",
                   subject_id=hero.id, location_id=port.id, novel_id=1))
    db.add(KgEvent(project_id=pid, sequence=2, chapter=9, segment_no=99,
                   event_type="incident", summary="别书事件",
                   subject_id=hero.id, location_id=port.id, novel_id=2))
    db.commit()
    return pid, hero


def test_judge_ignores_events_from_other_book():
    db = _db()
    pid, _hero = _fixture(db, active_book=1)
    assert len(check_teleportation(db, pid, "许七安站在云州。", 2,
                                   novel_id=1)) == 1
    # 只看第 2 本书：本段没有第 2 本书的事件 → 不报
    assert check_teleportation(db, pid, "许七安站在云州。", 2,
                               novel_id=2) == []


def test_event_list_defaults_to_active_book():
    db = _db()
    pid, _hero = _fixture(db, active_book=1)
    out = get_events(project_id=pid, limit=50, event_type=None,
                     novel_id=None, db=db)
    assert [e["summary"] for e in out["events"]] == ["许七安抵达云州"]

    out2 = get_events(project_id=pid, limit=50, event_type=None,
                      novel_id=2, db=db)
    assert [e["summary"] for e in out2["events"]] == ["别书事件"]
