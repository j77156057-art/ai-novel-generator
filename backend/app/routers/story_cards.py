"""设定卡 CRUD：/api/projects/{project_id}/story-cards

卡片是作者意图，写入即生效——不做审批、不做 diff。三条硬规则：

1. **未知字段键直接拒绝**（422），不静默丢弃：静默无效比报错更难排查；
2. **删除是真删**（不软删）；想临时停用请把 `enabled` 置 false；
3. **GET 同时下发字段 schema**，前端据此渲染表单，避免两边各写一份字段表。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.story_cards import (
    KINDS,
    field_schema_payload,
    normalize_card_input,
    sync_from_project,
)
from app.models import Project, StoryCard
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["story-cards"])


class StoryCardCreate(StrictRequest):
    kind: str
    title: str
    fields: dict[str, str] | None = None
    enabled: bool = True
    sort_order: int | None = None


class StoryCardPatch(StrictRequest):
    """部分更新：只改传了的字段；`fields` 传 `{}` 表示清空全部字段。"""

    kind: str | None = None
    title: str | None = None
    fields: dict[str, str] | None = None
    enabled: bool | None = None
    sort_order: int | None = None


def _project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


def _card_or_404(db: Session, project_id: int, card_id: int) -> StoryCard:
    card = db.get(StoryCard, card_id)
    if card is None or card.project_id != project_id:
        raise HTTPException(status_code=404, detail="设定卡不存在")
    return card


def _serialize(card: StoryCard) -> dict:
    return {
        "id": card.id,
        "project_id": card.project_id,
        "kind": card.kind,
        "title": card.title,
        "fields": card.fields or {},
        "enabled": bool(card.enabled),
        "sort_order": card.sort_order,
        "created_at": card.created_at.isoformat() if card.created_at else None,
        "updated_at": card.updated_at.isoformat() if card.updated_at else None,
    }


@router.get("/{project_id}/story-cards")
def list_story_cards(
    project_id: int, db: Session = Depends(get_db)
) -> dict:
    """返回字段 schema 与本项目全部卡片（含停用卡，前端需要展示开关）。"""
    project = _project_or_404(db, project_id)
    cards = (
        db.query(StoryCard)
        .filter(StoryCard.project_id == project_id)
        .all()
    )
    cards.sort(
        key=lambda c: (
            KINDS.index(c.kind) if c.kind in KINDS else len(KINDS),
            c.sort_order or 0,
            c.id,
        )
    )
    return {
        "project_id": project_id,
        "kinds": field_schema_payload(),
        "cards": [_serialize(card) for card in cards],
        # 前端据此决定要不要自动同步一次已有设定；已同步过就不自动导入，
        # 否则用户删空卡片后每次加载都会被填回来。
        "seeded": project.story_cards_seeded_at is not None,
    }


@router.post("/{project_id}/story-cards/sync")
def sync_story_cards(
    project_id: int, db: Session = Depends(get_db)
) -> dict:
    """把剧情圣经与知识图谱里已有的实体补成卡片；只新增、不覆盖。"""
    project = _project_or_404(db, project_id)
    result = sync_from_project(db, project)
    db.commit()
    db.refresh(project)

    cards = (
        db.query(StoryCard)
        .filter(StoryCard.project_id == project_id)
        .all()
    )
    cards.sort(
        key=lambda c: (
            KINDS.index(c.kind) if c.kind in KINDS else len(KINDS),
            c.sort_order or 0,
            c.id,
        )
    )
    return {
        "project_id": project_id,
        "created": result["created"],
        "skipped": result["skipped"],
        "filled": result.get("filled", 0),
        "seeded": True,
        "cards": [_serialize(card) for card in cards],
    }


@router.post("/{project_id}/story-cards")
def create_story_card(
    project_id: int, body: StoryCardCreate, db: Session = Depends(get_db)
) -> dict:
    project = _project_or_404(db, project_id)
    try:
        kind, title, fields = normalize_card_input(body.kind, body.title, body.fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sort_order = body.sort_order
    if sort_order is None:
        last = (
            db.query(StoryCard)
            .filter(
                StoryCard.project_id == project_id,
                StoryCard.kind == kind,
            )
            .order_by(StoryCard.sort_order.desc())
            .first()
        )
        sort_order = (last.sort_order + 1) if last else 0

    card = StoryCard(
        project_id=project_id,
        kind=kind,
        title=title,
        fields=fields,
        enabled=bool(body.enabled),
        sort_order=sort_order,
    )
    # 用户已经手动建过卡：视为「不需要自动导入」。否则他删空卡片再刷新，
    # 前端会把它当成「从未同步」又灌回来。
    if project.story_cards_seeded_at is None:
        project.story_cards_seeded_at = datetime.now(timezone.utc)
    db.add(card)
    db.commit()
    db.refresh(card)
    return _serialize(card)


@router.patch("/{project_id}/story-cards/{card_id}")
def update_story_card(
    project_id: int,
    card_id: int,
    body: StoryCardPatch,
    db: Session = Depends(get_db),
) -> dict:
    _project_or_404(db, project_id)
    card = _card_or_404(db, project_id, card_id)

    target_kind = body.kind if body.kind is not None else card.kind
    target_title = body.title if body.title is not None else card.title
    target_fields = body.fields if body.fields is not None else (card.fields or {})
    try:
        kind, title, fields = normalize_card_input(
            target_kind, target_title, target_fields
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    card.kind = kind
    card.title = title
    card.fields = fields
    if body.enabled is not None:
        card.enabled = bool(body.enabled)
    if body.sort_order is not None:
        card.sort_order = body.sort_order
    db.commit()
    db.refresh(card)
    return _serialize(card)


@router.delete("/{project_id}/story-cards/{card_id}")
def delete_story_card(
    project_id: int, card_id: int, db: Session = Depends(get_db)
) -> dict:
    _project_or_404(db, project_id)
    card = _card_or_404(db, project_id, card_id)
    deleted_id = card.id
    db.delete(card)
    db.commit()
    return {"ok": True, "deleted_id": deleted_id}
