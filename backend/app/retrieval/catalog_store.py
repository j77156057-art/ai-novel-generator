"""卡片索引的落盘与读取。

卡片是**派生索引**，不是第二份档案：不写回 `style_distills.style_narrative`，
只写 `backend/data/catalog/` 下的 JSON。删掉后可以从档案重建，所以这里不
做迁移、不做版本兼容——只保证 `distill_id + catalog_version` 是缓存键。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "v2"

_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "catalog"


class CatalogNotBuilt(RuntimeError):
    """卡表还没建。带上可直接复制的建卡命令，避免调用方猜。"""

    def __init__(self, distill_id: int) -> None:
        self.distill_id = distill_id
        super().__init__(
            f"distill_id={distill_id} 的卡片索引不存在；先运行 "
            f"`python -m app.retrieval.build_cards --distill-id {distill_id}`"
        )


def data_dir() -> Path:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DATA_DIR


def catalog_path(distill_id: int, *, versioned: bool = False,
                 catalog_version: str | None = None) -> Path:
    if versioned and catalog_version:
        return data_dir() / f"catalog_{distill_id}_{catalog_version}.json"
    return data_dir() / f"catalog_{distill_id}.json"


def skeleton_path(distill_id: int) -> Path:
    return data_dir() / f"skeleton_{distill_id}.json"


def card_id(dim: str, text: str) -> str:
    raw = f"{dim}\x00{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_catalog(payload: dict) -> Path:
    """写版本化文件 + 最新指针文件；返回最新指针路径。"""
    distill_id = int(payload["distill_id"])
    version = str(payload["catalog_version"])
    versioned = catalog_path(distill_id, versioned=True, catalog_version=version)
    write_json_atomic(versioned, payload)
    latest = catalog_path(distill_id)
    write_json_atomic(latest, payload)
    return latest


@dataclass
class Catalog:
    """进程内只读视图。索引按需构建一次，之后只读。"""

    data: dict
    cards: list[dict] = field(init=False)
    by_id: dict[str, dict] = field(init=False)
    discarded: dict[str, dict] = field(init=False)
    _indexes: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.cards = list(self.data.get("cards") or [])
        self.by_id = {str(c["id"]): c for c in self.cards}
        self.discarded = dict(self.data.get("discarded") or {})

    # ----- 基本属性 -----
    @property
    def distill_id(self) -> int:
        return int(self.data["distill_id"])

    @property
    def novel_id(self) -> int | None:
        value = self.data.get("novel_id")
        return int(value) if value is not None else None

    @property
    def catalog_version(self) -> str:
        return str(self.data.get("catalog_version") or "")

    @property
    def stats(self) -> dict:
        return dict(self.data.get("stats") or {})

    # ----- 索引 -----
    def _ensure_indexes(self) -> None:
        if "by_l1" in self._indexes:
            return
        by_l1: dict[str, list[int]] = defaultdict(list)
        by_l2: dict[str, list[int]] = defaultdict(list)
        by_l3: dict[str, list[int]] = defaultdict(list)
        by_l4: dict[str, list[int]] = defaultdict(list)
        by_path: dict[str, list[int]] = defaultdict(list)
        for i, card in enumerate(self.cards):
            path = card.get("path") or ""
            # 用卡片的结构化层级，不按 path 段位置：technique/global 的
            # `style/dialogue` 第 2 段是 L4，不是 L2。
            for level, bucket in (
                ("l1", by_l1), ("l2", by_l2), ("l3", by_l3), ("l4", by_l4),
            ):
                value = card.get(level)
                if value:
                    bucket[str(value)].append(i)
            by_path[path].append(i)
        self._indexes = {
            "by_l1": by_l1,
            "by_l2": by_l2,
            "by_l3": by_l3,
            "by_l4": by_l4,
            "by_path": by_path,
            "active": [c for c in self.cards if not c.get("deprecated")],
        }

    def by_l1(self) -> dict[str, list[int]]:
        self._ensure_indexes()
        return self._indexes["by_l1"]

    def by_l2(self) -> dict[str, list[int]]:
        self._ensure_indexes()
        return self._indexes["by_l2"]

    def by_l3(self) -> dict[str, list[int]]:
        self._ensure_indexes()
        return self._indexes["by_l3"]

    def by_l4(self) -> dict[str, list[int]]:
        self._ensure_indexes()
        return self._indexes["by_l4"]

    def by_path(self) -> dict[str, list[int]]:
        self._ensure_indexes()
        return self._indexes["by_path"]

    def path_counts(self) -> Counter[str]:
        return Counter({p: len(v) for p, v in self.by_path().items()})

    def find(self, card_id_: str) -> dict | None:
        # rule 回查必须能拿到 deprecated 卡；默认检索走下面 active_cards。
        return self.by_id.get(str(card_id_))

    @property
    def active_cards(self) -> list[dict]:
        """非 deprecated 卡：browse / search / similar / skeleton 的默认集合。"""
        self._ensure_indexes()
        return self._indexes["active"]

    def filter_cards(self, *, path: str | None = None,
                     l1: Iterable[str] | None = None,
                     include_deprecated: bool = False) -> list[dict]:
        """按路径前缀 / L1 过滤。给 search / browse / similar 共用，避免两套过滤。"""
        prefix = (path or "").strip().strip("/")
        wanted = {str(x) for x in (l1 or []) if str(x)}
        out: list[dict] = []
        source = self.cards if include_deprecated else self.active_cards
        for card in source:
            p = card.get("path") or ""
            if prefix and not (p == prefix or p.startswith(prefix + "/")):
                continue
            if wanted and p.split("/")[0] not in wanted:
                continue
            out.append(card)
        return out


#: 进程内缓存：同一个卡表被 browse/search 反复读，不重复解析 20MB JSON。
_CACHE: dict[tuple[str, int, int], Catalog] = {}
_CACHE_LOCK = threading.Lock()


def load_catalog(distill_id: int) -> Catalog:
    path = catalog_path(distill_id)
    if not path.exists():
        raise CatalogNotBuilt(distill_id)
    stat = path.stat()
    key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    started = time.perf_counter()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    catalog = Catalog(data)
    catalog.data.setdefault("stats", {})["load_seconds"] = round(
        time.perf_counter() - started, 3
    )
    with _CACHE_LOCK:
        # 只保留每个 distill 的最新一份，避免反复建卡把旧 JSON 全留在内存。
        for old_key in [k for k in _CACHE if k[0] == str(path)]:
            _CACHE.pop(old_key, None)
        _CACHE[key] = catalog
    return catalog


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
