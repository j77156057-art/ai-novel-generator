# -*- coding: utf-8 -*-
"""给实体层 / 提及层加书维度（``novel_dev.db``）。

为什么只改这两张表（认定过程见报告 §I）：

- ``kg_entities`` / ``kg_mentions`` 是 **v3.0 批量抽取真正在写**的表
  （``batch_worker._persist_extraction`` 落实体、``_upsert_entity`` 落提及），
  各自有数据、最近写入是 2026-11-10；
- ``kg_character_state`` / ``kg_entity_knowledge`` / ``kg_foreshadows`` /
  ``kg_relation_evolution`` 是 0 行的死表，**不动**；
- ``kg_mentions`` 的 ``chunk_id`` 链不存在：提及的上游是 ``generations``
  （本项目正文），不是 ``novel_chunks``。所以提及不能「按 chunk 归书」，
  只能跟随所属实体。

改动：

1. ``kg_entities`` 加可空 ``novel_id``，唯一键从
   ``(project_id, kind, canonical_name)`` 换成
   ``(project_id, kind, canonical_name, COALESCE(novel_id, -1))``
   ——表级 UNIQUE 改不了，只能 12-step 重建（保留 id，relations/mentions 的
   外键不断）；
2. ``kg_mentions`` 加可空 ``novel_id``（唯一键不动）；
3. 回填：实体按「名字在项目参考语料里的唯一命中」归书，归不出留 ``NULL``；
   提及跟随所属实体的书。

停止条件（脚本内硬校验，触发即退出、不写库）：

- 实体名字在组表（``kg_item_group.novel_id``）里归书 A、在实体层要归书 B
  （A/B 都非空且不同）→ 两套分槽规则矛盾，先报设计问题；
- 新唯一键在现有数据上撞行（理论不会：新键比旧键宽松）。

用法::

    python apply_kg_book.py                 # dry-run（默认）
    python apply_kg_book.py --apply         # 重建 + 回填
    python apply_kg_book.py --rollback      # 结构与值回退
    python apply_kg_book.py --db <副本> --generated-db <副本> --backup-dir <目录>
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
GEN_DB = BACKEND / "data" / "generated.db"
BACKUP_DIR = BACKEND / "backups"
SNAP_DIR = HERE.parent / "snapshots"

ENT = "kg_entities"
MENTION = "kg_mentions"

ENTITY_COLS = ("id", "project_id", "kind", "canonical_name", "aliases", "attrs",
               "first_chapter", "last_chapter", "mention_count", "confidence",
               "created_at", "updated_at")
MENTION_COLS = ("id", "project_id", "entity_id", "generation_id", "segment_no",
                "context_window", "mention_kind")

NEW_ENTITIES_DDL = """
CREATE TABLE kg_entities_new (
    id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    novel_id INTEGER,
    kind VARCHAR(16) NOT NULL,
    canonical_name VARCHAR(128) NOT NULL,
    aliases JSON,
    attrs JSON,
    first_chapter INTEGER,
    last_chapter INTEGER,
    mention_count INTEGER NOT NULL,
    confidence FLOAT NOT NULL,
    created_at DATETIME,
    updated_at DATETIME,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES projects (id)
)
"""

OLD_ENTITIES_DDL = """
CREATE TABLE kg_entities_old (
    id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    kind VARCHAR(16) NOT NULL,
    canonical_name VARCHAR(128) NOT NULL,
    aliases JSON,
    attrs JSON,
    first_chapter INTEGER,
    last_chapter INTEGER,
    mention_count INTEGER NOT NULL,
    confidence FLOAT NOT NULL,
    created_at DATETIME,
    updated_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT uq_kg_entity UNIQUE (project_id, kind, canonical_name),
    FOREIGN KEY(project_id) REFERENCES projects (id)
)
"""


def _now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(db: pathlib.Path, tag: str, backup_dir: pathlib.Path) -> pathlib.Path:
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


def _md5(rows) -> str:
    return hashlib.md5(
        json.dumps([tuple(r) for r in rows], ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()


def _table_rows(con: sqlite3.Connection, table: str, cols: tuple[str, ...]):
    have = set(_cols(con, table))
    sel = ", ".join(c if c in have else "NULL" for c in cols)
    return con.execute(f'SELECT {sel} FROM "{table}" ORDER BY id').fetchall()


def state(con: sqlite3.Connection) -> dict:
    ent_cols = _cols(con, ENT)
    men_cols = _cols(con, MENTION)
    return {
        "entity_columns": ent_cols,
        "mention_columns": men_cols,
        "entity_has_novel": "novel_id" in ent_cols,
        "mention_has_novel": "novel_id" in men_cols,
        "entity_rows": len(_table_rows(con, ENT, ENTITY_COLS)),
        "mention_rows": len(_table_rows(con, MENTION, MENTION_COLS)),
        "entity_md5": _md5(_table_rows(con, ENT, ENTITY_COLS)),
        "mention_md5": _md5(_table_rows(con, MENTION, MENTION_COLS)),
        "entity_schema": [tuple(r) for r in con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name=?"
            " ORDER BY type, name", (ENT,))],
        "mention_schema": [tuple(r) for r in con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name=?"
            " ORDER BY type, name", (MENTION,))],
    }


def _refs(raw) -> list[int]:
    if isinstance(raw, (list, tuple)):
        items = raw
    elif isinstance(raw, str):
        try:
            items = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(items, (list, tuple)):
            return []
    else:
        return []
    out = []
    for x in items:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _book_text(con: sqlite3.Connection) -> dict[int, str]:
    """每本书的全文（按 chunk 拼），只为回答「这个名字出现在哪些书」。"""
    out: dict[int, str] = {}
    for (bid,) in con.execute("SELECT id FROM raw_novels ORDER BY id"):
        parts = [r[0] or "" for r in con.execute(
            "SELECT text FROM novel_chunks WHERE novel_id=?"
            " ORDER BY chapter_no, id", (bid,))]
        out[int(bid)] = "\n".join(parts)
    return out


def decide(project_refs: list[int], names: list[str],
           texts: dict[int, str]) -> tuple[int | None, str]:
    """返回 (书 id 或 None, 依据)。规则：**唯一命中参考语料**才归书。"""
    hits = {bid for bid, text in texts.items() if any(n and n in text for n in names)}
    in_refs = hits & set(project_refs)
    if len(in_refs) == 1:
        return next(iter(in_refs)), "唯一命中参考语料"
    if len(in_refs) > 1:
        return None, f"命中多本参考语料 {sorted(in_refs)}"
    if hits:
        return None, f"只命中非参考语料 {sorted(hits)}"
    return None, "语料里没有这个名字"


def _group_books(gen_db: pathlib.Path) -> dict[tuple[int, str], list[dict]]:
    """generated.db 里同名组归了哪本书（停止条件的对照物）。"""
    if not gen_db.exists():
        return {}
    con = sqlite3.connect(f"file:{gen_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "kg_item_group" not in tables:
            return {}
        cols = {r["name"] for r in con.execute("PRAGMA table_info(kg_item_group)")}
        has_novel = "novel_id" in cols
        out: dict[tuple[int, str], list[dict]] = {}
        for r in con.execute(
                "SELECT id, project_id, canonical_name"
                + (", novel_id" if has_novel else ", NULL AS novel_id")
                + " FROM kg_item_group"):
            out.setdefault((int(r["project_id"]), r["canonical_name"]), []).append(
                {"gid": r["id"], "novel": r["novel_id"]})
        return out
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def plan(con: sqlite3.Connection, gen_db: pathlib.Path) -> dict:
    """只读计算：每个实体归哪本书、为什么归不出、与组是否矛盾。"""
    texts = _book_text(con)
    projects = {int(r["id"]): dict(r) for r in con.execute(
        "SELECT id, title, reference_novel_ids FROM projects ORDER BY id")}
    proj_ns = {r["id"] for r in con.execute("SELECT id FROM projects")}
    groups = _group_books(gen_db)

    entities = []
    for r in con.execute(
            f'SELECT {", ".join(ENTITY_COLS)} FROM {ENT} ORDER BY id'):
        row = dict(zip(ENTITY_COLS, r))
        pid = int(row["project_id"])
        refs = _refs(projects.get(pid, {}).get("reference_novel_ids"))
        try:
            aliases = json.loads(row["aliases"] or "[]") or []
        except (TypeError, ValueError):
            aliases = []
        names = [row["canonical_name"]] + [a for a in aliases if isinstance(a, str)]
        book, why = decide(refs, names, texts)
        g = groups.get((pid, row["canonical_name"]), [])
        conflict = bool(g) and book is not None and any(
            x["novel"] is not None and int(x["novel"]) != int(book) for x in g)
        entities.append({
            "eid": int(row["id"]), "pid": pid, "kind": row["kind"],
            "name": row["canonical_name"], "book": book, "why": why,
            "groups": g, "group_book_conflict": conflict,
            "project_exists": pid in proj_ns,
        })

    stats: dict = {"total": len(entities), "assigned": 0, "null": 0,
                   "by_reason": {}, "by_book": {}, "conflicts": []}
    for e in entities:
        if e["book"] is not None:
            stats["assigned"] += 1
            stats["by_book"][str(e["book"])] = stats["by_book"].get(
                str(e["book"]), 0) + 1
        else:
            stats["null"] += 1
            stats["by_reason"][e["why"]] = stats["by_reason"].get(e["why"], 0) + 1
        if e["group_book_conflict"]:
            stats["conflicts"].append(
                {"eid": e["eid"], "name": e["name"], "book": e["book"],
                 "groups": e["groups"]})
    return {"entities": entities, "stats": stats, "projects": projects}


def apply(db: pathlib.Path, gen_db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")

    before = state(con)
    if before["entity_has_novel"] or before["mention_has_novel"]:
        con.close()
        raise SystemExit("kg_entities / kg_mentions 已有 novel_id，拒绝重复加列")

    p = plan(con, gen_db)
    if p["stats"]["conflicts"]:
        con.close()
        raise SystemExit(
            "停止：实体层与 kg_item_group 的分槽矛盾（同名组与实体归到不同书）：\n"
            + json.dumps(p["stats"]["conflicts"], ensure_ascii=False, indent=2))

    backup = _backup(db, "kg-book", backup_dir)
    book_by_eid = {e["eid"]: e["book"] for e in p["entities"]}

    with con:
        con.execute(NEW_ENTITIES_DDL)
        con.execute(
            f"INSERT INTO kg_entities_new ({', '.join(ENTITY_COLS)}, novel_id)"
            f" SELECT {', '.join(ENTITY_COLS)}, NULL FROM {ENT}")
        for eid, book in book_by_eid.items():
            if book is not None:
                con.execute("UPDATE kg_entities_new SET novel_id=? WHERE id=?",
                            (book, eid))
        con.execute(f"DROP TABLE {ENT}")
        con.execute(f"ALTER TABLE kg_entities_new RENAME TO kg_entities")
        con.execute("CREATE UNIQUE INDEX uq_kg_entity ON kg_entities"
                    " (project_id, kind, canonical_name, COALESCE(novel_id, -1))")
        con.execute("CREATE INDEX ix_kg_entity_project_kind ON kg_entities"
                    " (project_id, kind)")
        con.execute("CREATE INDEX ix_kg_entity_project_canonical ON kg_entities"
                    " (project_id, canonical_name)")
        con.execute(f"ALTER TABLE {MENTION} ADD COLUMN novel_id INTEGER")
        con.execute(
            f"UPDATE {MENTION} SET novel_id = ("
            f"  SELECT e.novel_id FROM {ENT} e WHERE e.id = {MENTION}.entity_id)")

    after = state(con)
    # 结构验收：新键存在、旧列没了、行数不变
    idx = {r[1] for r in after["entity_schema"] if r[0] == "index"}
    assert "uq_kg_entity" in idx, idx
    assert after["entity_rows"] == before["entity_rows"]
    assert after["mention_rows"] == before["mention_rows"]
    assigned = con.execute(
        f"SELECT COUNT(*) FROM {ENT} WHERE novel_id IS NOT NULL").fetchone()[0]
    men_assigned = con.execute(
        f"SELECT COUNT(*) FROM {MENTION} WHERE novel_id IS NOT NULL").fetchone()[0]
    nulls = con.execute(
        f"SELECT COUNT(*) FROM {ENT} WHERE novel_id IS NULL").fetchone()[0]
    con.close()

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap = SNAP_DIR / f"kg_book_{_now_ts()}.json"
    snap.write_text(json.dumps({
        "ts": _now_ts(), "db": str(db), "generated_db": str(gen_db),
        "backup": str(backup), "plan": p, "before": before, "after": after,
        "filled": {"entities": assigned, "mentions": men_assigned,
                   "entity_null": nulls},
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return {"backup": str(backup), "snapshot": str(snap), "plan": p,
            "before": before, "after": after,
            "filled": {"entities": assigned, "mentions": men_assigned,
                       "entity_null": nulls}}


def rollback(db: pathlib.Path, backup_dir: pathlib.Path) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=OFF")
    before = state(con)
    if not before["entity_has_novel"] and not before["mention_has_novel"]:
        con.close()
        return {"rolled_back": False, "why": "两张表都没有 novel_id，无需回滚"}

    backup = _backup(db, "kg-book-rollback", backup_dir)
    with con:
        if before["entity_has_novel"]:
            con.execute(OLD_ENTITIES_DDL)
            con.execute(
                f"INSERT INTO kg_entities_old ({', '.join(ENTITY_COLS)})"
                f" SELECT {', '.join(ENTITY_COLS)} FROM {ENT}")
            con.execute(f"DROP TABLE {ENT}")
            con.execute("ALTER TABLE kg_entities_old RENAME TO kg_entities")
            con.execute("CREATE INDEX ix_kg_entity_project_kind ON kg_entities"
                        " (project_id, kind)")
            con.execute("CREATE INDEX ix_kg_entity_project_canonical ON kg_entities"
                        " (project_id, canonical_name)")
        if before["mention_has_novel"]:
            con.execute(f"ALTER TABLE {MENTION} DROP COLUMN novel_id")

    after = state(con)
    con.close()

    assert not after["entity_has_novel"] and not after["mention_has_novel"]
    assert after["entity_rows"] == before["entity_rows"]
    assert after["mention_rows"] == before["mention_rows"]
    # 值回得去：去掉 novel_id 后的行内容必须与加列前逐字节一致
    assert after["entity_md5"] == before["entity_md5"], "实体行内容回不去"
    assert after["mention_md5"] == before["mention_md5"], "提及行内容回不去"
    return {"rolled_back": True, "backup": str(backup),
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--db", default=str(APP), help="novel_dev.db 路径")
    ap.add_argument("--generated-db", default=str(GEN_DB))
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    args = ap.parse_args()

    db = pathlib.Path(args.db)
    gen_db = pathlib.Path(args.generated_db)
    backup_dir = pathlib.Path(args.backup_dir)

    if args.rollback:
        print(json.dumps(rollback(db, backup_dir), ensure_ascii=False,
                         indent=2, default=str))
        return 0

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = state(con)
    p = plan(con, gen_db)
    con.close()
    print(json.dumps({"db": str(db), "generated_db": str(gen_db),
                      "entity_has_novel": cur["entity_has_novel"],
                      "mention_has_novel": cur["mention_has_novel"],
                      "entity_rows": cur["entity_rows"],
                      "mention_rows": cur["mention_rows"],
                      "stats": p["stats"]}, ensure_ascii=False, indent=2))
    if not args.apply:
        print("\n归书计划（前 80 行）:")
        print(json.dumps(p["entities"][:80], ensure_ascii=False, indent=2))
        print("\n[dry-run] 未写库。加 --apply 执行。")
        return 0

    out = apply(db, gen_db, backup_dir)
    print("\n备份:", out["backup"])
    print("快照:", out["snapshot"])
    print("回填:", json.dumps(out["filled"], ensure_ascii=False))
    print("行数: 实体", out["before"]["entity_rows"], "→", out["after"]["entity_rows"],
          "；提及", out["before"]["mention_rows"], "→", out["after"]["mention_rows"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
