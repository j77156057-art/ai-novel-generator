"""LLM 提供方（M4 分段生成环）。

- DashScopeChatProvider：通义千问对话，OpenAI 兼容 /chat/completions 端点。
- LocalFallbackLLMProvider：确定性模板，离线可测；会把注入的角色/未填坑回显进续写，
  便于在无 key 环境下验证 M3.3 约束确实流进了生成环（防漂移）。
- get_llm_provider()：按配置选择；未配 key 时自动降级本地兜底，保证离线可运行。
"""

from app.llm.provider import (
    DashScopeChatProvider,
    LLMProvider,
    LocalFallbackLLMProvider,
    OpenAICompatibleChatProvider,
    OpenRouterChatProvider,
    get_llm_provider,
)

__all__ = [
    "DashScopeChatProvider",
    "LLMProvider",
    "LocalFallbackLLMProvider",
    "OpenAICompatibleChatProvider",
    "OpenRouterChatProvider",
    "get_llm_provider",
]
