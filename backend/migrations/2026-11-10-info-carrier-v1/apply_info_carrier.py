# -*- coding: utf-8 -*-
"""信息载体从 ``kind='item'`` 分离 v1（历史数据修复）。

判据在 ``app/kg/info_carrier.py``：只看名字形态，语境只用来拆「文件」这类两可词。
本脚本**只改** ``kg_entities.kind``（``item`` → ``info``），不碰
``kg_item_group`` / ``kg_item_surface`` / ``kg_item_holding`` /
``kg_holding_story_segment``，也不动 ``kg_mentions``。

硬约束：任何已挂组、或出现在持有/旁注表里的实体，无论判据结果如何都**不改**，
并在报告里单列「本应改但被硬约束拦下」的条目——改了会直接打断读通道。

用法::

    python backend/migrations/2026-11-10-info-carrier-v1/apply_info_carrier.py --dry-run
    python backend/migrations/2026-11-10-info-carrier-v1/apply_info_carrier.py
    python ... --db backend/backups/xxx.db --no-backup
    python ... --rollback backend/migrations/2026-11-10-info-carrier-v1/snapshots/info_carrier_xxx.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
BACKEND = PROJ / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg import info_carrier as ic  # noqa: E402

APP_DB = BACKEND / "novel_dev.db"
GEN_DB = BACKEND / "data" / "generated.db"
BACKUP_DIR = BACKEND / "backups"
SNAP_DIR = HERE / "snapshots"


def _connect(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _load_protection(gen_path: pathlib.Path):
    """返回 (归属映射, 持有组集合, 旁注组集合)。

    归属映射只按「组的 project_id」匹配实体，避免把项目 5 的组误算到项目 7
    的同名实体上（读通道的正主判定同此口径）。
    """
    gen = _connect(gen_path)
    try:
        groups = gen.execute(
            "select id, project_id, canonical_name from kg_item_group").fetchall()
        group_pid = {g["id"]: g["project_id"] for g in groups}
        by_key: dict[tuple[int, str], set[int]] = {}
        for g in groups:
            by_key.setdefault(
                (g["project_id"], (g["canonical_name"] or "").strip()), set()
            ).add(g["id"])
        for s in gen.execute("select group_id, surface from kg_item_surface"):
            pid = group_pid.get(s["group_id"])
            if pid is None:
                continue
            by_key.setdefault((pid, (s["surface"] or "").strip()), set()).add(s["group_id"])
        holding = {r[0] for r in gen.execute(
            "select distinct group_id from kg_item_holding where group_id is not null")}
        story = {r[0] for r in gen.execute(
            "select distinct group_id from kg_holding_story_segment "
            "where group_id is not null")}
        return by_key, holding, story
    finally:
        gen.close()


def _inventory(app: sqlite3.Connection, gen_path: pathlib.Path):
    by_key, holding, story = _load_protection(gen_path)
    rows = []
    for e in app.execute(
        "select id, project_id, canonical_name, aliases, mention_count "
        "from kg_entities where kind='item' order by project_id, id"
    ):
        name = (e["canonical_name"] or "").strip()
        aliases = json.loads(e["aliases"]) if e["aliases"] else []
        forms = {name, *[a.strip() for a in aliases if isinstance(a, str) and a.strip()]}
        gids: set[int] = set()
        for f in forms:
            gids |= by_key.get((e["project_id"], f), set())
        has_holding = sorted(gids & holding)
        has_story = sorted(gids & story)
        protected = bool(gids) or bool(has_holding) or bool(has_story)

        mentions = app.execute(
            "select segment_no, context_window from kg_mentions where entity_id=? "
            "order by segment_no", (e["id"],)).fetchall()
        context = "\n".join((m["context_window"] or "") for m in mentions)
        verdict = ic.classify_name(name, context=context or None)

        if protected:
            tier = "不动"
        elif verdict.label == ic.INFO:
            tier = "会改"
        elif verdict.label == ic.REVIEW:
            tier = "待审"
        else:
            tier = "不动"
        rows.append({
            "id": e["id"], "project_id": e["project_id"], "name": name,
            "mentions": len(mentions),
            "segments": [m["segment_no"] for m in mentions],
            "verdict": verdict.label, "rule": verdict.rule,
            "detail": verdict.detail,
            "groups": sorted(gids),
            "holding_groups": has_holding, "story_groups": has_story,
            "protected": protected, "tier": tier,
        })
    return rows


def _print_inventory(rows: list[dict]) -> None:
    print("三档汇总:", Counter(r["tier"] for r in rows), "总计", len(rows))
    for tier in ("会改", "待审", "不动"):
        sel = [r for r in rows if r["tier"] == tier]
        print(f"\n== {tier}（{len(sel)}）==")
        for r in sel:
            print(f"  id={r['id']:<4} p={r['project_id']:<3} {r['name']:<12} "
                  f"mentions={r['mentions']} groups={r['groups']} "
                  f"hold={r['holding_groups']} story={r['story_groups']} "
                  f"[{r['rule']} → {r['verdict']}] {r['detail']}")
    blocked = [r for r in rows if r["protected"] and r["verdict"] == ic.INFO]
    if blocked:
        print("\n== 本应改、被硬约束拦下 ==")
        for r in blocked:
            print(f"  id={r['id']} p={r['project_id']} {r['name']} groups={r['groups']}")


def _write_snapshot(rows: list[dict]) -> pathlib.Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAP_DIR / f"info_carrier_{datetime.now():%Y%m%d-%H%M%S}.json"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "note": "kind='item' → 'info' 的历史修复；回滚把 new_kind 改回 old_kind",
        "rows": [dict(r, old_kind="item", new_kind=ic.INFO) for r in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _apply(app: sqlite3.Connection, rows: list[dict], *, dry_run: bool, no_backup: bool,
           db_path: pathlib.Path) -> int:
    changes = [r for r in rows if r["tier"] == "会改"]
    if not changes:
        print("\n没有「会改」条目，不动库。")
        return 0

    # 硬约束再核一遍（防御性）：要改的绝不能挂在组/持有/旁注上。
    bad = [r for r in changes if r["protected"]]
    if bad:
        names = "、".join(f"{r['name']}(id={r['id']})" for r in bad)
        raise SystemExit(f"停止：以下实体挂了组/持有行，按硬约束不许改：{names}")

    # 唯一键冲突检查：(project_id, kind, canonical_name)
    for r in changes:
        hit = app.execute(
            "select id from kg_entities where project_id=? and kind='info' "
            "and canonical_name=?", (r["project_id"], r["name"])).fetchone()
        if hit:
            raise SystemExit(
                f"停止：项目 {r['project_id']} 已有 info 实体「{r['name']}」"
                f"(id={hit['id']})，改 kind 会撞唯一键。")

    snap = _write_snapshot(changes)

    if dry_run:
        print("--dry-run：不备份、不写库。将改：")
        for r in changes:
            print(f"  id={r['id']} p={r['project_id']} {r['name']} "
                  f"（{r['rule']}）{r['detail']}")
        print(f"（快照草稿 → {snap}）")
        return 0

    print(f"\n快照 → {snap}")

    if not no_backup:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / f"novel_dev-pre-info-carrier-{datetime.now():%Y%m%d-%H%M%S}.db"
        shutil.copy2(db_path, dst)
        print(f"备份 → {dst}")

    with app:
        for r in changes:
            cur = app.execute(
                "update kg_entities set kind=? where id=? and kind='item'",
                (ic.INFO, r["id"]))
            if cur.rowcount != 1:
                raise SystemExit(f"停止：id={r['id']} 的 item 行未按预期更新（rowcount=0）")
    print(f"已改 {len(changes)} 条：")
    for r in changes:
        print(f"  id={r['id']} p={r['project_id']} {r['name']}：item → info")
    return len(changes)


def _rollback(app: sqlite3.Connection, snap_path: pathlib.Path) -> int:
    payload = json.loads(snap_path.read_text(encoding="utf-8"))
    rows = payload.get("rows") or []
    with app:
        for r in rows:
            app.execute(
                "update kg_entities set kind=? where id=? and kind=?",
                (r.get("old_kind", "item"), r["id"], r.get("new_kind", ic.INFO)))
    print(f"已回滚 {len(rows)} 条（快照 {snap_path.name}）：info → item")
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="信息载体从 kind='item' 分离")
    ap.add_argument("--db", default=str(APP_DB), help="应用库（默认 backend/novel_dev.db）")
    ap.add_argument("--gen-db", default=str(GEN_DB), help="生成层库（只读）")
    ap.add_argument("--dry-run", action="store_true", help="只出清单，不写库")
    ap.add_argument("--no-backup", action="store_true", help="不复制备份（在副本上跑时用）")
    ap.add_argument("--rollback", metavar="SNAPSHOT", help="按快照回滚")
    args = ap.parse_args()

    app = _connect(pathlib.Path(args.db))
    try:
        if args.rollback:
            _rollback(app, pathlib.Path(args.rollback))
            return 0
        rows = _inventory(app, pathlib.Path(args.gen_db))
        _print_inventory(rows)
        _apply(app, rows, dry_run=args.dry_run, no_backup=args.no_backup,
               db_path=pathlib.Path(args.db))
        return 0
    finally:
        app.close()


if __name__ == "__main__":
    raise SystemExit(main())
