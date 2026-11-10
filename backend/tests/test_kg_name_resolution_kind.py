"""同名跨 kind（item vs character）的名字解析测试。

背景：``kg_entities`` 的唯一键是 ``(project_id, kind, canonical_name)``，
所以「长刀」既可以是物品也可以是人物。任何按名 ``db.scalar`` 的查询在两类
并存后都会抛 ``MultipleResultsFound``——本文件把两处链路（v3 检索切片 /
bible 关系端点）和一个同类兜底（批抽关系端点反查）钉住行为。

跑法：python tests/test_kg_name_resolution_kind.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.batch_worker import _lookup_entity_id
from app.kg.bible import detect_conflicts
from app.kg.retrieval_v3 import (
    can_character_know,
    relation_curve,
    slice_for_prompt,
)
from app.models import (
    KgCharacterState, KgEntity, KgEntityKnowledge, KgRelation,
    KgRelationEvolution,
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


engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

# —— 同名陷阱 A：「长刀」既是人物也是物品（角色状态/情报用） ——
char = KgEntity(project_id=1, kind="character", canonical_name="长刀")
item = KgEntity(project_id=1, kind="item", canonical_name="长刀")
# —— 同名陷阱 B：「金锣」既是人物也是物品（关系曲线/关系端点用） ——
jin_char = KgEntity(project_id=1, kind="character", canonical_name="金锣")
jin_item = KgEntity(project_id=1, kind="item", canonical_name="金锣")
yin_char = KgEntity(project_id=1, kind="character", canonical_name="银锣")
# —— 只有物品的端点（owns 关系仍要能解析） ——
sword = KgEntity(project_id=1, kind="item", canonical_name="神剑")
hero = KgEntity(project_id=1, kind="character", canonical_name="许七安")
db.add_all([char, item, jin_char, jin_item, yin_char, sword, hero])
db.commit()

db.add(KgCharacterState(project_id=1, character_id=char.id, segment_no=1,
                        state_type="location", new_value="京城"))
db.add(KgEntityKnowledge(project_id=1, entity_id=char.id, known_kind="secret",
                         known_text="他其实是女子"))
rel_jin = KgRelation(project_id=1, from_id=jin_char.id, to_id=yin_char.id,
                     rel_type="ally", strength=0.7, status="active")
db.add_all([rel_jin,
            KgRelation(project_id=1, from_id=hero.id, to_id=sword.id,
                       rel_type="owns", strength=1.0, status="active")])
db.commit()
db.add(KgRelationEvolution(project_id=1, edge_id=rel_jin.id, chapter=3,
                           strength=0.7, note="结拜"))
db.commit()

# ====================================================================
# 1. retrieval_v3.slice_for_prompt：角色状态 / 情报只认 character
# ====================================================================
sl = slice_for_prompt(db, 1, current_segment_no=1, active_characters=["长刀"])

states = [s for s in sl["character_states"] if s["character"] == "长刀"]
check("切片 同名时仍取到角色状态", len(states), 1)
check("切片 状态值来自 character", states[0]["new_value"], "京城")
check("切片 同名时仍取到角色情报", [f["character"] for f in sl["known_facts"]], ["长刀"])

check("can_know 同名时命中角色秘密",
      can_character_know(db, 1, "长刀", "秘密：他其实是女子"), True)
check("can_know 物品名当角色查不得命中",
      can_character_know(db, 1, "神剑", "秘密：他其实是女子"), False)

# relation_curve 不限 kind，但同名时必须确定取 character（KIND_ORDER 首位）
curve = relation_curve(db, 1, "金锣", "银锣")
check("关系曲线 同名端点不炸", len(curve), 1)
check("关系曲线 同名端点解析到人物边", curve[0]["note"], "结拜")

# ====================================================================
# 2. bible.detect_conflicts：关系端点不限 kind，同名时确定且不炸
# ====================================================================
conflicts = detect_conflicts(db, 1, {"relationships": [
    {"a": "金锣", "b": "银锣", "type": "enemy"},
    {"a": "许七安", "b": "神剑", "type": "enemy"},
]})
check("bible 同名端点报出冲突", [c["kind"] for c in conflicts],
      ["relationship_type", "relationship_type"])
check("bible 同名端点解析到人物（ally→enemy）",
      next(c["kg_value"] for c in conflicts if c["kg_key"] == "金锣↔银锣"), "ally")
check("bible 物品端点（许七安↔神剑）也能解析",
      next(c["kg_value"] for c in conflicts if c["kg_key"] == "许七安↔神剑"), "owns")

# ====================================================================
# 3. batch_worker._lookup_entity_id：同名时取 character（KIND_ORDER 首位）
# ====================================================================
check("批抽反查 同名取 character", _lookup_entity_id(db, 1, "长刀"), char.id)
check("批抽反查 物品名取 item", _lookup_entity_id(db, 1, "神剑"), sword.id)
check("批抽反查 不存在返回 None", _lookup_entity_id(db, 1, "不存在"), None)

print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
