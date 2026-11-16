"""正文级照搬检测：生成出来的正文 vs 参考语料原文。

## 为什么要有这道闸门

项目里已有两处「照搬」防线，但都管不到正文：

- `generation/plot_structure.py` 的 `copy_check` 管**规划节点 vs 参考规则**；
- `agents/ingest_clean.py` 的水印检测管**语料清洗**（剔盗版站 SEO 串）。

**正文**这一段是空的，而这不是理论风险 —— 项目自己踩过：

    v2.272 五章实测证据：5 章全在重写王府迎接道人那一段，第 4/5 段前 3 句几乎逐字相同。

模型会把语料里的整段原话复读进正文，系统照收不误。

## 判据

与语料共享一段**连续 ≥25 字**的逐字片段 = 照搬。

25 不是拍的，是 2026-10-26 在真实语料（5,323 段 / 498 万字）上标定的：

| 对象 | 与语料的最长公共串 | 那一段是什么 |
|---|---|---|
| 真实生产产物（自创故事，13,722 字） | 9 字 | 纯标点 + 套话 |
| 同书续写（10,289 字） | 10 字 | 一个人名 + 一个动作 |
| 本轮实测 25 篇（83,390 字） | 11 字 | 《千字文》开篇 |
| **基准：与语料毫无关系的真书（987,101 字）** | **15 字** | 老子的话 |

**跟语料毫无关系的另一本书，和语料的最长重合（15 字）反而比我们生成的正文更长。**
这是「中文共用地毯」的实测上限 ⇒ 25 字远高于它，判得死，够得上 error 级。
12~24 字只告警不拦截（误报会堵死真活）。

## 尺的三处坑（写在这里，免得后人重踩）

1. **两侧必须先归一化空白**。语料是清洗过的（去空行、去站点广告），
   生成文本有自己的换行缩进；不归一化会**系统性漏报**
   （实测：连「语料自己跟自己」都测不出重合）。
2. **n-gram 只能定位候选，不能当结论**。连续的 12-gram 全命中，可能来自语料里
   **不同位置**的片段（误拼接）。实测：同一部书的另一份来源 vs 语料，
   12-gram 命中 99.5% 但整段在语料里找不到（两份来源是同章不同版本）。
   ⇒ 必须**逐字向两侧延伸**，只认真正连续的那一段。
3. **报「0」之前要把 n-gram 下限降下来再量一遍**。只用 12-gram 索引时量出来
   全是 0 —— 那不是「一个字都不重合」，是「最长也不到 12 字」。

## 算法：纯 Python（项目刻意不引入 numpy）

DP 是 498 万 × 3000 ≈ 150 亿格，跑不动。改成：

- **建索引（离线，一次）**：语料归一化 → 逐位置取 12 字窗口算 `zlib.crc32`，
  与位置打包成 64 位整数 `(hash << 32) | position`，塞进 `array('Q')` 后整体排序。
- **扫描（一段正文一次）**：正文每个位置取 25 字窗口 → CRC32 → 二分找候选 →
  逐字复核 → 命中就向两侧延伸（锚点式最长公共串）。

CRC32 只有 32 位、有碰撞可能，所以命中后**逐字复核**才算数 ——
碰撞只会多查一次，不会误判。`zlib.crc32` 与 `bisect` 都是 C 实现，
单次探测 ~3µs，一段 3000 字正文 ≈ 10ms，**一次都不调用 LLM**。

## 索引缓存

建索引要扫全语料（498 万字 ⇒ 几十秒），不能每段生成都做。

- 缓存键 = **语料指纹** = `(novel_ids, chunk 条数, text 总长度, 最大 chunk id)` 的 sha1。
  指纹只依赖 SQL 聚合结果，**不用把 498 万字读出来**。
- 两级缓存：进程内 LRU（4 份）+ 落盘 `.cache/verbatim/<fingerprint>.vbi`。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import struct
import threading
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable, Sequence
from zlib import crc32

_log = logging.getLogger(__name__)

#: 候选定位用的 n-gram 长度。
NGRAM = 12

#: 判定为「照搬」的最小连续逐字长度（实测标定，见模块 docstring）。
DEFAULT_THRESHOLD = 25

#: 只告警不拦截的下限（中文共用地毯实测 ≤15 字）。
WARN_THRESHOLD = 12

#: 单次最多报几处。
MAX_REPORT = 20

#: 落盘索引的 magic。
_MAGIC = b"VBI1"

#: 一个 n-gram 在语料里最多试几个候选位置（CRC32 撞了要逐字复核）。
MAX_CANDIDATES = 64

#: 进程内缓存保留几份。
_CACHE_MAX = 4

_INDEX_CACHE: "OrderedDict[str, VerbatimIndex]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def normalize_for_match(text: str) -> str:
    """归一化：**去掉所有空白**。

    不做这一步会系统性漏报 —— 语料是清洗过的（去空行、去站点广告），
    生成文本有自己的换行与缩进，逐字符比对根本对不齐。
    """
    return re.sub(r"\s+", "", text or "")


class VerbatimIndex:
    """语料的 n-gram 索引（可落盘缓存）。

    只做一件事：给定一段文本，找出它与语料共享的**连续逐字片段**。
    """

    __slots__ = ("corpus", "n", "_table")

    def __init__(self, corpus: str, table: array, n: int = NGRAM):
        self.corpus = corpus
        self.n = n
        self._table = table

    # —— 建索引 ——————————————————————————————————————————————

    @classmethod
    def from_chunks(cls, chunks: Iterable[str], n: int = NGRAM) -> "VerbatimIndex":
        corpus = normalize_for_match("\n".join(c or "" for c in chunks))
        return cls.build(corpus, n)

    @classmethod
    def build(cls, corpus: str, n: int = NGRAM) -> "VerbatimIndex":
        """扫全语料建索引。

        纯 Python 扫 498 万字要几十秒 —— 所以只该在**离线/后台**调（见
        `warm_index`），不要在请求里同步做。
        """
        table = array("Q")
        append = table.append
        for i in range(len(corpus) - n + 1):
            append((crc32(corpus[i:i + n].encode("utf-8")) << 32) | i)
        table = array("Q", sorted(table))
        return cls(corpus, table, n)

    # —— 落盘 ——————————————————————————————————————————————

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.corpus.encode("utf-8")
        head = _MAGIC + struct.pack("<IIQI", self.n, len(data), len(self._table), 0)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(head)
            fh.write(data)
            fh.write(self._table.tobytes())
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "VerbatimIndex":
        with open(path, "rb") as fh:
            blob = fh.read()
        if blob[:4] != _MAGIC:
            raise ValueError("不是照搬索引文件")
        n, dlen, tlen, _ = struct.unpack("<IIQI", blob[4:24])
        data = blob[24:24 + dlen]
        table = array("Q")
        table.frombytes(blob[24 + dlen:24 + dlen + tlen * 8])
        return cls(data.decode("utf-8"), table, n)

    # —— 查询 ——————————————————————————————————————————————

    def find_spans(
        self,
        text: str,
        threshold: int = DEFAULT_THRESHOLD,
        limit: int = MAX_REPORT,
    ) -> list[dict[str, Any]]:
        """找出与语料共享的连续逐字片段（长度 ≥ threshold）。

        返回的每条：`{text, start, length, corpus_pos}`，
        `text` 是**归一化之后**的片段（空白已去）。

        ## 两步：先用索引自身的 n-gram 定位，再逐字延伸

        索引是按 `self.n`（12 字）建的，所以查询也必须用 12 字去查 ——
        拿 25 字窗口去查 12-gram 索引，哈希对不上，**一条都查不到**（踩过）。
        延伸完之后再按 threshold 筛：延伸出来的才是真正连续的那一段。
        """
        t = normalize_for_match(text)
        n = len(t)
        g = self.n
        if n < g or not self._table:
            return []
        corpus = self.corpus
        table = self._table
        spans: list[dict[str, Any]] = []
        i = 0
        while i + g <= n and len(spans) < limit:
            needle = t[i:i + g]
            head = crc32(needle.encode("utf-8")) << 32
            lo = bisect_left(table, head)
            hi = bisect_right(table, head | 0xFFFFFFFF)
            if lo >= hi:
                i += 1
                continue
            best: tuple[int, int, int] | None = None   # (长度, 文本起点, 语料起点)
            for k in range(lo, min(hi, lo + MAX_CANDIDATES)):
                p = table[k] & 0xFFFFFFFF
                if corpus[p:p + g] != needle:
                    continue           # CRC32 撞了 —— 逐字复核才算数
                ts, cs = i, p
                while ts > 0 and cs > 0 and t[ts - 1] == corpus[cs - 1]:
                    ts -= 1
                    cs -= 1
                te, ce = i + g, p + g
                while te < n and ce < len(corpus) and t[te] == corpus[ce]:
                    te += 1
                    ce += 1
                if best is None or (te - ts) > best[0]:
                    best = (te - ts, ts, cs)
            if best is None:
                i += 1
                continue
            length, ts, cs = best
            if length >= threshold:
                spans.append({
                    "text": t[ts:ts + length],
                    "start": ts,
                    "length": length,
                    "corpus_pos": cs,
                })
            # 跳过这一段（不管达没达到阈值）—— 同一段的后续锚点延伸出来还是它。
            i = ts + length
        return spans

    def longest_span(self, text: str, threshold: int = 1) -> int:
        """最长公共片段有多长（诊断用；threshold=1 时量的是任意长度的重合）。"""
        spans = self.find_spans(text, threshold=threshold, limit=MAX_REPORT)
        return max((s["length"] for s in spans), default=0)


# —— 指纹与缓存 ————————————————————————————————————————————

def _cache_dir() -> Path:
    d = os.environ.get("VERBATIM_CACHE_DIR")
    if d:
        return Path(d)
    return Path(__file__).resolve().parents[3] / ".cache" / "verbatim"


def corpus_fingerprint(db, novel_ids: Sequence[int]) -> str | None:
    """语料指纹（只查聚合，不读正文）。

    指纹变了 = 语料变了 = 索引要重建。
    """
    ids = sorted({int(i) for i in (novel_ids or []) if i is not None})
    if not ids:
        return None
    try:
        from sqlalchemy import func

        from app.models import NovelChunk

        count, total, max_id = db.query(
            func.count(NovelChunk.id),
            func.coalesce(func.sum(func.length(NovelChunk.text)), 0),
            func.coalesce(func.max(NovelChunk.id), 0),
        ).filter(NovelChunk.novel_id.in_(ids)).one()
    except Exception as exc:  # noqa: BLE001 —— 取不到指纹就当没有语料
        _log.warning("照搬索引：语料指纹取不到 %s", exc)
        return None
    if not count:
        return None
    raw = f"{ids}|{count}|{total}|{max_id}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _remember(key: str, index: "VerbatimIndex") -> None:
    with _CACHE_LOCK:
        _INDEX_CACHE[key] = index
        _INDEX_CACHE.move_to_end(key)
        while len(_INDEX_CACHE) > _CACHE_MAX:
            _INDEX_CACHE.popitem(last=False)


def load_index(db, novel_ids: Sequence[int]) -> VerbatimIndex | None:
    """取现成的索引（进程内 / 落盘）。**没有就返回 None，不现场建**。

    现场建索引要扫全语料（498 万字 ⇒ 几十秒），放在请求里会把生成卡住。
    建索引交给 `warm_index`（入库后或后台线程）。
    """
    fp = corpus_fingerprint(db, novel_ids)
    if not fp:
        return None
    with _CACHE_LOCK:
        hit = _INDEX_CACHE.get(fp)
        if hit is not None:
            _INDEX_CACHE.move_to_end(fp)
            return hit
    path = _cache_dir() / f"{fp}.vbi"
    if path.exists():
        try:
            index = VerbatimIndex.load(path)
        except Exception as exc:  # noqa: BLE001 —— 坏文件就重建
            _log.warning("照搬索引读失败（将重建）: %s", exc)
            index = None
        if index is not None:
            _remember(fp, index)
            return index
    return None


def build_index(db, novel_ids: Sequence[int]) -> VerbatimIndex | None:
    """**同步**建索引并落盘（慢；只该在入库后或后台调）。"""
    from app.models import NovelChunk

    ids = sorted({int(i) for i in (novel_ids or []) if i is not None})
    if not ids:
        return None
    fp = corpus_fingerprint(db, ids)
    if not fp:
        return None
    with _CACHE_LOCK:
        hit = _INDEX_CACHE.get(fp)
        if hit is not None:
            return hit
    rows = (
        db.query(NovelChunk.text)
        .filter(NovelChunk.novel_id.in_(ids))
        .order_by(NovelChunk.novel_id, NovelChunk.idx)
        .all()
    )
    if not rows:
        return None
    index = VerbatimIndex.from_chunks(r[0] for r in rows)
    try:
        index.save(_cache_dir() / f"{fp}.vbi")
    except OSError as exc:
        _log.warning("照搬索引落盘失败（不影响使用）: %s", exc)
    _remember(fp, index)
    return index


def warm_index(db, novel_ids: Sequence[int]) -> VerbatimIndex | None:
    """确保索引可用：没有就建（**同步**）。

    入库完成后调一次，用户第一次生成时就不会卡在「建索引」。
    """
    existing = load_index(db, novel_ids)
    if existing is not None:
        return existing
    try:
        return build_index(db, novel_ids)
    except Exception as exc:  # noqa: BLE001 —— 建索引失败不影响入库
        _log.warning("照搬索引构建失败: %s", exc)
        return None


def warm_index_async(db, novel_ids: Sequence[int]) -> None:
    """后台线程建索引（避免把请求卡住）。

    期间门禁**放行**（降级）—— 宁可漏判，不堵死真活。

    ## 线程里必须**自己开一个 session**

    请求的 session 不是线程安全的，而且请求一结束它就被关掉了 ——
    在后台线程里用它会踩到「session already closed」。
    沿用 `engine._schedule_async_audit_kg` 的做法：`SessionLocal()` 自开自关。
    """
    if load_index(db, novel_ids) is not None:
        return

    def _worker():
        s = None
        try:
            from app.db import SessionLocal

            s = SessionLocal()
            build_index(s, novel_ids)
        except Exception as exc:  # noqa: BLE001
            _log.warning("照搬索引后台构建失败: %s", exc)
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:  # noqa: BLE001
                    pass

    threading.Thread(target=_worker, name="verbatim-index", daemon=True).start()


# —— 闸门 ————————————————————————————————————————————————————

class VerbatimViolation(Exception):
    """生成内容把参考语料里的原文**逐字搬了过来**。

    与 `plot_structure.CopyViolation` 同一口径：它是**可重试**的失败 ——
    把命中的片段回灌给模型让它自己改，比直接报错让用户重做一次改对的概率高得多。
    """

    #: 失败分类 —— 调用方据此决定错误码。
    code = "possible_copy"

    def __init__(self, report: dict[str, Any]):
        self.report = report
        self.spans = list(report.get("spans") or [])
        self.threshold = int(report.get("threshold") or DEFAULT_THRESHOLD)
        self.detail = f"possible_copy: {len(self.spans)} 处与参考语料逐字相同"
        super().__init__(self.detail)
        self.retry_hint = self._build_hint(self.spans, self.threshold)

    @staticmethod
    def _build_hint(spans: list[dict[str, Any]], threshold: int = DEFAULT_THRESHOLD) -> str:
        lines = [f"  - 「{s.get('text') or ''}」" for s in spans[:5]]
        return (
            "\n\n—— 纠正照搬问题 ——\n"
            "你上一次的输出被丢弃了：它把参考语料里的原句**逐字搬了过来**。\n"
            "请把下面这些片段改写成**你自己的措辞**（保留情节与信息，"
            "换掉句子、换掉用词、换掉句子顺序都可以），不要出现任何连续 "
            f"{threshold} 个字以上与参考原文相同的片段：\n"
            + "\n".join(lines)
            + "\n改完后重新输出整段。"
        )


def scan_verbatim(
    text: str,
    index: VerbatimIndex | None,
    *,
    threshold: int = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """扫正文，返回判定报告。

    一次扫描出**两档**：
    - `spans`：**≥threshold**（默认 25 字）—— 照搬，拦；
    - `warn_spans`：12~threshold-1 字 —— 中文共用地毯，**只告警不拦**。

    `index` 为 None（没有语料 / 索引没建好）⇒ **放行**，并在报告里标注
    「未执行」（不静默：没查 ≠ 没问题）。
    """
    if index is None:
        return {
            "verdict": "skipped",
            "reason": "no_index",
            "threshold": threshold,
            "spans": [],
            "match_count": 0,
            "warn_spans": [],
            "warn_count": 0,
        }
    # 一次扫到下限（12 字），再按阈值分成两档 —— 扫两遍更贵，且没有必要。
    found = index.find_spans(text, threshold=WARN_THRESHOLD)
    hard = [s for s in found if s["length"] >= threshold]
    warn = [s for s in found if s["length"] < threshold]
    verdict = "possible_copy" if hard else ("warning" if warn else "clean")
    return {
        "verdict": verdict,
        "threshold": threshold,
        "spans": hard[:MAX_REPORT],
        "match_count": len(hard),
        "warn_spans": warn[:MAX_REPORT],
        "warn_count": len(warn),
    }
