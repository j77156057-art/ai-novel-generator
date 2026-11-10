# -*- coding: utf-8 -*-
"""降级路径的 alias mention 索引 + mention_kind 区分 + 幂等。

对应 B 的三条要求：
1. provider 不可用 / 抽取失败时仍能落 mention
2. 实体来自库里已有的 item / character（不依赖本批 LLM 结果）
3. ``mention_kind`` 区分 ``llm`` 与 ``alias``
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.kg import batch_worker as bw  # noqa: E402
from app.models import Generation, KgEntity, KgEvent, KgMention  # noqa: E402

#: 只建用到的表：不碰 raw_novels 等带向量列的表，也不连真实库
_TABLES = (KgEntity, KgEvent, Generation, KgMention)


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite:///{pathlib.Path(tmp_path) / 'app.db'}")
    for model in _TABLES:
        model.__table__.create(bind=engine)
    return Session(engine)


def _item(db: Session, name: str, aliases: list[str] | None = None) -> KgEntity:
    ent = KgEntity(project_id=5, kind="item", canonical_name=name,
                   aliases=aliases or [], attrs={})
    db.add(ent)
    db.commit()
    return ent


def test_provider_unavailable_covers_none_and_local_fallback():
    from app.llm.provider import LocalFallbackLLMProvider

    assert bw._provider_unavailable(None) is True
    assert bw._provider_unavailable(LocalFallbackLLMProvider()) is True
    assert bw._provider_unavailable(object()) is False


def test_alias_mention_hits_existing_entity(tmp_path):
    db = _session(tmp_path)
    ent = _item(db, "神剑", ["黄铜剑"])
    batch = [bw._QueueItem(project_id=5, segment_no=1, text="他握着黄铜剑，久久不语。")]
    assert bw._index_mentions_only(db, 5, batch) == 1
    row = db.scalar(select(KgMention))
    assert row.entity_id == ent.id
    assert row.segment_no == 1
    assert row.mention_kind == "alias"
    assert "黄铜剑" in (row.context_window or "")
    # mention_count 同步回写
    db.refresh(ent)
    assert ent.mention_count == 1
    db.close()


def test_alias_mention_skips_text_without_names(tmp_path):
    db = _session(tmp_path)
    _item(db, "神剑", ["黄铜剑"])
    _item(db, "地书", [])
    batch = [bw._QueueItem(project_id=5, segment_no=2,
                           text="小杨站在废弃的工厂前，风从破窗里灌进来。")]
    assert bw._index_mentions_only(db, 5, batch) == 0
    assert db.scalar(select(KgMention)) is None
    db.close()


def test_alias_mention_is_idempotent(tmp_path):
    db = _session(tmp_path)
    _item(db, "神剑", ["黄铜剑"])
    batch = [bw._QueueItem(project_id=5, segment_no=1, text="黄铜剑出鞘。")]
    assert bw._index_mentions_only(db, 5, batch) == 1
    assert bw._index_mentions_only(db, 5, batch) == 0
    assert len(db.scalars(select(KgMention)).all()) == 1
    db.close()


def test_llm_and_alias_kinds_do_not_double_write(tmp_path):
    """LLM 已写过同一 (entity, segment) 时，降级路径不重复写、也不改 kind。"""
    db = _session(tmp_path)
    ent = _item(db, "神剑", ["黄铜剑"])
    db.add(KgMention(project_id=5, entity_id=ent.id, generation_id=None,
                     segment_no=1, context_window="黄铜剑", mention_kind="llm"))
    db.commit()
    batch = [bw._QueueItem(project_id=5, segment_no=1, text="黄铜剑出鞘。")]
    assert bw._index_mentions_only(db, 5, batch) == 0
    assert db.scalar(select(KgMention)).mention_kind == "llm"
    db.close()


def test_llm_path_marks_mention_kind_llm(tmp_path):
    db = _session(tmp_path)
    db.add(Generation(project_id=5, segment_no=3, text="神剑出鞘。"))
    db.commit()
    result = {"entities": [{"kind": "item", "canonical_name": "神剑",
                            "aliases": [], "attrs": {}}]}
    bw._persist_extraction(db, 5, result,
                           [bw._QueueItem(project_id=5, segment_no=3,
                                          text="神剑出鞘。")])
    row = db.scalar(select(KgMention))
    assert row.mention_kind == "llm"
    # 同一段再走降级路径：不重复写
    assert bw._index_mentions_only(db, 5, [bw._QueueItem(
        project_id=5, segment_no=3, text="神剑出鞘。")]) == 0
    db.close()
