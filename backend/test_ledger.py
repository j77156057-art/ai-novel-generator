"""事实账本 + 别名归一回归。

两组断言：
  A. 纯函数 `plan_alias_merges`——不碰库，把规则边界钉死（含实测出来的错误合并反例）
  B. 落库链路 `upsert_facts` → `resolve_aliases`——含图谱改写与 get_reminders 效果

反例清单来自真实语料实测（1066 分片/120 万字），不是想象出来的：
  姐儿→凤姐儿（泛称）、元帅→康元帅（官职）、满面→容满面（残片）、
  宝玉→甄宝玉（不同人）、尤二姐→尤二（「姐」是名字组成部分）、贾母→贾母王（并列短语）
这些**必须**停留在 review，绝不能进 auto。
"""

import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_ledger_test.db")
if os.path.exists(_DB):
    try:
        os.remove(_DB)
    except OSError:
        pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.db import Base, SessionLocal  # noqa: E402
from sqlalchemy import select  # noqa: E402
from app.kg.ledger import (  # noqa: E402
    apply_merges,
    canonical_name,
    facts_from_extraction,
    get_facts,
    plan_alias_merges,
    resolve_aliases,
    upsert_facts,
)
from app.kg.queries import get_reminders  # noqa: E402
from app.models import Fact, KgEdge, KgNode, Project  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


def fact(key, mentions=5, speaking=True, named=False, first=1, last=1):
    return {
        "type": "character",
        "key": key,
        "props": {"mentions": mentions, "speaking": speaking, "named": named},
        "first_segment": first,
        "last_segment": last,
    }


def auto_pairs(plan):
    return {(m["alias"], m["canonical"]) for m in plan["auto"]}


def review_names(plan):
    return {m["alias"] for m in plan["review"]}


# ---------------------------------------------------------------------------
print("=" * 72)
print("A. 规划器边界：自动档只收零反例族，其余一律待裁决")
print("=" * 72)

# A1 生成时序归一：先全名后简称 → 并回全名（review-A 的旗舰反例 宁折/宁折枝）
plan = plan_alias_merges(
    [fact("宁折枝", first=3), fact("宁折", first=7), fact("陆见", first=1)],
    source="generated",
)
pairs = auto_pairs(plan)
check("宁折 → 宁折枝（后出现的简称并回全名）", ("宁折", "宁折枝") in pairs, str(pairs))
check("陆见 不被牵连（无包含关系）",
      all("陆见" != a and "陆见" != c for a, c in pairs), str(pairs))

# A2 晏无/晏无咎 同型
plan = plan_alias_merges(
    [fact("晏无咎", first=2), fact("晏无", first=9)], source="generated"
)
check("晏无 → 晏无咎", ("晏无", "晏无咎") in auto_pairs(plan), str(auto_pairs(plan)))

# A3 反向（先简称后全名）不得自动并：那可能是新角色
plan = plan_alias_merges(
    [fact("林夜", mentions=40, first=1), fact("林夜白", first=6)], source="generated"
)
check("先短后长不自动并（可能是新角色）",
      not auto_pairs(plan), str(auto_pairs(plan)))
check("先短后长进入待裁决", "林夜" in review_names(plan), str(review_names(plan)))

# A3b 同一人但「先短后长」也不并：规则不可判时宁可漏，不可错
plan = plan_alias_merges(
    [fact("宝钗", mentions=200, first=1), fact("薛宝钗", first=9)], source="generated"
)
check("先短后长：宝钗 不并进 薛宝钗（漏，但安全）",
      not auto_pairs(plan), str(auto_pairs(plan)))

# A3c 同人「先长后短」→ 并（模型的合理回指）
plan = plan_alias_merges(
    [fact("薛宝钗", first=1), fact("宝钗", mentions=30, first=9)], source="generated"
)
check("先长后短：宝钗 → 薛宝钗（回指成立）",
      ("宝钗", "薛宝钗") in auto_pairs(plan), str(auto_pairs(plan)))

# A4 头部粘连碎片：听宝玉 → 宝玉（实测语料有 听宝玉/听贾母/听黛玉/听袭人）
plan = plan_alias_merges([fact("宝玉", first=1), fact("听宝玉", first=2)],
                         source="generated")
check("听宝玉 → 宝玉（存量语料里的碎片，Pass 1.6 抓不到）",
      ("听宝玉", "宝玉") in auto_pairs(plan), str(auto_pairs(plan)))

