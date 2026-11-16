"""章纲写端点的路由级测试：内存 SQLite + 假 provider，不跑生成、不写生产库。

跑法：项目根 `python -m pytest backend/tests/test_chapter_outline_api.py -q`。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.generation.planning import source_fingerprint
from app.models import Generation, Project
from app.routers.chapter_outline import (
    ChapterOutlineApply,
    ChapterOutlineDraft,
    ChapterOutlineEntryIn,
    ChapterOutlinePut,
    apply_chapter_outline,
    delete_chapter_outline,
    draft_chapter_outline,
    list_chapter_outline,
    put_chapter_outline,
)

PREMISE = "医院值班室的金属托盘里放着一把镊子和几卷纱布。"


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


def _seed(db, *, outline_len: int = 40, written: int = 43) -> Project:
    project = Project(
        title="测试项目",
        outline=[f"第{i}章：内容{i}" for i in range(1, outline_len + 1)],
        premise=PREMISE,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    for chapter in range(1, written + 1):
        db.add(Generation(
            project_id=project.id,
            segment_no=chapter,
            chapter_range=f"第{chapter}章",
            text="正文",
        ))
    db.commit()
    db.refresh(project)
    return project


def _put(entry="第44章《夜审》：晚上10点，林川在值班室拿起镊子。", **overrides):
    data = {
        "entry": entry,
        "expected_fingerprint": overrides.pop("expected_fingerprint"),
        "confirm": overrides.pop("confirm", True),
    }
    return ChapterOutlinePut(**data)


# ── GET 区间查看 ────────────────────────────────────────────────────────


def test_list_marks_missing_and_beyond_end_explicitly(db):
    project = _seed(db)
    result = list_chapter_outline(project.id, from_chapter=1, to_chapter=44, db=db)

    assert result["outline_len"] == 40
    assert result["written_chapters"] == 43
    assert result["missing_chapters"] == [41, 42, 43, 44]
    first_missing = next(e for e in result["entries"] if e["chapter"] == 41)
    assert first_missing == {
        "chapter": 41, "entry": None, "status": "missing",
        "written": True, "node_id": None, "node_range": None,
    }
    assert result["source_fingerprint"]
    assert "node_mapping:no_timeline" in result["skipped_checks"]
    assert any(c["rule"] == "outline_coverage" and c["status"] == "failed"
               for c in result["checks"])


def test_list_maps_saved_node_windows(db):
    project = _seed(db)
    project.plot_timeline = {"nodes": {
        "n1": {"id": "n1", "chapter_start": 1, "chapter_end": 20,
               "title": "开局", "entry_condition": "", "exit_condition": ""},
        "n2": {"id": "n2", "chapter_start": 21, "chapter_end": 44,
               "title": "后段", "entry_condition": "", "exit_condition": ""},
    }}
    db.commit()

    result = list_chapter_outline(project.id, from_chapter=41, to_chapter=41, db=db)
    assert result["entries"][0]["node_id"] == "n2"
    assert result["entries"][0]["node_range"] == [21, 44]
    assert "node_mapping:no_timeline" not in result["skipped_checks"]


# ── 单章保存 ────────────────────────────────────────────────────────────


def test_put_extends_outline_and_reports_auto_filled(db):
    project = _seed(db)
    fingerprint = source_fingerprint(project)

    result = put_chapter_outline(
        project.id, 44, _put(expected_fingerprint=fingerprint), db=db,
    )
    assert result["entry"]["status"] == "confirmed"
    assert result["auto_filled_missing"] == [41, 42, 43]
    assert result["changed"] == [44]
    assert result["source_fingerprint"] != fingerprint

    db.refresh(project)
    assert len(project.outline) == 44
    assert project.outline[40:43] == ["", "", ""]
    assert "第44章" in project.outline[43]


def test_put_rejects_stale_fingerprint(db):
    project = _seed(db)
    with pytest.raises(HTTPException) as exc:
        put_chapter_outline(
            project.id, 44, _put(expected_fingerprint="stale"), db=db,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "stale"


def test_put_requires_explicit_confirm(db):
    project = _seed(db)
    with pytest.raises(HTTPException) as exc:
        put_chapter_outline(
            project.id, 44,
            _put(expected_fingerprint=source_fingerprint(project), confirm=False),
            db=db,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "confirm_required"


def test_put_empty_entry_is_422_with_report(db):
    project = _seed(db)
    with pytest.raises(HTTPException) as exc:
        put_chapter_outline(
            project.id, 44,
            _put(entry="   ", expected_fingerprint=source_fingerprint(project)),
            db=db,
        )
    assert exc.value.status_code == 422
    report = exc.value.detail["report"]
    assert any(e["code"] == "empty_entry" for e in report["errors"])


def test_put_quoted_unknown_term_is_422_with_position(db):
    project = _seed(db)
    with pytest.raises(HTTPException) as exc:
        put_chapter_outline(
            project.id, 44,
            _put(entry="第44章：他拿起「录音笔」，走进值班室。",
                 expected_fingerprint=source_fingerprint(project)),
            db=db,
        )
    assert exc.value.status_code == 422
    error = next(e for e in exc.value.detail["report"]["errors"]
                 if e["code"] == "authority_quoted_term_unknown")
    assert error["term"] == "录音笔"
    assert "录音笔" in error["position"]["snippet"]


def test_put_written_chapter_returns_warning(db):
    project = _seed(db)
    result = put_chapter_outline(
        project.id, 30,
        _put(entry="第30章：晚上9点，林川在值班室拿起镊子。",
             expected_fingerprint=source_fingerprint(project)),
        db=db,
    )
    assert any(w["code"] == "written_chapter_edit"
               for w in result["validation"]["warnings"])


# ── 区间保存 ────────────────────────────────────────────────────────────


def _apply_body(project, *, entries, reset=False):
    return ChapterOutlineApply(
        from_chapter=44, to_chapter=45,
        entries=[ChapterOutlineEntryIn(**item) for item in entries],
        expected_fingerprint=source_fingerprint(project),
        confirm=True, reset_timeline=reset,
    )


def test_apply_writes_two_chapters(db):
    project = _seed(db)
    body = _apply_body(project, entries=[
        {"chapter": 44, "entry": "第44章：晚上10点，林川在值班室拿起镊子。"},
        {"chapter": 45, "entry": "第45章：第二天早上7点，林川离开值班室。"},
    ])
    result = apply_chapter_outline(project.id, body, db=db)
    assert result["changed"] == [44, 45]
    assert result["auto_filled_missing"] == [41, 42, 43]
    assert result["validation"]["ok"] is True

    db.refresh(project)
    assert len(project.outline) == 45


def test_apply_gap_is_422(db):
    project = _seed(db)
    body = _apply_body(project, entries=[
        {"chapter": 44, "entry": "第44章：晚上10点，林川在值班室拿起镊子。"},
    ])
    with pytest.raises(HTTPException) as exc:
        apply_chapter_outline(project.id, body, db=db)
    assert exc.value.status_code == 422
    gap = next(e for e in exc.value.detail["report"]["errors"]
               if e["code"] == "chapter_gap")
    assert gap["chapters"] == [45]


def test_apply_reset_timeline_is_explicit(db):
    project = _seed(db)
    project.plot_timeline = {"nodes": {"n1": {"chapter_start": 1, "chapter_end": 44}}}
    db.commit()

    body = _apply_body(project, reset=True, entries=[
        {"chapter": 44, "entry": "第44章：晚上10点，林川在值班室拿起镊子。"},
        {"chapter": 45, "entry": "第45章：第二天早上7点，林川离开值班室。"},
    ])
    result = apply_chapter_outline(project.id, body, db=db)
    assert result["timeline_impact"]["reset_timeline"] is True
    db.refresh(project)
    assert project.plot_timeline is None


def test_apply_without_reset_keeps_timeline(db):
    project = _seed(db)
    project.plot_timeline = {"nodes": {"n1": {"chapter_start": 1, "chapter_end": 44}}}
    db.commit()

    body = _apply_body(project, entries=[
        {"chapter": 44, "entry": "第44章：晚上10点，林川在值班室拿起镊子。"},
        {"chapter": 45, "entry": "第45章：第二天早上7点，林川离开值班室。"},
    ])
    result = apply_chapter_outline(project.id, body, db=db)
    assert result["timeline_impact"]["reset_timeline"] is False
    assert result["timeline_impact"]["node_content_stale"] is True
    db.refresh(project)
    assert project.plot_timeline is not None


# ── 显式缺失 ────────────────────────────────────────────────────────────


def test_delete_marks_chapter_missing(db):
    project = _seed(db)
    result = delete_chapter_outline(
        project.id, 30, expected_fingerprint=source_fingerprint(project), db=db,
    )
    assert result["entry"]["status"] == "missing"
    assert result["changed"] == [30]
    # 删除是「显式允许缺省」，不该回执一条校验失败；跳过的检查要看得见。
    assert result["validation"]["ok"] is True
    assert "time_window:no_entry" in result["validation"]["skipped_checks"]
    db.refresh(project)
    assert project.outline[29] == ""


def test_delete_out_of_range_is_422(db):
    project = _seed(db)
    with pytest.raises(HTTPException) as exc:
        delete_chapter_outline(
            project.id, 99, expected_fingerprint=source_fingerprint(project), db=db,
        )
    assert exc.value.status_code == 422


# ── AI 起草（不落库） ───────────────────────────────────────────────────


class _FakeProvider:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, system: str, user: str) -> str:
        return self.reply


_DRAFT_REPLY = (
    '{"entries":["第44章《夜审》：晚上10点，林川在值班室拿起镊子。",'
    '"第45章《清晨》：第二天早上7点，林川离开值班室。"]}'
)


def test_draft_does_not_write_and_can_be_applied(db, monkeypatch):
    project = _seed(db)
    before = source_fingerprint(project)
    monkeypatch.setattr(
        "app.routers.chapter_outline._provider_or_none",
        lambda: _FakeProvider(_DRAFT_REPLY),
    )

    draft = draft_chapter_outline(
        project.id, ChapterOutlineDraft(from_chapter=44, to_chapter=45), db=db,
    )
    assert draft["status"] == "draft"
    assert draft["apply_requires_confirm"] is True
    assert draft["source_fingerprint"] == before
    assert [e["chapter"] for e in draft["entries"]] == [44, 45]
    db.refresh(project)
    assert len(project.outline) == 40

    apply_body = ChapterOutlineApply(
        from_chapter=44, to_chapter=45,
        entries=[ChapterOutlineEntryIn(**item) for item in draft["entries"]],
        expected_fingerprint=draft["source_fingerprint"],
        confirm=True,
    )
    applied = apply_chapter_outline(project.id, apply_body, db=db)
    assert applied["validation"]["ok"] is True
    db.refresh(project)
    assert len(project.outline) == 45


def test_draft_without_provider_is_503(db, monkeypatch):
    project = _seed(db)
    monkeypatch.setattr("app.routers.chapter_outline._provider_or_none", lambda: None)
    with pytest.raises(HTTPException) as exc:
        draft_chapter_outline(
            project.id, ChapterOutlineDraft(from_chapter=44, to_chapter=45), db=db,
        )
    assert exc.value.status_code == 503


def test_draft_range_too_large_is_422(db, monkeypatch):
    project = _seed(db)
    monkeypatch.setattr(
        "app.routers.chapter_outline._provider_or_none",
        lambda: _FakeProvider(_DRAFT_REPLY),
    )
    with pytest.raises(HTTPException) as exc:
        draft_chapter_outline(
            project.id, ChapterOutlineDraft(from_chapter=44, to_chapter=70), db=db,
        )
    assert exc.value.status_code == 422


def test_draft_parse_failure_is_502(db, monkeypatch):
    project = _seed(db)
    monkeypatch.setattr(
        "app.routers.chapter_outline._provider_or_none",
        lambda: _FakeProvider("不是 JSON"),
    )
    # 三次重试之间会 sleep；测试里去掉等待（planning 在函数内 import time，
    # 只能打在标准库模块属性上）。
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(HTTPException) as exc:
        draft_chapter_outline(
            project.id, ChapterOutlineDraft(from_chapter=44, to_chapter=45), db=db,
        )
    assert exc.value.status_code == 502
    assert exc.value.detail["error"] == "draft_failed"
