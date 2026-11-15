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


def test_prompt_block_spells_out_frontend_only():
    block = actions_prompt_block("能不能回滚到快照")
    assert "只能由用户在前端页面操作" in block
    assert "不得代执行" in block
