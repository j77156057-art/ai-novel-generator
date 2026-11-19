"""蒸馏产物（叙事档案）审核（main 侧无测试，这里补最小冒烟 + 阈值断言）。

`audit_narrative` 自身只依赖 `method_rules(profile)` 与
`narrative._PLOT_BEARING_DIMS` / `RENDER_PRESETS` 的形状，而这两个模块在 feat
侧尚未搬（第 8 批 style 层）。因此用同形状的替身把**规则判定**钉住：
- 没有档案 → info，不是 error；
- 规则过短按比例 warn/error（10% / 30% 两档）；
- 单维超过 100 条 → info，且引用的是 `RENDER_PRESETS` 的**现行配额**；
- 剧情型维度的「剧情成分」不算泄漏，只统计非剧情维度。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.agents import audit_narrative as an  # noqa: E402


@pytest.fixture
def style(monkeypatch):
    """注入 `app.style.layers` / `app.style.narrative` 的同形状替身。

    注意：替身里**故意不提供** `RULES_PER_DIM` —— 2026-11 的配额重构之后
    真实模块已经没有这个常量，审计必须跟着走 `RENDER_PRESETS`。早先这条
    用例替身里塞了 `RULES_PER_DIM`，于是端点 500 了单测还是绿的。
    """
    state = {"rules": {}, "plot_bearing": {"语气", "节奏"}}
    layers = types.ModuleType("app.style.layers")
    layers.method_rules = lambda profile: state["rules"]
    narrative = types.ModuleType("app.style.narrative")
    narrative._PLOT_BEARING_DIMS = state["plot_bearing"]
    narrative.RENDER_PRESETS = {
        # 原创：默认每维 2 条，「视角」单独放宽到 5
        "original": types.SimpleNamespace(per_dim=2, dim_limits={"视角": 5}),
        # 续写：默认每维 3 条，没有维度级覆盖
        "continuation": types.SimpleNamespace(per_dim=3, dim_limits={}),
    }
    monkeypatch.setitem(sys.modules, "app.style.layers", layers)
    monkeypatch.setitem(sys.modules, "app.style.narrative", narrative)
    return state


def _rules(**dims):
    """`{维度: [规则正文或 {rule:…}]}` → method_rules 的返回形状。"""
    return {dim: [{"rule": r} for r in rules] for dim, rules in dims.items()}


def test_missing_profile_is_informational_only():
    issues, stats = an.run_narrative_audit(None)
    assert [i["rule"] for i in issues] == ["narrative_missing"]
    assert issues[0]["severity"] == "info"
    assert stats == {}


def test_evidence_suffix_is_stripped_before_length_check(style):
    assert an.strip_evidence("用动作代替情绪（证据：G1P001 他攥紧手）") == "用动作代替情绪"


def test_short_rules_only_warn_until_they_pass_the_error_line(style):
    long_rule = "用具体的动作与器物细节替代抽象的情绪形容词，避免直接说出感受。"
    style["rules"] = _rules(视角=[long_rule] * 13, 节奏=["短"])   # 1/14 ≈ 7%
    issues, stats = an.rule_narrative_too_short({})
    assert issues == [] and stats["total"] == 14

    style["rules"] = _rules(视角=[long_rule] * 6, 节奏=["短"] * 4)  # 40% → error
    issues, stats = an.rule_narrative_too_short({})
    assert stats["too_short"] == 4 and stats["too_short_pct"] == 40.0
    assert issues[0]["rule"] == "narrative_rules_too_short"
    assert issues[0]["severity"] == "error"

    style["rules"] = _rules(视角=[long_rule] * 9, 节奏=["短"])      # 10% → 不报
    assert an.rule_narrative_too_short({})[0] == []

    style["rules"] = _rules(视角=[long_rule] * 4, 节奏=["短"] * 2)  # 33% → error（>30）
    issues, _ = an.rule_narrative_too_short({})
    assert issues[0]["severity"] == "error"


def test_dimension_bloat_counts_usage_from_the_live_preset(style):
    style["rules"] = _rules(视角=["规则正文足够长可以执行的内容在这里"] * 101,
                            节奏=["规则正文足够长可以执行的内容在这里"] * 100)
    issues, stats = an.rule_dimension_bloat({})
    assert stats["bloated_dims"] == {"视角": 101}
    assert issues[0]["severity"] == "info"
    assert "只用前 5 条" in issues[0]["title"]
    # 维度级覆盖优先；没覆盖的维度取各模式 per_dim 的最大值（保守口径）
    assert an.dim_usage("视角") == 5
    assert an.dim_usage("节奏") == 3
    assert an.dim_usage() == 3


def test_plot_bearing_dimensions_are_exempt_from_leak_counting(style):
    # 泄漏全在剧情型维度里（语气/节奏）→ 不算问题
    style["rules"] = _rules(语气=["他走到门口说道，声音低了下来"],
                            节奏=["他说完就转身离开"])
    issues, _ = an.rule_narrative_plot_leak({})
    assert issues == []

    # 非剧情维度出现叙事成分，且比例 > 15% → warn
    style["rules"] = _rules(视角=["他走到门口说道"],
                            对白=["他说完就转身离开"],
                            节奏=["句子以短句起，逐步放长，在转折前收住"])
    issues, stats = an.rule_narrative_plot_leak({})
    assert stats["视角"] == {"total": 1, "narrative_leak": 1}
    assert "语气" not in stats
    assert issues[0]["rule"] == "narrative_method_dim_plot_leak"
    assert issues[0]["severity"] == "warn"


def test_empty_dimensions_names_the_missing_ones(style):
    style["rules"] = _rules(视角=["规则正文足够长可以执行的内容在这里"])
    issues, stats = an.rule_empty_dimensions({}, ["视角", "对白", "节奏"])
    assert stats == {"checked": 3, "empty": ["对白", "节奏"]}
    assert issues[0]["rule"] == "narrative_dimension_empty"
    assert "对白、节奏" in issues[0]["title"]


def test_full_audit_aggregates_all_rules_and_stats(style):
    style["rules"] = _rules(视角=["短"], 对白=[])
    issues, stats = an.run_narrative_audit({"version": 1}, expected_dims=["视角", "对白"])
    rules = {i["rule"] for i in issues}
    assert "narrative_rules_too_short" in rules
    assert "narrative_dimension_empty" in rules
    assert set(stats) == {"rule_narrative_too_short", "rule_dimension_bloat",
                          "rule_narrative_plot_leak", "empty_dims"}
