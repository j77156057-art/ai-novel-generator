"""P0：规划就绪闸——占位节点/规划失败时暴露缺口，block 模式 409。"""
import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.config import settings  # noqa: E402
from app.generation.planning_guard import planning_block  # noqa: E402
from app.models import Project  # noqa: E402

_NODE_LLM = {
    "id": "n1", "kind": "main", "title": "开局",
    "entry_condition": "起点", "exit_condition": "完成",
    "source": "llm",
}
_NODE_PLACEHOLDER = {
    "id": "n2", "kind": "main", "title": "主干节点2",
    "entry_condition": "", "exit_condition": "",
    "source": "placeholder",
}


def _project(nodes: dict, status: str = "", reason: str = "") -> Project:
    return Project(
        id=7,
        title="t",
        plot_timeline={
            "planning_status": status,
            "planning_reason": reason,
            "nodes": nodes,
        },
    )


def test_no_timeline_is_not_blocked():
    assert planning_block(Project(id=7, title="t")) is None


def test_fully_planned_timeline_is_not_blocked():
    assert planning_block(_project({"n1": _NODE_LLM}, status="llm")) is None


def test_placeholder_status_is_blocked():
    block = planning_block(
        _project({"n2": _NODE_PLACEHOLDER}, status="placeholder",
                 reason="模型规划失败，已回退占位节点")
    )

    assert block["code"] == "needs_planning"
    assert block["reason"] == "模型规划失败，已回退占位节点"
    assert block["placeholder_nodes"] == ["n2"]
    assert block["retry"] == "POST /api/projects/7/timeline/plan"


def test_legacy_archive_with_placeholder_nodes_is_blocked():
    """旧存档没有 planning_status，但节点仍是占位 → 同样算规划不完整。"""
    block = planning_block(_project({"n1": _NODE_LLM, "n2": _NODE_PLACEHOLDER}))

    assert block["code"] == "needs_planning"
    assert block["placeholder_nodes"] == ["n2"]


def test_user_appended_node_is_not_a_placeholder():
    user = {**_NODE_LLM, "id": "n3", "source": "user"}
    assert planning_block(_project({"n1": _NODE_LLM, "n3": user}, status="llm")) is None


def test_require_planning_ready_defaults_to_report(monkeypatch):
    from app.routers.generation import _require_planning_ready

    monkeypatch.setattr(settings, "planning_guard_mode", "report")
    _require_planning_ready(_project({"n1": _NODE_PLACEHOLDER}))


def test_require_planning_ready_blocks_in_block_mode(monkeypatch):
    from app.routers.generation import _require_planning_ready

    monkeypatch.setattr(settings, "planning_guard_mode", "block")
    with pytest.raises(HTTPException) as err:
        _require_planning_ready(_project({"n1": _NODE_PLACEHOLDER}))

    assert err.value.status_code == 409
    assert err.value.detail["code"] == "needs_planning"
