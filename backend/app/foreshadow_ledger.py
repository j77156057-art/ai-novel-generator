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

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ForeshadowLedger, KgNode, Project

#: 作者侧三态。顺序即展示优先级（未回收在最前）。
STATUSES = ("open", "resolved", "dropped")

#: 中文标签（前端与摘要共用，避免两边各写一份）。
STATUS_LABELS = {"open": "未回收", "resolved": "已回收", "dropped": "已放弃"}

#: 伏笔级别。
#:
#: - `main`：贯穿全书的主线暗线（轮回/系统/局中局的真相），临近结局才揭晓。
#:   注入写作提示时**只给暗示点，不给标题与真相**——标题本身常常就是答案。
#: - `arc`：卷/篇级，几十章内回收；近期只允许侧面呼应。
#: - `chapter`：小节级，几章内埋下并回收。
TIERS = ("main", "arc", "chapter")
TIER_LABELS = {"main": "主线（全书）", "arc": "卷级", "chapter": "小节级"}
_TIER_ORDER = {tier: index for index, tier in enumerate(TIERS)}

#: 每档最多注入几条。主线只有一个（全书一条），卷级/小节级各留 3 条，
#: 避免「一章一堆钩子」和提示词膨胀。
INJECT_LIMIT = {"main": 1, "arc": 3, "chapter": 3}

#: 主线真相分几层揭晓（对应「局中局」的反转层数）。
#: 到达计划揭晓章后，每章只翻一层，避免最后一章一次性把底牌全说完。
MAIN_REVEAL_LAYERS = 3

#: 真相里的分层标记（作者/模型按「第一层反转：…」写）。
_LAYER_MARKERS = ("第一层", "第二层", "第三层", "第四层", "第五层")


def truth_layer(truth: str, layer: int) -> str:
    """从完整真相里取出第 `layer` 层（含前置背景），取不到就返回全文。

    真相通常写成「背景…第一层反转：…第二层反转：…第三层反转：…」。
    逐层揭晓时只把当前层交给模型，避免它看到后续反转后提前说破。
    """
    text = str(truth or "").strip()
    if not text:
        return ""
    positions: list[tuple[int, int]] = []
    for number, marker in enumerate(_LAYER_MARKERS, start=1):
        pos = text.find(marker)
        if pos != -1:
            positions.append((number, pos))
    if len(positions) < 2:
        return text
    positions.sort(key=lambda item: item[1])
    preamble = text[: positions[0][1]].strip(" ，。；;")
    for index, (number, pos) in enumerate(positions):
        if number != layer:
            continue
        end = positions[index + 1][1] if index + 1 < len(positions) else len(text)
        body = text[pos:end].strip(" ，。；;")
        return f"{preamble}；{body}" if preamble else body
    return text


def reveal_window(entry: Any) -> tuple[int | None, int]:
    """算主线的揭晓窗口 `(起始章, 跨度章数)`。

    优先用百分比：`total_chapters` + `reveal_ratio`（+ `reveal_span_ratio`）。
    三者不全时回退到绝对章号 `planned_chapter` / `reveal_span`。
    """
    total = _attr(entry, "total_chapters")
    ratio = _attr(entry, "reveal_ratio")
    span_ratio = _attr(entry, "reveal_span_ratio")
    if total and ratio is not None:
        try:
            total = int(total)
            start = max(1, round(total * float(ratio)))
            span = (
                round(total * float(span_ratio))
                if span_ratio is not None
                else MAIN_REVEAL_LAYERS
            )
            return start, max(MAIN_REVEAL_LAYERS, span)
        except (TypeError, ValueError):
            pass
    span = _attr(entry, "reveal_span") or MAIN_REVEAL_LAYERS
    try:
        span = max(int(span), MAIN_REVEAL_LAYERS)
    except (TypeError, ValueError):
        span = MAIN_REVEAL_LAYERS
    return _attr(entry, "planned_chapter"), span


