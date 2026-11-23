# -*- coding: utf-8 -*-
"""素材调研后台 worker：单线程、DB 状态机、可恢复、与请求线程隔离。

任务流：
    queued → running（推断主题 + 逐查询抓取）→ consolidating（每 10 条整理一批）
           → done / partial / failed

为什么独立线程：
- 抓取素材要联网、整理要调模型，放在生成请求里会拖慢首字；本模块只消费队列，
  在 `SessionLocal()` 自己的会话里写库，绝不碰请求会话。
- 重启后 running 行会丢线程，`recover_running()` 把心跳过期的任务重新入队。
"""
from __future__ import annotations

import logging
import os
import secrets
import socket
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.materials import research, store
from app.materials.settings import (
    MaterialSettings,
    load_settings,
    target_raw,
    target_usable,
)
from app.models import MaterialResearchJob, Project, ProjectMaterial

logger = logging.getLogger("materials.worker")

WORKER_ID = f"{socket.gethostname()}#{os.getpid()}#{secrets.token_hex(3)}"
#: running 行超过这么久没心跳，视为进程遗留，可被重新入队。
STUCK_SECONDS = 120
#: 队列上限：超了丢最旧的 id（任务行仍是 queued，下次恢复会重捞）。
MAX_QUEUE = 100

_QUEUE: deque[int] = deque()
_QUEUE_LOCK = threading.Lock()
_STOP = threading.Event()
_THREADS: list[threading.Thread] = []


# --------------------------------------------------------------------------- #
# 入队 / 恢复 / 启停
# --------------------------------------------------------------------------- #

def _dedupe_key(project_id: int, chapter_no: int | None) -> str:
    return f"p{project_id}:c{chapter_no if chapter_no is not None else 0}"


def enqueue_research(
    db,
    project: Project,
    chapter_no: int | None,
    *,
    trigger: str = "manual",
    settings: MaterialSettings | None = None,
) -> MaterialResearchJob | None:
    """创建（或复用）一条调研任务并入队；失败返回 None，不抛出。"""
    cfg = settings or load_settings()
    if not cfg.enabled:
        return None
    key = _dedupe_key(int(project.id), chapter_no)
    try:
        active = db.scalars(
            select(MaterialResearchJob)
            .where(
                MaterialResearchJob.project_id == int(project.id),
                MaterialResearchJob.dedupe_key == key,
                MaterialResearchJob.status.in_(["queued", "running", "consolidating"]),
            )
            .order_by(MaterialResearchJob.id.desc())
        ).first()
        if active is not None:
            _push(active.id)
            ensure_worker()
            return active
        job = MaterialResearchJob(
            project_id=int(project.id),
            chapter_no=chapter_no,
            trigger=trigger,
            status="queued",
            target_raw=target_raw(project.target_length, cfg),
            target_usable=target_usable(project.target_length, cfg),
            dedupe_key=key,
            meta={"worker": WORKER_ID},
        )
        db.add(job)
        db.commit()
        _push(job.id)
        ensure_worker()
        return job
    except IntegrityError:
        db.rollback()
        return None
    except Exception as exc:  # noqa: BLE001 —— 入队失败不能影响生成
        db.rollback()
        logger.warning("素材调研入队失败（已忽略）：%s", exc)
        return None


def _push(job_id: int) -> None:
    with _QUEUE_LOCK:
        if job_id in _QUEUE:
            return
        _QUEUE.append(int(job_id))
        while len(_QUEUE) > MAX_QUEUE:
            _QUEUE.popleft()


