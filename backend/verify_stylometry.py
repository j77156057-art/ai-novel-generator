"""文体档案（步骤 3）的可证伪验证。全部离线，用真实语料。

要证的三件事（每件都可能被证伪，所以有 FAIL 闸门）：

  A. **判别力**：特征集必须能分开不同语料。
     命题「同语料留出单元到档案的距离 ≪ 其它语料单元到档案的距离」。
     若这条不成立（比值接近 1），说明这批特征根本不含文体信息，
     那用它去判「AI 腔」就是自欺 —— 本脚本应判 FAIL，而不是把阈值调松。

  B. **粒度对齐是必需的**（我在模块文档里声称的）。
     命题「用整本书估的档案，去判 3000 字单元，会系统性误判」。
     若实测两者差不多，那这条声称就是多余的，应当从文档里删掉而不是留着唬人。

  C. **代理指标要单独交代**。
     「四字格密度 / 短串占比」不用成语词典，只是结构性近似。
     逐个报它们的判别力，判别力差的必须明说不能单独用于判读。

另加 D：`describe()` 的确定性（静态 prompt 块的缓存友好性依赖它）。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys

from app.style.stylometry import (
    FEATURES,
    PROXY_FEATURES,
    cut_units,
    describe,
    feature_table,
    features,
    fit_profile,
    style_distance,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ← {detail}" if detail and not ok else ""))
    if ok and detail:
        print(f"         {detail}")


def section(t: str) -> None:
    print()
    print("-" * 78)
    print(t)
    print("-" * 78)


def load_corpus(db: str = "novel_dev.db") -> dict[str, str]:
    """从库里取回语料正文，按书名聚合。"""
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out: dict[str, str] = {}
    for r in con.execute("SELECT id, title FROM raw_novels ORDER BY id"):
        chunks = con.execute(
            "SELECT text FROM novel_chunks WHERE novel_id=? ORDER BY idx", (r["id"],)
        ).fetchall()
        out[r["title"]] = "\n".join(c["text"] for c in chunks)
    con.close()
    return out


UNIT = 3000


def main() -> int:
    corpus = load_corpus()
    if len(corpus) < 2:
        print("语料不足 2 本，无法做跨语料判别力验证。")
        return 1
    names = list(corpus)
    print("语料：")
    for k, v in corpus.items():
        units = cut_units(v, UNIT)
        print(f"  {k}：{len(v):,} 字 → 切出 {len(units)} 个 {UNIT} 字单元")
    print(f"（按 {UNIT} 字切；该值应与生成长度一致，否则统计粒度不可比）")

    # ======================================================================
    section("A. 判别力：同语料留出 ≪ 跨语料（本脚本的核心命题）")

    profiles = {n: fit_profile([corpus[n]], unit_chars=UNIT, label=n) for n in names}

    print(f"\n  {'档案':<12} {'n_train':>8} {'n_hold':>7} | "
          f"{'自身留出 z':>10} {'自身p90':>8} | 跨语料 z（其它各本）")
    print("  " + "-" * 96)

    ratios: list[float] = []
    own_stats: dict[str, dict] = {}
    cross_stats: dict[str, dict[str, float]] = {}

    for n in names:
        p = profiles[n]
        hold = p["holdout"]
        own_mean = hold["mean_abs_z"]["mean"]
        own_p90 = hold["mean_abs_z"]["p90"]
        own_stats[n] = {"mean": own_mean, "p90": own_p90, "max": hold["mean_abs_z"]["max"]}

        cross: dict[str, float] = {}
        for m in names:
            if m == n:
                continue
            units = cut_units(corpus[m], UNIT, max_units=60)
            ds = [style_distance(u, p)["mean_abs_z"] for u in units]
            cross[m] = statistics.fmean(ds)
        cross_stats[n] = cross
        worst_ratio = min(cross[m] / own_mean for m in cross) if own_mean else float("inf")
        ratios.append(worst_ratio)

        cells = "  ".join(f"{m}={cross[m]:.2f} ({cross[m] / own_mean:.1f}×)" for m in cross)
        print(f"  {n:<12} {p['n_train']:>8} {p['n_holdout']:>7} | "
              f"{own_mean:>10.3f} {own_p90:>8.3f} | {cells}")

    min_ratio = min(ratios)
    check(
        "跨语料距离至少是最宽松同语料留出距离的 1.5×（特征集有判别力）",
        min_ratio >= 1.5,
        f"最小比值 = {min_ratio:.2f}×（若 <1.5 则这批特征不足以分开语料，判 FAIL）",
    )
    print(f"    最保守的跨语料/同语料比值 = {min_ratio:.2f}× "
          f"（全组合里的最小值；比值越大越好）")

    # —— 更强的判据：分位分离 + 重叠率（不靠均值比值）——
    # 「均值比」会被单侧离群拉抬；「重叠率」直接回答最要命的问题：
    # **有多大比例的其它语料单元，会被本语料的档案判成「正常」？**
    print(f"\n  分位分离（判据比均值比更硬）：")
    print(f"  {'档案':<12} {'自身留出 p90':>11} | {'跨语料 p10':>10} | "
          f"{'分离':>5} | {'重叠率（跨语料落在自身p90内）':>26}")
    print("  " + "-" * 92)
    overlaps: list[float] = []
    separated: list[bool] = []
    for n in names:
        p = profiles[n]
        own_p90 = p["holdout"]["mean_abs_z"]["p90"]
        for m in names:
            if m == n:
                continue
            units = cut_units(corpus[m], UNIT, max_units=60)
            ds = sorted(style_distance(u, p)["mean_abs_z"] for u in units)
            p10 = ds[max(0, len(ds) // 10 - 1)]
            overlap = sum(1 for d in ds if d <= own_p90) / len(ds)
            overlaps.append(overlap)
            separated.append(p10 > own_p90)
            print(f"  {n:<12} {own_p90:>11.3f} | {p10:>10.3f} | "
                  f"{'✅ 是' if p10 > own_p90 else '❌ 否':>5} | {overlap * 100:>24.0f}%")
    worst_overlap = max(overlaps)
    check(
        "最坏情况下，其它语料被误判为「本语料正常」的单元比例 < 25%",
        worst_overlap < 0.25,
        f"最坏重叠率 = {worst_overlap * 100:.0f}%（越高说明两本越难分）",
    )
    print(f"    最坏重叠率 = {worst_overlap * 100:.0f}%；"
          f"跨语料 p10 全部高于自身 p90 的组合：{sum(separated)}/{len(separated)}")
    print("    判读：只有 3 本语料参与标定，重叠率是**这三本之间**的实测值，")
    print("          不外推到全部网文；它说明的是「特征集确实携带文体信息」，")
    print("          而不是「任意两本都能分清」。")

    # 逐特征判别力
    print("\n  逐特征判别力（跨语料 |z| 均值 / 同语料留出 |z| 均值；<1.2 视为无判别力）：")
    feat_ratio: dict[str, float] = {}
    for k in FEATURES:
        own_z: list[float] = []
        cross_z: list[float] = []
        for n in names:
            p = profiles[n]
            st = p["feat"][k]
            sd = st["std"] or 0.0
            if sd <= 0:
                continue
            for u in cut_units(corpus[n], UNIT, max_units=60):
                own_z.append(abs((features(u)[k] - st["mean"]) / sd))
            for m in names:
                if m == n:
                    continue
                for u in cut_units(corpus[m], UNIT, max_units=30):
                    cross_z.append(abs((features(u)[k] - st["mean"]) / sd))
        if own_z and cross_z:
            feat_ratio[k] = statistics.fmean(cross_z) / max(statistics.fmean(own_z), 1e-9)
    for k in sorted(feat_ratio, key=lambda x: -feat_ratio[x]):
        mark = "  ← 代理指标" if k in PROXY_FEATURES else ""
        weak = "  ⚠ 判别力弱，不得单独判读" if feat_ratio[k] < 1.2 else ""
        print(f"    {k:<14} {feat_ratio[k]:>6.2f}×{mark}{weak}")

    proxies = {k: feat_ratio.get(k) for k in PROXY_FEATURES if k in feat_ratio}
    print(f"\n  代理指标实测判别力：{ {k: round(v, 2) for k, v in proxies.items()} }")
    check(
        "代理指标（四字格/短串）**至少有一个**具备判别力（≥1.2×），否则应从特征集剔除",
        any(v >= 1.2 for v in proxies.values()),
        f"实测 {proxies}",
    )

    # ======================================================================
    section("B. 粒度对齐是否真的必需（测我自己在模块文档里的声称）")

    n0 = names[0]
    fine = profiles[n0]                                  # unit = 3000（对齐）
    coarse = fit_profile([corpus[n0]], unit_chars=UNIT * 10, label=f"{n0}@10x")
    hold_units = cut_units(corpus[n0], UNIT)
    hold_units = [u for i, u in enumerate(hold_units) if i % 10 >= 7][:40]
    if not hold_units:
        hold_units = cut_units(corpus[n0], UNIT)[:40]

    d_fine = [style_distance(u, fine)["mean_abs_z"] for u in hold_units]
    d_coarse = [style_distance(u, coarse)["mean_abs_z"] for u in hold_units]
    m_fine, m_coarse = statistics.fmean(d_fine), statistics.fmean(d_coarse)
    print(f"  同 {UNIT} 字留出单元（{len(hold_units)} 个）：")
    print(f"    对「{UNIT} 字档案」的距离 = {m_fine:.3f}"
          f"（自称 p90 阈值 {fine['holdout']['mean_abs_z']['p90']:.3f}）")
    print(f"    对「{UNIT * 10} 字档案」的距离 = {m_coarse:.3f}")
    inflated = m_coarse / max(m_fine, 1e-9)
    print(f"    粗粒度档案把距离抬高 {inflated:.2f}×")
    check(
        "用不对齐粒度的档案去判单元，确实会系统性偏移（≥1.2×）——"
        "所以 unit_chars 必须与生成长度一致",
        inflated >= 1.2,
        f"实测只抬高 {inflated:.2f}×，说明这条声称不成立，模块文档该改",
    )

    # ======================================================================
    section("C. 特征向量本身的自检（防低级错误）")

    # 方案 C（features 长度归一化）对**极短**测试串会把数值整体压低
    # （exp(-|log(len/3000)|*0.3)：8 字的串系数只有 0.16）。所以这里的用例
    # 一律**补到 ~3000 字**再量 —— 否则量的是归一化系数，不是被测特征本身。
    # （这两条曾长期误报：引号占比 0.078≠0.3~0.5、段长 CV 0.293<0.3，
    #   根因都是拿 8~34 字的串去卡「3000 字口径」的阈值。）
    def _pad(text: str, target: int = 3000) -> str:
        body = text if len(text) > 1 else text + "。"
        reps = max(1, target // max(len(body), 1))
        return "\n".join([body] * reps)

    cases = [
        ("全角问号必须计成问号而非漏计",
         "他为什么走？", lambda v: v["问号密度"] > 0),
        ("「……」算一句而不是多句",
         "他想了想……\n然后走了。", lambda v: v["句长均值"] > 0),
        ("引号内字数计入对白",
         _pad('“你来了。”他说。'), lambda v: 0.3 < v["对白字占比"] < 0.5),
        ("无引号时对白占比为 0",
         "他推开门走进去。", lambda v: v["对白字占比"] == 0),
        ("等长段落 → 段长 CV 极低",
         "\n".join(["甲乙丙丁戊己庚辛"] * 6), lambda v: v["段长CV"] < 0.01),
        ("参差段落 → 段长 CV 明显更高",
         _pad("\n".join(["甲", "甲乙丙丁戊己庚辛壬癸子丑寅卯辰", "甲乙"] * 2)),
         lambda v: v["段长CV"] > 0.3),
        ("段尾拔高被识别",
         "他明白了。\n原来这一切都是假的。", lambda v: v["段尾升华率"] > 0),
    ]
    for name, text, pred in cases:
        v = features(text)
        check(name, bool(pred(v)), json.dumps({k: round(x, 3) for k, x in v.items()},
                                              ensure_ascii=False)[:150])

    # ======================================================================
    section("D. describe() 的确定性与可读性（静态 prompt 块的前提）")

    p0 = profiles[n0]
    a, b = describe(p0), describe(p0)
    check("同一档案两次 describe 逐字节一致（可安全放进静态 system 块）", a == b,
          "不一致 → 会破坏 prompt 缓存")
    check("describe 非空且含具体数字（不是形容词）", len(a) > 120 and any(c.isdigit() for c in a),
          f"长度 {len(a)}")
    check("describe 不含典型空话形容词",
          not any(w in a for w in ("生动", "细腻", "引人入胜", "文笔优美")),
          "出现形容词即失去可验收性")
    print("\n  ---- describe() 输出 ----")
    for ln in describe(p0).split("\n"):
        print("    " + ln)

    # ======================================================================
    section("E. 档案摘要（给人看的判读材料）")

    print(f"\n  {n0} 的特征档案（unit_chars={UNIT}）：")
    print(f"  {'特征':<14}{'均值':>10}{'标准差':>10}{'p10':>10}{'p90':>10}"
          f"{'留出p90|z|':>11}  说明")
    for row in feature_table(p0):
        print(f"  {row['name']:<14}{row['mean']:>10.3f}{row['std']:>10.3f}"
              f"{row['p10']:>10.3f}{row['p90']:>10.3f}"
              f"{(row['holdout_p90_z'] if row['holdout_p90_z'] is not None else 0):>11.2f}"
              f"  {row['note']}{'  [代理]' if row['proxy'] else ''}")

    print(f"\n  留出集距离分布（人写的 {UNIT} 字单元到档案有多远）：")
    h = p0["holdout"]["mean_abs_z"]
    print(f"    p50={h['p50']:.3f}  p90={h['p90']:.3f}  max={h['max']:.3f}")
    print("    判读：这就是「正常」的范围。生成段报的是它在这个分布里的**分位**，")
    print("          而不是「超过我随手定的某个阈值」。分位 ≥97.5 才值得当异常处理。")

    print()
    print("=" * 78)
    print(f"结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    print("=" * 78)
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
