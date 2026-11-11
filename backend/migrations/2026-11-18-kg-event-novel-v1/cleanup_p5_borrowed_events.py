# -*- coding: utf-8 -*-
"""清理项目 5 的 5 条他书事件（默认 id 29–33），可回滚。

判定依据见报告：主体（许七安/许新年）是书 5 实体，摘要对应《大奉打更人》ch1，
而项目 5 是原创都市悬疑（记者小杨），只有 2 段生成正文，事件却挂在 segment 1–5；
项目 5 的两段正文里没有任何一条出现这些主体名。

本脚本默认 dry-run；--apply 才删。删除前把整行 + 关联的 kg_event_links 写快照，
--rollback 按快照原样插回。**不删 kg_entities（71–73 许七安/许平志/许新年）**，
它们是否清理由报告单独给建议。

    python cleanup_p5_borrowed_events.py --db <path>
    python cleanup_p5_borrowed_events.py --db <path> --apply
    python cleanup_p5_borrowed_events.py --db <path> --rollback
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_IDS = "29,30,31,32,33"
SNAPSHOT = Path(__file__).with_name("snapshot_p5_events_29_33.json")


def _rows(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    conn.row_factory = sqlite3.Row
    q = ",".join("?" for _ in ids)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM kg_events WHERE id IN ({q}) ORDER BY id", ids)]


def _links(conn: sqlite3.Connection, ids: list[int]) -> list[dict]:
    conn.row_factory = sqlite3.Row
    q = ",".join("?" for _ in ids)
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM kg_event_links WHERE from_event_id IN ({q})"
        f" OR to_event_id IN ({q}) ORDER BY id", ids + ids)]


def _print(conn: sqlite3.Connection, ids: list[int]) -> None:
    rows = _rows(conn, ids)
    print(f"将清理 {len(rows)} 行 kg_events:")
    for r in rows:
        print(f"  id={r['id']} project={r['project_id']} segment={r['segment_no']} "
              f"chapter={r['chapter']} novel_id={r.get('novel_id')} "
              f"subject_id={r['subject_id']}｜{(r['summary'] or '')[:36]}")
    links = _links(conn, ids)
    print(f"关联 kg_event_links: {len(links)} 行")
    other = [r for r in rows if r["project_id"] != 5]
    if other:
        print(f"警告：命中非项目 5 的行 {[r['id'] for r in other]}")


def _apply(conn: sqlite3.Connection, ids: list[int]) -> None:
    rows, links = _rows(conn, ids), _links(conn, ids)
    SNAPSHOT.write_text(
        json.dumps({"events": rows, "links": links}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    q = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM kg_event_links WHERE from_event_id IN ({q})"
        f" OR to_event_id IN ({q})", ids + ids)
    conn.execute(f"DELETE FROM kg_events WHERE id IN ({q})", ids)
    conn.commit()
    print(f"已删除 events={len(rows)} links={len(links)}；快照：{SNAPSHOT}")


def _rollback(conn: sqlite3.Connection) -> None:
    if not SNAPSHOT.exists():
        raise SystemExit(f"找不到快照：{SNAPSHOT}")
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for table, key in (("kg_events", "events"), ("kg_event_links", "links")):
        for row in snap.get(key) or []:
            cols = ",".join(row)
            marks = ",".join("?" for _ in row)
            conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})",
                         list(row.values()))
    conn.commit()
    print(f"已还原 events={len(snap.get('events') or [])} "
          f"links={len(snap.get('links') or [])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--ids", default=DEFAULT_IDS)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true")
    g.add_argument("--rollback", action="store_true")
    args = ap.parse_args()
    ids = [int(x) for x in args.ids.split(",") if x.strip()]

    conn = sqlite3.connect(args.db)
    try:
        if args.rollback:
            _rollback(conn)
            return
        _print(conn, ids)
        if args.apply:
            _apply(conn, ids)
        else:
            print("\n（dry-run：未删任何行；确认后加 --apply）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
