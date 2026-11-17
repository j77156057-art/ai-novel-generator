"""时间线节点端点（B2）：读状态、重算章区间、追加节点。

背景（`reports/2026-11-15-platform-gap.md` B2）：时间线节点只在首次生成时由
`plan_nodes_with_llm` 规划一次，之后没有追加入口；章纲变长后节点区间也不会
自动扩，平台侧没法「在大纲后半段补一个新剧情点」。

本模块只做三件事，全部围绕 `projects.plot_timeline`（`TimelineState` 的 JSON）：

- `GET  /{id}/timeline`：只读当前状态（节点 + 章区间 + 当前锚点）；
- `POST /{id}/timeline/assign-ranges`：按当前章纲长度重算所有主干节点的章区间；
- `POST /{id}/timeline/nodes`：追加一个主干节点（可选顺带重算区间）。

不调 LLM、不新增存储。`state_from_dict` 负责把骨架（order/total_main/
branch_budget）按当前项目参数复算，所以这里不需要自己维护一份。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.engine import _outline_entries, _total_segments
from app.generation.timeline import (
    DEFAULT_BRANCH_RATIO,
    PlotNode,
    TimelineState,
    assign_chapter_ranges,
    has_chapter_ranges,
    plan_timeline,
    state_from_dict,
    state_to_dict,
)
from app.models import Project
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["timeline"])

#: 节点情绪与 `generation/timeline.PlotNode.emotion` 的口径一致。
NODE_EMOTIONS = ("爽", "虐", "甜", "怒", "悬疑", "日常", "过渡")

#: 一次追加的节拍上限：节拍是提示词里的短句，太多会把节点提示撑爆。
MAX_BEATS = 8


class TimelineNodeIn(StrictRequest):
    """追加一个主干节点。`re_assign=True` 时顺带重算全部章区间。"""

    title: str
    emotion: str = "爽"
    entry_condition: str = ""
    exit_condition: str = ""
    beats: list[str] = []
    segment_budget: int | None = None
    re_assign: bool = True


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _avg_node_segments(proj: Project) -> int:
    """节奏蒸馏值（合法则用，否则回退默认 5——与引擎同口径）。"""
    rhythm = proj.plot_rhythm if isinstance(proj.plot_rhythm, dict) else {}
    value = rhythm.get("avg_node_segments")
    return value if isinstance(value, int) and value >= 1 else 5


def _branch_ratio(proj: Project) -> float:
    """支线额度比例（节奏蒸馏值合法则用，否则回退默认）。"""
    rhythm = proj.plot_rhythm if isinstance(proj.plot_rhythm, dict) else {}
    value = rhythm.get("branch_ratio")
    if isinstance(value, (int, float)) and 0 <= value <= 1:
        return float(value)
    return DEFAULT_BRANCH_RATIO


def _state(proj: Project) -> TimelineState | None:
    """读存档状态；没有存档返回 None（不建、不写）。"""
    entries = _outline_entries(proj)
    if not entries:
        return None
    saved = proj.plot_timeline if isinstance(proj.plot_timeline, dict) else None
    if not saved:
        return None
    return state_from_dict(
        saved, total_segments=_total_segments(proj), total_chapters=len(entries)
    )


def _save(db: Session, proj: Project, state: TimelineState) -> None:
    proj.plot_timeline = state_to_dict(state)
    db.add(proj)
    db.commit()
    db.refresh(proj)


def _node_out(node: PlotNode) -> dict:
    return {
        "id": node.id,
        "kind": node.kind,
        "title": node.title,
        "emotion": node.emotion,
        "entry_condition": node.entry_condition,
        "exit_condition": node.exit_condition,
        "beats": list(node.beats),
        "segment_budget": node.segment_budget,
        "parent": node.parent,
        "children": list(node.children),
        "chapter_start": node.chapter_start,
        "chapter_end": node.chapter_end,
    }


def _state_out(proj: Project, state: TimelineState | None) -> dict:
    entries = _outline_entries(proj)
    if state is None:
        return {
            "exists": False,
            "outline_chapters": len(entries),
            "nodes": [],
            "order": [],
            "current_node": None,
            "current_beat": 0,
            "done": [],
            "early_done": [],
            "branch_used": 0,
            "has_chapter_ranges": False,
        }
    return {
        "exists": True,
        "outline_chapters": len(entries),
        "order": list(state.order),
        "nodes": [_node_out(state.nodes[nid]) for nid in state.order if nid in state.nodes],
        "current_node": state.current_node,
        "current_beat": state.current_beat,
        "done": sorted(state.done),
        "early_done": sorted(state.early_done),
        "branch_used": state.branch_used,
        "has_chapter_ranges": has_chapter_ranges(state),
    }


@router.get("/{project_id}/timeline")
def get_timeline(project_id: int, db: Session = Depends(get_db)) -> dict:
    """读当前时间线（只读；无存档时 `exists=false`，不建骨架）。"""
    proj = _require_project(db, project_id)
    return _state_out(proj, _state(proj))


@router.post("/{project_id}/timeline/assign-ranges")
def assign_ranges(project_id: int, db: Session = Depends(get_db)) -> dict:
    """按当前章纲长度重算全部主干节点的章区间并落库。

    适用场景：章纲从 40 章追加到 60 章后，想让「第 41–60 章」也落到节点上。
    没有存档就 409：先让引擎规划一次（或调用 `/timeline/nodes` 追加）再重算。
    """
    proj = _require_project(db, project_id)
    entries = _outline_entries(proj)
    if not entries:
        raise HTTPException(status_code=400, detail="项目还没有章纲：先写 outline 再分配章区间")
    state = _state(proj)
    if state is None:
        raise HTTPException(
            status_code=409,
            detail="时间线尚未规划：先生成一次让引擎建骨架，或调用 /timeline/nodes 追加节点",
        )
    assign_chapter_ranges(state, len(entries))
    _save(db, proj, state)
    return _state_out(proj, state)


@router.post("/{project_id}/timeline/nodes")
def append_node(
    project_id: int, body: TimelineNodeIn, db: Session = Depends(get_db)
) -> dict:
    """追加一个主干节点（B2）。没有存档时先用占位骨架建一份，再追加。"""
    proj = _require_project(db, project_id)
    entries = _outline_entries(proj)
    if not entries:
        raise HTTPException(status_code=400, detail="项目还没有章纲：先写 outline 再追加节点")

    title = (body.title or "").strip()
    if not title or len(title) > 40:
        raise HTTPException(status_code=400, detail="title 必填且不超过 40 字")
    if body.emotion not in NODE_EMOTIONS:
        raise HTTPException(
            status_code=400, detail=f"emotion 必须是 {NODE_EMOTIONS} 之一"
        )
    if len(body.beats) > MAX_BEATS:
        raise HTTPException(status_code=400, detail=f"beats 最多 {MAX_BEATS} 条")
    if body.segment_budget is not None and body.segment_budget < 1:
        raise HTTPException(status_code=400, detail="segment_budget 必须 ≥ 1")

    state = _state(proj)
    if state is None:
        state = plan_timeline(
            total_segments=_total_segments(proj),
            avg_node_segments=_avg_node_segments(proj),
            branch_ratio=_branch_ratio(proj),
        )
        assign_chapter_ranges(state, len(entries))

    # 新 id 取「现有主干序号最大值 + 1」，避免和占位节点 n1..n12 撞名。
    existing = {nid for nid in state.nodes if nid.startswith("n") and nid[1:].isdigit()}
    next_no = max((int(nid[1:]) for nid in existing), default=0) + 1
    new_id = f"n{next_no}"
    while new_id in state.nodes:
        next_no += 1
        new_id = f"n{next_no}"

    budget = body.segment_budget
    if budget is None:
        first = state.nodes.get(state.order[0]) if state.order else None
        budget = first.segment_budget if first is not None else 1

    node = PlotNode(
        id=new_id,
        kind="main",
        title=title,
        emotion=body.emotion,
        entry_condition=(body.entry_condition or "").strip(),
        exit_condition=(body.exit_condition or "").strip(),
        beats=[b.strip() for b in body.beats if b.strip()],
        segment_budget=budget,
        parent=None,
        children=[],
    )
    state.nodes[new_id] = node
    state.order.append(new_id)
    state.total_main = len(state.order)
    if state.current_node is None:
        state.current_node = new_id

    if body.re_assign:
        assign_chapter_ranges(state, len(entries))
    _save(db, proj, state)

    return {
        "created": _node_out(node),
        "timeline": _state_out(proj, state),
    }
