# -*- coding: utf-8 -*-
"""素材包注入策略：按章纲确定性挑 Top-N，包太大时改给目录。

为什么不用额外模型轮：
- 每章多一轮结构化补全会把「等待首字」再拖十几秒；素材包的检索与整理已经在
  后台完成，生成时只需要一次本地 SQL + 关键词打分，毫秒级。
- 只有当包大到无法整块塞进 prompt（usable > `toc_threshold`）时，才注入目录并
  提示模型调用 `material_pack_*` 工具按需取用。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.materials import store
from app.materials.research import FICTION_CATEGORY
from app.materials.settings import MaterialSettings, load_settings
from app.models import Project


def _bigrams(text: str) -> set[str]:
    text = " ".join(str(text or "").split()).lower()
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _overlap(source: str, target_grams: set[str]) -> float:
    grams = _bigrams(source)
    if not grams:
        return 0.0
    return len(grams & target_grams) / len(grams)


def _chapter_text(project: Project, chapter: int) -> str:
    parts: list[str] = []
    try:
        from app.generation.prompts import chapter_outline_entry

        parts.append(str(chapter_outline_entry(project.outline, chapter) or ""))
    except Exception:  # noqa: BLE001
        pass
    parts.append(str(project.premise or ""))
    parts.append(str(project.genre or ""))
    return " ".join(p for p in parts if p)


def _year_score(year: int | None) -> float:
    if not year:
        return 0.0
    current = datetime.now(timezone.utc).year
    age = max(0, current - int(year))
    if age <= 5:
        return 0.8
    if age <= 10:
        return 0.4
    # 旧内容降权：每超出一个十年扣 0.2，最低 -1.0。
    return max(-1.0, -0.2 * ((age - 10) // 10 + 1))


def score_material(row, chapter_grams: set[str]) -> float:
    text = " ".join(
        [
            str(row.title or ""),
            str(row.summary or ""),
            str(row.snippet or ""),
            str(row.use_case or ""),
            str(row.category or ""),
        ]
    )
    score = 3.0 * _overlap(text, chapter_grams)
    if row.use_case:
        score += 1.0 * _overlap(str(row.use_case), chapter_grams)
    score += _year_score(row.published_year)
    score += 0.3 * math.log1p(max(0, int(row.use_count or 0)))
    return score


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render_rows(rows: list, cfg: MaterialSettings) -> str:
    lines = ["【本书素材包 · 已标注年份与适用处】"]
    used = len(lines[0])
    for row in rows:
        year = str(row.published_year) if row.published_year else "年份未知"
        category = str(row.category or "未分类")
        head = f"[#{row.id}][{year}][{category}] {_clip(row.title, 60)}"
        body = _clip(row.summary or row.snippet or "", cfg.inject_snippet_chars)
        case = str(row.use_case or "").strip()
        block = [head]
        if body:
            block.append(f"  {body}")
        if case:
            block.append(f"  可用在：{_clip(case, 80)}")
        if row.category == FICTION_CATEGORY:
            block.append("  说明：同类小说参考，只参考写法/设定，不要照抄。")
        block_text = "\n".join(block)
        if used + len(block_text) + 1 > cfg.max_block_chars:
            break
        lines.append(block_text)
        used += len(block_text) + 1
    return "\n".join(lines) if len(lines) > 1 else ""


def build_toc_block(toc: dict[str, Any], cfg: MaterialSettings) -> str:
    stats = toc.get("stats") or {}
    lines = [
        "【本书素材包目录 · 素材较多，按需取用】",
        f"共 {stats.get('total', 0)} 条，可用 {stats.get('usable', 0)} 条；"
        "需要具体内容时调用 material_pack_search / material_pack_get / material_pack_by_use。",
    ]
    cats = [c for c in (toc.get("categories") or []) if c.get("key")]
    if cats:
        lines.append(
            "分类：" + "、".join(f"{c['key']}({c['count']})" for c in cats[:12])
        )
    years = [y for y in (toc.get("years") or []) if y.get("key")]
    if years:
        lines.append(
            "年份：" + "、".join(f"{y['key']}({y['count']})" for y in years[:10])
        )
    cases = [u for u in (toc.get("use_cases") or []) if u.get("key")]
    if cases:
        lines.append(
            "用途：" + "、".join(f"{u['key']}({u['count']})" for u in cases[:8])
        )
    recent = toc.get("recent") or []
    if recent:
        lines.append("最近入库示例：")
        for item in recent[: cfg.toc_titles]:
            lines.append(f"- [#{item['id']}] {_clip(item['title'], 50)}")
    return "\n".join(lines)


def _norm_title(title: str) -> str:
    """标题归一，与入库去重共用一套规则（`store.title_key`）。

    短通用标题（`title_key` 返回空串）退回原文，仍按「标题前 24 字 + 摘要」
    去重，避免把不同来源的同名短标题合并。
    """
    return store.title_key(title) or " ".join(str(title or "").lower().split())[:60]


def _pick_rows(ranked: list, top_n: int) -> list:
    """按分数挑选，但对「同类小说参考」限流并去重，且把知识素材排在前面。

    小说参考适合放进大目录/工具按需查，不应该挤占正文注入的知识素材；
    直接注入最多保留 2 条，并统一放在末尾。
    """
    knowledge: list = []
    fiction: list = []
    seen: set[tuple[str, str]] = set()
    for row in ranked:
        # 标题前 24 字 + 摘要前 24 字：能挡住「同名文章换个站点后缀」的重复。
        key = (
            _norm_title(row.title)[:24],
            " ".join(str(row.summary or row.snippet or "").split())[:24],
        )
        if key in seen:
            continue
        seen.add(key)
        if row.category == FICTION_CATEGORY:
            if len(fiction) >= 2:
                continue
            fiction.append(row)
        else:
            knowledge.append(row)
        if len(knowledge) + len(fiction) >= top_n:
            break
    picked = (knowledge + fiction)[:top_n]
    # 若知识素材不够，允许用小说参考补足，但最多 2 条。
    if len(picked) < top_n:
        for row in fiction:
            if row not in picked:
                picked.append(row)
            if len(picked) >= top_n:
                break
    return picked[:top_n]


def build_rows_block(
    db: Session,
    project: Project,
    chapter: int,
    *,
    settings: MaterialSettings | None = None,
) -> tuple[str, list[int]]:
    """不看目录阈值，直接按章相关度挑 Top-N，返回 (注入块, 素材 id)。

    目录模式的兜底用：模型在工具轮里没取到任何具体素材时，本章仍要拿到
    可以照着写的条目（带年份与适用处），而不是只剩一份目录。
    """
    cfg = settings or load_settings()
    if not cfg.enabled:
        return "", []
    if store.count_usable(db, project.id) <= 0:
        return "", []

    grams = _bigrams(_chapter_text(project, chapter))
    rows = store.usable_rows(db, project.id, limit=500)
    ranked = sorted(rows, key=lambda r: score_material(r, grams), reverse=True)
    picked = _pick_rows(ranked, cfg.top_n)
    block = _render_rows(picked, cfg)
    return block, [r.id for r in picked] if block else []


def build_block(
    db: Session,
    project: Project,
    chapter: int,
    *,
    settings: MaterialSettings | None = None,
) -> tuple[str, list[int]]:
    """返回 (注入块, 被注入的素材 id)。没有素材时返回 ("", [])。"""
    cfg = settings or load_settings()
    if not cfg.enabled:
        return "", []
    total = store.count_usable(db, project.id)
    if total <= 0:
        return "", []
    if total > cfg.toc_threshold:
        return build_toc_block(store.toc(db, project.id), cfg), []
    return build_rows_block(db, project, chapter, settings=cfg)


def needs_tool_round(
    db: Session, project: Project, *, settings: MaterialSettings | None = None
) -> bool:
    """包是否大到需要「目录 + 工具按需取」而不是整块注入。"""
    cfg = settings or load_settings()
    return cfg.enabled and store.count_usable(db, project.id) > cfg.toc_threshold
