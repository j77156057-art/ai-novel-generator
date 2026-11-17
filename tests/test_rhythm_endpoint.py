"""B6：高潮密度蒸馏端点（显式触发 + 采样上限 + 无渠道拒绝）。

直接调路由函数，provider 用假实现（不联网、不建 FastAPI app）。
"""
import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.models import NovelChunk, Project, RawNovel  # noqa: E402
from app.routers import rhythm as rhythm_router  # noqa: E402
from app.routers.rhythm import RhythmDistillIn, distill_rhythm  # noqa: E402


class _FakeProvider:
    label = "fake"

    def chat(self, system, user):
        return '{"emotion": "爽"}'


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


def _corpus(db, chapters: int = 6) -> tuple[Project, RawNovel]:
    novel = RawNovel(title="参考书")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(1, chapters + 1):
        db.add(NovelChunk(novel_id=novel.id, chapter_no=i, idx=0, text=f"第{i}章 正文"))
    proj = Project(title="t", reference_novel_ids=[novel.id])
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj, novel


def test_distill_writes_plot_rhythm_with_sampling(db, monkeypatch):
    proj, novel = _corpus(db, chapters=6)
    monkeypatch.setattr(rhythm_router, "_provider_or_none", lambda: _FakeProvider())

    out = distill_rhythm(proj.id, RhythmDistillIn(novel_ids=[novel.id], max_chapters=3), db)

    assert out["sampled_chapters"] == 3
    assert out["max_chapters"] == 3
    assert out["avg_node_segments"] == 3  # 全部高潮 → 3（clamp 下限）
    db.refresh(proj)
    assert proj.plot_rhythm["source_ids"] == [novel.id]
    assert proj.plot_rhythm["sampled_chapters"] == 3


def test_distill_defaults_to_project_reference_novels(db, monkeypatch):
    proj, _novel = _corpus(db, chapters=4)
    monkeypatch.setattr(rhythm_router, "_provider_or_none", lambda: _FakeProvider())

    out = distill_rhythm(proj.id, RhythmDistillIn(), db)

    assert out["sampled_chapters"] == 4


def test_distill_rejects_without_channel(db, monkeypatch):
    proj, _novel = _corpus(db)
    monkeypatch.setattr(rhythm_router, "_provider_or_none", lambda: None)

    with pytest.raises(HTTPException) as err:
        distill_rhythm(proj.id, RhythmDistillIn(), db)
    assert err.value.status_code == 503


def test_distill_rejects_project_without_corpus(db, monkeypatch):
    proj = Project(title="t", reference_novel_ids=[])
    db.add(proj)
    db.commit()
    monkeypatch.setattr(rhythm_router, "_provider_or_none", lambda: _FakeProvider())

    with pytest.raises(HTTPException) as err:
        distill_rhythm(proj.id, RhythmDistillIn(), db)
    assert err.value.status_code == 400


def test_distill_rejects_bad_sample_cap(db, monkeypatch):
    proj, _novel = _corpus(db)
    monkeypatch.setattr(rhythm_router, "_provider_or_none", lambda: _FakeProvider())

    with pytest.raises(HTTPException) as err:
        distill_rhythm(proj.id, RhythmDistillIn(max_chapters=0), db)
    assert err.value.status_code == 400
