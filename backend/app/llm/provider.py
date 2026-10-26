"""LLM 提供方实现。

接口约定（与 embeddings/provider.py 同构）：
- chat(system, user, constraints=None) -> str  续写/对话
- summarize(text) -> str                       生成段级总结，供链式注入
- get_llm_provider() / get_assistant_provider() / get_distiller_provider()
                                               三轨工厂，按 role 隔离

constraints 仅本地兜底使用：把 M3.3 注入的角色/未填坑回显进确定性续写，
让离线验证能断言「约束确实进入了生成环」。DashScope 实现忽略该参数。

ADR-2026-10-12 第 2 步：Writer 单例（_PROVIDER）行为**完全不变**；新增 Assistant 单例
（_ASSISTANT_PROVIDER）与 Distiller 单例（_DISTILLER_PROVIDER）独立持有，
ProviderRegistry 在此之上做 role→provider 路由 + 双锁并发安全 + 失效重载。
任何改动都不得让某个角色的异常回落到另一个角色的缓存。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request

from app.config import settings
from app.llm.reasoning import ReasoningFilter, strip_reasoning

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
    #: 该渠道在**长 prompt 蒸馏**（约 1.6K 输入 / 0.3-0.7K 输出 token）下
    #: 实测的安全并发数。0 = 未实测，调用方用自己的默认值。
    #:
    #: 为什么需要按渠道分：限流口径不同，代码里一个全局常量必然有一边不合适。
    #:   Zhipu 官方写「并发 30」，但那是并发数口径；实测长 prompt 下并发 3
    #:   就 8/12 触发 429，因为撞的是 TPM（约 28K）而不是并发数。
    #: 蒸馏是几千次调用的长任务，并发定高了整条链路一直在 429 重试，
    #: 定低了白白浪费一晚上。
    max_concurrent_hint: int = 0

    def chat(self, system: str, user: str, constraints: dict | None = None,
             temperature: float | None = None) -> str:
        """chat 调用。temperature=None = 用渠道默认（创作 0.7）。

        **低温档给「机械改写」用**：形态校正、标点修复这类任务是**变换**不是创作，
        高温度会让同一份输入产出完全不同的结果（实测同一段正文、同一提示，
        一次把纯台词段 76% 改到 20%，一次改到 92% —— 方向都反了）。
        """
        raise NotImplementedError  # pragma: no cover - 接口

    async def async_chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        """v2.225+：异步包装同步 chat，用 loop.run_in_executor（避免 to_thread 跨 loop 问题）。

        narrative.py 蒸馏多 chunk 并发时 gather 此方法。注意：单 provider 实例的
        限流/锁/重试仍由 chat() 自己处理；并发只在不同请求间共享 default executor 池。
        子类可重写为原生异步实现（如 OpenAIAsyncClient）。
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.chat, system, user, constraints)

    def summarize(self, text: str) -> str:
        """段级总结，默认截断；子类可覆盖为真实调用。"""
        if not text:
            return ""
        return text[:_FALLBACK_SUMMARY_CHARS].strip() + (
            "…" if len(text) > _FALLBACK_SUMMARY_CHARS else ""
        )

    def stream_chat(self, system: str, user: str, constraints: dict | None = None,
                     stop_sequences: list[str] | None = None):
        """流式续写：默认实现把整段一次性 yield（非流式 provider 也兼容 SSE）。

        逐 delta 产出的子类（OpenAI 兼容）应重写此方法，降低长文首字延迟。
        stop_sequences：与 chat 同款，per-request 透传（不依赖单例 setter）。
        """
        yield self.chat(system, user, constraints=constraints)

    def set_stop_sequences(self, sequences: list[str] | None) -> None:
        """v2.6：设置下一次 chat/stream_chat 的 stop token 列表。

        节点粒度根治（最后攻破）—— 让模型在写到 stop marker 时停，
        避免"演完整个故事"。

        默认实现：不存（子类按需重写）。调用方在 chat 之前 set 一次即可。
        """
        # 默认 no-op——子类重写
        pass

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

    ADR-2026-10-12 增加 max_tokens / timeout / stream_timeout 三个字段，默认值与
    b58c763 的硬编码值**完全一致**（max_tokens=None 即「不写入请求体」；
    timeout=120 即 chat 路径 120 秒；stream_timeout=180 即流式续写 180 秒——
    长文首字到达前的整个连接窗口必须留够，否则会被截断）。
    任何调用方未显式传这三个参数时，Writer 行为**绝对不变**。
    """

    label = "LLM"

    def __init__(self, api_key: str, base_url: str, model: str, extra_body: dict | None = None,
                 models: list[str] | None = None,
                 max_tokens: int | None = None, timeout: int = 120,
                 stream_timeout: int = 180) -> None:
        if not api_key:
            raise ValueError(f"{self.label} 需要 API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = extra_body or {}
        # None = 不在请求体里加 max_tokens（保留 Writer 原行为，调用方需明确给值才注入）
        self.max_tokens = max_tokens
        # 最近一次调用的 finish_reason（'length' = 被 max_tokens 截断）。
        # 仅供诊断/测试；provider 是单例，多线程下可能互相覆盖，但用途就是打日志。
        self.last_finish_reason: str | None = None
        # chat 路径默认 120s = b58c763 硬编码值；Assistant 可独立调小（诊断必须快速失败）
        self.timeout = timeout
        # stream_chat 路径默认 180s = b58c763 硬编码值；**不能**与 chat 共用默认 120s，
        # 否则长文流式续写会从 180s 截到 120s（QA 报告 3.1 节已记）。Assistant 不走流式
        # 但 build_assistant_provider 仍显式传 stream_timeout=60 保持防御性。
        self.stream_timeout = stream_timeout
        # 候选模型链：去重保序，默认模型置首；仅首个之后的模型用于限流时切换
        chain = [model] + [m for m in (models or []) if m and m != model]
        self._models = chain

        # v2.6：stop token 列表——下一次 chat/stream_chat 时设到 payload。
        # None = 不传 stop（默认）。调用方在 chat 之前 set 一次。
        # 注意：provider 是单例，多线程下 stop 可能互相覆盖；调用方要负责
        # 同步（同 chat 调用链内 set+chat 是原子的）。
        self._stop_sequences: list[str] | None = None

    def set_stop_sequences(self, sequences: list[str] | None) -> None:
        """v2.6：设置下一次 chat/stream_chat 的 stop token 列表。

        注意：provider 是单例，多线程/多请求下 stop 可能互相覆盖。
        建议直接用 chat(stream_stop_sequences=...) / stream_chat(stop_sequences=...)
        参数传值（每次调用独立），不用这个 setter。
        """
        self._stop_sequences = list(sequences) if sequences else None

    def _model_chain(self) -> list[str]:
        return self._models

    def _complete(self, messages: list[dict], stop_sequences: list[str] | None = None,
                  max_tokens: int | None = None,
                  temperature: float | None = None) -> str:
        payload_base = {
            "messages": messages,
            # 小说续写要克制的文风：温度压到 0.7，避免"漂亮但套路"的堆料输出。
            # **机械改写**（形态校正/标点修复）要低温：那是变换不是创作，
            # 0.7 下同一份输入的输出会大幅漂移（实测方向都反过）。
            "temperature": 0.7 if temperature is None else float(temperature),
            "top_p": 0.9,
            **self.extra_body,
        }
        # per-request 的 max_tokens 优先（整段验收要回抄的 id 数量是可知的）；
        # 没给就用 provider 的默认值 —— Writer 路径行为不变。
        effective_max_tokens = max_tokens if max_tokens is not None else self.max_tokens
        if effective_max_tokens is not None:
            payload_base["max_tokens"] = effective_max_tokens
        # v2.6：stop token 列表——模型遇到任一 token 时停止生成。
        # 优先用本次调用传的 stop_sequences（per-request），回退到单例 set 的。
        effective_stop = stop_sequences if stop_sequences is not None else self._stop_sequences
        if effective_stop:
            payload_base["stop"] = effective_stop
        last_exc: Exception | None = None
        for model in self._model_chain():
            payload = {**payload_base, "model": model}
            body = json.dumps(payload).encode("utf-8")
            for attempt in range(_RETRIES_PER_MODEL):
                req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST")
                req.add_header("Authorization", f"Bearer {self.api_key}")
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    choice = data["choices"][0]
                    self._note_finish_reason(
                        choice.get("finish_reason"), self.label, model, self.max_tokens
                    )
                    return strip_reasoning(
                        choice["message"].get("content") or ""
                    ).strip()
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

    def _note_finish_reason(
        self,
        finish_reason: str | None,
        label: str,
        model: str,
        max_tokens: int | None,
    ) -> None:
        """记录模型的 ``finish_reason``；被 token 上限截断时告警。

        ## 为什么加这个

        实测（2026-10-14）：``max_tokens=400`` 时模型返回
        ``finish_reason="length"``，输出 619 字且**停在半句**
        （「李阳没有理会男子的质问，」）。而当时 ``provider.py`` 里
        ``finish_reason`` 出现 **0 次** —— 系统完全不知情，半截文字会被
        当成完整段落直接落库，故事从断句处继续，用户看不出来。

        注意「段长不足」和「被截断」是两件事：实测 ``max_tokens=2048``
        时 ``finish_reason="stop"``、只用了 1038/2048 token，说明模型
        常常是**自己**提前收手，那不是截断。所以本方法只在真的撞上上限
        时告警，不去猜测段长为何偏短。

        本方法只**告警 + 留痕**，不改返回内容：把截断段落直接拒掉会让
        长文生成在正常路径上突然失败，比截断更难用。是否要在引擎层
        拒绝/自动续写，那是另一个决定。
        """
        self.last_finish_reason = finish_reason
        if finish_reason == "length":
            logger.warning(
                "%s 模型 %s 撞上 max_tokens=%s 被**拦腰截断**（finish_reason=length）；"
                "这段正文是半截的，后续剧情会从断句处接上。若要更长，"
                "调高 config/providers.yaml 的 writer.max_tokens。",
                label, model, max_tokens,
            )

    def chat(self, system: str, user: str, constraints: dict | None = None,
             stop_sequences: list[str] | None = None,
             max_tokens: int | None = None,
             temperature: float | None = None) -> str:
        """chat 调用。

        stop_sequences：v2.6 —— 本次调用独立的 stop token 列表（推荐用这个，
        避免和 provider 单例共享的 set_stop_sequences 互相覆盖）。

        max_tokens：本次调用的输出上限（不给就用 provider 的默认值）。
        整段验收用它申请与「必须回抄的 id 数量」相称的额度 ——
        否则正文一长，响应就被拦腰截断，验收 fail closed（实测五段正文必现）。

        temperature：本次调用的温度（不给 = 0.7，创作档）。
        **机械改写要传低温**（0.1~0.3）—— 见基类 docstring。
        """
        user = _apply_constraints(user, constraints)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        return self._complete(messages, stop_sequences=stop_sequences,
                               max_tokens=max_tokens)

    def stream_chat(self, system: str, user: str, constraints: dict | None = None,
                    stop_sequences: list[str] | None = None):
        """流式续写：stream=True 逐 delta 产出，长文首字即显、不干等。

        stop_sequences：v2.6 —— 本次调用独立的 stop token 列表（同 chat）。
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
        if self.max_tokens is not None:
            payload_base["max_tokens"] = self.max_tokens
        # v2.6：stop token 列表——优先用本次调用传的（per-request），回退到单例
        effective_stop = stop_sequences if stop_sequences is not None else self._stop_sequences
        if effective_stop:
            payload_base["stop"] = effective_stop
        last_exc: Exception | None = None
        for model in self._model_chain():
            payload = {**payload_base, "model": model}
            body = json.dumps(payload).encode("utf-8")
            for attempt in range(_RETRIES_PER_MODEL):
                yielded = False
                finish_reason: str | None = None
                try:
                    req = urllib.request.Request(
                        f"{self.base_url}/chat/completions", data=body, method="POST"
                    )
                    req.add_header("Authorization", f"Bearer {self.api_key}")
                    req.add_header("Content-Type", "application/json")
                    with urllib.request.urlopen(req, timeout=self.stream_timeout) as resp:
                        # 推理块逐 token 到达，不能逐块简单替换——开标签会被
                        # token 边界切断（"<thi" + "nk>"）。过滤器对"可能是开
                        # 标签前缀"的尾巴持有不发，故不会漏出推理内容。
                        # 每次尝试新建：重试必须从干净状态重来。
                        rfilter = ReasoningFilter()
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
                            choice0 = obj.get("choices", [{}])[0]
                            if choice0.get("finish_reason"):
                                finish_reason = choice0["finish_reason"]
                            delta = choice0.get("delta", {})
                            piece = delta.get("content") or ""
                            if piece:
                                safe = rfilter.feed(piece)
                                if safe:
                                    yielded = True
                                    yield safe
                        tail = rfilter.flush()
                        if tail:
                            yielded = True
                            yield tail
                        if rfilter.saw_reasoning:
                            logger.info(
                                "%s 模型 %s 的流里含推理块，已剥离", self.label, model
                            )
                        self._note_finish_reason(
                            finish_reason, self.label, model, self.max_tokens
                        )
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

    def __init__(self, api_key: str, base_url: str, model: str, **kwargs) -> None:
        # **kwargs 透传 max_tokens / timeout（ADR-2026-10-12 第 2 步）；
        # 未传时 Writer 行为不变。
        super().__init__(api_key, base_url, model, **kwargs)


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
                 models: list[str] | None = None, **kwargs) -> None:
        extra: dict = {}
        if not reasoning:
            # 显式关闭推理：不返回思维链，降低延迟与 token 消耗
            extra["reasoning"] = {"enabled": False}
        super().__init__(api_key, base_url, model, extra_body=extra, models=models, **kwargs)


