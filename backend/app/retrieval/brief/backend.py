"""检索后端：一个可替换的 httpx JSON 实现。

只约定「POST 一个查询、拿回若干片段」，不绑定具体供应商：
- 请求体固定为 {"query": ..., "count": ...}；
- 响应按常见结构取列表（results / organic_results / items / data）；
- 每条取 title/name、snippet/description/content/summary、url/link，缺字段容忍。

本文件是旧 brief 工具**唯一保留**的部分：`app/materials/research.py` 的
SearchRunner 仍在用它抓免费结果页（百度 → DuckDuckGo → Bing）。
"""
from __future__ import annotations

import html as _html
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, quote, unquote, urlsplit



#: 免费后端的默认 UA（桌面 Chrome），可用构造参数 `user_agent` 覆盖。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class SearchResult:
    title: str = ""
    snippet: str = ""
    url: str = ""


class SearchError(RuntimeError):
    """检索失败（HTTP/超时/结构异常）。调用方一律降级为无素材。

    `blocked=True` 表示被反爬拦下（验证码页 / 空结果但 HTTP 200 / 跳转验证），
    与普通错误分开留痕，且**不重试**——被拦就降级，这是设计的一部分。
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        blocked: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.blocked = blocked


class SearchBackend(Protocol):
    def search(self, query: str, max_results: int) -> list[SearchResult]:
        ...


def _first_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "organic_results", "items", "data", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _pick(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class JSONSearchBackend:
    """基于 httpx 的最小实现；不导入具体搜索引擎 SDK。"""

    def __init__(self, url: str, api_key: str = "", *, timeout_s: float = 3.0) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout_s = timeout_s

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        if not self.url:
            raise SearchError("search_url 未配置", retryable=False)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 依赖缺失时明确降级
            raise SearchError(f"httpx 不可用: {exc}", retryable=False) from exc

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            resp = httpx.post(
                self.url,
                json={"query": query, "count": max_results},
                headers=headers,
                timeout=self.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise SearchError(f"检索超时: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"检索网络错误: {exc}", retryable=True) from exc

        if resp.status_code >= 400:
            retryable = resp.status_code == 429 or resp.status_code >= 500
            raise SearchError(
                f"检索 HTTP {resp.status_code}", retryable=retryable, status_code=resp.status_code
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SearchError(f"检索响应非 JSON: {exc}", retryable=False) from exc

        out: list[SearchResult] = []
        for item in _first_list(data)[:max_results]:
            if not isinstance(item, dict):
                continue
            out.append(
                SearchResult(
                    title=_pick(item, "title", "name"),
                    snippet=_pick(item, "snippet", "description", "content", "summary"),
                    url=_pick(item, "url", "link"),
                )
            )
        return out


# --------------------------------------------------------------------------- #
# 免费后端：百度结果页优先，DuckDuckGo / Bing html 端点依次回退
# --------------------------------------------------------------------------- #

#: 反爬特征。命中即视为 `blocked`，降级为空素材，不重试、不绕验证码。
_BAIDU_BLOCK_MARKERS = (
    "百度安全验证",
    "请输入验证码",
    "wappass.baidu.com",
    "网络不给力",
    "安全验证",
    "访问过于频繁",
)
_DDG_BLOCK_MARKERS = (
    "anomaly",
    "unusual traffic",
    "bots use duckduckgo",
    "challenge",
    "please try again",
)
_BING_BLOCK_MARKERS = (
    "unusual traffic",
    "captcha",
    "验证码",
    "请验证",
    "too many requests",
)
_VERIFY_URL_MARKERS = ("wappass", "captcha", "verify")

#: 单引擎被拦后的冷却：先跳过它，避免反复撞验证码把封禁拉长。
#: 连续被拦时指数退避（180 → 360 → …，封顶 1800 秒）。
ENGINE_COOLDOWN_BASE_S = 180.0
ENGINE_COOLDOWN_MAX_S = 1800.0

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class _GlobalThrottle:
    """进程级最小请求间隔：所有免费后端实例共用，保证单线程、低频。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(
        self,
        min_interval_s: float,
        clock: Callable[[], float],
        sleep_fn: Callable[[float], None],
    ) -> None:
        with self._lock:
            now = clock()
            gap = now - self._last
            if self._last > 0 and gap < min_interval_s:
                sleep_fn(min_interval_s - gap)
                now = clock()
            self._last = now


_GLOBAL_THROTTLE = _GlobalThrottle()


def _plain(raw: str) -> str:
    """去标签 + 解实体 + 压空白；URL/命令句清洗交给 `clean_text`。"""
    text = _TAG_RE.sub(" ", raw or "")
    return _WS_RE.sub(" ", _html.unescape(text)).strip()


def clean_text(text: str) -> str:
    """结果文本清洗：去标签/实体/多余空白，丢掉纯 URL 的「标题」。"""
    cleaned = _plain(text)
    if cleaned.startswith(("http://", "https://", "www.")) and " " not in cleaned:
        return ""
    return cleaned


def _first_href(tag: str) -> str:
    match = re.search(r'href="([^"]+)"', tag or "", re.I)
    return _html.unescape(match.group(1)) if match else ""


