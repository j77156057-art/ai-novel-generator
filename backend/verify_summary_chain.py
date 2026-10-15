"""步骤 2 证据：前情块的**改前/改后**对照 + 事实留存的归属划分。

评审 A 的贯穿判断 ①：「段级 60 字摘要链本身就是遗忘机制（有损压缩随 30 段累积）」。
这句话其实含两个**互相独立**的问题，必须分开测，否则会把「链条太长」和「事实丢了」
混为一谈，从而在错误的地方下手：

  A) 尺寸问题：前情块随段数**线性无界**增长 → 预算被吃干、越写越挤。
  B) 留存问题：第 1 段埋的事实，第 N 段还在不在？

  B 不靠摘要链解决（那是有损的，越滚越糊）——靠事实账本/图谱（无损、可累积）。
  所以本脚本同时验证**步骤 1 的接续是否真的成立**：账本侧必须与段数无关地保住事实。

本脚本只跑**离线、无 LLM** 的路径：滚上函数用确定性兜底实现，
所以这里的数字是可复现的，不受模型抖动影响。

跑法：python verify_summary_chain.py
"""

import os
import sys
import tempfile

_DB = os.path.join(tempfile.gettempdir(), "ai_novel_summary_chain.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    _p = _DB + _suffix
    if os.path.exists(_p):
        try:
            os.remove(_p)
        except OSError:
            pass
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["EMBEDDING_PROVIDER"] = "local"
os.environ["LLM_PROVIDER"] = "local"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app.db import Base, SessionLocal  # noqa: E402
from app.generation import build_context  # noqa: E402
from app.generation.prompts import build_user_prompt  # noqa: E402
from app.generation.summary_tree import (  # noqa: E402
    DEFAULT_BUDGET_CHARS,
    budget_for_generation,
    compose_prior,
    empty_tree,
    record_l1,
    rollup_volumes,
)
from app.kg.ledger import facts_from_extraction, upsert_facts  # noqa: E402
from app.llm.provider import LLMProvider  # noqa: E402
from app.models import KgNode, Project  # noqa: E402

# 段级摘要的真实量级：provider.summarize 提示词要求「不超过 60 字」，
# 但**没有任何截断兜底**——模型超发就照原样落库。故取两档测：
#   合规档 60 字 / 超发档 120 字（真实模型常见）。
SEGMENT_CHARS = 3000  # engine 默认一次生成 generation_length 字
GRID = (1, 5, 8, 16, 30, 60, 100, 300)

# 离线确定性滚上：直接用 LLMProvider 的兜底实现本体（同一份代码，不带 self 依赖）
_rollup = LLMProvider().summarize_rollup


def _extract(chars: dict):
    """造一个最小抽取结果（facts_from_extraction 吃属性、不吃 dict）。

    chars: {名字: (mentions, 段集合, speaking, named)}
    """
    from types import SimpleNamespace

    class _C:
        def __init__(self, name, spec):
            self.name = name
            self.mentions, self.chapters, self.speaking, self.named = spec

    return SimpleNamespace(
        characters={n: _C(n, spec) for n, spec in chars.items()},
        plotholes={},
        relationships={},
    )


def legacy_prompt_size(prior: list[str]) -> tuple[int, int]:
    """旧口径：把整条扁平摘要链按 1..N 编号塞进【前情提要】。"""
    prompt = build_user_prompt(
        prior_summaries=prior,
        rag_context=["检索片段占位" * 20],
        outline=["第一幕：主角登场", "第二幕：真相浮现"],
        max_chars=SEGMENT_CHARS,
        plot_bible=None,
        premise=None,
    )
    block = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prior)) if prior else "（首段，无前情）"
    return len(block), len(prompt)


def new_prompt_size(n_seg: int, seg_len: int, budget: int) -> tuple[int, int, dict]:
    """新口径：L1 → 滚上 L2/L3 → compose_prior 按预算拼。"""
    tree = empty_tree()
    for s in range(1, n_seg + 1):
        record_l1(tree, s, "主" * seg_len)
    rollup_volumes(tree, _rollup)
    lines, rep = compose_prior(tree, budget_chars=budget)
    block = "\n".join(lines) if lines else "（首段，无前情）"
    prompt = build_user_prompt(
        prior_summaries=[],
        rag_context=["检索片段占位" * 20],
        outline=["第一幕：主角登场", "第二幕：真相浮现"],
        max_chars=SEGMENT_CHARS,
        plot_bible=None,
        premise=None,
        prior_blocks=lines,
    )
    return len(block), len(prompt), rep


