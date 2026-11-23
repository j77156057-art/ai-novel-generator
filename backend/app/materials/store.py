# -*- coding: utf-8 -*-
"""素材包读写：去重入库、目录聚合、筛选分页、使用次数回填。

素材按 `(project_id, fingerprint)` 去重；`fingerprint` 由标题 + URL/摘要归一后
哈希得到，因此同一条网页被不同查询重复抓到只保留首次。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select, update as sa_update
from sqlalchemy.orm import Session

from app.models import MaterialResearchJob, ProjectMaterial

_WS_RE = re.compile(r"\s+")
#: 强日期：至少精确到月，才当作「渠道发布时间」证据。
#: 只有四位年份的 `published_at` 可能是旧规则从正文推出来的，回填时不能反证自己。
_STRONG_DATE_RE = re.compile(r"(?:19|20)\d{2}\s*[-/.年]\s*\d{1,2}")

#: 站点后缀：「 - 知乎 / _百度百科 / | 搜狐」这类尾巴。
#: 同一篇文章经不同站点/跳转链进来时会带不同后缀，标题主体其实相同。
_SITE_SUFFIX_RE = re.compile(
    r"\s*[-—_|]\s*(?:知乎|百度|搜狐|今日头条|起点|创世|番茄|网易|新浪|简书|"
    r"微信|qq|豆瓣|豆瓣阅读).*$",
    re.IGNORECASE,
)

#: 标题去重键的最短有效长度：太短的通用标题（如「建筑风水禁忌」）不据此合并，
#: 否则会把不同来源的同名文章误删。
TITLE_KEY_MIN_CHARS = 10


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text(text: str | None) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()


def title_key(title: str | None) -> str:
    """标题去重键：去空白、去站点后缀、小写；太短的通用标题返回空串。

    用于挡住「同一篇文章换了跳转 URL」的重复：百度 `link?url=` 每次都不同，
    fingerprint 必然不同，但标题主体相同。
    """
    text = normalize_text(title).lower()
    if not text:
        return ""
    text = _SITE_SUFFIX_RE.sub("", text).strip()
    if len(text) < TITLE_KEY_MIN_CHARS:
        return ""
    return text[:60]


def fingerprint(title: str, url: str = "", snippet: str = "") -> str:
    """项目内去重键：优先 URL，其次摘要；标题始终参与。"""
    raw = "\x1f".join(
        [
            normalize_text(title).lower(),
            normalize_text(url).lower(),
            normalize_text(snippet).lower()[:400],
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def to_dict(row: ProjectMaterial) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "job_id": row.job_id,
        "origin": row.origin,
        "title": row.title,
        "url": row.url or "",
        "snippet": row.snippet or "",
        "summary": row.summary or "",
        "use_case": row.use_case or "",
        "chapter_hint": row.chapter_hint,
        "category": row.category or "",
        "published_year": row.published_year,
        "published_at": row.published_at or "",
        "provider": row.provider or "",
        "query": row.query or "",
        "tags": row.tags or [],
        "status": row.status,
        "consolidated_by": row.consolidated_by or "",
        "use_count": int(row.use_count or 0),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def job_to_dict(row: MaterialResearchJob) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "chapter_no": row.chapter_no,
        "trigger": row.trigger,
        "topics": row.topics or [],
        "queries": row.queries or [],
        "status": row.status,
        "target_raw": row.target_raw,
        "target_usable": row.target_usable,
        "raw_count": row.raw_count,
        "usable_count": row.usable_count,
        "zero_streak": row.zero_streak,
        "attempts": row.attempts,
        "error": row.error or "",
        "meta": row.meta or {},
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #

def add_many(
    db: Session,
    project_id: int,
    items: Iterable[dict[str, Any]],
    *,
    job_id: int | None = None,
    status: str = "raw",
    origin: str = "research",
) -> list[ProjectMaterial]:
    """按 fingerprint 去重后批量入库；返回实际新增的行。"""
    existing = {
        row
        for row in db.scalars(
            select(ProjectMaterial.fingerprint).where(
                ProjectMaterial.project_id == project_id
            )
        ).all()
    }
    # 第二道去重：标题主体相同（站点后缀已去掉）就当同一条——百度跳转链每次
    # 不同，只靠 fingerprint 会让同一篇文章重复入库。短通用标题由 title_key
    # 返回空串，不参与这一道。
    existing_titles = {
        k
        for k in (
            title_key(t)
            for t in db.scalars(
                select(ProjectMaterial.title).where(
                    ProjectMaterial.project_id == project_id
                )
            ).all()
        )
        if k
    }
    added: list[ProjectMaterial] = []
    for item in items:
        title = normalize_text(item.get("title"))
        snippet = normalize_text(item.get("snippet"))
        url = normalize_text(item.get("url"))
        if not title and not snippet:
            continue
        key = fingerprint(title, url, snippet)
        if key in existing:
            continue
        tkey = title_key(title)
        if tkey and tkey in existing_titles:
            continue
        existing.add(key)
        if tkey:
            existing_titles.add(tkey)
        year = _as_int_or_none(item.get("published_year"))
        year_text = normalize_text(item.get("published_at"))
        if year is None and not year_text:
            # 调研链路已解析过年份；手动添加的素材也从句子里补一次，
            # 避免用户明明写了年份、目录里却显示「未知」。
            from app.materials.research import find_year

            year, year_text = find_year(title, snippet, url)
        row = ProjectMaterial(
            project_id=project_id,
            job_id=job_id,
            origin=origin,
            title=(title or snippet[:80] or "未命名素材")[:500],
            url=url or None,
            snippet=snippet or None,
            summary=normalize_text(item.get("summary")) or None,
            use_case=normalize_text(item.get("use_case")) or None,
            chapter_hint=_as_int_or_none(item.get("chapter_hint")),
            category=normalize_text(item.get("category")) or None,
            published_year=year,
            published_at=year_text or None,
            provider=normalize_text(item.get("provider")) or None,
            query=normalize_text(item.get("query")) or None,
            tags=item.get("tags") or None,
            status=status,
            consolidated_by=item.get("consolidated_by") or None,
            fingerprint=key,
        )
        db.add(row)
        added.append(row)
    if added:
        db.commit()
    return added


def update_material(db: Session, project_id: int, material_id: int, fields: dict[str, Any]) -> ProjectMaterial | None:
    row = db.get(ProjectMaterial, material_id)
    if row is None or row.project_id != project_id:
        return None
    allowed = {
        "title", "url", "snippet", "summary", "use_case", "chapter_hint",
        "category", "published_year", "published_at", "status", "tags",
    }
    for key, value in (fields or {}).items():
        if key not in allowed:
            continue
        if key in ("title", "url", "snippet", "summary", "use_case", "category", "published_at"):
            value = normalize_text(value) or None
        if key == "chapter_hint":
            value = _as_int_or_none(value)
        elif key == "published_year":
            # 用户可能输入「2023 年」，这里统一抽成四位年份。
            value = _as_year_or_none(value)
        if key == "status" and value not in ("raw", "usable", "hidden"):
            continue
        setattr(row, key, value)
    row.updated_at = _now()
    db.commit()
    return row


def delete_material(db: Session, project_id: int, material_id: int) -> bool:
    row = db.get(ProjectMaterial, material_id)
    if row is None or row.project_id != project_id:
        return False
    db.delete(row)
    db.commit()
    return True


def mark_used(db: Session, ids: Iterable[int], *, project_id: int | None = None) -> int:
    """占用次数 +1；返回实际命中的行数。"""
    wanted = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not wanted:
        return 0
    stmt = (
        sa_update(ProjectMaterial)
        .where(ProjectMaterial.id.in_(wanted))
        .values(use_count=ProjectMaterial.use_count + 1, last_used_at=_now())
    )
    if project_id is not None:
        stmt = stmt.where(ProjectMaterial.project_id == project_id)
    result = db.execute(stmt)
    db.commit()
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# 读取
# --------------------------------------------------------------------------- #

def list_materials(
    db: Session,
    project_id: int,
    *,
    q: str = "",
    status: str | None = None,
    category: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    unknown_year: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    conds = [ProjectMaterial.project_id == project_id]
    text = normalize_text(q)
    if text:
        like = f"%{text}%"
        conds.append(
            or_(
                ProjectMaterial.title.ilike(like),
                ProjectMaterial.summary.ilike(like),
                ProjectMaterial.snippet.ilike(like),
                ProjectMaterial.use_case.ilike(like),
                ProjectMaterial.category.ilike(like),
            )
        )
    if status:
        conds.append(ProjectMaterial.status == status)
    if category:
        if category == "__none__":
            conds.append(ProjectMaterial.category.is_(None))
        else:
            conds.append(ProjectMaterial.category == category)
    if unknown_year:
        conds.append(ProjectMaterial.published_year.is_(None))
    if year_from is not None:
        conds.append(ProjectMaterial.published_year >= int(year_from))
    if year_to is not None:
        conds.append(ProjectMaterial.published_year <= int(year_to))

    total = int(
        db.scalar(select(func.count()).select_from(ProjectMaterial).where(*conds)) or 0
    )
    size = max(1, min(int(page_size or 20), 200))
    page_no = max(1, int(page or 1))
    rows = db.scalars(
        select(ProjectMaterial)
        .where(*conds)
        .order_by(ProjectMaterial.created_at.desc(), ProjectMaterial.id.desc())
        .offset((page_no - 1) * size)
        .limit(size)
    ).all()
    return {
        "items": [to_dict(r) for r in rows],
        "total": total,
        "page": page_no,
        "page_size": size,
    }


def search(
    db: Session,
    project_id: int,
    q: str,
    *,
    k: int = 8,
    category: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    status: str = "usable",
) -> list[ProjectMaterial]:
    conds = [ProjectMaterial.project_id == project_id, ProjectMaterial.status == status]
    text = normalize_text(q)
    if text:
        like = f"%{text}%"
        conds.append(
            or_(
                ProjectMaterial.title.ilike(like),
                ProjectMaterial.summary.ilike(like),
                ProjectMaterial.snippet.ilike(like),
                ProjectMaterial.use_case.ilike(like),
                ProjectMaterial.category.ilike(like),
            )
        )
    if category:
        conds.append(ProjectMaterial.category == category)
    if year_from is not None:
        conds.append(ProjectMaterial.published_year >= int(year_from))
    if year_to is not None:
        conds.append(ProjectMaterial.published_year <= int(year_to))
    limit = max(1, min(int(k or 8), 30))
    return list(
        db.scalars(
            select(ProjectMaterial)
            .where(*conds)
            .order_by(ProjectMaterial.use_count.asc(), ProjectMaterial.id.desc())
            .limit(limit)
        ).all()
    )


def get_by_ids(db: Session, project_id: int, ids: Iterable[int]) -> list[ProjectMaterial]:
    wanted = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
    if not wanted:
        return []
    rows = db.scalars(
        select(ProjectMaterial).where(
            ProjectMaterial.project_id == project_id,
            ProjectMaterial.id.in_(wanted),
        )
    ).all()
    by_id = {r.id: r for r in rows}
    return [by_id[i] for i in wanted if i in by_id]


def usable_rows(db: Session, project_id: int, limit: int = 500) -> list[ProjectMaterial]:
    """确定性注入的候选池：按最近入库排序取一批，再由 pack 打分。"""
    return list(
        db.scalars(
            select(ProjectMaterial)
            .where(
                ProjectMaterial.project_id == project_id,
                ProjectMaterial.status == "usable",
            )
            .order_by(ProjectMaterial.created_at.desc(), ProjectMaterial.id.desc())
            .limit(max(1, int(limit)))
        ).all()
    )


def count_usable(db: Session, project_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ProjectMaterial)
            .where(
                ProjectMaterial.project_id == project_id,
                ProjectMaterial.status == "usable",
            )
        )
        or 0
    )


def count_raw(db: Session, project_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ProjectMaterial)
            .where(
                ProjectMaterial.project_id == project_id,
                ProjectMaterial.status == "raw",
            )
        )
        or 0
    )


def stats(db: Session, project_id: int) -> dict[str, Any]:
    rows = db.execute(
        select(ProjectMaterial.status, func.count())
        .where(ProjectMaterial.project_id == project_id)
        .group_by(ProjectMaterial.status)
    ).all()
    by_status = {str(k): int(v) for k, v in rows}
    unknown_years = int(
        db.scalar(
            select(func.count())
            .select_from(ProjectMaterial)
            .where(
                ProjectMaterial.project_id == project_id,
                ProjectMaterial.published_year.is_(None),
            )
        )
        or 0
    )
    used = int(
        db.scalar(
            select(func.count())
            .select_from(ProjectMaterial)
            .where(
                ProjectMaterial.project_id == project_id,
                ProjectMaterial.use_count > 0,
            )
        )
        or 0
    )
    return {
        "total": sum(by_status.values()),
        "usable": by_status.get("usable", 0),
        "raw": by_status.get("raw", 0),
        "hidden": by_status.get("hidden", 0),
        "unknown_year": unknown_years,
        "used": used,
    }


def toc(db: Session, project_id: int) -> dict[str, Any]:
    """目录：分类 / 年份 / 用途的计数与前若干标题，供超大素材包按需浏览。"""
    base = [ProjectMaterial.project_id == project_id, ProjectMaterial.status == "usable"]

    def _groups(column, limit: int) -> list[dict[str, Any]]:
        rows = db.execute(
            select(column, func.count())
            .where(*base)
            .group_by(column)
            .order_by(func.count().desc())
            .limit(limit)
        ).all()
        return [
            {"key": ("" if key is None else str(key)), "count": int(count)}
            for key, count in rows
        ]

    categories = _groups(ProjectMaterial.category, 60)
    years = _groups(ProjectMaterial.published_year, 30)
    years.sort(key=lambda x: (x["key"] == "", x["key"]), reverse=False)
    use_cases = _groups(ProjectMaterial.use_case, 30)

    # 大目录：每个分类下列出若干条目标题，前端可展开浏览；同类小说参考也在其中。
    directory: list[dict[str, Any]] = []
    for item in categories:
        key = item["key"]
        condition = (
            ProjectMaterial.category.is_(None)
            if key == ""
            else ProjectMaterial.category == key
        )
        rows = db.scalars(
            select(ProjectMaterial)
            .where(*base, condition)
            .order_by(ProjectMaterial.id.desc())
            .limit(12)
        ).all()
        directory.append(
            {
                "category": key or "未分类",
                "count": item["count"],
                "items": [
                    {
                        "id": r.id,
                        "title": r.title,
                        "published_year": r.published_year,
                        "use_case": r.use_case or "",
                    }
                    for r in rows
                ],
            }
        )

    recent = db.scalars(
        select(ProjectMaterial)
        .where(*base)
        .order_by(ProjectMaterial.created_at.desc(), ProjectMaterial.id.desc())
        .limit(20)
    ).all()
    return {
        "stats": stats(db, project_id),
        "categories": categories,
        "years": years,
        "use_cases": use_cases,
        "directory": directory,
        "recent": [{"id": r.id, "title": r.title, "category": r.category or ""} for r in recent],
    }


# --------------------------------------------------------------------------- #
# 任务查询
# --------------------------------------------------------------------------- #

def active_jobs(db: Session, project_id: int) -> list[MaterialResearchJob]:
    return list(
        db.scalars(
            select(MaterialResearchJob)
            .where(
                MaterialResearchJob.project_id == project_id,
                MaterialResearchJob.status.in_(["queued", "running", "consolidating"]),
            )
            .order_by(MaterialResearchJob.id.desc())
        ).all()
    )


def list_jobs(db: Session, project_id: int, limit: int = 20) -> list[MaterialResearchJob]:
    return list(
        db.scalars(
            select(MaterialResearchJob)
            .where(MaterialResearchJob.project_id == project_id)
            .order_by(MaterialResearchJob.id.desc())
            .limit(max(1, min(int(limit or 20), 100)))
        ).all()
    )


def _as_int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_year_or_none(value: Any) -> int | None:
    """把「2023 / 2023 年 / 2023-05」统一成 2023；无年份返回 None。"""
    if value is None or value == "":
        return None
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1950 <= year <= 2100 else None


def backfill_years(
    db: Session, project_id: int, *, include_manual: bool = False
) -> dict[str, int]:
    """按新的年份解析规则重算存量素材。

    只处理非手动素材（手动的年份是用户意图，不自动改）。无法从原文/URL/发布时间
    找到证据的旧年份会被清掉（对应旧的宽松解析把「2007 年成立」当成素材年份）；
    找到证据的则更新为解析出的年份。
    """
    from app.materials.research import resolve_year

    rows = db.scalars(
        select(ProjectMaterial).where(ProjectMaterial.project_id == project_id)
    ).all()
    scanned = updated = cleared = 0
    for row in rows:
        if row.origin == "manual" and not include_manual:
            continue
        scanned += 1
        raw_at = row.published_at or ""
        # 只有「2023-05 / 2023年5月」这类强日期才当作发布时间证据；
        # 裸年份可能是旧规则自己写的，拿它回填等于自证。
        trusted_at = raw_at if _STRONG_DATE_RE.search(raw_at) else ""
        year, text, evidence = resolve_year(
            row.title or "",
            row.snippet or "",
            row.url or "",
            trusted_at,
        )
        if year is not None:
            if row.published_year != year:
                row.published_year = year
                row.published_at = text or str(year)
                updated += 1
        elif evidence == "none" and row.published_year is not None:
            # 旧规则可能把正文里的历史年份当成素材年份；没有证据就清掉，显示「未知」。
            row.published_year = None
            row.published_at = None
            cleared += 1
    if updated or cleared:
        db.commit()
    return {"scanned": scanned, "updated": updated, "cleared": cleared}
