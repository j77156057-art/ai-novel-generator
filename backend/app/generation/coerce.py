"""模型输出字段的轻量归一（全项目唯一实现）。

卡表 / 修炼档案 / 体系 / 量级 / 人设都从模型拿「可能是字符串、可能是列表」
的字段。此前每个模块各抄一份 `_as_text` / `_as_list`，口径虽然一致，但一改
就得改六处。这里只保留最常用的两种形态；刻意与 `background._as_text`
（只收字符串）和 `power_prompt._as_text`（dict 展开 k=v）区分：
它们的语义不同，不做强行合并。
"""
from __future__ import annotations

from typing import Any


def as_text(value: Any) -> str:
    """列表拼成「；」分隔的文本；其它值 `str().strip()`，None → 空串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def as_list(value: Any, limit: int) -> list[str]:
    """字符串/元组/单值 → `list[str]`，最多 `limit` 条。

    调用方显式给 `limit`：不同字段的上限本来就不同（口头禅 3 条、路线 8 条…），
    给个统一默认只会让上限悄悄漂移。
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [str(value).strip()]
    return items[:limit]
