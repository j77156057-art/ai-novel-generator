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
import logging
import re
from copy import deepcopy

from sqlalchemy import select

from app.generation.planning import model_json

logger = logging.getLogger(__name__)

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
             feedback: str | None,
             extra: str | None = None) -> tuple[list[dict], list[dict]]:
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
    if extra:
        # 章模式：新节点必须长在刚补的章纲上，不能另起一条故事线。
        user += "\n\n" + extra
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


def _check_nodes(provider, authority: str, nodes: dict, count: int,
                 proposals: list[dict], failures: list[dict],
                 extra: str | None = None
                 ) -> tuple[list[dict], list[str], list[dict]]:
    """逐个做权威核对（段模式）；不合格就回灌让模型自改一次。

    返回 `(accepted, problems, failures)`。`accepted` 为空时调用方必须按失败
    处理——**不能**先把节点写进去再发现不合格。
    """
    problems: list[str] = []
    feedback = None
    for attempt in range(MAX_PROPOSE_ATTEMPTS):
        accepted: list[dict] = []
        problems = []
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
            return accepted, problems, failures
        if attempt + 1 < MAX_PROPOSE_ATTEMPTS:
            feedback = ("\n".join(problems)
                        + f"\n请重新输出这 {count} 个节点，并严格满足上面的问题。")
            proposals, more = _propose(
                provider, authority, nodes, count, feedback, extra=extra
            )
            failures += more
            if not proposals:
                break
    return [], problems, failures


def _append_chapter_nodes(db, project, accepted, timeline, nodes, order):
    """章模式追加节点：只写节点与顺序，不写段契约（契约是段号维度的）。

    章区间不在这里算——`state_from_dict` 每次读都会按当前章纲长度重排，
    写入 0 占位，随后 `_timeline_state_for` 会把区间与当前节点对齐并落库。
    """
    new_nodes = dict(nodes)
    new_order = list(order)
    first = new_nodes.get(new_order[0]) if new_order else None
    budget = int(first.get("segment_budget") or 1) if isinstance(first, dict) else 1
    added: list[dict] = []
    for node in accepted:
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
            "segment_budget": budget,
            "parent": None,
            "children": [],
            "chapter_start": 0,
            "chapter_end": 0,
            "source": "llm",
        }
        new_order.append(nid)
        added.append({"node_id": nid, "title": node["title"]})

    new_timeline = dict(timeline)
    new_timeline["nodes"] = new_nodes
    new_timeline["order"] = new_order
    project.plot_timeline = new_timeline
    db.commit()
    return added


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


