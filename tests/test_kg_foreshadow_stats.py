"""伏笔落库的**口径**（2026-10-28 真机验收后拆开）。

## 为什么有这份测试

原来 `ExtractionStats.foreshadows` 记的是**模型吐出的条目数**，读的人会以为
「新增了这么多条」。实测那次 note 写 `foreshadows: 11`，账上实际是
**7 条同名复用、0 条新增**（模型在同一次响应里吐了重复的名字）。

拆成四个数之后，最要紧的是**恒等式**：

    extracted == reused + duplicate_in_batch + new

口径漂了立刻红（而不是等到某天有人读到这份 note 才发现数字对不上）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.kg.foreshadow_quality import (  # noqa: E402
    NEAR_VARIANT_THRESHOLD,
    name_similarity,
    nearest_existing_name,
)


# ====================================================================
# 1. 相似度：实测样例（阈值 0.55 的依据）
# ====================================================================

def test_相似度_同一件事的两种说法():
    """账上实测：这两对是同一件事，相似度都在阈值之上。"""
    assert name_similarity("断刀刀镡上的陌生姓", "断刀刀镡上的陌生姓氏") >= NEAR_VARIANT_THRESHOLD
    assert name_similarity("沈铁山临死前未说完的话", "沈铁山未说完的话") >= NEAR_VARIANT_THRESHOLD


def test_相似度_一字之差的两件事不判为近似():
    """「萧」与「柳」—— 相似度 0.167，**不能**说成同一件事。"""
    sim = name_similarity("断刀上的‘萧’字", "断刀上的“柳”字与柳家往事")
    assert sim < NEAR_VARIANT_THRESHOLD
    assert sim < 0.3


def test_最近邻只认高置信():
    existing = ["断刀刀镡上的陌生姓", "沈铁山未说完的话", "断刀上的‘萧’字"]
    hit = nearest_existing_name("断刀刀镡上的陌生姓氏", existing)
    assert hit is not None and hit[0] == "断刀刀镡上的陌生姓"
    miss = nearest_existing_name("完全不相干的一件事", existing)
    assert miss is None


def test_最近邻不拿自己当近似():
    existing = ["断刀刀镡上的陌生姓"]
    assert nearest_existing_name("断刀刀镡上的陌生姓", existing) is None


# ====================================================================
# 2. 落库口径：新增 / 同名复用 / 批内重复 / 近似变体
# ====================================================================

@pytest.fixture()
def mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    import app.models  # noqa: F401

    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    try:
        yield db
    finally:
        db.close()


def _persist(db, foreshadows: list[dict]):
    from app.kg.batch_worker import _QueueItem, _persist_extraction

    batch = [_QueueItem(project_id=1, segment_no=1, text="正文", chapter=1)]
    result = {
        "entities": [], "relations": [], "events": [], "event_links": [],
        "entity_knowledge": [], "state_changes": [], "foreshadows": foreshadows,
    }
    return _persist_extraction(db, 1, result, batch)


def test_落库口径_恒等式(mem_session):
    """四种情况各来一条，恒等式必须成立，且账上行数对得上。"""
    from app.models import KgForeshadow, Project

    db = mem_session
    db.add(Project(id=1, title="t", generation_length=800))
    db.commit()
    # 先在账上放一条（下面那条要复用它）
    db.add(KgForeshadow(project_id=1, name="断刀刀镡上的陌生姓",
                        status="planted", planted_chapter=1))
    db.commit()

    stats = _persist(db, [
        {"name": "断刀刀镡上的陌生姓", "status": "developing", "planted_chapter": 1},   # 复用
        {"name": "断刀刀镡上的陌生姓", "status": "developing", "planted_chapter": 1},   # 批内重复
        {"name": "沈砚在渡口的来历", "status": "planted", "planted_chapter": 1},        # 新
        {"name": "断刀刀镡上的陌生姓氏", "status": "planted", "planted_chapter": 1},    # 新 + 近似变体
    ])
    db.commit()

    assert stats.foreshadows == 4, "模型吐了 4 条"
    assert stats.foreshadows_reused == 1
    assert stats.foreshadows_duplicate_in_batch == 1
    assert stats.foreshadows_new == 2
    assert stats.foreshadows_near_variant == 1, "「…陌生姓氏」与「…陌生姓」是近似变体"
    assert (stats.foreshadows
            == stats.foreshadows_reused
            + stats.foreshadows_duplicate_in_batch
            + stats.foreshadows_new), "口径恒等式破了"

    rows = db.query(KgForeshadow).filter_by(project_id=1).all()
    assert len(rows) == 3, "批内重复的名字不该插出第二行"
    names = sorted(r.name for r in rows)
    assert names == sorted(["断刀刀镡上的陌生姓", "沈砚在渡口的来历", "断刀刀镡上的陌生姓氏"])


def test_落库口径_复用只推进不后退(mem_session):
    """同名复用时状态机只前进不后退（这是既有行为，顺手守住）。"""
    from app.models import KgForeshadow, Project

    db = mem_session
    db.add(Project(id=1, title="t", generation_length=800))
    db.add(KgForeshadow(project_id=1, name="断刀刀镡上的陌生姓",
                        status="developing", planted_chapter=1))
    db.commit()

    stats = _persist(db, [
        {"name": "断刀刀镡上的陌生姓", "status": "planted", "planted_chapter": 1},
    ])
    db.commit()
    assert stats.foreshadows_reused == 1
    row = db.query(KgForeshadow).filter_by(project_id=1, name="断刀刀镡上的陌生姓").one()
    assert row.status == "developing", "developing 不该退回 planted"


def test_落库口径_近似变体只留痕不改账本(mem_session):
    """近似变体**只计数**（warn），账本里两条都在（改账本要人点头）。"""
    from app.models import KgForeshadow, Project

    db = mem_session
    db.add(Project(id=1, title="t", generation_length=800))
    db.add(KgForeshadow(project_id=1, name="断刀刀镡上的陌生姓",
                        status="planted", planted_chapter=1))
    db.commit()

    stats = _persist(db, [
        {"name": "断刀刀镡上的陌生姓氏", "status": "planted", "planted_chapter": 1},
    ])
    db.commit()
    assert stats.foreshadows_near_variant == 1
    assert db.query(KgForeshadow).filter_by(project_id=1).count() == 2, \
        "抽取层只留痕，不该合并或删除账本里的条目"
