"""v3.0 异步批抽 worker：分段落库后入队，一次抽 5 段合并落 v3.0 表。

与 v2.6 同步合并（engine._merge_segment_into_graph）的关系：
- v2.6 规则基线同步合并保留（零 LLM、即时）
- v3.0 是**异步增强**：段 commit 后只入队（轻量），worker 线程池后台跑 LLM 抽取，
  落 v3.0 的 9 张表。省 50% LLM 调用（5 段合并一次抽）+ 跨段一致性（同一角色
  跨段同一规范名）
- 队列用 AgentRun 表（已有 role/status 机）+ 内存 deque，不新建表（精巧优先）：
  **每段一行** AgentRun(role=kg_batch_extract)——enqueue 时随 generation 事务
  一起落库（入队即持久化，重启后 running 行 = 丢失/卡住的段），批完成统一翻
  success/failed，note 记批级 stats（一次 LLM 抽 5 段，stats 是批粒度，诚实反映）
- worker 按 batch_size=5 攒批，攒满或 flush 时抽一次
- 失败静默兜底：抽取失败不阻塞生成主链（与 v2.6 同纪律）；但**必须留痕**：
  同批立即重试 ≤RETRY_LIMIT 次仍失败 → 行标 failed（毒批不自动重入队，
  走 POST /kg-v3/worker/requeue 手动重捞）
- 落库幂等：entity upsert（canonical_name+kind），event 追加，foreshadow 状态机推进
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.kg import canon_source
from app.kg.book_scope import book_for_project, scope_sql_values
from app.models import (
    AgentRun, AppSetting, Generation, KgEntity, KgRelation, KgEvent, KgEventLink,
    KgCharacterState, KgForeshadow, KgEntityKnowledge, KgIngestQuarantine,
    KgMention, Project,
)

_log = logging.getLogger(__name__)

#: 批大小：一次抽 5 段。省 LLM 调用（5:1）+ 跨段一致性窗口
BATCH_SIZE = 5
#: 队列上限：防内存膨胀（超过则最旧段出队丢弃，生成主链不阻塞）
MAX_QUEUE = 200
#: worker 线程：单线程串行（LLM 限流友好，避免并发打爆免费档）
WORKER_THREADS = 1
#: 同批失败后的立即重试上限。只覆盖网络抖动；持续挂是 key/配额问题，
#: 重试解决不了——暴露成 failed 行给人看（requeue API 手动重捞）才是对的。
#:
#: 最坏总时长 = (RETRY_LIMIT + 1) × 单次调用总预算
#:            = 3 × settings.kg_extract_total_timeout（默认 300s）≈ 15 分钟；
#: 每次尝试内若首字节迟迟不到，会先被 `kg_extract_first_byte_timeout`
#: （默认 120s）判死，而不是像改造前那样每次烧满 Writer 的 960s（合计 ≈48 分钟）。
RETRY_LIMIT = 2
#: parse_failed 的定向重抽约束：模型输出被 4096 token 上限截断时，只压「条数」
#: 让 JSON 收得进一个回复；抽取口径（kind/枚举/证据要求）不变。
_OUTPUT_BUDGET_NOTE = (
    "【输出预算】整段 JSON 必须能一次输出完：实体最多 10 个、事件最多 5 条、"
    "关系最多 5 条、状态变化最多 4 条、其它数组最多 4 项；只保留对剧情最关键"
    "的信息，宁少勿多，不要输出解释。"
)
#: AgentRun 里 kg 批抽行的 role 标识（assistant 端点按 role 过滤，互不污染）
ROLE_KG_EXTRACT = "kg_batch_extract"
#: mention 来源标记（读侧不区分，统计可分开看）：
#: - ``llm``：本批 LLM 抽到的实体在段文本里命中
#: - ``alias``：降级路径用库里**已有**实体（规范名 + aliases）做的别名匹配
MENTION_KIND_LLM = "llm"
MENTION_KIND_ALIAS = "alias"
#: 降级 mention 索引覆盖的实体类型（生成侧只消费 item 与 character）
MENTION_KINDS = ("item", "character")
#: running 行超过这个分钟数视为 stuck（进程重启的残留/真卡死）
STUCK_MINUTES = 30
#: 同项目累计 no_generation 告警阈值（超过才告警，不阻断落库）。
#: 依据：enqueue 发生在 generation commit 之后，正常路径 no_generation 应为 0；
#: 单批最多 5 段，偶发 1 段可归因段号写入竞态，累计到 6（≈ 一整批全缺）就已
#: 不可能是抖动——要么段号写错、要么写到不存在的段，正是要抓的后门。
NO_GENERATION_ALERT_THRESHOLD = 5

# ---------------------------------------------------------------------------
# 多进程认领（`uvicorn --workers>1`）
#
# 为什么需要：内存队列不跨进程共享，但 AgentRun 行是共享的。每个进程启动都会
# 跑 ``recover_running``；旧实现「SELECT 所有 running 行 → 逐行重入队」在同批
# 行上会被两个进程各捞一次，同一批被抽两遍（LLM 双计费 + 重复写）。
#
# 机制：**库内按行原子认领 + 租约**。认领信息写在 ``agent_runs.note`` 的 JSON
# 里（不改表）：
#   - ``claim_owner``：认领者（host#pid#nonce，唯一到进程级）
#   - ``claim_at`` / ``claim_until``：认领时间 / 租约到期（ISO-8601 UTC 字符串）
# 认领语句是带条件的 ``UPDATE``，条件全部在 SQL 里求值；SQLite 的写事务是串行的，
# 同一行只可能有一个 UPDATE 命中（rowcount=1），其余进程得到 rowcount=0。所以
# 「谁先 SELECT 到」不决定归属，「数据库里的那次 UPDATE」才决定——不会双领。
# ---------------------------------------------------------------------------

#: 认领者标识（host#pid#nonce）：同机可用 pid 判活，跨机靠心跳兜底
WORKER_ID = f"{socket.gethostname()}#{os.getpid()}#{secrets.token_hex(3)}"
#: 认领租约：认领后这么久内其他进程不得抢。批只跑一次 LLM（秒级），15 分钟
#: 是给「长尾批 + 时钟误差」的余量；租约到期即可被其他进程接管（防死锁）。
CLAIM_LEASE_MINUTES = 15
#: worker 心跳间隔 / 过期阈值（app_settings 键 ``kg.worker.<WORKER_ID>``）。
#: 同机进程优先用 pid 判活（即时、精确）；心跳只兜「跨机 / pid 被复用」。
HEARTBEAT_INTERVAL_SECONDS = 15.0
HEARTBEAT_STALE_SECONDS = 45.0


@dataclass
class _QueueItem:
    """待抽取的一段。"""

    project_id: int
    segment_no: int
    text: str
    chapter: int | None = None
    #: 对应的 AgentRun 行 id（溢出丢弃时用它留痕；None = 无行的历史调用）
    run_id: int | None = None
    #: 显式按章重建：同章旧版本（含本段自身）一律置非当前，新结果写为新版本。
    #: 普通新章入库为 False（只把**同章其它段**的旧版本置非当前）。
    rebuild: bool = False


class _BatchQueue:
    """内存队列 + 每项目独立攒批。线程安全（锁保护队列与攒批缓冲）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[int, deque[_QueueItem]] = {}
        self._flush_event = threading.Event()
        self._stop = False

    def put(self, item: _QueueItem) -> list[_QueueItem]:
        """入队；超过上限时丢最旧的段并**返回被丢的段**。

        返回而不是直接丢：调用方必须把「哪一段被丢」写进库（AgentRun.note），
        否则队列溢出只剩一行日志，事后查不到哪段没抽、丢了几次。
        """
        with self._lock:
            q = self._queues.setdefault(item.project_id, deque())
            q.append(item)
            # 超上限丢最旧（生成优先，KG 补齐靠后续批）
            dropped: list[_QueueItem] = []
            while len(q) > MAX_QUEUE:
                dropped.append(q.popleft())
            return dropped

    def take_batch(self, project_id: int) -> list[_QueueItem]:
        """取一个项目的一个批（攒够 BATCH_SIZE 或队列不足时全取）。"""
        with self._lock:
            q = self._queues.get(project_id)
            if not q:
                return []
            n = min(BATCH_SIZE, len(q))
            return [q.popleft() for _ in range(n)]

    def pending_projects(self) -> list[int]:
        with self._lock:
            return [pid for pid, q in self._queues.items() if q]

    def pending_count(self, project_id: int) -> int:
        with self._lock:
            q = self._queues.get(project_id)
            return len(q) if q else 0

    def queued_segments(self, project_id: int) -> set[int]:
        """本进程内存队列里已排队的段号（周期扫描排除它们，避免重复入队）。"""
        with self._lock:
            q = self._queues.get(project_id)
            return {it.segment_no for it in q} if q else set()

    def signal_flush(self) -> None:
        self._flush_event.set()

    def wait_flush(self, timeout: float = 5.0) -> None:
        self._flush_event.wait(timeout)
        self._flush_event.clear()

    def stop(self) -> None:
        self._stop = True
        self.signal_flush()


_queue = _BatchQueue()
_worker_started = False
_worker_lock = threading.Lock()


class KgEnqueueError(RuntimeError):
    """入队失败：AgentRun 入队行没能持久化。

    以前这里是「warning 后 return None」：任务已进内存队列，但库里没有任何痕迹，
    进程重启后既查不到也捞不回——静默丢任务。现在改为向上抛，由调用方决定
    是否降级（生成主链不因 KG 失败而中断，但必须把失败报出去）。
    """


def enqueue_segment(db: Session, project_id: int, segment_no: int, text: str,
                    chapter: int | None = None, *, rebuild: bool = False) -> int:
    """段 commit 后调用：入队等待异步批抽。零 LLM、零阻塞。

    顺序固定为「先落库、再入队」：
    1. 写一行 AgentRun(role=kg_batch_extract, status=pending) 并**自己 commit**。
       不再依赖调用方后续（如 `_rollup_summary_tree`）的 commit——那一步之前一旦
       抛异常，入队行就丢了，worker 捞不到、库里也查不到。
    2. 行落地成功后才进内存队列，避免 worker 抢在 commit 之前处理、查不到行。

    为什么是 pending 而不是「running + 入队即认领」：写章进程是短命的（每章一个
    CLI 进程，落库后立刻退出），由它认领等于把行交给一个马上要死的 owner，常驻
    worker 只能等 15 分钟租约过期再接管——每章白等一个租约。改成 pending 后，
    「谁认领」由真正跑抽取的 worker 决定：本进程若是常驻服务，内存队列直接处理；
    若是短命写章进程，常驻 worker 通过 `_discover_pending` 扫到并认领。

    request_hash = sha256("{project_id}:{segment_no}")，稳定可关联（重捞时按它找回）。
    失败抛 `KgEnqueueError`（调用方必须感知），不返回 None。
    返回 AgentRun id。
    """
    now = datetime.now(timezone.utc)
    try:
        # 只记录「谁写进来的」用于追责，不预占：认领权归真正执行抽取的 worker。
        payload = json.dumps({
            "segment_no": segment_no, "chapter": chapter,
            "rebuild": bool(rebuild),
            "enqueued_by": WORKER_ID,
            "enqueued_at": now.isoformat(),
        }, ensure_ascii=False)
        row = AgentRun(
            project_id=project_id, role=ROLE_KG_EXTRACT, status="pending",
            started_at=now,
            request_hash=hashlib.sha256(
                f"{project_id}:{segment_no}".encode("utf-8")).hexdigest(),
            note=payload,
        )
        db.add(row)
        db.commit()  # 入队行自己落库：不依赖后续调用方的 commit
        row_id = row.id
    except Exception as exc:  # noqa: BLE001 —— 转成调用方可感知的错误
        db.rollback()
        raise KgEnqueueError(
            f"kg 批抽入队行写入失败（project {project_id} seg {segment_no}）：{exc}"
        ) from exc

    dropped = _queue.put(_QueueItem(
        project_id=project_id, segment_no=segment_no,
        text=text, chapter=chapter, run_id=row_id, rebuild=bool(rebuild)))
    if dropped:
        # 队列满：被挤掉的段必须在库里留痕（不能只打日志）
        try:
            _mark_overflow(db, dropped, reason="enqueue_overflow")
            db.commit()
        except Exception:  # noqa: BLE001 —— 留痕失败不拖垮入队本身
            db.rollback()
            _log.warning("kg 队列溢出留痕写入失败（项目 %s）", project_id,
                         exc_info=True)
    _signal_flush()
    return row_id


def _signal_flush() -> None:
    """通知 worker 有新段（攒批窗口内尽快处理）。"""
    _queue.signal_flush()


def flush_project(project_id: int) -> None:
    """手动触发一个项目的批抽（测试/收尾用）。

    契约：**返回时该项目的队列已空，且没有在飞的批**——包括批已经被后台
    worker 抢先取走的情况。实现靠 ``_run_gate`` 把「取批 + LLM 抽取 + 落库」
    整段互斥：flush 拿不到闸门时会等 worker 当前这批跑完，再去排干剩余。
    （同一进程里若 worker 已先取走批，flush 会等它跑完——闸门保证不会提前返回。）
    """
    _run_batches(project_id)


#: worker 线程引用（status API 看存活用；ensure_worker 幂等赋值）
_worker_thread: threading.Thread | None = None
#: 周期扫描线程引用（与批抽线程分离，见 ``_sweep_loop``）
_sweep_thread: threading.Thread | None = None


def ensure_worker() -> None:
    """确保后台 worker 与扫描线程在跑（幂等）。顺带启动心跳（多进程判活用）。"""
    global _worker_started, _worker_thread, _sweep_thread
    _ensure_heartbeat()
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="kg-v3-batch-worker")
        t.start()
        _worker_thread = t
        s = threading.Thread(target=_sweep_loop, daemon=True, name="kg-v3-stuck-sweeper")
        s.start()
        _sweep_thread = s
        _worker_started = True


def worker_alive() -> bool:
    """worker 线程是否活着（从未启动 = False）。"""
    t = _worker_thread
    return bool(t is not None and t.is_alive())


def _discover_pending(*, limit: int = 50) -> int:
    """把库里 ``pending`` 的批抽行补进本进程内存队列（跨进程入队用）。

    写章进程是短命的：它只落 pending 行、不跑抽取（见 ``enqueue_segment``）。
    常驻 worker 靠这条路径发现这些行——入队到认领的延迟由此降到一次轮询
    （≤5 秒），不再需要等 15 分钟认领租约。

    只做「发现 + 入队」，不认领：认领仍在 ``_rows_for`` 里按行原子做，多进程
    同时发现同一行时由那次条件 UPDATE 决出唯一赢家。本进程认领中的行
    （``claim_owner == WORKER_ID``）跳过，避免同一段在本进程被抽两遍。

    正文缺失的行标 failed 留痕（与 ``_requeue_row`` 同口径，不静默丢）。
    """
    from app.db import SessionLocal

    added = 0
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        rows = db.scalars(select(AgentRun).where(
            AgentRun.role == ROLE_KG_EXTRACT,
            AgentRun.status == "pending",
        ).order_by(AgentRun.id).limit(limit)).all()
        if not rows:
            return 0
        chapter_maps: dict[int, dict[int, int | None]] = {}
        for row in rows:
            note = _note_dict(row)
            seg_no = note.get("segment_no")
            if seg_no is None:
                row.status = "failed"
                row.finished_at = now
                _merge_note(row, {"error": "pending_segment_missing"})
                continue
            if note.get("claim_owner") == WORKER_ID:
                continue  # 本进程已认领（正在跑），不再重复排队
            if seg_no in _queue.queued_segments(row.project_id):
                continue
            gen = db.scalar(select(Generation).where(
                Generation.project_id == row.project_id,
                Generation.segment_no == seg_no,
            ))
            if gen is None or not (gen.text or "").strip():
                row.status = "failed"
                row.finished_at = now
                _merge_note(row, {"error": "pending_text_missing",
                                  "segment_no": seg_no})
                _log.warning(
                    "kg 待跑行正文缺失：项目 %s run %s 段 %s，已标 failed 留痕",
                    row.project_id, row.id, seg_no)
                continue
            if row.project_id not in chapter_maps:
                chapter_maps[row.project_id] = _segment_chapter_map(db, row.project_id)
            dropped = _queue.put(_QueueItem(
                project_id=row.project_id, segment_no=seg_no,
                text=gen.text,
                chapter=(_chapter_of(gen)
                         or chapter_maps[row.project_id].get(seg_no)),
                run_id=row.id, rebuild=bool(note.get("rebuild"))))
            if dropped:
                _mark_overflow(db, dropped, reason="discover_overflow")
                if any(it.run_id == row.id for it in dropped):
                    continue
            added += 1
        db.commit()
    if added:
        _signal_flush()
    return added


#: 空闲时扫 pending 行的最小间隔（秒）：既是「入队→认领」的延迟上限，
#: 也防止「发现了却认领不到」时变成忙等。
_DISCOVER_MIN_INTERVAL = 3.0
#: 上一次扫 pending 的时刻（monotonic；0 = 本进程还没扫过）
_last_discover_at: float = 0.0
#: 最近一次扫 pending 的结果（status API 暴露）：排查「行 pending 却没人认领」时
#: 先看这里——ticks 不动 = 线程没跑到，error 非空 = 扫库/入队异常。
_discover_stat: dict = {"ticks": 0, "added": 0, "at": None, "error": None}


def _worker_loop() -> None:
    """worker 主循环：轮询各项目队列，攒批抽取落库。

    空闲时扫一次库里的 pending 行（``_discover_pending``）：写章进程只落
    pending、不跑 worker，常驻 worker 必须自己发现它们。

    周期扫描**不在这里**：LLM 批抽是同步阻塞的（实测单批约 4 分钟），扫描
    若与它共用线程，批抽在飞时扫描会被一起挡住——孤儿 running 行因此可能
    超过两个扫描周期无人处理。扫描交给独立的 ``_sweep_loop`` 线程。
    """
    global _last_discover_at
    while not _queue._stop:
        projects = _queue.pending_projects()
        if not projects:
            now = time.monotonic()
            if now - _last_discover_at >= _DISCOVER_MIN_INTERVAL:
                _last_discover_at = now
                _discover_stat["ticks"] += 1
                _discover_stat["at"] = datetime.now(timezone.utc).isoformat()
                try:
                    n = _discover_pending()
                    _discover_stat["added"] = n
                    _discover_stat["error"] = None
                    if n:
                        continue
                except Exception as exc:  # noqa: BLE001 —— 发现失败不影响正常批抽
                    _discover_stat["error"] = f"{type(exc).__name__}: {exc}"[:300]
                    _log.warning("kg 待跑行发现失败（忽略，下轮再试）", exc_info=True)
            _queue.wait_flush(timeout=5.0)
            continue
        for pid in projects:
            try:
                _run_batches(pid)
            except Exception:  # noqa: BLE001 —— 单项目失败不影响其他项目
                _log.warning("kg v3 批抽项目 %d 失败", pid, exc_info=True)


def _sweep_loop() -> None:
    """独立的周期扫描线程：按 ``kg_stuck_sweep_seconds`` 重捞 stuck 行。

    与批抽线程分离，保证「扫描在两分钟内跑一次」不被长批抽拖垮。
    """
    global _last_sweep_at
    while not _queue._stop:
        interval = _sweep_interval()
        if interval <= 0:
            time.sleep(10.0)
            continue
        try:
            _sweep_stuck()
            _last_sweep_at = time.monotonic()
        except Exception:  # noqa: BLE001 —— 扫描失败不影响正常批抽
            _log.warning("kg 批抽周期扫描失败（忽略，下轮再试）", exc_info=True)
        time.sleep(interval)


