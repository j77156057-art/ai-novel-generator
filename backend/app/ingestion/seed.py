"""种子语料加载：扫描 SEED_DIR 下 *.txt 入库（按 source_url 幂等）。"""

import os
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import REPO_ROOT, settings
from app.ingestion.encoding import read_text_file
from app.ingestion.store import ingest_text
from app.models import RawNovel


def resolve_seed_dir(seed_dir: str | None = None) -> Path:
    """把 SEED_DIR 解析成绝对路径：相对路径一律按仓库根解析。

    修复前的实现直接 `os.path.isdir(settings.seed_dir)`——而默认值 "data/seed"
    是相对**进程工作目录**的。按 README 要求 `cd backend` 启动时它会指向
    backend/data/seed（不存在），于是 `/api/novels/seed` 恒返回 200 []，
    看起来成功了其实一个文件都没读。

    Raises:
        FileNotFoundError: 目录不存在或不是目录。**不再静默返回空列表**，
            调用方（路由层）把它转成 400，让用户看见真正的原因。
    """
    path = Path(seed_dir or settings.seed_dir).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_dir():
        raise FileNotFoundError(f"SEED_DIR 不存在或不是目录：{path}")
    return path


def run_seed(db: Session, seed_dir: str | None = None) -> list[dict]:
    resolved = resolve_seed_dir(seed_dir)
    results: list[dict] = []

    for fn in sorted(os.listdir(resolved)):
        if not fn.lower().endswith(".txt"):
            continue
        path = resolved / fn
        src = f"seed://{fn}"
        if db.query(RawNovel).filter_by(source_url=src).first():
            continue
        text, _detected = read_text_file(str(path))
        results.append(
            ingest_text(db, text, title=fn, platform="seed", source_url=src)
        )
    return results
