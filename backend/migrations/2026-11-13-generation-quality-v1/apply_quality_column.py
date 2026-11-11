# -*- coding: utf-8 -*-
"""给 ``generations`` 加可空 ``quality_json``（复读闸门的标记列）。

口径（见 ``docs/repetition-gate-insertion.md`` §5.1）：

- ``NULL`` = **未检测**：历史行、接线前写入的行、P4 回滚恢复的行；
  前端必须显示「未检测」，**不能**显示成「正常」；
- 非 ``NULL`` = 该段正文的检测结论 JSON，``verdict`` 三态
  （``ok`` / ``degenerate`` / ``skipped_short``）+ ``truncated`` + ``checked_at``；
- 本脚本**只加列、不回填**：把历史行填成 ``{"verdict":"ok"}`` 等于伪造「已检测」；
- 只改 ``novel_dev.db`` 的 ``generations`` 表；``generated.db`` 一行不动。

加列**不参与指纹**。现有守卫指纹是 ``md5(f"{id}\\0{project_id}\\0{segment_no}\\0"
"{chapter_range}\\0{text}\\0")``（见 ``.tmp_verify/post_guards.py`` 的等价口径），
本脚本在 apply / rollback 前后都会重算，对不上就 assert 失败；同时校验
``generated.db`` 的 sha256 前后一致。

用法::

    python apply_quality_column.py                 # dry-run（默认，只读）
    python apply_quality_column.py --apply         # 加列（不做任何 UPDATE）
    python apply_quality_column.py --rollback      # 清值 + 删列
    python apply_quality_column.py --db <副本> --backup-dir <目录> --snap-dir <目录>
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

TABLE = "generations"
COLUMN = "quality_json"

#: 加列前的 6 列。指纹与 rows_md5 只覆盖这些列——新列不参与，
#: 否则 apply 后全为 NULL 也会改变 md5，失去「正文没动」的证明力。
BASE_COLUMNS = ("id", "project_id", "segment_no", "chapter_range", "text", "summary")

#: ``DROP COLUMN`` 需要的最低 SQLite 版本。
MIN_SQLITE_FOR_DROP = (3, 35, 0)


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(db: pathlib.Path, tag: str, backup_dir: pathlib.Path) -> pathlib.Path:
    """整库备份（含 WAL）：先 checkpoint 再 ``con.backup()``。"""
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


def _base_rows(con: sqlite3.Connection) -> list[tuple]:
    cols = ", ".join(BASE_COLUMNS)
    return con.execute(f'SELECT {cols} FROM "{TABLE}" ORDER BY id').fetchall()


def _fingerprint(rows: list[tuple]) -> dict:
    """与守卫脚本同源的正文指纹：只看 id / 段号 / 章节名 / 正文。"""
    h = hashlib.md5()
    for r in rows:
        h.update(f"{r[0]}\0{r[1]}\0{r[2]}\0{r[3] or ''}\0{r[4] or ''}\0".encode())
    return {"rows": len(rows), "md5": h.hexdigest()}


def _file_sha(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def state(con: sqlite3.Connection, gen_db: pathlib.Path) -> dict:
    cols = _cols(con, TABLE)
    rows = _base_rows(con)
    return {
        "columns": cols,
        "column_count": len(cols),
        "has_column": COLUMN in cols,
        "generations_rows": len(rows),
        "rows_md5": _md5(rows),
        "fingerprint": _fingerprint(rows),
        "generated_db_sha256": _file_sha(gen_db),
        "schema": [tuple(r) for r in con.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE tbl_name=? ORDER BY type, name", (TABLE,))],
    }


def _assert_unchanged(before: dict, after: dict, *, db: pathlib.Path) -> None:
    assert after["generations_rows"] == before["generations_rows"], (
        db, before["generations_rows"], after["generations_rows"])
    assert after["rows_md5"] == before["rows_md5"], db
    assert after["fingerprint"] == before["fingerprint"], db
    assert after["generated_db_sha256"] == before["generated_db_sha256"], (
        "generated.db 被改动了", db)


def apply(db: pathlib.Path, backup_dir: pathlib.Path, gen_db: pathlib.Path,
          snap_dir: pathlib.Path) -> dict:
    before_bak = _backup(db, "quality-col", backup_dir)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=OFF")

    before = state(con, gen_db)
    assert not before["has_column"], f"{TABLE}.{COLUMN} 已存在，拒绝重复加列"

    # 只加列，不回填：NULL = 未检测。
    with con:
        con.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "{COLUMN}" TEXT')

    after = state(con, gen_db)
    con.close()

    assert after["has_column"], after["columns"]
    assert after["columns"][-1] == COLUMN, after["columns"]
    assert after["column_count"] == before["column_count"] + 1, after["columns"]
    _assert_unchanged(before, after, db=db)

    snap_dir.mkdir(parents=True, exist_ok=True)
    snap = snap_dir / f"quality_col_{_now_ts()}.json"
    snap.write_text(json.dumps({
        "action": "apply", "ts": _now_ts(), "db": str(db),
        "generated_db": str(gen_db), "backup": str(before_bak),
        "before": before, "after": after,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {"backup": str(before_bak), "snapshot": str(snap),
            "before": before, "after": after}


def rollback(db: pathlib.Path, backup_dir: pathlib.Path,
             gen_db: pathlib.Path) -> dict:
    if sqlite3.sqlite_version_info < MIN_SQLITE_FOR_DROP:
        raise SystemExit(
            f"SQLite {sqlite3.sqlite_version} < "
            f"{'.'.join(map(str, MIN_SQLITE_FOR_DROP))}，不支持 DROP COLUMN；"
            "请改用「重建表」方案或升级 Python/SQLite")

    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=OFF")
    before = state(con, gen_db)
    if not before["has_column"]:
        con.close()
        return {"rolled_back": False,
                "why": f"{TABLE}.{COLUMN} 不存在，无需回滚",
                "before": before}

    bak = _backup(db, "quality-col-rollback", backup_dir)
    with con:
        # 与模板同序：先清值再删列。即使 DROP 失败，值也已复位。
        con.execute(f'UPDATE "{TABLE}" SET "{COLUMN}"=NULL')
        con.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN "{COLUMN}"')
    after = state(con, gen_db)
    con.close()

    assert not after["has_column"], after["columns"]
    assert after["column_count"] == before["column_count"] - 1, after["columns"]
    assert COLUMN not in after["columns"]
    _assert_unchanged(before, after, db=db)

    return {"rolled_back": True, "backup": str(bak),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(APP), help="novel_dev.db 路径（默认生产库）")
    ap.add_argument("--gen-db", default=str(GEN), help="generated.db 路径")
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    ap.add_argument("--snap-dir", default=str(SNAP_DIR))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    gen_db = pathlib.Path(args.gen_db)
    backup_dir = pathlib.Path(args.backup_dir)
    snap_dir = pathlib.Path(args.snap_dir)

    if args.rollback:
        out = rollback(db, backup_dir, gen_db)
        print(json.dumps({
            k: v for k, v in out.items() if k not in ("before", "after")},
            ensure_ascii=False, indent=2))
        if out.get("rolled_back"):
            print("列数:", out["before"]["column_count"], "→",
                  out["after"]["column_count"])
            print("指纹:", out["before"]["fingerprint"], "→",
                  out["after"]["fingerprint"])
            print("generated.db sha256 未变:",
                  out["before"]["generated_db_sha256"] ==
                  out["after"]["generated_db_sha256"])
        return 0

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = state(con, gen_db)
    con.close()

    print(json.dumps({
        "db": str(db), "generated_db": str(gen_db),
        "sqlite": sqlite3.sqlite_version,
        "has_column": cur["has_column"], "columns": cur["columns"],
        "generations_rows": cur["generations_rows"],
        "fingerprint": cur["fingerprint"],
    }, ensure_ascii=False, indent=2))

    if cur["has_column"]:
        print(f"\n[{TABLE}.{COLUMN}] 已存在：--apply 会拒绝执行（防重复加列）。")
        return 0

    if not args.apply:
        print("\n[dry-run] 计划：ALTER TABLE \"generations\" ADD COLUMN "
              "\"quality_json\" TEXT（不回填，历史行保持 NULL = 未检测）。")
        print("[dry-run] 未写库。加 --apply 执行。")
        return 0

    out = apply(db, backup_dir, gen_db, snap_dir)
    print("\n备份:", out["backup"])
    print("快照:", out["snapshot"])
    print("列数:", out["before"]["column_count"], "→", out["after"]["column_count"])
    print("改前列:", out["before"]["columns"])
    print("改后列:", out["after"]["columns"])
    print("行数:", out["before"]["generations_rows"], "→",
          out["after"]["generations_rows"])
    print("正文指纹未变:", out["before"]["fingerprint"] ==
          out["after"]["fingerprint"], out["after"]["fingerprint"])
    print("generated.db sha256 未变:",
          out["before"]["generated_db_sha256"] ==
          out["after"]["generated_db_sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
