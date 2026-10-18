"""v3.0 检索 + 8 产品功能读侧 API 测试。

覆盖：
- slice_for_prompt：按当前段号取 KG 切片
  - recent_entities（mention 反查，带 context + mention_count）
  - character_states（每角色每 state_type 最新一条）
  - active_foreshadows（planted/developing）
  - known_facts（entity_knowledge）
  - recent_events（in_story_date）
- format_slice_for_prompt 渲染
- relation_curve 关系演变曲线
- can_character_know 人物情报追踪
- rhythm_analysis 节奏分析
- build_context 接入 kg_slice（静态检查）
- build_user_prompt kg_slice 块（有则注入，无则跳过）

跑法：python tests/test_v30_retrieval.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.retrieval_v3 import (
    slice_for_prompt, format_slice_for_prompt,
    relation_curve, can_character_know, rhythm_analysis,
)
from app.models import (
    KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow,
    KgMention, KgRelation, KgRelationEvolution,
)
from app.generation.prompts import build_user_prompt

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

# —— 预置实体 ——
ly = KgEntity(project_id=1, kind="character", canonical_name="林悠然", attrs={"secrets": ["母亲之死"]})
gy = KgEntity(project_id=1, kind="character", canonical_name="顾辰逸")
office = KgEntity(project_id=1, kind="location", canonical_name="办公室")
port = KgEntity(project_id=1, kind="location", canonical_name="天津港")
db.add_all([ly, gy, office, port])
db.commit()

# —— 预置数据：段1-2 林悠然在办公室，段3 到天津港 ——
db.add_all([
    KgCharacterState(project_id=1, character_id=ly.id, segment_no=1, state_type="location", new_value="办公室"),
    KgCharacterState(project_id=1, character_id=ly.id, segment_no=2, state_type="goal", new_value="查清数据异常"),
    KgCharacterState(project_id=1, character_id=ly.id, segment_no=3, state_type="location", new_value="天津港"),
    KgEvent(project_id=1, sequence=1, segment_no=1, event_type="incident", summary="林悠然入职", location_id=office.id, in_story_date="2024年春"),
    KgEvent(project_id=1, sequence=2, segment_no=3, event_type="decision", summary="林悠然签确认书", location_id=port.id, in_story_date="2024年夏"),
    KgMention(project_id=1, entity_id=ly.id, segment_no=1, context_window="林悠然推开门"),
    KgMention(project_id=1, entity_id=ly.id, segment_no=2, context_window="林悠然盯着表格"),
    KgMention(project_id=1, entity_id=ly.id, segment_no=3, context_window="林悠然站在码头"),
    KgMention(project_id=1, entity_id=gy.id, segment_no=3, context_window="顾辰逸赶到"),
    KgForeshadow(project_id=1, name="辰逸父亲是董事长", status="developing", planted_chapter=3, planned_payoff_chapter=50),
    KgForeshadow(project_id=1, name="已回收的伏笔", status="paid_off", planted_chapter=1, planned_payoff_chapter=10),
])
db.commit()

# ====================================================================
# 1. slice_for_prompt（当前段=3，window=5）
# ====================================================================
sl = slice_for_prompt(db, 1, current_segment_no=3, window=5, active_characters=["林悠然"])

# recent_entities：林悠然×3 + 顾辰逸×1 = 2 实体
check("切片 recent_entities 数", len(sl["recent_entities"]), 2)
ly_ent = next(e for e in sl["recent_entities"] if e["name"] == "林悠然")
check("切片 林悠然 mention_count", ly_ent["mention_count"], 3)
check_true("切片 林悠然 context 非空", bool(ly_ent["context"]))

# character_states：林悠然 location(最新=天津港) + goal
ly_states = {s["state_type"]: s["new_value"] for s in sl["character_states"] if s["character"] == "林悠然"}
check("切片 location 取最新", ly_states.get("location"), "天津港")
check("切片 goal 保留", ly_states.get("goal"), "查清数据异常")

# active_foreshadows：只有 developing 那条
check("切片 active_foreshadows 数", len(sl["active_foreshadows"]), 1)
check("切片 foreshadow 是 developing", sl["active_foreshadows"][0]["status"], "developing")

# known_facts：暂无 knowledge 记录
check("切片 known_facts 空", sl["known_facts"], [])

# recent_events：段1+段3 两条
check("切片 recent_events 数", len(sl["recent_events"]), 2)
check_true("切片 event 带 in_story_date", any(e["in_story_date"] for e in sl["recent_events"]))

# ====================================================================
# 2. window 边界：段=1 window=5 → 只有段1
# ====================================================================
sl1 = slice_for_prompt(db, 1, current_segment_no=1, window=5)
check("切片 window 下界", len(sl1["recent_events"]), 1)  # 只有段1事件

# ====================================================================
# 3. known_facts（加 knowledge 记录后）
# ====================================================================
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=port.id,
                         known_kind="entity", learned_at_chapter=3))
db.commit()
sl2 = slice_for_prompt(db, 1, current_segment_no=3, active_characters=["林悠然"])
check("切片 known_facts 有记录", len(sl2["known_facts"]), 1)
check_true("切片 林悠然知道天津港", "天津港" in sl2["known_facts"][0]["knows"])

# ====================================================================
# 4. format_slice_for_prompt 渲染
# ====================================================================
text = format_slice_for_prompt(sl2)
check_true("渲染含近期出场实体", "【近期出场实体】" in text)
check_true("渲染含角色当前状态", "【角色当前状态】" in text)
check_true("渲染含未回收伏笔", "【未回收伏笔】" in text)
check_true("渲染含角色已知情报", "【角色已知情报】" in text)
check_true("渲染含林悠然名字", "林悠然" in text)
check_true("渲染含天津港伏笔计划", "计划第50章回收" in text)

# 空切片渲染空串
check("空切片渲染空串", format_slice_for_prompt({"recent_entities": []}), "")

# ====================================================================
# 5. relation_curve 关系演变曲线
# ====================================================================
rel = KgRelation(project_id=1, from_id=ly.id, to_id=gy.id, rel_type="romantic", strength=0.5)
db.add(rel)
db.commit()
db.add_all([
    KgRelationEvolution(project_id=1, edge_id=rel.id, chapter=5, strength=0.3, note="初次相遇"),
    KgRelationEvolution(project_id=1, edge_id=rel.id, chapter=15, strength=0.8, note="共渡危机"),
    KgRelationEvolution(project_id=1, edge_id=rel.id, chapter=40, strength=0.5, note="政见分歧"),
])
db.commit()
curve = relation_curve(db, 1, "林悠然", "顾辰逸")
check("关系曲线 3 点", len(curve), 3)
check("关系曲线按章升序", [c["chapter"] for c in curve], [5, 15, 40])
check("关系曲线 strength 序列", [c["strength"] for c in curve], [0.3, 0.8, 0.5])

# 反向查询（顾辰逸→林悠然）也应命中（from/to 双向 in_）
curve_rev = relation_curve(db, 1, "顾辰逸", "林悠然")
check("关系曲线反向查询", len(curve_rev), 3)

# 不存在的角色 → 空
check("关系曲线不存在的角色", relation_curve(db, 1, "张三", "李四"), [])

# ====================================================================
# 6. can_character_know 人物情报追踪
# ====================================================================
check("can_know 林悠然知道天津港", can_character_know(db, 1, "林悠然", "天津港"), True)
check("can_know 顾辰逸不知道天津港", can_character_know(db, 1, "顾辰逸", "天津港"), False)
check("can_know 不存在角色", can_character_know(db, 1, "张三", "天津港"), False)

# ====================================================================
# 7. rhythm_analysis 节奏分析
# ====================================================================
rhythm = rhythm_analysis(db, 1, window=10)
check_true("节奏 有 top_entities", len(rhythm["top_entities"]) > 0)
check("节奏 林悠然 mention 最多", rhythm["top_entities"][0]["name"], "林悠然")
check("节奏 林悠然 mention 数", rhythm["top_entities"][0]["mentions"], 3)

# 空项目
rhythm_empty = rhythm_analysis(db, 999, window=10)
check("节奏 空项目", rhythm_empty["top_entities"], [])

# ====================================================================
# 8. build_user_prompt kg_slice 块
# ====================================================================
# 有 kg_slice → 注入
up = build_user_prompt(
    prior_summaries=["前情1"], rag_context=[], outline=None, max_chars=1500,
    kg_slice="【近期出场实体】\n- 林悠然（character）",
)
check_true("prompt 注入 kg_slice 块", "【世界观事实" in up)
check_true("prompt 含 kg_slice 内容", "林悠然（character）" in up)

# 无 kg_slice → 不注入
up2 = build_user_prompt(
    prior_summaries=["前情1"], rag_context=[], outline=None, max_chars=1500,
    kg_slice=None,
)
check_true("prompt 无 kg_slice 不注入", "【世界观事实" not in up2)

# kg_slice 与 node_prompt 并存（不互斥——kg_slice 是已生成事实，node_prompt 是剧情点）
up3 = build_user_prompt(
    prior_summaries=["前情1"], rag_context=[], outline=None, max_chars=1500,
    node_prompt="当前剧情点：开端", kg_slice="世界观事实文本",
)
check_true("kg_slice 与 node_prompt 并存", "【世界观事实" in up3 and "【当前剧情进度" in up3)

# ====================================================================
# 9. engine 接入（静态检查）
# ====================================================================
engine_py = (BACKEND / "app" / "generation" / "engine.py").read_text(encoding="utf-8")
check_true("engine build_context 含 kg_slice", '"kg_slice"' in engine_py)
check_true("engine 调 _kg_slice_text", "_kg_slice_text" in engine_py)
check_true("engine 传 kg_slice 给 build_user_prompt", "kg_slice=ctx.get" in engine_py)

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
