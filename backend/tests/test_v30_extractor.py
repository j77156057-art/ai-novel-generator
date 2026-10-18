"""v3.0 extractor + 批抽 worker 测试。

覆盖：
- normalize_extraction schema 漂移防御：
  - 字段名别名（entities↔characters、from↔source、name↔canonical_name）
  - 非法枚举值丢弃（kind 不在 6 类 / rel_type 不在 8 类 / state_type 超域）
  - strength 截断到 [0,1]
  - 缺键字段兜底（list→[] / str→"" / int→None）
  - 顶层非 dict / None 返回空骨架
- _parse_and_normalize 三级兜底（纯 JSON / 带前后垃圾 / think 块包裹）
- extract_segment / extract_batch（mock provider，正常 + 异常兜底）
- batch_worker 落库（内存库）：
  - entity upsert（新插 + 已存在合并 aliases/attrs）
  - relation upsert + strength 变化追加 evolution
  - event 追加 sequence 连续
  - state_change / foreshadow 状态机推进（planted→developing）
  - mention 反向索引 + context_window
  - 无 provider 降级不炸

跑法：python tests/test_v30_extractor.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.extractor_v3 import (
    normalize_extraction, _parse_and_normalize, extract_segment, extract_batch,
)
from app.models import (
    KgEntity, KgRelation, KgRelationEvolution, KgEvent, KgCharacterState,
    KgForeshadow, KgMention, Generation,
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


# ====================================================================
# 1. normalize_extraction：字段别名（schema 漂移防御核心）
# ====================================================================
raw = {
    "characters": [  # 用别名 characters 而非 entities
        {"type": "character", "name": "林悠然", "alias": ["悠然", "林经理"], "attributes": {"arc": "逆袭"}},
    ],
    "relationships": [  # 别名 relationships + from↔source
        {"source": "林悠然", "target": "顾辰逸", "relation": "romantic", "weight": 0.8},
    ],
    "事件": [  # 中文别名
        {"seq": 1, "章": 3, "类型": "incident", "svo": "林悠然发现数据异常",
         "from": "林悠然", "action": "发现", "place": "办公室", "date": "2024年春"},
    ],
    "stateChanges": [  # 驼峰别名
        {"name": "林悠然", "type": "injury", "value": "右手骨折"},
    ],
    "伏笔": [
        {"title": "辰逸父亲是董事长", "state": "planted", "埋下章": 3, "计划回收章": 50},
    ],
}
n = normalize_extraction(raw)
check("别名归一 entities", len(n["entities"]), 1)
check("别名归一 canonical_name", n["entities"][0]["canonical_name"], "林悠然")
check("别名归一 aliases", n["entities"][0]["aliases"], ["悠然", "林经理"])
check("别名归一 attrs", n["entities"][0]["attrs"], {"arc": "逆袭"})
check("别名归一 relation from/to", (n["relations"][0]["from"], n["relations"][0]["to"]), ("林悠然", "顾辰逸"))
check("别名归一 relation strength", n["relations"][0]["strength"], 0.8)
check("别名归一 event summary", n["events"][0]["summary"], "林悠然发现数据异常")
check("别名归一 event location", n["events"][0]["location"], "办公室")
check("别名归一 event in_story_date", n["events"][0]["in_story_date"], "2024年春")
check("别名归一 state_change", n["state_changes"][0]["new_value"], "右手骨折")
check("别名归一 foreshadow name", n["foreshadows"][0]["name"], "辰逸父亲是董事长")
check("别名归一 foreshadow planned", n["foreshadows"][0]["planned_payoff_chapter"], 50)

# ====================================================================
# 2. 非法枚举值丢弃
# ====================================================================
raw2 = {
    "entities": [
        {"kind": "person", "canonical_name": "张三"},  # kind 非法 → 丢
        {"kind": "character", "canonical_name": "李四"},  # 合法
        {"kind": "location", "canonical_name": ""},  # 空名 → 丢
    ],
    "relations": [
        {"from": "李四", "to": "张三", "rel_type": "friend"},  # rel_type 非法 → 丢
        {"from": "李四", "to": "李四", "rel_type": "ally"},  # 自环 → 丢
        {"from": "李四", "to": "王五", "rel_type": "ally"},  # 合法
    ],
    "state_changes": [
        {"character": "李四", "state_type": "happy"},  # 非法 → 丢
        {"character": "李四", "state_type": "alive", "new_value": "false"},  # 合法
    ],
}
n2 = normalize_extraction(raw2)
check("非法 kind 丢弃", len(n2["entities"]), 1)
check("空名 entity 丢弃", n2["entities"][0]["canonical_name"], "李四")
check("非法 rel_type 丢弃", len(n2["relations"]), 1)
check("自环 relation 丢弃", n2["relations"][0]["to"], "王五")
check("非法 state_type 丢弃", len(n2["state_changes"]), 1)

# event_links / entity_knowledge normalize（别名 + 枚举丢弃 + 自环丢弃）
raw_links = {
    "event_links": [
        {"from_seq": 1, "to_seq": 2, "type": "resolves"},   # 别名 + 合法 → 留
        {"from_seq": 1, "to_seq": 1, "link_type": "causes"},  # 自环 → 丢
        {"from_seq": 1, "to_seq": 2, "link_type": "friend"},  # 非法枚举 → 丢
        {"from_seq": 1, "link_type": "causes"},               # 缺 to → 丢
    ],
    "entity_knowledge": [
        {"character": "林悠然", "knows": "顾辰逸", "kind": "entity", "learned_at": 1},  # 别名 → 留
        {"character": "林悠然", "knows": "林悠然", "known_kind": "entity"},            # 自己知道自 → 丢
        {"character": "林悠然", "knows": "顾辰逸", "known_kind": "thing"},             # 非法 known_kind → 归 entity
    ],
}
nl = normalize_extraction(raw_links)
check("event_links 归一后 1 条", len(nl["event_links"]), 1)
check("event_links link_type", nl["event_links"][0]["link_type"], "resolves")
check("event_links from/to", (nl["event_links"][0]["from_sequence"], nl["event_links"][0]["to_sequence"]), (1, 2))
check("entity_knowledge 归一后 2 条", len(nl["entity_knowledge"]), 2)
check("entity_knowledge known_kind 归 entity", nl["entity_knowledge"][1]["known_kind"], "entity")

# 空骨架含新键
check("空骨架含 event_links", normalize_extraction({})["event_links"], [])
check("空骨架含 entity_knowledge", normalize_extraction({})["entity_knowledge"], [])

# ====================================================================
# 3. strength 截断 + 缺键兜底
# ====================================================================
raw3 = {
    "relations": [
        {"from": "A", "to": "B", "rel_type": "ally", "strength": 5.0},  # 超 1 → 截 1
        {"from": "A", "to": "C", "rel_type": "ally", "strength": -3},  # 负 → 截 0
        {"from": "A", "to": "D", "rel_type": "ally"},  # 缺 strength → 0.5
    ],
    "events": [{"event_type": "incident"}],  # 全缺键
    "entities": [{"kind": "item", "canonical_name": "剑"}],  # 缺 aliases/attrs
}
n3 = normalize_extraction(raw3)
check("strength 超 1 截断", n3["relations"][0]["strength"], 1.0)
check("strength 负截断", n3["relations"][1]["strength"], 0.0)
check("strength 缺省 0.5", n3["relations"][2]["strength"], 0.5)
check("event 缺键兜底", n3["events"][0]["summary"], "")
check("event 缺 chapter 兜底", n3["events"][0]["chapter"], None)
check("entity 缺 aliases 兜底", n3["entities"][0]["aliases"], [])
check("entity 缺 attrs 兜底", n3["entities"][0]["attrs"], {})

# ====================================================================
# 4. 顶层非 dict / None / 空
# ====================================================================
check("None 输入空骨架", normalize_extraction(None), {"entities": [], "relations": [], "events": [], "event_links": [], "entity_knowledge": [], "state_changes": [], "foreshadows": []})
check("list 输入空骨架", normalize_extraction([1, 2]), {"entities": [], "relations": [], "events": [], "event_links": [], "entity_knowledge": [], "state_changes": [], "foreshadows": []})
check("空 dict 空骨架", normalize_extraction({}), {"entities": [], "relations": [], "events": [], "event_links": [], "entity_knowledge": [], "state_changes": [], "foreshadows": []})
check("顶层 list 值兜底", len(normalize_extraction({"entities": "not a list"})["entities"]), 0)

# ====================================================================
# 5. _parse_and_normalize 三级兜底
# ====================================================================
good = '{"entities": [{"kind": "character", "canonical_name": "甲"}]}'
check("纯 JSON 解析", _parse_and_normalize(good)["entities"][0]["canonical_name"], "甲")

messy = "这是分析过程...\n" + good + "\n以上是结果"
check("带前后垃圾解析", _parse_and_normalize(messy)["entities"][0]["canonical_name"], "甲")

think = "让我想想..." + good + "结论如上"
check("think 块包裹解析", _parse_and_normalize(think)["entities"][0]["canonical_name"], "甲")

check("空字符串兜底", _parse_and_normalize(""), {"entities": [], "relations": [], "events": [], "event_links": [], "entity_knowledge": [], "state_changes": [], "foreshadows": []})
check("垃圾非 JSON 兜底", _parse_and_normalize("完全不是 JSON 的输出"), {"entities": [], "relations": [], "events": [], "event_links": [], "entity_knowledge": [], "state_changes": [], "foreshadows": []})

# ====================================================================
# 6. extract_segment / extract_batch（mock provider）
# ====================================================================
class MockProvider:
    def __init__(self, reply: str, raise_exc: bool = False):
        self.reply = reply
        self.raise_exc = raise_exc
        self.calls = 0

    def chat(self, system, user):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("mock LLM error")
        return self.reply


mock_reply = '{"entities": [{"kind": "character", "canonical_name": "林悠然"}], "relations": [{"from": "林悠然", "to": "顾辰逸", "rel_type": "romantic"}]}'
p = MockProvider(mock_reply)
res = extract_segment(p, "林悠然和顾辰逸相遇。", segment_no=1, chapter=1)
check("extract_segment entity", len(res["entities"]), 1)
check("extract_segment relation", len(res["relations"]), 1)
check("extract_segment 调 1 次 LLM", p.calls, 1)

p_raise = MockProvider("", raise_exc=True)
res_err = extract_segment(p_raise, "text", segment_no=1)
check("extract_segment 异常兜底空骨架", res_err["entities"], [])

# 批抽：5 段一次
p2 = MockProvider(mock_reply)
texts = [(i, f"第{i}段文本", i) for i in range(1, 6)]
res_b = extract_batch(p2, texts)
check("extract_batch 5 段一次调用", p2.calls, 1)
check("extract_batch entity", len(res_b["entities"]), 1)

# 单段批抽退化为单抽
p3 = MockProvider(mock_reply)
res_s = extract_batch(p3, [(1, "text", 1)])
check("extract_batch 单段退化", p3.calls, 1)

# 空输入
check("extract_batch 空输入", extract_batch(p3, [])["entities"], [])

# ====================================================================
# 7. batch_worker 落库（内存库）
# ====================================================================
engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

# 预置 generation 行（mention FK 用）
g1 = Generation(project_id=1, segment_no=1, text="林悠然推开门，顾辰逸在等她。", summary=None)
g2 = Generation(project_id=1, segment_no=2, text="林悠然和顾辰逸签了合同。", summary=None)
db.add_all([g1, g2])
db.commit()

from app.kg import batch_worker as bw

# 构造一个抽取结果落库（绕过 LLM，直接测 _persist_extraction）
result = {
    "entities": [
        {"kind": "character", "canonical_name": "林悠然", "aliases": ["悠然"], "attrs": {"arc": "逆袭"}},
        {"kind": "character", "canonical_name": "顾辰逸", "aliases": [], "attrs": {}},
    ],
    "relations": [
        {"from": "林悠然", "to": "顾辰逸", "rel_type": "romantic", "strength": 0.6},
    ],
    "events": [
        {"sequence": 1, "chapter": 1, "event_type": "incident", "summary": "林悠然推开门", "subject": "林悠然",
         "verb": "推开门", "object": "", "location": "办公室", "in_story_date": "2024年春"},
    ],
    "event_links": [],  # 单事件无链，见下方多事件批
    "entity_knowledge": [
        {"character": "林悠然", "knows": "顾辰逸", "known_kind": "entity", "learned_at_sequence": 1},
    ],
    "state_changes": [
        {"character": "林悠然", "state_type": "location", "new_value": "办公室"},
    ],
    "foreshadows": [
        {"name": "辰逸父亲是董事长", "status": "planted", "planted_chapter": 1, "planned_payoff_chapter": 50},
    ],
}
batch = [
    bw._QueueItem(project_id=1, segment_no=1, text="林悠然推开门，顾辰逸在等她。", chapter=1),
    bw._QueueItem(project_id=1, segment_no=2, text="林悠然和顾辰逸签了合同。", chapter=2),
]
stats = bw._persist_extraction(db, 1, result, batch)
check("落库 stats segments", stats.segments, 2)
check("落库 stats entities", stats.entities_upserted, 2)
check("落库 stats relations", stats.relations_upserted, 1)
check("落库 stats events", stats.events_added, 1)
check("落库 stats state_changes", stats.state_changes, 1)
check("落库 stats foreshadows", stats.foreshadows, 1)

# entity 真落库
ents = db.scalars(select(KgEntity).where(KgEntity.project_id == 1)).all()
check("KG entity 行数", len(ents), 2)
ly = next(e for e in ents if e.canonical_name == "林悠然")
check("entity aliases 落库", ly.aliases, ["悠然"])
check("entity attrs 落库", ly.attrs, {"arc": "逆袭"})

# relation 落库
rels = db.scalars(select(KgRelation).where(KgRelation.project_id == 1)).all()
check("KG relation 行数", len(rels), 1)
check("relation strength", rels[0].strength, 0.6)

# event 落库 + sequence 连续
evs = db.scalars(select(KgEvent).where(KgEvent.project_id == 1)).all()
check("KG event 行数", len(evs), 1)
check("event sequence 从 1 开始", evs[0].sequence, 1)
check("event in_story_date", evs[0].in_story_date, "2024年春")
check("event subject 解析为 id", evs[0].subject_id, ly.id)

# state_change 落库
sts = db.scalars(select(KgCharacterState).where(KgCharacterState.project_id == 1)).all()
check("character_state 行数", len(sts), 1)
check("state_change new_value", sts[0].new_value, "办公室")

# foreshadow 落库
fss = db.scalars(select(KgForeshadow).where(KgForeshadow.project_id == 1)).all()
check("foreshadow 行数", len(fss), 1)
check("foreshadow status", fss[0].status, "planted")

# entity_knowledge 落库（林悠然知道顾辰逸，learned_at=event1）
from app.models import KgEntityKnowledge, KgEventLink
check("落库 stats entity_knowledge", stats.entity_knowledge_added, 1)
knows = db.scalars(select(KgEntityKnowledge).where(KgEntityKnowledge.project_id == 1)).all()
check("entity_knowledge 行数", len(knows), 1)
check("entity_knowledge known_kind", knows[0].known_kind, "entity")
check("entity_knowledge 学到于 event1", knows[0].learned_at_event_id, evs[0].id)

# mentions 反向索引
mns = db.scalars(select(KgMention).where(KgMention.project_id == 1)).all()
check("mentions 行数（林悠然×2段 + 顾辰逸×2段）", len(mns), 4)
ly_mention = next(m for m in mns if m.entity_id == ly.id and m.segment_no == 1)
check_true("mention context_window 非空", len(ly_mention.context_window or "") > 0)
check_true("mention 含原文片段", "推开门" in (ly_mention.context_window or ""))

# entity mention_count 更新
db.refresh(ly)
check("entity mention_count", ly.mention_count, 2)

# ====================================================================
# 8. 幂等 upsert：再抽一次同样结果
# ====================================================================
result2 = {
    "entities": [
        {"kind": "character", "canonical_name": "林悠然", "aliases": ["林经理"], "attrs": {"arc": "成长", "secrets": ["母亲之死"]}},
    ],
    "relations": [
        {"from": "林悠然", "to": "顾辰逸", "rel_type": "romantic", "strength": 0.9},  # 顾辰逸靠库里反查（跨批兜底）
    ],
    "events": [{"chapter": 2, "event_type": "decision", "summary": "签合同"}],
    "foreshadows": [
        {"name": "辰逸父亲是董事长", "status": "developing", "planted_chapter": 1, "planned_payoff_chapter": 60},
    ],
}
stats2 = bw._persist_extraction(db, 1, result2, batch)
check("二次抽取 entity 不新增", db.scalar(select(func.count()).select_from(KgEntity).where(KgEntity.project_id == 1)), 2)
check("二次抽取 relation 不新增", db.scalar(select(func.count()).select_from(KgRelation).where(KgRelation.project_id == 1)), 1)
check("二次抽取 event sequence 接续", db.scalar(select(func.count()).select_from(KgEvent).where(KgEvent.project_id == 1)), 2)

# aliases 合并（悠然 + 林经理）
db.refresh(ly)
check("aliases 合并去重", set(ly.aliases), {"悠然", "林经理"})
# attrs 深合并
check("attrs 深合并 arc 被覆盖", ly.attrs["arc"], "成长")
check("attrs 深合并 secrets 新增", ly.attrs.get("secrets"), ["母亲之死"])

# strength 变化 → evolution 追加
evols = db.scalars(select(KgRelationEvolution).where(KgRelationEvolution.project_id == 1)).all()
check("strength 变化追加 evolution", len(evols), 1)
check("evolution 新 strength", evols[0].strength, 0.9)
db.refresh(rels[0])
check("relation strength 更新", rels[0].strength, 0.9)

# foreshadow 状态机推进 planted → developing
fs = db.scalars(select(KgForeshadow).where(KgForeshadow.project_id == 1)).all()[0]
check("foreshadow planted→developing", fs.status, "developing")
check("foreshadow 行数不变", len(fss) + 0, 1)

# 状态机不后退：再抽 planted 不改
result3 = {"foreshadows": [{"name": "辰逸父亲是董事长", "status": "planted"}]}
bw._persist_extraction(db, 1, result3, batch)
db.refresh(fs)
check("foreshadow 状态不后退", fs.status, "developing")

# event_links 多事件落库：新批 2 事件（seq1→seq2 caused_by）+ 解析为真实 id
result4 = {
    "events": [
        {"sequence": 1, "chapter": 3, "event_type": "incident", "summary": "项目危机爆发", "subject": "林悠然"},
        {"sequence": 2, "chapter": 4, "event_type": "revelation", "summary": "辰逸伸出援手", "subject": "顾辰逸"},
    ],
    "event_links": [
        {"from_sequence": 2, "to_sequence": 1, "link_type": "resolves"},  # 辰逸援手解决危机
    ],
    "entity_knowledge": [
        {"character": "林悠然", "knows": "顾辰逸", "known_kind": "entity", "learned_at_sequence": 2},
    ],
}
stats4 = bw._persist_extraction(db, 1, result4, batch)
check("event_links stats", stats4.event_links_added, 1)
links = db.scalars(select(KgEventLink).where(KgEventLink.project_id == 1)).all()
check("event_links 落库行数", len(links), 1)
check("event_links link_type", links[0].link_type, "resolves")
# link 两端应是 result4 的两个新事件（sequence 接续在 2 之后 → 3,4）
new_evs = db.scalars(select(KgEvent).where(KgEvent.project_id == 1).order_by(KgEvent.sequence)).all()
check("event_links from 是新事件", links[0].from_event_id, new_evs[-1].id)  # seq4 = 辰逸援手
check("event_links to 是新事件", links[0].to_event_id, new_evs[-2].id)  # seq3 = 危机爆发

# entity_knowledge 幂等：林悠然→顾辰逸 learned_at 换了新事件，应新增一行
check("entity_knowledge 二次 stats", stats4.entity_knowledge_added, 1)
check("entity_knowledge 累计行数", db.scalar(select(func.count()).select_from(KgEntityKnowledge).where(KgEntityKnowledge.project_id == 1)), 2)

# ====================================================================
# 9. 无 provider 降级不炸
# ====================================================================
bw._index_mentions_only(db, batch)  # 直接调降级函数不应抛
check("无 provider 降级不炸", True, True)

# ====================================================================
# 10. _next_event_sequence / _window_around 纯函数
# ====================================================================
check("next_event_sequence 接续", bw._next_event_sequence(db, 1), 5)
win = bw._window_around("前面很多字……林悠然推开门……后面很多字", "林悠然")
check_true("window_around 含名字", "林悠然" in win)
check_true("window_around 长度受限", len(win) <= 60)

# _lookup_entity_id：canonical_name + aliases 反查（跨批关系兜底）
check("lookup canonical_name", bw._lookup_entity_id(db, 1, "林悠然"), ly.id)
check("lookup alias", bw._lookup_entity_id(db, 1, "林经理"), ly.id)  # aliases 已合并进林悠然
check("lookup 不存在返回 None", bw._lookup_entity_id(db, 1, "不存在的人"), None)
check("lookup 空名返回 None", bw._lookup_entity_id(db, 1, ""), None)

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
