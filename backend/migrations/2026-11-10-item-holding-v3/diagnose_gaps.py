# -*- coding: utf-8 -*-
"""缺口分级取证：只读 v3 缓存与原著文本，不写库。

两件事：
1. 列出「出现 >=3 章、owns/belongs_to 覆盖 <=1 章」的物品（任务 E 的 57 个）；
2. 给定物品名，回原文找持有/转手/仅存放三类证据句。

用法：
    python diagnose_gaps.py --list
    python diagnose_gaps.py --items 神剑,金牌,酒壶
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
PROJ = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.kg.canon_source import fold_item_aliases  # noqa: E402

CACHE = PROJ / "experiments" / "2026-11-06-kg-audit" / "minimax-vol1" / "cache_minimax"
NOVEL = BACKEND / "novel_dev.db"

HOLD_RELS = ("owns", "belongs_to", "holds")
LOC_RELS = ("located_at",)

# 持有/转手动作 vs 仅存放
ACQUIRE = ("得到", "获得", "得手", "取走", "取来", "拿走", "取回", "夺过", "夺取",
           "收下", "收起", "揣进", "揣入", "接过", "握", "提着", "拔出", "拔出",
           "佩戴", "随身", "带在", "带上", "藏进")
GIVE = ("奉上", "献上", "呈上", "赐", "交给", "递给", "交到", "送给", "赠", "转交")
STORE = ("供奉", "供着", "摆在", "放在", "存放在", "锁在", "封在", "埋", "挂在", "置于")


def load_chapter(ch: int) -> dict:
    p = CACHE / f"ch{ch:03d}.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    main = d.get("main") or {}
    fold_item_aliases(main)
    return main


def build_index() -> tuple[dict, dict, dict]:
    """item -> 出现章 / 持有边章 / 别名集合。"""
    appear: dict[str, set] = {}
    owned: dict[str, set] = {}
    local: dict[str, set] = {}
    alias: dict[str, set] = {}
    for ch in range(1, 230):
        main = load_chapter(ch)
        if not main:
            continue
        items = {e.get("canonical_name") for e in main.get("entities", [])
                 if e.get("kind") == "item" and e.get("canonical_name")}
        for e in main.get("entities", []):
            if e.get("kind") == "item" and e.get("canonical_name"):
                alias.setdefault(e["canonical_name"], set()).update(
                    [e["canonical_name"], *(e.get("aliases") or [])])
        for r in main.get("relations", []):
            a, b, rel = r.get("from"), r.get("to"), r.get("rel_type")
            if a in items and b not in items:
                item = a
            elif b in items:
                item = b
            else:
                continue
            appear.setdefault(item, set()).add(ch)
            if rel in HOLD_RELS:
                owned.setdefault(item, set()).add(ch)
            elif rel in LOC_RELS:
                local.setdefault(item, set()).add(ch)
        for item in items:
            appear.setdefault(item, set()).add(ch)
    return appear, owned, local, alias


def chapter_text(con, ch: int) -> str:
    rows = con.execute(
        "SELECT text FROM novel_chunks WHERE novel_id=5 AND chapter_no=? ORDER BY idx",
        (ch,)).fetchall()
    return "\n".join(r[0] for r in rows)


def sentences(text: str) -> list[str]:
    parts = re.split(r"[。！？\n]", text)
    return [p.strip() for p in parts if p.strip()]


def scan(con, item: str, names: list[str], chapters: list[int]) -> list[dict]:
    out = []
    for ch in chapters:
        text = chapter_text(con, ch)
        if not text:
            continue
        for sent in sentences(text):
            if not any(n and n in sent for n in names):
                continue
            if any(v in sent for v in ACQUIRE):
                kind = "acquire"
            elif any(v in sent for v in GIVE):
                kind = "give"
            elif any(v in sent for v in STORE):
                kind = "store"
            else:
                kind = "mention"
            out.append({"ch": ch, "kind": kind, "s": sent[:90]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--items", default="")
    ap.add_argument("--skip-mention", action="store_true")
    ap.add_argument("--max-per-item", type=int, default=40)
    ap.add_argument("--min-ch", type=int, default=3)
    ap.add_argument("--max-own-ch", type=int, default=1)
    args = ap.parse_args()

    appear, owned, local, alias = build_index()
    gaps = []
    for item, chs in appear.items():
        if len(chs) >= args.min_ch and len(owned.get(item, ())) <= args.max_own_ch:
            gaps.append({"item": item, "chapters": len(chs),
                         "owned_ch": sorted(owned.get(item, ())),
                         "located_ch": sorted(local.get(item, ())),
                         "ch_list": sorted(chs)})
    gaps.sort(key=lambda g: (-g["chapters"], g["item"]))
    print(f"出现>={args.min_ch}章、持有覆盖<={args.max_own_ch}章的物品: {len(gaps)}")
    for g in gaps:
        print(f"  {g['item']:<12} 章数={g['chapters']:<3} 持有章={g['owned_ch']} "
              f"located章={g['located_ch']} 采证章={g['ch_list'][:6]}")

    if args.items:
        con = sqlite3.connect(f"file:{NOVEL}?mode=ro", uri=True)
        for item in [x.strip() for x in args.items.split(",") if x.strip()]:
            names = sorted((n for n in alias.get(item, {item}) if len(n) >= 2),
                           key=len, reverse=True) or [item]
            chapters = sorted(appear.get(item, set()))
            print(f"\n===== {item} 别名={names} 章={chapters}")
            rows = scan(con, item, names, chapters)
            if args.skip_mention:
                rows = [r for r in rows if r["kind"] != "mention"]
            for row in rows[:args.max_per_item]:
                print(f"  ch{row['ch']:03d} [{row['kind']}] {row['s']}")
        con.close()


if __name__ == "__main__":
    main()
