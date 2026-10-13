"""LLM 提供方实现。

接口约定（与 embeddings/provider.py 同构）：
- chat(system, user, constraints=None) -> str  续写/对话
- summarize(text) -> str                       生成段级总结，供链式注入
- get_llm_provider()                           工厂，未配 key 自动降级本地

constraints 仅本地兜底使用：把 M3.3 注入的角色/未填坑回显进确定性续写，
让离线验证能断言「约束确实进入了生成环」。DashScope 实现忽略该参数。
"""

import json
import logging
import time
import urllib.request

from app.config import settings

logger = logging.getLogger("llm.provider")

# 段级总结默认保留长度（本地兜底用，真实模型走 summarize 提示词）
_FALLBACK_SUMMARY_CHARS = 80

# 层级摘要滚上的兜底保留长度：卷摘要（要覆盖 8 段的内容）比主线骨架长。
_ROLLUP_FALLBACK_CHARS = {"volume": 200, "spine": 320}

# 滚上提示词：两者都必须**丢掉细节、保住因果与人物去向**——
# 卷摘要的读者是「拼前情的模型」，它需要的是「这一段时期发生了什么、谁去了哪」，
# 不是逐段复述（逐段复述等于没压缩，预算白控）。温度保持 0（沿用 _complete 默认）。
_ROLLUP_PROMPTS = {
    "volume": (
        "你是长篇小说创作助手。下面是一卷（若干连续段落）的分段摘要，"
        "请把它们合并成一段**不超过 120 字**的中文卷情概括："
        "交代这段时间里主线推进到哪、主要人物处在什么处境、留下了什么悬念。"
        "不要逐段罗列，不要评价文笔，只输出概括本身。"
    ),
    "spine": (
        "你是长篇小说创作助手。下面是这部小说各卷的情节点概括，"
        "请把它们压缩成一段**不超过 200 字**的「主线骨架」："
        "只保留贯穿全篇的因果链与人物命运走向，删掉一切阶段性细节。"
        "不要分点，不要复述原文，只输出这段骨架本身。"
    ),
}

# 真实 LLM（尤其 OpenRouter 免费档）偶发限流/网拥：
# - 同一模型对网络抖动做短退避重试（_RETRIES_PER_MODEL 次）；
# - 429/403（限流/无权限）直接换模型链上的下一个免费模型重试；
# - 全链失败才抛错，交由调用方降级。
_RETRIES_PER_MODEL = 2
_RETRYABLE_HTTP = {429, 500, 502, 503, 529}
# 429/403 重试当前模型无意义（免费档限流/无权限是配额问题），应换模型
_SWITCH_HTTP = {429, 403}


def _http_code(exc: Exception) -> int | None:
    return getattr(exc, "code", None)


def _is_http_error(exc: Exception, codes: set[int]) -> bool:
    return _http_code(exc) in codes


def _is_account_daily_limit(exc: Exception) -> bool:
    """OpenRouter 账号级日限额（free-models-per-day，所有 :free 模型共享）。

    这种 429 换模型无益——限额按账号计，不按模型计。读 body 识别后应直接失败，
    避免把整条候选链空跑一遍。HTTPError 是类文件对象，body 只能读一次。
    """
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return False
    return "free-models-per-day" in body or "openrouter_free_tier_daily" in body


