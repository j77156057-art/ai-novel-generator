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


def test_k1_shared_alias_is_ignored(db):
    """两个物件共用「手机」这个泛称别名时，该别名不能定位物件。"""
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    old = _entity(db, proj, "旧手机", "item", aliases=["手机"])
    rec = _entity(db, proj, "录像手机", "item", aliases=["手机"])
    db.add_all([
        KgRelation(project_id=proj.id, from_id=li.id, to_id=old.id,
                   rel_type="owns", status="active"),
        KgRelation(project_id=proj.id, from_id=li.id, to_id=rec.id,
                   rel_type="owns", status="active"),
    ])
    db.commit()

    report = run_authority_checks(db, proj, "张三拿出手机看了一眼。", 1)
    assert not [f for f in report["findings"] if f["check"] == "authority_holding"]


def test_k1_shared_alias_survives_ambiguous_item(db):
    """一个物件因持有者冲突被跳过时，共用别名不能转由另一个物件独占。"""
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    wang = _entity(db, proj, "王五", "character")
    old = _entity(db, proj, "旧手机", "item", aliases=["手机"])
    rec = _entity(db, proj, "录像手机", "item", aliases=["手机"])
    db.add_all([
        KgRelation(project_id=proj.id, from_id=li.id, to_id=old.id,
                   rel_type="owns", status="active", since_chapter=1),
        KgRelation(project_id=proj.id, from_id=wang.id, to_id=old.id,
                   rel_type="owns", status="active", since_chapter=1),
        KgRelation(project_id=proj.id, from_id=wang.id, to_id=rec.id,
                   rel_type="owns", status="active", since_chapter=1),
    ])
    db.commit()
    report = run_authority_checks(db, proj, "张三拿出手机看了一眼。", 3)
    assert not [f for f in report["findings"] if f["check"] == "authority_holding"]


def test_k1_skips_possessive_alias_and_self_claim(db):
    """别名自带物主、或正文写「自己的」，都不构成越界。"""
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    _entity(db, proj, "王五", "character")
    wang = _entity(db, proj, "赵六", "character")
    phone = _entity(db, proj, "旧手机", "item",
                    aliases=["王五的旧手机", "旧手机"])
    db.add(KgRelation(project_id=proj.id, from_id=wang.id, to_id=phone.id,
                      rel_type="owns", status="active", since_chapter=1))
    db.commit()

    assert run_authority_checks(db, proj, "张三拿出王五的旧手机。", 2)["findings"] == []
    assert run_authority_checks(db, proj, "张三拿出自己的旧手机。", 2)["findings"] == []


def test_k3_rejects_non_name_phrases(db):
    """「我知道/没有回答」这类短语不能被当成未登记人物。"""
    proj = _project(db)
    report = run_authority_checks(
        db, proj, "我知道。我不知道。没有回答。", 1
    )
    assert not [f for f in report["findings"] if f["check"] == "authority_unknown_person"]


def test_k1_holds_as_of_chapter(db):
    """同一物件多次易手时，只按「截至本章」的持有者判，不用未来持有者。"""
    proj = _project(db)
    zhang = _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add_all([
        KgRelation(project_id=proj.id, from_id=li.id, to_id=sword.id,
                   rel_type="owns", status="active", since_chapter=1),
        KgRelation(project_id=proj.id, from_id=zhang.id, to_id=sword.id,
                   rel_type="owns", status="active", since_chapter=5),
    ])
    db.commit()

    # 第 3 章：持有者是李四，张三拿来用 → 判
    report = run_authority_checks(db, proj, "张三握着青锋剑。", 3)
    assert _checks(report) == ["authority_holding"]
    # 第 6 章：已易手给张三 → 不判
    assert run_authority_checks(db, proj, "张三握着青锋剑。", 6)["findings"] == []


