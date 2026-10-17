#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键运行 backend/tests/ 下的脚本式断言工具（非 pytest 测试）。

背景：
    backend/tests/ 下的 5 个 test_*.py 不是 pytest 用例，而是「脚本式断言工具」——
    它们没有 test_* 函数，靠 main() + sys.exit() 或模块级 sys.exit() 表达成败，
    因此 pytest 对它们收集到 0 个用例（详见同目录 README.md）。
    要验证它们，只能当作普通脚本逐个执行。

本脚本做的事：
    1. 用门禁解释器（backend/venv/Scripts/python.exe）逐个 subprocess 执行这 5 个脚本；
    2. 固定 cwd=backend/ 并注入 PYTHONPATH=backend/，保证从任意 cwd 调用都能 import app；
    3. 捕获各自 returncode 与末尾输出，解析通过/失败数；
    4. 汇总表格式打印；任一脚本非零退出 → 本脚本整体非零退出（便于将来挂 CI）。

判定原则（重要）：
    * 一律以子进程 returncode 为准，不做任何「白名单/豁免/特殊 hack」。
    * test_name_normalize_probe.py 用 sys.exit(0/1) 表达成败 —— 按 returncode 判定即天然正确。
    * test_narrative_extract.py 当前有 1 条已知红（FakeDB 缺 scalars()），
      本脚本会如实报红，绝不为了汇总好看而排除它或改判定。

用法：
    backend/venv/Scripts/python.exe backend/tests/run_legacy_asserts.py
    backend/venv/Scripts/python.exe backend/tests/run_legacy_asserts.py --tail 12
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field

# ── 路径常量：全部由 __file__ 推导，保证从任意 cwd 运行都成立 ──
HERE = os.path.dirname(os.path.abspath(__file__))            # .../backend/tests
BACKEND_DIR = os.path.dirname(HERE)                          # .../backend
REPO_ROOT = os.path.dirname(BACKEND_DIR)                     # 仓库根

# 5 个脚本式断言工具。顺序即执行顺序；不删不减，全量执行。
LEGACY_SCRIPTS: tuple[str, ...] = (
    "test_finale_clean.py",
    "test_name_normalize_probe.py",
    "test_narrative_extract.py",
    "test_style_exemplar.py",
    "test_style_warning.py",
)

# 形如「结果：33/34 通过」
RE_RESULT_RATIO = re.compile(r"结果：\s*(\d+)\s*/\s*(\d+)\s*通过")
# 形如「结果：N 项失败 -> [...]」或「结果：全部通过」
RE_RESULT_ALLPASS = re.compile(r"结果：\s*全部通过")
RE_RESULT_NFAIL = re.compile(r"结果：\s*(\d+)\s*项失败")
# 逐行前缀统计（test_name_normalize_probe.py 形态）
RE_LINE_PASS = re.compile(r"^PASS\s")
RE_LINE_FAIL = re.compile(r"^FAIL\s")

DEFAULT_TAIL_LINES = 8
SCRIPT_TIMEOUT_SEC = 900  # fit_profile 等重型断言留足时间


@dataclass
class ScriptRun:
    """单个脚本的执行结果。"""

    name: str
    returncode: int = 0
    passed: int = 0
    failed: int = 0
    counts_known: bool = False
    tail: list[str] = field(default_factory=list)
    error: str = ""


def display_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（东亚宽字符按 2 计），用于表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad_right(text: str, width: int) -> str:
    """按显示宽度右填充空格。"""
    return text + " " * max(width - display_width(text), 1)


def resolve_python_exe() -> tuple[str, str]:
    """返回 (解释器路径, 来源说明)。优先门禁 venv，缺失时降级到当前解释器。"""
    candidates = (
        os.path.join(BACKEND_DIR, "venv", "Scripts", "python.exe"),  # Windows 门禁
        os.path.join(BACKEND_DIR, "venv", "bin", "python"),          # POSIX
        os.path.join(BACKEND_DIR, "venv", "bin", "python3"),
    )
    for path in candidates:
        if os.path.isfile(path):
            return path, "backend/venv"
    return sys.executable, "sys.executable（未找到 backend/venv，已降级）"


def parse_counts(output: str) -> tuple[int, int, bool]:
    """从脚本输出中解析 (通过数, 失败数, 是否解析到)。

    优先匹配「结果：X/Y 通过」；否则逐行统计 PASS/FAIL 前缀；
    两者都匹配不到时返回 counts_known=False（表格里显示 —，不臆造数字）。
    """
    match = RE_RESULT_RATIO.search(output)
    if match:
        passed = int(match.group(1))
        total = int(match.group(2))
        return passed, max(total - passed, 0), True

    # test_name_normalize_probe.py 形态：每行以 PASS/FAIL 开头
    line_pass = 0
    line_fail = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if RE_LINE_PASS.match(line):
            line_pass += 1
        elif RE_LINE_FAIL.match(line):
            line_fail += 1
    if line_pass or line_fail:
        return line_pass, line_fail, True

    # 只有「结果：全部通过」这类总结、没有逐行前缀时的兜底
    if RE_RESULT_ALLPASS.search(output):
        # 数量未知，但结论明确：全绿
        return 0, 0, False
    match = RE_RESULT_NFAIL.search(output)
    if match:
        return 0, int(match.group(1)), False
    return 0, 0, False


