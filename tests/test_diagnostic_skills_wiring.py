"""诊断技能体系接线回归（2026-11-21）。

覆盖四件事：

1. 实验性诊断的授权凭据只认用户原话（`app.agents.consent`）；
2. 三个诊断工具进入模型可见集合与只读白名单；
3. `list_diagnostic_skills` 能发现全部技能，且列目录不授权执行；
4. 确定性诊断 `run_diagnosis` 通过统一工具入口可调用。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.agents.action_catalog import model_tool_names  # noqa: E402
from app.agents.consent import (  # noqa: E402
    reset_experimental_consent,
    set_experimental_consent,
    user_granted_experimental,
)
from app.agents.registry import describe_tools, invoke_tool  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import Project  # noqa: E402

DIAGNOSTIC_TOOLS = {
    "run_diagnosis", "run_diagnostic_task", "list_diagnostic_skills",
    "prepare_body_repair",
}


@pytest.fixture(autouse=True)
def _clean():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("同意使用实验性诊断技能", True),
        ("我允许你跑实验性技能", True),
        ("实验诊断，我同意了", True),          # 逗号不断句
        ("不同意使用实验性诊断技能", False),    # 否定词
        ("这段实验报告我同意", False),          # 没有「技能/诊断/能力」上下文
        ("", False),
        (None, False),
    ],
)
def test_consent_only_from_user_words(text, expected):
    assert user_granted_experimental(text) is expected


def test_consent_contextvar_roundtrip():
    assert user_granted_experimental("同意使用实验性诊断技能") is True
    token = set_experimental_consent(True)
    try:
        from app.agents.consent import experimental_consent_granted

        assert experimental_consent_granted() is True
    finally:
        reset_experimental_consent(token)
    from app.agents.consent import experimental_consent_granted

    assert experimental_consent_granted() is False


def test_diagnostic_tools_are_exposed_to_model():
    names = set(model_tool_names())
    assert DIAGNOSTIC_TOOLS <= names

    described = {
        item["function"]["name"] for item in describe_tools(only=names)
    }
    assert DIAGNOSTIC_TOOLS <= described


def test_list_diagnostic_skills_discovers_and_does_not_authorize():
    result = invoke_tool("list_diagnostic_skills", None, project_id=1)
    assert result["business_read_only"] is True
    assert "allow_experimental" in result["authorization_note"]
    assert "历史版本" in result["hidden_versions_note"]

    skills = {s["id"]: s for s in result["skills"]}
    # 每个技能只暴露最新版：8 组技能 → 8 条；历史版本不主动列出。
    assert len(skills) == 8
    assert skills["closed_inventory"]["latest_version"] == 8
    assert skills["closed_inventory"]["superseded_versions"] == [1, 2, 3, 4, 5, 6, 7]
    assert skills["planning_review"]["latest_version"] == 2
    assert skills["body_consistency"]["superseded_versions"] == [1]

    planning = skills["planning_review"]
    assert planning["status"] == "experimental"
    assert planning["requires_experimental_opt_in"] is True
    assert planning["required_capabilities"]
    assert result["capabilities"]


def test_text_quality_repair_has_no_dead_repair_step():
    """修复卡由主 agent 的 propose_text_fix 发起；诊断步骤里不再有死能力。"""
    from app.agents.diagnostic_skills import CAPABILITIES, load_skill

    manifest = load_skill("text_quality_repair", 1, allow_experimental=True)
    assert [s["capability"] for s in manifest["steps"]] == ["inspect_text_quality"]
    assert "prepare_text_repair" not in CAPABILITIES


def test_body_tasks_map_to_distinct_skills():
    """body_consistency=评估矛盾；body_repair_plan=规划修复，不再二选一靠猜。"""
    from app.agents.diagnostic import resolve_skill_selection
    from app.agents.diagnostic_skills import load_skill

    assess = load_skill("body_consistency", 2, allow_experimental=True)
    repair = load_skill("body_repair", 4, allow_experimental=True)
    assert "body_repair_plan" not in assess["applicable_tasks"]
    assert repair["applicable_tasks"] == ["body_repair_plan", "repair_verification"]

    assert resolve_skill_selection("body_consistency", None, None) == (
        "body_consistency", 2,
    )
    assert resolve_skill_selection("body_repair_plan", None, None) == (
        "body_repair", 4,
    )
    # 不传版本 → 最新版；显式旧版本仍可复现。
    assert resolve_skill_selection("planning_review", None, None) == (
        "planning_review", 2,
    )
    assert resolve_skill_selection("body_consistency", "body_consistency", 1) == (
        "body_consistency", 1,
    )
    # 复验沿用上一次 run 的技能与版本；显式换技能必须 fail loud。
    previous = {"skill": {"id": "body_repair", "version": 3, "status": "experimental"}}
    assert resolve_skill_selection(
        "repair_verification", None, None, previous,
    ) == ("body_repair", 3)
    with pytest.raises(ValueError, match="verification_requires_pinned_previous_run"):
        resolve_skill_selection(
            "repair_verification", "body_consistency", 2, previous,
        )


def test_experimental_execution_requires_user_consent():
    from app.agents.diagnostic import diagnostic_tool

    with pytest.raises(ValueError) as err:
        diagnostic_tool(None, 1, allow_experimental=True)
    assert "明确授权" in str(err.value)


def test_narrative_binding_capability_runs_without_references():
    """绑定能力必须能跑通：旧代码给 narrative_fingerprint 传了已不存在的参数。"""
    from app.agents.diagnostic import read_capability

    s = SessionLocal()
    try:
        proj = Project(title="诊断绑定测试", generation_length=3000)
        s.add(proj)
        s.commit()
        s.refresh(proj)
        obs = read_capability(s, proj.id, "inspect_narrative_binding", {})
    finally:
        s.close()

    assert obs["data"]["reason"] == "no_references"
    assert obs["coverage"]["complete"] is True


def test_run_diagnosis_tool_returns_unified_issue_shape():
    s = SessionLocal()
    try:
        proj = Project(title="诊断接线测试", generation_length=3000)
        s.add(proj)
        s.commit()
        s.refresh(proj)
        result = invoke_tool("run_diagnosis", s, project_id=proj.id)
    finally:
        s.close()

    assert {"issues", "findings", "count", "rules_evaluated", "stats"} <= set(result)
    assert result["issues"] is result["findings"]
    assert result["count"] == len(result["issues"])