def _sweep_interval() -> float:
    """周期扫描间隔（秒）：配置 ``kg_stuck_sweep_seconds``，缺省 120。

    配置读不到（老配置/测试环境）时退回缺省值，绝不让扫描因配置缺失而停摆。
    """
    try:
        from app.config import settings

        return float(getattr(settings, "kg_stuck_sweep_seconds", 120) or 0)
    except Exception:  # noqa: BLE001
        return 120.0


def _sweep_stuck() -> dict:
    """扫库把超时 running 行重新入队（不依赖进程重启）。

    只处理「已经超 STUCK_MINUTES」的行，并且跳过本进程内存队列里已排队的段
    （否则同一段会被排两次，白跑一次 LLM）。按项目分组调用 ``recover_running``，
    认领/去重/正文缺失留痕全部复用那条路径，不另写一套。
    """
    from app.db import SessionLocal

    out = {"projects": 0, "requeued": 0, "segments": []}
    with SessionLocal() as db:
        project_ids = db.scalars(
            select(AgentRun.project_id).where(
                AgentRun.role == ROLE_KG_EXTRACT,
                AgentRun.status == "running",
            ).distinct()
        ).all()
        for pid in project_ids:
            try:
                res = recover_running(
                    db, pid, stuck_only=True,
                    skip_segments=_queue.queued_segments(pid),
                )
            except Exception:  # noqa: BLE001 —— 单项目失败不影响其他项目
                _log.warning("kg 周期扫描项目 %s 失败", pid, exc_info=True)
                db.rollback()
                continue
            if res.get("requeued"):
                out["projects"] += 1
                out["requeued"] += res["requeued"]
                out["segments"].extend(res.get("segments") or [])
        if out["requeued"]:
            _log.info("kg 周期扫描：重捞 %d 段（项目 %s）",
                      out["requeued"], [s.get("segment_no") for s in out["segments"]])
    return out


#: 取批 + 抽取 + 落库的互斥闸门（可重入：flush 内部再调 _run_batches 不会自锁）。
#: 没有它，`enqueue → flush_project` 会被 `_enqueue_v3_extraction` 顺手起的
#: worker 抢跑：worker 把批取走、flush 看到空队列秒返回，调用方以为跑完了。
_run_gate = threading.RLock()

#: 上一次周期扫描的时刻（time.monotonic；None = 本进程还没扫过）。
#: 只用于可观测性：`/kg-v3/worker/status` 的 last_sweep_seconds_ago——
#: 「worker_alive=true」不等于扫描在跑，有这个字段才能确认扫描周期在推进。
_last_sweep_at: float | None = None


def _run_batches(project_id: int) -> None:
    """串行排干一个项目的队列（持闸门；worker 与 flush 共用）。"""
    with _run_gate:
        _run_batches_locked(project_id)


def _run_batches_locked(project_id: int) -> None:
    """把一个项目的当前队列抽干（每批 BATCH_SIZE 段）。

    每批的 5 行 AgentRun 在批处理结束时统一翻 success/failed：
    - 成功：finished_at + note 追加批级 stats（ExtractionStats 是一次 LLM 抽
      整批的产物，无法拆到单段——记批级是诚实反映）
    - 失败：同批立即重试 ≤RETRY_LIMIT 次仍挂 → status=failed（毒批不重入队）
    整条链路吞异常：批抽是增强，绝不阻断生成主链。
    """
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.kg.extractor_v3 import ERROR_FIELD, ExtractionStats, extract_batch

    def _rows_for(batch: list[_QueueItem]) -> dict[int, AgentRun]:
        """本批各段对应的 AgentRun 行（request_hash 精确匹配）+ 原子认领。

        pending 行（写章进程只写不认领）在这里由真正跑抽取的 worker 认领；
        别的进程持有的行不重复抽。
        """
        out: dict[int, AgentRun] = {}
        for it in batch:
            h = hashlib.sha256(f"{project_id}:{it.segment_no}".encode("utf-8")).hexdigest()
            row = db.scalar(select(AgentRun).where(
                AgentRun.project_id == project_id,
                AgentRun.role == ROLE_KG_EXTRACT,
                AgentRun.request_hash == h,
                AgentRun.status.in_(("pending", "running")),
            ))
            if row is None:
                continue
            # 只处理本进程认领的行：别的进程（租约内）认领的段即使误入了本进程
            # 内存队列，也不能再抽一遍。
            owner = _note_dict(row).get("claim_owner")
            if owner == WORKER_ID:
                out[it.segment_no] = row
                continue
            if owner:
                now = datetime.now(timezone.utc)
                if _claim_orphaned(row, now, db):
                    # 同机 owner PID 已死：立即接管，不等 15 分钟租约。
                    # （短命 CLI 认领后进程被杀会留下 pending+owner 的行，
                    #  只有这里能把它捞回来——discover 只看 pending，sweep 只看 running）
                    _merge_note(row, {"claim_owner": None, "claim_at": None,
                                      "claim_until": None,
                                      "requeue_reason": "orphan_owner_dead"})
                    db.commit()
                    _log.warning(
                        "kg 批抽接管同机死进程认领的行：项目 %s 段 %s（owner=%s）",
                        project_id, it.segment_no, owner)
                else:
                    _log.warning("kg 批抽跳过他人认领的行：项目 %s 段 %s（owner=%s）",
                                 project_id, it.segment_no, owner)
                    continue
            # 无主行（pending）：必须显式认领后再抽，否则两个进程会各抽一遍。
            err = _claim_row(db, row)
            if err:
                _log.warning("kg 批抽认领失败：项目 %s 段 %s（%s）",
                             project_id, it.segment_no, err)
                continue
            out[it.segment_no] = row
        # 认领必须在调用 LLM **之前**落库并释放写锁。
        # 认领是一条 UPDATE（见 `_claim_row`），此前它和抽取共用一个事务：
        # 抽取实测 4 分 01 秒，这 4 分钟里本连接一直持 SQLite 的 RESERVED 写锁，
        # 心跳（另一连接）、启动快照清理、周期扫描、外部写全部 `database is locked`
        # （实测：心跳只写一次就停摆、worker_alive=true 但 last_sweep 涨到 90s+，
        #  `BEGIN IMMEDIATE` 连做 3 次全失败；进程被杀后 3/3 成功、db/journal
        #  mtime 不变）。先 commit ⇒ 抽取期间不持有任何写事务。
        # 结果落库是另一个短事务（成功 `L732` / 失败 `L761` 那两处 commit）。
        # 「认领后崩溃」不会把行卡死：行仍是 pending + claim_owner（本机 ID 带 pid），
        # 下次 `_rows_for` 用 `_claim_orphaned` 判「同机 PID 已消失」即时接管；
        # 跨机则等 CLAIM_LEASE_MINUTES 租约过期。若行原本是 running，则由启动时的
        # `reap_stale_runs` 标 failed（可 `requeue_failed` 重捞）。
        db.commit()
        return out

    while True:
        batch = _queue.take_batch(project_id)
        if not batch:
            return
        db = SessionLocal()
        canon_meta: dict = {}
        # 本批落书：写侧唯一的书来源（续写书 → 事实来源书）。在这里算一次，
        # 传给落库层；None = 项目没指定书，实体/提及写 NULL（项目级通配）。
        _project = db.get(Project, project_id)
        book = book_for_project(_project) if _project is not None else None
        try:
            # —— 记账：回答「这批是用哪份词表抽的」（源 + 内容 hash + 规模）——
            canon_meta = canon_source.log_effective(
                _log,
                prefix=(f"kg v3 批抽 project={project_id} "
                        f"段={','.join(str(it.segment_no) for it in batch)} "),
            )
            rows = _rows_for(batch)
            if len(rows) != len(batch):
                # 行已被别的进程认领 / 已被处理：本批只抽还能归本进程的段。
                # 否则会出现「照抽照写、但没有一行 AgentRun 记这笔账」。
                _log.warning(
                    "kg 批抽跳过无归属行：项目 %s 段 %s",
                    project_id,
                    [it.segment_no for it in batch if it.segment_no not in rows])
                batch = [it for it in batch if it.segment_no in rows]
                if not batch:
                    continue
            provider = _get_provider()
            if _provider_unavailable(provider):
                # provider 不可用（None / 本地模板兜底）：抽不出实体，但**仍要落
                # mention**——用库里已有实体做别名匹配，不能「一条 mention 都没有」。
                binding: dict = {}
                gate: dict = {}
                added = _index_mentions_only(db, project_id, batch, book=book,
                                             binding_out=binding, gate_out=gate)
                # no_generation 累计/阈值（不阻断；超阈值 → note.text_gate_alert）
                gate_alert = _annotate_no_generation(db, project_id, gate, rows)
                for row in rows.values():
                    row.status = "failed"
                    row.finished_at = datetime.now(timezone.utc)
                    _merge_note(row, {
                        "error": ("provider 不可用（"
                                  f"{type(provider).__name__ if provider else 'None'}）"),
                        # 失败可见性：为什么失败 / 试了几次 / 是否放弃
                        "error_kind": "provider_unavailable",
                        "attempts": 0,
                        "max_attempts": RETRY_LIMIT + 1,
                        "abandoned": True,
                        "alias_only": True, "mentions_added": added,
                        "mention_binding": binding,
                        "text_gate": gate,
                        "text_gate_alert": gate_alert,
                        "canon": canon_meta,
                        "llm": _llm_budget_note(provider),
                    })
                db.commit()
                _log.warning(
                    "kg v3 批抽降级为 alias mention（项目 %s，%s 段）：provider 不可用，"
                    "落 %d 条 mention", project_id, len(batch), added)
                continue
            texts = [(it.segment_no, it.text, it.chapter) for it in batch]
            # 把账上已有的未回收伏笔名给抽取器，要求**同名复用**。
            # 落库本来就按名字去重，但抽取器从前看不到已有的名字 ⇒ 每段给同一件事
            # 重新起说法，账本无界增长（实测 9 段攒出 117 个名字，
            # 切片把 97 条 active 全渲染进提示词 = 2,639 字）。
            known_fores = _active_foreshadow_names(db, project_id)
            last_exc: Exception | None = None
            stats: ExtractionStats | None = None
            # 探针：extract_batch 内部不抛异常（避免 KG 拖垮生成主链），失败时
            # 返回带 ERROR_FIELD 的空骨架。所以这里既有「裸 chat 探活」兜 provider
            # 连不上，也有「ERROR_FIELD 判定」兜「连得上但抽不出来」——后者以前
            # 会被记成 success + model="(empty)"，把失败伪装成成功，现已改为 failed。
            # 注意区分：合法 JSON 但确实没实体（真空）不写 ERROR_FIELD，仍记 success，
            # 只打 empty 标记。
            attempts_made = 0
            for _attempt in range(RETRY_LIMIT + 1):
                attempts_made += 1
                try:
                    provider.chat("连通性探针，请回复 ok", "ok")
                    result = extract_batch(provider, texts, known_foreshadows=known_fores)
                    err = result.get(ERROR_FIELD)
                    if err and str(err).startswith("parse_failed"):
                        # 输出被模型 4096 token 上限截断（长段常见）：加一条输出
                        # 预算约束重抽一次。只压「条数」，不改抽取口径；仍失败
                        # 就按原逻辑记 failed，不无限重试。
                        squeezed = extract_batch(
                            provider, texts, note=_OUTPUT_BUDGET_NOTE,
                            known_foreshadows=known_fores)
                        if not squeezed.get(ERROR_FIELD):
                            result, err = squeezed, None
                    if err:
                        raise RuntimeError(str(err))
                    stats = _persist_extraction(db, project_id, result, batch,
                                                book=book)
                    # 持有投影：v3.0 结果 → generated.db 的组/写法/持有段。
                    # 失败不重试（重试会让 events 重复追加），只把错误记进 stats，
                    # 由下面统一把批标 failed——不能 success 但什么都没写。
                    try:
                        stats.projection = _project_holdings(
                            db, project_id, result, batch, book=book)
                    except Exception as pexc:  # noqa: BLE001 —— 投影失败要可见
                        stats.projection = {"errors": [
                            f"projection_failed: {type(pexc).__name__}: {pexc}"[:300]]}
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 —— 重试吃掉本次异常
                    db.rollback()
                    last_exc = exc
            now = datetime.now(timezone.utc)
            if last_exc is None and stats is not None:
                # no_generation 累计/阈值（不阻断；超阈值 → note.text_gate_alert）
                gate_alert = _annotate_no_generation(
                    db, project_id, stats.text_gate, rows)
                # 真空：LLM 正常返回、解析成功，但整批确实 0 产出。
                # 这不是失败（有些段落就是没有可抽实体），但要看得出「没抽到东西」。
                empty = (stats.entities_upserted == 0 and stats.events_added == 0
                         and stats.relations_upserted == 0
                         and stats.events_skipped == 0)
                note_extra: dict = {"batch_stats": stats.__dict__.copy(),
                                    "text_gate_alert": gate_alert,
                                    "canon": canon_meta,
                                    # 成功路径要显式清掉上一轮失败留下的降级标记，
                                    # 否则 note 里会一直挂着 alias_only=true 误导排查
                                    "alias_only": False,
                                    # 失败可见性：试了几次 / 上限几次 / 是否已放弃
                                    "attempts": attempts_made,
                                    "max_attempts": RETRY_LIMIT + 1,
                                    "abandoned": False,
                                    "llm": _llm_budget_note(provider)}
                if empty:
                    note_extra["empty"] = True
                # 零事件静默通过：实体抽到了、批也 success，但一条事件都没落。
                # 实测 ch21/22/25/31/33 都是这种形态，成功判定完全看不出来。
                # 只做可见性：不改 status、不改重试策略。
                if stats.events_added == 0 and stats.entities_upserted > 0:
                    note_extra["empty_events"] = True
                proj = stats.projection or {}
                proj_errors = [str(e) for e in (proj.get("errors") or [])]
                if proj:
                    note_extra["projection"] = proj
                note_extra["abandoned"] = bool(proj_errors)
                if proj_errors:
                    note_extra["error_kind"] = "projection_failed"
                    # 抽取与 v3.0 落库都成功，但持有投影有错：批标 failed，错误
                    # 落 note.error（同时保留成功投影的部分统计，便于判断写到哪步）。
                    note_extra["error"] = ("projection_failed: "
                                           + "; ".join(proj_errors))[:200]
                    _log.warning("kg v3 持有投影失败（项目 %s，%s 段）：%s",
                                 project_id, len(batch), note_extra["error"])
                status = "failed" if proj_errors else "success"
                for row in rows.values():
                    row.status = status
                    row.finished_at = now
                    # 留痕实际应答模型（此前恒为 None，事后无从确认跑的是哪条链）
                    row.model = _llm_budget_note(provider)["model"]
                    _merge_note(row, note_extra)
                db.commit()
            else:
                err = str(last_exc)[:200] if last_exc else "unknown"
                # 抽取失败也必须留 mention：`_persist_extraction` 的 rollback 会把
                # 本批已写的 mention 一起丢，这里用库里已有实体补一次别名索引。
                binding: dict = {}
                gate: dict = {}
                added = _index_mentions_only(db, project_id, batch, book=book,
                                             binding_out=binding, gate_out=gate)
                # no_generation 累计/阈值（不阻断；超阈值 → note.text_gate_alert）
                gate_alert = _annotate_no_generation(db, project_id, gate, rows)
                for row in rows.values():
                    row.status = "failed"
                    row.finished_at = now
                    row.model = _llm_budget_note(provider)["model"]
                    _merge_note(row, {
                        "error": err,
                        # 失败可见性：原因分类 / 实际尝试次数 / 上限 / 已放弃
                        # （放弃后不会再自动重试，只能 POST /worker/requeue 手动捞）
                        "error_kind": "retry_exhausted",
                        "attempts": attempts_made,
                        "max_attempts": RETRY_LIMIT + 1,
                        "abandoned": True,
                        "alias_only": True,
                        "mentions_added": added,
                        "mention_binding": binding,
                        "text_gate": gate,
                        "text_gate_alert": gate_alert,
                        "canon": canon_meta,
                        "llm": _llm_budget_note(provider),
                    })
                db.commit()
                _log.warning(
                    "kg v3 批抽失败（项目 %s，%s 段）：%s；尝试 %d/%d 次后放弃，"
                    "已补 %d 条 alias mention",
                    project_id, len(batch), err, attempts_made,
                    RETRY_LIMIT + 1, added)
        finally:
            # 每 5 章质量表：随批抽落库自动刷新（不再依赖人记得跑）。
            # 失败只记日志，绝不拖累批抽主链。
            try:
                from app.kg.quality_report import maybe_emit_after_batch
                maybe_emit_after_batch(
                    db, project_id,
                    {it.chapter for it in batch if it.chapter})
            except Exception:  # noqa: BLE001 —— 报表是旁路
                _log.warning("kg 五章质量表刷新失败（忽略）", exc_info=True)
            db.close()


def _merge_note(row: AgentRun, extra: dict) -> None:
    """AgentRun.note 是入队时写的 JSON（segment_no/chapter）；批结束时把
    stats/error 合并进去而不是覆盖——保留段的身份信息。"""
    try:
        base = json.loads(row.note) if row.note else {}
    except (ValueError, TypeError):
        base = {}
    base.update(extra)
    row.note = json.dumps(base, ensure_ascii=False)


def _llm_budget_note(provider) -> dict:  # noqa: ANN001
    """本次抽取的 LLM 留痕：渠道、候选链、预算、实际应答模型。

    只写进 ``agent_runs.note``（JSON），不动表结构。用途：事后能回答
    「这条抽取行跑的是哪条链、什么预算」——此前 note 只有 stats/error，
    排查 `#205` 时无法确认，只能回头翻代码与配置快照。
    """
    if provider is None:
        return {"provider": None, "models": [], "model": None,
                "total_timeout": None, "first_byte_timeout": None}
    chain = getattr(provider, "_model_chain", None)
    return {
        "provider": type(provider).__name__,
        "models": list(chain()) if callable(chain) else [],
        # 最近一次成功应答的模型；全程失败时回落到配置的首选模型
        "model": getattr(provider, "last_model", None) or getattr(provider, "model", None),
        "total_timeout": getattr(provider, "total_timeout", None),
        "first_byte_timeout": getattr(provider, "first_byte_timeout", None),
    }


#: note 里取 JSON 的公共表达式：脏 note（非 JSON）当空对象，绝不让 UPDATE 抛错
_NOTE_JSON = "CASE WHEN json_valid(note) THEN note ELSE '{}' END"
#: 作废旧租约时写的过期时间（固定过去时刻，便于肉眼识别）
_EXPIRED = "1970-01-01T00:00:00+00:00"


