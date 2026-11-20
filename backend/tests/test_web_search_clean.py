"""搜索结果清洗（去重 / 去噪 / 同站限流）的规则测试。"""
from __future__ import annotations

from app.web_search.clean import (
    clean_text,
    clean_title,
    filter_results,
    is_search_page,
    normalize_url,
)
from app.web_search.base import SearchResult


def _r(title: str, url: str, snippet: str = "这是一段足够长的正常摘要内容，用来描述页面要点。", score=None):
    return SearchResult(title=title, url=url, snippet=snippet, score=score)


def test_clean_text_strips_tags_entities_and_boilerplate():
    raw = (
        "<p>雾港的潮汐观测网在近未来被市政局垄断。</p>"
        "本文转自某某内容站。"
        "责任编辑：张三"
        "&nbsp;"
    )
    text = clean_text(raw)
    assert "<p>" not in text
    assert "本文转自" not in text
    assert "责任编辑" not in text
    assert "潮汐观测网" in text


def test_clean_title_strips_site_suffix():
    assert clean_title("林汐 - 知乎") == "林汐"
    assert clean_title("雾港观测者_百度百科") == "雾港观测者"
    assert clean_title("<b>标题</b> | 掘金") == "标题"


def test_normalize_url_drops_tracking_and_www():
    a = normalize_url("https://www.example.com/post/1/?utm_source=x&id=7#top")
    b = normalize_url("https://example.com/post/1?id=7")
    assert a == b


def test_search_page_is_rejected():
    assert is_search_page("https://www.google.com/search?q=x") is True
    assert is_search_page("https://www.baidu.com/s?wd=x") is True
    assert is_search_page("https://example.com/article") is False


def test_filter_drops_login_wall_and_404():
    items = [
        _r("请先登录后查看全文", "https://a.example.com/1"),
        _r("页面不存在", "https://b.example.com/2"),
        _r("正常文章", "https://c.example.com/3"),
    ]
    kept, dropped = filter_results(items, max_results=5)
    assert [x.title for x in kept] == ["正常文章"]
    assert dropped == 2


def test_filter_drops_empty_snippet_and_search_page():
    items = [
        _r("只有标题", "https://a.example.com/1", snippet=""),
        _r("搜索页", "https://www.bing.com/search?q=x"),
    ]
    kept, dropped = filter_results(items, max_results=5)
    assert kept == []
    assert dropped == 2


def test_filter_dedupes_urls_and_near_titles():
    items = [
        _r("林汐的人物动机", "https://www.example.com/a?utm_source=x", score=0.9),
        _r("林汐的人物动机", "https://example.com/a", score=0.8),
        _r("另一篇讲林汐",
           "https://other.example.com/b", score=0.7),
        _r("另一篇讲林汐", "https://third.example.com/c", score=0.6),
    ]
    kept, dropped = filter_results(items, max_results=5)
    assert len(kept) == 2
    assert dropped == 2
    assert kept[0].score == 0.9


def test_filter_caps_per_domain():
    items = [
        _r(f"站点内第{i}篇", f"https://same.example.com/p{i}", score=0.9 - i * 0.1)
        for i in range(5)
    ]
    kept, dropped = filter_results(items, max_results=5, per_domain=2)
    assert len(kept) == 2
    assert dropped == 3
    assert all("same.example.com" in x.url for x in kept)


def test_filter_sorts_by_score_and_caps_max_results():
    items = [
        _r("低分", "https://a.example.com/1", score=0.2),
        _r("高分", "https://b.example.com/2", score=0.9),
        _r("无分数", "https://c.example.com/3", score=None),
    ]
    kept, dropped = filter_results(items, max_results=2)
    assert [x.title for x in kept] == ["高分", "低分"]
    assert dropped == 0


def test_filter_keeps_legit_chinese_article():
    item = _r(
        "近未来科幻小说的城市书写",
        "https://literature.example.com/scifi-city",
        "文章讨论雾港这类近未来城市如何用基础设施承载悬疑结构，并举了三部作品。",
        score=0.81,
    )
    kept, dropped = filter_results([item], max_results=5)
    assert len(kept) == 1 and dropped == 0
    assert kept[0].title == "近未来科幻小说的城市书写"


def test_safe_search_drops_adult_content_only_when_enabled():
    item = _r(
        "成人网站导航",
        "https://example.com/links",
        "色情内容合集，包含成人视频与裸聊入口。",
        score=0.9,
    )
    kept, dropped = filter_results([item], max_results=5, safe=True)
    assert kept == [] and dropped == 1

    kept, dropped = filter_results([item], max_results=5, safe=False)
    assert len(kept) == 1 and dropped == 0


def test_safe_search_checks_domain_labels():
    item = _r(
        "一个页面",
        "https://www.porn.example.com/post",
        "页面正常的摘要内容，描述某个城市的悬疑气氛。",
        score=0.7,
    )
    kept, dropped = filter_results([item], max_results=5, safe=True)
    assert kept == [] and dropped == 1


def test_safe_search_does_not_drop_normal_coming_of_age_story():
    item = _r(
        "成人礼上的家族冲突",
        "https://literature.example.com/story",
        "小说写的是成人礼当天，主角与父亲爆发冲突，气氛紧张。",
        score=0.7,
    )
    kept, dropped = filter_results([item], max_results=5, safe=True)
    assert len(kept) == 1 and dropped == 0
