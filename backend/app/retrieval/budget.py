"""预算池的**软着陆执行**（设计稿 §5.12.2）。

之前四类预算池（catalog / rule / anchor / writing）只写进轨迹，没有任何读回
执行；结果是「接近上限」和「远未到上限」行为完全一样，直到某一方硬性掐断。
本模块把预算做成**两段式**：

- `normal`：正常返回；
- `narrow`：达到 soft 上限后**收窄返回**（k 变小、browse 只看一层、
  anchor 窗口变窄），仍然给结果，不中断；
- `exhausted`：达到 hard 上限后只给最小结果（1 条命中 / 3 个子节点 /
  锚点附近最小窗口），并在返回里明确写「池已用尽」。**不是报错，也不是空手**。

`writing` 池目前没有工具入口，保留配置给生成侧接线后用。

计数口径：同 `session_id` + 同 `budget_pool` 的 `retrieval_logs` 行数。
一次调用一行（含 error / empty），所以「调用数 = 消耗」；重试会再耗一格，
这是刻意的——重试同样占预算。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RetrievalLog

PHASE_NORMAL = "normal"
PHASE_NARROW = "narrow"
PHASE_EXHAUSTED = "exhausted"

#: soft 之后开始收窄，hard 之后只给最小结果。数值按项目 6 实测校准：
#: 段 9 的 32 次调用里 catalog 20 / rule 11，正常会话 7–8 次；取 12/18 能
#: 在长会话前半段就提醒方向错误，又不至于刚查几条就被收窄。
POOL_SOFT: dict[str, int] = {
    "catalog": 12,
    "rule": 6,
    "anchor": 6,
    "writing": 8,
}
POOL_HARD: dict[str, int] = {
    "catalog": 18,
    "rule": 9,
    "anchor": 9,
    "writing": 12,
}

#: 收窄后的返回上限。
NARROW_K = 2
NARROW_CHILDREN = 6
EXHAUSTED_K = 1
EXHAUSTED_CHILDREN = 3
NARROW_ANCHOR_WIDTH = 200
EXHAUSTED_ANCHOR_WIDTH = 80

#: 单条规则的扩窗次数上限。超过后不再加宽，只给锚点附近最小窗口。
ANCHOR_MAX_EXPANSIONS = 6


def pool_usage(db: Session, session_id: str, pool: str) -> int:
    """本会话该池已消耗的调用数。没有 session_id 时按 0（不阻塞直调）。"""
    if not session_id or not pool:
        return 0
    return int(db.execute(
        select(func.count(RetrievalLog.id)).where(
            RetrievalLog.session_id == session_id,
            RetrievalLog.budget_pool == pool,
        )
    ).scalar() or 0)


def budget_state(db: Session, session_id: str, pool: str | None) -> dict[str, Any]:
    """返回 `{pool, used, soft, hard, remaining, phase}`。

    未登记的池（None / 未知）按 `normal` 处理：不因为新工具忘记登记就被挡。
    """
    soft = POOL_SOFT.get(pool or "")
    if soft is None:
        return {
            "pool": pool, "used": 0, "soft": None, "hard": None,
            "remaining": None, "phase": PHASE_NORMAL,
        }
    hard = POOL_HARD.get(pool or "", soft * 3 // 2)
    used = pool_usage(db, session_id, pool or "")
    phase = PHASE_NORMAL
    if used >= hard:
        phase = PHASE_EXHAUSTED
    elif used >= soft:
        phase = PHASE_NARROW
    return {
        "pool": pool,
        "used": used,
        "soft": soft,
        "hard": hard,
        "remaining": max(0, hard - used),
        "phase": phase,
    }


def narrowed_k(k: int, state: dict[str, Any]) -> int:
    if state.get("phase") == PHASE_EXHAUSTED:
        return min(k, EXHAUSTED_K)
    if state.get("phase") == PHASE_NARROW:
        return min(k, NARROW_K)
    return k


def narrowed_children(n: int, state: dict[str, Any]) -> int:
    if state.get("phase") == PHASE_EXHAUSTED:
        return min(n, EXHAUSTED_CHILDREN)
    if state.get("phase") == PHASE_NARROW:
        return min(n, NARROW_CHILDREN)
    return n


def narrowed_width(width: int, state: dict[str, Any]) -> int:
    if state.get("phase") == PHASE_EXHAUSTED:
        return min(width, EXHAUSTED_ANCHOR_WIDTH)
    if state.get("phase") == PHASE_NARROW:
        return min(width, NARROW_ANCHOR_WIDTH)
    return width


def width_for_expansion(width: int, expansions: int) -> int:
    """扩窗次数越多，单次窗口越小：把「无限加宽」变成边际递减。"""
    if expansions <= 0:
        return width
    return max(60, int(width / (1 + 0.6 * expansions)))


def note_for(state: dict[str, Any]) -> str | None:
    if state.get("phase") == PHASE_EXHAUSTED:
        return (
            f"{state.get('pool')} 池已用尽（{state.get('used')}/{state.get('hard')}）；"
            "本次只给最小结果，请基于已有规则收尾，不要继续检索"
        )
    if state.get("phase") == PHASE_NARROW:
        return (
            f"{state.get('pool')} 池接近上限（{state.get('used')}/{state.get('soft')}）；"
            "本次已收窄返回，请优先读已经命中的规则"
        )
    return None