def _pid_alive(pid: int) -> bool:
    """本机 pid 是否存活（Windows 用 OpenProcess/WaitForSingleObject，其余 os.kill 0）。

    注意：Windows 上 ``os.kill(pid, 0)`` 会**终止**进程（TerminateProcess 语义），
    绝不能拿来探活，所以这里必须走 kernel32。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        query_limited = 0x1000
        handle = kernel32.OpenProcess(synchronize | query_limited, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _worker_alive(owner: str, db: Session | None = None) -> bool:
    """认领者是否还活着：同机看 pid（即时），跨机看心跳（HEARTBEAT_STALE_SECONDS）。"""
    if not owner:
        return False
    if owner == WORKER_ID:
        return True
    host, _, rest = owner.partition("#")
    pid_s, _, _nonce = rest.partition("#")
    if host == socket.gethostname():
        try:
            pid = int(pid_s)
        except ValueError:
            pid = None
        if pid is not None:
            return _pid_alive(pid)
    try:
        if db is not None:
            row = db.get(AppSetting, f"kg.worker.{owner}")
            value = row.value if row is not None else None
        else:
            from app.db import SessionLocal

            with SessionLocal() as s:
                row = s.get(AppSetting, f"kg.worker.{owner}")
                value = row.value if row is not None else None
    except Exception:  # noqa: BLE001 —— 判活失败按「不活」处理（宁可重捞不可永久丢）
        return False
    if not value:
        return False
    try:
        last = datetime.fromisoformat(value)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() <= HEARTBEAT_STALE_SECONDS


#: 心跳写入的可观测状态。
#:
#: 为什么需要：心跳失败以前 `_log.debug` 一条就完事，等于完全不可见。实测 8000
#: 常驻实例上心跳只写成功一次（随后被批抽那条 4 分钟的写事务挡掉），
#: `worker_alive` 仍是 true、日志里什么都没有 —— 只能靠外部 `BEGIN IMMEDIATE`
#: 失败和 `last_sweep_seconds_ago` 涨到 90s+ 才发现。
#:
#: 记累计失败数 + 当前连续失败次数 + 最后一次错误，由 `worker_status()` 暴露。
_heartbeat_stat: dict = {
    "writes": 0,
    "failures": 0,
    "fail_streak": 0,
    "last_ok_at": None,
    "last_error": None,
    "last_error_at": None,
}
_heartbeat_stat_lock = threading.Lock()


def heartbeat_status() -> dict:
    """心跳写入状态快照（`worker_status()["heartbeat"]` 的数据源）。"""
    with _heartbeat_stat_lock:
        return dict(_heartbeat_stat)


def _heartbeat(*, prune: bool = False) -> None:
    """把自己的心跳写进 app_settings（键 ``kg.worker.<WORKER_ID>``）。

    为什么不新建表：心跳只是「别的进程还在不在」的旁证，借用既有 KV 表零迁移。
    同机进程优先用 pid 判活，心跳只兜跨机与 pid 复用。
    """
    from app.db import SessionLocal

    now_s = datetime.now(timezone.utc).isoformat()
    try:
        with SessionLocal() as s:
            key = f"kg.worker.{WORKER_ID}"
            row = s.get(AppSetting, key)
            if row is None:
                s.add(AppSetting(key=key, value=now_s))
            else:
                row.value = now_s
            if prune:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                for old in s.scalars(select(AppSetting).where(
                        AppSetting.key.like("kg.worker.%"),
                        AppSetting.value < cutoff)).all():
                    s.delete(old)
            s.commit()
    except Exception as exc:  # noqa: BLE001 —— 心跳是增强，失败不影响抽取
        with _heartbeat_stat_lock:
            _heartbeat_stat["failures"] += 1
            _heartbeat_stat["fail_streak"] += 1
            streak = _heartbeat_stat["fail_streak"]
            _heartbeat_stat["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            _heartbeat_stat["last_error_at"] = now_s
        # 首次失败必须可见（warning）；之后不再每 15 秒刷屏，失败次数可查。
        if streak == 1:
            _log.warning(
                "kg worker 心跳写入失败（首次）：%s —— 之后每 %ss 重试，"
                "累计/连续失败次数见 worker_status()[heartbeat]",
                _heartbeat_stat["last_error"], HEARTBEAT_INTERVAL_SECONDS)
        else:
            _log.debug("kg worker 心跳写入仍失败（连续 %d 次）", streak, exc_info=True)
        return
    with _heartbeat_stat_lock:
        prev_streak = _heartbeat_stat["fail_streak"]
        _heartbeat_stat["writes"] += 1
        _heartbeat_stat["fail_streak"] = 0
        _heartbeat_stat["last_ok_at"] = now_s
    if prev_streak:
        _log.info("kg worker 心跳恢复写入（此前连续失败 %d 次）", prev_streak)


_heartbeat_started = False


def _ensure_heartbeat() -> None:
    """启动心跳：先立刻写一次（恢复前别的进程就能看到本进程活着），再每
    HEARTBEAT_INTERVAL_SECONDS 续一次。幂等。"""
    global _heartbeat_started
    with _worker_lock:
        if _heartbeat_started:
            return
        _heartbeat_started = True
    _heartbeat(prune=True)

    def _beat() -> None:
        while not _queue._stop:
            threading.Event().wait(HEARTBEAT_INTERVAL_SECONDS)
            _heartbeat()

    threading.Thread(target=_beat, daemon=True, name="kg-worker-heartbeat").start()


def _claim_execute(db: Session, sql: str, params: dict):
    """执行认领 UPDATE；SQLite 写锁冲突时短重试。

    **不 rollback**：同一事务里可能已经有本进程刚认领成功的其他行，回滚会把
    它们一起丢掉。SQLITE_BUSY 的语句本身没有生效，重试是安全的。3 次仍忙就
    放弃这一行（返回 None），由赢家进程处理。
    """
    for attempt in range(3):
        try:
            return db.execute(text(sql), params)
        except OperationalError:
            if attempt == 2:
                return None
            time.sleep(0.02)
    return None


def _claim_row(db: Session, row: AgentRun, *, now=None,
               force: bool = False) -> str | None:
    """按行原子认领一条批抽行（pending / running）。

    返回 ``None`` = 认领成功（本进程独占该行）；否则返回失败原因（用于留痕）。
    多进程下不会双领的原因：认领是一条带条件的 ``UPDATE``，条件（未认领 /
    租约过期 / 认领者是自己）在 SQL 里求值，SQLite 写事务串行执行——同一行
    最多一个进程命中，``rowcount`` 只会对其中一个等于 1。

    ``force=True``（人工超时重捞）：无视认领者是否存活，先作废旧租约再抢。
    """
    now = now or datetime.now(timezone.utc)
    now_s = now.isoformat()
    note = _note_dict(row)
    owner = note.get("claim_owner")
    if owner and owner != WORKER_ID:
        # 租约过期的行即使认领者还活着也可被接管（长尾批不能永久占住一行）。
        expired = True
        until = note.get("claim_until")
        if until:
            try:
                expired_at = datetime.fromisoformat(until)
                if expired_at.tzinfo is None:
                    expired_at = expired_at.replace(tzinfo=timezone.utc)
                expired = expired_at < now
            except (ValueError, TypeError):
                expired = True
        if not force and not expired and _worker_alive(owner, db):
            return f"claimed_by:{owner}"
        # 认领者已死 / 人工强制接管：先把旧租约作废（同一事务），再走标准 CAS。
        # 只作废租约、不改 owner，所以即使另一个进程同时抢，也由下一步的
        # 条件 UPDATE 决出唯一赢家。
        if _claim_execute(db, (
            f"UPDATE agent_runs SET note = json_set({_NOTE_JSON}, "
            "'$.claim_until', :past) WHERE id = :id AND role = :role "
            f"AND json_extract({_NOTE_JSON}, '$.claim_owner') = :owner"
        ), {"id": row.id, "role": ROLE_KG_EXTRACT, "past": _EXPIRED,
            "owner": owner}) is None:
            # 另一个进程一直占着写锁：当作没抢到（对方会处理该行）
            return "claim_busy"
    until_s = (now + timedelta(minutes=CLAIM_LEASE_MINUTES)).isoformat()
    res = _claim_execute(db, (
        f"UPDATE agent_runs SET note = json_set({_NOTE_JSON}, "
        "'$.claim_owner', :me, '$.claim_at', :now, '$.claim_until', :until) "
        "WHERE id = :id AND role = :role AND status IN ('pending', 'running') "
        f"AND (json_extract({_NOTE_JSON}, '$.claim_owner') IS NULL "
        f"OR json_extract({_NOTE_JSON}, '$.claim_owner') = :me "
        f"OR json_extract({_NOTE_JSON}, '$.claim_until') IS NULL "
        f"OR json_extract({_NOTE_JSON}, '$.claim_until') < :now)"
    ), {"id": row.id, "role": ROLE_KG_EXTRACT, "me": WORKER_ID,
        "now": now_s, "until": until_s})
    if res is None:
        return "claim_busy"
    if res.rowcount != 1:
        return "claim_lost"
    # note 被原生 SQL 改过：让 ORM 丢掉旧值，后续 _merge_note 才能基于新 note
    # 合并（否则会用入队时的旧 JSON 覆盖掉认领字段）。
    db.expire(row)
    return None


def _apply_overflow_note(row: AgentRun, *, reason: str, project_id: int,
                         segment_no: int | None, now) -> None:
    """把「队列满被丢」写进一行 AgentRun 的 note（不 commit）。

    ``count`` 累计同一行被丢的次数（重复入队又被挤掉时会 +1）；``abandoned``
    置真表示不再自动重试，需要 POST /worker/requeue 手动捞。
    """
    prev = _note_dict(row).get("queue_overflow") or {}
    count = int(prev.get("count") or 0) + 1
    row.status = "failed"
    row.finished_at = now
    _merge_note(row, {
        "error": "queue_overflow",
        "error_kind": "queue_overflow",
        "abandoned": True,
        "attempts": 0,
        "max_attempts": RETRY_LIMIT + 1,
        "queue_overflow": {
            "reason": reason,
            "count": count,
            "dropped_at": now.isoformat(),
            "max_queue": MAX_QUEUE,
            "project_id": project_id,
            "segment_no": segment_no,
        },
    })


def _mark_overflow(db: Session, dropped: list[_QueueItem], *, reason: str) -> int:
    """给被队列挤掉的段留痕（**不 commit**，由调用方提交）。

    行标 ``status='failed'`` + ``note.error='queue_overflow'`` +
    ``note.queue_overflow={reason,count,dropped_at,max_queue,segment_no}``。
    这样「丢哪一段、为什么丢、丢了几次」都能从 run 状态/表里查到，而不是只在
    日志里。查询方式：``worker_status`` 的 recent_runs（error_kind）或直接
    ``SELECT id, note FROM agent_runs WHERE json_extract(note,'$.error_kind')
    = 'queue_overflow'``。
    """
    now = datetime.now(timezone.utc)
    marked = 0
    for it in dropped:
        if it.run_id is None:
            _log.warning("kg 队列溢出丢弃无 AgentRun 行的段：项目 %s 段 %s",
                         it.project_id, it.segment_no)
            continue
        row = db.get(AgentRun, it.run_id)
        if row is None:
            continue
        _apply_overflow_note(row, reason=reason, project_id=it.project_id,
                             segment_no=it.segment_no, now=now)
        marked += 1
    if marked:
        _log.warning("kg 批抽队列溢出（%s）：丢弃 %d 段并标 failed 留痕，"
                     "段号=%s", reason, marked,
                     [it.segment_no for it in dropped if it.run_id is not None])
    return marked


def reap_stale_runs(db: Session) -> int:
    """收割被进程重启打断的批抽行（role=kg_batch_extract, status=running）。

    ## 为什么必须有

    批抽跑在**进程内线程**（`WORKER_THREADS = 1`），队列在**内存 deque** 里。
    进程一重启（uvicorn reload / 多 worker / 服务重启 / 容器被杀），
    队列没了，但库里的 running 行还在 ⇒ 永远 running。
    周期扫描（`_sweep_stuck`）只处理超过 `STUCK_MINUTES` 的行，
    刚重启时这批行要再等 30 分钟才被扫到。

    实测（2026-10-29）：30 条 running，最早 2026-10-20，
    最新一条是**当天自己跑的那次**（服务被重启了）。

    ## 为什么是 failed 而不是「继续跑」

    后台线程已经没了，没人能接着跑完它；留成 running 等于撒谎。
    诚实的做法是标记失败 + 说明原因 + 提示「重捞一次即可」。

    ## 只碰 running

    success / failed 是已经结束的结论，覆盖了就再也找不回来。
    """
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(AgentRun).where(
        AgentRun.role == ROLE_KG_EXTRACT,
        AgentRun.status == "running",
    )).all()
    n = 0
    for row in rows:
        started = row.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        # 本进程启动后入队的行 started_at 一定晚于启动时刻 ⇒ 不得冒
        if started is not None and started >= now:
            continue
        row.status = "failed"
        row.finished_at = now
        # interrupted 标记：**这不是"跑了很久才失败"**，是进程重启时队列丢了、它根本没跑完。
        # 不标的话，前端会用 finished_at - started_at 算出「跑了 3 天」（实测 658822s）。
        _merge_note(row, {"error": "服务重启时中断（队列已丢失）", "interrupted": True})
        n += 1
    if n:
        db.commit()
    return n


def worker_status(db: Session, project_id: int, *, recent_limit: int = 50) -> dict:
    """批抽 worker 状态总览（GET /kg-v3/worker/status 的数据源）。

    - worker_alive：线程引用 is_alive（从未启动=False）
    - pending：内存队列积压段数（重启即清零——持久状态看 summary.pending）
    - summary.pending：库里 status=pending 的待认领行（写章进程只落 pending，
      常驻 worker 扫到后认领）
    - stuck：running 且 started_at 超 STUCK_MINUTES（进程重启的残留/真卡死）
    - recent_runs：最近 N 行（新的在前），note 里的 JSON 已解析
    """
    from datetime import datetime, timezone

    runs = db.scalars(select(AgentRun).where(
        AgentRun.project_id == project_id,
        AgentRun.role == ROLE_KG_EXTRACT,
    ).order_by(AgentRun.id.desc()).limit(recent_limit)).all()
    now = datetime.now(timezone.utc)
    summary = {"success": 0, "failed": 0, "running": 0, "pending": 0,
               "stuck": 0, "empty": 0,
               # 失败可见性 / 溢出留痕的汇总（recent_runs 里可看逐行明细）
               "abandoned": 0, "overflow": 0, "claimed": 0}
    items = []
    for r in runs:
        started = r.started_at
        if started is not None and started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        finished = r.finished_at
        if finished is not None and finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        duration_ms = (int((finished - started).total_seconds() * 1000)
                       if started and finished else None)
        stuck = _is_stuck(r, now, db)
        if stuck:
            summary["stuck"] += 1
        elif r.status in summary:
            summary[r.status] += 1
        try:
            note = json.loads(r.note) if r.note else {}
        except (ValueError, TypeError):
            note = {"raw": r.note}
        # 被「启动收割」标 failed 的行**耗时不给** —— 它根本没跑完。
        # 否则前端会用「收割时刻 - 开始时刻」算出「跑了 3 天」（实测 658822s）。
        interrupted = bool(note.get("interrupted"))
        if interrupted:
            duration_ms = None
        claim_owner = note.get("claim_owner")
        claim_expired = (_claim_expired(r, now)
                         if r.status == "running" and claim_owner else False)
        owner_alive = (_worker_alive(claim_owner, db)
                       if claim_owner else None)
        if note.get("empty"):
            summary["empty"] += 1
        if note.get("error_kind") == "queue_overflow":
            summary["overflow"] += 1
        if note.get("abandoned"):
            summary["abandoned"] += 1
        if r.status == "running" and note.get("claim_owner"):
            summary["claimed"] += 1
        items.append({
            "id": r.id,
            "segment_no": note.get("segment_no"),
            "status": r.status,
            "stuck": stuck,
            "empty": bool(note.get("empty")),
            "duration_ms": duration_ms,
            "interrupted": interrupted,
            "batch_stats": note.get("batch_stats"),
            "error": note.get("error"),
            # 失败可见性：原因分类 / 尝试次数 / 上限 / 是否放弃 / 溢出明细
            "error_kind": note.get("error_kind"),
            "attempts": note.get("attempts"),
            "max_attempts": note.get("max_attempts"),
            "abandoned": bool(note.get("abandoned")),
            "queue_overflow": note.get("queue_overflow"),
            # 多进程认领：谁领的、租约到什么时候、认领者是否还活着
            "claim_owner": claim_owner,
            "claim_until": note.get("claim_until"),
            "claim_expired": claim_expired,
            "owner_alive": owner_alive,
            "claimed_by_me": claim_owner == WORKER_ID,
            "started_at": started.isoformat() if started else None,
        })
    return {
        "worker_id": WORKER_ID,
        "worker_alive": worker_alive(),
        "pending": _queue.pending_count(project_id),
        # 周期扫描可观测性：worker_alive=true 只代表线程活着，
        # last_sweep_seconds_ago 才会随每个扫描周期增长/归零，是「扫描在跑」的直接证据
        "sweep_interval_seconds": _sweep_interval(),
        "last_sweep_seconds_ago": (
            round(time.monotonic() - _last_sweep_at, 1)
            if _last_sweep_at is not None else None
        ),
        # 入队→认领链路自检：ticks 是否在涨、last error 是什么
        "discover": dict(_discover_stat),
        # 心跳写入自检：worker_alive=true 也注解不出「心跳写不进去」，
        # 失败次数与最后一次错误看这里（首次失败同时会打 warning）
        "heartbeat": heartbeat_status(),
        "summary": summary,
        "recent_runs": items,
    }


def _aware(dt):
    """把 naive datetime 当 UTC（SQLite 读回来常是 naive）。"""
    from datetime import timezone

    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _claim_expired(row: AgentRun, now) -> bool:
    """认领租约是否已过期（无租约字段按过期处理）。"""
    until = _note_dict(row).get("claim_until")
    if not until:
        return True
    try:
        at = datetime.fromisoformat(until)
    except (ValueError, TypeError):
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at < now


def _owner_same_host(owner: str) -> bool:
    """认领者是否在本机（worker_id 形如 ``host#pid#nonce``）。"""
    return bool(owner) and owner.partition("#")[0] == socket.gethostname()


def _claim_orphaned(row: AgentRun, now, db: Session | None = None) -> bool:
    """认领者已死、行已不归任何活进程：等同于 stuck。

    两个判据：
    - **同机 owner PID 已消失**：立即算孤儿，不等租约。同机 PID 判活是精确的，
      而租约只是「防止抢正在跑的长批」的保守兜底；owner 进程都没了，再等租约
      只是白等（实测每章白等 15 分钟）。租约维持 15 分钟不动——它仍是跨机 /
      拿不到 PID 信息时的唯一判据。
    - **租约过期且认领者不活**（含跨机心跳过期）：兜底判据。
    无认领信息的老行不在这里判（交给年龄判据），避免把旧版本在飞的行抢重。
    """
    owner = _note_dict(row).get("claim_owner")
    if not owner:
        return False
    same_host = _owner_same_host(owner)
    if same_host and not _worker_alive(owner, db):
        return True
    if not _claim_expired(row, now):
        return False
    return not _worker_alive(owner, db)


