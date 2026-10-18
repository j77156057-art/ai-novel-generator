"""AI 腔检测器增强（取例句 + 子串去重）回归测试。

守护三件事：

  D1 新返回结构：`occurrences` 每个命中的 pattern 带 `[{offset, snippet}]`，
     snippet 是命中处前后各 15 字的原文片段（触界补省略号）——「总结本次
     具体哪里犯了 AI 腔」靠它取例句，只有「仿佛×3」是讲不出问题的。
  D2 子串重复计数 bug：`AI_FLAVOR_PATTERNS` 同时含「毫无疑问」与
     「毫无疑问地」，旧实现逐模式 `text.count()` 会让同一处被两个模式
     各计一次（total 虚高）。现按长模式优先 + 区间占用去重，钉死只计 1 次。
  D3 向后兼容：`hits`（模式→次数）与 `total` 语义不变，`_should_rewrite`
     的阈值行为（AI 腔 4 不告警 / 5 告警，反向禁忌 1 不告警 / 2 告警）不变。

纯函数测试，不接 db、零 LLM。
"""

from __future__ import annotations

import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.generation.engine import (  # noqa: E402
    _FLAVOR_SNIPPET_CONTEXT,
    _detect_ai_flavor,
    _should_rewrite,
)

#: 构造「截断两侧」用的超长前后文（各 20 字 > 15 字窗口）
_LONG_PREFIX = "甲" * 20
_LONG_SUFFIX = "乙" * 20


# ---------------------------------------------------------------------------
# D1：offset 与 snippet
# ---------------------------------------------------------------------------


def test_occurrences_carry_offset_and_snippet_with_truncation_marks():
    """命中处前后各 15 字进 snippet；被截断的两侧补「…」。"""
    text = _LONG_PREFIX + "仿佛" + _LONG_SUFFIX
    flavor = _detect_ai_flavor(text)

    occ = flavor["occurrences"]["仿佛"]
    assert len(occ) == 1, f"应只有一次命中：{occ}"
    assert occ[0]["offset"] == 20, f"offset 应指向「仿佛」起始下标：{occ[0]}"
    assert occ[0]["snippet"] == (
        "…" + "甲" * _FLAVOR_SNIPPET_CONTEXT + "仿佛" + "乙" * _FLAVOR_SNIPPET_CONTEXT + "…"
    ), f"snippet 应为命中处前后各 {_FLAVOR_SNIPPET_CONTEXT} 字：{occ[0]!r}"


def test_snippet_at_text_start_has_no_leading_ellipsis():
    """命中贴着文本开头：头部不补省略号（原文确实就这么长），尾部补。"""
    text = "仿佛" + "乙" * 40
    occ = _detect_ai_flavor(text)["occurrences"]["仿佛"]

    assert occ[0]["offset"] == 0
    assert occ[0]["snippet"] == "仿佛" + "乙" * _FLAVOR_SNIPPET_CONTEXT + "…"


def test_multiple_occurrences_are_all_listed_in_order():
    """同一模式多次命中全部列出，offset 升序——例句要按出现顺序可读。"""
    text = "甲" * 20 + "他深吸一口气。" + "乙" * 20 + "他深吸一口气。" + "丙" * 20
    flavor = _detect_ai_flavor(text)

    occ = flavor["occurrences"]["深吸一口气"]
    # 「甲×20」占 0-19，「他」占 20，模式「深吸一口气」从 21 起；
    # 第二处：前段共 27 字（0-26），「乙×20」占 27-46，「他」占 47，模式从 48 起。
    assert [o["offset"] for o in occ] == [21, 48], f"offset 应升序且精确：{occ}"
    # 两处命中都远离文本边界，snippet 两侧均应被截断并补「…」（前后各 15 字）
    assert occ[0]["snippet"] == (
        "…" + "甲" * 14 + "他深吸一口气。" + "乙" * 14 + "…"
    ), f"snippet 应为命中处前后各 15 字：{occ[0]!r}"
    assert occ[1]["snippet"] == (
        "…" + "乙" * 14 + "他深吸一口气。" + "丙" * 14 + "…"
    ), f"snippet 应为命中处前后各 15 字：{occ[1]!r}"
    assert flavor["hits"]["深吸一口气"] == 2


