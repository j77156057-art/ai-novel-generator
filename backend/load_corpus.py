"""把 data/seed/ 下的真实小说 txt 灌入库并重建知识图谱。

背景：RAG 每次生成都会把检索到的语料原文作为"文风参考"注入提示词。原来库里
只有一段 322 字 demo 文本（随仓库不带，见 data/seed/README-投放真实语料.md），
而那段文本本身带 AI 腔（段尾升华 + 旁白预告），模型在学它。换真人小说才能治本。

用法：
    python load_corpus.py                         # 灌入 data/seed 全部 txt + 重建图谱
    python load_corpus.py --drop-demo             # 额外清掉 demo 语料（会连带清chunk）
    python load_corpus.py --rebuild-only          # 只重建图谱，不导入
    python load_corpus.py --only A.txt --only B.txt --no-graph
                                                  # 定向只导 A/B（**不进 data/seed 扫描**），
                                                  # 跳过大图谱重建

`--only` 的存在理由：`data/seed/` 里既有已入库的三本古典，也有**不该入库**的
网文腔 demo（`【示例·网文腔】*.txt`）。全目录扫描会把它们一起灌进去。要定向补两本
真实语料时用 `--only <绝对路径>`（可多次传），只导这几本。

注意（第 8 步修正 docstring 与被写库的口径一致）：
本脚本写的是 **`settings.database_url` 指向的库**（默认 `sqlite:///./novel_dev.db`，
即相对当前工作目录解析），**不是** docstring 旧文案里写的 novel_demo.db ——
旧文案与实现不符，会让人误判「要写哪个库」。启动时会打印**实际使用的库绝对路径**，
执行前请看一眼，确认不是误写生产库。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import ensure_schema
from app.ingestion.encoding import read_text_file
from app.kg.build import build_graph
from app.models import NovelChunk, RawNovel

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = os.path.abspath(os.path.join(HERE, "..", "data", "seed"))
DEMO_NAME = "sample_novel.txt"


def _engine():
    url = settings.database_url
    if url.startswith("sqlite:///")and not url.startswith("sqlite:////"):
        # 相对路径按cwd 解析，这里统一以 backend/ 为基准，与服务启动目录一致
        url = "sqlite:///" + os.path.abspath(
            url.replace("sqlite:///", "", 1))
    return create_engine(url)


def _db_display_path(url) -> str:  # noqa: ANN001  (SQLAlchemy URL)
    """把 engine.url 显示成**绝对文件路径**（sqlite），非 sqlite 原样返回。"""
    s = str(url)
    if s.startswith("sqlite:///"):
        return os.path.abspath(s.replace("sqlite:///", "", 1))
    return s


def drop_demo(db: Session) -> None:
    demo = db.scalars(
        select(RawNovel).where(RawNovel.title == DEMO_NAME)
    ).first()
    if demo is None:
        print("  demo 语料不存在，跳过")
        return
    n = db.query(NovelChunk).filter(
        NovelChunk.novel_id == demo.id).delete()
    db.delete(demo)
    db.commit()
    print(f"  已清掉 demo 语料 novel_id={demo.id}（连带 {n} 个分片）")


def dedupe_novels(db: Session) -> None:
    """清掉同名重复的语料（导入中断重跑会留下重复）。

    保留 id 最小的那条，其余连同分片一起删——否则 M2 检索会召回重复内容，
    RRF 融合排序被同一段文本的多份副本污染。
    """
    seen: dict[str, int] = {}
    for n in db.scalars(select(RawNovel).order_by(RawNovel.id)).all():
        key = (n.title or "").strip()
        if not key:
            continue
        if key in seen:
            n_chunks = db.query(NovelChunk).filter(
                NovelChunk.novel_id == n.id).delete()
            db.delete(n)
            print(f"  删除重复语料 id={n.id}{key!r}（连带 {n_chunks} 个分片）")
        else:
            seen[key] = n.id
    db.commit()


def import_txts(db: Session, paths: list[str]) -> list[int]:
    """复用 M1 ingest 通路（解析 → 分章 → 切分 → 入库），保持与上传一致。"""
    from app.ingestion.store import ingest_text

    ids = []
    for p in paths:
        title = os.path.basename(p)
        # 编码探测：GBK / GB18030 的国内网文 txt 不再被硬编码 utf-8 挡住。
        text, detected = read_text_file(p)
        res = ingest_text(db, text, title=title, platform="upload")
        ids.append(res["novel_id"])
        report = res.get("clean_report") or {}
        print(f"  已灌入 {title}: novel_id={res['novel_id']} "
              f"{res['chapters']} 章 / {res['chunks']} 分片 / 编码={detected}")
        if report.get("cleaned"):
            marks = "，".join(
                f"{m['text']}×{m['count']}" for m in report.get("marks", [])
            )
            print(f"    清洗：删除 {report['removed_chars']} 字"
                  f"（{report['chars_in']}→{report['chars_out']}）；检出 {marks}")
        else:
            print("    清洗：未检出注入水印")
    return ids


def _select_txts(only: list[str] | None) -> list[str]:
    """决定要导入哪些 txt：给了 `--only` 就只导这些，否则扫描 SEED_DIR 全量。

    `--only` 的路径会被解析成**绝对路径**并事先校验存在性——传错路径要 fail loud，
    不能默默地「一本没导」还打印成功。
    """
    if only:
        paths = [os.path.abspath(p) for p in only]
        missing = [p for p in paths if not os.path.isfile(p)]
        if missing:
            raise SystemExit("--only 路径不存在：\n  " + "\n  ".join(missing))
        return paths
    return sorted(
        os.path.join(SEED_DIR, f)
        for f in os.listdir(SEED_DIR)
        if f.lower().endswith(".txt")
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop-demo", action="store_true",
                    help="清掉带 AI 腔的 demo 语料")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="只重建图谱，不导入 txt")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="跳过重复语料清理")
    ap.add_argument("--only", action="append", default=None, metavar="PATH",
                    help="定向导入：只导指定 txt（可多次传），不再扫描 data/seed")
    ap.add_argument("--no-graph", action="store_true",
                    help="跳过大图谱重建（定向补录语料时通常不需要重建全量图）")
    args = ap.parse_args()

    engine = _engine()
    # 建库 + 补列（含第 8 步新增的 raw_novels.clean_report）。
    # 离线脚本直接 create_engine，不走 app.db 的启动迁移，这里必须显式补一次，
    # 否则导入到未迁移的旧库会 "no such column: clean_report"。
    ensure_schema(engine)
    with Session(engine) as db:
        # 打印**绝对路径**：执行前先看清要写哪个库，避免误写生产库。
        print(f"库: {engine.url}")
        print(f"实际写入的库绝对路径: {_db_display_path(engine.url)}")

        # 清理重复语料（导入中断重跑会留下同名副本，污染 M2 检索排序）。
        # 放在导入之外：--rebuild-only 也可能需要先清。
        if not args.no_dedupe:
            print("\n=== 清理重复语料 ===")
            dedupe_novels(db)

        if not args.rebuild_only:
            txts = _select_txts(args.only)
            scope = "定向导入" if args.only else "扫描 data/seed"
            print(f"\n=== 导入语料（{len(txts)} 本，{scope}） ===")
            if not txts:
                print(f"  {SEED_DIR} 下没有 .txt —— 请把小说 txt 放进来")
            else:
                import_txts(db, txts)

        if args.drop_demo:
            print("\n=== 清理 demo 语料 ===")
            drop_demo(db)

        print("\n=== 库内语料清单 ===")
        for n in db.scalars(select(RawNovel).order_by(RawNovel.id)).all():
            row = db.execute(
                select(
                    NovelChunk.id,
                    NovelChunk.text,
                ).where(NovelChunk.novel_id == n.id)
            ).all()
            chars = sum(len(t or "") for _, t in row)
            print(f"  novel_id={n.id} {n.title!r} 分片={len(row)} 字数={chars}")

        # 重建图谱：汇总全部语料建**一张**图。
        # 不能逐本 build_graph —— 图谱是 project 维度的一张表，每本调用都会
        # _clear_project_graph 抹掉上一本，实测三本只留下最后一本文的图。
        if args.no_graph:
            print("\n=== 跳过知识图谱重建（--no-graph） ===")
        else:
            print("\n=== 重建知识图谱（全语料汇总） ===")
            res = build_graph(db, project_id=1, all_corpora=True)
            print(f"  {res}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())