def _is_stuck(row: AgentRun, now=None, db: Session | None = None) -> bool:
    """running 且满足任一：年龄超 ``STUCK_MINUTES`` / 认领者已不可用。

    后者覆盖「进程被杀但行没跑满 30 分钟」：同机 owner PID 已消失即刻算孤儿，
    跨机则等租约过期 + 心跳过期，不能等年龄判据。
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    if row.status != "running":
        return False
    started = _aware(row.started_at)
    if (started is not None
            and (now - started).total_seconds() > STUCK_MINUTES * 60):
        return True
    return _claim_orphaned(row, now, db)


def _note_dict(row: AgentRun) -> dict:
    try:
        value = json.loads(row.note) if row.note else {}
    except (ValueError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _chapter_of(gen: Generation | None) -> int | None:
    """从 generation.chapter_range 的章节标签派生故事章号（``第12章`` → 12）。

    章号一律按标签派生：同一章重写会产生新的段号（实测「第1章」有 5 个段），
    拿段号当章号会把同一章写成 5 个不同章。解析不出返回 None，调用方按
    ``_segment_chapter_map`` 回退到写作顺序上最近的有标签章（**不拿段号顶章号**）。
    """
    if gen is None:
        return None
    return _chapter_of_label(getattr(gen, "chapter_range", None) or "")


def _chapter_of_label(label: str) -> int | None:
    """``第12章`` → 12；解析不出返回 None。"""
    if not label:
        return None
    try:
        from app.coherence import chapter_index

        return chapter_index(label)
    except Exception:  # noqa: BLE001 —— 标签解析失败只影响章号，不阻断重捞
        return None


def _nearest_labeled_chapter(seg_no: int,
                             labeled: list[tuple[int, int]]) -> int | None:
    """段号没有章节标签时，取写作顺序上最近的**有标签章**（先前再后）。

    旧稿（``段1``/``段2``…）是开篇的碎片：它前面的章不存在时归到后面最早的
    一章（即第 1 章）。只解决「归属哪一章」，不授予当前资格。
    """
    prev: int | None = None
    for seg, ch in labeled:
        if seg < seg_no:
            prev = ch
        elif seg > seg_no:
            return prev if prev is not None else ch
    return prev


def _segment_chapter_map(db: Session, project_id: int) -> dict[int, int | None]:
    """段号 → 章号：标签派生优先，派生不出才按写作顺序回退到最近的有标签章。

    不能拿段号顶章号：段号是篇幅序号，章号是故事章。直接顶用会与真正的同号章
    碰撞（旧段 1 撞上新写的第 1 章），产物被写成别人的旧版本、``is_current=0``，
    等于白抽；回退只决定归属，不改变当前资格（旧稿仍是非当前版本）。
    """
    rows = db.execute(
        select(Generation.segment_no, Generation.chapter_range)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no, Generation.id)
    ).all()
    labeled = [(seg, ch) for seg, label in rows
               if (ch := _chapter_of_label(label or "")) is not None]
    out: dict[int, int | None] = {}
    for seg, label in rows:
        ch = _chapter_of_label(label or "")
        out[seg] = ch if ch is not None else _nearest_labeled_chapter(seg, labeled)
    return out


def _requeue_row(db: Session, row: AgentRun, *, reason: str) -> tuple[str, int | None]:
    """把一行 AgentRun 重新入队（正文从 Generation 回读）。

    返回 ``(outcome, segment_no)``：
    - ``"requeued"``：已重新入队，行重置 running 并重新认领给本进程
    - ``"missing"``：正文不存在/为空，行标 failed 并写 error（**不静默丢**）
    - ``"overflow"``：入队即被队列上限挤掉，行标 failed + queue_overflow（留痕）
    """
    from datetime import datetime, timezone

    note = _note_dict(row)
    seg_no = note.get("segment_no")
    gen = db.scalar(select(Generation).where(
        Generation.project_id == row.project_id,
        Generation.segment_no == seg_no,
    )) if seg_no is not None else None
    if seg_no is None or gen is None or not (gen.text or "").strip():
        row.status = "failed"
        row.finished_at = datetime.now(timezone.utc)
        _merge_note(row, {"error": f"{reason}_text_missing", "segment_no": seg_no})
        _log.warning(
            "kg 批抽重捞失败：项目 %s run %s 段 %s 的正文不存在，已标 failed 留痕",
            row.project_id, row.id, seg_no)
        return "missing", seg_no
    now = datetime.now(timezone.utc)
    dropped = _queue.put(_QueueItem(
        project_id=row.project_id, segment_no=seg_no,
        # 章号以章节标签为准（重捞时按最新正文重算）；标签解析不出才回退到
        # 写作顺序上最近的有标签章（不拿段号顶章号）
        text=gen.text,
        chapter=(_chapter_of(gen)
                 or _segment_chapter_map(db, row.project_id).get(seg_no)),
        run_id=row.id,
        rebuild=bool(note.get("rebuild"))))
    if any(it.run_id == row.id for it in dropped):
        # 重捞的动作本身把队列挤爆了：这一段没入队，必须标 failed 留痕，
        # 而不是重置成 running 假装它在跑。
        _apply_overflow_note(row, reason="requeue_overflow",
                             project_id=row.project_id,
                             segment_no=seg_no, now=now)
        _log.warning("kg 批抽重捞溢出：项目 %s run %s 段 %s 被队列上限挤掉，"
                     "已标 failed 留痕", row.project_id, row.id, seg_no)
        return "overflow", seg_no
    if dropped:
        _mark_overflow(db, dropped, reason="requeue_overflow")
    row.status = "running"
    row.started_at = now
    row.finished_at = None
    _merge_note(row, {
        "requeued_at": now.isoformat(),
        "requeue_reason": reason,
        # 重捞重置重试计数；上一次的错误留档（last_error），别让「曾经失败过」消失。
        # error_kind 同步清空：它是「这次失败的分类」，重捞后还挂着会让
        # success 行看起来像失败（status 才是终态）。
        "error": None,
        "error_kind": None,
        "last_error": note.get("error"),
        "attempts": 0,
        "abandoned": False,
        # 重新认领给本进程（重捞出来的行也必须只有一个进程处理）
        "claim_owner": WORKER_ID,
        "claim_at": now.isoformat(),
        "claim_until": (now + timedelta(minutes=CLAIM_LEASE_MINUTES)).isoformat(),
    })
    return "requeued", seg_no


def requeue_failed(db: Session, project_id: int, run_ids: list[int]) -> dict:
    """重捞失败的段（POST /kg-v3/worker/requeue 的数据源）。

    按 run_id 找本项目的 **failed 行或超时 running 行**（stuck）→ 段文本从
    Generation 表回读（enqueue 时的文本没存，正文才是事实源）→ 重新入队 +
    行重置 running。

    为什么必须收 stuck running：进程重启后内存队列没了，库里只剩 status=running
    的行；以前这些行只能被统计成 stuck，永远捞不回来（永久静默丢失）。

    正文已不存在的段报 missing 并把行标 failed（不静默丢）。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    requeued = 0
    missing: list[int] = []
    overflow: list[dict] = []
    skipped: list[dict] = []
    for rid in run_ids:
        row = db.scalar(select(AgentRun).where(
            AgentRun.id == rid,
            AgentRun.project_id == project_id,
            AgentRun.role == ROLE_KG_EXTRACT,
        ))
        if row is None:
            skipped.append({"run_id": rid, "reason": "not_found"})
            continue
        if row.status != "failed" and not _is_stuck(row, now):
            skipped.append({"run_id": rid, "reason": f"status:{row.status}"})
            continue
        outcome, seg_no = _requeue_row(db, row, reason="manual_requeue")
        if outcome == "requeued":
            requeued += 1
        elif outcome == "overflow":
            overflow.append({"run_id": rid, "segment_no": seg_no})
        else:
            missing.append(rid)
    if requeued or missing or overflow:
        db.commit()
    if requeued:
        _signal_flush()
    return {"requeued": requeued, "missing": missing, "overflow": overflow,
            "skipped": skipped}


def recover_running(db: Session, project_id: int | None = None, *,
                    stuck_only: bool = False,
                    skip_segments: set[int] | None = None) -> dict:
    """把库里停在 running 的批抽行重新入队（启动恢复 / 手动超时重捞）。

    - ``stuck_only=False``（启动时用）：**所有** running 行都重入队。启动那一刻
      本进程的内存队列一定是空的，所以「running 行」等价于「上一进程留下的、
      永远没人处理的段」——不需要等它超时（30 分钟）才恢复。
    - ``stuck_only=True``（手动 API / worker 周期扫描）：只重捞超过
      STUCK_MINUTES 的 running 行。
    - ``skip_segments``（周期扫描用）：这些段已在本进程内存队列里排着，
      跳过它们，避免同一段被排两次、白跑一次 LLM。

    同一段存在多条 running 行时只重入队一次，其余标 failed（duplicate 留痕），
    避免同一段被重复抽取。

    多进程（``uvicorn --workers>1``）：每个进程都会调本函数，但重入队前必须先用
    ``_claim_row`` 做**按行原子认领**（条件 UPDATE 的 rowcount 决出唯一赢家）。
    并发启动时两个进程会 SELECT 到同一批行，但只有其中一个的 UPDATE 命中；
    另一个得到 ``claim_lost`` 并跳过——同一批不会被双领。认领者已死的行
    （同机 pid 不在 / 跨机心跳过期）会被接管，避免崩溃后永久卡住。

    返回 ``{"worker_id", "requeued", "missing", "overflow", "skipped",
    "segments"}`` 并在日志里留痕；正文缺失的行会被标 failed + error，
    而不是静默跳过。
    """
    from datetime import datetime, timezone

    # 先写一次心跳：本进程马上要认领行，别的进程要能看见「我活着」。
    # 必须在 SELECT 之前写（WAL 下读事务会定住快照）。
    _heartbeat()

    stmt = select(AgentRun).where(
        AgentRun.role == ROLE_KG_EXTRACT,
        AgentRun.status == "running",
    )
    if project_id is not None:
        stmt = stmt.where(AgentRun.project_id == project_id)
    rows = db.scalars(stmt.order_by(AgentRun.id)).all()

    now = datetime.now(timezone.utc)
    requeued = 0
    overflow: list[dict] = []
    missing: list[int] = []
    skipped: list[dict] = []
    segments: list[dict] = []
    seen: set[tuple[int, int | None]] = set()
    for row in rows:
        if stuck_only and not _is_stuck(row, now, db):
            skipped.append({"run_id": row.id, "reason": "not_stuck"})
            continue
        note = _note_dict(row)
        if skip_segments and note.get("segment_no") in skip_segments:
            # 本进程内存队列里已经排着这一段：再入队只会白跑一次 LLM。
            skipped.append({"run_id": row.id, "reason": "already_queued"})
            continue
        key = (row.project_id, note.get("segment_no"))
        if key in seen:
            row.status = "failed"
            row.finished_at = now
            _merge_note(row, {"error": "duplicate_running_row"})
            skipped.append({"run_id": row.id, "reason": "duplicate_segment"})
            continue
        seen.add(key)
        # 按行原子认领：多进程下同一行只有一个进程能拿到（rowcount=1）。
        claim_err = _claim_row(db, row, now=now, force=stuck_only)
        if claim_err:
            skipped.append({"run_id": row.id, "reason": claim_err})
            continue
        outcome, seg_no = _requeue_row(
            db, row,
            reason="startup_recover" if not stuck_only else "stuck_recover",
        )
        if outcome == "requeued":
            requeued += 1
            segments.append({"run_id": row.id, "project_id": row.project_id,
                             "segment_no": seg_no})
        elif outcome == "overflow":
            overflow.append({"run_id": row.id, "segment_no": seg_no})
        else:
            missing.append(row.id)
    if requeued or missing or overflow or skipped:
        db.commit()
    if requeued:
        _signal_flush()
    if requeued or missing or overflow:
        _log.info(
            "kg 批抽恢复（%s，worker=%s）：重入队 %d 段，正文缺失 %d 行，"
            "溢出丢弃 %d 行，跳过 %d 行%s",
            "启动" if not stuck_only else "超时重捞", WORKER_ID, requeued,
            len(missing), len(overflow), len(skipped),
            f"：{segments}" if segments else "",
        )
    return {"worker_id": WORKER_ID, "requeued": requeued, "missing": missing,
            "overflow": overflow, "skipped": skipped, "segments": segments}


def backfill_project(db: Session, project_id: int) -> dict:
    """为「压根没有入队行」的已生成段补建 AgentRun 并重新入队。

    修的是 ``requeue_failed`` 够不到的那一类：段正文早就生成过，但当时
    **还没有入队功能**（或入队行随进程/事务一起丢了）——库里一行 AgentRun 都
    没有，于是既查不到也重捞不了，永久静默补不回来。

    行为：
    - 逐段（按 segment_no 去重，同段多行取最早一条）检查 request_hash 是否已有
      AgentRun 行；有则跳过（reason 带现有 status），**不覆盖**已成功/已失败的账。
    - 没有行且正文非空 → ``enqueue_segment`` 建行入队。
    - 没有行但正文为空 → 跳过并 WARN（不伪造空任务）。

    返回完整明细（enqueued / skipped），调用方负责 ``ensure_worker()`` 或
    在脚本里 ``flush_project()``。幂等：重复调用不会为同一段建第二行。
    """
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no, Generation.id)
    ).all()
    by_seg: dict[int, Generation] = {}
    for g in gens:
        by_seg.setdefault(g.segment_no, g)

    enqueued: list[dict] = []
    skipped: list[dict] = []
    resolved_ch = _segment_chapter_map(db, project_id)
    for seg_no, gen in sorted(by_seg.items()):
        h = hashlib.sha256(f"{project_id}:{seg_no}".encode("utf-8")).hexdigest()
        existing = db.scalar(select(AgentRun).where(
            AgentRun.project_id == project_id,
            AgentRun.role == ROLE_KG_EXTRACT,
            AgentRun.request_hash == h,
        ))
        if existing is not None:
            skipped.append({"segment_no": seg_no,
                            "reason": f"already_has_run:{existing.status}",
                            "run_id": existing.id})
            continue
        if not (gen.text or "").strip():
            skipped.append({"segment_no": seg_no, "reason": "empty_text"})
            _log.warning("kg 回填跳过：项目 %s 段 %s 正文为空（不伪造空任务）",
                         project_id, seg_no)
            continue
        try:
            # 章号按章节标签派生；解析不出才回退最近的有标签章（不拿段号顶章号）
            rid = enqueue_segment(db, project_id, seg_no, gen.text,
                                  chapter=(_chapter_of(gen)
                                           or resolved_ch.get(seg_no)))
        except KgEnqueueError as exc:
            skipped.append({"segment_no": seg_no,
                            "reason": f"enqueue_failed:{str(exc)[:120]}"})
            _log.error("kg 回填入队失败：项目 %s 段 %s：%s", project_id, seg_no, exc)
            continue
        enqueued.append({"segment_no": seg_no, "run_id": rid})

    report = {
        "project_id": project_id,
        "total_segments": len(by_seg),
        "enqueued": enqueued,
        "skipped": skipped,
        "counts": {"enqueued": len(enqueued), "skipped": len(skipped)},
    }
    _log.info(
        "kg 回填：项目 %s 共 %d 段，补建入队 %d 段，跳过 %d 段%s",
        project_id, len(by_seg), len(enqueued), len(skipped),
        f"（跳过原因：{skipped}）" if skipped else "",
    )
    return report


def rebuild_chapter(db: Session, project_id: int, chapter: int) -> dict:
    """显式「按章重建」v3 KG（修数据入口，走版本化，不删行）。

    与普通生成的差别只有一点：``rebuild=True`` 入队——批抽落库时把**同章所有
    旧版本**（含本段自身旧稿）的 events/states/relations/mentions 置
    ``is_current=0``，新结果作为当前版本写入。旧行留在库里可回溯，读侧只认
    当前版本（检索/切片/状态渲染/时间线均已按 is_current 过滤）。

    章号一律从 ``generation.chapter_range`` 的标签派生（``_chapter_of``），
    解析不出标签的段不参与——不猜、不按段号冒充章号。同章多稿取**段号最大**
    的一稿（同章重写产生更大的段号）。

    同段若还有未完成的 running 行，先标 failed(error=superseded_by_rebuild)
    留痕：``_rows_for`` 按 request_hash 只认一行，不清理会让新旧行互相抢。

    返回 ``{project_id, chapter, segment_no, run_id, superseded_runs}``；
    找不到该章正文时 ``run_id=None`` 且带 ``reason``。
    """
    now = datetime.now(timezone.utc)
    gens = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no, Generation.id)
    ).all()
    matched = [g for g in gens
               if (g.text or "").strip() and _chapter_of(g) == chapter]
    if not matched:
        _log.warning("kg 按章重建：项目 %s 第 %s 章没有可用的正文（标签派生不到）",
                     project_id, chapter)
        return {"project_id": project_id, "chapter": chapter, "segment_no": None,
                "run_id": None, "superseded_runs": [],
                "reason": "chapter_text_missing"}
    latest = matched[-1]
    seg_no = latest.segment_no

    superseded: list[int] = []
    for row in db.scalars(select(AgentRun).where(
        AgentRun.project_id == project_id,
        AgentRun.role == ROLE_KG_EXTRACT,
        AgentRun.status == "running",
    )).all():
        if _note_dict(row).get("segment_no") != seg_no:
            continue
        row.status = "failed"
        row.finished_at = now
        _merge_note(row, {"error": "superseded_by_rebuild",
                          "abandoned": True, "superseded_at": now.isoformat()})
        superseded.append(row.id)
    if superseded:
        db.commit()

    rid = enqueue_segment(db, project_id, seg_no, latest.text,
                          chapter=chapter, rebuild=True)
    _log.info("kg 按章重建：项目 %s 第 %s 章 → 段 %s（run %s），作废旧 running 行 %s",
              project_id, chapter, seg_no, rid, superseded or "无")
    return {"project_id": project_id, "chapter": chapter, "segment_no": seg_no,
            "run_id": rid, "superseded_runs": superseded}


def _get_provider():
    """KG 批抽专用 provider（预算独立于 Writer）。

    **不要**改回 `get_llm_provider()`：那条单例的总预算来自
    `settings.writer_total_timeout`（960s，为长文重写设定）。抽取是秒级小任务，
    沿用它会让人一次挂起烧掉 16 分钟/次 × 3 次 ≈ 48 分钟（`agent_runs #205`）。
    这里的单例渠道/模型链与 Writer 完全相同，只有两个预算不同：
    总预算 `kg_extract_total_timeout`（默认 300s）、首字节
    `kg_extract_first_byte_timeout`（默认 120s）。
    """
    try:
        from app.llm.provider import get_kg_extract_provider

        return get_kg_extract_provider()
    except Exception:  # noqa: BLE001
        return None


def _active_foreshadow_names(db: Session, project_id: int) -> list[str]:
    """账上**未回收**的伏笔名（给抽取器做同名复用，见 `extractor_v3.known_foreshadow_block`）。

    只取 planted/developing —— 已回收/已丢弃的线索不该再要求模型沿用它的名字。
    取不到就返回空名单：那等于回到「抽取器看不到已有名字」的旧行为，
    提示词与从前逐字节相同（宁可不省，不能因此挡住落库）。
    """
    try:
        rows = db.scalars(
            select(KgForeshadow.name).where(
                KgForeshadow.project_id == project_id,
                KgForeshadow.status.in_(["planted", "developing"]),
            ).order_by(KgForeshadow.planted_chapter, KgForeshadow.id)
        ).all()
        return [str(n) for n in rows if n]
    except Exception:  # noqa: BLE001 —— 名单是优化项，取不到不该挡住抽取
        return []


