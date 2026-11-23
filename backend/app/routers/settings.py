"""模型设置路由：前端「模型设置」页驱动——选渠道 / 填 key / 选模型 / 测连通。

## 三角色视图（第 7 步新增 writer / assistant，第 8 步新增 distiller）

`GET /api/settings/llm` 在保留全部原有顶层字段（`provider` / `model` /
`keys` / `providers` / `effective`——旧前端不能坏）之外，新增 `writer`、
`assistant`、`distiller` 三个角色对象，各含：

    {provider, model, api_key_masked, has_key, base_url,
     max_tokens, timeout, is_fallback, label}

这是前端「模型设置」页的数据基础：三个角色各选各的渠道 / key / 模型，
互不覆盖。

- `writer`    ：正文生成（续写 / 重生成 / 出方案）。
- `assistant` ：助手（只读问答、结构化诊断、提案与版本审核）。
- `distiller` ：文风档案抽取。**注意**：它与 `NarrativeTier` 的「蒸馏档位」
  （full / balanced / fast，采样与计算档位，定义在 app/style/narrative.py）
  不是一回事——档位决定「抽多细」，本角色决定「用哪个模型抽」。

## PUT 的两种模式

- **legacy 模式**（请求体不带 `role`）：行为与第 6 步**完全一致**——
  写 `llm.*` 键、覆盖 settings、`apply_runtime_config` 热生效。
- **角色模式**（`role="writer" | "assistant" | "distiller"`）：写
  `writer.*` / `assistant.*` / `distiller.*` 键（key 形如
  `writer.provider` / `distiller.api_key`），覆盖 settings 的对应
  `*_` 字段，并**只重置该角色**的 provider 缓存
  （`ProviderRegistry.invalidate(role)`）——改 Assistant 配置不会连带
  重建 Writer 单例，反之亦然。

`role="writer"` 会额外把配置**镜像**到 legacy 字段（`llm.provider` /
渠道 `*_api_key` / `llm.model` …）：Writer 单例（b58c763 路径）只读
legacy 字段，不镜像则角色配置落而不生效——那会是「UI 显示已改、
生成链路照旧」的静默失效。镜像写入与读取都经过同一套
`_MODEL_FIELD` / `_KEY_FIELD` 映射，不新造平行逻辑。

Writer 的 `max_tokens` / `timeout` **不接受** PUT：b58c763 硬红线固定
chat timeout=120 / stream_timeout=180，`llm/*` 行为不变；GET 的
`writer` 对象仍如实展示单例上的真实值。

## 配置优先级（读侧，四档从高到低）

1. 显式 role 配置（`writer_*` / `assistant_*` settings 字段，由角色 PUT
   或启动加载写入）；
2. `config/providers.yaml` 默认值（settings 类字段默认值）；
3. 渠道 legacy 字段回退（`config._legacy_channel_field`，dc39bbb 实现，
   Assistant 显式留空时按渠道名借 `*_api_key` / `*_base_url`）；
4. 都没有 → LocalFallback（离线可跑，非模型质量）。

`config.Settings.get_provider_config('assistant')` 已实现 1→3 的链条，
本模块不重复实现，只负责把持久化值写进 settings（= 第 1 档）。
"""

import json
import os
import time
import urllib.request
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import KG_EXTRACT_CHANNELS, settings
from app.db import get_db
from app.llm.provider import (
    apply_runtime_config,
    build_provider,
    get_kg_extract_provider,
    get_llm_provider,
    reset_kg_extract_provider_cache,
)
from app.llm.registry import (
    ASSISTANT_ROLE,
    DISTILLER_ROLE,
    WRITER_ROLE,
    get_provider_registry,
)
from app.models import AppSetting
from app.schemas import StrictRequest
from app.text_values import parse_bool

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 渠道元数据：供前端下拉与说明（key 获取地址 / 免费政策 / 默认候选模型）
#
# ⚠️ 「各渠道/模型的思考控制方式」不写在这里，统一查
# `app/llm/thinking_models.py` 的实测表（`thinking_control_for` 只负责翻译成
# 界面契约）。同一件事散在多处，新加渠道漏一处就是静默失效。
def thinking_control_for(provider: str, model: str = "") -> dict:
    """这个渠道 + 这个模型能用哪种思考控制？拿不准就返回 `mode="none"`。

    三道问题（是不是思考型 / 关不关得掉 / 预算地板给多少）都查同一张实测表；
    本函数只把表**翻译成界面契约**。`mode="none"` 有两种含义，靠
    `verification` 区分：`verified`（模型本来就不思考）与 `unverified`（没测过）。
    """
    from app.llm.thinking_models import THINK_OFF_CAPABLE, THINK_TUNABLE, lookup

    row = lookup(provider, model)
    mode = "none"
    field = None
    if row.thinking == THINK_TUNABLE:
        mode, field = "effort", "reasoning_effort"
    elif row.thinking == THINK_OFF_CAPABLE:
        mode, field = "disable", "thinking"

    return {
        "mode": mode,
        "field": field,
        "levels": row.levels if mode != "none" else (),
        "note": row.note,
        # 界面据此决定「没有开关」这句话怎么说。
        "verification": "verified" if row.thinking != "unverified" else "unverified",
        # 实测依据跟着能力一起下发：界面上能答「你凭什么是这样」。
        "evidence": row.evidence,
    }


