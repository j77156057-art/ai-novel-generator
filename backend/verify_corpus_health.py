"""语料体检（水印检测）的可证伪验证。全部离线。

真实语料只能证明「它抓到了斗破那一条」，**不能**证明它不误报 —— 而误报的代价对
文体档案是直接的（正常短语被当成水印删掉，档案就建在被削过的语料上）。
所以这里的主要内容是**合成对抗用例**，每条都写清楚「如果判据是错的，会怎样暴露」。

要证的事（每条都可能被证伪）：

  A. **不误报**：三本真实语料里，全部正常高频短语（`点点头`/`地说道`/`了一声`
     /`了摇头`/`的声音`）都不得入榜；并且要把它们的「句尾比例」实测出来，
     把与阈值 0.9 的余量摆出来 —— 余量窄就说明阈值是在赌，不是有区分度。

  B. **去嵌套必须是真去嵌套**：定长 n-gram 会在注入串周围造出一整套阶梯
     （`动乾坤` ⊂ `武动乾坤` ⊂ `道武动乾坤` ⊂ `笑道武动乾坤` …）。
     这里给四个方向的用例：
       B1 纯注入            → 只报 1 条，且是注入串本身
       B3 正文合法含 `撼动乾坤`（含 `动乾坤`）→ 仍只报 `武动乾坤`，不得报 `动乾坤`
       B4 注入串前缀恒定    → 报**带前缀**的那一串（前缀确实属于注入内容）
       B5 短串计数 > 长串计数（这次的实战形态）→ 不得两个都报

  C. **清洗不得伤及正文**（这是最要紧的一条：误删是静默损失，事后无法审计）。
     用**独立的第二实现**（逐字符走）当预言机，与模块的 regex 实现对拍；
     外加「已知干净文本 → 清洗后必须逐字相同」。

  D. 边界：空文本 / 无水印 / marks=[] / 水印出现在文末。
"""
from __future__ import annotations

import random
import re
import sys

from app.corpus_health import (
    DEFAULT_MIN_FINAL_RATIO,
    NEST_KEEP_RATIO,
    find_repeated_watermarks,
    strip_watermarks,
)

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ← {detail}" if not ok else ""))
    if ok and detail:
        print(f"         {detail}")


def section(t: str) -> None:
    print()
    print("-" * 78)
    print(t)
    print("-" * 78)


def load_corpus(db: str = "novel_dev.db") -> dict[str, str]:
    import sqlite3

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


# --------------------------------------------------------------------------
# 独立第二实现：逐字符走，O(n)，与模块的 regex 实现对拍
# --------------------------------------------------------------------------
def strip_oracle(text: str, marks: list[str], sent_final: str = "。！？") -> str:
    """预言机：不做整体替换，逐字符前进。故意用与模块不同的写法。"""
    out: list[str] = []
    i = 0
    n = len(text)
    verbs = sorted(set(marks), key=len, reverse=True)
    while i < n:
        hit = None
        for v in verbs:
            if text.startswith(v, i) and i + len(v) < n and text[i + len(v)] in sent_final:
                hit = v
                break
        if hit:
            i += len(hit)
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


# --------------------------------------------------------------------------
# 合成语料
# --------------------------------------------------------------------------
_BODY = [
    "他抬起头看向远处那一线灰白的天际",
    "风从山谷里卷上来带着些微凉意",
    "屋外的雨敲在青石板上响成一片",
    "火光照着墙上那道斜长的影子",
    "指尖在桌面上轻叩了两下",
    "远处传来几声零落的犬吠",
    "袖口上还沾着没干的露水",
    "她把茶盏往炉边挪了挪",
    "檐角的铜铃被风带得叮当作响",
    "院里的老槐树落了满地细碎的花",
    "空气里浮着一股潮湿的土腥味",
    "他停了停又把话咽了回去",
    "天色一点点沉下来",
    "廊下的灯笼晃了晃没有灭",
    "石阶上积着薄薄一层落叶",
    "不知是谁在隔壁低声说了句什么",
    "他把拢在袖里的手抽了出来",
    "铜壶里的水正好沸到了第三遍",
    "窗外那棵树上的鸟忽然全飞了",
    "他低头看了看自己掌心的纹路",
    "案上摊着的图纸边角已经卷起",
    "她把手里的帕子绞了又绞",
    "更鼓声从很远的地方传过来",
    "雨丝斜斜地打在窗纸上",
]


