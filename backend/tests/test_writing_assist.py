# -*- coding: utf-8 -*-
"""助手写作辅助：章前建议、写作进度、开篇注入、句子区间、章节消息。"""
from __future__ import annotations

import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
from app.generation.opening_options import build_opening_options  # noqa: E402
from app.generation.prompts import build_user_prompt  # noqa: E402
from app.generation.writing_progress import build_writing_progress  # noqa: E402
from app.models import Generation, Project  # noqa: E402
from app.routers.assistant_wizard import AppendMessageIn, append_message  # noqa: E402
from app.routers.generation import list_generation_sentences  # noqa: E402
from app.routers.writing_assist import writing_progress  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _project(db, **kwargs) -> Project:
    outline = [f"第{i}章：第 {i} 章的事件。" for i in range(1, 6)]
    project = Project(
        title="写作辅助测试",
        outline=outline,
        generation_length=4000,
        target_length=20000,
        plot_bible={
            "characters": [
                {"name": "许照", "role": "房东，背着房贷"},
                {"name": "钱多多", "role": "车库租客，跑腿"},
            ]
        },
        **kwargs,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _gen(db, project: Project, no: int, label: str, text: str = "") -> Generation:
    row = Generation(
        project_id=project.id,
        segment_no=no,
        chapter_range=label,
        text=text or f"第 {no} 章正文。",
        summary={"text": f"第 {no} 章摘要。"},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_writing_progress_counts_chapters(db):
    project = _project(db)
    _gen(db, project, 1, "第1章")
    _gen(db, project, 2, "第2章")

    got = build_writing_progress(db, project)
    assert got["current_chapter"] == 2
    assert got["next_chapter"] == 3
    assert got["generated_chapters"] == 2
    assert got["outline_total"] == 5
    assert got["outline_percent"] == 40.0
    assert got["to_outline_percent"] == 60.0
    assert got["timeline"]["exists"] is False

    endpoint = writing_progress(project.id, db)
    assert endpoint["current_chapter"] == 2


def test_opening_options_fallback_for_first_and_later_chapter(db):
    project = _project(db)
    _gen(db, project, 1, "第1章")

    first = build_opening_options(db, project, 1, use_model=False)
    assert first["is_first_chapter"] is True
    assert first["source"] == "fallback"
    assert len(first["options"]) == 3

    later = build_opening_options(db, project, 3, use_model=False)
    assert later["is_first_chapter"] is False
    assert later["source"] == "fallback"
    assert len(later["options"]) == 3
    assert later["options"][0]["opening"]


def test_chapter_opening_block_is_injected_only_when_given():
    base = build_user_prompt([], [], "", 4000, {}, "")
    with_block = build_user_prompt(
        [], [], "", 4000, {}, "", chapter_opening_block="从车库的凉气切入。"
    )
    assert "本章开场要求" not in base
    assert "本章开场要求" in with_block
    assert "从车库的凉气切入。" in with_block


def test_sentences_endpoint_returns_indices(db):
    project = _project(db)
    gen = _gen(db, project, 1, "第1章", text="第一句。第二句！第三句？")

    got = list_generation_sentences(project.id, gen.id, db)
    assert got["count"] == 3
    assert [item["index"] for item in got["items"]] == [1, 2, 3]
    assert got["items"][0]["text"] == "第一句。"


def test_append_chapter_message_is_idempotent(db):
    project = _project(db)
    body = AppendMessageIn(
        role="assistant",
        kind="chapter",
        payload={"generation_id": 7, "segment_no": 1, "chars": 1200},
        dedupe_key="chapter:7",
    )
    first = append_message(project.id, body, db)
    second = append_message(project.id, body, db)
    assert first["kind"] == "chapter"
    assert first["id"] == second["id"]
    assert first["payload"]["generation_id"] == 7
