# -*- coding: utf-8 -*-
"""自动返修：确定性修复、硬伤诊断、验收。"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.style.deterministic_repair import (  # noqa: E402
    deterministic_repair,
    find_dialogue_punctuation_issues,
    find_multi_speaker_paragraphs,
    find_number_word_mismatch,
    find_tics,
    repair_dialogue_punctuation,
    split_multi_speaker_paragraphs,
)
from app.style.repair import build_repair_plan  # noqa: E402
from app.style.repair_accept import evaluate_repair  # noqa: E402

LIMITS = {"极短纯对白段占比": {"max": 0.032}, "段数": {"min": 70.0}}


# ── 引语标点 ─────────────────────────────────────────────────────────────


def test_punctuation_added_after_speech_colon():
    text = (
        "许照把手机往膝盖上一拍，道：“谁搁的”，没人应。\n"
        "许照喊道：“有人吗”，没人应。\n"
        "许照把扳手往手心一拍，道：“行”，掰开电梯门。"
    )
    got, info = repair_dialogue_punctuation(text)
    assert "：“谁搁的？”没人应" in got
    assert "：“有人吗？”没人应" in got
    assert "：“行。”掰开电梯门" in got
    assert info["fixed"] == 3


def test_punctuation_ignores_word_mention():
    text = "他在群里回了个“好”，没人接话。\n备注写了“电费再议”，没写日期。"
    got, info = repair_dialogue_punctuation(text)
    assert got == text
    assert info["changed"] is False


def test_punctuation_issue_finder_counts_same_cases():
    text = "道：“谁搁的”，没人应。\n喊道：“有人吗”，没人应。"
    assert len(find_dialogue_punctuation_issues(text)) == 2


# ── 双人同段 ─────────────────────────────────────────────────────────────


def test_split_two_speakers_with_attribution_colon():
    text = "“没一样是能见光的……”许照说：“那你别搬……”"
    got, info = split_multi_speaker_paragraphs(text)
    assert got == "“没一样是能见光的……”\n许照说：“那你别搬……”"
    assert info["split"] == 1


def test_split_two_adjacent_quotes():
    text = "“算谁的。”“算小的的。”"
    got, _info = split_multi_speaker_paragraphs(text)
    assert got == "“算谁的。”\n“算小的的。”"
    assert len(find_multi_speaker_paragraphs(text)) == 1


def test_does_not_split_same_speaker_mid_attribution():
    text = "“嗯。”她走到门口又停下来，“楼底下那个车库，租出去了？”"
    got, info = split_multi_speaker_paragraphs(text)
    assert got == text
    assert info["split"] == 0


def test_split_keeps_blank_line_style():
    text = "第一段。\n\n“算谁的。”“算小的的。”"
    got, _info = split_multi_speaker_paragraphs(text)
    assert "第一段。\n\n“算谁的。”\n\n“算小的的。”" == got


# ── 数字自洽 / 口头禅 ─────────────────────────────────────────────────────


def test_number_word_mismatch():
    text = "“子时后禁止开电梯。”还在。\n字还是那七个字，连标点都没变。"
    got = find_number_word_mismatch(text)
    assert got and got[0]["declared"] == 7 and got[0]["actual"] == 8


def test_number_word_no_false_positive():
    assert find_number_word_mismatch("备注写了四个字：电费再议。") == []


def test_repair_number_word_mismatch():
    text = "“子时后禁止开电梯。”还在。\n字还是那七个字，连标点都没变。"
    got, info = deterministic_repair(text)
    assert "字还是那八个字" in got
    assert "那七个字" not in got
    assert info["numbers"]["fixed"] == 1
    again, info2 = deterministic_repair(got)
    assert again == got
    assert info2["numbers"]["fixed"] == 0


def test_repair_number_word_mismatch_handles_far_reference():
    text = (
        "“子时后禁止开电梯。”还在。\n"
        + "他又删了一遍，退出来，重新进。\n" * 8
        + "那行字还在，字还是那七个字。"
    )
    got, info = deterministic_repair(text)
    assert info["numbers"]["fixed"] == 1
    assert "那七个字" not in got
    assert "那八个字" in got


def test_tics_over_limit():
    got = find_tics("小的" * 9)
    assert got and got[0]["word"] == "小的" and got[0]["count"] == 9


# ── 编排与验收 ───────────────────────────────────────────────────────────


def test_deterministic_repair_is_idempotent():
    text = (
        "许照道：“谁搁的”，没人应。\n"
        "“算谁的。”“算小的的。”"
    )
    once, info = deterministic_repair(text)
    twice, info2 = deterministic_repair(once)
    assert info["changed"] is True
    assert twice == once
    assert info2["changed"] is False


def test_plan_includes_hard_issues_without_archive():
    text = "小的" * 9 + "\n\n“子时后禁止开电梯。”还在。\n字还是那七个字。"
    plan = build_repair_plan(text, None, None)
    dims = {item["dimension"] for item in plan["issues"]}
    assert "人物声纹" in dims
    assert "事实自洽" in dims
    assert plan["ok"] is True
    assert "小的" in plan["instruction"]


def test_plan_instruction_is_not_treated_as_local_rewrite():
    from app.generation.scope_guard import declared_local

    text = (
        "许照道：“谁搁的”，没人应。\n"
        "“算谁的。”“算小的的。”\n"
        "“子时后禁止开电梯。”还在。字还是那七个字。\n"
        "小的说小的做的是辛苦钱，小的不问箱子里是什么。"
    )
    plan = build_repair_plan(text, None, None)
    assert plan["ok"] is True
    assert plan["instruction"]
    # 定向返修必须整段重写；一旦命中「只/仅/保留」会被当成局部改写拦下。
    assert declared_local(plan["instruction"]) is False


def test_acceptance_passes_for_punctuation_only_fix():
    before = "许照道：“谁搁的”，没人应。楼里安安静静。"
    after = "许照道：“谁搁的？”没人应。楼里安安静静。"
    result = evaluate_repair(before, after, 4000, LIMITS)
    assert result["ok"] is True


def test_acceptance_fails_when_text_is_cut_in_half():
    before = "许照道：“你搬什么呢。”钱多多说：“货。”\n" * 10
    after = before[: len(before) // 2]
    result = evaluate_repair(before, after, 4000, LIMITS)
    assert result["ok"] is False
    assert any(c["name"] == "字数" and not c["ok"] for c in result["checks"])


def test_acceptance_fails_when_dialogue_dropped():
    before = (
        "他说：“今天不搬了，明天再说，箱子先放在车库里。”\n"
        "她说：“那你去把押金退了，别拖到月底。”\n" * 6
    )
    after = "他走了。她也走了。\n" * 6
    result = evaluate_repair(before, after, 4000, LIMITS)
    assert result["ok"] is False
    assert any(c["name"] == "对白守恒" and not c["ok"] for c in result["checks"])
