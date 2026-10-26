"""消费清洗的**结构判据**（v5）：站点块自带边界的三种载体。

背景（docs/authority-corpus-cleaning-round2.md）：盗版站把广告插进正文，
载体是「域名 token」「括号块」「整行」。这三种都**自带边界**，
所以只删边界以内的那一段，块外的字一律不动。

统计判据（scan_watermarks）对这三种载体天生不适用（只认「紧跟句号的高频
汉字串」），所以这里用封闭模板。

本文件同时钉住**不误伤**：正文里当动词的「收藏」、正文里的「纵横术士」、
不是行首的「更新快」—— 一个都不能被删。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.ingestion.clean import clean_consumption_text  # noqa: E402


def clean(text: str) -> str:
    out, _ = clean_consumption_text(text)
    return out


# ---------------------------------------------------------------- 删得掉


def test_bare_info_token_is_removed_without_touching_the_sentence():
    """站点把域名尾巴插在句中：只删 token，句子其余部分逐字不动。"""
    text = "手中把玩着赵青锋上交王府的名刺。.info赵青锋站在凉亭外。"
    assert clean(text) == "手中把玩着赵青锋上交王府的名刺。赵青锋站在凉亭外。"


def test_wap_prefixed_info_token_is_removed():
    text = "正文一句。wap..info第二句。"
    assert clean(text) == "正文一句。第二句。"


def test_strong_wrapped_info_token_is_removed():
    text = "正文一句。<strong>..info</strong>第二句。"
    assert clean(text) == "正文一句。第二句。"


def test_bracketed_site_block_is_removed_as_a_whole():
    """括号块：块内的整段站点促销语删掉，块外的正文一字不动。"""
    text = ("当不得真。[.info更新快，网站页面清爽，广告少，无弹窗，最喜欢这种网站了，"
            "一定要好评]所以耳濡目染的阿古达木，比所有北庭武将都深知虚虚实实兵不厌诈的道理。")
    out = clean(text)
    assert "网站页面清爽" not in out
    assert out == "当不得真。所以耳濡目染的阿古达木，比所有北庭武将都深知虚虚实实兵不厌诈的道理。"


def test_bracketed_block_keeps_the_text_around_it():
    text = "前一句。[想看的书几乎都有啊，比一般的小说网站要稳定很多更新还快，全文字的没有广告。]后一句。"
    out = clean(text)
    assert out == "前一句。后一句。"


def test_line_starting_with_a_site_phrase_is_removed_whole_line():
    text = "第一行正文。\r\n最快更新无错小说阅读，请访问.feizw.\r\n第二行正文。"
    out = clean(text)
    assert "最快更新" not in out
    assert "第一行正文。" in out and "第二行正文。" in out


def test_mnemonic_line_is_removed():
    text = "正文。\r\n天才一秒记住本站地址：.。顶点笔趣阁手机版阅读网址：m.\r\n正文二。"
    out = clean(text)
    assert "一秒记住" not in out
    assert "正文二。" in out


# ---------------------------------------------------------------- 不误伤


def test_collect_as_an_ordinary_verb_is_never_removed():
    """「收藏」在正文里是普通动词 —— 删了就把句子删坏了。"""
    text = "那些临摹红甲符箓梵文绘制而成的宣纸，也都由她整理收藏在书箱中。"
    assert clean(text) == text


def test_zongheng_in_prose_is_never_removed():
    """「纵横术士」是正文词，不是站点。"""
    text = "师从上阴学宫韩谷子韩大家，成了兵法大家许煌、纵横术士司马灿等一干帝国名流的小师妹。"
    assert clean(text) == text


def test_site_phrase_in_the_middle_of_a_line_is_kept():
    """只有**行首**的站点短语才算整行广告；行中间的不能连着正文一起删。"""
    text = "他把书名改成了《北荒刀歌》，更新快，很得意。"
    assert clean(text) == text


def test_narrative_parenthetical_is_kept():
    text = "他想了想（这事儿得先跟掌柜的说），便转身出门。"
    assert clean(text) == text


def test_prose_with_a_dot_info_like_word_is_kept():
    """`.info` 前面是字母时仍按 token 处理，但普通英文句不该被误删。"""
    text = "He opened the file and read the note. Nothing else happened."
    assert clean(text) == text


def test_bare_domain_inside_a_word_stays_as_review_evidence():
    """既有契约：嵌在词里的裸域名**不删**，只作复核证据
    （tests/test_distill_residual_policy.py 钉的就是这条）。"""
    text = "收藏天下武学秘笈。甲.info乙（..info好看的小说"
    assert clean(text) == text
    _out, report = clean_consumption_text(text)
    assert report['unresolved'], "裸域名必须留证据，不能悄悄删掉"


# ---------------------------------------------------------------- 报告与确定性


def test_ordinary_words_are_still_reported_as_unresolved_evidence():
    """「收藏」只标记不删 —— 报告里必须看得见「为什么没删」。"""
    text = "也都由她整理收藏在书箱中。"
    _out, report = clean_consumption_text(text)
    markers = [u["marker"] for u in report["unresolved"]]
    assert "收藏" in markers
    assert report["removed_chars"] == 0


def test_cleaning_is_deterministic_and_idempotent():
    text = ("正文一句。wap..info第二句。\r\n最快更新无错小说阅读，请访问.feizw.\r\n"
            "当不得真。[.info更新快，广告少，一定要好评]后一句。\r\n收藏在书箱中。")
    once = clean(text)
    assert clean(text) == once
    assert clean(once) == once, "清洗不幂等"
