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

from app.models import (
    AgentRun, Generation, KgEntity, KgRelation, KgEvent, KgEventLink,
    KgCharacterState, KgForeshadow, KgEntityKnowledge, KgMention, Project,
)

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
#: running 行超过这个分钟数视为 stuck（进程重启的残留/真卡死）
STUCK_MINUTES = 30


def _session_factory():
    """批抽用的 session 工厂（默认 `SessionLocal`；测试可注入）。

    为什么要留这个口子：批处理的异常兜底要验证「异常时那批的行被翻成 failed」，
    而 worker 原本硬编码 `SessionLocal()` ⇒ 测试连不到自己的临时库。
    """
    from app.db import SessionLocal

    return SessionLocal


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


def enqueue_segment(db: Session, project_id: int, segment_no: int, text: str,
                    chapter: int | None = None) -> int | None:
    """段 commit 后调用：入队等待异步批抽。零 LLM、零阻塞。

    同时写一行 AgentRun(role=kg_batch_extract, status=running)——**与 generation
    共用调用方的事务**（本函数不 commit），随段一起落库：入队即持久化。
    request_hash = sha256("{project_id}:{segment_no}")，稳定可关联（重捞时按它
    找回同一行）。失败静默返回 None（可观测性自身不能拖垮生成主链）。
    返回 AgentRun id（调用方目前不用，测试/调试用）。
    """
    from datetime import datetime, timezone

    _queue.put(_QueueItem(project_id=project_id, segment_no=segment_no,
                          text=text, chapter=chapter))
    _signal_flush()
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
        db.flush()  # 拿 id；commit 由调用方（generation 事务）负责
        return row.id
    except Exception:  # noqa: BLE001 —— 可观测性失败不阻断生成
        import logging
        logging.getLogger(__name__).warning(
            "kg 批抽 AgentRun 写入失败（seg %s）", segment_no, exc_info=True)
        return None


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

    from app.kg.extractor_v3 import extract_batch, ExtractionStats

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
        db = _session_factory()()
        try:
            rows: dict[int, AgentRun] = {}
            try:
                rows = _rows_for(batch)
                provider = _get_provider()
                if provider is None:
                    # 无 provider：段直接落 v3.0 mention 索引（零 LLM 部分仍做）
                    _index_mentions_only(db, batch)
                    continue
                texts = [(it.segment_no, it.text, it.chapter) for it in batch]
                # 把账上已有的未回收伏笔名给抽取器，要求**同名复用**（2026-10-28）。
                # 落库本来就按名字去重，但抽取器从前看不到已有的名字 ⇒ 每段给同一件事
                # 重新起说法，账本无界增长（实测 9 段攒出 117 个名字，
                # 切片把 97 条 active 全渲染进提示词 = 2,639 字）。
                known_fores = _active_foreshadow_names(db, project_id)
                last_exc: Exception | None = None
                stats: ExtractionStats | None = None
                # 探针：extract_batch 内部把 LLM 异常吞成空骨架（extract_segment 的
                # 静默兜底），LLM 挂掉的异常永远到不了这里——会被记成「成功但 0 产出」。
                # 每次尝试先用 1 次裸 chat 探活：provider 挂则本次尝试失败进重试；
                # 探针过了才让整批文本进 LLM（省得挂着的 provider 白收 5 段长文）。
                # 探针只探「连不连得上」，不探「抽不抽得出」——空骨架（可达但服从性
                # 归零）走下面的 empty 标记，重试通常没用，暴露给人判断更诚实。
                for _attempt in range(RETRY_LIMIT + 1):
                    try:
                        provider.chat("连通性探针，请回复 ok", "ok")
                        result = extract_batch(provider, texts, known_foreshadows=known_fores)
                        stats = _persist_extraction(db, project_id, result, batch)
                        last_exc = None
                        break
                    except Exception as exc:  # noqa: BLE001 —— 重试吃掉本次异常
                        db.rollback()
                        last_exc = exc
                now = datetime.now(timezone.utc)
                if last_exc is None and stats is not None:
                    # 空骨架检测：真实小说文本几乎不可能整批 0 产出——大概率 LLM 挂了
                    empty = (stats.entities_upserted == 0 and stats.events_added == 0
                             and stats.relations_upserted == 0)
                    note_extra: dict = {"batch_stats": stats.__dict__.copy()}
                    if empty:
                        note_extra["empty"] = True
                    for row in rows.values():
                        row.status = "success"
                        row.finished_at = now
                        row.model = "(empty)" if empty else None
                        _merge_note(row, note_extra)
                    db.commit()
                else:
                    err = str(last_exc)[:200] if last_exc else "unknown"
                    for row in rows.values():
                        row.status = "failed"
                        row.finished_at = now
                        _merge_note(row, {"error": err})
                    db.commit()
                    import logging
                    logging.getLogger(__name__).warning(
                        "kg v3 批抽失败（项目 %s，%s 段）：%s", project_id, len(batch), err)
            except Exception as exc:  # noqa: BLE001 —— 批内任何异常都要把这批的行翻成 failed
                # 之前只有 extract_batch 抛错才会翻；_get_provider / _persist_extraction
                # 等在 for 循环外的异常会冒到 _worker_loop ⇒ 整批的行永远留在 running。
                _mark_batch_failed(db, rows, exc)
                import logging
                logging.getLogger(__name__).warning(
                    "kg v3 批处理异常（项目 %s，%s 段）：%s", project_id, len(batch), exc)
        finally:
            db.close()


