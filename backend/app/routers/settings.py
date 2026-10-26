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

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.llm.provider import apply_runtime_config, build_provider, get_llm_provider
from app.llm.registry import (
    ASSISTANT_ROLE,
    DISTILLER_ROLE,
    WRITER_ROLE,
    get_provider_registry,
)
from app.models import AppSetting
from app.schemas import StrictRequest

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 渠道元数据：供前端下拉与说明（key 获取地址 / 免费政策 / 默认候选模型）
#: 各渠道的「思考控制」方式 —— **按厂家文档与实测**填，没验证过的不写。
#:
#: mode 的含义：
#:   "effort"   只能调档（reasoning_effort），**关不掉**；省略 = 最高档
#:   "disable"  能开关（thinking={"type":"disabled"}）
#:   "none"     该渠道/模型不支持控制（不发任何字段）
#:
#: 为什么不能给一个全局「思考强度」下拉：三家写法互不相同，且同一渠道的不同模型
#: 也可能不同（MiniMax M3.1 只能调档，老 M3 能关）。给错字段的后果是
#: **按了没反应**（服务端静默忽略），比没有这个开关更糟。
THINKING_CONTROL: dict[str, dict] = {
    "minimax": {
        "mode": "effort",
        "field": "reasoning_effort",
        # 实测 2026-10-26：M3.1 系（含 Preview）吃这个字段；
        # 老 M3 不吃（传了反而更慢）—— 见 provider.MiniMaxChatProvider 的注释。
        "models": ("MiniMax-M3.1-Flash", "MiniMax-M3.1-Flash-Preview"),
        # 只给**实测能写出正文**的三档。xhigh / max 在写手场景写不出正文
        # （xhigh：reasoning 8190 把 8192 预算吃满，finish=length，正文 0 字；
        #   max：>300s 未返回）⇒ 不提供。
        "levels": ("low", "medium", "high"),
        "note": "M3.1 关不掉思考，只能调档；省略档位 = 最高档（最慢最贵，且写不出正文）",
    },
    "deepseek": {
        "mode": "disable",
        "field": "thinking",
        # 实测 2026-10-26：flash 3.4s→2.4s、v4-pro 27.8s→8.2s，且 reasoning 归零。
        "models": ("deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-v4-pro"),
        "levels": ("default", "off"),
        "note": "能关掉思考（实测 v4-pro 27.8s → 8.2s）",
    },
    "openai": {
        # 通用 OpenAI 兼容渠道：可以为任何厂商服务，所以**按模型名认**。
        # DeepSeek 走这条渠道时也命中（`/v1` + Bearer，与本渠道同形）。
        "mode": "disable",
        "field": "thinking",
        "models": ("deepseek-chat", "deepseek-reasoner", "deepseek-flash", "deepseek-v4-pro"),
        "levels": ("default", "off"),
        "note": "通用渠道：DeepSeek 系可关思考；其它模型不保证，未验证",
    },
}

