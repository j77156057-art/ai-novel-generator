"""三条静默失败路径的回归测试（修复前必红）。

覆盖：
1. `extractor_v3` 抽取失败 → 返回带 `ERROR_FIELD` 的空骨架（不再是无标记空骨架）
2. `batch_worker` 看到 `ERROR_FIELD` → 该批判 failed，不再写 success + model="(empty)"
   同时「LLM 正常返回、确实没有实体」仍记 success + empty 标记（两者必须能区分）
3. `enqueue_segment` 自己 commit 入队行（新会话可见，不依赖后续 commit）
4. `enqueue_segment` 写行失败 → 抛 `KgEnqueueError`，且不把任务塞进内存队列

跑法：pytest backend/tests/test_kg_silent_failures.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.kg import batch_worker as bw  # noqa: E402
from app.kg.extractor_v3 import ERROR_FIELD, extract_batch  # noqa: E402
from app.models import AgentRun, Project  # noqa: E402


def _make_session_factory(url: str, **kw):
    engine = create_engine(url, future=True, **kw)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


@pytest.fixture()
def mem(monkeypatch):
    """内存库 + 把 worker 内部的 SessionLocal 指过去。"""
    Session = _make_session_factory(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr("app.db.SessionLocal", Session)
    db = Session()
    db.add(Project(id=1, title="静默失败测试"))
    db.commit()
    bw._queue._queues.clear()
    yield Session, db
    bw._queue._queues.clear()
    db.close()


class _ProbeOkExtractBoom:
    """探针回 ok（provider 可达），但抽取调用直接抛异常。"""

    def chat(self, system, user):
        if "连通性探针" in system:
            return "ok"
        raise RuntimeError("extract boom")


# ====================================================================
# 1. extractor_v3：失败必须留标记
# ====================================================================
def test_extract_batch_marks_error_on_llm_exception():
    class Boom:
        def chat(self, system, user):
            raise RuntimeError("boom")

    out = extract_batch(Boom(), [(1, "第一段正文。", 1), (2, "第二段正文。", 2)])
    assert out[ERROR_FIELD].startswith("llm_call_failed")


def test_extract_batch_marks_error_on_non_json():
    class Garbage:
        def chat(self, system, user):
            return "完全不是 JSON 的输出"

    out = extract_batch(Garbage(), [(1, "第一段正文。", 1), (2, "第二段正文。", 2)])
    assert out[ERROR_FIELD].startswith("parse_failed")


def test_extract_batch_genuine_empty_has_no_error():
    """真实的「这一段没有实体」不写失败标记——否则会把它误判成失败。"""

    class Empty:
        def chat(self, system, user):
            return '{"entities": [], "relations": [], "events": []}'

    out = extract_batch(Empty(), [(1, "第一段正文。", 1), (2, "第二段正文。", 2)])
    assert ERROR_FIELD not in out


# ====================================================================
# 2. batch_worker：失败不标成功，真空不标失败
# ====================================================================
def test_worker_marks_failed_when_extraction_raises(monkeypatch, mem):
    Session, db = mem
    monkeypatch.setattr(bw, "_get_provider", lambda: _ProbeOkExtractBoom())
    rid = bw.enqueue_segment(db, 1, 1, "林悠然推开门。", chapter=1)

    bw._run_batches(1)

    row = db.get(AgentRun, rid)
    assert row.status == "failed"
    assert row.model != "(empty)"       # 不再用 model 字段伪装成功
    note = json.loads(row.note)
    assert "extract boom" in note["error"]


def test_worker_marks_failed_when_response_not_json(monkeypatch, mem):
    class Garbage:
        def chat(self, system, user):
            return "不是 JSON"

    Session, db = mem
    monkeypatch.setattr(bw, "_get_provider", lambda: Garbage())
    rid = bw.enqueue_segment(db, 1, 1, "林悠然推开门。", chapter=1)

    bw._run_batches(1)

    row = db.get(AgentRun, rid)
    assert row.status == "failed"
    assert "parse_failed" in json.loads(row.note)["error"]


def test_worker_keeps_success_for_genuine_empty(monkeypatch, mem):
    class Empty:
        def chat(self, system, user):
            return '{"entities": [], "relations": [], "events": []}'

    Session, db = mem
    monkeypatch.setattr(bw, "_get_provider", lambda: Empty())
    rid = bw.enqueue_segment(db, 1, 1, "林悠然推开门。", chapter=1)

    bw._run_batches(1)

    row = db.get(AgentRun, rid)
    assert row.status == "success"          # 不判失败
    assert json.loads(row.note)["empty"] is True   # 但看得出「没抽到东西」
    assert "error" not in json.loads(row.note)


# ====================================================================
# 3. enqueue_segment：入队行自己 commit
# ====================================================================
def test_enqueue_commits_row_itself(tmp_path):
    """用文件库 + 两个会话验证：不调 commit 也能被新会话读到。

    内存库 StaticPool 是同一个连接，uncommitted 数据互相可见，测不出这个缺陷，
    所以这里必须用文件库。
    """
    Session = _make_session_factory(f"sqlite:///{tmp_path / 'enq.db'}")
    s1 = Session()
    s1.add(Project(id=1, title="commit 测试"))
    s1.commit()

    rid = bw.enqueue_segment(s1, 1, 1, "林悠然推开门。")  # 故意不 commit

    s2 = Session()
    row = s2.get(AgentRun, rid)
    assert row is not None, "入队行未持久化：还在依赖调用方后续的 commit"
    assert row.status == "running"

    s2.close()
    s1.close()
    bw._queue._queues.clear()


# ====================================================================
# 4. enqueue_segment：写行失败要抛，不能静默丢任务
# ====================================================================
def test_enqueue_raises_and_does_not_queue_when_write_fails(monkeypatch, mem):
    Session, db = mem

    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(bw, "AgentRun", boom)

    with pytest.raises(bw.KgEnqueueError) as ei:
        bw.enqueue_segment(db, 1, 1, "林悠然推开门。")

    assert "db down" in str(ei.value)
    # 行没写成就不许进队列：否则进程重启后任务既查不到也捞不回
    assert bw._queue.pending_count(1) == 0
