"""Project-scoped text staging. No caller-supplied server paths."""
from pathlib import Path
import json
import re
import uuid
from fastapi import HTTPException
from app.models import Project
from app.config import settings
from app.ingestion.encoding import decode_text, EncodingDetectionError

ROOT = Path(__file__).resolve().parents[3] / "data" / "attachments"
ID_RE = re.compile(r"^[0-9a-f]{32}$")

def require_project(db, project_id):
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project 不存在")
    return project

def paths(project_id, attachment_id):
    if not ID_RE.fullmatch(attachment_id) or not isinstance(project_id, int) or project_id < 1:
        raise HTTPException(404, "附件不存在")
    directory = ROOT / str(project_id)
    return directory / (attachment_id + ".json"), directory / (attachment_id + ".txt")

def get_metadata(*, db, project_id, attachment_id):
    require_project(db, project_id)
    meta, _ = paths(project_id, attachment_id)
    if not meta.is_file():
        raise HTTPException(404, "附件不存在或不属于该项目")
    data = json.loads(meta.read_text(encoding="utf-8"))
    if data["project_id"] != project_id:
        raise HTTPException(404, "附件不存在")
    return data

def stage(*, db, project_id, filename, data):
    require_project(db, project_id)
    if not filename or len(filename) > 255 or any(c in filename for c in ('/', '\\', ':', '\x00')) or filename in (".", ".."):
        raise HTTPException(422, "文件名不合法")
    if Path(filename).suffix.lower() not in (".txt", ".md"):
        raise HTTPException(415, "仅支持 .txt / .md 文本附件")
    if len(data) > settings.upload_max_bytes:
        raise HTTPException(413, "附件过大")
    try:
        text, encoding = decode_text(data, filename=filename)
    except EncodingDetectionError:
        raise HTTPException(400, "附件文本无法解码")
    if '\x00' in text:
        raise HTTPException(415, "不支持二进制附件")
    aid = uuid.uuid4().hex
    meta, body = paths(project_id, aid)
    meta.parent.mkdir(parents=True, exist_ok=True)
    # Enforce a bounded project staging quota as well as per-file limit.
    if sum(f.stat().st_size for f in meta.parent.glob("*.txt")) + len(text.encode("utf-8")) > settings.upload_max_bytes * 10:
        raise HTTPException(413, "项目附件暂存空间已满")
    result = {"attachment_id": aid, "project_id": project_id, "filename": filename,
              "size": len(data), "encoding": encoding, "status": "staged", "preview": text[:400]}
    body.write_text(text, encoding="utf-8", newline="")
    meta.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return get_metadata(db=db, project_id=project_id, attachment_id=aid)

def read_attachment(*, db, project_id, attachment_id, offset=0, limit=4000):
    get_metadata(db=db, project_id=project_id, attachment_id=attachment_id)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise HTTPException(422, "offset>=0, limit=1..8000")
    _, body = paths(project_id, attachment_id)
    text = body.read_text(encoding="utf-8")
    return {"attachment_id": attachment_id, "offset": offset, "text": text[offset:offset+limit],
            "total_chars": len(text), "has_more": offset+limit < len(text), "untrusted": True}

def prepare_attachment(*, db, project_id, attachment_id):
    get_metadata(db=db, project_id=project_id, attachment_id=attachment_id)
    from app.agents.registry import invoke_tool
    _, body = paths(project_id, attachment_id)
    return invoke_tool("preview_ingest_clean", db=db, project_id=project_id, text=body.read_text(encoding="utf-8"))

def preview_attachment_cleaning(*, db, project_id, attachment_id):
    p = prepare_attachment(db=db, project_id=project_id, attachment_id=attachment_id)
    r = p["report"]
    return {"attachment_id": attachment_id, "chapters": len(p["chapters"]),
            "report": {k: v for k, v in r.items() if k not in ("removed_spans", "unresolved")},
            "changes": [{**s, "before": s["before"][:500], "after": s["after"][:500]} for s in r["removed_spans"][:8]],
            "unresolved_count": len(r["unresolved"]), "untrusted": True}

def check_attachment_cleaning(*, db, project_id, attachment_id):
    from app.agents.registry import invoke_tool
    prepared = prepare_attachment(db=db, project_id=project_id, attachment_id=attachment_id)
    return {"attachment_id": attachment_id, **invoke_tool("check_ingest_clean", db=db, project_id=project_id, prepared=prepared)}

def explicit_ingest(instruction):
    return any(t in instruction for t in ("投放", "入库", "导入语料", "import attachment")) and not any(t in instruction for t in ("不要", "别", "不必", "不要入库"))

def propose_ingest(*, db, project_id, attachment_id, instruction):
    meta = get_metadata(db=db, project_id=project_id, attachment_id=attachment_id)
    if not explicit_ingest(instruction):
        return None
    from app.agents.pending_actions import build_pending_action
    return build_pending_action(kind="ingest_attachment", title="清洗并投放附件：" + meta["filename"],
        detail={"attachment_id": attachment_id, "filename": meta["filename"], "destructive": False,
                "description": "确认后清洗、助手抽样检查并入库；不确定内容保留待复查。"},
        body={"project_id": project_id, "attachment_id": attachment_id, "instruction": instruction})
