"""KG 双层（原著层 / 生成层）的公共约定。

- canon：229 章原文抽取，只读，是身份/境界/势力等设定的真值。
- generated：AI 新写章节抽取，只记录“已经写过什么”，不得覆盖 canon 设定。

本模块只放常量与纯函数，不碰数据库，便于两边复用与单测。
"""
from __future__ import annotations

from typing import Any, Mapping

CANON = "canon"
GENERATED = "generated"

SOURCE_PRIORITY = {CANON: 2, GENERATED: 1}

#: canon 受保护字段：generated 不得写入/覆盖这些属性。
PROTECTED_ATTRS = frozenset({
    "identity", "identity_tags", "rank", "realm", "faction", "affiliation",
    "belongs_to", "type", "position", "title", "alive", "status",
    "system", "tier", "held_by", "sect", "organization",
})


def source_of(attrs: Mapping[str, Any] | None) -> str:
    """从 attrs 读来源标记；没有标记的历史数据按 canon 处理。"""
    value = (attrs or {}).get("_source")
    return GENERATED if value == GENERATED else CANON


def source_priority(source: str | None) -> int:
    """来源优先级：canon(2) > generated(1) > 未知(0)。"""
    if not source:
        return SOURCE_PRIORITY[CANON]
    return SOURCE_PRIORITY.get(source, 0)


def tag_attrs(attrs: Mapping[str, Any] | None, source: str,
              chapter: int | None = None) -> dict:
    """给 attrs 打上来源与来源章号；不改原字典。"""
    out = dict(attrs or {})
    out["_source"] = source
    if chapter is not None:
        out["_source_chapter"] = int(chapter)
    return out


def strip_protected(attrs: Mapping[str, Any] | None) -> dict:
    """去掉受保护字段与内部标记，只保留生成层可记录的观察值。"""
    return {
        key: value
        for key, value in (attrs or {}).items()
        if key not in PROTECTED_ATTRS and not key.startswith("_")
    }


def may_override(field: str, current_source: str, new_source: str) -> bool:
    """一个字段是否允许被新来源覆盖。受保护字段只有更高优先级能覆盖。"""
    if field not in PROTECTED_ATTRS:
        return True
    return source_priority(new_source) >= source_priority(current_source)


def prefer(canon_item: Mapping[str, Any] | None,
           generated_item: Mapping[str, Any] | None) -> dict:
    """合并两层同名实体：canon 身份字段优先，generated 只补非保护观察值。"""
    canon_item = dict(canon_item or {})
    generated_item = dict(generated_item or {})
    if not canon_item:
        return tag_attrs(generated_item, GENERATED, generated_item.get("source_chapter"))
    merged = dict(canon_item)
    merged.update(strip_protected(generated_item))
    merged["_source"] = CANON
    return merged
