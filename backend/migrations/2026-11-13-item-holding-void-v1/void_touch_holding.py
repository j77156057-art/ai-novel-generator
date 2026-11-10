# -*- coding: utf-8 -*-
"""作废「触摸」误登记的持有段 ``kg_item_holding.id=56``。

背景：ch98「……甚至登上神坛，大不敬的触碰先祖法相，触摸那柄黄铜剑」只是**查看/触摸**，
不是持有。该行以 ``valid_from=98, valid_to=NULL`` 登记成「元景帝 own 黄铜剑」，
会让 98 章起一直算作持有，与 ch106「飞入元景帝手中」的真实交接冲突。

处理：**整行移除**，不改成 ``[98,106)``——改成闭区间等于宣称他持有过 98–106，
与文本相反。98 章起没有这条段，读通道回到「无记录」（≠ 无主）。

可追溯：移除前把整行逐字段快照写到本目录 ``holding_56_snapshot.json``，
报告里列原值；回滚时按快照 INSERT 回 ``kg_item_holding`` 即可（备份在
``backend/backups/``）。

边界：只删这一行。``kg_holding_story_segment`` 里引用该行的段会先检查，
有引用就中止（本轮实测 0 行）；其他持有段、``kg_relations`` 一行不动。

用法::

    python backend/migrations/2026-11-13-item-holding-void-v1/void_touch_holding.py --dry-run
    python backend/migrations/2026-11-13-item-holding-void-v1/void_touch_holding.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
GEN_DB = PROJ / "backend" / "data" / "generated.db"
BACKUP_DIR = PROJ / "backend" / "backups"
SNAPSHOT = HERE / "holding_56_snapshot.json"
TARGET_ID = 56


def _backup(path: pathlib.Path) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-pre-void56-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _row_dict(con: sqlite3.Connection, holding_id: int) -> dict | None:
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM kg_item_holding WHERE id=?", (holding_id,)).fetchone()
    return dict(row) if row else None


def _segment_refs(con: sqlite3.Connection, holding_id: int) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(
        "SELECT * FROM kg_holding_story_segment WHERE holding_id=?", (holding_id,))]


def run(gen_path: pathlib.Path, dry_run: bool) -> dict:
    con = sqlite3.connect(gen_path)
    con.row_factory = sqlite3.Row
    try:
        row = _row_dict(con, TARGET_ID)
        if row is None:
            return {"action": "skip", "why": f"id={TARGET_ID} 不存在（已作废或未生成）"}
        refs = _segment_refs(con, TARGET_ID)
        if refs:
            return {"action": "abort", "why": f"kg_holding_story_segment 仍有 {len(refs)} 行引用，"
                                              "先处理旁注段再作废", "refs": refs}
        snapshot = {
            "table": "kg_item_holding",
            "id": TARGET_ID,
            "reason": "ch98 只是触摸/查看，不构成持有；整行作废，不改成 [98,106)",
            "taken_at": datetime.now().isoformat(timespec="seconds"),
            "row": row,
        }
        SNAPSHOT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        if dry_run:
            return {"action": "would_delete", "snapshot_path": str(SNAPSHOT), "row": row,
                    "segment_refs": len(refs)}
        con.execute("DELETE FROM kg_item_holding WHERE id=?", (TARGET_ID,))
        con.commit()
        return {"action": "deleted", "snapshot_path": str(SNAPSHOT), "row": row,
                "segment_refs": len(refs)}
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(GEN_DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    gen_path = pathlib.Path(args.db)
    if not args.dry_run and not args.no_backup:
        print("backup:", _backup(gen_path))
    report = run(gen_path, args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "target": TARGET_ID, "report": report},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
