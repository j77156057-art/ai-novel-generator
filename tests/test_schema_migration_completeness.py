"""迁移完整性护栏：相对基线新增的每一列，都必须在迁移清单里。

## 要防的 bug

`models.py` 加了新列但 `db.py` 的迁移清单忘了加 → `create_all` 只给**新库**
建该列，**已存在的库永远补不上** → 任何 SELECT 都 `no such column`。

真实事故：`93a6337` 给 `Project` 加了 `style_narrative` 但没加迁移项，
在用户那份 123MB 真实库上直接炸：

    sqlite3.OperationalError: no such column: projects.style_narrative

而当时 183 项测试全绿——它们都建全新库，`create_all` 把 bug 掩盖了。

## 怎么做到「自动发现漏项」

「某列是建表就有的基础列，还是后来加的」这个信息**只存在于 git 历史里**，
单测自己拿不到。于是把迁移体系建立之前的 schema 抽成可版本化的基线
`backend/schema_baseline.json`（由 `gen_schema_baseline.py` 从首次引入
数据层的提交自动生成），测试用它算差集：

    当前 schema − 基线 = 历史上所有「后加的列」

这些列每一项都必须在迁移清单里——漏一项，测试就红。这与「测试里写死
某几个列名」的本质区别是：**下次加列忘了改清单，同样会被抓住**，
不需要人记得去改测试。

## 为什么不用「真的 DROP COLUMN 再跑迁移」那种测法

那种测法只能删「清单里已有的列」，清单漏掉的列根本不会被删 → 永远测不出
漏项（我第一版就是这么写的，被自己的变异检查判成假测试，已弃用）。
它测的是「机制有效」，不是「清单完整」。这里改成静态差集，才真正对得上
要防的东西。
"""
from __future__ import annotations

import json
import os
import re

import app.db as _dbmod
from app.db import Base

import app.models  # noqa: F401  确保模型注册进 Base.metadata

_BASELINE = os.path.join(os.path.dirname(_dbmod.__file__), "schema_baseline.json")


def _listed_migration_columns() -> set[tuple[str, str]]:
    src = open(_dbmod.__file__, encoding="utf-8").read()
    assert "migrations = [" in src, "db.py 里找不到 migrations 清单，结构变了？"
    # ⚠️ 必须截到 migrations 列表自己的闭合 `]`：此前取 `split(...)[-1]` 一直读到文件尾，
    # 而 db.py 在其后新增了 `v30_indexes = [...]`（(表, 索引名, 列SQL, 说明)），
    # 于是索引被当成「列」解析出来 → 本用例误报 14 个 ghost column。
    # 这里只取 migrations 列表体，索引由 db.py 的另一条路径负责，不属本断言范围。
    body = src.split("migrations = [", 1)[1].split("\n    ]", 1)[0]
    return set(re.findall(r'\(\s*"(\w+)"\s*,\s*"(\w+)"', body))


def _baseline_columns() -> tuple[set[tuple[str, str]], set[str]]:
    """返回 ({基线已有的 (表,列)}, {基线已有的表})。

    表也要返回：基线之后**新建的表**由 `create_all` 建表建列，不需要迁移
    项——只有「基线就存在的表」上新加的列才必须进清单。把两类混在一起会
    误报所有新表（实测会误报 53 项），那样测试就会被当成噪音关掉。
    """
    with open(_BASELINE, encoding="utf-8") as f:
        data = json.load(f)
    cols = {(t, c) for t, cs in data["tables"].items() for c in cs}
    return cols, set(data["tables"])


def _current_columns() -> set[tuple[str, str]]:
    return {(t.name, c.name) for t in Base.metadata.sorted_tables for c in t.columns}


def test_columns_added_since_baseline_are_all_in_migration_list():
    """核心护栏：基线已有的表上新加的列，一项都不许漏在迁移清单之外。"""
    baseline, baseline_tables = _baseline_columns()
    current = _current_columns()
    listed = _listed_migration_columns()

    # 只看「基线就存在的表」上新加的列。基线之后新建的表由 create_all 负责，
    # 不该要求迁移项。
    added = {(t, c) for t, c in current - baseline if t in baseline_tables}
    assert added, (
        "算不出「基线已有的表上新加的列」——schema_baseline.json 可能不是迁移"
        f"体系之前的那份。（基线 {len(baseline)} 列 / 当前 {len(current)} 列 / "
        f"清单 {len(listed)} 项）"
    )

    missing = sorted(added - listed)
    assert not missing, (
        f"这些列是基线之后新增的，但 `db.py` 的 migrations 清单里没有 → "
        f"已存在的库升级后会 `no such column`：{missing}。"
        "修法：在 db.py 的 migrations 里加对应 (表, 列, 类型) 三元组。"
    )


def test_migration_list_has_no_typos():
    """反向检查：清单里的 (表, 列) 必须真实存在于模型里。

    防的是「清单写了但列名拼错」——那种漏项会以另一种形式炸：
    ALTER 一个不存在的列名，SQLite 直接报错，老库永远升不了级。
    """
    current = _current_columns()
    listed = _listed_migration_columns()
    ghost = sorted(listed - current)
    assert not ghost, f"迁移清单里有模型并不存在的列，ALTER 会失败：{ghost}"


def test_declared_columns_report():
    """可读诊断：打印新增列是否都在清单里，供人工核对。不作断言。"""
    baseline, baseline_tables = _baseline_columns()
    current = _current_columns()
    listed = _listed_migration_columns()
    added = sorted({(t, c) for t, c in current - baseline if t in baseline_tables})
    new_tables = sorted({t for t, _ in current - baseline} - baseline_tables)
    print(f"\n基线 {len(baseline)} 列 / 当前 {len(current)} 列 / 迁移清单 {len(listed)} 项")
    print(f"  基线已有表上新加的列（必须在清单内）: {len(added)}")
    for t, c in added:
        print(f"    + {t}.{c}  {'在清单' if (t, c) in listed else '漏'}")
    print(f"  基线之后新建的表（create_all 负责，无需清单）: {len(new_tables)}"
          f" → {new_tables}")
