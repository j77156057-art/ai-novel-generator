# -*- coding: utf-8 -*-
"""诊断「神剑」类抽取缺口：逐章抽取对比 + 别名折叠前后护栏。

只读 v3 缓存；`--reextract` 会用同一模型同一参数重抽指定章（调 run_route32k.py）。
产出只打印，不写库。

用法：
    python diagnose_sword_gap.py                  # 只看旧缓存 + 折叠对照
    python diagnose_sword_gap.py --reextract      # 先重抽 9 章，再对照
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
PROJ = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.kg.canon_source import ITEM_ALIASES, fold_item_aliases  # noqa: E402

OLD_CACHE = PROJ / "experiments" / "2026-11-06-kg-audit" / "minimax-vol1" / "cache_minimax"
NEW_CACHE = PROJ / "experiments" / "2026-11-06-kg-audit" / "minimax-vol1" / "cache_swordfix" / "cache_minimax"
RUNNER = PROJ / "experiments" / "2026-11-06-kg-audit" / "model-compare-30" / "max32k" / "run_route32k.py"
CHAPTERS = [95, 97, 98, 99, 106, 107, 108, 122, 165]


def load_main(cache: pathlib.Path, ch: int) -> dict | None:
    p = cache / f"ch{ch:03d}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get("main") or {}


def hold_rows(main: dict, ch: int) -> list[dict]:
    """把关系里端点为 item 的持有边翻成「持有者 → 物品」。"""
    item_names = {e.get("canonical_name") for e in main.get("entities", [])
                  if e.get("kind") == "item"}
    out = []
    for r in main.get("relations", []):
        rel = r.get("rel_type")
        a, b = r.get("from"), r.get("to")
        if rel in ("owns", "holds"):
            if a in item_names and b not in item_names:
                out.append({"ch": ch, "item": a, "holder": b, "rel": rel,
                            "dir": "reversed", "strength": r.get("strength")})
            elif b in item_names:
                out.append({"ch": ch, "item": b, "holder": a, "rel": rel,
                            "dir": "ok", "strength": r.get("strength")})
        elif rel == "belongs_to":
            if a in item_names:
                out.append({"ch": ch, "item": a, "holder": b, "rel": rel,
                            "dir": "ok", "strength": r.get("strength")})
    return out


def item_fragments(main: dict) -> set[str]:
    return {e.get("canonical_name") for e in main.get("entities", [])
            if e.get("kind") == "item" and any(k in (e.get("canonical_name") or "")
                                               for k in ("神剑", "宝剑", "佩剑"))}


def summarize(cache: pathlib.Path, label: str, fold: bool) -> dict:
    print(f"\n===== {label}（{'折叠后' if fold else '原始'}）")
    frag, rows, missing = set(), [], []
    for ch in CHAPTERS:
        main = load_main(cache, ch)
        if main is None:
            missing.append(ch)
            continue
        main = copy.deepcopy(main)
        if fold:
            fold_item_aliases(main)
        frag |= item_fragments(main)
        hr = hold_rows(main, ch)
        rows.extend(hr)
        swords = [h for h in hr if "神剑" in h["item"]]
        rels = [r for r in main.get("relations", [])
                if "神剑" in json.dumps(r, ensure_ascii=False)]
        print(f"  ch{ch:03d}: 神剑持有边={swords or '—'}")
        print(f"          全部神剑关系={[(r.get('from'), r.get('rel_type'), r.get('to'), r.get('strength')) for r in rels]}")
    print(f"  --- 物品名碎片({len(frag)}): {sorted(frag)}")
    print(f"  --- 神剑持有边合计: {[r for r in rows if '神剑' in r['item']]}")
    if missing:
        print(f"  --- 缺缓存章: {missing}")
    return {"fragments": sorted(frag), "rows": [r for r in rows if "神剑" in r["item"]]}


def reextract(chapters: list[int]) -> None:
    out_dir = NEW_CACHE.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    env0 = dict(os.environ)
    for ch in chapters:
        env = dict(env0)
        env.update({"ROUTE": "minimax", "OUT_DIR": str(out_dir), "MAX_CH": str(ch),
                    "CH_FROM": str(ch), "CH_TO": str(ch), "MAX_TOKENS": "32768",
                    "TAG": f"_swordfix{ch}", "GROUP": "2", "RETRIES": "1",
                    "MINIMAX_TIMEOUT": "150"})
        print(f"[reextract] ch{ch} ...", flush=True)
        p = subprocess.run([sys.executable, str(RUNNER)], env=env,
                           cwd=str(PROJ), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = (p.stdout or "").strip().splitlines()[-3:]
        print("   ", p.returncode, " | ".join(tail)[:400], flush=True)
        if p.returncode != 0:
            print("    stderr:", (p.stderr or "")[-500:], flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reextract", action="store_true")
    ap.add_argument("--chapters", default="",
                    help="只重抽这些章，逗号分隔；默认 9 章全跑")
    args = ap.parse_args()
    chapters = ([int(x) for x in args.chapters.split(",") if x.strip()]
                if args.chapters else CHAPTERS)
    print("ITEM_ALIASES:", json.dumps(ITEM_ALIASES, ensure_ascii=False))
    if args.reextract:
        reextract(chapters)
    summarize(OLD_CACHE, "旧缓存", fold=False)
    summarize(OLD_CACHE, "旧缓存 + 别名折叠", fold=True)
    if NEW_CACHE.exists():
        summarize(NEW_CACHE, "重抽 + 别名折叠", fold=True)


if __name__ == "__main__":
    main()
