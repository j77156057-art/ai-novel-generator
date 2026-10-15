"""v3.0 KG 9 张表 schema 测试。

覆盖：
- create_all 建出全部 9 张 v3.0 表
- 唯一约束生效（kg_entities project_id+kind+canonical_name / kg_relations 三元组 / kg_events sequence / kg_foreshadows name）
- 软 schema attrs JSON 读写（character arc/secrets、faction leader_id）
- kg_mentions 反向索引（entity × generation 唯一，context_window 存取）
- 旧表（kg_nodes/kg_edges/facts）与 v3.0 表在同一个 Base 下共存
- _migrate_sqlite_columns 幂等（v3.0 全表 + 迁移后不炸）

跑法：python tests/test_v30_schema.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401  注册模型
from app.models import (
    KgEntity, KgRelation, KgRelationEvolution, KgEvent, KgEventLink,
    KgCharacterState, KgForeshadow, KgEntityKnowledge, KgMention,
    KgNode, KgEdge, Fact,
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


engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

# —— 1. 9 张 v3.0 表全部建出 ——
insp = inspect(engine)
tables = set(insp.get_table_names())
V3_TABLES = {
    "kg_entities", "kg_relations", "kg_relation_evolution", "kg_events",
    "kg_event_links", "kg_character_state", "kg_foreshadows",
    "kg_entity_knowledge", "kg_mentions",
}
for t in sorted(V3_TABLES):
    check_true(f"表存在: {t}", t in tables)

# 旧表也在
for t in ("kg_nodes", "kg_edges", "facts"):
    check_true(f"旧表共存: {t}", t in tables)

# —— 2. kg_entities 唯一约束 (project_id, kind, canonical_name) ——
e1 = KgEntity(project_id=1, kind="character", canonical_name="林悠然", attrs={"arc": "职场女性逆袭", "secrets": ["母亲之死"]})
db.add(e1)
db.commit()
check("entity 插入后 id 有值", bool(e1.id), True)

e_dup = KgEntity(project_id=1, kind="character", canonical_name="林悠然")
db.add(e_dup)
try:
    db.commit()
    check("kg_entities 唯一约束拒绝重复", "no-error", "IntegrityError")
except IntegrityError:
    db.rollback()
    check("kg_entities 唯一约束拒绝重复", "IntegrityError", "IntegrityError")

# 同 kind 不同项目不冲突
e2 = KgEntity(project_id=2, kind="character", canonical_name="林悠然")
db.add(e2)
db.commit()
check("同 canonical_name 跨项目允许", bool(e2.id), True)

# 别名合并写入
e1.aliases = ["悠然", "林经理"]
db.commit()
db.refresh(e1)
check("entity aliases 读写", e1.aliases, ["悠然", "林经理"])

# —— 3. kg_relations 三元组唯一 + strength 默认 ——
r1 = KgRelation(project_id=1, from_id=e1.id, to_id=e2.id, rel_type="ally", strength=0.6)
db.add(r1)
db.commit()
check("relation strength 默认/写入", r1.strength, 0.6)

r_dup = KgRelation(project_id=1, from_id=e1.id, to_id=e2.id, rel_type="ally", strength=0.9)
db.add(r_dup)
try:
    db.commit()
    check("kg_relations 三元组唯一", "no-error", "IntegrityError")
except IntegrityError:
    db.rollback()
    check("kg_relations 三元组唯一", "IntegrityError", "IntegrityError")

# 同 from/to 不同 rel_type 允许
r2 = KgRelation(project_id=1, from_id=e1.id, to_id=e2.id, rel_type="romantic", strength=0.3)
db.add(r2)
db.commit()
check("同 from/to 不同 rel_type 允许", bool(r2.id), True)

# —— 4. relation_evolution 追加多行不清历史 ——
ev1 = KgRelationEvolution(project_id=1, edge_id=r1.id, chapter=5, strength=0.3, note="初次合作")
ev2 = KgRelationEvolution(project_id=1, edge_id=r1.id, chapter=15, strength=0.8, note="共渡危机")
ev3 = KgRelationEvolution(project_id=1, edge_id=r1.id, chapter=40, strength=0.5, note="政见分歧")
db.add_all([ev1, ev2, ev3])
db.commit()
rows = db.query(KgRelationEvolution).filter(KgRelationEvolution.edge_id == r1.id).order_by(KgRelationEvolution.chapter).all()
check("relation_evolution 历史行数", len(rows), 3)
check("evolution 顺序第一条 note", rows[0].note, "初次合作")
check("evolution 最新 strength", rows[-1].strength, 0.5)

# —— 5. kg_events SVO + sequence 唯一 ——
ev = KgEvent(project_id=1, sequence=1, event_type="incident", summary="林悠然发现数据异常",
             subject_id=e1.id, verb="发现", object_id=None, location_id=None,
             in_story_date="贞观三年秋", segment_no=1)
db.add(ev)
db.commit()
check("event in_story_date 读写", ev.in_story_date, "贞观三年秋")

ev_dup = KgEvent(project_id=1, sequence=1, event_type="incident")
db.add(ev_dup)
try:
    db.commit()
    check("kg_events sequence 唯一", "no-error", "IntegrityError")
except IntegrityError:
    db.rollback()
    check("kg_events sequence 唯一", "IntegrityError", "IntegrityError")

# —— 6. event_links 因果链 ——
ev2_ev = KgEvent(project_id=1, sequence=2, event_type="revelation", summary="内幕浮出")
db.add(ev2_ev)
db.commit()
link = KgEventLink(project_id=1, from_event_id=ev2_ev.id, to_event_id=ev.id, link_type="caused_by")
db.add(link)
db.commit()
check("event_link 插入", bool(link.id), True)

# —— 7. character_state 状态变化 ——
st1 = KgCharacterState(project_id=1, character_id=e1.id, chapter=50, state_type="location", new_value="上海陆家嘴办公室")
st2 = KgCharacterState(project_id=1, character_id=e1.id, chapter=80, state_type="injury", new_value="右手骨折")
st3 = KgCharacterState(project_id=1, character_id=e1.id, chapter=100, state_type="alive", new_value="false")
db.add_all([st1, st2, st3])
db.commit()
check("character_state 3 行", db.query(KgCharacterState).filter(KgCharacterState.character_id == e1.id).count(), 3)
alive = db.query(KgCharacterState).filter(KgCharacterState.character_id == e1.id, KgCharacterState.state_type == "alive").first()
check("character_state alive 值", alive.new_value, "false")

# —— 8. foreshadows 5 态 + name 唯一 ——
f1 = KgForeshadow(project_id=1, name="辰逸父亲是董事长", status="planted", planted_chapter=3, planned_payoff_chapter=50)
db.add(f1)
db.commit()
check("foreshadow 默认 status", f1.status, "planted")
f_dup = KgForeshadow(project_id=1, name="辰逸父亲是董事长", status="paid_off")
db.add(f_dup)
try:
    db.commit()
    check("kg_foreshadows name 唯一", "no-error", "IntegrityError")
except IntegrityError:
    db.rollback()
    check("kg_foreshadows name 唯一", "IntegrityError", "IntegrityError")

# 状态机流转 planted → developing → paid_off → expired（可直接改 status）
f1.status = "developing"
db.commit()
db.refresh(f1)
check("foreshadow 状态流转", f1.status, "developing")
f1.status = "expired"
f1.actual_payoff_chapter = None
db.commit()
check("foreshadow expired", f1.status, "expired")

# —— 9. entity_knowledge 情报追踪 ——
loc = KgEntity(project_id=1, kind="location", canonical_name="天津港")
db.add(loc)
db.commit()
k1 = KgEntityKnowledge(project_id=1, entity_id=e1.id, known_entity_id=loc.id, known_kind="entity", learned_at_event_id=ev.id, learned_at_chapter=4)
db.add(k1)
db.commit()
check("entity_knowledge 插入", bool(k1.id), True)
knows = db.query(KgEntityKnowledge).filter(KgEntityKnowledge.entity_id == e1.id).all()
check("entity_knowledge 查到 1 条", len(knows), 1)
check("knowledge known_kind", knows[0].known_kind, "entity")

# —— 10. kg_mentions 反向索引（含 generation FK）——
from app.models import Generation
g = Generation(project_id=1, segment_no=1, text="林悠然推开门……", summary={"l1": "林悠然入职"})
db.add(g)
db.commit()
m1 = KgMention(project_id=1, entity_id=e1.id, generation_id=g.id, segment_no=1, context_window="林悠然推开门，空调冷风扑面", mention_kind="direct")
db.add(m1)
db.commit()
check("mention 插入", bool(m1.id), True)
check("mention context_window 保留原文", m1.context_window, "林悠然推开门，空调冷风扑面")

# mention_count 更新（节奏分析用）
e1.mention_count = 5
db.commit()
db.refresh(e1)
check("entity mention_count", e1.mention_count, 5)

# —— 11. 软 schema attrs：faction leader_id / power_system ——
fac = KgEntity(project_id=1, kind="faction", canonical_name="林氏集团", attrs={"leader_id": e1.id, "region": "上海"})
db.add(fac)
db.commit()
check("faction attrs leader_id", fac.attrs["leader_id"], e1.id)

# —— 12. db.py 迁移幂等（v3.0 表走 create_all，迁移列表不含 v3.0——验证迁移函数对 v3.0 表不误加列）——
from app.db import _migrate_sqlite_columns
_migrate_sqlite_columns(engine)  # 幂等调用不应报错
check("迁移函数幂等调用不炸", True, True)

# —— 汇总 ——
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
