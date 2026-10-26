"""文体档案：把「风格」从形容词变成可测的数字（纯离线统计，零 LLM）。

对外入口：
- `stylometry.fit_profile` / `style_distance` / `describe` —— 拟合与判读
- `pipeline.project_profile` —— 「参考语料 → 体检清洗 → 档案 → 落库缓存」整条链路
"""

from app.style.pipeline import (
    build_reference_profile,
    health_summary,
    profile_fingerprint,
    project_profile,
    project_profile_safe,
)
from app.style.stylometry import (
    FEATURES,
    FEATURE_NOTES,
    PROXY_FEATURES,
    cut_units,
    describe,
    feature_table,
    features,
    fit_profile,
    style_distance,
)
from app.style.warning_store import (
    append_style_warning,
    get_style_warnings,
    is_truncated,
)

__all__ = [
    "FEATURES",
    "FEATURE_NOTES",
    "PROXY_FEATURES",
    "append_style_warning",
    "build_reference_profile",
    "cut_units",
    "describe",
    "feature_table",
    "features",
    "fit_profile",
    "get_style_warnings",
    "health_summary",
    "is_truncated",
    "profile_fingerprint",
    "project_profile",
    "project_profile_safe",
    "style_distance",
]