def _provider_unavailable(provider) -> bool:  # noqa: ANN001
    """provider 是否抽不出东西：None，或本地模板兜底。

    ``get_llm_provider()`` 在没配 key 时**不会**返回 None——它兜底
    ``LocalFallbackLLMProvider``（确定性模板），所以原来 `provider is None`
    是一条永远走不到的死分支。这里把「兜底模板」也算作不可用：它确实能返回
    JSON，但不会真的从正文里抽实体，用它跑 ``extract_batch`` 只会产出噪声。
    """
    if provider is None:
        return True
    try:
        from app.llm.provider import LocalFallbackLLMProvider

        return isinstance(provider, LocalFallbackLLMProvider)
    except Exception:  # noqa: BLE001 —— 导入失败按「可用」处理，不影响主链
        return False


def _item_mention_guards(db: Session, project_id: int) -> frozenset[str]:
    """物品实体的规范名 + aliases，作为 mention 计数的「更长写法」守卫。

    ``canon_source`` 有意不读 ``generated.db``；登记层的东西由这里（调用方）
    取好再注入。作用：mention 索引里「地书」不再被「地书碎片」算成命中。
    """
    rows = db.execute(
        select(KgEntity.canonical_name, KgEntity.aliases).where(
            KgEntity.project_id == project_id, KgEntity.kind == "item")
    ).all()
    names: set[str] = set()
    for canonical, aliases in rows:
        if canonical:
            names.add(canonical.strip())
        for alias in (aliases or []):
            if isinstance(alias, str) and alias.strip():
                names.add(alias.strip())
    return frozenset(n for n in names if n)


def _reclassify_info_carriers(db: Session, project_id: int, entities: list[dict],
                              *, context: str | None = None) -> int:
    """判定侧闸门：信息载体不再以 ``kind='item'`` 落库（注入侧不动）。

    只拦「本项目还没有同名 item 实体」的名字：已有 item 行可能挂着
    ``kg_item_group`` / ``kg_item_holding`` / ``kg_holding_story_segment``，
    改 kind 会直接打断读通道；历史存量由一次性迁移脚本处理，不在这里动。

    Returns:
        实际改写的实体条数。
    """
    from app.kg import info_carrier

    changed = 0
    for e in entities:
        if e.get("kind") != "item":
            continue
        name = (e.get("canonical_name") or "").strip()
        if not name:
            continue
        if info_carrier.gate_kind(name, "item", context=context) == "item":
            continue
        exists = db.scalar(
            select(KgEntity.id).where(
                KgEntity.project_id == project_id,
                KgEntity.kind == "item",
                KgEntity.canonical_name == name,
            ).limit(1)
        )
        if exists is not None:
            continue
        e["kind"] = info_carrier.INFO
        changed += 1
    return changed


def _text_gate(binding_report: dict, batch: list[_QueueItem]) -> dict[int, str]:
    """写侧文本归属门：返回 ``{段号: 拦截原因}``（空 = 本批全放行）。

    **判定复用 ``_seg_to_gen`` 的逐字比对结果**，不另写一套文本判定：
    - ``text_mismatch``：该段号有唯一 generation，但正文与入队文本不逐字相同
      → 直接隔离（这就是「别书语料贴上本项目段号」的事故形态）。
    - ``batch_tainted``：本批已有一段证明是别书正文，同批其余「无法比对」的段
      （no_generation / multi_candidate）一并隔离——同一次 LLM 调用吃的是同一批
      文本，不能只拦其中一段。
    - 整批都没有 text_mismatch（含 no_generation / multi_candidate）→ 全放行，
      与门的引入前行为一致（无法比对不等于不一致）。

    纯函数、不碰库、不抛异常：拦截是正常分支，不是异常。
    """
    seg_rows = {int(s["segment_no"]): s
                for s in (binding_report.get("segments") or [])
                if s.get("segment_no") is not None}
    blocked: dict[int, str] = {}
    for item in batch:
        row = seg_rows.get(item.segment_no)
        if row and row.get("status") == "unbound" \
                and row.get("reason") == "text_mismatch":
            blocked[item.segment_no] = "text_mismatch"
    if blocked:
        for item in batch:
            if item.segment_no in blocked:
                continue
            row = seg_rows.get(item.segment_no)
            if row is None or row.get("status") != "bound":
                blocked[item.segment_no] = "batch_tainted"
    return blocked


def _gate_reason(blocked: dict[int, str]) -> str:
    """批级产物的拦截原因：有确证不一致就记 text_mismatch，否则记批污染。"""
    return ("text_mismatch" if "text_mismatch" in blocked.values()
            else next(iter(blocked.values()), "batch_tainted"))


def _run_id_of(batch: list[_QueueItem]) -> int | None:
    for item in batch:
        if item.run_id is not None:
            return item.run_id
    return None


def _quarantine(db: Session, *, project_id: int, run_id: int | None,
                segment_no: int | None, kind: str, reason: str,
                detail: dict) -> None:
    """把本该落业务表的行转存隔离区（同事务，不单独 commit）。

    只 add 不 flush：写入失败会随整批事务一起回滚（与正常落库同命运），
    绝不在这里抛异常打断生成/抽取。
    """
    try:
        # 先做一次 JSON 往返：把非 JSON 类型降级成字符串，保证 detail 在 flush
        # 时不会因序列化失败而拖垮整批（门本身绝不能让抽取任务失败）。
        safe_detail = json.loads(json.dumps(detail, ensure_ascii=False,
                                            default=str))
        db.add(KgIngestQuarantine(
            project_id=project_id, run_id=run_id, segment_no=segment_no,
            kind=kind, reason=reason, detail=safe_detail,
        ))
    except Exception:  # noqa: BLE001 —— 隔离留痕失败不能拖垮落库主链
        _log.warning("kg 文本归属门隔离行写入失败（项目 %s 段 %s %s）",
                     project_id, segment_no, kind, exc_info=True)


def _gate_no_generation(binding_report: dict | None) -> dict:
    """本批 ``no_generation`` 段清单（放行，但必须留痕：这是段号写错的后门）。"""
    segs = sorted({
        int(s["segment_no"]) for s in ((binding_report or {}).get("segments") or [])
        if s.get("segment_no") is not None and s.get("reason") == "no_generation"
    })
    return {"count": len(segs), "segments": segs}


def _new_gate_stats(blocked: dict[int, str],
                    binding_report: dict | None = None) -> dict:
    return {
        "blocked_segments": {str(k): v for k, v in blocked.items()},
        "quarantined": {
            "entity": 0, "mention": 0, "event": 0, "relation": 0,
            "event_link": 0, "entity_knowledge": 0, "state_change": 0,
            "foreshadow": 0,
        },
        # 门对 no_generation 放行（无法比对 ≠ 不一致），但要能统计到
        "no_generation": _gate_no_generation(binding_report),
    }


def _scan_no_generation_history(db: Session, project_id: int,
                                *, exclude_ids: frozenset[int] = frozenset()
                                ) -> dict:
    """同项目历史批的 no_generation 累计（段号去重、按段计次）。

    一批 5 行的 note 记的是**同一份批级 stats**，直接求和会把一批算 5 次；
    所以只认「行自己的 segment_no 落在该行 note 的 no_generation 段清单里」
    的行——一段一行，一批恰好命中它自己缺 generation 的那几段。
    加统计之前写入的 note 没有 ``text_gate.no_generation``，回退读绑定报告
    （``mention_binding``）的 ``reason=no_generation``，口径一致。
    """
    with db.no_autoflush:  # 只读历史；不因当前批未提交的写入触发 flush
        rows = db.scalars(select(AgentRun).where(
            AgentRun.project_id == project_id,
            AgentRun.role == ROLE_KG_EXTRACT,
            AgentRun.note.isnot(None),
            AgentRun.note.like('%"no_generation"%'),
        )).all()
    count = 0
    segs: set[int] = set()
    for row in rows:
        if row.id in exclude_ids:
            continue
        note = _note_dict(row)
        seg = note.get("segment_no")
        if seg is None:
            continue
        seg = int(seg)
        gate = note.get("text_gate")
        if not isinstance(gate, dict):
            gate = (note.get("batch_stats") or {}).get("text_gate")
        ng = (gate or {}).get("no_generation")
        if isinstance(ng, dict):
            hit = seg in {int(s) for s in (ng.get("segments") or [])
                          if s is not None}
        else:
            binding = (note.get("batch_stats") or {}).get("mention_binding") or {}
            hit = any(s.get("segment_no") == seg
                      and s.get("reason") == "no_generation"
                      for s in (binding.get("segments") or []))
        if hit:
            count += 1
            segs.add(seg)
    return {"count": count, "segments": sorted(segs)}


def _annotate_no_generation(db: Session, project_id: int,
                            gate_stats: dict,
                            rows: dict[int, AgentRun]) -> dict:
    """把本批 no_generation 并入同项目累计，写回 ``gate_stats``。

    返回告警 dict（空 = 未超阈值），由调用方写进 ``note.text_gate_alert``。
    纯留痕：任何异常都只降级为「没有历史值」，绝不让批抽失败。
    """
    current = gate_stats.get("no_generation") or {}
    cur_count = int(current.get("count") or 0)
    cur_segs = sorted(int(s) for s in (current.get("segments") or [])
                      if s is not None)
    try:
        prior = _scan_no_generation_history(
            db, project_id, exclude_ids=frozenset(r.id for r in rows.values()))
    except Exception:  # noqa: BLE001 —— 统计失败不阻断落库
        _log.warning("kg no_generation 历史累计失败（项目 %s）", project_id,
                     exc_info=True)
        prior = {"count": 0, "segments": []}
    segs = sorted(set(prior["segments"]) | set(cur_segs))
    total = int(prior["count"]) + cur_count
    gate_stats["no_generation"] = {
        "count": cur_count,
        "segments": cur_segs,
        "cumulative_count": total,
        "cumulative_segments": segs,
        "threshold": NO_GENERATION_ALERT_THRESHOLD,
    }
    if total <= NO_GENERATION_ALERT_THRESHOLD:
        return {}
    _log.warning("kg 文本归属门告警：项目 %s 累计 %d 段 no_generation"
                 "（阈值 %d，段 %s）", project_id, total,
                 NO_GENERATION_ALERT_THRESHOLD, segs)
    return {
        "level": "error",
        "kind": "no_generation",
        "threshold": NO_GENERATION_ALERT_THRESHOLD,
        "cumulative_count": total,
        "segments": segs,
        "message": (f"项目累计 {total} 段 no_generation（阈值 "
                    f"{NO_GENERATION_ALERT_THRESHOLD}）：段号 {segs}。"
                    "段号写错 / 写到不存在的段也会落进这一类，需人工核对；"
                    "本条不阻断落库。"),
    }


def text_gate_no_generation_summary(db: Session,
                                    project_id: int | None = None) -> dict:
    """按项目汇总 no_generation 累计（查询端点用）。"""
    q = select(AgentRun.project_id).where(
        AgentRun.role == ROLE_KG_EXTRACT, AgentRun.note.isnot(None))
    if project_id is not None:
        q = q.where(AgentRun.project_id == project_id)
    pids = sorted({pid for pid in db.scalars(q).all() if pid is not None})
    projects = []
    for pid in pids:
        hist = _scan_no_generation_history(db, pid)
        projects.append({
            "project_id": pid,
            "cumulative_count": hist["count"],
            "segments": hist["segments"],
            "threshold": NO_GENERATION_ALERT_THRESHOLD,
            "alert": hist["count"] > NO_GENERATION_ALERT_THRESHOLD,
        })
    return {
        "threshold": NO_GENERATION_ALERT_THRESHOLD,
        "alert_projects": [p["project_id"] for p in projects if p["alert"]],
        "projects": projects,
    }


def _chapter_generation_map(db: Session, project_id: int
                            ) -> tuple[dict[int, int], dict[int, set[int]], dict[int, set[int]]]:
    """项目内「章号 → 最新 generation id」「章号 → 全部 generation id」
    「章号 → 全部段号」。

    章号从 ``generation.chapter_range`` 的标签派生（``_chapter_of``）；派生不出的
    旧段按写作顺序回退到最近的有标签章（``_segment_chapter_map``），**不再拿段号
    顶章号**。同一章可能有多行（重写会新增段），**最新一行才是当前版本**，用于
    判定本次入库的产物是否应写成 current。
    """
    newest: dict[int, int] = {}
    all_ids: dict[int, set[int]] = {}
    segs: dict[int, set[int]] = {}
    rows = db.execute(
        select(Generation.id, Generation.segment_no, Generation.chapter_range)
        .where(Generation.project_id == project_id)
    ).all()
    resolved = _segment_chapter_map(db, project_id)
    for gid, seg_no, _label in rows:
        ch = resolved.get(seg_no)
        if ch is None:
            continue
        all_ids.setdefault(ch, set()).add(gid)
        segs.setdefault(ch, set()).add(seg_no)
        newest[ch] = max(newest.get(ch, 0), gid)
    return newest, all_ids, segs


def _max_version(db: Session, model, project_id: int, chapter: int | None) -> int:
    """该项目该章已有的最大版本号（无行 = 0）。"""
    if chapter is None:
        return 0
    return int(db.scalar(
        select(func.max(model.version)).where(
            model.project_id == project_id, model.chapter == chapter)
    ) or 0)


def _state_segment(state: dict, batch: list[_QueueItem]) -> int | None:
    """给 LLM 的 state_changes 找它所属的段（不改抽取口径，只做证据归属）。

    旧实现一律记 ``batch[0].segment_no``：一次抽 5 段时后 4 段的状态全挂到首段。
    这里按「new_value 原文命中 → 角色名命中」在批内定位；唯一命中才认，
    多处命中/零命中退回批首段（保持旧行为，不猜）。
    """
    if not batch:
        return None
    if len(batch) == 1:
        return batch[0].segment_no
    raw = (state.get("new_value") or "").strip().strip("“”\"'「」『』（）() 。，,")
    probes = [p for p in (raw[:24], raw) if p]
    for probe in probes:
        hits = [it.segment_no for it in batch if probe in (it.text or "")]
        if len(hits) == 1:
            return hits[0]
    name = (state.get("character") or "").strip()
    if name:
        hits = [it.segment_no for it in batch if name in (it.text or "")]
        if len(hits) == 1:
            return hits[0]
    return batch[0].segment_no


def _supersede_previous_versions(
    db: Session, project_id: int, batch: list[_QueueItem],
    *, book: int | None, seg_to_gen: dict[int, int | None],
    blocked: dict[int, str] | None = None,
) -> tuple[dict[int, int], dict[int, tuple[bool, int]]]:
    """把同章旧版本置 ``is_current=0``，并给出本章新版本号。

    口径（2026-11-14 裁决）：**不物理删行**，旧事件/提及/关系/状态留在库里，
    只标非当前；切片/检索/状态渲染只认 current。

    - 「当前版本」= 同章 generation id 最大的那一行；显式 ``rebuild`` 的批也按
      当前版本写（重建入口只对最新 generation 调用）。
    - 普通新章入库只把**同章其它段**的旧版本置非当前；显式 rebuild 连本段自身
      的旧产物一起置非当前（原地重写同一段的情况）。
    - 作用域按 prompt 书白名单收窄，避免把参考书语料在同一章号上的行误标非当前。

    返回：
    - ``{章号: 新版本号}``（无 generation 可绑定的段不在此表，调用方按 1 写）
    - ``{段号: (是否当前版本, 版本号)}``（落库逐段用）
    """
    resolved_ch = _segment_chapter_map(db, project_id)
    ch_of_seg = {
        it.segment_no: (it.chapter if it.chapter is not None
                        else resolved_ch.get(it.segment_no))
        for it in batch
    }
    gen_of_seg = {seg: gid for seg, gid in seg_to_gen.items() if gid is not None}
    newest, all_ids, segs_by_ch = _chapter_generation_map(db, project_id)
    rebuild = any(it.rebuild for it in batch)
    # 被正文门拦下的段不参与「置非当前」：本轮产物先进隔离区、不落业务表，
    # 若照样把旧版本标非当前，这一章会变成「一条当前行都没有」——读侧直接空窗。
    blocked = blocked or {}
    # 「本章没有任何 generation」的段按当前写：generation 行不带章号标签时
    # `_chapter_generation_map` 的 newest 表里查不到本章，此时「谁更新」无据可判，
    # 若一律标非当前，导入/手写正文的提及会被 `_refresh_mention_counts` 全量排除
    # （mention_count 恒 0），读侧空窗。本章有带章号的 generation 时口径不变：
    # 只有绑到本章最新 generation 的段才是当前。
    current_chs = {
        ch for seg, ch in ch_of_seg.items()
        if ch is not None and seg not in blocked
        and (rebuild or newest.get(ch) is None
             or gen_of_seg.get(seg) == newest.get(ch))
    }
    version_by_ch: dict[int, int] = {}
    for ch in set(ch_of_seg.values()):
        if ch is None:
            continue
        ev_v = _max_version(db, KgEvent, project_id, ch)
        st_v = _max_version(db, KgCharacterState, project_id, ch)
        version_by_ch[ch] = max(ev_v, st_v) + 1

    # 整个项目一个章号标签都没有（导入/手写正文按「段N」切）：没有任何「谁更新」
    # 可比，一律按当前写。否则 current_chs 为空会把全项目 KG 写成非当前，
    # `_refresh_mention_counts` 只数 current → mention_count 恒 0，切片/检索/
    # 状态渲染永远空窗（项目 1、5 就是这种形态；旧数据是靠旧代码写的才读得到）。
    no_label_at_all = bool(ch_of_seg) and all(
        ch is None for ch in ch_of_seg.values())
    if not current_chs:
        return version_by_ch, {
            seg: (bool(no_label_at_all and seg not in blocked), 1)
            for seg in ch_of_seg
        }

    scope = scope_sql_values((book, None) if book is not None else (None,))
    in_scope_ent = select(KgEntity.id).where(
        KgEntity.project_id == project_id,
        func.coalesce(KgEntity.novel_id, -1).in_(scope),
    )
    # 事件：同章全部 current → 非当前（重抽命中会在下面翻回来）
    db.execute(
        update(KgEvent).where(
            KgEvent.project_id == project_id,
            KgEvent.chapter.in_(current_chs),
            KgEvent.is_current.is_(True),
            func.coalesce(KgEvent.novel_id, -1).in_(scope),
        ).values(is_current=False)
    )
    # 角色状态：同章、角色在白名单内
    db.execute(
        update(KgCharacterState).where(
            KgCharacterState.project_id == project_id,
            KgCharacterState.chapter.in_(current_chs),
            KgCharacterState.is_current.is_(True),
            KgCharacterState.character_id.in_(in_scope_ent),
        ).values(is_current=False)
    )
    # 关系：同章起始、两端都在白名单内（端点收窄避免误伤参考书语料的同章关系）
    db.execute(
        update(KgRelation).where(
            KgRelation.project_id == project_id,
            KgRelation.since_chapter.in_(current_chs),
            KgRelation.is_current.is_(True),
            KgRelation.from_id.in_(in_scope_ent),
            KgRelation.to_id.in_(in_scope_ent),
        ).values(is_current=False)
    )
    # 提及：同章其它 generation 的提及 → 非当前；rebuild 时连本段自己的一起
    other_gens: set[int] = set()
    for ch in current_chs:
        other_gens |= all_ids.get(ch, set())
    if not rebuild:
        other_gens -= set(gen_of_seg.values())
    if other_gens:
        db.execute(
            update(KgMention).where(
                KgMention.project_id == project_id,
                KgMention.generation_id.in_(other_gens),
                KgMention.is_current.is_(True),
            ).values(is_current=False)
        )
    # 提及：版本化之前的老行没有 generation 归属（generation_id 为空），上面那条
    # 永远命中不到它们，旧稿 mentions 会一直留在 current。按「章覆盖的段号」收窄，
    # 一起置非当前——只看段号，不碰其它章。
    # 书维度必须一起收窄：参考书语料的提及也是 generation_id 为空，段号与本书
    # 章节撞车（参考书按 1/4/7… 切段，本书按 1..N 连续），只按段号会把参考书的
    # 当前提及误标非当前（实测 9 个撞车段 169 行）。
    ch_segs: set[int] = set()
    for ch in current_chs:
        ch_segs |= segs_by_ch.get(ch, set())
    if ch_segs:
        db.execute(
            update(KgMention).where(
                KgMention.project_id == project_id,
                KgMention.generation_id.is_(None),
                KgMention.segment_no.in_(ch_segs),
                KgMention.is_current.is_(True),
                func.coalesce(KgMention.novel_id, -1).in_(scope),
            ).values(is_current=False)
        )

    seg_ctx = {
        seg: (ch in current_chs, version_by_ch.get(ch, 1))
        for seg, ch in ch_of_seg.items()
    }
    return version_by_ch, seg_ctx


