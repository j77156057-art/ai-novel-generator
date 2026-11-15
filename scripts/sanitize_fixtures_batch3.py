#!/usr/bin/env python3
"""第三批测试夹具脱敏：保留结论、剥掉文本。

处理三个仍带真实文本的夹具，把人物、地点、道具说法换成同结构的合成场景：

1. ``backend/tests/fixtures/extractor_v2_cases.json`` 第 1 例
   （原名「南宫爵+安小落（项目 20 风格，3 字名+对白前置）」）
2. ``tests/fixtures/closed_inventory_valid_constraints.json``
3. ``tests/fixtures/closed_inventory_invalid_constraint.json``

脱敏口径与前两批一致：**判定所依赖的特征一个不丢**。

- 抽取器用例：仍是「3 字名 + 对白前置」，仍检查 2 字前缀不进角色表；
- 封闭清单：``kind`` / ``mode`` / ``source_id``（C1S096、C1S090）/
  ``covered_source_ids`` / 第 1 至 48 页 / 「仅两」「清单完整」「不增添」
  「地点始终」「均不得在本节点使用」等封闭措辞全部保留；
- 只改字面值，不改任何测试断言。

脚本按字面替换实现，重复执行是幂等的：已脱敏的文件会直接跳过。

用法::

    python scripts/sanitize_fixtures_batch3.py            # 就地脱敏
    python scripts/sanitize_fixtures_batch3.py --check    # 只检查，有待改则退出码 1
    python scripts/sanitize_fixtures_batch3.py --root DIR # 对另一份副本执行（验证用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXTRACTOR_CASE = "backend/tests/fixtures/extractor_v2_cases.json"
CLOSED_VALID = "tests/fixtures/closed_inventory_valid_constraints.json"
CLOSED_INVALID = "tests/fixtures/closed_inventory_invalid_constraint.json"

# ---------------------------------------------------------------------------
# 1. 抽取器用例：3 字名 + 对白前置（结构不变，人名与场景换成合成文本）
# ---------------------------------------------------------------------------

NEW_CASE_NAME = "裴明川+温以宁（合成场景，3 字名+对白前置）"
NEW_CASE_TEXT = (
    "“灯？”裴明川重复了一遍这个字。\n\n"
    "温以宁抬眼，视线落在他脸上。\n\n"
    "“要添灯油，自己去添。”裴明川又说了一遍。\n\n"
    "温以宁抿了抿唇，指尖收进袖口。\n\n"
    "裴明川看着她。温以宁别过头去。"
)
NEW_CASE_CHARACTERS = ["裴明川", "温以宁"]
NEW_CASE_NO_NOISE = ["裴明", "温以"]

# ---------------------------------------------------------------------------
# 2/3. 封闭清单：人物、地点、道具换名；数字、来源 ID、封闭措辞不动
# ---------------------------------------------------------------------------

#: 顺序重要：先替换长串（含地点全称、图册全称），再替换短串。
CLOSED_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("社区图书室修复桌旁", "镇立档案室裱糊桌旁"),
    ("社区图书室", "镇立档案室"),
    ("一本封面松脱但第1至48页齐全的旧植物图册", "一本封面松脱但第1至48页齐全的旧测绘簿"),
    ("旧植物图册", "旧测绘簿"),
    ("管理员林夏", "管理员陆苓"),
    ("志愿者陈舟", "志愿者程屿"),
    ("林夏", "陆苓"),
    ("陈舟", "程屿"),
    ("图册", "测绘簿"),
    ("无酸纸", "衬纸"),
    ("浆糊", "糨糊"),
    ("软刷", "排刷"),
    ("登记本", "登记簿"),
)

OLD_MARKERS = ("林夏", "陈舟", "社区图书室", "旧植物图册", "无酸纸")
NEW_MARKERS = ("陆苓", "程屿", "镇立档案室", "旧测绘簿", "衬纸")


class SanitizeError(RuntimeError):
    """夹具形态与预期不符，拒绝盲改。"""


def _read(path: Path) -> str:
    # 用 bytes 读入，避免 read_text 的换行归一化改动文件换行。
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    hits = text.count(old)
    if hits != 1:
        raise SanitizeError(f"{label}: 期望字面出现 1 次，实际 {hits} 次：{old[:40]!r}")
    return text.replace(old, new, 1)


def sanitize_extractor_case(path: Path, *, check: bool) -> str:
    text = _read(path)
    cases = json.loads(text)
    if not isinstance(cases, list) or not cases:
        raise SanitizeError(f"{path}: 期望非空数组")

    first = cases[0]
    if first.get("name") == NEW_CASE_NAME and first.get("text") == NEW_CASE_TEXT:
        return "already"

    fragments = (
        (json.dumps(first["name"], ensure_ascii=False),
         json.dumps(NEW_CASE_NAME, ensure_ascii=False), "case.name"),
        (json.dumps(first["text"], ensure_ascii=False),
         json.dumps(NEW_CASE_TEXT, ensure_ascii=False), "case.text"),
        (json.dumps(first["expect_characters"], ensure_ascii=False),
         json.dumps(NEW_CASE_CHARACTERS, ensure_ascii=False), "case.expect_characters"),
        (json.dumps(first["expect_no_noise"], ensure_ascii=False),
         json.dumps(NEW_CASE_NO_NOISE, ensure_ascii=False), "case.expect_no_noise"),
    )
    for old, new, label in fragments:
        text = _replace_once(text, old, new, label=label)

    parsed = json.loads(text)  # 改完必须是合法 JSON
    if parsed[0]["name"] != NEW_CASE_NAME or parsed[0]["text"] != NEW_CASE_TEXT:
        raise SanitizeError(f"{path}: 脱敏后回读不一致")
    if len(parsed) != len(cases):
        raise SanitizeError(f"{path}: 用例数量被改动（{len(cases)} -> {len(parsed)}）")

    if check:
        return "would-update"
    _write(path, text)
    return "updated"


def _validate_closed_payload(path: Path, text: str, *, expect_source: str,
                             expect_kinds: list[str]) -> None:
    outer = json.loads(text)
    if set(outer) != {"raw"}:
        raise SanitizeError(f"{path}: 外层应只有 raw 键，实际 {sorted(outer)}")
    inner = json.loads(outer["raw"])
    constraints = inner.get("constraints")
    if not isinstance(constraints, list) or not constraints:
        raise SanitizeError(f"{path}: 期望非空 constraints")

    kinds = [c.get("kind") for c in constraints]
    if kinds != expect_kinds:
        raise SanitizeError(f"{path}: kind 序列被改动：{kinds}")
    if {c.get("source_id") for c in constraints} != {expect_source}:
        raise SanitizeError(f"{path}: source_id 被改动")
    if any(c.get("mode") != "closed" for c in constraints):
        raise SanitizeError(f"{path}: mode 被改动")

    joined = "".join(c["quote"] for c in constraints)
    if "第1至48页" not in joined:
        raise SanitizeError(f"{path}: 48 页数字特征丢失")
    if any(marker in text for marker in OLD_MARKERS):
        raise SanitizeError(f"{path}: 仍残留旧文本")

    if expect_source == "C1S096":
        needed = ("仅两", "地点始终", "清单完整", "不增添")
    else:
        needed = ("始终在", "尚未检查", "不得在本节点使用")
    missing = [needle for needle in needed if needle not in joined]
    if missing:
        raise SanitizeError(f"{path}: 封闭/矛盾措辞丢失：{missing}")


def sanitize_closed_fixture(path: Path, *, check: bool, expect_source: str,
                            expect_kinds: list[str]) -> str:
    text = _read(path)
    has_old = any(marker in text for marker in OLD_MARKERS)

    if not has_old and "镇立档案室" in text:
        _validate_closed_payload(path, text, expect_source=expect_source,
                                 expect_kinds=expect_kinds)
        return "already"
    if has_old and "镇立档案室" in text:
        raise SanitizeError(f"{path}: 新旧文本混在一起，拒绝继续")
    if not has_old:
        raise SanitizeError(f"{path}: 找不到任何旧文本锚点")

    new_text = text
    for old, new in CLOSED_REPLACEMENTS:
        new_text = new_text.replace(old, new)

    _validate_closed_payload(path, new_text, expect_source=expect_source,
                             expect_kinds=expect_kinds)
    if check:
        return "would-update"
    _write(path, new_text)
    return "updated"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="第三批测试夹具脱敏（保留结论、剥掉文本）")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="仓库根目录（默认：本脚本上一级）")
    parser.add_argument("--check", action="store_true",
                        help="只检查是否需要脱敏；有待改时退出码 1")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    jobs = [
        (root / EXTRACTOR_CASE, lambda p: sanitize_extractor_case(p, check=args.check)),
        (root / CLOSED_VALID, lambda p: sanitize_closed_fixture(
            p, check=args.check, expect_source="C1S096",
            expect_kinds=["person", "place", "object"])),
        (root / CLOSED_INVALID, lambda p: sanitize_closed_fixture(
            p, check=args.check, expect_source="C1S090",
            expect_kinds=["object"])),
    ]

    pending = 0
    for path, job in jobs:
        if not path.exists():
            print(f"[MISSING] {path}")
            return 2
        try:
            status = job(path)
        except SanitizeError as exc:
            print(f"[FAIL] {path}\n       {exc}")
            return 1
        if status == "would-update":
            pending += 1
        print(f"[{status.upper()}] {path}")

    if args.check and pending:
        print(f"待脱敏 {pending} 个文件")
        return 1
    print("完成。" + ("（--check 模式，未写盘）" if args.check else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
