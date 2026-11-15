"""情节点切分与判定口径的纯函数测试（不碰数据库、不跑模型）。"""
from __future__ import annotations

from app.generation.structure_check import _beats, check_structure


# ─── 三级切分 ────────────────────────────────────────────────────────────


def test_level1_splits_on_semicolon():
    beats, level = _beats(
        "第1章《测试》：林川打开了旧铁门；他数了数箱子里剩下的铜牌；"
        "苏禾在门口等他一起走。"
    )
    assert level == 1
    assert beats == [
        "林川打开了旧铁门",
        "他数了数箱子里剩下的铜牌",
        "苏禾在门口等他一起走",
    ]


def test_level2_splits_on_period_when_semicolon_not_enough():
    beats, level = _beats(
        "第2章《测试》：林川打开了旧铁门。他数了数箱子里剩下的铜牌。"
        "苏禾在门口等他一起走。"
    )
    assert level == 2
    assert len(beats) == 3


def test_level3_splits_on_comma_plus_progression_word():
    beats, level = _beats(
        "第3章《测试》：他推开门走进屋子，随后把箱子放在地上，接着数了数铜牌。"
    )
    assert level == 3
    assert beats == [
        "他推开门走进屋子",
        "随后把箱子放在地上",
        "接着数了数铜牌",
    ]


def test_short_clauses_and_duplicates_are_dropped():
    beats, _ = _beats(
        "第4章《测试》：他走了；林川打开了旧铁门；林川打开了旧铁门。"
    )
    assert beats == ["林川打开了旧铁门"]


def test_split_stops_at_min_units():
    """够数就停：已经切出 3 条时不再继续往更细一级切。"""
    beats, level = _beats(
        "第5章《测试》：甲打开了旧铁门；乙数了数铜牌。"
        "丙在门口等他们，随后一起走进屋子，接着关上门。"
    )
    assert level == 2
    assert beats == [
        "甲打开了旧铁门",
        "乙数了数铜牌",
        "丙在门口等他们，随后一起走进屋子，接着关上门",
    ]


# ─── 判定输出 ────────────────────────────────────────────────────────────


def _outline(entry: str) -> list[str]:
    return [entry]


def test_insufficient_switches_basis_to_fallback():
    """条数不足时单元覆盖率不判，改用兜底分母（与 beat_missing 同权）。"""
    entry = "第1章《测试》：林川在旧铁门那里等苏禾。"
    text = "林川在旧铁门那里等苏禾。"
    result = check_structure(
        text, outline=_outline(entry), chapter=1, mode="report")
    beat = result["beat"]
    assert beat["units"] == 1
    assert beat["discrimination"] == "insufficient"
    assert beat["basis"] == "fallback_term_coverage"
    assert beat["counted"] is True
    # 1/1 恒 1.00：原始单元覆盖率照旧不给，避免误读成判据值
    assert beat["coverage"] is None
    assert beat["missing"] == []
    assert "beat_missing" not in result["blocking_reasons"]
    # 兜底分母（本章大纲独有词三元组）自本次起是正式判据
    fallback = beat["fallback"]
    assert fallback["status"] == "checked"
    assert fallback["source"] == "outline_unique_trigrams"
    assert fallback["basis"] == "fallback_term_coverage"
    assert fallback["counted"] is True
    assert fallback["min_coverage"] == beat["min_coverage"]
    # 正文照抄大纲正文 → 只差标题「测试」这一个二元组：8 项命中 7 项
    assert fallback["coverage"] == 0.875
    assert fallback["passed"] is True
    assert result["blocking"] is False
    assert result["enforced"] is False


def test_insufficient_fallback_miss_enters_blocking_reasons():
    """兜底覆盖率不达标 → beat_fallback_low 进 blocking_reasons，report 仍不拦。"""
    entry = "第1章《测试》：林川在旧铁门那里等苏禾。"
    text = "他推开门，把灯拉亮，什么也没说。"
    result = check_structure(
        text, outline=_outline(entry), chapter=1, mode="report")
    beat = result["beat"]
    assert beat["basis"] == "fallback_term_coverage"
    fallback = beat["fallback"]
    assert fallback["coverage"] < fallback["min_coverage"]
    assert fallback["passed"] is False
    assert "beat_fallback_low" in result["blocking_reasons"]
    assert result["blocking"] is True
    assert result["enforced"] is False


def test_sufficient_computes_coverage_and_flags_missing():
    entry = ("第1章《测试》：林川打开了旧铁门；他数了数箱子里剩下的铜牌；"
             "苏禾在门口等他一起走。")
    text = "林川打开了旧铁门。苏禾在门口等他一起走。"
    result = check_structure(
        text, outline=_outline(entry), chapter=1, mode="report")
    beat = result["beat"]
    assert beat["units"] == 3
    assert beat["discrimination"] == "sufficient"
    assert beat["counted"] is True
    assert beat["covered"] == 2
    assert beat["coverage"] == 0.667
    assert beat["missing"] == ["他数了数箱子里剩下的铜牌"]
    assert "fallback" not in beat
    # 只判不拦：报告模式标记 blocking，但不 enforced
    assert "beat_missing" in result["blocking_reasons"]
    assert result["blocking"] is True
    assert result["enforced"] is False


def test_min_units_is_configurable():
    entry = "第1章《测试》：林川打开了旧铁门。"
    text = "林川打开了旧铁门。"
    result = check_structure(
        text, outline=_outline(entry), chapter=1, beat_min_units=1, mode="report")
    beat = result["beat"]
    assert beat["discrimination"] == "sufficient"
    assert beat["counted"] is True
    assert beat["coverage"] == 1.0
