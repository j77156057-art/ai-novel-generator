"""LLM 提供方（M4 分段生成环 + ADR-2026-10-12 双轨隔离）。

- DashScopeChatProvider：通义千问对话，OpenAI 兼容 /chat/completions 端点。
- LocalFallbackLLMProvider：确定性模板，离线可测；会把注入的角色/未填坑回显进续写，
  便于在无 key 环境下验证 M3.3 约束确实流进了生成环（防漂移）。
- get_llm_provider()：Writer 单例（b58c763 行为完全保留）。
- get_assistant_provider()：Assistant 单例（与 Writer 完全隔离；ProviderRegistry 的
  默认构造路径，外部绝大多数场景用 ProviderRegistry.get(role) 而非直接调本函数）。
- get_distiller_provider()：Distiller 单例（文风档案抽取专用，第三条轨道）。
- GenericOpenAIChatProvider：通用 OpenAI 兼容渠道（渠道名 "openai"）的实现。

三个角色的路由统一走 `app.llm.registry.ProviderRegistry`（role → provider）；
`routers/settings.py` 与 `config.Settings.get_provider_config(role)` 是配置的
唯一读写口径，不要在别处另起一份渠道判断。
"""

from app.llm.provider import (
    DashScopeChatProvider,
    GenericOpenAIChatProvider,
    LLMProvider,
    LocalFallbackLLMProvider,
    OpenAICompatibleChatProvider,
    OpenRouterChatProvider,
    ZhipuChatProvider,
    SiliconFlowChatProvider,
    MiniMaxChatProvider,
    build_assistant_provider,
    build_distiller_provider,
    build_provider,
    build_role_provider,
    get_assistant_provider,
    get_distiller_provider,
    get_llm_provider,
    reset_assistant_provider_cache,
    reset_distiller_provider_cache,
    reset_provider_cache,
)

__all__ = [
    "DashScopeChatProvider",
    "GenericOpenAIChatProvider",
    "LLMProvider",
    "LocalFallbackLLMProvider",
    "OpenAICompatibleChatProvider",
    "OpenRouterChatProvider",
    "ZhipuChatProvider",
    "SiliconFlowChatProvider",
    "MiniMaxChatProvider",
    "build_assistant_provider",
    "build_distiller_provider",
    "build_provider",
    "build_role_provider",
    "get_assistant_provider",
    "get_distiller_provider",
    "get_llm_provider",
    "reset_assistant_provider_cache",
    "reset_distiller_provider_cache",
    "reset_provider_cache",
]
