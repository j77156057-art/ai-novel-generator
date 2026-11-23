# -*- coding: utf-8 -*-
"""章前「这一章怎么开头」建议：模型生成 3 条 + 规则兜底。

素材全部来自生成链路已经在用的数据，不另造一套：

- 本章章纲：`chapter_outline_entry(projects.outline, chapter)`
- 当前时间线节点：`projects.plot_timeline` → `node_for_chapter`
- 上一章：最新一条 `generations.summary` 的正文摘要
- 钩子：`get_reminders(...)["unresolved_plotholes"]`
- 人物：`plot_bible["characters"]` 的名字与角色

模型调用失败、返回不是 JSON、条数不足时，一律退回规则模板；调用方从
`source=model|fallback` 就能看出这次是哪种。本模块不写任何业务表。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import desc, select

from app.generation.prompts import chapter_outline_entry
from app.generation.engine import (
    _chapter_ordinal,
    _outline_entries,
    _total_segments,
)
from app.generation.timeline import node_for_chapter, state_from_dict
from app.kg.queries import get_reminders
from app.models import Generation, Project

logger = logging.getLogger("generation.opening_options")

MAX_OPTIONS = 3

_SYSTEM = (
    "你是中文网文的开头编辑。只输出 JSON 数组，不要 Markdown，不要解释。"
)

_USER = """第 {chapter} 章的写作素材：
章纲：{outline}
时间线节点：{node}
上一章摘要：{previous}
未解决钩子：{hooks}
主要人物：{characters}

