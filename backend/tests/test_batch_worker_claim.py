"""批抽 worker 的多进程认领 + 溢出留痕 + 失败可见性。

覆盖：
1. 两个 worker 进程（同库、同一批 running 行）各自 recover：只有一个认领到，
   另一个得到 ``claimed_by:<owner>``，内存队列不重复入队；
2. 认领者已死（pid 不存在）/ 租约过期的行会被接管（不会永久卡 running）；
3. 队列满：被挤掉的段在 AgentRun.note 里留痕（error_kind / count / 段号），
   ``worker_status`` summary 能汇总到 overflow；
4. 毒批重试耗尽：note 记 attempts / max_attempts / abandoned / error_kind；
   ``requeue_failed`` 后 attempts 归零、错误留档到 last_error；
5. provider 不可用：单独的错误分类，attempts=0（没试过模型）。

跑法：pytest backend/tests/test_batch_worker_claim.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
import app.models  # noqa: F401,E402
from app.kg import batch_worker as bw  # noqa: E402
from app.models import AgentRun, Generation, Project  # noqa: E402

_HOST = socket.gethostname()
_PID = os.getpid()


def _hash(project_id: int, seg: int) -> str:
    return hashlib.sha256(f"{project_id}:{seg}".encode("utf-8")).hexdigest()


def _drain() -> None:
    """清空内存队列（测试之间不互相污染）。"""
    for pid in bw._queue.pending_projects():
        while bw._queue.pending_count(pid):
            bw._queue.take_batch(pid)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """文件库（两个 Session 模拟两个进程）+ 队列/心跳隔离。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.db.SessionLocal", Session)
    bw._queue._queues.clear()
    s = Session()
    s.add(Project(id=1, title="认领测试"))
    s.commit()
    s.close()
    yield Session
    _drain()
    bw._queue._queues.clear()


def _seed_running(Session, seg: int, *, note: dict | None = None) -> int:
    s = Session()
    s.add(Generation(project_id=1, segment_no=seg, text=f"第 {seg} 段正文。"))
    row = AgentRun(project_id=1, role=bw.ROLE_KG_EXTRACT, status="running",
                   request_hash=_hash(1, seg),
                   note=json.dumps(note if note is not None
                                   else {"segment_no": seg}, ensure_ascii=False))
    s.add(row)
    s.commit()
    rid = row.id
    s.close()
    return rid


def _note(Session, run_id: int) -> dict:
    s = Session()
    try:
        row = s.get(AgentRun, run_id)
        return json.loads(row.note or "{}")
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 1. 两个 worker 同批：只有一个领到
# ---------------------------------------------------------------------------
def test_two_workers_claim_a_batch_only_once(db, monkeypatch):
    ids = [_seed_running(db, seg) for seg in (1, 2)]

    worker_a = f"{_HOST}#{_PID}#aaa"
    monkeypatch.setattr(bw, "WORKER_ID", worker_a)
    res_a = bw.recover_running(db(), project_id=1)
    assert res_a["requeued"] == 2
    assert bw._queue.pending_count(1) == 2

    worker_b = f"{_HOST}#{_PID}#bbb"
    monkeypatch.setattr(bw, "WORKER_ID", worker_b)
    res_b = bw.recover_running(db(), project_id=1)

    # B 看到的是「A 已认领」而不是空状态：同一批没有被 B 再捞一次
    assert res_b["requeued"] == 0
    assert [sk["reason"] for sk in res_b["skipped"]] == [
        f"claimed_by:{worker_a}", f"claimed_by:{worker_a}"]
    assert bw._queue.pending_count(1) == 2  # 队列没被 B 翻倍

    for rid in ids:
        note = _note(db, rid)
        assert note["claim_owner"] == worker_a
        assert note["claim_until"] and note["claim_until"] != "1970-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 2. 死主 / 过期租约会被接管
# ---------------------------------------------------------------------------
def test_dead_owner_and_expired_lease_are_taken_over(db, monkeypatch):
    # 真起一个子进程再等它退出：拿到一个确定已死的 pid（不猜数字）
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    dead = _seed_running(db, 3, note={
        "segment_no": 3,
        "claim_owner": f"{_HOST}#{dead_pid}#dead",
        "claim_at": "2026-11-11T00:00:00+00:00",
        "claim_until": "2999-01-01T00:00:00+00:00",  # 租约未过期，但进程已死
    })
    expired = _seed_running(db, 4, note={
        "segment_no": 4,
        "claim_owner": f"{_HOST}#{_PID}#self",       # 活着的进程
        "claim_at": "2026-11-11T00:00:00+00:00",
        "claim_until": "1970-01-01T00:00:00+00:00",  # 但租约已过期
    })

    me = f"{_HOST}#{_PID}#me"
    monkeypatch.setattr(bw, "WORKER_ID", me)
    res = bw.recover_running(db(), project_id=1)

    assert res["requeued"] == 2, (res["requeued"], res["skipped"],
                                  res["missing"], res["overflow"])
    assert _note(db, dead)["claim_owner"] == me
    assert _note(db, expired)["claim_owner"] == me