def tail_lines(output: str, limit: int) -> list[str]:
    """取输出末尾的若干有效行（去掉尾部空行）。"""
    lines = [line for line in output.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines[-limit:] if limit > 0 else []


def run_one(python_exe: str, script_name: str, tail_limit: int) -> ScriptRun:
    """执行单个脚本并收集结果。"""
    script_path = os.path.join(HERE, script_name)
    result = ScriptRun(name=script_name)

    if not os.path.isfile(script_path):
        result.returncode = 1
        result.error = f"脚本不存在：{script_path}"
        return result

    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    # 把 backend/ 放进 PYTHONPATH，保证 `from app.xxx import` 可用；
    # 同时 cwd 固定为 backend/，兼容 test_name_normalize_probe.py 的 sys.path.insert(0, ".")。
    env["PYTHONPATH"] = os.pathsep.join([BACKEND_DIR] + ([existing_pp] if existing_pp else []))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.run(
            [python_exe, script_path],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCRIPT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        result.returncode = 1
        result.error = f"执行超时（>{SCRIPT_TIMEOUT_SEC}s）"
        result.tail = tail_lines(partial, tail_limit)
        return result
    except OSError as exc:
        result.returncode = 1
        result.error = f"无法启动子进程：{exc}"
        return result

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    combined = stdout + (("\n[stderr]\n" + stderr) if stderr.strip() else "")

    # returncode 可能是 None（极端情况），兜底成 1
    result.returncode = proc.returncode if proc.returncode is not None else 1
    result.passed, result.failed, result.counts_known = parse_counts(combined)
    result.tail = tail_lines(combined, tail_limit)
    return result


def print_table(results: list[ScriptRun]) -> None:
    """打印汇总表。"""
    name_w, pass_w, fail_w, code_w = 32, 6, 6, 7
    print()
    print(pad_right("脚本", name_w) + pad_right("通过", pass_w)
          + pad_right("失败", fail_w) + pad_right("退出码", code_w) + "结论")
    print("-" * 78)
    for item in results:
        passed_s = str(item.passed) if item.counts_known else "—"
        failed_s = str(item.failed) if item.counts_known else "—"
        verdict = "PASS" if item.returncode == 0 else "FAIL"
        if item.error:
            verdict = "ERROR"
        print(pad_right(item.name, name_w) + pad_right(passed_s, pass_w)
              + pad_right(failed_s, fail_w) + pad_right(str(item.returncode), code_w)
              + verdict + (f"  ({item.error})" if item.error else ""))
    print("-" * 78)
    total_p = sum(r.passed for r in results if r.counts_known)
    total_f = sum(r.failed for r in results if r.counts_known)
    failed_scripts = [r.name for r in results if r.returncode != 0]
    print(pad_right("合计", name_w) + pad_right(str(total_p), pass_w)
          + pad_right(str(total_f), fail_w)
          + pad_right("", code_w) + f"{len(results) - len(failed_scripts)}/{len(results)} 脚本通过")


def print_details(results: list[ScriptRun], tail_limit: int) -> None:
    """打印各脚本末尾输出。"""
    print()
    print(f"── 末尾输出（各取最后 {tail_limit} 行）──")
    for index, item in enumerate(results, start=1):
        print(f"\n[{index}/{len(results)}] {item.name}  exit={item.returncode}")
        if item.error:
            print(f"  ! {item.error}")
        if item.tail:
            for line in item.tail:
                print(f"  | {line}")
        else:
            print("  | （无输出）")


def main(argv: list[str] | None = None) -> int:
    """入口：跑齐 5 个脚本并汇总。返回 0 表示全部通过，否则非 0。"""
    parser = argparse.ArgumentParser(
        description="一键运行 backend/tests/ 下的脚本式断言工具（非 pytest 测试）",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=DEFAULT_TAIL_LINES,
        help=f"每个脚本展示的末尾行数（默认 {DEFAULT_TAIL_LINES}，0 表示不展示）",
    )
    args = parser.parse_args(argv)

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    python_exe, python_src = resolve_python_exe()

    print("=" * 78)
    print(" backend/tests 脚本式断言工具 · 一键运行汇总")
    print(f" 解释器 : {python_exe}  [{python_src}]")
    print(f" 工作目录: {BACKEND_DIR}  （固定 cwd，兼容 sys.path.insert(0, '.') 的脚本）")
    print(f" 脚本目录: {HERE}")
    print("=" * 78)

    results: list[ScriptRun] = []
    for script_name in LEGACY_SCRIPTS:
        results.append(run_one(python_exe, script_name, args.tail))

    print_table(results)
    if args.tail > 0:
        print_details(results, args.tail)

    failed = [r for r in results if r.returncode != 0]
    print()
    print("── 结论 ──")
    if not failed:
        print(f"全部 {len(results)} 个脚本退出码为 0 → 整体 PASS（退出码 0）")
        return 0
    print(f"{len(failed)}/{len(results)} 个脚本非零退出 → 整体 FAIL（退出码 1）：")
    for item in failed:
        reason = item.error or f"断言失败 {item.failed} 条 / 通过 {item.passed} 条"
        print(f"  - {item.name}: exit={item.returncode}  {reason}")
    print("注：失败为如实上报，本脚本不做任何豁免或排除。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
