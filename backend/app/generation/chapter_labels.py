"""章号推导与章标签：平台侧唯一的「第N章」生产者。

背景（2026-11-15）：在此之前只有 `D:\\Temp` 下的临时脚本传 `chapter_range`，
平台四个生成入口都不传，引擎在 `_commit_segment` 里兜底写 `段{segment_no}`，
于是平台永远停在段模式——章纲注入、结构检测、节点对齐、跨章锚点全部关闭。

边界口径（用户 2026-11-15 定）：
- 入参形如 `"44"` 或 `"44-46"`；不传 → 库内最大章号 +1（库里没有章号则为 1）。
- 章号 < 1、区间倒序、或与库内已有章号冲突（调用方未声明重写）→ 抛
  `ChapterRangeError`，由路由翻成 400。**不静默改数**：静默改成 45 会让用户以为
  写的是 44，而重复的 `第44章` 会直接毁掉按章号的趋势与台账。
- 标题取 `project.outline[章号-1]` 的《…》；取不到就只写 `第N章`，
  由 `title_meta` 记 `title_source="missing_outline"`，不悄悄变成「无标题章」。

两套编号的隔离：
- `segment_no` 仍是行序主键（`段N` 只出现在没有章号的段上），章号只从
  `第N章` 标签解析；两者**不互相换算**，段序也不会被章号影响。
- 章号推导只读 `第N章` 标签，因此同一项目里已有 `段1–段61` 不影响下一章是 1。
- 平台写作时拒绝重复章号，避免同一 `第N章` 出现两次（`chapter_index` 取最大值，
  重复会让后续推导原地打转）。

唯一不推导的入口（用户 2026-11-15 采纳）：
- `/api/regenerate-stream` 不传 `chapter_range` 时**保留目标段原标签**，不推导新章号。
  理由：重写不产生新内容，而「本次章号 = 库内最大章号 + 1」会把一个旧段悄悄挪到
  未来章节上（例如重写段 5 变成第 44 章），既改数又破坏按章号的趋势与台账。
  传了则以传入为准，并允许指向已存在章（重写本身就是声明重写）。
  其余三个前向写作入口（`/generate`、`/generate-stream`、`/generate-loop`）缺省即推导。
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.coherence import chapter_index
from app.models import Generation, Project

#: 入参：`44` / `44-46`（半角、全角、波浪线都收，但不收「第44章」这类标签）。
_REQUESTED_RE = re.compile(r"^\s*(\d{1,4})\s*(?:[-–—~至]\s*(\d{1,4}))?\s*$")
#: 标签里的章号区间：`第44章` / `第44–46章`。
_LABEL_RE = re.compile(r"第\s*(\d{1,4})\s*(?:[-–—~]\s*(\d{1,4}))?\s*章")
#: 条目标题：`第41章《到期》：……`。
_TITLE_RE = re.compile(r"《([^》]{1,40})》")


class ChapterRangeError(ValueError):
    """章号入参不合法，或与库内已有章号冲突。路由负责翻成 400。"""


def chapter_number(label: str | None) -> int | None:
    """从标签取起始章号（复用 `coherence.chapter_index` 的解析口径）。"""
    return chapter_index(label or "")


def label_span(label: str | None) -> tuple[int, int] | None:
    """从标签取 `(起, 止)`；`第44章` 返回 `(44, 44)`，`第44–46章` 返回 `(44, 46)`。"""
    m = _LABEL_RE.search(str(label or ""))
    if not m:
        return None
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    return start, end


def parse_requested(raw: str | None) -> tuple[int, int] | None:
    """解析入参；`None`/空串返回 `None`（表示交给推导）。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    m = _REQUESTED_RE.match(text)
    if not m:
        raise ChapterRangeError(
            f"chapter_range 需形如 '44' 或 '44-46'，收到 {raw!r}")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else start
    if start < 1:
        raise ChapterRangeError(f"chapter_range 的章号必须 ≥ 1，收到 {raw!r}")
    if end < start:
        raise ChapterRangeError(f"chapter_range 区间倒序，收到 {raw!r}")
    return start, end


