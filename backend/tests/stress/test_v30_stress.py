"""Step 6 压力测试：验证 v3.0 KG 在 100 / 1000 / 3000 段规模下的存储与检索性能。

方法论：
- 正文用**确定性合成**（不调 LLM 生成）——压力测试验的是存储/检索/性能规模，
  不是 LLM 质量（质量已由 Step 1-5 的真环境验证覆盖）
- 抽取用**确定性 mock**（固定 entity/event 池轮转）——同样验管线不验 LLM
- 真实落 v3.0 表 + 真实检索（slice_for_prompt / rhythm_analysis）
- 每级记录：表大小、各表行数、检索耗时、KG 增长曲线

判定标准（任何一步不过 → 回设计阶段改）：
- 100 段：检索 < 100ms，表增长正常
- 1000 段：检索 < 500ms（拐点检查），SQLite 文件 < 100MB
- 3000 段：检索 < 2000ms，验证百万字容量（3000×1500字 ≈ 450 万字）

跑法：python tests/stress/test_v30_stress.py （在 backend/ 下）
"""
from __future__ import annotations

import os
import sys
import time
import random
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.batch_worker import _persist_extraction, _QueueItem
from app.kg.retrieval_v3 import slice_for_prompt, rhythm_analysis
from app.models import (
    Generation, KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent,
    KgForeshadow, KgMention, KgRelation,
)

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(label: str, got, want, tol: float = 0.0) -> None:
    global PASS, FAIL
    if tol:
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILED.append(label)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got={got!r} want={want!r}")


def check_true(label: str, cond) -> None:
    check(label, bool(cond), True)


DB_PATH = r"C:\Users\h'h'h\AppData\Local\hermes\cache\scratch\step6_stress.db"
engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

PROJECT_ID = 1

# —— 合成语料：固定 entity 池轮转（模拟真实项目：几十角色 + 若干地点）——
CHARS = ["林悠然", "顾辰逸", "陈默", "林妙妙", "老陈", "赵助理", "董事长", "法务", "秘书", "司机"]
LOCS = ["办公室", "会议室", "天津港", "码头", "仓库", "停车场", "咖啡厅", "电梯间"]
FACTS = ["数据异常", "钒钛造假", "董事会阴谋", "母亲之死", "战略入股"]

random.seed(42)  # 确定性


def synth_extraction(seg_no: int) -> dict:
    """合成一段的抽取结果（轮转 entity 池，跨段累积）。"""
    n_chars = 3 + (seg_no % len(CHARS))
    chars = random.sample(CHARS, min(n_chars, len(CHARS)))
    return {
        "entities": [{"kind": "character", "canonical_name": c, "aliases": [], "attrs": {"secrets": [FACTS[seg_no % len(FACTS)]] if seg_no % 3 == 0 else []}} for c in chars]
                    + [{"kind": "location", "canonical_name": LOCS[seg_no % len(LOCS)], "aliases": [], "attrs": {}}],
        "relations": [{"from": chars[0], "to": chars[1 % len(chars)], "rel_type": "ally", "strength": 0.3 + (seg_no % 7) * 0.1}],
        "events": [{"chapter": seg_no, "event_type": "incident", "summary": f"{chars[0]}在{LOCS[seg_no % len(LOCS)]}处理事务",
                    "subject": chars[0], "verb": "处理", "object": "", "location": LOCS[seg_no % len(LOCS)],
                    "in_story_date": f"2024年{1 + seg_no % 12}月"}],
        "state_changes": [{"character": chars[0], "state_type": "location", "new_value": LOCS[seg_no % len(LOCS)]}],
        "foreshadows": [{"name": FACTS[seg_no % len(FACTS)], "status": "planted", "planted_chapter": seg_no, "planned_payoff_chapter": seg_no + 50}] if seg_no % 5 == 0 else [],
    }


def synth_text(seg_no: int) -> str:
    """合成一段正文（1500 字量级）。"""
    chars = random.sample(CHARS, 4)
    loc = LOCS[seg_no % len(LOCS)]
    return f"{chars[0]}站在{loc}，{chars[1]}走了过来。两人谈起{FACTS[seg_no % len(FACTS)]}。" + ("叙述文字。" * 300)