class ZhipuChatProvider(OpenAICompatibleChatProvider):
    """智谱 GLM（open.bigmodel.cn）：GLM-4-Flash 永久免费、不限 token，中文写作强。

    2026-10 官网政策：GLM-4-Flash / GLM-Z1-Flash 完全免费（并发 30），注册送 2000 万 token 礼包。
    小说中文续写/构思首选兜底渠道；链上限流时切换 glm-4-flash ↔ glm-z1-flash。
    """

    label = "Zhipu"

    # 实测（2026-10-20，真实蒸馏 prompt：1565 in + 293 out tokens）：
    #   并发 1  4/4   成功，15K TPM
    #   并发 2  8/8   成功，27K TPM   ← 上限
    #   并发 3  4/12  成功，8 个 429，32K TPM
    #   并发 5  4/12  成功，8 个 429，33K TPM
    # 即：免费档的真实约束是约 28K TPM。官方说的「并发 30」是并发数口径，
    # 短 prompt 能跑到，长 prompt 到不了。
    max_concurrent_hint = 2


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

    2026-10-26 补充：M3.1 系的思考**关不掉但可以调低** ——
    官方 effort 档位（low/medium/high/xhigh/max，省略 = max）。
    真跑见下面 `__init__` 的注释。**M3（老）不吃 effort**，行为不变。
    """

    label = "MiniMax"

    # 实测（2026-10-20，真实拆组蒸馏 prompt）：并发 15 时 24/24 全成功、
    # 0.86 req/s、114K TPM；并发 20 开始出现 HTTPError（15/24），
    # 并发 30 又能全通但不稳。取 15 作为 100% 成功的安全值。
    max_concurrent_hint = 15

    # 强制 adaptive thinking 的模型：禁传 thinking 参数，否则 400。
    #
    # 2026-10-20 实测（13 种参数形式全试过）：
    #   thinking={"type":"disabled"}                → 400 "requires adaptive thinking"
    #   reasoning_effort="none"                     → 400 "reasoning effort none is not allowed"
    #   reasoning_effort="low"/"minimal"            → 不降反升（104/174 vs 基线 96）
    #   chat_template_kwargs.enable_thinking=False  → 短任务 96→12，但长任务 5160→11548（无效）
    # 结论：**推理关不掉，只能靠加大 max_tokens 预算**。实测思考占 completion 的 88%，
    # 写 3000 字正文需要 32000 预算。
    #
    # 注意 "MiniMax-M3.1-Flash"（不带 -Preview）同样是 adaptive-thinking 模型，
    # 不在白名单里就会传 disabled 拿 400。
    _ADAPTIVE_THINKING_MODELS = (
        "MiniMax-M3.1-Flash-Preview",
        "MiniMax-M3.1-Flash",
    )

    #: adaptive thinking 模型的**预算地板**（max_tokens / 超时秒）——分两种：
    #:
    #: **没发 effort**（用官方默认 max）：思考照样吃预算 ⇒ 地板按最坏情况给
    #:   （32768 / 300s）。2026-10-25 真跑：一次生成用掉 **111.8 秒**就撞上
    #:   120 秒的默认超时线；max_tokens 8192 也不够 —— 思考先把预算吃掉了，
    #:   正文被拦腰截断（finish_reason='length'）。这条对「429 自动切到
    #:   M3.1-Flash」的链路同样要紧 —— 那条路今天就是带着 8192 跑的。
    MIN_ADAPTIVE_MAX_TOKENS = 32768
    MIN_ADAPTIVE_TIMEOUT = 300
    #:
    #: **发了 effort**（思考调低）：思考几乎不占预算 ⇒ 地板可以降一档
    #:   （8192 / 180s）。2026-10-26 真跑（effort=low，写 2400 字正文）：
    #:   36.0 秒、completion 2605、正文完整写出 ⇒ 8192 绰绰有余。
    MIN_ADAPTIVE_MAX_TOKENS_EFFORT = 8192
    MIN_ADAPTIVE_TIMEOUT_EFFORT = 180

    #: effort 的合法档位（官方：low / medium / high / xhigh / max；省略 = max）。
    #: 非法值**不发**（发了会被忽略，白白让人以为调了档）。
    _EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

    def __init__(self, api_key: str, base_url: str, model: str,
                 models: list[str] | None = None,
                 reasoning_effort: str | None = None, **kwargs) -> None:
        extra: dict = {}
        if model.startswith(self._ADAPTIVE_THINKING_MODELS):
            # adaptive thinking：思考关不掉（disabled/none → 400），但**可以调低**。
            #
            # 2026-10-26 真跑（api.minimaxi.com，OpenAI 兼容端点，字段名 reasoning_effort）：
            #   2400 字续写（max_tokens=8000）
            #     省略 effort(=max)：66.7s  completion 8000 打满  finish=length  **正文为空**
            #     effort=low        ：36.0s  completion 2605      finish=stop    正文完整 4079 字
            #   300 字小任务：M3.1-Flash 默认 20.5s/3081 token → low 5.2s/262 token
            #                  M3.1-Flash-Preview 默认 20.9s/2160 → low 13.8s/284
            #   **M3（老）不吃 effort**（传 low 反而更慢）—— 它不在本分支里，行为不变。
            #
            # 非 adaptive 分支**一个字节都不改**（ADR-2026-10-12：调用方没显式
            # 传这三个参数时，Writer 行为绝对不变）。
            kwargs = dict(kwargs)
            effort = (reasoning_effort or "").strip().lower()
            if effort in self._EFFORT_LEVELS:
                extra["reasoning_effort"] = effort
            elif effort:
                logging.getLogger(__name__).warning(
                    "minimax reasoning_effort=%r 不是合法档位（%s），已忽略",
                    reasoning_effort, "/".join(self._EFFORT_LEVELS),
                )
            # 预算地板按「有没有真的把思考调低」给：
            #   调低了 ⇒ 思考几乎不占预算 ⇒ 地板降一档（实测 2605 token / 36 秒够用）；
            #   没调低 ⇒ 思考照旧吃预算 ⇒ 地板保持最坏情况（否则正文会被静默截断）。
            if extra.get("reasoning_effort"):
                floor_tokens = self.MIN_ADAPTIVE_MAX_TOKENS_EFFORT
                floor_timeout = self.MIN_ADAPTIVE_TIMEOUT_EFFORT
            else:
                floor_tokens = self.MIN_ADAPTIVE_MAX_TOKENS
                floor_timeout = self.MIN_ADAPTIVE_TIMEOUT
            kwargs["max_tokens"] = max(
                int(kwargs.get("max_tokens") or 0), floor_tokens
            )
            kwargs["timeout"] = max(
                int(kwargs.get("timeout") or 0), floor_timeout
            )
            kwargs["stream_timeout"] = max(
                int(kwargs.get("stream_timeout") or 0), floor_timeout
            )
        else:
            # 老模型（M3/M2.x）会吐思维链，能关则关；关不掉由 _strip_thinking 兜底。
            #
            # 2026-10-20 实测（900 字改写任务，同一 prompt）：
            #   M3 默认        147s  reasoning 16445  句长 49.2  引号 15
            #   M3 关思考       15s  reasoning None   句长 60.2  引号 16   ← 快 10 倍且更好
            # 关思考不仅省时，长句质量反而更高（默认档会把预算耗在推理上）。
            # 所以 M3 是当前首选：既能关思考，质量又接近参考语料（60.3）。
            extra["thinking"] = {"type": "disabled"}
        super().__init__(api_key, base_url, model, extra_body=extra, models=models, **kwargs)


class GenericOpenAIChatProvider(OpenAICompatibleChatProvider):
    """通用 OpenAI 兼容接口（渠道名 "openai"）。

    不绑定任何厂商：只要端点实现了 `POST {base_url}/chat/completions` +
    `Authorization: Bearer <key>` 就能接（OpenAI 官方 / 自建 one-api /
    vLLM / LM Studio / LocalAI / 第三方中转站…）。

    与父类的唯一差别是 label（界面上要显示成「通用 OpenAI」而不是泛泛的
    "LLM"）——协议层完全复用，因此本渠道**不新增任何依赖**（仍是 stdlib urllib）。
    """

    label = "OpenAI"


class LocalFallbackLLMProvider(LLMProvider):
    """确定性模板续写（开发/测试用，非生产质量）。

    把注入的 constraints（角色 + 未填坑）回显进续写正文，保证离线可验证
    M3.3 约束确实流进了生成环；同时拼接前情/检索上下文占位，模拟真实续写结构。
    """

    def chat(self, system: str, user: str, constraints: dict | None = None,
             stop_sequences: list[str] | None = None,
             temperature: float | None = None) -> str:
        """确定性模板续写。stop_sequences 接住但不处理（模板无 LLM stop 概念）——
        仅与 OpenAICompatibleChatProvider 签名对齐，避免 engine 在 fallback 路径
        500（v2.6.1 改 per-request 透传时漏同步此签名，已在 kg-e2e journey
        冒烟中暴露）。
        """
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


# ====================================================================
# Writer 单例（b58c763 行为完全保留）
# ====================================================================

_PROVIDER: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """Writer provider 单例：b58c763 时已有的唯一获取入口，签名/行为完全保留。

    新代码请走 ProviderRegistry.get('writer') 而不是直接调本函数。
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    # 写手分支一律带 `timeout=settings.writer_timeout`：
    #
    # **这个设置此前根本没有生效** —— 六个渠道分支都只传了 max_tokens，
    # timeout 一直用基类默认的 120 秒，界面上那个「timeout 秒」是只读展示的
    # 写手基线值，看着能配、实际不生效。2026-10-25 换 M3.1-Flash 做写手时
    # 实测单段要 112~141 秒，120 秒会直接撞线（read timeout）。
    #
    # 默认值 120 与基类默认一致，所以没显式配过 timeout 的安装**行为不变**。
    if settings.llm_provider == "openrouter" and settings.openrouter_api_key:
        _PROVIDER = OpenRouterChatProvider(
            settings.openrouter_api_key,
            settings.openrouter_base_url,
            settings.openrouter_model,
            models=settings.openrouter_model_list,
            max_tokens=settings.writer_max_tokens,
            timeout=settings.writer_timeout,
        )
    elif settings.llm_provider == "zhipu" and settings.zhipu_api_key:
        _PROVIDER = ZhipuChatProvider(
            settings.zhipu_api_key,
            settings.zhipu_base_url,
            settings.zhipu_model,
            models=settings.zhipu_model_list,
            max_tokens=settings.writer_max_tokens,
            timeout=settings.writer_timeout,
        )
    elif settings.llm_provider == "siliconflow" and settings.siliconflow_api_key:
        _PROVIDER = SiliconFlowChatProvider(
            settings.siliconflow_api_key,
            settings.siliconflow_base_url,
            settings.siliconflow_model,
            models=settings.siliconflow_model_list,
            max_tokens=settings.writer_max_tokens,
            timeout=settings.writer_timeout,
        )
    elif settings.llm_provider == "minimax" and settings.minimax_api_key:
        _PROVIDER = MiniMaxChatProvider(
            settings.minimax_api_key,
            settings.minimax_base_url,
            settings.minimax_model,
            models=settings.minimax_model_list,
            max_tokens=settings.writer_max_tokens,
            timeout=settings.writer_timeout,
            # 写手要吞吐：思考调低（实测又快又不再截断，见 MiniMaxChatProvider）。
            # 蒸馏/助理角色不传 ⇒ 保持官方默认（max），那两个角色要判断力。
            reasoning_effort=settings.minimax_reasoning_effort,
        )
    # ------------------------------------------------------------------
    # 通用 OpenAI 兼容渠道（第 8 步新增，判据与上面 5 个分支同形）。
    #
    # **仅当 llm_provider == "openai" 时命中**；其余渠道（zhipu / siliconflow /
    # minimax / openrouter / dashscope / 其它任意值）这个判据为假，继续走各自
    # 的原有分支，**行为逐字节不变**——上面 5 个分支一行未改。
    #
    # 为什么必须补这一支：Writer 角色在「模型设置」页选了「通用 OpenAI 接口」
    # 后，PUT 会把 provider 镜像成 llm_provider="openai"（b58c763 的 Writer
    # 单例只读 legacy 字段）。若本函数不认这个渠道，就会静默落进下面的
    # LocalFallback 兜底——UI 显示「已生效」而正文其实是模板生成的，
    # 正是「配了但一直在用离线模板」的静默失效。
    #
    # 端点：显式 llm_base_url 优先，否则回落该渠道的默认地址
    # （settings.openai_base_url，默认官方 https://api.openai.com/v1）。
    # 模型链：读该渠道的 CSV（settings.openai_models），与 zhipu 等分支同口径。
    # ------------------------------------------------------------------
    elif settings.llm_provider == "openai" and settings.openai_api_key:
        _PROVIDER = GenericOpenAIChatProvider(
            settings.openai_api_key,
            settings.llm_base_url or settings.openai_base_url,
            settings.openai_model,
            models=settings.openai_model_list,
            max_tokens=settings.writer_max_tokens,
            timeout=settings.writer_timeout,
        )
    elif settings.llm_provider == "dashscope" and settings.dashscope_api_key:
        base = settings.llm_base_url or settings.dashscope_base_url
        _PROVIDER = DashScopeChatProvider(
            settings.dashscope_api_key, base, settings.llm_model,
            max_tokens=settings.writer_max_tokens,
            timeout=settings.writer_timeout,
        )
    else:
        # 未配置 key 或显式 local：降级本地兜底，保证离线可跑
        _PROVIDER = LocalFallbackLLMProvider()
    return _PROVIDER


