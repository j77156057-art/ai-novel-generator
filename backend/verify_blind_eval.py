"""盲评判据：把「更像人写」的作答变成**可算的判定**，不靠感觉。

==============================================================================
用在哪：docs/acceptance-metrics.md §5「人工盲评流程」。
==============================================================================

判据（与该文档一致，只是把它写成可执行的）：

- 每份题面 **N 道题**（跨位置取样；默认 N=6，可用 `--snips` 加到 18）。
- 评者**只看 A/B**，不知道谁是谁；映射在密封文件里。
- 记「我们那段被判为**更像人写**」的次数 k，判定数 n = 评者人数 × N。
- 二项检验（H₀：评者只是随机猜，p = 0.5），看**下尾**：

      P(X ≤ k) < 0.05  ⇒ 我们的段显著更少被选中 ⇒ **可被识别为 AI** ⇒ FAIL
      P(X ≤ k) ≥ 0.05  ⇒ 不能拒绝随机猜 ⇒ 判为「不可被识别为 AI」⇒ PASS

  ⚠️ 下尾是**单边**的：文档里的门槛是「k ≥ 6（n=18）即通过」，
  也就是说**只有明显更差才判 FAIL**，比随机猜略差（k=6/18）也算通过。
  这是刻意的宽松口径 —— 盲评样本小，宁可放过也不误杀。

  脚本**同时**打印上尾 `P(X ≥ k)`：它 < 0.05 说明我们的段显著**更常**被选中
  （比随机猜更像人写），这是比「通过」更强的证据。

  （口径校准：n=18 时 P(X ≤ 5) = 0.048 —— 与文档里写的 0.048 对得上，
  确认这一侧的尾就是文档要的那一侧。）

⚠️ 人数不足时这个检验**没有力量**：n=6（1 个评者 × 6 题）时，k=4 的 p=0.344，
   k=2 的 p=0.109 —— 也就是「随机猜」根本甩不掉 ⇒ 这时候只能当作**观察**，
   不能当结论。脚本会把这一点明确打出来，不让人误读成「通过」。
   ⇒ 想要 n=18，有两条路：**多请人**（3 人 × 6 题）或**把题数加到 18**
   （1 人 × 18 题）。后者省事，但同一个人的 18 次判断不是独立的
   （口味是相关的）⇒ 符号检验会把 p 值算得偏乐观，结论只能说「倾向」。

## 记录表只有两列，另一列用来出**交叉表**

- 第 1 列「更像人写」—— 判定用（上面的二项检验）；
- 第 2 列「主线更清」—— **独立维度**（叙事清晰度 ≠ 像不像人写的）；
  脚本拿它出交叉表：两列**指向不同侧**的那些题 = 诊断信息
  （我们的产出是「主线清楚但不像人写」⇒ 语言层问题；
  「像人写但主线乱」⇒ 结构层问题）。

⚠️ 旧的四列表（「有 AI 腔」「猜 AI」）在**强制二选一**下与第 1 列**互补**
⇒ 信息量为零 ⇒ 2026-10-31 删掉（判定脚本本来也只用第 1 列）。
旧表仍能读；**按表头名取列，不按位置**。

用法：
    python verify_blind_eval.py --sheet out/acceptance/blind_sheet.csv \\
                                --key  out/acceptance/blind_key.json
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path

# 题面里「更像人写」是第 1 列判断
COL_HUMAN = "更像人写"
COL_MAIN = "主线更清"
COL_AI = "有AI腔"
COL_GUESS = "猜AI"

#: 旧表头 → 规范名（表头里可能有空格）。
#:
#: ⚠️ **只有两列是有效的**：「更像人写」「主线更清」。
#: 「有 AI 腔」「猜 AI」两列在**强制二选一**下与第一列**互补**（必然取反）⇒
#: 信息量为零 ⇒ 2026-10-31 删掉（主人当场指出：「猜了哪个更像人写，
#: 另外那个不就是 AI 了么」⇒ 同一个意思问了两遍）。
#:
#: 旧表仍然能读：多出来的列按表头位置取，不影响判定。
_ALIASES = {
    "更像人写": COL_HUMAN,
    "主线更清": COL_MAIN,
    "有AI腔": COL_AI,
    "猜AI": COL_GUESS,
}


def _norm_header(cell: str) -> str:
    """表头归一：去掉空格与强调符号。"""
    return re.sub(r"[\s*]", "", cell or "")


def parse_sheet(path: Path) -> list[dict]:
    """读作答表。支持 CSV 与 Markdown 表格两种填法。

    ⚠️ **按表头取列**，不按位置 —— 位置一改就全错位（2026-10-31 删列时踩过）。
    表头认不出来时退回位置约定（sample / 更像人写 / 主线更清 / …）。
    """
    text = path.read_text(encoding="utf-8")
    rows: list[dict] = []
    if path.suffix.lower() == ".csv":
        for r in csv.DictReader(text.splitlines()):
            if (r.get("sample") or "").strip():
                rows.append({k: (v or "").strip() for k, v in r.items()})
        return rows
    # markdown 表：先读表头拿列名，再按列名取
    header: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [_norm_header(c) for c in line.strip("|").split("|")]
        if not header and cells and cells[0] == "sample":
            header = cells
            continue
        if not cells or not re.match(r"^s\d+$", cells[0]):
            continue
        if not header:
            header = ["sample", COL_HUMAN, COL_MAIN, COL_AI, COL_GUESS, "备注"]
        rec: dict[str, str] = {}
        for i, cell in enumerate(cells):
            name = header[i] if i < len(header) else ""
            if not name:
                continue
            rec[_ALIASES.get(name, name)] = cell
        rec.setdefault("sample", cells[0])
        rows.append(rec)
    return rows


def binom_tail_le(k: int, n: int) -> float:
    """P(X ≤ k)，X ~ Binom(n, 0.5)。**下尾** —— 文档要的那一侧。"""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    total = 2 ** n
    return sum(math.comb(n, i) for i in range(0, k + 1)) / total


def binom_tail_ge(k: int, n: int) -> float:
    """P(X ≥ k)，X ~ Binom(n, 0.5)。**上尾** —— 用来显示「比随机猜更像人写」的强度。"""
    if n <= 0:
        return 1.0
    k = max(0, min(k, n))
    total = 2 ** n
    return sum(math.comb(n, i) for i in range(k, n + 1)) / total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--key", required=True)
    args = ap.parse_args()

    sheet_path = Path(args.sheet)
    key_path = Path(args.key)
    rows = parse_sheet(sheet_path)
    key_obj = json.loads(key_path.read_text(encoding="utf-8"))
    key_items = {it["sample_id"]: it for it in key_obj["items"]}

    print("=" * 78)
    print("盲评判定")
    print("=" * 78)

    # —— 先看这个包**是不是盲评** ——
    # 密封文件里 `blind=false` 说明生成时检出这个包不满足盲评条件
    # （题材/年代跨代、或脱敏痕迹密度差得出来）
    # ⇒ 评者不看内容就能分出哪边是人写的 ⇒ **不能做统计判定**。
    if key_obj.get("blind") is False:
        print("⚠️ 这个包**不是盲评包**（密封文件里 blind=false）。")
        for reason in (key_obj.get("blind_reasons") or []):
            print(f"   · {reason}")
        ev = (key_obj.get("evidence") or {})
        if ev.get("era_profiles"):
            print(f"   年代标记密度：{ev['era_profiles']}")
        if ev.get("mask_density"):
            print(f"   脱敏痕迹密度：{ev['mask_density']}")
        print("   ⇒ **不出统计结论**。要出结论，得用**同题材的人类语料**重出包。")
        print(f"   （人类语料来源：{key_obj.get('human_corpus')}）")
        return 2

    if not rows:
        print("作答表里没有可用的行（sample 列要形如 s01）")
        return 1

    answered = [r for r in rows if (r.get(COL_HUMAN) or "").upper() in ("A", "B")]
    missing = [r["sample"] for r in rows if r not in answered]
    if missing:
        print(f"⚠️ 还没作答的题：{missing}（按已作答的算，结果偏乐观）")

    # ⚠️ **备注列写了的题单独拎出来** —— 评者如果认出某一侧出自哪本书，
    #    那一题的判断就不是「盲」的了 ⇒ 人工剔除，不计入统计。
    flagged = [r["sample"] for r in answered
               if (r.get("备注") or r.get("remark") or r.get("note") or "").strip()]
    if flagged:
        print(f"⚠️ 这些题**评者写了备注**（可能认出了出处）⇒ 请人工确认是否剔除：{flagged}")

    n = len(answered)
    k = 0
    detail = []
    #: 第二列「主线更清」的交叉分析：对我们那一段，四种组合各几题
    cross = {"both": 0, "human_only": 0, "ours_only": 0, "neither": 0}
    k_main = 0
    n_main = 0
    for r in answered:
        item = key_items.get(r["sample"])
        if not item:
            print(f"  {r['sample']}：密封文件里没有这一题 —— 跳过")
            continue
        slot = item["slot_A"] if (r[COL_HUMAN] or "").upper() == "A" else item["slot_B"]
        ours = (slot == "ours")
        if ours:
            k += 1
        detail.append((r["sample"], "A" if ours else "B", r.get(COL_HUMAN, "").upper(),
                       item["our_segment_no"]))

        # —— 第二列：主线更清（**独立维度**：叙事清晰度，不是「像不像人写的」）——
        ans_main = (r.get(COL_MAIN) or "").upper()
        if ans_main in ("A", "B"):
            n_main += 1
            slot_main = item["slot_A"] if ans_main == "A" else item["slot_B"]
            ours_main = (slot_main == "ours")
            if ours_main:
                k_main += 1
            if ours and ours_main:
                cross["both"] += 1
            elif ours:
                cross["human_only"] += 1
            elif ours_main:
                cross["ours_only"] += 1
            else:
                cross["neither"] += 1

    print()
    for s, ours_slot, ans, seg in detail:
        mark = "✓" if ours_slot == ans else " "
        print(f"  {s}  我们的段在 {ours_slot} 侧，评者选 {ans}  {mark}"
              + (f"（第 {seg} 段）" if seg else ""))

    p = binom_tail_le(k, n)
    p_up = binom_tail_ge(k, n)
    print()
    print(f"  n = {n}　k = {k}（我们那段被判「更像人写」的次数）")
    print(f"  P(X ≤ k | p=0.5) = {p:.4f}　← 判定用的那一侧（下尾）")
    print(f"  P(X ≥ k | p=0.5) = {p_up:.4f}　← 反向证据：比随机猜更常被选中")

    if n_main:
        p_main = binom_tail_le(k_main, n_main)
        print()
        print(f"  【第二列】n = {n_main}　k = {k_main}"
              f"（我们那段被判「主线更清」的次数）　P(X ≤ k) = {p_main:.4f}")
        print("  两列交叉（对我们那段）：")
        print(f"    更像人写 + 主线更清 都是我们 = {cross['both']}")
        print(f"    只有更像人写是我们           = {cross['human_only']}")
        print(f"    只有主线更清是我们           = {cross['ours_only']}")
        print(f"    两个都不是我们               = {cross['neither']}")
        split = cross["human_only"] + cross["ours_only"]
        print(f"    ⇒ 两列**指向不同侧**的题 = {split}/{n_main}"
              "（评者认为「主线更清楚的那段」和「更像人写的那段」不是同一段）")
        if split:
            print("      这一格是**诊断信息**：我们的产出若是「主线清楚但不像人写」，")
            print("      问题在语言层（腔、句式）；若是「像人写但主线乱」，问题在结构层。")
    else:
        print("  （第二列「主线更清」没有作答 —— 交叉分析跳过；判定不受影响。）")

    if n < 18:
        print(f"  ⚠️ n = {n} < 18：这个检验**没有足够力量**"
              f"（多请人，或把题数加到 18，凑够 n=18）。")
        print("     ⇒ 本次结果只能当**观察**，不能当结论。")
    if p < 0.05:
        print("  ⇒ 判定：我们的产出**可被识别为 AI**（FAIL）——")
        print("     先看是哪一维出的问题（比喻密度 / 句长齐 / 升华结尾），再改提示词，")
        print("     别靠「感觉不好」去调参。")
    else:
        print("  ⇒ 判定：不能拒绝「评者只是随机猜」⇒ 我们的产出**不可被识别为 AI**（PASS）")
        if p_up < 0.05:
            print("     （且上尾 < 0.05 ⇒ 我们的段比随机猜**更常**被选中，证据比「通过」更强）")
    return 0 if p >= 0.05 else 1


if __name__ == "__main__":
    raise SystemExit(main())
