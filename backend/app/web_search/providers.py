"""多家联网搜索服务的协议适配器（纯 HTTP → SearchResult）。

统一契约见 `base.SearchProvider`：不判断是否该搜、不做缓存、不吞异常；
异常一律包成 `SearchProviderError`，由 `service.search` 统一降级成可读提示。

选型说明（2026-11 市面通用程度）：
- Tavily：为 LLM 设计的摘要级搜索，见 `tavily.py`。
- Brave：独立索引，免费档每月 2000 次左右，覆盖英文较好。
- Serper：Google 结果代理，按次计费，中文时效性好。
- 博查 Bocha：国内中文搜索，摘要直接可用于中文创作。
- Exa：语义检索，适合找长文与相似内容。
- SearXNG：**自建聚合器**，可同时接入 Google/Bing/Brave/DuckDuckGo，
  是「一个端点搜全网」的通用方案；不需要 key，只需实例地址。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.web_search.base import SearchProviderError, SearchResult


def _clip(text: str, limit: int = 600) -> str:
    value = (text or "").strip()
    return value[:limit]


def _clean_url(raw: object) -> str:
    url = str(raw or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _read_json(
    req: urllib.request.Request,
    *,
    label: str,
    timeout: float,
) -> dict:
    """发请求并解析 JSON；把 HTTP / 网络错误翻译成可读中文。"""
    try:
        with urllib.request.urlopen(req, timeout=max(2.0, float(timeout))) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (401, 403):
            if label == "SearXNG":
                raise SearchProviderError(
                    "SearXNG 返回 401/403：请确认实例地址正确，"
                    "并确认实例的 settings.yml 里 search.formats 包含 json"
                    "（多数公网实例默认关闭 JSON API）"
                ) from exc
            raise SearchProviderError(
                f"{label} 拒绝了这个 Key（401/403），请在设置里检查 API Key"
            ) from exc
        if exc.code == 429:
            raise SearchProviderError(f"{label} 触发限流（429），请稍后再试") from exc
        raise SearchProviderError(
            f"{label} 返回 HTTP {exc.code}{('：' + detail) if detail else ''}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SearchProviderError(f"{label} 连接失败：{exc.reason}") from exc
    except TimeoutError as exc:
        raise SearchProviderError(f"{label} 请求超时") from exc
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        raise SearchProviderError(f"{label} 返回了无法解析的 JSON") from exc
    if not isinstance(data, dict):
        raise SearchProviderError(f"{label} 返回结构不是对象")
    return data


def _items(data: dict, *path: str) -> list[dict]:
    node: object = data
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, dict)]


class BraveProvider:
    """Brave Search API（https://api.search.brave.com）。"""

    name = "brave"
    default_base_url = "https://api.search.brave.com"
    _MAX_PER_REQUEST = 20

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    def search(self, query: str, *, max_results: int, safe: bool = True,
               timeout: float = 8.0) -> list[SearchResult]:
        if not self.api_key:
            raise SearchProviderError("未配置 Brave API Key")
        count = max(1, min(int(max_results or 1), self._MAX_PER_REQUEST))
        params = urllib.parse.urlencode({
            "q": query,
            "count": count,
            "safesearch": "strict" if safe else "moderate",
            "text_decorations": "false",
            "spellcheck": "false",
        })
        req = urllib.request.Request(f"{self.base_url}/res/v1/web/search?{params}")
        req.add_header("Accept", "application/json")
        req.add_header("X-Subscription-Token", self.api_key)
        data = _read_json(req, label="Brave", timeout=timeout)
        out: list[SearchResult] = []
        for item in _items(data, "web", "results"):
            url = _clean_url(item.get("url"))
            if not url:
                continue
            out.append(SearchResult(
                title=_clip(str(item.get("title") or "（无标题）"), 200),
                url=url,
                snippet=_clip(str(item.get("description") or "")),
                published_at=_clip(str(item.get("age") or ""), 40) or None,
            ))
        return out


