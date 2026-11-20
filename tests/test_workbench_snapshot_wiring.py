"""工作台重写也走两段式快照（2026-11-21 接线回归）。

背景：`GenerationSnapshot` 此前只在助手重写链路创建，工作台
`POST /api/regenerate-stream` 不建快照，导致「撤销/回滚」只对助手重写可用。
本文件锁住三件事：

1. 工作台重写在调模型前写 `pending`，并把 `snapshot_id / expected_before`
   传给引擎；
2. 生成失败时快照置 `failed`（不留 pending 卡住同段）；
3. 同段已有在途重写时返回 409，并释放生成租约。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation import inflight  # noqa: E402
from app.models import Generation, GenerationSnapshot, Project  # noqa: E402

OLD_TEXT = "旧正文：林夜推开门。"


@pytest.fixture(autouse=True)
def _clean():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    inflight.reset()
    yield
    inflight.reset()


@pytest.fixture
def seeded() -> tuple[int, int]:
    s = SessionLocal()
    try:
        proj = Project(
            title="工作台快照测试",
            target_length=5000,
            generation_length=3000,
        )
        s.add(proj)
        s.commit()
        s.refresh(proj)
        gen = Generation(
            project_id=proj.id,
            segment_no=1,
            text=OLD_TEXT,
            summary="旧摘要",
            chapter_range="段1",
        )
        s.add(gen)
        s.commit()
        s.refresh(gen)
        return proj.id, gen.id
    finally:
        s.close()


def _stub_engine(monkeypatch, behaviour: str = "ok") -> list[dict]:
    """替换引擎：记录入参，按 behaviour 产出 done 或抛错。"""
    calls: list[dict] = []

    def fake(db, project, segment_no, **kwargs):
        calls.append({"segment_no": segment_no, **kwargs})
        if behaviour == "boom":
            raise RuntimeError("模型失败")
        yield {"type": "done", "id": 1, "summary": "新摘要"}

    monkeypatch.setattr(
        "app.generation.engine.regenerate_segment_stream", fake,
    )
    return calls


def _client() -> TestClient:
    from app.main import app

    return TestClient(app)


def test_workbench_rewrite_creates_pending_snapshot(monkeypatch, seeded):
    pid, gid = seeded
    calls = _stub_engine(monkeypatch)

    with _client() as client:
        resp = client.post(
            "/api/regenerate-stream",
            json={"project_id": pid, "segment_no": 1},
        )
    assert resp.status_code == 200, resp.text
    assert '"type": "done"' in resp.text

    assert len(calls) == 1
    assert calls[0]["snapshot_id"] is not None
    assert calls[0]["expected_before"] == OLD_TEXT

    s = SessionLocal()
    try:
        row = s.scalar(
            select(GenerationSnapshot).where(
                GenerationSnapshot.project_id == pid,
                GenerationSnapshot.segment_no == 1,
            )
        )
        assert row is not None
        # 引擎替身没有第二段写入，所以留 pending —— 恰好证明第一段已发生。
        assert row.status == "pending"
        assert row.before_text == OLD_TEXT
        assert row.operation == "rewrite"
    finally:
        s.close()


def test_workbench_rewrite_failure_marks_snapshot_failed(monkeypatch, seeded):
    pid, _gid = seeded
    _stub_engine(monkeypatch, behaviour="boom")

    with _client() as client:
        resp = client.post(
            "/api/regenerate-stream",
            json={"project_id": pid, "segment_no": 1},
        )
    assert resp.status_code == 200
    assert '"type": "error"' in resp.text

    s = SessionLocal()
    try:
        row = s.scalar(
            select(GenerationSnapshot).where(
                GenerationSnapshot.project_id == pid,
                GenerationSnapshot.segment_no == 1,
            )
        )
        assert row is not None
        assert row.status == "failed"
        assert row.error_reason == "RuntimeError"
    finally:
        s.close()


def test_second_rewrite_is_rejected_and_lease_released(monkeypatch, seeded):
    pid, gid = seeded
    # 先造一条在途 pending：模拟另一个写入者正在重写同段。
    from app.agents.dialog.snapshot import begin_snapshot

    s = SessionLocal()
    try:
        proj = s.get(Project, pid)
        gen = s.get(Generation, gid)
        begin_snapshot(s, proj, gen)
    finally:
        s.close()

    _stub_engine(monkeypatch)
    with _client() as client:
        resp = client.post(
            "/api/regenerate-stream",
            json={"project_id": pid, "segment_no": 1},
        )
    assert resp.status_code == 409
    # 被挡下的请求不能留下生成租约，否则同项目之后的所有生成都会 409。
    assert inflight.is_in_flight(pid) is False
