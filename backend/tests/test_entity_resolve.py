"""实体归一测试：entity_resolve 纯函数 + batch_worker 落库集成。

覆盖：
- should_merge：长名"X的Y"型合并 / 父亲≠母亲不合并 / 父亲≠父亲不合并自环
- should_merge：职级（"林总"vs"林经理"）保守不合并
- should_merge：长度边界（< 2 字、> 6 字拒绝）
- _upsert_entity 落库归一（短名并入长名 / 长名提升 canonical / 别名合并）
- _lookup_entity_id 归一反查（短名 → 长名 entity）
- 反向（已落库长名后，来短名能合并）
- 关键安全网：父亲 vs 母亲绝对不能合并

跑法：python tests/test_entity_resolve.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.entity_resolve import should_merge, merge_target, parse_canonical
from app.kg import batch_worker as bw
from app.models import KgEntity

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
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


# ====================================================================
# 1. should_merge 纯函数（单字核心称谓）
# ====================================================================
# 核心场景：长名"X的Y"型（Y 单字）合并
check("父 ← 张三的父", should_merge("父", "张三的父"), True)
check("父 ← 林悠然的父", should_merge("父", "林悠然的父"), True)
check("母 ← 顾辰逸的母", should_merge("母", "顾辰逸的母"), True)
check("友 ← 张三之友", should_merge("友", "张三之友"), True)
check("师 ← 老者者师", should_merge("师", "老者者师"), True)

# 关键安全网：父 vs 母绝对不能合并
check("母 ← 林悠然的父（亲属不同）", should_merge("母", "林悠然的父"), False)
check("父 ← 顾辰逸的母（亲属不同）", should_merge("父", "顾辰逸的母"), False)

# 多字核心不合并（设计：核心只取单字，漏报优于误报）
check("父亲 ← 林悠然的父亲", should_merge("父亲", "林悠然的父亲"), False)
check("朋友 ← 张三的朋友", should_merge("朋友", "张三的朋友"), False)

# 职级/通称保守不合并（不在 _CORE_KINDS）
check("经理 ← 林总的经理", should_merge("经理", "林总的经理"), False)
check("医生 ← 王医生", should_merge("医生", "王医生"), False)

# 边界
check("自己不合并自己", should_merge("父", "父"), False)
check("空字符串", should_merge("", "张三的父"), False)
check("无修饰词", should_merge("父", "张三父"), False)
check("1 字", should_merge("林", "林"), False)
check("无核心称谓", should_merge("树", "张的书"), False)

# parse_canonical
check("parse 父 ← 张三的父", parse_canonical("张三的父"), "父")
check("parse 林悠然 不剥", parse_canonical("林悠然"), "林悠然")
check("parse 顾辰逸 不剥", parse_canonical("顾辰逸"), "顾辰逸")
check("parse 多字核心不剥", parse_canonical("林悠然的父亲"), "林悠然的父亲")

# merge_target：长名信息量大，留 canonical
check("merge_target 长名", merge_target("父", "张三的父"), "张三的父")

# ====================================================================
# 2. _upsert_entity 落库归一
# ====================================================================
engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

# 2.1 先插长名"林悠然的父"
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "林悠然的父", "aliases": [], "attrs": {"occupation": "退休"}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
ents = db.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all()
check("先插长名行数", len(ents), 1)
check("先插长名 canonical", ents[0].canonical_name, "林悠然的父")

# 2.2 再插短名"父" → 应并入长名（短名加到 aliases）
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "父", "aliases": [], "attrs": {}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
ents = db.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all()
check("并入后仍是 1 行（短名合入长名）", len(ents), 1)
check("并入后 canonical 仍是长名", ents[0].canonical_name, "林悠然的父")
check("并入后 short 名入 aliases", "父" in (ents[0].aliases or []), True)

# 2.3 先插短名"母"再插长名"顾辰逸的母" → 长名提升 canonical
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "母", "aliases": [], "attrs": {}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "顾辰逸的母", "aliases": [], "attrs": {"health": "好"}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
mu = next(e for e in db.scalars(select(KgEntity).where(KgEntity.project_id == 1)) if e.canonical_name in ("顾辰逸的母", "母"))
check("长名提升 canonical", mu.canonical_name, "顾辰逸的母")
check("短名 母 入 aliases", "母" in (mu.aliases or []), True)
check("attrs 深合并", mu.attrs.get("health"), "好")

# 2.4 关键安全网："父"和"母"绝对不能合并
n_before = len(db.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all())
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "林悠然的母", "aliases": [], "attrs": {}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
n_after = len(db.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all())
check("母新建不并入父", n_after, n_before + 1)

# 2.5 多字核心不合并（设计保守）："林悠然的父亲"和"林总"不合并
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "林总", "aliases": [], "attrs": {}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
bw._persist_extraction(db, 1, {
    "entities": [{"kind": "character", "canonical_name": "林悠然的父亲", "aliases": [], "attrs": {}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
ents = db.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all()
# "林悠然的父亲"剥后核心"父亲"是 2 字不剥（设计保守）；"林总"也不是单字核心。
# 所以两者各自成行。
check("多字核心 林悠然的父亲 独立成行", any(e.canonical_name == "林悠然的父亲" for e in ents), True)
check("林总 独立成行", any(e.canonical_name == "林总" for e in ents), True)

# ====================================================================
# 3. _lookup_entity_id 归一反查
# ====================================================================
# 现 db 里有"林悠然的父"，用"父"应能查到
id1 = bw._lookup_entity_id(db, 1, "父")
check_true("lookup 父 命中归一后长名", id1 is not None)
ent = db.get(KgEntity, id1)
check("lookup 父 → 林悠然的父", ent.canonical_name, "林悠然的父")

# 用"母"应查到"顾辰逸的母"
id2 = bw._lookup_entity_id(db, 1, "母")
check_true("lookup 母 命中", id2 is not None)
ent2 = db.get(KgEntity, id2)
check("lookup 母 → 顾辰逸的母", ent2.canonical_name, "顾辰逸的母")

# 父/母查的是不同 entity（安全网）
check("父≠母 实体", id1 != id2, True)

# 不存在的归一：应 None
check("lookup 不存在", bw._lookup_entity_id(db, 1, "不存在的亲属称谓"), None)

# ====================================================================
# 4. 真实变体回放（独立 db 隔离前面 #2 段残留）
# ====================================================================
engine4 = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine4)
Session4 = sessionmaker(bind=engine4, future=True)
db2 = Session4()
# 网文高频 2 变体：长名"林悠然的父" + 短名"父" → 归一为 1 行
# 注意：不同人不能合并（"X的父" vs "Y的父"是两人，剥后核心都"父"但前缀不同），
# 反向合并要求 short 是单字核心，避免长-长误并
for name in ["林悠然的父", "父"]:
    bw._persist_extraction(db2, 1, {
        "entities": [{"kind": "character", "canonical_name": name, "aliases": [], "attrs": {}}],
        "relations": [], "events": [], "state_changes": [], "foreshadows": [],
        "event_links": [], "entity_knowledge": [],
    }, [])
ents = db2.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all()
check("2 变体（长+短）归一后只 1 个 entity", len(ents), 1)
check("归一 canonical 是长名", ents[0].canonical_name, "林悠然的父")
check("归一 aliases 含短名", sorted(ents[0].aliases or []), ["父"])

# 关键安全：再插"顾辰逸的父"（另一人）应新建不合并
n_before = len(ents)
bw._persist_extraction(db2, 1, {
    "entities": [{"kind": "character", "canonical_name": "顾辰逸的父", "aliases": [], "attrs": {}}],
    "relations": [], "events": [], "state_changes": [], "foreshadows": [],
    "event_links": [], "entity_knowledge": [],
}, [])
ents = db2.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all()
check("另一人（顾辰逸的父）不并入林悠然父", len(ents), n_before + 1)

# 汇总
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
