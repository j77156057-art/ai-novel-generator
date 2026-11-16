"""章纲（``project.outline``）的纯逻辑层：归一化、缺失标记、保存前结构校验。

为什么单独一个模块：

- 章纲只有一个 JSON 列（``projects.outline``），读侧（``chapter_outline_entry``）、
  写侧（新端点）必须共用同一套口径，否则「界面上有、提示词里没有」会再次出现；
- 校验要能在**不建库、不跑模型**的情况下测试；
- 每条规则都返回 ``checked`` / ``skipped``。查不到就记 skipped，禁止
  「查不了就当通过」——这是本轮「不许静默」的硬约束。

存储约定（与 ``chapter_outline_entry`` 对齐）：

- 第 N 章 = ``outline[N-1]``；
- 缺失章存空字符串 ``""``，**不存 JSON null**。读侧对每条做 ``str(x).strip()``，
  ``str(None)`` 会得到字面量 ``"None"``，把「没有章纲」当成一条内容注入提示。
  对外 API 可以把缺失显示成 ``entry: null``，落库仍是 ``""``。

字符位置一律是 **0 基偏移**，``snippet`` 是命中点前后各 12 字的原文，
用于人工判断是否误伤。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

#: 缺失章在库里的表示。必须是空串，理由见模块 docstring。
MISSING_ENTRY = ""

#: 章纲条目前缀「第N章 / 第N回 / 第N节」。
_CHAPTER_PREFIX = re.compile(r"^\s*第\s*(\d+)\s*[章回节]")

#: 时段 → 小时偏移。用于把「晚上十点」这类写法折成当天分钟数。
_PERIOD_HOURS = {
    "凌晨": 0,
    "早上": 0,
    "上午": 0,
    "中午": 12,
    "下午": 12,
    "傍晚": 12,
    "晚上": 12,
    "夜里": 12,
    "半夜": 0,
}

#: 绝对时间：可选时段 + 小时 + 分钟（``22:30`` / ``晚上10点`` / ``十点``不认，
#: 只认阿拉伯数字——中文数字歧义太多，宁可少判也不乱判）。
_ABS_TIME = re.compile(
    r"(?:(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|半夜)\s*)?"
    r"(\d{1,2})\s*(?:[:：]\s*(\d{1,2})|[点时]\s*(半|\d{1,2})?\s*分?)"
)

#: 明确表示「时间往前推了一天/一段时间」的标记。出现时不判「时间倒退」。
_FORWARD_MARKERS = re.compile(
    r"次日|翌日|第二天|第三天|隔天|隔日|第[二三四五六七八九十]+天|"
    r"(?:\d+|[一二三四五六七八九十]+|半)\s*(?:分钟|小时|天|日|周|个月|年)\s*后|"
    r"当天晚些时候|入夜后|天亮后"
)

#: 明确表示「同一时刻 / 平行时间线」的标记。出现时不判「时间相同」。
_SAME_TIME_MARKERS = re.compile(r"与此同时|同一时间|同时|当天|同日|当日|稍后|片刻后")

#: 引号内专名：作者用引号强调，误报率低，作为硬拦依据。
#: **不含书名号 《》**——章纲条目普遍写成「第N章《标题》」，书名号里是章节标题，
#: 不是权威物件；把它当专名会让每条 AI 草稿都误报。
_QUOTED = re.compile(r"[「“]([^」”]{1,40})[」”]")

#: 物件后缀。只做字面命中，不是中文分词；因此只给 warning，不硬拦。
#: 多字后缀放前面，正则按长度降序拼装。
_OBJECT_SUFFIXES = (
    "镊子", "钥匙", "玉佩", "信封", "文件", "档案", "盒子", "箱子", "镜子",
    "令牌", "符纸", "药瓶", "匕首", "长剑", "短刀", "铜牌", "铁盒", "木匣",
    "函", "册", "卷", "刀", "剑", "药", "针", "钟", "盒", "箱", "镜",
    "牌", "令", "符",
)
_OBJECT_SUFFIX_ONLY = re.compile(
    "|".join(sorted(_OBJECT_SUFFIXES, key=len, reverse=True))
)

#: 物件词左边常见的量词/助词/动词。命中后不再向左扩词，避免把
#: 「起一把铜牌」整段当物件名；只保留真正像名词的部分（如「青玉佩」）。
_OBJECT_LEADING = set(
    "的了着把一一个枚块根份封柄支张只些起拿取握掏抽翻推拉抬捏抓看递接放写读打"
    "几第这那两三四五六七八九十"
)
_HANZI = re.compile(r"[\u4e00-\u9fff]")

#: 权威文本过短时不做物件检查——没有权威可比，硬判只会误伤。
_AUTHORITY_MIN_CHARS = 20


def normalize_outline(raw: Any) -> list[str]:
    """把 ``project.outline`` 归一成 ``list[str]``；缺失项统一成空串。

    兼容四种历史形态：list/tuple、JSON 字符串、换行分隔字符串、None。
    解析失败时按换行切，不抛异常——读侧不应该因为一条脏数据整体失败。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [MISSING_ENTRY if item is None else str(item).strip() for item in raw]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001 —— 不是 JSON 就走换行切分
            parsed = None
        if isinstance(parsed, list):
            return normalize_outline(parsed)
        return [line.strip() for line in text.splitlines() if line.strip()]
    return []


