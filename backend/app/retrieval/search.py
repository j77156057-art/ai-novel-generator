"""混合检索：BM25（关键词） + 向量（语义） → RRF 融合排序。

MVP 实现：向量相似度在 Python 内对单本小说的分片计算（语料规模可控）。
若后续语料膨胀，应将向量检索下推到 PG 的 `<=>` 算子（见 README 的 M2-later）。
"""

import json
import math

from sqlalchemy import select

from app.embeddings.provider import get_embedding_provider
from app.models import NovelChunk
from app.retrieval.bm25 import bm25_scores
from app.ingestion.clean import clean_consumption_text
from app.retrieval.views import chunk_view, prepare_hits


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _parse_embedding(raw):
    if raw is None:
        return None
    if isinstance(raw, str):  # sqlite 退化为 JSON 字符串
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw  # pgvector：已是 list


def _rrf(ranked_idx: list[int], k: int = 60) -> dict[int, float]:
    score: dict[int, float] = {}
    for rank, idx in enumerate(ranked_idx):
        score[idx] = score.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return score


def hybrid_search(db, novel_id: int, query: str, k: int = 10) -> list[dict]:
    """对单本小说做混合检索，返回前 k 个分片（含融合分与双路分数）。"""
    chunks = db.scalars(
        select(NovelChunk).where(NovelChunk.novel_id == novel_id)
    ).all()
    if not chunks:
        return []

    bm25 = bm25_scores(query, [c.text for c in chunks])

    vec_scores = [0.0] * len(chunks)
    try:
        provider = get_embedding_provider()
        qvec = provider.embed([query])[0]
        for i, c in enumerate(chunks):
            emb = _parse_embedding(c.embedding)
            if emb is not None:
                vec_scores[i] = _cosine(qvec, emb)
    except Exception:  # noqa: BLE001 - 无嵌入则退化为纯 BM25
        pass

    bm25_rank = sorted(range(len(chunks)), key=lambda i: -bm25[i])
    vec_rank = sorted(range(len(chunks)), key=lambda i: -vec_scores[i])
    fused = {
        i: _rrf(bm25_rank).get(i, 0.0) + _rrf(vec_rank).get(i, 0.0)
        for i in range(len(chunks))
    }
    top = sorted(fused.keys(), key=lambda i: -fused[i])[:k]

    return prepare_hits([
        {
            "chunk_id": chunks[i].id,
            "chapter_no": chunks[i].chapter_no,
            "idx": chunks[i].idx,
            "score": round(fused[i], 6),
            "bm25": round(bm25[i], 6),
            "vector": round(vec_scores[i], 6),
            **chunk_view(chunks[i]),
        }
        for i in top
    ])

def weighted_search(
    db, novel_id: int, query: str, k: int = 6, w_bm25: float = 0.75,
) -> list[dict]:
    """混合检索：BM25 与向量按**归一化分数线性融合**。

    ## 为什么不用 RRF（`hybrid_search`）

    RRF 只按排名给分（`1/(60+rank)`），把绝对分数丢掉了。实测同一查询下：

        第262章  bm25=24.70  vector=0.4765  →  RRF 0.03252
        第966章  bm25=17.58  vector=0.4989  →  RRF 0.03068

    BM25 的 24.7 与 17.6 相差 40%，融合后只差 6%。区分度被压平，擦边的块
    和真正相关的块排在几乎一样的位置。更糟的是两路 top1 常常不是同一块，
    各得一半权重，把相关性高的那一块压下去。

    归一化线性融合保留分数比例：`bm25/max(bm25) × w + vector/max(vector) × (1-w)`。

    ## 权重默认 0.75

    中文关键词匹配在「找某个词/某个人出现在哪」这类需求上远强于本地
    embedding。实测按人名检索时，向量版每次都把全书最长的那一章（8 万字）
    排在第一位——长文档的 embedding 天然接近任何 query，这是已知偏置。
    所以以关键词为主，语义只做补充。

    ## 返回

    与 `hybrid_search` 同构，额外带 `bm25_n` / `vector_n`（归一化分），
    便于调用方判断这次是关键词命中还是语义命中。
    """
    chunks = db.scalars(
        select(NovelChunk).where(NovelChunk.novel_id == novel_id)
    ).all()
    if not chunks:
        return []

    bm25 = bm25_scores(query, [c.text for c in chunks])

    vec_scores = [0.0] * len(chunks)
    try:
        provider = get_embedding_provider()
        qvec = provider.embed([query])[0]
        for i, c in enumerate(chunks):
            emb = _parse_embedding(c.embedding)
            if emb is not None:
                vec_scores[i] = _cosine(qvec, emb)
    except Exception:  # noqa: BLE001 - 无嵌入则退化为纯 BM25
        pass

    max_b = max(bm25) if bm25 else 0.0
    max_v = max(vec_scores) if vec_scores else 0.0
    # 全零时不归一化（除零保护）：此时所有块同分，排序交给稳定顺序
    bm25_n = [b / max_b if max_b > 0 else 0.0 for b in bm25]
    vec_n = [v / max_v if max_v > 0 else 0.0 for v in vec_scores]

    fused = [
        w_bm25 * bn + (1.0 - w_bm25) * vn
        for bn, vn in zip(bm25_n, vec_n)
    ]
    top = sorted(range(len(fused)), key=lambda i: -fused[i])[:k]

    return prepare_hits([
        {
            "chunk_id": chunks[i].id,
            "chapter_no": chunks[i].chapter_no,
            "idx": chunks[i].idx,
            "score": round(fused[i], 6),
            "bm25": round(bm25[i], 6),
            "vector": round(vec_scores[i], 6),
            "bm25_n": round(bm25_n[i], 4),
            "vector_n": round(vec_n[i], 4),
            **chunk_view(chunks[i]),
        }
        for i in top
    ])