def reveal_schedule(entry: Any, chapter: int | None) -> tuple[int, bool, bool] | None:
    """算当前章在主线揭晓窗口里的位置。

    返回 `(layer, is_layer_start, finished)`：

    - `layer`：当前应揭到第几层（1..MAIN_REVEAL_LAYERS）；
    - `is_layer_start`：本章是不是这一层的**第一**章（只有这一章注入该层真相，
      其余章只写余波，避免同一层反复说）；
    - `finished`：窗口已走完，可以完整揭晓。

    窗口长度取 `reveal_span`（默认 3 章）。三层反转在窗口内均匀分布：
    跨度 10 章时，三层分别从第 1、5、8 章开始，而不是连续三章各翻一层。
    未到揭晓章返回 None。
    """
    reveal, span = reveal_window(entry)
    if chapter is None or not reveal or chapter < reveal:
        return None
    offset = chapter - reveal
    if offset >= span:
        return MAIN_REVEAL_LAYERS, False, True
    layer = min(MAIN_REVEAL_LAYERS, offset * MAIN_REVEAL_LAYERS // span + 1)
    # 该层第一次出现的偏移：ceil((layer-1) * span / layers)
    layer_start = ((layer - 1) * span + MAIN_REVEAL_LAYERS - 1) // MAIN_REVEAL_LAYERS
    return layer, offset == layer_start, False


#: 标题与文本上限。
MAX_TITLE_CHARS = 200
MAX_TEXT_CHARS = 500
MAX_TRUTH_CHARS = 2000
MAX_HINTS_CHARS = 2000
#: 主线揭晓跨度上限（章）。太小会退化成连续三章各翻一层。
MAX_REVEAL_SPAN = 50
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


def _span(value: Any) -> int | None:
    """揭晓跨度：None / 空串 → None；其余必须 1..MAX_REVEAL_SPAN 的整数。"""
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("揭晓跨度必须是整数") from exc
    if number < 1 or number > MAX_REVEAL_SPAN:
        raise ValueError(f"揭晓跨度必须在 1..{MAX_REVEAL_SPAN} 之间")
    return number


def _ratio(value: Any, field: str) -> float | None:
    """比例：None / 空串 → None；其余必须 0..1 的小数。"""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是 0–1 的小数") from exc
    if number < 0 or number > 1:
        raise ValueError(f"{field}必须在 0–1 之间")
    return number


def normalize_entry(
    title: Any,
    *,
    status: Any = "open",
    tier: Any = "chapter",
    planted_chapter: Any = None,
    planned_chapter: Any = None,
    resolved_chapter: Any = None,
    evidence: Any = None,
    resolution: Any = None,
    truth: Any = None,
    hints: Any = None,
    reveal_span: Any = None,
    total_chapters: Any = None,
    reveal_ratio: Any = None,
    reveal_span_ratio: Any = None,
) -> dict:
    """校验并归一一条台账（不落库）。非法输入抛 `ValueError`。"""
    name = _text(title, MAX_TITLE_CHARS, "标题")
    if not name:
        raise ValueError("标题不能为空")
    state = str(status or "open").strip()
    if state not in STATUSES:
        raise ValueError(f"状态必须是 {'/'.join(STATUSES)}")
    level = str(tier or "chapter").strip()
    if level not in TIERS:
        raise ValueError(f"级别必须是 {'/'.join(TIERS)}")
    return {
        "title": name,
        "status": state,
        "tier": level,
        "planted_chapter": _chapter(planted_chapter, "埋设章"),
        "planned_chapter": _chapter(planned_chapter, "计划回收章"),
        "resolved_chapter": _chapter(resolved_chapter, "回收章"),
        "evidence": _text(evidence, MAX_TEXT_CHARS, "依据"),
        "resolution": _text(resolution, MAX_TEXT_CHARS, "回收说明"),
        "truth": _text(truth, MAX_TRUTH_CHARS, "真相"),
        "hints": _text(hints, MAX_HINTS_CHARS, "暗示点"),
        "reveal_span": _span(reveal_span),
        "total_chapters": _chapter(total_chapters, "全书章数"),
        "reveal_ratio": _ratio(reveal_ratio, "揭晓起点比例"),
        "reveal_span_ratio": _ratio(reveal_span_ratio, "揭晓跨度比例"),
    }


def tier_of(entry: Any) -> str:
    """取条目级别；脏值（老数据 / 手改库）一律按小节级处理。"""
    level = str(_attr(entry, "tier") or "chapter").strip()
    return level if level in TIERS else "chapter"


def hint_lines(entry: Any) -> list[str]:
    """把「暗示点」多行文本拆成逐条短句（换行 / 分号都算分隔）。"""
    raw = str(_attr(entry, "hints") or "")
    parts = [part.strip(" \t·-—") for part in re.split(r"[\n;；]+", raw)]
    return [part for part in parts if part]


def entries_of(
    db: Session, project_id: int, statuses: tuple[str, ...] | None = None
) -> list[ForeshadowLedger]:
    """取台账，按「未回收优先 → 级别 → 计划章 → id」排序。

    `statuses` 传 None 表示全部；传 `("open",)` 时只取未回收（提示词路径）。
    """
    stmt = select(ForeshadowLedger).where(ForeshadowLedger.project_id == project_id)
    if statuses is not None:
        stmt = stmt.where(ForeshadowLedger.status.in_(statuses))
    rows = list(db.scalars(stmt).all())
    rows.sort(
        key=lambda row: (
            _ORDER.get(row.status, len(STATUSES)),
            _TIER_ORDER.get(tier_of(row), len(TIERS)),
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


def open_lines(entries: Any, chapter: int | None = None) -> list[str]:
    """只取 open 条目，按级别渲染成短句列表（供 constraints / RAG 查询词使用）。

    `chapter` 是**当前要写的章号**（拿不到时传 None）：

    - 主线：默认只输出「暗示点」，**绝不输出标题与真相**；到达计划揭晓章后
      才允许把真相写进提示词（否则模型会一路把底牌当普通伏笔，或者提前说破）。
    - 卷级 / 小节级：照常输出标题与计划章，但各档有条数上限。
    """
    buckets: dict[str, list[Any]] = {tier: [] for tier in TIERS}
    for entry in entries or []:
        if _attr(entry, "status") != "open":
            continue
        buckets[tier_of(entry)].append(entry)

    lines: list[str] = []
    for entry in buckets["main"][: INJECT_LIMIT["main"]]:
        truth = str(_attr(entry, "truth") or "").strip()
        schedule = reveal_schedule(entry, chapter) if truth else None
        if schedule is not None:
            layer, is_layer_start, finished = schedule
            if finished:
                lines.append(
                    "主线已揭晓完毕：后续章节不要再重复解释真相，只写它带来的后果与人物选择"
                )
            elif is_layer_start:
                lines.append(
                    f"主线揭晓第 {layer}/{MAIN_REVEAL_LAYERS} 层"
                    f"（本章只揭这一层，按真相里的反转顺序推进，不要提前说破后续）："
                    f"{truth_layer(truth, layer)}"
                )
            else:
                lines.append(
                    f"主线揭晓进行中（第 {layer}/{MAIN_REVEAL_LAYERS} 层已揭）："
                    "本章只写这一层带来的余波与人物反应，不要提前翻下一层"
                )
            continue
        hints = hint_lines(entry)[:5]
        if hints:
            lines.append(
                "全书主线暗线（只可侧面暗示，禁止解释 / 推进 / 提前揭晓）："
                + "；".join(hints)
            )
        else:
            lines.append(
                "全书主线暗线（作者已定义，只可侧面暗示，禁止解释 / 推进 / 提前揭晓）"
            )
    for entry in buckets["arc"][: INJECT_LIMIT["arc"]]:
        lines.append("卷级伏笔（近期只呼应，不得回收）：" + _one_line(entry))
    for entry in buckets["chapter"][: INJECT_LIMIT["chapter"]]:
        lines.append("小节级伏笔（本轮最多推进或回收一条）：" + _one_line(entry))
    return lines


def render_open_block(entries: Any, chapter: int | None = None) -> str:
    """渲染提示词里的一段「未回收伏笔」；空则返回空串（调用方整块不输出）。"""
    lines = open_lines(entries, chapter=chapter)
    if not lines:
        return ""
    return "伏笔台账（按级别执行，禁止越级回收）：" + "；".join(lines)


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
        level, truth, hints, planned, span = "chapter", "", "", None, None
        total, ratio, span_ratio = None, None, None
        if isinstance(item, dict):
            title = item.get("name") or item.get("title")
            status = str(item.get("status") or "open")
            level = str(item.get("tier") or "chapter").strip()
            truth = str(item.get("truth") or "").strip()
            raw_hints = item.get("hints")
            if isinstance(raw_hints, list):
                hints = "\n".join(str(h).strip() for h in raw_hints if str(h).strip())
            else:
                hints = str(raw_hints or "").strip()
            planned = item.get("planned_chapter") or item.get("reveal_chapter")
            span = item.get("reveal_span")
            total = item.get("total_chapters")
            ratio = item.get("reveal_ratio")
            span_ratio = item.get("reveal_span_ratio")
        elif isinstance(item, str):
            title, status = item, "open"
        else:
            continue
        name = str(title or "").strip()[:MAX_TITLE_CHARS]
        if not name or name in existing:
            continue
        if level not in TIERS:
            level = "chapter"
        try:
            planned_chapter = _chapter(planned, "计划回收章")
        except ValueError:
            planned_chapter = None
        try:
            reveal_span = _span(span)
        except ValueError:
            reveal_span = None
        try:
            total_chapters = _chapter(total, "全书章数")
        except ValueError:
            total_chapters = None
        try:
            reveal_ratio = _ratio(ratio, "揭晓起点比例")
        except ValueError:
            reveal_ratio = None
        try:
            reveal_span_ratio = _ratio(span_ratio, "揭晓跨度比例")
        except ValueError:
            reveal_span_ratio = None
        db.add(
            ForeshadowLedger(
                project_id=project.id,
                title=name,
                status=_BIBLE_STATUS.get(status.strip(), "open"),
                tier=level,
                planned_chapter=planned_chapter,
                truth=truth[:MAX_TRUTH_CHARS] or None,
                hints=hints[:MAX_HINTS_CHARS] or None,
                reveal_span=reveal_span,
                total_chapters=total_chapters,
                reveal_ratio=reveal_ratio,
                reveal_span_ratio=reveal_span_ratio,
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


#: 从对话里误抽出来的「坑」共用这几个外观特征：整句被引号包住、带
#: 「某某：“……」」的说话人前缀，或干脆是「你是谁？」这类第二人称短问句。
_DIALOGUE_PREFIXES = ("“", "\"", "「", "『", "【", "】", "‘", "'")
_DEICTIC_STARTS = (
    "你", "您", "我", "咱", "这", "那", "哪", "谁", "什么", "怎么", "为什么",
    "到底", "至于", "没有",
)
_SHORT_QUESTION_MARKERS = ("我", "你", "您", "咱", "吗", "呢", "不是", "对不对", "是不是", "不？")


def _is_plausible_plothole(question: str) -> bool:
    """判断 KG 里的「坑」像不像作者埋的伏笔，而不是正文对话里的一句问话。

    宁可漏收，也不把「你是谁？」这类台词登记进作者台账——台账是作者的
    意图清单，混进台词后每次生成都会把它们当成待回收伏笔注入。
    """
    q = question.strip()
    if not q:
        return False
    if q.startswith(_DIALOGUE_PREFIXES):
        return False
    if any(mark in q for mark in ("：“", "：「", ':"')):
        return False
    if q.startswith(_DEICTIC_STARTS):
        return False
    if q.endswith(("？", "?")):
        # 五个字以内的问句基本是台词（「还有呢？」「现在几点？」）；
        # 带第一/第二人称且不长时，同样按台词处理。
        if len(q) <= 5:
            return False
        if len(q) < 24 and any(marker in q for marker in _SHORT_QUESTION_MARKERS):
            return False
    return True


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
        if not question or not _is_plausible_plothole(question):
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
