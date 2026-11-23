# -*- coding: utf-8 -*-
"""素材包工具：目录 / 搜索 / 按 id 取 / 按用途取。

同一份实现服务两条链路：
- 助手问答：注册进 `agents/registry.py` 的只读白名单（callable 签名 `(db, project_id, **kw)`）；
- 生成链路：包太大时用 `gather_pack_blocks()` 跑一轮函数调用，拿回可注入的素材块。

所有工具只读；`used_ids` 只作为编排层回填使用次数的信号，工具本身不写库。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.materials import store
from app.materials.research import FICTION_CATEGORY
from app.materials.settings import MaterialSettings, load_settings
from app.models import Project

logger = logging.getLogger("materials.tool")

TOOL_TOC = "material_pack_toc"
TOOL_SEARCH = "material_pack_search"
TOOL_GET = "material_pack_get"
TOOL_BY_USE = "material_pack_by_use"

TOOL_NAMES = (TOOL_TOC, TOOL_SEARCH, TOOL_GET, TOOL_BY_USE)

#: 单次返回块的字数上限，避免工具把 prompt 塞爆。
MAX_TOOL_CHARS = 2600


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
    }


#: 助手 function calling 的参数 schema，同时用于生成链路。
MODEL_TOOL_PARAMETERS: dict[str, dict[str, Any]] = {
    TOOL_TOC: _obj({}),
    TOOL_SEARCH: _obj(
        {
            "query": {"type": "string", "description": "要查的素材关键词"},
            "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
            "category": {"type": "string", "description": "可选：目录分类"},
            "year_from": {"type": "integer", "description": "可选：最早年份"},
            "year_to": {"type": "integer", "description": "可选：最晚年份"},
        },
        ["query"],
    ),
    TOOL_GET: _obj(
        {
            "ids": {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": 20,
                "description": "素材 id 列表（来自目录或搜索结果）",
            }
        },
        ["ids"],
    ),
    TOOL_BY_USE: _obj(
        {
            "use_case": {"type": "string", "description": "用途/场景关键词，如「旧城改造流程」"},
            "chapter_no": {"type": "integer", "minimum": 1, "description": "可选：按章节提示过滤"},
            "k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
        },
        ["use_case"],
    ),
}

_DESCRIPTIONS = {
    TOOL_TOC: "查看本书素材包目录：分类、年份、用途与最近入库的素材标题。",
    TOOL_SEARCH: "在本书素材包中按关键词检索可用素材，返回带年份与用途的条目。",
    TOOL_GET: "按 id 取本书素材包中的具体素材内容（先用目录/搜索拿到 id）。",
    TOOL_BY_USE: "按「可用在什么地方」检索素材，例如某个章节场景或职业流程。",
}


# --------------------------------------------------------------------------- #
# 实现（只读）
# --------------------------------------------------------------------------- #

def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render(rows: Iterable[Any], settings: MaterialSettings | None = None) -> str:
    cfg = settings or load_settings()
    lines: list[str] = []
    used = 0
    for row in rows:
        year = str(row.published_year) if row.published_year else "年份未知"
        head = f"[#{row.id}][{year}][{row.category or '未分类'}] {_clip(row.title, 60)}"
        body = _clip(row.summary or row.snippet or "", cfg.inject_snippet_chars)
        case = _clip(row.use_case or "", 100)
        parts = [head]
        if body:
            parts.append(f"  {body}")
        if case:
            parts.append(f"  可用在：{case}")
        if row.category == FICTION_CATEGORY:
            parts.append("  说明：同类小说参考，只参考写法/设定，不要照抄。")
        text = "\n".join(parts)
        if used + len(text) + 1 > MAX_TOOL_CHARS:
            break
        lines.append(text)
        used += len(text) + 1
    return "\n".join(lines)


def impl_toc(db: Session, project_id: int, **_kw: Any) -> dict[str, Any]:
    data = store.toc(db, project_id)
    return {
        "ok": True,
        "stats": data["stats"],
        "categories": data["categories"],
        "years": data["years"],
        "use_cases": data["use_cases"],
        "recent": data["recent"],
        "text": _toc_text(data),
        "used_ids": [],
    }


def impl_search(
    db: Session,
    project_id: int,
    *,
    query: str = "",
    k: int = 6,
    category: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    rows = store.search(
        db,
        project_id,
        query,
        k=k,
        category=category,
        year_from=year_from,
        year_to=year_to,
    )
    return {
        "ok": True,
        "count": len(rows),
        "items": [store.to_dict(r) for r in rows],
        "text": _render(rows) or f"素材包里没有命中「{_clip(query, 40)}」的可用素材。",
        # 搜索只是「看到候选」，不算真正使用；只有 get 到具体条目才计数。
        "used_ids": [],
    }


def impl_get(db: Session, project_id: int, *, ids: Iterable[int] | None = None, **_kw: Any) -> dict[str, Any]:
    try:
        wanted = [int(i) for i in (ids or [])]
    except (TypeError, ValueError):
        wanted = []
    rows = store.get_by_ids(db, project_id, wanted)
    return {
        "ok": True,
        "count": len(rows),
        "items": [store.to_dict(r) for r in rows],
        "text": _render(rows) or "没有找到这些 id 对应的素材（可能已被删除）。",
        "used_ids": [r.id for r in rows],
    }


def impl_by_use(
    db: Session,
    project_id: int,
    *,
    use_case: str = "",
    chapter_no: int | None = None,
    k: int = 6,
    **_kw: Any,
) -> dict[str, Any]:
    query = str(use_case or "")
    if chapter_no:
        query = f"{query} 第{chapter_no}章".strip()
    rows = store.search(db, project_id, query, k=k)
    return {
        "ok": True,
        "count": len(rows),
        "items": [store.to_dict(r) for r in rows],
        "text": _render(rows) or "没有找到匹配该用途的素材。",
        # 与 search 同口径：候选不算使用，get 到具体条目才计数。
        "used_ids": [],
    }


_IMPLS = {
    TOOL_TOC: impl_toc,
    TOOL_SEARCH: impl_search,
    TOOL_GET: impl_get,
    TOOL_BY_USE: impl_by_use,
}


def _toc_text(data: dict[str, Any]) -> str:
    stats = data.get("stats") or {}
    lines = [
        f"本书素材包：共 {stats.get('total', 0)} 条，可用 {stats.get('usable', 0)} 条，"
        f"未知年份 {stats.get('unknown_year', 0)} 条。"
    ]
    cats = [c for c in (data.get("categories") or []) if c.get("key")]
    if cats:
        lines.append("分类：" + "、".join(f"{c['key']}({c['count']})" for c in cats[:12]))
    years = [y for y in (data.get("years") or []) if y.get("key")]
    if years:
        lines.append("年份：" + "、".join(f"{y['key']}({y['count']})" for y in years[:10]))
    cases = [u for u in (data.get("use_cases") or []) if u.get("key")]
    if cases:
        lines.append("用途：" + "、".join(f"{u['key']}({u['count']})" for u in cases[:8]))
    recent = data.get("recent") or []
    if recent:
        lines.append("最近入库：")
        for item in recent[:10]:
            lines.append(f"- [#{item['id']}] {_clip(item['title'], 50)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 生成链路：一轮可选的按需取用
# --------------------------------------------------------------------------- #

def openai_specs() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": _DESCRIPTIONS[name],
                "parameters": MODEL_TOOL_PARAMETERS[name],
            },
        }
        for name in TOOL_NAMES
    ]


#: 从渲染后的工具文本里认「[#id]」——只统计**真正拼进 prompt** 的条目。
_RENDERED_ID_RE = re.compile(r"\[#(\d+)\]")
#: 按「行首 [#id]」把渲染文本切回条目（`_render` 每条都从 [#id] 开头）。
_ENTRY_SPLIT_RE = re.compile(r"(?m)^(?=\[#\d+\])")


def filter_duplicate_entries(text: str, seen: set[int]) -> str:
    """去掉 `seen` 里已出现过的素材条目，并把保留下来的 id 记进 `seen`。

    工具轮里 `search` 和随后的 `get` 常返回同一条素材；只在前一个块里保留
    全文，后面的块去掉重复条目，避免同一段内容在 prompt 里出现两次。
    """
    if not text:
        return ""
    kept: list[str] = []
    for part in _ENTRY_SPLIT_RE.split(text):
        m = _RENDERED_ID_RE.match(part)
        if m:
            mid = int(m.group(1))
            if mid in seen:
                continue
            seen.add(mid)
        kept.append(part)
    return "\n".join(p for p in kept if p.strip())


def dispatch(
    db: Session,
    project_id: int,
    name: str,
    arguments: dict[str, Any] | str | None,
    *,
    count_hits: bool = False,
    seen_ids: set[int] | None = None,
) -> str:
    """执行一次工具调用并返回文本。

    `count_hits`：生成链路的工具轮用它**把 search / by_use 的命中条目也计入
    使用次数**——那些条目会被原样拼进正文 prompt，按「被使用」口径就该计数。
    助手问答保持默认 False：候选列表不算使用，只有 `get` 到的才计数。

    `seen_ids`：同一轮工具调用内**跨调用去重**（多次 search 命中同一条只计 1 次）。
    """
    impl = _IMPLS.get(name)
    if impl is None:
        return ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except (TypeError, ValueError):
            arguments = {}
    try:
        result = impl(db, project_id, **(arguments or {}))
    except Exception as exc:  # noqa: BLE001 —— 工具失败不阻断生成
        logger.warning("素材包工具 %s 调用失败：%s", name, exc)
        return ""
    text = str(result.get("text") or "")
    ids = list(result.get("used_ids") or [])
    if count_hits and name in (TOOL_SEARCH, TOOL_BY_USE):
        # 只认渲染文本里出现的 id：被 MAX_TOOL_CHARS 截掉的条目没有进 prompt。
        ids = [int(x) for x in _RENDERED_ID_RE.findall(text)]
    if seen_ids is not None:
        ids = [i for i in ids if i not in seen_ids]
        seen_ids.update(ids)
    if ids:
        try:
            # 独立会话回填使用次数：生成链路传入的 db 里可能还有未提交状态，
            # 不能在这个工具里替它 commit。
            from app.db import SessionLocal

            with SessionLocal() as mdb:
                store.mark_used(mdb, ids, project_id=project_id)
        except Exception:  # noqa: BLE001 —— 计数失败不影响取素材
            pass
    return text


def gather_pack_blocks(
    provider,
    db: Session,
    project: Project,
    system: str,
    user: str,
    *,
    budget_s: float = 30.0,
    max_rounds: int = 3,
) -> list[str]:
    """包太大时的一轮工具调用：模型先看目录，再按需 search/get。

    `system` / `user` 必须是**瘦上下文**（目录 + 本章大纲），不要传整份写作
    prompt：实测 28.9k 字的写作 prompt 单次调用要 27.5s，且模型常直接写正文；
    1.6k 字的瘦上下文只要 1.5s，会正常调工具。

    `budget_s` 是**硬顶**：剩余额度会作为 `budget_seconds` 传给单次调用，
    不再只是轮间检查（此前单次调用可跑满 writer 的 300s）。
    """
    from app.llm.provider import ToolsUnsupportedError

    specs = openai_specs()
    messages = [
        {
            "role": "system",
            "content": system
            + "\n\n【本书素材包】包内素材较多，已注入目录。需要具体细节时调用 "
            "material_pack_search / material_pack_get / material_pack_by_use；"
            "不需要就直接回复「无」。本轮只取素材，不写正文。",
        },
        {"role": "user", "content": user},
    ]
    deadline = time.monotonic() + max(1.0, budget_s)
    blocks: list[str] = []
    seen_ids: set[int] = set()
    try:
        for _ in range(max_rounds):
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                break
            try:
                message = provider.complete_message(
                    messages, tools=specs, tool_choice="auto",
                    budget_seconds=remaining,
                )
            except TypeError:
                # 老 provider 的签名不接 budget_seconds：退回不带预算调用
                # （轮间 deadline 仍然生效）。
                message = provider.complete_message(
                    messages, tools=specs, tool_choice="auto"
                )
            calls = list(message.get("tool_calls") or [])
            if not calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": str(message.get("content") or ""),
                    "tool_calls": calls,
                }
            )
            for call in calls:
                fn = call.get("function") or {}
                name = str(fn.get("name") or "")
                # 生成链路：search / by_use 的命中条目会拼进正文 prompt，
                # 按「被使用」计数（助手问答仍只认 get）。
                seen_before = set(seen_ids)
                result = dispatch(
                    db, project.id, name, fn.get("arguments"),
                    count_hits=True, seen_ids=seen_ids,
                )
                if result:
                    # 同一轮里 search 后再 get 到同一条：后一个块不再重复注入。
                    result = filter_duplicate_entries(result, seen_before)
                if result:
                    blocks.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id") or ""),
                        "content": result or "（无结果）",
                    }
                )
    except ToolsUnsupportedError:
        logger.warning("当前模型不支持 function calling，跳过素材包工具轮")
        return []
    except Exception as exc:  # noqa: BLE001 —— 素材是增强项
        logger.warning("素材包工具轮失败（已忽略）：%s", exc)
        return blocks
    return blocks
