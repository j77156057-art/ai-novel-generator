"""把 data/seed/ 下的真实小说 txt 灌入库并重建知识图谱。

背景：RAG 每次生成都会把检索到的语料原文作为"文风参考"注入提示词。原来库里
只有 322 字 demo 文本（data/seed/sample_novel.txt），而那段文本本身带 AI 腔
（段尾升华 + 旁白预告），模型在学它。换真人小说才能治本。

用法：
    python load_corpus.py                 # 灌入本目录全部 txt + 重建图谱
    python load_corpus.py --drop-demo     # 额外清掉 demo 语料（会连带清chunk）
    python load_corpus.py --rebuild-only  # 只重建图谱，不导入

注意：脚本直接操作运行中的 SQLite 库（novel_demo.db），需后端已启动且库路径一致。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.config import settings
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
        with open(p, "r", encoding="utf-8") as f:
            text = f.read()
        res = ingest_text(db, text, title=title, platform="upload")
        ids.append(res["novel_id"])
        print(f"  已灌入 {title}: novel_id={res['novel_id']} "
              f"{res['chapters']} 章 / {res['chunks']} 分片")
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop-demo", action="store_true",
                    help="清掉带 AI 腔的 demo 语料")
    ap.add_argument("--rebuild-only", action="store_true",
                    help="只重建图谱，不导入 txt")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="跳过重复语料清理")
    args = ap.parse_args()

    engine = _engine()
    with Session(engine) as db:
        print(f"库: {engine.url}")

        # 清理重复语料（导入中断重跑会留下同名副本，污染 M2 检索排序）。
        # 放在导入之外：--rebuild-only 也可能需要先清。
        if not args.no_dedupe:
            print("\n=== 清理重复语料 ===")
            dedupe_novels(db)

        if not args.rebuild_only:
            txts = sorted(
                os.path.join(SEED_DIR, f)
                for f in os.listdir(SEED_DIR)
                if f.lower().endswith(".txt")
            )
            print(f"\n=== 导入语料（{len(txts)} 本） ===")
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
        print("\n=== 重建知识图谱（全语料汇总） ===")
        res = build_graph(db, project_id=1, all_corpora=True)
        print(f"  {res}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())