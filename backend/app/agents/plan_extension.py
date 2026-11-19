"""扩展规划（B 子系统移植，适配 A 的段模式）。

背景：规划用尽后生成被拒是对的——没有计划就没有可核对的权威。但只关门不出路，
作者想继续写时无路可走。这条路径在**保存权威允许的范围内**追加剧情点，
并预写段契约，让生成能在计划内继续。

三条纪律（与 B 侧同源）：

1. 权威只读：不改 premise、不改已冻结契约、不改已写正文；
2. 只追加：节点与契约都往后加，段号不复用，历史不动；
3. 越界即失败：清单外物件/人物/地点 → 回灌让模型自改，仍犯则拒绝；
   抽不出权威或时间余量 → 显式说清差在哪，不替作者编剧情。

与 B 的差异（A 是章级产品）：

- **只支持段模式**。章级项目的「下一个剧情点」由章纲决定，追加节点却没有章纲
  条目，`node_for_chapter` 永远选不到它；这种项目应先用章纲端点补第 N 章，
  再追加节点。这里返回 `chapter_mode_requires_outline` 而不是静默写一个死节点。
- 节点 schema 用 A 的 `PlotNode`（title/emotion/entry/exit/beats），
  不是 B 的 11 字段因果节点；因此没有「时间窗」判据，只做权威清单核对。
"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from sqlalchemy import select

from app.generation.planning import model_json

#: 一次扩展最多追加几个剧情点。
MAX_EXTEND_COUNT = 3

#: 提案次数上限（含一次「回灌自改」）。
MAX_PROPOSE_ATTEMPTS = 2

_EMOTIONS = ("爽", "虐", "甜", "怒", "悬疑")

_SYSTEM = (
    "你是小说剧情规划编辑。任务：在**保存权威允许的范围内**续写剧情点，"
    "供后续分段写作按点推进。\n"
    "严格只输出 JSON {\"nodes\":[...]}。每个节点必须有以下字段："
    "title（短名）, emotion（爽|虐|甜|怒|悬疑）, "
    "entry_condition（进入该点时人物/场面的状态）, "
    "exit_condition（写到什么事件算完成，具体可判定）, beats（2–4 个短句节拍）。\n"
    "要求：\n"
    "1. 恰好输出 {count} 个节点，按故事顺序。\n"
    "2. 不得重复已写剧情点做过的事；不写故事结局。\n"
    "3. **不得引入保存权威之外的物件、人物、地点**——要用的东西只能用权威里已有的。\n"
    "4. 不确定的写 unknown，不要编造作者没写过的设定。"
)


def _written_segments(db, project) -> int:
    from app.models import Generation

    rows = db.scalars(
        select(Generation.id).where(Generation.project_id == project.id)
    ).all()
    return len(rows)


def _chapter_based(db, project) -> bool:
    """项目是否已经按「第N章」落库（决定段模式/章模式）。"""
    from app.coherence import chapter_index
    from app.models import Generation

    labels = db.scalars(
        select(Generation.chapter_range).where(
            Generation.project_id == project.id,
            Generation.chapter_range.is_not(None),
        )
    ).all()
    return any(chapter_index(label) for label in labels)


def _authority_text(project) -> str | None:
    premise = str(project.premise or "").strip()
    return premise or None


def _parse_nodes(text: str, count: int) -> list[dict]:
    payload = json.loads(text)
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list) or len(nodes) != count:
        raise ValueError("invalid node count")
    out: list[dict] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("invalid node")
        title = str(node.get("title") or "").strip()
        entry = str(node.get("entry_condition") or "").strip()
        exit_ = str(node.get("exit_condition") or "").strip()
        if not title or not entry or not exit_:
            raise ValueError("missing node fields")
        emotion = str(node.get("emotion") or "").strip()
        beats = node.get("beats")
        if not isinstance(beats, list):
            beats = []
        out.append({
            "title": title,
            "emotion": emotion if emotion in _EMOTIONS else "爽",
            "entry_condition": entry,
            "exit_condition": exit_,
            "beats": [str(x).strip() for x in beats if str(x).strip()][:4],
        })
    return out


def _propose(provider, authority: str, nodes: dict, count: int,
             feedback: str | None) -> tuple[list[dict], list[dict]]:
    known = [
        f"- 「{node.get('title')}」"
        for node in (nodes or {}).values()
        if isinstance(node, dict) and node.get("title")
    ]
    user = (
        f"【保存权威】（唯一依据，不得超出）\n{authority}\n\n"
        f"【已经用过的剧情点标题】（不许重复）\n"
        + ("\n".join(known) or "（无）")
        + f"\n\n输出 {count} 个节点的 JSON。"
    )
    if feedback:
        user += "\n\n—— 纠正上一次不合格的输出 ——\n" + feedback
    return model_json(
        provider, _SYSTEM.replace("{count}", str(count)), user,
        lambda text: _parse_nodes(text, count),
        attempts=MAX_PROPOSE_ATTEMPTS,
    )


def _node_text(node: dict) -> str:
    parts = [node.get("title"), node.get("entry_condition"),
             node.get("exit_condition")]
    parts += list(node.get("beats") or [])
    return "\n".join(str(p) for p in parts if p)


def _split_for_protocol(text: str, limit: int = 600) -> list[tuple[int, int, str]]:
    """把节点文本切成协议能吃的段落（每段 ≤ limit 字，保留偏移）。"""
    pieces: list[tuple[int, int, str]] = []
    offset = 0
    for line in (text or "").split("\n"):
        if not line.strip():
            offset += len(line) + 1
            continue
        start = offset
        while len(line) > limit:
            cut = max(line.rfind("。", 0, limit), line.rfind("，", 0, limit))
            cut = limit if cut <= 0 else cut + 1
            pieces.append((start, start + cut, line[:cut]))
            start += cut
            line = line[cut:]
        if line.strip():
            pieces.append((start, start + len(line), line))
        offset += len(line) + 1
    return pieces


def _out_of_authority(provider, authority: str, node: dict) -> list[dict]:
    """用与正文同一套封闭清单协议核对新节点，返回越界项。"""
    from app.agents.closed_inventory_v2 import inspect_inventory

    text = _node_text(node)
    sources = {
        "C1S001": {"source_id": "contract:1", "path": "/authority/premise",
                   "quote": authority, "start": 0, "end": len(authority)},
    }
    for index, (start, end, quote) in enumerate(_split_for_protocol(text), 1):
        sources[f"G900P{index:03d}"] = {
            "source_id": "generation:900", "path": "/body",
            "quote": quote, "start": start, "end": end,
        }
    observation = {"data": {
        "sources": sources,
        "segments": [{"segment_no": 1, "generation_id": 900,
                      "contract": {"authority": {"premise": authority}},
                      "text_hash": "", "unknown": None}],
    }}
    report = {"unresolved": [], "model_participated": False}
    inspect_inventory(provider, observation, report,
                      kinds=["object", "person", "place"], protocol=5)
    violations = []
    for candidate in (report["inventory"].get("candidates") or []):
        verdict = candidate.get("verdict") or {}
        if verdict.get("status") != "violated":
            continue
        mention = candidate.get("mention") or {}
        violations.append({
            "entity": mention.get("canonical"),
            "kind": mention.get("kind"),
            "reason": str(verdict.get("reason"))[:200],
        })
    return violations


def _next_node_id(nodes: dict) -> str:
    used = set()
    for nid in nodes:
        match = re.fullmatch(r"n(\d+)", str(nid))
        if match:
            used.add(int(match.group(1)))
    return f"n{max(used) + 1 if used else 1}"


def _write_extension(db, project, accepted, authority, timeline, nodes, order,
                     written):
    """只追加：新节点进 order，预写 next_segment 起的契约，历史不动。"""
    new_nodes = dict(nodes)
    new_order = list(order)
    contracts = dict((timeline.get("planning") or {}).get("segment_contracts") or {})
    added_nodes, added_contracts = [], []

    for offset, node in enumerate(accepted):
        nid = _next_node_id(new_nodes)
        new_nodes[nid] = {
            "id": nid,
            "kind": "main",
            "title": node["title"],
            "emotion": node["emotion"],
            "entry_condition": node["entry_condition"],
            "exit_condition": node["exit_condition"],
            "beats": list(node["beats"]),
            "opens": [],
            "closes": [],
            "segment_budget": 1,
            "parent": None,
            "children": [],
            "chapter_start": 0,
            "chapter_end": 0,
            "source": "llm",
        }
        new_order.append(nid)
        contract_key = str(written + 1 + offset)
        contracts[contract_key] = {
            "node_id": nid,
            "prompt_sha256": None,
            "authority": {
                "premise": authority,
                "causal_node": deepcopy(new_nodes[nid]),
            },
        }
        added_nodes.append({"node_id": nid, "title": node["title"]})
        added_contracts.append({
            "segment_no": written + 1 + offset,
            "node_id": nid,
        })

    new_timeline = dict(timeline)
    new_timeline["nodes"] = new_nodes
    new_timeline["order"] = new_order
    new_timeline["current_node"] = added_nodes[0]["node_id"]
    new_timeline["current_beat"] = 0
    planning = dict(timeline.get("planning") or {})
    planning["segment_contracts"] = contracts
    new_timeline["planning"] = planning
    project.plot_timeline = new_timeline
    db.commit()
    return {
        "status": "extended",
        "nodes": added_nodes,
        "contracts": added_contracts,
        "message": f"已在保存权威允许的范围内追加 {len(added_nodes)} 个剧情点并写入契约。",
    }


def extend_plan(db, project, provider, *, count: int = 1) -> dict:
    """在权威允许的范围内追加剧情点；失败时返回带 `message` 的状态码。"""
    count = max(1, min(int(count or 1), MAX_EXTEND_COUNT))
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    if not timeline:
        return {"status": "no_plan", "message": "这个项目没有时间线状态，无法扩展规划。"}

    authority = _authority_text(project)
    if not authority:
        return {
            "status": "no_saved_authority",
            "message": "没有保存权威，不能扩展规划：新剧情点没有可核对的依据。",
        }

    if _chapter_based(db, project):
        return {
            "status": "chapter_mode_requires_outline",
            "message": "章节项目的下一个剧情点由章纲决定：请先用章纲端点补第 N 章，"
                       "再追加节点；段模式才支持直接扩展规划。",
        }

    nodes = timeline.get("nodes") if isinstance(timeline.get("nodes"), dict) else {}
    order = [nid for nid in (timeline.get("order") or [])
             if nid in nodes and (nodes[nid] or {}).get("kind", "main") == "main"]
    if not order:
        order = [nid for nid, node in nodes.items()
                 if isinstance(node, dict) and node.get("kind", "main") == "main"]
    done = {str(x) for x in (timeline.get("done") or [])}
    remaining = sorted(set(order) - done)
    if not order:
        return {"status": "no_plan", "message": "时间线里没有主干节点，无法扩展规划。"}
    if remaining:
        return {
            "status": "plan_not_exhausted",
            "message": f"规划还没写完（{len(remaining)} 个剧情点未写），不需要扩展。",
        }

    proposals, failures = _propose(provider, authority, nodes, count, None)
    if not proposals:
        return {
            "status": "proposal_failed",
            "failures": failures,
            "message": "模型没有给出可用的新剧情点（格式或内容不合格）。",
        }

    feedback = None
    problems: list[str] = []
    for attempt in range(MAX_PROPOSE_ATTEMPTS):
        accepted, problems = [], []
        for node in proposals:
            violations = _out_of_authority(provider, authority, node)
            if violations:
                items = "、".join(f"{v['entity']}（{v['kind']}）" for v in violations)
                problems.append(
                    f"「{node['title']}」引入了保存权威之外的物件/人物/地点：{items}。"
                    "请改用权威里已有的东西承担同样的剧情功能。"
                )
                continue
            accepted.append(node)
        if accepted:
            return _write_extension(
                db, project, accepted, authority, timeline, nodes, order,
                _written_segments(db, project),
            )
        if attempt + 1 < MAX_PROPOSE_ATTEMPTS:
            feedback = ("\n".join(problems)
                        + f"\n请重新输出这 {count} 个节点，并严格满足上面的问题。")
            proposals, more = _propose(provider, authority, nodes, count, feedback)
            failures += more
            if not proposals:
                break

    detail = "；".join(problems) if problems else "新剧情点不合格。"
    return {
        "status": "out_of_authority_items",
        "failures": failures,
        "message": "新剧情点越出了保存权威允许的范围，纠正后仍不合格：" + detail,
    }
