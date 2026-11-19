"""助理记忆的读写端点：`/api/assistant/memories`。

## 为什么单独一个文件

`routers/assistant.py` 有两条源码级静态断言守着「不暴露写端点 / 不写业务表」
（见 `tests/test_assistant_readonly.py`、`tests/test_assistant_diagnose.py`）。
删除记忆虽然是写操作，但它是用户对自动抽取结果的**清理**，与业务表无关；
单独成文件后边界清楚，问答路由只管「读记忆拼提示词 + 自动抽取写入口」。

## 语义

- 记忆是**项目级**的：换一本书不该带着上一本的约束；
- 记忆内容由助手在问答后**自动抽取**，用户不手写；这里只提供「查看」与
  「删除」两条路（抽错了能删掉，而不是只能忍）；
- 读取按写入顺序返回，供抽屉展示与 `limit` 提示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import assistant_memory
from app.db import get_db
from app.models import Project

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _require_project(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return project


def _payload(db: Session, project_id: int) -> dict:
    rows = assistant_memory.list_memories(db, project_id)
    return {
        "project_id": project_id,
        "items": [assistant_memory.row_to_dict(row) for row in rows],
        "count": len(rows),
        "limit": assistant_memory.MAX_MEMORIES,
    }


@router.get("/memories")
def list_memories(project_id: int, db: Session = Depends(get_db)) -> dict:
    """列出该项目的长期记忆（按写入顺序）。"""
    _require_project(db, project_id)
    return _payload(db, project_id)


@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: int, project_id: int, db: Session = Depends(get_db)
) -> dict:
    """删除一条记忆（不存在返回 404，便于前端区分「已经没了」）。"""
    _require_project(db, project_id)
    if not assistant_memory.delete_memory(db, project_id, memory_id):
        raise HTTPException(status_code=404, detail="这条记忆不存在")
    return _payload(db, project_id)
