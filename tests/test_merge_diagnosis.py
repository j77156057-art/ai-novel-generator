"""形态合并的失败诊断：**并 0 段时要说清是哪一种成因**（2026-10-27）。

## 这份测试盯的是一件事

原来「找不到可并的叙述」是一句笼统的话，但它其实盖着**两种成因相反**的情况：

| 成因 | 特征 | 处置方向 |
|---|---|---|
| **真没有纯叙述段** | `N = 0` | 没辙，形状本来就那样 |
| **有纯叙述段但不相邻** | `N > 0` 却一个配对都找不到 | **混合段把链隔断了** —— 可救 |

第二种曾被误判成第一种（实测某轮真跑 `N=15 / B=22 / M=32`）：
一句笼统的「找不到」会让人**顺着错的方向去修**（以为「这形状没办法」）。

⇒ 这里钉住：两种成因的 reason **必须不同**，且都带分类计数。

不依赖真实网络、不依赖真实语料。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.style.form_merge import _classify, merge_bare_dialogue  # noqa: E402


def _narration(text: str) -> str:
    """一段纯叙述（引号外字数够长，可配对）。"""
    return text


# ====================================================================
# 1. 成因一：真没有纯叙述段
# ====================================================================


def test_reason_when_no_narration_at_all():
    """全是裸台词段 ⇒ 报「没有可并的纯叙述段」，并给出计数。"""
    paras = ["“第一句。”", "“第二句。”", "“第三句。”"]
    out, info = merge_bare_dialogue(paras, target_ratio=0.1)

    assert info["merged"] == 0
    assert "没有可并的纯叙述段" in info["reason"]
    # 计数在诊断里 ⇒ 光看 reason 也能定位
    assert info["counts"]["N"] == 0
    assert info["counts"]["B"] == 3


# ====================================================================
# 2. 成因二：有纯叙述段，但与裸台词段不相邻（被混合段隔断）
# ====================================================================


def test_reason_when_narration_exists_but_not_adjacent():
    """有纯叙述段，却被混合段隔断 ⇒ reason 必须与「真没有」**不同**。

    这是本次要修的核心：原来两种情况都报「找不到可并的叙述」，
    让人以为「叙述根本不存在」⇒ 顺着错方向去修。
    """
    # 形状：N（纯叙述） M（混合） B（裸台词） M（混合） B ……
    # N 与 B 从不相邻 ⇒ 配对数 0，但叙述**存在**
    #
    # ⚠️ 分类口径（`is_bare_paragraph`）：**引号外字数 ≤ 6 才是裸台词段**。
    # 所以混合段必须**引号外有 7 字以上**，例如「……他问。」这类。
    paras = [
        "他低头看了看桌上的账册，手指在纸页上停了很久。",   # N（纯叙述）
        "“你说这话是什么意思。”他抬起眼睛看她。",            # M（混合，引号外 7+ 字）
        "“我不明白。”",                                    # B（裸台词）
        "“那就别问了。”她把杯子推回桌子中央。",              # M（混合，引号外 7+ 字）
        "“随你。”",                                        # B（裸台词）
    ]
    kinds = _classify(paras, 7)
    assert kinds.count("N") == 1, kinds
    assert kinds.count("B") == 2, kinds
    assert kinds.count("M") == 2, kinds

    out, info = merge_bare_dialogue(paras, target_ratio=0.1)

    assert info["merged"] == 0
    reason = info["reason"]
    # 关键：说的是「有但不相邻」，**不是**「没有」
    assert "没有一段与裸台词段相邻" in reason, reason
    assert "没有可并的纯叙述段" not in reason, reason
    # 计数如实
    assert info["counts"]["N"] == 1
    assert info["counts"]["B"] == 2
    assert info["counts"]["M"] == 2
    # 原文一个字都没动
    assert out == paras


# ====================================================================
# 3. 行为零变化：诊断改进不能碰任何合并结果
# ====================================================================


def test_diagnostic_change_does_not_alter_merging():
    """同一份输入 ⇒ 合并结果与守恒检查与改之前完全一样。"""
    # 有可配对：N B 相邻
    paras = [
        "他低头看了看桌上的账册，手指在纸页上停了很久。",
        "“第一句。”",
        "“第二句。”",
        "她把杯子推回去。",
        "“第三句。”",
    ]
    out, info = merge_bare_dialogue(paras, target_ratio=0.2)

    assert info["merged"] >= 1
    assert info["reason"] == "已并"
    # 「把冒号改回去」之后必须与原文逐字相同 ⇒ 除那一处标点形态修正外一字未动
    revert = info["revert_paragraphs"]
    assert "".join(revert).replace("：", "。") == "".join(paras)


def test_counts_present_in_every_outcome():
    """诊断字段在**每条路径**上都在（并成了、没并成、已达目标都要有）。"""
    cases = [
        ["“只有台词。”", "“还是台词。”"],                       # 没有纯叙述
        ["他低头看了看账册，手指停了很久。", "“台词。”"],           # 有可配对
        ["他看了看账册。", "“台词。”", "“还是台词。”"],            # 已达目标
    ]
    for paras in cases:
        _out, info = merge_bare_dialogue(paras, target_ratio=0.5)
        assert "counts" in info, info
        for k in ("N", "n", "B", "M"):
            assert k in info["counts"], info
        assert sum(info["counts"].values()) == len(paras), info
