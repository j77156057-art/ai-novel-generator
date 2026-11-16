"""生成侧的章纲回执（S4）：把「这次生成实际拿到的章纲」讲清楚。

与 S2 写入侧的职责分层：

- 写入侧（``routers/chapter_outline.py``）用同一套校验当**闸门**，失败 422 不落库；
- 生成侧只用它做**可见性**：缺纲、空条目、被跳过的检查都原样回执，但
  ``blocking`` 恒为 False —— 已经写到第 43 章的项目不会因为 41–43 没章纲
  突然生成不了。

``prompt_entry`` 直接取 ``chapter_outline_entry`` 的结果，而不是另算一套：
界面说的就是提示词里实际有没有这一章，避免「界面有、提示词没有」。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.generation.chapter_labels import existing_chapters
from app.generation.chapter_outline import (
    chapter_status,
    normalize_outline,
    validate_chapter_range,
)
from app.generation.prompts import chapter_outline_entry
from app.kg.book_scope import prompt_scope_book_ids, scope_sql_values
from app.models import KgEntity, Project

#: 生成侧回执的固定说明。前端原样展示，避免各页面各写一句口径不同的话。
NOTICE = (
    "缺纲只提示、不阻断生成；这里描述的是本次提示词实际收到的章纲。"
    "改章纲不会自动重建节点内容（node_content_stale 需显式重建）。"
)


def written_chapters(db: Session, project_id: int) -> int:
    """已写正文的最大章号；没有正文返回 0。"""
    chapters = existing_chapters(db, project_id)
    return max(chapters) if chapters else 0


def node_windows(project: Project) -> list[dict]:
    """从存档 ``plot_timeline`` 取节点章区间；没有存档就返回空列表。"""
    raw = getattr(project, "plot_timeline", None)
    if not isinstance(raw, dict):
        return []
    nodes = raw.get("nodes")
    if isinstance(nodes, dict):
        items = nodes.items()
    elif isinstance(nodes, list):
        items = ((str(node.get("id") or index), node) for index, node in enumerate(nodes))
    else:
        return []
    windows: list[dict] = []
    for node_id, node in items:
        if not isinstance(node, dict):
            continue
        windows.append({
            "node_id": node.get("id") or node_id,
            "chapter_start": int(node.get("chapter_start") or 0),
            "chapter_end": int(node.get("chapter_end") or 0),
            "title": node.get("title") or "",
            "entry_condition": node.get("entry_condition") or "",
            "exit_condition": node.get("exit_condition") or "",
        })
    return windows


def kg_names(db: Session, project: Project) -> list[str]:
    """本项目 + 当前书白名单内的实体名/别名，供权威物件字面检查。"""
    scope = scope_sql_values(prompt_scope_book_ids(project))
    rows = db.execute(
        select(KgEntity.canonical_name, KgEntity.aliases).where(
            KgEntity.project_id == project.id,
            func.coalesce(KgEntity.novel_id, -1).in_(scope),
        )
    ).all()
    names: list[str] = []
    for canonical, aliases in rows:
        if canonical:
            names.append(str(canonical))
        if isinstance(aliases, (list, tuple)):
            names.extend(str(alias) for alias in aliases if alias)
    return names


def validation_kwargs(db: Session, project: Project) -> dict:
    """校验所需的只读上下文；写入侧与生成侧共用同一份口径。"""
    return {
        "outline": normalize_outline(project.outline),
        "written_chapters": written_chapters(db, project.id),
        "premise": project.premise,
        "bible": project.plot_bible,
        "kg_names": kg_names(db, project),
        "node_windows": node_windows(project),
    }


def _status_for(outline: list[str], written: int, chapter: int) -> str:
    status = chapter_status(outline, chapter)
    # 已写正文却无章纲（历史 41–43 这类）按 missing 报，不按 beyond_end：
    # 「超出章纲范围」会被读成「还不需要章纲」。
    if status == "beyond_end" and written and chapter <= written:
        return "missing"
    return status


def build_notice(
    db: Session,
    project: Project,
    from_chapter: int,
    to_chapter: int | None = None,
) -> dict:
    """构造生成侧回执。``blocking`` 恒为 False —— 只报告，不拦生成。"""
    start = max(1, int(from_chapter))
    end = int(to_chapter) if to_chapter is not None else start
    end = max(start, end)
    outline = normalize_outline(project.outline)
    written = written_chapters(db, project.id)

    chapters: list[dict] = []
    checks_entries: list[dict] = []
    for chapter in range(start, end + 1):
        status = _status_for(outline, written, chapter)
        entry = chapter_outline_entry(project.outline, chapter)
        chapters.append({
            "chapter": chapter,
            "status": status,
            "written": bool(written and chapter <= written),
            "prompt_entry": entry,
        })
        checks_entries.append({"chapter": chapter, "entry": entry})

    missing = [item["chapter"] for item in chapters if item["status"] != "confirmed"]
    report = validate_chapter_range(
        checks_entries,
        from_chapter=start,
        to_chapter=end,
        **validation_kwargs(db, project),
    )
    return {
        "from_chapter": start,
        "to_chapter": end,
        "chapters": chapters,
        "missing_chapters": missing,
        "first_missing_chapter": missing[0] if missing else None,
        "prompt_has_outline": all(item["prompt_entry"] for item in chapters),
        "validation": report,
        "blocking": False,
        "note": NOTICE,
    }
