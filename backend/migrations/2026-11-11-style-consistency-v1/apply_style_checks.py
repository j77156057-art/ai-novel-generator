# -*- coding: utf-8 -*-
"""文风一致性校验第一阶段：新建 `style_baselines` / `style_checks` 两张表。

对应设计：`.trae/documents/style-consistency-check-design.md` §5.1。

只新增表与索引，**不改任何既有表/列**（脚本内会校验既有表 DDL 前后逐字节一致）。
第一阶段写入的记录 `verdict` 恒为 `'shadow'`，不产生 pass/warn/fail。

用法::

    python apply_style_checks.py                  # dry-run（默认，只读）
    python apply_style_checks.py --apply          # 备份 + 建表 + 校验
    python apply_style_checks.py --rollback       # 备份 + 删表（回退）

    # 副本演练（推荐先做）
    python apply_style_checks.py --db ../novel_dev.db.copy --apply
    python apply_style_checks.py --db ../novel_dev.db.copy --rollback
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
APP_DB = BACKEND / "novel_dev.db"
BACKUP_DIR = BACKEND / "backups"

NEW_TABLES = ("style_baselines", "style_checks")

DDL = (
    """
    CREATE TABLE IF NOT EXISTS style_baselines (
        id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        baseline_version INTEGER NOT NULL,
        window_segments INTEGER NOT NULL,
        sample_count INTEGER NOT NULL,
        source VARCHAR(16) NOT NULL,
        source_range VARCHAR(128),
        insufficient_reason TEXT,
        metrics_json JSON NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_style_baseline_version UNIQUE (project_id, baseline_version),
        FOREIGN KEY(project_id) REFERENCES projects (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS style_checks (
        id INTEGER NOT NULL,
        project_id INTEGER NOT NULL,
        scope VARCHAR(16) NOT NULL,
        generation_id INTEGER,
        agent_run_id INTEGER,
        segment_no INTEGER,
        chapter_range VARCHAR(32),
        text_md5 VARCHAR(32) NOT NULL,
        check_version VARCHAR(16) NOT NULL,
        baseline_id INTEGER NOT NULL,
        baseline_version INTEGER NOT NULL,
        verdict VARCHAR(16) NOT NULL,
        metric_json JSON NOT NULL,
        missing_json JSON,
        evidence_json JSON,
        note TEXT,
        stale_at DATETIME,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (id),
        FOREIGN KEY(project_id) REFERENCES projects (id),
        FOREIGN KEY(generation_id) REFERENCES generations (id),
        FOREIGN KEY(agent_run_id) REFERENCES agent_runs (id),
        FOREIGN KEY(baseline_id) REFERENCES style_baselines (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_style_baseline_project"
    " ON style_baselines (project_id, baseline_version)",
    "CREATE INDEX IF NOT EXISTS idx_style_checks_proj_created"
    " ON style_checks (project_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_style_checks_gen"
    " ON style_checks (generation_id)",
)


def _connect(db: pathlib.Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def state(con: sqlite3.Connection) -> dict:
    tables = {
        r["name"]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    return {
        "has_baselines": "style_baselines" in tables,
        "has_checks": "style_checks" in tables,
        "baselines_rows": (
            con.execute("SELECT count(*) FROM style_baselines").fetchone()[0]
            if "style_baselines" in tables else None
        ),
        "checks_rows": (
            con.execute("SELECT count(*) FROM style_checks").fetchone()[0]
            if "style_checks" in tables else None
        ),
        "indexes": sorted(
            r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND name IN ('ix_style_baseline_project',"
                " 'idx_style_checks_proj_created', 'idx_style_checks_gen')")
        ),
    }


def _existing_schema(con: sqlite3.Connection) -> dict[str, str]:
    """既有表（除新表外）的 DDL 快照——用于证明脚本没碰任何老表。"""
    out = {}
    for r in con.execute(
            "SELECT name, sql FROM sqlite_master"
            " WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name"):
        if r["name"] not in NEW_TABLES:
            out[r["name"]] = r["sql"] or ""
    return out


def _backup(db: pathlib.Path, tag: str, backup_dir: pathlib.Path) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"novel_dev-{tag}-{stamp}.db"
    src = _connect(db, readonly=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def apply(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    """备份 → 建表/索引 → 校验（既有表 DDL 不变、FK 无违约）。"""
    con = _connect(db)
    before = state(con)
    schema_before = _existing_schema(con)

    backup = _backup(db, "style-consistency", backup_dir)
    try:
        with con:
            for stmt in DDL:
                con.execute(stmt)
        after = state(con)
        schema_after = _existing_schema(con)
        assert schema_before == schema_after, "既有表结构被改动了"
        assert after["has_baselines"] and after["has_checks"]
        fk = [tuple(r) for r in con.execute("PRAGMA foreign_key_check(style_checks)")]
        assert not fk, f"外键违约: {fk}"
        idx = {
            r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")
        }
        for name in ("ix_style_baseline_project", "idx_style_checks_proj_created",
                     "idx_style_checks_gen"):
            assert name in idx, f"索引缺失: {name}"
    finally:
        con.close()
    return {"backup": str(backup), "before": before, "after": after}


def rollback(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    """备份 → 删两张新表（索引随表删除）→ 校验既有表未变。"""
    con = _connect(db)
    before = state(con)
    if not before["has_baselines"] and not before["has_checks"]:
        con.close()
        return {"rolled_back": False, "why": "两张表都不存在，无需回滚"}

    schema_before = _existing_schema(con)
    backup = _backup(db, "style-consistency-rollback", backup_dir)
    try:
        with con:
            # 先删引用方，再删被引用方（即便 FK 未开启，顺序仍保持清晰）
            con.execute("DROP TABLE IF EXISTS style_checks")
            con.execute("DROP TABLE IF EXISTS style_baselines")
        after = state(con)
        assert not after["has_baselines"] and not after["has_checks"]
        assert _existing_schema(con) == schema_before, "回退时碰到了既有表"
    finally:
        con.close()
    return {"rolled_back": True, "backup": str(backup),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(APP_DB), help="novel_dev.db 路径")
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    if not db.is_file():
        print(f"库不存在: {db}", file=sys.stderr)
        return 2
    backup_dir = pathlib.Path(args.backup_dir)

    if args.rollback:
        print(json.dumps(rollback(db, backup_dir), ensure_ascii=False,
                         indent=2, default=str))
        return 0

    con = _connect(db, readonly=True)
    cur = state(con)
    con.close()
    print(json.dumps({"db": str(db), **cur}, ensure_ascii=False, indent=2))
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 建表。")
        return 0

    out = apply(db, backup_dir)
    print("\n备份:", out["backup"])
    print("表:", f"style_baselines={out['after']['has_baselines']}",
          f"style_checks={out['after']['has_checks']}")
    print("索引:", out["after"]["indexes"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
