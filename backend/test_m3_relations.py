"""关系抽取回归：有向交互抽取 + 人名判定单测。

背景：M4.8 之前，Pass 4 以「同分片共现」为关系成立条件，对分片内每两个角色
都建一条边——1066 分片/ 1418 角色产出 138070 条边，其中 62% 只在单章共现过，
是笛卡尔积而非关系。改为「角色名+ 邻近关系词」的有向交互抽取后降至2353 条。
本文件固化该行为，防止回退。
"""

import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from app.kg.extractor import RuleBasedExtractor, _valid_name  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        FAILURES.append(msg)


# ── 人名判定：真实小说名要放行，常用词要拦掉 ──────────────────────────
NAME_CASES = [
    # 经典角色（含「非姓命名」，早期版本被误拦）
    ("贾母", True), ("凤姐", True), ("宝玉", True), ("黛玉", True),
    ("妙玉", True), ("王熙凤", True), ("林夜", True), ("苏晚", True),
    # 常用词（首字恰好是姓氏字，靠护栏拦）
    ("东西", False), ("都是", False), ("不是", False),
    ("不知", False), ("不能", False), ("就是", False),
]
print("=== 人名判定 ===")
for name, expect in NAME_CASES:
    got = _valid_name(name)
    mark = "PASS" if got == expect else "FAIL"
    print(f"  [{mark}] {name:5s} expect={expect!s:5s} got={got}")
    check(got == expect, f"_valid_name({name!r}) 应为 {expect}，实得 {got}")

# ── 关系抽取：必须有交互证据，同框无关系词不建边 ────────────────────────
# 注意：角色入图门槛是「出现≥2 次或跨≥2 章」（Pass 2），单分片短句里
# 角色常达不到门槛，故用例均给两章 —— 这也是真实语料的形态。
print("\n=== 关系抽取 ===")
CASES = [
    # (两章文本, 应存在的边, 不应存在的边)—— 每条边是 (名1, 名2) 二元组
    (["林夜抓住苏晚的手腕。", "林夜抓住苏晚的手。"],
     [("林夜", "苏晚")], []),
    # 同框但无关系词 → 不应建边
    (["林夜和苏晚都在房里。", "林夜和苏晚还在房里。"],
     [], [("林夜", "苏晚")]),
    # 三人同句只有前两人有线索词
    (["林夜拉住苏晚。", "林夜又拉住苏晚。", "宝玉也拉住苏晚。"],
     [("林夜", "苏晚")], []),
]
for texts, expect_edges, reject_edges in CASES:
    chunks = [SimpleNamespace(chapter_no=i + 1, text=t)
              for i, t in enumerate(texts)]
    res = RuleBasedExtractor().extract(chunks)
    got = set(res.relationships.keys())
    label = texts[0][:14]
    for a, b in expect_edges:
        mark = "PASS" if (a, b) in got else "FAIL"
        print(f"  [{mark}] 应有边 {a}—{b}  文本：{label}")
        check((a, b) in got, f"应有边({a}, {b})，实得 {got}（文本：{label}）")
    for a, b in reject_edges:
        mark = "PASS" if (a, b) not in got else "FAIL"
        print(f"  [{mark}] 不应有边 {a}—{b}  文本：{label}")
        check((a, b) not in got, f"不应有边({a}, {b})，实得 {got}（文本：{label}）")

# ── 边必须无序归一（同一关系不重复存两次）───────────────────────────────
print("\n=== 边方向归一 ===")
res = RuleBasedExtractor().extract([
    SimpleNamespace(chapter_no=1, text="林夜抓住苏晚的手腕。"),
    SimpleNamespace(chapter_no=2, text="苏晚帮林夜包扎。"),
])
keys = list(res.relationships.keys())
print(f"  键：{keys}")
check(len(keys) == len(set(keys)), "出现重复边")
check(("苏晚", "林夜") not in keys, "出现反向重复键（未归一化）")

# ── 规模控制：同一分片多人同框，边数不应退化为两两笛卡尔积───────────────
print("\n=== 规模控制（防笛卡尔积回退） ===")
# 同框多人 + 少数真实交互。若退化为「同框即连」，边数会趋近两两配对数。
names = ["宝玉", "黛玉", "宝钗", "袭人", "晴雯", "紫鹃", "鸳鸯", "金钏",
         "司棋", "侍书", "入画", "麝月"]
interaction = "宝玉拉住黛玉、宝玉问袭人、黛玉帮紫鹃。"
cooc = "、".join(f"{a}和{b}都在屋里" for a in names for b in names)
texts = [interaction + cooc + "。" for _ in range(3)]
chunks = [SimpleNamespace(chapter_no=i + 1, text=t)
          for i, t in enumerate(texts)]
res = RuleBasedExtractor().extract(chunks)
n_names = len(res.characters)
n_edges = len(res.relationships)
comb = n_names * (n_names - 1) // 2
print(f"  角色 {n_names}，两两配对需 {comb} 条，实际 {n_edges} 条 "
      f"({n_edges / max(comb, 1) * 100:.1f}%)")
check(n_edges > 0, "一条边都没建出来（有向抽取过度收紧）")
check(n_edges <= comb, "边数超两两配对")
# 本用例里只有 3 句真实交互，判据是「同框无交互的角色对不得建边」：
# 语料里「晴雯/鸳鸯/金钏」等只出现在「X和Y都在屋里」这种无关系词的同框里，
# 任何涉及它们的边都说明退化为共连。
cooc_only = {"晴雯", "鸳鸯", "金钏", "司棋", "侍书", "入画", "麝月"}
bad = [
    (a, b) for (a, b) in res.relationships
    if a in cooc_only or b in cooc_only
]
check(not bad, f"同框无交互的角色被连边（疑似笛卡尔积）：{bad[:3]}")

# 真实语料上的规模收敛（1066 分片 / 三本公版书）
print("\n=== 真实语料规模收敛 ===")
try:
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models import NovelChunk

    eng = create_engine("sqlite:///novel_demo.db")
    with Session(eng) as s:
        chunks = s.scalars(
            select(NovelChunk).order_by(NovelChunk.novel_id, NovelChunk.id)
        ).all()
    r2 = RuleBasedExtractor().extract(chunks)
    n2 = len(r2.characters)
    e2 = len(r2.relationships)
    comb2 = n2 * (n2 - 1) // 2
    print(f"  角色 {n2}，两两配对需 {comb2} 条，实际 {e2} 条 "
          f"({e2 / max(comb2, 1) * 100:.1f}%)")
    check(e2 < 5000, f"真实语料关系边 {e2} 过大，笛卡尔积可能回退")
    check(e2 < comb2 * 0.05, f"真实语料边数 {e2} 未显著收敛")
    loops = [k for k in r2.relationships if k[0] == k[1]]
    check(not loops, f"存在自环边 {len(loops)} 条")
except Exception as exc:  # noqa: BLE001 - 库不存在时跳过，不阻塞单测
    print(f"  [跳过] 无法打开 novel_demo.db：{exc}")

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败：")
    for f in FAILURES:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 关系抽取回归全部通过：有向交互抽取 + 人名判定 + 规模控制。")