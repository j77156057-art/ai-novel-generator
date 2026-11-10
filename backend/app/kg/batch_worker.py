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
import threading
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kg import canon_source
from app.models import (
    AgentRun, Generation, KgEntity, KgRelation, KgEvent, KgEventLink,
    KgCharacterState, KgForeshadow, KgEntityKnowledge, KgMention, Project,
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
RETRY_LIMIT = 2
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


@dataclass
class _QueueItem:
    """待抽取的一段。"""

    project_id: int
    segment_no: int
    text: str
    chapter: int | None = None


class _BatchQueue:
    """内存队列 + 每项目独立攒批。线程安全（锁保护队列与攒批缓冲）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[int, deque[_QueueItem]] = {}
        self._flush_event = threading.Event()
        self._stop = False

    def put(self, item: _QueueItem) -> None:
        with self._lock:
            q = self._queues.setdefault(item.project_id, deque())
            q.append(item)
            # 超上限丢最旧（生成优先，KG 补齐靠后续批）
            while len(q) > MAX_QUEUE:
                q.popleft()

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
                    chapter: int | None = None) -> int:
    """段 commit 后调用：入队等待异步批抽。零 LLM、零阻塞。

    顺序固定为「先落库、再入队」：
    1. 写一行 AgentRun(role=kg_batch_extract, status=running) 并**自己 commit**。
       不再依赖调用方后续（如 `_rollup_summary_tree`）的 commit——那一步之前一旦
       抛异常，入队行就丢了，worker 捞不到、库里也查不到。
    2. 行落地成功后才进内存队列，避免 worker 抢在 commit 之前处理、查不到行。

    request_hash = sha256("{project_id}:{segment_no}")，稳定可关联（重捞时按它找回）。
    失败抛 `KgEnqueueError`（调用方必须感知），不返回 None。
    返回 AgentRun id。
    """
    from datetime import datetime, timezone

    try:
        payload = json.dumps({"segment_no": segment_no, "chapter": chapter},
                             ensure_ascii=False)
        row = AgentRun(
            project_id=project_id, role=ROLE_KG_EXTRACT, status="running",
            started_at=datetime.now(timezone.utc),
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

    _queue.put(_QueueItem(project_id=project_id, segment_no=segment_no,
                          text=text, chapter=chapter))
    _signal_flush()
    return row_id


def _signal_flush() -> None:
    """通知 worker 有新段（攒批窗口内尽快处理）。"""
    _queue.signal_flush()


def flush_project(project_id: int) -> None:
    """手动触发一个项目的批抽（测试/收尾用）。"""
    _run_batches(project_id)


#: worker 线程引用（status API 看存活用；ensure_worker 幂等赋值）
_worker_thread: threading.Thread | None = None


def ensure_worker() -> None:
    """确保后台 worker 线程在跑（幂等）。"""
    global _worker_started, _worker_thread
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="kg-v3-batch-worker")
        t.start()
        _worker_thread = t
        _worker_started = True


def worker_alive() -> bool:
    """worker 线程是否活着（从未启动 = False）。"""
    t = _worker_thread
    return bool(t is not None and t.is_alive())


def _worker_loop() -> None:
    """worker 主循环：轮询各项目队列，攒批抽取落库。"""
    while not _queue._stop:
        projects = _queue.pending_projects()
        if not projects:
            _queue.wait_flush(timeout=10.0)
            continue
        for pid in projects:
            try:
                _run_batches(pid)
            except Exception:  # noqa: BLE001 —— 单项目失败不影响其他项目
                import logging
                logging.getLogger(__name__).warning("kg v3 批抽项目 %d 失败", pid, exc_info=True)


def _run_batches(project_id: int) -> None:
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
        """本批各段对应的 AgentRun 行（request_hash 精确匹配）。"""
        out: dict[int, AgentRun] = {}
        for it in batch:
            h = hashlib.sha256(f"{project_id}:{it.segment_no}".encode("utf-8")).hexdigest()
            row = db.scalar(select(AgentRun).where(
                AgentRun.project_id == project_id,
                AgentRun.role == ROLE_KG_EXTRACT,
                AgentRun.request_hash == h,
                AgentRun.status == "running",
            ))
            if row is not None:
                out[it.segment_no] = row
        return out

    while True:
        batch = _queue.take_batch(project_id)
        if not batch:
            return
        db = SessionLocal()
        canon_meta: dict = {}
        try:
            # —— 记账：回答「这批是用哪份词表抽的」（源 + 内容 hash + 规模）——
            canon_meta = canon_source.log_effective(
                _log,
                prefix=(f"kg v3 批抽 project={project_id} "
                        f"段={','.join(str(it.segment_no) for it in batch)} "),
            )
            rows = _rows_for(batch)
            provider = _get_provider()
            if _provider_unavailable(provider):
                # provider 不可用（None / 本地模板兜底）：抽不出实体，但**仍要落
                # mention**——用库里已有实体做别名匹配，不能「一条 mention 都没有」。
                added = _index_mentions_only(db, project_id, batch)
                for row in rows.values():
                    row.status = "failed"
                    row.finished_at = datetime.now(timezone.utc)
                    _merge_note(row, {
                        "error": ("provider 不可用（"
                                  f"{type(provider).__name__ if provider else 'None'}）"),
                        "alias_only": True, "mentions_added": added,
                        "canon": canon_meta,
                    })
                db.commit()
                _log.warning(
                    "kg v3 批抽降级为 alias mention（项目 %s，%s 段）：provider 不可用，"
                    "落 %d 条 mention", project_id, len(batch), added)
                continue
            texts = [(it.segment_no, it.text, it.chapter) for it in batch]
            last_exc: Exception | None = None
            stats: ExtractionStats | None = None
            # 探针：extract_batch 内部不抛异常（避免 KG 拖垮生成主链），失败时
            # 返回带 ERROR_FIELD 的空骨架。所以这里既有「裸 chat 探活」兜 provider
            # 连不上，也有「ERROR_FIELD 判定」兜「连得上但抽不出来」——后者以前
            # 会被记成 success + model="(empty)"，把失败伪装成成功，现已改为 failed。
            # 注意区分：合法 JSON 但确实没实体（真空）不写 ERROR_FIELD，仍记 success，
            # 只打 empty 标记。
            for _attempt in range(RETRY_LIMIT + 1):
                try:
                    provider.chat("连通性探针，请回复 ok", "ok")
                    result = extract_batch(provider, texts)
                    err = result.get(ERROR_FIELD)
                    if err:
                        raise RuntimeError(str(err))
                    stats = _persist_extraction(db, project_id, result, batch)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 —— 重试吃掉本次异常
                    db.rollback()
                    last_exc = exc
            now = datetime.now(timezone.utc)
            if last_exc is None and stats is not None:
                # 真空：LLM 正常返回、解析成功，但整批确实 0 产出。
                # 这不是失败（有些段落就是没有可抽实体），但要看得出「没抽到东西」。
                empty = (stats.entities_upserted == 0 and stats.events_added == 0
                         and stats.relations_upserted == 0)
                note_extra: dict = {"batch_stats": stats.__dict__.copy(),
                                    "canon": canon_meta}
                if empty:
                    note_extra["empty"] = True
                for row in rows.values():
                    row.status = "success"
                    row.finished_at = now
                    row.model = None
                    _merge_note(row, note_extra)
                db.commit()
            else:
                err = str(last_exc)[:200] if last_exc else "unknown"
                # 抽取失败也必须留 mention：`_persist_extraction` 的 rollback 会把
                # 本批已写的 mention 一起丢，这里用库里已有实体补一次别名索引。
                added = _index_mentions_only(db, project_id, batch)
                for row in rows.values():
                    row.status = "failed"
                    row.finished_at = now
                    _merge_note(row, {"error": err, "alias_only": True,
                                      "mentions_added": added, "canon": canon_meta})
                db.commit()
                _log.warning(
                    "kg v3 批抽失败（项目 %s，%s 段）：%s；已补 %d 条 alias mention",
                    project_id, len(batch), err, added)
        finally:
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


def worker_status(db: Session, project_id: int, *, recent_limit: int = 50) -> dict:
    """批抽 worker 状态总览（GET /kg-v3/worker/status 的数据源）。

    - worker_alive：线程引用 is_alive（从未启动=False）
    - pending：内存队列积压段数（重启即清零——持久状态看 runs）
    - stuck：running 且 started_at 超 STUCK_MINUTES（进程重启的残留/真卡死）
    - recent_runs：最近 N 行（新的在前），note 里的 JSON 已解析
    """
    from datetime import datetime, timezone

    runs = db.scalars(select(AgentRun).where(
        AgentRun.project_id == project_id,
        AgentRun.role == ROLE_KG_EXTRACT,
    ).order_by(AgentRun.id.desc()).limit(recent_limit)).all()
    now = datetime.now(timezone.utc)
    summary = {"success": 0, "failed": 0, "running": 0, "stuck": 0, "empty": 0}
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
        stuck = (r.status == "running" and started is not None
                 and (now - started).total_seconds() > STUCK_MINUTES * 60)
        if stuck:
            summary["stuck"] += 1
        elif r.status in summary:
            summary[r.status] += 1
        try:
            note = json.loads(r.note) if r.note else {}
        except (ValueError, TypeError):
            note = {"raw": r.note}
        if note.get("empty"):
            summary["empty"] += 1
        items.append({
            "id": r.id,
            "segment_no": note.get("segment_no"),
            "status": r.status,
            "stuck": stuck,
            "empty": bool(note.get("empty")),
            "duration_ms": duration_ms,
            "batch_stats": note.get("batch_stats"),
            "error": note.get("error"),
            "started_at": started.isoformat() if started else None,
        })
    return {
        "worker_alive": worker_alive(),
        "pending": _queue.pending_count(project_id),
        "summary": summary,
        "recent_runs": items,
    }


def _aware(dt):
    """把 naive datetime 当 UTC（SQLite 读回来常是 naive）。"""
    from datetime import timezone

    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_stuck(row: AgentRun, now=None) -> bool:
    """running 且 started_at 超过 STUCK_MINUTES = 进程残留/真卡死。"""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    started = _aware(row.started_at)
    return (row.status == "running" and started is not None
            and (now - started).total_seconds() > STUCK_MINUTES * 60)


def _note_dict(row: AgentRun) -> dict:
    try:
        value = json.loads(row.note) if row.note else {}
    except (ValueError, TypeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _requeue_row(db: Session, row: AgentRun, *, reason: str) -> tuple[str, int | None]:
    """把一行 AgentRun 重新入队（正文从 Generation 回读）。

    返回 ``(outcome, segment_no)``：
    - ``"requeued"``：已重新入队，行重置 running
    - ``"missing"``：正文不存在/为空，行标 failed 并写 error（**不静默丢**）
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
    _queue.put(_QueueItem(project_id=row.project_id, segment_no=seg_no,
                          text=gen.text, chapter=note.get("chapter")))
    row.status = "running"
    row.started_at = datetime.now(timezone.utc)
    row.finished_at = None
    _merge_note(row, {"requeued_at": row.started_at.isoformat(),
                      "requeue_reason": reason, "error": None})
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
        outcome, _seg = _requeue_row(db, row, reason="manual_requeue")
        if outcome == "requeued":
            requeued += 1
        else:
            missing.append(rid)
    if requeued or missing:
        db.commit()
    if requeued:
        _signal_flush()
    return {"requeued": requeued, "missing": missing, "skipped": skipped}


def recover_running(db: Session, project_id: int | None = None, *,
                    stuck_only: bool = False) -> dict:
    """把库里停在 running 的批抽行重新入队（启动恢复 / 手动超时重捞）。

    - ``stuck_only=False``（启动时用）：**所有** running 行都重入队。启动那一刻
      本进程的内存队列一定是空的，所以「running 行」等价于「上一进程留下的、
      永远没人处理的段」——不需要等它超时（30 分钟）才恢复。
    - ``stuck_only=True``（手动 API）：只重捞超过 STUCK_MINUTES 的 running 行。

    同一段存在多条 running 行时只重入队一次，其余标 failed（duplicate 留痕），
    避免同一段被重复抽取。

    返回 ``{"requeued", "missing", "skipped", "segments"}`` 并在日志里留痕；
    正文缺失的行会被标 failed + error，而不是静默跳过。
    """
    stmt = select(AgentRun).where(
        AgentRun.role == ROLE_KG_EXTRACT,
        AgentRun.status == "running",
    )
    if project_id is not None:
        stmt = stmt.where(AgentRun.project_id == project_id)
    rows = db.scalars(stmt.order_by(AgentRun.id)).all()

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    requeued = 0
    missing: list[int] = []
    skipped: list[dict] = []
    segments: list[dict] = []
    seen: set[tuple[int, int | None]] = set()
    for row in rows:
        if stuck_only and not _is_stuck(row, now):
            skipped.append({"run_id": row.id, "reason": "not_stuck"})
            continue
        note = _note_dict(row)
        key = (row.project_id, note.get("segment_no"))
        if key in seen:
            row.status = "failed"
            row.finished_at = now
            _merge_note(row, {"error": "duplicate_running_row"})
            skipped.append({"run_id": row.id, "reason": "duplicate_segment"})
            continue
        seen.add(key)
        outcome, seg_no = _requeue_row(
            db, row,
            reason="startup_recover" if not stuck_only else "stuck_recover",
        )
        if outcome == "requeued":
            requeued += 1
            segments.append({"run_id": row.id, "project_id": row.project_id,
                             "segment_no": seg_no})
        else:
            missing.append(row.id)
    if requeued or missing or skipped:
        db.commit()
    if requeued:
        _signal_flush()
    if requeued or missing:
        _log.info(
            "kg 批抽恢复（%s）：重入队 %d 段，正文缺失 %d 行，跳过 %d 行%s",
            "启动" if not stuck_only else "超时重捞", requeued, len(missing),
            len(skipped), f"：{segments}" if segments else "",
        )
    return {"requeued": requeued, "missing": missing, "skipped": skipped,
            "segments": segments}


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
            rid = enqueue_segment(db, project_id, seg_no, gen.text, chapter=seg_no)
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


def _get_provider():
    try:
        from app.llm.provider import get_llm_provider

        return get_llm_provider()
    except Exception:  # noqa: BLE001
        return None


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


def _persist_extraction(db: Session, project_id: int, result: dict, batch: list[_QueueItem],
                        *, mention_guards: frozenset[str] | None = None):
    """把归一化后的抽取结果落 v3.0 表。幂等 upsert。"""
    from app.kg.extractor_v3 import ExtractionStats

    stats = ExtractionStats(segments=len(batch))
    # 称呼层归一（cleaned 源生效时才动手）：许郎/许公子/魏公/许二叔 等写法折到
    # 实体规范名，原文词进 aliases；关系两端同步归一，避免同一人被拆成两个节点。
    # legacy 源下 normalize 是恒等函数，行为不变。
    canon_source.normalize_extraction(result)
    # 段→generation id 映射（mentions 落库用）
    seg_to_gen = _seg_to_gen(db, project_id, batch)

    # —— 1. entities upsert（canonical_name + kind 唯一）——
    name_to_id: dict[str, int] = {}
    for e in result.get("entities", []):
        ent = _upsert_entity(db, project_id, e)
        if ent:
            name_to_id[e["canonical_name"]] = ent.id
            stats.entities_upserted += 1

    # —— 2. relations upsert（两端名字解析为 id，缺失跳过）——
    # 集合端点门（与 v2.6 同一条判定逻辑）：成员/归属类边挂在集合/上位节点、
    # 且窗口里没有任何具体人名 → 落 status='candidate'，不删边。
    # 窗口取本批文本里同时提到两端的那一句；人名表用词表人物 + 本批抽到的人物。
    persons = canon_source.character_forms(result.get("entities") or [])
    batch_text = "\n".join(item.text or "" for item in batch)
    kind_by_name = {e.get("canonical_name"): e.get("kind")
                    for e in result.get("entities", []) if e.get("canonical_name")}
    for r in result.get("relations", []):
        a_id = name_to_id.get(r["from"]) or _lookup_entity_id(db, project_id, r["from"])
        b_id = name_to_id.get(r["to"]) or _lookup_entity_id(db, project_id, r["to"])
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
                             status=status,
                             description="item_endpoint" if item_endpoint else None)
            db.add(rel)
            stats.relations_upserted += 1
        else:
            if rel.status != "active" and status == "active":
                # 只升不降：同一对实体在别的章里有明确人名支撑时转正；
                # 单章上下文弱不足以把已有正式边降级。
                rel.status = "active"
            if abs((rel.strength or 0.5) - r.get("strength", 0.5)) > 0.01:
                # strength 变化 → 追加 evolution 行（记过程不记快照）
                from app.models import KgRelationEvolution

                db.add(KgRelationEvolution(
                    project_id=project_id, edge_id=rel.id,
                    strength=r.get("strength", 0.5), note="批抽更新",
                ))
                rel.strength = r.get("strength", 0.5)

    # —— 3. events 追加（sequence 项目内连续）——
    # seq_map：LLM 给的批内 sequence → 落库后的实际 event.id。
    # event_links / entity_knowledge 用 sequence 引用事件（LLM 不知道自增 id），
    # 必须 flush 拿到 id 后才能解析——两段式落库。
    base_seq = _next_event_sequence(db, project_id)
    seq_map: dict[int, int] = {}
    for i, ev in enumerate(result.get("events", [])):
        subject_id = name_to_id.get(ev.get("subject") or "") or _lookup_entity_id(db, project_id, ev.get("subject") or "")
        object_id = name_to_id.get(ev.get("object") or "") or _lookup_entity_id(db, project_id, ev.get("object") or "")
        location_id = name_to_id.get(ev.get("location") or "") or _lookup_entity_id(db, project_id, ev.get("location") or "")
        event = KgEvent(
            project_id=project_id,
            sequence=base_seq + i,
            chapter=ev.get("chapter"),
            segment_no=batch[0].segment_no if batch else None,
            event_type=ev.get("event_type", "incident"),
            summary=ev.get("summary", "")[:500],
            subject_id=subject_id,
            verb=(ev.get("verb") or "")[:64],
            object_id=object_id,
            location_id=location_id,
            in_story_date=(ev.get("in_story_date") or "")[:64],
        )
        db.add(event)
        stats.events_added += 1
    db.flush()  # 拿 event.id 建 sequence→id 映射
    # 重新查本批 events（flush 后 id 已分配），按 sequence 排序对齐 LLM 的批内序号
    db_events = db.scalars(
        select(KgEvent).where(
            KgEvent.project_id == project_id,
            KgEvent.sequence >= base_seq,
        ).order_by(KgEvent.sequence)
    ).all()
    for pos, ev in enumerate(result.get("events", [])):
        llm_seq = ev.get("sequence")
        if llm_seq is not None and pos < len(db_events):
            seq_map[llm_seq] = db_events[pos].id

    # —— 3.5 event_links（用 sequence→id 映射解析事件引用）——
    for lk in result.get("event_links", []):
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
        char_id = name_to_id.get(k.get("character") or "") or _lookup_entity_id(db, project_id, k.get("character") or "")
        if char_id is None:
            continue
        kk = k.get("known_kind") or "entity"
        known_id: int | None = None
        known_text: str | None = None
        learned_event_id = seq_map.get(k.get("learned_at_sequence"))
        if kk == "entity":
            known_id = name_to_id.get(k.get("knows") or "") or _lookup_entity_id(db, project_id, k.get("knows") or "")
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
        char_id = name_to_id.get(s.get("character") or "") or _lookup_entity_id(db, project_id, s.get("character") or "")
        if char_id is None:
            continue
        db.add(KgCharacterState(
            project_id=project_id, character_id=char_id,
            chapter=None, segment_no=batch[0].segment_no if batch else None,
            state_type=s.get("state_type", "mood"), new_value=(s.get("new_value") or "")[:500],
        ))
        stats.state_changes += 1

    # —— 5. foreshadows upsert（name 唯一，状态机推进）——
    for f in result.get("foreshadows", []):
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
    guards = (mention_guards if mention_guards is not None
              else _item_mention_guards(db, project_id))
    for item in batch:
        for name, ent_id in name_to_id.items():
            # 命中规范名或其称呼层写法都算（「许郎」也要落到许七安）；
            # 带约束的写法（白莲 vs 白莲花）在 canon_source 里统一校验；
            # 物品名再用登记集合挡一次（「地书」不吃「地书碎片」）。
            form = canon_source.text_mention(item.text, name, guards=guards)
            if form:
                gen_id = seg_to_gen.get(item.segment_no)
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
                        segment_no=item.segment_no,
                        context_window=_window_around(item.text, form),
                        mention_kind=MENTION_KIND_LLM,
                    ))

    _refresh_mention_counts(db, project_id, set(name_to_id.values()))
    db.commit()
    return stats