def _decode_ddg_url(url: str) -> str:
    """DDG 结果链接是 /l/?uddg=<encoded>，还原成真实地址。"""
    try:
        parts = urlsplit(url)
        if "duckduckgo.com" in (parts.netloc or "") and parts.path.startswith("/l/"):
            target = parse_qs(parts.query).get("uddg")
            if target:
                return unquote(target[0])
    except ValueError:
        pass
    return url


def _blocked_reason(page: str, final_url: str, markers: tuple[str, ...]) -> str:
    low = (page or "").lower()
    for marker in markers:
        if marker.lower() in low:
            return marker
    url = (final_url or "").lower()
    for marker in _VERIFY_URL_MARKERS:
        if marker in url:
            return f"redirect:{marker}"
    return ""


def parse_baidu_html(page: str) -> list[SearchResult]:
    """解析百度结果页：标题/摘要/链接；结构变了就返回空，由调用方判 blocked。"""
    out: list[SearchResult] = []
    blocks = re.split(r'(?=<div[^>]+class="[^"]*\bresult\b)', page or "", flags=re.I)
    for block in blocks[1:]:
        h3 = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S | re.I)
        if not h3:
            continue
        anchor = re.search(r"<a[^>]+>", h3.group(1), re.I)
        if not anchor:
            continue
        url = _first_href(anchor.group(0))
        title = _plain(h3.group(1).split(anchor.group(0), 1)[-1].split("</a>", 1)[0])
        snippet = ""
        for pattern in (
            r'<div[^>]+class="[^"]*c-abstract[^"]*"[^>]*>(.*?)</div>',
            r'<span[^>]+class="[^"]*content-right[^"]*"[^>]*>(.*?)</span>',
            r'<div[^>]+class="[^"]*c-span-last[^"]*"[^>]*>(.*?)</div>',
        ):
            match = re.search(pattern, block, re.S | re.I)
            if match:
                snippet = _plain(match.group(1))
                break
        if title:
            out.append(
                SearchResult(title=clean_text(title), snippet=clean_text(snippet), url=url)
            )
    return out


def parse_bing_html(page: str) -> list[SearchResult]:
    """解析 Bing 结果页（`li.b_algo`）：标题/摘要/链接。

    只取直链；Bing 偶尔把结果包在 `/ck/a?...&u=a1<base64>` 跳转里，这种条目
    直接跳过（不猜编码），避免把跳转地址当素材来源。
    """
    out: list[SearchResult] = []
    blocks = re.split(r'(?=<li[^>]+class="[^"]*\bb_algo\b)', page or "", flags=re.I)
    for block in blocks[1:]:
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S | re.I)
        if not h2:
            continue
        anchor = re.search(r"<a[^>]+>", h2.group(1), re.I)
        if not anchor:
            continue
        url = _first_href(anchor.group(0))
        if not url or "/ck/a" in url:
            continue
        title = _plain(h2.group(1).split(anchor.group(0), 1)[-1].split("</a>", 1)[0])
        snippet = ""
        for pattern in (
            r'<p[^>]+class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>',
            r'<div[^>]+class="[^"]*b_caption[^"]*"[^>]*>(.*?)</div>',
            r"<p[^>]*>(.*?)</p>",
        ):
            match = re.search(pattern, block, re.S | re.I)
            if match:
                snippet = _plain(match.group(1))
                break
        if title:
            out.append(
                SearchResult(title=clean_text(title), snippet=clean_text(snippet), url=url)
            )
    return out