def _pop(timeout: float = 1.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _QUEUE_LOCK:
            if _QUEUE:
                return _QUEUE.popleft()
        time.sleep(0.1)
    return None


def ensure_worker() -> None:
    """确保后台线程存在（幂等）。"""
    with _QUEUE_LOCK:
        alive = [t for t in _THREADS if t.is_alive()]
        _THREADS[:] = alive
        if alive:
            return
        _STOP.clear()
        thread = threading.Thread(target=_loop, name="material-research", daemon=True)
        _THREADS.append(thread)
        thread.start()


def stop_worker() -> None:
    _STOP.set()
    for thread in list(_THREADS):
        if thread.is_alive():
            thread.join(timeout=3.0)
    _THREADS.clear()


def recover_running(db) -> dict[str, int]:
    """启动恢复：queued 直接入队；心跳过期的 running/consolidating 重置为 queued。"""
    now = datetime.now(timezone.utc)
    requeued = 0
    stale = 0
    rows = db.scalars(
        select(MaterialResearchJob).where(
            MaterialResearchJob.status.in_(["queued", "running", "consolidating"])
        )
    ).all()
    for job in rows:
        if job.status == "queued":
            _push(job.id)
            requeued += 1
            continue
        heartbeat = job.heartbeat_at
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if heartbeat is not None and (now - heartbeat).total_seconds() < STUCK_SECONDS:
            continue
        job.status = "queued"
        job.owner = None
        job.error = "服务重启后重新排队"
        db.add(job)
        stale += 1
        _push(job.id)
    if stale:
        db.commit()
    ensure_worker()
    return {"requeued": requeued, "stale": stale}


def _loop() -> None:
    while not _STOP.is_set():
        job_id = _pop()
        if job_id is None:
            continue
        try:
            with SessionLocal() as db:
                if _run_job(db, job_id):
                    continue
        except Exception:  # noqa: BLE001 —— worker 永不退出
            logger.warning("素材调研任务 %s 异常退出", job_id, exc_info=True)


def _run_job(db, job_id: int) -> bool:
    # 原子认领：只有仍处于 queued 的行能被本进程领取，避免多进程重复跑。
    claimed = db.execute(
        update(MaterialResearchJob)
        .where(
            MaterialResearchJob.id == job_id,
            MaterialResearchJob.status == "queued",
        )
        .values(
            status="running",
            owner=WORKER_ID,
            started_at=datetime.now(timezone.utc),
            heartbeat_at=datetime.now(timezone.utc),
            attempts=MaterialResearchJob.attempts + 1,
        )
    )
    db.commit()
    if not claimed.rowcount:
        return False

    job = db.get(MaterialResearchJob, job_id)
    if job is None:
        return False
    project = db.get(Project, job.project_id)
    if project is None:
        _finish(db, job, "failed", "项目不存在")
        return True

    cfg = load_settings()
    try:
        _research(db, job, project, cfg)
        return True
    except Exception as exc:  # noqa: BLE001 —— 失败回队或标 failed
        logger.warning("素材调研任务 %s 失败：%s", job.id, exc)
        db.rollback()
        job = db.get(MaterialResearchJob, job_id)
        if job is None:
            return True
        if job.attempts <= cfg.retries:
            job.status = "queued"
            job.error = f"{type(exc).__name__}: {exc}"[:500]
            job.owner = None
            db.commit()
            time.sleep(1.0)
            _push(job.id)
        else:
            _finish(db, job, "failed", f"{type(exc).__name__}: {exc}"[:500])
        return True


#: 每次调研最多执行几条 reference（同题材网文）查询；不占知识素材目标。
REFERENCE_QUERY_LIMIT = 3


def _collect_results(db, job, project, runner, query: str) -> tuple[int, int, str]:
    """执行一次检索并入库；返回 (新增条数, 其中同类小说数, 状态)。

    状态：ok / error / cooldown。cooldown 表示免费源已被限流，应立即停止
    本轮抓取，先整理已入库素材，避免继续撞验证码。
    """
    results, error = runner.search(query)
    if error == "source_cooldown" or error.startswith("blocked:"):
        return 0, 0, "cooldown"
    if error:
        return 0, 0, "error"
    items = []
    fiction_count = 0
    for item in results:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        url = str(item.get("url") or "")
        if len(title) + len(snippet) < 8:
            continue
        # 同类小说页保留作参考：单独归到「同类小说参考」，方便大目录浏览。
        fiction = research.is_fiction_result(title, snippet, url)
        if fiction:
            fiction_count += 1
        published_at = str(item.get("published_at") or "").strip()
        year, year_text, _evidence = research.resolve_year(
            title, snippet, url, published_at
        )
        items.append(
            {
                "title": title,
                "snippet": snippet,
                "url": url,
                "published_year": year,
                "published_at": year_text,
                "provider": runner.provider_name,
                "query": query,
                "category": research.FICTION_CATEGORY if fiction else "",
                "tags": [research.FICTION_CATEGORY] if fiction else None,
            }
        )
    added = store.add_many(
        db, project.id, items, job_id=job.id, status="raw", origin="research"
    )
    return len(added), fiction_count, "ok"


def _research(db, job: MaterialResearchJob, project: Project, cfg: MaterialSettings) -> None:
    queries = list(job.queries or [])
    topics = list(job.topics or [])
    reference_queries: list[str] = []
    if not queries:
        plan = research.infer_topics(db, project, job.chapter_no or 1, settings=cfg)
        queries = list(plan["queries"])
        topics = list(plan["topics"])
        reference_queries = list(plan.get("reference_queries") or [])
        job.queries = queries
        job.topics = topics
        job.meta = {
            **(job.meta or {}),
            "plan_source": plan["source"],
            "theme": plan.get("theme") or "",
            "surface": plan.get("surface") or [],
            "needs": plan.get("needs") or [],
            "reference_queries": reference_queries,
        }
        db.commit()

    runner = research.SearchRunner(db, cfg)
    job.meta = {**(job.meta or {}), "provider": runner.provider_name}
    db.commit()

    ref_set = set(reference_queries)
    general_queries = [q for q in queries if q not in ref_set]

    zero_streak = 0
    fiction_kept = 0
    for query in general_queries:
        if _cancelled(db, job.id):
            _finish(db, job, "cancelled", "用户取消")
            return
        added, fiction_this, status = _collect_results(
            db, job, project, runner, query
        )
        fiction_kept += fiction_this
        if status == "cooldown":
            job.meta = {**(job.meta or {}), "cooldown": True}
            job.error = "检索源触发限流，已进入冷却"
            db.commit()
            break
        if status == "error":
            zero_streak += 1
        else:
            zero_streak = 0 if added else zero_streak + 1

        job.raw_count = store.count_raw(db, project.id)
        job.zero_streak = zero_streak
        job.heartbeat_at = datetime.now(timezone.utc)
        if fiction_kept:
            job.meta = {**(job.meta or {}), "fiction_kept": fiction_kept}
        db.commit()
        if job.raw_count >= job.target_raw or zero_streak >= cfg.zero_streak_limit:
            break

    # reference 查询独立执行：即使知识素材已达标，也抓几条同题材网文参考。
    for query in reference_queries[:REFERENCE_QUERY_LIMIT]:
        if _cancelled(db, job.id):
            break
        _added, fiction_this, status = _collect_results(
            db, job, project, runner, query
        )
        fiction_kept += fiction_this
        if status == "cooldown":
            job.meta = {**(job.meta or {}), "cooldown": True}
            break
        job.raw_count = store.count_raw(db, project.id)
        job.heartbeat_at = datetime.now(timezone.utc)
        if fiction_kept:
            job.meta = {**(job.meta or {}), "fiction_kept": fiction_kept}
        db.commit()

    # 免费搜索源的摘要通常不带日期：按 URL 抓一次页面补发布年份，再进入整理。
    _enrich_years_from_pages(db, job)

    # 整理阶段：每批 10 条，直到可用素材达标或没有 raw 可整理。
    job.status = "consolidating"
    db.commit()
    while not _cancelled(db, job.id):
        rows = list(
            db.scalars(
                select(ProjectMaterial)
                .where(
                    ProjectMaterial.project_id == project.id,
                    ProjectMaterial.status == "raw",
                )
                .order_by(ProjectMaterial.id.asc())
                .limit(cfg.batch_size)
            ).all()
        )
        if not rows:
            break
        research.consolidate(db, rows, settings=cfg)
        job.usable_count = store.count_usable(db, project.id)
        job.raw_count = store.count_raw(db, project.id)
        job.heartbeat_at = datetime.now(timezone.utc)
        db.commit()
        if job.usable_count >= job.target_usable:
            break

    # 同类小说参考不占可用目标，但必须进大目录：从 raw 直接标 usable。
    research.finalize_fiction_rows(db, project.id)
    job.usable_count = store.count_usable(db, project.id)
    job.raw_count = store.count_raw(db, project.id)
    db.commit()

    if _cancelled(db, job.id):
        _finish(db, job, "cancelled", "用户取消")
        return

    done = job.usable_count >= job.target_usable or job.raw_count >= job.target_raw
    if done and job.usable_count > 0:
        _finish(db, job, "done", "")
    elif job.usable_count > 0:
        reason = (
            "检索源冷却中，可用素材未达目标"
            if (job.meta or {}).get("cooldown")
            else "可用素材未达目标，检索源可能已耗尽"
        )
        _finish(db, job, "partial", reason)
    else:
        _finish(db, job, "failed", "未获取到可用素材")


#: 年份补全的并发抓页数：外站小流量，4 个足够把 180s 预算用满。
_YEAR_FETCH_WORKERS = 4
#: 每收到这么多条结果落一次库并发一次心跳（减少写锁占用）。
_YEAR_COMMIT_EVERY = 5


def _enrich_years_from_pages(db, job: MaterialResearchJob) -> None:
    """把本任务里年份未知的素材按 URL 抓页面补发布年份。

    只处理本任务新入库、年份为空、有真实 URL 的行；总时长有上限（默认 180s），
    超时即停。任何单页失败都跳过，不影响素材本身。

    抓页并发（2026-11-23）：串行时每页最多 5s，180s 预算常常只够 30 多页；
    改成 4 线程抓取（网络在线程里，写库仍回主线程，避免跨线程复用 Session），
    同一预算能覆盖全部素材，素材包「年份未知」的比例明显下降。
    """
    rows = list(
        db.scalars(
            select(ProjectMaterial)
            .where(
                ProjectMaterial.project_id == job.project_id,
                ProjectMaterial.job_id == job.id,
                ProjectMaterial.published_year.is_(None),
                ProjectMaterial.url.isnot(None),
            )
            .order_by(ProjectMaterial.id.asc())
            .limit(max(1, job.target_raw))
        ).all()
    )
    if not rows:
        return
    deadline = time.monotonic() + 180.0
    enriched = 0
    finished = 0
    cancelled = False

    with ThreadPoolExecutor(
        max_workers=_YEAR_FETCH_WORKERS, thread_name_prefix="year-enrich"
    ) as pool:
        iterator = iter(rows)
        in_flight: dict[Future, ProjectMaterial] = {}

        def _submit_next() -> None:
            try:
                nxt = next(iterator)
            except StopIteration:
                return
            in_flight[pool.submit(research.fetch_published_year, nxt.url or "")] = nxt

        for _ in range(_YEAR_FETCH_WORKERS):
            _submit_next()

        while in_flight:
            if time.monotonic() > deadline:
                break
            done, _ = wait(
                list(in_flight), timeout=0.5, return_when=FIRST_COMPLETED
            )
            for fut in done:
                row = in_flight.pop(fut, None)
                if row is None:
                    continue
                finished += 1
                try:
                    year, text = fut.result()
                except Exception:  # noqa: BLE001 —— 单页失败不影响素材
                    year, text = None, ""
                if year is not None:
                    row.published_year = year
                    row.published_at = (text or str(year))[:64]
                    enriched += 1
                if finished % _YEAR_COMMIT_EVERY == 0:
                    job.heartbeat_at = datetime.now(timezone.utc)
                    db.commit()
                    # 只在提交后判取消：`_cancelled` 会 expire_all，
                    # 未提交的年份会因此丢失。
                    if _cancelled(db, job.id):
                        cancelled = True
                        break
                if not cancelled:
                    _submit_next()
            if cancelled:
                break
    if enriched:
        job.meta = {**(job.meta or {}), "year_enriched": enriched}
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


def _cancelled(db, job_id: int) -> bool:
    db.expire_all()
    job = db.get(MaterialResearchJob, job_id)
    return job is None or job.status == "cancelled"


def _finish(db, job: MaterialResearchJob, status: str, error: str) -> None:
    job.status = status
    job.error = error or None
    job.finished_at = datetime.now(timezone.utc)
    job.heartbeat_at = datetime.now(timezone.utc)
    db.add(job)
    db.commit()
