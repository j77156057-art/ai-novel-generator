from __future__ import annotations

import os

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_yaml_defaults(path: str) -> dict:
    """读 providers.yaml 默认配置块；缺失或解析失败时返回空 dict（向后兼容老部署）。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return data


# providers.yaml 默认值：单例加载一次，类字段默认值直接吃进去。
# env 仍可通过 WRITER_* / ASSISTANT_* 覆盖（pydantic-settings env 大小写不敏感）。
_PROVIDERS_YAML = _load_yaml_defaults(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "providers.yaml")
)
_WRITER_DEFAULTS = _PROVIDERS_YAML.get("writer") if isinstance(_PROVIDERS_YAML.get("writer"), dict) else {}
_ASSISTANT_DEFAULTS = _PROVIDERS_YAML.get("assistant") if isinstance(_PROVIDERS_YAML.get("assistant"), dict) else {}
_DISTILLER_DEFAULTS = _PROVIDERS_YAML.get("distiller") if isinstance(_PROVIDERS_YAML.get("distiller"), dict) else {}

# Assistant / Distiller **显式留空**时的渠道回退表（backend/config/providers.yaml 的注释承诺）：
#   <role>.api_key 为空 → 按 <role>.provider 的渠道名去 legacy 字段取对应 *_api_key
#   <role>.base_url 为空 → 同上取对应 *_base_url
#
# 这里的「legacy」指的是 Writer 路径原本就有的老字段（zhipu_api_key / openrouter_base_url
# 等）——它们早在双轨拆分之前就存在，用户的 key 一直配在这些字段上。Assistant 只是
# **读零拷贝地借一把钥匙**，不写回、不改变 Writer 的任何字段，也不接 Writer 的模型链 /
# 单例 / 超时；两者仍然是完全独立的两条轨道。
#
# 未知渠道名（含空串）不在表内 → 返回 ""（保持「降级 LocalFallback」的既有行为），
# 不抛错：渠道名可能来自运行时 PUT /api/settings/llm 的自由输入。
_LEGACY_CHANNEL_FIELDS: dict[str, tuple[str, str]] = {
    "zhipu": ("zhipu_api_key", "zhipu_base_url"),
    "openrouter": ("openrouter_api_key", "openrouter_base_url"),
    "siliconflow": ("siliconflow_api_key", "siliconflow_base_url"),
    "minimax": ("minimax_api_key", "minimax_base_url"),
    "dashscope": ("dashscope_api_key", "dashscope_base_url"),
    # 通用 OpenAI 兼容接口：没有厂商默认值，端点必须来自本表（用户可覆盖）。
    "openai": ("openai_api_key", "openai_base_url"),
}

#: 走「渠道 legacy 字段回退」的角色（Writer 有自己的 legacy 路径，不走这张表）。
_CHANNEL_FALLBACK_ROLES: tuple[str, ...] = ("assistant", "distiller")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./novel_dev.db"
    seed_dir: str = "data/seed"
    chunk_size: int = 1200
    chunk_overlap: int = 200
    upload_max_bytes: int = 20_000_000

    # Embedding（M2 混合检索）
    embedding_provider: str = "dashscope"  # dashscope | local
    embedding_dim: int = 1536
    embedding_model: str = "text-embedding-v3"
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # LLM（M4 分段生成环）—— Writer 路径的全部原始字段，保留不动。
    llm_provider: str = "dashscope"  # dashscope | openrouter | local
    llm_model: str = "qwen-plus"
    llm_base_url: str = ""  # 空则复用 dashscope_base_url

    # OpenRouter（可选 provider：免费大上下文模型）
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # 实测 15 款 :free 模型后选定：
    #   apodex/apodex-1.1-mini:free            ~2.5s/段，中文干净（默认，适合分段循环生成）
    #   nvidia/nemotron-3-super-120b-a12b:free ~5s/段，质量好
    #   nvidia/nemotron-3-ultra-550b-a55b:free ~22s/段，1M 上下文，输出最好（长文用）
    openrouter_model: str = "apodex/apodex-1.1-mini:free"
    # 限流自动切换链（逗号分隔）：当前模型 429/403 时按序换下一个。
    # 默认链 = 实测可用（按速度）：mini → super → lightning → ultra。
    # 未列入：poolside/laguna（中英混杂）、inkling*(403)、gemma-4*(429)、lfm-2.5(400)。
    openrouter_models: str = (
        "apodex/apodex-1.1-mini:free,"
        "nvidia/nemotron-3-super-120b-a12b:free,"
        "nvidia/nemotron-3.5-lightning:free,"
        "nvidia/nemotron-3-ultra-550b-a55b:free"
    )

    # 智谱 GLM（open.bigmodel.cn）：GLM-4-Flash 永久免费、不限 token，中文写作强，国内低延迟
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_model: str = "glm-4-flash"
    # 候选链：默认 glm-4-flash（免费不限量，写作主力）→ glm-z1-flash（带推理，慢但稳）
    # → glm-4.6 / glm-4.5-air（付费档，长文与复杂结构更好）。链尾两款不免费，仅作末位兜底。
    zhipu_models: str = "glm-4-flash,glm-z1-flash,glm-4.6,glm-4.5-air"

    # 硅基流动 SiliconFlow（api.siliconflow.cn）：国产开源模型最全，新用户送 2000 万 token
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "Qwen/Qwen2.5-7B-Instruct"
    siliconflow_models: str = (
        "Qwen/Qwen2.5-7B-Instruct,"
        "THUDM/glm-4-9b-chat,"
        "Qwen/Qwen3-8B,"
        "Qwen/Qwen2.5-72B-Instruct"
    )

    # MiniMax（国内站 api.minimaxi.com；key 仅国内站有效，国际站 401）
    # 2026-10-12 实测 9 款模型：只有 M3.1-Flash-Preview 把推理放在独立字段 reasoning_content，
    # content 干净（JSON 可直接解析、中文质量好）；M3/M2.7/M2.5 等把 <think> 混进 content，
    # 且传 thinking:{"type":"disabled"} 无效。故默认与切换链均只用 M3.1-Flash-Preview；
    # MiniMax-Text-01 仅作链尾兜底（content 可能混思维链，实际很少走到）。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimaxi.com/v1"
    minimax_model: str = "MiniMax-M3.1-Flash-Preview"
    minimax_models: str = "MiniMax-M3.1-Flash-Preview,MiniMax-Text-01"

    # 阿里百炼 DashScope（兼容 OpenAI 协议）
    dashscope_models: str = "qwen-plus,qwen-max,qwen-turbo,qwen-flash"

    # ------------------------------------------------------------------
    # 通用 OpenAI 兼容接口（新增渠道 "openai"）
    #
    # 不是某一家厂商：任何实现了 /chat/completions + Bearer 鉴权的服务都能接
    # （OpenAI 官方 / 自建 one-api / vLLM / LM Studio / LocalAI / 中转站…）。
    # 因此 base_url **必须由用户给完整地址**（形如 https://xxx/v1），
    # 默认仅回落到官方 https://api.openai.com/v1。
    # 复用既有的 OpenAICompatibleChatProvider（纯 stdlib urllib），零新增依赖。
    # ------------------------------------------------------------------
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_models: str = "gpt-4o-mini,gpt-4o"

    # ------------------------------------------------------------------
    # ADR-2026-10-12 第 2 步：Writer / Assistant 双轨隔离（第 8 步再加 Distiller 第三轨）。
    #
    # 6 字段各自独立（provider / api_key / base_url / model / max_tokens / timeout），
    # 默认值来自 config/providers.yaml，env 可覆盖
    #（WRITER_* / ASSISTANT_* / DISTILLER_*）。
    #
    # Writer 字段默认空：工厂 get_provider_config("writer") 看到 provider 为空时，
    # 会委托给 get_llm_provider() 的原 Writer 单例（不复制构建逻辑），保证 b58c763
    # 的角色锁定 / 节拍约束 / KG 透传 / 提示词限长不被绕过。
    #
    # Assistant 字段默认 zhipu / glm-4-flash：永久免费不限 token、中文诊断稳，
    # 且与 Writer 默认 zhipu 共享渠道但**完全独立**的 api_key / timeout / max_tokens。
    # ------------------------------------------------------------------
    writer_provider: str = _WRITER_DEFAULTS.get("provider", "")
    writer_api_key: str = _WRITER_DEFAULTS.get("api_key", "")
    writer_base_url: str = _WRITER_DEFAULTS.get("base_url", "")
    writer_model: str = _WRITER_DEFAULTS.get("model", "")
    writer_max_tokens: int = int(_WRITER_DEFAULTS.get("max_tokens", 2048))
    writer_timeout: int = int(_WRITER_DEFAULTS.get("timeout", 120))

    assistant_provider: str = _ASSISTANT_DEFAULTS.get("provider", "zhipu")
    assistant_api_key: str = _ASSISTANT_DEFAULTS.get("api_key", "")
    assistant_base_url: str = _ASSISTANT_DEFAULTS.get("base_url", "")
    assistant_model: str = _ASSISTANT_DEFAULTS.get("model", "glm-4-flash")
    assistant_max_tokens: int = int(_ASSISTANT_DEFAULTS.get("max_tokens", 1024))
    assistant_timeout: int = int(_ASSISTANT_DEFAULTS.get("timeout", 60))

    # ---- 蒸馏模型（Distiller，第 3 个角色）----
    # 用途：**文风档案抽取**（把参考语料压成可注入的文风特征），与
    # NarrativeTier 的「蒸馏档位」（full / balanced / fast，采样与计算档位）
    # 不是一回事——档位决定「抽多细」，本角色决定「用哪个模型抽」。
    # 它是长任务（一次要读几十段语料），所以默认 timeout 明显大于 Assistant。
    distiller_provider: str = _DISTILLER_DEFAULTS.get("provider", "zhipu")
    distiller_api_key: str = _DISTILLER_DEFAULTS.get("api_key", "")
    distiller_base_url: str = _DISTILLER_DEFAULTS.get("base_url", "")
    distiller_model: str = _DISTILLER_DEFAULTS.get("model", "glm-4-flash")
    distiller_max_tokens: int = int(_DISTILLER_DEFAULTS.get("max_tokens", 2048))
    distiller_timeout: int = int(_DISTILLER_DEFAULTS.get("timeout", 180))

    def _legacy_channel_field(self, provider: str, field: str) -> str:
        """按渠道名取 legacy 字段值（Assistant / Distiller 的 api_key / base_url 回退用）。

        Args:
            provider: 渠道名（zhipu / openrouter / siliconflow / minimax / dashscope / openai）。
            field: 'api_key' 或 'base_url'。

        Returns:
            对应 legacy 字段的字符串值。渠道名未知（或空）时返回空串——让
            build_assistant_provider() 继续走 LocalFallback 降级，绝不抛错。
        """
        key_field, url_field = _LEGACY_CHANNEL_FIELDS.get((provider or "").strip().lower(), ("", ""))
        name = key_field if field == "api_key" else url_field
        if not name:
            return ""
        value = getattr(self, name, "")
        return str(value or "").strip()

    @property
    def openrouter_model_list(self) -> list[str]:
        """解析候选模型链：去空、去重、保序。"""
        return self._dedup_models(self.openrouter_models)

    @property
    def zhipu_model_list(self) -> list[str]:
        return self._dedup_models(self.zhipu_models)

    @property
    def siliconflow_model_list(self) -> list[str]:
        return self._dedup_models(self.siliconflow_models)

    @property
    def minimax_model_list(self) -> list[str]:
        return self._dedup_models(self.minimax_models)

    @property
    def dashscope_model_list(self) -> list[str]:
        return self._dedup_models(self.dashscope_models)

    @property
    def openai_model_list(self) -> list[str]:
        """通用 OpenAI 兼容渠道的候选链。"""
        return self._dedup_models(self.openai_models)

    @staticmethod
    def _dedup_models(csv: str) -> list[str]:
        seen: list[str] = []
        for m in csv.split(","):
            m = m.strip()
            if m and m not in seen:
                seen.append(m)
        return seen

    # ------------------------------------------------------------------
    # 按 role 返回 6 字段独立 config 的工厂（ADR-2026-10-12 第 2 步 / 第 5 步第 1 步）。
    # ------------------------------------------------------------------
    def get_provider_config(self, role: str) -> dict[str, object]:
        """按 role 读取 writer / assistant / distiller 的独立配置块。

        - role="writer"     : 字段名 = writer_* / 6 字段；与 legacy llm_provider 共存但
                              **不冲突**。委托方（registry._build）若发现 writer_provider
                              为空，会自动委托给原 get_llm_provider() 单例，不复制构建逻辑。
        - role="assistant"  : 字段名 = assistant_* / 6 字段；从不读 legacy llm_provider。
        - role="distiller"  : 字段名 = distiller_* / 6 字段；与 assistant 同构（同样走
                              渠道 legacy 回退），但默认 timeout 更大（长任务）。

        assistant / distiller 共用「渠道回退」逻辑：显式留空时按 provider 渠道名
        去 `_LEGACY_CHANNEL_FIELDS` 借对应的 `*_api_key` / `*_base_url`。
        渠道 "openai" 也在这张表里——通用 OpenAI 兼容接口的端点默认来自
        `openai_base_url`，用户可覆盖。

        返回 dict 字段稳定：provider / api_key / base_url / model / max_tokens / timeout，
        这是 ProviderRegistry 与 build_*_provider() 的唯一契约。

        抛出 ValueError 当 role 不是已知值（让上层立刻 fail-fast）。
        """
        if role == "writer":
            return {
                "provider": self.writer_provider,
                "api_key": self.writer_api_key,
                "base_url": self.writer_base_url,
                "model": self.writer_model,
                "max_tokens": self.writer_max_tokens,
                "timeout": self.writer_timeout,
                # 标记：writer 空 provider 时由工厂委托给原 Writer 单例
                "_fallback_to_legacy": not bool(self.writer_provider),
            }
        if role in _CHANNEL_FALLBACK_ROLES:
            # assistant / distiller 同构：按 role 前缀取各自的 6 个字段，
            # 不复制两份代码（避免以后加第 4 个角色时又漏一处）。
            provider = str(getattr(self, f"{role}_provider") or "").strip()
            api_key = str(getattr(self, f"{role}_api_key") or "").strip()
            base_url = str(getattr(self, f"{role}_base_url") or "").strip()
            # providers.yaml 承诺的回退：显式留空 → 按渠道名去 legacy 字段取。
            # 修复前 assistant_api_key 恒为空（yaml 里 assistant.api_key 就是 ""），
            # 于是 build_assistant_provider() 恒定降级 LocalFallback——Assistant 从来
            # 没调过真实模型。注释一直是对的，是代码没跟上。
            # 显式配置优先：非空时**不**被 legacy 值覆盖。
            if not api_key:
                api_key = self._legacy_channel_field(provider, "api_key")
            if not base_url:
                base_url = self._legacy_channel_field(provider, "base_url")
            return {
                "provider": provider,
                "api_key": api_key,
                "base_url": base_url,
                "model": str(getattr(self, f"{role}_model") or "").strip(),
                "max_tokens": getattr(self, f"{role}_max_tokens"),
                "timeout": getattr(self, f"{role}_timeout"),
                "_fallback_to_legacy": False,
            }
        raise ValueError(f"未知 role: {role!r}（应为 'writer' / 'assistant' / 'distiller'）")


settings = Settings()
