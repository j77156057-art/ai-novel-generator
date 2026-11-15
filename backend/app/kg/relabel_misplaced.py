"""把「段号顶章号」留下的错位行重标回正确的故事章（一次性修复入口）。

## 为什么要有这个入口

较早的按段重抽链路在查不到 generation 的章节标签时，直接拿**段号当章号**：
旧稿段 1–5（``chapter_range`` 是 ``段1``…``段5``，没有故事章标签）的抽取产物
因此落进章 2–5，每条都是一份「非当前历史行」（``is_current=0``）。它们不再
影响当前读数，但会让以后按章排查第 2–5 章时反复误判成「这章也有过抽取」。

修法是**重标、不删除**：删除会丢掉「这里曾经错过」的证据。所以本入口只改
``chapter``，不动 ``is_current`` / ``version`` / 正文证据；每行 ``note`` 记下
来源、原章号和对应的批抽 run id，事后能追到是哪一次抽取、从哪一章挪过来的。

归属口径与 ``batch_worker._segment_chapter_map`` 完全一致：能按标签派生的
按标签；派生不出（``段N`` 这类旧段）才回退到写作顺序上最近的有标签章——也
就是第 1 章。回退只决定归属，不授予当前资格（``is_current`` 保持原值）。

## 用法（在 ``backend/`` 目录下）

    # 先看会改哪些行（不落库）
    python -m app.kg.relabel_misplaced --project-id 6 --dry-run

    # 执行；执行前请先备份主库
    python -m app.kg.relabel_misplaced --project-id 6

``--db`` 同 ``rebuild_chapter``：缺省用项目配置，建议先在副本上演练。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.kg.batch_worker import (
    ROLE_KG_EXTRACT,
    _chapter_of_label,
    _segment_chapter_map,
)
from app.models import AgentRun, Generation, KgCharacterState, KgEvent, Project

#: 写进每行 note 的来源标识：说清是哪一类错位、什么时候修的
DEFAULT_SOURCE = "segment_as_chapter_bug: 按段补路径拿段号当章号（2026-11-14 修复）"

#: 只处理这两张带 chapter 列的表。kg_mentions 没有章号（按 generation/段号挂），
#: kg_relations 的 since_chapter 是「关系从哪章开始」，都不属于这次错位。
TABLES = (("kg_events", KgEvent), ("kg_character_state", KgCharacterState))


def _naive_utc(value: datetime | None) -> datetime | None:
    """统一成 naive UTC：SQLite 存的是 naive UTC，比较前先抹掉 tz。"""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _provenance_run(db: Session, project_id: int, segment_no: int,
                    created_at: datetime | None) -> int | None:
    """反查产出这行的批抽 run：段号相同且 created_at 落在 run 的起止区间内。

    只做尽力而为的追溯——查不到就留 None，不影响重标本身。
    """
    created = _naive_utc(created_at)
    if created is None:
        return None
    rows = db.scalars(
        select(AgentRun).where(
            AgentRun.role == ROLE_KG_EXTRACT,
            AgentRun.project_id == project_id,
        ).order_by(AgentRun.id.desc())
    ).all()
    for run in rows:
        try:
            note = json.loads(run.note) if run.note else {}
        except (ValueError, TypeError):
            note = {}
        if not isinstance(note, dict) or note.get("segment_no") != segment_no:
            continue
        started = _naive_utc(run.started_at)
        finished = _naive_utc(run.finished_at)
        if started is not None and started <= created and (finished is None or created <= finished):
            return run.id
    return None


def _relabel_table(db: Session, model, *, project_id: int,
                   resolved: dict[int, int | None],
                   labels: dict[int, int | None],
                   source: str, now_iso: str, dry_run: bool) -> list[dict]:
    """把一张表里的错位历史行改归正确的章，返回改动明细。"""
    rows = db.scalars(
        select(model).where(
            model.project_id == project_id,
            model.is_current.is_(False),
            model.segment_no.is_not(None),
        )
    ).all()
    changes: list[dict] = []
    for row in rows:
        seg = row.segment_no
        # 有章节标签的段从未走「段号顶章号」的回退，改了反而超出本次修复范围
        if labels.get(seg) is not None:
            continue
        target = resolved.get(seg)
        if target is None or row.chapter == target:
            continue
        detail = {
            "row_id": row.id,
            "segment_no": seg,
            "orig_chapter": row.chapter,
            "new_chapter": target,
            "run_id": _provenance_run(db, project_id, seg, row.created_at),
        }
        changes.append(detail)
        if dry_run:
            continue
        row.chapter = target
        row.note = json.dumps({
            "source": source,
            "orig_chapter": detail["orig_chapter"],
            "new_chapter": target,
            "segment_no": seg,
            "run_id": detail["run_id"],
            "relabeled_at": now_iso,
        }, ensure_ascii=False)
    return changes


def _chapter1_readings(db: Session, project_id: int) -> dict:
    """第 1 章当前读数（与读侧同口径：is_current=1）。"""
    def _count(model) -> dict:
        total = db.scalar(
            select(func.count()).select_from(model).where(
                model.project_id == project_id, model.chapter == 1))
        current = db.scalar(
            select(func.count()).select_from(model).where(
                model.project_id == project_id, model.chapter == 1,
                model.is_current.is_(True)))
        return {"total": int(total or 0), "current": int(current or 0)}

    return {"events": _count(KgEvent), "states": _count(KgCharacterState)}


def _historical_rows(db: Session, project_id: int) -> dict:
    """章 2–5 里还剩多少非当前历史行（修复后复查用）。"""
    out: dict[str, dict[int, int]] = {}
    for name, model in TABLES:
        rows = db.execute(
            select(model.chapter, func.count()).where(
                model.project_id == project_id,
                model.chapter.in_((2, 3, 4, 5)),
                model.is_current.is_(False),
            ).group_by(model.chapter)
        ).all()
        out[name] = {int(ch): int(n) for ch, n in rows}
    return out


def _own_book(db: Session, project_id: int) -> int | None:
    """项目自己的书：续写书优先，其次当前书；原创项目（都没挂）返回 None。"""
    proj = db.get(Project, project_id)
    if proj is None:
        return None
    return (proj.continuation_novel_id
            if proj.continuation_novel_id is not None else proj.active_novel_id)


def relabel_labeled_current(db: Session, project_id: int, *,
                            source: str, dry_run: bool = False) -> dict:
    """把**有章节标签的段**里仍按「段号顶章号」写的当前行改归标签章。

    与 ``relabel_misplaced`` 的区别：那边只动历史行（``is_current=0``），这里
    针对「段先入库、后补标签」的遗留：段 20 的抽取发生在标签还是 ``段20`` 的
    时候，写成了 ``chapter=20``；该段只有一代，没有被 supersede，所以至今仍是
    ``is_current=1`` 却挂在虚构的第 20 章。

    范围刻意收紧，避免碰到参考书数据：
    - events：只认 ``novel_id`` 等于本项目自己的书（原创项目 = NULL）；
    - states：没有书列，只认段号属于本项目 generation 的段（参考书的段号在
      千级，不会命中）；
    - 两条都要求 ``chapter == segment_no``（bug 特征），不猜其它错位。
    """
    rows = db.execute(
        select(Generation.segment_no, Generation.chapter_range).where(
            Generation.project_id == project_id)
    ).all()
    label_of = {seg: ch for seg, label in rows
                if (ch := _chapter_of_label(label or "")) is not None}
    segs = sorted(label_of)
    now_iso = datetime.now(timezone.utc).isoformat()
    out: dict[str, list[dict]] = {"kg_events": [], "kg_character_state": []}
    if not segs:
        return out

    own = _own_book(db, project_id)
    ev_q = select(KgEvent).where(
        KgEvent.project_id == project_id,
        KgEvent.is_current.is_(True),
        KgEvent.segment_no.in_(segs),
        KgEvent.chapter == KgEvent.segment_no,
    )
    ev_q = ev_q.where(KgEvent.novel_id.is_(None) if own is None
                      else KgEvent.novel_id == own)
    ev_rows = db.scalars(ev_q).all()
    # 状态表没有书列，用「同段本书事件的落库时刻」做归属证据：参考书导入的
    # 状态（段 1–18 与参考书段号重名）与本书事件的时刻相差数月，不会被误收。
    ev_times: dict[int, list[datetime]] = {}
    for row in ev_rows:
        if row.created_at is not None:
            ev_times.setdefault(row.segment_no, []).append(row.created_at)
    st_q = select(KgCharacterState).where(
        KgCharacterState.project_id == project_id,
        KgCharacterState.is_current.is_(True),
        KgCharacterState.segment_no.in_(segs),
        KgCharacterState.chapter == KgCharacterState.segment_no,
    )
    st_rows = []
    for row in db.scalars(st_q).all():
        if row.created_at is None:
            continue
        if any(abs((row.created_at - t).total_seconds()) <= 600
               for t in ev_times.get(row.segment_no, [])):
            st_rows.append(row)
    for name, model, rows_ in (("kg_events", KgEvent, ev_rows),
                               ("kg_character_state", KgCharacterState, st_rows)):
        for row in rows_:
            target = label_of[row.segment_no]
            if target == row.chapter:
                continue
            detail = {"row_id": row.id, "segment_no": row.segment_no,
                      "orig_chapter": row.chapter, "new_chapter": target}
            out[name].append(detail)
            if dry_run:
                continue
            row.chapter = target
            row.note = json.dumps({
                "source": source,
                "orig_chapter": detail["orig_chapter"],
                "new_chapter": target,
                "segment_no": row.segment_no,
                "relabeled_at": now_iso,
            }, ensure_ascii=False)
    return out


def relabel_misplaced(db: Session, project_id: int, *,
                      source: str = DEFAULT_SOURCE,
                      dry_run: bool = False,
                      labeled_current: bool = False) -> dict:
    """重标错位历史行并返回可观测统计（不删行、不动 is_current）。"""
    rows = db.execute(
        select(Generation.segment_no, Generation.chapter_range).where(
            Generation.project_id == project_id)
    ).all()
    labels = {seg: _chapter_of_label(label or "") for seg, label in rows}
    resolved = _segment_chapter_map(db, project_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    before = _chapter1_readings(db, project_id)
    by_table: dict[str, list[dict]] = {}
    for name, model in TABLES:
        by_table[name] = _relabel_table(
            db, model, project_id=project_id, resolved=resolved, labels=labels,
            source=source, now_iso=now_iso, dry_run=dry_run)
    labeled_hits: dict[str, list[dict]] = {"kg_events": [], "kg_character_state": []}
    if labeled_current:
        labeled_hits = relabel_labeled_current(
            db, project_id, source=source + ":labeled_current", dry_run=dry_run)
    if not dry_run and (any(by_table.values()) or any(labeled_hits.values())):
        db.commit()
    after = _chapter1_readings(db, project_id)

    return {
        "project_id": project_id,
        "dry_run": dry_run,
        "source": source,
        "changed": sum(len(v) for v in by_table.values())
                   + sum(len(v) for v in labeled_hits.values()),
        "by_table": by_table,
        "labeled_current": labeled_hits,
        "chapter1_before": before,
        "chapter1_after": after,
        "chapters_2_5_historical_left": _historical_rows(db, project_id),
        "resolved_chapters": {str(k): v for k, v in sorted(resolved.items())},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="把段号顶章号留下的错位历史行重标回正确章（不删行）")
    ap.add_argument("--project-id", type=int, required=True)
    ap.add_argument("--db", default="",
                    help="SQLAlchemy URL；缺省用 settings.database_url")
    ap.add_argument("--dry-run", action="store_true", help="只演练，不落库")
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="写进每行 note 的来源说明")
    ap.add_argument("--labeled-current", action="store_true",
                    help="同时处理「有标签段但仍写成 chapter=段号」的当前行")
    args = ap.parse_args(argv)

    # DATABASE_URL 必须在 import app.config / app.db 之前生效
    if args.db:
        os.environ["DATABASE_URL"] = args.db
    backend_root = pathlib.Path(__file__).resolve().parents[2]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.db import SessionLocal, ensure_schema

    ensure_schema()  # 补 note 列等增量迁移（幂等），离线入口不绕过迁移
    with SessionLocal() as db:
        result = relabel_misplaced(
            db, args.project_id, source=args.source, dry_run=args.dry_run,
            labeled_current=args.labeled_current)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
