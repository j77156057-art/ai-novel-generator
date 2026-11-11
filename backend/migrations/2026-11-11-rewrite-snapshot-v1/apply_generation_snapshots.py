# -*- coding: utf-8 -*-
"""建「重写前快照」表 ``generation_snapshots``（设计见
``.trae/documents/rewrite-snapshot-design.md``）。

正常路径**不需要跑这个脚本**：新表由 ``Base.metadata.create_all`` 在服务启动
时建好（``app/db.py: ensure_schema``）。本脚本用于：

- 运维想在不停服的情况下手工补表（先建表、再重启）；
- 回退：``--rollback`` 删表（唯一损失是历史快照，正文/摘要/图谱都不动）。

停止条件（脚本内硬校验，触发即退出）：

- ``--apply`` 时表已存在但缺的**不是**租约三列 → 不试图改表，报出来让人判断；
- ``--rollback`` 时表不存在 → 直接返回「无需回滚」，不报错。

v2（2026-11-12）加在途租约：``owner_pid`` / ``owner_started_at`` /
``heartbeat_at``。**存量 pending 行不回填**：按 ``created_at`` 补心跳等于给
僵尸行发一张新租约，会让崩溃残留多活一个判据周期；补 pid 可能撞上被复用的
pid。三列保持 NULL 的语义是「升级前写入、无租约证据」，启动收割按「pid
不存在」清掉。因此本迁移应在重启前执行（在线执行时旧进程写下的 pending
没有租约，可能被新进程接管，属升级窗口的已知代价）。

用法::

    python apply_generation_snapshots.py                  # dry-run（默认，只读）
    python apply_generation_snapshots.py --apply          # 备份 + 建表 + 建索引
    python apply_generation_snapshots.py --rollback       # 备份 + 删表
    python apply_generation_snapshots.py --db <副本.db>    # 在副本上演练
"""
from __future__ import annotations

import argparse
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

TABLE = "generation_snapshots"

# 与 app/models.py::GenerationSnapshot 一一对应。列序、类型、可空性必须一致，
# 否则 create_all 看到「表已存在」就不再管，ORM 会按模型列名去查一个缺列的
# 表，运行时才炸。
EXPECTED_COLS = (
    ("id", "INTEGER", 0),
    ("project_id", "INTEGER", 0),
    ("generation_id", "INTEGER", 0),
    ("segment_no", "INTEGER", 0),
    ("batch_id", "VARCHAR(36)", 0),
    ("operation", "VARCHAR(24)", 0),
    ("status", "VARCHAR(16)", 0),
    ("before_text", "TEXT", 0),
    ("after_text", "TEXT", 1),
    ("before_summary", "JSON", 1),
    ("after_summary", "JSON", 1),
    ("before_hash", "VARCHAR(64)", 0),
    ("after_hash", "VARCHAR(64)", 1),
    ("instruction", "TEXT", 1),
    ("agent_run_id", "INTEGER", 1),
    ("bible_hash", "VARCHAR(64)", 1),
    ("error_reason", "VARCHAR(255)", 1),
    ("created_at", "DATETIME", 0),
    ("finished_at", "DATETIME", 1),
    ("rolled_back_at", "DATETIME", 1),
    ("rolled_back_by_snapshot_id", "INTEGER", 1),
    # v2 在途租约：存量行保持 NULL（不回填，理由见模块 docstring）
    ("owner_pid", "INTEGER", 1),
    ("owner_started_at", "VARCHAR(64)", 1),
    ("heartbeat_at", "DATETIME", 1),
)

#: 允许在已存在的表上自动 ADD COLUMN 的列（只限租约三列）。
LEASE_COLS = ("owner_pid", "owner_started_at", "heartbeat_at")