#: 渠道 → 该渠道下「模型名 → 控制方式」的解析结果。
def thinking_control_for(provider: str, model: str = "") -> dict:
    """这个渠道 + 这个模型能用哪种思考控制？拿不准就返回 {"mode": "none"}。

    **宁可返回 none，也不给一个按了没反应的开关** —— 服务端对不认识的字段是
    静默忽略的，用户会以为生效了。

    两条容易踩的坑（都实测过）：
    1. **同一个渠道的不同模型规则不同**：MiniMax M3.1 只能调档（关不掉），
       而老 M3 不认 effort（传 low 反而更慢 22.8s vs 19.9s），它走 thinking=disabled。
    2. **通用兼容渠道按模型名认厂商**：DeepSeek 就是从这条渠道进来的。
    """
    provider = (provider or "").strip().lower()
    model = (model or "").strip()

    if provider == "minimax":
        if model.startswith("MiniMax-M3.1"):
            # 只能调档：关不掉（disabled → 400 "requires adaptive thinking"），省略 = max
            #
            # 2026-10-26 两轮实测（写 2400 字，共 15 次调用）：
            #   档位    成功/总   用时       reasoning    裸台词段(项目尺)
            #   low      7/7     55~99s    92~398       70~92%
            #   medium   6/7     40~57s    250~3203     4~98%
            #   high     6/6     53~127s   429~2835     66~89%
            #   xhigh    2/3     150~169s  5763~8191    **1 次正文 0 字**（8192 打满）
            #   max(省略) **0/2  >300s 未返回**
            # ⇒ 三个可用档位**形态分不出高下**（组内方差远大于组间差异），
            #   能分辨的只有用时（medium 快约 25%）与思考 token（low 最省）。
            # ⇒ **xhigh / max 不提供**：它们在写手场景要么正文 0 字，要么读超时。
            return {"mode": "effort", "field": "reasoning_effort",
                    "levels": ("low", "medium", "high"),
                    "note": "关不掉思考，只能调档。实测三档形态差不多；"
                            "medium 快约 25%，low 最省思考 token。"
                            "xhigh/max 写手场景会写不出正文（超 300 秒或正文为空），不提供"}
        if model.startswith("MiniMax-M3"):
            # 老 M3：不认 effort（实测传了更慢）；能关，代码里已默认关
            return {"mode": "disable", "field": "thinking",
                    "levels": ("off",),
                    "note": "老 M3 只能关思考（它不认 effort，默认就关着）"}
        return {"mode": "none", "field": None, "levels": (), "note": ""}

    if provider == "openai":
        # 通用兼容渠道：只有实测过的 DeepSeek 系给出开关
        if any(model.startswith(m) for m in ("deepseek-chat", "deepseek-reasoner",
                                             "deepseek-flash", "deepseek-v4-pro")):
            # 2026-10-26 真跑（2400 字续写，两种模型各两组）—— 两个模型的**结论相反**：
            #
            #   flash   基线 25.8s / reasoning 87 / 纯台词段 41.0%
            #           关思考 18.6s / 0        / 41.2%      ⇒ **快 28%，质量基本不变**
            #
            # v4-pro 上「关思考会毁掉对白形态」这个说法**已被推翻**（同日复测 6 篇）：
            #   原来记的「基线 4.9% → 关掉 44.0%」不可信 —— 4.9% 那一篇的
            #   **含引号段只有 20 段**，比例在极小的分母上失真。
            #   复测 6 篇（3 场景 × 2 次）：纯台词段给出 0.0% / 12.5% / 15.4% /
            #   42.9% / 71.4% / 100.0% —— 其中 100% 那篇全文 10 段、**1 个引号**。
            #   换成不吃分母的绝对量：v4-pro 每篇 2400~2900 字只有 **1~13 段对白**，
            #   对白段长中位 72 字（参考书 30 字）、段内叙述字中位 43 字（参考书 24 字）
            #   ⇒ **它是对白写得少，不是写得好**。
            #   用时也顶不住：6 次平均 173s，最长 291s（单段落就快撞 300s 超时线）。
            #
            # ⇒ 两个 DeepSeek 模型都给「可关」，但理由不同：
            #   flash —— 关了又快 28%、质量不变，**建议关**；
            #   v4-pro —— 关了不省时间，且它本来就少写对白，**建议保持默认**。
            if model.startswith("deepseek-v4-pro"):
                return {"mode": "disable", "field": "thinking",
                        "levels": ("default", "off"),
                        "note": "建议保持默认：实测关掉几乎不省时间（64.6s→63.9s），"
                                "而它每篇只有 1~13 段对白、平均 173s，最长 291s。"}
            return {"mode": "disable", "field": "thinking",
                    "levels": ("default", "off"),
                    "note": "可关：实测快 28%（25.8s→18.6s），对白形态基本不变"}
        return {"mode": "none", "field": None, "levels": (), "note": ""}

    spec = THINKING_CONTROL.get(provider)
    if spec is None:
        return {"mode": "none", "field": None, "levels": (), "note": ""}
    return {
        "mode": spec["mode"],
        "field": spec["field"],
        "levels": spec["levels"],
        "note": spec["note"],
    }


