# -*- coding: utf-8 -*-
"""持有投影（v3.0 抽取 → generated.db）+ 事件段号归属 + 成功路径 item 提及。

覆盖：
1. ``project_holdings`` 真写：新物品 → 组 + 已批准写法 + 持有段 + 故事旁注；
2. 同一份抽取重跑：零新增（组/写法复用、持有段 confirmed）；
3. 预置人工组（approved 写法）时复用，不建第二个同名组；
4. 只有 ``novel_id IS NULL`` 通配组时按同一本书复用（跨书槽位）；
5. ``attribute_event_segments``：5 段批抽的事件各落各段，不再一律批首段；
6. ``_persist_extraction`` 成功路径补 item 提及（库里已有物品 + 正文命中）。

跑法：pytest backend/tests/test_holding_projection.py -q
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import pytest  # noqa: E402

from app.kg import batch_worker as bw  # noqa: E402
from app.kg import generated_store as store  # noqa: E402
from app.kg.extractor_v3 import attribute_event_segments  # noqa: E402
from app.kg.holding_projection import project_holdings  # noqa: E402
from app.models import Generation, KgEntity, KgEvent, KgMention  # noqa: E402

_PROJECT = 7
_BOOK = 5


def _gen_db(tmp_path) -> pathlib.Path:
    path = pathlib.Path(tmp_path) / "generated.db"
    con = store.connect(path)
    store.ensure_schema(con)
    # 组/写法表不在 SCHEMA 里（生产库由迁移建），新库靠 ensure_item_group 兜底；
    # 这里提前建好，便于测试直接预置人工组。
    con.executescript(store._GROUP_SCHEMA)
    con.close()
    return path


def _count(con: sqlite3.Connection, table: str) -> int:
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _result() -> dict:
    return {
        "entities": [
            {"kind": "character", "canonical_name": "许七安", "aliases": [], "attrs": {}},
            {"kind": "item", "canonical_name": "镇北王刀", "aliases": [],
             "attrs": {"type": "武器", "held_by": "许七安"}},
        ],
        "relations": [],
        "events": [],
    }


def _batch() -> list:
    return [
        bw._QueueItem(project_id=_PROJECT, segment_no=11,
                      text="许七安握住镇北王刀，刀身嗡鸣。", chapter=3),
        bw._QueueItem(project_id=_PROJECT, segment_no=12,
                      text="风从长街尽头吹来。", chapter=3),
    ]


def _kind_of(name: str) -> str | None:
    return {"许七安": "character", "镇北王刀": "item"}.get(name)


def _owner(name: str) -> tuple[int | None, str | None]:
    return None, "character"


def _project(path, result=None, batch=None) -> dict:
    return project_holdings(
        project_id=_PROJECT, book=_BOOK, result=result or _result(),
        batch=batch or _batch(), kind_of=_kind_of, owner_lookup=_owner,
        generated_db=str(path))


# ---------------------------------------------------------------------------
# 1. 真写：组 / 写法 / 持有段 / 故事旁注
# ---------------------------------------------------------------------------
def test_projection_writes_group_surface_holding_and_story_segment(tmp_path):
    path = _gen_db(tmp_path)
    stats = _project(path)

    assert stats["pairs"] == 1
    assert stats["groups_created"] == 1
    assert stats["surfaces_created"] == 1
    assert stats["holdings"] == {"inserted": 1}
    assert stats["story_segments"] == 1
    assert stats["errors"] == []

    con = store.connect(path, readonly=True)
    try:
        group = con.execute(
            "SELECT * FROM kg_item_group WHERE canonical_name = '镇北王刀'").fetchone()
        assert group is not None and group["novel_id"] == _BOOK
        assert group["status"] == "active"

        surface = con.execute(
            "SELECT * FROM kg_item_surface WHERE surface = '镇北王刀'").fetchone()
        assert surface is not None
        assert surface["group_id"] == group["id"]
        assert surface["status"] == "approved"

        holding = con.execute("SELECT * FROM kg_item_holding").fetchone()
        assert holding["item_name"] == "镇北王刀"
        assert holding["owner_name"] == "许七安"
        assert holding["group_id"] == group["id"]
        assert holding["valid_from"] == 3
        assert holding["valid_to"] is None
        assert holding["reason_code"] == "attr_held_by"

        seg = con.execute("SELECT * FROM kg_holding_story_segment").fetchone()
        assert seg["holding_id"] == holding["id"]
        assert seg["narration"] == "forward"
        assert seg["evidence_chapter"] == 3
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 1b. transfer_event_id：持有变化要能指回「哪件事」
# ---------------------------------------------------------------------------
def test_projection_links_transfer_event(tmp_path):
    path = _gen_db(tmp_path)
    seen = {}

    def _lookup(item, owner, segment_no):
        seen["args"] = (item, owner)
        return 77

    stats = project_holdings(
        project_id=_PROJECT, book=_BOOK, result=_result(), batch=_batch(),
        kind_of=_kind_of, owner_lookup=_owner, event_lookup=_lookup,
        generated_db=str(path))
    assert stats["errors"] == []
    assert seen["args"] == ("镇北王刀", "许七安")

    con = store.connect(path, readonly=True)
    try:
        holding = con.execute("SELECT * FROM kg_item_holding").fetchone()
        assert holding["transfer_event_id"] == 77
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. 重跑幂等：零新增
# ---------------------------------------------------------------------------
def test_projection_is_idempotent_on_rerun(tmp_path):
    path = _gen_db(tmp_path)
    _project(path)
    con = store.connect(path, readonly=True)
    try:
        before = tuple(_count(con, t) for t in
                       ("kg_item_group", "kg_item_surface", "kg_item_holding",
                        "kg_holding_story_segment"))
    finally:
        con.close()

    stats = _project(path)
    assert stats["groups_created"] == 0
    assert stats["surfaces_created"] == 0
    assert stats["holdings"] == {"confirmed": 1}
    assert stats["story_segments"] == 0

    con = store.connect(path, readonly=True)
    try:
        after = tuple(_count(con, t) for t in
                      ("kg_item_group", "kg_item_surface", "kg_item_holding",
                       "kg_holding_story_segment"))
    finally:
        con.close()
    assert after == before


# ---------------------------------------------------------------------------
# 3. 预置人工组 + approved 写法 → 复用，不建第二个
# ---------------------------------------------------------------------------
def test_projection_reuses_existing_human_group(tmp_path):
    path = _gen_db(tmp_path)
    con = store.connect(path)
    try:
        cur = con.execute(
            "INSERT INTO kg_item_group (project_id, kind, canonical_name, status,"
            " generic_members, note, created_at, novel_id)"
            " VALUES (?, 'item', '镇北王刀', 'active', '[]', '人工', ?, ?)",
            (_PROJECT, store._now(), _BOOK))
        gid = cur.lastrowid
        con.execute(
            "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
            " source, confidence, note, created_at, novel_id)"
            " VALUES (?, '镇北王刀', ?, 'approved', 'human', 1.0, '人工', ?, ?)",
            (_PROJECT, gid, store._now(), _BOOK))
        con.commit()
    finally:
        con.close()

    stats = _project(path)
    assert stats["groups_created"] == 0
    assert stats["surfaces_created"] == 0

    con = store.connect(path, readonly=True)
    try:
        assert _count(con, "kg_item_group") == 1
        assert con.execute(
            "SELECT group_id FROM kg_item_holding").fetchone()["group_id"] == gid
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 4. 只有 NULL 通配组时按同一本书复用（跨书槽位）
# ---------------------------------------------------------------------------
def test_projection_reuses_generic_group_when_book_group_absent(tmp_path):
    path = _gen_db(tmp_path)
    con = store.connect(path)
    try:
        cur = con.execute(
            "INSERT INTO kg_item_group (project_id, kind, canonical_name, status,"
            " generic_members, note, created_at, novel_id)"
            " VALUES (?, 'item', '镇北王刀', 'active', '[]', '通配', ?, NULL)",
            (_PROJECT, store._now()))
        gid = cur.lastrowid
        con.execute(
            "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
            " source, confidence, note, created_at, novel_id)"
            " VALUES (?, '镇北王刀', ?, 'approved', 'migration', 1.0, '', ?, NULL)",
            (_PROJECT, gid, store._now()))
        con.commit()
    finally:
        con.close()

    stats = _project(path)
    assert stats["groups_created"] == 0
    assert stats["surfaces_created"] == 0
    con = store.connect(path, readonly=True)
    try:
        assert _count(con, "kg_item_group") == 1
        assert con.execute(
            "SELECT group_id FROM kg_item_holding").fetchone()["group_id"] == gid
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 4b. 名字不是组名、但是某个组的 approved 写法 → 复用那个组（D）
# ---------------------------------------------------------------------------
def _seed_group(con, name: str, *, status: str = "active",
                novel_id: int | None = _BOOK) -> int:
    cur = con.execute(
        "INSERT INTO kg_item_group (project_id, kind, canonical_name, status,"
        " generic_members, note, created_at, novel_id)"
        " VALUES (?, 'item', ?, ?, '[]', '人工', ?, ?)",
        (_PROJECT, name, status, store._now(), novel_id))
    return int(cur.lastrowid)


def _seed_surface(con, surface: str, gid: int, *,
                  novel_id: int | None = _BOOK) -> None:
    con.execute(
        "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
        " source, confidence, note, created_at, novel_id)"
        " VALUES (?, ?, ?, 'approved', 'human', 1.0, '人工', ?, ?)",
        (_PROJECT, surface, gid, store._now(), novel_id))


def test_ensure_item_group_reuses_group_registered_by_approved_surface(tmp_path):
    path = _gen_db(tmp_path)
    con = store.connect(path)
    try:
        gid = _seed_group(con, "地书")
        for surface in ("地书碎片", "地书聊天群"):
            _seed_surface(con, surface, gid)
        con.commit()

        for surface in ("地书碎片", "地书聊天群"):
            out = store.ensure_item_group(
                con, surface, project_id=_PROJECT, novel_id=_BOOK, chapter=120)
            assert out["group_id"] == gid, "已登记写法必须复用组 9，不建新组"
            assert out["group_created"] is False
            assert out["surface_created"] is False

        # 反向：真正的新物品名仍要正常建组，别把「复用」写成「永远不建组」
        fresh = store.ensure_item_group(
            con, "新铁锤", project_id=_PROJECT, novel_id=_BOOK, chapter=120)
        assert fresh["group_created"] is True
        assert fresh["surface_created"] is True
        con.commit()

        assert _count(con, "kg_item_group") == 2
        assert _count(con, "kg_item_surface") == 3
    finally:
        con.close()


def test_ensure_item_group_reuses_generic_group_with_generic_semantics(tmp_path):
    path = _gen_db(tmp_path)
    con = store.connect(path)
    try:
        gid = _seed_group(con, "玉石小镜", status="generic")
        _seed_surface(con, "三号碎片", gid)
        con.commit()

        out = store.ensure_item_group(
            con, "三号碎片", project_id=_PROJECT, novel_id=_BOOK, chapter=120)
        assert out["group_id"] == gid
        assert out["group_status"] == "generic"
        assert out["group_created"] is False
        # 泛称组不参与单件持有解析：复用是复用，语义仍是 generic
        assert store.resolve_group_id(
            con, "三号碎片", novel_id=_BOOK, project_id=_PROJECT) is None
        assert _count(con, "kg_item_group") == 1
    finally:
        con.close()


def test_ensure_item_group_does_not_reuse_another_books_surface(tmp_path):
    path = _gen_db(tmp_path)
    con = store.connect(path)
    try:
        other = _seed_group(con, "地书", novel_id=6)
        _seed_surface(con, "地书碎片", other, novel_id=6)
        con.commit()

        out = store.ensure_item_group(
            con, "地书碎片", project_id=_PROJECT, novel_id=_BOOK, chapter=120)
        assert out["group_id"] != other
        assert out["group_created"] is True, "别书的写法不得跨书复用"
        con.commit()
    finally:
        con.close()


def test_ensure_item_group_raises_on_ambiguous_surface_hits(tmp_path):
    path = _gen_db(tmp_path)
    con = store.connect(path)
    try:
        book_gid = _seed_group(con, "地书")
        wild_gid = _seed_group(con, "地书残卷", novel_id=None)
        _seed_surface(con, "地书碎片", book_gid, novel_id=_BOOK)
        _seed_surface(con, "地书碎片", wild_gid, novel_id=None)
        con.commit()

        # 不传书时两行都在候选里、指向不同组：不猜，报歧义
        with pytest.raises(RuntimeError, match="命中多个组"):
            store.ensure_item_group(con, "地书碎片", project_id=_PROJECT,
                                    chapter=120)
        # 传了书：本书显式写法优先，可判定
        out = store.ensure_item_group(
            con, "地书碎片", project_id=_PROJECT, novel_id=_BOOK, chapter=120)
        assert out["group_id"] == book_gid
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 5. 事件段号：每条事件落自己所属的段
# ---------------------------------------------------------------------------
def test_event_segments_follow_each_segment():
    texts = [
        (1, "许七安拔刀。", 1),
        (2, "魏渊走进书房。", 2),
        (3, "临安公主落泪。", 3),
        (4, "税银入库。", 4),
        (5, "司天监的钟响了。", 5),
    ]
    out = {"events": [
        {"sequence": 1, "summary": "拔刀", "subject": "许七安"},
        {"sequence": 2, "summary": "走进书房", "subject": "魏渊"},
        {"sequence": 3, "segment": 3, "summary": "落泪", "subject": "临安公主"},
        {"sequence": 4, "segment": 99, "summary": "税银入库", "subject": "税银"},
        {"sequence": 5, "summary": "钟响", "subject": ""},
        # 模型声称第 1 段，但「司天监」只出现在第 5 段 → 以正文为准纠正
        {"sequence": 6, "segment": 1, "summary": "钟声", "subject": "司天监"},
    ]}
    attribute_event_segments(out, texts)
    assert [ev["segment"] for ev in out["events"]] == [1, 2, 3, 4, 4, 5]


def test_event_segments_degenerate_all_first_segment():
    """整批事件都被模型标成第 1 段：不能照单全收，按正文/顺序分回各自段。"""
    texts = [
        (1, "许七安醒来，发现自己关在监牢。", 1),
        (2, "许七安回忆起原主的经历，得知将被发配。", 2),
        (3, "许新年前来探望许七安。", 3),
        (4, "许七安请求许新年帮他拿到卷宗。", 4),
        (5, "许新年将卷宗交给许七安。", 5),
    ]
    out = {"events": [
        {"segment": 1, "summary": "醒来", "subject": "许七安", "location": "监牢"},
        {"segment": 1, "summary": "回忆", "subject": "许七安"},
        {"segment": 1, "summary": "探望", "subject": "许新年", "object": "许七安"},
        {"segment": 1, "summary": "请求", "subject": "许七安", "object": "卷宗"},
        {"segment": 1, "summary": "交给", "subject": "许新年", "object": "卷宗"},
    ]}
    attribute_event_segments(out, texts)
    assert [ev["segment"] for ev in out["events"]] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# 6. 成功路径补 item 提及（库里已有物品 + 正文命中）
# ---------------------------------------------------------------------------
_TABLES = (KgEntity, KgEvent, Generation, KgMention)


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{pathlib.Path(tmp_path) / 'app.db'}")
    for model in _TABLES:
        model.__table__.create(bind=engine)
    return Session(engine)


def test_success_path_adds_item_mentions(tmp_path):
    db = _session(tmp_path)
    item = KgEntity(project_id=9, kind="item", canonical_name="镇北王刀",
                    aliases=["北王刀"], attrs={})
    db.add(item)
    db.commit()
    result = {"entities": [
        {"kind": "character", "canonical_name": "许七安", "aliases": [], "attrs": {}}]}
    batch = [bw._QueueItem(project_id=9, segment_no=1,
                           text="许七安握住北王刀，久久不语。")]

    stats = bw._persist_extraction(db, 9, result, batch)
    assert stats.mentions_alias_added == 1
    rows = {m.entity_id: m.mention_kind
            for m in db.scalars(select(KgMention)).all()}
    assert rows[item.id] == "alias"

    # 重跑：零新增（LLM 提及 + alias 提及都按 (entity, segment) 去重）
    stats2 = bw._persist_extraction(db, 9, result, batch)
    assert stats2.mentions_alias_added == 0
    assert db.scalar(select(func.count()).select_from(KgMention)) == 2
    db.close()


# ---------------------------------------------------------------------------
# 7. 事件幂等：同一段重抽不重复追加事件
# ---------------------------------------------------------------------------
def test_persist_extraction_dedupes_events_on_rerun(tmp_path):
    db = _session(tmp_path)
    batch = [bw._QueueItem(project_id=9, segment_no=3,
                           text="许七安握住北王刀。", chapter=2)]
    result = {
        "entities": [
            {"kind": "character", "canonical_name": "许七安", "aliases": [], "attrs": {}},
            {"kind": "item", "canonical_name": "北王刀", "aliases": [], "attrs": {}},
        ],
        "events": [{
            "sequence": 1, "segment": 3, "chapter": 2, "event_type": "incident",
            "subject": "许七安", "verb": "握住", "object": "北王刀",
            "summary": "许七安握住北王刀", "location": "",
        }],
    }
    s1 = bw._persist_extraction(db, 9, result, batch)
    s2 = bw._persist_extraction(db, 9, result, batch)
    assert s1.events_added == 1
    assert s2.events_added == 0 and s2.events_skipped == 1
    assert db.scalar(select(func.count()).select_from(KgEvent)) == 1
    row = db.scalars(select(KgEvent)).one()
    assert row.segment_no == 3
    db.close()
