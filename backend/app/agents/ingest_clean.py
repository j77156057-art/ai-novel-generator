"""投放清洗的体检报告（批次 1：只搬「混书体检」这一块）。

main 侧这个模块还包含 `preview_ingest_clean` / `check_ingest_clean` 两个工具与
更完整的清洗复核流水线，属于台账第 6 批；本次只搬入库链路立刻要用的部分：

- `fingerprint`：清洗文本的短指纹，用于「报告是不是针对同一份文本」的复核；
- `detect_foreign_blocks_report`：把 `corpus_health.find_foreign_blocks` 的结果
  压成可存库、可下发的报告（**只报不删**）。

纪律：本模块不读库、不写库、不改文本；剔除动作由 `ingestion/store.py` 在
调用方显式开关下执行。
"""
from __future__ import annotations

import hashlib

from app.corpus_health import find_foreign_blocks


def fingerprint(text: str) -> str:
    """清洗文本的短指纹（16 位十六进制）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def detect_foreign_blocks_report(text: str, blocks: list[dict] | None = None) -> dict:
    """混书体检报告：检测 + 压成可存库的形状（**不改文本**）。

    `blocks` 允许调用方传入已算好的 `find_foreign_blocks` 结果（入库链路要
    先拿块做剔除，再出报告），避免对同一份百万字文本跑两遍。

    返回字段：
    - `detected`：有没有检出混入；
    - `block_count` / `chars` / `ratio`：块数、总字数、占全书比例；
    - `blocks`：每块的区间、章号、独占词与样本（供人复核）；
    - `text_fingerprint`：被检测文本的指纹（复核「这份结论对应哪份原文」）。
    """
    if blocks is None:
        blocks = find_foreign_blocks(text or "")
    if not blocks:
        return {"detected": False, "text_fingerprint": fingerprint(text)}
    total_chars = sum(int(b.get("chars") or 0) for b in blocks)
    return {
        "detected": True,
        "block_count": len(blocks),
        "chars": total_chars,
        "ratio": round(total_chars / max(len(text or ""), 1), 6),
        "blocks": [
            {
                "start": b.get("start"),
                "end": b.get("end"),
                "chars": b.get("chars"),
                "chapter_count": len(b.get("chapters") or []),
                "chapters": (b.get("chapters") or [])[:12],
                "chapter_titles": (b.get("chapter_titles") or [])[:6],
                "terms": [t.get("term") for t in (b.get("terms") or [])][:8],
                "exclusive_ratio": b.get("exclusive_ratio"),
                "sample": (b.get("sample") or "")[:120],
            }
            for b in blocks
        ],
        "text_fingerprint": fingerprint(text),
    }
