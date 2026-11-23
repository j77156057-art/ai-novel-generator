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
    node_to_dict,
    plan_nodes_with_llm,
    plan_timeline,
    state_from_dict,
    state_to_dict,
)
from app.llm.provider import LocalFallbackLLMProvider
from app.models import Project
from app.routers.projects import _provider_or_none
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["timeline"])

#: 重规划需要可用的模型渠道（占位节点再规划还是占位，没有意义）。
TIMELINE_NO_CHANNEL = (
    "未配置可用的模型渠道：无法把占位节点重规划成真实剧情点。"
    "请到「模型设置」页选择渠道并填写 API Key 后重试。"
)

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


class TimelinePlanIn(StrictRequest):
    """重规划：用模型把占位节点填实；默认保留人工追加的节点。"""

    keep_user_nodes: bool = True


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
            "planning_status": "",
            "planning_incomplete": True,
            "planning_reason": "时间线尚未规划：首次生成或追加节点时才会建立骨架",
        }
    return {
        "exists": True,
        "outline_chapters": len(entries),
        "order": list(state.order),
        "nodes": [node_to_dict(state.nodes[nid]) for nid in state.order if nid in state.nodes],
        "current_node": state.current_node,
        "current_beat": state.current_beat,
        "done": sorted(state.done),
        "early_done": sorted(state.early_done),
        "branch_used": state.branch_used,
        "has_chapter_ranges": has_chapter_ranges(state),
        "planning_status": state.planning_status,
        "planning_incomplete": state.planning_status in ("", "placeholder"),
        "planning_reason": state.planning_reason,
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
        state.planning_status = "placeholder"
        state.planning_reason = (
            "时间线由人工追加节点建立，其余节点仍是占位；"
            "可调用 POST /api/projects/{id}/timeline/plan 让模型补全"
        )

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
        source="user",
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
        "created": node_to_dict(node),
        "timeline": _state_out(proj, state),
    }


@router.post("/{project_id}/timeline/plan")
def replan_timeline(
    project_id: int, body: TimelinePlanIn, db: Session = Depends(get_db)
) -> dict:
    """用模型把占位节点重规划成真实剧情点（B 子系统第一刀）。

    - 没有模型渠道 → 503（占位再规划还是占位，不假装成功）；
    - 模型返回不可用节点 → 502，**不改存档**；
    - 成功 → 只覆盖非 `user` 节点，进度字段（done/current/seg_counts）原样保留。
    """
    proj = _require_project(db, project_id)
    entries = _outline_entries(proj)
    if not entries:
        raise HTTPException(status_code=400, detail="项目还没有章纲：先写 outline 再重规划")

    provider = _provider_or_none()
    if provider is None or isinstance(provider, LocalFallbackLLMProvider):
        raise HTTPException(status_code=503, detail=TIMELINE_NO_CHANNEL)

    state = _state(proj)
    if state is None:
        state = plan_timeline(
            total_segments=_total_segments(proj),
            avg_node_segments=_avg_node_segments(proj),
            branch_ratio=_branch_ratio(proj),
        )

    filled = plan_nodes_with_llm(provider, entries, proj.plot_bible, state.total_main)
    if not filled:
        raise HTTPException(
            status_code=502,
            detail="模型没有返回可用的剧情点（超时或解析失败），时间线保持原状；请稍后重试",
        )

    replaced = 0
    for node in filled:
        old = state.nodes.get(node.id)
        if old is not None and body.keep_user_nodes and old.source == "user":
            continue
        if old is not None:
            node.segment_budget = old.segment_budget or node.segment_budget
        state.nodes[node.id] = node
        replaced += 1

    state.planning_status = "llm"
    state.planning_reason = ""
    assign_chapter_ranges(state, len(entries))
    _save(db, proj, state)

    return {"replaced": replaced, "timeline": _state_out(proj, state)}
