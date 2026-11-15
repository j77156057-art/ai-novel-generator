# -*- coding: utf-8 -*-
"""v19 句长口径（hanzi-only.v2）与「句长形状」判据。

三条要锁住的事：

1. `split_sentences` 会把 `……` 与收尾引号切成无汉字片段，句长统计必须过滤；
2. 阈值与口径同批重算——`limits_from_archive` 的短句下限不能再是 0；
3. `_sentence_shape_check` 只判不拦：有 `mode="report"`，且不改 passed/violations。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.style import consistency, stylometry  # noqa: E402


def test_sentence_lens_drops_punctuation_fragments():
    text = "他说：“好。”……然后走了。"
    # split_sentences 会切出 `”…` 与 `…` 两个无汉字片段
    assert len(stylometry.split_sentences(text)) == 4
    assert stylometry.sentence_lens(text) == [6, 5]
    assert stylometry.SENTENCE_METRIC_SCOPE == "hanzi-only.v2"


def test_measure_short_ratio_ignores_fragments():
    text = "他说：“好。”……然后走了。今天天气不错，我们出去走走吧。"
    got = consistency.measure(text)
    lens = stylometry.sentence_lens(text)
    assert got["sentence_scope"] == "hanzi-only.v2"
    # 3 句（6 / 5 / 15 字），≤4 字为 0；若不过滤，分母会被 2 个碎片抬高
    assert len(lens) == 3
    assert got["core"]["短句占比"] == 0.0
    assert abs(got["core"]["句长均值"] - sum(lens) / len(lens)) < 1e-9


def test_limits_from_archive_short_floor_is_first_positive():
    limits = consistency.GEN_GATE_LIMITS
    row = consistency.ARCHIVE_BLOCK_PERCENTILES["短句占比"]
    # v20 短句改 ≤4 后块级 p10 / p25 都是 0，下限必须继续退到第一个非零分位
    assert row["p10"] == 0 and row["p25"] == 0
    assert limits["短句占比"]["min"] == row["p50"]
    assert limits["短句占比"]["min"] > 0
    assert limits["短句占比"]["max"] == row["p90"]


def test_avg_sentences_per_para_uses_same_ruler():
    """v20：`段均句数` 必须和句长类同一把尺，否则段长三角校核算不通。"""
    # 一个自然段含 1 句叙述 + 1 句对白；`……` 与收尾引号不得再算成句子
    text = "他推门进来，屋里没人。\n“人呢？”\n……\n"
    got = consistency.measure(text)
    paras = stylometry.paragraphs(text)
    assert [len(stylometry.sentence_lens(stylometry.strip_edge_quotes(p))) for p in paras] == [1, 1, 0]
    # 三个自然段共 2 句含汉字句 → 段均 ≈ 0.667；旧口径会把 `……` 数成 2 句
    assert abs(got["core"]["段均句数"] - 2 / 3) < 1e-9


def _repeat_to_gate(unit: str) -> str:
    """把测试片段重复到闸门最小字数（按非空白字符计），比例不变。"""
    while len("".join(unit.split())) < consistency.GEN_GATE_MIN_CHARS:
        unit = unit + unit
    return unit


def test_short_sentence_threshold_is_four():
    """v20：短句字长统一为 ≤4（原 generation_gate 是 ≤5）。"""
    # 句长含句末标点：「他走了吗？」= 5 字，不算短句；「走吧。」= 3 字，算
    text = _repeat_to_gate("他走了吗？走吧。")
    gate = consistency.generation_gate(text)
    metrics = gate["metrics"]
    assert consistency.SHORT_SENTENCE_MAX_LEN == 4
    assert abs(metrics["短句占比"] - 0.5) < 1e-9
    assert abs(consistency.measure(text)["core"]["短句占比"] - 0.5) < 1e-9


def test_single_sentence_para_ignores_punctuation_only():
    """v20：纯标点段（“……”）得到 0 句，不算叙述单句段；独立对白段归对白。"""
    text = _repeat_to_gate("……\n他推门进来，屋里没人。\n“人呢？”\n")
    gate = consistency.generation_gate(text)
    # 每组 3 段：纯标点段不计数、叙述段算 1、独立对白段归「对白单句段」
    # metrics 保留 3 位小数
    assert gate["metrics"]["叙述单句段占比"] == 0.333


def test_sentence_shape_check_is_report_only():
    from app.generation import engine

    meta = {
        "sentence_shape": {
            "band": {"p25": 9.0, "p75": 29.0},
            "crowd_max": 0.596,
            "long_ratio_floor": 0.066,
            "source": "archive_pooled",
            "chapters": 500,
        }
    }
    text = "。".join(["这是一句长度正好落在窄带里面的普通叙述句子"] * 8) + "。"
    got = engine._sentence_shape_check(
        text, {"长句占比": 0.0}, meta)
    assert got["mode"] == "report"
    assert got["scope"] == "hanzi-only.v2"
    assert got["crowd"]["exceeded"] is True
    assert got["long_ratio"]["below"] is True
    # 只判不拦：没有 passed / violations 这类能改变结局的字段
    assert "passed" not in got and "violations" not in got


def test_sentence_shape_check_without_reference():
    from app.generation import engine

    got = engine._sentence_shape_check("短。这句稍长一点点。", {"长句占比": 0.1}, {})
    assert got["source"] == "no_reference"
    assert got["crowd"]["value"] is None
    assert got["crowd"]["exceeded"] is None
    assert got["long_ratio"]["below"] is None