def reset_provider_cache() -> None:
    """清掉 Writer provider 单例缓存：下次 get_llm_provider() 按最新 settings 重建。

    仅影响 Writer；Assistant 单例由 reset_assistant_provider_cache() 管理，互不干扰。
    """
    global _PROVIDER
    _PROVIDER = None


def apply_runtime_config(overrides: dict[str, object | None]) -> None:
    """把持久化/前端传入的配置覆盖到 settings 并重置对应 role 的 provider 缓存（热切换）。

    overrides 的键为 settings 字段名（如 llm_provider / zhipu_api_key /
    assistant_model / distiller_model）；值为 None 的项跳过。
    - 涉及 assistant_* 字段：额外重置 Assistant 缓存；
    - 涉及 distiller_* 字段：额外重置 Distiller 缓存；
    - 涉及其它字段（含 writer_* / llm_provider / zhipu_api_key / openrouter_* 等）：
      走与 b58c763 一致的行为——重置 Writer 单例缓存（保持向后兼容）。
    - 同一调用里几类都改：三个角色各自重置，互不影响。

    **值按原类型 setattr，不要预先 str()**：布尔字段传字符串 "False" 会被
    当成真值（`bool("False") is True`），曾导致档位切到「极速」后改写仍开着。
    """
    has_assistant = False
    has_distiller = False
    for field, value in overrides.items():
        if value is None:
            continue
        if hasattr(settings, field):
            setattr(settings, field, value)
            if field.startswith("assistant_"):
                has_assistant = True
            elif field.startswith("distiller_"):
                has_distiller = True
    reset_provider_cache()
    if has_assistant:
        reset_assistant_provider_cache()
    if has_distiller:
        reset_distiller_provider_cache()


