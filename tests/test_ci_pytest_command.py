"""CI 的 pytest 命令必须**看得见结尾汇总行**（2026-10-28 加）。

## 为什么要有这条

CI 日志里本来**看不到**「1549 passed, 9 skipped in 123s」那行 ——
只剩一串点和 SKIPPED 列表，然后直接 `Post job cleanup`。
读者（和以后的我）无法从日志判断「到底跑了多少条」。

根因不是 PowerShell 吞最后一行（那是本机的坑），是**命令多带了一个 `-q`**：

- `pytest.ini` 的 `addopts` 里**已经有** `-q`；
- 命令行再加一个 `-q` ⇒ 实际是 `-qq` ⇒ pytest **静默吞掉结尾的汇总行**；
- 去掉命令行那个 `-q`（沿用 addopts 里的）⇒ 汇总行就回来了。

⇒ 把这条变成**机器能验的不变量**：CI 与文档里的命令不许再出现多余的 `-q`。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
PYTEST_INI = REPO / "pytest.ini"
README = REPO / "README.md"


def _ci_pytest_commands() -> list[str]:
    text = CI_YML.read_text(encoding="utf-8")
    return re.findall(r"run:\s*(python -m pytest[^\n#]*)", text)


def test_ci_uses_pytest_at_all():
    cmds = _ci_pytest_commands()
    assert cmds, "CI 里找不到 pytest 命令 —— 工作流被改坏了吗？"


def test_ci_pytest_command_has_no_extra_q():
    """命令行不能多带 `-q`（会与 addopts 里的叠加成 -qq ⇒ 汇总行被吞）。"""
    for cmd in _ci_pytest_commands():
        assert "-qq" not in cmd, cmd
        # 只允许出现一个 -q 形态的参数；addopts 已经给了一个
        assert not re.search(r"(?<![\w-])-q(?=\s|$)", cmd), (
            f"CI 的 pytest 命令多带了一个 -q：{cmd}\n"
            "pytest.ini 的 addopts 里已经有 -q ⇒ 变成 -qq ⇒ 结尾汇总行被静默吞掉。"
        )


def test_ci_runs_the_same_command_as_readme():
    """CI 跑的就是 README「跑测试」那一节写的命令 —— 两边不许各写一套。"""
    readme = README.read_text(encoding="utf-8")
    m = re.search(r"python -m pytest tests/.*", readme)
    assert m, "README 里找不到跑测试的命令"
    readme_cmd = m.group(0).split("#")[0].strip()
    ci_cmds = [c.strip() for c in _ci_pytest_commands()]
    assert readme_cmd in ci_cmds, (
        f"README 写的命令 {readme_cmd!r} 与 CI 的 {ci_cmds!r} 不一致 —— "
        "CI 注释里写着「跑的就是 README 那一节的命令」，别让它们分叉。"
    )


def test_pytest_ini_still_has_quiet_addopts():
    """`-q` 必须留在 pytest.ini 里（它还负责 --collect-only 的逐文件计数格式）。"""
    text = PYTEST_INI.read_text(encoding="utf-8")
    m = re.search(r"^addopts\s*=\s*(.+)$", text, re.M)
    assert m, "pytest.ini 里没有 addopts"
    assert "-q" in m.group(1), m.group(1)
