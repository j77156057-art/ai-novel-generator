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
