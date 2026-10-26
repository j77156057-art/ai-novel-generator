"""把用户提供的真实网文 txt（默认下载目录，可用 NOVEL_DIR 覆盖）灌入语料库，作为「参考语料」。

设计取舍：
- 网文单本常百万字级（《焚天录》 11MB / 《长夜司》 9MB）。对「风格参考 RAG」而言，
  只需开篇若干章即可锁定网文调性，整本入库纯属浪费 DB 空间（本地哈希向量每片 ~9KB）。
  故每本只取**前 CAP_CHARS 字**（按章截断），足够覆盖网文声口；如需全本，把 CAP 调大或走上传。
- 每本独立成一条 RawNovel（平台标 "网文"），不在导入阶段建图谱——
  参考语料的图谱改为「生成后 / 按需」按 source 标签各自建（见 kg/build.py）。
- 幂等：按 source_url 跳过已导入的本子。

用法（在 backend/ 目录下运行）：
    python ingest_web_novels.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base
from app.ingestion.encoding import read_text_file
from app.ingestion.store import ingest_text
from app.models import NovelChunk, RawNovel

HERE = os.path.dirname(os.path.abspath(__file__))
CAP_CHARS = 350_000  # 每本参考切片上限（按章截断）

# 语料目录：默认取用户下载目录，可用 NOVEL_DIR 覆盖（也可直接传参）。
DOWNLOADS = os.environ.get("NOVEL_DIR") or os.path.expanduser("~/Downloads")
FILES = [
    "《长夜司》.txt",
    "《豪门夜宴》.txt",
    "《焚天录》.txt",
]


def _engine():
    url = settings.database_url
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        url = "sqlite:///" + os.path.abspath(url.replace("sqlite:///", "", 1))
    return create_engine(url)


def _read_text(path: str) -> str:
    """网文 txt 多为 GBK/GB18030 —— 统一走 `encoding.decode_text`（带可读性校验）。

    旧实现「试解码成功即收工」有隐患：错误编码也可能『解得出』但全是乱码。
    现在交由 `decode_text` 做中文可读性校验，并把检出编码打出来备查。
    """
    text, detected = read_text_file(path)
    print(f"[enc] {os.path.basename(path)} → {detected}")
    return text


def _cap_by_chapters(text: str, cap: int) -> str:
    """保留原文「第X章」标记，只在 cap 处截断（截断点可能落在某章中段，无妨）。

    关键：必须保留原始章标记，否则 re-ingest 时 parse_txt 再也认不出章节，
    全部被并成 1 个无章号大块，后续按语料建图谱也会丢失章节跨度。
    """
    return text[:cap]


def main() -> int:
    engine = _engine()
    Base.metadata.create_all(bind=engine)  # 确保迁移后表存在
    with Session(engine) as db:
        for fn in FILES:
            path = os.path.join(DOWNLOADS, fn)
            if not os.path.isfile(path):
                print(f"[skip] 找不到 {path}")
                continue
            src = f"downloads://{fn}"
            if db.execute(select(RawNovel).where(RawNovel.source_url == src)).first():
                print(f"[skip] 已导入 {fn}")
                continue
            raw = _read_text(path)
            capped = _cap_by_chapters(raw, CAP_CHARS)
            title = fn[:-4] if fn.lower().endswith(".txt") else fn
            res = ingest_text(
                db, capped, title=title, platform="网文", source_url=src
            )
            print(
                f"[ok] {title}: novel_id={res['novel_id']} "
                f"章={res['chapters']} 片={res['chunks']} "
                f"用字≈{len(capped)} / 全本≈{len(raw)}"
            )
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
