"""KG v3.0 读侧 API：检索切片 / 关系演变 / 人物情报 / 节奏 / 校验 / 作者豁免。

路由组挂在 /api/projects/{project_id}/kg-v3 下，全部只读（豁免是唯一写入）。
数据源是 v3.0 的 9 张表——由 batch_worker 异步批抽落库（挂生成主链后自动有数据）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import KgEvent, KgEventLink, Project

router = APIRouter(prefix="/api/projects/{project_id}/kg-v3", tags=["kg-v3"])


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"项目 {project_id} 不存在")
    return proj


@router.get("/slice")
def get_slice(
    project_id: int,
    segment_no: int = Query(..., description="当前段号（按此动态取窗口）"),
    window: int = Query(5, ge=1, le=50),
    max_entities: int = Query(20, ge=1, le=100),
    characters: str | None = Query(None, description="逗号分隔的角色名，缺省用本段 mention"),
    db: Session = Depends(get_db),
) -> dict:
    """按当前剧情点取 KG 切片（generate 注入 prompt 的同款数据）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import slice_for_prompt

    chars = [c.strip() for c in characters.split(",")] if characters else None
    return slice_for_prompt(db, project_id, segment_no,
                            window=window, max_entities=max_entities,
                            active_characters=chars)


@router.get("/slice/prompt")
def get_slice_prompt(
    project_id: int,
    segment_no: int = Query(...),
    window: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    """渲染成 prompt 文本（调试用：看 generate 实际注入什么）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import slice_for_prompt, format_slice_for_prompt

    sl = slice_for_prompt(db, project_id, segment_no, window=window)
    return {"text": format_slice_for_prompt(sl)}


@router.get("/relations/curve")
def get_relation_curve(
    project_id: int,
    from_name: str = Query(..., alias="from"),
    to_name: str = Query(..., alias="to"),
    db: Session = Depends(get_db),
) -> dict:
    """关系演变曲线（strength 历史）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import relation_curve

    return {"from": from_name, "to": to_name, "curve": relation_curve(db, project_id, from_name, to_name)}


@router.get("/knowledge")
def can_character_know(
    project_id: int,
    character: str = Query(...),
    fact: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    """人物情报追踪：character 此刻是否知道 fact。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import can_character_know as _check

    return {"character": character, "fact": fact, "knows": _check(db, project_id, character, fact)}


@router.get("/rhythm")
def get_rhythm(
    project_id: int,
    window: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    """节奏分析：最近 N 段每实体的 mention 分布（谁在唱主角）。"""
    _require_project(db, project_id)
    from app.kg.retrieval_v3 import rhythm_analysis

    return rhythm_analysis(db, project_id, window=window)


@router.get("/events")
def get_events(
    project_id: int,
    limit: int = Query(50, ge=1, le=500),
    event_type: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    """事件列表（含因果链），按 sequence 降序。"""
    _require_project(db, project_id)
    from app.models import KgEventLink

    q = select(KgEvent).where(KgEvent.project_id == project_id)
    if event_type:
        q = q.where(KgEvent.event_type == event_type)
    events = db.scalars(q.order_by(KgEvent.sequence.desc()).limit(limit)).all()
    links = db.scalars(
        select(KgEventLink).where(KgEventLink.project_id == project_id)
    ).all() if events else []
    seq_to_id = {e.sequence: e.id for e in events}
    return {
        "events": [
            {
                "sequence": e.sequence, "chapter": e.chapter, "segment_no": e.segment_no,
                "event_type": e.event_type, "summary": e.summary,
                "in_story_date": e.in_story_date,
            }
            for e in sorted(events, key=lambda x: x.sequence)
        ],
        "links": [
            {"from_event_id": lk.from_event_id, "to_event_id": lk.to_event_id, "link_type": lk.link_type}
            for lk in links
        ],
    }


class ExemptRequest(BaseModel):
    """作者豁免请求体。"""

    finding_id: int | None = None
    rule: str
    entity: str = ""
    note: str


@router.post("/exemptions")
def mark_intentional(
    project_id: int,
    body: ExemptRequest,
    db: Session = Depends(get_db),
) -> dict:
    """作者豁免（Novelium mark as intentional）：用户认定某告警是故意的，Judge 以后不再报。

    豁免按 (rule, entity) 记录——同角色同规则不再告警。note 留痕。
    """
    _require_project(db, project_id)
    from app.models import Fact

    key = f"exempt:{body.rule}:{body.entity}"
    existing = db.scalar(select(Fact).where(Fact.project_id == project_id, Fact.key == key))
    if existing is None:
        db.add(Fact(
            project_id=project_id, type="exemption", key=key,
            props={"rule": body.rule, "entity": body.entity, "note": body.note,
                   "finding_id": body.finding_id},
            source="author", confidence="high",
        ))
        db.commit()
    return {"exempted": True, "rule": body.rule, "entity": body.entity}


@router.get("/exemptions")
def list_exemptions(project_id: int, db: Session = Depends(get_db)) -> dict:
    from app.models import Fact

    rows = db.scalars(select(Fact).where(
        Fact.project_id == project_id, Fact.type == "exemption"
    )).all()
    return {"exemptions": [
        {"rule": r.props.get("rule"), "entity": r.props.get("entity"),
         "note": r.props.get("note")} for r in rows
    ]}
