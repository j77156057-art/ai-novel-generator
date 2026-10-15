"""一次性脚本：在**临时库**上演示「编码探测 + 导入清洗」整链路（第 8 步交付 2 验收）。

不碰真实开发库：DATABASE_URL 必须由外部指向临时库（脚本会自检并拒绝非临时库）。

用法（在 backend/ 目录下）：
    DATABASE_URL="sqlite:///D:/Temp/demo_ingest.db" python _demo_ingest_temp.py "C:/Users/h'h'h/Downloads/豪门宠婚，爵少你别闹.txt"
"""
from __future__ import annotations

import json
import os
import sys

# —— 必须在 import app.db 之前锁定库（否则 engine 会绑到默认的 novel_dev.db）——
_url = os.environ.get("DATABASE_URL", "")
if "novel_dev.db" in _url or "novel_demo.db" in _url or not _url:
    raise SystemExit(
        f"拒绝运行：DATABASE_URL={_url!r} 未指向临时库。"
        "请显式设置到临时目录（如 D:/Temp/demo_ingest.db）。"
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import SessionLocal, init_db  # noqa: E402
from app.ingestion.encoding import read_text_file  # noqa: E402
from app.ingestion.store import ingest_text  # noqa: E402
from app.models import NovelChunk, RawNovel  # noqa: E402
from app.schemas import NovelOut  # noqa: E402


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        "~/Downloads/豪门宠婚，爵少你别闹.txt"
    )
    init_db()
    text, detected = read_text_file(path)
    print(f"[enc] {os.path.basename(path)} → {detected}  （{len(text)} 字）")

    with SessionLocal() as db:
        res = ingest_text(db, text, title=os.path.basename(path), platform="upload")
        novel = db.get(RawNovel, res["novel_id"])
        report = novel.clean_report or {}
        chunks = db.query(NovelChunk).filter(NovelChunk.novel_id == novel.id).all()

    print(f"[db] 临时库: {os.environ['DATABASE_URL']}")
    print(f"[res] novel_id={res['novel_id']} 章={res['chapters']} 分片={res['chunks']}")
    print(f"[clean] cleaned={report.get('cleaned')} "
          f"chars {report.get('chars_in')}→{report.get('chars_out')} "
          f"删除={report.get('removed_chars')} 检出串={[m['text'] for m in report.get('marks', [])]}")
    if report.get("marks"):
        m = report["marks"][0]
        print(f"        首串样本数={len(m['samples'])} 次数={m['count']} final_ratio={m['final_ratio']}")
    total_chars = sum(len(c.text or "") for c in chunks)
    print(f"[chunks] 分片字数合计={total_chars}")

    # 反序列化验收：NovelOut 能带出 clean_report（说明 API 能读到清洗报告）
    out = NovelOut.model_validate(novel).model_dump()
    print(f"[api] NovelOut.clean_report keys={sorted((out.get('clean_report') or {}).keys())}")
    print(json.dumps(report, ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
