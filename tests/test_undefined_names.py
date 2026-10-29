"""闸门：后端代码里不许有**未定义名**（2026-10-30 加）。

==============================================================================
为什么加这条
==============================================================================

真模型验收跑一次就抓到三个「跑到某一步才炸」的洞，全是同一类：
**用了一个根本没定义的名字**。

- `engine.py` 每第 10 段：`NameError: project_id`（那个作用域里只有 `project`）
- `cultivation.py`：`if raise_errors:` —— 这个参数在签名里就不存在
- `narrative.py`：`psych_ratio` —— 从没取过，只在用到的那一行被解释器去找

它们全都躲过了类型检查、单测与 CI ⇒ 因为**它们只在特定路径上才被执行到**。

⇒ 把这类洞变成**静态可查**的：pyflakes 的「undefined name」检查。
它不需要运行任何代码 ⇒ 全仓扫一遍的成本是秒级。

## 为什么只看「undefined name」这一类

pyflakes 还会报「导入未使用」「f-string 没占位符」「重复定义」等 —— 那些是
整洁度问题，不该挡住 CI（存量还不少，一次性清理会把 diff 撑爆）。
**只拦会真炸的那一类**。

## 依赖

`backend/requirements-dev.txt` 里有 pyflakes；CI 装的就是 requirements-dev.txt。
没装时这条用例**跳过**而不是失败 —— 本机没装工具不等于代码有洞。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "backend" / "app"

#: 允许存在的「未定义名」——每一条都要写清为什么它无害。
#:
#: ⚠️ 往这里加一条之前先问：**它真的不会在运行时被求值吗？**
#:    字符串注解（`"Foo | None"`）在运行时不求值 ⇒ 无害；
#:    裸名字在任何执行到它的分支里都会炸 ⇒ 有害，必须修。
ALLOWED: dict[str, str] = {}


def _pyflakes_undefined() -> list[str] | None:
    """跑 pyflakes，返回「undefined name」的输出行；工具没装则返回 None。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pyflakes", str(APP)],
            cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    hits = []
    for line in out.splitlines():
        if "undefined name" not in line:
            continue
        # 形如 backend/app/x.py:12:3: undefined name 'foo'
        hits.append(line.strip())
    return hits


def test_backend_has_no_undefined_names():
    hits = _pyflakes_undefined()
    if hits is None:
        import pytest
        pytest.skip("没装 pyflakes（backend/requirements-dev.txt）")
    bad = []
    for line in hits:
        if any(allow in line for allow in ALLOWED):
            continue
        bad.append(line)
    assert not bad, (
        "后端有未定义名（运行到就会炸）：\n  "
        + "\n  ".join(bad)
        + "\n\n这类洞只有真跑才会暴露（类型检查与单测都抓不到），"
          "所以在这里静态拦住。"
    )