PROVIDER_META: dict[str, dict] = {
    "zhipu": {
        "label": "智谱 GLM",
        "key_hint": "open.bigmodel.cn → 头像 → API Keys（形如 xxxxx.xxxxx）",
        "free_note": "GLM-4-Flash 永久免费不限 token；注册送 2000 万 token",
        # 首项必须是 `config.zhipu_model`（默认模型）。master 把默认升级到
        # `glm-4-flash-250414`（2025-04 快照，7 类抽取全填）时漏改本清单，
        # 导致下拉首项与运行时默认不一致 —— 由
        # test_model_lists_are_consistent_across_three_sources 守住。
        "default_models": [
            "glm-4-flash-250414",
            "glm-4-flash",
            "glm-z1-flash",
            "glm-4.6",
            "glm-4.5-air",
        ],
    },
    "siliconflow": {
        "label": "硅基流动",
        "key_hint": "cloud.siliconflow.cn → API Keys（形如 sk-...）",
        "free_note": "新用户送 2000 万 token；Qwen2.5-7B / GLM-4-9b 等轻量模型免费",
        "default_models": [
            "Qwen/Qwen2.5-7B-Instruct",
            "THUDM/glm-4-9b-chat",
            "Qwen/Qwen3-8B",
            "Qwen/Qwen2.5-72B-Instruct",
        ],
    },
    "minimax": {
        "label": "MiniMax",
        "key_hint": "platform.minimaxi.com → 账号管理 → 接口 Keys（形如 sk-cp-...，仅国内站可用）",
        "free_note": "实测唯一干净可用：M3.1-Flash-Preview（推理独立字段，正文不混思维链）；M3/M2.x 会把思维链混进正文",
        # 2026-11-13：MiniMax-Text-01 已下线（调用直接 404），从默认链移除；
        # 这里必须与 `config.settings.minimax_models` 保持同源，否则设置页会
        # 引导用户切到一个必然失败的模型（test_settings_roles 三源一致性守这条）。
        "default_models": ["MiniMax-M3.1-Flash-Preview"],
    },
    "openrouter": {
        "label": "OpenRouter",
        "key_hint": "openrouter.ai → Keys（形如 sk-or-v1-...）",
        "free_note": "免费档账号级 50 次/日限额（所有 :free 模型共享）",
        # 这四款是 2026-10-12 实测后选定的（见 config.py 的注释），
        # 未实测的 :free 模型不列入——切换链上放一个会 403/429 的模型等于白跑一轮。
        "default_models": [
            "apodex/apodex-1.1-mini:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "nvidia/nemotron-3.5-lightning:free",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
        ],
    },
    "dashscope": {
        "label": "阿里百炼 DashScope",
        "key_hint": "bailian.console.aliyun.com → API-KEY（形如 sk-...）",
        "free_note": "新用户各模型 100 万 token 免费额度",
        "default_models": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-flash"],
    },
    "openai": {
        "label": "通用 OpenAI 接口",
        # 这个渠道没有厂商默认值可依赖：端点就是配置本身，必须给完整地址。
        "key_hint": "填**完整 Base URL**（形如 https://your-host/v1，不要只填域名）；key 填该端点的 Bearer key",
        "free_note": (
            "任何实现了 /chat/completions + Bearer 鉴权的端点都能接：OpenAI 官方、"
            "自建 one-api / vLLM / LM Studio / LocalAI、第三方中转站。"
            "留空 Base URL 则回落官方 https://api.openai.com/v1"
        ),
        "default_models": ["gpt-4o-mini", "gpt-4o"],
    },
    "command-code": {
        "label": "Command Code",
        "key_hint": (
            "默认读环境变量 COMMAND_CODE_GOAT_KEY；此处留空即用环境变量。"
            "key 形如 cc-...，来自 commandcode.ai 的 Goat 套餐"
        ),
        "free_note": (
            "按量计费；deepseek-v4.1-flash 上下文 1M token，"
            "额度与 opencode go 独立"
        ),
        "default_models": ["deepseek/deepseek-v4.1-flash"],
    },
    "ollama": {
        "label": "本地模型（Ollama）",
        "key_hint": (
            "不需要 key。Ollama 默认端点 http://127.0.0.1:11434/v1；"
            "LM Studio 改成 http://127.0.0.1:1234/v1"
        ),
        "free_note": (
            "真正调用本机模型，不产生 API 费用；先 `ollama pull qwen2.5:7b`，"
            "模型名必须与 `ollama list` 里的一致"
        ),
        "default_models": ["qwen2.5:7b", "qwen2.5:14b", "llama3.1:8b"],
    },
    "local": {
        "label": "本地兜底（模板，不调模型）",
        "key_hint": "",
        "free_note": "确定性模板生成，离线可跑，非模型质量；要跑本地模型请选「本地模型（Ollama）」",
        "default_models": ["local-template"],
    },
}

# 渠道 → settings 里的 key 字段名 / model 字段名 / models(链) 字段名
_KEY_FIELD = {
    "openrouter": "openrouter_api_key",
    "zhipu": "zhipu_api_key",
    "siliconflow": "siliconflow_api_key",
    "minimax": "minimax_api_key",
    "dashscope": "dashscope_api_key",
    "openai": "openai_api_key",
    "command-code": "command_code_api_key",
    "ollama": "local_llm_api_key",
}
_MODEL_FIELD = {
    "openrouter": "openrouter_model",
    "zhipu": "zhipu_model",
    "siliconflow": "siliconflow_model",
    "minimax": "minimax_model",
    "dashscope": "llm_model",
    "openai": "openai_model",
    "command-code": "command_code_model",
    "ollama": "local_llm_model",
}
_MODELS_FIELD = {
    "openrouter": "openrouter_models",
    "zhipu": "zhipu_models",
    "siliconflow": "siliconflow_models",
    "minimax": "minimax_models",
    "dashscope": "dashscope_models",
    "openai": "openai_models",
    "command-code": "command_code_models",
    "ollama": "local_llm_models",
}

#: 渠道 → settings 里的 base_url 字段名。多数渠道就是 `{provider}_base_url`；
#: 本地模型刻意用 `local_llm_base_url`（避开 Ollama 自己的 OLLAMA_* 环境变量）。
_BASE_URL_FIELD = {
    "ollama": "local_llm_base_url",
}

#: 需要用户填自定义 base_url 的渠道（没有厂商默认地址可回落 / 端点即配置）。
_CUSTOM_URL_PROVIDERS: frozenset[str] = frozenset({"openai", "ollama"})

_DB_KEY_PROVIDER = "llm.provider"
_DB_KEY_MODEL = "llm.model"
_DB_KEY_MODELS = "llm.models"
#: KG 抽取独立渠道 + 自定义模型链的持久化键（与角色键同一套 AppSetting 机制）。
_DB_KEY_KG_EXTRACT_PROVIDER = "kg_extract.provider"
_DB_KEY_KG_EXTRACT_MODELS = "kg_extract.models"

# ------------------------------------------------------------------
# 角色（role）配置：settings 字段名与 AppSetting 键的映射
# ------------------------------------------------------------------

#: 角色配置的字段（与 `Settings.get_provider_config` 的返回契约一致）。
_ROLE_FIELDS: tuple[str, ...] = (
    "provider", "api_key", "base_url", "model", "max_tokens", "timeout",
    # 思考档位：low/medium/high/xhigh/max/off/default，空 = 由 effective_thinking 解析。
    "thinking",
    # 预算地板开关：on = 不足 FLOOR_FULL(32768/300) 自动抬高；off = 严格按用户值。
    "budget_floor",
)
#: 其中取整数的字段（AppSetting 里按字符串存，读回时转 int）。
_ROLE_INT_FIELDS: frozenset[str] = frozenset({"max_tokens", "timeout"})
#: 其中取布尔值的字段（持久化成 "on"/"off"，读回时解析）。
_ROLE_BOOL_FIELDS: frozenset[str] = frozenset({"budget_floor"})

#: 合法 role 集合（与 `app.llm.registry._VALID_ROLES` 同源，避免两处漂移）。
_ROLES: tuple[str, ...] = (WRITER_ROLE, ASSISTANT_ROLE, DISTILLER_ROLE)

#: 走「渠道 legacy 回退」的角色 = 除 Writer 之外的全部角色。
#: Writer 有自己的 legacy 单例路径（`_legacy_effective_config`），不在此列。
_CHANNEL_FALLBACK_ROLES: frozenset[str] = frozenset({ASSISTANT_ROLE, DISTILLER_ROLE})


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:4]}***{key[-4:]}"


