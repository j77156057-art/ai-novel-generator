"""入库：解析 → 切分 → 填 embedding → 写 raw_novels + novel_chunks。"""

import json

from sqlalchemy.orm import Session

from app.config import settings
from app.db import IS_SQLITE
from app.embeddings.provider import get_embedding_provider
from app.ingestion.chunk import chunk_text
from app.ingestion.parse_txt import parse_txt
from app.models import NovelChunk, RawNovel


def _emb_value(vec: list[float] | None):
    """sqlite 退化为 JSON 字符串；PG 直接存 list（pgvector 接受）。"""
    if vec is None:
        return None
    return json.dumps(vec) if IS_SQLITE else vec


def ingest_text(
    db: Session,
    text: str,
    title: str | None = None,
    platform: str | None = None,
    source_url: str | None = None,
    chapter_meta: list[dict] | None = None,
    commit_batch: int = 200,
) -> dict:
    """解析 → 切分 → 填 embedding → 写 raw_novels + novel_chunks。

    commit_batch：每累计多少分片提交一次。大部头（百万字级语料）在单个事务里
    攒几千个待写对象会触发 MemoryError（SQLAlchemy identity map 持有全部
    ORM 实例直到 commit），故按批提交；SQLAlchemy flush 后对象已转为
    persistent，expire 掉引用即可释放。
    """
    novel = RawNovel(
        title=title, platform=platform, source_url=source_url, status="imported"
    )
    db.add(novel)
    db.flush()

    chapters = chapter_meta or parse_txt(content=text)
    pieces: list[tuple[int | None, int, str]] = []
    for ch in chapters:
        for i, piece in enumerate(
            chunk_text(ch["body"], settings.chunk_size, settings.chunk_overlap)
        ):
            pieces.append((ch.get("chapter_no"), i, piece))

    # 批量 embedding（一次请求多段，省 token/延迟）
    embeddings: list[list[float] | None] = [None] * len(pieces)
    if pieces:
        try:
            vecs = get_embedding_provider().embed([p[2] for p in pieces])
            embeddings = vecs
        except Exception as exc:  # noqa: BLE001 - 嵌入失败不阻断入库
            print(f"[warn] embedding 失败，跳过向量填充：{exc}")

    for idx, ((chapter_no, i, piece), vec) in enumerate(
        zip(pieces, embeddings), start=1
    ):
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=chapter_no,
                idx=i,
                text=piece,
                embedding=_emb_value(vec),
            )
        )
        if commit_batch and idx % commit_batch == 0:
            db.commit()
            db.refresh(novel)

    db.commit()
    return {
        "novel_id": novel.id,
        "title": title,
        "chapters": len(chapters),
        "chunks": len(pieces),
    }