def chapter_status(outline: Sequence[str], chapter: int) -> str:
    """某一章在章纲里的状态：``confirmed`` / ``missing`` / ``beyond_end``。"""
    if chapter <= 0:
        raise ValueError("chapter 必须 >= 1")
    if chapter > len(outline):
        return "beyond_end"
    return "confirmed" if str(outline[chapter - 1] or "").strip() else "missing"


def missing_chapters(
    outline: Sequence[str],
    *,
    from_chapter: int = 1,
    to_chapter: int | None = None,
    written_chapters: int = 0,
) -> list[int]:
    """列出区间内没有章纲的章号（显式可见，不省略）。

    默认上界取 ``max(章纲长度, 已写章数)``：已有 43 章、章纲 40 条时，
    第 41–43 章会被列出来，而不是因为「超出章纲长度」被静默截掉。
    """
    if from_chapter < 1:
        raise ValueError("from_chapter 必须 >= 1")
    end = to_chapter
    if end is None:
        end = max(len(outline), int(written_chapters or 0))
    if end < from_chapter:
        raise ValueError("to_chapter 不能小于 from_chapter")
    return [
        chapter
        for chapter in range(from_chapter, end + 1)
        if chapter_status(outline, chapter) != "confirmed"
    ]


def outline_after_entries(
    outline: Sequence[str], entries: Sequence[dict]
) -> dict:
    """把校验通过的条目写入副本，返回新列表与「自动补空」的章号。

    ``auto_filled_missing`` 必须回给调用方/界面：写第 44 章时 41–43 会被补成
    空串，这是**显式可见**的补空，不是静默跳过。
    """
    result = normalize_outline(list(outline))
    auto_filled: list[int] = []
    changed: list[int] = []
    for item in entries:
        chapter = int(item["chapter"])
        if chapter <= 0:
            raise ValueError("chapter 必须 >= 1")
        while len(result) < chapter:
            result.append(MISSING_ENTRY)
            auto_filled.append(len(result))
        entry = item.get("entry")
        value = MISSING_ENTRY if entry is None else str(entry).strip()
        if result[chapter - 1] != value:
            changed.append(chapter)
        result[chapter - 1] = value
    # 本次显式写入的章不算「自动补空」
    explicit = {int(item["chapter"]) for item in entries}
    auto_filled = [chapter for chapter in auto_filled if chapter not in explicit]
    return {
        "outline": result,
        "auto_filled_missing": auto_filled,
        "changed": sorted(set(changed)),
    }


def extract_time_tokens(text: str) -> list[dict]:
    """抽取字面绝对时间，返回 ``{minutes, token, start, end}``（按出现顺序）。

    ``minutes`` 是当天分钟数（0–1439）；时段只做 12 小时制折算：
    下午/傍晚/晚上/夜里 且小时 < 12 → +12；中午 12 点保持 12:00。
    不带时段时按 24 小时制原样解释。
    """
    out: list[dict] = []
    for match in _ABS_TIME.finditer(text or ""):
        period, hour_s, colon_min, point_min = match.groups()
        hour = int(hour_s)
        if hour > 23:
            continue
        minute = 0
        if colon_min is not None:
            minute = int(colon_min)
        elif point_min == "半":
            minute = 30
        elif point_min is not None:
            minute = int(point_min)
        if minute > 59:
            continue
        if period:
            hour = (hour % 12) + _PERIOD_HOURS[period]
            if period == "中午" and hour < 12:
                hour += 12
        out.append({
            "minutes": hour * 60 + minute,
            "token": match.group(0).strip(),
            "start": match.start(),
            "end": match.end(),
        })
    return out


def _snippet(entry: str, start: int, end: int) -> str:
    return entry[max(0, start - 12): min(len(entry), end + 12)]


