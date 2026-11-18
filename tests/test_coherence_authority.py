"""第三刀：权威越界 K1–K5 的回归（默认 report，不改正文）。"""
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.coherence_authority import run_authority_checks  # noqa: E402
from app.models import (  # noqa: E402
    KgCharacterState,
    KgEntity,
    KgRelation,
    Project,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _project(db, **kw) -> Project:
    proj = Project(title="t", **kw)
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _entity(db, proj, name, kind, **kw) -> KgEntity:
    ent = KgEntity(project_id=proj.id, canonical_name=name, kind=kind, **kw)
    db.add(ent)
    db.commit()
    db.refresh(ent)
    return ent


def _checks(report):
    return [f["check"] for f in report["findings"]]


def test_k1_item_held_by_someone_else(db):
    proj = _project(db)
    zhang = _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add(KgRelation(project_id=proj.id, from_id=li.id, to_id=sword.id,
                      rel_type="owns", status="active"))
    db.commit()

    report = run_authority_checks(db, proj, "张三握着青锋剑，缓步走进堂中。", 1)

    assert _checks(report) == ["authority_holding"]
    ev = report["findings"][0]["evidence"][0]
    assert ev["expected_holder"] == "李四" and ev["mentioned_holder"] == "张三"
    assert report["counts"]["definite"] == 1


def test_k1_exempt_when_holder_present_or_transfer_verb(db):
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add(KgRelation(project_id=proj.id, from_id=li.id, to_id=sword.id,
                      rel_type="owns", status="active"))
    db.commit()

    assert run_authority_checks(db, proj, "李四握着青锋剑。", 1)["findings"] == []
    assert run_authority_checks(db, proj, "张三接过青锋剑。", 1)["findings"] == []


def test_k1_ignores_reference_book_edges(db):
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add(KgRelation(project_id=proj.id, novel_id=5, from_id=li.id, to_id=sword.id,
                      rel_type="owns", status="active"))
    db.commit()

    assert run_authority_checks(db, proj, "张三握着青锋剑。", 1)["findings"] == []


def test_k2_dead_character_appearing(db):
    proj = _project(db)
    wang = _entity(db, proj, "王五", "character")
    db.add(KgCharacterState(project_id=proj.id, character_id=wang.id,
                            segment_no=3, state_type="alive",
                            new_value="false", is_current=True))
    db.commit()

    report = run_authority_checks(db, proj, "王五推门而入，笑道：好。", 4)
    assert _checks(report) == ["authority_dead"]

    # 回忆/遗物语境不判
    assert run_authority_checks(db, proj, "他想起王五生前的模样。", 4)["findings"] == []


def test_k3_unknown_speaker(db):
    proj = _project(db)
    text = "赵六说道：走。赵六问道：快。"
    report = run_authority_checks(db, proj, text, 1)

    unknown = [f for f in report["findings"] if f["check"] == "authority_unknown_person"]
    assert unknown and unknown[0]["evidence"][0]["name"] == "赵六"
    assert unknown[0]["level"] == "suspected"


def test_k3_skips_registered_character(db):
    proj = _project(db)
    _entity(db, proj, "赵六", "character")
    report = run_authority_checks(db, proj, "赵六说道：走。赵六又说：快。", 1)
    assert not [f for f in report["findings"] if f["check"] == "authority_unknown_person"]


def test_k4_ability_not_granted(db):
    proj = _project(db, plot_bible={
        "characters": [
            {"name": "张三", "skills": ["御剑术"]},
            {"name": "李四", "abilities": ["拳法"]},
        ]
    })
    report = run_authority_checks(db, proj, "李四施展御剑术。", 1)

    ability = [f for f in report["findings"] if f["check"] == "authority_ability"]
    assert ability and ability[0]["level"] == "suspected"
    assert ability[0]["evidence"][0]["ability"] == "御剑术"


def test_k4_without_schema_reports_reason(db):
    proj = _project(db, plot_bible={"characters": [{"name": "张三"}]})
    report = run_authority_checks(db, proj, "张三施展御剑术。", 1)

    assert not [f for f in report["findings"] if f["check"] == "authority_ability"]
    assert any(n["reason"] == "capability_schema_missing" for n in report["notes"])


def test_k5_outline_spoiler(db):
    proj = _project(db, outline=["少年入城", "夜探王府夺玉玺"])
    report = run_authority_checks(db, proj, "他决定夜探王府，取回玉玺。", 1)

    spoiler = [f for f in report["findings"] if f["check"] == "authority_outline_spoiler"]
    assert spoiler and spoiler[0]["level"] == "suspected"


def test_k5_requires_enough_tokens(db):
    proj = _project(db, outline=["少年入城", "夜探王府夺玉玺"])
    report = run_authority_checks(db, proj, "他走进王府。", 1)
    assert not [f for f in report["findings"] if f["check"] == "authority_outline_spoiler"]
