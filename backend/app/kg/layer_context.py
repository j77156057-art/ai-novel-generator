"""按本章出场人物取 canon + generated 的 1–2 跳邻域，渲染成有界 prompt 文本。

不是全表 dump：
- 先按 seeds 限定；
- 1 跳取种子相关边，只补 character/faction 且强度 >= 0.5；
- 2 跳只从核心人物扩展；
- 关系白名单 + 节点/边/事件/字符四重预算。
"""
from __future__ import annotations

import json
import pathlib
from collections import defaultdict

from app.kg import canon_source
from app.kg.generated_store import load_edges, load_events, load_nodes
from app.kg.layers import CANON, GENERATED

REL_WHITELIST = {
    "ally", "enemy", "subordinate", "superior", "mentor", "student",
    "family", "sibling", "parent", "child", "located_at", "member_of",
}

STATE_KEYS = (
    "identity", "identity_tags", "rank", "realm", "faction", "affiliation",
    "belongs_to", "status", "state", "location", "title", "position",
    "type", "note", "description", "current_goal", "held_by",
)


def read_canon(path: str | pathlib.Path) -> tuple[list[dict], list[dict]]:
    """canon 唯一读取点：来源由 KG_CANON_SOURCE 决定（legacy|cleaned）。

    具体读取、降级与 hash 记账都在 ``app.kg.canon_source``，这里不重复判断，
    避免抽取链路上散落「用哪份词表」的分支。
    """
    return canon_source.load(path)


def _node_index(nodes: list[dict]) -> tuple[dict, dict]:
    by_name: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for node in nodes:
        name = node["name"]
        if name not in by_name or (node["kind"] == "character"
                                   and by_name[name]["kind"] != "character"):
            by_name[name] = node
        for form in node.get("aliases") or []:
            if len(form) >= 2:
                alias.setdefault(form, name)
    return by_name, alias


def _attrs_summary(attrs: dict, limit: int = 4) -> str:
    parts: list[str] = []
    for key in STATE_KEYS:
        value = attrs.get(key)
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = "、".join(str(x) for x in value[:3])
        parts.append(f"{key}={value}")
        if len(parts) >= limit:
            break
    return "；".join(parts)[:160]


def _add(lines: list[str], candidate: list[str], budget: int) -> bool:
    if sum(len(x) + 1 for x in lines + candidate) > budget:
        return False
    lines.extend(candidate)
    return True


