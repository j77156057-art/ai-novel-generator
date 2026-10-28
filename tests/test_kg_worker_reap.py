"""KG 批抽 worker 的「启动收割」与「批内异常兜底」（2026-10-29）。

## 为什么要有

批抽跑在**进程内线程**里，队列在**内存 deque** 里。进程一重启，队列没了，
但库里的 `AgentRun(role=kg_batch_extract, status=running)` 行还在 ⇒ 永远 running。

实测：30 条 running，最早 2026-10-20，最新一条是**当天自己跑的那次**（服务被重启了）。
界面表现：Worker 页 `running 30`，看着像还在抽，实际早就没人管了。

蒸馏任务早就有 `reap_stale_runs`，批抽原先漏了 ⇒ 同样的模式补上。
"""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import AgentRun, Project  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    session.add(Project(id=1, title="测试"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _run(session, status: str, started_at=None, role: str = "kg_batch_extract") -> AgentRun:
    row = AgentRun(
        project_id=1, role=role, status=status, started_at=started_at,
        request_hash="h1", note="{}",
    )
    session.add(row)
    session.commit()
    return row


def test_reap_marks_running_rows_failed(db):
    """进程重启留下的 running 行 ⇒ 标记 failed（可重捞），不再假装在跑。"""
    from datetime import datetime, timedelta, timezone

    from app.kg.batch_worker import reap_stale_runs

    old = datetime.now(timezone.utc) - timedelta(days=3)
    stale = _run(db, "running", started_at=old)
    done = _run(db, "success", started_at=old)
    failed = _run(db, "failed", started_at=old)
    other_role = _run(db, "running", started_at=old, role="assistant")

    n = reap_stale_runs(db)
    assert n == 1, n
    db.refresh(stale)
    db.refresh(done)
    db.refresh(failed)
    db.refresh(other_role)
    assert stale.status == "failed"
    # 已经结束的结论绝不能被改写（覆盖了就再也找不回来）
    assert done.status == "success"
    assert failed.status == "failed"
    # 别的角色（assistant）不在批抽的收割范围里
    assert other_role.status == "running"


def test_reaped_row_has_no_duration(db):
    """被收割的行**耗时不给** —— 它根本没跑完。

    否则前端会用「收割时刻 - 开始时刻」算出「跑了 3 天」（实测 658822s）。
    """
    from datetime import datetime, timedelta, timezone

    from app.kg.batch_worker import reap_stale_runs, worker_status

    old = datetime.now(timezone.utc) - timedelta(days=3)
    _run(db, "running", started_at=old)
    reap_stale_runs(db)

    st = worker_status(db, 1)
    row = st["recent_runs"][0]
    assert row["interrupted"] is True
    assert row["duration_ms"] is None, row["duration_ms"]


def test_reap_keeps_rows_started_after_boot(db):
    """本进程启动后入队的行不能被误收（started_at 晚于收割时刻）。"""
    from datetime import datetime, timedelta, timezone

    from app.kg.batch_worker import reap_stale_runs

    fresh = _run(db, "running", started_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    n = reap_stale_runs(db)
    assert n == 0
    db.refresh(fresh)
    assert fresh.status == "running"


def test_batch_failure_marks_rows_failed(db, monkeypatch):
    """批处理里任何未捕获异常，都要把这批的行翻成 failed。

    之前只有 `extract_batch` 抛错才会翻；`_get_provider()` / `_persist_extraction()`
    等在 for 循环外的异常会冒到 `_worker_loop` ⇒ **整批的行永远留在 running**。
    """
    from app.kg import batch_worker

    row = _run(db, "running")
    row.request_hash = batch_worker.hashlib.sha256(b"1:7").hexdigest()
    db.commit()

    def _boom(*a, **k):
        raise RuntimeError("provider 挂了")

    monkeypatch.setattr(batch_worker, "_get_provider", _boom)
    monkeypatch.setattr(batch_worker, "_queue", _FakeQueue(), raising=False)
    monkeypatch.setattr(
        batch_worker, "_session_factory",
        lambda: sessionmaker(bind=db.get_bind(), future=True),
        raising=False,
    )

    batch_worker._run_batches(1)

    db.refresh(row)
    assert row.status == "failed", row.status


class _FakeQueue:
    """只给一批段，然后就没有了。"""

    def __init__(self):
        from app.kg.batch_worker import _QueueItem

        self._batches = [[_QueueItem(project_id=1, segment_no=7, text="沈砚握刀。", chapter=1)]]

    def take_batch(self, project_id):
        return self._batches.pop() if self._batches else []
