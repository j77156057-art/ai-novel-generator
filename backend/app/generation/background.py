"""世界背景与硬规则（背景组）—— 作者手写的那块「世界是什么样」。

## 为什么要有这一块

现状里「世界观」是半块：

- `premise`（小说大概内容）是可选的，而且**时间线模式下整块不输出**；
- `plot_bible` 的 synopsis/direction 讲的是「故事往哪走」，不是「世界长什么样」；
- `kg_entities(kind=worldrule)` 只能从**已写正文**反推 ⇒ 开书前是空的。

⇒ 模型每段自己现编一套世界观，前后不一致（城名、规则、势力各写各的）。

这一块补的是**开书前**的那一格：作者写下世界的一句话、几条硬规则、已知势力与地点，
它进每一段的提示词，且**时间线模式也进**（世界设定不含走向，剔掉只会让模型现编）。

## 为什么不按句切 premise

`premise` 是用户写的自由文本，世界设定与剧情走向**混在一起**。
在提示词层做句子级切分有两个问题：脆弱（换个说法就切错）、
静默丢内容（切错了没人知道）。所以改在**合成时**分流一次：
「合成剧情圣经」时让模型额外产出 `background`，此后走向走 direction、背景走 background，
提示词层不需要再猜。

## 与 KG 的 worldrule 的关系

KG 的 `kind=worldrule` 是从**已写正文反推的事实**（作者可能已经写偏了）；
这一块是**作者的设定**（意图）。两者矛盾时以这一块为准 —— 意图层高于事实层。
本模块**不**做从正文反推补全，那是 KG 的活。
"""

from __future__ import annotations

from typing import Any

#: 一句话世界的长度上限（提示词是逐段重发的，别让它变成一堵墙）。
MAX_SUMMARY_CHARS = 300
#: 单条硬规则/势力地点的长度上限。
MAX_ITEM_CHARS = 200
#: 硬规则条数上限。
MAX_RULES = 20
#: 势力与地点条数上限。
MAX_FACTIONS = 30

#: 归一化后的三个字段（中文键：作者手写的内容，JSON 编辑器里要能直接看懂）。
FIELD_SUMMARY = "一句话世界"
FIELD_RULES = "硬规则"
FIELD_FACTIONS = "势力与地点"

#: 模型输出可能用的别名 —— 归一化时按这个顺序找第一个命中。
_ALIASES: dict[str, tuple[str, ...]] = {
    FIELD_SUMMARY: ("一句话世界", "世界", "世界观", "背景", "世界设定", "一句话背景"),
    FIELD_RULES: ("硬规则", "规则", "世界规则", "限制", "设定规则"),
    FIELD_FACTIONS: ("势力与地点", "势力", "地点", "势力地点", "势力与地名", "地图"),
}

#: 反向映射：别名 → 规范字段。
_ALIAS_TO_FIELD: dict[str, str] = {
    alias: field for field, aliases in _ALIASES.items() for alias in aliases
}


def _as_text(value: Any) -> str:
    """只接受字符串；其它形状（dict / 列表 / 数字）一律当成没有。"""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _as_items(value: Any, limit: int) -> list[str]:
    """把「可能是一串字符串」的形状归一成 `list[str]`。

    - 字符串 → 拆成行（模型常把多条规则塞进一个字符串，用换行/分号分隔）；
    - 列表 → 逐条取字符串元素；
    - 其它（dict / 数字 / None）→ 空列表。
    """
    items: list[str] = []
    if isinstance(value, str):
        raw = value.replace("；", "\n").replace(";", "\n").splitlines()
    elif isinstance(value, (list, tuple)):
        raw = []
        for entry in value:
            if isinstance(entry, str):
                raw.extend(entry.replace("；", "\n").replace(";", "\n").splitlines())
            elif isinstance(entry, dict):
                # 模型偶尔给 {"名称": "…", "说明": "…"} —— 拼成一行，不丢内容。
                name = _as_text(entry.get("名称") or entry.get("name") or entry.get("标题"))
                desc = _as_text(entry.get("说明") or entry.get("desc") or entry.get("描述"))
                if name and desc:
                    raw.append(f"{name}——{desc}")
                elif name or desc:
                    raw.append(name or desc)
    else:
        raw = []

    seen: set[str] = set()
    for item in raw:
        text = _as_text(item)
        if not text:
            continue
        text = text[:MAX_ITEM_CHARS]
        if text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def normalize_background(raw: Any) -> dict:
    """把任意形状的背景组归一成 `{一句话世界, 硬规则, 势力与地点}`。

    **不抛** —— 脏数据只丢字段，不让整个项目崩。
    空的三字段组返回 `{}`（表示「没写背景」），前端据此不显示这一块。
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = {}
    for field, aliases in _ALIASES.items():
        value = None
        for alias in aliases:
            if alias in raw and raw.get(alias) not in (None, "", [], {}):
                value = raw.get(alias)
                break
        if value is None:
            continue
        if field == FIELD_SUMMARY:
            text = _as_text(value)[:MAX_SUMMARY_CHARS]
            if text:
                out[field] = text
        else:
            limit = MAX_RULES if field == FIELD_RULES else MAX_FACTIONS
            items = _as_items(value, limit)
            if items:
                out[field] = items
    return out


def background_of(bible: Any) -> dict:
    """从 `plot_bible` 里取出归一化后的背景组。"""
    if not isinstance(bible, dict):
        return {}
    return normalize_background(bible.get("background"))


def render_background(background: Any) -> str:
    """渲染成提示词里的一段（不含块标题）。

    空组返回空串 —— 调用方据此**整块不输出**（空块是负面暗示：
    「（暂无世界背景）」等于告诉模型「这个世界没有设定，你自己编吧」）。

    ## 条目之间怎么连

    用「；」连接，但**先去掉条目自带的结尾标点** ——
    否则会出现「…抵御黑暗。；灵气极度稀缺…」这种双标点
    （模型给的条目大多自带句号，直接拼就露出来了）。
    """
    data = normalize_background(background)
    if not data:
        return ""
    lines: list[str] = []
    summary = data.get(FIELD_SUMMARY)
    if summary:
        lines.append(f"一句话世界：{summary}")
    rules = data.get(FIELD_RULES) or []
    if rules:
        lines.append("硬规则：" + _join_items(rules))
    factions = data.get(FIELD_FACTIONS) or []
    if factions:
        lines.append("势力与地点：" + _join_items(factions))
    return "\n".join(lines)


def _join_items(items: list[str]) -> str:
    """用「；」连接条目（去掉每条自带的结尾标点，避免双标点）。"""
    cleaned = []
    for item in items:
        text = item.strip()
        while text and text[-1] in "。；;，,、 \t":
            text = text[:-1].rstrip()
        if text:
            cleaned.append(text)
    return "；".join(cleaned)
