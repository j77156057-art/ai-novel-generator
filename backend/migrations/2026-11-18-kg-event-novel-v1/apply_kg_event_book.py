# -*- coding: utf-8 -*-
"""kg_events 补书维度：加列 + 按来源回填 + 可回滚。

判定口径（机械、可复核，不调模型）：

1. 事件主体名出现在「本项目同 segment 的生成正文」里 → 归本项目原创（NULL）。
   这类事件是项目自己写出来的段落，没有来源书。
2. 否则，主体实体带 novel_id，且主体名在该书 ``novel_chunks.text`` 里出现、
   事件的 chapter 能对到该书的章号 → 归该书（迁入/借用的他书事件）。
3. 其余（主体缺失 / 主体没有书 / 对不上该书正文）→ 保持 NULL = 项目级通用事件。

用法（先拿数据库副本演练，再动真库）：

    python apply_kg_event_book.py --db <path>            # dry-run，只打方案
    python apply_kg_event_book.py --db <path> --apply    # 加列 + 回填，写回滚快照
    python apply_kg_event_book.py --db <path> --rollback # 按快照还原（列保留）

注意：本脚不 import app.*，可以对任意副本/老库直接跑。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

SNAPSHOT = Path(__file__).with_name("snapshot_kg_event_novel.json")


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _ensure_column(conn: sqlite3.Connection) -> None:
    if not _has_column(conn, "kg_events", "novel_id"):
        conn.execute("ALTER TABLE kg_events ADD COLUMN novel_id INTEGER")


def _own_text(conn: sqlite3.Connection, project_id: int, segment_no: int | None) -> str:
    """本项目同 segment 的生成正文；同段多行一并拼上（宁可多判给本项目）。"""
    if segment_no is None:
        return ""
    rows = conn.execute(
        "SELECT text FROM generations WHERE project_id=? AND segment_no=?",
        (project_id, segment_no),
    ).fetchall()
    return "\n".join((r[0] or "") for r in rows)


def _in_book(conn: sqlite3.Connection, novel_id: int, name: str,
             chapter: int | None) -> bool:
    """主体名是否出现在该书正文；有章号时要求同名章里也出现。"""
    if chapter is None:
        row = conn.execute(
            "SELECT 1 FROM novel_chunks WHERE novel_id=? AND instr(text, ?)>0 LIMIT 1",
            (novel_id, name),
        ).fetchone()
        return row is not None
    row = conn.execute(
        "SELECT 1 FROM novel_chunks WHERE novel_id=? AND chapter_no=?"
        " AND instr(text, ?)>0 LIMIT 1",
        (novel_id, chapter, name),
    ).fetchone()
    return row is not None


def plan(conn: sqlite3.Connection) -> list[dict]:
    """逐条给出 (event_id → novel_id) 方案与证据。"""
    rows = conn.execute(
        """
        SELECT e.id, e.project_id, e.segment_no, e.chapter, e.summary,
               su.canonical_name AS subject_name, su.novel_id AS subject_book
        FROM kg_events e
        LEFT JOIN kg_entities su ON su.id = e.subject_id
        ORDER BY e.id
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        (eid, pid, seg, chapter, summary,
         subject, subject_book) = r
        name = (subject or "").strip()
        proposed: int | None = None
        reason = "主体缺失，判不出来源"
        if name:
            own = _own_text(conn, pid, seg)
            if own and name in own:
                reason = "主体出现在本项目同段生成正文 → 本项目原创"
            elif subject_book is not None and _in_book(
                    conn, int(subject_book), name, chapter):
                proposed = int(subject_book)
                reason = f"主体「{name}」在该书 ch{chapter} 正文出现 → 归书 {subject_book}"
            else:
                reason = "主体名对不上任何来源书正文 → 通用事件"
        out.append({
            "event_id": eid,
            "project_id": pid,
            "segment_no": seg,
            "chapter": chapter,
            "subject": name,
            "subject_book": subject_book,
            "summary": (summary or "")[:40],
            "novel_id": proposed,
            "reason": reason,
        })
    return out


def _print_plan(rows: list[dict]) -> None:
    print(f"{'id':>4} {'proj':>4} {'seg':>4} {'ch':>4} "
          f"{'主体':<8} {'主体书':>6} {'回填':>6}  依据 / 摘要")
    for r in rows:
        proposed = "NULL" if r["novel_id"] is None else r["novel_id"]
        book = "NULL" if r["subject_book"] is None else r["subject_book"]
        print(f"{r['event_id']:>4} {r['project_id']:>4} "
              f"{str(r['segment_no']):>4} {str(r['chapter']):>4} "
              f"{r['subject'] or '-':<8} {book:>6} {proposed:>6}  "
              f"{r['reason']}｜{r['summary']}")
    counts: dict[str, int] = {}
    for r in rows:
        key = "NULL" if r["novel_id"] is None else str(r["novel_id"])
        counts[key] = counts.get(key, 0) + 1
    print("\n汇总（事件数按回填书）:", counts)


def _apply(conn: sqlite3.Connection, rows: list[dict]) -> None:
    _ensure_column(conn)
    snapshot = [
        {"id": r["event_id"], "novel_id": None} for r in rows
    ]
    SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    changed = 0
    for r in rows:
        cur = conn.execute(
            "SELECT novel_id FROM kg_events WHERE id=?", (r["event_id"],)
        ).fetchone()
        old = cur[0] if cur else None
        if old != r["novel_id"]:
            conn.execute("UPDATE kg_events SET novel_id=? WHERE id=?",
                         (r["novel_id"], r["event_id"]))
            changed += 1
    conn.commit()
    print(f"\n已回填 {changed} 行；回滚快照：{SNAPSHOT}")


def _rollback(conn: sqlite3.Connection) -> None:
    if not SNAPSHOT.exists():
        raise SystemExit(f"找不到快照：{SNAPSHOT}")
    _ensure_column(conn)
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for row in snapshot:
        conn.execute("UPDATE kg_events SET novel_id=? WHERE id=?",
                     (row["novel_id"], row["id"]))
    conn.commit()
    print(f"已按快照还原 {len(snapshot)} 行（列 novel_id 保留）。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="sqlite 数据库路径")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="加列并回填（写快照）")
    g.add_argument("--rollback", action="store_true", help="按快照还原")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        if args.rollback:
            _rollback(conn)
            return
        rows = plan(conn)
        _print_plan(rows)
        if args.apply:
            _apply(conn, rows)
        else:
            print("\n（dry-run：未写库；确认后加 --apply）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