def existing_chapters(db, project_id: int) -> list[int]:
    """库内已出现的章号（含区间末章）。段标签不计入。"""
    labels = db.scalars(
        select(Generation.chapter_range).where(Generation.project_id == project_id)
    ).all()
    nums: set[int] = set()
    for label in labels:
        span = label_span(label)
        if span is None:
            continue
        nums.update(range(span[0], span[1] + 1))
    return sorted(nums)


def next_chapter_number(db, project_id: int) -> int:
    nums = existing_chapters(db, project_id)
    return (nums[-1] + 1) if nums else 1


def outline_title(outline, chapter: int) -> str | None:
    entries = outline if isinstance(outline, list) else []
    if not (1 <= chapter <= len(entries)):
        return None
    m = _TITLE_RE.search(str(entries[chapter - 1] or ""))
    return m.group(1).strip() if m else None


def build_label(start: int, end: int, title: str | None) -> str:
    """章标签。区间（`end > start`）**不挂标题**：一段覆盖多章时，
    只挂起章标题会把「第44–46章《某标题》」读成三章同名。"""
    if end > start:
        return f"第{start}–{end}章"
    return f"第{start}章《{title}》" if title else f"第{start}章"


def title_meta(project: Project, start: int, end: int) -> dict:
    title = outline_title(getattr(project, "outline", None), start)
    return {
        "chapter": start,
        "chapter_end": end,
        "title": title,
        "title_source": "outline" if title else "missing_outline",
    }


def resolve(
    db,
    project: Project,
    raw: str | None,
    *,
    allow_existing: bool = False,
) -> dict:
    """把入参收敛成一个可直接落库的章标签。

    返回 `{"label", "chapter", "chapter_end", "title", "title_source",
    "derived", "requested"}`；`derived=True` 表示入参为空、章号是推导出来的。
    """
    requested = parse_requested(raw)
    if requested is None:
        chapter = next_chapter_number(db, project.id)
        start, end, derived = chapter, chapter, True
    else:
        start, end = requested
        derived = False
        if not allow_existing:
            taken = set(existing_chapters(db, project.id))
            clash = [n for n in range(start, end + 1) if n in taken]
            if clash:
                raise ChapterRangeError(
                    "章号已存在：" + "、".join(f"第{n}章" for n in clash)
                    + "（如需重写请走 /regenerate-stream）")
    meta = title_meta(project, start, end)
    return {
        "label": build_label(start, end, meta["title"]),
        **meta,
        "derived": derived,
        "requested": raw,
    }


def resolve_many(
    db,
    project: Project,
    raw: str | None,
    count: int,
) -> list[dict]:
    """连续多段的章标签（`/generate-loop` 用）。

    `count<=1` 时等价于 `resolve`（保留 `44-46` 这种区间标签）；
    `count>1` 时逐段各占一章，区间入参要求 `start+count-1 <= end`。
    """
    count = max(int(count or 1), 1)
    if count == 1:
        return [resolve(db, project, raw)]
    requested = parse_requested(raw)
    if requested is None:
        start = next_chapter_number(db, project.id)
        end = None
    else:
        start, end = requested
        if start + count - 1 > end:
            raise ChapterRangeError(
                f"chapter_range={raw!r} 只覆盖 {end - start + 1} 章，"
                f"但 segments={count}")
    taken = set(existing_chapters(db, project.id))
    out: list[dict] = []
    for i in range(count):
        chapter = start + i
        if chapter in taken:
            raise ChapterRangeError(
                f"章号已存在：第{chapter}章（如需重写请走 /regenerate-stream）")
        meta = title_meta(project, chapter, chapter)
        out.append({
            "label": build_label(chapter, chapter, meta["title"]),
            **meta,
            "derived": requested is None,
            "requested": raw,
        })
    return out


def commit_meta(project: Project, label: str | None, requested: str | None = None) -> dict | None:
    """写进 `quality_json.chapter_meta` 的旁路记录；段标签返回 `None`。

    这是「取不到标题」唯一的可见化出口：`title_source="missing_outline"`
    说明该章在 `project.outline` 里没有条目（或条目里没有《…》）。
    """
    span = label_span(label)
    if span is None:
        return None
    start, end = span
    meta = title_meta(project, start, end)
    meta["label"] = label
    meta["requested"] = requested
    return meta
