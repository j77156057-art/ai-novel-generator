# -*- coding: utf-8 -*-
"""素材包配置：目标条数、批大小、注入上限、目录阈值。

与 `retrieval/brief/settings.py` 同风格：全部上限集中声明，调用点不写死；
`MATERIAL_PACK_` 前缀的环境变量优先，其次 backend/.env，最后用默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_ENV_PREFIX = "MATERIAL_PACK_"
#: backend/.env（与 app.config 读同一个文件）。
_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _as_float(raw: str | None, default: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _read_dotenv(path: Path | None = None) -> dict[str, str]:
    """极简 .env 读取，只取 `MATERIAL_PACK_` 前缀。"""
    try:
        text = (path or _DOTENV_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.startswith(_ENV_PREFIX):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class MaterialSettings:
    """素材包的开关与边界。"""

    # 总开关：关掉时生成链路不注入、不排队、助手工具也返回空目录。
    enabled: bool = True
    # 每次生成前自动排队后台调研（只入队，不阻塞正文）。
    auto_research: bool = True
    # 每次交给模型整理的批大小（用户要求：攒够 10 条可用素材再交模型）。
    batch_size: int = 10
    # 一次调研任务的原始素材下限（用户要求固定 ≥40）。
    min_raw: int = 40
    # 单次查询请求的条数；已配置渠道内部硬顶 30，免费源按此值取。
    search_per_query: int = 20
    # 免费源的最小请求间隔；1.2s 容易触发百度验证码，默认放宽到 3s。
    search_min_interval_s: float = 3.0
    # 免费源被验证码拦截后的进程级冷却时间；期间所有任务直接跳过该源。
    block_cooldown_s: float = 180.0
    # 一个任务最多规划的查询数（防止章纲异常导致查询爆炸）。
    max_queries: int = 40
    # 连续多少个查询零新增就判定「网络源已耗尽」，任务转 partial。
    zero_streak_limit: int = 3
    # 注入正文的素材条数上限与总字数上限。
    top_n: int = 8
    max_block_chars: int = 3000
    # 包内 usable 超过此数时，注入改为「目录 + 按需工具」，避免塞爆 prompt。
    toc_threshold: int = 120
    # 目录块里列出的标题条数。
    toc_titles: int = 20
    # 目录模式的按需工具轮：总预算（秒）与最多轮数。
    # 工具轮用瘦上下文（目录 + 本章大纲，~1.5k 字），实测单次 1.5s，
    # 30s 足够跑完 3 轮；预算会作为 budget_seconds 传给单次调用，是真硬顶。
    tool_round_budget_s: float = 30.0
    tool_round_max_rounds: int = 3
    # 单条素材摘要进入正文注入时的截断长度。
    inject_snippet_chars: int = 260
    # 任务失败重试次数（不含首次）。
    retries: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MaterialSettings":
        if env is None:
            src: Mapping[str, str] = {
                **_read_dotenv(),
                **{k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIX)},
            }
        else:
            src = env

        def get(name: str) -> str | None:
            return src.get(_ENV_PREFIX + name)

        return cls(
            enabled=_as_bool(get("ENABLED"), cls.enabled),
            auto_research=_as_bool(get("AUTO_RESEARCH"), cls.auto_research),
            batch_size=max(1, _as_int(get("BATCH_SIZE"), cls.batch_size)),
            min_raw=max(1, _as_int(get("MIN_RAW"), cls.min_raw)),
            search_per_query=max(1, _as_int(get("SEARCH_PER_QUERY"), cls.search_per_query)),
            search_min_interval_s=max(
                1.0, _as_float(get("SEARCH_MIN_INTERVAL_S"), cls.search_min_interval_s)
            ),
            block_cooldown_s=max(
                1.0, _as_float(get("BLOCK_COOLDOWN_S"), cls.block_cooldown_s)
            ),
            max_queries=max(1, _as_int(get("MAX_QUERIES"), cls.max_queries)),
            zero_streak_limit=max(1, _as_int(get("ZERO_STREAK_LIMIT"), cls.zero_streak_limit)),
            top_n=max(1, _as_int(get("TOP_N"), cls.top_n)),
            max_block_chars=max(200, _as_int(get("MAX_BLOCK_CHARS"), cls.max_block_chars)),
            toc_threshold=max(1, _as_int(get("TOC_THRESHOLD"), cls.toc_threshold)),
            toc_titles=max(1, _as_int(get("TOC_TITLES"), cls.toc_titles)),
            tool_round_budget_s=max(
                5.0, _as_float(get("TOOL_ROUND_BUDGET_S"), cls.tool_round_budget_s)
            ),
            tool_round_max_rounds=max(
                1, _as_int(get("TOOL_ROUND_MAX_ROUNDS"), cls.tool_round_max_rounds)
            ),
            inject_snippet_chars=max(
                40, _as_int(get("INJECT_SNIPPET_CHARS"), cls.inject_snippet_chars)
            ),
            retries=max(0, _as_int(get("RETRIES"), cls.retries)),
        )


def load_settings() -> MaterialSettings:
    """读取当前环境配置；调用点不缓存，保证改 env 后立即生效。"""
    return MaterialSettings.from_env()


def target_usable(target_length: int | None, settings: MaterialSettings | None = None) -> int:
    """按目标字数算本书应有的可用素材数。

    用户口径：5 万字以下 ≥20，10 万字 ≥40；中间线性过渡，超过 10 万字继续线性增长。
    """
    length = max(0, int(target_length or 0))
    if length < 50_000:
        return 20
    if length >= 100_000:
        return 40 + (length - 100_000) // 2_500
    return 20 + round((length - 50_000) / 50_000 * 20)


def target_raw(target_length: int | None, settings: MaterialSettings | None = None) -> int:
    """原始素材目标：不低于固定下限（默认 40），也不低于可用目标。"""
    cfg = settings or load_settings()
    return max(cfg.min_raw, target_usable(target_length, cfg))
