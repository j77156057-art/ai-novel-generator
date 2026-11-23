# -*- coding: utf-8 -*-
"""写作辅助端点：章前开头建议 + 写作进度（只读，不写业务表）。

- `POST /api/projects/{id}/chapter-opening-options`
- `GET  /api/projects/{id}/writing-progress`

两者都服务于助手对话：生成前先问「这一章怎么开头」，右侧栏显示当前章 /
大纲 / 时间线节点进度。章前建议允许调一次 Assistant 模型，失败退回规则模板；
写作进度纯本地聚合。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.opening_options import build_opening_options, next_chapter
from app.generation.writing_progress import build_writing_progress
from app.models import Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["writing-assist"])


class ChapterOpeningBody(StrictRequest):
    """章前建议请求。`chapter` 缺省 = 下一章；`use_model=False` 只走规则。"""

    chapter: int | None = None
    use_model: bool = True


def _require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.post("/{project_id}/chapter-opening-options")
def chapter_opening_options(
    project_id: int,
    body: ChapterOpeningBody | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """给出「这一章怎么开头」的 2–3 条建议（模型优先，规则兜底）。"""
    body = body or ChapterOpeningBody()
    project = _require_project(db, project_id)
    chapter = body.chapter or next_chapter(db, project)
    if chapter <= 0:
        chapter = 1
    return build_opening_options(
        db, project, chapter, use_model=bool(body.use_model)
    )


@router.get("/{project_id}/writing-progress")
def writing_progress(project_id: int, db: Session = Depends(get_db)) -> dict:
    """当前章、大纲完成度、时间线当前节点与下一节点距离。"""
    project = _require_project(db, project_id)
    return build_writing_progress(db, project)
