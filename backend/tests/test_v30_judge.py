"""v3.0 独立 Judge Agent 测试。

覆盖 5 类规则（各自正反例对拍）+ judge_segment 集成 + engine 接入：
- teleportation 瞬移：location 状态与事件地点矛盾 + 无移动动词 → err；有移动动词 → 不报
- unauthorized_knowledge 预知外挂：有 knowledge 记录时角色提及未学到的秘密 → err；
  无 knowledge 记录 → 不报（全项目无记录会满屏误报，保守）
- impossible_sequences 时间穿越：sequence 递增但年份递减 → err；递增 → 不报；
  年号纪年（贞观三年→贞观元年）换算公元年后同 era 倒挂 → err；换年号 → 不报
- foreshadow_expiry 伏笔超期：planned_payoff 过期未 paid_off → warn；已回收 → 不报
- language_mix 语言混入：拉丁/假名串 → warn；白名单 DNA/CEO → 不报；纯中文 → 不报
- judge_segment 集成：单规则失败不影响其他（幂等兜底）
- engine 接入：_merge_segment_into_graph 的 stats 含 judge 字段

跑法：python tests/test_v30_judge.py
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
from app.kg.judge import (
    judge_segment, check_teleportation, check_unauthorized_knowledge,
    check_impossible_sequences, check_foreshadow_expiry, check_language_mix,
)
from app.models import (
    KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow, KgMention,
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

# —— 预置实体：林悠然（character）/ 办公室 / 天津港（location）——
ly = KgEntity(project_id=1, kind="character", canonical_name="林悠然", attrs={"secrets": ["母亲之死"]})
office = KgEntity(project_id=1, kind="location", canonical_name="办公室")
port = KgEntity(project_id=1, kind="location", canonical_name="天津港")
gy = KgEntity(project_id=1, kind="character", canonical_name="顾辰逸", attrs={"secrets": ["父亲是董事长"]})
db.add_all([ly, office, port, gy])
db.commit()

# ====================================================================
# 1. teleportation 瞬移
# ====================================================================
# 林悠然 location 状态 = 办公室（段1）
db.add(KgCharacterState(project_id=1, character_id=ly.id, segment_no=1,
                        state_type="location", new_value="办公室"))
# 段2 事件：林悠然 在 天津港（location_id=port.id）
db.add(KgEvent(project_id=1, sequence=1, segment_no=2, event_type="incident",
               summary="林悠然出现在天津港", subject_id=ly.id, location_id=port.id))
# 段2 提到林悠然
db.add(KgMention(project_id=1, entity_id=ly.id, segment_no=2, context_window="林悠然"))
db.commit()

# 无反移动动词 → 瞬移告警
findings = check_teleportation(db, 1, "林悠然站在天津港的码头上。", segment_no=2)
check("瞬移：无反移动动词报 err", len(findings), 1)
check_true("瞬移 rule 正确", findings and findings[0].rule == "teleportation")
check_true("瞬移 severity err", findings and findings[0].severity == "err")

# 有移动动词 → 正常移动，不报
findings2 = check_teleportation(db, 1, "林悠然赶到天津港的码头上。", segment_no=2)
check("瞬移：有移动动词不报", len(findings2), 0)

# ====================================================================
# 2. unauthorized_knowledge 预知外挂
# ====================================================================
# 先无 knowledge 记录 → 不报（保守，全项目无记录会满屏误报）
db.add(KgMention(project_id=1, entity_id=ly.id, segment_no=3, context_window="林悠然说道"))
db.add(KgMention(project_id=1, entity_id=gy.id, segment_no=3, context_window="顾辰逸"))
db.commit()
findings = check_unauthorized_knowledge(db, 1, "林悠然说道：顾辰逸的父亲是董事长。", segment_no=3)
check("预知：无 knowledge 记录不报", len(findings), 0)

# 加一条林悠然→办公室的学习记录（说明有 knowledge 体系），但没学到顾辰逸
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=office.id,
                         known_kind="entity", learned_at_chapter=1))
db.commit()
findings = check_unauthorized_knowledge(db, 1, "林悠然说道：顾辰逸的父亲是董事长。", segment_no=3)
check("预知：有记录但未学到目标 → 报 err", len(findings), 1)
check_true("预知 rule 正确", findings and findings[0].rule == "unauthorized_knowledge")

# 学到后 → 不报
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=gy.id,
                         known_kind="entity", learned_at_chapter=2))
db.commit()
findings = check_unauthorized_knowledge(db, 1, "林悠然说道：顾辰逸的父亲是董事长。", segment_no=3)
check("预知：已学到目标不报", len(findings), 0)

# ====================================================================
# 3. impossible_sequences 时间穿越
# ====================================================================
db.add_all([
    KgEvent(project_id=1, sequence=10, event_type="incident", summary="甲", in_story_date="2020年3月"),
    KgEvent(project_id=1, sequence=11, event_type="incident", summary="乙", in_story_date="2018年5月"),
])
db.commit()
findings = check_impossible_sequences(db, 1, "", segment_no=1)
check("时间穿越：年份递减报 err", len(findings), 1)
check_true("时间穿越 rule 正确", findings and findings[0].rule == "impossible_sequences")

# 递增 → 不报
db.add_all([
    KgEvent(project_id=1, sequence=20, event_type="incident", summary="丙", in_story_date="2021年"),
    KgEvent(project_id=1, sequence=21, event_type="incident", summary="丁", in_story_date="2022年"),
])
db.commit()
findings_inc = check_impossible_sequences(db, 1, "", segment_no=1)
# 只有 10→11 一对递减（20→21 递增）
check("时间穿越：递增不新增告警", len(findings_inc), 1)

# 非年份时间（年号纪年）现在也判——era_convert 换算公元年后按同 era 比较
# （口径 C，见 docs/kg-judge-era-dimension-design.md）
db.add_all([
    KgEvent(project_id=1, sequence=30, event_type="incident", summary="戊", in_story_date="贞观三年秋"),
    KgEvent(project_id=1, sequence=31, event_type="incident", summary="己", in_story_date="贞观元年春"),
])
db.commit()
findings_cn = check_impossible_sequences(db, 1, "", segment_no=1)
check("时间穿越：年号纪年倒挂报 err", len(findings_cn), 2)  # 10→11 公元年对 + 30→31 年号对
check_true("时间穿越：年号对 rule 正确",
           findings_cn and findings_cn[-1].rule == "impossible_sequences")

# 换年号（贞观末→永徽元）是正常历史推进，不报
db.add_all([
    KgEvent(project_id=1, sequence=40, event_type="incident", summary="庚", in_story_date="贞观二十三年"),
    KgEvent(project_id=1, sequence=41, event_type="incident", summary="辛", in_story_date="永徽元年"),
])
db.commit()
findings_era_switch = check_impossible_sequences(db, 1, "", segment_no=1)
check("时间穿越：换年号不新增告警", len(findings_era_switch), 2)

# ====================================================================
# 4. foreshadow_expiry 伏笔超期
# ====================================================================
fs1 = KgForeshadow(project_id=1, name="辰逸父亲是董事长", status="developing",
                   planted_chapter=3, planned_payoff_chapter=50)
db.add(fs1)
db.commit()
# current_chapter=60 → 超期
findings = check_foreshadow_expiry(db, 1, "", segment_no=1, current_chapter=60)
check("伏笔超期：过期报 warn", len(findings), 1)
check_true("伏笔超期 rule 正确", findings and findings[0].rule == "foreshadow_expiry")
check_true("伏笔超期 severity warn", findings and findings[0].severity == "warn")

# current_chapter=40 → 未过期
findings = check_foreshadow_expiry(db, 1, "", segment_no=1, current_chapter=40)
check("伏笔超期：未过期不报", len(findings), 0)

# 已 paid_off → 不报
fs1.status = "paid_off"
db.commit()
findings = check_foreshadow_expiry(db, 1, "", segment_no=1, current_chapter=60)
check("伏笔超期：已回收不报", len(findings), 0)

# ====================================================================
# 5. language_mix 语言混入
# ====================================================================
findings = check_language_mix("陈默还站在那里，draußen，没走开。")
check("语言混入：德语报 warn", len(findings), 1)
check_true("语言混入 rule 正确", findings and findings[0].rule == "language_mix")
check_true("语言混入 命中 draußen", findings and "draußen" in findings[0].message)

findings = check_language_mix("他拿出 DNA 检测报告。")
check("语言混入：白名单 DNA 不报", len(findings), 0)

findings = check_language_mix("林悠然推开门，顾辰逸在等她。")
check("语言混入：纯中文不报", len(findings), 0)

findings = check_language_mix("")
check("语言混入：空文本不报", len(findings), 0)

# ====================================================================
# 6. judge_segment 集成（单规则失败不影响其他）
# ====================================================================
# 正常调用
result = judge_segment(db, 1, "陈默 draußen 还站在那里。", segment_no=2)
check_true("集成：返回 findings 键", "findings" in result)
check_true("集成：返回 rules 键", "rules" in result)
check_true("集成：6 条规则登记", len(result["rules"]) == 6)
# 空文本/空库不炸
result_empty = judge_segment(db, 999, "", segment_no=1)
check("集成：空项目空文本 findings 空", result_empty["findings"], [])

# ====================================================================
# 7. engine 接入（静态检查）
# ====================================================================
engine_py = (BACKEND / "app" / "generation" / "engine.py").read_text(encoding="utf-8")
check_true("engine 调 judge_segment", "judge_segment" in engine_py)
check_true("engine stats 含 judge 键", 'stats["judge"]' in engine_py)

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
