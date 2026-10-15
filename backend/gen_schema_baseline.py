"""生成 schema 基线快照：`backend/app/schema_baseline.json`。

## 为什么需要它

「某列是建表就有的基础列，还是后来加的」——这个信息**只存在于 git 历史里**。
没有基线，就没法自动判断 `db.py` 的迁移清单是否漏项。

于是把**迁移体系建立之前**的 schema 抽成一份可版本化的 JSON 基线：
`schema_baseline.json` = 首次引入数据层那次提交（`git log --diff-filter=A
-- backend/app/models.py` 的最早一次）的全部 (表, 列)。

之后 `tests/test_schema_migration_completeness.py` 用它算差集：
    当前 schema − 基线 = 历史上新增过的列
这些列**每一项都必须在迁移清单里**，否则老库升级就会 `no such column`。

## 何时需要重跑

只有当「基线本身」需要重设时才重跑（例如决定把某个时间点作为新的起点）。
日常加列**不需要**——加列只要求改 `db.py` 的 migrations 清单，基线保持不变，
这样差集才是「相对起点的全部增量」，漏项才会被自动发现。

## 用法

    python backend/gen_schema_baseline.py            # 打印，不写
    python backend/gen_schema_baseline.py --write    # 写入 schema_baseline.json
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MODELS = "backend/app/models.py"


def first_models_commit() -> str:
    out = subprocess.run(
        ["git", "log", "--diff-filter=A", "--reverse", "--format=%H", "--", MODELS],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    commits = [c for c in out.stdout.split() if c.strip()]
    if not commits:
        raise SystemExit(f"找不到 {MODELS} 的首次提交")
    return commits[0]


def parse_columns(src: str) -> dict[str, list[str]]:
    """从 models.py 源码抽 {表名: [列名]}。

    刻意用正则而不是 import 那个历史版本：老版本可能与当前依赖不兼容，
    import 会失败；源码里的 `__tablename__ = "x"` + `name = Column(...)`
    这个形态在本项目一直稳定。
    """
    tables: dict[str, list[str]] = {}
    cur: str | None = None
    pending: list[str] = []
    for line in src.splitlines():
        m = re.search(r'__tablename__\s*=\s*"(\w+)"', line)
        if m:
            cur = m.group(1)
            tables.setdefault(cur, [])
            continue
        if cur is None:
            continue
        m2 = re.match(r"\s{4}(\w+)\s*=\s*Column\(", line)
        if m2:
            tables[cur].append(m2.group(1))
    return {t: cols for t, cols in tables.items() if cols}


def main() -> int:
    commit = first_models_commit()
    src = subprocess.run(
        ["git", "show", f"{commit}:{MODELS}"],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    if not src:
        raise SystemExit(f"读不到 {commit}:{MODELS}")
    tables = parse_columns(src)
    total = sum(len(v) for v in tables.values())
    print(f"基线提交 {commit[:8]}：{len(tables)} 张表 / {total} 列")
    for t, cols in sorted(tables.items()):
        print(f"  {t}: {len(cols)} 列")

    out = os.path.join(HERE, "app", "schema_baseline.json")
    payload = {
        "_comment": "迁移体系建立前的 schema；加列时不要重跑本脚本，只改 db.py 迁移清单",
        "_baseline_commit": commit,
        "tables": tables,
    }
    if "--write" in sys.argv:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        print(f"\n已写入 {out}")
    else:
        print("\n（加 --write 才落盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