PROVIDER_META: dict[str, dict] = {
    "zhipu": {
        "label": "智谱 GLM",
        "key_hint": "open.bigmodel.cn → 头像 → API Keys（形如 xxxxx.xxxxx）",
        "free_note": "GLM-4-Flash 永久免费不限 token；注册送 2000 万 token",
        # 与 `config.zhipu_models` 的 CSV 与 `_default_model_for("zhipu")` 三处一致
        # （首项 = 默认模型 = config.zhipu_model = glm-4-flash-250414）。
        "default_models": [
            "glm-4-flash-250414", "glm-4-flash", "glm-z1-flash", "glm-4.6", "glm-4.5-air",
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
        "default_models": [
            "MiniMax-M3.1-Flash-Preview", "MiniMax-M3", "MiniMax-M3.1-Flash", "MiniMax-Text-01",
        ],
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
    "local": {
        "label": "本地兜底（无需 key）",
        "key_hint": "",
        "free_note": "确定性模板生成，离线可跑，非模型质量",
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
}
_MODEL_FIELD = {
    "openrouter": "openrouter_model",
    "zhipu": "zhipu_model",
    "siliconflow": "siliconflow_model",
    "minimax": "minimax_model",
    "dashscope": "llm_model",
    "openai": "openai_model",
}
_MODELS_FIELD = {
    "openrouter": "openrouter_models",
    "zhipu": "zhipu_models",
    "siliconflow": "siliconflow_models",
    "minimax": "minimax_models",
    "dashscope": "dashscope_models",
    "openai": "openai_models",
}

#: 需要用户填自定义 base_url 的渠道（没有厂商默认地址可回落 / 端点即配置）。
_CUSTOM_URL_PROVIDERS: frozenset[str] = frozenset({"openai"})

_DB_KEY_PROVIDER = "llm.provider"
_DB_KEY_MODEL = "llm.model"
_DB_KEY_MODELS = "llm.models"

# ------------------------------------------------------------------
# 角色（role）配置：settings 字段名与 AppSetting 键的映射
# ------------------------------------------------------------------

#: 角色配置的六个字段（与 `Settings.get_provider_config` 的返回契约一致）。
_ROLE_FIELDS: tuple[str, ...] = (
    "provider", "api_key", "base_url", "model", "max_tokens", "timeout",
    # 思考档位（2026-10-26）。取值由渠道决定：
    #   MiniMax M3.1 → low/medium/high/xhigh/max（关不掉，只能调档）
    #   DeepSeek / 通用渠道 → default/off（off = 关掉思考）
    #   其它渠道 → 不出现（未验证的控制字段按下去没反应，比没有更糟）
    "thinking",
)
#: 其中取整数的字段（AppSetting 里按字符串存，读回时转 int）。
_ROLE_INT_FIELDS: frozenset[str] = frozenset({"max_tokens", "timeout"})

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
        base_url = str(getattr(settings, f"{provider}_base_url", "") or "").strip()
    return {
        "provider": provider,
        "api_key": str(getattr(settings, key_field, "") or "").strip() if key_field else "",
        "base_url": base_url,
        "model": str(getattr(settings, model_field, "") or "").strip() if model_field else "local-template",
        "max_tokens": settings.writer_max_tokens,
        "timeout": settings.writer_timeout,
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

    return {
        "provider": provider,
        "model": model,
        "api_key_masked": _mask(api_key),
        "has_key": bool(api_key),
        "base_url": base_url,
        "max_tokens": max_tokens,
        "timeout": timeout,
        "is_fallback": is_fallback,
        "label": label,
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
    }


class LlmConfigIn(StrictRequest):
    """PUT /api/settings/llm 请求体。

    `role` 缺省 → legacy 模式（与第 6 步完全一致：`provider` 必填，
    只写传入的字段，`api_key` 空则保留原值）。
    `role` 给定 → 角色模式：写该角色的六个字段（持久化到
    `writer.*` / `assistant.*` 键），只重置该角色的 provider 缓存。

    `base_url` / `max_tokens` / `timeout` 是角色模式的新字段；legacy
    模式下携带它们 → 400（legacy 路径从不接受这三个字段，静默忽略
    会造成「以为生效了」的假象，故 fail loud）。
    """

    provider: str
    api_key: str | None = None  # 空 = 不改该角色 key
    model: str | None = None
    models: list[str] | None = None  # 候选链（限流切换用；非 writer 角色不接受）
    base_url: str | None = None
    max_tokens: int | None = None
    timeout: int | None = None
    role: Literal["writer", "assistant", "distiller"] | None = None


@router.get("/llm")
def get_llm_config(db: Session = Depends(get_db)) -> dict:
    return _current(db)


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

    角色模式专属字段（base_url / max_tokens / timeout）在此出现即 400：
    这三个字段在第 6 步的协议里不存在（会 422），静默接受会让调用方
    以为配置生效了——而 legacy 路径的 Writer 单例根本不读它们。
    """
    if body.base_url is not None or body.max_tokens is not None or body.timeout is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "base_url / max_tokens / timeout 仅在 role 模式下有效；"
                "legacy 模式（不带 role）不接受这三个字段"
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
    - `max_tokens`/`timeout`：writer 带 → 400（基线固定）；
      assistant / distiller 接受——蒸馏是长任务，timeout 必须能调大。
    """
    if body.models is not None and role != WRITER_ROLE:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{role} 角色是单模型配置，不接受 models 候选链"
                "（限流切换链仅 Writer 路径有）"
            ),
        )
    if role == WRITER_ROLE and (body.max_tokens is not None or body.timeout is not None):
        raise HTTPException(
            status_code=400,
            detail=(
                "Writer 的 max_tokens / timeout 由 b58c763 基线固定"
                "（chat timeout=120 / stream_timeout=180），本端点不提供改写入口；"
                "GET 的 writer 对象仍会如实展示单例上的真实值"
            ),
        )
    if body.max_tokens is not None and body.max_tokens <= 0:
        raise HTTPException(status_code=400, detail="max_tokens 必须为正整数")
    if body.timeout is not None and body.timeout <= 0:
        raise HTTPException(status_code=400, detail="timeout 必须为正整数")

    # ---- 1) 持久化角色键（只写传入的字段；api_key 空则保留原值）----
    _set(db, f"{role}.provider", provider)
    overrides: dict[str, Any] = {f"{role}_provider": provider}

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

    if role != WRITER_ROLE:
        # Assistant / Distiller：max_tokens / timeout 都允许改。
        # 蒸馏是长任务（一次读几十段语料），timeout 尤其要给得开——
        # 所以这里**不做**「只有 assistant 能改」的二元判断。
        if body.max_tokens is not None:
            _set(db, f"{role}.max_tokens", str(body.max_tokens))
            overrides[f"{role}_max_tokens"] = body.max_tokens
        if body.timeout is not None:
            _set(db, f"{role}.timeout", str(body.timeout))
            overrides[f"{role}_timeout"] = body.timeout
    else:
        # ---- Writer：镜像到 legacy 字段，让 b58c763 单例真正热生效 ----
        _set(db, _DB_KEY_PROVIDER, provider)
        overrides["llm_provider"] = provider
        if body.model:
            model_field = _MODEL_FIELD.get(provider)
            if model_field:
                _set(db, _DB_KEY_MODEL, body.model.strip())
                overrides[model_field] = body.model.strip()
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
                base_field = f"{provider}_base_url"
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


@router.put("/llm")
def put_llm_config(body: LlmConfigIn, db: Session = Depends(get_db)) -> dict:
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
    去匹配档位定义。这样即使用户绕过档位接口手改了模型，也能显示真实归属
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
        model = (
            (body.model or "").strip()
            or str(cfg.get("model") or "").strip()
            or PROVIDER_META[provider]["default_models"][0]
        )
        base_url = str(cfg.get("base_url") or "").strip()
        if provider == "local":
            return _local_test(model, body.role)
        if not key:
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
    model = (body.model or "").strip()
    if not model:
        model_field = _MODEL_FIELD.get(provider)
        model = (getattr(settings, model_field, "") if model_field else "") or \
            PROVIDER_META[provider]["default_models"][0]

    if provider == "local":
        return _local_test(model, None)
    if not key:
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
            else:
                overrides[f"{role}_{field}"] = raw

    # ---- 2) legacy 键（既有行为）----
    if stored.get(_DB_KEY_PROVIDER):
        overrides["llm_provider"] = stored[_DB_KEY_PROVIDER]
    if stored.get(_DB_KEY_MODEL):
        provider = stored.get(_DB_KEY_PROVIDER, settings.llm_provider)
        overrides[_MODEL_FIELD.get(provider, "llm_model")] = stored[_DB_KEY_MODEL]
    if stored.get(_DB_KEY_MODELS):
        provider = stored.get(_DB_KEY_PROVIDER, settings.llm_provider)
        if provider in _MODELS_FIELD:
            overrides[_MODELS_FIELD[provider]] = stored[_DB_KEY_MODELS]
    for field in _KEY_FIELD.values():
        v = stored.get(f"llm.{field}")
        if v:
            overrides[field] = v
    # 蒸馏并发覆盖（0 = 按渠道自动，见 app.style.narrative._resolve_concurrency）。
    # 单独列出而不并进 _KEY_FIELD：那张表是「渠道 → api_key 字段」的映射，
    # 语义不同，混进去会让后续读代码的人以为并发也是按渠道配的。
    _dc = stored.get("llm.distill_max_concurrent")
    if _dc:
        try:
            overrides["distill_max_concurrent"] = int(_dc)
        except ValueError:
            pass  # 脏数据不当成配置，保持 yaml/env 默认

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
