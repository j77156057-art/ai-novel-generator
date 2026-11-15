"""简报缓存：同一/近似查询不重复联网。

键 = sha256(brief_version | 归一化查询词 | 结果条数)，落盘为 JSON 文件：
    {cache_dir}/{key[:2]}/{key}.json
    {cache_dir}/index.json

- 键里不含项目 id / 书籍 id，所以同一台机器上换项目可以直接复用。
- `brief_version` 参与哈希：清洗规则或返回结构变化时递增版本即可整体失效。
- TTL 过期：`ttl_s > 0` 时按写入时间失效（0 = 永不过期）；版本号仍是硬失效手段。
- 近义查询（字符二元组 Jaccard ≥ 阈值）命中已有条目时直接复用，避免「同一件事
  换个说法再查一次」；命中时把相似度一并返回，供 `retrieval_logs` 留痕。
- 写入使用同目录临时文件 + os.replace，避免进程中断留下半截 JSON。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


def normalize_query(query: str) -> str:
    """归一化：去空白/标点、统一小写，只保留文字与数字。"""
    text = (query or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard；中文短查询上比编辑距离更稳定。"""
    if a == b:
        return 1.0
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def similarity_threshold(n_chars: int) -> float:
    """按归一化查询长度给出近义阈值下限（实际取 max(配置值, 本值)）。

    依据：n 个字有 n-1 个二元组，末尾多一个字时 Jaccard = (n-1)/n。
    n=13 时恰好是 0.923——「有哪些」会被并进「有哪些呢」，所以短查询只认
    归一化后的精确命中；查询越长，单个字的增删对语义影响越小，才逐步放宽。
    """
    if n_chars < 12:
        return 1.0
    if n_chars <= 24:
        return 0.95
    return 0.92



@dataclass(frozen=True)
class CacheHit:
    """一次缓存命中：`similarity=1.0` 为精确命中，小于 1 为近义命中。"""

    results: list[dict[str, Any]]
    similarity: float
    key: str


class BriefCache:
    def __init__(
        self,
        root: str | Path,
        version: str,
        *,
        similarity_threshold: float = 0.9,
        scan_limit: int = 200,
        ttl_s: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.version = version
        self.similarity_threshold = similarity_threshold
        self.scan_limit = max(1, scan_limit)
        self.ttl_s = max(0.0, float(ttl_s))
        self._clock = clock
        self._index: list[dict[str, Any]] | None = None

    # ---- 键与路径 ----
    def key(self, query: str, max_results: int) -> str:
        raw = f"{self.version}\x1f{normalize_query(query)}\x1f{max_results}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    # ---- 过期 ----
    def _expired(self, created_ts: float) -> bool:
        """`ttl_s <= 0` 或时间戳缺失（旧条目）时视为不过期。"""
        if self.ttl_s <= 0 or created_ts <= 0:
            return False
        return (self._clock() - created_ts) > self.ttl_s

    def _alive(self, item: dict[str, Any]) -> bool:
        return not self._expired(float(item.get("created_ts") or 0))

    # ---- 索引 ----
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> list[dict[str, Any]]:
        if self._index is not None:
            return self._index
        path = self._index_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._index = data if isinstance(data, list) else []
        except (OSError, ValueError):
            self._index = []
        return self._index

    def _save_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._index_path(), json.dumps(self._load_index(), ensure_ascii=False))

    # ---- 读写 ----
    def get(self, query: str, max_results: int) -> CacheHit | None:
        """精确命中优先；未命中再按近义扫索引。过期条目不返回。"""
        key = self.key(query, max_results)
        payload = self._read_payload(self._path(key))
        if payload is not None and self._alive(payload):
            results = payload.get("results")
            if isinstance(results, list):
                return CacheHit(results=results, similarity=1.0, key=key)

        norm = normalize_query(query)
        if not norm:
            return None
        # 实际阈值 = max(配置值, 长度下限)：短查询即使配置很松也不会被并错。
        threshold = max(self.similarity_threshold, similarity_threshold(len(norm)))
        # 近义：只看同版本、同条数、未过期的最近若干条索引。
        entries = [
            e
            for e in reversed(self._load_index()[-self.scan_limit :])
            if e.get("version") == self.version
            and e.get("max_results") == max_results
            and self._alive(e)
        ]
        best: tuple[float, str] | None = None
        for entry in entries:
            score = similarity(norm, str(entry.get("normalized") or ""))
            if score >= threshold and (best is None or score > best[0]):
                best = (score, str(entry.get("key") or ""))
        if best is None or not best[1]:
            return None
        payload = self._read_payload(self._path(best[1]))
        if payload is None or not self._alive(payload):
            return None
        results = payload.get("results")
        if not isinstance(results, list):
            return None
        return CacheHit(results=results, similarity=best[0], key=best[1])

    def put(self, query: str, max_results: int, results: list[dict[str, Any]]) -> None:
        key = self.key(query, max_results)
        created_ts = self._clock()
        payload = {
            "version": self.version,
            "query": query,
            "normalized": normalize_query(query),
            "max_results": max_results,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "created_ts": created_ts,
            "results": results,
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._path(key), json.dumps(payload, ensure_ascii=False))

        index = self._load_index()
        # 顺手剔除过期条目，避免索引无限增长后被过期数据占满扫描窗口。
        index[:] = [e for e in index if self._alive(e)]
        index.append(
            {
                "key": key,
                "version": self.version,
                "normalized": payload["normalized"],
                "max_results": max_results,
                "created_at": payload["created_at"],
                "created_ts": created_ts,
            }
        )
        self._save_index()

    def _read_payload(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
