# -*- coding: utf-8 -*-
"""复跑后三项兜底：对白下限、段数下限、声纹改写长度；附带去单一化保数。"""
from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
from app.models import Project  # noqa: E402


def _session():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    return eng, sessionmaker(bind=eng, autoflush=False)()


class _P:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def variant(self, **_kwargs):
        return self

    def chat(self, _system, _user, **_kwargs):
        self.calls += 1
        return self.reply


# ── 段数下限 ─────────────────────────────────────────────────────────────


def test_ensure_min_paragraphs_splits_without_changing_text():
    from app.generation.paragraph_floor import ensure_min_paragraphs

    p1 = "他走到车库门口，卷帘门拉起来一半，里头的灯是黄的。箱子搁在地上，底下有一圈水渍。他蹲下去看了看，又站起来。"
    p2 = "钱多多背对着他，把箱子往车上推。推了两下没推动，鞋底在水泥地上蹭。许照没有出声，站在门框边上等着。"
    text = p1 + "\n" + p2
    got, info = ensure_min_paragraphs(text, 4)
    assert info["applied"] is True
    assert info["after"] >= 4
    # 只加换行，不改任何字
    assert "".join(got.split("\n")) == "".join(text.split("\n"))


def test_ensure_min_paragraphs_noop_when_met():
    from app.generation.paragraph_floor import ensure_min_paragraphs

    text = "一段。\n两段。"
    got, info = ensure_min_paragraphs(text, 2)
    assert got == text
    assert info["reason"] == "already_met"


# ── 对白下限 ─────────────────────────────────────────────────────────────


def test_dialogue_floor_rewrite_accepts_enough_dialogue():
    from app.generation.engine import (
        _dialogue_floor_rewrite_once,
        _dialogue_ratio_of,
    )

    p1 = ("许照站在车库门口，看着钱多多把箱子搬上三轮车。箱子底下垫着一块木板，"
          "板子上结了一层白霜，正往下滴水，水渍在水泥地上漫开一圈。")
    p2 = ("钱多多回头看了他一眼，脸上先是一笑，然后把手在裤子上蹭了蹭。"
          "巷子里没有人，路灯照着地上的水渍，声控灯在头顶闪了两下。")
    text = p1 + "\n" + p2
    fixed = (
        "【1】许照问：“这箱子怎么渗水？”钱多多说：“路上颠的，冰袋化了。”"
        "许照又问：“见光的另算？”钱多多笑了笑：“小的做的是辛苦钱，不寒碜。”\n"
        "【2】钱多多回头看了他一眼，脸上先是一笑，然后把手在裤子上蹭了蹭。"
        "巷子里没有人，路灯照着地上的水渍，声控灯在头顶闪了两下。"
    )
    got, info = _dialogue_floor_rewrite_once(_P(fixed), text, 0.2, 4000)
    assert info["applied"] is True, info
    assert _dialogue_ratio_of(got) >= 0.2
    assert len(got) >= len(text) * 0.85 and len(got) <= len(text) * 1.15


def test_dialogue_floor_compresses_when_paragraph_rewrite_overshoots():
    from app.generation.engine import (
        _dialogue_floor_rewrite_once,
        _dialogue_ratio_of,
    )

    p1 = ("许照站在车库门口，看着钱多多把箱子搬上三轮车。箱子底下垫着一块木板，"
          "板子上结了一层白霜，正往下滴水，水渍在水泥地上漫开一圈。")
    p2 = ("钱多多回头看了他一眼，脸上先是笑，然后把手在裤子上蹭了蹭。"
          "巷子里没有人，路灯照着地上的水渍，声控灯在头顶闪了两下。")
    text = p1 + "\n" + p2
    # 模型逐段加了很多对白，整章被顶过字数上限
    oversize = (
        "【1】许照问：“这箱子怎么渗水？”钱多多说：“路上颠的，冰袋化了。”"
        "许照又问：“见光的另算？”钱多多笑了笑：“小的做的是辛苦钱，不寒碜。”"
        "两个人又为加五十还是加一百五争了几句。\n"
        "【2】钱多多回头看了他一眼，脸上先是笑，然后把手在裤子上蹭了蹭。"
        "巷子里没有人，路灯照着地上的水渍，声控灯在头顶闪了两下。"
    )
    compact = (
        "许照站在车库门口，问：“这箱子怎么渗水？”钱多多说：“路上颠的，冰袋化了。”"
        "许照又问：“见光的另算？”钱多多笑了笑：“小的做的是辛苦钱。”"
        "巷子里没有人，路灯照着地上的水渍，声控灯在头顶闪了两下。"
        "他把手在裤子上蹭了蹭，没有再接话，低头去看那块渗水的木板。"
    )
    with patch("app.generation.engine._compression_round",
               return_value=(compact, {"accepted": True})):
        got, info = _dialogue_floor_rewrite_once(_P(oversize), text, 0.2, 4000)
    assert info["applied"] is True, info
    assert info["compressed"] is True
    assert _dialogue_ratio_of(got) >= 0.2
    assert len(got) <= min(4800, int(len(text) * 1.15))


def test_dialogue_ratio_uses_same_denominator_as_gate():
    from app.generation.engine import _dialogue_ratio_of
    from app.style.consistency import generation_gate

    text = ("他说：“走吧。”她没动，手还搭在门框上。“再等等？”他问。"
            "她摇头：“不等了。”楼道里的灯灭了又亮，谁也没再开口。\n") * 25
    # 对白字占比平时不进默认判罚带，这里显式注入，取闸门自己算的值做对照。
    gate = generation_gate(
        text, 4000, gate_limits={"对白字占比": {"min": 0.1}})
    metric = (gate.get("metrics") or {})["对白字占比"]
    assert round(_dialogue_ratio_of(text), 3) == metric


def test_dialogue_floor_keeps_original_when_still_low():
    from app.generation.engine import _dialogue_floor_rewrite_once

    text = "许照站在门口。他没有说话。" * 6
    got, info = _dialogue_floor_rewrite_once(_P("还是一段没有对白的叙述。" * 5),
                                             text, 0.2, 4000)
    assert info["applied"] is False
    assert got == text


# ── 声纹改写：超长时压缩兜底 ─────────────────────────────────────────────


def test_voice_rewrite_compresses_oversize_output():
    from app.generation.engine import _dialogue_voice_check, _voice_rewrite_once

    eng, s = _session()
    p = Project(title="voice-compress", plot_bible={"characters": [{
        "name": "许照",
        "card": {"声纹": {"自称": "我", "口头禅": ["得嘞"]}},
    }]})
    s.add(p)
    s.commit()

    original = "许照站在门口，没开口。许照换了个手，还是没说话。"
    check = _dialogue_voice_check(p, original)
    assert check["weak"] == ["许照"]

    valid = "许照说：「我得嘞，水表我看过。」许照说：「这楼我熟。」"
    oversize = valid + "他站在门口，把钥匙串换了个手，又看了看楼道里的灯。"
    with patch("app.generation.engine._compression_round",
               return_value=(valid, {"accepted": True, "chars_after": len(valid)})):
        got, info = _voice_rewrite_once(
            s, p, _P(oversize), original, check, 4000, "第1章")
    assert info["applied"] is True, info
    assert info["compressed"] is True
    assert got == valid
    s.close()
    eng.dispose()


# ── 去单一化：只换写法，不删归属 ─────────────────────────────────────────


def test_variety_preserves_attribution_count():
    from app.style.attribution_fix import variety_preserves

    assert variety_preserves(25, 23) is True
    assert variety_preserves(25, 13) is False
    assert variety_preserves(0, 0) is True