def _legacy_effective_config() -> dict[str, Any]:
    """legacy Writer 路径当前生效的配置块（writer_provider 为空时用）。

    只读 settings 的 legacy 字段（`llm_provider` + 渠道 `*_api_key` /
    `*_model` / `*_base_url`）——与 `get_llm_provider()` 的构建口径一致，
    不复制构建逻辑，只做「展示用」的取值。
    """
    provider = (settings.llm_provider or "local").strip() or "local"
    key_field = _KEY_FIELD.get(provider)
    model_field = _MODEL_FIELD.get(provider)
    base_url = (settings.llm_base_url or "").strip()
    if not base_url and provider != "local":
        base_url = str(
            getattr(settings, _BASE_URL_FIELD.get(provider, f"{provider}_base_url"), "") or ""
        ).strip()
    return {
        "provider": provider,
        "api_key": str(getattr(settings, key_field, "") or "").strip() if key_field else "",
        "base_url": base_url,
        "model": str(getattr(settings, model_field, "") or "").strip() if model_field else "local-template",
        "max_tokens": settings.writer_max_tokens,
        "timeout": settings.writer_timeout,
        # legacy 路径也读角色级开关（writer_thinking 留空时由 effective_thinking
        # 解析成 MINIMAX_REASONING_EFFORT 或默认 high）。
        "thinking": settings.effective_thinking("writer", provider),
        "budget_floor": bool(settings.writer_budget_floor),
    }


def _resolve_role_config(role: str) -> dict[str, Any]:
    """按优先级链解析某个角色的当前配置（GET / 测试端点共用）。

    优先级：显式 role 配置 > providers.yaml 默认 > 渠道 legacy 回退。

    - `assistant` / `distiller`：直接委托 `settings.get_provider_config(role)`——
      第 2/3 档（含 dc39bbb 的渠道回退）已在那个函数里实现，此处不重复。
    - `writer`：`writer_provider` 非空 → 显式角色配置（第 1 档）；
      为空 → legacy 生效配置（Writer 单例的实际口径，见
      `_legacy_effective_config`）。
    """
    if role in _CHANNEL_FALLBACK_ROLES:
        return dict(settings.get_provider_config(role))
    if role == WRITER_ROLE:
        if (settings.writer_provider or "").strip():
            return {
                "provider": settings.writer_provider,
                "api_key": settings.writer_api_key or "",
                "base_url": settings.writer_base_url or "",
                "model": settings.writer_model or "",
                "max_tokens": settings.writer_max_tokens,
                "timeout": settings.writer_timeout,
                "thinking": settings.effective_thinking("writer", settings.writer_provider),
                "budget_floor": bool(settings.writer_budget_floor),
            }
        return _legacy_effective_config()
    raise ValueError(f"未知 role: {role!r}（应为 {_ROLES} 之一）")


def _active_role_provider(role: str) -> Any:
    """取该角色**当前生效**的 provider 单例（权威事实，不是配置推断）。

    writer    → `get_llm_provider()`（b58c763 原单例，生成链路实际用的就是它）；
    assistant → `ProviderRegistry.get('assistant')`（与 Writer 物理隔离）；
    distiller → `ProviderRegistry.get('distiller')`（第三条轨道）。
    三个工厂按设计都不抛错（缺 key 降级 LocalFallback），但仍兜一层：
    单例万一取不到，GET 也不能 500——降级为「配置视图 + is_fallback 未知」。
    """
    if role == WRITER_ROLE:
        return get_llm_provider()
    return get_provider_registry().get(role)


def _role_block(role: str) -> dict[str, Any]:
    """单个角色的配置视图（GET 出参的 `writer` / `assistant` 对象）。

    取值口径：
    - `provider` 来自配置解析（角色单例不记录渠道名，只有 label）；
    - `model` / `base_url` / `api_key` / `max_tokens` / `timeout` /
      `is_fallback` / `label` **优先取生效单例上的真实值**——单例才是
      真正在跑的那一个，配置字段可能因 mixed 状态（env 手改）而与
      单例不一致，那种时候「实际在用什么」比「配置写了什么」重要。
    - `thinking` / `budget_floor` / `thinking_control` 取**配置解析**结果：
      单例不记录这两项，只有配置层知道用户选了什么。
    """
    cfg = _resolve_role_config(role)
    try:
        active = _active_role_provider(role)
    except Exception:  # noqa: BLE001 - 单例取到不影响 GET 的其余部分
        active = None

    provider = (cfg.get("provider") or "local").strip() or "local"
    if active is not None:
        api_key = str(getattr(active, "api_key", "") or "")
        model = str(getattr(active, "model", "") or cfg.get("model") or "")
        base_url = str(getattr(active, "base_url", "") or cfg.get("base_url") or "")
        max_tokens = getattr(active, "max_tokens", None)
        if max_tokens is None:
            max_tokens = cfg.get("max_tokens")
        timeout = getattr(active, "timeout", None)
        if timeout is None:
            timeout = cfg.get("timeout")
        label = getattr(active, "label", "Local")
        is_fallback = type(active).__name__ == "LocalFallbackLLMProvider"
    else:
        api_key = str(cfg.get("api_key") or "")
        model = str(cfg.get("model") or "")
        base_url = str(cfg.get("base_url") or "")
        max_tokens = cfg.get("max_tokens")
        timeout = cfg.get("timeout")
        label = "(unavailable)"
        is_fallback = True

    # 本地模型不需要 key：占位串不算「已保存 key」。
    if provider == "ollama":
        api_key = ""
    return {
        "provider": provider,
        "model": model,
        "api_key_masked": _mask(api_key),
        "has_key": bool(api_key),
        "base_url": base_url,
        "max_tokens": max_tokens,
        "timeout": timeout,
        # 思考档与地板开关是「配置口径」（单例不记录它们），直接取解析结果。
        "thinking": str(cfg.get("thinking") or ""),
        "budget_floor": bool(cfg.get("budget_floor", True)),
        # 前端下拉直接用：mode/levels/note 一张表，不在界面上重复渠道知识。
        "thinking_control": thinking_control_for(provider, model),
        "is_fallback": is_fallback,
        "label": label,
    }


def _split_model_csv(raw: str) -> list[str]:
    """逗号分隔的模型链 → 去空保序列表（不去重，去重由 provider 侧负责）。"""
    return [m.strip() for m in (raw or "").split(",") if m.strip()]


def _channel_chain(provider: str) -> list[str]:
    """某渠道在 settings 里配置的候选链（`*_models`）；local 无链 → []。

    与 `_build_channel_provider` 的口径一致：它是「渠道默认链」的唯一来源。
    """
    field = _MODELS_FIELD.get(provider)
    if not field:
        return []
    return _split_model_csv(str(getattr(settings, field, "") or ""))


