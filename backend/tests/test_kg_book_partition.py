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
# 3. 实体章号严格聚合：只认 segment_no（语料）/ 显式「第 N 章」（生成）
# ====================================================================
from app.kg.entity_chapters import (  # noqa: E402
    apply_plan,
    build_plan,
    chapter_of_label,
    format_chapter_range,
    summarize,
)

check("章号：第9章《大勇知道了》", chapter_of_label("第9章《大勇知道了》"), 9)
check("章号：第 12 章", chapter_of_label("第 12 章"), 12)
check("章号：段1 不认", chapter_of_label("段1"), None)
check("章号：段2 不认", chapter_of_label("段2"), None)
check("章号：空串", chapter_of_label(""), None)
check("章号：None", chapter_of_label(None), None)
check("章号：第0章 非法", chapter_of_label("第0章"), None)
check("展示：区间", format_chapter_range(1, 3137), "第1–3137章")
check("展示：单章", format_chapter_range(8, 8), "第8章")
check("展示：未知", format_chapter_range(None, None), "章号未知")
check("展示：半缺", format_chapter_range(3, None), "章号未知")

chap_path = os.path.join(tmpdir, "chapters.db")
chap_engine = create_engine(f"sqlite:///{chap_path}", future=True)
Base.metadata.create_all(bind=chap_engine)
Session = sessionmaker(bind=chap_engine, future=True)
db = Session()

from app.models import (  # noqa: E402
    Generation, KgEntity, KgMention, Project,
)

db.add(Project(id=1, title="章号测试"))
db.add_all([
    Generation(id=1, project_id=1, segment_no=1, chapter_range="第3章《甲》"),
    Generation(id=2, project_id=1, segment_no=2, chapter_range="第5章"),
    Generation(id=3, project_id=1, segment_no=3, chapter_range="段1"),
])
db.flush()
db.add_all([
    # 语料路径：段号即章号
    KgEntity(id=1, project_id=1, novel_id=5, kind="character",
             canonical_name="语料角色", first_chapter=None, last_chapter=None),
    # 生成路径：只认显式标签，段1 不计
    KgEntity(id=2, project_id=1, novel_id=None, kind="character",
             canonical_name="生成角色", first_chapter=None, last_chapter=None),
    # 两条路径都有：带书实体只吃本书语料 → [40,40]，不吃生成的「第5章」
    KgEntity(id=3, project_id=1, novel_id=5, kind="item",
             canonical_name="混合", first_chapter=1, last_chapter=99),
    # 只有段N / 没有 mention：未知
    KgEntity(id=4, project_id=1, novel_id=None, kind="info",
             canonical_name="未知实体", first_chapter=None, last_chapter=None),
    KgEntity(id=5, project_id=1, novel_id=None, kind="info",
             canonical_name="无提及", first_chapter=None, last_chapter=None),
    # 单列缺失：只填缺的那一列
    KgEntity(id=6, project_id=1, novel_id=5, kind="item",
             canonical_name="半缺", first_chapter=None, last_chapter=7),
])
db.flush()
db.add_all([
    KgMention(project_id=1, novel_id=5, entity_id=1, generation_id=None, segment_no=10),
    KgMention(project_id=1, novel_id=5, entity_id=1, generation_id=None, segment_no=20),
    KgMention(project_id=1, novel_id=None, entity_id=2, generation_id=1, segment_no=1),
    KgMention(project_id=1, novel_id=None, entity_id=2, generation_id=2, segment_no=2),
    KgMention(project_id=1, novel_id=None, entity_id=2, generation_id=3, segment_no=3),
    KgMention(project_id=1, novel_id=5, entity_id=3, generation_id=None, segment_no=40),
    KgMention(project_id=1, novel_id=None, entity_id=3, generation_id=2, segment_no=4),
    KgMention(project_id=1, novel_id=None, entity_id=4, generation_id=3, segment_no=5),
    KgMention(project_id=1, novel_id=5, entity_id=6, generation_id=None, segment_no=6),
])
db.commit()

plan = {p.entity_id: p for p in build_plan(db).plans}
check("语料路径按 segment_no 取区间", [plan[1].recomputed_first, plan[1].recomputed_last], [10, 20])
check("生成路径只认显式章号", [plan[2].recomputed_first, plan[2].recomputed_last], [3, 5])
check("段N 不计入章号", plan[2].unknown_mentions, 1)
check("带书实体不吃生成章号", [plan[3].recomputed_first, plan[3].recomputed_last], [40, 40])
check("只有段N → 未知（不回落 1）", [plan[4].recomputed_first, plan[4].recomputed_last], [None, None])
check("无 mention → 未知", [plan[5].recomputed_first, plan[5].recomputed_last], [None, None])
check("未知实体不漂移", plan[4].drift, False)
check("NULL → 区间算可填", plan[1].fillable, True)
check("已有非 NULL 不算可填", plan[3].fillable, False)
check("已有非 NULL 且不同 → 漂移", plan[3].drift, True)
check("漂移方向：收窄", plan[3].drift_direction, "narrow")
check("单列缺失只填一列", [plan[6].fill_first, plan[6].fill_last], [6, 7])

summary = summarize(build_plan(db))
check("dry-run：未知 2 个", summary["unknown"], 2)
check("dry-run：可填 3 个（含单列缺失）", summary["fillable_now"], 3)
check("dry-run：漂移 1 个", summary["drift"], 1)
check("dry-run：漂移方向分布", summary["drift_directions"], {"narrow": 1, "extend": 0, "both": 0})

written = apply_plan(db, build_plan(db))
db.commit()
check("apply 只写可填行（不覆盖非 NULL）", written, 3)
rows = {e.id: (e.first_chapter, e.last_chapter) for e in db.query(KgEntity).all()}
check("apply 后语料区间落库", rows[1], (10, 20))
check("已有非 NULL 区间原样保留", rows[3], (1, 99))
check("apply 后未知仍为 NULL", rows[4], (None, None))
check("apply 后无提及仍为 NULL", rows[5], (None, None))
check("单列缺失只填一列", rows[6], (6, 7))
check("apply 幂等：再跑不写", apply_plan(db, build_plan(db)), 0)
db.close()
chap_engine.dispose()


# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
