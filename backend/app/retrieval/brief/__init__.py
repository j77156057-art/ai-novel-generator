"""外部素材检索工具（独立模块，默认关闭）。

设计要点（2026-11-15 用户拍板）：
- 由**模型自主决定**是否调用、查什么；本模块不预生成查询词，也不规定题材。
- 只在初稿阶段暴露给模型；重写轮不开放，避免重复计费。
- 每次/每章的上限、超时、总预算、字数全部来自 `BriefSettings`，调用点不写死数字。
- 任何失败都降级为「无素材」，不阻断正文生成。
- 开关关闭时不联网、不读缓存、不写轨迹。

本模块**不**接线进 `prompts.py` / `engine.py`；接线由调用方在阶段工具列表里
使用 `BriefTool.spec()` 与 `BriefTool.dispatch()` 完成。
"""
from .backend import (
    DEFAULT_USER_AGENT,
    FreeSearchBackend,
    JSONSearchBackend,
    SearchBackend,
    SearchError,
)
from .cache import BriefCache, CacheHit
from .library import MaterialEntry, MaterialLibrary, overlap_ratio
from .render import render_block
from .settings import BriefSettings
from .tool import SELECT_TOOL_NAME, TOOL_NAME, TOOL_STAGE, BriefTool
from .browser import BrowserSearchBackend

__all__ = [
    "BriefCache",
    "BriefSettings",
    "BriefTool",
    "BrowserSearchBackend",
    "CacheHit",
    "DEFAULT_USER_AGENT",
    "FreeSearchBackend",
    "JSONSearchBackend",
    "MaterialEntry",
    "MaterialLibrary",
    "SELECT_TOOL_NAME",
    "SearchBackend",
    "SearchError",
    "TOOL_NAME",
    "TOOL_STAGE",
    "overlap_ratio",
    "render_block",
]
