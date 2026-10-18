"""KG v3.0 前端集成所需的读侧端点测试。

覆盖：
- judge findings 落库/读回（save_judge_findings / load_judge_findings）
  + 同段重跑先清后写 + 异段保留
- engine._merge_segment_into_graph 判定后落库（静态检查）
- 新端点：DELETE /exemptions（撤销豁免，Judge 恢复报告）
  GET /judge/findings（含 exempted 标记 + rules 清单）
  GET /characters（mention 降序 + has_secrets 标记）
  GET /relations（strength 降序 + 两端名解析 + 自环丢弃）

跑法：python tests/test_kg_v3_frontend_api.py
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
from app.main import app
from app.kg.judge import save_judge_findings, load_judge_findings, T_JUDGE_ALERT, RULES
from app.models import (
    Fact, Generation, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow,
    KgMention, KgRelation, Project,
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
db.add(Project(id=1, title="前端集成测试项目"))
db.add(Generation(project_id=1, segment_no=1, text="测试段", summary=None))
db.commit()

# ====================================================================
# 1. judge findings 落库/读回
# ====================================================================
findings_a = [
    {"rule": "teleportation", "severity": "err", "entity": "林悠然",
     "message": "瞬移了", "evidence": ["state@1: 办公室"]},
    {"rule": "secret_leak", "severity": "warn", "entity": "顾辰逸",
     "message": "说出秘密", "evidence": ["secret@2: 父亲并非因病去世"]},
]
n = save_judge_findings(db, 1, findings_a, segment_no=5)
check("落库 2 条", n, 2)
items = load_judge_findings(db, 1)
check("读回 2 条", len(items), 2)
check("读回 rule", items[0]["rule"], "teleportation")
check("读回 severity", items[0]["severity"], "err")
check("读回 segment", items[0]["segment_no"], 5)
check_true("读回 evidence", "state@1" in items[0]["evidence"][0])

# 同段重跑：先清后写
findings_a2 = [
    {"rule": "foreshadow_expiry", "severity": "warn", "entity": "伏笔A",
     "message": "超期", "evidence": []},
]
save_judge_findings(db, 1, findings_a2, segment_no=5)
items = load_judge_findings(db, 1)
check("同段重跑替换（不累积）", len(items), 1)
check("同段重跑后是新规则", items[0]["rule"], "foreshadow_expiry")

# 异段保留：段5 重跑剩 1 条 + 段6 新增 2 条 = 3 条共存
save_judge_findings(db, 1, findings_a, segment_no=6)
items = load_judge_findings(db, 1)
check("异段保留（2 段共存）", len(items), 3)
check("按段号倒序（段6 在前）", items[0]["segment_no"], 6)

# ====================================================================
# 2. GET /judge/findings 端点（含 exempted 标记 + rules）
# ====================================================================
r = client.get("/api/projects/1/kg-v3/judge/findings")
check("findings 状态码", r.status_code, 200)
data = r.json()
check("findings count", data["count"], 3)
check_true("findings rules 清单 6 条", len(data["rules"]) == 6)
check_true("findings by_severity 有 warn", data["by_severity"]["warn"] >= 1)
check_true("findings 初始未豁免", all(not i["exempted"] for i in data["items"]))

# 加豁免 → exempted 标记
client.post("/api/projects/1/kg-v3/exemptions", json={
    "rule": "secret_leak", "entity": "顾辰逸", "note": "故意让他说漏嘴"})
data = client.get("/api/projects/1/kg-v3/judge/findings").json()
leak_items = [i for i in data["items"] if i["rule"] == "secret_leak"]
check_true("豁免后 findings 带 exempted 标记", leak_items and leak_items[0]["exempted"] is True)

# ====================================================================
# 3. DELETE /exemptions 撤销豁免
# ====================================================================
r = client.delete("/api/projects/1/kg-v3/exemptions?rule=secret_leak&entity=顾辰逸")
check("撤销状态码", r.status_code, 200)
check("撤销计数", r.json()["revoked"], 1)
ex = client.get("/api/projects/1/kg-v3/exemptions").json()["exemptions"]
check("撤销后豁免清单空", len(ex), 0)
data = client.get("/api/projects/1/kg-v3/judge/findings").json()
check_true("撤销后 exempted 恢复 False", all(not i["exempted"] for i in data["items"]))

# 撤销不存在的豁免：revoked=0 不炸
r = client.delete("/api/projects/1/kg-v3/exemptions?rule=nothing&entity=")
check("撤销不存在不炸", (r.status_code, r.json()["revoked"]), (200, 0))

# 豁免后 Judge 不再报该告警（复用 judge_segment 语义验证撤销生效）
# ——此处只验证豁免行真删了（上面查过），judge_segment 过滤逻辑已有既有测试覆盖

# ====================================================================
# 4. GET /characters
# ====================================================================
ly = KgEntity(project_id=1, kind="character", canonical_name="林悠然",
              attrs={"secrets": ["母亲之死"]}, mention_count=7)
gy = KgEntity(project_id=1, kind="character", canonical_name="顾辰逸", mention_count=3)
office = KgEntity(project_id=1, kind="location", canonical_name="办公室", mention_count=99)
db.add_all([ly, gy, office])
db.flush()
from app.models import KgCharacterState, KgMention  # noqa: E402

db.add(KgCharacterState(project_id=1, character_id=ly.id, segment_no=1,
                        state_type="location", new_value="办公室"))
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=office.id,
                         known_kind="entity", learned_at_chapter=1))
db.commit()

# GET /slice/prompt 带 characters：渲染文本必须含状态/情报块
# （回归：曾因端点不接 characters 参数，prompt 文本永远缺这两块——浏览器冒烟抓到）
r = client.get("/api/projects/1/kg-v3/slice/prompt?segment_no=5&characters=林悠然")
check("prompt 状态码", r.status_code, 200)
check_true("prompt 含角色当前状态块", "角色当前状态" in r.json()["text"])
check_true("prompt 含角色已知情报块", "角色已知情报" in r.json()["text"])
check_true("prompt 含办公室情报", "办公室" in r.json()["text"])

db.add_all([
    KgRelation(project_id=1, from_id=ly.id, to_id=gy.id, rel_type="romantic", strength=0.9),
    KgRelation(project_id=1, from_id=gy.id, to_id=office.id, rel_type="located_at", strength=0.4),
    KgRelation(project_id=1, from_id=office.id, to_id=office.id, rel_type="located_at", strength=1.0),  # 自环
])
db.commit()

r = client.get("/api/projects/1/kg-v3/characters")
check("characters 状态码", r.status_code, 200)
chars = r.json()["characters"]
check("characters 只含角色（无 location）", [c["name"] for c in chars], ["林悠然", "顾辰逸"])
check("characters mention 降序", chars[0]["mention_count"], 7)
check_true("has_secrets 标记", chars[0]["has_secrets"] is True and chars[1]["has_secrets"] is False)

# ====================================================================
# 5. GET /relations
# ====================================================================
r = client.get("/api/projects/1/kg-v3/relations")
check("relations 状态码", r.status_code, 200)
rels = r.json()["relations"]
check("relations 2 条（自环丢弃，location 边保留）", len(rels), 2)
check("relations strength 降序首条", rels[0]["rel_type"], "romantic")
check("relations strength 降序次条", rels[1]["rel_type"], "located_at")
check("relations from 解析", rels[0]["from"], "林悠然")
check("relations to 解析", rels[0]["to"], "顾辰逸")
check("relations strength", rels[0]["strength"], 0.9)

# 404
check("characters 404", client.get("/api/projects/999/kg-v3/characters").status_code, 404)
check("relations 404", client.get("/api/projects/999/kg-v3/relations").status_code, 404)
check("findings 404", client.get("/api/projects/999/kg-v3/judge/findings").status_code, 404)
check("exemptions 404", client.delete("/api/projects/999/kg-v3/exemptions?rule=x").status_code, 404)

# ====================================================================
# 6. engine 落库接入（静态检查）
# ====================================================================
eng = (BACKEND / "app" / "generation" / "engine.py").read_text(encoding="utf-8")
check_true("engine 引 save_judge_findings", "save_judge_findings" in eng)

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
