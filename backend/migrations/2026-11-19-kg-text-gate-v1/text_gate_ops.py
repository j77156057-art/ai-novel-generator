# -*- coding: utf-8 -*-
"""写侧文本归属门：隔离表建表 + 查询/统计/回放/清理（可回滚）。

隔离表 ``kg_ingest_quarantine`` 只增不改业务表：

- ``--apply``   幂等建表（只 CREATE IF NOT EXISTS，不碰任何现有数据）
- ``--stats``   按项目/类型/原因统计「被拦下多少条」
- ``--list``    列出隔离行（可按项目/类型/原因过滤）
- ``--restore`` 按 id 把整行快照回放到业务表（可回滚方向一：恢复）
- ``--purge``   按 id 删除隔离行并落快照（可回滚方向二：确认重新抽取后清痕）

注意：本脚本不 import app.*，可对任意副本/老库直接跑。

用法：
    python text_gate_ops.py --db ../../novel_dev.db --apply
    python text_gate_ops.py --db ../../novel_dev.db --stats
    python text_gate_ops.py --db ../../novel_dev.db --list --project 5
    python text_gate_ops.py --db ../../novel_dev.db --restore 3
    python text_gate_ops.py --db ../../novel_dev.db --purge 3
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "novel_dev.db"
SNAPSHOT = Path(__file__).with_name("snapshot_purged_quarantine.json")

DDL = [
    """
    CREATE TABLE IF NOT EXISTS kg_ingest_quarantine (
        id INTEGER NOT NULL PRIMARY KEY,
        project_id INTEGER NOT NULL,
        run_id INTEGER,
        segment_no INTEGER,
        kind VARCHAR(24) NOT NULL,
        reason VARCHAR(32) NOT NULL,
        detail JSON,
        created_at DATETIME,
        FOREIGN KEY(project_id) REFERENCES projects (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_kg_quarantine_project_kind "
    "ON kg_ingest_quarantine (project_id, kind, reason)",
    "CREATE INDEX IF NOT EXISTS ix_kg_quarantine_run "
    "ON kg_ingest_quarantine (run_id)",
]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def apply(conn: sqlite3.Connection) -> None:
    before = _table_exists(conn)
    for sql in DDL:
        conn.execute(sql)
    conn.commit()
    print(("已存在，DDL 幂等跳过检查：" if before else "已创建：")
          + "kg_ingest_quarantine")
    print("列：", [r[1] for r in conn.execute(
        "PRAGMA table_info(kg_ingest_quarantine)")])


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='kg_ingest_quarantine'").fetchone() is not None


def stats(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn):
        print("隔离表不存在（先跑 --apply）")
        return
    rows = conn.execute(
        "SELECT project_id, kind, reason, count(*) n FROM kg_ingest_quarantine "
        "GROUP BY project_id, kind, reason ORDER BY project_id, kind, reason"
    ).fetchall()
    if not rows:
        print("隔离区为空：0 条。")
        return
    total = sum(r["n"] for r in rows)
    print(f"隔离总计 {total} 条")
    for r in rows:
        print(f"  project={r['project_id']:<3} kind={r['kind']:<16} "
              f"reason={r['reason']:<14} n={r['n']}")


def list_rows(conn: sqlite3.Connection, *, project: int | None,
              kind: str | None, reason: str | None, limit: int) -> None:
    if not _table_exists(conn):
        print("隔离表不存在（先跑 --apply）")
        return
    where, args = [], []
    if project is not None:
        where.append("project_id=?")
        args.append(project)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if reason:
        where.append("reason=?")
        args.append(reason)
    sql = "SELECT * FROM kg_ingest_quarantine"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id LIMIT ?"
    args.append(limit)
    for r in conn.execute(sql, args):
        detail = r["detail"]
        try:
            detail = json.loads(detail) if isinstance(detail, str) else detail
        except Exception:
            pass
        print(f"#{r['id']} project={r['project_id']} run={r['run_id']} "
              f"seg={r['segment_no']} kind={r['kind']} reason={r['reason']} "
              f"detail={json.dumps(detail, ensure_ascii=False)[:240]}")


def _entity_id(conn: sqlite3.Connection, project_id: int, name: str,
               kind: str | None = None) -> int | None:
    if not name:
        return None
    if kind:
        row = conn.execute(
            "SELECT id FROM kg_entities WHERE project_id=? AND canonical_name=? "
            "AND kind=? LIMIT 1", (project_id, name, kind)).fetchone()
        if row:
            return row[0]
    row = conn.execute(
        "SELECT id FROM kg_entities WHERE project_id=? AND canonical_name=? "
        "LIMIT 1", (project_id, name)).fetchone()
    return row[0] if row else None


def _mention_grounded(conn: sqlite3.Connection, pid: int,
                      d: dict) -> tuple[bool, str]:
    """回放前的两层守卫（与批量抽取同一口径）。

    第一层（词表/实体）：entity_id 必须属于本项目；
    第二层（文本归属）：必须绑定到真实 generation，且实体名确实出现在该段正文。
    任一层不过就拒绝回放——否则会把「点名字跳不到原文」的坏行重新写回去。
    """
    eid = d.get("entity_id")
    row = conn.execute(
        "SELECT canonical_name FROM kg_entities WHERE id=? AND project_id=?",
        (eid, pid)).fetchone()
    if row is None:
        return False, "entity_id 不存在或不属于本项目"
    name = row["canonical_name"]
    gid = d.get("generation_id")
    if gid is None:
        return False, "没有 generation_id（回放后仍点不到原文）"
    gen = conn.execute("SELECT text, segment_no FROM generations WHERE id=?",
                       (gid,)).fetchone()
    if gen is None:
        return False, f"generation_id={gid} 不存在"
    text = gen["text"] or ""
    if not name or name not in text:
        return False, f"实体名「{name}」未出现在 generation={gid} 正文"
    seg = d.get("segment_no")
    if seg is not None and gen["segment_no"] is not None and seg != gen["segment_no"]:
        return False, f"segment_no={seg} 与 generation 的段号 {gen['segment_no']} 不符"
    return True, ""


def restore(conn: sqlite3.Connection, rid: int) -> None:
    """把一条隔离行按快照回放到业务表。"""
    r = conn.execute("SELECT * FROM kg_ingest_quarantine WHERE id=?",
                     (rid,)).fetchone()
    if r is None:
        print(f"#{rid} 不存在")
        return
    d = r["detail"]
    if isinstance(d, str):
        d = json.loads(d or "{}")
    d = d or {}
    pid = r["project_id"]
    kind = r["kind"]
    if kind == "mention":
        if not d.get("entity_id"):
            print(f"#{rid} mention 缺 entity_id，无法回放")
            return
        ok, why = _mention_grounded(conn, pid, d)
        if not ok:
            print(f"#{rid} 守卫拒绝回放：{why}")
            return
        conn.execute(
            "INSERT OR IGNORE INTO kg_mentions "
            "(project_id, entity_id, generation_id, novel_id, segment_no, "
            " context_window, mention_kind) VALUES (?,?,?,?,?,?,?)",
            (pid, d.get("entity_id"), d.get("generation_id"), d.get("novel_id"),
             d.get("segment_no"), d.get("context_window"),
             d.get("mention_kind") or "llm"))
    elif kind == "event":
        seq = d.get("sequence")
        if seq is None:
            seq = (conn.execute(
                "SELECT coalesce(max(sequence),0)+1 FROM kg_events "
                "WHERE project_id=?", (pid,)).fetchone()[0])
        conn.execute(
            "INSERT INTO kg_events (project_id, novel_id, sequence, chapter, "
            " segment_no, event_type, summary, subject_id, verb, object_id, "
            " location_id, in_story_date) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, d.get("novel_id"), seq, d.get("chapter"), d.get("segment_no"),
             d.get("event_type") or "incident", d.get("summary"),
             d.get("subject_id"), d.get("verb"), d.get("object_id"),
             d.get("location_id"), d.get("in_story_date")))
    elif kind == "relation":
        a = _entity_id(conn, pid, d.get("from"))
        b = _entity_id(conn, pid, d.get("to"))
        if not a or not b:
            print(f"#{rid} relation 端点解析失败（from={d.get('from')} "
                  f"to={d.get('to')}），请先确认实体存在")
            return
        conn.execute(
            "INSERT OR IGNORE INTO kg_relations "
            "(project_id, from_id, to_id, rel_type, strength, status) "
            "VALUES (?,?,?,?,?,?)",
            (pid, a, b, d.get("rel_type"), d.get("strength", 0.5),
             d.get("status") or "active"))
    elif kind == "entity":
        conn.execute(
            "INSERT OR IGNORE INTO kg_entities "
            "(project_id, kind, canonical_name, aliases, attrs, novel_id) "
            "VALUES (?,?,?,?,?,?)",
            (pid, d.get("kind"), d.get("canonical_name"),
             json.dumps(d.get("aliases") or [], ensure_ascii=False),
             json.dumps(d.get("attrs") or {}, ensure_ascii=False),
             d.get("novel_id")))
    elif kind == "state_change":
        cid = _entity_id(conn, pid, d.get("character"), "character")
        if not cid:
            print(f"#{rid} state_change 角色解析失败：{d.get('character')}")
            return
        conn.execute(
            "INSERT INTO kg_character_state "
            "(project_id, character_id, segment_no, state_type, new_value) "
            "VALUES (?,?,?,?,?)",
            (pid, cid, d.get("segment_no") or r["segment_no"],
             d.get("state_type"), d.get("new_value")))
    elif kind == "foreshadow":
        conn.execute(
            "INSERT OR IGNORE INTO kg_foreshadows "
            "(project_id, name, status, planted_chapter, planned_payoff_chapter) "
            "VALUES (?,?,?,?,?)",
            (pid, d.get("name"), d.get("status") or "planted",
             d.get("planted_chapter"), d.get("planned_payoff_chapter")))
    else:
        print(f"#{rid} kind={kind} 无直接回放路径（event_link/entity_knowledge "
              "请重新抽取，脚本不猜 id）")
        return
    conn.commit()
    print(f"#{rid} 已回放到 {kind} 业务表")


