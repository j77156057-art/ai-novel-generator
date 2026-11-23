"""白名单外角色诊断：只报「说话人归属」位置的名字，不再报动作碎片。

背景（2026-11-23）：旧版扫全篇 2-4 字中文串，第 4 章报了 566 条假阳性
（「被照得睡」「看了一眼」），告警完全不可读。
"""
from __future__ import annotations

from app.generation.guard import detect_unlisted_characters


def test_action_fragments_are_not_reported():
    text = (
        '许照道：“又停电了。”\n'
        "被照得睡不安稳，他看了一眼窗外，头发垂在枕边。\n"
        "苏见月已走到窗边，锁芯没有响，群聊里多了几条消息。"
    )
    assert detect_unlisted_characters(text, ["许照"]) == []


def test_unlisted_speaker_is_reported():
    assert detect_unlisted_characters('陈默道：“先交钱。”', ["许照"]) == ["陈默"]


def test_quoted_then_attribution_is_reported():
    result = detect_unlisted_characters('“来了。”苏见月说。', ["许照"])
    assert "苏见月" in result


def test_allowed_speaker_is_filtered():
    assert detect_unlisted_characters('许照说：“嗯。”', ["许照"]) == []
