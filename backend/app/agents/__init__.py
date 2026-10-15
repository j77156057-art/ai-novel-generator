"""Assistant Agent 工具包（ADR-2026-10-12 第 5 步第 2、3 步）。

包级暴露仅放稳定的 import 入口（registry 暴露 register / invoke / reject；
diagnosis 暴露结构化诊断规则与规则跑批入口）。
具体工具实现以 `app.agents.registry` 为唯一对外面，避免引入平行路径。
"""
from app.agents.diagnosis import (  # noqa: F401
    RULE_ORDER,
    RULE_REGISTRY,
    DiagnosticIssue,
    DiagnosticReport,
    IssueEvidence,
    iter_evidence,
    run_diagnostic_rules,
)
from app.agents.registry import (  # noqa: F401
    ALLOWED_READ_ONLY_TOOLS,
    FORBIDDEN_PREFIXES,
    AssistantToolRegistry,
    ToolSpec,
    get_tool_registry,
    invoke_tool,
    reset_tool_registry,
)

__all__ = [
    "ALLOWED_READ_ONLY_TOOLS",
    "AssistantToolRegistry",
    "DiagnosticIssue",
    "DiagnosticReport",
    "FORBIDDEN_PREFIXES",
    "IssueEvidence",
    "RULE_ORDER",
    "RULE_REGISTRY",
    "ToolSpec",
    "get_tool_registry",
    "invoke_tool",
    "iter_evidence",
    "reset_tool_registry",
    "run_diagnostic_rules",
]