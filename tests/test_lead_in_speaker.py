"""判据 7：引导语的**说话人一致**（2026-10-27）。

## 为什么要有这条判据

引导语是贴在台词前面的动作叙述 —— 读者会把它的**主语**当成说话人。
主语点错人，读者就把这句话记到别人头上。这是这条路的已知代价
（README「引导语偶尔会指错说话人」）。

## 这份测试盯的是判据 7 的判定边界

**判得死 ⇒ 拒**（四种冲突）：
1. 声明「不明」却点了名；
2. 引导语开头点的主语 ≠ 声明的说话人；
3. 本段归属语点了唯一一个人，声明的说话人却是别人；
4. 说话人既不在上下文、也不在角色表里。

**判不准 ⇒ 放过**（误杀会堵死真活）：
- 模型没声明说话人；说话人写的是代词；
- 归属语点了 ≥2 个人（不知道谁在说）；
- 角色表拿不到（判据 3/4 自动失效）。

不依赖真实网络：provider 是假的。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.style.lead_in import insert_lead_ins  # noqa: E402

NARR = ("陆崇站在门口，手按在刀柄上，听见这句，眉头慢慢拧起，"
        "北荒王府里没几个人能让她这样说话。")
BARE = "“都退后。”"
#: 归属语点了名（引号外 3 字 ⇒ 仍是裸台词段）
ATTR = "“都退后。”陆崇道。"
ROSTER = {"陆崇", "陆迟"}


class _Provider:
    """按顺序返回预设回复。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system, user, constraints=None, temperature=None):
        self.prompts.append((system, user))
        i = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[i]


def _run(reply, *, paras=None, roster=ROSTER, proper_nouns=None):
    ps = paras if paras is not None else [NARR, BARE]
    provider = _Provider([reply])
    out, info = insert_lead_ins(
        ps, provider, target_ratio=0.0,
        character_names=roster, proper_nouns=proper_nouns,
    )
    return out, info


def _reason(info) -> str:
    return "；".join(r.get("reason", "") for r in info.get("rejected", []))


# ====================================================================
# 1. 拿不准却点了名 ⇒ 拒
# ====================================================================


def test_unknown_speaker_with_name_is_rejected():
    out, info = _run(
        "陆崇垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆崇\n说话人：不明",
        proper_nouns={"陆崇"},
    )
    assert info["inserted"] == 0, out
    assert "说话人不对" in _reason(info), info
    assert "拿不准" in _reason(info), info


def test_unknown_speaker_with_pronoun_is_accepted():
    """声明「不明」但用代词 ⇒ 没问题（这正是提示词要求的写法）。"""
    out, info = _run("那人垂下眼，指腹在刀柄上轻轻一磕。\n说话人：不明")
    assert info["inserted"] == 1, info
    assert out[1].startswith("那人垂下眼"), out


# ====================================================================
# 2. 引导语的主语必须就是声明的说话人
# ====================================================================


def test_lead_subject_other_than_speaker_is_rejected():
    out, info = _run(
        "陆迟垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆迟\n说话人：陆崇",
        proper_nouns={"陆崇", "陆迟"},
    )
    assert info["inserted"] == 0, out
    assert "引导语写的是 陆迟" in _reason(info), info


def test_lead_subject_same_as_speaker_is_accepted():
    out, info = _run(
        "陆崇垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆崇\n说话人：陆崇",
        proper_nouns={"陆崇"},
    )
    assert info["inserted"] == 1, info
    assert out[1].startswith("陆崇垂下眼"), out


def test_named_target_is_not_a_conflict():
    """引导语开头是代词、后面才出现别人的名字 ⇒ **不是**冲突。

    「对方抬眼看向陆崇」写的是陆崇**被看的人**，不是说话人 ——
    只看「引导语里出现了谁的名字」会误杀这种正常写法。
    """
    out, info = _run(
        "那人抬眼看向陆崇，神色平静。\n专名：陆崇\n说话人：陆迟",
        proper_nouns={"陆崇", "陆迟"},
    )
    assert info["inserted"] == 1, info


# ====================================================================
# 3. 本段归属语点了唯一一个人 ⇒ 那就是说话人
# ====================================================================


