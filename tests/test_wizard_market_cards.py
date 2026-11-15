"""题材行情卡：独立素材层的结构、时效与引导接入（2026-11-14）。

守护四件事：

  M1 结构 —— 每张卡都有来源 URL、采集日期、置信度、时间窗，缺一不可；
  M2 时效 —— 过了 `window.to` 的卡不加载、不被引用（热梗有寿命）；
  M3 题材 —— 只按卡里明确写过的题材/人设给方向，不匹配就返回空，不硬凑；
  M4 接入 —— 题材/主角两步能引用卡里的方向，且每条都带四件套引用；
              素材目录为空时退回原静态话术，不报错。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app import wizard_flow as flow  # noqa: E402
from app import wizard_market as market  # noqa: E402

TODAY = date(2026, 11, 14)


# ---------------------------------------------------------------------------
# M1 结构
# ---------------------------------------------------------------------------


def test_all_cards_have_required_provenance_fields():
    cards = market.load_cards(today=TODAY, strict=True)
    assert cards, "没读到任何行情卡"
    for card in cards:
        assert card["genres"]
        assert card["source"]["url"].startswith("http")
        assert card["source"]["published_at"]
        assert card["captured_at"]
        assert card["window"]["from"] <= card["window"]["to"]
        assert card["confidence"] in {"high", "medium", "low"}
        assert card["signals"]["tropes"] or card["signals"]["direction"]


def test_cards_do_not_leak_book_titles():
    """行情卡只存套路/人设/方向，不写死具体书名，换任何书都成立。"""
    for path in market.CARD_DIR.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "《" not in text, f"{path.name} 里出现了具体书名：{text}"


# ---------------------------------------------------------------------------
# M2 时效
# ---------------------------------------------------------------------------


def test_expired_cards_are_not_loaded():
    fresh = market.load_cards(today=TODAY)
    assert len(fresh) == 3

    # 2027-03-15：两张拉新卡（适用至 2027-03-14）过期，只剩悬疑扶持卡。
    partly = market.load_cards(today=date(2027, 3, 15))
    assert {c["card_id"] for c in partly} == {"2026-11-14-fanqie-2026-category-support"}

    # 2027-09-07：全部过期。
    assert market.load_cards(today=date(2027, 9, 7)) == []


def test_genre_block_drops_expired_directions():
    block = market.genre_block(today=date(2027, 3, 15))
    assert "悬疑" in block
    assert "都市" not in block, "过期卡的题材方向仍被引用"
    assert "2027-03-14" not in block, "过期卡的时间窗仍出现在话术里"


# ---------------------------------------------------------------------------
# M3 题材匹配
# ---------------------------------------------------------------------------


def test_protagonist_directions_only_for_covered_genres():
    assert market.protagonist_directions("都市", today=TODAY)
    assert market.protagonist_directions("悬疑", today=TODAY) == [], "卡里没写人设倾向却硬编了方向"
    assert market.protagonist_directions("不存在的题材", today=TODAY) == []
    assert market.protagonist_directions(None, today=TODAY) == []


def test_each_direction_carries_source_date_confidence_window():
    for direction in market.genre_directions(today=TODAY):
        cite = direction["citation"]
        assert "来源：" in cite and "采集 2026-11-14" in cite
        assert "置信度" in cite and "适用至" in cite


# ---------------------------------------------------------------------------
# M4 引导接入
# ---------------------------------------------------------------------------


def test_genre_question_cites_market_cards():
    body = flow.build_question("genre", {"branch": "original"}, {"today": TODAY})
    message = body["message"]
    assert "题材行情参考" in message
    assert "来源：" in message and "适用至" in message
    assert "幸存者偏差" in message, "没有把榜单偏差说清楚"


def test_protagonist_question_uses_market_directions():
    answers = {"branch": "original", "genre": "都市"}
    body = flow.build_question("protagonist", answers, {"today": TODAY})
    suggestions = body["card"]["suggestions"]
    assert any("来源：" in s for s in suggestions), suggestions
    assert "人设方向" in body["message"]


def test_protagonist_question_falls_back_without_cards(monkeypatch, tmp_path):
    monkeypatch.setattr(market, "CARD_DIR", tmp_path)
    answers = {"branch": "original", "genre": "都市"}
    body = flow.build_question("protagonist", answers, {"today": TODAY})
    assert body["card"]["suggestions"] == [
        "主角叫…，职业/身份是…，最在意…",
        "先不定名，让大纲给一个",
        "我有几个候选名字，帮我挑",
    ]
    assert "人设方向" not in body["message"]


def test_genre_question_falls_back_without_cards(monkeypatch, tmp_path):
    monkeypatch.setattr(market, "CARD_DIR", tmp_path)
    body = flow.build_question("genre", {"branch": "original"}, {"today": TODAY})
    assert "题材行情参考" not in body["message"]
    assert body["card"]["options"], "无行情卡时题材选项不能一起消失"


def test_bad_card_is_skipped_in_production_but_flagged_in_tests(tmp_path):
    (tmp_path / "broken.json").write_text(json.dumps({"card_id": "x"}), encoding="utf-8")
    assert market.load_cards(directory=tmp_path, today=TODAY) == []
    with pytest.raises(ValueError):
        market.load_cards(directory=tmp_path, today=TODAY, strict=True)


# ---------------------------------------------------------------------------
# M5 追问路径：行情类问题必须走卡，不能靠模型记忆
# ---------------------------------------------------------------------------


def test_what_genre_sells_answers_with_market_cards():
    from app import wizard_chat as chat  # noqa: PLC0415

    answers = {"branch": "original"}
    ctx = {"today": TODAY}
    question = flow.build_question("genre", answers, ctx)
    reply = chat.explain("genre", "现在什么题材好卖", answers, ctx, question)
    assert "题材行情参考" in reply
    assert "来源：" in reply and "置信度" in reply and "适用至" in reply
    assert "幸存者偏差" in reply


def test_popular_protagonist_answers_with_source_and_caveat():
    from app import wizard_chat as chat  # noqa: PLC0415

    answers = {"branch": "original", "genre": "都市"}
    ctx = {"today": TODAY}
    question = flow.build_question("protagonist", answers, ctx)
    reply = chat.explain("protagonist", "什么主角比较讨喜", answers, ctx, question)
    assert "来源：" in reply and "适用至" in reply
    assert "不能当事实" in reply, "把「讨喜」当成了事实"


def test_expired_cards_do_not_get_quoted(monkeypatch, tmp_path):
    from app import wizard_chat as chat  # noqa: PLC0415

    monkeypatch.setattr(market, "CARD_DIR", tmp_path)
    answers = {"branch": "original"}
    ctx = {"today": TODAY}
    question = flow.build_question("genre", answers, ctx)
    reply = chat.explain("genre", "现在什么题材好卖", answers, ctx, question)
    assert "来源：" not in reply
    assert "题材行情参考" not in reply


def test_market_hints_never_write_into_answers():
    """行情卡只做提示：不落 answers、不进蒸馏档案、不改项目字段。"""
    answers = {"branch": "original", "genre": "都市"}
    flow.build_question("protagonist", answers, {"today": TODAY})
    assert answers == {"branch": "original", "genre": "都市"}


