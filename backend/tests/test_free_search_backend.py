"""免费检索后端（素材调研用）：解析、回退、冷却。

旧 brief 工具已删除（2026-11-23），这里只保留仍被 `materials/research.py`
使用的 FreeSearchBackend / 解析器测试。
"""
from __future__ import annotations

import pytest

from app.retrieval.brief.backend import (
    FreeSearchBackend,
    SearchError,
    _GlobalThrottle,
    parse_baidu_html,
    parse_bing_html,
    parse_ddg_html,
)

BAIDU_PAGE = """
<html><body>
<div class="result c-container" id="1">
  <h3 class="t"><a href="http://example.com/a">示例标题甲</a></h3>
  <div class="c-abstract">摘要甲，说明某公开流程的公开信息。</div>
</div>
<div class="result c-container" id="2">
  <h3 class="t"><a href="http://example.com/b">示例标题乙</a></h3>
  <div class="c-abstract">摘要乙，另有可核对的具体细节。</div>
</div>
</body></html>
"""

DDG_PAGE = """
<html><body>
<div class="result results_links web-result">
  <a rel="nofollow" class="result__a"
     href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx&amp;rut=abc">标题丙</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx">摘要丙，说明另一条公开信息。</a>
</div>
</body></html>
"""

BING_PAGE = """
<html><body>
<ol id="b_results">
<li class="b_algo">
  <h2><a href="https://example.com/bing1">标题丁</a></h2>
  <div class="b_caption"><p class="b_lineclamp2">摘要丁，说明另一条公开信息。</p></div>
</li>
<li class="b_algo">
  <h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1aHR0cHM6">跳转标题</a></h2>
  <p>跳转摘要</p>
</li>
</ol>
</body></html>
"""


class StubFreeBackend(FreeSearchBackend):
    """替换网络层，只测解析与拦截判定。"""

    def __init__(self, responses: list[tuple[int, str, str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self.responses = list(responses)
        self.urls: list[str] = []

    def _http_get(self, url: str) -> tuple[int, str, str]:
        self.urls.append(url)
        return self.responses.pop(0)


def test_parse_baidu_html() -> None:
    results = parse_baidu_html(BAIDU_PAGE)
    assert [r.title for r in results] == ["示例标题甲", "示例标题乙"]
    assert results[0].url == "http://example.com/a"
    assert "公开流程" in results[0].snippet


def test_parse_ddg_html_decodes_redirect() -> None:
    results = parse_ddg_html(DDG_PAGE)
    assert len(results) == 1
    assert results[0].title == "标题丙"
    assert results[0].url == "https://example.com/x"


def test_parse_bing_html_skips_redirect_entries() -> None:
    results = parse_bing_html(BING_PAGE)
    assert [r.title for r in results] == ["标题丁"]
    assert results[0].url == "https://example.com/bing1"
    assert "公开信息" in results[0].snippet


def test_free_backend_prefers_baidu() -> None:
    backend = StubFreeBackend([(200, "https://www.baidu.com/s", BAIDU_PAGE)])
    results = backend.search("任意中性查询", 2)
    assert len(results) == 2
    assert backend.urls[0].startswith("https://www.baidu.com/s?wd=")


def test_free_backend_blocked_captcha_page() -> None:
    page = "<html><body>百度安全验证 请输入验证码</body></html>"
    backend = StubFreeBackend([(200, "https://www.baidu.com/s", page)], allow_fallback=False)
    with pytest.raises(SearchError) as excinfo:
        backend.search("任意中性查询", 2)
    assert excinfo.value.blocked is True
    assert excinfo.value.retryable is False


def test_free_backend_empty_http_200_is_blocked() -> None:
    backend = StubFreeBackend([(200, "https://www.baidu.com/s", "<html></html>")], allow_fallback=False)
    with pytest.raises(SearchError) as excinfo:
        backend.search("任意中性查询", 2)
    assert excinfo.value.blocked is True


def test_free_backend_falls_back_to_ddg() -> None:
    captcha = "<html><body>请输入验证码</body></html>"
    backend = StubFreeBackend(
        [
            (200, "https://www.baidu.com/s", captcha),
            (200, "https://html.duckduckgo.com/html/", DDG_PAGE),
        ]
    )
    results = backend.search("任意中性查询", 2)
    assert [r.title for r in results] == ["标题丙"]
    assert len(backend.urls) == 2
    assert "duckduckgo.com" in backend.urls[1]


def test_free_backend_falls_back_to_bing() -> None:
    captcha = "<html><body>百度安全验证</body></html>"
    backend = StubFreeBackend(
        [
            (200, "https://www.baidu.com/s", captcha),
            (200, "https://html.duckduckgo.com/html/", "<html></html>"),
            (200, "https://cn.bing.com/search", BING_PAGE),
        ]
    )
    results = backend.search("任意中性查询", 2)
    assert [r.title for r in results] == ["标题丁"]
    assert "bing.com" in backend.urls[2]


def test_free_backend_skips_cooling_engine() -> None:
    """被拦的源在该任务内进入冷却，后续查询直接跳过它（不再撞验证码）。"""
    captcha = "<html><body>百度安全验证</body></html>"
    backend = StubFreeBackend(
        [
            (200, "https://www.baidu.com/s", captcha),
            (200, "https://html.duckduckgo.com/html/", DDG_PAGE),
            (200, "https://html.duckduckgo.com/html/", DDG_PAGE),
        ]
    )
    assert backend.search("查询一", 2)[0].title == "标题丙"
    assert backend.search("查询二", 2)[0].title == "标题丙"
    assert len(backend.urls) == 3
    assert backend.urls[2].startswith("https://html.duckduckgo.com")


def test_free_backend_no_fallback_when_disabled() -> None:
    captcha = "<html><body>请输入验证码</body></html>"
    backend = StubFreeBackend([(200, "https://www.baidu.com/s", captcha)], allow_fallback=False)
    with pytest.raises(SearchError):
        backend.search("任意中性查询", 2)
    assert len(backend.urls) == 1


def test_min_interval_cannot_go_below_one_second() -> None:
    assert FreeSearchBackend(min_interval_s=0.1).min_interval_s == 1.0
    assert FreeSearchBackend().min_interval_s >= 1.0


def test_global_throttle_sleeps_between_requests() -> None:
    throttle = _GlobalThrottle()
    now = [100.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    throttle.wait(1.5, clock, slept.append)  # 第一次不等待
    assert slept == []
    throttle.wait(1.5, clock, slept.append)  # 间隔不足，补睡
    assert slept == [1.5]
