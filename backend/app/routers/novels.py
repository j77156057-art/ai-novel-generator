"""小说/语料相关路由：上传、种子、列表、分片查看。"""

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.ingestion.encoding import EncodingDetectionError, decode_text
from app.ingestion.seed import run_seed
from app.ingestion.store import ingest_text
from app.models import NovelChunk, RawNovel
from app.retrieval.search import hybrid_search
from app.schemas import ChunkOut, NovelOut, SearchHit, UploadResult

router = APIRouter(prefix="/api/novels", tags=["novels"])


@router.post("/upload", response_model=UploadResult)
def upload(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    data = file.file.read()
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"文件过大（上限 {settings.upload_max_bytes} 字节）",
        )
    # 不再硬编码 utf-8：GBK / GB18030 的国内网文 txt 会被自动识别并正确解码。
    # 全部候选都失败时 decode_text 抛 EncodingDetectionError，这里转成 400 +
    # 具体原因（含文件名与各候选失败原因），不做静默兜底。
    try:
        text, detected = decode_text(data, filename=file.filename)
    except EncodingDetectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = ingest_text(db, text, title=file.filename, platform="upload")
    # 把检出编码一并回显，便于追溯「这本是按什么编码读的」。
    result["detected_encoding"] = detected
    return result


@router.post("/seed", response_model=list[UploadResult])
def seed(db: Session = Depends(get_db)) -> list[dict]:
    return run_seed(db)


@router.get("", response_model=list[NovelOut])
def list_novels(db: Session = Depends(get_db)) -> list[RawNovel]:
    return db.scalars(select(RawNovel).order_by(RawNovel.id)).all()


@router.get("/{novel_id}/chunks", response_model=list[ChunkOut])
def list_chunks(novel_id: int, db: Session = Depends(get_db)) -> list[NovelChunk]:
    return db.scalars(
        select(NovelChunk)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.chapter_no, NovelChunk.idx)
    ).all()


@router.get("/{novel_id}/search", response_model=list[SearchHit])
def search(
    novel_id: int, q: str, k: int = 10, db: Session = Depends(get_db)
) -> list[dict]:
    if db.get(RawNovel, novel_id) is None:
        raise HTTPException(status_code=404, detail="novel 不存在")
    return hybrid_search(db, novel_id, q, k=k)