def build_context(seeds: list[str], canon_db: str | pathlib.Path,
                  generated_con=None, chapter: int = 0, *,
                  max_nodes: int = 24, max_edges: int = 20,
                  max_events: int = 8, budget_chars: int = 1800) -> dict:
    """取本章出场实体的 1–2 跳邻域，canon 优先、generated 只补已写情节。"""
    canon_nodes, canon_edges = read_canon(canon_db)
    by_name, alias = _node_index(canon_nodes)

    seed_names: list[str] = []
    for seed in seeds:
        canonical = alias.get(seed, seed)
        if canonical in by_name and canonical not in seed_names:
            seed_names.append(canonical)
    # 只去掉“确实是别人别名”的短名，不能用子串判断：
    # 「许七安」是「许七安的二叔」的子串，但绝不是同一个人。
    seed_names = [
        name for name in seed_names
        if not any(name in (by_name[other].get("aliases") or [])
                   for other in seed_names if other != name)
    ]

    adjacency: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for edge in canon_edges:
        if edge["rel"] not in REL_WHITELIST:
            continue
        adjacency[edge["from"]].append((edge["to"], edge))
        adjacency[edge["to"]].append((edge["from"], edge))

    selected: dict[str, dict] = {name: by_name[name] for name in seed_names}

    # 1 跳：种子相关边，只补人物/势力，强度 >= 0.5。
    for name in list(seed_names):
        for other, edge in adjacency.get(name, []):
            if len(selected) >= max_nodes:
                break
            if other in selected or other not in by_name:
                continue
            node = by_name[other]
            if node["kind"] in ("character", "faction") and edge["strength"] >= 0.5:
                selected[other] = node

    # 2 跳：只从核心人物（seed 且 character，最多 5 个）扩展，最多再补 6 个。
    core = [n for n in seed_names if by_name.get(n, {}).get("kind") == "character"][:5]
    added = 0
    for name in core:
        for other, edge in adjacency.get(name, []):
            if added >= 6 or len(selected) >= max_nodes + 6:
                break
            if other in selected or other not in by_name:
                continue
            node = by_name[other]
            if node["kind"] in ("character", "faction") and edge["strength"] >= 0.6:
                selected[other] = node
                added += 1

    # 最终只保留两端都在选中集合内的白名单边。
    edge_map: dict[tuple[str, str, str], dict] = {}
    for edge in canon_edges:
        if edge["rel"] not in REL_WHITELIST:
            continue
        if edge["from"] in selected and edge["to"] in selected:
            key = (edge["from"], edge["rel"], edge["to"])
            edge_map.setdefault(key, edge)
    selected_edges = sorted(edge_map.values(), key=lambda e: -e["strength"])[:max_edges]

    generated_events: list[dict] = []
    generated_nodes: list[dict] = []
    generated_edges: list[dict] = []
    if generated_con is not None:
        wanted = set(seed_names) | set(selected)
        generated_events = [
            ev for ev in load_events(generated_con, before_chapter=chapter or None,
                                     limit=40)
            if (ev.get("subject") in wanted or ev.get("object") in wanted)
        ][:max_events]
        # 生成层“已写过”的出场记录：即使人物在 canon 里存在，也要提示上一章写过。
        generated_nodes = [
            n for n in load_nodes(generated_con)
            if n["name"] in wanted
        ][:max_events]
        # 去掉被更长名字包含的短名（「七安」之于「许七安」），避免重复刷屏。
        gen_names = {n["name"] for n in generated_nodes}
        generated_nodes = [
            n for n in generated_nodes
            if not any(n["name"] != other and n["name"] in other for other in gen_names)
        ]
        generated_edges = [
            e for e in load_edges(generated_con)
            if e.get("source_chapter") and (chapter == 0 or e["source_chapter"] < chapter)
            and e.get("rel_type") in REL_WHITELIST
            and e["from_name"] in wanted and e["to_name"] in wanted
            # 只保留人物之间的生成层关系，避免把机构/物品的共现噪声带进下一章。
            and by_name.get(e["from_name"], {}).get("kind") == "character"
            and (by_name.get(e["to_name"], {}).get("kind") == "character"
                 or (e["rel_type"] == "located_at"
                     and by_name.get(e["to_name"], {}).get("kind") == "location"))
        ][:6]

    # —— 渲染：原著层在前，生成层在后，最后写优先级规则 ——
    lines: list[str] = ["【原著层·实体（身份/境界/势力以这里为准）】"]
    render_nodes = [
        n for n in selected.values()
        if not any(n["name"] in (other.get("aliases") or [])
                   for other in selected.values() if other["name"] != n["name"])
    ]
    for node in sorted(render_nodes,
                       key=lambda n: (n["kind"], -int(n.get("mentions") or 0)))[:16]:
        summary = _attrs_summary(node.get("attrs") or {})
        line = f"- {node['name']}（{node['kind']}）"
        if summary:
            line += f"：{summary}"
        _add(lines, [line], budget_chars)

    if selected_edges:
        render_names = {n["name"] for n in render_nodes}
        rel_lines = ["【原著层·关系】"]
        for edge in selected_edges:
            if edge["from"] not in render_names or edge["to"] not in render_names:
                continue
            rel_lines.append(
                f"- {edge['from']} —{edge['rel']}→ {edge['to']}"
                f"（强度 {edge['strength']:.2f}）"
            )
        _add(lines, rel_lines, budget_chars)

    if generated_events or generated_nodes or generated_edges:
        gen_lines = ["【生成层·已写情节（只保证连续性，不得覆盖原著设定）】"]
        for node in generated_nodes:
            gen_lines.append(
                f"- 第 {node['last_chapter']} 章已写过：{node['name']}"
                f"（{node['kind']}，出现 {node['mentions']} 次）"
            )
        for edge in generated_edges:
            gen_lines.append(
                f"- 第 {edge['source_chapter']} 章写过：{edge['from_name']}"
                f" —{edge['rel_type']}→ {edge['to_name']}"
            )
        for event in generated_events:
            gen_lines.append(
                f"- 第 {event.get('chapter')} 章：[{event['event_type']}] {event['summary']}"
            )
        _add(lines, gen_lines, budget_chars)

    lines.append(
        "规则：两层冲突时以原著层为准；生成层只说明「已经写过什么」，"
        "不得据此改写身份、境界、势力等原著设定。"
    )
    text = "\n".join(lines)

    return {
        "chapter": chapter,
        "seeds": seed_names,
        "layers": {
            "canon": len(selected),
            "generated": len(generated_events) + len(generated_nodes) + len(generated_edges),
        },
        "nodes": [
            {"name": n["name"], "kind": n["kind"], "source": CANON,
             "facts": _attrs_summary(n.get("attrs") or {})}
            for n in selected.values()
        ],
        "edges": [
            {"from": e["from"], "rel": e["rel"], "to": e["to"],
             "strength": e["strength"], "source": CANON}
            for e in selected_edges
        ],
        "events": [{**e, "source": GENERATED} for e in generated_events],
        "text": text,
        "chars": len(text),
        "budget_chars": budget_chars,
    }


def write_context(path: str | pathlib.Path, ctx: dict) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
