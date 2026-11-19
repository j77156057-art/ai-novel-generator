"""规划就绪闸（P0）：规划失败/占位节点时，给生成入口一个可解释的 409。

背景：`plan_nodes_with_llm` 失败时调用方会静默回退到占位节点，生成继续跑，
事后才知道这一章没有真实剧情点。这里把「规划不完整」变成一个**可查询、
可拦截**的状态，而不是只写在 `planning_status` 里没人读。

口径：

- 只在**已有时间线存档**时判断；新项目第一次生成会现场规划，不能提前拦；
- `planning_status == "placeholder"` 或仍有 `source == "placeholder"` 的主干节点
  → `needs_planning`；
- `planning_guard_mode="report"`（默认）只回传缺口，不拦；
  `"block"` 时生成入口 409，重试入口是 `POST /api/projects/{id}/timeline/plan`。
"""
from __future__ import annotations

BLOCK = "block"


def planning_block(project) -> dict | None:
    """返回规划缺口；已经可生成时返回 None。"""
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    if not timeline:
        return None
    status = str(timeline.get("planning_status") or "")
    nodes = timeline.get("nodes")
    nodes = nodes if isinstance(nodes, dict) else {}
    placeholders = sorted(
        nid for nid, node in nodes.items()
        if isinstance(node, dict)
        and node.get("kind", "main") == "main"
        and node.get("source") == "placeholder"
    )
    if status != "placeholder" and not placeholders:
        return None
    reason = str(timeline.get("planning_reason") or "").strip()
    if not reason:
        reason = (
            "规划仍是占位节点，标题/节拍/准入准出为空"
            if placeholders else "上一次规划失败"
        )
    return {
        "code": "needs_planning",
        "reason": reason,
        "planning_status": status or "placeholder",
        "placeholder_nodes": placeholders,
        "retry": f"POST /api/projects/{project.id}/timeline/plan",
    }
