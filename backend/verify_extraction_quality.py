"""抽取质量审计：两个变量、三个臂，逐一隔离。

臂 A  消歧 pass 关 + 折行不归一   → 相当于本轮改动**之前**的状态
臂 B  消歧 pass 开 + 折行归一     → **当前生产代码**（_deglue + _drop_shadowed_prefixes + _unwrap_lines 全开）
臂 C  消歧 pass 开 + 折行不归一   → 只把 _unwrap_lines 换成恒等函数（唯一变量）

（注：B/C 的定义以本文件下方的实际打桩代码为准——上面这三行曾是反的，已按代码改正。）

指标（全部在语料上复算，同一套测量代码测三组）：
  glue     粘连体：3 字名，末字 ∈ 粘连尾字集，且 2 字前缀也是角色
  shadow   切片伪名：短名是更长名严格前缀，且全文每次出现都被更长名覆盖
  wrap     换行伪名：名字里被换行切断的组合（如「林黛⏎玉」→「林黛」被当独立出现）
  dirty 边 两端任一是上述伪形态的边
"""
import re
import sys
import time

sys.path.insert(0, ".")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.kg.extractor import RuleBasedExtractor, _GLUE_TAIL_CHARS  # noqa: E402
from app.models import NovelChunk  # noqa: E402

# 换行续行归一：只在**前后都是汉字**时把换行视为硬折行。
# 段末（前一字是标点，如「。”⏎」）不合并，避免把两段话缝在一起造假匹配。
_WRAP_NL_RE = re.compile(r"(?<=[一-鿿])[ \t]*\r?\n[ \t]*(?=[一-鿿])")

_ORIG_DEGLUE = RuleBasedExtractor._deglue_candidates
_ORIG_SHADOW = RuleBasedExtractor._drop_shadowed_prefixes


def shadow_and_wrap(names, text):
    """独立复算切片伪名；额外返回「存在换行切断形态」的名字集合。"""
    longer_by_prefix = {}
    for n in names:
        for L in range(2, len(n)):
            longer_by_prefix.setdefault(n[:L], []).append(n)
    total = dict.fromkeys(longer_by_prefix, 0)
    covered = dict.fromkeys(longer_by_prefix, 0)
    for i in range(len(text)):
        for L in (2, 3):
            p = text[i:i + L]
            if len(p) < L:
                continue
            longs = longer_by_prefix.get(p)
            if longs is None:
                continue
            total[p] += 1
            if any(text.startswith(lo, i) for lo in longs):
                covered[p] += 1
    shadow = {p for p, t in total.items() if t == covered[p] and p in names}
    # 换行伪名：把换行去掉后，比原文多出的「被覆盖」判定
    flat = _WRAP_NL_RE.sub("", text)
    wrap = set()
    for n in names:
        if len(n) < 4 and text.count(n) > flat.count(n):
            wrap.add(n)
    return shadow, wrap


def audit(res, text, label):
    names = set(res.characters)
    edges = set(res.relationships)
    glue = {n for n in names
            if len(n) == 3 and n[-1] in _GLUE_TAIL_CHARS and n[:2] in names}
    shadow, wrap = shadow_and_wrap(names, text)
    bad = glue | shadow
    dirty = {e for e in edges if e[0] in bad or e[1] in bad}
    print(f"  {label:24s} 角色 {len(names):5d}  边 {len(edges):5d}  "
          f"glue {len(glue):3d}  shadow {len(shadow):3d}  "
          f"dirty边 {len(dirty):4d} ({len(dirty) / max(len(edges), 1) * 100:.1f}%)")
    return {"names": names, "glue": glue, "shadow": shadow, "dirty": dirty,
            "edges": edges}


def run(chunks, label):
    text = "\n".join(c.text or "" for c in chunks)
    t0 = time.time()
    res = RuleBasedExtractor().extract(chunks)
    out = audit(res, text, label)
    return out, time.time() - t0, res


eng = create_engine("sqlite:///novel_demo.db")
with Session(eng) as s:
    chunks = s.scalars(select(NovelChunk).order_by(NovelChunk.id)).all()
kinds = sum(1 for c in chunks
            if _WRAP_NL_RE.search(c.text or ""))
print(f"语料：{len(chunks)} 分片 / "
      f"{sum(len(c.text or '') for c in chunks)} 字；"
      f"含「汉字间断行」的分片 {kinds} 个 ({kinds / len(chunks) * 100:.0f}%)\n")

RuleBasedExtractor._deglue_candidates = staticmethod(lambda res: None)
RuleBasedExtractor._drop_shadowed_prefixes = staticmethod(lambda res, ch: None)
a, ta, _ = run(chunks, "A 消歧关 / 不归一")

