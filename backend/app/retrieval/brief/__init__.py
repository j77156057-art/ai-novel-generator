"""免费检索后端（无 key 的公开结果页），只服务素材调研。

旧「外部素材工具」链路（BriefTool / 缓存 / 素材库 / 渲染 / 无头浏览器）已于
2026-11-23 删除：那一轮在慢模型上会撞 960s 总时长，且与「本书素材包」功能重叠。
现在本包只保留 `backend.py`：百度 → DuckDuckGo → Bing 的公开结果页抓取与解析，
由 `app/materials/research.py` 的 SearchRunner 复用。
"""
from .backend import (
    DEFAULT_USER_AGENT,
    FreeSearchBackend,
    JSONSearchBackend,
    SearchBackend,
    SearchError,
)

__all__ = [
    "DEFAULT_USER_AGENT",
    "FreeSearchBackend",
    "JSONSearchBackend",
    "SearchBackend",
    "SearchError",
]
