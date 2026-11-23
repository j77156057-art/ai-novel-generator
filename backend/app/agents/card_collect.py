"""从工具结果里收集待确认卡（只收可信的那些）。

`propose_*` 工具只**构造**卡、不落库。前端收到的卡必须同时满足：端点正确、
方法是 POST、body 里的 `project_id` 就是当前项目 —— 否则用户点下去必然失败。
这段口径此前在动作提案与改写提案里各写了一遍，集中到这里避免一边改一边漏。
"""
from __future__ import annotations

from typing import Any, Mapping


def collect_pending_cards(
    tool_results: Mapping[str, Any] | None,
    project_id: int,
    *,
    kind: str,
    endpoint: str,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for key, value in (tool_results or {}).items():
        name = str(key).split(":", 1)[0]
        if not name.startswith("propose_") or not isinstance(value, dict):
            continue
        action = value.get("pending_action")
        if (isinstance(action, dict)
                and action.get("kind") == kind
                and action.get("endpoint") == endpoint
                and action.get("method") == "POST"
                and (action.get("body") or {}).get("project_id") == project_id):
            cards.append(action)
    return cards