def build_provider(provider: str, api_key: str, model: str, base_url: str = "",
                   max_tokens: int | None = None, timeout: int = 120,
                   stream_timeout: int = 180) -> LLMProvider:
    """按渠道名直接构造 provider 实例（测试端点用，不走缓存/单例）。

    max_tokens / timeout / stream_timeout 是 ADR-2026-10-12 新增字段：
    - Assistant 工厂会显式传 timeout=60 / stream_timeout=60；
    - 其它调用方（settings.py 的连通性测试）不传 → 默认 None / 120 / 180 =
      与 b58c763 行为一致（stream_timeout=180 是关键，不能与 chat 共用 120）。
    """
    if provider == "openrouter":
        url = base_url or settings.openrouter_base_url
        return OpenRouterChatProvider(api_key, url, model, models=[model],
                                       max_tokens=max_tokens, timeout=timeout,
                                       stream_timeout=stream_timeout)
    if provider == "zhipu":
        url = base_url or settings.zhipu_base_url
        return ZhipuChatProvider(api_key, url, model, models=[model],
                                  max_tokens=max_tokens, timeout=timeout,
                                  stream_timeout=stream_timeout)
    if provider == "siliconflow":
        url = base_url or settings.siliconflow_base_url
        return SiliconFlowChatProvider(api_key, url, model, models=[model],
                                        max_tokens=max_tokens, timeout=timeout,
                                        stream_timeout=stream_timeout)
    if provider == "minimax":
        url = base_url or settings.minimax_base_url
        return MiniMaxChatProvider(api_key, url, model, models=[model],
                                    max_tokens=max_tokens, timeout=timeout,
                                    stream_timeout=stream_timeout)
    if provider == "dashscope":
        url = base_url or settings.dashscope_base_url
        return DashScopeChatProvider(api_key, url, model,
                                      max_tokens=max_tokens, timeout=timeout,
                                      stream_timeout=stream_timeout)
    if provider == "openai":
        # 通用 OpenAI 兼容渠道：任何实现了 /chat/completions + Bearer 鉴权的端点
        # （官方 / one-api / vLLM / LM Studio / 中转站…）。端点优先取调用方给的
        # base_url，否则回落 settings.openai_base_url（默认官方地址）。
        # 注意：**必须**在下面的 LocalFallback 兜底 return 之前——
        # 未知 provider 名会静默降级成本地兜底，那会让「渠道名拼错」表现为
        # 「配了但一直在用离线模板」，是极难排查的静默失效。
        url = base_url or settings.openai_base_url
        return GenericOpenAIChatProvider(api_key, url, model, models=[model],
                                         max_tokens=max_tokens, timeout=timeout,
                                         stream_timeout=stream_timeout)
    return LocalFallbackLLMProvider()


