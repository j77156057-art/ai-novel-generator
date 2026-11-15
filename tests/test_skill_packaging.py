"""skill 沉淀回归：实测有效的做法要能作为 skill 被发现、被加载、角色由清单声明。

main 原文件里「能力执行」那半段（`read_capability('inspect_text_quality')`、
`run_diagnostic_task('text_quality_audit')`、registry 的 `_DIAGNOSTIC_PARAMETERS`）
依赖 feat 侧尚未搬的运行时件：
- `inspect_text_quality` → `style.scope.resolve_project_scope`
- `inspect_prompt_health` → `agents.prompt_audit`
- registry 任务枚举 → 第 2 批 registry
这三段保留为 strict xfail（搬过来后会自动提醒补测），其余照搬。

清单加载机制（`load_skill` + manifest 校验）的完整矩阵见
`tests/test_diagnostic_skills_manifest.py`，这里只钉「三份新 skill 的身份与角色」。
"""

from __future__ import annotations

import os
import sys

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents.diagnostic import ROLE_SYSTEM_PROMPTS  # noqa: E402
from app.agents.diagnostic_skills import (  # noqa: E402
    ROLES,
    discover_skills,
    load_skill,
)


def test_the_three_new_skills_are_discoverable():
    ids = {entry["id"] for entry in discover_skills(allow_experimental=True)}
    assert {"text_quality_audit", "text_quality_repair", "prompt_health_audit"} <= ids


def test_each_skill_declares_its_own_executor_role():
    assert load_skill("text_quality_audit", 1, allow_experimental=True)["agent_roles"] == ["diagnostic"]
    assert load_skill("text_quality_repair", 1, allow_experimental=True)["agent_roles"] == ["editor"]
    assert load_skill("prompt_health_audit", 1, allow_experimental=True)["agent_roles"] == ["diagnostic"]
    assert set(ROLES) == {"diagnostic", "editor"}


def test_every_role_has_exactly_one_system_prompt():
    """角色与口径一一对应 —— 少一条就会掉回默认诊断口径。"""
    assert set(ROLE_SYSTEM_PROMPTS) == set(ROLES)
    for role, prompt in ROLE_SYSTEM_PROMPTS.items():
        assert f"角色 {role}" in prompt


def test_text_quality_capability_reports_issues_and_mechanical_fixes():
    from app.agents.diagnostic import read_capability
    from app.db import Base, SessionLocal, engine
    from app.models import Generation, Project

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        project = Project(title="skill", target_length=3000, generation_length=1200)
        db.add(project)
        db.commit()
        db.add(Generation(project_id=project.id, segment_no=1,
                          text="少年把竹篙插进石缝,船身晃了晃。。他稳住气,又撑了两下。"
                               "窗外的雾散了一点,远处的灯塔亮起来,一下,又灭了。"))
        db.commit()

        obs = read_capability(db, project.id, "inspect_text_quality", {})
        assert obs["capability"] == "inspect_text_quality"
        assert obs["coverage"]["source"] == "Generation.text"
        assert obs["coverage"]["complete"] is True
        rules = {i["rule"] for i in obs["data"]["issues"]}
        assert "text_punctuation_accident" in rules
        # 修法由规则算出，模型不重新措辞
        assert obs["data"]["fixes"]
        fix = obs["data"]["fixes"][0]
        assert fix["after"].count("，") == 5      # 5 处半角逗号换全角
        assert "。。" not in fix["after"]
        assert fix["changes"] == 6               # 5 处半角 + 1 处重复标点
        # 事实自洽那一层没跑 —— 必须**看得见**
        assert obs["data"]["stats"]["fact_checked"] is False
    finally:
        db.close()


@pytest.mark.xfail(strict=True, reason=(
    "`inspect_prompt_health` 依赖 feat 未搬的 `agents.prompt_audit`（第 7 批）"))
def test_prompt_health_capability_is_read_only():
    from app.agents.diagnostic import read_capability
    from app.db import Base, SessionLocal, engine
    from app.models import Project

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        project = Project(title="skill", target_length=3000, generation_length=1200)
        db.add(project)
        db.commit()
        obs = read_capability(db, project.id, "inspect_prompt_health", {})
        assert {"issues", "stats"} <= set(obs["data"])
    finally:
        db.close()


@pytest.mark.xfail(strict=True, reason=(
    "feat 的 agents.registry 尚未注册 `_DIAGNOSTIC_PARAMETERS`（第 2 批）"))
def test_the_assistant_can_ask_for_the_new_tasks():
    from app.agents.registry import _DIAGNOSTIC_PARAMETERS
    enum = _DIAGNOSTIC_PARAMETERS["properties"]["task_type"]["enum"]
    assert {"text_quality_audit", "text_quality_repair", "prompt_health_audit"} <= set(enum)
