# -*- coding: utf-8 -*-
"""给 ``projects`` 加可空 ``continuation_novel_id``（「续写书」）。

与 ``active_novel_id``（事实来源书）并存，回答的是另一个问题：

- ``active_novel_id``：这个项目的书级事实（物品组/写法/持有段）来自哪本书；
- ``continuation_novel_id``：这个项目正在从哪本书往后写；原创项目为 ``NULL``。

两边都可空；``NULL`` = 未指定。本脚本**只改 ``novel_db.db`` 的 ``projects``
表**（加一列 + 回填），``generated.db`` 一行不动，生成链路/读侧/前端/API 都不接。

回填规则（数据驱动，不写死项目号）：

库内**没有任何「续写」证据**——``generations`` 只有 ``project_id/segment_no``，
段号是项目内计数，``chapter_range`` 存的是「段N」，没有章号也没有书绑定。所以：

1. ``reference_novel_ids`` 为空 → ``NULL``（没有书可选）；
2. 有语料 → 仍需逐项看证据：正文是原创、还是某本书的续写；判不出 → ``NULL``。
   当前 5 个项目全部落在这一档。

用法::

    python apply_continuation_novel.py                 # dry-run（默认）
    python apply_continuation_novel.py --apply         # 加列 + 回填
    python apply_continuation_novel.py --rollback      # 值改回 NULL + 删列
    python apply_continuation_novel.py --db <副本> --backup-dir <目录>
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
BACKUP_DIR = BACKEND / "backups"
SNAP_DIR = HERE.parent / "snapshots"

TABLE = "projects"
COLUMN = "continuation_novel_id"


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
    select = 'id, title, reference_novel_ids, active_novel_id' + (
        f', "{COLUMN}"' if has else ", NULL")
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
    if isinstance(raw, (list, tuple)):
        items = raw
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(items, (list, tuple)):
            return []
    else:
        return []
    out = []
    for x in items:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _gen_facts(con: sqlite3.Connection, project_id: int) -> dict:
    rows = con.execute(
        "SELECT segment_no, chapter_range, substr(text,1,40) FROM generations"
        " WHERE project_id=? ORDER BY segment_no", (project_id,)).fetchall()
    return {"segments": len(rows),
            "chapter_range": [r[1] for r in rows],
            "heads": [r[2] for r in rows]}


def _distill_books(con: sqlite3.Connection, project_id: int) -> list[int]:
    """蒸馏覆盖了哪几本书。``distill_chunks`` 在 ``novel_dev.db``（不是 generated）。"""
    try:
        return sorted({int(r[0]) for r in con.execute(
            "SELECT DISTINCT novel_id FROM distill_chunks WHERE project_id=?"
            " AND novel_id IS NOT NULL", (project_id,))})
    except sqlite3.Error:
        return []


def decide(refs: list[int], facts: dict, distilled: list[int]) -> tuple[int | None, str]:
    """返回 (continuation_novel_id, 依据)。没有续写证据就 (None, 原因)。"""
    if not refs:
        return None, "reference_novel_ids 为空/不是列表 → 无书可选"
    if facts["segments"] == 0:
        return None, f"参考语料 {refs}，但没有正文（0 段）→ 没有可判定的续写"
    ranges = facts["chapter_range"]
    if not any("章" in str(r) for r in ranges):
        return None, (
            f"参考语料 {refs}，{facts['segments']} 段正文的 chapter_range 是 "
            f"{ranges}（段标签，无章号）；蒸馏覆盖 {distilled or '无'} 本 → "
            "与任何一本都没有章节续接关系，判不出")
    return None, (f"参考语料 {refs}，chapter_range={ranges} 含章号，但库内没有"
                  "书↔章节的绑定字段，无法确认续写哪本 → 留 NULL")


def apply(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    before_bak = _backup(db, "continuation-novel", backup_dir)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")

    before = state(con)
    assert not before["has_column"], f"{TABLE}.{COLUMN} 已存在，拒绝重复加列"

    decisions: list[dict] = []
    for pid, title, raw in con.execute(
            f'SELECT id, title, reference_novel_ids FROM "{TABLE}" ORDER BY id'):
        refs = _refs(raw)
        nid, why = decide(refs, _gen_facts(con, int(pid)),
                          _distill_books(con, int(pid)))
        decisions.append({"id": int(pid), "title": title, "refs": refs,
                          "continuation_novel_id": nid, "why": why})

    with con:
        con.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{COLUMN}" INTEGER')
        for d in decisions:
            if d["continuation_novel_id"] is not None:
                con.execute(f'UPDATE "{TABLE}" SET "{COLUMN}"=? WHERE id=?',
                            (d["continuation_novel_id"], d["id"]))

    after = state(con)
    con.close()

    assert after["has_column"] and after["columns"][-1] == COLUMN, after["columns"]
    assert after["project_rows"] == before["project_rows"]
    assert len(after["columns"]) == len(before["columns"]) + 1

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAP_DIR / f"continuation_novel_{_now_ts()}.json"
    snap.write_text(json.dumps({
        "ts": _now_ts(), "db": str(db),
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

    bak = _backup(db, "continuation-novel-rollback", backup_dir)
    with con:
        con.execute(f'UPDATE "{TABLE}" SET "{COLUMN}"=NULL')
        con.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN "{COLUMN}"')
    after = state(con)
    con.close()

    assert not after["has_column"]
    assert COLUMN not in after["columns"]
    assert after["project_rows"] == before["project_rows"]
    return {"rolled_back": True, "backup": str(bak),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(APP), help="novel_dev.db 路径（默认生产库）")
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
                      "columns": len(cur["columns"]),
                      "tail": cur["columns"][-3:],
                      "project_rows": cur["project_rows"]},
                     ensure_ascii=False, indent=2))
    if not args.apply:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        plan = []
        for pid, title, raw in con.execute(
                f'SELECT id, title, reference_novel_ids FROM "{TABLE}" ORDER BY id'):
            refs = _refs(raw)
            nid, why = decide(refs, _gen_facts(con, int(pid)),
                              _distill_books(con, int(pid)))
            plan.append({"id": pid, "title": title, "refs": refs,
                         "continuation_novel_id": nid, "why": why})
        con.close()
        print("\n回填计划:")
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    out = apply(db, backup_dir)
    print("\n备份:", out["backup"])
    print("快照:", out["snapshot"])
    print("\n回填:")
    print(json.dumps(out["decisions"], ensure_ascii=False, indent=2))
    print("\n列数:", len(out["before"]["columns"]), "→", len(out["after"]["columns"]))
    print("行数:", out["before"]["project_rows"], "→", out["after"]["project_rows"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
