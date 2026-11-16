"""机械形态校正（只改段落边界）的回归测试。

要守住的：
1. **一个字都不新增**：并段只去掉段落边界，去掉全部空白后前后完全一致；
   —— **唯一的例外**是并接处的「。」改成「：」（对齐参考书的混合段形态），
   所以守恒判据升级成「**把冒号改回去**之后必须逐字相同」；
2. **只并「前面有叙述」的台词段**：连续对白（前面也是台词）不动；
3. **不越界**：已经低于目标就不动；并的段数按目标算；
4. 合并后**裸台词段确实变少**（形态真的被校正了，不是换个写法）；
5. 并接处产出**冒号形态**（参考书 2059 个混合段里只有 1 段用「。」+引号）。
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


def _reverted(info: dict) -> str:
    """把冒号修正改回去之后的正文（守恒判据要比的就是它）。"""
    return "\n".join(info["revert_paragraphs"])


def test_bare_detection():
    assert is_bare_paragraph(BARE)
    assert not is_bare_paragraph(NARR)
    assert not is_bare_paragraph("院子里静了下来，檐下的灯笼转了半圈。")


def test_merge_keeps_content_byte_for_byte():
    paras = [NARR, BARE, NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.1)
    assert info["merged"] == 2, info
    # 段落数少了（两个叙述被并进台词段）
    assert len(new_paras) == 2
    # 守恒：**把冒号改回去**之后，与原文逐字相同
    assert _strip_ws(_reverted(info)) == _strip_ws("\n".join(paras))


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
    """太短的叙述不并（并了会变成半截话）。

    门槛 = **指标自己的边界**（引号外字数 > 6）：「他点头。」引号外 3 字 ⇒ 不并。
    """
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


# ── 并接处的标点形态 ────────────────────────────────────────────────
def test_merge_uses_colon_form():
    """并接处是「叙述：台词」，不是「叙述。台词」。

    参考书前 40 万字 2059 个混合段里只有 1 段用「。」+引号，83.6% 是冒号形态。
    """
    paras = [NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["merged"] == 1, info
    assert info["colon_fixed"] == 1, info
    assert new_paras[0] == NARR[:-1] + "：" + BARE
    # 改回去之后与原文逐字相同
    assert _strip_ws(_reverted(info)) == _strip_ws("\n".join(paras))


def test_colon_form_can_be_switched_off():
    """关掉冒号修正 ⇒ 完全不动内容（老行为）。"""
    paras = [NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0, colon_form=False)
    assert info["colon_fixed"] == 0, info
    assert new_paras[0] == NARR + BARE
    assert _strip_ws("\n".join(new_paras)) == _strip_ws("\n".join(paras))


def test_colon_form_does_not_change_the_metric():
    """冒号修正是**形态**修正，不是指标修正 —— 裸台词占比必须一模一样。"""
    paras = [NARR, BARE] * 6
    _a, info_on = merge_bare_dialogue(paras, 0.3, colon_form=True)
    _b, info_off = merge_bare_dialogue(paras, 0.3, colon_form=False)
    assert info_on["after"] == info_off["after"], info_on


def test_colon_form_leaves_other_endings_alone():
    """叙述末尾不是「。」的（已经带「：」等）⇒ 不动它。"""
    paras = ["守卒把碗放下，碗底磕在木桌上：", BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["colon_fixed"] == 0, info
    assert new_paras[0] == "守卒把碗放下，碗底磕在木桌上：" + BARE


# ── 最大匹配：叙述在台词**后面**的形状 ──────────────────────────────
def test_narration_after_dialogue_is_merged_too():
    """`B N`（叙述在台词后面）也要并 —— 早先的实现完全看不到这种形状。

    文字顺序不变，只是并成一段：`“台词”叙述`。
    """
    paras = [BARE, NARR]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["merged"] == 1, info
    assert info.get("backward") == 1, info
    assert new_paras[0] == BARE + NARR, "向后并不能调换顺序"
    # 不产生冒号修正（这一处没有句号要改）
    assert info["colon_fixed"] == 0, info


def test_backward_merge_beats_greedy():
    """`B B N` 这种形状：贪心（只找前面那段叙述）救不回来，最大匹配能救 1 段。"""
    paras = [BARE, BARE, NARR]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["merged"] == 1, info
    assert info.get("backward") == 1, info
    assert len(new_paras) == 2
    assert new_paras[0] == BARE, "第一个裸台词段没有可并的叙述，应保持独立"


def test_mixed_paragraph_blocks_the_merge():
    """混合段是**隔断**：叙述不能跨过它去并后面的裸台词段。"""
    paras = [BARE, "他抬眼看向门外，沉声道：“走。”", NARR, BARE]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    # 只有最后一处能并（它前面就是那段叙述）
    assert info["merged"] == 1, info
    assert info.get("backward") == 0, info
    assert new_paras[-1] == NARR[:-1] + "：" + BARE


def test_direction_forward_keeps_old_behaviour():
    """`direction="forward"` = 早先的行为（只并到前面那段叙述）。"""
    paras = [BARE, NARR]
    new_paras, info = merge_bare_dialogue(paras, 0.0, direction="forward")
    assert info["merged"] == 0, info
    assert new_paras == paras


def test_backward_merge_never_reorders_the_text():
    """任何方向的并段都**不能调换文字顺序**。

    守恒判据（把冒号改回去 == 原文）能查出来，但这条把「顺序」单独钉住。
    """
    paras = [NARR, BARE, NARR, BARE, NARR]
    new_paras, info = merge_bare_dialogue(paras, 0.0)
    assert info["merged"] >= 2, info
    # 逐段把合并段拆回去，字符序列必须与原文完全一致
    assert _strip_ws("\n".join(info["revert_paragraphs"])) == _strip_ws("\n".join(paras))
