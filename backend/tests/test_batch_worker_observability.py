"""批抽 worker 可观测性测试。

覆盖：
- enqueue_segment 落一行 AgentRun(role=kg_batch_extract, status=running)，
  request_hash 稳定，note 带 segment_no/chapter
- _run_batches 批完成翻 success + note 追加 batch_stats；
  LLM 失败重试 ≤RETRY_LIMIT 次后翻 failed（note 带 error）
- worker_status：alive/pending/summary/空批 empty 标记
- requeue_failed：failed 行重置 running 并重新入队；文本丢失报 missing
- assistant 隔离：fetch_run_history 默认只看 role=assistant，
  GET /api/assistant/runs 不含 kg_batch_extract 行

跑法：python tests/test_batch_worker_observability.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# 本文件是可直接运行的脚本（脚本文末会 sys.exit），不是 pytest 用例。
# 不加这道闸，pytest 收集阶段会执行全文件并以 SystemExit 中断整个测试会话。
if __name__ != "__main__":
    import pytest

    pytest.skip(
        "standalone script: python tests/test_batch_worker_observability.py",
        allow_module_level=True,
    )

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
import app.models  # noqa: F401
from app.main import app
from app.kg import batch_worker as bw
from app.models import AgentRun, Generation, Project
from app.agents.log_audit import fetch_run_history

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)


def override_get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

# _run_batches 在 worker 线程里自建 SessionLocal（from app.db import SessionLocal，
# 函数内 import 取的是 app.db 模块属性）——必须 patch app.db.SessionLocal 指向
# 内存库，否则 worker 查的是 novel_dev.db，测试行永远找不到（:memory: 每连接
# 独立的同款坑，StaticPool 只是让**同一个** Session 能看到共享连接）
import app.db as _appdb  # noqa: E402

_appdb.SessionLocal = Session

db = Session()
db.add(Project(id=1, title="worker 观测测试"))
g1 = Generation(project_id=1, segment_no=1, text="林悠然推开门。", summary=None)
g2 = Generation(project_id=1, segment_no=2, text="顾辰逸在等她。", summary=None)
g3 = Generation(project_id=1, segment_no=3, text="两人签了合同。", summary=None)
db.add_all([g1, g2, g3])
db.commit()

# 清掉内存队列残留（其他测试可能入队过）
bw._queue._queues.clear()

# ====================================================================
# 1. enqueue_segment 落一行 pending（认领权归常驻 worker）
# ====================================================================
rid1 = bw.enqueue_segment(db, 1, 1, "林悠然推开门。", chapter=1)
db.commit()
check_true("enqueue 返回 AgentRun id", rid1 is not None)
row = db.get(AgentRun, rid1)
check("role 是 kg_batch_extract", row.role, bw.ROLE_KG_EXTRACT)
check("status 是 pending", row.status, "pending")
import json as _json  # noqa: E402

note = _json.loads(row.note)
check("note 带 segment_no", note.get("segment_no"), 1)
check("note 带 chapter", note.get("chapter"), 1)
import hashlib  # noqa: E402

check("request_hash 稳定", row.request_hash,
      hashlib.sha256(b"1:1").hexdigest())

# ====================================================================
# 2. _run_batches：mock provider 成功 → 翻 success + batch_stats
# ====================================================================
class FakeProvider:
    def chat(self, system, user):
        return ('{"entities": [{"kind": "character", "canonical_name": "林悠然", '
                '"aliases": [], "attrs": {}}], "relations": [], "events": [], '
                '"event_links": [], "entity_knowledge": [], "state_changes": [], '
                '"foreshadows": []}')


bw.enqueue_segment(db, 1, 2, "顾辰逸在等她。", chapter=2)
db.commit()
_orig = bw._get_provider
bw._get_provider = lambda: FakeProvider()
bw._run_batches(1)
bw._get_provider = _orig

row2 = db.scalar(select(AgentRun).where(
    AgentRun.project_id == 1, AgentRun.role == bw.ROLE_KG_EXTRACT,
    AgentRun.request_hash == hashlib.sha256(b"1:2").hexdigest()))
check("批完成翻 success", row2.status, "success")
check_true("finished_at 已写", row2.finished_at is not None)
note2 = _json.loads(row2.note)
check_true("note 有 batch_stats", "batch_stats" in note2)
check("batch_stats entities", note2["batch_stats"]["entities_upserted"], 1)
# 门留痕：note 里带 no_generation 统计与告警槽（本批正文逐字一致 → 0 段）
check("note no_generation 段清单",
      note2["batch_stats"]["text_gate"]["no_generation"]["segments"], [])
check("note no_generation 累计",
      note2["batch_stats"]["text_gate"]["no_generation"]["cumulative_count"], 0)
check_true("note 有 text_gate_alert 槽", "text_gate_alert" in note2)
# 耗时留痕槽：真实 provider 会写 {provider, model, first_byte_ms, total_ms, bytes}；
# FakeProvider 没有 last_call_stats，槽位存在且为 None（不因缺字段而崩）。
check_true("note.llm 有 timing 槽", "timing" in (note2.get("llm") or {}))

# ====================================================================
# 3. LLM 失败 → 重试 ≤ RETRY_LIMIT 次 → failed
# ====================================================================
calls = {"n": 0}


class BoomChatProvider:
    def chat(self, system, user):
        calls["n"] += 1
        raise RuntimeError("LLM 炸了")


class BoomThroughProvider:
    """chat 不炸但永远返回垃圾——extract_batch 的**内部**兜底会把它吞成空骨架。

    这正是生产里「LLM 挂了却记成成功」的真路径：provider 层超时/限流往往
    表现为空响应而非异常。worker 的重试与 failed 判定必须穿透这层兜底。
    """

    def chat(self, system, user):
        return "完全不是 JSON 的输出"


bw.enqueue_segment(db, 1, 3, "两人签了合同。", chapter=3)
db.commit()
bw._get_provider = lambda: BoomThroughProvider()
bw._run_batches(1)
bw._get_provider = _orig

row3 = db.scalar(select(AgentRun).where(
    AgentRun.project_id == 1, AgentRun.role == bw.ROLE_KG_EXTRACT,
    AgentRun.request_hash == hashlib.sha256(b"1:3").hexdigest()))
# 非 JSON 响应 = 抽取失败（不是「抽了但没有」）→ 重试 RETRY_LIMIT+1 次 → failed。
# 以前这里会被 extract_batch 的内部兜底吞成空骨架并记 success + model="(empty)"，
# 把一次失败伪装成成功；现在靠 extractor_v3.ERROR_FIELD 穿透到 worker。
check("非 JSON 响应 → failed", row3.status, "failed")
note3 = _json.loads(row3.note)
check_true("note 带解析失败原因", "parse_failed" in (note3.get("error") or ""))
check("失败行不写 model=(empty)", row3.model != "(empty)", True)

# ====================================================================
# 3b. 抛异常路径（provider.chat 直接炸）→ 重试 RETRY_LIMIT+1 次 → failed
# ====================================================================
calls = {"n": 0}


class BoomProvider:
    def chat(self, system, user):
        calls["n"] += 1
        raise RuntimeError("LLM 炸了")


bw.enqueue_segment(db, 1, 4, "第三段正文。", chapter=4)
db.commit()
g4 = db.scalar(select(Generation).where(
    Generation.project_id == 1, Generation.segment_no == 4))
if g4 is None:
    db.add(Generation(project_id=1, segment_no=4, text="第三段正文。", summary=None))
    db.commit()
bw._get_provider = lambda: BoomChatProvider()
bw._run_batches(1)
bw._get_provider = _orig

check("探针+重试次数 = RETRY_LIMIT+1", calls["n"], bw.RETRY_LIMIT + 1)
row4 = db.scalar(select(AgentRun).where(
    AgentRun.project_id == 1, AgentRun.role == bw.ROLE_KG_EXTRACT,
    AgentRun.request_hash == hashlib.sha256(b"1:4").hexdigest()))
check("异常翻 failed", row4.status, "failed")
note4 = _json.loads(row4.note)
check_true("note 带 error", "LLM 炸了" in (note4.get("error") or ""))

# ====================================================================
# 4. worker_status：汇总 + pending + alive
# ====================================================================
st = bw.worker_status(db, 1)
# 2 个 success：段 1 / 段 2（FakeProvider 同批抽到实体）
# 2 个 failed：段 3（非 JSON 响应）、段 4（provider 直接抛异常）
# 0 个 empty：本次没有「LLM 正常返回但确实无实体」的批次
check("summary success 计数", st["summary"]["success"], 2)
check("summary failed 计数", st["summary"]["failed"], 2)
check("summary empty 计数", st["summary"]["empty"], 0)
check_true("recent_runs 4 行", len(st["recent_runs"]) == 4)
check_true("recent_runs 新的在前", st["recent_runs"][0]["segment_no"] == 4)
check_true("worker_alive 是 bool", isinstance(st["worker_alive"], bool))
check("failed 行 segment_no 可读", st["recent_runs"][0]["status"], "failed")

# ====================================================================
# 5. requeue_failed：failed → running + 重新入队
# ====================================================================
r = bw.requeue_failed(db, 1, [row4.id])
check("重捞计数", r["requeued"], 1)
check("无 missing", r["missing"], [])
db.refresh(row4)
check("行重置 running", row4.status, "running")
check("pending 有 1 段", bw._queue.pending_count(1), 1)

# 重捞不适用于 success 行
r2 = bw.requeue_failed(db, 1, [row2.id])
check("success 行不重捞", r2["requeued"], 0)

# 不存在的 run id
r3 = bw.requeue_failed(db, 1, [99999])
check("不存在 id 静默跳过", (r3["requeued"], r3["missing"]), (0, []))

# ====================================================================
# 6. 端点：GET /worker/status + POST /worker/requeue
# ====================================================================
data = client.get("/api/projects/1/kg-v3/worker/status").json()
# row4 已被上面 requeue 翻 running；此时 failed=1（row3 非 JSON 失败）、running=1（row4）
check("端点 summary failed+running", data["summary"]["failed"] + data["summary"]["running"], 2)
check_true("端点返回 worker_alive 键", "worker_alive" in data)

rr = client.post("/api/projects/1/kg-v3/worker/requeue",
                 json={"run_ids": [row4.id]})
# row4 已是 running（上一步 requeue 过），再 requeue 同一行 → 0
check("requeue 已 running 行", rr.json()["requeued"], 0)
# 队列清空（后面测试别被残留影响）
bw._queue._queues.clear()

# ====================================================================
# 7. assistant 隔离：kg 行不进助手日志
# ====================================================================
hist = fetch_run_history(db, 1, limit=100)
check_true("助手历史不含 kg_batch_extract 行",
           all(h["role"] == "assistant" for h in hist))
# role 参数透传（worker 视角能看到自己的行）
hist_kg = fetch_run_history(db, 1, role=bw.ROLE_KG_EXTRACT, limit=100)
check("worker 视角能看到自己的行", len(hist_kg), 4)

runs_api = client.get("/api/assistant/runs?project_id=1").json()
check_true("助手 runs 端点不含 kg 行",
           all(i["role"] == "assistant" for i in runs_api["runs"]))

# ====================================================================
# 8. stuck 判定：running 且 started_at 超 STUCK_MINUTES
# ====================================================================
from datetime import datetime, timedelta, timezone  # noqa: E402

old = AgentRun(project_id=1, role=bw.ROLE_KG_EXTRACT, status="running",
               started_at=datetime.now(timezone.utc) - timedelta(minutes=bw.STUCK_MINUTES + 5),
               request_hash=hashlib.sha256(b"1:99").hexdigest(),
               note=_json.dumps({"segment_no": 99}))
db.add(old)
db.commit()
st2 = bw.worker_status(db, 1)
check("stuck 计数", st2["summary"]["stuck"], 1)
stuck_item = next(i for i in st2["recent_runs"] if i["segment_no"] == 99)
check_true("stuck 行标记", stuck_item["stuck"] is True)

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