def run_level(n_segments: int, batch_size: int = 5) -> dict:
    """跑一个规模级别：合成 N 段 → 分批落 v3.0 表 → 测检索性能。"""
    print(f"\n{'='*60}\n压力级别：{n_segments} 段\n{'='*60}")
    t0 = time.time()

    # 落 generations + 批抽落 v3.0 表
    for i in range(1, n_segments + 1):
        text = synth_text(i)
        gen = Generation(project_id=PROJECT_ID, segment_no=i, text=text, summary={"l1": f"段{i}摘要"})
        db.add(gen)
        # 每 batch_size 段落一次 v3.0 表
        if i % batch_size == 0 or i == n_segments:
            batch = [_QueueItem(project_id=PROJECT_ID, segment_no=j, text=synth_text(j), chapter=j)
                     for j in range(max(1, i - batch_size + 1), i + 1)]
            result = synth_extraction(i)
            try:
                _persist_extraction(db, PROJECT_ID, result, batch)
            except Exception as e:
                print(f"  批抽落库失败 @seg{i}: {e}")
    db.commit()
    build_t = time.time() - t0

    # 表大小
    db_size_mb = os.path.getsize(DB_PATH) / (1024 * 1024) if os.path.exists(DB_PATH) else 0

    # 各表行数
    counts = {}
    for name, model in [("kg_entities", KgEntity), ("kg_relations", KgRelation),
                        ("kg_events", KgEvent), ("kg_character_state", KgCharacterState),
                        ("kg_foreshadows", KgForeshadow), ("kg_mentions", KgMention),
                        ("kg_entity_knowledge", KgEntityKnowledge)]:
        counts[name] = db.scalar(select(func.count()).select_from(model).where(model.project_id == PROJECT_ID))

    # 检索性能（跑 5 次取平均）
    retrieve_times = []
    for k in range(5):
        t1 = time.time()
        sl = slice_for_prompt(db, PROJECT_ID, n_segments, window=5, active_characters=CHARS[:5])
        retrieve_times.append((time.time() - t1) * 1000)
    avg_retrieve_ms = sum(retrieve_times) / len(retrieve_times)

    # rhythm_analysis 性能
    t2 = time.time()
    rhythm = rhythm_analysis(db, PROJECT_ID, window=min(10, n_segments))
    rhythm_ms = (time.time() - t2) * 1000

    print(f"  构建耗时: {build_t:.1f}s")
    print(f"  SQLite 文件: {db_size_mb:.1f} MB")
    print(f"  各表行数: {counts}")
    print(f"  检索 slice_for_prompt: {avg_retrieve_ms:.1f}ms (5 次平均)")
    print(f"  检索 rhythm_analysis: {rhythm_ms:.1f}ms")
    print(f"  切片实体数: {len(sl['recent_entities'])}, 状态数: {len(sl['character_states'])}")

    return {
        "n": n_segments,
        "build_s": build_t,
        "db_mb": db_size_mb,
        "counts": counts,
        "retrieve_ms": avg_retrieve_ms,
        "rhythm_ms": rhythm_ms,
        "slice_entities": len(sl["recent_entities"]),
    }


# ====================================================================
# 跑三级压力
# ====================================================================
# 100 段
r100 = run_level(100)
check_true("100段 entity 有值", r100["counts"]["kg_entities"] > 0)
check_true("100段 检索 < 100ms", r100["retrieve_ms"] < 100)

# 1000 段
r1000 = run_level(1000)
check_true("1000段 检索 < 500ms", r1000["retrieve_ms"] < 500)
check_true("1000段 SQLite < 100MB", r1000["db_mb"] < 100)

# 3000 段
r3000 = run_level(3000)
check_true("3000段 检索 < 2000ms", r3000["retrieve_ms"] < 2000)

# 百万字容量：3000 段 × 1500 字 ≈ 450 万字
total_chars = db.scalar(
    select(func.sum(func.length(Generation.text))).where(Generation.project_id == PROJECT_ID)
) or 0
print(f"\n总正文字数: {total_chars} ({total_chars / 10000:.0f} 万字)")
check_true("百万字容量（≥100万字）", total_chars >= 1_000_000)

# KG 增长曲线（行数 vs 段数应近似线性，不爆炸）
print(f"\nKG 增长曲线:")
print(f"  100段:  entities={r100['counts']['kg_entities']}  events={r100['counts']['kg_events']}  mentions={r100['counts']['kg_mentions']}")
print(f"  1000段: entities={r1000['counts']['kg_entities']}  events={r1000['counts']['kg_events']}  mentions={r1000['counts']['kg_mentions']}")
print(f"  3000段: entities={r3000['counts']['kg_entities']}  events={r3000['counts']['kg_events']}  mentions={r3000['counts']['kg_mentions']}")

# entity 应饱和（池只有 18 个），不随段数线性爆炸——这是软 schema 动态生长的关键验证
check_true("entity 饱和不爆炸（3000段 entity < 100）", r3000["counts"]["kg_entities"] < 100)

print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
