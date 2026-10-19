"""invoke() 白名单 fail-closed 回归测试（纵深防御缺口）。

背景：架构/QA 验证发现——`register()` 有白名单校验（fail-closed，是实的），
但 `invoke()` 只有「黑名单前缀」+「read_only is False」两道校验，**没有白名单
校验**。于是绕过 `register` 直接往 `_tools` 塞一个不在白名单里的只读工具时，
`invoke` 会放行并执行它的 callable。

这不构成现网可利用漏洞（`_tools` 是私有字典，正常路径只有 `register` 一个写入
口），但它是**未来重构的雷**：一旦有人改用配置/插件填充 `_tools`，白名单会静默
失效。同时 `invoke` 的 docstring 宣称「双重门」，而那个门只覆盖黑名单、没覆盖
白名单——文档承诺与实际行为不一致。

本文件证明该缺口（先红）并锁定修复后的行为（后绿）。测试不需要数据库。
"""
import os
import sys

# 与 backend/tests/ 下其余 36 个测试文件同一套 bootstrap：本文件会被
# tests/test_legacy_script_suite.py 以**子进程脚本**方式（cwd=backend/）跑一遍并
# 断言退出码 0。直接跑脚本时 sys.path[0] 是 tests/ 而非 backend/，不补这一行
# `import app` 会 ModuleNotFoundError → 退出码 1 → 门禁红。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.agents.registry import (
    ALLOWED_READ_ONLY_TOOLS,
    AssistantToolRegistry,
    ToolSpec,
)


def test_invoke_rejects_non_allowlisted_tool_bypassing_register():
    """① 绕过 register 塞进 _tools 的非白名单只读工具：invoke 必须拒绝且不执行 callable。

    修复前（缺口存在时）：invoke 放行，callable 被执行 → 本测试 FAIL。
    修复后：invoke 抛 PermissionError，且拒绝发生在 callable 执行之前 → PASS。
    """
    calls = []

    def spy(db, project_id, **kwargs):
        """记录每一次真实调用，用于断言 callable 根本没被执行。"""
        calls.append((db, project_id, kwargs))
        return {"pwned": True}

    reg = AssistantToolRegistry()
    # 直接写私有字典，**完全绕过** register() 的白名单校验
    reg._tools["totally_fine_reader"] = ToolSpec(
        name="totally_fine_reader",
        description="不在白名单中的只读工具，用于验证 invoke 的 fail-closed。",
        read_only=True,
        callable=spy,
    )

    with pytest.raises(PermissionError):
        reg.invoke("totally_fine_reader", db=None, project_id=1)

    # 关键：拒绝必须发生在 callable 执行**之前**——一次都没被调用过
    assert calls == []


def test_invoke_still_allows_whitelisted_tool():
    """② 正向对照：白名单内的真实工具仍可正常 invoke（防止新校验误杀）。"""
    calls = []

    def ok_tool(db, project_id, **kwargs):
        calls.append(project_id)
        return {"ok": True}

    name = "get_reminders"  # 白名单内真实存在的工具名
    assert name in ALLOWED_READ_ONLY_TOOLS

    reg = AssistantToolRegistry()
    reg._tools[name] = ToolSpec(
        name=name,
        description="白名单内真实工具，验证 invoke 不被新校验误杀。",
        read_only=True,
        callable=ok_tool,
    )

    out = reg.invoke(name, db=None, project_id=1)
    assert out == {"ok": True}
    assert calls == [1]
