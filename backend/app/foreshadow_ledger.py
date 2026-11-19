"""作者侧伏笔台账（意图层）。

## 为什么单独一张表

仓库里已经有两处伏笔数据，都不是「作者可编辑的台账」：

- `plot_bible.foreshadowing` 只是 `string[]`，没有状态、章号、回收说明；
- `kg_foreshadows` / `KgNode(type=plothole)` 是**从正文或参考语料反推的事实**，
  状态机是 planted/developing/paid_off/dropped/expired，作者不需要理解。

所以台账独立建表：作者只面对 `open / resolved / dropped` 三态，事实层保持不动。
提示词只注入 `open` 条目——已回收的不该再被模型当成「待收的线」推进。

## 与剧情圣经旧字段的关系

`plot_bible.foreshadowing` 在合成圣经时仍会写入，作为**导入来源**与旧项目回退：
- 台账为空时，提示词仍走旧字段（`prompts.merged_constraint_items` 的回退）；
- 台账一旦有条目，台账即唯一权威，旧字段只在「导入」时读取一次。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ForeshadowLedger, KgNode, Project

#: 作者侧三态。顺序即展示优先级（未回收在最前）。
STATUSES = ("open", "resolved", "dropped")

#: 中文标签（前端与摘要共用，避免两边各写一份）。
STATUS_LABELS = {"open": "未回收", "resolved": "已回收", "dropped": "已放弃"}

#: 标题与文本上限。
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 500
#: 章号上限（防手滑输入 99999999）。
MAX_CHAPTER = 9999

#: 旧字段/抽取状态的归一映射：抽取状态机 → 作者三态。
_BIBLE_STATUS = {
    "planted": "open",
    "open": "open",
    "developing": "open",
    "resolved": "resolved",
    "paid_off": "resolved",
    "dropped": "dropped",
    "expired": "dropped",
}

_ORDER = {status: index for index, status in enumerate(STATUSES)}


def _text(value: Any, limit: int, field: str) -> str:
    """trim + 类型 + 长度校验。空值返回空串。"""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field}必须是字符串")
    text = value.strip()
    if len(text) > limit:
        raise ValueError(f"{field}不能超过 {limit} 字")
    return text


def _chapter(value: Any, field: str) -> int | None:
    """章号：None / 空串 → None；其余必须 1..MAX_CHAPTER 的整数。"""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是整数") from exc
    if number < 1 or number > MAX_CHAPTER:
        raise ValueError(f"{field}必须在 1..{MAX_CHAPTER} 之间")
    return number


def normalize_entry(
    title: Any,
    *,
    status: Any = "open",
    planted_chapter: Any = None,
    planned_chapter: Any = None,
    resolved_chapter: Any = None,
    evidence: Any = None,
    resolution: Any = None,
) -> dict:
    """校验并归一一条台账（不落库）。非法输入抛 `ValueError`。"""
    name = _text(title, MAX_TITLE_CHARS, "标题")
    if not name:
        raise ValueError("标题不能为空")
    state = str(status or "open").strip()
    if state not in STATUSES:
        raise ValueError(f"状态必须是 {'/'.join(STATUSES)}")
    return {
        "title": name,
        "status": state,
        "planted_chapter": _chapter(planted_chapter, "埋设章"),
        "planned_chapter": _chapter(planned_chapter, "计划回收章"),
        "resolved_chapter": _chapter(resolved_chapter, "回收章"),
        "evidence": _text(evidence, MAX_TEXT_CHARS, "依据"),
        "resolution": _text(resolution, MAX_TEXT_CHARS, "回收说明"),
    }


def entries_of(
    db: Session, project_id: int, statuses: tuple[str, ...] | None = None
) -> list[ForeshadowLedger]:
    """取台账，按「未回收优先 → 计划章 → id」排序。

    `statuses` 传 None 表示全部；传 `("open",)` 时只取未回收（提示词路径）。
    """
    stmt = select(ForeshadowLedger).where(ForeshadowLedger.project_id == project_id)
    if statuses is not None:
        stmt = stmt.where(ForeshadowLedger.status.in_(statuses))
    rows = list(db.scalars(stmt).all())
    rows.sort(
        key=lambda row: (
            _ORDER.get(row.status, len(STATUSES)),
            row.planned_chapter or 9999,
            row.id,
        )
    )
    return rows


def _attr(entry: Any, key: str, default: Any = None) -> Any:
    """同时支持 ORM 行与 dict（测试与纯函数调用方便）。"""
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _one_line(entry: Any) -> str:
    title = str(_attr(entry, "title") or "").strip()
    if not title:
        return ""
    planned = _attr(entry, "planned_chapter")
    return f"{title}（计划第{planned}章回收）" if planned else title


def open_lines(entries: Any) -> list[str]:
    """只取 open 条目，渲染成短句列表（供 constraints / RAG 查询词使用）。"""
    lines: list[str] = []
    for entry in entries or []:
        if _attr(entry, "status") != "open":
            continue
        text = _one_line(entry)
        if text:
            lines.append(text)
    return lines


def render_open_block(entries: Any) -> str:
    """渲染提示词里的一段「未回收伏笔」；空则返回空串（调用方整块不输出）。"""
    lines = open_lines(entries)
    if not lines:
        return ""
    return "未回收伏笔（本节最多推进一条，其余留待后续）：" + "；".join(lines)


def bible_foreshadow_titles(project: Project) -> list[str]:
    """读 `plot_bible.foreshadowing` 的标题（兼容 str 与 {name,status}）。"""
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    raw = bible.get("foreshadowing") or []
    titles: list[str] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            title = item.get("name") or item.get("title")
        elif isinstance(item, str):
            title = item
        else:
            continue
        name = str(title or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        titles.append(name[:MAX_TITLE_CHARS])
    return titles


def legacy_titles(project: Project, entries: list[Any]) -> list[str]:
    """剧情圣经里尚未导入台账的旧伏笔（GET 时告诉前端可导入多少条）。"""
    known = {str(_attr(entry, "title") or "").strip() for entry in entries or []}
    return [title for title in bible_foreshadow_titles(project) if title not in known]


def import_from_bible(db: Session, project: Project) -> dict:
    """把剧情圣经旧伏笔导入台账；按标题去重，只新增不覆盖。

    返回 `{"created": n, "titles": [...]}`。**不 commit**——由路由统一提交，
    与设定卡 `sync_from_project` 的纪律一致。
    """
    bible = project.plot_bible if isinstance(project.plot_bible, dict) else {}
    raw = bible.get("foreshadowing") or []
    if not isinstance(raw, list):
        return {"created": 0, "titles": []}

    existing = {
        row.title
        for row in db.scalars(
            select(ForeshadowLedger).where(ForeshadowLedger.project_id == project.id)
        ).all()
    }
    created: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            title = item.get("name") or item.get("title")
            status = str(item.get("status") or "open")
        elif isinstance(item, str):
            title, status = item, "open"
        else:
            continue
        name = str(title or "").strip()[:MAX_TITLE_CHARS]
        if not name or name in existing:
            continue
        db.add(
            ForeshadowLedger(
                project_id=project.id,
                title=name,
                status=_BIBLE_STATUS.get(status.strip(), "open"),
                source="bible",
            )
        )
        existing.add(name)
        created.append(name)
    return {"created": len(created), "titles": created}


# ======================================================================
# KG 联动：把事实层的未填坑登记进台账 + 已回收坑的回收建议
#
# 方向只有一个：KG → 台账（只读 KG，从不反写）。
# 图谱是「从正文/参考语料反推的事实」，台账是「作者意图」；两者同名即视为
# 同一条线，用名字做桥，不建关联表——作者随时可以改台账标题，改完就自动断开。
# ======================================================================

#: 只读本项目生成后的图谱。参考语料分图（`novel:{id}`）里的坑属于别人的书，
#: 登记进作者台账就是污染（与 `kg.queries.get_reminders` 同一条隔离纪律）。
KG_SOURCE = "generated"


def _norm_question(value: Any) -> str:
    """坑/标题的比较口径：压掉多余空白，其余逐字比较。"""
    return " ".join(str(value or "").split())


def kg_plotholes(db: Session, project_id: int, source: str = KG_SOURCE) -> list[dict]:
    """取图谱里的坑，按问题去重（同一问题多节点时保留最近提出的那条）。

    返回 `[{question, status, first_chapter}]`，按首现段号倒序——最近开的坑
    更可能仍是当前叙事主线，与 `kg.queries.get_reminders` 同口径。
    """
    rows = db.scalars(
        select(KgNode).where(
            KgNode.project_id == project_id,
            KgNode.type == "plothole",
            KgNode.source == source,
        )
    ).all()
    merged: dict[str, dict] = {}
    for node in rows:
        props = node.props if isinstance(node.props, dict) else {}
        question = _norm_question(props.get("question"))
        if not question:
            continue
        current = merged.get(question)
        if current is None or (node.first_chapter or -1) > (current["first_chapter"] or -1):
            merged[question] = {
                "question": question,
                "status": str(props.get("status") or "open"),
                "first_chapter": node.first_chapter,
            }
    return sorted(
        merged.values(), key=lambda item: item["first_chapter"] or -1, reverse=True
    )


def kg_linkage(db: Session, project_id: int, entries: Any) -> dict:
    """算两份联动清单（只读，不落库）：

    - `importable`：KG 里仍 open、台账里还没有的坑（可一键登记）；
    - `resolvable`：台账仍 open、但同名 KG 坑已不是 open 的条目（回收建议）。
    """
    holes = kg_plotholes(db, project_id)
    by_question = {hole["question"]: hole for hole in holes}
    known = {_norm_question(_attr(entry, "title")) for entry in entries or []}
    importable = [
        hole for hole in holes if hole["status"] == "open" and hole["question"] not in known
    ]
    resolvable = []
    for entry in entries or []:
        if _attr(entry, "status") != "open":
            continue
        hole = by_question.get(_norm_question(_attr(entry, "title")))
        if hole is not None and hole["status"] != "open":
            resolvable.append(
                {
                    "entry_id": _attr(entry, "id"),
                    "title": _attr(entry, "title"),
                    "kg_status": hole["status"],
                    "first_chapter": hole["first_chapter"],
                }
            )
    return {"importable": importable, "resolvable": resolvable}


def import_from_kg(db: Session, project_id: int, entries: Any) -> dict:
    """把 KG 里仍 open 的未填坑登记进台账；按标题去重，只新增不覆盖。

    章号刻意留空：KG 的 `first_chapter` 是**段号**（生成段），台账的章号是作者
    口径，两者不是一回事；段号写进「依据」即可，不冒充章号。

    返回 `{"created": n, "titles": [...]}`。**不 commit**——由路由统一提交。
    """
    existing = {_norm_question(_attr(entry, "title")) for entry in entries or []}
    existing |= {
        _norm_question(row.title)
        for row in db.scalars(
            select(ForeshadowLedger).where(ForeshadowLedger.project_id == project_id)
        ).all()
    }
    created: list[str] = []
    for hole in kg_plotholes(db, project_id):
        if hole["status"] != "open":
            continue
        title = hole["question"][:MAX_TITLE_CHARS]
        key = _norm_question(title)
        if not key or key in existing:
            continue
        evidence = (
            f"KG 未填坑，第 {hole['first_chapter']} 段提出"
            if hole["first_chapter"] is not None
            else "KG 未填坑"
        )
        db.add(
            ForeshadowLedger(
                project_id=project_id,
                title=title,
                status="open",
                evidence=evidence,
                source="kg",
            )
        )
        existing.add(key)
        created.append(title)
    return {"created": len(created), "titles": created}