def _kg_extract_block() -> dict[str, Any]:
    """KG 抽取渠道 + 模型链的当前视图（GET 出参的 `kg_extract` 对象）。

    与三个角色块同口径：**优先读生效单例**（它才是抽取真正在用的），
    配置字段只用于回答「用户选了哪个渠道 / 是否跟随主渠道 / 链从哪来」。

    字段约定：
    - `provider`        ：已保存的显式渠道；"" = 跟随主渠道；
    - `follow_main`     ：是否跟随主渠道（前端据此显示语义说明）；
    - `effective_provider` / `effective_label` / `is_fallback`：实际生效值；
    - `models`          ：**当前实际生效**的模型链（首选在首位）；
    - `models_source`   ：`follow_main`（随主渠道）/ `channel`（所选渠道的链）
                          / `custom`（`kg_extract.models` 自定义链）；
    - `models_override` ：已保存的自定义链（空 = 没自定义）；
    - `channel_models`  ：各渠道的默认链（前端换渠道时用它回填输入框）；
    - `valid` / `error` ：配置非法时明确报出来，不静默回退主渠道。
    """
    configured = (settings.kg_extract_provider or "").strip()
    follow_main = not configured
    main_provider = (settings.llm_provider or "local").strip() or "local"
    effective_provider = main_provider if follow_main else configured.lower()
    override = _split_model_csv(settings.kg_extract_models)
    models_source = "custom" if override else ("follow_main" if follow_main else "channel")

    valid = follow_main or configured.lower() in KG_EXTRACT_CHANNELS
    error: str | None = None
    active = None
    if valid:
        try:
            active = get_kg_extract_provider()
        except ValueError as exc:
            # 未知渠道值：工厂只会在这里抛错（不静默回落），如实展示给前端。
            valid = False
            error = str(exc)
    else:
        allowed = "、".join(sorted(KG_EXTRACT_CHANNELS))
        error = (
            f"kg_extract.provider={configured!r} 不是已知渠道"
            f"（可选：空 = 跟随主渠道，或 {allowed}）。不会回退到主渠道。"
        )

    models: list[str] = []
    if active is not None and hasattr(active, "_model_chain"):
        models = list(active._model_chain())
    if not models:
        models = override or _channel_chain(effective_provider)

    # 展示标签优先用 PROVIDER_META（与渠道按钮同一套文案），避免出现
    # 「按钮写 智谱 GLM、生效行写 Zhipu」这种同名不同写的困惑。
    label = (
        PROVIDER_META.get(effective_provider, {}).get("label")
        or getattr(active, "label", None)
        or effective_provider
    )
    is_fallback = active is not None and type(active).__name__ == "LocalFallbackLLMProvider"

    # 只暴露「界面上有元数据」的渠道：否则前端会渲染出没有标签、没有
    # 默认模型链的裸按钮（opencode-go 就属于这种情况）。后端校验仍用完整
    # KG_EXTRACT_CHANNELS —— 已保存的旧配置不会被这里判非法。
    ui_channels = sorted(p for p in KG_EXTRACT_CHANNELS if p in PROVIDER_META)
    channel_models = {p: _channel_chain(p) for p in ui_channels}
    # 跟随主渠道时，输入框要按主渠道回填默认链；主渠道可能恰好不在 UI 列表里。
    if main_provider and main_provider not in channel_models:
        channel_models[main_provider] = _channel_chain(main_provider)

    return {
        "provider": configured,
        "follow_main": follow_main,
        "main_provider": main_provider,
        "effective_provider": effective_provider,
        "effective_label": label,
        "models": models,
        "models_source": models_source,
        "models_override": override,
        "channel_models": channel_models,
        "allowed_providers": ui_channels,
        "total_timeout": settings.kg_extract_total_timeout,
        "first_byte_timeout": settings.kg_extract_first_byte_timeout,
        "is_fallback": is_fallback,
        "valid": valid,
        "error": error,
    }


def _current(db: Session) -> dict:
    """当前生效配置 + 各渠道 key 脱敏状态（GET 用）。

    原有顶层字段（provider / model / keys / providers / effective）**逐字
    保留**——旧前端与既有测试都按这个形状读；新增的 `writer` /
    `assistant` 两个角色对象只做加法，不改变任何既有键的语义。
    """
    keys_status: dict[str, dict] = {}
    for provider, field in _KEY_FIELD.items():
        env_val = getattr(settings, field, "") or ""
        db_row = db.get(AppSetting, f"llm.{field}")
        db_val = (db_row.value if db_row else "") or ""
        val = db_val or env_val
        # command-code 的 key 通常只存在于进程环境变量里（不落库）。
        if not val and provider == "command-code":
            val = os.environ.get("COMMAND_CODE_GOAT_KEY", "")
        keys_status[provider] = {"has": bool(val), "masked": _mask(val) if val else None}

    provider = settings.llm_provider or "local"
    model_field = _MODEL_FIELD.get(provider)
    model = getattr(settings, model_field, "") if model_field else "local-template"

    # 当前实际生效的 provider 单例标签
    active = get_llm_provider()
    effective = {
        "provider": provider,
        "model": model,
        "label": getattr(active, "label", "Local"),
        "is_fallback": type(active).__name__ == "LocalFallbackLLMProvider",
    }
    return {
        "provider": provider,
        "model": model,
        "keys": keys_status,
        "providers": PROVIDER_META,
        # 每个渠道 + 当前模型能用的思考档位（拿不准就是 mode=none，
        # 界面上不给按了没反应的开关）。
        "thinking_options": {
            name: thinking_control_for(name, (meta.get("default_models") or [""])[0])
            for name, meta in PROVIDER_META.items()
        },
        "effective": effective,
        # 第 7 步：分角色视图（前端「模型设置」页的数据基础）。
        # 三个角色各一块，键名与前端 LlmConfig 的字段一一对应。
        "writer": _role_block(WRITER_ROLE),
        "assistant": _role_block(ASSISTANT_ROLE),
        "distiller": _role_block(DISTILLER_ROLE),
        # KG 抽取独立渠道 + 模型链（只影响抽取，不影响写正文）。
        "kg_extract": _kg_extract_block(),
    }


class KgExtractIn(StrictRequest):
    """KG 抽取渠道 + 模型链（`PUT /api/settings/llm` 的 `kg_extract` 字段）。

    `provider` 空串 = **跟随主渠道**（默认；抽取与写正文同渠道同链）；
    其它值必须属于 `KG_EXTRACT_CHANNELS`，否则 400——绝不静默回退主渠道。

    `models`：
    - 缺省（None）= 不改动已存的自定义链；若同时换了渠道，则清空自定义链，
      让链跟着新渠道走（避免拿着 A 渠道的链去打 B 渠道）；
    - `[]`（空数组）= 显式清空自定义链，回到所选渠道的默认链；
    - 非空 = 该链只供 KG 抽取使用，**不影响**写正文的候选链。
    """

    provider: str = ""
    models: list[str] | None = None


class LlmConfigIn(StrictRequest):
    """PUT /api/settings/llm 请求体。

    `role` 缺省 → legacy 模式（与第 6 步完全一致：`provider` 必填，
    只写传入的字段，`api_key` 空则保留原值）。
    `role` 给定 → 角色模式：写该角色的独立字段（持久化到
    `writer.*` / `assistant.*` / `distiller.*` 键），只重置该角色的 provider
    缓存。三角色的 provider/model/key/base_url/max_tokens/timeout/thinking/
    budget_floor 均可配（默认思考 high + 地板 on）。
    `kg_extract` 给定 → KG 抽取模式：写 `kg_extract.*` 键，只重置抽取单例。

    `base_url` / `max_tokens` / `timeout` / `thinking` / `budget_floor` 是角色
    模式的新字段；legacy 模式下携带它们 → 400（legacy 路径从不接受这些字段，
    静默忽略会造成「以为生效了」的假象，故 fail loud）。
    """

    #: legacy / 角色模式必填；`kg_extract` 模式下不需要（由 kg_extract 决定渠道）。
    provider: str | None = None
    api_key: str | None = None  # 空 = 不改该角色 key
    model: str | None = None
    models: list[str] | None = None  # 候选链（限流切换用；非 writer 角色不接受）
    base_url: str | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    #: 思考档位（low/medium/high/xhigh/max/off/default）；None = 不改。
    thinking: str | None = None
    #: 预算地板开关；None = 不改。False 时必须同时给出 max_tokens 与 timeout。
    budget_floor: bool | None = None
    role: Literal["writer", "assistant", "distiller"] | None = None
    #: KG 抽取独立渠道 + 模型链（只影响抽取；不碰 writer 的渠道与预算）。
    kg_extract: KgExtractIn | None = None


