# -*- coding: utf-8 -*-
"""把 v3 抽取缓存里的 item 实体同步进 ``novel_dev.db`` 的 ``kg_entities``。

规则（与任务口径一一对应）：

- **只写 kind='item'**，其它类别一行都不碰；
- ``canonical_name`` 用 ``fold_item_aliases`` 折叠后的名字，原写法进 ``aliases``
  （折叠函数自己就会把原写法塞进 aliases，见 canon_source.ITEM_ALIASES）；
- **幂等**：按 ``(project_id, kind, canonical_name)`` 去重；已存在的行只**合并**
  aliases，不替换、不删除，其它列（attrs/first_chapter...）一律不动；
- ``--dry-run`` 只读，报会插多少行、会改多少行的 aliases、哪些与已有行冲突。

真实库的写入只在 ``--dry-run`` 之外的调用发生，且**先备份**。

用法::

    python backend/migrations/2026-11-10-item-holding-v3/sync_item_entities.py --dry-run
    python backend/migrations/2026-11-10-item-holding-v3/sync_item_entities.py
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sqlite3
import sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
sys.path.insert(0, str(PROJ / "backend"))

from app.kg.canon_source import fold_item_aliases  # noqa: E402

REAL_DB = PROJ / "backend" / "novel_dev.db"
CACHE_DIR = PROJ / "experiments" / "2026-11-06-kg-audit" / "minimax-vol1" / "cache_minimax"
GEN_DB = PROJ / "backend" / "data" / "generated.db"


# ─── 数据源 ────────────────────────────────────────────────────────────


def load_cache_entities(cache_dir: pathlib.Path) -> dict[str, dict]:
    """把 229 章缓存里的 item 实体聚成 ``canonical_name -> 记录``。

    canonical_name 取**折叠之后**的名字；折叠前的原写法（含原始 canonical 与
    原始 aliases）都并进 aliases，并剔除与规范名相同的那一条。
    """
    agg: dict[str, dict] = {}
    files = sorted(cache_dir.glob("ch*.json"))
    if not files:
        raise SystemExit(f"缓存目录里没有 ch*.json：{cache_dir}")
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        main = copy.deepcopy(payload.get("main") or {})
        ch = payload.get("chapter") or int(f.stem[2:])
        fold_item_aliases(main)
        for ent in main.get("entities") or []:
            if ent.get("kind") != "item":
                continue
            name = (ent.get("canonical_name") or "").strip()
            if not name:
                continue
            rec = agg.setdefault(name, {"aliases": set(), "chapters": set(), "mentions": 0})
            rec["chapters"].add(int(ch))
            rec["mentions"] += 1
            for alias in ent.get("aliases") or []:
                alias = (alias or "").strip()
                if alias and alias != name:
                    rec["aliases"].add(alias)
    return agg


def load_holding_names() -> list[str]:
    if not GEN_DB.exists():
        return []
    con = sqlite3.connect(f"file:{GEN_DB}?mode=ro", uri=True)
    try:
        names = {r[0] for r in con.execute("SELECT DISTINCT item_name FROM kg_item_holding")}
    except sqlite3.Error:
        names = set()
    finally:
        con.close()
    return sorted(names)


# ─── 现有状态 ──────────────────────────────────────────────────────────


def kind_counts(con: sqlite3.Connection, project_id: int | None = None) -> dict[str, int]:
    sql = "SELECT kind, COUNT(*) FROM kg_entities"
    params: tuple = ()
    if project_id is not None:
        sql += " WHERE project_id = ?"
        params = (project_id,)
    sql += " GROUP BY kind ORDER BY kind"
    return {kind: n for kind, n in con.execute(sql, params)}


def existing_rows(con: sqlite3.Connection, project_id: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for kind, name, aliases in con.execute(
        "SELECT kind, canonical_name, aliases FROM kg_entities "
        "WHERE project_id = ? AND kind = 'item'", (project_id,)
    ):
        try:
            parsed = json.loads(aliases) if aliases else []
        except (TypeError, ValueError):
            parsed = []
        out[name] = {"kind": kind, "aliases": [a for a in parsed if isinstance(a, str)]}
    return out


# ─── 计划 ──────────────────────────────────────────────────────────────


def build_plan(agg: dict[str, dict], existing: dict[str, dict],
               *, only_names: set[str] | None = None) -> dict:
    inserts, alias_updates, unchanged, conflicts = [], [], [], []

    for name in sorted(agg):
        if only_names is not None and name not in only_names:
            continue
        rec = agg[name]
        new_aliases = sorted(rec["aliases"])
        chapters = sorted(rec["chapters"])
        row = {
            "canonical_name": name,
            "aliases": new_aliases,
            "first_chapter": chapters[0],
            "last_chapter": chapters[-1],
            "mention_count": rec["mentions"],
        }
        old = existing.get(name)
        if old is None:
            inserts.append(row)
            continue
        merged = sorted(set(old["aliases"]) | set(new_aliases))
        added = [a for a in merged if a not in old["aliases"]]
        if added:
            conflicts.append({"canonical_name": name, "added_aliases": added})
            alias_updates.append({"canonical_name": name, "aliases": merged,
                                  "added": added})
        else:
            unchanged.append(name)

    # 别名与别的物品规范名撞车：写进去以后按别名解析会二义，必须先报。
    canonical_set = set(agg)
    alias_cross = {}
    for name in agg:
        if only_names is not None and name not in only_names:
            continue
        hits = sorted(a for a in agg[name]["aliases"] if a in canonical_set and a != name)
        if hits:
            alias_cross[name] = hits

    return {"insert": inserts, "alias_updates": alias_updates,
            "unchanged": unchanged, "conflicts": conflicts,
            "alias_cross": alias_cross}


def print_plan(plan: dict, agg: dict[str, dict]) -> None:
    print(f"v3 缓存 item 实体（折叠后）：{len(agg)} 个")
    print(f"计划插入：{len(plan['insert'])} 行")
    print(f"计划更新 aliases：{len(plan['alias_updates'])} 行")
    print(f"无变化：{len(plan['unchanged'])} 行")
    print(f"与已有行冲突（aliases 有新增）：{len(plan['conflicts'])} 行")
    for c in plan["conflicts"][:20]:
        print(f"  - {c['canonical_name']}：新增 {c['added_aliases']}")
    if len(plan["alias_cross"]):
        print(f"别名与另一物品规范名撞车：{len(plan['alias_cross'])} 个物品"
              "（读通道按名解析会取先到的一行，仅提示，不阻断）")
        for name, hits in list(plan["alias_cross"].items())[:10]:
            print(f"  - {name} 的别名命中规范名 {hits}")

    sample = plan["insert"][:8]
    if sample:
        print("插入样例：")
        for row in sample:
            print(f"  - {row['canonical_name']}  aliases={row['aliases'][:6]}"
                  f"  ch={row['first_chapter']}-{row['last_chapter']}"
                  f"  mentions={row['mention_count']}")


# ─── 写库 ──────────────────────────────────────────────────────────────


def backup(db_path: pathlib.Path) -> pathlib.Path:
    backup_dir = PROJ / "backend" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"novel_dev-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
        n = chk.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
    finally:
        chk.close()
    print(f"备份：{dst}  integrity={ok}  kg_entities={n} 行")
    return dst


def apply_plan(con: sqlite3.Connection, project_id: int, plan: dict) -> tuple[int, int]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    inserted = updated = 0
    for row in plan["insert"]:
        con.execute(
            "INSERT INTO kg_entities (project_id, kind, canonical_name, aliases, attrs,"
            " first_chapter, last_chapter, mention_count, confidence,"
            " created_at, updated_at)"
            " VALUES (?, 'item', ?, ?, '{}', ?, ?, ?, 1.0, ?, ?)",
            (project_id, row["canonical_name"],
             json.dumps(row["aliases"], ensure_ascii=False),
             row["first_chapter"], row["last_chapter"], row["mention_count"],
             now, now),
        )
        inserted += 1
    for row in plan["alias_updates"]:
        con.execute(
            "UPDATE kg_entities SET aliases = ?, updated_at = ?"
            " WHERE project_id = ? AND kind = 'item' AND canonical_name = ?",
            (json.dumps(row["aliases"], ensure_ascii=False), now,
             project_id, row["canonical_name"]),
        )
        updated += 1
    con.commit()
    return inserted, updated


def main() -> None:
    ap = argparse.ArgumentParser(description="同步 v3 item 实体到 kg_entities")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--project-id", type=int, default=5,
                    help="目标 project；本库 = 5（大奉打更人）")
    ap.add_argument("--db", default=str(REAL_DB))
    ap.add_argument("--cache", default=str(CACHE_DIR))
    ap.add_argument("--only-holdings", action="store_true",
                    help="只同步持有表里出现过的物品名（影响面最小）")
    args = ap.parse_args()

    db_path = pathlib.Path(args.db)
    agg = load_cache_entities(pathlib.Path(args.cache))
    only_names = set(load_holding_names()) if args.only_holdings else None

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        before = kind_counts(con, args.project_id)
        existing = existing_rows(con, args.project_id)
        proj = con.execute("SELECT id, title FROM projects WHERE id = ?",
                           (args.project_id,)).fetchone()
        total_before = con.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
    finally:
        con.close()

    print(f"目标库：{db_path}")
    print(f"project_id={args.project_id}  projects 行={proj}")
    print(f"kg_entities 总行数={total_before}  本 project 各 kind={before or '{}'}")
    if only_names is not None:
        print(f"--only-holdings：持有表 {len(only_names)} 个物品名参与过滤")

    plan = build_plan(agg, existing, only_names=only_names)
    print_plan(plan, agg)

    if args.dry_run:
        print("\n[dry-run] 未写库。")
        return

    if not plan["insert"] and not plan["alias_updates"]:
        print("\n没有需要写入的行，跳过。")
        return

    bak = backup(db_path)
    con = sqlite3.connect(str(db_path))
    try:
        inserted, updated = apply_plan(con, args.project_id, plan)
        after = kind_counts(con, args.project_id)
        total_after = con.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
    finally:
        con.close()

    print(f"\n写入完成：插入 {inserted} 行，更新 aliases {updated} 行")
    print(f"kg_entities 总行数：{total_before} → {total_after}")
    print(f"本 project 各 kind（前）：{before or '{}'}")
    print(f"本 project 各 kind（后）：{after or '{}'}")
    print(f"\n回滚：停服后把 {bak} 复制回 {db_path}，"
          "并删掉同目录的 -wal / -shm。")


if __name__ == "__main__":
    main()
