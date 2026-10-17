"""文风告警落库：把 _style_warning 返回的字典追加到 projects.style_narrative.style_warnings[]。

设计：docs/style-distillation-v2.1-design.md 第 2 节。
- 不新建表（projects.style_narrative 已有 JSON 列）
- append 而非 replace（段级历史）
- >500 条截断 + 提示（防 SQLite 字段膨胀）

实现要点（v2.1 修过的 bug）：
JSON 列在 SQLAlchemy ORM 上是 mutable，但同 session 多次写入时 ORM 缓存
不自动 invalidate，会导致第 2 次 append 读不到第 1 次写入 → 第 2 次覆盖。
修法：直接用 session.execute 走 raw SQL（绕开 ORM 缓存），不依赖 ORM 属性路径。
"""
from __future__ import annotations

import json
from typing import Any
from datetime import datetime, timezone
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session


_MAX_HISTORY = 500
_TRUNCATE_NOTICE_KEY = "_truncated_at"


def append_style_warning(
    db: Session, project: Any, segment_no: int, warn: dict
) -> None:
    """把 style_warning 落库（段级历史，>500 截断）。

    走 raw SQL 绕开 ORM JSON 列缓存（同 session 多次 append 不丢）。
    """
    # 1) SELECT 拿最新 narrative（raw SQL 返回 str，需 json.loads）
    row = db.execute(_sql_text(
        "SELECT style_narrative FROM projects WHERE id = :pid"
    ), {"pid": project.id}).fetchone()
    raw = row[0] if row and row[0] is not None else None
    if raw is None or raw == "":
        narrative = {}
    elif isinstance(raw, str):
        try:
            narrative = json.loads(raw)
        except (TypeError, ValueError):
            narrative = {}
    elif isinstance(raw, dict):
        narrative = raw
    else:
        narrative = {}

    history = narrative.get("style_warnings") or []
    if not isinstance(history, list):
        history = []

    history.append({
        "segment_no": int(segment_no),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "percentile": warn.get("percentile"),
        "mean_abs_z": warn.get("mean_abs_z"),
        "worst_features": [k for k, _ in (warn.get("worst") or [])[:5]],
        "warn_message": warn.get("message"),
        # 方案 B「对数粒度 profile」：落 bucket + scale_ratio + chars 给前端展示
        # 段 1 short 段 2 long 是粒度对齐改善的关键信号，前端可在 Style 审计 subtab
        # 显式标出"本段走 long bucket"——用户能直观看到 bucket 切分在跑。
        "bucket": warn.get("bucket"),
        "scale_ratio": warn.get("scale_ratio"),
        "chars": warn.get("chars"),
    })

    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
        narrative[_TRUNCATE_NOTICE_KEY] = datetime.now(timezone.utc).isoformat()

    narrative["style_warnings"] = history

    # 2) UPDATE 直接写
    db.execute(_sql_text(
        "UPDATE projects SET style_narrative = :n WHERE id = :pid"
    ), {"n": json.dumps(narrative, ensure_ascii=False), "pid": project.id})
    db.commit()


def get_style_warnings(project: Any) -> list[dict]:
    """读 style_warnings 数组（按 segment_no 倒序）。无 → []."""
    narrative = project.style_narrative or {}
    if not isinstance(narrative, dict):
        return []
    history = narrative.get("style_warnings") or []
    if not isinstance(history, list):
        return []
    return sorted(history, key=lambda e: e.get("segment_no", 0), reverse=True)


def is_truncated(project: Any) -> bool:
    """是否截断过（>500 条）。"""
    narrative = project.style_narrative or {}
    if not isinstance(narrative, dict):
        return False
    return _TRUNCATE_NOTICE_KEY in narrative