# A5 实测反例：**两种时序都不许自动并**——只测一种时序证明不了闸门有效，
#    因为时序本身就把一半情况挡掉了。证据值取真实语料实测值（标注在注释里）。
#    第 5 列是「生成这个词形时文本里一定同时存在的另一个名字」——闸门 ⑤ 与 ③
#    靠的就是它：并列切片 `贾母王` 之所以存在，正是因为同段还有个 `王夫人`；
#    `宝玉` 之所以不可判，正是因为账上有 `贾宝玉` 与 `甄宝玉` 两个人。
BAD = [
    # (短名, 长名, 长名证据, 上下文名字, 说明)
    ("姐儿", "凤姐儿", dict(mentions=695, speaking=True), [], "泛称「姐儿」非同一人"),
    ("元帅", "康元帅", dict(mentions=236, speaking=True), [], "官职「元帅」非同一人"),
    ("满面", "容满面", dict(mentions=47, speaking=False), [], "「笑容满面」残片，不说话"),
    ("贾母", "贾母王", dict(mentions=76, speaking=False), ["王夫人"],
     "并列短语「贾母王夫人」切片"),
    ("袭人", "袭人麝", dict(mentions=23, speaking=True), ["麝月"],
     "并列短语「袭人麝月」切片（带说话证据）"),
    ("李纨", "李纨探", dict(mentions=12, speaking=True), ["探春"],
     "并列短语「李纨探春」切片"),
    ("宝玉", "甄宝玉", dict(mentions=49, speaking=True), ["贾宝玉"],
     "不同人（镜子里的那位），且 贾宝玉 也在账上 → 一对多歧义"),
]
for short, long_, long_ev, ctx, why in BAD:
    for why_order in ("长名先", "短名先"):
        if why_order == "长名先":
            facts = [fact(long_, first=1, **long_ev), fact(short, first=9)]
        else:
            facts = [fact(short, first=1), fact(long_, first=9, **long_ev)]
        facts += [fact(c, first=1) for c in ctx]
        plan = plan_alias_merges(facts, source="generated")
        got = {(a, c) for a, c in auto_pairs(plan) if a == short or c == short}
        check(f"不自动并【{why_order}】：{short} ↛ {long_}", not got,
              f"{why}  实得 auto={got}")

# A6 歧义：短名可归多个长名 → 待裁决
plan = plan_alias_merges(
    [fact("贾宝玉", first=1), fact("甄宝玉", first=2), fact("宝玉", first=9)],
    source="generated",
)
check("一对多歧义不自动并", not auto_pairs(plan), str(auto_pairs(plan)))
check("一对多进入待裁决", "宝玉" in review_names(plan), str(review_names(plan)))

# A7 参考语料侧禁用时序法（没有「作者笔下次序」这种语义）
plan = plan_alias_merges(
    [fact("宁折枝", first=3), fact("宁折", first=7)], source="novel:1"
)
check("参考语料侧不做时序自动并", not auto_pairs(plan), str(auto_pairs(plan)))

# A8 确定性：同样输入多次规划结果一致
p1 = plan_alias_merges([fact("宁折枝", first=3), fact("宁折", first=7)])
p2 = plan_alias_merges([fact("宁折", first=7), fact("宁折枝", first=3)])
check("规划结果与输入顺序无关（确定性）",
      auto_pairs(p1) == auto_pairs(p2), f"{auto_pairs(p1)} vs {auto_pairs(p2)}")

# ---------------------------------------------------------------------------
print()
print("=" * 72)
print("B. 落库链路：upsert → 别名归一 → 图谱改写 → 提醒只吐规范名")
print("=" * 72)

Base.metadata.create_all(bind=SessionLocal().bind)
db = SessionLocal()
project = Project(title="账本回归", target_length=3000, generation_length=3000,
                  reference_novel_ids=[])
db.add(project)
db.commit()
db.refresh(project)
pid = project.id


class _C:
    def __init__(self, mentions=3, speaking=True, named=False, chapters=(1,)):
        self.mentions, self.speaking, self.named = mentions, speaking, named
        self.chapters = set(chapters)


class _R:
    def __init__(self, a, b, rel, chapters=(1,)):
        self.a, self.b, self.relation = a, b, rel
        self.chapters = set(chapters)


class _Res:
    def __init__(self, chars, rels):
        self.characters, self.relationships, self.plotholes = chars, rels, {}


def add_node(name, ch=1):
    """模拟 merge_graph_from_text 落下的图谱节点（账本测试不跑真实抽取）。"""
    n = KgNode(project_id=pid, type="character",
               props={"name": name, "mentions": 3, "speaking": True},
               first_chapter=ch, last_chapter=ch, source="generated")
    db.add(n)
    db.flush()
    return n


def add_edge(a_id, b_id, rel):
    e = KgEdge(from_id=a_id, to_id=b_id, type="relationship",
               props={"relation": rel, "chapters": [1]})
    db.add(e)
    db.flush()
    return e


