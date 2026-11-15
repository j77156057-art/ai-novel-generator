"""素材库：检索结果先落库，再由模型挑选，未选中的留给后续章节。

与 `BriefCache` 同风格：单个 JSON 文件 + 原子写，不依赖数据库与迁移。
- 每条素材带入库时间、来源查询、原始标题与摘要，按内容哈希去重；
- 未使用条目受 TTL 与条数上限约束，超限按「最久未用 → 入库最旧」淘汰；
- `select()` 标记使用时间与次数，跨章复用时能看出它有多旧。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .cache import normalize_query

if TYPE_CHECKING:  # 仅用于注解，避免运行时循环导入
    from .backend import SearchResult


@dataclass
class MaterialEntry:
    id: str
    query: str
    title: str
    snippet: str
    ingested_at: float
    last_used_at: float = 0.0
    use_count: int = 0

    @property
    def age_days(self) -> int:
        return max(0, int((time.time() - self.ingested_at) // 86400))


def _entry_id(title: str, snippet: str) -> str:
    raw = f"{normalize_query(title)}\x1f{normalize_query(snippet)}"
    return "m" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def overlap_ratio(source: str, text: str) -> float:
    """source 的二元组有多大比例出现在 text 里（0~1），用于粗判是否被正文采纳。"""
    a, b = normalize_query(source), normalize_query(text)
    if not a or not b:
        return 0.0
    grams = _bigrams(a)
    if not grams:
        return 0.0
    return len(grams & _bigrams(b)) / len(grams)


class MaterialLibrary:
    def __init__(
        self,
        root: str | Path,
        *,
        ttl_s: float = 60 * 24 * 3600.0,
        max_items: int = 200,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.ttl_s = max(0.0, float(ttl_s))
        self.max_items = max(1, int(max_items))
        self._clock = clock
        self._entries: list[MaterialEntry] | None = None

    # ------------------------------------------------------------------ 存储
    def _path(self) -> Path:
        return self.root / "library.json"

    def _load(self) -> list[MaterialEntry]:
        if self._entries is not None:
            return self._entries
        try:
            data = json.loads(self._path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        raw = data.get("entries") if isinstance(data, dict) else None
        entries: list[MaterialEntry] = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    MaterialEntry(
                        id=str(item["id"]),
                        query=str(item.get("query") or ""),
                        title=str(item.get("title") or ""),
                        snippet=str(item.get("snippet") or ""),
                        ingested_at=float(item.get("ingested_at") or 0.0),
                        last_used_at=float(item.get("last_used_at") or 0.0),
                        use_count=int(item.get("use_count") or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._entries = entries
        return entries

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "entries": [asdict(e) for e in self._load()]}
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp, self._path())
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ------------------------------------------------------------------ 容量
    def _prune(self, entries: list[MaterialEntry]) -> list[MaterialEntry]:
        """先按 TTL 淘汰未使用条目，再按「最久未用 → 入库最旧」压到上限。"""
        now = self._clock()
        if self.ttl_s > 0:
            entries = [
                e
                for e in entries
                if e.use_count > 0 or (now - e.ingested_at) <= self.ttl_s
            ]
        if len(entries) > self.max_items:
            # 未用（use_count=0）优先淘汰；同组内先比最近使用时间，再比入库时间。
            ordered = sorted(
                entries,
                key=lambda e: (e.use_count > 0, e.last_used_at, e.ingested_at),
            )
            entries = ordered[-self.max_items :]
        return entries

    # ------------------------------------------------------------------ 对外
    def add(self, query: str, results: Iterable[SearchResult]) -> list[MaterialEntry]:
        """入库并返回实际新增的条目；相同内容只保留首次入库的那条。"""
        existing = {e.id: e for e in self._load()}
        now = self._clock()
        added: list[MaterialEntry] = []
        for item in results:
            title = str(getattr(item, "title", "") or "").strip()
            snippet = str(getattr(item, "snippet", "") or "").strip()
            if not title and not snippet:
                continue
            key = _entry_id(title, snippet)
            if key in existing:
                continue
            entry = MaterialEntry(
                id=key,
                query=query,
                title=title,
                snippet=snippet,
                ingested_at=now,
            )
            existing[key] = entry
            added.append(entry)
        entries = self._prune(list(existing.values()))
        self._entries = entries
        self._save()
        return added

    def candidates(self, limit: int) -> list[MaterialEntry]:
        """可用候选：未过期，按入库时间倒序（最新先给），最多 limit 条。"""
        live = self._prune(list(self._load()))
        self._entries = live
        ordered = sorted(live, key=lambda e: (e.ingested_at, e.id), reverse=True)
        return ordered[: max(0, limit)]

    def select(self, ids: Iterable[str]) -> list[MaterialEntry]:
        """标记选中条目为已使用；返回按传入顺序实际命中的条目。"""
        wanted = [str(i) for i in (ids or []) if str(i)]
        by_id = {e.id: e for e in self._load()}
        now = self._clock()
        picked: list[MaterialEntry] = []
        for key in wanted:
            entry = by_id.get(key)
            if entry is None:
                continue
            entry.last_used_at = now
            entry.use_count += 1
            picked.append(entry)
        if picked:
            self._entries = self._prune(list(by_id.values()))
            self._save()
        return picked

    def get(self, ids: Iterable[str]) -> list[MaterialEntry]:
        by_id = {e.id: e for e in self._load()}
        return [by_id[k] for k in (str(i) for i in ids) if k in by_id]

    def size(self) -> int:
        return len(self._load())

    def stats(self) -> dict[str, Any]:
        entries = self._load()
        return {
            "total": len(entries),
            "unused": sum(1 for e in entries if e.use_count == 0),
            "used": sum(1 for e in entries if e.use_count > 0),
        }