# ---------------------------------------------------------------------------
# D2：子串重复计数（长模式优先）
# ---------------------------------------------------------------------------


def test_wu_yi_wen_di_counts_once_not_twice():
    """「毫无疑问地」只计 1 次——旧实现会被「毫无疑问」+「毫无疑问地」双计。"""
    flavor = _detect_ai_flavor("他毫无疑问地推门而入。")

    assert flavor["hits"] == {"毫无疑问地": 1}, (
        f"「毫无疑问地」应只归最长模式一次，实际 hits={flavor['hits']}"
    )
    assert flavor["total"] == 1, f"total 应为 1（旧实现为 2，虚高）：{flavor['total']}"
    assert "毫无疑问" not in flavor["hits"], "短模式不应把长模式的命中再计一遍"


def test_short_pattern_still_counts_when_not_covered_by_longer():
    """短模式独立出现时照常计数；被长模式盖住的那次不计数。"""
    text = "毫无疑问，他会来。毫无疑问地，她也会来。"
    flavor = _detect_ai_flavor(text)

    assert flavor["hits"] == {"毫无疑问": 1, "毫无疑问地": 1}, (
        f"独立的「毫无疑问」应保留，被盖住的不应重复：{flavor['hits']}"
    )
    assert flavor["total"] == 2, f"total 应为 2（旧实现为 3）：{flavor['total']}"


def test_same_pattern_non_overlapping_semantics_match_str_count():
    """同模式内部仍按「不重叠」计数（与 str.count 一致），不逐字滑动。"""
    text = "仿佛" * 3
    flavor = _detect_ai_flavor(text)

    assert flavor["hits"]["仿佛"] == text.count("仿佛") == 3
    assert [o["offset"] for o in flavor["occurrences"]["仿佛"]] == [0, 2, 4]


# ---------------------------------------------------------------------------
# D3：向后兼容——hits/total 语义与 _should_rewrite 阈值
# ---------------------------------------------------------------------------


def test_hits_and_total_plain_semantics_preserved():
    """无子串纠缠时，hits/total 与旧版逐字节同语义。"""
    flavor = _detect_ai_flavor("仿佛如梦。宛如隔世。")

    assert flavor["hits"] == {"仿佛": 1, "宛如": 1}
    assert flavor["total"] == 2


def test_should_rewrite_threshold_four_no_five_yes():
    """AI 腔阈值：恰好 4 命中不告警、恰好 5 命中告警（既有回归语义）。"""
    assert _should_rewrite("突然之间" * 4, None) == (False, [])

    need, reasons = _should_rewrite("突然之间" * 5, None)
    assert need is True
    assert reasons and all(isinstance(r, str) for r in reasons)
    assert any("突然之间" in r and "5" in r for r in reasons), (
        f"告警原因应带上模式与次数：{reasons}"
    )


def test_should_rewrite_forbidden_threshold_unchanged():
    """反向禁忌阈值：1 次不告警、2 次告警（与检测器改动解耦，防回归）。"""
    forbidden = {"禁用转折词": ["紫水晶吊坠"]}
    assert _should_rewrite("林夜握紧了紫水晶吊坠。", forbidden)[0] is False

    need, reasons = _should_rewrite(
        "林夜握紧了紫水晶吊坠。月光下，他举起紫水晶吊坠。", forbidden
    )
    assert need is True
    assert any("紫水晶吊坠" in r for r in reasons)


def test_occurrences_consistent_with_hits_and_json_serializable():
    """occurrences 与 hits 计数一致，且整体可 JSON 序列化（要进告警/记忆）。"""
    text = "终于" * 3 + "从此" * 2
    flavor = _detect_ai_flavor(text)

    assert flavor["hits"] == {"终于": 3, "从此": 2}
    assert {k: len(v) for k, v in flavor["occurrences"].items()} == flavor["hits"]
    json.dumps(flavor, ensure_ascii=False)  # 不可序列化会直接抛
