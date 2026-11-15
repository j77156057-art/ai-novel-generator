"""批抽事务边界：认领必须在调 LLM **之前**提交；心跳失败必须可见。

## 背景（有完整证据链的线上缺陷）

8000 常驻实例启动后心跳（`kg.worker.*`，每 15s 写一次）只写一次就停摆；
外部 `BEGIN IMMEDIATE` 连做 3 次全部 `database is locked`；`worker_alive=true`
但 `last_sweep_seconds_ago` 涨到 90 秒以上。

根因：`_claim_row` 是一条**不提交的 UPDATE**，随后同步调 `provider.chat` +
`extract_batch`（实测 4 分 01 秒），到结果落库才 commit。这 4 分钟里库一直处在
RESERVED 写事务中，心跳（另一连接）、启动快照清理、周期扫描、外部写全被拒。
杀掉进程后 `BEGIN IMMEDIATE` 3/3 成功且 db/journal mtime 不变，重启后立刻又
locked；触发条件是残留 pending 行 —— 所以是「被杀 → 留残留 → 下次启动卡 4
分钟 → 再被杀」的自循环。

本文件钉四件事：
1. 调 `extract_batch` 那一刻，认领已对**其它连接**可见，且**没有写着锁**
   （另起连接 `BEGIN IMMEDIATE` 能成功）；
2. 认领不随「结果写失败」的 rollback 一起丢（行仍归本进程，进而在失败路径落
   failed，或崩在中间时留给接管逻辑）；
3. 认领后崩溃留下的行能被**即时接管**（同机 PID 已死不等 15 分钟租约）；
4. 心跳写失败首次记 warning（不刷屏）、次数可查、成功后 streak 归零。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import AgentRun, Project  # noqa: E402

ROLE = "kg_batch_extract"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """**文件库**（不是内存库）：要验证跨连接的可见性与锁，内存库测不出来。"""
    db_path = tmp_path / "worker_txn.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True,
                           connect_args={"timeout": 1.0})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    import app.db as app_db

    # `batch_worker` 内部是函数级 `from app.db import SessionLocal`，改模块属性即可
    monkeypatch.setattr(app_db, "SessionLocal", Session)
    with Session() as s:
        s.add(Project(id=1, title="测试"))
        s.commit()
    try:
        yield {"Session": Session, "path": str(db_path)}
    finally:
        engine.dispose()


def _make_row(Session, seg: int = 1, note: dict | None = None) -> int:
    h = hashlib.sha256(f"1:{seg}".encode("utf-8")).hexdigest()
    with Session() as s:
        row = AgentRun(project_id=1, role=ROLE, status="pending",
                       request_hash=h,
                       note=json.dumps(note if note is not None else {"segment_no": seg},
                                       ensure_ascii=False))
        s.add(row)
        s.commit()
        return row.id


class _StubProvider:
    """`provider.chat` 在批抽里是「连通性探活」，先于 extract_batch 被调用。"""

    def chat(self, system, user, **kwargs):
        return "ok"


def _run_one_batch(monkeypatch, row_id: int, on_extract) -> list:
    """跑一批（段 1）；`on_extract` 在假 extract_batch 里执行，返回值汇总到列表。"""
    from app.kg import batch_worker as bw

    hits: list = []

    def fake_extract(provider, texts, **kwargs):
        hits.append(on_extract())
        raise RuntimeError("模拟抽取失败：本用例只验证事务边界")

    monkeypatch.setattr("app.kg.extractor_v3.extract_batch", fake_extract)
    monkeypatch.setattr(bw, "_get_provider", lambda: _StubProvider())
    bw._queue.put(bw._QueueItem(project_id=1, segment_no=1, text="他走了。",
                                run_id=row_id))
    bw._run_batches(1)
    return hits


def test_claim_is_committed_and_db_is_unlocked_during_extract(env, monkeypatch):
    """调 extract_batch 时：认领已落库（其它连接可见），且没有写锁。"""
    from app.kg import batch_worker as bw

    row_id = _make_row(env["Session"])

    def probe():
        con = sqlite3.connect(env["path"], timeout=1.0, isolation_level=None)
        try:
            note = json.loads(con.execute(
                "SELECT note FROM agent_runs WHERE id=?", (row_id,)).fetchone()[0])
            owner = note.get("claim_owner")
            # 另一连接能否立刻拿到写锁 —— 「LLM 期间不持写事务」的直接证据
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE agent_runs SET note = note WHERE id = ?", (row_id,))
            con.execute("COMMIT")
            return owner, True
        finally:
            con.close()

    hits = _run_one_batch(monkeypatch, row_id, probe)

    assert hits, "extract_batch 应当被调用"
    owner, immediate_ok = hits[0]
    assert owner == bw.WORKER_ID, f"抽取时认领必须已提交且可见，实际 {owner!r}"
    assert immediate_ok is True, "抽取期间不应持有写事务（BEGIN IMMEDIATE 必须成功）"


def test_claim_survives_result_write_rollback(env, monkeypatch):
    """结果写失败（rollback）不能把认领一起丢掉。"""
    from app.kg import batch_worker as bw

    row_id = _make_row(env["Session"])
    _run_one_batch(monkeypatch, row_id, lambda: "probe")

    with env["Session"]() as s:
        row = s.get(AgentRun, row_id)
        note = json.loads(row.note or "{}")
        assert note.get("claim_owner") == bw.WORKER_ID, note
        # 重试耗尽后按 failed 落库（可 requeue 重捞），不是永远 running
        assert row.status == "failed", row.status
        assert note.get("error_kind") == "retry_exhausted", note.get("error_kind")


def test_crashed_claim_is_taken_over_without_waiting_lease(env):
    """认领后崩溃：同机 PID 已死 ⇒ 立即接管，不白等 15 分钟租约。"""
    from app.kg import batch_worker as bw

    now = datetime.now(timezone.utc)
    dead_owner = f"{socket.gethostname()}#999999999#dead"
    row_id = _make_row(env["Session"], note={
        "segment_no": 1,
        "claim_owner": dead_owner,
        "claim_at": now.isoformat(),
        # 租约还没过期：证明接管靠的是「同机 PID 已消失」而不是等租约
        "claim_until": (now + timedelta(minutes=15)).isoformat(),
    })

    with env["Session"]() as s:
        row = s.get(AgentRun, row_id)
        assert bw._claim_orphaned(row, now, s) is True
        assert bw._claim_row(s, row) is None, "死进程占的 pending 行必须能被接管"
        s.commit()

    with env["Session"]() as s:
        note = json.loads(s.get(AgentRun, row_id).note)
        assert note["claim_owner"] == bw.WORKER_ID, note


def test_running_row_left_by_crash_is_reaped(env):
    """若行原本是 running：启动收割把它标 failed 并标 interrupted（可重捞）。"""
    from app.kg import batch_worker as bw

    old = datetime.now(timezone.utc) - timedelta(days=3)
    with env["Session"]() as s:
        row = AgentRun(project_id=1, role=ROLE, status="running",
                       started_at=old, request_hash="h-crash", note="{}")
        s.add(row)
        s.commit()
        row_id = row.id

    with env["Session"]() as s:
        assert bw.reap_stale_runs(s) == 1
    with env["Session"]() as s:
        row = s.get(AgentRun, row_id)
        assert row.status == "failed"
        assert json.loads(row.note)["interrupted"] is True


class _BoomSession:
    """替身 SessionLocal：任何一次心跳写入都抛（模拟 database is locked）。"""

    def __init__(self, *a, **kw):
        raise RuntimeError("database is locked")


def test_first_heartbeat_failure_warns_once_and_is_queryable(env, monkeypatch, caplog):
    import app.db as app_db
    from app.kg import batch_worker as bw

    base = bw.heartbeat_status()
    monkeypatch.setattr(app_db, "SessionLocal", _BoomSession)

    with caplog.at_level(logging.WARNING):
        bw._heartbeat()
        bw._heartbeat()

    warns = [r for r in caplog.records if "心跳写入失败（首次）" in r.getMessage()]
    assert len(warns) == 1, f"首次失败记一次 warning，不该每 15 秒刷屏：{len(warns)}"

    st = bw.heartbeat_status()
    assert st["failures"] == base["failures"] + 2
    assert st["fail_streak"] == 2
    assert "database is locked" in st["last_error"]
    assert st["last_error_at"] is not None


def test_heartbeat_recovery_resets_streak_and_is_exposed(env, monkeypatch):
    import app.db as app_db
    from app.kg import batch_worker as bw

    monkeypatch.setattr(app_db, "SessionLocal", _BoomSession)
    bw._heartbeat()
    assert bw.heartbeat_status()["fail_streak"] >= 1

    monkeypatch.setattr(app_db, "SessionLocal", env["Session"])
    bw._heartbeat()

    st = bw.heartbeat_status()
    assert st["fail_streak"] == 0
    assert st["last_ok_at"] is not None
    assert st["writes"] >= 1

    with env["Session"]() as s:
        exposed = bw.worker_status(s, 1)["heartbeat"]
    assert exposed["fail_streak"] == 0
    assert exposed["writes"] == st["writes"]
    # 累计失败不重置：问「这个进程一共失败过几次」要能答
    assert exposed["failures"] >= 1
    assert exposed["last_error"]
