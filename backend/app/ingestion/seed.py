"""种子语料加载：扫描 SEED_DIR 下 *.txt 入库（按 source_url 幂等）。"""

import os

from sqlalchemy.orm import Session

from app.config import settings
from app.ingestion.encoding import read_text_file
from app.ingestion.store import ingest_text
from app.models import RawNovel


def run_seed(db: Session, seed_dir: str | None = None) -> list[dict]:
    seed_dir = seed_dir or settings.seed_dir
    results: list[dict] = []
    if not os.path.isdir(seed_dir):
        return results

    for fn in sorted(os.listdir(seed_dir)):
        if not fn.lower().endswith(".txt"):
            continue
        path = os.path.join(seed_dir, fn)
        src = f"seed://{fn}"
        if db.query(RawNovel).filter_by(source_url=src).first():
            continue
        text, _detected = read_text_file(path)
        results.append(
            ingest_text(db, text, title=fn, platform="seed", source_url=src)
        )
    return results