class SerperProvider:
    """Serper.dev（Google 结果代理，https://google.serper.dev）。"""

    name = "serper"
    default_base_url = "https://google.serper.dev"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    def search(self, query: str, *, max_results: int, safe: bool = True,
               timeout: float = 8.0) -> list[SearchResult]:
        if not self.api_key:
            raise SearchProviderError("未配置 Serper API Key")
        payload = {
            "q": query,
            "num": max(1, min(int(max_results or 10), 100)),
            "hl": "zh-cn",
            "gl": "cn",
            "safe": "active" if safe else "off",
        }
        req = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-KEY", self.api_key)
        data = _read_json(req, label="Serper", timeout=timeout)
        out: list[SearchResult] = []
        for item in _items(data, "organic"):
            url = _clean_url(item.get("link"))
            if not url:
                continue
            out.append(SearchResult(
                title=_clip(str(item.get("title") or "（无标题）"), 200),
                url=url,
                snippet=_clip(str(item.get("snippet") or "")),
                published_at=_clip(str(item.get("date") or ""), 40) or None,
            ))
        return out


class BochaProvider:
    """博查 Bocha（中文网页搜索，https://api.bochaai.com）。"""

    name = "bocha"
    default_base_url = "https://api.bochaai.com"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    def search(self, query: str, *, max_results: int, safe: bool = True,
               timeout: float = 8.0) -> list[SearchResult]:
        if not self.api_key:
            raise SearchProviderError("未配置博查 API Key")
        payload = {
            "query": query,
            "summary": True,
            "count": max(1, min(int(max_results or 10), 50)),
            "page": 1,
        }
        req = urllib.request.Request(
            f"{self.base_url}/v1/web-search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {self.api_key}")
        data = _read_json(req, label="博查", timeout=timeout)
        out: list[SearchResult] = []
        for item in _items(data, "data", "webPages", "value"):
            url = _clean_url(item.get("url"))
            if not url:
                continue
            snippet = item.get("summary") or item.get("snippet") or ""
            out.append(SearchResult(
                title=_clip(str(item.get("name") or "（无标题）"), 200),
                url=url,
                snippet=_clip(str(snippet)),
                published_at=_clip(str(item.get("datePublished") or ""), 40) or None,
            ))
        return out


class ExaProvider:
    """Exa（语义检索，https://api.exa.ai）。"""

    name = "exa"
    default_base_url = "https://api.exa.ai"

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    def search(self, query: str, *, max_results: int, safe: bool = True,
               timeout: float = 8.0) -> list[SearchResult]:
        if not self.api_key:
            raise SearchProviderError("未配置 Exa API Key")
        payload = {
            "query": query,
            "numResults": max(1, min(int(max_results or 10), 100)),
            "type": "auto",
            "contents": {"text": {"maxCharacters": 600}},
        }
        req = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", self.api_key)
        data = _read_json(req, label="Exa", timeout=timeout)
        out: list[SearchResult] = []
        for item in _items(data, "results"):
            url = _clean_url(item.get("url"))
            if not url:
                continue
            out.append(SearchResult(
                title=_clip(str(item.get("title") or "（无标题）"), 200),
                url=url,
                snippet=_clip(str(item.get("text") or "")),
                published_at=_clip(str(item.get("publishedDate") or ""), 40) or None,
            ))
        return out


class SearxngProvider:
    """SearXNG 自建聚合搜索（一个实例聚合 Google/Bing/Brave 等多家）。"""

    name = "searxng"

    def __init__(self, api_key: str = "", base_url: str = "") -> None:
        # SearXNG 通常不需要 key；保留参数是为了与统一构建签名一致。
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").rstrip("/")

    def search(self, query: str, *, max_results: int, safe: bool = True,
               timeout: float = 8.0) -> list[SearchResult]:
        if not self.base_url:
            raise SearchProviderError(
                "SearXNG 需要实例地址（例如 http://127.0.0.1:8080），请在设置里填写"
            )
        count = max(1, min(int(max_results or 10), 50))
        params = urllib.parse.urlencode({
            "q": query,
            "format": "json",
            "safesearch": "1" if safe else "0",
            "language": "zh-CN",
        })
        req = urllib.request.Request(f"{self.base_url}/search?{params}")
        req.add_header("Accept", "application/json")
        data = _read_json(req, label="SearXNG", timeout=timeout)
        out: list[SearchResult] = []
        for item in _items(data, "results")[:count]:
            url = _clean_url(item.get("url"))
            if not url:
                continue
            out.append(SearchResult(
                title=_clip(str(item.get("title") or "（无标题）"), 200),
                url=url,
                snippet=_clip(str(item.get("content") or "")),
                published_at=_clip(str(item.get("publishedDate") or ""), 40) or None,
            ))
        return out