def synth(
    *,
    n_inject: int = 400,
    mark: str = "",
    legit: list[tuple[str, int, int]] | None = None,
    seed: int = 11,
) -> tuple[str, list[str]]:
    """造一份合成语料。

    - `mark`：注入串，追加在每个句子末尾（跟在句末标点**之前**）
    - `legit`：`[(短语, 句中出现次数, 句尾出现次数)]` 正常短语，混在句子里
    返回 (文本, 注入后的应有清洗结果——即正文句子本身)。
    """
    rng = random.Random(seed)
    legit = legit or []
    sents: list[str] = []
    plan: list[tuple[str, str]] = []  # (短语, 位置)

    for ph, mid_cnt, end_cnt in legit:
        plan += [(ph, "mid")] * mid_cnt + [(ph, "end")] * end_cnt
    rng.shuffle(plan)

    pool = list(_BODY)
    for i in range(n_inject + sum(c for _, c, _ in legit)):
        body = pool[i % len(pool)]
        if i < len(plan):
            ph, pos = plan[i]
            sents.append(f"他{ph}，{body}。" if pos == "mid" else f"{body}，他{ph}。")
        else:
            sents.append(body + "。")
    rng.shuffle(sents)

    if mark:
        # 必须**不重复采样**：重复采样会把水印叠在一起变成 `武动乾坤武动乾坤。`，
        # 于是没有一处跟在句末标点前，整个用例会假 PASS（我第一版就踩了这个坑）。
        assert n_inject <= len(sents), f"句子不够注入: {n_inject} > {len(sents)}"
        for k in rng.sample(range(len(sents)), n_inject):
            sents[k] = sents[k][:-1] + mark + "。"
    return "".join(sents), sents