# ---------------------------------------------------------------------------
# 3. 队列满：留痕可查
# ---------------------------------------------------------------------------
def test_queue_overflow_is_recorded_in_run_note(db, monkeypatch):
    monkeypatch.setattr(bw, "MAX_QUEUE", 2)
    ids = [bw.enqueue_segment(db(), 1, seg, f"第 {seg} 段正文。")
           for seg in (1, 2, 3)]

    dropped = _note(db, ids[0])
    assert dropped["error"] == "queue_overflow"
    assert dropped["error_kind"] == "queue_overflow"
    assert dropped["abandoned"] is True
    assert dropped["queue_overflow"]["reason"] == "enqueue_overflow"
    assert dropped["queue_overflow"]["count"] == 1
    assert dropped["queue_overflow"]["segment_no"] == 1
    assert dropped["queue_overflow"]["max_queue"] == 2

    # 同一行再被丢一次 → count 累加（「丢了几次」可查）
    s = db()
    row = s.get(AgentRun, ids[0])
    bw._apply_overflow_note(row, reason="enqueue_overflow", project_id=1,
                            segment_no=1, now=datetime.now(timezone.utc))
    s.commit()
    s.close()
    assert _note(db, ids[0])["queue_overflow"]["count"] == 2

    status = bw.worker_status(db(), 1)
    assert status["summary"]["overflow"] >= 1
    assert status["summary"]["abandoned"] >= 1
    assert any(it["error_kind"] == "queue_overflow"
               for it in status["recent_runs"])


# ---------------------------------------------------------------------------
# 4. 毒批耗尽重试：attempts / abandoned 可查；重捞后归零
# ---------------------------------------------------------------------------
class _ProbeOkExtractBoom:
    def chat(self, system, user):
        if "连通性探针" in system:
            return "ok"
        raise RuntimeError("extract boom")


def test_retry_exhausted_is_visible_then_requeue_resets(db, monkeypatch):
    monkeypatch.setattr(bw, "_get_provider", lambda: _ProbeOkExtractBoom())
    monkeypatch.setattr(bw, "_provider_unavailable", lambda p: False)
    s = db()
    s.add(Generation(project_id=1, segment_no=1, text="第 1 段正文。"))
    s.commit()
    s.close()
    rid = bw.enqueue_segment(db(), 1, 1, "第 1 段正文。")

    bw.flush_project(1)

    note = _note(db, rid)
    assert note["error_kind"] == "retry_exhausted"
    assert note["attempts"] == bw.RETRY_LIMIT + 1
    assert note["max_attempts"] == bw.RETRY_LIMIT + 1
    assert note["abandoned"] is True
    assert note["error"]

    st = bw.worker_status(db(), 1)
    assert st["summary"]["failed"] == 1
    assert st["summary"]["abandoned"] == 1

    res = bw.requeue_failed(db(), 1, [rid])
    assert res["requeued"] == 1
    again = _note(db, rid)
    assert again["attempts"] == 0 and again["abandoned"] is False
    assert again["last_error"] == note["error"]
    assert again["claim_owner"] == bw.WORKER_ID


# ---------------------------------------------------------------------------
# 4b. parse_failed（输出被截断）→ 带输出预算约束定向重抽一次
# ---------------------------------------------------------------------------
class _TruncateThenOk:
    def __init__(self):
        self.notes: list[bool] = []

    def chat(self, system, user):
        if "连通性探针" in system:
            return "ok"
        self.notes.append("【输出预算】" in user)
        if "【输出预算】" not in user:
            # 模拟被 4096 token 上限截断的半截 JSON
            return '{"entities": [{"kind": "character", "canonical_name": "某'
        return json.dumps({"entities": [], "relations": [], "events": [],
                           "state_changes": []}, ensure_ascii=False)


def test_parse_failed_retries_with_output_budget(db, monkeypatch):
    prov = _TruncateThenOk()
    monkeypatch.setattr(bw, "_get_provider", lambda: prov)
    monkeypatch.setattr(bw, "_provider_unavailable", lambda p: False)
    s = db()
    s.add(Generation(project_id=1, segment_no=1, text="第 1 段正文。"))
    s.commit()
    s.close()
    rid = bw.enqueue_segment(db(), 1, 1, "第 1 段正文。")

    bw.flush_project(1)

    note = _note(db, rid)
    assert "error_kind" not in note, note
    assert prov.notes == [False, True]  # 第一次裸抽、第二次带输出预算


# ---------------------------------------------------------------------------
# 4c. 版本化之前的老 mentions（generation_id 为空）按章段号一起置非当前
# ---------------------------------------------------------------------------
class _OkEmpty:
    def chat(self, system, user):
        if "连通性探针" in system:
            return "ok"
        return json.dumps({"entities": [], "relations": [], "events": [],
                           "state_changes": []}, ensure_ascii=False)


def test_null_generation_mentions_superseded_on_rebuild(db, monkeypatch):
    from app.models import KgMention

    monkeypatch.setattr(bw, "_get_provider", lambda: _OkEmpty())
    monkeypatch.setattr(bw, "_provider_unavailable", lambda p: False)
    s = db()
    for seg in (14, 15, 16, 17, 18):
        s.add(Generation(project_id=1, segment_no=seg,
                         chapter_range=f"第1章《测试章》（v{seg}）",
                         text=f"第 {seg} 段正文。"))
    s.flush()
    old = KgMention(project_id=1, entity_id=1, generation_id=None,
                    segment_no=16, is_current=True, version=1)
    s.add(old)
    s.commit()
    old_id = old.id
    s.close()

    bw.enqueue_segment(db(), 1, 18, "第 18 段正文。", chapter=1, rebuild=True)
    bw.flush_project(1)

    s = db()
    assert s.get(KgMention, old_id).is_current is False
    s.close()


# ---------------------------------------------------------------------------
# 5. provider 不可用：单独分类，attempts=0
# ---------------------------------------------------------------------------
def test_provider_unavailable_is_visible(db, monkeypatch):
    monkeypatch.setattr(bw, "_get_provider", lambda: None)
    rid = bw.enqueue_segment(db(), 1, 1, "第 1 段正文。")

    bw.flush_project(1)

    note = _note(db, rid)
    assert note["error_kind"] == "provider_unavailable"
    assert note["attempts"] == 0
    assert note["abandoned"] is True
