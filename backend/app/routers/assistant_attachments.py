"""Explicit attachment upload/confirmation writes; never registered as read-only."""
from fastapi import APIRouter, Depends, File, Form, UploadFile, HTTPException
from sqlalchemy.orm import Session
from app.db import get_db
from app.config import settings
from app.schemas import StrictRequest, UploadResult
from app.agents import attachments
from app.ingestion.store import ingest_text

router = APIRouter(prefix="/api/assistant/attachments", tags=["assistant"])

@router.post("")
def upload_attachment(project_id: int = Form(...), file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = file.file.read(settings.upload_max_bytes + 1)
    return attachments.stage(db=db, project_id=project_id, filename=file.filename, data=data)

@router.get("/{attachment_id}")
def metadata(attachment_id: str, project_id: int, db: Session = Depends(get_db)):
    return attachments.get_metadata(db=db, project_id=project_id, attachment_id=attachment_id)

@router.get("/{attachment_id}/preview")
def preview(attachment_id: str, project_id: int, db: Session = Depends(get_db)):
    """投放前的清洗预览（只读）：会删掉什么、还有什么没解决。"""
    return attachments.preview_attachment_cleaning(
        db=db, project_id=project_id, attachment_id=attachment_id
    )

class IngestAttachmentRequest(StrictRequest):
    project_id: int
    attachment_id: str
    instruction: str
    #: 是否剔除「混进来的别的书」的整章正文（盗版站的 txt 常把多本书拼在一起）。
    #: 默认 False：检测照做（体检报告会标出），但不删 —— 删的是整章正文，
    #: 判据万一错了丢的是真内容，所以要人点头。
    drop_foreign_blocks: bool = False

@router.post("/ingest", response_model=UploadResult)
def confirm_ingest(body: IngestAttachmentRequest, db: Session = Depends(get_db)):
    import json
    from app.models import RawNovel, NovelChunk
    from sqlalchemy import select
    from app.agents.ingest_clean import fingerprint
    meta = attachments.get_metadata(db=db, project_id=body.project_id, attachment_id=body.attachment_id)
    if not attachments.explicit_ingest(body.instruction):
        raise HTTPException(422, "需要明确投放指令和用户确认")
    if meta.get("novel_id"):
        novel = db.get(RawNovel, meta["novel_id"])
        if novel is None:
            raise HTTPException(409, "附件目标语料已不存在")
        report = novel.clean_report or {}
        return {"novel_id": novel.id, "title": novel.title, "chapters": meta["chapters"],
                "chunks": meta["chunks"], "clean_report": report,
                "check_status": report.get("agent_check", {}).get("status", "review_required")}
    mp, tp = attachments.paths(body.project_id, body.attachment_id)
    text = tp.read_text(encoding="utf-8")
    # 投放前必须过同一套清洗体检（与 main 一致）：blocked 直接拒；
    # review_required 允许入库但标成待人工确认。检查结论写进 clean_report，
    # 之后的读取/重试不用再跑一遍模型抽样。
    from app.agents.registry import invoke_tool
    prepared = invoke_tool(
        "preview_ingest_clean", db=db, project_id=body.project_id, text=text
    )
    checked = invoke_tool(
        "check_ingest_clean", db=db, project_id=body.project_id, prepared=prepared
    )
    if checked["status"] == "blocked":
        raise HTTPException(
            422, detail={"message": "清洗检查阻止入库", "agent_check": checked}
        )
    result = ingest_text(db, text, title=meta["filename"],
                         platform="assistant_attachment",
                         drop_foreign_blocks=bool(body.drop_foreign_blocks))
    project = attachments.require_project(db, body.project_id)
    project.reference_novel_ids = list(dict.fromkeys([*(project.reference_novel_ids or []), result["novel_id"]]))
    # 体检结论落到语料上：feat 的 ingest_text 不跑 agent 检查，这里补写，
    # 口径与 main 一致（review_required ⇒ imported_review）。
    novel = db.get(RawNovel, result["novel_id"])
    if novel is not None:
        novel.clean_report = {**(novel.clean_report or {}), "agent_check": checked}
        if checked["status"] == "review_required":
            novel.status = "imported_review"
        result.update({
            "status": novel.status,
            "clean_report": novel.clean_report,
            "check_status": checked["status"],
        })
    db.commit()
    # Read back exact project association before making staging state visible.
    db.refresh(project)
    if result["novel_id"] not in project.reference_novel_ids:
        raise RuntimeError("attachment association mismatch")
    meta.update({"status": "ingested", "novel_id": result["novel_id"], "chapters": result["chapters"], "chunks": result["chunks"], "check_status": checked["status"]})
    mp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    attachments.get_metadata(db=db, project_id=body.project_id, attachment_id=body.attachment_id)
    return result
