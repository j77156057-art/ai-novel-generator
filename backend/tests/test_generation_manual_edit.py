"""手动改正文端点（`PATCH /api/generations/{pid}/{generation_id}`）的定向测试。

这条路径与生成/改写不同：不调模型、不走提案，用户直接改字。最容易出错的
边界有三个：
1. 只改 text —— 段号与章身份不能跟着变；
2. 旧正文上的复读/文风结论必须失效（quality_json 清空），不能冒充新正文；
3. 空正文、跨项目 id 必须拒绝。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Generation, Project
from app.routers.generation import (
    UpdateGenerationTextBody,
    _resync_after_manual_edit,
    update_generation_text,
)


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


@pytest.fixture()
def stub_cascade(monkeypatch):
    """端点测试只关心正文与质量标记；配套重算单独测，避免连模型/图谱。"""
    from app.routers import generation as gen_router

    calls: dict = {}

    def fake(db, project, gen, text):
        calls["text"] = text
        return {
            "skipped": False,
            "summary": True,
            "tree": True,
            "kg": True,
            "v3_enqueued": True,
            "errors": [],
        }

    monkeypatch.setattr(gen_router, "_resync_after_manual_edit", fake)
    return calls


def _seed(db) -> tuple[Project, Generation]:
    project = Project(title="手动改正文测试")
    db.add(project)
    db.commit()
    db.refresh(project)
    gen = Generation(
        project_id=project.id,
        segment_no=3,
        chapter_range="第3章",
        text="旧正文",
        quality_json='{"repetition": {"verdict": "warn"}}',
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return project, gen


def test_updates_text_and_invalidates_quality(db, stub_cascade):
    project, gen = _seed(db)

    out = update_generation_text(
        project.id,
        gen.id,
        UpdateGenerationTextBody(text="新的正文。"),
        db,
    )

    assert out["text"] == "新的正文。"
    # 身份不变：改字不等于换段/换章
    assert out["segment_no"] == 3
    assert out["chapter_range"] == "第3章"
    # 旧正文的结论失效 → 回到未检测
    assert out["quality"]["repetition"]["verdict"] == "undetected"
    # 配套重算拿到的是落库后的新正文
    assert stub_cascade["text"] == "新的正文。"
    assert out["cascade"]["summary"] is True


def test_normalizes_line_endings_and_trims(db, stub_cascade):
    project, gen = _seed(db)

    out = update_generation_text(
        project.id,
        gen.id,
        UpdateGenerationTextBody(text="  第一行\r\n第二行\r第三行  "),
        db,
    )

    assert out["text"] == "第一行\n第二行\n第三行"


def test_rejects_empty_text(db):
    project, gen = _seed(db)

    with pytest.raises(HTTPException) as exc:
        update_generation_text(
            project.id,
            gen.id,
            UpdateGenerationTextBody(text="   \n  "),
            db,
        )

    assert exc.value.status_code == 400


def test_rejects_generation_from_another_project(db):
    project, gen = _seed(db)
    other = Project(title="别的项目")
    db.add(other)
    db.commit()
    db.refresh(other)

    with pytest.raises(HTTPException) as exc:
        update_generation_text(
            other.id,
            gen.id,
            UpdateGenerationTextBody(text="不该生效"),
            db,
        )

    assert exc.value.status_code == 404


def _patch_cascade(monkeypatch, *, fail_summary: bool = False) -> None:
    """把配套重算的外部依赖换成假的：不连模型、不跑真图谱。"""
    import app.generation.engine as engine_mod
    import app.kg.build as kg_build
    import app.llm.provider as provider_mod

    class FakeProvider:
        def summarize(self, text: str) -> str:
            if fail_summary:
                raise RuntimeError("模型超时")
            return f"摘要：{text[:6]}"

    monkeypatch.setattr(provider_mod, "get_llm_provider", lambda: FakeProvider())
    monkeypatch.setattr(engine_mod, "rebuild_summary_tree", lambda db, project, provider: {})
    monkeypatch.setattr(
        kg_build,
        "rebuild_generated_kg",
        lambda db, project_id: {
            "complete": True, "segments": 1, "replayed": 1, "failed": [],
        },
    )
    monkeypatch.setattr(
        engine_mod, "_enqueue_v3_extraction", lambda *a, **k: {"enqueued": True, "pending": 1},
    )


def test_cascade_updates_summary_tree_and_kg(db, monkeypatch):
    project, gen = _seed(db)
    gen.text = "新的正文。"
    db.commit()
    db.refresh(gen)
    _patch_cascade(monkeypatch)

    out = _resync_after_manual_edit(db, project, gen, "新的正文。")

    assert out["summary"] is True
    assert out["tree"] is True
    assert out["kg"] is True
    assert out["v3_enqueued"] is True
    assert out["errors"] == []
    db.refresh(gen)
    assert (gen.summary or {}).get("text", "").startswith("摘要：")


def test_cascade_falls_back_when_summary_model_fails(db, monkeypatch):
    project, gen = _seed(db)
    gen.text = "新的正文。"
    db.commit()
    db.refresh(gen)
    _patch_cascade(monkeypatch, fail_summary=True)

    out = _resync_after_manual_edit(db, project, gen, "新的正文。")

    # 模型失败 → 退回确定性截断摘要；摘要树仍然可重建
    assert out["summary"] is True
    assert out["tree"] is True
    assert out["kg"] is True
    assert any("截断兜底" in item for item in out["errors"])
    db.refresh(gen)
    assert (gen.summary or {}).get("text", "")


def test_cascade_skips_tree_when_summary_totally_fails(db, monkeypatch):
    project, gen = _seed(db)
    gen.text = "新的正文。"
    db.commit()
    db.refresh(gen)
    _patch_cascade(monkeypatch, fail_summary=True)
    # 连确定性兜底也失败：此时只能保留旧摘要，并跳过摘要树
    import app.llm.provider as provider_mod

    monkeypatch.setattr(
        provider_mod.LLMProvider,
        "summarize",
        lambda self, text: (_ for _ in ()).throw(RuntimeError("兜底也失败")),
    )

    out = _resync_after_manual_edit(db, project, gen, "新的正文。")

    assert out["summary"] is False
    assert out["tree"] is False
    assert out["kg"] is True
    assert any("剧情总结" in item for item in out["errors"])
    db.refresh(gen)
    assert gen.text == "新的正文。"

