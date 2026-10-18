"""plot_bible 级联协议测试（kg-cascade-protocol-design）。

覆盖：
- diff_bible：新增/删除/改动 3 类顶层 key
- detect_conflicts：character_role / character_goal / relationship_type /
  foreshadow_status 四类冲突；新增角色/关系不报；非字段不报
- apply_bible_revision：写 revision + diff + 扫冲突 + 覆盖
  + revision_no 严格 +1 + 二次 patch 走 rev_no=2
- 端点：POST /bible/apply 走新路径、GET /bible/revisions 历史清单
  + 老 PATCH 路径仍工作（不破兼容性）
- 老库兼容：新表创建 + 已有 plot_bible=None 的项目能首次 apply

跑法：python tests/test_kg_cascade_protocol.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
import app.models  # noqa: F401
from app.kg.bible import (
    diff_bible,
    detect_conflicts,
    apply_bible_revision,
    list_bible_revisions,
)
from app.main import app
from app.models import (
    KgBibleRevision, KgEntity, KgForeshadow, Project,
)

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


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)


def override_get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

db = Session()
db.add(Project(id=1, title="级联测试项目"))
db.commit()

# ====================================================================
# 1. diff_bible 单元
# ====================================================================
old = {"characters": [{"name": "林悠然", "role": "员工"}], "direction": "升职"}
new = {
    "characters": [{"name": "林悠然", "role": "经理"}, {"name": "顾辰逸", "role": "对手"}],
    "direction": "升职",
    "foreshadowing": ["账本之谜"],
}
d = diff_bible(old, new)
check("diff: 改 role", d["changed"].get("characters") is not None, True)
check("diff: 不改 direction（值同）", "direction" in d["changed"], False)
check("diff: 新增 foreshadowing", "foreshadowing" in d["added"], True)
check("diff: 旧没改 key removed 空", d["removed"], {})
check("diff: new 没删字段", d["added"].get("foreshadowing") == ["账本之谜"], True)

# diff 兜底：None/破损输入
d2 = diff_bible(None, {"a": 1})
check("diff: old=None 当空 dict（new 多 a）", d2["added"], {"a": 1})
check("diff: new=None 当空 dict（双向都空）", diff_bible({"x": 1}, None), {"added": {}, "removed": {"x": 1}, "changed": {}})

# ====================================================================
# 2. detect_conflicts 单元（需要 KG 数据）
# ====================================================================
# 设 KG：林悠然 旧 role=员工, goal=查清异常
ly = KgEntity(project_id=1, kind="character", canonical_name="林悠然",
              attrs={"role": "员工", "current_goal": "查清数据异常"})
gy = KgEntity(project_id=1, kind="character", canonical_name="顾辰逸", attrs={})
office = KgEntity(project_id=1, kind="location", canonical_name="办公室", attrs={})
# 关系：林悠然→顾辰逸 是 romantic
from app.models import KgRelation
db.add_all([ly, gy, office])
db.flush()  # 拿 ly/gy.id
rel = KgRelation(project_id=1, from_id=ly.id, to_id=gy.id,
                 rel_type="romantic", strength=0.7)
fs = KgForeshadow(project_id=1, name="账本之谜", status="planted",
                 planted_chapter=3, planned_payoff_chapter=30)
db.add_all([rel, fs])
db.commit()

# 改 bible：role 改 manager、relationship 改 enemy、foreshadow 改 dropped
new_bible = {
    "characters": [
        {"name": "林悠然", "role": "经理", "goal": "查清数据异常"},
        {"name": "顾辰逸", "role": "对手"},
    ],
    "relationships": [{"a": "林悠然", "b": "顾辰逸", "type": "enemy"}],
    "foreshadowing": ["账本之谜"],  # 状态没改不报
    "direction": "翻盘",
}
conflicts = detect_conflicts(db, 1, new_bible)
kinds = [c["kind"] for c in conflicts]
check("冲突: character_role 命中", "character_role" in kinds, True)
check("冲突: relationship_type 命中", "relationship_type" in kinds, True)
check("冲突: goal 相同不报（goal 字段没变）", "character_goal" in kinds, False)
check("冲突: foreshadowing 状态同不报", "foreshadow_status" in kinds, False)

# goal 真的改 → 报
new_bible["characters"][0]["goal"] = "找父亲"
conflicts2 = detect_conflicts(db, 1, new_bible)
check("冲突: character_goal 命中", any(c["kind"] == "character_goal" for c in conflicts2), True)

# 加新角色不报冲突
new_bible["characters"].append({"name": "新人", "role": "同事"})
conflicts3 = detect_conflicts(db, 1, new_bible)
check_true("冲突: 新增角色不报", not any(c["kg_key"] == "新人" for c in conflicts3))

# ====================================================================
# 3. apply_bible_revision 端到端
# ====================================================================
r = apply_bible_revision(db, 1, new_bible, note="提升林悠然为经理")
check("apply: revision_no 1", r["revision_no"], 1)
# 首次 apply：老 bible=None（新项目）→ diff 全是 added，没 changed
check("apply: 首次 diff 全 added（无 changed）", r["diff"]["changed"], {})
check("apply: 首次 diff 含 characters added", r["diff"]["added"].get("characters") is not None, True)
check("apply: conflicts_count ≥ 1", r["conflicts_count"] >= 1, True)
check("apply: bible 真的覆盖了", db.get(Project, 1).plot_bible.get("direction"), "翻盘")
# revision 行落库
rev_count = db.scalar(select(func.count(KgBibleRevision.id)).where(
    KgBibleRevision.project_id == 1
))
check("apply: revision 行写入", rev_count, 1)

# 第二次 apply → revision_no=2
r2 = apply_bible_revision(db, 1, {**new_bible, "direction": "团圆"})
check("apply: 第二次 rev_no=2", r2["revision_no"], 2)
check("apply: 第二次 diff.changed.direction", r2["diff"]["changed"].get("direction", {}).get("new"), "团圆")
rev_count2 = db.scalar(select(func.count(KgBibleRevision.id)).where(
    KgBibleRevision.project_id == 1
))
check("apply: revision 累计 2", rev_count2, 2)

# 第三次不改 → 仍有 diff（"啥都没改"也是 diff.changed 空）
r3 = apply_bible_revision(db, 1, {**new_bible, "direction": "团圆"})
check("apply: 第三次 rev_no=3", r3["revision_no"], 3)
check("apply: 第三次 diff 空（值同）", r3["diff"], {"added": {}, "removed": {}, "changed": {}})

# 404：项目不存在
try:
    apply_bible_revision(db, 999, {})
except ValueError as e:
    check("apply: 不存在项目 ValueError", "不存在" in str(e), True)

# ====================================================================
# 4. 端点：POST /bible/apply
# ====================================================================
r = client.post("/api/projects/1/bible/apply", json={
    "bible": {"characters": [{"name": "新人", "role": "同事"}], "direction": "新的"},
    "note": "端点测试",
})
check("端点 apply 状态码", r.status_code, 200)
data = r.json()
check("端点 apply: revision_id 有值", "revision_id" in data, True)
check("端点 apply: revision_no=4", data["revision_no"], 4)

# 404
r = client.post("/api/projects/999/bible/apply", json={"bible": {}})
check("端点 apply: 不存在项目 404", r.status_code, 404)

# ====================================================================
# 5. 端点：GET /bible/revisions
# ====================================================================
r = client.get("/api/projects/1/bible/revisions?limit=10")
check("端点 revisions 状态码", r.status_code, 200)
revs = r.json()["revisions"]
check("端点 revisions 数", len(revs), 4)
check("端点 revisions 新的在前", revs[0]["revision_no"], 4)
check("端点 revisions 含 diff", "diff" in revs[0], True)
check("端点 revisions 含 conflicts", "conflicts" in revs[0], True)
check_true("端点 revisions 含 created_at", revs[0].get("created_at") is not None)

r = client.get("/api/projects/999/bible/revisions")
check("端点 revisions: 不存在项目 404", r.status_code, 404)

# ====================================================================
# 6. 阶段 2-3 收尾：merged_constraint_items 的「bible 删除权威」
# ====================================================================
from app.generation.prompts import merged_constraint_items

rem = {"character_constraints": [
    {"name": "林悠然", "mentions": 5},
    {"name": "顾辰逸", "mentions": 3},
    {"name": "贾正", "mentions": 2},
], "unresolved_plotholes": [{"question": "账本的隐藏页"}, {"question": "父亲的死因"}]}

# bible 显式清册只留林悠然 → 顾辰逸/贾正 的 KG 约束被剔除（bible 删除权威）
b1 = {"characters": [{"name": "林悠然", "role": "主角"}]}
chars1, fores1 = merged_constraint_items(rem, b1)
names1 = [c.get("name") if isinstance(c, dict) else str(c) for c in chars1]
check("删除权威: 锁定名单只剩林悠然", [n for n in names1 if n], ["林悠然"])
check_true("删除权威: 被删角色不出锁定名单", "顾辰逸" not in names1 and "贾正" not in names1)
check_true("删除权威: 伏笔不受影响（本轮只做角色侧）", "账本的隐藏页" in fores1)

# 保守边界 1：bible 无 characters 键 → 不过滤（老项目不误伤）
chars2, _ = merged_constraint_items(rem, {"relationships": []})
check("保守: 无 characters 键不过滤", len(chars2), 3)

# 保守边界 2：bible.characters 空列表 → 不过滤（未显式宣告清册）
chars3, _ = merged_constraint_items(rem, {"characters": []})
check("保守: characters 空列表不过滤", len(chars3), 3)

# 保守边界 3：bible=None → 不过滤
chars4, _ = merged_constraint_items(rem, None)
check("保守: bible=None 不过滤", len(chars4), 3)

# 保守边界 4：characters 是纯字符串列表（无 name 键）也能识别清册
chars5, _ = merged_constraint_items(rem, {"characters": ["林悠然", "顾辰逸"]})
check("字符串清册: 只留宣告过的", len(chars5), 2)

# ====================================================================
# 7. 老 PATCH 路径仍工作（兼容性）
# ====================================================================
r = client.patch("/api/projects/1", json={"title": "新名字"})
check("老 PATCH: 状态码", r.status_code, 200)
check("老 PATCH: title 更新", r.json()["title"], "新名字")
# 老 PATCH 改 plot_bible 不会写 revision（兼容：老逻辑不动）
r = client.patch("/api/projects/1", json={"plot_bible": {"old_path": True}})
check("老 PATCH: 改 bible 成功", r.status_code, 200)
rev_after_old = db.scalar(select(func.count(KgBibleRevision.id)).where(
    KgBibleRevision.project_id == 1
))
check("老 PATCH: 不写 revision 行", rev_after_old, 4)  # 仍是 4

# ====================================================================
# 7. 老库兼容：plot_bible=None 项目首次 apply
# ====================================================================
db2 = Session()
db2.add(Project(id=2, title="空 bible 项目"))
db2.commit()
r = apply_bible_revision(db2, 2, {"direction": "从无到有"})
check("老库兼容: 空 bible 首 apply", r["revision_no"], 1)
check("老库兼容: diff 全 added", r["diff"]["added"].get("direction"), "从无到有")
db2.close()

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