def _relation_chapter(forms_a: tuple[str, ...], forms_b: tuple[str, ...],
                      seg_items: list[tuple[int, int | None, str]],
                      fallback: int | None) -> int | None:
    """关系章归属：优先「两端实体名同段共现」的唯一章，否则退回 fallback。

    关系没有段号，旧实现一律取批内最小章，批内后几章的关系会被最早章吸附
    （实测 ch19/ch20 并批时 ch20 的 7 条边全记到 ch19）。这里用批内各段正文
    判归属：
    - 恰好一段同时出现两端名字 → 该段所在章（多段同章也算唯一章）
    - 零命中（模型给的关系在原文里没有直呼其名）/ 多段跨章命中（分不清在哪
      一章成立）→ 退回批内最小章，保守，与 corpus_ingest 的 chapters[0] 同口径
    """
    hits: set[int] = set()
    for _seg, ch, txt in seg_items:
        if ch is None or not txt:
            continue
        if any(f in txt for f in forms_a) and any(f in txt for f in forms_b):
            hits.add(ch)
    return next(iter(hits)) if len(hits) == 1 else fallback


def _persist_extraction(db: Session, project_id: int, result: dict, batch: list[_QueueItem],
                        *, mention_guards: frozenset[str] | None = None,
                        book: int | None = None):
    """把归一化后的抽取结果落 v3.0 表。幂等 upsert。

    ``book``：本批所属的书，由 ``_run_batches_locked`` 用 ``book_for_project``
    算好传进来。``None`` = 项目没指定书，实体/提及落 ``NULL``（项目级通配），
    与加书维度之前一致。**本函数不自己查项目**：写侧的书来源只应有一处，
    否则很容易出现「读侧续写优先、写侧来源优先」这种分叉。
    """
    from app.kg.extractor_v3 import ExtractionStats

    stats = ExtractionStats(segments=len(batch))
    # 称呼层归一（cleaned 源生效时才动手）：许郎/许公子/魏公/许二叔 等写法折到
    # 实体规范名，原文词进 aliases；关系两端同步归一，避免同一人被拆成两个节点。
    # legacy 源下 normalize 是恒等函数，行为不变。
    # 派生 item 名单按项目设定传（plot_bible.derived_item_names）；没配就不丢。
    _proj = db.get(Project, project_id)
    canon_source.normalize_extraction(
        result,
        derived_item_names=canon_source.derived_item_names_from_project(_proj),
    )
    # 归一里丢掉的派生 item 实体（地书碎片/地书聊天群等）：计数带进批 stats，
    # 否则「为什么没建这个实体」在库里没有线索。
    if result.get("derived_items_dropped"):
        stats.derived_items_dropped = dict(result["derived_items_dropped"])
        stats.derived_refs_dropped = int(result.get("derived_refs_dropped") or 0)
    # 信息载体闸门：判定侧把「文件/邮件/策划书」这类从 item 里摘出去，
    # 注入侧（engine._item_holding_text）与读通道一行不改。
    _reclassify_info_carriers(
        db, project_id, result.get("entities") or [],
        context="\n".join(item.text or "" for item in batch),
    )
    # 段→generation id 映射（mentions 落库用）。加严：正文不逐字相同 / 同段多行
    # generation 一律不绑（宁 NULL 不挂错），绑定结果记进 stats 供 AgentRun.note 查。
    seg_to_gen, binding_report = _seg_to_gen(db, project_id, batch)
    stats.mention_binding = binding_report
    # 写侧文本归属门：本批正文与目标 generation 不逐字一致的段，本轮所有产物
    # 一律不落业务表（转存隔离区），保证提示词读路径默认看不到它们。
    blocked = _text_gate(binding_report, batch)
    gate_stats = _new_gate_stats(blocked, binding_report)
    stats.text_gate = gate_stats
    fully_blocked = bool(blocked) and all(
        item.segment_no in blocked for item in batch)
    gate_run_id = _run_id_of(batch)

    # —— 章节版本化（2026-11-14 裁决：不删行，只置非当前）——
    # 章号按 generation 的章节标签派生；派生不出的旧段回退到最近的有标签章
    # （不拿段号顶章号）。同章最新段才是当前版本。
    # 这里先落「旧版本 → is_current=0」，后面的 upsert 命中会把同名行翻回来，
    # 从而保证：新稿是新版本、旧稿保留可回溯、提示词里不会新旧混出。
    resolved_ch = _segment_chapter_map(db, project_id)
    ch_of_seg = {
        it.segment_no: (it.chapter if it.chapter is not None
                        else resolved_ch.get(it.segment_no))
        for it in batch
    }
    version_by_ch, seg_ctx = _supersede_previous_versions(
        db, project_id, batch, book=book, seg_to_gen=seg_to_gen, blocked=blocked)

    def _ver_ctx(seg: int | None) -> tuple[bool, int, int | None]:
        ch = ch_of_seg.get(seg) if seg is not None else None
        is_cur, ver = seg_ctx.get(seg, (False, 1)) if seg is not None else (False, 1)
        return is_cur, ver, ch

    def _ent(name: str) -> int | None:
        """本批实体优先；跨批反查只认同书或通配（绝不串到另一本书的实体）。"""
        return name_to_id.get(name) or _lookup_entity_id(
            db, project_id, name, novel_id=book)

    # —— 1. entities upsert（canonical_name + kind + 书 唯一）——
    name_to_id: dict[str, int] = {}
    for e in result.get("entities", []):
        if fully_blocked:
            # 整批都是别书正文：不建实体（否则实体表被污染，后续别名匹配还会
            # 把别书人名挂到正常段上）。只隔离不留业务行。
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=None, kind="entity",
                        reason=_gate_reason(blocked), detail=e)
            gate_stats["quarantined"]["entity"] += 1
            continue
        ent = _upsert_entity(db, project_id, e, novel_id=book)
        if ent:
            name_to_id[e["canonical_name"]] = ent.id
            stats.entities_upserted += 1

    # —— 2. relations upsert（两端名字解析为 id，缺失跳过）——
    # 关系模型里没有段号，只有「从哪章开始」；不写 since_chapter 会导致两个后果：
    # 按章统计关系永远是 0（全被算成 NULL），同章重写时 supersede 也定位不到
    # 这些边（条件是 since_chapter in 当前章）。
    # 章归属优先「两端实体名同段共现」的段（见 _relation_chapter）；零命中或
    # 跨章多命中才退回批内最小章（保守，与 corpus_ingest 的 chapters[0] 同口径）。
    _rel_chs = [ch for ch in ch_of_seg.values() if ch is not None]
    rel_chapter = min(_rel_chs) if _rel_chs else None
    seg_items = [(it.segment_no, ch_of_seg.get(it.segment_no), it.text or "")
                 for it in batch]
    batch_chapters = set(_rel_chs)
    _forms_cache: dict[int, tuple[str, ...]] = {}

    def _rel_forms(eid: int, name: str) -> tuple[str, ...]:
        """关系的名字形态（本批名 + 规范名 + 别名），用于段内共现判定。"""
        forms = _forms_cache.get(eid)
        if forms is None:
            ent = db.get(KgEntity, eid)
            cand = [name, getattr(ent, "canonical_name", None)]
            cand += list(getattr(ent, "aliases", None) or [])
            forms = tuple(dict.fromkeys(
                c for c in cand if isinstance(c, str) and len(c) >= 2))
            _forms_cache[eid] = forms
        return forms

    # 关系演化行延后到事件落库后再写：evolution 要尽量挂到「本批哪件事」上，
    # 而事件在关系之后才建（见步骤 3），这里只登记待写项。
    rel_evo_pending: list[tuple[KgRelation, float | None, float, str]] = []
    # 集合端点门（与 v2.6 同一条判定逻辑）：成员/归属类边挂在集合/上位节点、
    # 且窗口里没有任何具体人名 → 落 status='candidate'，不删边。
    # 窗口取本批文本里同时提到两端的那一句；人名表用词表人物 + 本批抽到的人物。
    persons = canon_source.character_forms(result.get("entities") or [])
    batch_text = "\n".join(item.text or "" for item in batch)
    kind_by_name = {e.get("canonical_name"): e.get("kind")
                    for e in result.get("entities", []) if e.get("canonical_name")}
    # 关系边受 uq（实体对+类型）约束只能原地升版：本批若含当前版本章，
    # 重断言的边翻回 current 并升版；若整批都是被替换掉的旧稿，则新边写成非当前。
    _cur_vers = [v for ok, v in seg_ctx.values() if ok]
    rel_is_current = bool(_cur_vers)
    rel_version = max(_cur_vers) if _cur_vers else 1
    for r in result.get("relations", []):
        if blocked:
            # 关系没有段号，无法逐段证明归属；本批只要有一段正文不一致，
            # 整批关系都不落业务表（宁可隔离，不可把别书关系注入提示词）。
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=None, kind="relation",
                        reason=_gate_reason(blocked), detail=dict(r))
            gate_stats["quarantined"]["relation"] += 1
            continue
        a_id = _ent(r["from"])
        b_id = _ent(r["to"])
        if a_id is None or b_id is None:
            continue
        gate_reason = canon_source.collection_edge_gate(
            r["rel_type"], r["from"], r["to"],
            canon_source.gate_window(batch_text, r["from"], r["to"]), persons,
        )
        # 物品不是 belongs_to 的合法端点（人物/组织 ↔ 物品是「持有」，该走 owns）。
        # 不建正式边，落 candidate 留人工复核；reason 记在 description（该列此前
        # 未使用），与 v2.6 侧 gen_candidates.reason='item_endpoint' 同口径。
        item_endpoint = r["rel_type"] == "belongs_to" and (
            _endpoint_kind(db, r["from"], a_id, kind_by_name) == "item"
            or _endpoint_kind(db, r["to"], b_id, kind_by_name) == "item"
        )
        status = "candidate" if (gate_reason or item_endpoint) else "active"
        # 本批证据判定的章：唯一共现章优先，否则批内最小章
        rel_hit = _relation_chapter(
            _rel_forms(a_id, r["from"]), _rel_forms(b_id, r["to"]),
            seg_items, None)
        rel_ch = rel_hit if rel_hit is not None else rel_chapter
        rel = db.scalar(
            select(KgRelation).where(
                KgRelation.project_id == project_id,
                KgRelation.from_id == a_id, KgRelation.to_id == b_id,
                KgRelation.rel_type == r["rel_type"],
            )
        )
        if rel is None:
            rel = KgRelation(project_id=project_id, from_id=a_id, to_id=b_id,
                             rel_type=r["rel_type"], strength=r.get("strength", 0.5),
                             status=status, since_chapter=rel_ch,
                             description="item_endpoint" if item_endpoint else None,
                             is_current=rel_is_current, version=rel_version)
            db.add(rel)
            rel_evo_pending.append((rel, None, rel.strength or 0.5, "批抽建立"))
            stats.relations_upserted += 1
        else:
            # 老行（早期批抽产物）没有 since_chapter：补上，否则永远按章查不到。
            cur_ch = rel.since_chapter
            if rel_ch is not None and (cur_ch is None or rel_ch < cur_ch):
                rel.since_chapter = rel_ch
            elif (rel_hit is not None and cur_ch is not None
                  and cur_ch != rel_hit and cur_ch in batch_chapters):
                # 同批内改判：证据只在另一章出现 → 把边还给它自己的章。
                # 旧逻辑只降不升（rel_chapter < since_chapter 才写），批内被
                # 最早章 min() 吸附后，后几章重抽也永远拿不回来。
                rel.since_chapter = rel_hit
            if rel_is_current and not rel.is_current:
                # 同章重写后边被重新断言 → 翻回当前版本（行不删，历史在 evolution）
                rel.is_current = True
                rel.version = max(int(rel.version or 1), rel_version)
            if rel.status != "active" and status == "active":
                # 只升不降：同一对实体在别的章里有明确人名支撑时转正；
                # 单章上下文弱不足以把已有正式边降级。
                rel.status = "active"
            if abs((rel.strength or 0.5) - r.get("strength", 0.5)) > 0.01:
                # strength 变化 → 追加 evolution 行（记过程不记快照）
                rel_evo_pending.append(
                    (rel, rel.strength, r.get("strength", 0.5), "批抽更新"))
                rel.strength = r.get("strength", 0.5)

    # —— 3. events 追加（sequence 项目内连续）——
    # seq_map：LLM 给的批内 sequence → 落库后的实际 event.id。
    # event_links / entity_knowledge 用 sequence 引用事件（LLM 不知道自增 id），
    # 必须 flush 拿到 id 后才能解析——两段式落库。
    base_seq = _next_event_sequence(db, project_id)
    seq_map: dict[int, int] = {}
    # 事件段号：批抽一次吃 5 段，**每条事件记它自己所属的段**（extractor 的
    # attribute_event_segments 已按段内命中打标）；拿不到合法段号才退批首段。
    # 旧实现一律写 batch[0]，后 4 段的事件全挂到第 1 段，按段过滤（judge /
    # retrieval）就错位。
    valid_segs = {int(it.segment_no) for it in batch}
    created: list[tuple[int | None, KgEvent]] = []
    next_seq = base_seq
    for ev in result.get("events", []):
        subject_id = _ent(ev.get("subject") or "")
        object_id = _ent(ev.get("object") or "")
        location_id = _ent(ev.get("location") or "")
        ev_seg = ev.get("segment")
        if isinstance(ev_seg, bool) or not isinstance(ev_seg, int) \
                or ev_seg not in valid_segs:
            ev_seg = batch[0].segment_no if batch else None
        event_type = ev.get("event_type", "incident")
        verb = (ev.get("verb") or "")[:64]
        summary = (ev.get("summary") or "")[:500]
        if ev_seg in blocked:
            # 事件按自己的段号归属；该段正文已被判定不是本项目正文 → 隔离。
            # detail 存**已解析好 id 的完整行**，--restore 可直接回放。
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=ev_seg, kind="event",
                        reason=blocked[ev_seg],
                        detail={"novel_id": book, "sequence": None,
                                "chapter": ev.get("chapter"),
                                "segment_no": ev_seg, "event_type": event_type,
                                "summary": summary, "subject_id": subject_id,
                                "verb": verb, "object_id": object_id,
                                "location_id": location_id,
                                "in_story_date": (ev.get("in_story_date") or "")[:64]})
            gate_stats["quarantined"]["event"] += 1
            continue
        # 幂等：同一段重抽（requeue / 回放）不许把同一事件再追加一条。
        # 去重键 = (project, segment_no, event_type, subject_id, object_id, verb, summary)。
        dup_id = db.scalar(select(KgEvent.id).where(
            KgEvent.project_id == project_id,
            KgEvent.segment_no == ev_seg,
            KgEvent.event_type == event_type,
            KgEvent.subject_id == subject_id,
            KgEvent.object_id == object_id,
            KgEvent.verb == verb,
            KgEvent.summary == summary,
        ))
        llm_seq = ev.get("sequence")
        ev_cur, ev_ver, ev_ch = _ver_ctx(ev_seg)
        # 章号一律用权威值（章节标签派生），模型自己从正文里猜的章号只作回退：
        # 实测同一段会同时出现 chapter=1 与 chapter=N 两种，按章读会漏/串。
        if ev_ch is None:
            ev_ch = ev.get("chapter")
        if dup_id is not None:
            # 重抽命中同一条：翻回当前版本（版本号对齐本章），不再追加行
            db.execute(update(KgEvent).where(KgEvent.id == dup_id).values(
                chapter=ev_ch, is_current=ev_cur, version=ev_ver))
            stats.events_skipped += 1
            if llm_seq is not None:
                seq_map[llm_seq] = dup_id
            continue
        event = KgEvent(
            project_id=project_id,
            novel_id=book,
            sequence=next_seq,
            chapter=ev_ch,
            segment_no=ev_seg,
            event_type=event_type,
            summary=summary,
            subject_id=subject_id,
            verb=verb,
            object_id=object_id,
            location_id=location_id,
            in_story_date=(ev.get("in_story_date") or "")[:64],
            is_current=ev_cur,
            version=ev_ver,
        )
        next_seq += 1
        db.add(event)
        created.append((llm_seq, event))
        stats.events_added += 1
    db.flush()  # 拿 event.id 建 sequence→id 映射
    for llm_seq, event in created:
        if llm_seq is not None:
            seq_map[llm_seq] = event.id

    # —— 3.5 event_links（用 sequence→id 映射解析事件引用）——
    for lk in result.get("event_links", []):
        if blocked:
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=None, kind="event_link",
                        reason=_gate_reason(blocked), detail=dict(lk))
            gate_stats["quarantined"]["event_link"] += 1
            continue
        from_eid = seq_map.get(lk.get("from_sequence"))
        to_eid = seq_map.get(lk.get("to_sequence"))
        if from_eid is None or to_eid is None:
            continue
        existing = db.scalar(
            select(KgEventLink).where(
                KgEventLink.project_id == project_id,
                KgEventLink.from_event_id == from_eid,
                KgEventLink.to_event_id == to_eid,
                KgEventLink.link_type == lk.get("link_type"),
            )
        )
        if existing is None:
            db.add(KgEventLink(
                project_id=project_id, from_event_id=from_eid, to_event_id=to_eid,
                link_type=lk.get("link_type"),
            ))
            stats.event_links_added += 1

    # —— 3.6 entity_knowledge（人物情报，learned_at 用 sequence 解析）——
    # known_kind 三类的落库策略（Novelium 情报追踪闭环的写入侧）：
    # - entity：knows 是实体名 → 解析 entity id（原有行为）
    # - event：knows 是事件描述 → 按 kg_events.summary 匹配解析 event id；
    #   匹配不上降级为 known_text 存文本（不丢情报，known_entity_id=None）
    # - secret：秘密文本没有对应实体 → known_entity_id=None + known_text 存原文
    for k in result.get("entity_knowledge", []):
        if blocked:
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=None, kind="entity_knowledge",
                        reason=_gate_reason(blocked), detail=dict(k))
            gate_stats["quarantined"]["entity_knowledge"] += 1
            continue
        char_id = _ent(k.get("character") or "")
        if char_id is None:
            continue
        kk = k.get("known_kind") or "entity"
        known_id: int | None = None
        known_text: str | None = None
        learned_event_id = seq_map.get(k.get("learned_at_sequence"))
        if kk == "entity":
            known_id = _ent(k.get("knows") or "")
            if known_id is None or known_id == char_id:
                continue
        elif kk == "event":
            known_id = _lookup_event_id(db, project_id, k.get("knows") or "")
            if known_id is None:
                known_text = (k.get("knows") or "")[:200]  # 降级：事件没匹配上，存文本
        else:  # secret：秘密是自由文本，无实体可指
            known_text = (k.get("knows") or "")[:200]
        # 去重按 uq 同款四元组（不带 kind：entity id 与 event id 是两套自增，
        # 数值可能撞——任何已存在行都挡插入，宁可漏一行不可整批 IntegrityError）；
        # 文本行（known_id=None）追加 known_text 匹配，否则同一角色的第二条秘密会被误判重复
        dup_q = (
            select(KgEntityKnowledge).where(
                KgEntityKnowledge.project_id == project_id,
                KgEntityKnowledge.entity_id == char_id,
                KgEntityKnowledge.known_entity_id == known_id,  # None → IS NULL
                KgEntityKnowledge.learned_at_event_id == learned_event_id,
            )
        )
        if known_id is None:
            dup_q = dup_q.where(KgEntityKnowledge.known_text == known_text)
        existing = db.scalar(dup_q)
        if existing is None:
            db.add(KgEntityKnowledge(
                project_id=project_id, entity_id=char_id, known_entity_id=known_id,
                known_kind=kk, known_text=known_text, learned_at_event_id=learned_event_id,
            ))
            stats.entity_knowledge_added += 1

    # —— 4. state_changes ——
    for s in result.get("state_changes", []):
        if blocked:
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=None, kind="state_change",
                        reason=_gate_reason(blocked), detail=dict(s))
            gate_stats["quarantined"]["state_change"] += 1
            continue
        char_id = _ent(s.get("character") or "")
        if char_id is None:
            continue
        # 状态归属到**证据所在段**（旧实现一律 batch[0]：一次抽 5 段时后 4 段
        # 的状态全挂首段），章号跟着该段的权威章号走，不再写 chapter=None。
        st_seg = _state_segment(s, batch)
        st_cur, st_ver, st_ch = _ver_ctx(st_seg)
        st_type = s.get("state_type", "mood")
        st_val = (s.get("new_value") or "")[:500]
        dup_st = db.scalar(select(KgCharacterState.id).where(
            KgCharacterState.project_id == project_id,
            KgCharacterState.character_id == char_id,
            KgCharacterState.chapter == st_ch,
            KgCharacterState.state_type == st_type,
            KgCharacterState.new_value == st_val,
        )) if st_ch is not None else None
        if dup_st is not None:
            # 同章同状态重抽命中：翻回当前版本，不追加重复行
            db.execute(update(KgCharacterState).where(
                KgCharacterState.id == dup_st).values(
                segment_no=st_seg, is_current=st_cur, version=st_ver))
            continue
        db.add(KgCharacterState(
            project_id=project_id, character_id=char_id,
            chapter=st_ch, segment_no=st_seg,
            state_type=st_type, new_value=st_val,
            is_current=st_cur, version=st_ver,
        ))
        stats.state_changes += 1

    # —— 5. foreshadows upsert（name 唯一，状态机推进）——
    for f in result.get("foreshadows", []):
        if blocked:
            _quarantine(db, project_id=project_id, run_id=gate_run_id,
                        segment_no=None, kind="foreshadow",
                        reason=_gate_reason(blocked), detail=dict(f))
            gate_stats["quarantined"]["foreshadow"] += 1
            continue
        fs = db.scalar(
            select(KgForeshadow).where(
                KgForeshadow.project_id == project_id, KgForeshadow.name == f["name"]
            )
        )
        if fs is None:
            fs = KgForeshadow(project_id=project_id, name=f["name"],
                              status=f.get("status", "planted"),
                              planted_chapter=f.get("planted_chapter"),
                              planned_payoff_chapter=f.get("planned_payoff_chapter"))
            db.add(fs)
        else:
            # 状态机只前进不后退：paid_off 是终态倾向，planted→developing 可推进
            _ORDER = {"planted": 0, "developing": 1, "paid_off": 2, "expired": 3, "dropped": 4}
            if _ORDER.get(f.get("status", ""), 0) > _ORDER.get(fs.status, 0):
                fs.status = f["status"]
            if f.get("planned_payoff_chapter"):
                fs.planned_payoff_chapter = f["planned_payoff_chapter"]
        stats.foreshadows += 1

    # —— 6. mentions 反向索引（实体×段，context_window 原文片段）——
    # 本批内按「唯一键口径」先去重：SessionLocal 是 autoflush=False，上面那条
    # select 看不到同批尚未 flush 的新增行；同一实体被两个抽取名解析到（或两段
    # 共用同一 generation）时会插两条同 (project, entity, generation) 的行，
    # 撞 uq_kg_mention 让整批重试也必失败（run 168 三次全挂在这）。
    seen_mention: set[tuple[int, int]] = set()
    guards = (mention_guards if mention_guards is not None
              else _item_mention_guards(db, project_id))
    for item in batch:
        for name, ent_id in name_to_id.items():
            # 命中规范名或其称呼层写法都算（「许郎」也要落到许七安）；
            # 带约束的写法（白莲 vs 白莲花）在 canon_source 里统一校验；
            # 物品名再用登记集合挡一次（「地书」不吃「地书碎片」）。
            form = canon_source.text_mention(item.text, name, guards=guards)
            if form:
                if item.segment_no in blocked:
                    # 该段正文不是本项目正文：提及不落 kg_mentions，
                    # 整行（含 context_window）转存隔离区备查/回放。
                    _quarantine(
                        db, project_id=project_id,
                        run_id=item.run_id or gate_run_id,
                        segment_no=item.segment_no, kind="mention",
                        reason=blocked[item.segment_no],
                        detail={"entity_id": ent_id, "entity_name": name,
                                "generation_id": seg_to_gen.get(item.segment_no),
                                "novel_id": book, "segment_no": item.segment_no,
                                "context_window": _window_around(item.text, form),
                                "mention_kind": MENTION_KIND_LLM})
                    gate_stats["quarantined"]["mention"] += 1
                    continue
                gen_id = seg_to_gen.get(item.segment_no)
                # 唯一键是 (project, entity, generation)；generation 为空时 SQLite
                # 唯一键不生效，退回按段去重。
                ukey = (ent_id, gen_id if gen_id is not None else -item.segment_no)
                if ukey in seen_mention:
                    continue
                seen_mention.add(ukey)
                mn_cur, mn_ver, _ = _ver_ctx(item.segment_no)
                # 查询键必须与唯一键 `(project, entity, generation)` 对齐：按段号查
                # 时，同一 generation 换段号重入（或并发重抽）查不到已有行，插入
                # 就会撞 uq（ch19 的 `UNIQUE constraint failed: kg_mentions`）。
                # generation 为空（无绑定）时唯一键不生效，才退回按段去重。
                if gen_id is not None:
                    existing = db.scalar(
                        select(KgMention).where(
                            KgMention.project_id == project_id,
                            KgMention.entity_id == ent_id,
                            KgMention.generation_id == gen_id,
                        )
                    )
                else:
                    existing = db.scalar(
                        select(KgMention).where(
                            KgMention.project_id == project_id,
                            KgMention.entity_id == ent_id,
                            KgMention.segment_no == item.segment_no,
                        )
                    )
                if existing is None:
                    db.add(KgMention(
                        project_id=project_id, entity_id=ent_id, generation_id=gen_id,
                        novel_id=book,
                        segment_no=item.segment_no,
                        context_window=_window_around(item.text, form),
                        mention_kind=MENTION_KIND_LLM,
                        is_current=mn_cur, version=mn_ver,
                    ))
                else:
                    # 同段重抽命中：翻回当前版本（旧稿版本化保留，不删行）
                    existing.is_current = mn_cur
                    existing.version = mn_ver

    # 6.5 提及补漏：LLM 没抽到、但库里**已有**实体（尤其 41 条物品）在正文里
    # 出现时同样要落提及。p5 的 8 条提及全指向 character、item 提及 0 条，根因
    # 就是成功路径只给本批 LLM 返回的实体建提及，已有实体不参与匹配。
    added_alias, alias_touched = _alias_mentions_into(
        db, project_id, batch, book=book, guards=guards,
        seg_to_gen=seg_to_gen,
        candidates=_mention_candidates(db, project_id, novel_id=book),
        blocked=blocked, seg_ctx=seg_ctx,
    )
    stats.mentions_alias_added = added_alias
    _refresh_mention_counts(db, project_id, set(name_to_id.values()) | alias_touched)
    # —— 6.6 事件投影：状态 / 伏笔 / 情报 / 关系演化 ——
    # 这四张表此前的写入只依赖抽取器原样返回的 state_changes / foreshadows /
    # entity_knowledge / strength 变化，模型几乎不产出 → 四张表长期 0 行。这里
    # 从**本批已落库的事件**做机械投影（不新增抽取口径），保证读侧（judge /
    # retrieval / bible）查得到数据；没有依据的字段留空，不猜。
    id_to_name = {v: k for k, v in name_to_id.items()}
    _project_event_facts(
        db, project_id=project_id, events=created,
        name_of=lambda eid: (id_to_name.get(eid)
                             or (_entity_name(db, eid))),
        rel_evo_pending=rel_evo_pending, stats=stats,
    )
    db.commit()
    return stats