# ====================================================================
# Assistant 单例（ADR-2026-10-12 新增，独立于 _PROVIDER）
# ====================================================================


def _default_model_for(provider: str) -> str:
    """模型兜底：assistant_model / distiller_model 留空时用（两角色共用）。

    这张表必须与 `routers/settings.py` 的 `PROVIDER_META[*]["default_models"]`
    以及 `config.py` 的 `*_models` CSV 保持**三处一致**——下拉里能选的模型
    若不在运行时切换链上（或反过来），界面与生成链路就会对不上。
    """
    return {
        "openrouter": "apodex/apodex-1.1-mini:free",
        "zhipu": "glm-4-flash-250414",
        "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
        "minimax": "MiniMax-M3.1-Flash-Preview",
        "dashscope": "qwen-plus",
        "openai": "gpt-4o-mini",
    }.get(provider, "")


def build_role_provider(role: str, stream_timeout: int = 60) -> LLMProvider:
    """按 `<role>.*` 字段独立构造一个 provider 实例（与 writer 完全隔离）。

    这是 `build_assistant_provider()` 与 `build_distiller_provider()` 的共用
    实现——两角色同构（都走 `settings.get_provider_config(role)` 的渠道回退），
    只有默认 stream_timeout 不同（蒸馏是长任务，窗口给得更宽）。

    6 字段全部从 `settings.get_provider_config(role)` 取，**绝不读** llm_provider /
    *_api_key 等 legacy 字段（渠道回退在 config 那一层完成，不是绕过隔离）。

    异常与降级：
    - provider 留空 / provider='local' → 直接返回 LocalFallbackLLMProvider()（无 key 也跑）；
    - api_key 空 → 同样降级 LocalFallback（避免发请求时 401 拖慢诊断路径）；
    - model 留空 → 按 provider 走渠道默认（zhipu→glm-4-flash 等）；
    - max_tokens / timeout 走 settings.<role>_*；负值/0 视为未设。

    失败兜底**不动** Writer 单例：若本函数抛错，_PROVIDER 全程未被读写。
    """
    cfg = settings.get_provider_config(role)
    provider = str(cfg.get("provider") or "").strip()
    if not provider or provider == "local":
        return LocalFallbackLLMProvider()
    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key:
        return LocalFallbackLLMProvider()
    base_url = str(cfg.get("base_url") or "").strip()
    model = str(cfg.get("model") or "").strip() or _default_model_for(provider)
    # max_tokens / timeout 仅取正整数；负值/0/None 都不注入（保留默认）。
    kwargs: dict = {}
    mt = cfg.get("max_tokens")
    if isinstance(mt, int) and mt > 0:
        kwargs["max_tokens"] = mt
    to = cfg.get("timeout")
    if isinstance(to, int) and to > 0:
        kwargs["timeout"] = to
    # stream_timeout 暂时不分配置项（这两个角色都不走流式）但显式传值，
    # 防御未来子类把默认改成别的值。
    kwargs["stream_timeout"] = stream_timeout
    return build_provider(provider, api_key, model, base_url, **kwargs)


