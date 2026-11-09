# -*- coding: utf-8 -*-
"""KG 生成层影子接入（默认关闭）。

每段 commit 后，把同一段正文异步喂给零 LLM 的机械抽取，落在**独立 sqlite**
（默认 ``backend/data/kg_shadow/gen_<project_id>.db``）。

边界（与 v3 批抽 / v2.6 合并互不影响）：
- 只写不读：本模块写出的库不参与 prompt 注入，也没有任何生产代码读它；
- 不调 LLM、不写主库、不写原著层；
- 默认关闭（``KG_SHADOW_ENABLED=1`` 才启用）；
- 任何异常都在线程内吞掉并记日志，绝不影响生成主链。

环境变量：
- ``KG_SHADOW_ENABLED``  : 1/true/yes 才启用，默认关闭。
- ``KG_SHADOW_DIR``      : 落库目录，默认 ``backend/data/kg_shadow``。
- ``KG_SHADOW_CANON_DB`` : 原著层库路径；留空则按「无 canon」跑（只抽不判冲突）。
"""
from __future__ import annotations

import logging
import os
import pathlib
import threading

from app.kg import generated_store as store
from app.kg.layer_pipeline import ingest_chapter

logger = logging.getLogger(__name__)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_lock = threading.Lock()
_threads: list[threading.Thread] = []
_inflight: set[tuple[int, int]] = set()


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    """影子抽取是否启用（默认关闭）。"""
    return _env_flag("KG_SHADOW_ENABLED")


def shadow_dir() -> pathlib.Path:
    raw = os.getenv("KG_SHADOW_DIR", "").strip()
    return pathlib.Path(raw) if raw else REPO_ROOT / "backend" / "data" / "kg_shadow"


def db_path(project_id: int) -> pathlib.Path:
    """影子库路径：按项目分库，与主库、原著库都不同文件。"""
    return shadow_dir() / f"gen_{int(project_id)}.db"


def canon_db() -> str | None:
    raw = os.getenv("KG_SHADOW_CANON_DB", "").strip()
    if not raw:
        return None
    path = pathlib.Path(raw)
    return str(path) if path.exists() else None


def run_now(project_id: int, text: str, chapter: int,
            *, canon: str | None = None) -> dict:
    """同步跑一次影子抽取（测试/离线对账用）。异常不外抛，返回 ok=False。"""
    path = db_path(project_id)
    try:
        con = store.connect(path)
        try:
            store.ensure_schema(con)
            result = ingest_chapter(con, canon if canon is not None else canon_db(),
                                    text, chapter)
        finally:
            con.close()
        return {"ok": True, "db": str(path), **result}
    except Exception as exc:  # noqa: BLE001 —— 影子抽取失败绝不外抛
        logger.warning("kg shadow 抽取失败（project=%s chapter=%s）: %s",
                       project_id, chapter, exc)
        return {"ok": False, "db": str(path), "error": str(exc)[:200]}


def _worker(project_id: int, text: str, chapter: int, canon: str | None) -> None:
    try:
        run_now(project_id, text, chapter, canon=canon)
    finally:
        with _lock:
            _inflight.discard((project_id, chapter))


def submit(project_id: int, text: str, chapter: int,
           *, canon: str | None = None) -> dict:
    """异步提交一段正文到影子库。未启用/同章在跑时直接跳过，不阻塞调用方。"""
    if not enabled():
        return {"queued": False, "reason": "disabled"}
    if not text or chapter is None:
        return {"queued": False, "reason": "empty"}

    key = (int(project_id), int(chapter))
    with _lock:
        if key in _inflight:
            return {"queued": False, "reason": "inflight"}
        _inflight.add(key)
        thread = threading.Thread(
            target=_worker, args=(project_id, text, chapter, canon),
            daemon=True, name=f"kg-shadow-{project_id}-{chapter}",
        )
        _threads.append(thread)
        thread.start()
    return {"queued": True, "db": str(db_path(project_id))}


def wait_idle(timeout: float = 10.0) -> None:
    """等待当前所有影子线程结束（测试/收尾用）。"""
    with _lock:
        threads = list(_threads)
    for thread in threads:
        thread.join(timeout)
    with _lock:
        _threads[:] = [t for t in _threads if t.is_alive()]
