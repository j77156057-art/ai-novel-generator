"""**动作提案**的审批端点（agent 提「做什么事」，人批了才执行）。

## 为什么审批与执行分成两个端点

和文本提案（`assistant_proposals.py`）同一条纪律：**审批是人的判断，
落库是系统的动作**。合成一个「一键生效」就取消了「先看清楚要做什么
再决定」这一步 —— 而这正是这条流程存在的理由。

## 状态与失败语义

```text
pending ──approve──> approved ──apply──> applied
   │                     │
   └──reject──> rejected └──前提不符──> stale（409，前提变了要重新提）
```

「前提不符」也要**留痕**：stale 状态单独落库，不随 apply 的失败一起回滚。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import action_proposals as action_lib
from app.db import get_db
from app.models import ActionProposal
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/assistant/action-proposals", tags=["assistant"])


class ActionProposalActionRequest(StrictRequest):
    """approve / reject / apply 的请求体。

    `action` 必填且必须与路径动作一致 —— 少写了 action 就该 422，
    而不是让服务端替调用方猜（同一个请求体不能被复用到另一个动作上）。
    """

    action: Literal["approve", "reject", "apply"]
    actor: str | None = None
    note: str | None = None


class ActionProposalCreateRequest(StrictRequest):
    """创建一条待批动作提案的请求体（卡片确认后前端原样回传）。

    字段与服务端拼好的卡片 `body` 完全一致（`pending_actions.build_action_proposal_action`），
    前端不参与构造。
    """

    project_id: int
    kind: str
    title: str
    detail: dict[str, Any] = {}
    action_endpoint: str
    action_method: str = "POST"
    action_body: dict[str, Any] = {}
    params: dict[str, Any] = {}
    risk_level: str = "low"


def _raise_http(exc: Exception) -> None:
    """动作提案域异常 → HTTP（唯一一处集中映射）。"""
    code = getattr(exc, 'code', 'action_proposal_failed')
    message = getattr(exc, 'message', str(exc))
    if code == 'invalid_transition':
        raise HTTPException(status_code=409, detail=message) from exc
    if code == 'stale_precondition':
        # 前提变了：409 + 结构化 detail，界面要能说清「过期了，重新提一条」。
        raise HTTPException(status_code=409, detail={
            'error': 'stale_precondition',
            'message': message,
        }) from exc
    if code in ('unknown_action_kind', 'unknown_status', 'unknown_risk_level'):
        raise HTTPException(status_code=422, detail=message) from exc
    raise HTTPException(status_code=400, detail=message) from exc


def _get(db: Session, proposal_id: int) -> ActionProposal:
    row = db.get(ActionProposal, proposal_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f'动作提案 {proposal_id} 不存在')
    return row


@router.get("")
def list_action_proposals(
    project_id: int,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """按项目列出动作提案；`status` 可选过滤（非法值 → 422）。"""
    if status is not None and status not in action_lib.STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f'status 必须是 {list(action_lib.STATUSES)} 之一')
    q = select(ActionProposal).where(ActionProposal.project_id == project_id)
    if status is not None:
        q = q.where(ActionProposal.status == status)
    rows = list(db.scalars(q.order_by(ActionProposal.id.desc()).limit(limit)).all())
    return {
        'project_id': project_id,
        'status': status,
        'count': len(rows),
        'proposals': [action_lib.action_proposal_to_dict(r) for r in rows],
    }


@router.post("")
def create_action_proposal(
    body: ActionProposalCreateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """创建一条**待批**动作提案（卡片确认后前端打这个端点）。

    只落提案行，**不执行**那个动作 —— 执行是人再点一次「批准 / 执行」之后的事。
    """
    from app.models import Project
    project = db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail='项目不存在')
    try:
        row = action_lib.build_action_proposal(
            db, project,
            kind=body.kind,
            title=body.title,
            detail=body.detail,
            endpoint=body.action_endpoint,
            method=body.action_method,
            body=body.action_body,
            params=body.params,
            risk_level=body.risk_level,
        )
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    return {
        'proposal': action_lib.action_proposal_to_dict(row),
        'next_action': 'approve',
        'note': '提案已创建，还没执行 —— 批准并执行后才会真的发生。',
    }


@router.post("/{proposal_id}/approve")
def approve_action_proposal(
    proposal_id: int,
    body: ActionProposalActionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """pending → approved。**只改状态，不执行动作。**"""
    if body.action != 'approve':
        raise HTTPException(status_code=422,
                            detail=f"action 必须是 'approve'，收到 {body.action!r}")
    row = _get(db, proposal_id)
    try:
        action_lib.validate_action(row.status, 'approve')
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    from datetime import datetime, timezone
    row.status = 'approved'
    row.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return {
        'proposal': action_lib.action_proposal_to_dict(row),
        'next_action': 'apply',
        'note': '已批准；动作还没执行 —— 再点一次「执行」才会真的发生。',
    }


@router.post("/{proposal_id}/reject")
def reject_action_proposal(
    proposal_id: int,
    body: ActionProposalActionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """pending → rejected（终态）。永不执行。"""
    if body.action != 'reject':
        raise HTTPException(status_code=422,
                            detail=f"action 必须是 'reject'，收到 {body.action!r}")
    row = _get(db, proposal_id)
    try:
        action_lib.validate_action(row.status, 'reject')
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    from datetime import datetime, timezone
    row.status = 'rejected'
    row.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return {
        'proposal': action_lib.action_proposal_to_dict(row),
        'note': '已拒绝；这条提案是终态，不会再执行。',
    }


@router.post("/{proposal_id}/apply")
def apply_action_proposal(
    proposal_id: int,
    body: ActionProposalActionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """approved → applied：**真正执行**这个动作（唯一会动业务表的一步）。

    执行前先验前提：前提变了 → 置 `stale` + 409（不静默重算、不覆盖）。
    """
    if body.action != 'apply':
        raise HTTPException(status_code=422,
                            detail=f"action 必须是 'apply'，收到 {body.action!r}")
    row = _get(db, proposal_id)
    try:
        result: dict[str, Any] = action_lib.apply_action_proposal(
            db, row, actor=body.actor, background_tasks=background_tasks)
    except Exception as exc:  # noqa: BLE001
        _raise_http(exc)
    return {
        'proposal': action_lib.action_proposal_to_dict(row),
        'result': result,
        'note': result.get('note') if isinstance(result, dict) else None,
    }
