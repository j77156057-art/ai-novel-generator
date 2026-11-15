# -*- coding: utf-8 -*-
"""为已生成段落回填 ``kg_mentions``（零 LLM，库里已有物品名做别名匹配）。

背景：``kg_mentions`` 全库 0 行，生成侧 ``_active_item_entries`` 永远取不到物品，
持有块在真实生成里是空的。本脚本只补**已经生成过的段**（project 5 的
segment_no=1、2），不改任何其他表。

匹配口径：``canon_source.text_mention``（最长优先 + 材质字边界 + 注入式守卫），
写法 = ``kg_entities`` 中 item 的规范名 **与 aliases**。守卫集合就是全部物品
写法，所以「地书」不会被「地书碎片」算成命中、「铜剑」不会被「黄铜剑」算中。

**预期命中 0**：这 2 段是模板文本（「小杨站在废弃的工厂前…」），里面没有物品名。
0 就是 0——脚本不放宽匹配、不改阈值、不做模糊匹配。

幂等：同一 ``(project_id, entity_id, segment_no)`` 已有行就跳过，重复跑不新增。

用法::

    python backend/migrations/2026-11-14-item-mention-backfill-v1/backfill_mentions.py --dry-run
    python backend/migrations/2026-11-14-item-mention-backfill-v1/backfill_mentions.py \
        --db <副本.db> --no-backup          # 先在副本上跑

边界：只 INSERT ``kg_mentions``；不动 ``kg_entities`` / ``generations`` / 持有表。
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

from app.kg import canon_source  # noqa: E402

APP_DB = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"
DEFAULT_PROJECT = 5
DEFAULT_SEGMENTS = (1, 2)


def _backup(path: pathlib.Path) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-pre-mention-backfill-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _parse_aliases(raw) -> list[str]:
    """``kg_entities.aliases`` 是 JSON 文本；容错解析成字符串列表。"""
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _item_forms(con: sqlite3.Connection, project_id: int) -> list[tuple[int, list[str]]]:
    """库里 item 实体的匹配写法：规范名在前，aliases 在后（去重）。"""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, canonical_name, aliases FROM kg_entities "
        "WHERE project_id=? AND kind='item' ORDER BY id",
        (project_id,),
    ).fetchall()
    out: list[tuple[int, list[str]]] = []
    for row in rows:
        forms: list[str] = []
        for raw in [row["canonical_name"], *_parse_aliases(row["aliases"])]:
            form = (raw or "").strip() if isinstance(raw, str) else ""
            if form and form not in forms:
                forms.append(form)
        if forms:
            out.append((row["id"], forms))
    return out


def _window_around(text: str, form: str, width: int = 60) -> str:
    """提及处原文片段（与 batch_worker 同口径：前后各 30 字）。"""
    i = text.find(form)
    if i < 0:
        return text[:width]
    half = width // 2
    return text[max(0, i - half): min(len(text), i + len(form) + half)]


def run(app_db: pathlib.Path, project_id: int, segments: tuple[int, ...],
        dry_run: bool) -> dict:
    con = sqlite3.connect(app_db)
    con.row_factory = sqlite3.Row
    try:
        items = _item_forms(con, project_id)
        # 守卫 = 全部物品写法（含更长写法），由调用方注入，canon_source 自己不读库
        guards = frozenset(form for _, forms in items for form in forms)
        placeholders = ",".join("?" for _ in segments)
        gens = con.execute(
            f"SELECT id, segment_no, text FROM generations "
            f"WHERE project_id=? AND segment_no IN ({placeholders}) ORDER BY segment_no",
            (project_id, *segments),
        ).fetchall()

        report = {
            "app_db": str(app_db),
            "project_id": project_id,
            "requested_segments": list(segments),
            "segments_found": [g["segment_no"] for g in gens],
            "item_entities": len(items),
            "guards": len(guards),
            "per_segment": [],
            "inserted": 0,
            "skipped_existing": 0,
            "rows": [],
        }
        for gen in gens:
            text = gen["text"] or ""
            hits: list[dict] = []
            for ent_id, forms in items:
                matched = None
                for form in forms:
                    if canon_source.text_mention(text, form, guards=guards):
                        matched = form
                        break
                if matched:
                    hits.append({"entity_id": ent_id, "surface": matched})
            report["per_segment"].append({
                "segment_no": gen["segment_no"],
                "generation_id": gen["id"],
                "chars": len(text),
                "preview": text[:60],
                "hits": hits,
            })
            for hit in hits:
                exists = con.execute(
                    "SELECT id FROM kg_mentions WHERE project_id=? AND entity_id=? "
                    "AND segment_no=?",
                    (project_id, hit["entity_id"], gen["segment_no"]),
                ).fetchone()
                if exists is not None:
                    report["skipped_existing"] += 1
                    continue
                row = {
                    "project_id": project_id,
                    "entity_id": hit["entity_id"],
                    "generation_id": gen["id"],
                    "segment_no": gen["segment_no"],
                    "context_window": _window_around(text, hit["surface"]),
                    "mention_kind": "alias",
                    "surface": hit["surface"],
                }
                report["rows"].append(row)
                if not dry_run:
                    con.execute(
                        "INSERT INTO kg_mentions (project_id, entity_id, generation_id,"
                        " segment_no, context_window, mention_kind)"
                        " VALUES (?,?,?,?,?,?)",
                        (row["project_id"], row["entity_id"], row["generation_id"],
                         row["segment_no"], row["context_window"], row["mention_kind"]),
                    )
                report["inserted"] += 1
        if not dry_run and report["inserted"]:
            con.commit()
        return report
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(APP_DB))
    ap.add_argument("--project", type=int, default=DEFAULT_PROJECT)
    ap.add_argument("--segment", type=int, action="append", default=None,
                    help="可重复；缺省 1、2")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()
    app_path = pathlib.Path(args.db)
    segments = tuple(args.segment) if args.segment else DEFAULT_SEGMENTS
    backup = None
    if not args.dry_run and not args.no_backup:
        backup = _backup(app_path)
    canon_source.reset_cache()
    report = run(app_path, args.project, segments, args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "backup": str(backup) if backup else None,
                      "report": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