def test_k1_ambiguous_holders_are_skipped(db):
    """同一章出现两个持有者说明 KG 自相矛盾，宁可不判。"""
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    wang = _entity(db, proj, "王五", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add_all([
        KgRelation(project_id=proj.id, from_id=li.id, to_id=sword.id,
                   rel_type="owns", status="active", since_chapter=1),
        KgRelation(project_id=proj.id, from_id=wang.id, to_id=sword.id,
                   rel_type="owns", status="active", since_chapter=1),
    ])
    db.commit()
    assert run_authority_checks(db, proj, "张三握着青锋剑。", 3)["findings"] == []


def test_k1_ignores_edges_from_future_chapters(db):
    """持有边从未来章才开始，本章就还没有已知持有者，不能判。"""
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add(KgRelation(project_id=proj.id, from_id=li.id, to_id=sword.id,
                      rel_type="owns", status="active", since_chapter=9))
    db.commit()
    assert run_authority_checks(db, proj, "张三握着青锋剑。", 3)["findings"] == []


def test_k1_without_chapter_skips_multi_owner(db):
    """章号未知时不能按「最新持有者」判：只能接受唯一持有者。"""
    proj = _project(db)
    _entity(db, proj, "张三", "character")
    li = _entity(db, proj, "李四", "character")
    wang = _entity(db, proj, "王五", "character")
    sword = _entity(db, proj, "青锋剑", "item")
    db.add_all([
        KgRelation(project_id=proj.id, from_id=li.id, to_id=sword.id,
                   rel_type="owns", status="active", since_chapter=1),
        KgRelation(project_id=proj.id, from_id=wang.id, to_id=sword.id,
                   rel_type="owns", status="active", since_chapter=5),
    ])
    db.commit()
    assert run_authority_checks(db, proj, "张三握着青锋剑。", None)["findings"] == []

    proj2 = _project(db)
    _entity(db, proj2, "张三", "character")
    li2 = _entity(db, proj2, "李四", "character")
    sword2 = _entity(db, proj2, "青锋剑", "item")
    db.add(KgRelation(project_id=proj2.id, from_id=li2.id, to_id=sword2.id,
                      rel_type="owns", status="active", since_chapter=1))
    db.commit()
    assert _checks(run_authority_checks(db, proj2, "张三握着青锋剑。", None)) == [
        "authority_holding"
    ]


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
    proj = _project(db, outline=["少年入城", "夜探王府：张三与李四交手"])
    _entity(db, proj, "张三", "character")
    _entity(db, proj, "李四", "character")
    report = run_authority_checks(db, proj, "张三望着李四。", 1)

    spoiler = [f for f in report["findings"] if f["check"] == "authority_outline_spoiler"]
    assert spoiler and spoiler[0]["level"] == "suspected"
    assert set(spoiler[0]["evidence"][0]["tokens"]) == {"张三", "李四"}


def test_k5_requires_enough_tokens(db):
    proj = _project(db, outline=["少年入城", "夜探王府：张三与李四交手"])
    _entity(db, proj, "张三", "character")
    _entity(db, proj, "李四", "character")
    report = run_authority_checks(db, proj, "张三望着远处。", 1)
    assert not [f for f in report["findings"] if f["check"] == "authority_outline_spoiler"]


def test_k5_ignores_non_entity_tokens(db):
    """没有 KG 实体支撑时不能凭字面重合判剧透（影子跑里的标点/虚词误报）。"""
    proj = _project(db, outline=["少年入城", "夜探王府：张三与李四交手"])
    report = run_authority_checks(db, proj, "张三望着李四。", 1)
    assert not [f for f in report["findings"] if f["check"] == "authority_outline_spoiler"]


def test_k5_ignores_introduced_character(db):
    """更早章纲里出现过的人物不算提前登场（否则主角每章都会命中）。"""
    proj = _project(
        db, outline=["张三进城", "李四同行", "张三与李四会合"],
    )
    for name in ("张三", "李四"):
        _entity(db, proj, name, "character")
    report = run_authority_checks(db, proj, "张三和李四对坐。", 2)
    assert not [f for f in report["findings"] if f["check"] == "authority_outline_spoiler"]
