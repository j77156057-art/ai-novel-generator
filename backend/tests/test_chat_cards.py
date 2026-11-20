"""对话卡片（生成邀请 / 写设定标记）的提取与校验测试。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from app.agents.chat_cards import (
    ActionMarkerFilter,
    build_bible_edit_card,
    build_generation_card,
    split_bible_marker,
)


def _project():
    return SimpleNamespace(
        id=9,
        plot_bible={
            "characters": [
                {
                    "name": "林汐",
                    "role": "夜班观测员",
                    "motivation": "查清姐姐死因",
                    "card": {"身份": {"性别": "女", "身份职业": "观测员"}},
                }
            ]
        },
    )


# ---------- 生成邀请卡 ----------

def test_generation_card_from_invitation():
    card = build_generation_card(
        project_id=9,
        query="那接下来怎么走",
        answer="这一段可以先把矛盾压住。要不要我写下一段？",
    )
    assert card is not None
    assert card["kind"] == "invoke_generation"
    assert card["endpoint"] == "/api/assistant/invoke-generation"
    assert card["body"]["action"] == "generate_next"
    assert card["body"]["project_id"] == 9
    assert "生成下一段" in card["title"]


def test_generation_card_from_button_hint():
    card = build_generation_card(
        project_id=9,
        query="好，落笔吧",
        answer="如果要落笔，点下面的「生成下一段」，我会按第 3 段约 3000 字来写。",
    )
    assert card is not None
    assert card["body"]["action"] == "generate_next"


def test_generation_card_ignores_plain_mention():
    assert build_generation_card(
        project_id=9,
        query="第 3 段写得好吗",
        answer="第 3 段的节奏不错，下一段可以放慢一点，多写环境。",
    ) is None


# ---------- 【写设定】标记 ----------

def test_split_marker_returns_clean_answer_and_payload():
    payload = {"character": "林汐", "field": "motivation", "value": "怕失去妹妹"}
    answer = "她现在的动机可以更私人。\n" + "【写设定】" + json.dumps(payload, ensure_ascii=False)
    clean, data, hit = split_bible_marker(answer)
    assert hit is True
    assert data == payload
    assert "【写设定】" not in clean
    assert clean == "她现在的动机可以更私人。"


def test_split_marker_invalid_json_is_stripped_but_no_card():
    answer = "先这样。\n【写设定】{not-json}"
    clean, data, hit = split_bible_marker(answer)
    assert hit is True
    assert data is None
    assert "【写设定】" not in clean


def test_split_marker_tolerates_trailing_punctuation():
    payload = {"character": "陈小满", "field": "motivation", "value": "怕病拖垮全家"}
    answer = "可以这样收。\n【写设定】" + json.dumps(payload, ensure_ascii=False) + "。"
    clean, data, hit = split_bible_marker(answer)
    assert hit is True
    assert data == payload
    assert clean == "可以这样收。"


def test_split_marker_tolerates_trailing_note():
    payload = {"character": "陈小满", "field": "motivation", "value": "怕病拖垮全家"}
    answer = "【写设定】" + json.dumps(payload, ensure_ascii=False) + "（确认后写入）"
    clean, data, hit = split_bible_marker(answer)
    assert hit is True
    assert data == payload
    assert "【写设定】" not in clean


def test_split_marker_accepts_half_width_brackets():
    payload = {"character": "沈渡", "field": "motivation", "value": "找出真相"}
    answer = "先这样推。\n[写设定]" + json.dumps(payload, ensure_ascii=False)
    clean, data, hit = split_bible_marker(answer)
    assert hit is True
    assert data == payload
    assert "[写设定]" not in clean
    assert clean == "先这样推。"


def test_split_marker_absent():
    clean, data, hit = split_bible_marker("普通回答，没有标记。")
    assert clean == "普通回答，没有标记。"
    assert data is None
    assert hit is False


def test_inline_marker_discussion_is_not_stripped():
    """模型在解释这个标记（行内引用）时，不能把后半句解释切掉。"""
    answer = (
        "如果是「写设定」的卡：那说明我上一轮末尾那行"
        '`【写设定】{"character":"林汐","field":"motivation","value":"x"}`'
        "没被界面识别，所以卡片没弹出来。"
    )
    clean, data, hit = split_bible_marker(answer)
    assert hit is False
    assert data is None
    assert clean == answer


def test_marker_filter_does_not_hold_inline_marker():
    """行内标记必须原样流出；只有行首标记才扣住。"""
    f = ActionMarkerFilter()
    answer = (
        "那是行内引用：`【写设定】"
        '{"character":"林汐","field":"motivation","value":"x"}'
        "`，不是提议。"
    )
    streamed = ""
    for i in range(0, len(answer), 5):
        streamed += f.feed(answer[i : i + 5])
    streamed += f.flush()
    assert streamed == answer


def test_marker_filter_holds_marker_and_json():
    f = ActionMarkerFilter()
    assert f.feed("结论先给到这里。") == "结论先给到这里。"
    assert f.feed("【写设定】") == ""
    assert f.feed('{"character":"林汐"') == ""
    assert f.flush().startswith("【写设定】")


def test_marker_filter_releases_when_not_marker():
    f = ActionMarkerFilter()
    assert f.feed("【写") == ""
    assert f.feed("法】不是标记") == "【写法】不是标记"
    assert f.flush() == ""


# ---------- 改设定卡 ----------

def test_bible_card_builds_fact_resolution_proposal():
    card = build_bible_edit_card(
        project=_project(),
        payload={"character": "林汐", "field": "motivation", "value": "怕失去妹妹"},
    )
    assert card is not None
    assert card["kind"] == "proposal"
    assert card["endpoint"] == "/api/assistant/proposals"
    assert card["body"]["type"] == "fact_resolution"
    assert card["body"]["target_kind"] == "project_plot_bible"
    assert card["body"]["target_id"] == "plot_bible"
    new_bible = json.loads(card["body"]["after_text"])
    assert new_bible["characters"][0]["motivation"] == "怕失去妹妹"
    assert card["detail"]["old_value"] == "查清姐姐死因"
    assert card["detail"]["new_value"] == "怕失去妹妹"


def test_bible_card_supports_nested_string_field():
    card = build_bible_edit_card(
        project=_project(),
        payload={
            "character": "林汐",
            "field": "card.身份.身份职业",
            "value": "潮汐观测员",
        },
    )
    assert card is not None
    new_bible = json.loads(card["body"]["after_text"])
    assert new_bible["characters"][0]["card"]["身份"]["身份职业"] == "潮汐观测员"


def test_bible_card_rejects_unknown_character_or_field():
    project = _project()
    assert build_bible_edit_card(
        project=project,
        payload={"character": "不存在的人", "field": "motivation", "value": "x"},
    ) is None
    assert build_bible_edit_card(
        project=project,
        payload={"character": "林汐", "field": "不存在的字段", "value": "x"},
    ) is None
    # 只改已有字符串字段：不新增键
    assert build_bible_edit_card(
        project=project,
        payload={"character": "林汐", "field": "secret", "value": "x"},
    ) is None
    # 非字符串字段（card 是对象）不允许整块替换成一句话
    assert build_bible_edit_card(
        project=project,
        payload={"character": "林汐", "field": "card", "value": "x"},
    ) is None


def test_bible_card_rejects_no_op_and_too_long_value():
    project = _project()
    assert build_bible_edit_card(
        project=project,
        payload={
            "character": "林汐",
            "field": "motivation",
            "value": "查清姐姐死因",
        },
    ) is None
    assert build_bible_edit_card(
        project=project,
        payload={"character": "林汐", "field": "motivation", "value": "长" * 401},
    ) is None
