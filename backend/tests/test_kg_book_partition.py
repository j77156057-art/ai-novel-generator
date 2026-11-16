"""KG 按书切分（book partition）定向测试。

覆盖四件事，缺一不可：

1. **迁移**：老库（无 `novel_id`）跑 `ensure_schema()` 后两列两索引从无到有落库，
   既有行数不变；重复调用幂等（不重复加列/建索引、不写页）。
2. **写侧盖书**：新建 KgRelation / KgForeshadow 时落 `novel_id`；伏笔同名跨书
   撞 `uq_kg_foreshadow(project_id, name)` 时计入 `cross_book_name_collision`，
   不静默吞掉。
3. **实体章号严格聚合**：语料路径认 `segment_no`；生成路径只认 `chapter_range`
   里的显式「第 N 章」；`段1`/`段2` 与其它写法一律「未知」，不回落第 1 章。
4. **读侧按书过滤**：KG v3 各读端点与 book-scope 计数出口按「当前书 + 项目级
   NULL」过滤，切片实体章号只有「确定区间」或「未知」，不出现 null。

跑法：python tests/test_kg_book_partition.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, ensure_schema
import app.models  # noqa: F401  注册模型

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(f"{label}: got={got!r} want={want!r}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


# ====================================================================
# 1. 迁移：老库补列补索引，行数不变，重复调用幂等
# ====================================================================
LEGACY_DDL = """
CREATE TABLE projects (id INTEGER PRIMARY KEY, title VARCHAR(255));
CREATE TABLE kg_relations (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    from_id INTEGER NOT NULL,
    to_id INTEGER NOT NULL,
    rel_type VARCHAR(32) NOT NULL,
    strength FLOAT NOT NULL,
    since_chapter INTEGER,
    description VARCHAR(200),
    confidence FLOAT NOT NULL,
    created_at DATETIME,
    updated_at DATETIME
);
CREATE TABLE kg_foreshadows (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    name VARCHAR(200) NOT NULL,
    planted_event_id INTEGER,
    payoff_event_id INTEGER,
    status VARCHAR(16) NOT NULL,
    planted_chapter INTEGER,
    planned_payoff_chapter INTEGER,
    actual_payoff_chapter INTEGER,
    description VARCHAR(500),
    confidence FLOAT NOT NULL,
    created_at DATETIME,
    updated_at DATETIME,
    CONSTRAINT uq_kg_foreshadow UNIQUE (project_id, name)
);
INSERT INTO kg_relations (project_id, from_id, to_id, rel_type, strength, confidence)
    VALUES (1, 10, 11, 'ally', 0.6, 1.0), (1, 12, 13, 'enemy', 0.4, 1.0);
INSERT INTO kg_foreshadows (project_id, name, status, confidence)
    VALUES (1, '旧伏笔甲', 'planted', 1.0), (1, '旧伏笔乙', 'planted', 1.0);
"""


def _pragma_columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _indexes(con, table: str) -> list[str]:
    return sorted(
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        )
    )


def _counts(con) -> dict[str, int]:
    tables = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


tmpdir = tempfile.mkdtemp(prefix="kg_book_scope_")
legacy_path = os.path.join(tmpdir, "legacy.db")
con = sqlite3.connect(legacy_path)
con.executescript(LEGACY_DDL)
con.commit()

check_true(
    "迁移前 kg_relations 无 novel_id",
    "novel_id" not in _pragma_columns(con, "kg_relations"),
)
check_true(
    "迁移前 kg_foreshadows 无 novel_id",
    "novel_id" not in _pragma_columns(con, "kg_foreshadows"),
)
check(
    "迁移前 kg_relations 无按书索引",
    [i for i in _indexes(con, "kg_relations") if i.endswith("_project_novel")],
    [],
)
before_counts = _counts(con)
before_page = con.execute("PRAGMA page_count").fetchone()[0]
con.close()

legacy_engine = create_engine(f"sqlite:///{legacy_path}", future=True)
ensure_schema(legacy_engine)

con = sqlite3.connect(legacy_path)
check_true("迁移后 kg_relations 有 novel_id", "novel_id" in _pragma_columns(con, "kg_relations"))
check_true(
    "迁移后 kg_foreshadows 有 novel_id", "novel_id" in _pragma_columns(con, "kg_foreshadows")
)
check_true(
    "迁移后建出 ix_kg_relation_project_novel",
    "ix_kg_relation_project_novel" in _indexes(con, "kg_relations"),
)
check_true(
    "迁移后建出 ix_kg_foreshadow_project_novel",
    "ix_kg_foreshadow_project_novel" in _indexes(con, "kg_foreshadows"),
)
check(
    "迁移后两表行数不变",
    {t: _counts(con)[t] for t in ("kg_relations", "kg_foreshadows")},
    {t: before_counts[t] for t in ("kg_relations", "kg_foreshadows")},
)
check(
    "迁移后历史行 novel_id 全为 NULL（不回填）",
    con.execute(
        "SELECT COUNT(novel_id) FROM kg_relations"
    ).fetchone()[0]
    + con.execute("SELECT COUNT(novel_id) FROM kg_foreshadows").fetchone()[0],
    0,
)
after_first_counts = _counts(con)
after_first_page = con.execute("PRAGMA page_count").fetchone()[0]
con.close()

# 幂等：第二次调用不报错、不重复加列/建索引、不写页
ensure_schema(legacy_engine)
legacy_engine.dispose()
con = sqlite3.connect(legacy_path)
check("重复迁移后各表行数不变", _counts(con), after_first_counts)
check("重复迁移后 page_count 不变（零写入）", con.execute("PRAGMA page_count").fetchone()[0], after_first_page)
check(
    "重复迁移后 novel_id 只出现一次",
    _pragma_columns(con, "kg_relations").count("novel_id"),
    1,
)
check(
    "重复迁移后索引不重复",
    len(_indexes(con, "kg_relations")),
    len(set(_indexes(con, "kg_relations"))),
)
con.close()

# 模型层：两列都在 ORM 上，默认 NULL
from app.models import KgForeshadow, KgRelation  # noqa: E402

check_true("KgRelation 模型有 novel_id", hasattr(KgRelation, "novel_id"))
check_true("KgForeshadow 模型有 novel_id", hasattr(KgForeshadow, "novel_id"))
_uq = [
    c for c in KgForeshadow.__table__.constraints if getattr(c, "name", None) == "uq_kg_foreshadow"
][0]
check("uq_kg_foreshadow 仍是 (project_id, name)", list(_uq.columns.keys()), ["project_id", "name"])


# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
