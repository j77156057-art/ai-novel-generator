# -*- coding: utf-8 -*-
"""给 ``kg_item_group`` 加可空 ``novel_id`` 并回填（只标注，不参与任何查询）。

背景
----
``generated.db`` 的 ``kg_item_group`` 只有 ``project_id``，没有「这本书」的维度。
项目 5 同时引用《大奉打更人》(novel 5, 925 章) 与《豪门宠婚…》(novel 6, 603 章)，
两本书的章号都从 1 开始，``kg_item_surface.evidence_chapter`` 只存章号，存在潜在歧义。

本步只做「已有事实的标注」：

- ``ALTER TABLE kg_item_group ADD COLUMN novel_id INTEGER``（可空，无默认值）；
- 53 行按「规范名/写法在语料里的章命中」回填；
- ``status='generic'`` 的 8 个泛称组留 ``NULL``，语义 = 项目级泛称，不做书过滤；
- **读侧/写侧代码一行不改**，``novel_id`` 不参与任何 WHERE。

为什么没有 FK
-------------
``kg_item_group`` 在 ``generated.db``，而 ``raw_novels`` 在 ``novel_dev.db``，
两个库物理隔离、不 ATTACH，SQLite 无法跨库建外键；``ALTER TABLE ADD COLUMN``
本身也不支持加 FK。因此这里只声明为逻辑引用（指向 ``novel_dev.raw_novels.id``），
由本脚本在回填时校验取值是否存在。

用法::

    python backend/migrations/2026-11-13-item-group-novel-id-v1/apply_novel_id.py            # dry-run
    python backend/migrations/2026-11-13-item-group-novel-id-v1/apply_novel_id.py --apply
    python backend/migrations/2026-11-13-item-group-novel-id-v1/apply_novel_id.py --rollback  # 删列
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
GEN = PROJ / "backend" / "data" / "generated.db"
APP = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"
SNAP_DIR = HERE / "snapshots"
MIGRATION = "2026-11-13-item-group-novel-id-v1"


def _now() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _checkpoint(con: sqlite3.Connection) -> None:
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.DatabaseError:
        pass


def _backup(path: pathlib.Path, label: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"{path.stem}-full-{label}-{_now()}{path.suffix}"
    src = sqlite3.connect(path)
    _checkpoint(src)
    dst = sqlite3.connect(target)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def _has_column(gen: sqlite3.Connection) -> bool:
    return any(r[1] == "novel_id" for r in gen.execute("pragma table_info(kg_item_group)"))


def _chapters(app: sqlite3.Connection, name: str, novel_id: int) -> int:
    return app.execute(
        "SELECT COUNT(DISTINCT chapter_no) FROM novel_chunks"
        " WHERE novel_id=? AND text LIKE ?", (novel_id, f"%{name}%")).fetchone()[0]


def build_plan(app: sqlite3.Connection, gen: sqlite3.Connection) -> list[dict]:
    novels = [r[0] for r in app.execute("SELECT id FROM raw_novels ORDER BY id")]
    plan: list[dict] = []
    for g in gen.execute("SELECT id, project_id, canonical_name, status, note"
                         " FROM kg_item_group ORDER BY id"):
        gid, project_id, canonical, status, note = (
            g["id"], g["project_id"], g["canonical_name"], g["status"], g["note"] or "")
        names = [canonical] + [r[0] for r in gen.execute(
            "SELECT surface FROM kg_item_surface WHERE group_id=? AND status='approved'"
            " ORDER BY id", (gid,))]
        hits = {nid: max(_chapters(app, name, nid) for name in names) for nid in novels}
        best = max(hits, key=lambda k: (hits[k], -k))
        best_hits = hits[best]

        if status == "generic":
            novel_id, rule = None, "泛称组：类名不指单件，留 NULL（项目级，不做书过滤）"
        elif best_hits > 0:
            runner_up = sorted((v for k, v in hits.items() if k != best), reverse=True)[0]
            novel_id = best
            rule = (f"章命中：novel {best} {best_hits} 章"
                    + (f"，次高 {runner_up} 章" if runner_up else ""))
        elif "从「神剑」组拆出" in note:
            novel_id, rule = 5, "正文零命中；来源链：note 记「从「神剑」组拆出」，神剑组归 novel 5"
        else:
            novel_id, rule = None, "正文零命中且无来源链，暂留 NULL，待人工判断"
        plan.append({
            "id": gid,
            "project_id": project_id,
            "canonical_name": canonical,
            "status": status,
            "novel_id": novel_id,
            "rule": rule,
            "hits": {str(k): v for k, v in hits.items()},
            "best_surface": max(names, key=lambda n: _chapters(app, n, best)) if best_hits else "",
        })
    return plan


def _schema(gen: sqlite3.Connection) -> list[tuple]:
    return [(r[0], r[1], (r[2] or "").replace("\n", " ")) for r in gen.execute(
        "SELECT type, name, sql FROM sqlite_master"
        " WHERE tbl_name='kg_item_group' OR name LIKE 'ux_item_group%' ORDER BY name")]


def apply_plan(plan: list[dict], stamp: str) -> dict:
    gen = sqlite3.connect(GEN)
    gen.row_factory = sqlite3.Row
    app = sqlite3.connect(APP)

    before = _schema(gen)
    bak_gen = _backup(GEN, "novel-id")
    bak_app = _backup(APP, "novel-id")

    gen.execute("ALTER TABLE kg_item_group ADD COLUMN novel_id INTEGER")
    for item in plan:
        gen.execute("UPDATE kg_item_group SET novel_id=? WHERE id=?",
                    (item["novel_id"], item["id"]))
    gen.commit()

    after = _schema(gen)
    snap = {
        "migration": MIGRATION,
        "applied_at": stamp,
        "generated_db": str(GEN),
        "app_db": str(APP),
        "backups": {"generated": str(bak_gen), "novel_dev": str(bak_app)},
        "schema_before": before,
        "schema_after": after,
        "rows": plan,
    }
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"group_novel_id_{stamp}.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    gen.close()
    app.close()
    return {"snapshot": path, "backups": (bak_gen, bak_app),
            "schema_before": before, "schema_after": after}


def verify() -> dict:
    gen = sqlite3.connect(GEN)
    gen.row_factory = sqlite3.Row
    app = sqlite3.connect(APP)
    novels = {r[0] for r in app.execute("SELECT id FROM raw_novels")}
    total = gen.execute("SELECT COUNT(*) FROM kg_item_group").fetchone()[0]
    nulls = gen.execute("SELECT COUNT(*) FROM kg_item_group WHERE novel_id IS NULL").fetchone()[0]
    values = [r[0] for r in gen.execute(
        "SELECT DISTINCT novel_id FROM kg_item_group WHERE novel_id IS NOT NULL")]
    bad = [v for v in values if v not in novels]
    by_novel = {str(v): gen.execute(
        "SELECT COUNT(*) FROM kg_item_group WHERE novel_id IS ?", (v,)).fetchone()[0] for v in values}
    return {"total": total, "null": nulls, "values": values, "bad": bad,
            "by_novel": by_novel,
            "schema": _schema(gen)}


def rollback() -> dict:
    gen = sqlite3.connect(GEN)
    gen.row_factory = sqlite3.Row
    if not _has_column(gen):
        return {"dropped": False, "why": "novel_id 不存在"}
    before = _schema(gen)
    bak = _backup(GEN, "novel-id-rollback")
    gen.execute("ALTER TABLE kg_item_group DROP COLUMN novel_id")
    gen.commit()
    after = _schema(gen)
    gen.close()
    return {"dropped": True, "backup": str(bak), "schema_before": before, "schema_after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        print(json.dumps(rollback(), ensure_ascii=False, indent=2))
        return 0

    app = sqlite3.connect(APP)
    gen = sqlite3.connect(GEN)
    gen.row_factory = sqlite3.Row
    plan = build_plan(app, gen)

    print(f"组数 {len(plan)}；NULL {sum(1 for p in plan if p['novel_id'] is None)}；"
          f"回填 {sum(1 for p in plan if p['novel_id'] is not None)}")
    for p in plan:
        if p["novel_id"] is None or p["rule"].startswith(("章命中：novel 5 0", "正文零命中")) \
                or "次高" in p["rule"]:
            print(f"  gid={p['id']:3d} {p['canonical_name']:14s} -> {p['novel_id']}  {p['rule']}")

    if _has_column(gen):
        print("\n列已存在，执行回填自检：")
        print(json.dumps(verify(), ensure_ascii=False, indent=2))
        return 0

    if not args.apply:
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    stamp = _now()
    out = apply_plan(plan, stamp)
    print("\n备份:", out["backups"])
    print("快照:", out["snapshot"])
    print("\n结构 diff（改前 vs 改后）:")
    for a, b in zip(out["schema_before"], out["schema_after"]):
        print("  -", a[1], "|", a[2][:120])
        print("  +", b[1], "|", b[2][:120])
    print("\n自检:", json.dumps(verify(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
