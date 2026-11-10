"""助理对话记录的落库 / 读取（`assistant_messages` 表的唯一入口）。

## 为什么单独一个模块

写入方分散在三处：引导路由（`routers/assistant_wizard.py`）、普通问答与
角色对话（`routers/assistant.py`）、后台任务的通知（`style/jobs.py` 的
`emit_notice`）。如果各写各的，字段口径（role/kind 的取值、payload 形状）
很快就会漂。这里把「写一条消息」收敛成一个函数，三处都调它。

## 失败策略

记录是**增强**，不是主链路：写失败只记日志、返回 None，绝不让对话本身报错
（与 `emit_notice` 同口径）。用户宁可少一条历史，也不该因为历史表写不进去
而发不出消息。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssistantMessage

logger = logging.getLogger(__name__)


def record_message(
    db: Session,
    project_id: int,
    *,
    role: str,
    kind: str = "plain",
    text: str = "",
    payload: dict[str, Any] | None = None,
    session_id: int | None = None,
    dedupe_key: str | None = None,
) -> AssistantMessage | None:
    """写一条对话消息（幂等：`dedupe_key` 命中已有行时直接返回它）。"""
    try:
        if dedupe_key:
            existing = db.scalar(
                select(AssistantMessage).where(
                    AssistantMessage.project_id == project_id,
                    AssistantMessage.dedupe_key == dedupe_key,
                )
            )
            if existing is not None:
                return existing
        row = AssistantMessage(
            project_id=project_id,
            session_id=session_id,
            role=role,
            kind=kind,
            text=text or "",
            payload=payload,
            dedupe_key=dedupe_key,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:  # noqa: BLE001 —— 历史写失败不该让对话/任务失败
        logger.exception(
            "助理消息写入失败 project_id=%s role=%s kind=%s", project_id, role, kind
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def list_messages(
    db: Session, project_id: int, limit: int = 200
) -> list[AssistantMessage]:
    """按时间正序取该项目最近的对话记录（取尾部再翻转，避免一次拉全表）。"""
    rows = (
        db.scalars(
            select(AssistantMessage)
            .where(AssistantMessage.project_id == project_id)
            .order_by(AssistantMessage.id.desc())
            .limit(max(1, min(int(limit), 500)))
        )
        .all()
    )
    return list(reversed(rows))
