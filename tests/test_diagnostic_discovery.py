"""诊断技能目录的入口回归（从 main 搬入，去掉 react_loop 依赖）。

原版依赖 `app.agents.react_loop.build_tool_catalog/validate_arguments`；
本分支没有 react_loop，用注册表的 `describe_tools` + 一个等价的最小参数校验
替代。断言目标不变：目录可发现、实验执行仍要用户授权。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.agents.registry import describe_tools, get_tool_registry  # noqa: E402


def _invalid_arguments(schema: dict, args: dict) -> bool:
    """等价于 react_loop.validate_arguments 的最小判定：返回 True = 有错。"""
    props = set((schema.get("properties") or {}).keys())
    required = set(schema.get("required") or [])
    if not required <= set(args):
        return True
    if schema.get("additionalProperties") is False and set(args) - props:
        return True
    return False


def test_diagnostic_discovery_exposes_contract_without_opt_in_execution():
    catalog = {
        item["function"]["name"]: item["function"]
        for item in describe_tools()
    }
    assert "list_diagnostic_skills" in catalog

    tool = get_tool_registry().resolve("list_diagnostic_skills")
    result = tool.callable(None, 1)
    planning = next(s for s in result["skills"] if s["id"] == "planning_review")
    assert planning["status"] == "experimental"
    assert planning["requires_experimental_opt_in"] is True
    assert planning["required_capabilities"]
    assert result["business_read_only"] is True
    assert "allow_experimental" in result["authorization_note"]

    params = catalog["run_diagnostic_task"]["parameters"]
    assert not _invalid_arguments(params, {
        "novel_ids": [1], "allow_experimental": True, "cursor": 2,
    })
    # project_id 由服务端注入；模型不该（也不能）自己传。
    assert _invalid_arguments(params, {"project_id": 2})
