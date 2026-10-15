"""风格样例采样：从参考语料里挑几段原文，作为 few-shot 样例注入 user prompt。

## 为什么需要它

文风档案（`style/stylometry.py`）给模型的是**目标数字**（句长、对白占比、标点密度），
数字能验收「像不像」，但**教不会模型怎么写**——「句子怎么呼吸、段落怎么接、
什么时候突然用一个短句」这类东西只能靠原文 in-context 学习。

调研结论（写作蒸馏器 writing-dna-skill / 小说风格蒸馏器 writer-novel-style-distiller）：
每次生成前必须挑几篇与本次写作体裁、题材最接近的原文通读，原文用于校准
「分层产物描述不出来的句子的呼吸感与断句节奏」。

## 与 RAG 的分工

RAG（`hybrid_search`）按查询词检索**与当前剧情相关**的片段，目标是「找素材」。
本模块按结构位置均匀采样**与当前 project 体裁接近**的段落，目标是「学写法」。
两者目标不同，**绝不共用同一路检索**，且必须**排除 RAG 已命中的 chunk**——
同一段原文同时作为「相关剧情」和「风格样例」出现两次，模型会困惑。

## 采样策略

- **体裁接近优先**：优先从 genre 与 project.genre 匹配的语料里采样
- **均匀分布**：在全书范围内按 chunk idx 均匀分布，覆盖开头/中段/结尾不同节奏
- **段落边界完整**：每个样例是完整的自然段（以换行切），不截断句子
- **不进 system prompt**：样例每次生成现采样，内容随段号变化，进 system 会击穿缓存
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

#: 每本语料最多取几个样例（总量 = min(per_novel × 语料数, max_total)）
PER_NOVEL = 2
#: 样例总量上限（避免挤占生成空间）
MAX_TOTAL = 3
#: 单个样例最少字符数（太短撑不起段落编排分析）
MIN_SAMPLE_CHARS = 200
#: 单个样例最多字符数（太长会挤占生成空间，与 unit_chars 对齐的量级）
MAX_SAMPLE_CHARS = 600


def _paragraphs(text: str) -> list[str]:
    """按换行切自然段，丢弃空白段。"""
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


def _split_into_samples(text: str) -> list[str]:
    """把一段 chunk 文本切成若干「段落级样例」。

    每个样例 = 连续 1~2 个自然段拼接，长度落在
    [MIN_SAMPLE_CHARS, MAX_SAMPLE_CHARS] 区间内。
    过短的段落单独丢弃（撑不起段落编排分析）。
    """
    paras = _paragraphs(text)
    samples: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        if size and size + len(p) + 1 > MAX_SAMPLE_CHARS:
            joined = "\
".join(buf)
            if len(joined) >= MIN_SAMPLE_CHARS:
                samples.append(joined)
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 1
    if buf:
        joined = "\
".join(buf)
        if len(joined) >= MIN_SAMPLE_CHARS:
            samples.append(joined)
    return samples


def _genre_match(project_genre: str | None, novel_genre: str | None) -> bool:
    """粗粒度体裁匹配：完全相等或互相包含即算匹配。

    不做同义词归一（仙侠/修真/玄幻的等价关系需要词表），只做字面匹配——
    匹配不上就退化为均匀采样，不阻断。
    """
    if not project_genre or not novel_genre:
        return False
    a, b = str(project_genre).strip(), str(novel_genre).strip()
    return a == b or a in b or b in a


def _pick_evenly(items: list, n: int) -> list:
    """从列表里均匀取 n 个（首尾必取，中间均匀分布）。"""
    if not items or n <= 0:
        return []
    if len(items) <= n:
        return list(items)
    # 均匀取 n 个：步长 = (len-1)/(n-1)，取整后去重保序
    step = (len(items) - 1) / max(n - 1, 1)
    picks = sorted({round(i * step) for i in range(n)})
    return [items[i] for i in picks[:n]]


def sample_style_exemplars(
    db: Session,
    project,
    novel_ids: list[int],
    exclude_texts: set[str] | None = None,
    per_novel: int = PER_NOVEL,
    max_total: int = MAX_TOTAL,
) -> list[dict]:
    """从参考语料采样风格样例。

    返回 [{novel_id, novel_title, genre, text, chunk_idx}, ...]，按 novel_id 排序。
    失败（语料不存在/无段落/全被排除）返回空列表，**不抛异常**——样例是增强，
    不是生成的前提，绝不能让整次生成失败。
    """
    from app.models import NovelChunk, RawNovel

    exclude_texts = exclude_texts or set()
    project_genre = getattr(project, "genre", None)
    results: list[dict] = []

    # 体裁接近的语料排前面（用户可能选多本，优先取最像的）
    novels = []
    for nid in novel_ids:
        novel = db.get(RawNovel, nid)
        if novel is None:
            continue
        match = _genre_match(project_genre, getattr(novel, "genre", None))
        novels.append((match, nid, novel))
    novels.sort(key=lambda t: (not t[0], t[1]))  # 匹配的在前，同优先级按 id

    # 每本均匀取 per_novel 段，总量不超过 max_total
    budget = max_total
    for _match, nid, novel in novels:
        if budget <= 0:
            break
        try:
            rows = db.scalars(
                select(NovelChunk)
                .where(NovelChunk.novel_id == nid)
                .order_by(NovelChunk.idx)
            ).all()
        except Exception:  # noqa: BLE001 —— 单本失败不阻断其余语料
            logging.getLogger(__name__).warning("风格样例采样失败 novel_id=%s", nid)
            continue

        # 收集所有 chunk 里的段落级样例（排除已被 RAG 命中的）
        pool: list[tuple[int, str]] = []  # (chunk_idx, sample_text)
        for row in rows:
            text = (row.text or "").strip()
            if not text:
                continue
            for s in _split_into_samples(text):
                if s in exclude_texts:
                    continue
                pool.append((row.idx, s))

        if not pool:
            continue

        take = min(per_novel, budget)
        picked = _pick_evenly(pool, take)
        for idx, s in picked:
            results.append({
                "novel_id": nid,
                "novel_title": novel.title or f"novel:{nid}",
                "genre": getattr(novel, "genre", None),
                "text": s,
                "chunk_idx": idx,
            })
        budget -= len(picked)

    return results
