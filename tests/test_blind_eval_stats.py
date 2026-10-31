"""盲评二项检验的**口径校准**测试（2026-10-30 加）。

==============================================================================
为什么要有这条
==============================================================================

第一版把尾方向写反了：判据写成 `P(X ≥ k) < 0.05 ⇒ FAIL`，
结果「六题全选我们那段」（k = n = 6）被判成 **FAIL** ——
方向完全反了（那明明是最好的结果）。

现在按文档的口径改：判定用**下尾** `P(X ≤ k)`。
文档里给的数字正好能校准这一侧：

    n = 18 时，P(X ≤ 5) = 0.048 ⇒ k ≤ 5 判 FAIL
             P(X ≤ 6) = 0.096 ⇒ k ≥ 6 判 PASS

这两条就是 `docs/acceptance-metrics.md` §5.4 写的
「k ≥ 6 即不能拒绝随机猜（p ≥ 0.048）；k ≤ 5 则 p < 0.05，判定可被识别为 AI」。

⇒ 用文档里的数字钉住实现，比自己重推一遍可靠。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from verify_blind_eval import binom_tail_ge, binom_tail_le  # noqa: E402


def test_文档给的两个数必须对得上():
    """文档 §5.4：n=18 时 k=5 → p<0.05；k ≥ 6 → 不能拒绝随机猜。

    P(X ≤ 5 | n=18) = 12616/262144 = 0.0481 —— 正是文档写的那个 0.048。
    """
    assert abs(binom_tail_le(5, 18) - 0.0481) < 0.001, binom_tail_le(5, 18)
    assert binom_tail_le(6, 18) > 0.05, binom_tail_le(6, 18)
    # k=5 与 k=6 正好跨过 0.05 这条线（文档的门槛就落在这两格之间）
    assert binom_tail_le(5, 18) < 0.05 < binom_tail_le(6, 18)


def test_判定用的是下尾():
    """k 越大越不可能被判 FAIL —— 方向不能反。"""
    # 六题全选我们：下尾大（不可能比随机猜差）⇒ 不判 FAIL
    assert binom_tail_le(6, 6) > 0.05
    # 六题一题都没选我们：下尾小 ⇒ 判 FAIL
    assert binom_tail_le(0, 6) < 0.05
    # 随 k 单调不减
    vals = [binom_tail_le(k, 18) for k in range(0, 19)]
    assert vals == sorted(vals)


def test_上尾与下尾互补():
    # 互补关系：P(X ≤ k) + P(X ≥ k+1) = 1（k 取 0..n-1）
    for k in range(0, 18):
        assert abs(binom_tail_le(k, 18) + binom_tail_ge(k + 1, 18) - 1.0) < 1e-9


def test_评者不足时脚本必须说清楚没力量():
    """n=6 时 k=4 的 p=0.344 ——「随机猜」根本甩不掉 ⇒ 只能当观察。"""
    assert binom_tail_le(4, 6) > 0.05
    assert binom_tail_le(2, 6) > 0.05


# --------------------------------------------------------------------------- #
# 2026-10-31 第二次真跑暴露的两个缺陷（补测，防止再写反）
# --------------------------------------------------------------------------- #
#
# ① 题号写坏的行被**直接丢掉**
#    实测：评者把 `s02` 写成 `s`、`s09` 写成 `09`
#    ⇒ 两题被丢，n 从 17 掉到 15 ⇒ 检验力量更弱、结论更不准。
#
# ② ✓ 标记与「我们的段在哪一侧」**都是反的**
#    ⇒ 读脚本输出的人会被直接误导（「✓ 越多的题越像人写」恰好是错的）。


def _sheet(tmp_path, body: str):
    p = tmp_path / "sheet.md"
    p.write_text(
        "| sample | 更像人写 | 主线更清 | 备注 |\n"
        "|---|---|---|---|\n" + body,
        encoding="utf-8")
    return p


def test_题号写坏的行不能丢(tmp_path):
    """`s` / `09` 这种题号**要留下**（那一行的作答有价值）。"""
    from verify_blind_eval import parse_sheet

    p = _sheet(tmp_path, "| s01 | A | A | |\n| s | B | A | |\n| 09 | B | B | |\n")
    rows = parse_sheet(p)
    assert len(rows) == 3, rows
    assert [r.get("_bad_id") for r in rows] == [None, True, True]


def test_题号按表内顺序补回(tmp_path):
    """行序 = 题序 ⇒ 第 2 行补 s02、第 3 行补 s03。"""
    from verify_blind_eval import parse_sheet, repair_sample_ids

    p = _sheet(tmp_path, "| s01 | A | A | |\n| s | B | A | |\n| 09 | B | B | |\n")
    rows = parse_sheet(p)
    key_items = {"s01": {}, "s02": {}, "s03": {}}
    notes = repair_sample_ids(rows, key_items)
    assert notes, notes
    assert [r["sample"] for r in rows] == ["s01", "s02", "s03"]
    assert not any(r.get("_bad_id") for r in rows)


def test_行数对不上就不补(tmp_path):
    """行数 ≠ 题数 ⇒ 位置对应不可靠 ⇒ 不补，宁可作废。"""
    from verify_blind_eval import parse_sheet, repair_sample_ids

    p = _sheet(tmp_path, "| s01 | A | A | |\n| s | B | A | |\n")
    rows = parse_sheet(p)
    key_items = {"s01": {}, "s02": {}, "s03": {}}
    notes = repair_sample_ids(rows, key_items)
    assert notes and "作废" in notes[0], notes
    assert rows[1]["sample"] == "s"          # 没被改


def test_我们的段在哪一侧不能写反():
    """`our_slot_of` 返回「我们那段的位置」，不是「评者选了哪一侧」。"""
    from verify_blind_eval import our_slot_of

    assert our_slot_of({"slot_A": "ours", "slot_B": "human"}) == "A"
    assert our_slot_of({"slot_A": "human", "slot_B": "ours"}) == "B"


def test_E1包也能读_引擎侧算我们那一侧(tmp_path):
    """盲评 v2 的 E1 包：两侧都是模型产出，标签是 engine / generic。

    ⇒ 判定脚本不能只认 `"ours"` —— 否则 E1 包算出来的 k 全是 0。
    """
    from verify_blind_eval import our_slot_of

    assert our_slot_of({"slot_A": "engine", "slot_B": "generic"}) == "A"
    assert our_slot_of({"slot_A": "generic", "slot_B": "engine"}) == "B"


def test_记录表第1列改叫更好看_旧列名照样能读(tmp_path):
    """新包用「更好看」，旧包用「更像人写」⇒ 两者都归一到同一列。"""
    from verify_blind_eval import COL_BETTER, parse_sheet

    new = _sheet(tmp_path, "| s01 | A | B | |\n")
    old = _sheet(tmp_path, "| s01 | A | B | |\n")
    new.write_text(
        "| sample | 更好看 | 主线更清 | 备注 |\n|---|---|---|---|\n| s01 | A | B | |\n",
        encoding="utf-8")
    old.write_text(
        "| sample | 更像人写 | 主线更清 | 备注 |\n|---|---|---|---|\n| s01 | A | B | |\n",
        encoding="utf-8")
    assert COL_BETTER == "更好看"
    assert parse_sheet(new)[0][COL_BETTER] == "A"
    assert parse_sheet(old)[0][COL_BETTER] == "A"
