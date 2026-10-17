"""R5 运行时白名单真校验回归测试（§4.2①）。

背景：架构评估发现 `ALLOWED_READ_ONLY_TOOLS` 在运行时**从未被读取**——
`AssistantToolRegistry.register()` 只拒绝 `read_only is False`、FORBIDDEN_PREFIXES
和重名，等于「信任注册」：一个新增的只读工具只要没命中黑名单前缀就能注册进来，
绕过了白名单。本文件证明该 bug（先红）并锁定修复后的行为（后绿）。

测试不需要数据库：直接构造 `AssistantToolRegistry` / `ToolSpec`。
"""

import pytest

from app.agents.registry import (
    ALLOWED_READ_ONLY_TOOLS,
    AssistantToolRegistry,
    ToolSpec,
    _DEFAULT_SPECS,
)


def _noop(**kwargs):
    """纯占位 callable：仅用于构造测试用的 ToolSpec，不触碰业务数据。"""
    return {}


# ------------------------------------------------------------------
# T1：未出现在白名单里的只读工具必须被 register() 拒绝（fail-closed）。
# ------------------------------------------------------------------
def test_r5_register_rejects_tool_not_in_allowlist():
    """① RED→GREEN：不在 ALLOWED_READ_ONLY_TOOLS 的只读工具必须抛 PermissionError。

    修复前（bug 存在时）：register() 不读白名单，'read_future_probe' 会成功注册
    → 本测试 FAIL（确认 bug）。
    修复后：register() 新增白名单校验，主动抛 PermissionError → 本测试 PASS。
    """
    reg = AssistantToolRegistry()
    spec = ToolSpec(
        name="read_future_probe",
        description="不在白名单中的占位只读工具，用于验证 fail-closed 校验。",
        read_only=True,
        callable=_noop,
    )
    with pytest.raises(PermissionError):
        reg.register(spec)


# ------------------------------------------------------------------
# T2：默认白名单里的 11 个工具必须全部注册成功，且集合与白名单一致。
# ------------------------------------------------------------------
def test_r5_default_specs_all_register_and_match_allowlist():
    """② 回归：_DEFAULT_SPECS 全部注册不报错，注册表最终恰好 11 个工具名。

    证明白名单与默认注册集合保持同步——二者数量、成员必须逐一对应。
    """
    reg = AssistantToolRegistry()
    # register_many 内部逐个调用 register，会触发白名单校验
    reg.register_many(_DEFAULT_SPECS)

    registered = reg.names()
    assert len(registered) == 11
    # 默认规格的工具名必须**恰好**等于白名单（顺序无所谓，集合一致即可）
    assert set(registered) == set(ALLOWED_READ_ONLY_TOOLS)
    # 逐个确认都在注册表里
    for spec in _DEFAULT_SPECS:
        assert spec.name in reg


# ------------------------------------------------------------------
# T3：白名单里真实存在的工具名必须能正常注册（不误杀）。
# ------------------------------------------------------------------
def test_r3_real_whitelisted_tool_registers_ok():
    """③ 回归：显式注册一个确实在白名单里的真实工具名（get_reminders），不得误杀。"""
    reg = AssistantToolRegistry()
    spec = ToolSpec(
        name="get_reminders",
        description="复用的既有提醒入口，真实存在于白名单。",
        read_only=True,
        callable=_noop,
    )
    # 不应抛任何异常
    reg.register(spec)
    assert "get_reminders" in reg