RuleBasedExtractor._deglue_candidates = staticmethod(_ORIG_DEGLUE)
RuleBasedExtractor._drop_shadowed_prefixes = staticmethod(_ORIG_SHADOW)
b, tb, res_on = run(chunks, "B 消歧开 / 折行归一（现状）")

# 唯一变量：把 _unwrap_lines 换成恒等函数（源文本里的换行照旧参与覆盖度判定）
import app.kg.extractor as _ex  # noqa: E402

_ORIG_UNWRAP = _ex._unwrap_lines
_ex._unwrap_lines = lambda t: t
c_, tc, _ = run(chunks, "C 消歧开 / 折行不归一")
_ex._unwrap_lines = _ORIG_UNWRAP  # 还原：后面还要用生产配置做别名测量

print(f"\n  （耗时 A {ta:.1f}s / B {tb:.1f}s / C {tc:.1f}s）")
print(f"  角色  A {len(a['names'])}  B {len(b['names'])}  C {len(c_['names'])}")
print(f"  边    A {len(a['edges'])}  B {len(b['edges'])}  C {len(c_['edges'])}")
print(f"  glue  A {len(a['glue'])}  B {len(b['glue'])}  C {len(c_['glue'])}")
print(f"  shadow A {len(a['shadow'])}  B {len(b['shadow'])}  C {len(c_['shadow'])}")
print(f"  dirty A {len(a['dirty'])}  B {len(b['dirty'])}  C {len(c_['dirty'])}")
print(f"\n  B 相对 A 新消失的角色（消歧 pass 的净效果，前 15）："
      f"{sorted(a['names'] - b['names'])[:15]}")
# 唯一变量是折行归一（B 有 / C 无）。两个方向分开念：
#   在 C 不在 B（_only_c）＝ 折行归一**清掉**的。期望：全是 林黛/乌进 这类被换行切出的切片伪名。
#   在 B 不在 C（_only_b）＝ 折行归一**认回**的。期望：本语料为 0（长名在断行处之外也出现过，
#                            因此 C 里本来就有），非 0 是收益。
_only_c = sorted(c_["names"] - b["names"])   # 被折行归一清掉的
_only_b = sorted(b["names"] - c_["names"])   # 被折行归一认回的
print(f"  折行归一【清掉】{len(_only_c)} 个（在 C 在、B 不在），前 20：{_only_c[:20]}")
print(f"  折行归一【认回】{len(_only_b)} 个（在 B 在、C 不在），前 20：{_only_b[:20]}")
_wrap_slices = [n for n in _only_c
                if any(o.startswith(n) and len(o) > len(n) for o in b["names"])]
print(f"  清掉的里面，可确认是「更长真名前缀切片」{len(_wrap_slices)}/{len(_only_c)} 个"
      f"（＝真伪名，清对）：{_wrap_slices[:20]}")
_residue = [n for n in _only_c if n not in _wrap_slices]
print(f"  余下 {len(_residue)} 个待人工判读：{_residue[:20]}")

# ── 别名归一的可行性测量：真实语料上自动档 vs 待裁决 ────────────────────
# 这段数据是 ledger.plan_alias_merges 只保留两个自动族、其余全部降级为提案的**依据**。
# 换语料/换阈值后重跑本段即可复核该决策，不必凭记忆。
from app.kg.ledger import plan_alias_merges  # noqa: E402

print("\n" + "=" * 72)
print("别名归一可行性（语料当生成侧跑，即最宽松的时序条件）")
print("=" * 72)
char_facts = [
    {
        "type": "character",
        "key": n,
        "props": {"mentions": c.mentions, "speaking": bool(c.speaking),
                  "named": bool(c.named)},
        # 章号充当时序：章节靠前＝先出现
        "first_segment": min(c.chapters) if c.chapters else 0,
        "last_segment": max(c.chapters) if c.chapters else 0,
    }
    for n, c in res_on.characters.items()
]
plan = plan_alias_merges(char_facts, source="generated")
print(f"  自动档 {len(plan['auto'])} 条：")
for m in plan["auto"][:15]:
    print(f"    {m['alias']} → {m['canonical']}   （{m['reason']}）")
print(f"  待裁决 {len(plan['review'])} 条（不写库、不改图，仅上报）：")
for m in plan["review"][:15]:
    print(f"    {m['alias']} ⇒ {m['candidates']}")
print("""
  判读：自动档的正确率必须逐个人工核过才可信；待裁决那批正是「字符串包含但身份不可判」
  的部分（姐儿/元帅/满面/宝玉 这类）。这解释了为什么账本把自动档收得这么紧——
  错并一次就是静默篡改身份，比漏并严重得多。""")