def _mark_batch_failed(db: Session, rows: dict[int, AgentRun], exc: BaseException) -> None:
    """把这一批的行全部翻成 failed（并写下错误原因）。

    自身也不许向外抛（否则 finally 会被绕过）。
    """
    from datetime import datetime, timezone

    try:
        db.rollback()
    except Exception:  # noqa: BLE001
        pass
    try:
        now = datetime.now(timezone.utc)
        err = f"{type(exc).__name__}: {exc}"[:200]
        for row in rows.values():
            row.status = "failed"
            row.finished_at = now
            _merge_note(row, {"error": err})
        db.commit()
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("批失败状态写回也失败了")


def reap_stale_runs(db: Session) -> int:
    """收割被进程重启打断的批抽行（role=kg_batch_extract, status=running）。

    ## 为什么必须有

    批抽跑在**进程内线程**（`WORKER_THREADS = 1`），队列在**内存 deque** 里。
    进程一重启（uvicorn reload / 多 worker / 服务重启 / 容器被杀），
    队列没了，但库里的 running 行还在 ⇒ 永远 running。

    实测（2026-10-29）：30 条 running，最早 2026-10-20，
    最新一条是**当天自己跑的那次**（服务被重启了）。

    ## 为什么是 failed 而不是「继续跑」

    后台线程已经没了，没人能接着跑完它；留成 running 等于撒谎。
    诚实的做法是标记失败 + 说明原因 + 提示「重捞一次即可」（界面��有手动重捞的入口）。

    ## 只碰 running

    success / failed 是已经结束的结论，覆盖了就再也找不回来。
    """
    from datetime import datetime, timezone

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
        # 中断的行：它**根本没跑完**（进程重启、队列丢了），
        # finished_at 是收割时刻 ⇒ 拿它减 started_at 会算出「跑了 3 天」（实测 658822s）⇒ 耗时不给。
        interrupted = bool(note.get("interrupted"))
        items.append({
            "id": r.id,
            "segment_no": note.get("segment_no"),
            "status": r.status,
            "stuck": stuck,
            "interrupted": interrupted,
            "empty": bool(note.get("empty")),
            "duration_ms": None if interrupted else duration_ms,
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


def requeue_failed(db: Session, project_id: int, run_ids: list[int]) -> dict:
    """重捞失败的段（POST /kg-v3/worker/requeue 的数据源）。

    按 run_id 找本项目的 failed 行 → 段文本从 Generation 表回读（enqueue 时
    的文本没存，正文才是事实源）→ 重新入队 + 行重置 running。
    文本已不存在的段报 missing（不静默丢）。
    """
    from datetime import datetime, timezone

    requeued = 0
    missing: list[int] = []
    for rid in run_ids:
        row = db.scalar(select(AgentRun).where(
            AgentRun.id == rid,
            AgentRun.project_id == project_id,
            AgentRun.role == ROLE_KG_EXTRACT,
        ))
        if row is None or row.status != "failed":
            continue
        try:
            note = json.loads(row.note) if row.note else {}
        except (ValueError, TypeError):
            note = {}
        seg_no = note.get("segment_no")
        gen = db.scalar(select(Generation).where(
            Generation.project_id == project_id,
            Generation.segment_no == seg_no,
        )) if seg_no is not None else None
        if seg_no is None or gen is None or not gen.text:
            missing.append(rid)
            continue
        _queue.put(_QueueItem(project_id=project_id, segment_no=seg_no,
                              text=gen.text, chapter=None))
        row.status = "running"
        row.started_at = datetime.now(timezone.utc)
        row.finished_at = None
        _merge_note(row, {"requeued_at": row.started_at.isoformat(),
                          "error": None})
        requeued += 1
    if requeued:
        db.commit()
        _signal_flush()
    return {"requeued": requeued, "missing": missing}


def _get_provider():
    try:
        from app.llm.provider import get_llm_provider

        return get_llm_provider()
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


def _is_suspect_foreshadow(name: str | None) -> bool:
    """这条抽出来的伏笔疑似**对话碎片**？（只用于留痕，不删不改账本）"""
    try:
        from app.kg.foreshadow_quality import looks_like_dialogue_fragment

        return looks_like_dialogue_fragment(name)
    except Exception:  # noqa: BLE001 —— 判不出来就不留痕，绝不能因此挡住落库
        return False


def _sanitize_payoff_chapter(planned, planted, text: str, *, closed: bool = False):
    """回收章过关才返回原值，否则 None（**当作未知**）。

    判据在 `app/kg/foreshadow_quality.plausible_payoff_chapter`：
    章号 ≥ 1、晚于埋下章、**且在正文里真的出现过**。
    `closed=True`（已回收/已丢弃）⇒ 只判「章号 ≥ 1」（历史记录不重新裁决）。
    判不出来（异常）⇒ **不放行**（返回 None）：宁可当未知，
    也不让一个凭空来的值进账。
    """
    try:
        from app.kg.foreshadow_quality import sanitize_payoff_chapter

        return sanitize_payoff_chapter(planned, planted, text, closed=closed)
    except Exception:  # noqa: BLE001 —— 判不出来就当未知，绝不能挡住落库
        return None


def _nearest_existing_name(name: str, existing: list[str]) -> tuple[str, float] | None:
    """新抽出的伏笔名，是不是账上某条老名字的**近似说法**？

    只用于留痕（计数 + 警告），**不改账本** ——
    阈值 0.55 的实测依据见 `app/kg/foreshadow_quality.py`。
    """
    try:
        from app.kg.foreshadow_quality import nearest_existing_name

        return nearest_existing_name(name, existing)
    except Exception:  # noqa: BLE001 —— 判不出来就不留痕，绝不能因此挡住落库
        return None


def _persist_extraction(db: Session, project_id: int, result: dict, batch: list[_QueueItem]):
    """把归一化后的抽取结果落 v3.0 表。幂等 upsert。"""
    from app.kg.extractor_v3 import ExtractionStats

    stats = ExtractionStats(segments=len(batch))
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
    for r in result.get("relations", []):
        a_id = name_to_id.get(r["from"]) or _lookup_entity_id(db, project_id, r["from"])
        b_id = name_to_id.get(r["to"]) or _lookup_entity_id(db, project_id, r["to"])
        if a_id is None or b_id is None:
            continue
        rel = db.scalar(
            select(KgRelation).where(
                KgRelation.project_id == project_id,
                KgRelation.from_id == a_id, KgRelation.to_id == b_id,
                KgRelation.rel_type == r["rel_type"],
            )
        )
        if rel is None:
            rel = KgRelation(project_id=project_id, from_id=a_id, to_id=b_id,
                             rel_type=r["rel_type"], strength=r.get("strength", 0.5))
            db.add(rel)
            stats.relations_upserted += 1
        elif abs((rel.strength or 0.5) - r.get("strength", 0.5)) > 0.01:
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
    #
    # 口径（2026-10-28 真机验收后拆开）：原来只有一个 `foreshadows`，
    # 记的是**模型吐出的条目数** —— 读的人会以为「新增了这么多条」。
    # 实测那次 note 写 11，账上实际是 7 条同名复用、0 条新增
    # （模型在同一次响应里吐了重复的名字）。现在拆成四个数：
    #   reused + duplicate_in_batch + new == extracted
    # 另加 near_variant：新落库的名字与某条老名字**高置信相似**
    # （只计数 + 警告，不改账本 —— 改账本要人点头）。
    existing_names: list[str] = []
    try:
        existing_names = [str(n) for n in db.scalars(
            select(KgForeshadow.name).where(KgForeshadow.project_id == project_id)
        ).all() if n]
    except Exception:  # noqa: BLE001 —— 取不到就当没有（只影响近似变体计数）
        existing_names = []
    seen_names: set[str] = set()
    # —— 回收章的正文证据（2026-10-29）——
    # 判据见 `app/kg/foreshadow_quality.plausible_payoff_chapter`：
    # 章号 ≥ 1、晚于埋下章、**且在正文里真的出现过**。
    # 没有证据的回收章 ⇒ 置空（当作未知），不写进账。
    # ⚠️ 「第 N 回」**不算**章号（「第一回」= 第一次）——见 `app.kg.chapter_evidence`。
    _batch_text = "\n".join(str(it.text or "") for it in (batch or []))
    for f in result.get("foreshadows", []):
        # —— 只留痕，不裁决（2026-10-28）——
        # 抽出来的伏笔有相当一部分其实是**台词碎片**（实测 19 条里 14 条）。
        # 这里**只计数 + 警告**，账本原样保留（删数据要人点头）；
        # 提示词侧另有渲染层过滤（见 `generation/prompts.py`）。
        name = str(f.get("name") or "")
        if _is_suspect_foreshadow(f.get("name")):
            stats.foreshadows_suspect += 1
            logging.getLogger(__name__).warning(
                "伏笔疑似对话碎片（已入账、但不会进提示词约束）：%s", f.get("name"))
        # —— 回收章过关才写（2026-10-29）——
        # 实测老账本 140 条里 **108 条**的回收章是编的（`0`/`50`/`30`/`12`），
        # 它们会让 judge 报出 **83 条假「伏笔超期」告警**，并在界面上显示
        # 「计划第 50 章回收」这种假计划。⇒ 源头拦住，不再写脏值。
        _status = f.get("status", "planted")
        _closed = _status in ("paid_off", "dropped")
        _payoff = _sanitize_payoff_chapter(
            f.get("planned_payoff_chapter"),
            f.get("planted_chapter"),
            _batch_text,
            closed=_closed,
        )
        if f.get("planned_payoff_chapter") is not None and _payoff is None:
            stats.foreshadows_bad_payoff += 1
            logging.getLogger(__name__).info(
                "伏笔回收章无正文证据，置空（当未知）：%s", f.get("name"))
        fs = db.scalar(
            select(KgForeshadow).where(
                KgForeshadow.project_id == project_id, KgForeshadow.name == name
            )
        )
        if fs is None and name in seen_names:
            # 同一次响应里重复的名字：不再插第二行（唯一约束会炸）。
            stats.foreshadows_duplicate_in_batch += 1
            stats.foreshadows += 1
            continue
        if fs is None:
            fs = KgForeshadow(project_id=project_id, name=name,
                              status=_status,
                              planted_chapter=f.get("planted_chapter"),
                              planned_payoff_chapter=_payoff)
            db.add(fs)
            stats.foreshadows_new += 1
            near = _nearest_existing_name(name, existing_names)
            if near is not None:
                stats.foreshadows_near_variant += 1
                logging.getLogger(__name__).warning(
                    "伏笔名与账上已有的近似（相似度 %.2f）：新名「%s」≈ 老名「%s」"
                    "（只留痕，不改账本）", near[1], name, near[0])
        elif name in seen_names:
            stats.foreshadows_duplicate_in_batch += 1
        else:
            stats.foreshadows_reused += 1
            # 状态机只前进不后退：paid_off 是终态倾向，planted→developing 可推进
            _ORDER = {"planted": 0, "developing": 1, "paid_off": 2, "expired": 3, "dropped": 4}
            if _ORDER.get(f.get("status", ""), 0) > _ORDER.get(fs.status, 0):
                fs.status = f["status"]
            # —— 复用行也只写**过关**的回收章（2026-10-29）——
            # 无证据的值**不覆盖**老值（老值可能合法）：宁可不更新，
            # 也不拿一个编出来的值把账上已有的计划改掉。
            if _payoff is not None and _payoff != fs.planned_payoff_chapter:
                fs.planned_payoff_chapter = _payoff
        seen_names.add(name)
        stats.foreshadows += 1

    # —— 6. mentions 反向索引（实体×段，context_window 原文片段）——
    for item in batch:
        for name, ent_id in name_to_id.items():
            if name in item.text:
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
                        context_window=_window_around(item.text, name),
                        mention_kind="direct",
                    ))

    # entity mention_count 更新
    from sqlalchemy import func

    for name, ent_id in name_to_id.items():
        cnt = db.scalar(
            select(func.count()).select_from(KgMention).where(
                KgMention.project_id == project_id, KgMention.entity_id == ent_id
            )
        )
        ent = db.get(KgEntity, ent_id)
        if ent is not None:
            ent.mention_count = cnt or 0

    db.commit()
    return stats


def _index_mentions_only(db: Session, batch: list[_QueueItem]) -> None:
    """无 provider 时的降级：只落 mention 索引（零 LLM 部分仍可用）。"""
    # 无实体可索引（抽取需要 LLM），直接跳过——mention 需要 entity 先存在
    return


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
    # 1) 精确匹配 canonical_name
    ent = db.scalar(
        select(KgEntity).where(
            KgEntity.project_id == project_id, KgEntity.canonical_name == name
        )
    )
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
