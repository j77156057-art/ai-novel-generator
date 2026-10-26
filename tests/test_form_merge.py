"""机械形态校正（只改段落边界）的回归测试。

要守住的：
1. **一个字都不新增**：并段只去掉段落边界，去掉全部空白后前后完全一致；
2. **只并「前面有叙述」的台词段**：连续对白（前面也是台词）不动；
3. **不越界**：已经低于目标就不动；并的段数按目标算；
4. 合并后**裸台词段确实变少**（形态真的被校正了，不是换个写法）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.style.form_merge import (  # noqa: E402
    is_bare_paragraph,
    merge_bare_dialogue,
)

BARE = "“都退后。”"
NARR = "陆崇站在门口，手按在刀柄上，听见这句，眉头慢慢拧起，北荒王府里没几个人能让她这样说话。"


def _strip_ws(t: str) -> str:
    return "".join(t.split())


def test_bare_detection():
    assert is_bare_paragraph(BARE)
    assert not is_bare_paragraph(NARR)
    assert not is_bare_paragraph("院子里静了下来，檐下的灯笼转了半圈。")


def test_merge_keeps_content_byte_for_byte():
    paras = [NARR, BARE, NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.1)
    assert info["merged"] == 2, info
    assert _strip_ws("\n".join(new_paras)) == _strip_ws("\n".join(paras))
    # 段落数少了（两个叙述被并进台词段）
    assert len(new_paras) == 2


def test_merge_moves_form():
    paras = [NARR, BARE, NARR, BARE, NARR, BARE, NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["before"] == 1.0
    assert info["after"] < info["before"], info
    assert all("“" in p for p in new_paras)


def test_no_merge_when_already_below_target():
    paras = [BARE, NARR, "他抬眼看向门外，沉声道：“走。”"]     # 2 段含引号，1 段裸 → 50%
    new_paras, info = merge_bare_dialogue(paras, 0.5)
    assert info["merged"] == 0
    assert "低于目标" in info["reason"], info
    assert new_paras == paras


def test_consecutive_dialogue_is_not_merged():
    """连续对白（前面也是台词）没有叙述可并 → 不动它，也不硬并。"""
    paras = [BARE, BARE, NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["merged"] == 1, info
    # 第一个 BARE 前面也是 BARE → 保持独立
    assert new_paras[0] == BARE


def test_short_narration_is_not_merged():
    """太短的叙述不并（并了会变成半截话）。"""
    paras = ["他点头。", BARE, NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["merged"] == 1, info
    assert new_paras[0] == "他点头。"


def test_merge_count_follows_target():
    """并多少段由目标比例决定（不是并到底）。"""
    paras = [NARR, BARE] * 6      # 6 段台词 + 6 段叙述 → 裸台词 100%
    _new, info = merge_bare_dialogue(paras, 0.5)
    # 12 段里 6 段含引号；目标 50% → 留 3 段裸台词 → 并 3 段
    assert info["merged"] == 3, info
    assert abs(info["after"] - 0.5) < 0.01, info
