"""量级 15 层表 + 三档体系校验的回归测试。

关键边界：
- `scale_level()` 行为不变（15 个细层标签都带五档前缀）；
- `scale_seq()` 只认显式细层/序号，只写五档名或境界名一律 None（不推定）；
- 细分型：倒退 + 单章跳级 + 同段跨阶对撞；
- 序列型：只做同体系相对高低（倒退），不判跳级/对撞；
- 粗放型：不做任何数值比较。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.generation.power_prompt import build_power_constraint
from app.generation.power_scale import (
    SCALE_FIELDS,
    detailed_scale,
    normalize_scale,
    scale_level,
    scale_seq,
)
from app.generation.power_system_type import (
    normalize_system_type,
    system_type_from_bible,
)
from app.kg.sentinel import E_REALM_UP, Event, check_segment

_REALM = "realm"


def _state(order: int = 2, segment_no: int = 1, scale: str = "修仙") -> dict:
    return {
        (_REALM, "甲"): {
            "value": "炼气", "order": order, "scale": scale,
            "segment_no": segment_no, "span": [0, 2], "text": "炼气",
        }
    }


def _up(entity: str, order: int, segment_no: int, value: str, scale: str = "修仙",
        start: int = 10) -> Event:
    return Event(
        E_REALM_UP, entity, segment_no, (start, start + 2), value,
        value=value, order=order, scale=scale,
    )


def test_five_level_behavior_unchanged():
    # scale_level 返回序数（0..4），不是标签；15 层标签带五档前缀，故仍返回 2。
    assert scale_level("毁物级·摧墙") == 2
    assert scale_level("毁物级") == 2
    assert normalize_scale({"量级": "毁物级·摧墙"})["量级"] == "毁物级·摧墙"


def test_additive_fields_do_not_change_scale_level():
    assert "量级细层" in SCALE_FIELDS and "量级序号" in SCALE_FIELDS
    card = {"量级": "毁物级·摧墙", "量级细层": "毁物级·摧墙", "量级序号": 8}
    norm = normalize_scale(card)
    # normalize_scale 既有行为是把所有字段收敛成文本；新增字段不改变这一点。
    assert norm["量级序号"] == "8"
    assert scale_level(norm) == 2


def test_scale_seq_only_accepts_explicit_evidence():
    assert detailed_scale("毁物级·摧墙") == "毁物级·摧墙"
    assert scale_seq("毁物级·摧墙") == 8
    assert scale_seq(8) == 8
    # 只写五档名 / 只写境界名：没有细层证据 ⇒ unknown
    assert detailed_scale("毁物级") is None
    assert scale_seq("毁物级") is None
    assert scale_seq("九品") is None


def test_system_type_default_is_neutral_and_explicit_is_honored():
    assert system_type_from_bible({}) == ("序列型", "default")
    assert system_type_from_bible({"体系类型": "细分型"}) == (
        "细分型", "plot_bible.体系类型"
    )
    assert system_type_from_bible({"systems": [{"类型": "粗放型"}]}) == (
        "粗放型", "plot_bible.systems"
    )
    assert normalize_system_type("乱七八糟") == "序列型"


def test_subtle_system_checks_regression_jump_and_collision():
    state = _state(order=2, segment_no=1)

    jump = _up("甲", 5, segment_no=2, value="筑基")
    assert [f.rule for f in check_segment(state, [jump], system_type="细分型")] == [
        "realm_jump"
    ]

    regress = _up("甲", 1, segment_no=3, value="凡手")
    assert [f.rule for f in check_segment(state, [regress], system_type="细分型")] == [
        "realm_regression"
    ]

    first = _up("乙", 6, segment_no=2, value="金丹", start=0)
    second = _up("乙", 2, segment_no=2, value="炼气", start=20)
    rules = [f.rule for f in check_segment({}, [first, second], system_type="细分型")]
    assert "realm_collision" in rules


def test_sequence_system_only_relative_high_low():
    state = _state(order=2, segment_no=1)
    jump = _up("甲", 5, segment_no=2, value="筑基")
    assert check_segment(state, [jump], system_type="序列型") == []

    regress = _up("甲", 1, segment_no=3, value="凡手")
    assert [f.rule for f in check_segment(state, [regress], system_type="序列型")] == [
        "realm_regression"
    ]


def test_coarse_system_does_no_numeric_comparison():
    state = _state(order=2, segment_no=1)
    regress = _up("甲", 1, segment_no=2, value="凡手")
    assert check_segment(state, [regress], system_type="粗放型") == []

    first = _up("乙", 6, segment_no=2, value="圣人", start=0)
    second = _up("乙", 2, segment_no=2, value="凡人", start=20)
    assert check_segment({}, [first, second], system_type="粗放型") == []


def test_power_prompt_marks_unknown_and_coarse_rule():
    project = SimpleNamespace(
        plot_bible={
            "system_type": "粗放型",
            "characters": [{"name": "甲", "境界": "圣人"}],
        }
    )
    block = build_power_constraint(project)
    assert "体系类型：粗放型" in block
    assert "不做数值比较" in block
    assert "量级 unknown" in block

    ladder_project = SimpleNamespace(
        plot_bible={"characters": [{"name": "乙", "境界": "九品"}]}
    )
    assert "十五层细层" in build_power_constraint(ladder_project)

    plain = SimpleNamespace(plot_bible={})
    assert "十五层细层" not in build_power_constraint(plain)