def test_attribution_speaker_mismatch_is_rejected():
    out, info = _run(
        "陆迟垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆迟\n说话人：陆迟",
        paras=[NARR, ATTR], proper_nouns={"陆崇", "陆迟"},
    )
    assert info["inserted"] == 0, out
    assert "本段明说 陆崇 在说" in _reason(info), info


def test_attribution_speaker_match_is_accepted():
    out, info = _run(
        "陆崇垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆崇\n说话人：陆崇",
        paras=[NARR, ATTR], proper_nouns={"陆崇"},
    )
    assert info["inserted"] == 1, info


def test_ambiguous_attribution_is_not_rejected():
    """归属语点了 ≥2 个人 ⇒ 不知道谁在说 ⇒ **不判**（放过）。

    ⚠️ 这条在真实文本里几乎不可能发生：裸台词段的引号外**只有 6 字以内**，
    两个名字塞不下。这是防御分支 —— 别让它在别的形状上误伤。
    """
    two = "“都退后。”陆崇陆迟道。"
    out, info = _run(
        "陆迟垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆迟\n说话人：陆迟",
        paras=[NARR, two], proper_nouns={"陆崇", "陆迟"},
    )
    assert info["inserted"] == 1, info


# ====================================================================
# 4. 说话人不在场
# ====================================================================


def test_speaker_not_in_scene_is_only_noted():
    """说话人不在角色表、也不在这一段里 ⇒ **只留痕，不拦**。

    「门内的人」这类泛称与模型编造的人名**分不出来** ⇒ 按纪律不拦。
    """
    out, info = _run(
        "赵青山垂下眼，指腹在刀柄上轻轻一磕。\n专名：赵青山\n说话人：赵青山",
        proper_nouns={"陆崇", "赵青山"},
    )
    assert info["inserted"] == 1, out
    assert info.get("notes"), "应当留痕"
    assert "不在角色表里" in info["notes"][0]["note"], info["notes"]


def test_empty_roster_disables_speaker_judgement():
    """角色表拿不到 ⇒ 三条判据全失效（只留痕都不留）。"""
    out, info = _run(
        "赵青山垂下眼，指腹在刀柄上轻轻一磕。\n专名：赵青山\n说话人：赵青山",
        roster=set(), proper_nouns={"陆崇", "赵青山"},
    )
    assert info["inserted"] == 1, out
    assert not info.get("notes"), info


def test_pronoun_subject_with_named_target_is_not_rejected():
    """主语是代词、宾语里带人名 ⇒ **不是**冲突（第一版误杀过这一类）。"""
    out, info = _run(
        "他抬眼看了沈砚一眼，神色平静。\n专名：沈砚\n说话人：不明",
        roster={"沈砚", "陆崇"}, proper_nouns={"沈砚"},
    )
    assert info["inserted"] == 1, out


def test_object_name_is_not_treated_as_person():
    """模型把物件写进「专名」行 ⇒ 不能当成「点了人的名」。"""
    out, info = _run(
        "他垂下眼，目光落向木盒里面。\n专名：木盒\n说话人：不明",
        roster={"沈砚"}, proper_nouns={"沈砚", "木盒"},
    )
    assert info["inserted"] == 1, out


# ====================================================================
# 5. 判不准 ⇒ 放过
# ====================================================================


def test_missing_declaration_is_not_rejected():
    """模型没声明说话人（旧版输出格式）⇒ 不判。"""
    out, info = _run("陆崇垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆崇",
                     proper_nouns={"陆崇"})
    assert info["inserted"] == 1, info


def test_pronoun_speaker_is_not_rejected():
    out, info = _run("他垂下眼，指腹在刀柄上轻轻一磕。\n说话人：他")
    assert info["inserted"] == 1, info


# ====================================================================
# 6. 「说话人」那行绝不能进正文
# ====================================================================


def test_speaker_line_never_enters_the_text():
    out, info = _run(
        "陆崇垂下眼，指腹在刀柄上轻轻一磕。\n专名：陆崇\n说话人：陆崇",
        proper_nouns={"陆崇"},
    )
    assert info["inserted"] == 1, info
    assert "说话人" not in out[1], out
    assert "专名" not in out[1], out
    assert out[1].endswith("“都退后。”"), out