def build_assistant_provider() -> LLMProvider:
    """按 assistant.* 字段独立构造一个 provider 实例（与 writer 完全隔离）。

    Assistant 端的所有检索/诊断调用都走本入口对应的单例（`get_assistant_provider()`）。
    stream_timeout=60 与 timeout 配对：诊断路径必须快速失败，不允许长等。
    """
    return build_role_provider("assistant", stream_timeout=60)


_ASSISTANT_PROVIDER: LLMProvider | None = None


def get_assistant_provider() -> LLMProvider:
    """Assistant provider 单例：与 _PROVIDER 互不影响。

    Assistant 端的所有检索/诊断调用都走本入口（不直接调 build_assistant_provider()，
    以避免每次都新建连接 / 重新解析配置）。
    """
    global _ASSISTANT_PROVIDER
    if _ASSISTANT_PROVIDER is not None:
        return _ASSISTANT_PROVIDER
    _ASSISTANT_PROVIDER = build_assistant_provider()
    return _ASSISTANT_PROVIDER


def reset_assistant_provider_cache() -> None:
    """清掉 Assistant 单例缓存：下次 get_assistant_provider() 按最新 settings 重建。

    完全不动 _PROVIDER；不同 role 互不串扰。
    """
    global _ASSISTANT_PROVIDER
    _ASSISTANT_PROVIDER = None


