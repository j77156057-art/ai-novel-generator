"""章长统计：这位作者**一章写多少字**（纯量层）。

## 为什么要有这一层

主人的要求里点名了「作者习惯一个章节写多长」—— 而这件事仓库里**从来没量过**：

- `stylometry` 的 33 项特征全是「**单元内**」的统计（句长、段长、密度…），
  单元是 3000 字左右的切片，**跨章**；
- 生成端只有项目配置里的 `generation_length`（默认 3000），那是**用户拍脑袋定的**，
  与参考书没有关系。

⇒ 于是「写手一章该写多少字」这件事，今天只能靠感觉，或者靠用户在配置里手填。

## 口径

- 单位 = **汉字**（与 `stylometry.cjk_len` 同一口径）。
- 只统计**正文**：太短的章（默认 < 200 汉字）不进分布 ——
  版权页残留、作者有话说、完本感言会把分布的左端拉出一条假尾巴。
- 分布用 p10 / p50 / p90：给模型的是「落在哪一带算合格」，
  和 `stylometry.band_check` 用的是同一对分位点。

## 为什么不做「章」以外的聚合

章是**作者自己的分节单位**（`parse_txt` 按「第X章/回/卷/篇」切），
段、节都是我们切出来的 ⇒ 只有章长能直接回答「这本书一章多长」。
"""
from __future__ import annotations

import statistics
from typing import Iterable, Sequence

from app.style.stylometry import cjk_len

#: 一章至少这么多汉字才参与分布统计（太短的章画像不稳）。
DEFAULT_MIN_CHAPTER_CHARS = 200


def chapter_lengths(
    chapters: Iterable[str],
    *,
    min_chars: int = DEFAULT_MIN_CHAPTER_CHARS,
) -> list[int]:
    """一组章节正文 → 每章的汉字数（过滤掉太短的章）。"""
    out: list[int] = []
    for text in chapters or []:
        n = cjk_len(text or "")
        if n >= min_chars:
            out.append(n)
    return out


def chapter_length_stats(
    lengths: Sequence[int],
    *,
    label: str = "",
) -> dict:
    """章长分布（纯量层）：p10 / p50 / p90 / mean / min / max + 章数。

    没有样本时返回空 dict（**不返回 0**）—— 0 会被下游当成「这本书一章 0 字」，
    而真相是「没有可用章节」。这两种情况必须分得开。
    """
    vals = [int(n) for n in (lengths or []) if int(n) > 0]
    if not vals:
        return {}
    vals_sorted = sorted(vals)
    return {
        "label": label,
        "n_chapters": len(vals),
        "p10": float(_quantile(vals_sorted, 0.10)),
        "p50": float(_quantile(vals_sorted, 0.50)),
        "p90": float(_quantile(vals_sorted, 0.90)),
        "mean": float(statistics.fmean(vals)),
        "min": float(vals_sorted[0]),
        "max": float(vals_sorted[-1]),
    }


def _quantile(sorted_vals: Sequence[int], q: float) -> float:
    """分位数（与 `stylometry._quantile` 同口径：线性插值）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def load_chapter_lengths(db, novel_ids: Iterable[int]) -> list[int]:
    """从库里取若干本参考书的**每章汉字数**。

    数据源是 `novel_chunks`（入库时已按章切好、每块带 `chapter_no`）：
    按 `chapter_no` 聚合成章，再量汉字数。

    为什么走 chunk 而不是 `raw_novels.source_chapters`：
    chunk 是**清洗后**入库的正文（去掉了完本感言、跨书简介、水印），
    与生成链路实际读到的语料是同一份 ⇒ 统计出来的章长与「模型看到的东西」一致。
    """
    from sqlalchemy import select

    from app.models import NovelChunk

    ids = [int(i) for i in (novel_ids or [])]
    if not ids:
        return []
    rows = db.scalars(
        select(NovelChunk).where(NovelChunk.novel_id.in_(ids)).order_by(
            NovelChunk.novel_id, NovelChunk.chapter_no, NovelChunk.idx
        )
    ).all()
    by_book: dict[int, dict[int, list[str]]] = {}
    for row in rows:
        key = int(row.novel_id)
        chap = row.chapter_no
        ch = int(chap) if chap is not None else 0
        by_book.setdefault(key, {}).setdefault(ch, []).append(row.text or "")
    lengths: list[int] = []
    for key in sorted(by_book):
        lengths.extend(chapter_lengths(by_book[key].values()))
    return lengths
