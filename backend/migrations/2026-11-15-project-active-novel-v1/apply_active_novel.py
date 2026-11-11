# -*- coding: utf-8 -*-
"""给 ``projects`` 加可空 ``active_novel_id``（「当前书」）+ 回填。

口径（见 reports/project-cleanup-6-7-and-novel-scope-2026-11-11.md §F）：

- ``NULL`` = 未指定，读侧不按书过滤 —— 与加列前**逐字一致**；
- 非 ``NULL`` = 指向 ``raw_novels.id`` 的「当前书」；
- 本脚本**只改 ``novel_dev.db`` 的 ``projects`` 表**：加一列 + 回填能判定的
  项目。``generated.db`` 一行不动。

回填规则（数据驱动，不写死项目号）：

1. ``reference_novel_ids`` 为空/非列表 → ``NULL``（没有书可选）；
2. 有参考语料 → 看 ``generated.db.kg_item_group`` 按 ``novel_id`` 的组数：
   **恰好一本书有组**（其余为 0）→ 取该书；否则 ``NULL``（判不出，不猜）。
3. 取到的 id 必须存在于 ``raw_novels``，否则放弃该行。

用法::

    python apply_active_novel.py                 # dry-run（默认）
    python apply_active_novel.py --apply         # 加列 + 回填
    python apply_active_novel.py --rollback      # 值改回 NULL + 删列
    python apply_active_novel.py --db <副本> --backup-dir <目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve()
BACKEND = HERE.parents[2]
APP = BACKEND / "novel_dev.db"
GEN = BACKEND / "data" / "generated.db"
BACKUP_DIR = BACKEND / "backups"
SNAP_DIR = HERE.parent / "snapshots"

TABLE = "projects"
COLUMN = "active_novel_id"


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(db: pathlib.Path, tag: str, backup_dir: pathlib.Path) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{db.stem}-full-{tag}-{_now_ts()}.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out = sqlite3.connect(dst)
    with out:
        con.backup(out)
    out.close()
    con.close()
    return dst


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _md5(rows: list[tuple]) -> str:
    return hashlib.md5(
        json.dumps([tuple(r) for r in rows], ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()


def state(con: sqlite3.Connection) -> dict:
    cols = _cols(con, TABLE)
    has = COLUMN in cols
    select = f'id, title, reference_novel_ids' + (f', "{COLUMN}"' if has else ", NULL")
    rows = con.execute(f'SELECT {select} FROM "{TABLE}" ORDER BY id').fetchall()
    return {
        "columns": cols,
        "has_column": has,
        "project_rows": len(rows),
        "rows_md5": _md5(rows),
        "rows": [tuple(r) for r in rows],
        "schema": [tuple(r) for r in con.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE tbl_name=? ORDER BY type, name", (TABLE,))],
    }


def _refs(raw) -> list[int]:
    if not isinstance(raw, (list, tuple)):
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return []
        else:
            return []
    out = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _groups_by_novel(gen: sqlite3.Connection) -> dict[int, int]:
    """project_id → {novel_id: 组数}（generated.db 是另一条连接，不能 JOIN）。"""
    out: dict[int, dict[int, int]] = {}
    has = "novel_id" in _cols(gen, "kg_item_group")
    col = "novel_id" if has else "NULL"
    for pid, nid, c in gen.execute(
            f"SELECT project_id, {col}, count(*) FROM kg_item_group"
            " GROUP BY project_id, " + col):
        if nid is None:
            continue
        out.setdefault(int(pid), {})[int(nid)] = int(c)
    return out


def decide(refs: list[int], by_novel: dict[int, int],
           known_novels: set[int]) -> tuple[int | None, str]:
    """返回 (active_novel_id, 依据)。判不出就 (None, 原因)。"""
    if not refs:
        return None, "reference_novel_ids 为空/不是列表 → 无书可选"
    counts = {nid: by_novel.get(nid, 0) for nid in refs}
    positive = {nid: c for nid, c in counts.items() if c > 0}
    if not positive:
        return None, f"参考语料 {refs} 里没有任何一本建过物品组 → 判不出"
    if len(positive) > 1:
        return None, (f"参考语料里多本都有物品组 {positive} → 一本项目同时按两本"
                      "建组，单值装不下，留 NULL")
    nid = next(iter(positive))
    if nid not in known_novels:
        return None, f"候选书 {nid} 不在 raw_novels 里 → 放弃"
    zero = [n for n, c in counts.items() if c == 0]
    return nid, (f"参考语料 {refs}；物品组只落在书 {nid}（{positive[nid]} 组），"
                 f"书 {zero} 0 组")


def _known_novels(con: sqlite3.Connection) -> set[int]:
    try:
        return {int(r[0]) for r in con.execute("SELECT id FROM raw_novels")}
    except sqlite3.Error:
        return set()


def apply(db: pathlib.Path, backup_dir: pathlib.Path,
          gen_db: pathlib.Path = GEN) -> dict:
    before_bak = _backup(db, "active-novel", backup_dir)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")
    gen = sqlite3.connect(f"file:{gen_db}?mode=ro", uri=True)

    before = state(con)
    assert not before["has_column"], f"{TABLE}.{COLUMN} 已存在，拒绝重复加列"

    known = _known_novels(con)
    groups = _groups_by_novel(gen)
    gen.close()

    decisions: list[dict] = []
    for pid, title, raw in con.execute(
            f'SELECT id, title, reference_novel_ids FROM "{TABLE}" ORDER BY id'):
        refs = _refs(raw)
        nid, why = decide(refs, groups.get(int(pid), {}), known)
        decisions.append({"id": int(pid), "title": title, "refs": refs,
                          "active_novel_id": nid, "why": why})

    with con:
        con.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{COLUMN}" INTEGER')
        for d in decisions:
            if d["active_novel_id"] is not None:
                con.execute(f'UPDATE "{TABLE}" SET "{COLUMN}"=? WHERE id=?',
                            (d["active_novel_id"], d["id"]))

    after = state(con)
    con.close()

    assert after["has_column"] and after["columns"][-1] == COLUMN, after["columns"]
    assert after["project_rows"] == before["project_rows"]
    assert len(after["columns"]) == len(before["columns"]) + 1

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAP_DIR / f"active_novel_{_now_ts()}.json"
    snap.write_text(json.dumps({
        "ts": _now_ts(), "db": str(db), "generated_db": str(gen_db),
        "backup": str(before_bak), "decisions": decisions,
        "before": before, "after": after,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {"backup": str(before_bak), "snapshot": str(snap),
            "decisions": decisions, "before": before, "after": after}


def rollback(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    before = state(con)
    if not before["has_column"]:
        con.close()
        return {"rolled_back": False, "why": f"{TABLE}.{COLUMN} 不存在，无需回滚"}

    bak = _backup(db, "active-novel-rollback", backup_dir)
    backup = str(bak)
    with con:
        # 先清值再删列：即使 DROP COLUMN 在旧 SQLite 上不支持，值也已复位。
        con.execute(f'UPDATE "{TABLE}" SET "{COLUMN}"=NULL')
        con.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN "{COLUMN}"')
    after = state(con)
    con.close()

    assert not after["has_column"]
    assert after["columns"] == before["columns"][:-1] or COLUMN not in after["columns"]
    assert after["project_rows"] == before["project_rows"]
    return {"rolled_back": True, "backup": backup,
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(APP), help="novel_dev.db 路径（默认生产库）")
    ap.add_argument("--gen-db", default=str(GEN), help="generated.db 路径")
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    backup_dir = pathlib.Path(args.backup_dir)

    if args.rollback:
        print(json.dumps(rollback(db, backup_dir), ensure_ascii=False,
                         indent=2, default=str))
        return 0

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = state(con)
    con.close()
    print(json.dumps({"db": str(db), "has_column": cur["has_column"],
                      "columns": cur["columns"], "project_rows": cur["project_rows"]},
                     ensure_ascii=False, indent=2))
    if not args.apply:
        gen = sqlite3.connect(f"file:{args.gen_db}?mode=ro", uri=True)
        groups = _groups_by_novel(gen)
        gen.close()
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        known = _known_novels(con)
        plan = []
        for pid, title, raw in con.execute(
                f'SELECT id, title, reference_novel_ids FROM "{TABLE}" ORDER BY id'):
            refs = _refs(raw)
            nid, why = decide(refs, groups.get(int(pid), {}), known)
            plan.append({"id": pid, "title": title, "refs": refs,
                         "active_novel_id": nid, "why": why})
        con.close()
        print("\n回填计划:")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    out = apply(db, backup_dir, pathlib.Path(args.gen_db))
    print("\n备份:", out["backup"])
    print("快照:", out["snapshot"])
    print("\n回填:")
    print(json.dumps(out["decisions"], ensure_ascii=False, indent=2))
    print("\n改前列:", out["before"]["columns"])
    print("改后列:", out["after"]["columns"])
    print("行数:", out["before"]["project_rows"], "→", out["after"]["project_rows"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