def _mention_candidates(db: Session, project_id: int) -> list[tuple[int, list[str]]]:
    """库里**已有**实体的 mention 匹配写法：规范名 + aliases（item 与 character）。

    降级路径不依赖本批 LLM 结果——这是「抽取失败也还能落 mention」的前提。
    """
    rows = db.execute(
        select(KgEntity.id, KgEntity.canonical_name, KgEntity.aliases).where(
            KgEntity.project_id == project_id,
            KgEntity.kind.in_(MENTION_KINDS),
        )
    ).all()
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
    """按 ``kg_mentions`` 实际行数回写 ``entity.mention_count``（两条写路径共用）。"""
    from sqlalchemy import func

    for ent_id in entity_ids:
        cnt = db.scalar(
            select(func.count()).select_from(KgMention).where(
                KgMention.project_id == project_id, KgMention.entity_id == ent_id
            )
        )
        ent = db.get(KgEntity, ent_id)
        if ent is not None:
            ent.mention_count = cnt or 0


def _index_mentions_only(db: Session, project_id: int,
                         batch: list[_QueueItem]) -> int:
    """降级路径：零 LLM 落 mention 索引，返回新增行数。

    抽取失败 / provider 不可用（含本地模板兜底）时调用。实体来自
    ``kg_entities`` 里**已有**的 item 与 character（规范名 + aliases），不看本批
    LLM 结果——所以「抽取失败」不再等于「一条 mention 都没有」。

    ``mention_kind='alias'`` 与 LLM 路径的 ``'llm'`` 区分，读侧不区分。
    幂等：同一 (project, entity, segment_no) 已有行（不论 kind）就跳过。
    """
    candidates = _mention_candidates(db, project_id)
    guards = _item_mention_guards(db, project_id)
    seg_to_gen = _seg_to_gen(db, project_id, batch)
    added = 0
    touched: set[int] = set()
    per_seg: dict[int, int] = {}
    empty_segments: list[int] = []
    for item in batch:
        text = item.text or ""
        if not text:
            empty_segments.append(item.segment_no)
            continue
        per_seg[item.segment_no] = 0
        for ent_id, forms in candidates:
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
            db.add(KgMention(
                project_id=project_id, entity_id=ent_id,
                generation_id=seg_to_gen.get(item.segment_no),
                segment_no=item.segment_no,
                context_window=_window_around(text, form),
                mention_kind=MENTION_KIND_ALIAS,
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
    if not added:
        return 0
    db.flush()
    _refresh_mention_counts(db, project_id, touched)
    db.commit()
    return added


def _lookup_entity_id(db: Session, project_id: int, name: str) -> int | None:
    """按 canonical_name / aliases / 归一后别名 反查已有 entity id（跨批关系解析兜底）。

    本批 entities 里没有、但库里已存在的实体（前批抽过的），关系仍能建上。
    aliases 用 JSON 包含查询（SQLite json_each 兜底用 Python 侧过滤——批抽规模
    小，entity 表通常在千级以内，线性扫可接受）。
    新增归一兜底：name 是某 entity canonical_name 剥前缀后的核心称谓（如"父亲"），
    也能反查到该 entity（"林悠然的父亲"）。
    """
    if not name:
        return None
    # 1) 精确匹配 canonical_name（不限 kind：关系端点可以是物品，最终 kind 由
    #    _endpoint_kind 另行判定）。同名跨 kind 时不能交给 db.scalar 抛
    #    MultipleResultsFound，走确定性解析。
    from app.kg.entity_lookup import resolve_entity_by_name

    ent = resolve_entity_by_name(db, project_id, name)
    if ent is not None:
        return ent.id
    # 2) aliases 反查
    for e in db.scalars(select(KgEntity).where(KgEntity.project_id == project_id)):
        if name in (e.aliases or []):
            return e.id
    # 3) 归一反查：name 是某 entity 剥修饰词后的核心称谓（如"父亲" → "林悠然的父亲"）
    from app.kg.entity_resolve import should_merge
    for e in db.scalars(select(KgEntity).where(KgEntity.project_id == project_id)):
        if should_merge(name, e.canonical_name):
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


def _upsert_entity(db: Session, project_id: int, e: dict):
    """entity upsert：aliases 合并，attrs 深合并。

    三层匹配（漏报优于误报）：
    1. canonical_name 完全相等 → 标准 upsert
    2. 归一等价（entity_resolve.should_merge）→ 短名并入长名，
       短名作为别名加到长名的 aliases
    3. 都不匹配 → 新建
    """
    from app.kg.entity_resolve import should_merge, merge_target

    ent = db.scalar(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.kind == e["kind"],
            KgEntity.canonical_name == e["canonical_name"],
        )
    )
    if ent is None:
        # 归一匹配：遍历同 kind 的现有 entity，找"长名含本名修饰词"或反向
        for existing in db.scalars(
            select(KgEntity).where(
                KgEntity.project_id == project_id, KgEntity.kind == e["kind"]
            )
        ):
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


def _seg_to_gen(db: Session, project_id: int, batch: list[_QueueItem]) -> dict[int, int]:
    """段号 → generation id 映射（mentions FK 用）。"""
    segs = [it.segment_no for it in batch]
    rows = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id, Generation.segment_no.in_(segs)
        )
    ).all()
    return {g.segment_no: g.id for g in rows}


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
