# -*- coding: utf-8 -*-
"""一次性迁移：v3 判定结果 → kg_item_holding。

数据源：``holdings_v3.json``（由 ``reports/kg-holding-migration.md`` §7 生成，
每条含原始写法 + 归一名 + 判定 + 来源边 id）。

规则：
- 只灌 ``verdict == "转持有"`` 的 112 条；判错、存疑一条不写；
- 写入只走 ``generated_store.record_holding``，不直接 INSERT；
- 同一 (物品, 持有类型) 内按章推进；同章先处理「上一段的持有者」再处理新持有者，
  否则 地书 ch74 的「地宗复述」会把刚开的「天地会」段删掉；
- 幂等：已有同名同起点、且覆盖本章的段直接跳过，不重开段、不删段；
- ``--dry-run`` 在事务里跑完再回滚，给出与真实写入一致的动作计数。

用法（在 backend 下）：
    python migrations/2026-11-10-item-holding-v3/migrate_v3_item_holding.py --dry-run
    python migrations/2026-11-10-item-holding-v3/migrate_v3_item_holding.py
    python migrations/2026-11-10-item-holding-v3/migrate_v3_item_holding.py --verify-only
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND))

from app.kg import generated_store as store  # noqa: E402

DEFAULT_JSON = HERE / "holdings_v3.json"
DEFAULT_DB = BACKEND / "data" / "generated.db"


def load_rows(path: pathlib.Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["rows"] if isinstance(payload, dict) else payload
    return [r for r in rows if r["verdict"] == "转持有"]


def order_rows(rows: list[dict]) -> list[dict]:
    """(物品, hold_type) 内按章推进；同章内「当前持有者」优先，避免复述行触发同章竞争。"""
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        groups[(r["item_name"], r["hold_type"])][r["valid_from"]].append(r)

    out: list[dict] = []
    for key in sorted(groups):
        cur = None
        for ch in sorted(groups[key]):
            batch = sorted(groups[key][ch],
                           key=lambda r: (0 if r["holder_name"] == cur else 1, r["seq"]))
            for r in batch:
                out.append(r)
                if r["holder_name"]:
                    cur = r["holder_name"]
    return out


def _covered(con, r: dict) -> bool:
    """该主张是否已被表中同主段覆盖（含「在本段关闭章复述」的情况）。"""
    row = con.execute(
        """SELECT 1 FROM kg_item_holding
            WHERE item_name = ? AND hold_type = ?
              AND COALESCE(owner_name, '') = COALESCE(?, '')
              AND owner_state = ?
              AND valid_from <= ? AND (valid_to IS NULL OR valid_to >= ?)
            LIMIT 1""",
        (r["item_name"], r["hold_type"], r["holder_name"],
         r["owner_state"], r["valid_from"], r["valid_from"]),
    ).fetchone()
    return row is not None


def apply(con, rows: list[dict]) -> dict:
    stats = collections.Counter()
    items: set[str] = set()
    clashes: list[dict] = []
    for r in rows:
        if _covered(con, r):
            stats["no_new_segment"] += 1
            continue
        res = store.record_holding(
            con, r["item_name"], r["hold_type"], r["holder_name"], r["valid_from"],
            owner_kind=r.get("owner_kind"),
            owner_state=r["owner_state"],
            transfer_event_id=r.get("transfer_event_id"),
            reason_code=r["reason_code"],
            evidence=r.get("evidence", ""),
        )
        stats[res["action"]] += 1
        if res["action"] == "inserted":
            items.add(r["item_name"])
            if res["closed"]:
                stats["closed_segments"] += res["closed"]
        if res["action"] == "candidate":
            clashes.append({"item": r["item_name"], "hold_type": r["hold_type"],
                            "chapter": r["valid_from"], "holder": r["holder_name"]})
    return {"stats": dict(stats), "items": len(items), "clashes": clashes}


def verify(con, rows: list[dict]) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    # 1) 地书 own / custody 双线
    def segs(item, ht):
        return [(r["valid_from"], r["valid_to"], r["owner_name"])
                for r in con.execute(
                    """SELECT valid_from, valid_to, owner_name FROM kg_item_holding
                        WHERE item_name = ? AND hold_type = ? ORDER BY valid_from, id""",
                    (item, ht))]

    own = segs("地书", "own")
    custody = segs("地书", "custody")
    want_own = [(73, 74, "地宗"), (74, None, "天地会")]
    want_cus = [(73, 74, "金莲师兄"), (74, None, "许七安")]
    out.append(("地书 own", own == want_own, f"{own} == {want_own}"))
    out.append(("地书 custody", custody == want_cus, f"{custody} == {want_cus}"))

    # 2) 单段物品：每件 >=1 行且末段开口
    holders = collections.defaultdict(set)
    for r in rows:
        holders[r["item_name"]].add(r["holder_name"])
    singles = sorted(k for k, v in holders.items() if len(v) == 1)
    bad = []
    for item in singles:
        seg = segs(item, "own") + segs(item, "custody")
        if not seg:
            bad.append((item, "no_rows"))
        elif not all(s[1] is not None for s in seg[:-1]) or seg[-1][1] is not None:
            bad.append((item, seg))
    out.append((f"单段物品 {len(singles)} 件（原始写法 44 → 归一后 {len(singles)}）",
                not bad, f"异常 {bad}" if bad else "all ok，末段均 valid_to IS NULL"))

    # 3) 神剑 / 地书碎片 0 行
    for item in ("神剑", "地书碎片", "三号碎片"):
        n = con.execute("SELECT COUNT(*) FROM kg_item_holding WHERE item_name = ?",
                        (item,)).fetchone()[0]
        out.append((f"{item} 0 行", n == 0, f"实际 {n} 行"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(DEFAULT_JSON))
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    rows = order_rows(load_rows(pathlib.Path(args.json)))
    db_path = pathlib.Path(args.db)
    exists = db_path.exists()

    if exists or not args.dry_run:
        # 真实写入（或已有库）都落到文件上；只有「库还不存在 + 干跑」才用内存库
        con = store.connect(db_path)
        store.ensure_schema(con)
    else:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        store.ensure_schema(con)

    mode = "dry-run" if args.dry_run else ("verify-only" if args.verify_only else "write")
    print(f"[{mode}] db={db_path} exists={exists} rows={len(rows)}")

    if args.verify_only:
        for name, ok, detail in verify(con, rows):
            print(("PASS " if ok else "FAIL ") + name + " | " + detail)
        return 0

    if args.dry_run:
        con.execute("BEGIN")
        result = apply(con, rows)
        con.rollback()
    else:
        result = apply(con, rows)
        con.commit()

    print("actions:", json.dumps(result["stats"], ensure_ascii=False, sort_keys=True))
    print("items covered:", result["items"])
    print("same-chapter clashes:", json.dumps(result["clashes"], ensure_ascii=False))
    total = con.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0]
    print("kg_item_holding rows:", total)

    if not args.dry_run:
        print("--- verify ---")
        for name, ok, detail in verify(con, rows):
            print(("PASS " if ok else "FAIL ") + name + " | " + detail)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
