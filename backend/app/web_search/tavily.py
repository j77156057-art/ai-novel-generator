"""Tavily 搜索适配器。

只做「HTTP 请求 → SearchResult 列表」的协议转换；不判断是否该搜、
不做缓存，也不把异常吞掉（由 service 统一降级）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from app.web_search.base import SearchProviderError, SearchResult

_TAVILY_DEFAULT_URL = "https://api.tavily.com"


class TavilyProvider:
    name = "tavily"

    #: Tavily 单次请求的硬上限（接口 max_results 最大 20）。
    _MAX_PER_REQUEST = 20
    #: 本地允许拉取的总条数上限：超过 20 时拆成多条并行请求。
    _MAX_TOTAL = 30

    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or _TAVILY_DEFAULT_URL).rstrip("/")

    def _request(
        self,
        query: str,
        *,
        max_results: int,
        depth: str,
        timeout: float,
    ) -> list[SearchResult]:
        """发一次 Tavily 搜索并解析成 SearchResult。"""
        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": depth,
            "include_answer": False,
            "include_raw_content": False,
            "max_results": max(
                1, min(int(max_results or 1), self._MAX_PER_REQUEST)
            ),
        }
        req = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # 新老 Tavily 文档对 key 位置有差异：body 与 Bearer 同时带，兼容两版。
        req.add_header("Authorization", f"Bearer {self.api_key}")
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
                raise SearchProviderError(
                    "Tavily 拒绝了这个 Key（401/403），请在设置里检查 API Key"
                ) from exc
            if exc.code == 429:
                raise SearchProviderError("Tavily 触发限流（429），请稍后再试") from exc
            raise SearchProviderError(
                f"Tavily 返回 HTTP {exc.code}{('：' + detail) if detail else ''}"
            ) from exc
        except urllib.error.URLError as exc:
            raise SearchProviderError(f"Tavily 连接失败：{exc.reason}") from exc
        except TimeoutError as exc:
            raise SearchProviderError("Tavily 请求超时") from exc

        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            raise SearchProviderError("Tavily 返回了无法解析的 JSON") from exc

        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        out: list[SearchResult] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            title = str(item.get("title") or "（无标题）").strip()[:200]
            snippet = str(item.get("content") or item.get("snippet") or "").strip()
            published = item.get("published_date") or item.get("published_at")
            score = item.get("score")
            try:
                score_value = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_value = None
            out.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:600],
                    published_at=str(published).strip() if published else None,
                    score=score_value,
                )
            )
        return out

    def search(
        self,
        query: str,
        *,
        max_results: int,
        safe: bool = True,
        timeout: float = 8.0,
    ) -> list[SearchResult]:
        """执行搜索，必要时**并行**发多批请求。

        Tavily 单次最多返回 20 条；要 30 条就拆成「basic 20 + advanced 10」
        两批并行发出，再合并去重。为什么用不同深度而不是重复同一请求：
        同 query + 同参数会返回同一批结果，并行也拿不到新内容；advanced
        会做更深的抓取与重排，才真正扩大候选池。

        `safe` 在这里**不生效**：Tavily 没有 safe-search 参数，安全搜索由
        `service.search` 交给 `clean.filter_results` 在本地过滤；保留这个
        参数是为了与 Provider 协议一致。
        """
        if not self.api_key:
            raise SearchProviderError("未配置 Tavily API Key")
        total = max(1, min(int(max_results or 5), self._MAX_TOTAL))
        if total <= self._MAX_PER_REQUEST:
            return self._request(
                query, max_results=total, depth="basic", timeout=timeout
            )
        plans = [
            ("basic", self._MAX_PER_REQUEST),
            ("advanced", total - self._MAX_PER_REQUEST),
        ]
        results: list[SearchResult] = []
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=len(plans)) as pool:
            futures = [
                pool.submit(
                    self._request,
                    query,
                    max_results=count,
                    depth=depth,
                    timeout=timeout,
                )
                for depth, count in plans
            ]
            for future in futures:
                try:
                    results.extend(future.result())
                except Exception as exc:  # noqa: BLE001 - 单批失败不拖垮另一批
                    errors.append(exc)
        if not results and errors:
            first = errors[0]
            if isinstance(first, SearchProviderError):
                raise first
            raise SearchProviderError(str(first))
        return results
