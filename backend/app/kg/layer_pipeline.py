# -*- coding: utf-8 -*-
"""KG 生成层编排：一章文本进来，依次做「机械抽取 → 落生成层 → 冲突检测 → 落冲突表」。

边界：
- 只走机械路，不调 LLM、不写原著层、不碰 `gen_llm_*` 表；
- 同一章重跑前先 `reset_chapter`，所以计数可复算、不会越跑越多；
- 冲突只记录并带 `decision`（rewrite / mark / reported），重写动作由调用方决定。

用法（离线）：
    con = generated_store.connect("data/generated.db")
    generated_store.ensure_schema(con)
    ingest_chapter(con, "kg_vol1_p1fix.db", text, chapter=231)
    ctx = prepare_chapter(con, "kg_vol1_p1fix.db", chapter=232, out="data/ctx_232.json")
"""
from __future__ import annotations

import pathlib

from app.kg import generated_store as store
from app.kg.conflict_scan import scan_text
from app.kg.generated_extract import extract_from_text
from app.kg.layer_context import build_context, read_canon, write_context


def ingest_chapter(con, canon_db: str | pathlib.Path | None, text: str, chapter: int,
                   *, reset: bool = True) -> dict:
    """把一章生成文本落进生成层，并返回本轮抽取量、冲突与按决定分组的计数。

    canon_db 为 None 时按「无原著层」跑：只做抽取与落库，冲突检测拿到空的
    canon 事实表（不会误报）。
    """
    if canon_db is None:
        canon_nodes, canon_edges = [], []
    else:
        canon_nodes, canon_edges = read_canon(canon_db)
    if reset:
        store.reset_chapter(con, chapter)

    ext = extract_from_text(text, chapter, canon_nodes, canon_edges)
    for node in ext["nodes"]:
        store.upsert_node(con, node["name"], node["kind"], node.get("attrs"),
                          chapter, int(node.get("mentions") or 1))
    for edge in ext["edges"]:
        store.upsert_edge(
            con, edge["from"], edge["to"], edge["rel"],
            float(edge.get("strength") or 0.5), chapter,
            evidence=edge.get("evidence") or "",
            direction_basis=edge.get("direction_basis") or "",
            trigger=edge.get("trigger") or "",
            confidence=float(edge.get("confidence") or 0.5),
        )
    for event in ext["events"]:
        store.add_event(
            con, chapter, event["event_type"], event["summary"],
            subject=event.get("subject"), object_=event.get("object"),
            location=event.get("location"), evidence=event.get("evidence") or "",
            subject_status=event.get("subject_status") or "resolved",
            raw_subject=event.get("raw_subject"),
        )
    for cand in ext["candidates"]:
        store.add_candidate(con, cand["from"], cand["to"], cand["rel"], chapter,
                            cand.get("reason") or "", cand.get("evidence") or "")
    for rej in ext["rejected_subjects"]:
        store.add_rejected_subject(con, chapter, rej["raw_subject"],
                                   rej["reason"], rej.get("evidence") or "")

    found = scan_text(text, chapter, canon_nodes, canon_edges)
    for item in found:
        store.add_conflict(
            con, chapter, item["conflict_type"], item["subject"],
            item.get("canon_value") or "", item.get("generated_value") or "",
            item.get("evidence") or "", item.get("decision") or "mark",
            item.get("note") or "",
        )
    con.commit()

    by_decision: dict[str, int] = {}
    for item in found:
        decision = item.get("decision") or "mark"
        by_decision[decision] = by_decision.get(decision, 0) + 1

    return {
        "chapter": chapter,
        "nodes": len(ext["nodes"]),
        "edges": len(ext["edges"]),
        "events": len(ext["events"]),
        "candidates": len(ext["candidates"]),
        "rejected_subjects": len(ext["rejected_subjects"]),
        "conflicts": found,
        "by_decision": by_decision,
    }


def prepare_chapter(con, canon_db: str | pathlib.Path, chapter: int, *,
                    seeds: list[str] | None = None,
                    out: str | pathlib.Path | None = None, **ctx_kw) -> dict:
    """为下一章准备注入上下文：默认种子取上一章生成层里最后出场的人名。"""
    if seeds is None:
        seeds = store.seeds_for_chapter(con, chapter - 1)
    ctx = build_context(list(seeds), canon_db, con, chapter, **ctx_kw)
    ctx["seeds"] = list(seeds)
    if out is not None:
        write_context(out, ctx)
    return ctx
