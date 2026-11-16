"""章纲写入/编辑端点（S2）：区间查看、单章保存、显式缺失、草稿应用、AI 起草。

存储仍是 ``projects.outline``（第 N 章 = 下标 N-1，缺失存空串）。本模块只做
「读 → 校验 → 写」的编排；校验口径全部在 ``app.generation.chapter_outline``，
测试不需要模型、不需要真实库。

两条硬约束：

1. **AI 起草永不直接生效**：``/draft`` 只返回候选与指纹，``/apply`` 必须
   ``confirm=true`` 且指纹与当前项目一致；
2. **不静默**：校验回执固定带 ``checks`` / ``skipped_checks``；缺章纲、
   时间标记缺失、权威为空都会出现在返回里，界面可直接展示。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.generation.chapter_outline import (
    chapter_status,
    normalize_outline,
    outline_after_entries,
    validate_chapter_range,
    validate_single_chapter,
)
from app.generation.outline_notice import (
    node_windows as _node_windows,
    validation_kwargs as _validation_kwargs,
    written_chapters as _written_chapters,
)
from app.generation.planning import model_json, source_fingerprint
from app.llm.provider import LocalFallbackLLMProvider
from app.models import Project
from app.routers.projects import _model_call, _provider_or_none
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/projects", tags=["chapter-outline"])

#: 一次 AI 起草最多覆盖多少章。超过就让人拆批，避免一次提示塞进整本书。
MAX_DRAFT_CHAPTERS = 20

#: 起草端「没有真渠道」的拒绝文案：说清原因（无 key / 渠道不可用）和去哪配。
#: 不写死具体渠道名——`llm_provider` 可能被配置成任意值，原因统一是「取不到可用渠道」。
DRAFT_NO_CHANNEL = (
    "未配置可用的模型渠道：当前没有可用的 API Key，或所选渠道不可用。"
    "请到「模型设置」页选择渠道并填写 API Key 后重试；"
    "本地模板兜底只用于离线开发，不会用来起草章纲。"
)


class ChapterOutlineEntryIn(StrictRequest):
    """区间保存里的单章条目；``entry=None`` 表示显式缺省。"""

    chapter: int
    entry: str | None = None


class ChapterOutlinePut(StrictRequest):
    """单章保存。``confirm`` 缺省 False：必须显式确认才写。"""

    entry: str
    expected_fingerprint: str
    confirm: bool = False


class ChapterOutlineApply(StrictRequest):
    """区间保存 / 应用草稿。"""

    from_chapter: int
    to_chapter: int
    entries: list[ChapterOutlineEntryIn]
    expected_fingerprint: str
    confirm: bool = False
    #: True 才清空 plot_timeline（会丢 done/early_done）；默认不动。
    reset_timeline: bool = False


class ChapterOutlineDraft(StrictRequest):
    """AI 起草请求；只返回候选，不落库。"""

    from_chapter: int
    to_chapter: int
    direction: str | None = None


# ── 读取辅助 ────────────────────────────────────────────────────────────


def _project_or_404(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return proj


# 读取辅助（已写章数 / 节点区间 / KG 名 / 校验上下文）统一在
# app.generation.outline_notice，生成侧回执与本模块共用同一份口径。


def _raise_invalid(report: dict) -> None:
    """校验失败 → 422；整批不落库，错误逐条带回。"""
    raise HTTPException(
        status_code=422,
        detail={"error": "outline_invalid", "report": report},
    )


def _check_confirm(confirm: bool) -> None:
    if confirm is not True:
        raise HTTPException(
            status_code=409,
            detail={"error": "confirm_required",
                    "message": "写入章纲必须显式 confirm=true"},
        )


def _check_fingerprint(project: Project, expected: str) -> None:
    current = source_fingerprint(project)
    if expected != current:
        raise HTTPException(
            status_code=409,
            detail={"error": "stale", "expected": current,
                    "message": "项目设定/章纲/时间线已变化，请重新读取后再保存"},
        )


def _missing_receipt(chapter: int) -> dict:
    """删除（显式标记缺失）的回执：操作成功，但逐项说明哪些检查因此跳过。"""
    return {
        "ok": True,
        "errors": [],
        "warnings": [],
        "checks": [{"rule": "outline_coverage", "status": "checked",
                    "detail": f"第 {chapter} 章已显式标记为缺失（空条目）"}],
        "skipped_checks": ["authority_terms:no_entry", "time_window:no_entry"],
    }


def _timeline_impact(before: list[str], after: list[str], reset: bool) -> dict:
    return {
        "outline_len_before": len(before),
        "outline_len_after": len(after),
        "chapter_ranges_recomputed": len(before) != len(after),
        "node_content_stale": before != after,
        "reset_timeline": reset,
        "note": (
            "节点章区间会按新大纲长度重算；节点标题/entry/exit 仍来自旧章纲。"
            "需要重规划请显式 reset_timeline=true（会清空已完成进度）。"
        ),
    }


# ── 端点 ────────────────────────────────────────────────────────────────


@router.get("/{project_id}/chapter-outline")
def list_chapter_outline(
    project_id: int,
    from_chapter: int = 1,
    to_chapter: int | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """按区间批量查看；缺失章显式返回 ``status="missing"`` 与 ``entry=null``。"""
    project = _project_or_404(db, project_id)
    if from_chapter < 1:
        raise HTTPException(status_code=422, detail="from_chapter 必须 >= 1")
    outline = normalize_outline(project.outline)
    written = _written_chapters(db, project_id)
    end = to_chapter if to_chapter is not None else max(len(outline), written, from_chapter)
    if end < from_chapter:
        raise HTTPException(status_code=422, detail="to_chapter 不能小于 from_chapter")
    windows = _node_windows(project)

    entries: list[dict] = []
    for chapter in range(from_chapter, end + 1):
        status = chapter_status(outline, chapter)
        # 已写正文却没章纲的章（历史 41–43 这类）按 missing 报，不按 beyond_end：
        # 「超出章纲范围」会被读成「还不需要章纲」，而这几章是确实缺了。
        if status == "beyond_end" and written and chapter <= written:
            status = "missing"
        node = next(
            (n for n in windows
             if n["chapter_start"] <= chapter <= n["chapter_end"]),
            None,
        )
        entries.append({
            "chapter": chapter,
            "entry": outline[chapter - 1] if status == "confirmed" else None,
            "status": status,
            "written": bool(written and chapter <= written),
            "node_id": node["node_id"] if node else None,
            "node_range": [node["chapter_start"], node["chapter_end"]] if node else None,
        })

    missing = [item["chapter"] for item in entries if item["status"] != "confirmed"]
    skipped: list[str] = []
    checks: list[dict] = [{
        "rule": "outline_coverage",
        "status": "failed" if missing else "checked",
        "detail": (f"第{'、'.join(str(c) for c in missing)}章无章纲（已显式标记）"
                   if missing else "区间内章纲齐全"),
    }]
    if not windows:
        skipped.append("node_mapping:no_timeline")
        checks.append({"rule": "node_mapping", "status": "skipped",
                       "reason": "no_timeline"})
    else:
        checks.append({"rule": "node_mapping", "status": "checked"})

    return {
        "project_id": project.id,
        "outline_len": len(outline),
        "written_chapters": written,
        "missing_chapters": missing,
        "first_missing_chapter": missing[0] if missing else None,
        "source_fingerprint": source_fingerprint(project),
        "entries": entries,
        "checks": checks,
        "skipped_checks": skipped,
        "warnings": [],
    }


@router.put("/{project_id}/chapter-outline/{chapter}")
def put_chapter_outline(
    project_id: int,
    chapter: int,
    body: ChapterOutlinePut,
    db: Session = Depends(get_db),
) -> dict:
    """编辑单章；写第 44 章时 41–43 的补空由 ``auto_filled_missing`` 显式回执。"""
    project = _project_or_404(db, project_id)
    if chapter < 1:
        raise HTTPException(status_code=422, detail="chapter 必须 >= 1")
    _check_confirm(body.confirm)
    _check_fingerprint(project, body.expected_fingerprint)

    before = normalize_outline(project.outline)
    report = validate_single_chapter(chapter, body.entry, **_validation_kwargs(db, project))
    if not report["ok"]:
        _raise_invalid(report)

    result = outline_after_entries(before, [{"chapter": chapter, "entry": body.entry}])
    project.outline = result["outline"]
    db.commit()
    db.refresh(project)

    after = normalize_outline(project.outline)
    return {
        "project_id": project.id,
        "entry": {
            "chapter": chapter,
            "entry": after[chapter - 1],
            "status": chapter_status(after, chapter),
        },
        "auto_filled_missing": result["auto_filled_missing"],
        "changed": result["changed"],
        "validation": report,
        "timeline_impact": _timeline_impact(before, after, reset=False),
        "source_fingerprint": source_fingerprint(project),
    }


@router.delete("/{project_id}/chapter-outline/{chapter}")
def delete_chapter_outline(
    project_id: int,
    chapter: int,
    expected_fingerprint: str = Query(...),
    db: Session = Depends(get_db),
) -> dict:
    """显式把某章标记为缺失（写空串）；这是「允许缺省」的唯一入口。"""
    project = _project_or_404(db, project_id)
    if chapter < 1:
        raise HTTPException(status_code=422, detail="chapter 必须 >= 1")
    _check_fingerprint(project, expected_fingerprint)

    before = normalize_outline(project.outline)
    if chapter > len(before):
        raise HTTPException(
            status_code=422,
            detail=f"第 {chapter} 章不在章纲范围内（当前 {len(before)} 章）",
        )
    report = _missing_receipt(chapter)
    result = outline_after_entries(before, [{"chapter": chapter, "entry": None}])
    project.outline = result["outline"]
    db.commit()
    db.refresh(project)

    after = normalize_outline(project.outline)
    return {
        "project_id": project.id,
        "entry": {"chapter": chapter, "entry": None, "status": "missing"},
        "changed": result["changed"],
        "validation": report,
        "timeline_impact": _timeline_impact(before, after, reset=False),
        "source_fingerprint": source_fingerprint(project),
    }


@router.post("/{project_id}/chapter-outline/apply")
def apply_chapter_outline(
    project_id: int,
    body: ChapterOutlineApply,
    db: Session = Depends(get_db),
) -> dict:
    """区间保存 / 应用草稿；必须 confirm 且指纹一致，校验失败整批不落库。"""
    project = _project_or_404(db, project_id)
    _check_confirm(body.confirm)
    _check_fingerprint(project, body.expected_fingerprint)

    before = normalize_outline(project.outline)
    entries = [item.model_dump() for item in body.entries]
    report = validate_chapter_range(
        entries,
        from_chapter=body.from_chapter,
        to_chapter=body.to_chapter,
        **_validation_kwargs(db, project),
    )
    if not report["ok"]:
        _raise_invalid(report)

    result = outline_after_entries(before, entries)
    project.outline = result["outline"]
    if body.reset_timeline:
        project.plot_timeline = None
    db.commit()
    db.refresh(project)

    after = normalize_outline(project.outline)
    return {
        "project_id": project.id,
        "from_chapter": body.from_chapter,
        "to_chapter": body.to_chapter,
        "auto_filled_missing": result["auto_filled_missing"],
        "changed": result["changed"],
        "validation": report,
        "timeline_impact": _timeline_impact(before, after, reset=body.reset_timeline),
        "source_fingerprint": source_fingerprint(project),
    }


_DRAFT_SYSTEM = """你是小说章纲编辑。为指定章节区间各写一条章纲，每条以
「第N章《标题》：事件」开头，事件要具体、可判定。
只使用给定 premise、剧情圣经、前文章纲里已经出现的人物、物件、地点；
不得新造关键物件。时间必须相对前一章向前推进，不得倒退（需要跨天时写「次日/第二天」）。
不写结局，不提前使用后续章节的材料。
严格只输出 JSON：{"entries": ["第N章《标题》：…", …]}，条数必须等于要求。"""


def _parse_draft(raw: str, count: int) -> list[str]:
    data = json.loads(raw)
    items = data.get("entries") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("模型输出不是 entries 数组")
    if len(items) != count:
        raise ValueError(f"要求 {count} 条，模型给了 {len(items)} 条")
    out: list[str] = []
    for item in items:
        text = item.get("entry") if isinstance(item, dict) else item
        if not isinstance(text, str) or not text.strip():
            raise ValueError("存在空条目")
        out.append(text.strip())
    return out


@router.post("/{project_id}/chapter-outline/draft")
def draft_chapter_outline(
    project_id: int,
    body: ChapterOutlineDraft,
    db: Session = Depends(get_db),
) -> dict:
    """AI 起草一版章纲；**不写库**，返回草稿、指纹与预校验结果。"""
    project = _project_or_404(db, project_id)
    if body.from_chapter < 1 or body.to_chapter < body.from_chapter:
        raise HTTPException(status_code=422, detail="from/to 章号不合法")
    count = body.to_chapter - body.from_chapter + 1
    if count > MAX_DRAFT_CHAPTERS:
        raise HTTPException(
            status_code=422,
            detail=f"一次最多起草 {MAX_DRAFT_CHAPTERS} 章，当前 {count} 章",
        )

    provider = _provider_or_none()
    # `get_llm_provider()` 在没配 key 时不会返回 None——它兜底
    # `LocalFallbackLLMProvider`（确定性模板）。模板能回 JSON，但不是章纲，
    # 放它进来等于「界面看着起草成功、实际是占位文案」。所以两种都按不可用拒绝。
    if provider is None or isinstance(provider, LocalFallbackLLMProvider):
        raise HTTPException(status_code=503, detail=DRAFT_NO_CHANNEL)

    outline = normalize_outline(project.outline)
    prior_start = max(1, body.from_chapter - 30)
    prior_lines = [
        f"第{index}章：{outline[index - 1]}"
        for index in range(prior_start, body.from_chapter)
        if index <= len(outline) and outline[index - 1].strip()
    ]
    bible_text = json.dumps(project.plot_bible or {}, ensure_ascii=False)[:5000]
    user = "\n".join([
        f"作品前提：{project.premise or '（未填写）'}",
        f"剧情圣经：{bible_text}",
        "前文章纲：",
        "\n".join(prior_lines) or "（无）",
        f"请写第{body.from_chapter}–{body.to_chapter}章，共 {count} 条。",
        f"方向：{body.direction.strip()}" if body.direction and body.direction.strip() else "方向：顺承前文。",
    ])

    def call():
        return model_json(provider, _DRAFT_SYSTEM, user,
                          lambda raw: _parse_draft(raw, count), attempts=3)

    entries, failures = _model_call("起草章纲", call)
    if entries is None:
        raise HTTPException(
            status_code=502,
            detail={"error": "draft_failed", "attempts": failures},
        )

    draft_entries = [
        {"chapter": body.from_chapter + index, "entry": text}
        for index, text in enumerate(entries)
    ]
    report = validate_chapter_range(
        draft_entries,
        from_chapter=body.from_chapter,
        to_chapter=body.to_chapter,
        **_validation_kwargs(db, project),
    )
    return {
        "project_id": project.id,
        "status": "draft",
        "from_chapter": body.from_chapter,
        "to_chapter": body.to_chapter,
        "entries": draft_entries,
        "validation": report,
        "apply_requires_confirm": True,
        "source_fingerprint": source_fingerprint(project),
    }
