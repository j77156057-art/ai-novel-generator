# -*- coding: utf-8 -*-
"""参考语料 → KG v3 补建（corpus_ingest）与三类消费视图。

背景：蒸馏只产文风，不产 KG；3173 章的《修真聊天群》在 v3 表里一行都没有。
本测试锁住补建链路的关键契约：

1. 切窗不丢章、按章数与字数双上限封口；
2. 落库带 novel_id / chapter（章号既是 chapter 也是 segment_no），关系/伏笔/状态同步；
3. 同窗重跑幂等（事件先删后写，不翻倍）；
4. ``worldview`` / ``timeline`` / ``story_context`` 按章截取：N 章之后的事件、
   状态、伏笔一律不可见。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base  # noqa: E402
from app.kg.corpus_ingest import (  # noqa: E402
    build_windows, persist_entities_only, persist_window, window_key,
)
from app.models import (  # noqa: E402
    KgCharacterState, KgEntity, KgEvent, KgForeshadow, KgRelation, Project,
)
from app.kg.story_context import story_context, timeline, worldview  # noqa: E402


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _payload(seg_a: int, seg_b: int) -> dict:
    return {
        "entities": [
            {"kind": "character", "canonical_name": "宋书航", "aliases": ["书航"],
             "attrs": {"arc": "修真者"}},
            {"kind": "faction", "canonical_name": "玄天宗", "aliases": [], "attrs": {}},
            {"kind": "location", "canonical_name": "玄天宗山门", "aliases": [], "attrs": {}},
        ],
        "relations": [
            {"from": "宋书航", "to": "玄天宗", "rel_type": "belongs_to", "strength": 0.8},
        ],
        "events": [
            {"sequence": 1, "segment": seg_a, "chapter": seg_a, "event_type": "incident",
             "summary": "宋书航在玄天宗山门签到", "subject": "宋书航", "verb": "签到",
             "object": "", "location": "玄天宗山门", "in_story_date": ""},
            {"sequence": 2, "segment": seg_b, "chapter": seg_b, "event_type": "foreshadow_plant",
             "summary": "山门石碑出现裂纹", "subject": "宋书航", "verb": "发现",
             "object": "", "location": "玄天宗山门", "in_story_date": ""},
        ],
        "foreshadows": [{"name": "石碑裂纹", "status": "planted",
                         "planted_chapter": seg_b, "planned_payoff_chapter": 900}],
        "state_changes": [{"character": "宋书航", "state_type": "location",
                           "new_value": "玄天宗山门"}],
        "entity_knowledge": [],
        "event_links": [{"from_sequence": 1, "to_sequence": 2, "link_type": "follows"}],
    }


def test_build_windows_respects_chapter_and_char_caps():
    chapters = [(i, "x" * 100) for i in range(1, 21)]
    wins = build_windows(chapters, per_window=5, max_chars=100_000)
    assert [len(w) for w in wins] == [5, 5, 5, 5]
    assert window_key(7, wins[0]) == "7:1-5"
    assert [c for w in wins for c, _ in w] == list(range(1, 21))
    # 字数上限先触发：每章 100 字 + 上限 250 → 每窗最多 2 章
    wins2 = build_windows(chapters, per_window=5, max_chars=250)
    assert [len(w) for w in wins2] == [2] * 10


def test_persist_window_writes_book_scoped_rows_and_is_idempotent():
    db = _db()
    proj = Project(title="语料补建测试")
    db.add(proj)
    db.commit()
    win = [(10, "第一章正文"), (11, "第二章正文")]

    stats = persist_window(db, project_id=proj.id, novel_id=7, win=win,
                           payload=_payload(10, 11))
    db.commit()
    assert stats["events"] == 2

    events = db.scalars(select(KgEvent).where(KgEvent.novel_id == 7)).all()
    assert {e.chapter for e in events} == {10, 11}
    assert all(e.segment_no == e.chapter for e in events)
    assert all(e.sequence == e.chapter * 1000 for e in events)  # 每章一条 → 序号 0

    ent = db.scalar(select(KgEntity).where(KgEntity.canonical_name == "宋书航"))
    assert ent.novel_id == 7 and ent.kind == "character"
    assert ent.first_chapter == 10

    fs = db.scalar(select(KgForeshadow).where(KgForeshadow.name == "石碑裂纹"))
    assert fs.planted_chapter == 11 and fs.status == "planted"

    st = db.scalar(select(KgCharacterState).where(
        KgCharacterState.character_id == ent.id))
    assert st.chapter == 10 and st.new_value == "玄天宗山门"

    # 同窗重跑：事件不翻倍、实体不重复
    persist_window(db, project_id=proj.id, novel_id=7, win=win,
                   payload=_payload(10, 11))
    db.commit()
    assert db.scalar(select(func.count(KgEvent.id))) == 2
    assert db.scalar(select(func.count(KgEntity.id))) == 3


def test_persist_entities_only_upgrades_kind_without_touching_events():
    """定向补抽只改实体/关系：事件与提及必须原样保留，info/character 可改判。"""
    db = _db()
    proj = Project(title="定向补抽测试")
    db.add(proj)
    db.commit()
    win = [(10, "正文"), (11, "正文")]
    persist_window(db, project_id=proj.id, novel_id=7, win=win,
                   payload=_payload(10, 11))
    db.commit()
    # 主抽把门派判成 info（事件宾语兜底），名字与 faction 一致
    junk = KgEntity(project_id=proj.id, novel_id=7, kind="info",
                    canonical_name="青云门", first_chapter=10, last_chapter=10,
                    mention_count=1)
    db.add(junk)
    db.commit()

    stats = persist_entities_only(
        db, project_id=proj.id, novel_id=7, chapter=10,
        payload={"entities": [
            {"kind": "faction", "canonical_name": "青云门", "aliases": ["青云"],
             "attrs": {"type": "门派"}},
            {"kind": "power_system", "canonical_name": "青云剑诀", "aliases": [],
             "attrs": {"tier": "筑基"}},
        ], "relations": [
            {"from": "宋书航", "to": "青云门", "rel_type": "belongs_to", "strength": 0.8},
        ]})
    db.commit()
    assert stats["entities"] == 2 and stats["relations"] == 1

    assert db.scalar(select(KgEntity).where(
        KgEntity.canonical_name == "青云门")).kind == "faction"
    assert db.get(KgEntity, junk.id).kind == "faction", "info 应被定向结论改判"
    # 主抽的事件没有被定向通道清掉
    assert db.scalar(select(func.count(KgEvent.id))) == 2
    # 关系只挂已存在的两端（宋书航来自主抽）
    assert db.scalar(select(func.count(KgRelation.id))) == 2


def test_worldview_timeline_and_story_context_cut_at_chapter():
    db = _db()
    proj = Project(title="按章截取测试")
    db.add(proj)
    db.commit()
    for win in ([(5, "a"), (6, "b")], [(50, "c"), (51, "d")], [(120, "e"), (121, "f")]):
        persist_window(db, project_id=proj.id, novel_id=7, win=win,
                       payload=_payload(win[0][0], win[1][0]))
    db.commit()

    world = worldview(db, proj.id, 7)
    assert {f["name"] for f in world["factions"]} == {"玄天宗"}
    assert world["factions"][0]["evidence"], "世界观条目必须带证据章号"
    assert world["counts"]["location"] == 1

    tl = timeline(db, proj.id, 7, from_chapter=1, to_chapter=60)
    assert [e["chapter"] for e in tl["events"]] == [5, 6, 50, 51]
    assert tl["events"][0]["who"] == "宋书航"
    assert tl["events"][0]["where"] == "玄天宗山门"

    ctx = story_context(db, proj.id, 7, up_to_chapter=60)
    assert ctx["coverage"]["events_before"] == 4
    assert [e["chapter"] for e in ctx["recent_events"]] == [5, 6, 50, 51]
    # 51 章埋的伏笔在 60 章时仍开着；120 章的事件不可见
    assert [f["name"] for f in ctx["open_foreshadows"]] == ["石碑裂纹"]
    assert ctx["characters"][0]["location"] == "玄天宗山门"
    assert all(e["chapter"] <= 60 for e in ctx["recent_events"])
