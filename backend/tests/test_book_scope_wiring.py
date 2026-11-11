# -*- coding: utf-8 -*-
"""把「事实来源书 / 续写书」接进链路的验收测试（2026-11-11）。

锁死三件事：

1. ``_book_for_project`` 的三档优先级：续写书 → 事实来源书 → None；
2. 生成侧读通道确实把这个值传给了 ``current_item_holdings``（不传书的地方不能
   假装接了——那是最隐蔽的「静默无效」）；
3. 项目更新接口能独立设置 / 清空两个字段，并拒绝不存在的书与不在可写范围内
   （``reference_novel_ids``）的书。

跨书同名不串、NULL 泛称组在传书后仍可见：见 ``test_item_group_cross_novel.py``
（``test_read_book5_does_not_return_book6_rows`` /
``test_null_generic_group_visible_under_every_book``）。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base, get_db  # noqa: E402
from app.generation import engine  # noqa: E402
from app.kg import item_holding_read as read  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Project, RawNovel  # noqa: E402


def _proj(active=None, continuation=None) -> SimpleNamespace:
    return SimpleNamespace(id=5, active_novel_id=active,
                           continuation_novel_id=continuation)


# ---------------------------------------------------------------------------
# 1. 取值函数：三档优先级
# ---------------------------------------------------------------------------
def test_book_prefers_continuation():
    assert engine._book_for_project(_proj(active=5, continuation=6)) == 6


def test_book_falls_back_to_active():
    assert engine._book_for_project(_proj(active=5, continuation=None)) == 5


def test_book_is_none_when_both_empty():
    assert engine._book_for_project(_proj()) is None


def test_book_does_not_invent_a_book_when_columns_missing():
    """老库/老对象没有这两列时也不能猜一个 id 出来。"""
    assert engine._book_for_project(SimpleNamespace(id=5)) is None


# ---------------------------------------------------------------------------
# 2. 读通道：值真的传下去了
# ---------------------------------------------------------------------------
def _capture(monkeypatch):
    captured: dict = {}

    def fake_current_item_holdings(items, **kwargs):
        captured["items"] = list(items)
        captured.update(kwargs)
        return []

    monkeypatch.setattr(read, "current_item_holdings", fake_current_item_holdings)
    monkeypatch.setattr(engine, "_active_item_entries",
                        lambda db, project, seg: [{"name": "黑金长刀", "item_id": 48}])
    return captured


def test_item_holding_text_passes_active_book(monkeypatch):
    captured = _capture(monkeypatch)
    engine._item_holding_text(None, _proj(active=5), 1)
    assert captured["novel_id"] == 5
    assert captured["project_id"] == 5


def test_item_holding_text_passes_continuation_book(monkeypatch):
    captured = _capture(monkeypatch)
    engine._item_holding_text(None, _proj(active=5, continuation=6), 1)
    assert captured["novel_id"] == 6


def test_item_holding_text_passes_none_without_book(monkeypatch):
    captured = _capture(monkeypatch)
    engine._item_holding_text(None, _proj(), 1)
    assert captured["novel_id"] is None


# ---------------------------------------------------------------------------
# 3. API：设置 / 清空 / 拒绝
# ---------------------------------------------------------------------------
def _client(tmp_path, refs=(5, 6)):
    engine_ = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine_)
    Session = sessionmaker(bind=engine_, future=True)
    db = Session()
    db.add_all([RawNovel(id=5, title="书五"), RawNovel(id=6, title="书六")])
    proj = Project(title="测试项目", reference_novel_ids=list(refs))
    db.add(proj)
    db.commit()
    pid = proj.id
    db.close()

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), pid


def _patch(client, pid, body):
    return client.patch(f"/api/projects/{pid}", json=body)


def _cleanup():
    app.dependency_overrides.pop(get_db, None)


def test_api_sets_both_fields_independently(tmp_path):
    client, pid = _client(tmp_path)
    try:
        r1 = _patch(client, pid, {"active_novel_id": 5})
        assert r1.status_code == 200, r1.text
        assert r1.json()["active_novel_id"] == 5
        assert r1.json()["continuation_novel_id"] is None

        r2 = _patch(client, pid, {"continuation_novel_id": 6})
        assert r2.status_code == 200, r2.text
        # 只改续写书，不动事实来源书
        assert r2.json()["active_novel_id"] == 5
        assert r2.json()["continuation_novel_id"] == 6
    finally:
        _cleanup()


def test_api_clears_both_fields(tmp_path):
    client, pid = _client(tmp_path)
    try:
        _patch(client, pid, {"active_novel_id": 5, "continuation_novel_id": 6})
        r = _patch(client, pid, {"active_novel_id": None,
                                 "continuation_novel_id": None})
        assert r.status_code == 200, r.text
        assert r.json()["active_novel_id"] is None
        assert r.json()["continuation_novel_id"] is None
    finally:
        _cleanup()


def test_api_rejects_missing_book(tmp_path):
    client, pid = _client(tmp_path)
    try:
        r = _patch(client, pid, {"active_novel_id": 9999})
        assert r.status_code == 400, r.text
        assert "不存在" in r.json()["detail"]
    finally:
        _cleanup()


def test_api_rejects_book_outside_writable_scope(tmp_path):
    client, pid = _client(tmp_path, refs=(5,))
    try:
        r = _patch(client, pid, {"continuation_novel_id": 6})
        assert r.status_code == 400, r.text
        assert "可写范围" in r.json()["detail"]
    finally:
        _cleanup()


def test_api_rejects_any_book_when_no_reference_corpus(tmp_path):
    client, pid = _client(tmp_path, refs=())
    try:
        r = _patch(client, pid, {"active_novel_id": 5})
        assert r.status_code == 400, r.text
        assert "可写范围" in r.json()["detail"]
    finally:
        _cleanup()
