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


# ── 续写：力量体系只看被续写书的题材 ─────────────────────────────────


def _cont_ctx(**overrides) -> dict:
    novels = [
        {"id": 7, "title": "修真聊天群.txt", "genre": None},
        {"id": 8, "title": "豪门宠婚.txt", "genre": None},
        {"id": 9, "title": "某本游戏书.txt", "genre": "游戏"},
    ]
    return _ctx(novels=novels, project_corpus_ids=[7, 8, 9], **overrides)


def test_continuation_adds_system_for_power_genre_book():
    order = flow.step_order(
        flow.KIND_CONTINUATION,
        {"branch": flow.KIND_CONTINUATION},
        _ctx(novels=[{"id": 7, "title": "修真聊天群.txt", "genre": None}],
             project_corpus_ids=[7]),
    )
    # 书名关键词识别出「修真 → 仙侠」，即使语料 genre 为空。
    assert "system" in order
    assert order.index("continue_from") < order.index("system") < order.index("length")


def test_continuation_skips_system_for_non_power_book():
    order = flow.step_order(
        flow.KIND_CONTINUATION,
        {"branch": flow.KIND_CONTINUATION},
        _ctx(novels=[{"id": 8, "title": "豪门宠婚.txt", "genre": None}],
             project_corpus_ids=[8], project_genre=None),
    )
    assert "system" not in order


def test_continuation_system_follows_book_scope():
    ctx = _cont_ctx(continuation_novel_id=8)

    default_order = flow.step_order(
        flow.KIND_CONTINUATION, {"branch": flow.KIND_CONTINUATION}, ctx,
    )
    picked_order = flow.step_order(
        flow.KIND_CONTINUATION,
        {"branch": flow.KIND_CONTINUATION, "book_scope": 7},
        ctx,
    )
    assert default_order.index("book_scope") < default_order.index("length")
    assert "system" not in default_order
    assert "system" in picked_order


def test_continuation_summary_shows_system_only_when_applicable():
    ctx = _cont_ctx(continuation_novel_id=8)

    def steps(_answers: dict) -> set[str]:
        return {
            row["step"]
            for row in flow.summary_rows(flow.KIND_CONTINUATION, _answers, ctx)
        }

    assert "system" not in steps({"branch": flow.KIND_CONTINUATION})
    assert "system" in steps(
        {"branch": flow.KIND_CONTINUATION, "book_scope": 7}
    )


def test_overrides_cannot_move_corpus_or_distill():
    order = flow.step_order(
        flow.KIND_ORIGINAL,
        {"branch": flow.KIND_ORIGINAL, "corpus": [7]},
        _ctx(overrides={"order": ["outline", "genre", "distill"]}),
    )
    assert order[0] == flow.FIRST_STEP
    assert order[1] == "corpus"
    assert order.index("corpus") < order.index("distill")


def test_original_order_has_cards_step_between_outline_and_confirm():
    order = flow.step_order(
        flow.KIND_ORIGINAL,
        {"branch": flow.KIND_ORIGINAL, "outline": True},
        _ctx(),
    )
    assert order.index("outline") < order.index("cards") < order.index("confirm")


def test_original_order_skips_cards_when_writing_directly():
    order = flow.step_order(
        flow.KIND_ORIGINAL,
        {"branch": flow.KIND_ORIGINAL, "outline": False},
        _ctx(),
    )
    assert "cards" not in order
    assert order.index("outline") < order.index("confirm")


def test_cards_step_renders_bible_characters():
    question = flow.build_question(
        "cards",
        {"branch": flow.KIND_ORIGINAL, "outline": True},
        _ctx(
            bible_characters=[
                {"name": "林深", "role": "主角", "has_card": True},
                {"name": "周遥", "role": "配角", "has_card": False},
            ]
        ),
    )
    assert question["card"]["type"] == "cards"
    assert question["card"]["characters"][0]["name"] == "林深"
    assert question["can_skip"] is False


def test_cards_answer_accepts_true_only():
    ctx = _ctx()
    assert flow.validate("cards", True, flow.KIND_ORIGINAL, {}, ctx) is True
    with pytest.raises(flow.AnswerError):
        flow.validate("cards", "乱写", flow.KIND_ORIGINAL, {}, ctx)


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


# ── 角色卡步骤 ───────────────────────────────────────────────────────


def test_outline_answer_enters_cards_and_builds_bible(db, monkeypatch):
    project, session = _seed(db)
    session.answers = {"branch": flow.KIND_ORIGINAL, "corpus": [], "outline": True}
    session.step_key = "outline"
    db.commit()

    calls: list[int] = []

    def fake_build(_db, proj, _answers=None):
        calls.append(proj.id)
        proj.plot_bible = {
            "characters": [
                {"name": "林深", "role": "主角", "card": {"声纹": {"自称": "我"}}},
            ]
        }
        _db.commit()
        return proj.plot_bible, {
            "generated": 1, "names": ["林深"], "missing": [], "collisions": 0,
        }

    monkeypatch.setattr(wizard, "_build_bible", fake_build)

    out = wizard._advance(db, project, session, "outline", True, BackgroundTasks(), "好")

    assert calls == [project.id]
    assert session.step_key == "cards"
    assert out["question"]["card"]["type"] == "cards"
    assert out["question"]["card"]["characters"][0]["has_card"] is True
    assert out["cards_preview"]["stage"] == "cards"
    assert out["cards_preview"]["character_cards"]["generated"] == 1


def test_commit_reuses_generated_cards(db, monkeypatch):
    project, session = _seed(db)
    session.answers = {"branch": flow.KIND_ORIGINAL, "outline": True, "cards": True}
    session.step_key = "confirm"
    project.plot_bible = {
        "characters": [
            {"name": "林深", "role": "主角", "card": {"声纹": {"自称": "我"}}},
        ]
    }
    db.commit()

    called: list[int] = []
    monkeypatch.setattr(wizard, "_build_bible", lambda *a, **k: called.append(1))

    out = wizard._commit(db, project, session, BackgroundTasks())

    assert called == []
    stats = out["result"]["character_cards"]
    assert stats["reason"] == "reused"
    assert stats["generated"] == 1
    assert "沿用" in out["result"]["message"]


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
