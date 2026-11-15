# -*- coding: utf-8 -*-
"""物品同物组 v1 回填：把持有段/旁注段按写法登记挂到组上。

范围（``reports/item-canonical-design.md`` 第 5 节第 2 步）：

1. 给 ``kg_item_holding`` 与 ``kg_holding_story_segment`` 补可空 ``group_id``
   （旧库用 ALTER，已存在则跳过）；
2. 按 ``kg_item_surface``（approved + 组 active）把现有行的 ``item_name``
   解析成组并写入 ``group_id``；解析不到的行保持 NULL，逐条给出原因；
3. **只改 group_id**：不新增、不删除、不改持有段区间，也不做任何裁决；
4. 扫描回填后同组并行的开放段冲突（同组同 hold_type 同实例、不同写法），
   只报告，不改库。

幂等：只更新 ``group_id IS NULL`` 的行；重复跑第二遍新增 0。

用法::

    python .../apply_group_backfill.py --dry-run
    python .../apply_group_backfill.py
    python .../apply_group_backfill.py --db <副本.db>
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
GEN_DB = PROJ / "backend" / "data" / "generated.db"

TABLES = ("kg_item_holding", "kg_holding_story_segment")


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def ensure_columns(con: sqlite3.Connection) -> list[str]:
    made = []
    for table in TABLES:
        if "group_id" not in _columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN group_id INTEGER")
            made.append(f"{table}.group_id")
    con.commit()
    return made


def load_registry(con: sqlite3.Connection) -> dict:
    """写法 → 组。只认 approved 写法且组为 active 单件组。"""
    rows = con.execute(
        "SELECT s.surface, s.status AS s_status, g.id AS gid,"
        "       g.status AS g_status, g.canonical_name"
        "  FROM kg_item_surface s JOIN kg_item_group g ON g.id = s.group_id"
    ).fetchall()
    approved_active: dict[str, int] = {}
    approved_generic: set[str] = set()
    pending: set[str] = set()
    registered: set[str] = set()
    for row in rows:
        surface = row[0]
        registered.add(surface)
        if row[1] == "approved" and row[3] == "active":
            approved_active.setdefault(surface, int(row[2]))
        elif row[1] == "approved" and row[3] == "generic":
            approved_generic.add(surface)
        elif row[1] == "pending":
            pending.add(surface)
    # 规范名本身也算写法（老库里不一定有对应 surface 行）
    for row in con.execute(
            "SELECT id, canonical_name FROM kg_item_group WHERE status = 'active'"):
        approved_active.setdefault(row[1], int(row[0]))
    return {"map": approved_active, "generic": approved_generic,
            "pending": pending, "registered": registered}


def explain(registry: dict, name: str) -> str:
    if name in registry["map"]:
        return ""
    if name in registry["generic"]:
        return "只命中通称组（类名），不挂单件"
    if name in registry["pending"]:
        return "写法登记为 pending，未审核"
    return "写法未登记到任何组"


def table_counts(con: sqlite3.Connection, table: str) -> dict:
    row = con.execute(
        f"SELECT COUNT(*) AS total,"
        f"       SUM(CASE WHEN group_id IS NOT NULL THEN 1 ELSE 0 END) AS nonnull,"
        f"       SUM(CASE WHEN group_id IS NULL THEN 1 ELSE 0 END) AS nulls"
        f"  FROM {table}"
    ).fetchone()
    return {"total": row[0], "nonnull": row[1] or 0, "null": row[2] or 0}


def backfill(con: sqlite3.Connection, table: str, registry: dict,
             dry_run: bool) -> dict:
    rows = con.execute(
        f"SELECT id, item_name FROM {table} WHERE group_id IS NULL ORDER BY id"
    ).fetchall()
    updated, unresolved = 0, []
    for rid, name in rows:
        gid = registry["map"].get((name or "").strip())
        if gid is None:
            unresolved.append({"id": rid, "item_name": name,
                               "reason": explain(registry, (name or "").strip())})
            continue
        if not dry_run:
            con.execute(f"UPDATE {table} SET group_id = ? WHERE id = ? AND group_id IS NULL",
                        (gid, rid))
        updated += 1
    if not dry_run:
        con.commit()
    return {"updated": updated, "unresolved": unresolved}


def open_conflicts(con: sqlite3.Connection) -> list[dict]:
    """同组 + 同 hold_type + 同实例下，两个不同写法同时开放。只读。"""
    keys = con.execute(
        "SELECT group_id, hold_type, COALESCE(instance_id, -1) AS iid,"
        "       COUNT(DISTINCT item_name) AS n"
        "  FROM kg_item_holding"
        " WHERE valid_to IS NULL AND group_id IS NOT NULL"
        " GROUP BY group_id, hold_type, COALESCE(instance_id, -1)"
        "HAVING n > 1 ORDER BY group_id, hold_type"
    ).fetchall()
    out = []
    for gid, hold_type, iid, _n in keys:
        rows = con.execute(
            "SELECT id, item_name, instance_id, owner_name, valid_from, valid_to"
            "  FROM kg_item_holding"
            " WHERE group_id = ? AND hold_type = ?"
            "   AND COALESCE(instance_id, -1) = ? AND valid_to IS NULL"
            " ORDER BY item_name, id", (gid, hold_type, iid)).fetchall()
        out.append({
            "group_id": gid,
            "hold_type": hold_type,
            "instance_id": None if iid == -1 else iid,
            "rows": [{"id": r[0], "item_name": r[1], "owner_name": r[3],
                      "valid_from": r[4], "valid_to": r[5]} for r in rows],
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="同物组 v1：回填 group_id")
    ap.add_argument("--db", default=str(GEN_DB))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    db = pathlib.Path(args.db)

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        has_registry = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kg_item_surface'"
        ).fetchone() is not None
        if not has_registry:
            raise SystemExit("没有 kg_item_surface：先跑 2026-11-11-item-group-v1 建表迁移")

        made = ensure_columns(con)
        registry = load_registry(con)
        print(f"target={db}")
        print(f"新增列：{made or '无'}")
        print(f"可解析写法 {len(registry['map'])} 个")

        before = {t: table_counts(con, t) for t in TABLES}
        results = {t: backfill(con, t, registry, args.dry_run) for t in TABLES}
        after = {t: table_counts(con, t) for t in TABLES}

        for table in TABLES:
            print(f"\n[{table}]")
            print(f"  回填前：{before[table]}")
            print(f"  回填后：{after[table]}")
            print(f"  本次写入 group_id：{results[table]['updated']}")
            for item in results[table]["unresolved"]:
                print(f"  [未解析] id={item['id']} 「{item['item_name']}」：{item['reason']}")

        conflicts = open_conflicts(con)
        print(f"\n[同组并行开放段冲突] {len(conflicts)} 组")
        for c in conflicts:
            print(f"  组#{c['group_id']} hold_type={c['hold_type']} "
                  f"instance={c['instance_id']}")
            for r in c["rows"]:
                print(f"    - {r['item_name']}（holder={r['owner_name']}, "
                      f"[{r['valid_from']}, {r['valid_to']})）")
        if conflicts:
            print("  以上只报告，不自动裁决、不删行。")

        if args.dry_run:
            print("\n[dry-run] 未写库。")
        else:
            print("\n完成。")
        print("\nJSON:", json.dumps(
            {"before": before, "after": after,
             "updated": {t: results[t]["updated"] for t in TABLES},
             "unresolved": {t: results[t]["unresolved"] for t in TABLES},
             "conflicts": conflicts}, ensure_ascii=False))
    finally:
        con.close()


if __name__ == "__main__":
    main()
