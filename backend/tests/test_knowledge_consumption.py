"""known_kind=secret/event 消费路径端到端测试。

覆盖（Novelium 情报追踪闭环的最后一环）：
- knowledge_match：event 简述匹配（全等/包含/标点变体/不同事件不误链）
  + is_incident_reference（"X的死因"=secret / "烧毁账本的今晚"=event / 实体名=非事件）
- batch_worker._persist_extraction 写侧：三类 known_kind 落库
  - entity → known_entity_id 指向实体（原有行为不回归）
  - event → 按库内 summary 解析 id；解析失败降级 known_text
  - secret → known_entity_id=None + known_text 存原文
  - 去重：同情报重复抽不炸不重；不同秘密同行同人各自成行
- retrieval_v3 读侧：known_facts 三类渲染（"事件："/"秘密：" 前缀）
  + 老行（known_kind=None）容错 + can_character_know 三类形态查询
- judge.check_secret_leak：说出未学秘密 → warn；已学/自己秘密/短秘密/无 secret 行不报
- db 迁移：老库（无 known_text 列）补列 + v3.0 索引补建 + 164 行回归（默认路径必跑索引）

跑法：python tests/test_knowledge_consumption.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from app.kg.knowledge_match import event_summary_match, is_incident_reference, normalize_fact
from app.kg import batch_worker as bw
from app.kg.retrieval_v3 import slice_for_prompt, format_slice_for_prompt, can_character_know
from app.kg.judge import check_secret_leak, RULES
from app.models import (
    Generation, KgEntity, KgEntityKnowledge, KgEvent, KgMention,
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
# 1. knowledge_match 单元
# ====================================================================
check("匹配：全等", event_summary_match("项目危机爆发", "项目危机爆发"), True)
check("匹配：LLM 简述是缩写", event_summary_match("项目危机", "项目危机爆发"), True)
check("匹配：库内是缩写", event_summary_match("顾辰逸出手解决了项目危机", "项目危机"), True)
check("匹配：标点变体", event_summary_match("项目，危机爆发！", "项目危机爆发"), True)
check("匹配：不同事件不误链", event_summary_match("林悠然提出辞职", "项目危机爆发"), False)
check("匹配：同主体不同事件不误链", event_summary_match("林悠然解决了危机", "林悠然制造了危机"), False)
check("匹配：空串", event_summary_match("", "项目危机"), False)

check("判别：死因型是 secret", is_incident_reference("父亲的死因"), False)
check("判别：身世型是 secret", is_incident_reference("顾辰逸的身世"), False)
check("判别：之字领属是 secret", is_incident_reference("账本之下隐藏的真相"), False)
check("判别：时间尾巴是 event", is_incident_reference("烧毁账本的今晚"), True)
check("判别：时间开头是 event", is_incident_reference("当晚，账本被烧毁"), True)
check("判别：整句时间词是 event", is_incident_reference("今晚"), True)
check("判别：实体名不是 event", is_incident_reference("今晚", {"今晚"}), False)
check("判别：空串", is_incident_reference(""), False)

check("规范化：剥标点空白", normalize_fact("父亲，并非因病·去世！"), "父亲并非因病去世")

# ====================================================================
# 2. 写侧：三类 known_kind 落库（走真实 _persist_extraction）
# ====================================================================
engine = create_engine("sqlite:///:memory:", future=True)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, future=True)
db = Session()

db.add(Generation(project_id=1, segment_no=1, text="林悠然推开门。", summary=None))
db.commit()

# 批 A：抽出实体 + 事件「项目危机爆发」
result_a = {
    "entities": [
        {"kind": "character", "canonical_name": "林悠然", "aliases": [], "attrs": {}},
        {"kind": "character", "canonical_name": "顾辰逸", "aliases": [], "attrs": {}},
    ],
    "events": [
        {"sequence": 1, "chapter": 1, "event_type": "incident",
         "summary": "项目危机爆发", "subject": "林悠然", "verb": "引发",
         "object": "", "location": "", "in_story_date": ""},
    ],
}
batch = [bw._QueueItem(project_id=1, segment_no=1, text="林悠然推开门。", chapter=1)]
stats_a = bw._persist_extraction(db, 1, result_a, batch)
check("批A events 落库", stats_a.events_added, 1)

evs = db.scalars(select(KgEvent).where(KgEvent.project_id == 1)).all()
check("批A event summary", evs[0].summary, "项目危机爆发")
crisis_ev = evs[0]

# 批 B：三类 known_kind + 坏行
result_b = {
    "entity_knowledge": [
        # entity：实体名解析（原有行为）
        {"character": "林悠然", "knows": "顾辰逸", "known_kind": "entity"},
        # event：能按库内 summary 解析到 id
        {"character": "林悠然", "knows": "项目危机爆发", "known_kind": "event"},
        # event：解析不到 → 降级 known_text
        {"character": "林悠然", "knows": "仓库深夜的交接", "known_kind": "event"},
        # secret：自由文本
        {"character": "顾辰逸", "knows": "父亲并非因病去世", "known_kind": "secret"},
        # 角色不存在 → 整条丢
        {"character": "路人甲", "knows": "什么都知道", "known_kind": "secret"},
    ],
}
stats_b = bw._persist_extraction(db, 1, result_b, batch)
check("批B knowledge stats", stats_b.entity_knowledge_added, 4)

rows = db.scalars(select(KgEntityKnowledge).where(KgEntityKnowledge.project_id == 1)).all()
ly = db.scalar(select(KgEntity).where(KgEntity.canonical_name == "林悠然"))
gy = db.scalar(select(KgEntity).where(KgEntity.canonical_name == "顾辰逸"))

by_kind: dict[str, list] = {}
for r in rows:
    by_kind.setdefault(r.known_kind or "", []).append(r)
check("entity 行 1 条", len(by_kind.get("entity", [])), 1)
ent_row = by_kind["entity"][0]
check("entity 行指向顾辰逸", ent_row.known_entity_id, gy.id)
check("entity 行无 known_text", ent_row.known_text, None)

check("event 行 2 条", len(by_kind.get("event", [])), 2)
ev_rows = {r.known_entity_id: r for r in by_kind["event"]}
check_true("event 解析行指向危机事件", crisis_ev.id in ev_rows)
check("event 解析行无 known_text", ev_rows[crisis_ev.id].known_text, None)
unresolved = [r for r in by_kind["event"] if r.known_entity_id is None]
check("event 降级行 1 条", len(unresolved), 1)
check("event 降级行存文本", unresolved[0].known_text, "仓库深夜的交接")

check("secret 行 1 条", len(by_kind.get("secret", [])), 1)
sec_row = by_kind["secret"][0]
check("secret 行 entity 是顾辰逸", sec_row.entity_id, gy.id)
check("secret 行 known_entity_id 空", sec_row.known_entity_id, None)
check("secret 行存文本", sec_row.known_text, "父亲并非因病去世")

# 去重：重复抽同批不新增
stats_b2 = bw._persist_extraction(db, 1, result_b, batch)
check("批B 重复抽取不新增", stats_b2.entity_knowledge_added, 0)
check("knowledge 总行数不变", db.scalar(
    select(func.count()).select_from(KgEntityKnowledge).where(KgEntityKnowledge.project_id == 1)), 4)

# 同一角色两条不同秘密各自成行
result_c = {"entity_knowledge": [
    {"character": "顾辰逸", "knows": "母亲留下半块玉佩", "known_kind": "secret"},
]}
stats_c = bw._persist_extraction(db, 1, result_c, batch)
check("同角色第二条秘密入库", stats_c.entity_knowledge_added, 1)

# ====================================================================
# 3. 读侧：slice known_facts 三类渲染
# ====================================================================
db.add_all([
    KgMention(project_id=1, entity_id=ly.id, segment_no=1, context_window="林悠然推开门"),
    KgMention(project_id=1, entity_id=gy.id, segment_no=1, context_window="顾辰逸在等她"),
])
db.commit()

sl = slice_for_prompt(db, 1, current_segment_no=1, window=5, active_characters=["林悠然", "顾辰逸"])
kf = {f["character"]: f["knows"] for f in sl["known_facts"]}
check_true("切片：林悠然情报含顾辰逸（entity）", "顾辰逸" in kf.get("林悠然", []))
check_true("切片：林悠然情报含事件标签", any(k.startswith("事件：") for k in kf.get("林悠然", [])))
check_true("切片：事件标签用库内 summary", any("项目危机爆发" in k for k in kf.get("林悠然", [])))
check_true("切片：林悠然情报含降级事件文本", any("仓库深夜的交接" in k for k in kf.get("林悠然", [])))
check_true("切片：顾辰逸情报含秘密标签", any(k.startswith("秘密：") for k in kf.get("顾辰逸", [])))
check_true("切片：秘密文本原文", any("父亲并非因病去世" in k for k in kf.get("顾辰逸", [])))

text_rendered = format_slice_for_prompt(sl)
check_true("渲染含已知情报块", "【角色已知情报】" in text_rendered)
check_true("渲染含秘密行", "秘密：父亲并非因病去世" in text_rendered)

# 老行容错：known_kind=None + known_entity_id 指向实体 → 仍按实体名渲染
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=gy.id, known_kind=None))
db.commit()
sl_old = slice_for_prompt(db, 1, current_segment_no=1, window=5, active_characters=["林悠然"])
kf_old = {f["character"]: f["knows"] for f in sl_old["known_facts"]}
check_true("老行（kind=None）按实体名渲染", "顾辰逸" in kf_old.get("林悠然", []))

# ====================================================================
# 4. can_character_know 三类形态
# ====================================================================
check("can_know：实体名", can_character_know(db, 1, "林悠然", "顾辰逸"), True)
check("can_know：事件标签", can_character_know(db, 1, "林悠然", "事件：项目危机爆发"), True)
check("can_know：事件裸描述", can_character_know(db, 1, "林悠然", "项目危机爆发"), True)
check("can_know：降级事件文本", can_character_know(db, 1, "林悠然", "事件：仓库深夜的交接"), True)
check("can_know：秘密标签", can_character_know(db, 1, "顾辰逸", "秘密：父亲并非因病去世"), True)
check("can_know：秘密裸文本", can_character_know(db, 1, "顾辰逸", "父亲并非因病去世"), True)
check("can_know：秘密标点变体", can_character_know(db, 1, "顾辰逸", "父亲，并非因病去世。"), True)
check("can_know：不知道的秘密", can_character_know(db, 1, "林悠然", "秘密：母亲留下半块玉佩"), False)
check("can_know：未学到的实体", can_character_know(db, 1, "顾辰逸", "天津港"), False)
check("can_know：不存在角色", can_character_know(db, 1, "张三", "顾辰逸"), False)

# ====================================================================
# 5. judge.check_secret_leak
# 现状：顾辰逸持有秘密 S1「父亲并非因病去世」+ S2「母亲留下半块玉佩」
# ====================================================================
# 5.1 持有人自述自己的秘密 → 不报（我的秘密我陈述，不算外泄）
db.add(KgMention(project_id=1, entity_id=gy.id, segment_no=2, context_window="顾辰逸说道"))
db.commit()
findings = check_secret_leak(db, 1, "顾辰逸说道：父亲并非因病去世，这件事我查过了。", segment_no=2)
check("泄密：持有人自述不报", len(findings), 0)

# 5.2 未学到的角色说出他人秘密 → warn（林悠然没学过 S1）
db.add(KgMention(project_id=1, entity_id=ly.id, segment_no=3, context_window="林悠然说道"))
db.commit()
findings = check_secret_leak(db, 1, "林悠然说道：父亲并非因病去世。", segment_no=3)
check("泄密：说出未学秘密报 warn", len(findings), 1)
check_true("泄密 rule 正确", findings and findings[0].rule == "secret_leak")
check_true("泄密 severity warn", findings and findings[0].severity == "warn")
check_true("泄密说话人是林悠然", findings and findings[0].entity == "林悠然")

# 5.3 标点/空白变体仍命中
findings = check_secret_leak(db, 1, "林悠然说道：父亲，并非因病去世……", segment_no=3)
check("泄密：标点变体命中", len(findings), 1)

# 5.4 学到之后 → 不报
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=None,
                         known_kind="secret", known_text="父亲并非因病去世"))
db.commit()
findings = check_secret_leak(db, 1, "林悠然说道：父亲并非因病去世。", segment_no=3)
check("泄密：已学到不报", len(findings), 0)

# 5.5 第二条未学秘密 → 报；学到后不报
findings = check_secret_leak(db, 1, "林悠然说道：母亲留下半块玉佩。", segment_no=3)
check("泄密：第二条未学秘密报", len(findings), 1)
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=None,
                         known_kind="secret", known_text="母亲留下半块玉佩"))
db.commit()
findings = check_secret_leak(db, 1, "林悠然说道：母亲留下半块玉佩。", segment_no=3)
check("泄密：学到第二条后不报", len(findings), 0)

# 5.6 短秘密（<6 字）不判——正常叙述易撞车
db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=None,
                         known_kind="secret", known_text="她撒谎"))
db.commit()
findings = check_secret_leak(db, 1, "顾辰逸说道：她撒谎了。", segment_no=2)
check("泄密：短秘密不判", len(findings), 0)

# 5.7 无 secret 行的项目整体不判
findings = check_secret_leak(db, 999, "任何人说道任何话。", segment_no=1)
check("泄密：空项目不报", len(findings), 0)

# RULES 注册（judge_segment 集成在 test_v30_judge.py，那里同步改 6 条）
check("RULES 含 secret_leak", "secret_leak" in RULES, True)
check("RULES 共 6 条", len(RULES), 6)

# ====================================================================
# 6. db 迁移：老库补 known_text 列 + 索引补建 + 164 行回归
# ====================================================================
old = create_engine("sqlite:///:memory:", future=True)
with old.begin() as conn:
    # 模拟 6e6f406 时代的老库：9 表齐但无 known_text 列
    conn.execute(text(
        "CREATE TABLE kg_entity_knowledge ("
        " id INTEGER PRIMARY KEY, project_id INTEGER, entity_id INTEGER,"
        " known_entity_id INTEGER, known_kind VARCHAR(16),"
        " learned_at_event_id INTEGER, learned_at_chapter INTEGER, confidence FLOAT)"
    ))
from app.db import ensure_schema
ensure_schema(old)  # 幂等补列 + 补索引（默认路径也必须跑——164 行回归）
with old.connect() as conn:
    cols = {r[1] for r in conn.execute(text("PRAGMA table_info(kg_entity_knowledge)")).fetchall()}
    idx = {r[0] for r in conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}
check("迁移：老库补 known_text 列", "known_text" in cols, True)
check("迁移：老库补 v3.0 索引", "ix_kg_entity_knowledge_project_entity" in idx, True)
ensure_schema(old)  # 二次调用幂等
check("迁移：ensure_schema 幂等", True, True)

# 164 行回归：迁移循环里不允许再有「target 判定三元」——它让默认路径（init_db）跳过索引
db_src = (BACKEND / "app" / "db.py").read_text(encoding="utf-8")
check_true("回归：_migrate 无 target 三元短路",
           "CREATE INDEX IF NOT EXISTS" not in db_src or
           "if target_engine is not None else engine" not in db_src.split("CREATE INDEX IF NOT EXISTS")[1].split("\n")[0])

# ====================================================================
# 汇总
# ====================================================================
print(f"\n结果：{PASS} 通过, {FAIL} 失败" + (f" -> {FAILED}" if FAILED else ""))
sys.exit(0 if FAIL == 0 else 1)