def _is_retryable(exc: Exception) -> bool:
    """urllib 的 HTTPError 带 .code；其余网络抖动（超时/连接重置）也重试。"""
    if _http_code(exc) in _RETRYABLE_HTTP:
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ("timed out", "timeout", "connection reset", "broken pipe"))


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动，封顶 16s：2→4→8→（封顶）。"""
    base = min(2 ** (attempt + 1), 16)
    return base + (time.time() % 1)


class LLMProvider:
    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        raise NotImplementedError  # pragma: no cover - 接口

    def summarize(self, text: str) -> str:
        """段级总结，默认截断；子类可覆盖为真实调用。"""
        if not text:
            return ""
        return text[:_FALLBACK_SUMMARY_CHARS].strip() + (
            "…" if len(text) > _FALLBACK_SUMMARY_CHARS else ""
        )

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        """流式续写：默认实现把整段一次性 yield（非流式 provider 也兼容 SSE）。

        逐 delta 产出的子类（OpenAI 兼容）应重写此方法，降低长文首字延迟。
        """
        yield self.chat(system, user, constraints=constraints)

    def summarize_rollup(self, texts: list[str], level: str) -> str:
        """层级摘要的**滚上**（L1→L2 卷摘要 / L2→L3 主线骨架）。

        level ∈ {"volume", "spine"}：两者只是压缩强度和提示词不同。
        默认实现是确定性的截断兜底——**离线/无 key 时也必须产出稳定文本**，
        否则 `verify_summary_chain.py` 与回归测试的结果会随 provider 抖动。
        """
        joined = " ".join(t for t in texts if t).strip()
        if not joined:
            return ""
        limit = _ROLLUP_FALLBACK_CHARS[level] if level in _ROLLUP_FALLBACK_CHARS else 200
        return joined[:limit] + ("…" if len(joined) > limit else "")


def _apply_constraints(user: str, constraints: dict | None) -> str:
    """把 constraints（已锁定角色 / 未兑现伏笔）真正序列化进 prompt。

    旧实现只把 constraints 传给 LocalFallbackLLMProvider，真实 OpenAI 兼容 provider
    签名收下却从不使用——于是「约束进入生成环」只在离线兜底路径成立，生产模型完全
    拿不到角色约束。现统一在 user 末尾追加（不动 system，避免击穿静态提示词缓存）。
    """
    if not constraints:
        return user
    parts: list[str] = []
    chars = [str(c) for c in (constraints.get("characters") or []) if c]
    holes = [str(h) for h in (constraints.get("plotholes") or []) if h]
    if chars:
        parts.append(
            "【本段已锁定角色（必须保持一致，不得凭空新增主要人物）】" + "、".join(chars)
        )
    if holes:
        parts.append(
            "【尚未兑现的伏笔（可推进或隐晦呼应，不要集中回收）】" + "；".join(holes)
        )
    if not parts:
        return user
    return user + "\n\n" + "\n".join(parts)


class OpenAICompatibleChatProvider(LLMProvider):
    """OpenAI 兼容 /chat/completions 的公共实现（DashScope / OpenRouter 共用）。

    子类只需给出 api_key / base_url / model。extra_body 可追加厂商特有字段
    （如 OpenRouter 关闭 reasoning）。
    """

    label = "LLM"

    def __init__(self, api_key: str, base_url: str, model: str, extra_body: dict | None = None,
                 models: list[str] | None = None) -> None:
        if not api_key:
            raise ValueError(f"{self.label} 需要 API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = extra_body or {}
        # 候选模型链：去重保序，默认模型置首；仅首个之后的模型用于限流时切换
        chain = [model] + [m for m in (models or []) if m and m != model]
        self._models = chain

    def _model_chain(self) -> list[str]:
        return self._models

    def _complete(self, messages: list[dict]) -> str:
        payload_base = {
            "messages": messages,
            # 小说续写要克制的文风：温度压到 0.7，避免"漂亮但套路"的堆料输出
            "temperature": 0.7,
            "top_p": 0.9,
            **self.extra_body,
        }
        last_exc: Exception | None = None
        for model in self._model_chain():
            payload = {**payload_base, "model": model}
            body = json.dumps(payload).encode("utf-8")
            for attempt in range(_RETRIES_PER_MODEL):
                req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST")
                req.add_header("Authorization", f"Bearer {self.api_key}")
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    return (data["choices"][0]["message"].get("content") or "").strip()
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    # 429/403（限流/无权限）：换下一个模型；网络抖动：同模型短退避重试
                    if _is_http_error(exc, _SWITCH_HTTP):
                        if _is_account_daily_limit(exc):
                            logger.warning(
                                "%s 账号免费日限额已用完（所有 :free 模型共享，换模型无益），直接失败", self.label
                            )
                            raise RuntimeError(f"{self.label} chat 调用失败: {last_exc}") from last_exc
                        logger.warning("%s 模型 %s 受限（%s），切换下一个候选模型", self.label, model, _http_code(exc))
                        break
                    if not _is_retryable(exc) or attempt == _RETRIES_PER_MODEL - 1:
                        break
                    time.sleep(_backoff(attempt))
        raise RuntimeError(f"{self.label} chat 调用失败: {last_exc}") from last_exc

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        user = _apply_constraints(user, constraints)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._complete(messages)

    def stream_chat(self, system: str, user: str, constraints: dict | None = None):
        """流式续写：stream=True 逐 delta 产出，长文首字即显、不干等。

        429/403（限流/无权限）自动换模型链上的下一个免费模型重试；
        重试仅在「尚未产出任何内容」时生效——一旦开始吐字，半途失败直接抛错
        （由 SSE 处理层转成 error 事件），避免把已产出正文与重试用正文拼接污染。
        """
        payload_base = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _apply_constraints(user, constraints)},
            ],
            "temperature": 0.7,
            "top_p": 0.9,
            "stream": True,
            **self.extra_body,
        }
        last_exc: Exception | None = None
        for model in self._model_chain():
            payload = {**payload_base, "model": model}
            body = json.dumps(payload).encode("utf-8")
            for attempt in range(_RETRIES_PER_MODEL):
                yielded = False
                try:
                    req = urllib.request.Request(
                        f"{self.base_url}/chat/completions", data=body, method="POST"
                    )
                    req.add_header("Authorization", f"Bearer {self.api_key}")
                    req.add_header("Content-Type", "application/json")
                    with urllib.request.urlopen(req, timeout=180) as resp:
                        for raw in resp:
                            line = raw.decode("utf-8", "replace").strip()
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                            except Exception:  # noqa: BLE001 - 跳过不完整/心跳帧
                                continue
                            delta = obj.get("choices", [{}])[0].get("delta", {})
                            piece = delta.get("content") or ""
                            if piece:
                                yielded = True
                                yield piece
                    return
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    if yielded:
                        # 半途失败不换模型不重试：避免正文重复/拼接污染
                        raise RuntimeError(f"{self.label} stream 调用失败: {last_exc}") from last_exc
                    if _is_http_error(exc, _SWITCH_HTTP):
                        if _is_account_daily_limit(exc):
                            logger.warning(
                                "%s 账号免费日限额已用完（所有 :free 模型共享，换模型无益），直接失败", self.label
                            )
                            raise RuntimeError(f"{self.label} stream 调用失败: {last_exc}") from last_exc
                        logger.warning(
                            "%s 模型 %s 受限（%s），切换下一个候选模型", self.label, model, _http_code(exc)
                        )
                        break
                    if not _is_retryable(exc) or attempt == _RETRIES_PER_MODEL - 1:
                        break
                    time.sleep(_backoff(attempt))
        raise RuntimeError(f"{self.label} stream 调用失败: {last_exc}") from last_exc

    def summarize(self, text: str) -> str:
        if not text:
            return ""
        system = (
            "你是小说创作助手。请用一句中文（不超过 60 字）概括下面这段续写的核心进展："
            "包含发生了什么、涉及哪些角色、留下了什么悬念。只输出概括本身，不要解释。"
        )
        return self._complete([{"role": "system", "content": system}, {"role": "user", "content": text}])

    def summarize_rollup(self, texts: list[str], level: str) -> str:
        """真实模型的滚上调用（L1→L2 / L2→L3）。

        输入用**编号列表**而非裸拼接：编号能让模型看清「这是几段时间」，
        合并时更容易做因果归并而不是流水账。空输入直接返回，不发无效请求。
        """
        items = [str(t).strip() for t in texts if str(t).strip()]
        if not items:
            return ""
        system = _ROLLUP_PROMPTS.get(level, _ROLLUP_PROMPTS["volume"])
        user = "\n".join(f"{i}. {t}" for i, t in enumerate(items, start=1))
        out = self._complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return out.strip()


class DashScopeChatProvider(OpenAICompatibleChatProvider):
    label = "DashScope"

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        super().__init__(api_key, base_url, model)


class OpenRouterChatProvider(OpenAICompatibleChatProvider):
    """OpenRouter 免费模型 provider（默认关闭 reasoning，小说续写要吞吐）。

    2026-10 对 15 款 :free 模型做了真实中文续写实测，能用的按速度：

    | 模型 | 速度 | 中文质量 |
    |---|---|---|
    | apodex/apodex-1.1-mini:free | ~2.5s | 好，干净无杂质（默认） |
    | nvidia/nemotron-3-super-120b-a12b:free | ~5s | 好 |
    | nvidia/nemotron-3.5-lightning:free | ~6s | 快但夹英文、结尾失控 |
    | poolside/laguna-s-2.1:free | ~7s | **中英混杂严重，不建议** |
    | nvidia/nemotron-3-ultra-550b-a55b:free | ~22s | 最好（多段+结尾），长文用 |

    实测失败的：inkling*(403 无权限)、gemma-4*(429 限流)、lfm-2.5(400)。
    免费档限流普遍，遇 429/403 时按 models 链自动换下一个可用模型（见 _model_chain）。
    """

    label = "OpenRouter"

    def __init__(self, api_key: str, base_url: str, model: str, reasoning: bool = False,
                 models: list[str] | None = None) -> None:
        extra: dict = {}
        if not reasoning:
            # 显式关闭推理：不返回思维链，降低延迟与 token 消耗
            extra["reasoning"] = {"enabled": False}
        super().__init__(api_key, base_url, model, extra_body=extra, models=models)


class ZhipuChatProvider(OpenAICompatibleChatProvider):
    """智谱 GLM（open.bigmodel.cn）：GLM-4-Flash 永久免费、不限 token，中文写作强。

    2026-10 官网政策：GLM-4-Flash / GLM-Z1-Flash 完全免费（并发 30），注册送 2000 万 token 礼包。
    小说中文续写/构思首选兜底渠道；链上限流时切换 glm-4-flash ↔ glm-z1-flash。
    """

    label = "Zhipu"


class SiliconFlowChatProvider(OpenAICompatibleChatProvider):
    """硅基流动 SiliconFlow（api.siliconflow.cn）：国产开源模型最全，国内低延迟。

    新用户送 2000 万 token；Qwen2.5-7B-Instruct、GLM-4-9B-chat 等轻量模型可长期白嫖。
    """

    label = "SiliconFlow"


class MiniMaxChatProvider(OpenAICompatibleChatProvider):
    """MiniMax（国内站 api.minimaxi.com）：中文创作强，必选 M3.1-Flash-Preview。

    2026-10-12 实测（决定默认模型与 thinking 参数）：
    - M3.1-Flash-Preview：推理走独立字段 reasoning_content，content 干净 → 首选；
      但它「强制 adaptive thinking」，传 thinking:{"type":"disabled"} 会 400
      （"requires adaptive thinking; thinking.type='disabled' is not allowed"）。
    - M3 / M2.7 / M2.5：把 <think> 思维链混进 content，且传 disabled 也压不住，
      只能由 engine._strip_thinking 兜底剥离 → 不作默认，也不入切换链。
    """

    label = "MiniMax"

    # 强制 adaptive thinking 的模型：禁传 thinking 参数，否则 400
    _ADAPTIVE_THINKING_MODELS = ("MiniMax-M3.1-Flash-Preview",)

    def __init__(self, api_key: str, base_url: str, model: str,
                 models: list[str] | None = None) -> None:
        extra: dict = {}
        if not model.startswith(self._ADAPTIVE_THINKING_MODELS):
            # 老模型（M3/M2.x）会吐思维链，能关则关；关不掉由 _strip_thinking 兜底
            extra["thinking"] = {"type": "disabled"}
        super().__init__(api_key, base_url, model, extra_body=extra, models=models)


class LocalFallbackLLMProvider(LLMProvider):
    """确定性模板续写（开发/测试用，非生产质量）。

    把注入的 constraints（角色 + 未填坑）回显进续写正文，保证离线可验证
    M3.3 约束确实流进了生成环；同时拼接前情/检索上下文占位，模拟真实续写结构。
    """

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        chars = (constraints or {}).get("characters") or []
        holes = (constraints or {}).get("plotholes") or []
        char_line = "、".join(chars) if chars else "（暂无锁定角色）"
        hole_line = "、".join(holes) if holes else "（暂无未填坑）"

        # 从 user 提示里抽取「前情提要 / 检索上下文 / 大纲」片段，拼进续写以增强可读性
        context_blob = ""
        for tag in ("前情提要", "检索上下文", "大纲"):
            marker = f"【{tag}】"
            if marker in user:
                start = user.index(marker) + len(marker)
                end = user.find("【", start)
                snippet = user[start : end if end != -1 else len(user)].strip()
                context_blob += f"\n（{tag}：{snippet[:120]}）"

        return (
            "【离线兜底·确定性续写】已锁定角色：" + char_line + "。待解悬念：" + hole_line + "。"
            + context_blob
            + "\n（续写正文占位：晨光漫过窗台，故事沿着上一节的余韵继续推进……）"
        )


_PROVIDER: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    if settings.llm_provider == "openrouter" and settings.openrouter_api_key:
        _PROVIDER = OpenRouterChatProvider(
            settings.openrouter_api_key,
            settings.openrouter_base_url,
            settings.openrouter_model,
            models=settings.openrouter_model_list,
        )
    elif settings.llm_provider == "zhipu" and settings.zhipu_api_key:
        _PROVIDER = ZhipuChatProvider(
            settings.zhipu_api_key,
            settings.zhipu_base_url,
            settings.zhipu_model,
            models=settings.zhipu_model_list,
        )
    elif settings.llm_provider == "siliconflow" and settings.siliconflow_api_key:
        _PROVIDER = SiliconFlowChatProvider(
            settings.siliconflow_api_key,
            settings.siliconflow_base_url,
            settings.siliconflow_model,
            models=settings.siliconflow_model_list,
        )
    elif settings.llm_provider == "minimax" and settings.minimax_api_key:
        _PROVIDER = MiniMaxChatProvider(
            settings.minimax_api_key,
            settings.minimax_base_url,
            settings.minimax_model,
            models=settings.minimax_model_list,
        )
    elif settings.llm_provider == "dashscope" and settings.dashscope_api_key:
        base = settings.llm_base_url or settings.dashscope_base_url
        _PROVIDER = DashScopeChatProvider(
            settings.dashscope_api_key, base, settings.llm_model
        )
    else:
        # 未配置 key 或显式 local：降级本地兜底，保证离线可跑
        _PROVIDER = LocalFallbackLLMProvider()
    return _PROVIDER


# —— 运行时配置热切换（前端「模型设置」页驱动） ——

def reset_provider_cache() -> None:
    """清掉 provider 单例缓存：下次 get_llm_provider() 按最新 settings 重建。"""
    global _PROVIDER
    _PROVIDER = None


def apply_runtime_config(overrides: dict[str, str | None]) -> None:
    """把持久化/前端传入的配置覆盖到 settings 并重置 provider 缓存（热切换）。

    overrides 的键为 settings 字段名（如 llm_provider / zhipu_api_key / zhipu_model）；
    值为 None 的项跳过。调用后下一次 LLM 调用即用新配置。
    """
    for field, value in overrides.items():
        if value is None:
            continue
        if hasattr(settings, field):
            setattr(settings, field, value)
    reset_provider_cache()


def build_provider(provider: str, api_key: str, model: str, base_url: str = "") -> LLMProvider:
    """按渠道名直接构造 provider 实例（测试端点用，不走缓存/单例）。"""
    if provider == "openrouter":
        url = base_url or settings.openrouter_base_url
        return OpenRouterChatProvider(api_key, url, model, models=[model])
    if provider == "zhipu":
        url = base_url or settings.zhipu_base_url
        return ZhipuChatProvider(api_key, url, model, models=[model])
    if provider == "siliconflow":
        url = base_url or settings.siliconflow_base_url
        return SiliconFlowChatProvider(api_key, url, model, models=[model])
    if provider == "minimax":
        url = base_url or settings.minimax_base_url
        return MiniMaxChatProvider(api_key, url, model, models=[model])
    if provider == "dashscope":
        url = base_url or settings.dashscope_base_url
        return DashScopeChatProvider(api_key, url, model)
    return LocalFallbackLLMProvider()
