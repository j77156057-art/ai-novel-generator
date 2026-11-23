# -*- coding: utf-8 -*-
"""生成链路接线：注入已有素材 + 排队后台调研 + 大包时按需取用。

调用点只做一次函数调用，真正的检索/整理都在 worker 线程；这里只做
本地 SQL、一次入队与（包很大时）一轮工具调用。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.materials import pack, store, tool, worker
from app.materials.settings import MaterialSettings, load_settings
from app.models import Project

logger = logging.getLogger("materials.hook")

#: 工具轮的 system：**只做检索**，不继承写手那套万字写作约束。
#: 实测把整份写作 prompt 带进工具轮，单次调用要 27.5s 且模型常直接写正文；
#: 瘦上下文（目录 + 本章大纲）只要 1.5s，且会正常调工具。
_TOOL_ROUND_SYSTEM = "你是本书的素材检索助手。"


def collect(
    db: Session,
    project: Project,
    chapter: int,
    *,
    provider: Any | None = None,
    brief_blocks: list[str] | None = None,
    allow_tool_round: bool = True,
    settings: MaterialSettings | None = None,
) -> list[str]:
    """返回应拼进初稿 user prompt 的素材块列表（含原有 brief 块）。

    顺序：先保留 brief 的块，再追加本书素材包块；没有素材时原样返回。
    """
    cfg = settings or load_settings()
    blocks = list(brief_blocks or [])
    if not cfg.enabled:
        return blocks

    block = ""
    try:
        block, ids = pack.build_block(db, project, chapter, settings=cfg)
        if block:
            blocks.append(block)
            _mark_used(project.id, ids)
    except Exception as exc:  # noqa: BLE001 —— 注入失败按无素材继续
        logger.warning("素材包注入失败（已忽略）：%s", exc)

    if cfg.auto_research:
        try:
            from app.db import SessionLocal

            # 独立会话：生成请求的会话里可能还有未提交的读取/状态，不能在这里 commit。
            with SessionLocal() as mdb:
                worker.enqueue_research(
                    mdb, project, chapter, trigger="auto", settings=cfg
                )
        except Exception as exc:  # noqa: BLE001 —— 入队失败不影响正文
            logger.warning("素材调研入队失败（已忽略）：%s", exc)

    if pack.needs_tool_round(db, project, settings=cfg):
        extra: list[str] = []
        if allow_tool_round and provider is not None:
            try:
                extra = tool.gather_pack_blocks(
                    provider, db, project,
                    _TOOL_ROUND_SYSTEM,
                    _tool_round_user(db, project, chapter, block),
                    budget_s=cfg.tool_round_budget_s,
                    max_rounds=cfg.tool_round_max_rounds,
                )
            except Exception as exc:  # noqa: BLE001 —— 工具轮失败按无素材继续
                logger.warning("素材包工具轮失败（已忽略）：%s", exc)
        if not extra:
            # 模型没在工具轮里取到具体素材（不支持 function calling / 直接回了
            # 正文），或本轮根本没开工具（fast_mode）→ 确定性直注 Top-N，
            # 别让本章只剩一份目录。
            extra = _fallback_rows(db, project, chapter, cfg)
        blocks.extend(extra)
    return blocks


def _tool_round_hints(db: Session, project: Project, limit: int = 6) -> list[str]:
    """从目录里取几组**能命中的**检索词（分类 + 用途），给模型当锚点。

    实测模型的自然倾向是拿本章剧情词（旧改/拆迁/产权）去搜，而素材包是
    「城市龙脉 / 宗教文化 / 修真文化」这类主题——6 次查询 5 次空手。
    """
    try:
        from app.materials.research import FICTION_CATEGORY

        toc = store.toc(db, project.id)
        cats = [
            str(c.get("key"))
            for c in (toc.get("categories") or [])
            if c.get("key") and str(c.get("key")) != FICTION_CATEGORY
        ]
        cases = [
            str(u.get("key"))
            for u in (toc.get("use_cases") or [])
            if u.get("key") and len(str(u.get("key"))) >= 4
        ]
    except Exception:  # noqa: BLE001 —— 目录取不到就不给建议词
        return []
    out: list[str] = []
    for word in cats[:4] + cases[:2]:
        if word and word not in out:
            out.append(word)
    return out[:limit]


def _tool_round_user(db: Session, project: Project, chapter: int, toc_block: str) -> str:
    """工具轮的瘦上下文：素材目录 + 建议检索词 + 本章大纲。"""
    parts: list[str] = []
    if toc_block:
        parts.append(toc_block)
    hints = _tool_round_hints(db, project)
    if hints:
        parts.append("【建议检索词（取自目录分类/用途）】" + "、".join(hints))
    try:
        from app.generation.prompts import chapter_outline_entry

        outline = str(chapter_outline_entry(project.outline, chapter) or "").strip()
    except Exception:  # noqa: BLE001 —— 大纲取不到就只给目录
        outline = ""
    if outline:
        parts.append("【本章大纲】\n" + outline)
    parts.append(
        "【检索要求】\n"
        "1. 关键词只从上面的「分类 / 用途 / 建议检索词」里挑，不要用本章剧情里的"
        "专有名词（地名、合同、产权、离婚等）当关键词——素材包里通常没有；\n"
        "2. 先检索；命中后再按 id 取具体内容（get）；最多两轮；\n"
        "3. 两轮都没有命中就回复「无」。只取素材，不写正文。"
    )
    return "\n\n".join(parts)


def _mark_used(project_id: int, ids: list[int]) -> None:
    """独立会话回填使用次数：生成请求的会话里可能有未提交状态，不能复用。"""
    if not ids:
        return
    try:
        from app.db import SessionLocal

        with SessionLocal() as mdb:
            store.mark_used(mdb, ids, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 —— 计数失败不影响注入
        logger.warning("素材包使用次数回填失败（已忽略）：%s", exc)


def _fallback_rows(
    db: Session, project: Project, chapter: int, cfg: MaterialSettings
) -> list[str]:
    """目录模式的兜底：不看阈值直注 Top-N，并计入使用次数。"""
    try:
        block, ids = pack.build_rows_block(db, project, chapter, settings=cfg)
    except Exception as exc:  # noqa: BLE001 —— 兜底失败按无素材继续
        logger.warning("素材包兜底注入失败（已忽略）：%s", exc)
        return []
    if not block:
        return []
    _mark_used(project.id, ids)
    return [block]