def main() -> int:
    print("=" * 78)
    print("语料体检（水印检测）可证伪验证")
    print(f"min_final_ratio={DEFAULT_MIN_FINAL_RATIO}  nest_keep_ratio={NEST_KEEP_RATIO}")
    print("=" * 78)

    corpus = load_corpus()

    # ======================================================================
    section("A. 真实语料：命中数与「正常短语余量」")
    # ======================================================================
    marks_by_book: dict[str, list[dict]] = {}
    for name, text in corpus.items():
        hits = find_repeated_watermarks(text)
        marks_by_book[name] = hits
        print(f"  {name}: {len(text)} 字, 命中 {len(hits)} 条")
        for h in hits:
            print(f"      {h['text']!r} 计数={h['count']} 句尾比={h['final_ratio']} "
                  f"段尾比={h['para_end_ratio']}")
            print(f"        嵌套变体({len(h['nested_variants'])}): {h['nested_variants'][:6]}")
            print(f"        原文样本: {h['samples'][0][:56]}")

    n_hit_total = sum(len(v) for v in marks_by_book.values())
    check("三本真实语料合计只命中 1 条（预期：只有斗破的 SEO 水印）",
          n_hit_total == 1, f"实际 {n_hit_total} 条")
    if n_hit_total == 1:
        only = next(h for v in marks_by_book.values() for h in v)
        check("命中的就是《武动乾坤》这一串",
              only["text"] == "武动乾坤", repr(only["text"]))
        check("它 99% 以上直接跟在句末标点前（判据成立）",
              only["final_ratio"] >= 0.99, str(only["final_ratio"]))

    # 正常短语的句尾比例 —— 摆出余量，别让阈值看起来像在赌
    print()
    print("  正常短语句尾比例实测（判据是「句尾比例 >= 0.9」，余量越宽越可信）：")
    normal = ["点点头", "地说道", "了一声", "了摇头", "的声音", "的点了点头"]
    worst = 0.0
    worst_ph = ""
    for name, text in corpus.items():
        n_text = len(text)
        for ph in normal:
            occ = [(m.start(), m.end()) for m in re.finditer(re.escape(ph), text)]
            if len(occ) < 20:
                continue
            fin = sum(1 for _, e in occ if e < n_text and text[e] in "。！？")
            r = fin / len(occ)
            flag = "  <== 最接近阈值" if r > worst else ""
            if r > worst:
                worst, worst_ph = r, f"{name}/{ph}"
            print(f"    {name:12} {ph:8} 次数={len(occ):5} 句尾比={r:.3f}{flag}")
    check("全部正常短语的句尾比例都低于阈值，且至少留 2 倍余量",
          worst * 2 < DEFAULT_MIN_FINAL_RATIO,
          f"最接近的是 {worst_ph} = {worst:.3f}，阈值 {DEFAULT_MIN_FINAL_RATIO}，"
          f"余量 {DEFAULT_MIN_FINAL_RATIO / worst:.2f}×")

    # A3 真正的对抗式检验：把 min_count 压到 20，让全部正常短语都成为候选。
    # 如果判据是错的，这里就会误报 —— 上面那行「句尾比 0.4 < 0.9」只是诊断，
    # 这一行才是直接证伪：判据必须**自己**把正常短语挡在外面，而不是靠计数阈值挡。
    low: dict[str, list[str]] = {}
    for name, text in corpus.items():
        low[name] = [h["text"] for h in find_repeated_watermarks(text, min_count=20)]
    leaked = {k: sorted(set(v) & set(normal)) for k, v in low.items() if set(v) & set(normal)}
    check("A3 把 min_count 压到 20（正常短语全部成为候选）后仍不误报",
          not leaked, str(leaked))
    print(f"        min_count=20 时的候选总数: "
          f"{ {k: len(v) for k, v in low.items()} }（这些才是判据真正拦下来的）")

    # ======================================================================
    section("B. 去嵌套（合成对抗用例）")
    # ======================================================================
    # B1 纯注入
    txt, _ = synth(mark="武动乾坤")
    hits = find_repeated_watermarks(txt)
    check("B1 纯注入 → 恰好 1 条（不是阶梯里的 13 条）",
          len(hits) == 1, f"实际 {len(hits)}: {[h['text'] for h in hits]}")
    check("B1 报的就是注入串本身",
          hits and hits[0]["text"] == "武动乾坤", str([h["text"] for h in hits]))

    # B3 正文合法含「撼动乾坤」（其中含 `动乾坤`）—— 注入串仍须胜出
    txt3, _ = synth(mark="武动乾坤", legit=[("撼动乾坤", 20, 0)])
    hits3 = find_repeated_watermarks(txt3)
    names3 = [h["text"] for h in hits3]
    check("B3 正文合法含「撼动乾坤」时，仍只报 `武动乾坤`",
          names3 == ["武动乾坤"], f"实际 {names3}")
    check("B3 未把 `动乾坤` / `撼动乾坤` 当成水印",
          "动乾坤" not in names3 and "撼动乾坤" not in names3, f"实际 {names3}")
    print(f"        （`动乾坤` 此时计数被正文抬高到 "
          f"{txt3.count('动乾坤')} > `武动乾坤` {txt3.count('武动乾坤')}，"
          f"旧的「按计数降序 + in 判被包含」写法在这里必然漏掉）")

    # B4 前缀恒定 → 前缀属于注入内容，应当带上前缀
    txt4, _ = synth(mark="道武动乾坤")
    hits4 = find_repeated_watermarks(txt4)
    check("B4 注入串前缀恒定时，报带前缀的那一串（前缀确实属于注入内容）",
          len(hits4) == 1 and hits4[0]["text"] == "道武动乾坤",
          f"实际 {[h['text'] for h in hits4]}")

    # B5 短串计数 > 长串计数（本次实战形态）—— 不得两个都报
    print()
    print("  计数阶梯（B3 用例实测，说明为什么不能按计数排序去嵌套）：")
    cnt_all: dict[str, int] = {}
    for m in re.finditer(r"[\u4e00-\u9fff]+", txt3):
        s = m.group(0)
        for L in (3, 4, 5, 6):
            for i in range(len(s) - L + 1):
                cnt_all[s[i:i + L]] = cnt_all.get(s[i:i + L], 0) + 1
    ladder = sorted(((c, k) for k, c in cnt_all.items() if "武动乾坤" in k), reverse=True)
    for c, k in ladder[:6]:
        print(f"    {k!r:12} 计数={c}")
    check("B5 短串(`动乾坤`)计数确实高于长串(`武动乾坤`)——这正是旧写法失效的原因",
          txt3.count("动乾坤") > txt3.count("武动乾坤"),
          f"{txt3.count('动乾坤')} vs {txt3.count('武动乾坤')}")

    # ======================================================================
    section("C. 清洗安全性（误删正文是静默损失，必须双实现对拍）")
    # ======================================================================
    # C1 合成：已知干净文本 → 清洗后必须逐字相同
    clean_units = [b + "。" for b in _BODY]
    clean_text = "".join(clean_units * 3)
    txt_c, _ = synth(mark="武动乾坤")
    stripped_c = strip_watermarks(clean_text, find_repeated_watermarks(txt_c))
    check("C1 对**干净文本**施清洗 → 逐字不变",
          stripped_c == clean_text, f"长度 {len(clean_text)} → {len(stripped_c)}")

    # C2 合成：注入文本 → 清洗后必须等于「去掉注入串」的文本
    expect = txt_c.replace("武动乾坤。", "。")
    check("C2 对注入文本施清洗 → 等于逐点去掉注入串的结果",
          strip_watermarks(txt_c, find_repeated_watermarks(txt_c)) == expect,
          f"差 {abs(len(strip_watermarks(txt_c, find_repeated_watermarks(txt_c)))-len(expect))} 字")

    # C3 真实语料：与独立实现（逐字符）对拍
    t = corpus["斗破苍穹"]
    hits = marks_by_book["斗破苍穹"]
    if hits:
        got = strip_watermarks(t, hits)
        want = strip_oracle(t, [h["text"] for h in hits])
        check("C3 真实语料：模块实现 == 独立逐字符实现（对拍）", got == want,
              f"长度 {len(got)} vs {len(want)}")

        # C4 字数账必须对得上：删除量 == 被删处数 × 串长
        removed = len(t) - len(got)
        expect_removed = hits[0]["final"] * len(hits[0]["text"])
        check("C4 删除字数 == 句尾命中次数 × 串长（无多删无少删）",
              removed == expect_removed, f"实删 {removed}, 应收 {expect_removed}")

        # C5 上下文短语计数不得变化（水印之外的正文一个字都不该动）
        ctx_ok = all(t.count(w) == got.count(w) for w in ("笑道", "的目光", "点点头", "的气息"))
        check("C5 正文上下文短语计数前后不变（未误删上下文）", ctx_ok,
              str({w: (t.count(w), got.count(w)) for w in ("笑道", "的目光", "点点头", "的气息")}))
        print(f"        清洗: {len(t)} → {len(got)} 字, 删 {removed} "
              f"({removed/len(t)*100:.2f}%), 残留水印 {got.count('武动乾坤')} 处"
              f"（= 未被句末标点跟随的 {hits[0]['count']-hits[0]['final']} 处，故意不删）")
    else:
        check("C3-C5 需要斗破命中水印", False, "未命中")

    # ======================================================================
    section("D. 边界")
    # ======================================================================
    check("D1 空文本 → []", find_repeated_watermarks("") == [])
    check("D2 短文本（< min_count）→ []", find_repeated_watermarks("你好。") == [])
    check("D3 strip_watermarks(text, []) → 原样返回",
          strip_watermarks("你好。", []) == "你好。")
    tail = "".join(b + "武动乾坤。" for b in _BODY) * 8
    check("D4 未通过判据的串不会被删（判据与删除用同一把尺）",
          strip_watermarks(tail, [{"text": "的一点", "count": 1}]) == tail)

    # ======================================================================
    section("E. 清洗必须让档案变好 —— 否则清洗就没有存在意义")
    # ======================================================================
    # 这是本模块存在的**唯一理由**。如果清洗前后档案质量没差别，那正确做法是
    # 把这个模块删掉，而不是留着当仪式。所以这里设 FAIL 闸门。
    from app.style.stylometry import cut_units, fit_profile, style_distance

    unit = 3000
    order = [n for n in ("大奉打更人", "豪门宠婚，爵少你别闹", "斗破苍穹") if n in corpus]

    def measure(corp: dict[str, str]) -> dict:
        profs = {n: fit_profile([corp[n]], unit_chars=unit, label=n) for n in order}
        ov, sep, n_sep = 0.0, 0, 0
        for n in order:
            p = profs[n]
            own_p90 = p["holdout"]["mean_abs_z"]["p90"]
            for m in order:
                if m == n:
                    continue
                ds = sorted(
                    style_distance(u, p)["mean_abs_z"]
                    for u in cut_units(corp[m], unit, max_units=60)
                )
                ov = max(ov, sum(1 for d in ds if d <= own_p90) / len(ds))
                n_sep += 1
                sep += 1 if ds[max(0, len(ds) // 10 - 1)] > own_p90 else 0
        return {
            "p90": profs["斗破苍穹"]["holdout"]["mean_abs_z"]["p90"],
            "overlap": ov,
            "sep": sep,
            "n_sep": n_sep,
        }

    cleaned_corpus = {n: strip_watermarks(t, marks_by_book[n]) for n, t in corpus.items()}
    dirty = measure(corpus)
    clean = measure(cleaned_corpus)
    print(f"  {'读数':<24}{'清洗前':>10}{'清洗后':>10}")
    print(f"  {'斗破自身留出 p90':<22}{dirty['p90']:>10.3f}{clean['p90']:>10.3f}")
    print(f"  {'跨语料最坏重叠率':<22}{dirty['overlap']*100:>9.0f}%{clean['overlap']*100:>9.0f}%")
    print(f"  {'分位分离成立':<24}{dirty['sep']:>7}/{dirty['n_sep']}{clean['sep']:>7}/{clean['n_sep']}")
    check("清洗后斗破的自身留出离散度下降（档案更紧）",
          clean["p90"] < dirty["p90"], f"{dirty['p90']:.3f} → {clean['p90']:.3f}")
    check("清洗后跨语料最坏重叠率下降（判别力变好）",
          clean["overlap"] < dirty["overlap"],
          f"{dirty['overlap']*100:.0f}% → {clean['overlap']*100:.0f}%")
    check("清洗后分位分离成立比例不降低",
          clean["sep"] >= dirty["sep"], f"{dirty['sep']}/{dirty['n_sep']} → {clean['sep']}/{clean['n_sep']}")

    # 对照组：没被清洗的两本，自身留出 p90 必须逐位不变（证明是单变量）
    ctrl = []
    for n in order[:2]:
        a = fit_profile([corpus[n]], unit_chars=unit)["holdout"]["mean_abs_z"]["p90"]
        b = fit_profile([cleaned_corpus[n]], unit_chars=unit)["holdout"]["mean_abs_z"]["p90"]
        ctrl.append((n, a, b))
    check("对照组（未被清洗的两本）留出 p90 逐位不变 —— 位移只能归因于清洗",
          all(a == b for _, a, b in ctrl),
          str([(n, a, b) for n, a, b in ctrl]))
    for n, a, b in ctrl:
        print(f"    {n:<12} {a:.6f} → {b:.6f}   Δ={b-a:+.10f}")

    # ======================================================================
    print()
    print("=" * 78)
    print(f"PASS {len(PASSED)}   FAIL {len(FAILED)}")
    if FAILED:
        for f in FAILED:
            print(f"  FAIL: {f}")
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
