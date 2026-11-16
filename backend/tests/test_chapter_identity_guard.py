# -*- coding: utf-8 -*-
"""生成侧硬判据 1：章号唯一生产者，解析失败拒绝写库、不得用段号顶替。

锁住三件事：

1. `chapter_labels.require_label` 对空标签 / `段N` / 乱写文本 / 第0章一律拒绝，
   报错里必须带段号、原文与原因；
2. `_commit_segment` 在写库前校验：非法章标签不产生 Generation 行；
   `chapter_range=None` 仍是显式段模式（写 `段N`），不是解析失败后的兜底；
3. `_enqueue_v3_extraction(chapter=None)` 以空章号入队，不再把段号当章号（历史污染源）。
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app.generation.engine as engine  # noqa: E402
from app.generation.chapter_labels import (  # noqa: E402
    ChapterIdentityError,
    require_label,
)
from app.models import Base, Generation, Project  # noqa: E402


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
    p = Project(title="章身份测试", generation_length=1500)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.mark.parametrize("label", ["段20", "", "   ", "第三章节", "第0章", "最后一章"])
def test_require_label_rejects_non_chapter(label):
    with pytest.raises(ChapterIdentityError) as ei:
        require_label(label, segment_no=20, where="test")
    msg = str(ei.value)
    assert "第20段" in msg
    assert repr(label) in msg
    assert ei.value.to_dict()["status"] == "blocked"


def test_require_label_accepts_resolved_labels():
    assert require_label("第43章《本群禁止飞升》", segment_no=43) == (43, 43)
    assert require_label("第23–43章") == (23, 43)


def test_commit_rejects_segment_label_and_writes_nothing(db, project):
    with pytest.raises(ChapterIdentityError) as ei:
        engine._commit_segment(
            db, project, "他把门关上。", "摘要", chapter_range="段20")
    # 新项目下一段是 1：报错指向「将要写的这一段」，原文原样带回
    assert ei.value.segment_no == 1
    assert ei.value.label == "段20"
    assert "段号" in str(ei.value)
    assert db.scalars(select(Generation)).all() == []


def test_commit_keeps_explicit_segment_mode(db, project):
    gen, seg = engine._commit_segment(
        db, project, "他把门关上。", "摘要", chapter_range=None)
    assert seg == 1
    assert gen.chapter_range == "段1"


def test_enqueue_passes_none_instead_of_segment_no(project, monkeypatch):
    import app.kg.batch_worker as bw

    seen = {}

    def fake_enqueue(db, project_id, segment_no, text, chapter=None, *,
                     rebuild=False):
        seen.update(project_id=project_id, segment_no=segment_no,
                    chapter=chapter)
        return 7

    monkeypatch.setattr(bw, "enqueue_segment", fake_enqueue)
    monkeypatch.setattr(bw._queue, "pending_count", lambda pid: 0)

    out = engine._enqueue_v3_extraction(
        None, project, 20, "他把门关上。", chapter=None)
    assert out["enqueued"] is True
    assert seen["segment_no"] == 20
    # 关键：章号是 None，不是段号 20
    assert seen["chapter"] is None