# ====================================================================
# Distiller 单例（文风档案抽取专用，第三条轨道）
# ====================================================================


def build_distiller_provider() -> LLMProvider:
    """按 distiller.* 字段独立构造一个 provider 实例（第三轨，与前两轨隔离）。

    用途是**文风档案抽取**：把参考语料压成可注入的文风特征。它与
    `app.style.narrative` 的「蒸馏档位」（full/balanced/fast，决定抽多细）
    不是一回事——本函数只决定「用哪个模型抽」。

    蒸馏是长任务（一次读几十段语料），所以默认 stream_timeout 给到 300s，
    远大于 Assistant 的 60s；timeout 默认由 providers.yaml 给 180s。
    """
    return build_role_provider("distiller", stream_timeout=300)


_DISTILLER_PROVIDER: LLMProvider | None = None


def get_distiller_provider() -> LLMProvider:
    """Distiller provider 单例：与 _PROVIDER / _ASSISTANT_PROVIDER 互不影响。

    蒸馏链路统一走本入口（不直接调 build_distiller_provider()），
    以避免每次调用都新建连接 / 重新解析配置。
    """
    global _DISTILLER_PROVIDER
    if _DISTILLER_PROVIDER is not None:
        return _DISTILLER_PROVIDER
    _DISTILLER_PROVIDER = build_distiller_provider()
    return _DISTILLER_PROVIDER


def reset_distiller_provider_cache() -> None:
    """清掉 Distiller 单例缓存：下次 get_distiller_provider() 按最新 settings 重建。

    完全不动 _PROVIDER / _ASSISTANT_PROVIDER；不同 role 互不串扰。
    """
    global _DISTILLER_PROVIDER
    _DISTILLER_PROVIDER = None
