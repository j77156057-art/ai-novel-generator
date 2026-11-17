# -*- coding: utf-8 -*-
"""段模式生成入口必须分配章号，失败不得回落成 `段N`。

锁三件事：

1. 助手 `generate_next` 与动作提案 `invoke_generation` 两条裸调用点都先走
   `chapter_labels.resolve()`，把「第N章」传给引擎；
2. 库里只有历史 `段N` 行时，下一段仍分配「第1章」——不把段号当章号；
3. `resolve` 失败时直接抛错，**不调用生成**，绝不兜底成 `段N`。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.agents import action_proposals  # noqa: E402
from app.generation import chapter_labels, engine  # noqa: E402
from app.generation.chapter_labels import ChapterRangeError, resolve  # noqa: E402
from app.models import Base, Generation, Project  # noqa: E402
from app.routers import assistant_generation  # noqa: E402


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
    p = Project(title="段模式章号分配", generation_length=1500)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _row(db, project, segment_no=1, label="段1"):
    row = Generation(project_id=project.id, segment_no=segment_no,
                     chapter_range=label, text="旧稿正文")
    db.add(row)
    db.commit()
    return row


def test_resolve_assigns_chapter_one_when_no_chapters(db, project):
    label = resolve(db, project, None)["label"]
    assert label.startswith("第1章")
    assert "段" not in label


def test_resolve_ignores_legacy_segment_rows_and_leaves_them_untouched(db, project):
    old = _row(db, project, 1, "段1")
    assert resolve(db, project, None)["label"].startswith("第1章")
    db.refresh(old)
    assert old.chapter_range == "段1"  # 历史行一个字节都不改


def test_resolve_increments_from_existing_chapter(db, project):
    _row(db, project, 1, "第1章《测试》")
    assert resolve(db, project, None)["label"].startswith("第2章")


def test_assistant_generate_next_passes_resolved_chapter(db, project, monkeypatch):
    captured = {}

    def fake_generate(db_, project_, **kwargs):
        captured.update(kwargs)
        return {"segment_no": 1, "text": "新正文"}

    monkeypatch.setattr(assistant_generation, "generate_next_segment", fake_generate)
    assistant_generation._generate_next_with_chapter(db, project, 3000)
    assert captured["chapter_range"].startswith("第1章")
    assert "段" not in captured["chapter_range"]


def test_assistant_generate_next_refuses_to_fall_back_to_segment(db, project, monkeypatch):
    def boom(*args, **kwargs):
        raise ChapterRangeError("章号解析失败")

    called = {"generate": False}

    def fake_generate(*args, **kwargs):
        called["generate"] = True
        return {"segment_no": 1, "text": ""}

    monkeypatch.setattr(chapter_labels, "resolve", boom)
    monkeypatch.setattr(assistant_generation, "generate_next_segment", fake_generate)
    with pytest.raises(ChapterRangeError):
        assistant_generation._generate_next_with_chapter(db, project, 3000)
    assert called["generate"] is False


def test_action_proposal_invoke_generation_passes_resolved_chapter(
        db, project, monkeypatch):
    from app.agents import workflow

    captured = {}

    def fake_generate(db_, project_, **kwargs):
        captured.update(kwargs)
        return {"segment_no": 1, "text": "新正文"}

    monkeypatch.setattr(workflow, "generation_block", lambda *a, **k: None)
    monkeypatch.setattr(engine, "generate_next_segment", fake_generate)
    action_proposals._exec_invoke_generation(
        db, project, SimpleNamespace(body_json={}))
    assert captured["chapter_range"].startswith("第1章")
    assert "段" not in captured["chapter_range"]


def test_action_proposal_refuses_to_fall_back_to_segment(db, project, monkeypatch):
    from app.agents import workflow

    def boom(*args, **kwargs):
        raise ChapterRangeError("章号解析失败")

    called = {"generate": False}

    def fake_generate(*args, **kwargs):
        called["generate"] = True
        return {"segment_no": 1, "text": ""}

    monkeypatch.setattr(workflow, "generation_block", lambda *a, **k: None)
    monkeypatch.setattr(chapter_labels, "resolve", boom)
    monkeypatch.setattr(engine, "generate_next_segment", fake_generate)
    with pytest.raises(ChapterRangeError):
        action_proposals._exec_invoke_generation(
            db, project, SimpleNamespace(body_json={}))
    assert called["generate"] is False