@router.get("/llm")
def get_llm_config(db: Session = Depends(get_db)) -> dict:
    return _current(db)


@router.get("/llm/local-models")
def list_local_models(base_url: str = "") -> dict:
    """列出 Ollama 本机已安装的模型（只读探测，不落库）。

    Ollama 的 `/api/tags` 在根路径，不在 OpenAI 兼容的 `/v1` 下；这里把
    用户填的 `http://host:11434/v1` 自动还原成根地址再探测，省得让用户
    同时维护两个地址。
    """
    raw = (base_url or settings.local_llm_base_url).strip().rstrip("/")
    root = raw[:-3] if raw.endswith("/v1") else raw
    req = urllib.request.Request(f"{root}/api/tags")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 —— 探测失败只回可读原因，不 500
        return {
            "ok": False,
            "base_url": raw,
            "models": [],
            "error": f"读取失败：{exc}"[:200],
        }
    items = data.get("models") if isinstance(data, dict) else None
    models = [
        str(item.get("name"))
        for item in (items or [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {"ok": True, "base_url": raw, "models": models, "error": None}


def _set(db: Session, key: str, value: str) -> None:
    """写入一个 AppSetting 键（upsert）。

    先 `flush()` 再 `get()`：同一事务里先前 `add` 的行还没发 SQL，直接
    `get` 会查不到而走 INSERT——同一个 key 两条 INSERT 撞 UNIQUE 直接 500。
    flush 不发 COMMIT，仍在同一事务内，安全。
    """
    db.flush()
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value


def _put_legacy(body: LlmConfigIn, provider: str, db: Session) -> dict:
    """legacy 模式：第 6 步行为的逐字保留（写 llm.* 键 + 全量热生效）。

    角色模式专属字段（base_url / max_tokens / timeout / thinking /
    budget_floor）在此出现即 400：这些字段在第 6 步的协议里不存在（会 422），
    静默接受会让调用方以为配置生效了——而 legacy 路径的 Writer 单例
    根本不读它们（预算/思考开关只走角色键）。
    """
    if (
        body.base_url is not None
        or body.max_tokens is not None
        or body.timeout is not None
        or body.thinking is not None
        or body.budget_floor is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "base_url / max_tokens / timeout / thinking / budget_floor "
                "仅在 role 模式下有效；legacy 模式（不带 role）不接受这些字段"
            ),
        )

    def _set_key(key: str, value: str) -> None:
        _set(db, key, value)

    # 1) 持久化（只写传入的字段；api_key 空则保留原值）
    _set_key(_DB_KEY_PROVIDER, provider)
    overrides: dict[str, str] = {"llm_provider": provider}
    if body.model:
        _set_key(_DB_KEY_MODEL, body.model.strip())
        overrides[_MODEL_FIELD.get(provider, "llm_model")] = body.model.strip()
    if body.models is not None:
        chain = [m.strip() for m in body.models if m.strip()]
        _set_key(_DB_KEY_MODELS, ",".join(chain))
        if provider in _MODELS_FIELD:
            overrides[_MODELS_FIELD[provider]] = ",".join(chain)
    key_field = _KEY_FIELD.get(provider)
    if key_field and body.api_key:
        _set_key(f"llm.{key_field}", body.api_key.strip())
        overrides[key_field] = body.api_key.strip()
    db.commit()

    # 2) 热生效：覆盖 settings + 重置 provider 单例缓存
    apply_runtime_config(overrides)
    return _current(db)


def _put_role(body: LlmConfigIn, provider: str, role: str, db: Session) -> dict:
    """角色模式：写 `writer.*` / `assistant.*` / `distiller.*` 键 + 只重置该角色缓存。

    与 legacy 模式的三个关键差异（都是有意的）：

    1. **持久化键按角色命名**（`writer.provider` / `assistant.api_key` /
       `distiller.timeout`）——三个角色各存各的，不再共享 `llm.*` 单槽；
    2. **热生效只重置该角色**：`ProviderRegistry.invalidate(role)` 内部
       对 writer 调 `reset_provider_cache()`、对 assistant 调
       `reset_assistant_provider_cache()`、对 distiller 调
       `reset_distiller_provider_cache()`，其余角色的单例原样不动；
    3. **writer 额外镜像 legacy 字段**：Writer 单例（b58c763 路径）只读
       `llm_provider` / 渠道 `*_api_key` 等 legacy 字段，不镜像则角色
       配置「落而不生效」——UI 显示已改、生成链路照旧的静默失效。
       其余角色绝不镜像 legacy（隔离的底线：Assistant / Distiller 的配置
       变化不得以任何形式改写 Writer 的字段）。

    字段守卫（fail-loud，与前端 RoleModelPanel 的能力开关一一对应）：
    - `models`              ：仅 writer 接受（限流切换链），其余角色带 → 400；
    - `max_tokens`/`timeout`：三角色统一可配（红线已解除），越界 → 400；
    - `thinking`            ：必须在 `THINKING_LEVELS` 里，未知档位 → 400；
    - `budget_floor=False`  ：必须同时给出 max_tokens 与 timeout（关地板就是
      「严格按用户值」，留空让默认值顶上等于关了个寂寞，故拒绝）。
    """
    if body.models is not None and role != WRITER_ROLE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{role} 角色是单模型配置，不接受 models 候选链"
                "（限流切换链仅 Writer 路径有）"
            ),
        )

    from app.llm.thinking_models import (
        MAX_TOKENS_MAX,
        MAX_TOKENS_MIN,
        THINKING_LEVELS,
        TIMEOUT_MAX,
        TIMEOUT_MIN,
    )

    if body.max_tokens is not None and not (
        MAX_TOKENS_MIN <= body.max_tokens <= MAX_TOKENS_MAX
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"max_tokens 必须在 {MAX_TOKENS_MIN}..{MAX_TOKENS_MAX} 之间"
                f"（收到 {body.max_tokens}）"
            ),
        )
    if body.timeout is not None and not (TIMEOUT_MIN <= body.timeout <= TIMEOUT_MAX):
        raise HTTPException(
            status_code=400,
            detail=(
                f"timeout 必须在 {TIMEOUT_MIN}..{TIMEOUT_MAX} 秒之间"
                f"（收到 {body.timeout}）"
            ),
        )
    thinking = None
    if body.thinking is not None:
        thinking = body.thinking.strip().lower()
        if thinking not in THINKING_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"thinking 必须是 {'/'.join(THINKING_LEVELS)} 之一"
                    f"（收到 {body.thinking!r}）"
                ),
            )
    if body.budget_floor is False and (
        body.max_tokens is None or body.timeout is None
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "budget_floor=off 时必须同时给出 max_tokens 与 timeout："
                "关闭地板意味着严格按用户值执行，两项留空会让默认值顶上，"
                "等同于没关。"
            ),
        )

    # ---- 1) 持久化角色键（只写传入的字段；api_key 空则保留原值）----
    _set(db, f"{role}.provider", provider)
    overrides: dict[str, Any] = {f"{role}_provider": provider}

    # 预算 / 思考 / 地板：三角色统一口径（writer 也允许改，红线已解除）。
    if body.max_tokens is not None:
        _set(db, f"{role}.max_tokens", str(body.max_tokens))
        overrides[f"{role}_max_tokens"] = body.max_tokens
    if body.timeout is not None:
        _set(db, f"{role}.timeout", str(body.timeout))
        overrides[f"{role}_timeout"] = body.timeout
    if thinking is not None:
        _set(db, f"{role}.thinking", thinking)
        overrides[f"{role}_thinking"] = thinking
    if body.budget_floor is not None:
        _set(db, f"{role}.budget_floor", "on" if body.budget_floor else "off")
        overrides[f"{role}_budget_floor"] = bool(body.budget_floor)

    if body.model:
        model = body.model.strip()
        _set(db, f"{role}.model", model)
        overrides[f"{role}_model"] = model
    if body.api_key:
        api_key = body.api_key.strip()
        _set(db, f"{role}.api_key", api_key)
        overrides[f"{role}_api_key"] = api_key
    # base_url：Assistant / Distiller 允许显式清空（"" = 回渠道默认，
    # build_provider 对空 base_url 会取 settings 的渠道默认值，安全）；
    # Writer 只覆盖非空——清空渠道 base_url 会让 Writer 单例拿到空 URL
    # （legacy 路径没有「回默认」的读取逻辑，只有 dashscope 有
    # `llm_base_url or dashscope_base_url` 一层回退）。
    #
    # 这里**只写一次** `{role}.base_url`：清空分支若另起一次 `_set`，同一事务
    # 里会出现两个同 key 的 pending AppSetting——`db.get` 看不到未 flush 的
    # 行，于是第二条走 INSERT，撞 UNIQUE 直接 500（实测踩过）。
    if body.base_url is not None:
        if role != WRITER_ROLE or body.base_url.strip():
            base_url = body.base_url.strip()
            _set(db, f"{role}.base_url", base_url)
            overrides[f"{role}_base_url"] = base_url

    if role == WRITER_ROLE:
        # ---- Writer：镜像到 legacy 字段，让 b58c763 单例真正热生效 ----
        _set(db, _DB_KEY_PROVIDER, provider)
        overrides["llm_provider"] = provider
        if body.model:
            model_field = _MODEL_FIELD.get(provider)
            if model_field:
                _set(db, _DB_KEY_MODEL, body.model.strip())
                overrides[model_field] = body.model.strip()
            if body.models is None:
                # 只换模型、没给候选链：把链收敛成这一个模型。否则旧渠道的
                # 候选链会留在库里（实测 MiniMax 链尾挂着 deepseek/…，在
                # MiniMax 端点必然 400，把真实错误也盖成 400）。
                chain_models = body.model.strip()
                _set(db, _DB_KEY_MODELS, chain_models)
                overrides["llm_models"] = chain_models
                if provider in _MODELS_FIELD:
                    _set(db, f"llm.{_MODELS_FIELD[provider]}", chain_models)
                    overrides[_MODELS_FIELD[provider]] = chain_models
        if body.api_key:
            key_field = _KEY_FIELD.get(provider)
            if key_field:
                _set(db, f"llm.{key_field}", body.api_key.strip())
                overrides[key_field] = body.api_key.strip()
        if body.base_url and body.base_url.strip():
            # 只覆盖、不清空：清空渠道 base_url 会让 Writer 单例拿到空 URL
            # （legacy 路径没有「回默认」的读取逻辑，只有 dashscope 有
            # `llm_base_url or dashscope_base_url` 一层回退）。
            url = body.base_url.strip()
            if provider != "local":
                base_field = _BASE_URL_FIELD.get(provider, f"{provider}_base_url")
                if hasattr(settings, base_field):
                    _set(db, f"llm.{base_field}", url)
                    overrides[base_field] = url
                if provider == "dashscope":
                    _set(db, "llm.llm_base_url", url)
                    overrides["llm_base_url"] = url
        if body.models is not None:
            chain = [m.strip() for m in body.models if m.strip()]
            _set(db, _DB_KEY_MODELS, ",".join(chain))
            overrides["llm_models"] = ",".join(chain)
            if provider in _MODELS_FIELD:
                _set(db, f"llm.{_MODELS_FIELD[provider]}", ",".join(chain))
                overrides[_MODELS_FIELD[provider]] = ",".join(chain)

    db.commit()

    # ---- 2) 热生效：覆盖 settings + **只**重置该角色的 provider 缓存 ----
    # 刻意不走 apply_runtime_config：那个函数无条件 reset_provider_cache()
    # （Writer 单例），角色模式下改 Assistant 不该顺带重建 Writer。
    for field, value in overrides.items():
        if value is None:
            continue
        if hasattr(settings, field):
            setattr(settings, field, value)
    get_provider_registry().invalidate(role)
    return _current(db)


