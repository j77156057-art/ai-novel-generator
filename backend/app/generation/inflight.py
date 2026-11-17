"""单项目生成闸：同一项目不允许并发跑两次生成（B4）。

为什么需要：段模式没有章号可去重，双击「生成」会各起一次模型调用，
两次都落库，段序直接多一段；流式入口也一样（SSE 在后台线程跑，请求本身
早已返回 200）。

实现是**进程内**的项目级租约，不落库：
- `try_begin(project_id)` 原子占位，已被占则返回 False（路由回 409）；
- `end(project_id)` 释放（正常结束、异常、SSE 生成器 finally 都会调）；
- 租约带 TTL（默认 15 分钟）：守护重启/进程被杀会留下残留租约，
  超过 TTL 自动视为过期，避免项目被永久锁死。

单进程 uvicorn 是部署形态（`scripts/backend-supervisor.ps1` 一次只拉一个），
所以进程内锁足够；将来多 worker 需要换成 DB 租约。
"""
from __future__ import annotations

import threading
import time

#: 租约 TTL（秒）：超过视为残留，可被新的生成接管。
LEASE_SECONDS = 15 * 60

_guard = threading.Lock()
_inflight: dict[int, float] = {}


def try_begin(project_id: int, *, now: float | None = None) -> bool:
    """尝试占用项目；成功 True，已有未过期租约 False。"""
    ts = time.monotonic() if now is None else now
    with _guard:
        started = _inflight.get(project_id)
        if started is not None and ts - started < LEASE_SECONDS:
            return False
        _inflight[project_id] = ts
        return True


def end(project_id: int) -> None:
    """释放项目租约（幂等）。"""
    with _guard:
        _inflight.pop(project_id, None)


def is_in_flight(project_id: int, *, now: float | None = None) -> bool:
    """项目当前是否有未过期租约（只读判断）。"""
    ts = time.monotonic() if now is None else now
    with _guard:
        started = _inflight.get(project_id)
        return started is not None and ts - started < LEASE_SECONDS


def reset() -> None:
    """清空全部租约（仅供测试）。"""
    with _guard:
        _inflight.clear()