def _extend_chapters(db, project, provider, count: int) -> dict:
    """章模式：补 count 章章纲 + 追加 count 个剧情点，再把区间/当前节点对齐。

    只补章不补节点，等于把原有节点摊薄到更多章上：写完仍然 plan_exhausted，
    并没有真正「扩展规划」。所以一章配一个剧情点，两者一起补。

    顺序是「先起草 → 先核对 → 再写库」：节点不合格时整批不落库，
    不会出现「章纲加了、情节没加」的半成品。
    """
    from fastapi import HTTPException

    from app.generation.chapter_outline import normalize_outline
    from app.routers.chapter_outline import (
        ChapterOutlineApply,
        ChapterOutlineDraft,
        ChapterOutlineEntryIn,
        apply_chapter_outline,
        draft_chapter_outline,
    )

    start = len(normalize_outline(project.outline)) + 1
    end = start + count - 1
    try:
        draft = draft_chapter_outline(
            project.id,
            ChapterOutlineDraft(
                from_chapter=start,
                to_chapter=end,
                # 章纲校验会拒绝引用「权威外的专名」：提前把这个边界说清楚，
                # 少一次「起草成功、校验失败」的往返。
                direction="顺承前文；只使用权威里已有的人名、物件、地点，"
                          "不要新造专名、题记或引号里的术语。",
            ),
            db,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        message = detail.get("message") or detail.get("error") or str(exc.detail)
        return {
            "status": "proposal_failed",
            "message": f"起草第 {start}–{end} 章章纲失败：{message}",
        }
    if not (draft.get("validation") or {}).get("ok"):
        return {
            "status": "outline_invalid",
            "message": "起草出的章纲没通过校验，未写入："
                       + str(draft.get("validation"))[:300],
        }
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    if not timeline:
        return {"status": "no_plan", "message": "这个项目没有时间线状态，无法扩展规划。"}
    authority = _authority_text(project)
    if not authority:
        return {
            "status": "no_saved_authority",
            "message": "没有保存权威，不能扩展规划：新剧情点没有可核对的依据。",
        }
    nodes = timeline.get("nodes") if isinstance(timeline.get("nodes"), dict) else {}
    order = [nid for nid in (timeline.get("order") or [])
             if nid in nodes and (nodes[nid] or {}).get("kind", "main") == "main"]
    if not order:
        order = [nid for nid, node in nodes.items()
                 if isinstance(node, dict) and node.get("kind", "main") == "main"]

    chapter_lines = "\n".join(
        f"- 第{item['chapter']}章：{item['entry']}"
        for item in draft.get("entries") or []
    )
    extra = (
        "【本次新补的章纲】（新剧情点必须建立在这些章的事件之上，不得另起故事线）\n"
        + (chapter_lines or "（无）")
    )
    try:
        proposals, failures = _propose(
            provider, authority, nodes, count, None, extra=extra
        )
    except Exception as exc:  # noqa: BLE001 - 渠道断连等要回可读原因，不 500
        return {
            "status": "proposal_failed",
            "message": f"调用模型生成新剧情点失败：{exc}"[:300],
        }
    if not proposals:
        return {
            "status": "proposal_failed",
            "failures": failures,
            "message": "模型没有给出可用的新剧情点（格式或内容不合格）；章纲未写入。",
        }
    # 章模式不再跑封闭清单审计：那是**正文级**证据核对（多阶段模型调用，
    # 单节点约 1 分钟），用来审规划节点不划算，会把一次扩展拖到十分钟。
    # 规划节点的把关有两道更贴合的：章纲本身已过 authority 术语校验，
    # 节点提案的提示词也限定「只用权威里已有的人名/物件/地点」。
    accepted = proposals

    entries = [
        ChapterOutlineEntryIn(chapter=int(item["chapter"]), entry=item["entry"])
        for item in draft.get("entries") or []
    ]
    try:
        apply_chapter_outline(
            project.id,
            ChapterOutlineApply(
                from_chapter=start,
                to_chapter=end,
                entries=entries,
                expected_fingerprint=draft["source_fingerprint"],
                confirm=True,
            ),
            db,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        return {
            "status": "outline_invalid",
            "message": "章纲写入被拒：" + str(detail.get("message") or detail)[:300],
        }
    # 章纲已确认可用，才追加节点：两者要么一起成功，要么都不写。
    added_nodes = _append_chapter_nodes(db, project, accepted, timeline, nodes, order)

    # 立刻把存档对齐到新的「下一章」：否则 /timeline 读到的还是旧的
    # done=全部、current=None，界面显示「计划写完了」，和已放行的生成矛盾。
    try:
        from app.generation.engine import _timeline_progress, _timeline_state_for

        written, chapter_based = _timeline_progress(db, project)
        if chapter_based:
            _timeline_state_for(project, written, None, db=db, chapter_based=True)
    except Exception as exc:  # noqa: BLE001 - 对齐失败不推翻已写入的章纲
        logger.warning("扩展规划后对齐时间线失败：%s", exc)

    return {
        "status": "extended",
        "chapters": [item["chapter"] for item in draft.get("entries") or []],
        "nodes": added_nodes,
        "contracts": [],
        "message": f"已补第 {start}–{end} 章章纲并追加 {len(added_nodes)} 个剧情点；"
                   "节点区间按新章纲重算。",
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
        # 章模式：补章纲 + 追加节点，一章配一个剧情点（见 `_extend_chapters`）。
        return _extend_chapters(db, project, provider, count)

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

    accepted, problems, failures = _check_nodes(
        provider, authority, nodes, count, proposals, failures
    )
    if accepted:
        return _write_extension(
            db, project, accepted, authority, timeline, nodes, order,
            _written_segments(db, project),
        )

    detail = "；".join(problems) if problems else "新剧情点不合格。"
    return {
        "status": "out_of_authority_items",
        "failures": failures,
        "message": "新剧情点越出了保存权威允许的范围，纠正后仍不合格：" + detail,
    }