def purge(conn: sqlite3.Connection, ids: list[int]) -> None:
    rows = []
    for rid in ids:
        r = conn.execute("SELECT * FROM kg_ingest_quarantine WHERE id=?",
                         (rid,)).fetchone()
        if r is not None:
            rows.append(dict(r))
    if not rows:
        print("没有可清理的行")
        return
    SNAPSHOT.write_text(json.dumps(
        {"snapshot_at": datetime.now(timezone.utc).isoformat(), "rows": rows},
        ensure_ascii=False, indent=2), encoding="utf-8")
    conn.executemany("DELETE FROM kg_ingest_quarantine WHERE id=?",
                     [(r["id"],) for r in rows])
    conn.commit()
    print(f"已清理 {len(rows)} 行；整行快照：{SNAPSHOT}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--project", type=int)
    ap.add_argument("--kind")
    ap.add_argument("--reason")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--restore", type=int, nargs="+")
    ap.add_argument("--purge", type=int, nargs="+")
    args = ap.parse_args()

    conn = _connect(Path(args.db))
    try:
        if args.apply:
            apply(conn)
        if args.stats:
            stats(conn)
        if args.list:
            list_rows(conn, project=args.project, kind=args.kind,
                      reason=args.reason, limit=args.limit)
        for rid in args.restore or []:
            restore(conn, rid)
        if args.purge:
            purge(conn, args.purge)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