def _entity_name(db: Session, entity_id: int | None) -> str | None:
    if entity_id is None:
        return None
    from app.models import KgEntity

    ent = db.get(KgEntity, entity_id)
    return ent.canonical_name if ent is not None else None


def _project_event_facts(db: Session, *, project_id: int,
                         events: list[tuple[int | None, "KgEvent"]],
                         name_of, rel_evo_pending: list, stats) -> None:
    """把本批事件投影进四张「事件派生表」，只拆列不做新判定。

    - ``kg_character_state``：事件带 location → 主体位置状态；death → alive=false
    - ``kg_foreshadows``：foreshadow_plant / foreshadow_payoff 事件 → 伏笔状态机
    - ``kg_entity_knowledge``：revelation 事件 → 主体得知客体
    - ``kg_relation_evolution``：本批新建/改强度的关系各补一条演化行，章号尽量
      取自同时涉及两端的事件（事件是关系变化的依据）
    """
    from app.models import (
        KgCharacterState, KgEntityKnowledge, KgForeshadow, KgRelationEvolution,
    )

    latest: dict[tuple[int, str], str | None] = {}

    def _latest_state(character_id: int, state_type: str) -> str | None:
        key = (character_id, state_type)
        if key not in latest:
            row = db.scalar(
                select(KgCharacterState).where(
                    KgCharacterState.project_id == project_id,
                    KgCharacterState.character_id == character_id,
                    KgCharacterState.state_type == state_type,
                    # 只跟「当前版本」比：旧版本的旧值不该挡住本次投影
                    KgCharacterState.is_current.is_(True),
                ).order_by(
                    KgCharacterState.segment_no.desc(),
                    KgCharacterState.id.desc(),
                ).limit(1)
            )
            latest[key] = row.new_value if row is not None else None
        return latest[key]

    def _add_state(character_id: int, state_type: str, value: str,
                   chapter, segment_no, event_id: int,
                   is_current: bool = True, version: int = 1) -> None:
        # 同章同值已有（当前或历史）行 → 翻回当前版本，不追加重复行
        dup = db.scalar(select(KgCharacterState.id).where(
            KgCharacterState.project_id == project_id,
            KgCharacterState.character_id == character_id,
            KgCharacterState.chapter == chapter,
            KgCharacterState.state_type == state_type,
            KgCharacterState.new_value == value[:500],
        )) if chapter is not None else None
        if dup is not None:
            db.execute(update(KgCharacterState).where(
                KgCharacterState.id == dup).values(
                segment_no=segment_no, cause_event_id=event_id,
                is_current=is_current, version=version))
            latest[(character_id, state_type)] = value[:500]
            return
        db.add(KgCharacterState(
            project_id=project_id, character_id=character_id,
            chapter=chapter, segment_no=segment_no,
            state_type=state_type, new_value=value[:500],
            cause_event_id=event_id, is_current=is_current, version=version,
        ))
        latest[(character_id, state_type)] = value[:500]
        stats.state_changes += 1

    for _, ev in events:
        # 1) 位置 / 死亡（事件自带 subject + location / event_type）
        if ev.subject_id and ev.location_id:
            place = name_of(ev.location_id)
            if place and _latest_state(ev.subject_id, "location") != place:
                _add_state(ev.subject_id, "location", place,
                           ev.chapter, ev.segment_no, ev.id,
                           is_current=bool(ev.is_current), version=int(ev.version or 1))
        if ev.subject_id and ev.event_type == "death":
            if _latest_state(ev.subject_id, "alive") != "false":
                _add_state(ev.subject_id, "alive", "false",
                           ev.chapter, ev.segment_no, ev.id,
                           is_current=bool(ev.is_current), version=int(ev.version or 1))

        # 2) 伏笔状态机：摘要即伏笔标签（抽取层没有单独的伏笔名字段）
        if ev.event_type in ("foreshadow_plant", "foreshadow_payoff"):
            label = (ev.summary or "").strip()[:200]
            if not label:
                continue
            fs = db.scalar(select(KgForeshadow).where(
                KgForeshadow.project_id == project_id,
                KgForeshadow.name == label))
            if ev.event_type == "foreshadow_plant":
                if fs is None:
                    db.add(KgForeshadow(
                        project_id=project_id, name=label, status="planted",
                        planted_event_id=ev.id, planted_chapter=ev.chapter,
                        description=label[:500]))
                    stats.foreshadows += 1
                elif fs.planted_event_id is None:
                    fs.planted_event_id = ev.id
                    fs.planted_chapter = fs.planted_chapter or ev.chapter
            else:
                if fs is None:
                    db.add(KgForeshadow(
                        project_id=project_id, name=label, status="paid_off",
                        payoff_event_id=ev.id,
                        actual_payoff_chapter=ev.chapter,
                        description=label[:500]))
                else:
                    fs.status = "paid_off"
                    fs.payoff_event_id = ev.id
                    fs.actual_payoff_chapter = ev.chapter
                stats.foreshadows += 1

        # 3) 情报：revelation = 主体得知客体
        if (ev.event_type == "revelation" and ev.subject_id and ev.object_id
                and ev.subject_id != ev.object_id):
            dup = db.scalar(select(KgEntityKnowledge.id).where(
                KgEntityKnowledge.project_id == project_id,
                KgEntityKnowledge.entity_id == ev.subject_id,
                KgEntityKnowledge.known_entity_id == ev.object_id,
            ))
            if dup is None:
                db.add(KgEntityKnowledge(
                    project_id=project_id, entity_id=ev.subject_id,
                    known_entity_id=ev.object_id, known_kind="entity",
                    learned_at_event_id=ev.id,
                    learned_at_chapter=ev.chapter,
                ))
                stats.entity_knowledge_added += 1

    if not rel_evo_pending:
        return
    # 4) 关系演化：优先挂「同时涉及两端」的事件，其次用本批首个事件的章号
    db.flush()  # 新关系此时才有 id
    pair_events: dict[frozenset[int], "KgEvent"] = {}
    for _, ev in events:
        if ev.subject_id and ev.object_id:
            pair_events.setdefault(
                frozenset((ev.subject_id, ev.object_id)), ev)
    first_chapter = events[0][1].chapter if events else None
    for rel, _old, new_strength, note in rel_evo_pending:
        if rel.id is None:
            continue
        ev = pair_events.get(frozenset((rel.from_id, rel.to_id)))
        db.add(KgRelationEvolution(
            project_id=project_id, edge_id=rel.id,
            chapter=(ev.chapter if ev is not None else first_chapter),
            strength=new_strength, note=note,
        ))


def _project_holdings(db: Session, project_id: int, result: dict,
                      batch: list[_QueueItem], *, book: int | None) -> dict:
    """把本批抽取结果投影进 ``generated.db``（组 → 写法 → 持有段）。

    与 v3.0 落库分开：``generated.db`` 是独立库、独立连接，投影失败不回滚
    ``novel_dev.db`` 已提交的行，但必须把错误交给调用方（worker 据此把批标
    failed），不能 success 却什么都没写。
    """
    from app.kg.holding_projection import project_holdings

    batch_kinds = {e.get("canonical_name"): e.get("kind")
                   for e in result.get("entities") or []
                   if e.get("canonical_name")}

    def _kind(name: str) -> str | None:
        return _endpoint_kind(
            db, name,
            _lookup_entity_id(db, project_id, name, novel_id=book),
            batch_kinds)

    def _owner(name: str) -> tuple[int | None, str | None]:
        eid = _lookup_entity_id(db, project_id, name, novel_id=book)
        return eid, _endpoint_kind(db, name, eid, batch_kinds)

    def _event_id(item_name: str, owner_name: str,
                  segment_no: int | None) -> int | None:
        """反查「持有者拿到物品」的那条事件 id（transfer_event_id 用）。

        只认主体=持有者、客体=物品的真实事件；同段优先，其次取最新一条。
        查不到返回 None——不拿别的事件硬凑。
        """
        from app.models import KgEvent

        owner_id = _lookup_entity_id(db, project_id, owner_name, novel_id=book)
        item_id = _lookup_entity_id(db, project_id, item_name, novel_id=book)
        if owner_id is None or item_id is None:
            return None
        cond = [KgEvent.project_id == project_id,
                KgEvent.subject_id == owner_id,
                KgEvent.object_id == item_id]
        if book is not None:
            cond.append(KgEvent.novel_id.in_((None, book)))
        if segment_no is not None:
            hit = db.scalar(
                select(KgEvent.id).where(*cond,
                                         KgEvent.segment_no == segment_no)
                .order_by(KgEvent.sequence.desc()).limit(1))
            if hit is not None:
                return hit
        return db.scalar(
            select(KgEvent.id).where(*cond)
            .order_by(KgEvent.sequence.desc()).limit(1))

    return project_holdings(project_id=project_id, book=book, result=result,
                            batch=batch, kind_of=_kind, owner_lookup=_owner,
                            event_lookup=_event_id)


