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

    return [
        {
            "chunk_id": chunks[i].id,
            "chapter_no": chunks[i].chapter_no,
            "idx": chunks[i].idx,
            "score": round(fused[i], 6),
            "bm25": round(bm25[i], 6),
            "vector": round(vec_scores[i], 6),
            "text": chunks[i].text,
        }
        for i in top
    ]
