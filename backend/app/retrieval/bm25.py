"""BM25 关键词检索（纯 Python，离线可用）。

用于混合检索的关键词分支；向量分支见 search.py。
分词复用 app.tokenize（MVP 占位，正式环境换 jieba）。
"""

import math

from app.tokenize import tokenize


def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """返回与 docs 等长的 BM25 分数列表。"""
    doc_tokens = [tokenize(d) for d in docs]
    q_tokens = tokenize(query)
    n = len(doc_tokens)
    if n == 0 or not q_tokens:
        return [0.0] * n

    df: dict[str, int] = {}
    lengths: list[int] = []
    for toks in doc_tokens:
        lengths.append(len(toks))
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    avgdl = sum(lengths) / n

    scores: list[float] = []
    for toks, dl in zip(doc_tokens, lengths):
        freq: dict[str, int] = {}
        for t in toks:
            freq[t] = freq.get(t, 0) + 1
        score = 0.0
        for qt in q_tokens:
            f = freq.get(qt, 0)
            if f == 0 or qt not in df:
                continue
            idf = math.log((n - df[qt] + 0.5) / (df[qt] + 0.5) + 1)
            score += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(score)
    return scores
