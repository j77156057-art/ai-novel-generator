"""形态校正的回归测试。

要守住的：
1. 纯台词行判定准：只有台词的段算裸，带动作/神态的不算；
2. 测量口径与档案一致（逐单元 vs 整段汇总，两个数差很远，别混用）；
3. 形态样例：目标低 → 挑「叙述+台词同段」的；目标高 → 挑「纯台词」的；
4. 判据：字数变太多 / 对白内容丢 / 形态没靠近 / 过冲 → 一律拒绝，保留原文；
5. 形态真的靠近了才采纳。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.style.form import (  # noqa: E402
    _content_lost,
    _dialogue_contents,
    correct_form,
    form_target,
    measure_bare_ratio,
    measure_form,
    pick_form_examples,
)

BARE = "“都退后。”"
LEAD = "陆崇沉声道，声音压得很低：“都退后。”"
NARR = "院子里静了下来，檐下的灯笼被风吹得转了半圈。"


class _Provider:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def chat(self, system, user, **kw):
        self.calls += 1
        return self.reply


PROFILE = {
    "version": 2,
    "buckets": {
        "medium": {
            "unit_chars": 3000,
            "feat": {
                "纯台词段占比": {"mean": 0.16, "std": 0.15, "p10": 0.0, "p90": 0.33},
            },
        },
    },
}


def test_bare_detection():
    assert measure_bare_ratio([BARE]) == 1.0
    assert measure_bare_ratio([LEAD]) == 0.0
    assert measure_bare_ratio([NARR]) == 0.0        # 没有引号的段不参与
    assert measure_bare_ratio([BARE, LEAD]) == 0.5


def test_measure_form_reports_both_ratios():
    m = measure_form("\n".join([BARE, LEAD, NARR]))
    assert m["quote_paras"] == 2
    assert m["bare_paras"] == 1
    assert m["bare_ratio"] == 0.5
    assert "unit_ratio" in m


def test_form_target_from_profile():
    t = form_target(PROFILE)
    assert t["target"] == 0.16
    assert t["p10"] == 0.0 and t["p90"] == 0.33
    # 没有这项 → 不校正
    assert form_target({}) is None
    assert form_target({"buckets": {"medium": {"feat": {}}}}) is None


def test_examples_prefer_target_form():
    paras = [BARE, LEAD, NARR, "“别动。”"]
    # 目标低（0.16）→ 优先挑带叙述尾巴的
    low = pick_form_examples(paras, 0.16)
    assert LEAD in low
    # 目标高（0.85）→ 优先挑纯台词的
    high = pick_form_examples(paras, 0.85)
    assert BARE in high or "“别动。”" in high


def test_content_lost_detects_dropped_dialogue():
    before = _dialogue_contents("“都退后。”\n“你过来。”")
    after = _dialogue_contents("“都退后。”")
    assert _content_lost(before, after) == 0.5
    assert _content_lost(before, before) == 0.0


def test_reject_when_form_does_not_move():
    """形态没动 → 拒绝，保留原文。"""
    text = "\n".join([BARE] * 40)
    prov = _Provider("\n".join([BARE] * 40))
    out, info = correct_form(text, prov, target_ratio=0.16, examples=[LEAD])
    assert info["applied"] is False, info
    assert out == text
    assert prov.calls == 1


def test_reject_when_length_changes_too_much():
    """纯灌水（对白一个字没动，只是把叙述写长）→ 拒绝。"""
    text = _pad([BARE] * 40)
    flooded = _pad([BARE] * 40, filler=NARR + "陆长风没有再说话，只是把手里的木匣往袖中又塞深了一寸，腕子上的铜钱磕在匣壁上，发出极轻的一声脆响，屋里没有人接话。", n=400)
    out, info = correct_form(text, _Provider(flooded), target_ratio=0.16, examples=[LEAD])
    assert info["applied"] is False, info
    assert out == text


def test_reject_when_new_dialogue_invented():
    """凭空多出新对白（改写扩写的那一头）→ 拒绝。"""
    text = _pad([BARE] * 40)
    invented = _pad([BARE] * 40 + ["“你们都退下。”"] * 20)
    out, info = correct_form(text, _Provider(invented), target_ratio=0.16, examples=[LEAD])
    assert info["applied"] is False, info
    assert "对白" in info["reason"], info
    assert out == text


def test_reject_when_overshooting():
    """过冲（矫枉过正）→ 拒绝。"""
    text = _pad([BARE] * 40)            # 纯台词 100%
    swung = _pad([LEAD] * 40)            # 全变成叙述+台词 → 过冲到 0%
    out, info = correct_form(text, _Provider(swung), target_ratio=0.16, examples=[LEAD])
    assert info["applied"] is False
    assert "过冲" in info["reason"], info


def test_accept_when_form_moves_toward_target():
    """形态向目标靠近且不过冲 → 采纳（对白内容逐条守恒）。"""
    text = _pad([BARE] * 40)            # 纯台词 100%
    fixed = _pad([LEAD] * 35 + [BARE] * 5)   # 纯台词 100% → 12.5%
    out, info = correct_form(text, _Provider(fixed), target_ratio=0.16, examples=[LEAD])
    assert info["applied"] is True, info
    assert info["before"]["bare_ratio"] > info["after"]["bare_ratio"]
    assert out == fixed


def _pad(lines: list[str], filler: str = NARR, n: int = 40) -> str:
    """给夹具补上等量的叙述段 —— 改写必然改长度，不补齐的话
    「字数变化过大」那道判据会先拦下来，测不到真正要测的那道。"""
    return "\n".join(lines + [filler] * n)