# —— 段 1：模型先写全名 秦九歌 + 陆见，图谱与账本各落一份 ——
n_jiuge, n_lujian = add_node("秦九歌"), add_node("陆见")
add_edge(n_lujian.id, n_jiuge.id, "对话")
r1 = _Res(
    {"秦九歌": _C(5, chapters=(1,)), "陆见": _C(4, chapters=(1,))},
    {("陆见", "秦九歌"): _R("陆见", "秦九歌", "对话", (1,))},
)
st1 = upsert_facts(db, pid, facts_from_extraction(r1, 1), source="generated")
check("段1 台账写入（2 角色 + 1 关系 = 3 条）", st1["created"] == 3, str(st1))

# —— 段 2：模型改用简称「秦九」，并保留「陆见」 ——
n_qinjiu = add_node("秦九", ch=2)
add_edge(n_lujian.id, n_qinjiu.id, "对话")
r2 = _Res(
    {"秦九": _C(3, chapters=(2,)), "陆见": _C(2, chapters=(2,))},
    {("陆见", "秦九"): _R("陆见", "秦九", "对话", (2,))},
)
upsert_facts(db, pid, facts_from_extraction(r2, 2), source="generated")
print(f"  归一前账本角色 = {sorted(f['key'] for f in get_facts(db, pid, type_='character'))}")
rep = resolve_aliases(db, pid, source="generated")
print(f"  归一报告：auto={rep['auto']}")
print(f"           review={rep['review_count']} 项待裁决")
check("秦九 被并回 秦九歌", any(m["alias"] == "秦九" and m["canonical"] == "秦九歌"
                              for m in rep["auto"]), str(rep["auto"]))

chars = {f["key"]: f for f in get_facts(db, pid, type_="character")}
check("账本只留规范名 秦九歌", "秦九歌" in chars and "秦九" not in chars,
      str(sorted(chars)))
check("别名字段记录了 秦九", "秦九" in (chars["秦九歌"]["aliases"] or []),
      str(chars["秦九歌"]["aliases"]))
check("mentions 被累加（5+3=8）", (chars["秦九歌"]["props"] or {}).get("mentions") == 8,
      str(chars["秦九歌"]["props"]))
check("canonical_name 能把简称映射回规范名",
      canonical_name(db, pid, "秦九") == "秦九歌")

nodes = [n for n in db.scalars(
    select(KgNode).where(KgNode.project_id == pid, KgNode.type == "character")).all()]
names = sorted((n.props or {}).get("name") for n in nodes)
check("图谱角色节点只剩规范名", names == ["秦九歌", "陆见"], str(names))

edges = db.scalars(select(KgEdge)).all()
self_loops = [e for e in edges if e.from_id == e.to_id]
check("重指向未产生自环", not self_loops, f"{len(self_loops)} 条自环")
check("重指向后无重复边（(陆见,秦九歌) 只留一条）", len(edges) == 1,
      f"实得 {len(edges)} 条")

# 关系事实也要按规范名归一，否则账本里留一份「秦九」的引用
rel_keys = sorted(f["key"] for f in get_facts(db, pid, type_="relationship"))
check("关系事实键也归一为 (陆见→秦九歌)",
      rel_keys == ["陆见→秦九歌"], str(rel_keys))

rem = get_reminders(db, pid, source="generated")
rem_names = [c["name"] for c in rem["character_constraints"]]
print(f"  get_reminders 角色 = {rem_names}")
check("提醒只吐规范名（不再出现 秦九 分身）",
      "秦九歌" in rem_names and "秦九" not in rem_names, str(rem_names))

# 权威档位：参考语料侧与生成侧互不污染
upsert_facts(db, pid, facts_from_extraction(
    _Res({"林黛玉": _C(9, chapters=(1,))}, {}), 1), source="novel:7")
gen_names = {f["key"] for f in get_facts(db, pid, type_="character", source="generated")}
ref_names = {f["key"] for f in get_facts(db, pid, type_="character", source="novel:7")}
check("参考语料事实不混入生成侧", "林黛玉" not in gen_names, str(sorted(gen_names)))
check("参考语料事实独立成账", ref_names == {"林黛玉"}, str(sorted(ref_names)))
check("参考语料侧不参与生成侧改名（林黛玉 未被动过）",
      canonical_name(db, pid, "秦九", source="generated") == "秦九歌"
      and canonical_name(db, pid, "秦九", source="novel:7") == "秦九",
      "跨 source 串味")

print()
print("=" * 72)
total = len(PASSED) + len(FAILED)
print(f"结果：{len(PASSED)}/{total} 通过")
if FAILED:
    print("失败项：")
    for f in FAILED:
        print(f"  - {f}")
print("=" * 72)
db.close()
sys.exit(1 if FAILED else 0)