def parse_ddg_html(page: str) -> list[SearchResult]:
    """解析 DuckDuckGo html 端点结果。"""
    out: list[SearchResult] = []
    blocks = re.split(r'(?=<div[^>]+class="[^"]*\bresult\b)', page or "", flags=re.I)
    for block in blocks[1:]:
        tag = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*>', block, re.I)
        if not tag:
            continue
        rest = block[tag.end() :]
        title = _plain(rest.split("</a>", 1)[0])
        snippet = ""
        snippet_tag = re.search(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>', block, re.I)
        if snippet_tag:
            snippet = _plain(block[snippet_tag.end() :].split("</a>", 1)[0])
        if title:
            out.append(
                SearchResult(
                    title=clean_text(title),
                    snippet=clean_text(snippet),
                    url=_decode_ddg_url(_first_href(tag.group(0))),
                )
            )
    return out


class FreeSearchBackend:
    """无需 key 的公开结果页后端：百度 → DuckDuckGo → Bing 依次回退。

    边界（用户 2026-11-15 明确）：
    - 单线程、全局最小间隔（默认不小于 1 秒），不并发；
    - 被验证码/空结果页/跳转验证拦下 → `SearchError(blocked=True)`，不重试；
    - 只抓公开结果页，不绕验证码、不伪造登录态。

    限流处理（2026-11-23）：某个源被拦后**只冷却这一个源**（180s 起，连续被拦
    指数退避到 1800s）——一个调研任务要连发几十条查询，没必要每条都再撞一次
    验证码；其它源继续服务。三个源全部不可用时才按 `blocked` 上报。
    """

    BAIDU_URL = "https://www.baidu.com/s?wd={q}&rn={n}&ie=utf-8"
    DDG_URL = "https://html.duckduckgo.com/html/?q={q}"
    BING_URL = "https://cn.bing.com/search?q={q}&count={n}&setlang=zh-Hans"

    def __init__(
        self,
        *,
        timeout_s: float = 3.0,
        min_interval_s: float = 1.5,
        user_agent: str = DEFAULT_USER_AGENT,
        allow_fallback: bool = True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout_s = timeout_s
        # 用户要求最小间隔不小于 1 秒；配置更小也按 1 秒执行。
        self.min_interval_s = max(1.0, float(min_interval_s))
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.allow_fallback = allow_fallback
        self._clock = clock
        self._sleep = sleep
        # 单引擎冷却（实例级）：一个调研任务会连发几十条查询，被拦的源没必要
        # 每条都再撞一次验证码；跳过它，把机会留给还能用的源。
        self._engine_until: dict[str, float] = {}
        self._engine_strikes: dict[str, int] = {}

    def _cooling(self, name: str) -> bool:
        return self._clock() < self._engine_until.get(name, 0.0)

    def _mark_blocked(self, name: str) -> None:
        strikes = self._engine_strikes.get(name, 0) + 1
        self._engine_strikes[name] = strikes
        wait = min(ENGINE_COOLDOWN_BASE_S * (2 ** (strikes - 1)), ENGINE_COOLDOWN_MAX_S)
        self._engine_until[name] = self._clock() + wait

    def _mark_ok(self, name: str) -> None:
        self._engine_strikes.pop(name, None)
        self._engine_until.pop(name, None)

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        q = (query or "").strip()
        if not q:
            raise SearchError("查询为空", retryable=False)
        limit = max(1, int(max_results))
        engines = [
            ("baidu", self._search_baidu),
            ("ddg", self._search_ddg),
            ("bing", self._search_bing),
        ]
        if not self.allow_fallback:
            engines = engines[:1]
        errors: list[str] = []
        any_blocked = False
        any_retryable = False
        skipped = 0
        for name, fn in engines:
            if self._cooling(name):
                skipped += 1
                errors.append(f"{name}: 冷却中")
                continue
            try:
                results = fn(q, limit)
                self._mark_ok(name)
                return results
            except SearchError as exc:
                errors.append(f"{name}失败({exc})")
                any_retryable = any_retryable or exc.retryable
                if exc.blocked:
                    any_blocked = True
                    self._mark_blocked(name)
        blocked = any_blocked or skipped == len(engines)
        raise SearchError(
            "；".join(errors) or "所有免费源均在冷却中",
            retryable=any_retryable and not blocked,
            blocked=blocked,
        )

    def _search_engine(self, query: str, limit: int, *, name: str, url_tpl: str,
                       markers: tuple[str, ...], parser) -> list[SearchResult]:
        """三个引擎共用的骨架：请求 → 状态码 → 拦截页 → 空结果 → 解析。"""
        url = url_tpl.format(q=quote(query), n=limit)
        status, final_url, page = self._http_get(url)
        if status in (403, 429):
            raise SearchError(f"{name} HTTP {status}", status_code=status, blocked=True)
        if status >= 400:
            raise SearchError(
                f"{name} HTTP {status}", retryable=status >= 500, status_code=status
            )
        reason = _blocked_reason(page, final_url, markers)
        if reason:
            raise SearchError(f"{name} 验证/拦截: {reason}", status_code=status, blocked=True)
        results = parser(page)[:limit]
        if not results:
            # 空结果但 HTTP 200：按用户约定归为 blocked，而不是 ok/empty。
            raise SearchError(f"{name} 空结果页（HTTP 200）", status_code=status, blocked=True)
        return results

    def _search_baidu(self, query: str, limit: int) -> list[SearchResult]:
        return self._search_engine(
            query, limit, name="百度", url_tpl=self.BAIDU_URL,
            markers=_BAIDU_BLOCK_MARKERS, parser=parse_baidu_html,
        )

    def _search_ddg(self, query: str, limit: int) -> list[SearchResult]:
        return self._search_engine(
            query, limit, name="DuckDuckGo", url_tpl=self.DDG_URL,
            markers=_DDG_BLOCK_MARKERS, parser=parse_ddg_html,
        )

    def _search_bing(self, query: str, limit: int) -> list[SearchResult]:
        return self._search_engine(
            query, limit, name="Bing", url_tpl=self.BING_URL,
            markers=_BING_BLOCK_MARKERS, parser=parse_bing_html,
        )

    def _http_get(self, url: str) -> tuple[int, str, str]:
        """返回 (status, final_url, text)；网络层错误抛 SearchError。"""
        _GLOBAL_THROTTLE.wait(self.min_interval_s, self._clock, self._sleep)
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 依赖缺失时明确降级
            raise SearchError(f"httpx 不可用: {exc}", retryable=False) from exc
        try:
            resp = httpx.get(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                },
                timeout=self.timeout_s,
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise SearchError(f"检索超时: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"检索网络错误: {exc}", retryable=True) from exc
        return resp.status_code, str(resp.url), resp.text
