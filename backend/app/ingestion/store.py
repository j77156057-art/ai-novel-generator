"""入库：解析 → 清洗 → 切分 → 填 embedding → 写 raw_novels + novel_chunks。

清洗阶段（第 8 步接入）：解析之前先剔除盗版站注入的 SEO 水印，并把清洗报告
持久化到 `RawNovel.clean_report`。此前 `corpus_health` 只在拟合文体档案时被调用，
导入链路完全不清洗 —— 脏文本直接进 `novel_chunks` 污染检索召回。详见
`app/ingestion/clean.py`。
"""

import json

from sqlalchemy.orm import Session

from app.config import settings
from app.corpus_health import scan_watermarks, strip_watermarks, strip_repeated_ads
from app.db import IS_SQLITE
from app.embeddings.provider import get_embedding_provider
from app.ingestion.chunk import chunk_text
from app.ingestion.clean import build_clean_report, clean_consumption_text
from app.ingestion.parse_txt import parse_txt
from app.models import NovelChunk, RawNovel

#: 入库时文本级确定性扫描的**抽样**分片数上限。
#:
#: 为什么抽样、为什么放这里（第 10 步的接入点选择）：
#: - 本计数器不调模型；投放前助手会另做有限风险样本复核，模型不可用标待复查；
#: - 但「这份语料大概有多少待复核的可疑点」这个数，入库时顺手算出来对用户有用
#:   （否则要等用户主动跑清洗才知道脏不脏）；
#: - 一本 500 万字小说有上千分片，全量扫会拖慢导入，故**抽样**前 N 片做计数，
#:   并在报告里**显式标注 sampled=true**（不静默少算、不假装全量）。
#: 取 200：约当 20 万字样本，低频字/重复字的频率已稳定，耗时可控。
TEXT_CLEAN_SAMPLE_CHUNKS = 200


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

    清洗（第 8 步）：在解析之前先剔除水印，保证**入库的分片就是干净的**；
    清洗报告（含检出串、次数、删字数、原文样本）写入 `novel.clean_report`，
    可通过 API 读到。清洗是确定性的，同一输入两次结果逐字节相同。
    """
    from app.agents.registry import invoke_tool
    from fastapi import HTTPException
    prepared = invoke_tool("preview_ingest_clean", db=db, project_id=None, text=text, chapter_meta=chapter_meta)
    checked = invoke_tool("check_ingest_clean", db=db, project_id=None, prepared=prepared)
    if checked["status"] == "blocked":
        raise HTTPException(422, detail={"message": "清洗检查阻止入库", "agent_check": checked})
    chapters = prepared["chapters"]
    novel = RawNovel(title=title, platform=platform, source_url=source_url,
                     source_text=text, source_chapters=chapter_meta,
                     status="imported_review" if checked["status"] == "review_required" else "imported",
                     clean_report={**prepared["report"], "agent_check": checked})
    db.add(novel)
    db.flush()
    pieces: list[tuple[int | None, int, str]] = []
    for ch in chapters:
        for i, piece in enumerate(
            chunk_text(ch["body"], settings.chunk_size, settings.chunk_overlap)
        ):
            pieces.append((ch.get("chapter_no"), i, piece))

    # —— 文本级确定性扫描（第 10 步）：只计数、不调模型、不改正文 ——
    # 把「本语料待复核的可疑点数量」写进清洗报告，供用户在助手页按需触发
    # clean-corpus 做语义复核。抽样（见常量说明），并显式标注是否抽样。
    from app.agents.text_clean import summarize as _text_clean_summarize

    text_scan = _text_clean_summarize(
        [p[2] for p in pieces], sample_cap=TEXT_CLEAN_SAMPLE_CHUNKS
    )
    novel.clean_report = {**(novel.clean_report or {}), "text_clean": text_scan}

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
        consumed_piece = piece
        db.add(
            NovelChunk(
                novel_id=novel.id,
                chapter_no=chapter_no,
                idx=i,
                text=consumed_piece,
                raw_text=piece,
                embedding=_emb_value(vec),
            )
        )
        if commit_batch and idx % commit_batch == 0:
            db.commit()
            db.refresh(novel)

    from sqlalchemy import select
    from app.agents.ingest_clean import fingerprint
    db.flush()
    rows = list(db.scalars(select(NovelChunk).where(NovelChunk.novel_id == novel.id).order_by(NovelChunk.id)))
    actual = [(r.chapter_no, r.idx, r.text) for r in rows]
    matches = actual == pieces
    novel.clean_report = {**novel.clean_report, "chunks_fingerprint": fingerprint(actual),
                          "readback": {"matches": matches, "chunks": len(rows)}}
    if not matches:
        db.rollback()
        raise RuntimeError("ingest readback mismatch")
    db.commit()
    return {
        "check_status": checked["status"],
        "novel_id": novel.id,
        "title": title,
        "chapters": len(chapters),
        "chunks": len(pieces),
        # 清洗报告随导入结果一并回显（上传响应 / load_corpus 打印都用它）。
        "clean_report": novel.clean_report,
    }
