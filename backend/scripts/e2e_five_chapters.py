# -*- coding: utf-8 -*-
"""五章端到端回归：用**真实生成接口**验证平台能力，而不是手工跑一篇小说。

它做的事等价于用户在前端连续点 N 次「生成」，然后按平台自己落库的
`quality_json` 做断言：

- 每章必须非空、闸门通过（可用 `--allow-gate-fail` 放宽）
- 对白字占比不得低于本章硬下限
- 段数不得低于本章下限
- 归属率必须较校正前提升，或达到目标的 90%
- 复述检测不得命中（命中则必须已成功改写）
- 同一本书的引号口径一致（不得“”与「」混用）

用法（需后端已启动、且配置了真实 writer 模型）：

    python backend/scripts/e2e_five_chapters.py --project-id 11 --reset

`--reset` 会删除该项目全部正文与生成期图谱、重置摘要/时间线后重新规划；
**不要在正式项目上使用**。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import requests

DEFAULT_BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_DB = os.environ.get(
    "E2E_DB",
    str(Path(__file__).resolve().parents[1] / "novel_dev.db"),
)


def _reset_project(db_path: str, project_id: int) -> dict:
    """清空正文与生成期 KG，保留项目设定/参考书/章纲。"""
    c = sqlite3.connect(db_path)
    node_ids = [r[0] for r in c.execute(
        "select id from kg_nodes where project_id=?", (project_id,)).fetchall()]
    counts: dict[str, int] = {}
    if node_ids:
        qs = ",".join("?" * len(node_ids))
        counts["kg_edges"] = c.execute(
            f"delete from kg_edges where from_id in ({qs}) or to_id in ({qs})",
            node_ids + node_ids).rowcount
    for table in ("facts", "kg_events", "kg_mentions", "kg_character_state",
                  "kg_relations", "kg_relation_evolution"):
        counts[table] = c.execute(
            f"delete from {table} where project_id=?", (project_id,)).rowcount
    counts["kg_nodes"] = c.execute(
        "delete from kg_nodes where project_id=?", (project_id,)).rowcount
    counts["generations"] = c.execute(
        "delete from generations where project_id=?", (project_id,)).rowcount
    c.execute(
        "update projects set summary=NULL, summary_tree=NULL, plot_timeline=NULL, "
        "status='planned' where id=?", (project_id,))
    c.commit()
    c.close()
    return counts


def _get(base: str, path: str) -> dict:
    r = requests.get(f"{base}{path}", timeout=60)
    r.raise_for_status()
    return r.json()


def _post(base: str, path: str, body: dict, timeout: int) -> dict:
    r = requests.post(f"{base}{path}", json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _generate_once(base: str, project_id: int, max_chars: int | None):
    payload = {"project_id": project_id}
    if max_chars:
        payload["max_chars"] = max_chars
    t0 = time.time()
    with requests.post(f"{base}/api/generate-stream", json=payload,
                       stream=True, timeout=(30, 1800)) as r:
        if r.status_code != 200:
            return None, f"http {r.status_code}: {r.text[:200]}", 0.0
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                ev = json.loads(raw[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "error":
                return None, str(ev.get("message"))[:300], time.time() - t0
            if ev.get("type") == "done":
                return ev, None, time.time() - t0
    return None, "no done event", time.time() - t0


def _rate(v) -> float | None:
    if isinstance(v, dict):
        v = v.get("rate")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def check_chapter(item: dict, *, min_attr: float,
                  allow_gate_fail: bool) -> list[str]:
    """对单章做平台级断言，返回失败原因列表（空 = 通过）。"""
    fails: list[str] = []
    text = item.get("text") or ""
    q = item.get("quality") or {}
    if not text.strip():
        return ["正文为空"]

    gate = q.get("style_gate") or {}
    if not gate.get("passed") and not allow_gate_fail:
        reasons = "；".join(gate.get("reasons") or [])
        fails.append(f"闸门未通过：{reasons or gate.get('violations')}")

    metrics = gate.get("metrics") or {}
    floor_info = q.get("dialogue_floor") or {}
    floor = _rate(floor_info.get("floor"))
    dialogue = metrics.get("对白字占比")
    if floor is not None and dialogue is not None and float(dialogue) < floor:
        fails.append(f"对白占比 {dialogue:.1%} 低于下限 {floor:.1%}")

    para_info = q.get("paragraph_floor") or {}
    pmin = para_info.get("min")
    if pmin and metrics.get("段数") is not None and metrics["段数"] < int(pmin):
        fails.append(f"段数 {metrics['段数']} 低于下限 {pmin}")

    attr = (q.get("style_corrections") or {}).get("attr_info") or {}
    before = _rate(attr.get("before"))
    after = _rate(attr.get("after"))
    target = _rate(attr.get("target"))
    if before is not None:
        if after is None:
            fails.append(f"归属率未校正（仍为 {before:.1%}，目标 "
                         f"{(target or 0):.1%}）")
        else:
            ok = after >= before + 0.05
            if target is not None:
                ok = ok or after >= target * 0.9 or after >= min_attr
            if not ok:
                if target is not None:
                    fails.append(
                        f"归属率未达标：{before:.1%} → {after:.1%}"
                        f"（目标 {target:.1%}）")
                else:
                    fails.append(
                        f"归属率未提升：{before:.1%} → {after:.1%}")

    replay = q.get("replay_guard") or {}
    if (replay.get("check") or {}).get("replay"):
        if not (replay.get("rewrite") or {}).get("applied"):
            fails.append("检测到复述，但定向改写未采纳")

    if "“" in text and "「" in text:
        fails.append("引号口径混用（同时出现 “” 与 「」）")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="五章端到端回归（真实生成接口）")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--project-id", type=int, required=True)
    ap.add_argument("--chapters", type=int, default=5)
    ap.add_argument("--reset", action="store_true",
                    help="先清空该项目正文/生成期图谱并重规划时间线")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--min-attr", type=float, default=0.20,
                    help="归属率的最低可接受值（默认 20%%）")
    ap.add_argument("--allow-gate-fail", action="store_true")
    ap.add_argument("--out", default="", help="把回归报告写成 JSON")
    args = ap.parse_args(argv)

    if args.reset:
        if not Path(args.db).exists():
            print(f"数据库不存在：{args.db}", file=sys.stderr)
            return 2
        print("reset:", _reset_project(args.db, args.project_id))
        _post(args.base_url, f"/api/projects/{args.project_id}/timeline/plan", {}, 900)

    fail_streak = 0
    max_steps = max(args.chapters * 4, args.chapters)
    for step in range(1, max_steps + 1):
        gens = _get(args.base_url, f"/api/generations/{args.project_id}")
        good = [g for g in gens if (g.get("text") or "").strip()]
        if len(good) >= args.chapters:
            break
        max_chars = 3200 if fail_streak >= 2 else None
        print(f"--- step {step} good={len(good)}/{args.chapters} max_chars={max_chars}")
        done, err, dt = _generate_once(
            args.base_url, args.project_id, max_chars)
        if done and (done.get("chars") or 0) > 0:
            fail_streak = 0
            print(f"  done seg={done.get('segment_no')} chars={done.get('chars')} "
                  f"{dt/60:.1f} min")
        else:
            fail_streak += 1
            print(f"  failed: {err or 'empty content'} ({dt/60:.1f} min)")
            time.sleep(5)
    else:
        print("未能生成足够章节", file=sys.stderr)
        return 1

    gens = _get(args.base_url, f"/api/generations/{args.project_id}")
    rows = []
    failed = 0
    for g in gens:
        if not (g.get("text") or "").strip():
            continue
        fails = check_chapter(g, min_attr=args.min_attr,
                              allow_gate_fail=args.allow_gate_fail)
        rows.append({
            "segment_no": g.get("segment_no"),
            "chapter": g.get("chapter_range"),
            "chars": len(g.get("text") or ""),
            "passed": not fails,
            "fails": fails,
        })
        if fails:
            failed += 1

    report = {
        "project_id": args.project_id,
        "chapters": len(rows),
        "failed": failed,
        "rows": rows,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