请给出 3 个不同的「本章开头方式」。严格返回 JSON 数组，每项包含：
- title：不超过 12 字
- summary：一句话说明怎么开头（不超过 40 字）
- opening：镜头/动作/信息建议（不超过 80 字，只描述写法，不要写正文）
- why：为什么适合这一章（不超过 30 字）
"""


def _prev_summary(db, project_id: int) -> str:
    row = db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(desc(Generation.segment_no))
        .limit(1)
    ).first()
    if row is None:
        return ""
    summary = row.summary if isinstance(row.summary, dict) else {}
    text = summary.get("text")
    if text:
        return str(text).strip()
    # 导入/历史行可能没有摘要：退回正文末尾，至少给模型一个衔接点。
    body = (row.text or "").strip()
    return body[-160:] if body else ""


def _node_info(project: Project, chapter: int) -> dict[str, Any]:
    entries = _outline_entries(project)
    saved = project.plot_timeline if isinstance(project.plot_timeline, dict) else None
    state = state_from_dict(
        saved or {},
        total_segments=_total_segments(project),
        total_chapters=len(entries) or None,
    )
    if state is None:
        return {}
    node_id = node_for_chapter(state, chapter)
    node = state.nodes.get(node_id or "")
    if node is None:
        return {}
    return {
        "id": node.id,
        "title": node.title,
        "entry_condition": node.entry_condition,
        "exit_condition": node.exit_condition,
        "beats": list(node.beats),
        "chapter_start": node.chapter_start,
        "chapter_end": node.chapter_end,
    }


def _hooks(db, project_id: int) -> list[str]:
    try:
        data = get_reminders(db, project_id, source="generated", top_k=5)
    except Exception:  # noqa: BLE001 —— 建议接口不能因为图谱缺失失败
        return []
    out: list[str] = []
    for item in data.get("unresolved_plotholes") or []:
        question = str(item.get("question") or "").strip()
        # KG 的「未填坑」里混着台词残片（如「钱多多道：“什么公告？」），
        # 直接给模型会变成坏建议：只收长度合理、无引号/冒号的完整短句。
        if not (6 <= len(question) <= 40):
            continue
        if any(ch in question for ch in "“”「」『』：:"):
            continue
        # 带第二人称/自称的短问句基本都是台词残片（「您要帮小的抬？」）。
        if any(word in question for word in ("您", "你", "我", "小的", "许哥")):
            continue
        if question.endswith(("？", "?")) and "道" in question:
            continue
        if question not in out:
            out.append(question)
    return out[:3]


def _characters(project: Project) -> list[str]:
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    chars = bible.get("characters")
    out: list[str] = []
    if isinstance(chars, list):
        for item in chars:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            role = str(item.get("role") or "").strip()
            if not name:
                continue
            role = re.split(r"[，,。；;]", role)[0][:18]
            out.append(f"{name}（{role}）" if role else name)
    return out[:5]


def build_brief(db, project: Project, chapter: int) -> dict[str, Any]:
    """汇总一条章前建议需要的全部素材（只读）。"""
    entries = _outline_entries(project)
    return {
        "chapter": chapter,
        "is_first_chapter": chapter <= 1,
        "outline": chapter_outline_entry(project.outline, chapter) or "",
        "outline_total": len(entries),
        "node": _node_info(project, chapter),
        "previous": _prev_summary(db, project.id),
        "hooks": _hooks(db, project.id),
        "characters": _characters(project),
    }


def _fallback_options(brief: dict[str, Any]) -> list[dict[str, str]]:
    """规则兜底：保证永远能给出 3 条可选项。"""
    chapter = int(brief.get("chapter") or 1)
    outline = str(brief.get("outline") or "").strip()
    node = brief.get("node") or {}
    previous = str(brief.get("previous") or "").strip()
    hooks = [str(x) for x in (brief.get("hooks") or []) if str(x).strip()]
    characters = [str(x) for x in (brief.get("characters") or []) if str(x).strip()]

    if chapter <= 1:
        lead = characters[0] if characters else "主角"
        return [
            {
                "id": "cold_open",
                "title": "冷开场",
                "summary": "先给一个反常动作，不解释前因。",
                "opening": f"开场就让{lead}在做一件看似日常、但读者一眼觉得不对的事；"
                           "先写动作和物件，把解释留到后面。",
                "why": "第一章先建立悬念，比交代背景更快抓住人。",
            },
            {
                "id": "daily_open",
                "title": "日常切入",
                "summary": "从最具体的一天写起，让异常从日常里渗出来。",
                "opening": "先写清楚时间、地点和一件要办的具体事（收租、修东西、清点），"
                           "让读者跟着动作进入世界。",
                "why": "都市题材用日常建立真实感，异常出现时反差更大。",
            },
            {
                "id": "outline_open",
                "title": "按章纲开场",
                "summary": "直接落到本章章纲的第一个动作。",
                "opening": (outline[:80] or "从章纲里的第一个具体动作写起，先给画面再给信息。"),
                "why": "不额外设计镜头，最贴近大纲，后续最不容易跑偏。",
            },
        ]

    hook = hooks[0] if hooks else ""
    node_title = str(node.get("title") or "").strip()
    node_exit = str(node.get("exit_condition") or "").strip()
    lead = characters[0].split("（")[0] if characters else "主角"
    paras = [p.strip() for p in previous.split("\n\n") if p.strip()]
    prev_tail = "；".join(paras[-2:])[:120] if paras else ""

    return [
        {
            "id": "hook_open",
            "title": "接着钩子写",
            "summary": "从上一章留下的问题直接切入。",
            "opening": (
                f"开场先回应上一章的悬念：{prev_tail}" if prev_tail
                else (hook or "从上一章结尾的动作或问题上直接续上。")
            ),
            "why": "读者刚被上一章吊住，先给回应最稳。",
        },
        {
            "id": "node_open",
            "title": "从本章节点切入",
            "summary": f"先写「{node_title or '当前剧情点'}」的核心动作。",
            # 章纲才是本章的具体事件；节点只用来约束推进节奏。
            "opening": (
                outline[:120]
                or node_exit
                or "先写本章节点的关键动作，再补人物关系。"
            ),
            "why": "开场就推进主线，避免开头铺垫过长。",
        },
        {
            "id": "character_open",
            "title": "先给人物难题",
            "summary": f"让{lead}在开场就碰上本章的核心冲突。",
            "opening": f"开场把{lead}放进一个必须当场做选择的具体处境，"
                       "用他的反应交代性格与关系。",
            "why": "人物先动起来，比先讲设定更容易读进去。",
        },
    ]


def _extract_json_array(raw: str) -> list[Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def _clean_option(item: Any, index: int) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    opening = str(item.get("opening") or "").strip()
    why = str(item.get("why") or "").strip()
    if not (title and summary and opening):
        return None
    return {
        "id": f"model_{index}",
        "title": title[:16],
        "summary": summary[:60],
        "opening": opening[:160],
        "why": why[:40],
    }


def _model_options(brief: dict[str, Any]) -> list[dict[str, str]] | None:
    try:
        from app.routers.assistant_shared import _ensure_assistant_provider

        provider = _ensure_assistant_provider()
        node = brief.get("node") or {}
        node_text = "；".join(
            x for x in (
                str(node.get("title") or ""),
                str(node.get("entry_condition") or ""),
                str(node.get("exit_condition") or ""),
            ) if x
        )
        user = _USER.format(
            chapter=brief.get("chapter") or 1,
            outline=brief.get("outline") or "（本章暂无章纲）",
            node=node_text or "（暂无时间线节点）",
            previous=(brief.get("previous") or "（第一章，无上一章）")[:240],
            hooks="；".join(brief.get("hooks") or []) or "（无未解决钩子）",
            characters="；".join(brief.get("characters") or []) or "（暂无人物卡）",
        )
        raw = provider.chat(_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001 —— 模型失败一律退回规则
        logger.warning("章前建议模型调用失败，退回规则：%s", exc)
        return None
    data = _extract_json_array(raw if isinstance(raw, str) else str(raw or ""))
    if not data:
        return None
    cleaned = [x for i, item in enumerate(data[:MAX_OPTIONS], 1)
               if (x := _clean_option(item, i)) is not None]
    return cleaned if len(cleaned) >= 2 else None


def build_opening_options(
    db,
    project: Project,
    chapter: int | None = None,
    *,
    use_model: bool = True,
) -> dict[str, Any]:
    """生成章前建议。返回的 `options` 至少 2 条，最多 3 条。"""
    if chapter is None or chapter <= 0:
        chapter = next_chapter(db, project)
    brief = build_brief(db, project, chapter)
    options = _model_options(brief) if use_model else None
    source = "model"
    if not options:
        options = _fallback_options(brief)
        source = "fallback"
    return {
        "chapter": chapter,
        "is_first_chapter": bool(brief["is_first_chapter"]),
        "source": source,
        "options": options[:MAX_OPTIONS],
        "brief": {
            "outline": brief["outline"],
            "outline_total": brief["outline_total"],
            "node": brief["node"],
            "previous": brief["previous"][:300],
            "hooks": brief["hooks"],
            "characters": brief["characters"],
        },
    }


def next_chapter(db, project: Project) -> int:
    """下一章章号：历史章节标签的最大值 + 1。"""
    rows = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no)
    ).all()
    last = 0
    for row in rows:
        ordinal = _chapter_ordinal(row.chapter_range or "")
        last = max(last, ordinal or row.segment_no or 0)
    return last + 1
