"""生成侧章纲回执（S4）的定向测试：内存 SQLite + 桩，不跑真实生成。

跑法：项目根 `python -m pytest backend/tests/test_generation_outline_notice.py -q`。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.generation.outline_notice import build_notice
from app.models import Generation, Project

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
    """40 条章纲 + 43 章正文：历史库的典型形态（41–43 有正文没章纲）。"""
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


# ── 回执本身 ────────────────────────────────────────────────────────────


def test_missing_chapter_is_explicit_and_never_blocking(db):
    project = _seed(db)
    notice = build_notice(db, project, 41, 41)

    assert notice["chapters"] == [{
        "chapter": 41, "status": "missing", "written": True, "prompt_entry": None,
    }]
    assert notice["missing_chapters"] == [41]
    assert notice["first_missing_chapter"] == 41
    assert notice["prompt_has_outline"] is False
    assert notice["blocking"] is False
    # 「缺省」本身是 S2 允许的显式状态，不算校验错误；缺失靠 status/missing_chapters 报。
    assert notice["validation"]["ok"] is True


def test_confirmed_chapter_matches_what_prompt_sees(db):
    project = _seed(db)
    notice = build_notice(db, project, 40, 40)

    assert notice["chapters"][0]["status"] == "confirmed"
    assert notice["chapters"][0]["prompt_entry"] == "第40章：内容40"
    assert notice["validation"]["ok"] is True
    assert notice["prompt_has_outline"] is True
    # 保存侧的「跳过」口径原样带过来：没有时间标记就不假装查过时间窗。
    assert (
        "time_window_between_chapters:no_absolute_time_tokens_in_range"
        in notice["validation"]["skipped_checks"]
    )


def test_unwritten_tail_reports_beyond_end(db):
    project = _seed(db)
    notice = build_notice(db, project, 44, 44)

    assert notice["chapters"][0]["status"] == "beyond_end"
    assert notice["chapters"][0]["written"] is False
    assert notice["missing_chapters"] == [44]


def test_range_covers_multiple_chapters(db):
    project = _seed(db)
    notice = build_notice(db, project, 40, 42)

    assert [item["chapter"] for item in notice["chapters"]] == [40, 41, 42]
    assert notice["missing_chapters"] == [41, 42]
    assert notice["prompt_has_outline"] is False


def test_notice_carries_note_and_impact_wording(db):
    project = _seed(db)
    notice = build_notice(db, project, 41, 41)

    assert "不阻断生成" in notice["note"]
    assert "node_content_stale" in notice["note"]


# ── 路由接线 ────────────────────────────────────────────────────────────


def test_generate_response_carries_outline_notice(db, monkeypatch):
    project = _seed(db)
    # 只验证回执接线：闸门与模型都打桩，不碰真实生成。
    monkeypatch.setattr(
        "app.routers.generation._require_workflow_ready", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "app.routers.generation.generate_next_segment",
        lambda *a, **k: {
            "system_prompt": "s", "user_prompt": "u", "constraints": {},
            "rag_context": "", "warnings": [], "repetition": None, "length": {},
        },
    )

    from app.routers.generation import GenerateBody, generate

    result = generate(GenerateBody(project_id=project.id), db=db)

    # 原有键一个不少，新增只多一个 outline。
    assert result["text"] == "正文"
    assert "debug" in result
    assert result["outline"]["chapters"][0]["chapter"] == 44
    assert result["outline"]["chapters"][0]["status"] == "beyond_end"
    assert result["outline"]["blocking"] is False
