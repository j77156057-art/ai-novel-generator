"""README 里写的用例数，必须与仓库里**真实收集到**的条数一致（2026-10-27 加）。

## 为什么要有这条

上一轮发布体检发现：README 从上到下三处都写「**1,492** 项 pytest 用例」，
实测基线是 **1,461** —— **偏差 31 条，方向是「文档比实际多」**。
那种数字靠人记着改，早晚对不上（本项目已经因此错过一次）。

⇒ 把「文档里的数字」变成**机器能验的不变量**：
谁改了测试而忘了改 README，这条测试就红。

## 怎么取两个数

1. **真实条数**：用**子进程**跑 `pytest --collect-only -q`，取
   「N tests collected」。在 Python 进程里取输出，**不经 PowerShell** ——
   所以汇总行不会被吞（本项目的踩坑姿势：PowerShell 会吞掉 native 命令的最后一行）。
2. **README 里写的数**：正则抠出所有「N 项用例 / N 项 pytest 用例 / N 项测试」。
   **抠出几处就都要相等**（三处写的不是一个数，本身也是错）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"

#: README 里「**后端**用例数」的三种写法。
#:
#: 只认「后端 pytest 用例数」—— 前端也有「N 项测试」（组件级 26 项、纯逻辑 38 项），
#: 那是**另一套数**，不能混进来。
_PATTERNS = (
    re.compile(r"(\d{1,3}(?:,\d{3})*)\s*项用例"),          # 「# 1,498 项用例（…）」
    re.compile(r"(\d{1,3}(?:,\d{3})*)\s*项\s*pytest\s*用例"),  # 「**1,498** 项 pytest 用例」
    re.compile(r"后端\s+(\d{1,3}(?:,\d{3})*)\s*项测试"),      # 「后端 **1,498** 项测试」
)


def _readme_counts() -> list[tuple[int, str]]:
    text = README.read_text(encoding="utf-8")
    # 去掉 Markdown 强调标记，免得「**1,498** 项」这种写法对不上正则
    plain = text.replace("*", "").replace("_", " ")
    out: list[tuple[int, str]] = []
    for pat in _PATTERNS:
        for m in pat.finditer(plain):
            line = plain.count("\n", 0, m.start()) + 1
            out.append((int(m.group(1).replace(",", "")), f"README.md:{line}"))
    return out


def _collected_count() -> int:
    """真实收集到的条数。

    **不用汇总行**：`pytest.ini` 里已有 `addopts = -ra -q`，再传一个 `-q` 会变成 `-qq`，
    汇总行（"N tests collected"）被**静默吞掉** ⇒ 改成**逐文件求和**
    （输出形如 `tests/test_x.py: 23`）。汇总行只当兜底。
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    per_file = re.findall(r"^tests/[^:]+\.py:\s*(\d+)\s*$", proc.stdout, re.M)
    if per_file:
        return sum(int(n) for n in per_file)
    m = re.search(r"(\d+) tests? collected", proc.stdout)
    if m:
        return int(m.group(1))
    raise AssertionError(
        "拿不到收集到的用例数（pytest --collect-only 没输出逐文件计数，也没输出汇总行）\n"
        f"stdout 末尾:\n{proc.stdout[-800:]}\n"
        f"stderr 末尾:\n{proc.stderr[-800:]}\n"
        f"exit={proc.returncode}"
    )


def test_readme_test_count_matches_reality():
    """README 写的用例数 == pytest 真实收集到的条数，且几处写法彼此一致。"""
    written = _readme_counts()
    assert written, "README 里一个用例数都没找到 —— 正则失效了，先修正则"

    numbers = {n for n, _ in written}
    assert len(numbers) == 1, (
        "README 里的用例数几处不一致："
        + ", ".join(f"{n}（{where}）" for n, where in written)
    )

    real = _collected_count()
    claimed = numbers.pop()
    assert claimed == real, (
        f"README 写的用例数（{claimed}）与实际收集到的（{real}）对不上。\n"
        "改完测试记得改 README —— 或者干脆重跑一次 pytest 数一遍。\n"
        "数法：`pytest --collect-only -q` 的汇总行"
        "（在 Python 进程里取，别用 PowerShell 的管道，它会吞最后一行）。"
    )
