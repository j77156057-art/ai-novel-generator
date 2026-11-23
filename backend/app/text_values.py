"""文本型配置值的解析（全项目唯一实现）。"""
from __future__ import annotations


def parse_bool(value: str | None) -> bool | None:
    """把配置里的布尔串解析成 bool；脏值返回 None（调用方跳过）。

    支持 `on/off`、`true/false`、`1/0`、`yes/no`（大小写、首尾空白都容忍）。
    """
    raw = (value or "").strip().lower()
    if raw in {"on", "true", "1", "yes"}:
        return True
    if raw in {"off", "false", "0", "no"}:
        return False
    return None
