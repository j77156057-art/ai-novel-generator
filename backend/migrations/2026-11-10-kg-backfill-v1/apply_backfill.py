# -*- coding: utf-8 -*-
"""为已生成段主动补建 KG 批抽入队行（并可选同步跑完），带快照与回滚。

背景（三处缺陷之一）：``kg_mentions`` 的补回入口只认「既有 failed 行」，
而段可能是**早于入队功能**生成的——库里连 AgentRun 行都没有，于是
``requeue_failed`` 永远够不到，且完全静默。本脚本直接调用
``batch_worker.backfill_project``：逐段检查 request_hash 是否有入队行，
没有才补建并入队；随后可选 ``flush_project`` 同步跑完（等价 worker 线程）。

**正文命中守卫不放宽**：批抽/降级路径落 mention 前仍由
``_first_mentioned_form`` + ``canon_source.count_forms`` 判断正文是否真的出现
物品名。本脚本不参与匹配，只负责入队与记账——补不回就是补不回。

## 用法

    # 只看：哪些段没有入队行、物品名在正文里能不能命中
    python backend/migrations/2026-11-10-kg-backfill-v1/apply_backfill.py \
        --project 1 --project 5 --dry-run

    # 真跑（默认先备份库，再入队并同步跑完）
    python backend/migrations/2026-11-10-kg-backfill-v1/apply_backfill.py \
        --project 1 --project 5

    # 只入队、不在此进程跑（交给服务里的 worker）
    python .../apply_backfill.py --project 5 --no-process

    # 回滚某次快照（删除该次新建的 AgentRun 与 kg_mentions 行）
    python .../apply_backfill.py --rollback backend/migrations/.../snapshots/backfill_xxx.json

## 边界

只 INSERT ``agent_runs``(role=kg_batch_extract) 与 ``kg_mentions``；
不改正文、不改 `kg_entities` 的名字/别名、不改持有表。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
if str(PROJ / "backend") not in sys.path:
    sys.path.insert(0, str(PROJ / "backend"))

APP_DB = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"
SNAPSHOT_DIR = HERE / "snapshots"


def _backup(path: pathlib.Path) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-pre-kg-backfill-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _project_ids(db, requested: list[int] | None) -> list[int]:
    from sqlalchemy import select

    from app.models import Generation

    if requested:
        return sorted(set(requested))
    rows = db.scalars(select(Generation.project_id).distinct()).all()
    return sorted({int(r) for r in rows})


def _item_surfaces(db, project_id: int) -> list[tuple[int, str, list[str]]]:
    from sqlalchemy import select

    from app.models import KgEntity

    rows = db.execute(
        select(KgEntity.id, KgEntity.canonical_name, KgEntity.aliases).where(
            KgEntity.project_id == project_id, KgEntity.kind == "item")
    ).all()
    out: list[tuple[int, str, list[str]]] = []
    for ent_id, canonical, aliases in rows:
        forms = [f.strip() for f in [canonical, *(aliases or [])]
                 if isinstance(f, str) and f.strip()]
        if forms:
            out.append((ent_id, canonical, forms))
    return out


def _dry_run_project(db, project_id: int) -> dict:
    """逐段报告：有没有入队行、物品名在正文里能否命中（不写任何东西）。"""
    import hashlib

    from sqlalchemy import select

    from app.kg import canon_source
    from app.models import AgentRun, Generation, KgMention

    items = _item_surfaces(db, project_id)
    guards = frozenset(f for _, _, forms in items for f in forms)
    gens = db.scalars(
        select(Generation).where(Generation.project_id == project_id)
        .order_by(Generation.segment_no, Generation.id)
    ).all()
    by_seg: dict[int, Generation] = {}
    for g in gens:
        by_seg.setdefault(g.segment_no, g)

    segments = []
    for seg_no, gen in sorted(by_seg.items()):
        h = hashlib.sha256(f"{project_id}:{seg_no}".encode("utf-8")).hexdigest()
        run = db.scalar(select(AgentRun).where(
            AgentRun.project_id == project_id,
            AgentRun.role == "kg_batch_extract",
            AgentRun.request_hash == h,
        ))
        text = gen.text or ""
        hits = []
        for ent_id, canonical, forms in items:
            for form in forms:
                if canon_source.text_mention(text, form, guards=guards):
                    hits.append({"entity_id": ent_id, "canonical": canonical,
                                 "surface": form})
                    break
        segments.append({
            "segment_no": seg_no,
            "generation_id": gen.id,
            "chars": len(text),
            "preview": text[:60],
            "has_run": run is not None,
            "run_id": run.id if run is not None else None,
            "run_status": run.status if run is not None else None,
            "item_hits": hits,
            "would_enqueue": run is None and bool(text.strip()),
        })
    return {
        "project_id": project_id,
        "item_entities": len(items),
        "mentions_existing": db.scalar(
            select(KgMention.id).where(KgMention.project_id == project_id).limit(1)
        ) is not None,
        "segments": segments,
    }


def run_backfill(db, project_id: int, *, process: bool) -> dict:
    """补建入队行（可选同步跑完），返回本次新建的行 id 供快照/回滚。"""
    from sqlalchemy import select

    from app.kg.batch_worker import backfill_project, flush_project
    from app.models import AgentRun, KgMention

    before_runs = {int(i) for i in db.scalars(
        select(AgentRun.id).where(AgentRun.project_id == project_id,
                                  AgentRun.role == "kg_batch_extract")
    ).all()}
    before_mentions = {int(i) for i in db.scalars(
        select(KgMention.id).where(KgMention.project_id == project_id)
    ).all()}

    report = backfill_project(db, project_id)
    if process and report["enqueued"]:
        flush_project(project_id)

    db.expire_all()
    new_runs = sorted({int(i) for i in db.scalars(
        select(AgentRun.id).where(AgentRun.project_id == project_id,
                                  AgentRun.role == "kg_batch_extract")
    ).all()} - before_runs)
    new_mentions = sorted({int(i) for i in db.scalars(
        select(KgMention.id).where(KgMention.project_id == project_id)
    ).all()} - before_mentions)
    touched_entities: set[int] = set()
    for mid in new_mentions:
        row = db.get(KgMention, mid)
        if row is not None:
            touched_entities.add(int(row.entity_id))

    return {
        "project_id": project_id,
        "processed": bool(process),
        "report": report,
        "created_agent_run_ids": new_runs,
        "created_mention_ids": new_mentions,
        "touched_entity_ids": sorted(touched_entities),
    }


def rollback(snapshot_path: pathlib.Path, db) -> dict:
    """按快照删除该次新建的 AgentRun / kg_mentions，并重算 mention_count。"""
    from sqlalchemy import func, select

    from app.models import AgentRun, KgEntity, KgMention

    data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    removed_runs = 0
    removed_mentions = 0
    touched: set[int] = set()
    for part in data.get("projects", []):
        for rid in part.get("created_agent_run_ids") or []:
            row = db.get(AgentRun, rid)
            if row is not None:
                db.delete(row)
                removed_runs += 1
        for mid in part.get("created_mention_ids") or []:
            row = db.get(KgMention, mid)
            if row is not None:
                touched.add(int(row.entity_id))
                db.delete(row)
                removed_mentions += 1
    db.commit()
    # mention_count 是派生列：删行后按剩余行数回写
    for ent_id in touched:
        cnt = db.scalar(
            select(func.count()).select_from(KgMention).where(
                KgMention.entity_id == ent_id)
        )
        ent = db.get(KgEntity, ent_id)
        if ent is not None:
            ent.mention_count = cnt or 0
    db.commit()
    return {"snapshot": str(snapshot_path), "removed_agent_runs": removed_runs,
            "removed_mentions": removed_mentions, "entities_recounted": len(touched)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(APP_DB))
    ap.add_argument("--project", type=int, action="append", default=None,
                    help="可重复；缺省为所有有生成段的项目")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告逐段状态与物品名命中，不写库")
    ap.add_argument("--no-process", action="store_true",
                    help="只入队，不在本进程跑批抽（交给服务里的 worker）")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--rollback", default=None, help="按快照 JSON 回滚")
    args = ap.parse_args()

    app_path = pathlib.Path(args.db)
    if not app_path.exists():
        raise SystemExit(f"库不存在：{app_path}")

    # 导入 app 侧模型/worker 前先把库指到目标文件：`_run_batches` 内部用的是
    # `app.db.SessionLocal`，不改的话 flush_project 会读到不同的库。
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as app_db

    engine = create_engine(f"sqlite:///{app_path}", future=True)
    Session = sessionmaker(bind=engine, future=True)
    app_db.SessionLocal = Session

    from app.kg import canon_source

    canon_source.reset_cache()

    if args.rollback:
        snap = pathlib.Path(args.rollback)
        if not snap.exists():
            raise SystemExit(f"快照不存在：{snap}")
        db = Session()
        try:
            result = rollback(snap, db)
        finally:
            db.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    backup = None
    if not args.dry_run and not args.no_backup:
        backup = _backup(app_path)

    db = Session()
    try:
        project_ids = _project_ids(db, args.project)
        if args.dry_run:
            report = {"dry_run": True, "db": str(app_path),
                      "projects": [_dry_run_project(db, pid) for pid in project_ids]}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return

        parts = [run_backfill(db, pid, process=not args.no_process)
                 for pid in project_ids]
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        snap_path = SNAPSHOT_DIR / f"backfill_{stamp}.json"
        snapshot = {
            "created_at": stamp,
            "db": str(app_path),
            "backup": str(backup) if backup else None,
            "projects": parts,
        }
        snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(json.dumps({
            "db": str(app_path),
            "backup": str(backup) if backup else None,
            "snapshot": str(snap_path),
            "projects": parts,
        }, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