def _object_terms(entry: str) -> list[tuple[str, int, int]]:
    """字面抽物件候选：命中后缀后向左最多补 2 个实字，遇到量词/动词就停。

    返回 ``[(term, start, end)]``。只用于 warning，不追求分词正确率。
    """
    out: list[tuple[str, int, int]] = []
    for match in _OBJECT_SUFFIX_ONLY.finditer(entry):
        start = match.start()
        expanded = 0
        while start > 0 and expanded < 2:
            char = entry[start - 1]
            if not _HANZI.match(char) or char in _OBJECT_LEADING:
                break
            start -= 1
            expanded += 1
        term = entry[start:match.end()].strip()
        if len(term) >= 2:
            out.append((term, start, match.end()))
    return out


def _position(entry: str, start: int, end: int) -> dict:
    return {"start": start, "end": end, "snippet": _snippet(entry, start, end)}


def _in_authority(term: str, authority_text: str, kg_names: Iterable[str]) -> bool:
    if term and term in authority_text:
        return True
    for name in kg_names:
        name = str(name or "").strip()
        if len(name) < 2:
            continue
        if term == name or term in name or name in term:
            return True
    return False


def _check(checks: list[dict], skipped: list[str], rule: str, status: str,
           reason: str | None = None, detail: str | None = None) -> None:
    item = {"rule": rule, "status": status}
    if reason:
        item["reason"] = reason
    if detail:
        item["detail"] = detail
    checks.append(item)
    if status == "skipped":
        skipped.append(f"{rule}:{reason or 'unspecified'}")