def _put_kg_extract(body: KgExtractIn, db: Session) -> dict:
    """KG 抽取模式：写 `kg_extract.*` 键 + 只重置抽取单例。

    与角色模式的隔离底线一致——**不写任何 `llm.*` 键、不动 settings 上的
    渠道 key / 模型链、不重建 Writer 单例**。抽取渠道与写正文渠道互不影响。

    校验（fail-loud）：
    - `provider` 必须是 `KG_EXTRACT_CHANNELS` 之一或空串（空 = 跟随主渠道）；
      非法值 400，绝不静默回退主渠道（回退会把 `zhiphu` 这类笔误藏成
      「配置已生效」）。
    - `models` 里的空项被丢弃；全部为空 = 清空自定义链（回到渠道默认链）。
    """
    provider = (body.provider or "").strip().lower()
    if provider and provider not in KG_EXTRACT_CHANNELS:
        allowed = "、".join(sorted(KG_EXTRACT_CHANNELS))
        raise HTTPException(
            status_code=400,
            detail=(
                f"未知抽取渠道：{body.provider!r}"
                f"（可选：空 = 跟随主渠道，或 {allowed}）。不会回退到主渠道。"
            ),
        )

    previous = (settings.kg_extract_provider or "").strip().lower()
    _set(db, _DB_KEY_KG_EXTRACT_PROVIDER, provider)
    overrides: dict[str, Any] = {"kg_extract_provider": provider}

    if body.models is not None:
        chain = [m.strip() for m in body.models if m.strip()]
        _set(db, _DB_KEY_KG_EXTRACT_MODELS, ",".join(chain))
        overrides["kg_extract_models"] = ",".join(chain)
    elif provider != previous:
        # 换了渠道但没显式给链：旧渠道的自定义链必须清掉，否则等于拿着
        # A 渠道的模型名去打 B 渠道（表现为莫名其妙的 404/400）。
        _set(db, _DB_KEY_KG_EXTRACT_MODELS, "")
        overrides["kg_extract_models"] = ""

    db.commit()

    for field, value in overrides.items():
        if hasattr(settings, field):
            setattr(settings, field, value)
    # 只清抽取单例；Writer 单例原样不动（写正文的渠道与预算不受影响）。
    reset_kg_extract_provider_cache()
    return _current(db)


