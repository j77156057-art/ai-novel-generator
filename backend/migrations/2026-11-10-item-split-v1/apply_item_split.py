# -*- coding: utf-8 -*-
"""物品治理落库（2026-11-10）：神剑拆三组 + 玉石小镜漏口降级 + 铜锣/长枪出表 + 腰牌删别名。

五条一次做完，对应 ``reports/item-holding-change-checklist-2026-11-10.md``：

1. ``神剑``（组 24）拆成三组：``镇国剑`` / ``洛玉衡祖传神剑`` / ``佛门金身黄金神剑``。
   组 24 保留 ``canonical_name='神剑'``，但 ``status='generic'``（通称/歧义名），
   ``generic_members`` 指向三个新组。写法 86/87/88/89/90/91/94/95/96/97/98（11 条）
   迁到 ``镇国剑``；持有段 59/60 与旁注 1/2/3 改挂 ``镇国剑``。
   **``id=59 镇北王 99–106`` 保留不删**（用户明确拍板）。
   实体 26 的规范名同步改为 ``镇国剑``（否则读通道会判「写法组 ≠ 实体组」歧义）。
2. ``玉石碎片``(surface 65) / ``枕头底下的玉石小镜``(66) / ``那面玉石小镜``(67)
   从组 9（地书）移到通称组 46（玉石小镜），让 generic 判定真正生效。
3. ``铜锣``（组 32）出表：删 8 条写法 + 持有段 43 + 组 32 + 实体 34；
   ``法器铜锣`` 实体 19 去掉 ``铜锣`` 别名（最后做，避免中间态唯一命中）。
   ``长枪``（组 36）出表：持有段 48 **先 ``valid_to=228``**，再改名 ``银枪`` +
   挂组 33；写法 36 迁组 33；删组 36、实体 38；``银枪`` 实体 35 去掉 ``长枪`` 别名。
4. ``腰牌``：实体 31（金牌）aliases 去掉 ``腰牌``。不合并、不降泛称。
5. 读通道分层与完整时间线由 ``app/kg/item_holding_read.py`` 负责，本脚本不动代码。

硬约束落地：
- 删组前跑孤儿检查（``kg_item_holding.group_id`` 无外键，SQLite 默认不级联）。
- 每步写 JSON 快照到 ``snapshots/``，``--rollback`` 可按快照恢复。
- 库写入前 ``_backup()`` 复制到 ``backend/backups/``。

用法::

    python backend/migrations/2026-11-10-item-split-v1/apply_item_split.py --dry-run
    python backend/migrations/2026-11-10-item-split-v1/apply_item_split.py
    python ... --db backend/backups/xxx.db --app-db backend/backups/app_xxx.db --no-backup
    python ... --rollback backend/migrations/2026-11-10-item-split-v1/snapshots/item_split_xxx.json
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
APP_DB = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"
SNAP_DIR = HERE / "snapshots"

PROJECT_ID = 5

#: 神剑组与要迁走的 11 条写法
SWORD_GID = 24
SWORD_SURFACES = (86, 87, 88, 89, 90, 91, 94, 95, 96, 97, 98)
SWORD_HOLDINGS = (59, 60)
SWORD_SEGMENTS = (1, 2, 3)
SWORD_ENTITY = 26

#: 玉石小镜三条漏口
JADE_SURFACES = (65, 66, 67)
JADE_GENERIC_GID = 46

#: 铜锣 / 长枪出表
BRONZE_GID = 32
BRONZE_SURFACES = (32, 114, 115, 116, 117, 118, 119, 120)
BRONZE_HOLDING = 43
BRONZE_ENTITY = 34
BRONZE_ALIAS_ENTITY = 19  # 法器铜锣，去掉「铜锣」别名

SPEAR_GID = 36
SPEAR_SURFACE = 36
SPEAR_HOLDING = 48
SPEAR_ENTITY = 38
SPEAR_TARGET_GID = 33
SPEAR_TARGET_NAME = "银枪"
SPEAR_CLOSE_AT = 228
SPEAR_ALIAS_ENTITY = 35

#: 腰牌
BADGE_ALIAS_ENTITY = 31
BADGE_ALIAS = "腰牌"


def _backup(path: pathlib.Path) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-pre-split-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _pk(row: sqlite3.Row) -> str:
    return "id"


def _fetch_rows(con: sqlite3.Connection, table: str, ids) -> list[dict]:
    ids = [int(i) for i in ids]
    if not ids:
        return []
    rows = con.execute(
        f"SELECT * FROM {table} WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
        ids).fetchall()
    return [dict(r) for r in rows]


def _snap(con: sqlite3.Connection, snap: dict, kind: str, table: str, ids) -> None:
    """快照只记「第一次见到的原始行」，同一 id 重复更新不覆盖（否则回滚会记成中间态）。"""
    bucket = snap.setdefault(kind, {}).setdefault(table, [])
    seen = {r["id"] for r in bucket}
    rows = [r for r in _fetch_rows(con, table, ids) if r["id"] not in seen]
    if rows:
        bucket.extend(rows)


def _orphans(con: sqlite3.Connection) -> dict:
    """group_id 指向不存在组的行（读通道里表现为「消失」而不是「无记录」）。"""
    holding = [dict(r) for r in con.execute(
        "SELECT h.id, h.item_name, h.group_id FROM kg_item_holding h"
        " LEFT JOIN kg_item_group g ON g.id = h.group_id"
        " WHERE h.group_id IS NOT NULL AND g.id IS NULL ORDER BY h.id")]
    story = [dict(r) for r in con.execute(
        "SELECT s.id, s.item_name, s.group_id FROM kg_holding_story_segment s"
        " LEFT JOIN kg_item_group g ON g.id = s.group_id"
        " WHERE s.group_id IS NOT NULL AND g.id IS NULL ORDER BY s.id")]
    return {"holding": holding, "story": story}


def _assert_no_refs(con: sqlite3.Connection, gid: int) -> None:
    h = con.execute("SELECT COUNT(*) FROM kg_item_holding WHERE group_id=?",
                    (gid,)).fetchone()[0]
    s = con.execute(
        "SELECT COUNT(*) FROM kg_holding_story_segment WHERE group_id=?",
        (gid,)).fetchone()[0]
    if h or s:
        raise RuntimeError(
            f"删组 {gid} 前仍有引用：holding={h} story={s}；"
            "先改挂/删除这些行，别留孤儿")


def _group(con: sqlite3.Connection, name: str):
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM kg_item_group WHERE project_id=? AND kind='item'"
        " AND canonical_name=?", (PROJECT_ID, name)).fetchone()


def _ensure_group(con: sqlite3.Connection, name: str, status: str,
                  members: list[int], note: str, snap: dict) -> int:
    row = _group(con, name)
    if row is not None:
        return int(row["id"])
    cur = con.execute(
        "INSERT INTO kg_item_group (project_id, kind, canonical_name, status,"
        " generic_members, note, created_at) VALUES (?, 'item', ?, ?, ?, ?, ?)",
        (PROJECT_ID, name, status, json.dumps(members), note,
         datetime.now(tz=None).astimezone().isoformat(timespec="seconds")))
    gid = int(cur.lastrowid)
    snap.setdefault("created", {}).setdefault("kg_item_group", []).append(
        {"id": gid, "canonical_name": name})
    return gid


def _ensure_surface(con: sqlite3.Connection, surface: str, gid: int,
                    note: str, snap: dict) -> int:
    row = con.execute(
        "SELECT * FROM kg_item_surface WHERE project_id=? AND surface=?",
        (PROJECT_ID, surface)).fetchone()
    if row is not None:
        return int(row["id"])
    cur = con.execute(
        "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
        " source, confidence, instance_label, evidence_chapter, evidence, note,"
        " created_at) VALUES (?, ?, ?, 'approved', 'migration', 1.0, NULL, NULL,"
        " '', ?, ?)",
        (PROJECT_ID, surface, gid, note,
         datetime.now(tz=None).astimezone().isoformat(timespec="seconds")))
    sid = int(cur.lastrowid)
    snap.setdefault("created", {}).setdefault("kg_item_surface", []).append(
        {"id": sid, "surface": surface})
    return sid


def _update_surface_group(con: sqlite3.Connection, sid: int, gid: int,
                          snap: dict) -> None:
    _snap(con, snap, "updated", "kg_item_surface", [sid])
    con.execute("UPDATE kg_item_surface SET group_id=? WHERE id=?", (gid, sid))


def _delete_surface(con: sqlite3.Connection, sid: int, snap: dict) -> None:
    _snap(con, snap, "deleted", "kg_item_surface", [sid])
    con.execute("DELETE FROM kg_item_surface WHERE id=?", (sid,))


def _delete_holding(con: sqlite3.Connection, hid: int, snap: dict) -> None:
    _snap(con, snap, "deleted", "kg_item_holding", [hid])
    con.execute("DELETE FROM kg_item_holding WHERE id=?", (hid,))


def _update_holding(con: sqlite3.Connection, hid: int, sets: str,
                    params: tuple, snap: dict) -> None:
    _snap(con, snap, "updated", "kg_item_holding", [hid])
    con.execute(f"UPDATE kg_item_holding SET {sets} WHERE id=?", (*params, hid))


def _update_segment(con: sqlite3.Connection, sid: int, sets: str,
                    params: tuple, snap: dict) -> None:
    _snap(con, snap, "updated", "kg_holding_story_segment", [sid])
    con.execute(f"UPDATE kg_holding_story_segment SET {sets} WHERE id=?",
                (*params, sid))


def _entity(app: sqlite3.Connection, eid: int):
    app.row_factory = sqlite3.Row
    return app.execute("SELECT * FROM kg_entities WHERE id=?", (eid,)).fetchone()


def _update_entity(app: sqlite3.Connection, eid: int, *,
                   canonical: str | None = None,
                   aliases: list[str] | None = None,
                   snap: dict) -> None:
    _snap(app, snap, "updated", "kg_entities", [eid])
    if canonical is not None:
        app.execute("UPDATE kg_entities SET canonical_name=? WHERE id=?",
                    (canonical, eid))
    if aliases is not None:
        app.execute("UPDATE kg_entities SET aliases=? WHERE id=?",
                    (json.dumps(aliases, ensure_ascii=False), eid))


def _delete_entity(app: sqlite3.Connection, eid: int, snap: dict) -> None:
    _snap(app, snap, "deleted", "kg_entities", [eid])
    app.execute("DELETE FROM kg_entities WHERE id=?", (eid,))


def _drop_alias(app: sqlite3.Connection, eid: int, alias: str, snap: dict) -> bool:
    row = _entity(app, eid)
    if row is None:
        return False
    try:
        aliases = json.loads(row["aliases"] or "[]")
    except (TypeError, ValueError):
        return False
    if alias not in aliases:
        return False
    _update_entity(app, eid, aliases=[a for a in aliases if a != alias], snap=snap)
    return True


# ---------------------------------------------------------------------------
# 五条变更
# ---------------------------------------------------------------------------
def _step_sword(gen, app, snap: dict) -> dict:
    zg = _ensure_group(
        gen, "镇国剑", "active", [],
        "从「神剑」组拆出：开国皇帝佩剑、镇国宝剑等 11 条写法的归属本体。", snap)
    lyh = _ensure_group(
        gen, "洛玉衡祖传神剑", "active", [],
        "从「神剑」组拆出的独立一件（洛玉衡祖传），当前无持有段。", snap)
    fm = _ensure_group(
        gen, "佛门金身黄金神剑", "active", [],
        "从「神剑」组拆出的独立一件（佛门金身黄金神剑），当前无持有段。", snap)
    _ensure_surface(gen, "洛玉衡祖传神剑", lyh,
                    "拆分「神剑」组时补登的组规范名", snap)
    _ensure_surface(gen, "佛门金身黄金神剑", fm,
                    "拆分「神剑」组时补登的组规范名", snap)

    for sid in SWORD_SURFACES:
        _update_surface_group(gen, sid, zg, snap)

    _snap(gen, snap, "updated", "kg_item_group", [SWORD_GID])
    gen.execute(
        "UPDATE kg_item_group SET status='generic', generic_members=?, note=?"
        " WHERE id=?",
        (json.dumps([zg, lyh, fm]),
         "通称/歧义名：正文里至少指三件不同的剑（镇国剑、洛玉衡祖传神剑、"
         "佛门金身黄金神剑），不指向单件", SWORD_GID))

    for hid in SWORD_HOLDINGS:
        _update_holding(gen, hid, "group_id=?, item_name='镇国剑'",
                        (zg,), snap)
    for sid in SWORD_SEGMENTS:
        _update_segment(gen, sid, "group_id=?, item_name='镇国剑'",
                        (zg,), snap)

    ent = _entity(app, SWORD_ENTITY)
    if ent is not None:
        try:
            aliases = json.loads(ent["aliases"] or "[]")
        except (TypeError, ValueError):
            aliases = []
        _update_entity(app, SWORD_ENTITY,
                       canonical="镇国剑" if ent["canonical_name"] != "镇国剑" else None,
                       aliases=[a for a in aliases if a != "镇国剑"], snap=snap)
    return {"镇国剑_gid": zg, "洛玉衡祖传神剑_gid": lyh,
            "佛门金身黄金神剑_gid": fm,
            "moved_surfaces": list(SWORD_SURFACES),
            "moved_holdings": list(SWORD_HOLDINGS),
            "moved_segments": list(SWORD_SEGMENTS),
            "entity_renamed": SWORD_ENTITY}


def _step_jade(gen, snap: dict) -> dict:
    for sid in JADE_SURFACES:
        _update_surface_group(gen, sid, JADE_GENERIC_GID, snap)
    return {"moved_surfaces": list(JADE_SURFACES),
            "to_generic_group": JADE_GENERIC_GID}


def _step_bronze(gen, app, snap: dict) -> dict:
    for sid in BRONZE_SURFACES:
        _delete_surface(gen, sid, snap)
    _delete_holding(gen, BRONZE_HOLDING, snap)
    _assert_no_refs(gen, BRONZE_GID)
    _snap(gen, snap, "deleted", "kg_item_group", [BRONZE_GID])
    gen.execute("DELETE FROM kg_item_group WHERE id=?", (BRONZE_GID,))
    if _entity(app, BRONZE_ENTITY) is not None:
        _delete_entity(app, BRONZE_ENTITY, snap)
    return {"deleted_surfaces": list(BRONZE_SURFACES),
            "deleted_holding": BRONZE_HOLDING,
            "deleted_group": BRONZE_GID,
            "deleted_entity": BRONZE_ENTITY}


def _step_spear(gen, app, snap: dict) -> dict:
    # 顺序不能颠倒：先关区间，再改名/改组，否则撞 ux_item_holding_open。
    _update_holding(gen, SPEAR_HOLDING, "valid_to=?", (SPEAR_CLOSE_AT,), snap)
    _update_holding(gen, SPEAR_HOLDING, "item_name=?, group_id=?",
                    (SPEAR_TARGET_NAME, SPEAR_TARGET_GID), snap)
    _update_surface_group(gen, SPEAR_SURFACE, SPEAR_TARGET_GID, snap)
    _assert_no_refs(gen, SPEAR_GID)
    _snap(gen, snap, "deleted", "kg_item_group", [SPEAR_GID])
    gen.execute("DELETE FROM kg_item_group WHERE id=?", (SPEAR_GID,))
    if _entity(app, SPEAR_ENTITY) is not None:
        _delete_entity(app, SPEAR_ENTITY, snap)
    _drop_alias(app, SPEAR_ALIAS_ENTITY, "长枪", snap)
    return {"closed_holding": SPEAR_HOLDING, "valid_to": SPEAR_CLOSE_AT,
            "renamed_to": SPEAR_TARGET_NAME, "to_group": SPEAR_TARGET_GID,
            "moved_surface": SPEAR_SURFACE,
            "deleted_group": SPEAR_GID, "deleted_entity": SPEAR_ENTITY,
            "alias_removed_from_entity": SPEAR_ALIAS_ENTITY}


def _step_bronze_alias(app, snap: dict) -> dict:
    # 必须晚于删除实体 34：否则「铜锣」会唯一落到 19 上，反而变成可解析。
    return {"entity": BRONZE_ALIAS_ENTITY,
            "alias_removed": _drop_alias(app, BRONZE_ALIAS_ENTITY, "铜锣", snap)}


def _step_badge_alias(app, snap: dict) -> dict:
    return {"entity": BADGE_ALIAS_ENTITY,
            "alias_removed": _drop_alias(app, BADGE_ALIAS_ENTITY, BADGE_ALIAS, snap)}


def run(gen_path: pathlib.Path, app_path: pathlib.Path,
        dry_run: bool) -> tuple[dict, dict]:
    gen = sqlite3.connect(gen_path)
    gen.row_factory = sqlite3.Row
    app = sqlite3.connect(app_path)
    app.row_factory = sqlite3.Row
    snap: dict = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "created": {}, "updated": {}, "deleted": {},
    }
    report: dict = {}
    try:
        if _group(gen, "神剑") is None:
            raise RuntimeError("找不到组「神剑」（id=24），库状态与预期不符，中止")

        report["sword"] = _step_sword(gen, app, snap)
        report["jade"] = _step_jade(gen, snap)
        report["bronze"] = _step_bronze(gen, app, snap)
        report["spear"] = _step_spear(gen, app, snap)
        report["bronze_alias"] = _step_bronze_alias(app, snap)
        report["badge_alias"] = _step_badge_alias(app, snap)

        report["orphans"] = _orphans(gen)
        if report["orphans"]["holding"] or report["orphans"]["story"]:
            raise RuntimeError(f"出现孤儿行，已回滚：{report['orphans']}")

        report["counts"] = {
            "groups": gen.execute("SELECT COUNT(*) FROM kg_item_group").fetchone()[0],
            "surfaces": gen.execute("SELECT COUNT(*) FROM kg_item_surface").fetchone()[0],
            "holdings": gen.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0],
            "segments": gen.execute(
                "SELECT COUNT(*) FROM kg_holding_story_segment").fetchone()[0],
            "item_entities": app.execute(
                "SELECT COUNT(*) FROM kg_entities WHERE kind='item'").fetchone()[0],
        }
        if dry_run:
            gen.rollback()
            app.rollback()
        else:
            gen.commit()
            app.commit()
    except Exception:
        gen.rollback()
        app.rollback()
        raise
    finally:
        gen.close()
        app.close()
    return report, snap


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------
def _restore_row(con: sqlite3.Connection, table: str, row: dict) -> None:
    cols = [c for c in row if c in _cols(con, table)]
    sets = ", ".join(f"{c}=?" for c in cols if c != "id")
    con.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*[row[c] for c in cols if c != "id"], row["id"]))


def _insert_row(con: sqlite3.Connection, table: str, row: dict) -> None:
    cols = [c for c in row if c in _cols(con, table)]
    con.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)})"
        f" VALUES ({', '.join('?' * len(cols))})", [row[c] for c in cols])


def rollback(snap_path: pathlib.Path, gen_path: pathlib.Path,
             app_path: pathlib.Path) -> dict:
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    gen = sqlite3.connect(gen_path)
    app = sqlite3.connect(app_path)
    try:
        # 1) 恢复被改/被删的组（先组，后写法）
        for row in snap.get("updated", {}).get("kg_item_group", []):
            _restore_row(gen, "kg_item_group", row)
        for row in snap.get("deleted", {}).get("kg_item_group", []):
            _insert_row(gen, "kg_item_group", row)
        # 2) 写法
        for row in snap.get("updated", {}).get("kg_item_surface", []):
            _restore_row(gen, "kg_item_surface", row)
        for row in snap.get("deleted", {}).get("kg_item_surface", []):
            _insert_row(gen, "kg_item_surface", row)
        # 3) 持有段（48 的 item_name / valid_to 必须一次性恢复）
        for row in snap.get("updated", {}).get("kg_item_holding", []):
            _restore_row(gen, "kg_item_holding", row)
        for row in snap.get("deleted", {}).get("kg_item_holding", []):
            _insert_row(gen, "kg_item_holding", row)
        # 4) 旁注
        for row in snap.get("updated", {}).get("kg_holding_story_segment", []):
            _restore_row(gen, "kg_holding_story_segment", row)
        # 5) 实体
        for row in snap.get("updated", {}).get("kg_entities", []):
            _restore_row(app, "kg_entities", row)
        for row in snap.get("deleted", {}).get("kg_entities", []):
            _insert_row(app, "kg_entities", row)
        # 6) 删掉新建的写法与组
        for row in snap.get("created", {}).get("kg_item_surface", []):
            gen.execute("DELETE FROM kg_item_surface WHERE id=?", (row["id"],))
        for row in snap.get("created", {}).get("kg_item_group", []):
            gen.execute("DELETE FROM kg_item_group WHERE id=?", (row["id"],))
        gen.commit()
        app.commit()
        return {"restored": {k: sum(len(v) for v in snap.get(k, {}).values())
                             for k in ("created", "updated", "deleted")}}
    finally:
        gen.close()
        app.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(GEN_DB))
    ap.add_argument("--app-db", default=str(APP_DB))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--rollback", default=None, help="按快照 JSON 回滚")
    args = ap.parse_args()

    gen_path, app_path = pathlib.Path(args.db), pathlib.Path(args.app_db)

    if args.rollback:
        print(json.dumps(rollback(pathlib.Path(args.rollback), gen_path, app_path),
                         ensure_ascii=False, indent=2))
        return

    if not args.dry_run and not args.no_backup:
        print("backup:", _backup(gen_path))
        print("backup:", _backup(app_path))

    report, snap = run(gen_path, app_path, args.dry_run)
    if args.dry_run:
        print(json.dumps({"dry_run": True, "report": report},
                         ensure_ascii=False, indent=2))
        return

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snap_path = SNAP_DIR / f"item_split_{stamp}.json"
    snap_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(json.dumps({"dry_run": False, "snapshot": str(snap_path),
                      "report": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
