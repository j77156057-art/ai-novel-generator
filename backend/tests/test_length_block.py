# -*- coding: utf-8 -*-
"""生成侧硬判据 2：字数双向拦截的上限一侧。

口径：终稿 > 目标 × 120% ⇒ 先由现有 `_rewrite_rounds` 追加一次压缩轮；
压缩后仍超 ⇒ `_commit_segment` 抛 `LengthBlocked`，不写 Generation 行。
下限（<80%）保持既有「只留痕」行为，本文件不改变它。

锁住四件事：

1. 等于上限放行、超 1 字即拦（严格大于，不是大于等于）；
2. 上限取 `quality.length_path.limit`（与压缩轮同一把尺），缺省才用项目预算现算；
3. blocked 明细带段号 / 章标签 / 实测字数 / 上限 / 压缩是否接受；
4. 压缩未被接受（仍超）与压缩接受但压不够，都一律不落正文。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app.generation.engine as engine  # noqa: E402
from app.models import Base, Generation, Project  # noqa: E402
from app.style.consistency import hanzi_count, length_limits  # noqa: E402


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def project(db):
    p = Project(title="篇幅上限测试", generation_length=4000)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _text(n: int) -> str:
    return "字" * n


def test_limit_is_target_times_existing_ratio():
    # 不新增常量：沿用 length_limits 的既有 120% 口径
    assert length_limits(4000)["max"] == 4800
    assert length_limits(3000)["max"] == 3600


def test_exactly_at_limit_passes():
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=4000), _text(4800),
        {"length_path": {"path": "compression", "limit": 4800}},
    )
    assert ev is None


def test_one_char_over_limit_blocks():
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=4000), _text(4801),
        {"length_path": {"path": "compression", "limit": 4800,
                         "compression_accepted": True}},
    )
    assert ev is not None
    assert ev["hanzi"] == 4801
    assert ev["limit"] == 4800
    assert ev["path"] == "compression"
    assert ev["compression_accepted"] is True
    assert ev["still_over_limit"] is True


def test_falls_back_to_project_budget_without_length_path():
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=3000), _text(3601), None)
    assert ev is not None
    assert ev["limit"] == 3600
    assert ev["path"] is None


def test_no_budget_and_no_path_does_not_guess():
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=None), _text(9999), None)
    assert ev is None


def test_commit_raises_and_persists_nothing(db, project):
    quality = {"length_path": {"path": "compression", "limit": 4800,
                               "compression_accepted": False}}
    with pytest.raises(engine.LengthBlocked) as ei:
        engine._commit_segment(
            db, project, _text(6378), "摘要",
            quality=quality, chapter_range="第24章《夜半敲门》")
    payload = ei.value.to_dict()
    assert payload["status"] == "blocked"
    assert payload["blocked_by"] == "length_over_limit"
    assert payload["segment_no"] == 1
    assert payload["chapter_label"] == "第24章《夜半敲门》"
    assert payload["hanzi"] == 6378
    assert payload["limit"] == 4800
    assert db.scalars(select(Generation)).all() == []


def test_commit_inside_limit_still_writes(db, project):
    gen, seg = engine._commit_segment(
        db, project, _text(4800), "摘要",
        quality={"length_path": {"path": "compression", "limit": 4800}},
        chapter_range="第24章《夜半敲门》",
    )
    assert seg == 1
    assert hanzi_count(gen.text) == 4800
    assert gen.chapter_range == "第24章《夜半敲门》"
