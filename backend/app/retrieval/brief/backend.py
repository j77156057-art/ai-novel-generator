"""检索后端：一个可替换的 httpx JSON 实现。

只约定「POST 一个查询、拿回若干片段」，不绑定具体供应商：
- 请求体固定为 {"query": ..., "count": ...}；
- 响应按常见结构取列表（results / organic_results / items / data）；
- 每条取 title/name、snippet/description/content/summary、url/link，缺字段容忍。

超时与重试次数由 `BriefSettings` 控制；重试的判定与退避在 `tool.py`，
与 `app/llm/provider.py` 的 `_is_retryable` / `_backoff` 同一思路：
只重试超时/连接错误/429/5xx，退避指数增长并封顶。
"""
from __future__ import annotations

import html as _html
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .render import clean_text

#: 免费后端的默认 UA（桌面 Chrome）。可用 `BRIEF_TOOL_USER_AGENT` 覆盖。
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
# 免费后端：百度结果页优先，DuckDuckGo html 端点回退
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
_VERIFY_URL_MARKERS = ("wappass", "captcha", "verify")

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
    """无需 key 的公开结果页后端：百度优先，DuckDuckGo 回退。

    边界（用户 2026-11-15 明确）：
    - 单线程、全局最小间隔（默认不小于 1 秒），不并发；
    - 被验证码/空结果页/跳转验证拦下 → `SearchError(blocked=True)`，不重试；
    - 只抓公开结果页，不绕验证码、不伪造登录态。
    """

    BAIDU_URL = "https://www.baidu.com/s?wd={q}&rn={n}&ie=utf-8"
    DDG_URL = "https://html.duckduckgo.com/html/?q={q}"

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

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        q = (query or "").strip()
        if not q:
            raise SearchError("查询为空", retryable=False)
        limit = max(1, int(max_results))
        try:
            return self._search_baidu(q, limit)
        except SearchError as primary:
            if not self.allow_fallback:
                raise
            try:
                return self._search_ddg(q, limit)
            except SearchError as backup:
                blocked = primary.blocked or backup.blocked
                raise SearchError(
                    f"百度失败({primary})；DuckDuckGo 失败({backup})",
                    retryable=(primary.retryable or backup.retryable) and not blocked,
                    blocked=blocked,
                ) from backup

    def _search_baidu(self, query: str, limit: int) -> list[SearchResult]:
        url = self.BAIDU_URL.format(q=quote(query), n=limit)
        status, final_url, page = self._http_get(url)
        if status in (403, 429):
            raise SearchError(
                f"百度 HTTP {status}", status_code=status, blocked=True
            )
        if status >= 400:
            raise SearchError(
                f"百度 HTTP {status}", retryable=status >= 500, status_code=status
            )
        reason = _blocked_reason(page, final_url, _BAIDU_BLOCK_MARKERS)
        if reason:
            raise SearchError(f"百度验证/拦截: {reason}", status_code=status, blocked=True)
        results = parse_baidu_html(page)[:limit]
        if not results:
            # 空结果但 HTTP 200：按用户约定归为 blocked，而不是 ok/empty。
            raise SearchError("百度空结果页（HTTP 200）", status_code=status, blocked=True)
        return results

    def _search_ddg(self, query: str, limit: int) -> list[SearchResult]:
        status, final_url, page = self._http_get(self.DDG_URL.format(q=quote(query)))
        if status in (403, 429):
            raise SearchError(
                f"DuckDuckGo HTTP {status}", status_code=status, blocked=True
            )
        if status >= 400:
            raise SearchError(
                f"DuckDuckGo HTTP {status}", retryable=status >= 500, status_code=status
            )
        reason = _blocked_reason(page, final_url, _DDG_BLOCK_MARKERS)
        if reason:
            raise SearchError(
                f"DuckDuckGo 验证/拦截: {reason}", status_code=status, blocked=True
            )
        results = parse_ddg_html(page)[:limit]
        if not results:
            raise SearchError("DuckDuckGo 空结果页（HTTP 200）", status_code=status, blocked=True)
        return results

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
