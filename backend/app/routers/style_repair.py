"""定向返修计划接口（只读，不调模型）。

前端在「已生成段落」里点「定向返修」时先取这份计划：
- 命中了哪些指标偏离、哪些反向禁忌；
- 每条怎么修；
- 可直接交给 `POST /api/assistant/invoke-generation` 的返修指令。

真正执行仍走既有重写链路（快照 / 回滚 / AgentRun 审计都在那里），
本接口只负责把「文风偏离」翻译成人能看懂、模型能执行的几条要求。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Generation, Project
from app.style.repair import build_repair_plan

router = APIRouter(prefix="/api/projects", tags=["style-repair"])


@router.get("/{project_id}/segments/{segment_no}/repair-plan")
def get_repair_plan(
    project_id: int,
    segment_no: int,
    db: Session = Depends(get_db),
) -> dict:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")

    generation = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id,
            Generation.segment_no == segment_no,
        )
    ).first()
    if generation is None:
        raise HTTPException(status_code=404, detail=f"段 {segment_no} 不存在")

    profile = project.style_profile if isinstance(project.style_profile, dict) else None
    narrative = project.style_narrative if isinstance(project.style_narrative, dict) else None
    plan = build_repair_plan(generation.text or "", profile, narrative)
    return {
        "project_id": project_id,
        "segment_no": segment_no,
        "generation_id": generation.id,
        **plan,
    }
