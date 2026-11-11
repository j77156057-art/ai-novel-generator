# -*- coding: utf-8 -*-
"""kg_events 书维度 + kg_relations 注入 + 量级块空数据明示 的验收。

锁死四件事：

1. **事件按书读**：``slice_for_prompt`` 取事件时，本书事件 + 项目级 NULL 事件
   可见，别书事件绝不参与；项目没指定书时回到加列前的行为（不过滤）。
2. **事件按书写**：``_persist_extraction`` 落事件时带调用方算好的 ``book``。
3. **关系注入**：只取「两端都在本段实体里」的 active 边，去反向重复、限条数，
   书维度按两端实体的 ``novel_id`` 收窄；渲染时标明「已发生的事实，不得改写」。
4. **量级块不静默消失**：有角色但一个境界/量级都没登记时输出明示行；
   没有角色时不输出；有登记时行为不变。

真实库一行不动：全程内存库。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base  # noqa: E402
from app.generation.power_prompt import build_power_constraint  # noqa: E402
from app.kg.batch_worker import (  # noqa: E402
    _QueueItem, _persist_extraction, _upsert_entity,
)
from app.kg.retrieval_v3 import (  # noqa: E402
    format_slice_for_prompt, slice_for_prompt,
)
from app.models import KgEntity, KgEvent, KgMention, KgRelation, Project  # noqa: E402


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _project(db, *, active=None, continuation=None) -> Project:
    proj = Project(title="事件书维度测试", active_novel_id=active,
                   continuation_novel_id=continuation)
    db.add(proj)
    db.commit()
    return proj


def _event(db, pid, seg, summary, *, novel_id=None) -> KgEvent:
    ev = KgEvent(project_id=pid, sequence=0, segment_no=seg, chapter=1,
                 event_type="incident", summary=summary, novel_id=novel_id)
    db.add(ev)
    db.flush()
    ev.sequence = ev.id
    return ev


def _mention(db, pid, eid, seg, novel_id=None):
    db.add(KgMention(project_id=pid, entity_id=eid, novel_id=novel_id,
                     segment_no=seg, context_window="x", mention_kind="llm"))


# ---------------------------------------------------------------------------
# 1. 事件读侧按书
# ---------------------------------------------------------------------------
def test_slice_events_filtered_by_book_and_null_is_wildcard():
    db = _db()
    proj = _project(db, continuation=5)
    _event(db, proj.id, 1, "本项目原创事件")
    _event(db, proj.id, 1, "书5事件", novel_id=5)
    _event(db, proj.id, 1, "书6事件", novel_id=6)
    db.commit()

    sl = slice_for_prompt(db, proj.id, 1)
    summaries = [e["summary"] for e in sl["recent_events"]]
    assert "书6事件" not in summaries          # 别书绝不参与
    assert set(summaries) == {"本项目原创事件", "书5事件"}
    # 同段内本书显式事件优先于项目级 NULL
    assert summaries[0] == "书5事件"


def test_slice_events_without_book_keeps_old_behavior():
    db = _db()
    proj = _project(db)  # continuation/active 都空
    _event(db, proj.id, 1, "本项目原创事件")
    _event(db, proj.id, 1, "书5事件", novel_id=5)
    _event(db, proj.id, 1, "书6事件", novel_id=6)
    db.commit()

    summaries = {e["summary"] for e in slice_for_prompt(db, proj.id, 1)["recent_events"]}
    assert summaries == {"本项目原创事件", "书5事件", "书6事件"}


# ---------------------------------------------------------------------------
# 2. 事件写侧落书
# ---------------------------------------------------------------------------
def test_persist_extraction_tags_event_with_book():
    db = _db()
    proj = _project(db, active=5)
    result = {
        "entities": [{"kind": "character", "canonical_name": "小杨"}],
        "events": [{
            "subject": "小杨", "object": "", "location": "", "event_type": "incident",
            "verb": "遇到", "summary": "小杨遇到失踪者A", "chapter": 1, "segment": 1,
        }],
    }
    batch = [_QueueItem(project_id=proj.id, segment_no=1,
                        text="小杨遇到失踪者A。")]
    _persist_extraction(db, proj.id, result, batch, book=5)
    db.commit()

    ev = db.scalars(select(KgEvent)).one()
    assert ev.novel_id == 5


def test_persist_extraction_without_book_writes_null():
    db = _db()
    proj = _project(db)
    result = {"entities": [], "events": [{
        "subject": "", "object": "", "location": "", "event_type": "incident",
        "verb": "", "summary": "无来源事件", "chapter": 1, "segment": 1,
    }]}
    batch = [_QueueItem(project_id=proj.id, segment_no=1, text="正文。")]
    _persist_extraction(db, proj.id, result, batch)
    db.commit()

    assert db.scalars(select(KgEvent)).one().novel_id is None


# ---------------------------------------------------------------------------
# 3. 关系注入
# ---------------------------------------------------------------------------
def test_relationships_are_anchored_deduped_and_book_scoped():
    db = _db()
    proj = _project(db, continuation=6)
    xiaoyang = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "小杨"})
    missing_a = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "失踪者A"})
    xu = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "许七安"},
                        novel_id=5)
    xu_pingzhi = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "许平志"},
                                novel_id=5)
    for e in (xiaoyang, missing_a, xu):
        _mention(db, proj.id, e.id, 1)
    db.add_all([
        # 正向 + 反向重复：只留一条
        KgRelation(project_id=proj.id, from_id=xiaoyang.id, to_id=missing_a.id,
                   rel_type="ally", strength=0.8, status="active"),
        KgRelation(project_id=proj.id, from_id=missing_a.id, to_id=xiaoyang.id,
                   rel_type="ally", strength=0.8, status="active"),
        # 一端不是本段实体（许平志）：不注入
        KgRelation(project_id=proj.id, from_id=xu.id, to_id=xu_pingzhi.id,
                   rel_type="sibling", strength=0.9, status="active"),
        # 两端都是本段实体，但许七安是书 5、当前续写书 6：不注入
        KgRelation(project_id=proj.id, from_id=xiaoyang.id, to_id=xu.id,
                   rel_type="enemy", strength=0.9, status="active"),
        # 候选边不注入
        KgRelation(project_id=proj.id, from_id=xiaoyang.id, to_id=missing_a.id,
                   rel_type="owns", strength=0.9, status="candidate"),
    ])
    db.commit()

    sl = slice_for_prompt(db, proj.id, 1)
    assert sl["relationships"] == [
        {"from": "小杨", "to": "失踪者A", "rel_type": "ally"},
    ]
    text = format_slice_for_prompt(sl)
    assert "【既有人物关系（已发生的事实，不得改写）】" in text
    assert "- 小杨↔失踪者A：ally" in text


def test_relationships_respect_limit():
    db = _db()
    proj = _project(db)
    a = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "甲"})
    b = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "乙"})
    c = _upsert_entity(db, proj.id, {"kind": "character", "canonical_name": "丙"})
    for e in (a, b, c):
        _mention(db, proj.id, e.id, 1)
    db.add_all([
        KgRelation(project_id=proj.id, from_id=a.id, to_id=b.id, rel_type="ally"),
        KgRelation(project_id=proj.id, from_id=b.id, to_id=c.id, rel_type="ally"),
        KgRelation(project_id=proj.id, from_id=a.id, to_id=c.id, rel_type="enemy"),
    ])
    db.commit()

    assert len(slice_for_prompt(db, proj.id, 1, max_relations=2)["relationships"]) == 2


def test_format_slice_without_relationships_adds_nothing():
    text = format_slice_for_prompt({"recent_events": []})
    assert text == ""


# ---------------------------------------------------------------------------
# 4. 量级块：有角色但无境界/量级时输出明示行
# ---------------------------------------------------------------------------
def _proj_with(characters):
    return SimpleNamespace(plot_bible={"characters": characters})


def test_power_block_warns_when_characters_have_no_scale():
    text = build_power_constraint(_proj_with([{"name": "小杨"}]))
    assert "本段涉及角色未登记境界与量级" in text
    assert "不要自行编造等级称谓" in text
    assert "本作已登记" not in text


def test_power_block_silent_without_characters():
    text = build_power_constraint(_proj_with([]))
    assert "本段涉及角色未登记境界与量级" not in text
    assert "本作已登记" not in text


def test_power_block_keeps_rows_when_scale_exists():
    text = build_power_constraint(
        _proj_with([{"name": "小杨", "境界": "三品"}]), max_chars=5000)
    assert "本作已登记" in text
    assert "本段涉及角色未登记境界与量级" not in text