@router.put("/llm")
def put_llm_config(body: LlmConfigIn, db: Session = Depends(get_db)) -> dict:
    if body.kg_extract is not None:
        # 三种模式互斥：混用会让「这次到底改了谁」变得不可判定，直接 400。
        mixed = (
            body.role is not None
            or bool(body.provider)
            or bool(body.api_key)
            or bool(body.model)
            or body.models is not None
            or body.base_url is not None
            or body.max_tokens is not None
            or body.timeout is not None
            or body.thinking is not None
            or body.budget_floor is not None
        )
        if mixed:
            raise HTTPException(
                status_code=400,
                detail=(
                    "kg_extract 模式只接受 kg_extract 字段；不得与 "
                    "provider / role / api_key / model / models / base_url / "
                    "max_tokens / timeout / thinking / budget_floor 混用"
                ),
            )
        return _put_kg_extract(body.kg_extract, db)

    provider = (body.provider or "").strip()
    if provider not in PROVIDER_META:
        raise HTTPException(status_code=400, detail=f"未知渠道：{provider}")
    if body.role is None:
        return _put_legacy(body, provider, db)
    return _put_role(body, provider, body.role, db)


class LlmTestIn(StrictRequest):
    """POST /api/settings/llm/test 请求体。

    `role` 缺省 → legacy 行为（缺省 key/model 从 DB/.env 取）。
    `role` 给定 → 缺省 key/model/base_url 从**该角色当前生效配置**取
    （同一套优先级链）；表单渠道与角色已存渠道不一致时不回退——
    不拿 A 渠道的 key 去测 B 渠道。
    """

    provider: str
    api_key: str | None = None
    model: str | None = None
    role: Literal["writer", "assistant", "distiller"] | None = None


def _local_test(model: str, role: str | None) -> dict:
    p = build_provider("local", "", model)
    t0 = time.time()
    reply = p.chat("test", "test")
    out = {
        "ok": True,
        "model": model,
        "latency_ms": int((time.time() - t0) * 1000),
        "reply": reply[:60],
    }
    if role:
        out["role"] = role
    return out


class GenerationTierIn(StrictRequest):
    """POST /api/settings/generation-tier 请求体。

    档位是「设置」不是「每次请求的参数」——放进 generate-loop 的请求体，
    用户会在不知道烧多少 token 的情况下随手传 premium。
    """

    tier: str


@router.get("/generation-tiers")
def list_generation_tiers() -> dict:
    """列出三档生成配置（前端选档用），并标出当前生效的是哪一档。

    当前档位是**反查**出来的：拿 settings 里生效的 provider + model + style_rewrite
    去匹配档位定义。即使用户绕过档位接口手改了模型，也能显示真实归属
    （匹配不上就返回 None，前端显示「自定义」）。
    """
    from app.generation.tiers import list_tiers

    tiers = list_tiers()
    cur_provider = (settings.llm_provider or "").strip().lower()
    cur_model = ""
    for attr in ("zhipu_model", "minimax_model", "openrouter_model",
                 "dashscope_model", "siliconflow_model", "openai_model"):
        if cur_provider in attr:
            cur_model = str(getattr(settings, attr, "") or "").strip()
            break
    # 反查当前档位：provider + model + style_rewrite 三者全匹配才算。
    # 只看 provider/model 会把「极速」和「平衡」判成同一档——它们同模型，
    # 唯一区别就是 style_rewrite。
    cur_rewrite = bool(getattr(settings, "style_rewrite", True))
    matched = None
    for t in tiers:
        if (t["provider"] == cur_provider and t["model"] == cur_model
                and bool(t["style_rewrite"]) == cur_rewrite):
            matched = t["key"]
            break
    return {
        "tiers": tiers,
        "current": matched,
        "current_provider": cur_provider,
        "current_model": cur_model,
        "current_style_rewrite": cur_rewrite,
    }


@router.post("/generation-tier")
def set_generation_tier(body: GenerationTierIn, db: Session = Depends(get_db)) -> dict:
    """切换到指定档位：把 provider / model / max_tokens 写进 settings 并热生效。

    未知档位**报错不静默退回默认**——用户传错档位名却悄悄用免费模型，
    比直接报错更糟：他会以为自己在用高质量档。
    """
    from app.generation.tiers import resolve_tier, tier_settings_patch

    try:
        tier = resolve_tier(body.tier)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    patch = tier_settings_patch(tier)

    # 1) 持久化：写 llm.provider / llm.max_tokens / llm.<渠道>_model / llm.style_rewrite
    #
    # 布尔必须存成 "1"/"0" 而不是 "True"/"False"：`_hydrate_settings` 读回来时
    # 按 pydantic 字段类型解析，而 `bool("False") == True` 会让关闭档悄悄变成开启档。
    def _store(v):
        return ("1" if v else "0") if isinstance(v, bool) else str(v)

    _set(db, _DB_KEY_PROVIDER, tier.provider)
    for field, value in patch.items():
        if field == "llm_provider":
            continue
        _set(db, f"llm.{field}", _store(value))
    db.commit()

    # 2) 热生效（复用既有机制，重置 provider 单例缓存）
    #    同样不能 str(False) —— apply_runtime_config 直接 setattr，
    #    字符串 "False" 在布尔字段上是真值，会让 rewrite 永远开着。
    overrides: dict = {"llm_provider": tier.provider}
    for field, value in patch.items():
        if field == "llm_provider":
            continue
        overrides[field] = value
    apply_runtime_config(overrides)

    return {
        "tier": tier.to_public_dict(),
        "applied": patch,
    }


@router.post("/llm/test")
def test_llm_config(body: LlmTestIn, db: Session = Depends(get_db)) -> dict:
    """用给定（或已保存/默认）配置发一个真实小请求，验证 key 与模型可用。不落库。"""
    provider = (body.provider or "").strip()
    if provider not in PROVIDER_META:
        return {"ok": False, "error": f"未知渠道：{provider}"}

    if body.role is not None:
        # ---- 角色模式：缺省值从该角色当前生效配置取 ----
        cfg = _resolve_role_config(body.role)
        if (cfg.get("provider") or "") != provider:
            cfg = {}  # 表单渠道 ≠ 角色已存渠道 →不回退到别的渠道的 key
        key = (body.api_key or "").strip() or str(cfg.get("api_key") or "").strip()
        if not key and provider == "command-code":
            key = os.environ.get("COMMAND_CODE_GOAT_KEY", "").strip()
        model = (
            (body.model or "").strip()
            or str(cfg.get("model") or "").strip()
            or PROVIDER_META[provider]["default_models"][0]
        )
        base_url = str(cfg.get("base_url") or "").strip()
        if provider == "local":
            return _local_test(model, body.role)
        if not key and provider != "ollama":
            return {"ok": False, "role": body.role, "error": "尚未填写 API key"}
        t0 = time.time()
        try:
            p = build_provider(provider, key, model, base_url)
            reply = p.chat("你是连通性测试助手，只做简短回复。", "请只回复两个字：正常")
            return {
                "ok": True,
                "role": body.role,
                "model": model,
                "latency_ms": int((time.time() - t0) * 1000),
                "reply": (reply or "")[:60],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "role": body.role,
                "model": model,
                "error": str(exc)[:300],
            }

    # ---- legacy 模式：第 6 步行为逐字保留 ----
    # 解析 key：请求体 > DB 已存 > .env
    key = (body.api_key or "").strip()
    if not key:
        key_field = _KEY_FIELD.get(provider)
        if key_field:
            row = db.get(AppSetting, f"llm.{key_field}")
            key = (row.value if row else "") or (getattr(settings, key_field, "") or "")
    if not key and provider == "command-code":
        key = os.environ.get("COMMAND_CODE_GOAT_KEY", "").strip()
    model = (body.model or "").strip()
    if not model:
        model_field = _MODEL_FIELD.get(provider)
        model = (getattr(settings, model_field, "") if model_field else "") or \
            PROVIDER_META[provider]["default_models"][0]

    if provider == "local":
        return _local_test(model, None)
    if not key and provider != "ollama":
        return {"ok": False, "error": "尚未填写 API key"}

    t0 = time.time()
    try:
        p = build_provider(provider, key, model)
        reply = p.chat("你是连通性测试助手，只做简短回复。", "请只回复两个字：正常")
        return {
            "ok": True,
            "model": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "reply": (reply or "")[:60],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "model": model, "error": str(exc)[:300]}