CREATE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS generation_snapshots (
    id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    segment_no INTEGER NOT NULL,
    batch_id VARCHAR(36) NOT NULL,
    operation VARCHAR(24) NOT NULL,
    status VARCHAR(16) NOT NULL,
    before_text TEXT NOT NULL,
    after_text TEXT,
    before_summary JSON,
    after_summary JSON,
    before_hash VARCHAR(64) NOT NULL,
    after_hash VARCHAR(64),
    instruction TEXT,
    agent_run_id INTEGER,
    bible_hash VARCHAR(64),
    error_reason VARCHAR(255),
    created_at DATETIME NOT NULL,
    finished_at DATETIME,
    rolled_back_at DATETIME,
    rolled_back_by_snapshot_id INTEGER,
    owner_pid INTEGER,
    owner_started_at VARCHAR(64),
    heartbeat_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    FOREIGN KEY(generation_id) REFERENCES generations (id),
    FOREIGN KEY(agent_run_id) REFERENCES agent_runs (id),
    FOREIGN KEY(rolled_back_by_snapshot_id) REFERENCES generation_snapshots (id)
)
"""

INDEX_DDLS = (
    ("ix_gs_project_segment",
     "CREATE INDEX IF NOT EXISTS ix_gs_project_segment"
     " ON generation_snapshots (project_id, segment_no, id)"),
    ("ix_gs_batch",
     "CREATE INDEX IF NOT EXISTS ix_gs_batch"
     " ON generation_snapshots (batch_id, segment_no)"),
    ("ix_gs_status_created",
     "CREATE INDEX IF NOT EXISTS ix_gs_status_created"
     " ON generation_snapshots (status, created_at)"),
    # 闸门本身：同段同时只允许一个 pending。
    ("uq_gs_pending",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_gs_pending"
     " ON generation_snapshots (project_id, segment_no)"
     " WHERE status = 'pending'"),
)


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(db: pathlib.Path, tag: str, backup_dir: pathlib.Path) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"{db.stem}-full-{tag}-{_now_ts()}.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    out = sqlite3.connect(dst)
    con.backup(out)
    out.close()
    con.close()
    return dst


def _table_exists(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone() is not None


def _cols(con: sqlite3.Connection) -> list[tuple[str, str, int]]:
    return [(r[1], (r[2] or "").upper(), 1 if int(r[3]) == 0 else 0)
            for r in con.execute(f'PRAGMA table_info("{TABLE}")')]


def _indexes(con: sqlite3.Connection) -> list[str]:
    return sorted(r[1] for r in con.execute(f'PRAGMA index_list("{TABLE}")'))


def _row_stats(con: sqlite3.Connection) -> dict:
    if not _table_exists(con):
        return {"rows": 0, "bytes": 0, "by_status": {}}
    rows = con.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
    # 正文快照的字节数：只算这张表自己的 TEXT/JSON 列（页面开销另计）。
    nbytes = 0
    for col in ("before_text", "after_text", "before_summary", "after_summary",
                "instruction", "error_reason", "batch_id"):
        got = con.execute(
            f"SELECT COALESCE(SUM(LENGTH(CAST({col} AS BLOB))), 0) FROM {TABLE}"
        ).fetchone()[0]
        nbytes += int(got or 0)
    by_status = dict(con.execute(
        f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY status").fetchall())
    return {"rows": rows, "bytes": nbytes, "by_status": by_status}


def state(con: sqlite3.Connection) -> dict:
    return {
        "table_exists": _table_exists(con),
        "columns": _cols(con) if _table_exists(con) else [],
        "indexes": _indexes(con) if _table_exists(con) else [],
        "stats": _row_stats(con),
    }


def _check_columns(con: sqlite3.Connection) -> list[str]:
    """返回结构差异（空 = 完全一致）。故意不改表：加列/改类型应由人判断。"""
    have = {name: (typ, nullable) for name, typ, nullable in _cols(con)}
    problems = []
    for name, typ, nullable in EXPECTED_COLS:
        if name not in have:
            problems.append(f"缺列 {name} {typ}")
        elif have[name][0] != typ.upper():
            problems.append(f"列 {name} 类型 {have[name][0]} != {typ}")
    extra = set(have) - {c[0] for c in EXPECTED_COLS}
    if extra:
        problems.append(f"多余列 {sorted(extra)}")
    return problems


def apply(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")

    before = state(con)
    alters: list[tuple[str, str]] = []
    if before["table_exists"]:
        # v2：只允许自动补租约三列；其它结构差异（缺非租约列、类型不符、
        # 多余列）仍然停下来让人判断——迁移脚本不能变成隐式改表工具。
        have = {name for name, _typ, _nullable in _cols(con)}
        alters = [
            (name, typ) for name, typ, _nullable in EXPECTED_COLS
            if name in LEASE_COLS and name not in have
        ]
        allowed = {f"缺列 {name} {typ}" for name, typ in alters}
        problems = [p for p in _check_columns(con) if p not in allowed]
        if problems:
            con.close()
            raise SystemExit("停止：表已存在但结构不符，不自动改表：\n  - "
                             + "\n  - ".join(problems))
    else:
        # 依赖表必须在：否则外键约束名存实亡，回滚时按 generation_id 找不到正文。
        for dep in ("projects", "generations", "agent_runs"):
            if con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (dep,),
            ).fetchone() is None:
                con.close()
                raise SystemExit(f"停止：依赖表 {dep} 不存在，这不是本项目的库？")

    backup = _backup(db, "gen-snapshot", backup_dir)
    with con:
        con.execute(CREATE_TABLE_DDL)
        for name, typ in alters:
            con.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {typ}")
        for _name, ddl in INDEX_DDLS:
            con.execute(ddl)

    after = state(con)
    con.close()

    missing = [n for n, _ in INDEX_DDLS if n not in after["indexes"]]
    assert not missing, f"索引没建出来：{missing}"
    assert not _check_columns(sqlite3.connect(db)), "列结构与模型不一致"

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAP_DIR / f"gen_snapshot_{_now_ts()}.json"
    snap.write_text(json.dumps({
        "ts": _now_ts(), "db": str(db), "backup": str(backup),
        "before": before, "after": after,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"backup": str(backup), "snapshot": str(snap),
            "before": before, "after": after}


def rollback(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")
    before = state(con)
    if not before["table_exists"]:
        con.close()
        return {"rolled_back": False, "why": "表不存在，无需回滚"}

    # 回滚前先把「要丢什么」记下来：行数与状态分布是唯一凭证。
    backup = _backup(db, "gen-snapshot-rollback", backup_dir)
    with con:
        con.execute(f"DROP TABLE {TABLE}")
    after = state(con)
    con.close()

    assert not after["table_exists"]
    return {"rolled_back": True, "backup": str(backup),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(APP), help="novel_dev.db 路径")
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    backup_dir = pathlib.Path(args.backup_dir)
    if not db.exists():
        raise SystemExit(f"库不存在：{db}")

    if args.rollback:
        print(json.dumps(rollback(db, backup_dir), ensure_ascii=False,
                         indent=2, default=str))
        return 0

    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = state(ro)
    ro.close()
    print(json.dumps({"db": str(db), **cur}, ensure_ascii=False, indent=2,
                     default=str))
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    out = apply(db, backup_dir)
    print("\n备份:", out["backup"])
    print("快照:", out["snapshot"])
    print("索引:", out["after"]["indexes"])
    print("行数:", out["before"]["stats"]["rows"], "→",
          out["after"]["stats"]["rows"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
