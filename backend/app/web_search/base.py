"""联网搜索的公共数据契约。

Provider 只负责把各家的返回结构转成 `SearchResult`，业务判断（是否需要搜、
查询怎么改写、缓存与限流）全部在 `service.py`。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class SearchProviderError(RuntimeError):
    """Provider 调用失败。调用方负责降级，不把异常抛给用户。"""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str | None = None
    score: float | None = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "published_at": self.published_at,
            "score": self.score,
        }


@dataclass(frozen=True)
class SearchBundle:
    query: str
    provider: str
    results: tuple[SearchResult, ...] = field(default_factory=tuple)
    cached: bool = False
    error: str | None = None
    fetched_at: str = ""
    #: 被清洗规则剔除的原始条数（登录墙 / 重复 / 同站超限 / 导流垃圾）。
    dropped: int = 0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "provider": self.provider,
            "results": [item.to_dict() for item in self.results],
            "cached": self.cached,
            "error": self.error,
            "fetched_at": self.fetched_at,
            "dropped": self.dropped,
        }


class SearchProvider(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        max_results: int,
        safe: bool = True,
        timeout: float = 8.0,
    ) -> list[SearchResult]:
        """执行一次搜索；失败抛 `SearchProviderError`。"""
