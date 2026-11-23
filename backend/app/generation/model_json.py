"""模型输出 → JSON 的容错解析（全项目唯一实现）。

三个生成器此前各有一份 `_safe_json`，容错范围不同（有的只剥 ``` 包裹、
有的不认已经解析好的 dict/list）。模型输出形态是同一类问题，口径不应分叉。
"""
from __future__ import annotations

import json
import re
from typing import Any


def safe_json(raw: Any) -> Any:
    """模型输出 → dict/list（整体 JSON → 剥 ``` → 抓第一个 `{...}`）。

    已经解析好的 dict/list 原样返回；空输入与解析失败返回 {}。
    """
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return {}
