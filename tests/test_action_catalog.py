"""动作目录的约束回归。

收敛方案把「哪些操作能由助手做」变成数据，这里守住三条底线：

1. 破坏性操作必须是 frontend_only + 需确认（不能被助手代执行）；
2. 档 3 操作也必须登记在目录里（否则助手看不见，用户仍要回菜单找）；
3. 匹配是确定性、可解释的：同一问句命中哪些操作要稳定，空查询不命中。
"""

from app.agents.action_catalog import (
    ACTIONS,
    actions_prompt_block,
    list_actions,
    match_actions,
    model_tool_names,
)


def test_destructive_actions_are_frontend_only_and_confirmed():
    for spec in ACTIONS:
        if spec.category == "destructive":
            assert spec.policy == "frontend_only", spec.key
            assert spec.requires_confirmation is True, spec.key
            assert spec.frontend_entry, spec.key


def test_tier3_actions_are_registered_and_visible():
    """档 3（破坏性/批量/配置）必须出现在目录里，只是策略为 frontend_only。"""
    tier3 = [s for s in ACTIONS if s.category == "destructive" or s.batch or s.touches_config]
    assert tier3, "档 3 操作不应为空"
    visible = {item["key"] for item in list_actions()}
    assert {s.key for s in tier3} <= visible


def test_match_actions_is_deterministic_and_policy_aware():
    assert match_actions("") == []
    assert match_actions("随便聊聊天气") == []

    write_hits = match_actions("帮我把这段重写一下")
    assert "invoke_generation" in [s.key for s in write_hits]
    assert all(s.policy != "auto_read" for s in write_hits if s.key == "invoke_generation")

    rollback_hits = match_actions("能不能回滚到快照")
    keys = [s.key for s in rollback_hits]
    assert "snapshot_rollback" in keys
    rollback = next(s for s in rollback_hits if s.key == "snapshot_rollback")
    assert rollback.policy == "frontend_only"
    assert rollback.requires_confirmation is True


def test_kg_and_distill_reads_are_wired_to_tools():
    """原先 unwired_read 的只读能力已全部接到白名单内的只读工具。"""
    by_key = {s.key: s for s in ACTIONS}
    expected = {
        "kg_worldview": "get_kg_worldview",
        "kg_timeline": "get_kg_timeline",
        "kg_story_context": "get_story_context",
        "style_distill_detail": "get_style_distill_detail",
        "text_gate": "get_text_gate_no_generation",
        "project_diagnosis": "diagnose_project",
        "assistant_runs": "list_assistant_runs",
        "proposal_list": "list_proposals",
        "version_list": "list_versions",
        "prompt_active": "get_active_prompts",
        "snapshot_list": "list_snapshots",
    }
    for key, tool in expected.items():
        spec = by_key[key]
        assert spec.policy == "auto_read", key
        assert spec.read_tool == tool, key
        assert tool in model_tool_names(), tool
    assert "kg_worldview" in [s.key for s in match_actions("这个世界观有什么设定")]


def test_prompt_block_spells_out_frontend_only():
    block = actions_prompt_block("能不能回滚到快照")
    assert "只能由用户在前端页面操作" in block
    assert "不得代执行" in block