def _error(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


def _warning(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


def _entry_map(
    entries: Sequence[dict], errors: list[dict],
) -> dict[int, str | None]:
    """条目列表 → ``{章号: 文本或 None}``；结构错误就地记录。"""
    mapped: dict[int, str | None] = {}
    for item in entries:
        if not isinstance(item, dict):
            errors.append(_error("invalid_entry", "条目必须是对象"))
            continue
        raw_chapter = item.get("chapter")
        if isinstance(raw_chapter, bool) or not isinstance(raw_chapter, int):
            errors.append(_error(
                "invalid_entry", f"chapter 必须是整数，收到 {raw_chapter!r}",
                chapter=raw_chapter,
            ))
            continue
        if raw_chapter in mapped:
            errors.append(_error(
                "chapter_duplicate", f"第 {raw_chapter} 章重复提交",
                chapter=raw_chapter,
            ))
            continue
        entry = item.get("entry")
        if entry is not None and not isinstance(entry, str):
            errors.append(_error(
                "invalid_entry", f"第 {raw_chapter} 章的 entry 必须是字符串或 null",
                chapter=raw_chapter,
            ))
            continue
        mapped[raw_chapter] = entry
    return mapped


def validate_chapter_range(
    entries: Sequence[dict],
    *,
    from_chapter: int,
    to_chapter: int,
    outline: Sequence[str] | None = None,
    written_chapters: int = 0,
    premise: str | None = None,
    bible: dict | None = None,
    kg_names: Iterable[str] = (),
    node_windows: Sequence[dict] | None = None,
) -> dict:
    """保存前的结构校验：章号连续性 + 时间窗 + 权威外物件。

    返回 ``{ok, errors, warnings, checks, skipped_checks}``：

    - ``errors`` 非空 → 调用方应 422 拒绝整批，不落库；
    - ``skipped_checks`` 非空 → 允许保存，但必须原样回显到界面；
    - 每条规则都会出现在 ``checks`` 里（checked 或 skipped），不会缺席。

    ``entries`` 的每项是 ``{"chapter": int, "entry": str | None}``；
    ``entry=None`` 表示**显式缺省**，空字符串则是错误（避免用空串蒙混成内容）。
    """
    outline_list = normalize_outline(list(outline or []))
    errors: list[dict] = []
    warnings: list[dict] = []
    checks: list[dict] = []
    skipped: list[str] = []

    # ── 1. 章号连续性 ────────────────────────────────────────────────
    if from_chapter < 1:
        errors.append(_error("chapter_out_of_range", "from_chapter 必须 >= 1",
                             chapter=from_chapter))
    if to_chapter < from_chapter:
        errors.append(_error(
            "range_invalid", "to_chapter 不能小于 from_chapter",
            from_chapter=from_chapter, to_chapter=to_chapter,
        ))
    expected = list(range(from_chapter, to_chapter + 1)) if to_chapter >= from_chapter else []
    mapped = _entry_map(entries, errors)

    missing = [chapter for chapter in expected if chapter not in mapped]
    if missing:
        errors.append(_error(
            "chapter_gap",
            f"第 {'、'.join(str(c) for c in missing)} 章缺少条目；"
            "若要缺省请显式传 entry: null",
            chapters=missing,
        ))
    extra = sorted(chapter for chapter in mapped if chapter not in expected)
    if extra:
        errors.append(_error(
            "chapter_out_of_range",
            f"提交了区间外的章号：{'、'.join(str(c) for c in extra)}",
            chapters=extra,
        ))

    for chapter in expected:
        if chapter not in mapped:
            continue
        entry = mapped[chapter]
        if entry is not None and not entry.strip():
            errors.append(_error(
                "empty_entry",
                f"第 {chapter} 章的 entry 是空字符串；要标记缺省请用 null",
                chapter=chapter,
            ))
            continue
        if entry is None:
            continue
        prefix = _CHAPTER_PREFIX.match(entry)
        if prefix is not None and int(prefix.group(1)) != chapter:
            warnings.append(_warning(
                "chapter_prefix_mismatch",
                f"第 {chapter} 章的条目写的是「第 {prefix.group(1)} 章」",
                chapter=chapter,
                found=prefix.group(1),
                position=_position(entry, prefix.start(), prefix.end()),
            ))
        if written_chapters and chapter <= written_chapters:
            warnings.append(_warning(
                "written_chapter_edit",
                f"第 {chapter} 章已定稿；改章纲只影响重写，不会修改已落库正文",
                chapter=chapter,
            ))
    _check(checks, skipped, "chapter_continuity",
           "checked" if not missing and not extra else "checked",
           detail=f"覆盖第 {from_chapter}–{to_chapter} 章")

    # ── 2. 时间窗 ────────────────────────────────────────────────────
    submitted = {
        chapter: entry for chapter, entry in mapped.items()
        if entry is not None and entry.strip()
    }
    submitted_tokens = {
        chapter: extract_time_tokens(entry) for chapter, entry in submitted.items()
    }
    if not any(submitted_tokens.values()):
        _check(checks, skipped, "time_window_between_chapters", "skipped",
               reason="no_absolute_time_tokens_in_range")
    else:
        context: dict[int, str] = {}
        for chapter in range(max(1, from_chapter - 1), to_chapter + 1):
            if chapter in submitted:
                context[chapter] = submitted[chapter]
            elif chapter <= len(outline_list) and outline_list[chapter - 1].strip():
                context[chapter] = outline_list[chapter - 1]
        compared = 0
        for chapter in sorted(context):
            prev_entry = context.get(chapter - 1)
            next_entry = context.get(chapter)
            if not prev_entry or not next_entry:
                continue
            prev_tokens = extract_time_tokens(prev_entry)
            next_tokens = extract_time_tokens(next_entry)
            if not prev_tokens or not next_tokens:
                continue
            if _FORWARD_MARKERS.search(next_entry) or _SAME_TIME_MARKERS.search(next_entry):
                continue
            compared += 1
            prev_token = prev_tokens[-1]
            next_token = next_tokens[0]
            if next_token["minutes"] < prev_token["minutes"]:
                errors.append(_error(
                    "time_window_backwards",
                    f"第 {chapter - 1} 章「{prev_token['token']}」到"
                    f"第 {chapter} 章「{next_token['token']}」时间倒退，"
                    "且没有「次日/翌日/第二天/…后」等推进标记",
                    chapter=chapter,
                    previous_chapter=chapter - 1,
                    previous_token=prev_token["token"],
                    token=next_token["token"],
                    position=_position(next_entry, next_token["start"], next_token["end"]),
                ))
            elif next_token["minutes"] == prev_token["minutes"]:
                warnings.append(_warning(
                    "time_window_same_without_marker",
                    f"第 {chapter - 1} 章与第 {chapter} 章都写在"
                    f"「{next_token['token']}」，且没有「与此同时/同日/当天」标记",
                    chapter=chapter,
                    previous_chapter=chapter - 1,
                    token=next_token["token"],
                    position=_position(next_entry, next_token["start"], next_token["end"]),
                ))
        _check(checks, skipped, "time_window_between_chapters", "checked",
               detail=f"比较了 {compared} 组相邻章")

    # ── 3. 时间窗 vs 已存节点 ───────────────────────────────────────
    if not node_windows:
        _check(checks, skipped, "time_window_node", "skipped",
               reason="no_node_windows")
    else:
        compared = 0
        for chapter, entry in submitted.items():
            node = next(
                (n for n in node_windows
                 if int(n.get("chapter_start") or 0) <= chapter
                 <= int(n.get("chapter_end") or 0)),
                None,
            )
            if node is None:
                continue
            node_text = " ".join(str(node.get(key) or "") for key in
                                 ("title", "entry_condition", "exit_condition"))
            node_tokens = extract_time_tokens(node_text)
            entry_tokens = submitted_tokens.get(chapter) or []
            if not node_tokens or not entry_tokens:
                continue
            if _FORWARD_MARKERS.search(entry) or _SAME_TIME_MARKERS.search(entry):
                continue
            compared += 1
            node_token = node_tokens[0]
            entry_token = entry_tokens[0]
            if entry_token["minutes"] < node_token["minutes"]:
                warnings.append(_warning(
                    "time_window_node_mismatch",
                    f"第 {chapter} 章写「{entry_token['token']}」，"
                    f"但所属节点 {node.get('node_id')} 写「{node_token['token']}」",
                    chapter=chapter,
                    node_id=node.get("node_id"),
                    token=entry_token["token"],
                    node_token=node_token["token"],
                    position=_position(entry, entry_token["start"], entry_token["end"]),
                ))
        if compared == 0:
            _check(checks, skipped, "time_window_node", "skipped",
                   reason="no_comparable_node_time_tokens")
        else:
            _check(checks, skipped, "time_window_node", "checked",
                   detail=f"比较了 {compared} 章")

    # ── 4. 权威外物件 ────────────────────────────────────────────────
    authority_text = "\n".join([
        str(premise or ""),
        json.dumps(bible or {}, ensure_ascii=False) if bible else "",
        "\n".join(outline_list[:max(0, from_chapter - 1)]),
        "\n".join(
            outline_list[chapter - 1]
            for chapter in range(max(1, from_chapter),
                                 min(to_chapter, len(outline_list)) + 1)
            if outline_list[chapter - 1].strip()
        ),
    ]).strip()
    kg_list = [str(name or "").strip() for name in kg_names]
    if len(authority_text) < _AUTHORITY_MIN_CHARS and not kg_list:
        _check(checks, skipped, "authority_quoted", "skipped",
               reason="authority_empty_or_too_thin")
        _check(checks, skipped, "authority_suffix", "skipped",
               reason="authority_empty_or_too_thin")
    else:
        quoted_hits = 0
        suffix_hits = 0
        for chapter, entry in submitted.items():
            quoted_spans: list[tuple[int, int]] = []
            for match in _QUOTED.finditer(entry):
                term = match.group(1).strip()
                quoted_spans.append((match.start(), match.end()))
                if len(term) < 2:
                    continue
                if _in_authority(term, authority_text, kg_list):
                    continue
                quoted_hits += 1
                errors.append(_error(
                    "authority_quoted_term_unknown",
                    f"第 {chapter} 章引用了权威外的专名「{term}」",
                    chapter=chapter,
                    term=term,
                    position=_position(entry, match.start(), match.end()),
                ))
            for term, start, end in _object_terms(entry):
                if any(q_start <= start < q_end or q_start < end <= q_end
                       for q_start, q_end in quoted_spans):
                    continue
                if _in_authority(term, authority_text, kg_list):
                    continue
                suffix_hits += 1
                warnings.append(_warning(
                    "authority_item_suspect",
                    f"第 {chapter} 章的物件「{term}」在权威文本里找不到（字面检查）",
                    chapter=chapter,
                    term=term,
                    position=_position(entry, start, end),
                ))
        _check(checks, skipped, "authority_quoted", "checked",
               detail=f"命中 {quoted_hits} 个权威外专名")
        _check(checks, skipped, "authority_suffix", "checked",
               detail=f"命中 {suffix_hits} 个可疑物件")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "skipped_checks": skipped,
    }


def validate_single_chapter(
    chapter: int,
    entry: str | None,
    *,
    outline: Sequence[str] | None = None,
    written_chapters: int = 0,
    premise: str | None = None,
    bible: dict | None = None,
    kg_names: Iterable[str] = (),
    node_windows: Sequence[dict] | None = None,
) -> dict:
    """单章保存的薄封装，规则与区间保存完全一致。"""
    return validate_chapter_range(
        [{"chapter": chapter, "entry": entry}],
        from_chapter=chapter,
        to_chapter=chapter,
        outline=outline,
        written_chapters=written_chapters,
        premise=premise,
        bible=bible,
        kg_names=kg_names,
        node_windows=node_windows,
    )
