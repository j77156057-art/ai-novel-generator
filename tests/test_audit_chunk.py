"""语料**切分质量**审核（main 侧无测试，这里补最小冒烟 + 语义断言）。

这条规则的定位是「切分器正确性」，不是「语料内容好不好」：
- 分片结尾不是句末标点 / 开头是标点 / 成对符号不配平 → 说明切在句子中间；
- 空列表要**明确拒**，不广播全库（与 audit_corpus 同一纪律）；
- id 要能跨规则去重（`taken`）。
"""

from __future__ import annotations

import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.audit_chunk import (  # noqa: E402
    BROKEN_RATE_THRESHOLD,
    rule_chunk_boundary_damage,
    scan_chunks,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import NovelChunk, RawNovel  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def test_empty_input_is_all_zeroes():
    assert scan_chunks([]) == {"n": 0, "tail_broken": 0, "head_broken": 0,
                               "unbalanced": 0, "tail_pct": 0.0, "head_pct": 0.0,
                               "unbalanced_pct": 0.0, "samples": {}}
    # 纯空白不算一个分片
    assert scan_chunks(["", "   ", "\n"])["n"] == 0


def test_clean_chunks_have_no_damage():
    s = scan_chunks(["他推开门。", "「你来了。」她笑道。", "《山经》摊在桌上。"])
    assert (s["n"], s["tail_broken"], s["head_broken"], s["unbalanced"]) == (3, 0, 0, 0)
    assert s["samples"] == {"tail": [], "head": [], "unbalanced": []}


def test_broken_tail_head_and_pairs_are_counted_with_limited_samples():
    chunks = ["他推开门" + ch for ch in "abcd"] + ["，她笑了。"] + ["「他没合上。"]
    s = scan_chunks(chunks)
    assert s["n"] == 6
    assert s["tail_broken"] == 4          # 4 条结尾没有句末标点
    assert s["head_broken"] == 1          # 1 条以逗号开头
    assert s["unbalanced"] == 1           # 1 条引号不配平
    assert s["tail_pct"] == round(4 / 6 * 100, 1)
    assert len(s["samples"]["tail"]) == 3  # 样本最多 3 条
    assert len(s["samples"]["head"]) == 1


def test_empty_novel_id_list_is_refused_instead_of_broadcasting(db):
    novel = RawNovel(title="语料")
    db.add(novel)
    db.commit()
    db.add(NovelChunk(novel_id=novel.id, idx=0, text="他推开门"))
    db.commit()

    issues, stats = rule_chunk_boundary_damage(db, novel_ids=[])
    assert issues == []
    assert stats == {"skipped": "novel_ids 为空"}


def test_rule_reports_broken_rate_per_novel_and_escalates_above_half(db):
    clean = RawNovel(title="干净")
    dirty = RawNovel(title="硬切")
    db.add_all([clean, dirty])
    db.commit()
    db.add_all([NovelChunk(novel_id=clean.id, idx=i, text="他推开门。") for i in range(10)])
    # 10 个里 8 个半句结尾 → 80% > 50% → error
    db.add_all([NovelChunk(novel_id=dirty.id, idx=i, text=f"他推开{i}") for i in range(8)])
    db.add_all([NovelChunk(novel_id=dirty.id, idx=100 + i, text="门开了。") for i in range(2)])
    db.commit()

    issues, stats = rule_chunk_boundary_damage(db, novel_ids=[clean.id, dirty.id])
    assert stats["novels_scanned"] == 2 and stats["chunks_scanned"] == 20
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "error"
    assert issue.rule == "chunk_tail_broken"
    assert "80.0%" in issue.title
    assert issue.evidence and issue.suggestion


def test_threshold_just_above_is_warn_and_ids_are_unique_across_calls(db):
    novel = RawNovel(title="临界")
    db.add(novel)
    db.commit()
    # 20 个里 3 个坏 → 15%：超过阈值但不到 50% → warn
    db.add_all([NovelChunk(novel_id=novel.id, idx=i, text="他推开门") for i in range(3)])
    db.add_all([NovelChunk(novel_id=novel.id, idx=10 + i, text="门开了。") for i in range(17)])
    db.commit()

    taken: set[str] = set()
    first, _ = rule_chunk_boundary_damage(db, novel_ids=[novel.id], taken=taken)
    second, _ = rule_chunk_boundary_damage(db, novel_ids=[novel.id], taken=taken)
    assert first[0].severity == "warn"
    assert BROKEN_RATE_THRESHOLD * 100 < 15
    assert {i.id for i in first}.isdisjoint({i.id for i in second})
