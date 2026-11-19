"""向导语料前置：步骤顺序、语料即时落库、蒸馏提前启动。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import wizard_flow as flow
from app.db import Base
from app.models import AssistantWizardSession, Project
from app.routers import assistant_wizard as wizard
from app.routers.wizard import _bible_user_prompt


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


def _ctx(**overrides) -> dict:
    base = {
        "novels": [],
        "overrides": {},
        "project_has_corpus": False,
        "project_corpus_ids": [],
        "needs_distill": False,
        "project_genre": None,
        "generation_length": 3000,
    }
    base.update(overrides)
    return base


# ── 步骤顺序 ─────────────────────────────────────────────────────────


def test_original_order_starts_with_corpus_before_genre():
    order = flow.step_order(flow.KIND_ORIGINAL, {}, _ctx())
    assert order[:2] == [flow.FIRST_STEP, "corpus"]
    assert "distill" not in order
    assert order.index("corpus") < order.index("genre")


def test_one_corpus_adds_distill_between_corpus_and_genre():
    order = flow.step_order(
        flow.KIND_ORIGINAL,
        {"branch": flow.KIND_ORIGINAL, "corpus": [7]},
        _ctx(),
    )
    assert "book_scope" not in order
    assert order.index("corpus") < order.index("distill") < order.index("genre")


def test_two_corpora_insert_book_scope_before_distill():
    order = flow.step_order(
        flow.KIND_ORIGINAL,
        {"branch": flow.KIND_ORIGINAL, "corpus": [7, 8]},
        _ctx(),
    )
    assert order.index("corpus") < order.index("book_scope") < order.index("distill")
    assert order.index("distill") < order.index("genre")


def test_overrides_cannot_move_corpus_or_distill():
    order = flow.step_order(
        flow.KIND_ORIGINAL,
        {"branch": flow.KIND_ORIGINAL, "corpus": [7]},
        _ctx(overrides={"order": ["outline", "genre", "distill"]}),
    )
    assert order[0] == flow.FIRST_STEP
    assert order[1] == "corpus"
    assert order.index("corpus") < order.index("distill")


# ── 语料即时落库 / 蒸馏提前启动 ──────────────────────────────────────


def _seed(db) -> tuple[Project, AssistantWizardSession]:
    project = Project(title="向导顺序测试")
    db.add(project)
    db.commit()
    db.refresh(project)
    session = AssistantWizardSession(
        project_id=project.id,
        kind=flow.KIND_ORIGINAL,
        status="active",
        answers={"branch": flow.KIND_ORIGINAL},
        history=["branch"],
        step_key="corpus",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return project, session


def test_corpus_answer_persists_reference_ids(db):
    project, session = _seed(db)
    wizard._advance(db, project, session, "corpus", [7], BackgroundTasks(), "好")
    db.refresh(project)
    assert project.reference_novel_ids == [7]
    assert session.step_key == "distill"


def test_distill_answer_starts_background_job(db, monkeypatch):
    project, session = _seed(db)
    session.answers = {"branch": flow.KIND_ORIGINAL, "corpus": [7]}
    session.step_key = "distill"
    db.commit()

    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        wizard, "_start_distill",
        lambda _db, proj, tier, _bg: calls.append((proj.id, tier)) or {"job_id": 1},
    )

    out = wizard._advance(db, project, session, "distill", "fast", BackgroundTasks(), "好")
    assert calls == [(project.id, "fast")]
    assert project.narrative_tier == "fast"
    assert "蒸馏已在后台开始" in (out.get("ack") or "")


def test_distill_start_failure_does_not_fail_answer(db, monkeypatch):
    project, session = _seed(db)
    session.answers = {"branch": flow.KIND_ORIGINAL, "corpus": [7]}
    session.step_key = "distill"
    db.commit()

    def boom(*_args, **_kwargs):
        raise RuntimeError("no chunks")

    monkeypatch.setattr(wizard, "_start_distill", boom)

    out = wizard._advance(db, project, session, "distill", "fast", BackgroundTasks(), "好")
    assert "没能立即启动" in (out.get("ack") or "")
    assert session.step_key == "genre"


# ── 剧情圣经注入 ─────────────────────────────────────────────────────


def _project(**kwargs) -> SimpleNamespace:
    base = {"genre": "悬疑", "style": "番茄", "premise": "浮标事故"}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_bible_prompt_injects_digest_and_caps_sample():
    text = _bible_user_prompt(_project(), {}, "S" * 3000, "DIGEST")
    assert "蒸馏文风档案" in text and "DIGEST" in text
    assert "S" * 1200 in text
    assert "S" * 1201 not in text


def test_bible_prompt_without_digest_keeps_longer_sample():
    text = _bible_user_prompt(_project(), {}, "S" * 3000, "")
    assert "蒸馏文风档案" not in text
    assert "S" * 2000 in text
