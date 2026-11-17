# -*- coding: utf-8 -*-
"""结构检查的 skipped 必须带具体原因，不能退化成笼统的 skipped。"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.generation.structure_check import check_structure  # noqa: E402

CARDS = [{"name": "林川", "gender": "男"}]


def _codes(item: dict) -> tuple[str, str]:
    reason = item.get("reason") or {}
    return str(reason.get("code") or ""), str(reason.get("message") or "")


def test_segment_without_chapter_skips_with_reason_but_gender_still_runs():
    result = check_structure(
        "林川说：好。", outline=["第1章《测试》：林川打开门；他数了数铜牌；苏禾走进来。"],
        chapter=None, character_cards=CARDS, mode="report")
    for key in ("beat", "premature"):
        assert result[key]["status"] == "skipped"
        code, message = _codes(result[key])
        assert code == "no_chapter_label"
        assert "段N" in message and "章号" in message
    # 无章号只影响 beat/premature，性别一致性不需要章号
    assert result["gender"]["status"] == "checked"


def test_chapter_beyond_outline_names_the_missing_chapter():
    outline = [f"第{i}章《测试》：林川打开门；他数了数铜牌；苏禾走进来。"
               for i in range(1, 41)]
    result = check_structure("林川说：好。", outline=outline, chapter=44,
                             character_cards=CARDS, mode="report")
    for key in ("beat", "premature"):
        code, message = _codes(result[key])
        assert code == "chapter_beyond_outline"
        assert "第44章" in message and "40" in message
        assert "补" in message


def test_empty_outline_says_what_to_fill():
    for outline in ([], None):
        result = check_structure("林川说：好。", outline=outline, chapter=1,
                                 character_cards=CARDS, mode="report")
        for key in ("beat", "premature"):
            code, message = _codes(result[key])
            assert code == "outline_entry_empty"
            assert "章纲" in message and "补" in message


def test_blank_current_entry_skips_beat_only():
    outline = ["", "第2章《测试》：林川打开门；他数了数铜牌；苏禾走进来。"]
    result = check_structure("林川说：好。", outline=outline, chapter=1,
                             character_cards=CARDS, mode="report")
    code, message = _codes(result["beat"])
    assert code == "outline_entry_empty"
    assert "第1章" in message and "空" in message
    # 本章条目为空不影响「未来章」比对
    assert result["premature"]["status"] == "checked"


def test_title_only_entry_says_it_cannot_split_beats():
    outline = ["第1章《测试》：   ", "第2章《测试》：林川打开门；他数了数铜牌；苏禾走进来。"]
    result = check_structure("林川说：好。", outline=outline, chapter=1,
                             character_cards=CARDS, mode="report")
    code, message = _codes(result["beat"])
    assert code == "outline_entry_empty"
    assert "第1章" in message and "情节点" in message


def test_gender_without_cards_has_reason():
    result = check_structure(
        "林川说：好。", outline=["第1章《测试》：林川打开门；他数了数铜牌；苏禾走进来。"],
        chapter=1, character_cards=None, mode="report")
    code, message = _codes(result["gender"])
    assert code == "no_character_cards"
    assert "人物卡" in message


def test_empty_text_has_reason_for_all_three():
    result = check_structure("", outline=["第1章《测试》：林川打开门。；他数了数铜牌；苏禾走进来。"],
                             chapter=1, character_cards=CARDS, mode="report")
    for key in ("beat", "premature", "gender"):
        code, message = _codes(result[key])
        assert code == "no_text"
        assert message


def test_no_skipped_item_lacks_reason():
    outline = ["第1章《测试》：林川打开门；他数了数铜牌；苏禾走进来。"]
    cases = [
        check_structure("林川说：好。", outline=outline, chapter=None,
                        character_cards=CARDS),
        check_structure("林川说：好。", outline=outline, chapter=9,
                        character_cards=CARDS),
        check_structure("林川说：好。", outline=[], chapter=1,
                        character_cards=CARDS),
        check_structure("林川说：好。", outline=outline, chapter=1,
                        character_cards=None),
        check_structure("", outline=outline, chapter=1, character_cards=CARDS),
    ]
    for result in cases:
        for key in ("beat", "premature", "gender"):
            if result[key]["status"] == "skipped":
                code, message = _codes(result[key])
                assert code and message, (key, result[key])