def main() -> int:
    Base.metadata.create_all(bind=SessionLocal().bind)
    db = SessionLocal()

    print("=" * 96)
    print("A) 改前（对照臂）：段级摘要链全量拼接，无上界")
    print("=" * 96)
    print(f"（单段正文按 {SEGMENT_CHARS} 字计）\n")
    print(f"{'段数 N':>6} | {'摘要60字档':>12} {'占 user':>8} | {'摘要120字档':>12} {'占 user':>8}")
    print("-" * 66)
    rows = []
    for n in GRID:
        p60, t60 = legacy_prompt_size(["主" * 60] * n)
        p120, t120 = legacy_prompt_size(["主" * 120] * n)
        rows.append((n, p60, t60, p120, t120))
        print(f"{n:>6} | {p60:>12,} {p60 / t60 * 100:>7.0f}% | "
              f"{p120:>12,} {p120 / t120 * 100:>7.0f}%")
    print()
    n_last, p60_last, _, _, _ = rows[-1]
    n_30 = next(r for r in rows if r[0] == 30)[1]
    print(f"  线性性：N=30 → {n_30:,} 字；N=300 → {p60_last:,} 字 "
          f"（10 倍段数 → {p60_last / n_30:.1f} 倍前情块，无上界）")
    print(f"  与正文比：N=300 时前情块 {p60_last:,} 字 vs 单次生成 {SEGMENT_CHARS:,} 字 "
          f"→ 已占单段正文的 {p60_last / SEGMENT_CHARS * 100:.0f}%")
    print(f"  信息密度：每段 {SEGMENT_CHARS} 字压成 60 字 = 保留 {60 / SEGMENT_CHARS * 100:.1f}%")

    print()
    print("=" * 96)
    print("C) 改后：层级摘要（L1 段 / L2 卷 8 段 / L3 主线骨架）+ 预算裁剪")
    print("=" * 96)
    budget = budget_for_generation(SEGMENT_CHARS)
    print(f"（前情预算 = 单段正文 × 0.4 = {budget} 字；近情下限 3 段，按卷边界对齐）\n")
    print(f"{'段数 N':>6} | {'已封卷':>6} | {'前情块':>8} {'占 user':>8} | "
          f"{'对 A 臂压缩':>10} | {'留卷/丢卷':>10}")
    print("-" * 74)
    new_rows = []
    for n in GRID:
        blk, total, rep = new_prompt_size(n, 60, budget)
        legacy_blk = next(r[1] for r in rows if r[0] == n)
        new_rows.append((n, blk, total, rep, legacy_blk))
        print(f"{n:>6} | {rep['sealed_volumes']:>6} | {blk:>8,} {blk / total * 100:>7.0f}% | "
              f"{legacy_blk / max(blk, 1):>9.1f}x | "
              f"{len(rep['l2_rendered'])}/{len(rep['l2_dropped']):>8}")
    print()
    nb_last, nt_last, nrep_last, nleg_last = (
        new_rows[-1][1], new_rows[-1][2], new_rows[-1][3], new_rows[-1][4]
    )
    print(f"  N=300：前情块 {nb_last:,} 字（旧口径 {nleg_last:,} 字，压到 "
          f"{nb_last / nleg_last * 100:.1f}%）")
    print(f"  分层构成：主线骨架 1 条 + 卷摘要 {len(nrep_last['l2_rendered'])} 条 + "
          f"近情 {nrep_last['window'][1] - nrep_last['window'][0] + 1} 段（段 "
          f"{nrep_last['window'][0]}-{nrep_last['window'][1]}）")
    print(f"  覆盖证明：uncovered = {nrep_last['uncovered']}（恒空 = 无信息空洞）")
    print(f"  超预算标记：over_budget = {nrep_last['over_budget']}（诚实回报，不静默截断）")

    print()
    print("=" * 96)
    print("D) 卷层门槛：卷摘要从第几段起才真的参与，以及那时压缩收益是否已转正")
    print("=" * 96)
    print("（判据不是「有卷摘要了就算启用」，而是「同一段历史，分层用更少字数讲完」——")
    print("  若分层反而更贵，那它只是把信息换了个说法，不如老老实实逐段。）\n")
    print(f"{'段数 N':>6} | {'全量逐段':>8} | {'分层':>8} | {'节省':>7} | "
          f"{'窗口段':>6} {'占 N':>6} | {'卷摘要':>6} | {'骨架覆盖段':>10} | 判定")
    print("-" * 96)
    threshold = None
    regressions = []
    for n in range(8, 41):
        flat_chars, _ = legacy_prompt_size(["主" * 60] * n)
        blk, _, rep = new_prompt_size(n, 60, budget)
        verbatim = rep["window"][1] - rep["window"][0] + 1 if rep["window"] else 0
        spine_segs = rep["sealed_volumes"] * 8
        won = blk < flat_chars
        # 「卷层真的在干活」= 有卷摘要被渲染进 prompt
        working = len(rep["l2_rendered"]) > 0
        if threshold is None and working and won:
            threshold = n
        if working and not won:
            regressions.append((n, blk, flat_chars))
        mark = "卷层启用" if working else "—"
        if won and working:
            mark += " · 省钱"
        print(f"{n:>6} | {flat_chars:>8,} | {blk:>8,} | "
              f"{(1 - blk / flat_chars) * 100:>6.0f}% | {verbatim:>6} "
              f"{verbatim / n * 100:>5.0f}% | {len(rep['l2_rendered']):>6} | "
              f"{spine_segs:>10} | {mark}")
    print()
    # 门槛是**结构性**的，不是常量：卷摘要要求「至少封满一卷」才可能存在
    # （`sealed_volume_count` 要求整卷 L1 齐备），故门槛 = volume_size = 8 段。
    expected = 8
    print(f"  实测门槛 = 第 {threshold} 段起「卷层启用 且 比全量逐段更省」。")
    print(f"  预期门槛 = volume_size = {expected} 段（封满一卷是卷摘要存在的前提）—— "
          f"{'与实测一致 ✅' if threshold == expected else '与实测不一致，需复查'}。")
    print("  判读：门槛是**结构性的**（必须至少封满一卷），不是可以随便调的常量。")
    print("        早期版本因为要求窗口必须落在卷边界上，门槛被抬到 11 段，且窗口长度随")
    print("        N mod 8 在 3↔10 之间振荡、部分 N 上分层反而更贵（N=8/9/10 贵 53/48/44%）。")
    print("        改为允许「跨界卷摘要与窗口有界重叠」后，窗口稳定在 recent_min 附近，门槛回到 8。")

    print()
    print("=" * 96)
    print("B) 留存：第 1 段埋的事实到第 30 段还在不在（应当靠账本，不靠摘要链）")
    print("=" * 96)
    project = Project(title="层级摘要测量", target_length=300000, generation_length=SEGMENT_CHARS)
    db.add(project)
    db.commit()
    db.refresh(project)
    pid = project.id

    # 第 1 段：埋一个角色 + 一个关系；用一个**刻意不会进摘要**的冷门细节名
    seg1_facts = facts_from_extraction(_extract({"沈砚": (4, {1}, True, False)}), segment_no=1)
    upsert_facts(db, pid, seg1_facts, source="generated")
    db.add(KgNode(project_id=pid, type="character", source="generated",
                  props={"name": "沈砚", "mentions": 4, "speaking": True}))
    db.commit()

    # 再滚 29 段无关内容
    for seg in range(2, 31):
        f = facts_from_extraction(
            _extract({f"路人{seg}": (2, {seg}, False, False)}), segment_no=seg
        )
        upsert_facts(db, pid, f, source="generated")
    db.commit()

    # 补 30 段摘要链（模拟真实链条），然后看注入结果
    project.summary = [f"第{i}段：主角推进了第{i}个情节点，出现新的阻碍。" for i in range(1, 31)]
    db.commit()
    db.refresh(project)

    ctx = build_context(db, project)
    cons = ctx["constraints"]["characters"]
    print(f"  30 段后 constraints 角色数 = {len(cons)}")
    print(f"  第 1 段角色 沈砚 是否仍在 constraints = {'沈砚' in cons}")
    print(f"  前情块构成：{len(ctx['prior_blocks'])} 行，"
          f"uncovered = {ctx['prior_report']['uncovered']}")
    print(f"  （对照）旧口径下前情块 = {legacy_prompt_size(ctx['prior_summaries'])[0]:,} 字；"
          f"新口径 = {ctx['prior_report']['chars']:,} 字")

    from app.kg.ledger import get_facts  # noqa: E402

    facts = get_facts(db, pid, source="generated", type_="character")
    shen = [f for f in facts if f["key"] == "沈砚"]
    print(f"  账本侧：沈砚 事实条数 = {len(shen)}，"
          f"first_segment={shen[0]['first_segment'] if shen else None}，"
          f"last_segment={shen[0]['last_segment'] if shen else None}")
    print(f"  账本侧事实总数 = {len(get_facts(db, pid, source='generated'))}（与段数同阶，但不进 prompt）")

    ok_a = rows[-1][1] > 3 * rows[3][1]              # 旧口径确实线性增长
    ok_b = "沈砚" in cons                            # 账本侧事实与段数无关地保住
    ok_c = nb_last <= budget and not nrep_last["uncovered"]   # 新口径有界且无洞
    ok_d = threshold == expected and not regressions          # 卷层门槛符合结构预测、无倒挂
    print()
    print(f"  [{'PASS' if ok_a else 'FAIL'}] A 旧口径前情块无上界（复现评审 A 的 ①）")
    print(f"  [{'PASS' if ok_c else 'FAIL'}] C 新口径前情块有上界（{nb_last} ≤ {budget}）且无空洞")
    print(f"  [{'PASS' if ok_b else 'FAIL'}] B 事实留存已由账本承担（步骤 1 的接续成立）")
    print(f"  [{'PASS' if ok_d else 'FAIL'}] D 卷层门槛 = {threshold} 段（结构预测 {expected}），"
          f"且 8..40 段无「分层反而更贵」的倒挂" + (f"；倒挂：{regressions}" if regressions else ""))

    db.close()
    print()
    print("判读：A 是要修的、C 是修完的结果、B 是步骤 1 已经解决的——"
          "所以层级摘要的价值在**控预算 + 保叙事连贯**，而不是「让摘要链别忘事实」。")
    return 0 if (ok_a and ok_b and ok_c and ok_d) else 1



if __name__ == "__main__":
    sys.exit(main())
