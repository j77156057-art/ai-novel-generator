# -*- coding: utf-8 -*-
"""清理 kg_relations 里**确证跨书**的边（默认 dry-run，--apply 落快照后删除）。

判定口径（与存量回扫一致，只认确证，不猜）：

- ``keep``      两端实体名都出现在本项目任一 generation 正文里
- ``confirmed`` 两端都不在本项目任何正文里 —— 只有这一类会被删除
- ``doubtful``  只剩一端命中 / 端点实体缺失 / 本项目根本没有正文可比对
                —— 一律保留，并列出 id、两端实体、具体理由供人工复核

关系表没有段号，无法像 kg_events 那样逐段比对；因此「本项目任一正文」
是当前能拿到的最强证据。**没有任何正文的项目全部判存疑**，避免把
导入/待生成项目的关系误删。

用法：
    python cleanup_cross_book_relations.py --db ../../novel_dev.db
    python cleanup_cross_book_relations.py --db ../../novel_dev.db --apply
    python cleanup_cross_book_relations.py --db ../../novel_dev.db --rollback
    python cleanup_cross_book_relations.py --db ../../novel_dev.db --project 5
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[2] / "novel_dev.db"
SNAPSHOT = Path(__file__).with_name("snapshot_cross_book_relations.json")

REL_COLS = ["id", "project_id", "from_id", "to_id", "rel_type", "strength",
            "since_chapter", "description", "confidence", "status",
            "created_at", "updated_at"]
EVO_COLS = ["id", "project_id", "edge_id", "chapter", "strength", "note",
            "created_at"]


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _project_text(conn: sqlite3.Connection) -> dict[int, str]:
    texts: dict[int, str] = {}
    for r in conn.execute("select project_id, text from generations"):
        texts[r[0]] = texts.get(r[0], "") + "\n" + (r[1] or "")
    return texts


def classify(conn: sqlite3.Connection, project: int | None = None):
    """返回 (keep, confirmed, doubtful)，每项是 (row, from_name, to_name, why)。"""
    texts = _project_text(conn)
    ents = {r["id"]: dict(r) for r in conn.execute(
        "select id, canonical_name from kg_entities")}
    sql = "select * from kg_relations"
    args: list = []
    if project is not None:
        sql += " where project_id=?"
        args.append(project)
    sql += " order by project_id, id"
    keep, confirmed, doubtful = [], [], []
    for r in conn.execute(sql, args):
        f = ents.get(r["from_id"])
        t = ents.get(r["to_id"])
        fn = (f or {}).get("canonical_name")
        tn = (t or {}).get("canonical_name")
        text = texts.get(r["project_id"], "")
        if not text:
            item = (r, fn, tn, "本项目没有任何 generation 正文，无法比对")
            doubtful.append(item)
        elif not f or not t:
            item = (r, fn, tn,
                    f"端点实体缺失 from={r['from_id']} to={r['to_id']}")
            doubtful.append(item)
        elif fn in text and tn in text:
            keep.append((r, fn, tn, "两端都在本项目正文"))
        elif fn not in text and tn not in text:
            confirmed.append((r, fn, tn, "两端都不在本项目正文"))
        else:
            hit = fn if fn in text else tn
            doubtful.append((r, fn, tn, f"仅一端命中：{hit}"))
    return keep, confirmed, doubtful


def _print_report(keep, confirmed, doubtful) -> None:
    print(f"\n== 确证跨书（将删除）：{len(confirmed)} 条 ==")
    for r, fn, tn, why in confirmed:
        print(f"  #{r['id']:<4} project={r['project_id']:<3} {fn} -> {tn} "
              f"[{r['rel_type']}/{r['status']}] {why}")
    print(f"\n== 存疑（保留，待人工复核）：{len(doubtful)} 条 ==")
    for r, fn, tn, why in doubtful:
        print(f"  #{r['id']:<4} project={r['project_id']:<3} {fn} -> {tn} "
              f"[{r['rel_type']}/{r['status']}] {why}")
    print(f"\n== 保留：{len(keep)} 条 ==")


def apply(conn: sqlite3.Connection, project: int | None = None) -> None:
    keep, confirmed, doubtful = classify(conn, project)
    _print_report(keep, confirmed, doubtful)
    if not confirmed:
        print("\n没有确证跨书关系，无需删除。")
        return
    ids = [r["id"] for r, _f, _t, _w in confirmed]
    ph = ",".join("?" * len(ids))
    evo = [dict(r) for r in conn.execute(
        f"select * from kg_relation_evolution where edge_id in ({ph})", ids)]
    snapshot = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "criteria": ("两端实体名都不在本项目任一 generation 正文里；"
                     "仅一端命中/端点缺失/无正文可比对 = 存疑，不删"),
        "project_filter": project,
        "relations": [dict(r) for r, _f, _t, _w in confirmed],
        "endpoints": {str(r["id"]): {"from": fn, "to": tn, "reason": why}
                      for r, fn, tn, why in confirmed},
        "relation_evolution": evo,
    }
    SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
    print(f"\n已落整行快照：{SNAPSHOT}（关系 {len(ids)} 行、演变史 {len(evo)} 行）")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("delete from kg_relations where id=?",
                         [(i,) for i in ids])
        if evo:
            conn.executemany("delete from kg_relation_evolution where id=?",
                             [(e["id"],) for e in evo])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(f"已删除 {len(ids)} 条确证跨书关系（id={ids}）")
    for pid in sorted({r["project_id"] for r, _f, _t, _w in confirmed}):
        n = conn.execute("select count(*) from kg_relations where project_id=?",
                         (pid,)).fetchone()[0]
        print(f"  project={pid} 剩余关系：{n}")


def rollback(conn: sqlite3.Connection) -> None:
    if not SNAPSHOT.exists():
        print(f"快照不存在：{SNAPSHOT}")
        return
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rels = data.get("relations") or []
    evo = data.get("relation_evolution") or []
    restored = 0
    for r in rels:
        cols = [c for c in REL_COLS if c in r]
        conn.execute(
            f"insert or ignore into kg_relations ({','.join(cols)}) "
            f"values ({','.join('?' * len(cols))})",
            [r[c] for c in cols])
        restored += conn.execute("select changes()").fetchone()[0]
    for e in evo:
        cols = [c for c in EVO_COLS if c in e]
        conn.execute(
            f"insert or ignore into kg_relation_evolution ({','.join(cols)}) "
            f"values ({','.join('?' * len(cols))})",
            [e[c] for c in cols])
    conn.commit()
    print(f"已从快照回滚：关系恢复 {restored}/{len(rels)}，"
          f"演变史快照 {len(evo)} 行")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--project", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    conn = _connect(Path(args.db))
    try:
        if args.rollback:
            rollback(conn)
        elif args.apply:
            apply(conn, args.project)
        else:
            _print_report(*classify(conn, args.project))
            print("\n这是 dry-run，未改任何数据。确认无误后加 --apply。")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
