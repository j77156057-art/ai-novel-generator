# -*- coding: utf-8 -*-
"""写作进度：当前章、大纲完成度、时间线节点与下一节点距离。

只读聚合，不调模型、不写表。口径与生成链路一致：

- 章号解析复用 `engine._chapter_ordinal`（第N章/回/节/卷都认）；
- 时间线复用 `projects.plot_timeline` → `state_from_dict` / `node_for_chapter`；
- 主线进度复用 `timeline_progress(chapter_based=True)`。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.generation.engine import (
    _chapter_ordinal,
    _outline_entries,
    _total_segments,
)
from app.generation.timeline import (
    node_for_chapter,
    state_from_dict,
    timeline_progress,
)
from app.models import Generation, Project


def _chapter_rows(db, project: Project) -> list[tuple[int, int]]:
    rows = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no)
    ).all()
    out: list[tuple[int, int]] = []
    for row in rows:
        ordinal = _chapter_ordinal(row.chapter_range or "")
        out.append((row.segment_no or 0, ordinal or row.segment_no or 0))
    return out


def build_writing_progress(db, project: Project) -> dict[str, Any]:
    entries = _outline_entries(project)
    outline_total = len(entries)
    chapters = _chapter_rows(db, project)
    generated = sorted({ordinal for _seg, ordinal in chapters if ordinal > 0})
    current = max(generated) if generated else 0
    next_ch = current + 1

    outline_percent = (
        min(100.0, round(len(generated) / outline_total * 100, 1))
        if outline_total else 0.0
    )

    timeline_out: dict[str, Any] = {
        "exists": False,
        "current_node": None,
        "next_node": None,
        "node_progress": 0.0,
        "to_next_node_percent": 0.0,
        "main_progress": 0.0,
    }
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    state = state_from_dict(
        saved or {},
        total_segments=_total_segments(project),
        total_chapters=outline_total or None,
    )
    if state is not None:
        node_id = node_for_chapter(state, next_ch)
        node = state.nodes.get(node_id or "")
        order = [nid for nid in state.order if nid in state.nodes]
        index = order.index(node.id) + 1 if node is not None and node.id in order else 0
        next_node = None
        if node is not None and node.id in order:
            pos = order.index(node.id)
            for nid in order[pos + 1:]:
                candidate = state.nodes.get(nid)
                if candidate is not None and candidate.chapter_start > 0:
                    next_node = candidate
                    break

        node_progress = 0.0
        if node is not None and node.chapter_end >= node.chapter_start > 0:
            span = node.chapter_end - node.chapter_start + 1
            consumed = min(max(next_ch - node.chapter_start + 1, 0), span)
            node_progress = round(consumed / span, 3)

        progress = timeline_progress(state, chapter_based=True)
        timeline_out = {
            "exists": True,
            "current_node": (
                {
                    "id": node.id,
                    "title": node.title,
                    "chapter_start": node.chapter_start,
                    "chapter_end": node.chapter_end,
                    "index": index,
                    "total": len(order),
                }
                if node is not None else None
            ),
            "next_node": (
                {
                    "id": next_node.id,
                    "title": next_node.title,
                    "chapter_start": next_node.chapter_start,
                    "chapter_end": next_node.chapter_end,
                }
                if next_node is not None else None
            ),
            "node_progress": node_progress,
            "to_next_node_percent": round(max(0.0, 1 - node_progress) * 100, 1),
            "main_progress": float(progress.get("main_progress") or 0.0),
        }

    return {
        "current_chapter": current,
        "next_chapter": next_ch,
        "generated_chapters": len(generated),
        "outline_total": outline_total,
        "outline_percent": outline_percent,
        "to_outline_percent": round(max(0.0, 100 - outline_percent), 1),
        "chapters_remaining": max(0, outline_total - len(generated)) if outline_total else 0,
        "timeline": timeline_out,
    }
