# -*- coding: utf-8 -*-
"""把已登记的「泛称写法」从单件组转入通称（generic）组。

背景：``神兵``（surface id=92）与 ``铜剑``（id=93）被当成 ``神剑`` 的写法登记成
approved，实际是类名——``神兵`` 全书 78 段 55 章，ch289 指司天监另一把剑、
ch631 指太平刀+镇国剑；``铜剑`` 是裸材质名，且是 ``青铜剑``/``黄铜剑`` 的子串。
直接驳回会丢证据，所以转成 ``status='generic'`` 的通称组：仍可检索，但读通道
会明确回答「这是类名，不指单件」。

幂等：组按 ``UNIQUE(project_id, kind, canonical_name)`` 复用；写法已经在目标
通称组里就跳过；重复跑不新增行。默认只处理这两个已核过的写法，改别的必须
显式 ``--surface``。

用法::

    python backend/migrations/2026-11-12-item-generic-v1/apply_generic_surfaces.py --dry-run
    python backend/migrations/2026-11-12-item-generic-v1/apply_generic_surfaces.py
    python ... --db backend/backups/xxx.db --app-db backend/backups/app_xxx.db --no-backup

边界：不动 ``kg_item_holding`` / ``kg_holding_story_segment``，不重抽。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
if str(PROJ / "backend") not in sys.path:
    sys.path.insert(0, str(PROJ / "backend"))

from app.kg import item_generic as ig  # noqa: E402

GEN_DB = PROJ / "backend" / "data" / "generated.db"
APP_DB = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"
DEFAULT_SURFACES = ("神兵", "铜剑")


def _backup(path: pathlib.Path) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-pre-generic-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _surface_row(con: sqlite3.Connection, project_id: int, surface: str):
    con.row_factory = sqlite3.Row
    return con.execute(
        "SELECT * FROM kg_item_surface WHERE project_id=? AND surface=?",
        (project_id, surface)).fetchone()


def _group_row(con: sqlite3.Connection, gid: int):
    con.row_factory = sqlite3.Row
    return con.execute("SELECT * FROM kg_item_group WHERE id=?", (gid,)).fetchone()


def _chapter_evidence(app: sqlite3.Connection, surface: str,
                      novel_id: int) -> tuple[int | None, str, int, int]:
    """返回（首章、原句、字串出现章数、独立出现章数），只读。

    独立出现 = 左侧不是材质/颜色字（排除「黄铜剑」「青铜剑」里的「铜剑」）。
    """
    rows = app.execute(
        "SELECT chapter_no, idx, text FROM novel_chunks WHERE novel_id=?"
        " ORDER BY chapter_no, idx", (novel_id,)).fetchall()
    chapters, independent = set(), set()
    first = None
    for ch, _idx, text in rows:
        text = text or ""
        if surface not in text:
            continue
        chapters.add(ch)
        pos = text.find(surface)
        while pos >= 0:
            if pos == 0 or text[pos - 1] not in ig.MATERIAL_BOUNDARY_CHARS:
                independent.add(ch)
                break
            pos = text.find(surface, pos + 1)
        if first is None:
            start = text.find(surface)
            first = (ch, text[max(0, start - 40):start + 80].replace("\n", " "))
    return (first[0] if first else None,
            first[1] if first else "",
            len(chapters), len(independent))


def _ensure_generic_group(con: sqlite3.Connection, project_id: int, surface: str,
                          note: str) -> tuple[int, bool]:
    row = con.execute(
        "SELECT id FROM kg_item_group WHERE project_id=? AND kind='item' AND canonical_name=?",
        (project_id, surface)).fetchone()
    if row:
        gid = int(row[0])
        con.execute(
            "UPDATE kg_item_group SET status='generic', note=? WHERE id=? AND status<>'generic'",
            (note, gid))
        return gid, False
    cur = con.execute(
        "INSERT INTO kg_item_group (project_id, kind, canonical_name, status,"
        " generic_members, note, created_at) VALUES (?, 'item', ?, 'generic', '[]', ?, ?)",
        (project_id, surface, note, datetime.now().isoformat(timespec="seconds")))
    return int(cur.lastrowid), True


def _drop_entity_alias(app: sqlite3.Connection, canonical: str, surface: str) -> bool:
    row = app.execute(
        "SELECT id, aliases FROM kg_entities WHERE project_id=5 AND kind='item'"
        " AND canonical_name=?", (canonical,)).fetchone()
    if not row:
        return False
    try:
        aliases = json.loads(row[1] or "[]")
    except (TypeError, ValueError):
        return False
    if surface not in aliases:
        return False
    aliases = [a for a in aliases if a != surface]
    app.execute("UPDATE kg_entities SET aliases=? WHERE id=?",
                (json.dumps(aliases, ensure_ascii=False), row[0]))
    return True


def run(gen_path: pathlib.Path, app_path: pathlib.Path, project_id: int,
        targets: tuple[str, ...], dry_run: bool,
        novel_id: int | None = None) -> list[dict]:
    novel_id = project_id if novel_id is None else novel_id
    gen = sqlite3.connect(gen_path)
    gen.row_factory = sqlite3.Row
    app = sqlite3.connect(app_path)
    report: list[dict] = []
    try:
        for surface in targets:
            row = _surface_row(gen, project_id, surface)
            if row is None:
                report.append({"surface": surface, "action": "skip",
                               "why": "写法未登记"})
                continue
            old = _group_row(gen, row["group_id"])
            if old is None:
                report.append({"surface": surface, "action": "skip",
                               "why": f"组 {row['group_id']} 不存在"})
                continue
            if old["status"] == "generic" and old["canonical_name"] == surface:
                report.append({"surface": surface, "action": "skip",
                               "why": "已在通称组，幂等跳过"})
                continue

            shape = ig.classify_name(surface)
            chapter, evidence, n_raw, n_indep = _chapter_evidence(app, surface, novel_id)
            note = (f"泛称：类名，不指单件（{shape.detail}；正文 {n_raw} 章出现，"
                    f"其中独立出现 {n_indep} 章）。"
                    f"原登记在单件组「{old['canonical_name']}」，"
                    f"因「{surface}」是类别/材质写法而转出。")
            gid, created = _ensure_generic_group(gen, project_id, surface, note)
            gen.execute(
                "UPDATE kg_item_surface SET group_id=?, status='approved',"
                " confidence=0.9, evidence_chapter=?, evidence=?, note=?"
                " WHERE id=?",
                (gid, chapter, evidence, note, row["id"]))
            dropped = _drop_entity_alias(app, old["canonical_name"], surface)
            # 持有段一行不动；若真有行挂在旧组上，只报告。
            stuck = gen.execute(
                "SELECT COUNT(*) FROM kg_item_holding WHERE group_id=?"
                " AND item_name=?", (old["id"], surface)).fetchone()[0]
            report.append({
                "surface": surface, "action": "move",
                "from_group": old["canonical_name"], "to_group": surface,
                "new_group_id": gid, "created_group": created,
                "status": "approved", "confidence": 0.9,
                "chapters": n_raw, "independent_chapters": n_indep,
                "evidence_chapter": chapter,
                "entity_alias_removed": dropped, "stuck_holdings": stuck,
                "rules": [shape.rule], "detail": shape.detail,
            })
        if dry_run:
            gen.rollback()
            app.rollback()
        else:
            gen.commit()
            app.commit()
    finally:
        gen.close()
        app.close()
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(GEN_DB))
    ap.add_argument("--app-db", default=str(APP_DB))
    ap.add_argument("--project-id", type=int, default=5)
    ap.add_argument("--surface", action="append", default=None,
                    help="要转入通称的写法，可重复；默认只处理已核过的两条")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    targets = tuple(args.surface or DEFAULT_SURFACES)
    gen_path, app_path = pathlib.Path(args.db), pathlib.Path(args.app_db)
    if not args.dry_run and not args.no_backup:
        print("backup:", _backup(gen_path))
        print("backup:", _backup(app_path))

    report = run(gen_path, app_path, args.project_id, targets, args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "targets": list(targets),
                      "report": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
