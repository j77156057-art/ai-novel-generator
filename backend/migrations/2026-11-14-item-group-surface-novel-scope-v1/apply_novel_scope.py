# -*- coding: utf-8 -*-
"""读侧切换的 schema 部分：``kg_item_group`` / ``kg_item_surface`` 扩唯一键到「书」。

背景
----
``kg_item_group`` 已在上一步加了可空 ``novel_id``（53 行，其中 8 个泛称组为
``NULL``），但唯一键仍是 ``(project_id, kind, canonical_name)``——第二本书建同名
组插不进去；``kg_item_surface`` 同理，且它连 ``novel_id`` 都没有。

本步只改这两张表：

- ``kg_item_group``：去掉表级 ``UNIQUE(project_id, kind, canonical_name)``，改为
  表达式唯一索引 ``(project_id, kind, COALESCE(novel_id,-1), canonical_name)``；
  列与列序不变（``novel_id`` 仍在最后）。
- ``kg_item_surface``：新增可空 ``novel_id INTEGER``（回填为所属组的 ``novel_id``），
  去掉表级 ``UNIQUE(project_id, surface)``，改为表达式唯一索引
  ``(project_id, COALESCE(novel_id,-1), surface)``。

为什么用 ``COALESCE(novel_id,-1)``：SQLite 里 ``NULL`` 在普通 UNIQUE 中互不相等，
直接用 ``novel_id`` 会让「同项目两条 NULL 泛称同名」也能插进来。``-1`` 不是合法的
``raw_novels.id``（自增从 1 开始），所以不会与任何真书冲突。

为什么不加 FK：``novel_id`` 逻辑指向 ``novel_dev.raw_novels.id``，但组表在
``generated.db``，两库物理隔离、不 ATTACH，无法建外键；``novel_id = NULL`` 的语义
是「项目级通配」，也不是「空外键」。

SQLite 改不了表级 UNIQUE，所以两张表都走 12-step 重建（新建 → 拷贝 → 丢弃 → 改名
→ 重建索引）。``PRAGMA foreign_keys=OFF``（surface → group 的 FK 在重建期会短暂
悬空）与 ``legacy_alter_table=ON``（改名时不改别的表里的引用文本）。

用法（**必须在 backend/ 下运行**，因为 ``app/config.py`` 的库路径是相对的）::

    python migrations/2026-11-14-item-group-surface-novel-scope-v1/apply_novel_scope.py
    python migrations/.../apply_novel_scope.py --apply
    python migrations/.../apply_novel_scope.py --rollback
    python migrations/.../apply_novel_scope.py --db /tmp/copy.db --backup-dir /tmp/bak   # 副本实测

回滚只在「还没有跨书同名数据」时可用（否则旧唯一键装不下，脚本会明确拒绝）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sqlite3
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
GEN = PROJ / "backend" / "data" / "generated.db"
BACKUP_DIR = PROJ / "backend" / "backups"
SNAP_DIR = HERE / "snapshots"
MIGRATION = "2026-11-14-item-group-surface-novel-scope-v1"

GROUP_NEW = "kg_item_group__new"
SURFACE_NEW = "kg_item_surface__new"
UX_GROUP = "ux_item_group_project_novel_name"
UX_SURFACE = "ux_item_surface_project_novel_surface"
IX_SURFACE_GROUP = "ix_item_surface_group"

GROUP_SQL = """
CREATE TABLE kg_item_group (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'item',
    canonical_name TEXT   NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'generic')),
    generic_members TEXT  NOT NULL DEFAULT '[]',
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    novel_id      INTEGER
)
"""

#: 回滚形态：列与改前逐字一致（含 novel_id，那是上一步的成果，本脚本不回退它）。
GROUP_SQL_ROLLBACK = """
CREATE TABLE kg_item_group (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'item',
    canonical_name TEXT   NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'generic')),
    generic_members TEXT  NOT NULL DEFAULT '[]',
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL,
    novel_id      INTEGER,
    UNIQUE (project_id, kind, canonical_name)
)
"""

SURFACE_SQL = """
CREATE TABLE kg_item_surface (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL,
    surface       TEXT    NOT NULL CHECK (length(trim(surface)) > 0),
    group_id      INTEGER NOT NULL REFERENCES kg_item_group(id),
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected')),
    source        TEXT    NOT NULL DEFAULT 'migration'
                  CHECK (source IN ('human', 'candidate', 'migration')),
    confidence    REAL    NOT NULL DEFAULT 0.5
                  CHECK (confidence >= 0 AND confidence <= 1),
    instance_label TEXT,
    evidence_chapter INTEGER,
    evidence      TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    reviewer      TEXT,
    reviewed_at   TEXT,
    created_at    TEXT    NOT NULL,
    novel_id      INTEGER
)
"""

SURFACE_SQL_ROLLBACK = """
CREATE TABLE kg_item_surface (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL,
    surface       TEXT    NOT NULL CHECK (length(trim(surface)) > 0),
    group_id      INTEGER NOT NULL REFERENCES kg_item_group(id),
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected')),
    source        TEXT    NOT NULL DEFAULT 'migration'
                  CHECK (source IN ('human', 'candidate', 'migration')),
    confidence    REAL    NOT NULL DEFAULT 0.5
                  CHECK (confidence >= 0 AND confidence <= 1),
    instance_label TEXT,
    evidence_chapter INTEGER,
    evidence      TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    reviewer      TEXT,
    reviewed_at   TEXT,
    created_at    TEXT    NOT NULL,
    UNIQUE (project_id, surface)
)
"""

SURFACE_COLS = ("id, project_id, surface, group_id, status, source, confidence,"
                " instance_label, evidence_chapter, evidence, note, reviewer,"
                " reviewed_at, created_at")


def _now() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _checkpoint(con: sqlite3.Connection) -> None:
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.DatabaseError:
        pass


def _backup(path: pathlib.Path, label: str, backup_dir: pathlib.Path) -> pathlib.Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{path.stem}-full-{label}-{_now()}{path.suffix}"
    src = sqlite3.connect(path)
    _checkpoint(src)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def _schema(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [(r[0], r[1], (r[2] or "").replace("\n", " "))
            for r in con.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE tbl_name IN ('kg_item_group','kg_item_surface')"
                "    OR name IN (?, ?, ?)"
                " ORDER BY type, name", (UX_GROUP, UX_SURFACE, IX_SURFACE_GROUP))]


def _dump(con: sqlite3.Connection, sql: str) -> tuple[int, str]:
    # 必须转成 tuple：row_factory=Row 时 json.dumps 会退化成对象地址，md5 不稳定。
    rows = [tuple(r) for r in con.execute(sql).fetchall()]
    blob = json.dumps(rows, ensure_ascii=False, default=str)
    return len(rows), hashlib.md5(blob.encode("utf-8")).hexdigest()


def state(con: sqlite3.Connection) -> dict:
    """结构 + 行数 + 内容 md5：改前/改后/回滚后都用它逐项对照。"""
    g_n, g_md5 = _dump(
        con, "SELECT id, project_id, kind, canonical_name, status, generic_members,"
             " note, created_at, novel_id FROM kg_item_group ORDER BY id")
    # 写法表的 md5 只取改前就有的列：这样改前/改后/回滚后可逐字对照；
    # novel_id 的回填结果单独给分布。
    s_n, s_md5 = _dump(con, "SELECT " + SURFACE_COLS + " FROM kg_item_surface ORDER BY id")
    has_s_novel = _has_column(con, "kg_item_surface", "novel_id")
    return {
        "schema": _schema(con),
        "group_rows": g_n, "group_md5": g_md5,
        "surface_rows": s_n, "surface_md5": s_md5,
        "group_novel_null": con.execute(
            "SELECT COUNT(*) FROM kg_item_group WHERE novel_id IS NULL").fetchone()[0],
        "surface_novel_null": (con.execute(
            "SELECT COUNT(*) FROM kg_item_surface WHERE novel_id IS NULL").fetchone()[0]
            if has_s_novel else None),
        "sequence": [tuple(r) for r in con.execute(
            "SELECT name, seq FROM sqlite_sequence WHERE name LIKE 'kg_item%'"
            " ORDER BY name")],
    }


def _has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})"))


def _precheck(con: sqlite3.Connection) -> dict:
    if not _has_column(con, "kg_item_group", "novel_id"):
        raise SystemExit("kg_item_group 还没有 novel_id：先跑 "
                         "2026-11-13-item-group-novel-id-v1")
    dup_group = con.execute(
        "SELECT project_id, kind, COALESCE(novel_id,-1), canonical_name, COUNT(*)"
        "  FROM kg_item_group GROUP BY 1,2,3,4 HAVING COUNT(*) > 1").fetchall()
    if dup_group:
        raise SystemExit(f"组表已存在跨书同名（不可迁移）：{dup_group}")
    return {
        "group_rows": con.execute("SELECT COUNT(*) FROM kg_item_group").fetchone()[0],
        "group_novel_null": con.execute(
            "SELECT COUNT(*) FROM kg_item_group WHERE novel_id IS NULL").fetchone()[0],
        "surface_rows": con.execute("SELECT COUNT(*) FROM kg_item_surface").fetchone()[0],
        "surface_has_novel_id": _has_column(con, "kg_item_surface", "novel_id"),
    }


def _rebuild(con: sqlite3.Connection, *, rollback: bool) -> None:
    """12-step 重建两张表；两条路只差目标 DDL 与拷贝列。"""
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("PRAGMA legacy_alter_table=ON")
    con.execute("BEGIN")
    try:
        con.execute(f"DROP TABLE IF EXISTS {GROUP_NEW}")
        con.execute(f"DROP TABLE IF EXISTS {SURFACE_NEW}")
        con.execute((GROUP_SQL_ROLLBACK if rollback else GROUP_SQL)
                    .replace("CREATE TABLE kg_item_group", f"CREATE TABLE {GROUP_NEW}"))
        con.execute(
            f"INSERT INTO {GROUP_NEW} (id, project_id, kind, canonical_name, status,"
            " generic_members, note, created_at, novel_id)"
            " SELECT id, project_id, kind, canonical_name, status, generic_members,"
            " note, created_at, novel_id FROM kg_item_group ORDER BY id")

        con.execute((SURFACE_SQL_ROLLBACK if rollback else SURFACE_SQL)
                    .replace("CREATE TABLE kg_item_surface", f"CREATE TABLE {SURFACE_NEW}"))
        if rollback:
            con.execute(
                f"INSERT INTO {SURFACE_NEW} ({SURFACE_COLS})"
                f" SELECT {SURFACE_COLS} FROM kg_item_surface ORDER BY id")
        else:
            con.execute(
                f"INSERT INTO {SURFACE_NEW} ({SURFACE_COLS}, novel_id)"
                " SELECT s.id, s.project_id, s.surface, s.group_id, s.status, s.source,"
                "        s.confidence, s.instance_label, s.evidence_chapter, s.evidence,"
                "        s.note, s.reviewer, s.reviewed_at, s.created_at, g.novel_id"
                "   FROM kg_item_surface s"
                "   LEFT JOIN kg_item_group g ON g.id = s.group_id"
                "  ORDER BY s.id")

        # 丢旧表：先 surface（它引用 group），再 group。
        con.execute("DROP TABLE kg_item_surface")
        con.execute("DROP TABLE kg_item_group")
        con.execute(f"ALTER TABLE {GROUP_NEW} RENAME TO kg_item_group")
        con.execute(f"ALTER TABLE {SURFACE_NEW} RENAME TO kg_item_surface")

        if rollback:
            con.execute(f"DROP INDEX IF EXISTS {UX_GROUP}")
            con.execute(f"DROP INDEX IF EXISTS {UX_SURFACE}")
        else:
            con.execute(f"DROP INDEX IF EXISTS {UX_GROUP}")
            con.execute(
                f"CREATE UNIQUE INDEX {UX_GROUP} ON kg_item_group"
                " (project_id, kind, COALESCE(novel_id, -1), canonical_name)")
            con.execute(f"DROP INDEX IF EXISTS {UX_SURFACE}")
            con.execute(
                f"CREATE UNIQUE INDEX {UX_SURFACE} ON kg_item_surface"
                " (project_id, COALESCE(novel_id, -1), surface)")
        con.execute(f"DROP INDEX IF EXISTS {IX_SURFACE_GROUP}")
        con.execute(f"CREATE INDEX {IX_SURFACE_GROUP} ON kg_item_surface(group_id)")

        # 改名不改 sqlite_sequence 的 name（legacy_alter_table=ON），这里补齐。
        for old, new in ((GROUP_NEW, "kg_item_group"), (SURFACE_NEW, "kg_item_surface")):
            row = con.execute("SELECT seq FROM sqlite_sequence WHERE name = ?",
                              (old,)).fetchone()
            if row is not None:
                cur = con.execute("SELECT seq FROM sqlite_sequence WHERE name = ?",
                                  (new,)).fetchone()
                if cur is None:
                    con.execute("DELETE FROM sqlite_sequence WHERE name = ?", (old,))
                    con.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                                (new, row[0]))
                else:
                    con.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?",
                                (row[0], new))
                    con.execute("DELETE FROM sqlite_sequence WHERE name = ?", (old,))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def apply(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    pre = _precheck(con)
    before = state(con)
    bak = _backup(db, "novel-scope", backup_dir)

    _rebuild(con, rollback=False)

    after = state(con)
    _assert_apply(pre, before, after)
    snap = {
        "migration": MIGRATION,
        "applied_at": _now(),
        "db": str(db),
        "backup": str(bak),
        "precheck": pre,
        "before": before,
        "after": after,
        "new_unique_keys": {
            "kg_item_group": f"{UX_GROUP}(project_id, kind, COALESCE(novel_id,-1), canonical_name)",
            "kg_item_surface": f"{UX_SURFACE}(project_id, COALESCE(novel_id,-1), surface)",
        },
    }
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"novel_scope_{snap['applied_at']}.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    con.close()
    return {"snapshot": path, "backup": bak, "before": before, "after": after,
            "precheck": pre}


def _assert_apply(pre: dict, before: dict, after: dict) -> None:
    assert after["group_rows"] == before["group_rows"] == pre["group_rows"], after
    assert after["surface_rows"] == before["surface_rows"] == pre["surface_rows"], after
    assert after["group_md5"] == before["group_md5"], "组表内容被改了"
    assert after["surface_md5"] == before["surface_md5"], "写法表内容被改了"
    assert after["group_novel_null"] == before["group_novel_null"] == pre["group_novel_null"]
    assert after["surface_novel_null"] is not None
    names = {r[1] for r in after["schema"]}
    assert UX_GROUP in names and UX_SURFACE in names and IX_SURFACE_GROUP in names, names
    assert "sqlite_autoindex_kg_item_group_1" not in names, names
    assert "sqlite_autoindex_kg_item_surface_1" not in names, names


def rollback(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    if not _has_column(con, "kg_item_surface", "novel_id"):
        con.close()
        return {"rolled_back": False, "why": "surface 没有 novel_id，已是回滚形态"}
    dup_group = con.execute(
        "SELECT project_id, kind, canonical_name, COUNT(*) FROM kg_item_group"
        " GROUP BY 1,2,3 HAVING COUNT(*) > 1").fetchall()
    dup_surface = con.execute(
        "SELECT project_id, surface, COUNT(*) FROM kg_item_surface"
        " GROUP BY 1,2 HAVING COUNT(*) > 1").fetchall()
    if dup_group or dup_surface:
        con.close()
        return {"rolled_back": False, "why": "已有跨书同名数据，旧唯一键装不下",
                "dup_group": [tuple(r) for r in dup_group],
                "dup_surface": [tuple(r) for r in dup_surface]}
    before = state(con)
    bak = _backup(db, "novel-scope-rollback", backup_dir)
    _rebuild(con, rollback=True)
    after = state(con)
    assert after["group_rows"] == before["group_rows"]
    assert after["surface_rows"] == before["surface_rows"]
    assert after["group_md5"] == before["group_md5"]
    assert after["group_novel_null"] == before["group_novel_null"]
    con.close()
    return {"rolled_back": True, "backup": str(bak),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(GEN), help="generated.db 路径（默认生产库）")
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    backup_dir = pathlib.Path(args.backup_dir)
    if args.rollback:
        print(json.dumps(rollback(db, backup_dir), ensure_ascii=False,
                         indent=2, default=str))
        return 0

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    pre = _precheck(con)
    cur = state(con)
    con.close()
    print(json.dumps({"db": str(db), "precheck": pre,
                      "schema": cur["schema"]}, ensure_ascii=False, indent=2))
    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    out = apply(db, backup_dir)
    print("\n备份:", out["backup"])
    print("快照:", out["snapshot"])
    print("\n改前结构:")
    for r in out["before"]["schema"]:
        print("  ", r[0], "|", r[1], "|", r[2][:150])
    print("改后结构:")
    for r in out["after"]["schema"]:
        print("  ", r[0], "|", r[1], "|", r[2][:150])
    print("\n行数/md5:", json.dumps(
        {k: out["after"][k] for k in ("group_rows", "group_md5", "surface_rows",
                                      "surface_md5", "group_novel_null")},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
