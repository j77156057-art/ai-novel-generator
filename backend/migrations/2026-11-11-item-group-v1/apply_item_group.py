# -*- coding: utf-8 -*-
"""物品「同物组」v1：建表 + 把现有实体/surface 登记进表（不重写持有段）。

范围（本轮只做前两步，见 ``reports/item-canonical-design.md`` 第 4/5 节）：

1. 建 ``kg_item_group``（组）与 ``kg_item_surface``（写法→组，带审核状态与证据）；
2. 给 ``kg_item_holding`` 加**可空** ``group_id`` 列，**不回填**（回填留给后续步骤）；
3. 把 ``kg_entities`` 里 45 个 item 实体按其 aliases 登记成组与写法；
4. 返工 ``玉石小镜``：它是九块碎片共用的**通称**（ch596「跌出一件同款玉石小镜」），
   不登记成任何单件的写法，单独建 ``status='generic'`` 的通称组；同时把它从「地书」的
   别名里摘掉（只动 ``kg_entities.aliases``，持有段一行不动）。

幂等：全部 `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN`（先查 `PRAGMA table_info`）/
`INSERT OR IGNORE`，重复跑不会新增行、不会改已审核的状态。

用法::

    python backend/migrations/2026-11-11-item-group-v1/apply_item_group.py --dry-run
    python backend/migrations/2026-11-11-item-group-v1/apply_item_group.py
    # 在副本上验证（不备份、不动真实库）：
    python .../apply_item_group.py --db backend/backups/xxx.db --no-backup

回滚：`DROP TABLE kg_item_surface; DROP TABLE kg_item_group;`（`group_id` 列可留空不用），
再把 `kg_entities` 里「地书」的 aliases 恢复为含 `玉石小镜`/`玉石镜`。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
GEN_DB = PROJ / "backend" / "data" / "generated.db"
APP_DB = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"

#: 通称（类名，不能作为任何单件的写法）。members 只作「候选」提示，不改变持有关系。
GENERIC_SURFACES = {
    "玉石小镜": {
        "members": ["地书"],
        "note": "九块碎片是九面同款镜子（ch596「跌出一件同款玉石小镜」），是类通称，不对应单件",
        "evidence_chapter": 596,
        "evidence": "李妙真取出地书碎片……跌出一件同款玉石小镜。李灵素探出手掌接过",
    },
    "长刀": {
        "members": ["黑金长刀", "监正出品的长刀"],
        "note": "通用兵器名，全书 127 章出现；不指向某一柄",
        "evidence_chapter": 48,
        "evidence": "御刀卫的制式长刀",
    },
}

#: 通称的同义写法：同样不能算任何单件的 surface，登记到对应通称组。
GENERIC_ALIASES = {
    "玉石镜": "玉石小镜",
}

#: 已知「同名不同物」的写法：登记为 pending，不参与解析，等人审。
PENDING_SURFACES = {
    "腰牌": "打更人腰牌与御赐金牌不是一物（ch73/ch108）",
}

#: 专指某一号碎片的写法：解析时按 instance_label 收窄。
INSTANCE_LABELS = {
    "三号碎片": "三号",
    "叁号镜子": "三号",
    "地书碎片·三号": "三号",
}

#: 返工时要从「地书」别名里摘掉的通称。
DISHU_DROP_ALIASES = ("玉石小镜", "玉石镜")

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ─── 建表 ──────────────────────────────────────────────────────────────


def ensure_schema(con: sqlite3.Connection) -> dict:
    made = []
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS kg_item_group (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id    INTEGER NOT NULL,
            kind          TEXT    NOT NULL DEFAULT 'item',
            canonical_name TEXT   NOT NULL,
            status        TEXT    NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'generic')),
            generic_members TEXT  NOT NULL DEFAULT '[]',
            note          TEXT    NOT NULL DEFAULT '',
            created_at    TEXT    NOT NULL,
            UNIQUE (project_id, kind, canonical_name)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS kg_item_surface (
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
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_item_surface_group ON kg_item_surface(group_id)"
    )
    made += ["kg_item_group", "kg_item_surface"]

    cols = {r[1] for r in con.execute("PRAGMA table_info(kg_item_holding)")}
    if "group_id" not in cols:
        con.execute("ALTER TABLE kg_item_holding ADD COLUMN group_id INTEGER")
        made.append("kg_item_holding.group_id")
    con.commit()
    return {"tables": made}


# ─── 数据源 ────────────────────────────────────────────────────────────


def load_items(app_db: pathlib.Path, project_id: int) -> list[dict]:
    con = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, canonical_name, COALESCE(aliases, '[]') FROM kg_entities "
            "WHERE project_id = ? AND kind = 'item' ORDER BY id", (project_id,)
        ).fetchall()
    finally:
        con.close()
    out = []
    for rid, name, raw in rows:
        try:
            aliases = [a.strip() for a in json.loads(raw or "[]") if isinstance(a, str) and a.strip()]
        except (TypeError, ValueError):
            aliases = []
        out.append({"id": rid, "canonical_name": name.strip(), "aliases": aliases})
    return out


# ─── 计划 ──────────────────────────────────────────────────────────────


def build_plan(items: list[dict]) -> dict:
    canon = {it["canonical_name"] for it in items}
    groups, surfaces, conflicts = [], [], []

    for it in items:
        groups.append({"canonical_name": it["canonical_name"], "status": "active",
                       "generic_members": [], "note": ""})
        surfaces.append({
            "surface": it["canonical_name"], "group": it["canonical_name"],
            "status": "approved", "confidence": 1.0, "source": "migration",
            "instance_label": INSTANCE_LABELS.get(it["canonical_name"]),
            "evidence_chapter": None, "evidence": "", "note": "规范名",
        })

    for it in items:
        for alias in it["aliases"]:
            if alias in GENERIC_SURFACES or alias in GENERIC_ALIASES:
                continue  # 通称单独建组，不作为任何单件的写法
            if alias in canon and alias != it["canonical_name"]:
                conflicts.append({"surface": alias, "owner": it["canonical_name"],
                                  "winner": alias, "why": "与另一实体规范名同名，规范名优先"})
                continue
            if alias in PENDING_SURFACES:
                surfaces.append({
                    "surface": alias, "group": it["canonical_name"], "status": "pending",
                    "confidence": 0.3, "source": "migration",
                    "instance_label": INSTANCE_LABELS.get(alias),
                    "evidence_chapter": None, "evidence": "", "note": PENDING_SURFACES[alias],
                })
                continue
            surfaces.append({
                "surface": alias, "group": it["canonical_name"], "status": "approved",
                "confidence": 0.6, "source": "migration",
                "instance_label": INSTANCE_LABELS.get(alias),
                "evidence_chapter": None, "evidence": "",
                "note": "实体 aliases 迁移带入",
            })

    for surface, spec in GENERIC_SURFACES.items():
        groups.append({"canonical_name": surface, "status": "generic",
                       "generic_members": spec["members"], "note": spec["note"]})
        surfaces.append({
            "surface": surface, "group": surface, "status": "approved",
            "confidence": 0.9, "source": "migration", "instance_label": None,
            "evidence_chapter": spec["evidence_chapter"], "evidence": spec["evidence"],
            "note": spec["note"],
        })

    for surface, base in GENERIC_ALIASES.items():
        spec = GENERIC_SURFACES[base]
        surfaces.append({
            "surface": surface, "group": base, "status": "approved",
            "confidence": 0.9, "source": "migration", "instance_label": None,
            "evidence_chapter": spec["evidence_chapter"], "evidence": spec["evidence"],
            "note": f"通称「{base}」的同义写法",
        })
    return {"groups": groups, "surfaces": surfaces, "conflicts": conflicts}


# ─── 写库 ──────────────────────────────────────────────────────────────


def apply_plan(con: sqlite3.Connection, project_id: int, plan: dict) -> dict:
    inserted_g = inserted_s = 0
    gid: dict[str, int] = {}
    for g in plan["groups"]:
        cur = con.execute(
            "INSERT OR IGNORE INTO kg_item_group "
            "(project_id, kind, canonical_name, status, generic_members, note, created_at) "
            "VALUES (?, 'item', ?, ?, '[]', ?, ?)",
            (project_id, g["canonical_name"], g["status"], g["note"], NOW),
        )
        inserted_g += max(cur.rowcount, 0)
        row = con.execute(
            "SELECT id FROM kg_item_group WHERE project_id=? AND kind='item' AND canonical_name=?",
            (project_id, g["canonical_name"]),
        ).fetchone()
        gid[g["canonical_name"]] = row[0]

    # 通称组的成员要等所有组 id 落库后再回填（只写一次，重复跑结果相同）
    for g in plan["groups"]:
        if g["status"] != "generic":
            continue
        members = [gid[m] for m in g["generic_members"] if m in gid]
        con.execute(
            "UPDATE kg_item_group SET generic_members=? WHERE id=?",
            (json.dumps(members), gid[g["canonical_name"]]),
        )

    # 通称的同义写法若历史上被登记到某个单件组（如「玉石镜」曾随时地书的别名
    # 一起进表），先摘掉错误归属，再按通称组重新登记。重复跑无副作用。
    repaired = 0
    for surface, base in GENERIC_ALIASES.items():
        cur = con.execute(
            "DELETE FROM kg_item_surface WHERE project_id=? AND surface=? AND group_id<>?",
            (project_id, surface, gid[base]),
        )
        repaired += max(cur.rowcount, 0)

    for s in plan["surfaces"]:
        cur = con.execute(
            "INSERT OR IGNORE INTO kg_item_surface "
            "(project_id, surface, group_id, status, source, confidence, instance_label,"
            " evidence_chapter, evidence, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, s["surface"], gid[s["group"]], s["status"], s["source"],
             s["confidence"], s["instance_label"], s["evidence_chapter"],
             s["evidence"], s["note"], NOW),
        )
        inserted_s += cur.rowcount if cur.rowcount > 0 else 0
        # 已存在但仍是 pending 的写法，如果本次计划是 approved，不擅自升级：
        # 审核状态只能由人工改，迁移不做二次裁决。
    con.commit()
    return {"groups": inserted_g, "surfaces": inserted_s, "repaired": repaired}


def clean_dishu_aliases(app_db: pathlib.Path, project_id: int) -> dict:
    """把「玉石小镜 / 玉石镜」从地书的 aliases 里摘掉（幂等）。"""
    con = sqlite3.connect(str(app_db))
    try:
        row = con.execute(
            "SELECT id, aliases FROM kg_entities WHERE project_id=? AND kind='item' AND canonical_name='地书'",
            (project_id,),
        ).fetchone()
        if not row:
            return {"changed": False, "reason": "没有地书实体"}
        rid, raw = row
        try:
            aliases = json.loads(raw or "[]")
        except (TypeError, ValueError):
            aliases = []
        kept = [a for a in aliases if a not in DISHU_DROP_ALIASES]
        dropped = [a for a in aliases if a in DISHU_DROP_ALIASES]
        if dropped:
            con.execute("UPDATE kg_entities SET aliases=? WHERE id=?",
                        (json.dumps(kept, ensure_ascii=False), rid))
            con.commit()
        return {"changed": bool(dropped), "dropped": dropped, "kept": kept}
    finally:
        con.close()


def backup(db_path: pathlib.Path, tag: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"{tag}-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
        out.execute("PRAGMA journal_mode=DELETE")
    finally:
        out.close()
        src.close()
    chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    chk.close()
    print(f"[备份] {dst}  integrity={ok}")
    return dst


# ─── 报告 ──────────────────────────────────────────────────────────────


def counts(con: sqlite3.Connection, project_id: int) -> dict:
    def has(table: str) -> bool:
        return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                           (table,)).fetchone() is not None

    def n(sql, *args):
        return con.execute(sql, args).fetchone()[0]

    g = has("kg_item_group")
    s = has("kg_item_surface")
    return {
        "groups": n("SELECT COUNT(*) FROM kg_item_group WHERE project_id=?", project_id) if g else 0,
        "generic_groups": n("SELECT COUNT(*) FROM kg_item_group WHERE project_id=? AND status='generic'", project_id) if g else 0,
        "surfaces": n("SELECT COUNT(*) FROM kg_item_surface WHERE project_id=?", project_id) if s else 0,
        "approved": n("SELECT COUNT(*) FROM kg_item_surface WHERE project_id=? AND status='approved'", project_id) if s else 0,
        "pending": n("SELECT COUNT(*) FROM kg_item_surface WHERE project_id=? AND status='pending'", project_id) if s else 0,
        "holding_rows": n("SELECT COUNT(*) FROM kg_item_holding"),
        "holding_group_null": n("SELECT COUNT(*) FROM kg_item_holding WHERE group_id IS NULL") if has("kg_item_holding") else 0,
        "instance_rows": n("SELECT COUNT(*) FROM kg_item_instance"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="物品同物组 v1：建表 + 登记 surface")
    ap.add_argument("--db", default=str(GEN_DB), help="generated.db 路径")
    ap.add_argument("--app-db", default=str(APP_DB), help="novel_dev.db 路径")
    ap.add_argument("--project-id", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true", help="副本验证时用，不产备份")
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    app_db = pathlib.Path(args.app_db)
    items = load_items(app_db, args.project_id)
    plan = build_plan(items)

    print(f"target={db}")
    print(f"item 实体 {len(items)} 个 → 组 {len(plan['groups'])}（其中通称 "
          f"{sum(1 for g in plan['groups'] if g['status']=='generic')}）、"
          f"写法 {len(plan['surfaces'])}")
    for c in plan["conflicts"]:
        print(f"  [撞名跳过] {c['surface']}：在 {c['owner']} 下作别名，但它是另一实体规范名")
    pending = [s for s in plan["surfaces"] if s["status"] == "pending"]
    for s in pending:
        print(f"  [待审] {s['surface']} → {s['group']}：{s['note']}")

    if args.dry_run:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        print("当前计数：", counts(con, args.project_id))
        con.close()
        print("\n[dry-run] 未写库。")
        return

    if not args.no_backup:
        backup(db, "generated-item-group")
        backup(app_db, "novel_dev-item-group")

    con = sqlite3.connect(str(db))
    try:
        ensure_schema(con)  # 先建表，counts() 才能读到组/surface 表
        before = counts(con, args.project_id)
        applied = apply_plan(con, args.project_id, plan)
        after = counts(con, args.project_id)
    finally:
        con.close()

    alias = clean_dishu_aliases(app_db, args.project_id)

    print(f"\n新增组 {applied['groups']}、写法 {applied['surfaces']}、"
          f"修正通称误登记 {applied['repaired']}")
    print("迁移前：", before)
    print("迁移后：", after)
    print("地书别名清理：", alias)
    if after["holding_rows"] != 62 or after["instance_rows"] != 1:
        raise SystemExit("持有段/实例行数变了，按边界应保持 62 / 1，请回滚检查")


if __name__ == "__main__":
    main()