def _mention_candidates(db: Session, project_id: int,
                        *, novel_id: int | None = None) -> list[tuple[int, list[str]]]:
    """库里**已有**实体的 mention 匹配写法：规范名 + aliases（item 与 character）。

    降级路径不依赖本批 LLM 结果——这是「抽取失败也还能落 mention」的前提。
    书维度**始终收窄**（与读侧 ``prompt_scope_book_ids`` 同口径）：
    - 有本书 id → 本书实体 + ``novel_id IS NULL`` 通配；
    - 原创项目（``novel_id=None``）→ 只认 ``novel_id IS NULL`` 的项目级实体。

    不能把 ``None`` 当「不过滤」：原创项目的书值本来就是 NULL，而不是「未知」。
    早先这里按 None 放开，别名匹配把参考书实体（如 novel_id=7 的人物）配进了
    本书正文，写出「mention 行 novel_id=NULL、却指向参考书实体」的错配（第 10 章
    121 条里 103 条如此）；读侧第二道实体门虽然挡住了，但这类行本身就是脏的。
    """
    from sqlalchemy import or_
    from app.kg.book_scope import prompt_scope_book_ids, scope_sql_values

    scope = scope_sql_values(
        (novel_id, None) if novel_id is not None else (None,))
    stmt = select(KgEntity.id, KgEntity.canonical_name, KgEntity.aliases).where(
        KgEntity.project_id == project_id,
        KgEntity.kind.in_(MENTION_KINDS),
        func.coalesce(KgEntity.novel_id, -1).in_(scope),
    )
    rows = db.execute(stmt).all()
    out: list[tuple[int, list[str]]] = []
    for ent_id, canonical, aliases in rows:
        forms: list[str] = []
        for raw in [canonical, *(aliases or [])]:
            form = raw.strip() if isinstance(raw, str) else ""
            if form and form not in forms:
                forms.append(form)
        if forms:
            out.append((ent_id, forms))
    return out


def _first_mentioned_form(text: str, forms: list[str],
                          guards: frozenset[str]) -> str | None:
    """按 ``forms`` 顺序找第一个在文本里独立命中的写法。

    独立命中由 ``canon_source.count_forms`` 判：最长优先 + 材质字边界，
    「地书」不会被「地书碎片」算成命中。
    """
    for form in forms:
        if canon_source.count_forms(text, [form], guards=guards) > 0:
            return form
    return None


def _refresh_mention_counts(db: Session, project_id: int,
                            entity_ids: set[int]) -> None:
    """按 ``kg_mentions`` **当前版本**行数回写 ``entity.mention_count``。

    两条写路径共用；旧版本提及仍留在库里（版本化不删行），计数只看 current，
    否则重写一章会把提及数灌成 2 倍。
    """
    for ent_id in entity_ids:
        cnt = db.scalar(
            select(func.count()).select_from(KgMention).where(
                KgMention.project_id == project_id, KgMention.entity_id == ent_id,
                KgMention.is_current.is_(True),
            )
        )
        ent = db.get(KgEntity, ent_id)
        if ent is not None:
            ent.mention_count = cnt or 0


def _alias_mentions_into(db: Session, project_id: int, batch: list[_QueueItem], *,
                         book: int | None, guards: frozenset[str],
                         seg_to_gen: dict[int, int | None],
                         candidates: list[tuple[int, list[str]]],
                         blocked: dict[int, str] | None = None,
                         is_current: bool = True, version: int = 1,
                         seg_ctx: dict[int, tuple[bool, int]] | None = None,
                         ) -> tuple[int, set[int]]:
    """用「已有实体的规范名 + aliases」给本批各段补 mention（**不 commit**）。

    LLM 成功路径与降级路径共用同一个补漏口径：前者先落 LLM 抽到的实体提及，
    再用本函数补「LLM 没抽到、但正文确实写了」的实体——p5 的 8 条提及全指向
    character、item 提及 0 条，根因就是成功路径只给 LLM 返回的实体建提及，
    库里已有的 41 条物品实体从不参与匹配。

    幂等：同一 (project, entity, segment_no) 已有行（不论 kind）就跳过。
    """
    added = 0
    touched: set[int] = set()
    per_seg: dict[int, int] = {}
    empty_segments: list[int] = []
    # SessionLocal 是 autoflush=False：LLM 主路径刚 add 的 mention 还在 identity
    # map 里，不 flush 就查不到，补漏会再插一条同 (entity, generation_id) 的行
    # 撞 uq 约束。先 flush 再按 (entity, segment_no) 去重。
    db.flush()
    for item in batch:
        text = item.text or ""
        if not text:
            empty_segments.append(item.segment_no)
            continue
        per_seg[item.segment_no] = 0
        item_gen = seg_to_gen.get(item.segment_no)
        for ent_id, forms in candidates:
            # 与唯一键 `(project, entity, generation)` 对齐（见主路径同款注释）：
            # 按段号查会漏掉「同 generation 换了段号」的已有行，补漏再插就撞 uq。
            if item_gen is not None:
                existing = db.scalar(
                    select(KgMention.id).where(
                        KgMention.project_id == project_id,
                        KgMention.entity_id == ent_id,
                        KgMention.generation_id == item_gen,
                    )
                )
            else:
                existing = db.scalar(
                    select(KgMention.id).where(
                        KgMention.project_id == project_id,
                        KgMention.entity_id == ent_id,
                        KgMention.segment_no == item.segment_no,
                    )
                )
            if existing is not None:
                continue
            # 正文命中守卫：必须真实命中规范名/aliases，绝不放宽成「按实体存在就算提及」
            form = _first_mentioned_form(text, forms, guards)
            if form is None:
                continue
            if blocked and item.segment_no in blocked:
                # 文本归属门：被拦段不做别名补漏（LLM 路径已按段留痕，
                # 降级路径由 _index_mentions_only 统一留段级隔离行）。
                continue
            m_cur, m_ver = (seg_ctx or {}).get(
                item.segment_no, (is_current, version))
            db.add(KgMention(
                project_id=project_id, entity_id=ent_id,
                generation_id=seg_to_gen.get(item.segment_no),
                novel_id=book,
                segment_no=item.segment_no,
                context_window=_window_around(text, form),
                mention_kind=MENTION_KIND_ALIAS,
                is_current=m_cur, version=m_ver,
            ))
            added += 1
            touched.add(ent_id)
            per_seg[item.segment_no] += 1
    # 可观测：哪些段补到了、哪些段一个物品名都没命中、哪些段连正文都没有。
    # 「补不上」本身也是结论，不能只留一个 added=0 让人以为跑过了。
    _log.info(
        "kg alias mention 索引：项目 %s 候选实体 %d 个，命中段 %s，"
        "未命中段 %s，空正文段 %s，共新增 %d 条",
        project_id, len(candidates),
        {s: n for s, n in per_seg.items() if n},
        [s for s, n in per_seg.items() if not n],
        empty_segments, added,
    )
    return added, touched


def _index_mentions_only(db: Session, project_id: int,
                         batch: list[_QueueItem] | None = None, *,
                         book: int | None = None,
                         binding_out: dict | None = None,
                         gate_out: dict | None = None) -> int:
    """降级路径：零 LLM 落 mention 索引，返回新增行数。

    抽取失败 / provider 不可用（含本地模板兜底）时调用。实体来自
    ``kg_entities`` 里**已有**的 item 与 character（规范名 + aliases），不看本批
    LLM 结果——所以「抽取失败」不再等于「一条 mention 都没有」。

    ``book``：本批所属书（调用方算好传入），用于收窄候选实体并写进新 mention。
    ``None`` = 不按书收窄、mention 落 ``NULL``，与加书维度之前一致。

    兼容旧签名 ``(db, batch)``（``backend/tests/test_v30_extractor.py`` 直接这么
    调）：第二参是批次时 project_id 从批首段取。

    ``mention_kind='alias'`` 与 LLM 路径的 ``'llm'`` 区分，读侧不区分。
    """
    if batch is None:
        batch = project_id  # 旧调用：第二参是 batch
        project_id = batch[0].project_id if batch else None
    if not batch:
        return 0
    seg_to_gen, binding_report = _seg_to_gen(db, project_id, batch)
    if binding_out is not None:
        binding_out.update(binding_report)
    # 降级路径同样要过文本归属门（这里也写 kg_mentions）。
    blocked = _text_gate(binding_report, batch)
    gate_stats = _new_gate_stats(blocked, binding_report)
    if gate_out is not None:
        gate_out.update(gate_stats)
    added, touched = _alias_mentions_into(
        db, project_id, batch, book=book,
        guards=_item_mention_guards(db, project_id),
        seg_to_gen=seg_to_gen,
        candidates=_mention_candidates(db, project_id, novel_id=book),
        blocked=blocked,
    )
    # 降级路径没有 LLM 产物可逐条留痕：按段记一条隔离行，注明是 alias-only。
    for item in batch:
        if item.segment_no not in blocked:
            continue
        _quarantine(
            db, project_id=project_id,
            run_id=item.run_id or _run_id_of(batch),
            segment_no=item.segment_no, kind="mention",
            reason=blocked[item.segment_no],
            detail={"phase": "alias_only", "segment_no": item.segment_no,
                    "novel_id": book,
                    "text_head": (item.text or "")[:200]})
        gate_stats["quarantined"]["mention"] += 1
    if not added and not any(gate_stats["quarantined"].values()):
        return 0
    db.flush()
    # 即使 0 条新增，只要隔离区有行也必须 commit：否则拦截留痕随会话关闭丢失。
    if added:
        _refresh_mention_counts(db, project_id, touched)
    db.commit()
    return added


def _lookup_entity_id(db: Session, project_id: int, name: str,
                      *, novel_id: int | None = None) -> int | None:
    """按 canonical_name / aliases / 归一后别名 反查已有 entity id（跨批关系解析兜底）。

    本批 entities 里没有、但库里已存在的实体（前批抽过的），关系仍能建上。
    ``novel_id`` 传了才按书收窄（这本书 + NULL 通配）——**跨书同名不串**：
    书 6 的关系端点绝不会解析到书 5 的同名实体。
    aliases 用 JSON 包含查询（SQLite json_each 兜底用 Python 侧过滤——批抽规模
    小，entity 表通常在千级以内，线性扫可接受）。
    新增归一兜底：name 是某 entity canonical_name 剥前缀后的核心称谓（如"父亲"），
    也能反查到该 entity（"林悠然的父亲"）。
    """
    if not name:
        return None

    def _in_book(e) -> bool:
        if novel_id is None:
            # book=None 是**本书值**（原创项目没有独立书 id），不是通配：
            # 只认同书行，否则本书抽取会把同名实体并到参考书那本上（ch13 的
            # 「照片」「协议」就是这样挂到 novel_id=7 实体上的）。
            return e.novel_id is None
        return e.novel_id in (None, novel_id)

    # 1) 精确匹配 canonical_name（不限 kind：关系端点可以是物品，最终 kind 由
    #    _endpoint_kind 另行判定）。同名跨 kind 时不能交给 db.scalar 抛
    #    MultipleResultsFound，走确定性解析。
    from app.kg.entity_lookup import resolve_entity_by_name

    ent = resolve_entity_by_name(db, project_id, name, novel_id=novel_id,
                                 own_only=(novel_id is None))
    if ent is not None:
        return ent.id
    # 2) aliases 反查
    for e in db.scalars(select(KgEntity).where(KgEntity.project_id == project_id)):
        if name in (e.aliases or []) and _in_book(e):
            return e.id
    # 3) 归一反查：name 是某 entity 剥修饰词后的核心称谓（如"父亲" → "林悠然的父亲"）
    from app.kg.entity_resolve import should_merge
    for e in db.scalars(select(KgEntity).where(KgEntity.project_id == project_id)):
        if should_merge(name, e.canonical_name) and _in_book(e):
            return e.id
    return None


def _endpoint_kind(db: Session, name: str, ent_id: int | None,
                   kind_by_name: dict[str, str]) -> str | None:
    """关系端点实体的 kind：本批抽到的直接取，跨批已存在的回库查一次。"""
    kind = kind_by_name.get(name)
    if kind:
        return kind
    if ent_id is None:
        return None
    return db.scalar(select(KgEntity.kind).where(KgEntity.id == ent_id))


def _lookup_event_id(db: Session, project_id: int, summary: str) -> int | None:
    """按事件简述反查 kg_events.id（known_kind=event 的解析）。

    漏报优于误报（继承本项目纪律）：
    1. 精确匹配 summary 全等
    2. 双向包含（LLM 简述是库内 summary 的前缀/缩写，或反之）
    3. 2-字关键词全覆盖：LLM 简述里每个 ≥2 字的连续中文片段都出现在库内
       summary 里（数量随库增长，批抽规模千级事件线性扫可接受）
    都不命中返回 None——调用方降级为 known_text 存文本，不丢情报。
    """
    if not summary:
        return None
    from app.kg.knowledge_match import event_summary_match

    events = db.scalars(
        select(KgEvent).where(KgEvent.project_id == project_id)
    ).all()
    for ev in events:
        if event_summary_match(summary, ev.summary or ""):
            return ev.id
    return None


def _upsert_entity(db: Session, project_id: int, e: dict,
                   *, novel_id: int | None = None):
    """entity upsert：aliases 合并，attrs 深合并。

    书维度（``novel_id``，传了才生效）：只在「这本书」与「NULL 通配」里找，
    真书行优先；找不到就新建一行带书。**绝不并到另一本书的同名实体**——
    「书 5 的镇北王」和「书 6 的镇北王」是两个人，并了就等于让事实串书。
    新建时 ``novel_id=None`` 表示项目级通配（项目没指定书），行为与加列前一致。

    三层匹配（漏报优于误报）：
    1. canonical_name 完全相等 → 标准 upsert
    2. 归一等价（entity_resolve.should_merge）→ 短名并入长名，
       短名作为别名加到长名的 aliases
    3. 都不匹配 → 新建
    """
    from app.kg.entity_resolve import should_merge, merge_target

    def _in_book(row) -> bool:
        if novel_id is None:
            # 同 _lookup_entity_id：book=None 只认本书行，不跨书合并同名实体。
            return row.novel_id is None
        return row.novel_id in (None, novel_id)

    rows = db.scalars(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.kind == e["kind"],
            KgEntity.canonical_name == e["canonical_name"],
        )
    ).all()
    rows = [r for r in rows if _in_book(r)]
    ent = None
    if rows:
        ent = min(rows, key=lambda r: (r.novel_id is None, r.id))
    if ent is None:
        # 归一匹配：遍历同 kind 的现有 entity，找"长名含本名修饰词"或反向
        for existing in db.scalars(
            select(KgEntity).where(
                KgEntity.project_id == project_id, KgEntity.kind == e["kind"]
            )
        ):
            if not _in_book(existing):
                continue
            # 改名后的旧名回流防线（character_rename）：旧名被登记进 aliases 后，
            # 老段重抽只会产出旧名；精确等于 aliases 时必须并回同一实体，
            # 否则会再建一行，和新名并存。只认精确匹配，不做模糊归并。
            if e["canonical_name"] in (existing.aliases or []):
                ent = existing
                break
            if should_merge(e["canonical_name"], existing.canonical_name):
                # 现有是长名（如"林悠然的父亲"），新来是短名（如"父亲"）→ 并入现有
                ent = existing
                break
            if should_merge(existing.canonical_name, e["canonical_name"]):
                # 现有是短名，新来是长名 → 长名是 canonical，短名做 alias
                # 把现有的 aliases 搬过来
                old_aliases = existing.aliases or []
                new_aliases = list(dict.fromkeys(old_aliases + [existing.canonical_name]))
                existing.canonical_name = e["canonical_name"]
                existing.aliases = new_aliases[:20]
                ent = existing
                break
    if ent is None:
        ent = KgEntity(
            project_id=project_id, kind=e["kind"],
            novel_id=novel_id,
            canonical_name=e["canonical_name"],
            aliases=e.get("aliases") or [],
            attrs=e.get("attrs") or {},
        )
        db.add(ent)
        db.flush()  # 拿 id
        return ent
    # 已存在：合并 aliases（去重）+ attrs 深合并
    old_aliases = ent.aliases or []
    new_aliases = e.get("aliases") or []
    # 归一关键：把"对方原 canonical_name"也加进 aliases——
    # 否则短名"父亲"并入长名"林悠然的父亲"后，aliases 仍是 []，
    # 后续按"父亲"反查仍走归一路径而非 aliases 路径（效率低）
    if e["canonical_name"] != ent.canonical_name:
        new_aliases = list(new_aliases) + [e["canonical_name"]]
    merged = list(dict.fromkeys(old_aliases + new_aliases))
    if merged != old_aliases:
        ent.aliases = merged[:20]
    old_attrs = ent.attrs or {}
    new_attrs = e.get("attrs") or {}
    if new_attrs:
        merged_attrs = dict(old_attrs)
        merged_attrs.update({k: v for k, v in new_attrs.items() if v})
        ent.attrs = merged_attrs
    return ent


def _seg_to_gen(db: Session, project_id: int,
                batch: list[_QueueItem]) -> tuple[dict[int, int], dict]:
    """段号 → generation id 映射（mentions FK 用）+ 绑定报告。

    只按段号绑定是不够的：同一个段号可能对应**不是这段正文**的 generation
    （历史上把参考语料按项目段号入队就撞过：p5 段 1/2 的提及挂到了小杨正文的
    generation 上）。所以绑定前逐字比对正文，宁可不绑也不挂错：

    - 该段号没有 generation → ``no_generation``
    - 该段号有多条 generation（取谁都不唯一）→ ``multi_candidate``
    - 唯一一条但 ``text`` 与入队正文不逐字相同 → ``text_mismatch``
    - 唯一且逐字相同 → 绑定，计 ``bound``

    返回 ``(mapping, report)``；report 进 ``AgentRun.note``（不改 schema）：
    ``{"bound": int, "unbound": {"text_mismatch": n, "multi_candidate": n,
    "no_generation": n}, "segments": [...]}``。
    """
    segs = sorted({it.segment_no for it in batch})
    rows = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id, Generation.segment_no.in_(segs)
        )
    ).all()
    by_seg: dict[int, list[Generation]] = {}
    for g in rows:
        by_seg.setdefault(g.segment_no, []).append(g)

    mapping: dict[int, int] = {}
    report: dict = {
        "bound": 0,
        "unbound": {"text_mismatch": 0, "multi_candidate": 0, "no_generation": 0},
        "segments": [],
    }
    for item in batch:
        cands = by_seg.get(item.segment_no, [])
        if not cands:
            reason = "no_generation"
        elif len(cands) > 1:
            reason = "multi_candidate"
        elif (cands[0].text or "") != (item.text or ""):
            reason = "text_mismatch"
        else:
            mapping[item.segment_no] = cands[0].id
            report["bound"] += 1
            report["segments"].append({
                "segment_no": item.segment_no,
                "generation_id": cands[0].id, "status": "bound",
            })
            continue
        report["unbound"][reason] += 1
        report["segments"].append({
            "segment_no": item.segment_no, "status": "unbound", "reason": reason,
            "generation_ids": [g.id for g in cands],
        })

    unbound = report["unbound"]
    if any(unbound.values()):
        _log.warning(
            "kg mention 绑定未全覆盖：项目 %s 绑定 %d，未绑定 %s（段明细 %s）",
            project_id, report["bound"], unbound,
            [s for s in report["segments"] if s["status"] == "unbound"])
    else:
        _log.info("kg mention 绑定：项目 %s 全部 %d 段按正文逐字匹配成功",
                  project_id, report["bound"])
    return mapping, report


def _next_event_sequence(db: Session, project_id: int) -> int:
    """项目内下一个事件序号（连续）。"""
    from sqlalchemy import func

    mx = db.scalar(
        select(func.max(KgEvent.sequence)).where(KgEvent.project_id == project_id)
    )
    return (mx or 0) + 1


def _window_around(text: str, name: str, width: int = 60) -> str:
    """取名字在文本中的上下文片段（前后各 width/2 字）。"""
    i = text.find(name)
    if i < 0:
        return text[:width]
    half = width // 2
    start = max(0, i - half)
    end = min(len(text), i + len(name) + half)
    return text[start:end]