def load_persisted_llm_config(db: Session) -> None:
    """启动时把 app_settings 里的 LLM 配置覆盖到 settings（热切换的持久化半边）。

    应用顺序（后者覆盖前者，优先级从高到低）：

    1. **角色键**（`writer.*` / `assistant.*` / `distiller.*`）——角色 UI
       写入的显式角色配置，是最新的用户意图；
    2. **legacy 键**（`llm.*`）——第 6 步及更早的写入，保持既有行为。

    `writer.*` 键会额外镜像到 legacy 字段（与 `_put_role` 同一套映射）：
    Writer 单例只读 legacy 字段，不镜像则重启后角色配置丢失生效。

    Assistant 键只写 `assistant_*` settings 字段，**绝不**碰 legacy——
    渠道回退由 `get_provider_config('assistant')` 在读取时完成
    （显式留空 → 按渠道名借 legacy key，dc39bbb 的行为不破）。
    """
    rows = db.execute(select(AppSetting)).scalars().all()
    stored = {r.key: r.value for r in rows}
    if not stored:
        return
    from app.llm.thinking_models import THINKING_LEVELS

    overrides: dict[str, Any] = {}

    # ---- 1) 角色键（显式角色配置，优先级最高）----
    for role in _ROLES:
        for field in _ROLE_FIELDS:
            raw = stored.get(f"{role}.{field}")
            if raw is None or raw == "":
                continue
            if field in _ROLE_INT_FIELDS:
                try:
                    overrides[f"{role}_{field}"] = int(raw)
                except ValueError:
                    continue  # 脏数据不当成配置，保持 yaml/env 默认
            elif field in _ROLE_BOOL_FIELDS:
                parsed = parse_bool(raw)
                if parsed is not None:
                    overrides[f"{role}_{field}"] = parsed
                continue  # 脏值跳过（与脏 int 同策略）
            elif field == "thinking":
                level = raw.strip().lower()
                if level in THINKING_LEVELS:
                    overrides[f"{role}_{field}"] = level
                continue  # 未知档位跳过，保持 yaml/env 默认
            else:
                overrides[f"{role}_{field}"] = raw

    # ---- 1.5) KG 抽取独立渠道 + 模型链（kg_extract.* 键）----
    # 用「键存在」而非「值非空」判断：显式保存的 ""（跟随主渠道）也要盖过
    # yaml/env 里的旧值，否则界面上改回「跟随主渠道」重启后又变回独立渠道。
    if _DB_KEY_KG_EXTRACT_PROVIDER in stored:
        overrides["kg_extract_provider"] = (
            stored[_DB_KEY_KG_EXTRACT_PROVIDER] or ""
        ).strip()
    if _DB_KEY_KG_EXTRACT_MODELS in stored:
        overrides["kg_extract_models"] = stored[_DB_KEY_KG_EXTRACT_MODELS] or ""

    # ---- 2) legacy 键（既有行为）----
    if stored.get(_DB_KEY_PROVIDER):
        overrides["llm_provider"] = stored[_DB_KEY_PROVIDER]
    if stored.get(_DB_KEY_MODEL):
        provider = stored.get(_DB_KEY_PROVIDER, settings.llm_provider)
        overrides[_MODEL_FIELD.get(provider, "llm_model")] = stored[_DB_KEY_MODEL]
    # 角色键优先：writer 显式选过模型时，legacy `llm.models` 视为**陈旧链**，
    # 不能再覆盖该渠道的候选链。否则换渠道后会把上一个渠道的模型 id 带进来
    #（实测：MiniMax 链里混入 `deepseek/deepseek-v4.1-flash`，在 MiniMax 端点
    # 必然 400，还会把首个模型的真实错误盖成「最后错误: HTTP 400」）。
    writer_model_explicit = bool((stored.get("writer.model") or "").strip())
    if stored.get(_DB_KEY_MODELS) and not writer_model_explicit:
        provider = stored.get(_DB_KEY_PROVIDER, settings.llm_provider)
        if provider in _MODELS_FIELD:
            overrides[_MODELS_FIELD[provider]] = stored[_DB_KEY_MODELS]
    # 渠道级候选链（`llm.<channel>_models`）：上面的 `llm.models` 只按**当前**
    # 主渠道映射，其它渠道自己的链不会被应用。
    #
    # KG 抽取解耦（`kg_extract.provider=zhipu`）要求抽取的模型链取库里
    # `llm.zhipu_models`，而主渠道是 minimax——这里只补 zhipu：
    #   - assistant / distiller 用 `models=[model]` 构造，不受本链影响；
    #   - writer 当前走 minimax，不走 zhipu 分支，正文生成链不受影响。
    # 不补其它渠道：补 minimax 会把正文的候选链从 yaml 默认改成库值，
    # 那属于「顺手改正文路径」，不在本次范围。
    if stored.get("llm.zhipu_models"):
        overrides["zhipu_models"] = stored["llm.zhipu_models"]
    for field in _KEY_FIELD.values():
        v = stored.get(f"llm.{field}")
        if v:
            overrides[field] = v

    # ---- 3) writer 角色键镜像到 legacy（Writer 单例的实际口径）----
    writer_provider = (stored.get("writer.provider") or "").strip()
    if writer_provider:
        overrides["llm_provider"] = writer_provider
        writer_model = (stored.get("writer.model") or "").strip()
        if writer_model:
            model_field = _MODEL_FIELD.get(writer_provider)
            if model_field:
                overrides[model_field] = writer_model
        writer_key = (stored.get("writer.api_key") or "").strip()
        if writer_key:
            key_field = _KEY_FIELD.get(writer_provider)
            if key_field:
                overrides[key_field] = writer_key
        writer_base = (stored.get("writer.base_url") or "").strip()
        if writer_base and writer_provider != "local":
            base_field = f"{writer_provider}_base_url"
            if hasattr(settings, base_field):
                overrides[base_field] = writer_base
            if writer_provider == "dashscope":
                overrides["llm_base_url"] = writer_base
        writer_models = (stored.get("writer.models") or "").strip()
        if writer_models:
            overrides["llm_models"] = writer_models
            if writer_provider in _MODELS_FIELD:
                overrides[_MODELS_FIELD[writer_provider]] = writer_models

    if overrides:
        apply_runtime_config(overrides)
