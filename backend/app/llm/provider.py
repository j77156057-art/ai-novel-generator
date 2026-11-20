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
import os
import time
import urllib.error
import urllib.request
import uuid

from app.config import KG_EXTRACT_CHANNELS, settings
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

# ── 挂起判据：吞吐为主、总时长为备（2026-11-14）──────────────────────────────
#
# 为什么拆两条：720s 的单一总时长上限把第 17 章一次「慢但正常」的重写掐断
# （终稿回落初稿、段均 19.4 违规）；而 19 分钟卡死是服务端**每 1s 滴 1 字节**——
# 总时长上很长、速率上接近零。两种场景用一个尺子必然二选一。
#
# 下限 8 B/s 的依据：11 章历史日志（单轮耗时上界 × 初稿汉字数≈响应体 UTF-8 字节）
# 反推的隐含速率 min=13.3（第 17 章，该死的那次被 720s 掐断，耗时不真）/
# median=38.5 / max=58.3 B/s；只看成功样本则最慢 24.1 B/s。取最慢成功样本的
# 约 1/3 → 8 B/s。1 B/s 滴包只有下限的 1/8，一个窗口即判死。
# 该反推用的是耗时**上界**，所以速率是**下界**——下限据此取，偏保守（不会误杀）。
# 成功日志现在开始记真实字节数与平均速率，后续可按实测分布再校。
# 窗口 30s：正常非流式响应是「攒完一次性到达」，窗口几乎跑不满；且窗口从
# **首字节**开始计时，首字节前的长等待（慢调用）不会被误判成低速率。
_RATE_WINDOW_SECONDS = 30.0
_RATE_FLOOR_BPS = 8.0


class _RateGuard:
    """首字节之后的吞吐窗口判据——**非流式与流式共用同一份**。

    两条传输路径都只是「字节在到达」，判据没有理由写两份：
    - 非流式：`_read_body` 每读一块 `feed()` 一次；
    - 流式：`_iter_stream_lines` 每读一个 chunk `feed()` 一次（不是按行——按行
      会漏掉「只滴字节、很久不出换行」的假活）。

    首字节之前不判：那时没有字节可测，生成期可能长达数分钟，交给调用方的
    总时长硬顶兜住（流式路径没有硬顶，靠 `stream_timeout` 的空闲超时）。
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.started: float | None = None
        self.bytes = 0

    @property
    def open(self) -> bool:
        """是否已经收到过字节（速率窗口是否已开）。"""
        return self.started is not None

    def feed(self, n: int, now: float) -> None:
        if self.started is None:
            self.started = now  # 首个字节到达才开始计时，首字节前的等待不算速率
        self.bytes += n
        self.check(now)

    def check(self, now: float) -> None:
        if self.started is None:
            return
        span = now - self.started
        if span < _RATE_WINDOW_SECONDS:
            return
        rate = self.bytes / span
        if rate < _RATE_FLOOR_BPS:
            raise TimeoutError(
                f"{self.label} 响应吞吐 {rate:.1f}B/s 低于下限 "
                f"{_RATE_FLOOR_BPS:g}B/s（{span:.0f}s 窗口只收到 {self.bytes} 字节），"
                "判定为挂起"
            )
        self.started, self.bytes = now, 0


def _iter_stream_lines(resp, guard: _RateGuard):
    """把 SSE 响应切成行，同时把每次到达的字节喂给速率判据。

    直接 `for raw in resp` 是 `readline()`：服务端每 1s 滴 1 字节且迟迟不出
    换行时，每行都读不到、但 socket 从不空闲，既不会超时也不会产出 chunk——
    第 19 章日志里的 `[stream] 113.0s chars=8` 就是这种形态。改用 `read1()`
    拿到每块到达的字节先过判据，再拼行。
    """
    reader = getattr(resp, "read1", None) or resp.read
    buf = b""
    while True:
        chunk = reader(65536)
        if not chunk:
            if buf:
                yield buf
            return
        guard.feed(len(chunk), time.monotonic())
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line


def _stream_ms(started: float) -> int:
    """从 `started` 到现在的毫秒数（流式留痕统一口径）。"""
    return int((time.monotonic() - started) * 1000)


def _reasoning_source(field_chars: int, inline_chars: int) -> str | None:
    """这条流的 reasoning 字符数从哪来；都没有就 None（不是 0）。"""
    if field_chars and inline_chars:
        return "both"
    if field_chars:
        return "reasoning_content"
    if inline_chars:
        return "inline_think"
    return None


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


#: 触发跨渠道兜底的 HTTP 状态。其余 4xx **一律不切换**：400 参数错 / 401 鉴权 /
#: 403 无权限 / 404 模型不存在都是配置问题，换渠道只会把真实错误藏起来。
_FALLBACK_HTTP = {429, 500, 502, 503, 529}

#: 触发跨渠道兜底的传输层错误关键字（含 urllib 包装后的文案）。
_FALLBACK_MSG_HINTS = (
    "timed out", "timeout", "connection reset", "connection refused",
    "broken pipe", "remote end closed connection",
    "name or service not known", "temporary failure in name resolution",
    "network is unreachable",
)


def _root_cause(exc: Exception) -> Exception:
    """沿 `__cause__` 找到最里层原因。

    provider 会把自己的异常再包一层 `RuntimeError(f"{label} chat 调用失败: …")`，
    直接看外层只能得到「调用失败」，判不出到底是超时还是 400。
    """
    cur, seen = exc, set()
    while cur.__cause__ is not None and id(cur) not in seen:
        seen.add(id(cur))
        cur = cur.__cause__
    return cur


def _fallback_eligible(exc: Exception) -> bool:
    """这个异常值不值得换渠道再试。

    只认**传输层 / 服务端**故障：超时、连接类错误、429、5xx。
    配置类错误（400/401/403/404）与内容不可解析不换渠道——换过去只会
    让「key 写错」「模型名写错」看起来像「跑在备用渠道上，一切正常」。
    """
    root = _root_cause(exc)
    if isinstance(root, TimeoutError):
        return True
    code = _http_code(root)
    if code is not None:
        return code in _FALLBACK_HTTP
    if isinstance(root, urllib.error.URLError):
        return True  # HTTPError 已由上面的 code 分支处理，这里只剩纯传输层
    return any(k in str(root).lower() for k in _FALLBACK_MSG_HINTS)


def _channel_outcome(exc: Exception) -> str:
    """给一次失败分类，写进 `agent_runs.note.llm.channels[].outcome`。"""
    root = _root_cause(exc)
    if isinstance(root, TimeoutError):
        return "timeout"
    code = _http_code(root)
    if code is not None:
        return f"http_{code}"
    if isinstance(root, urllib.error.URLError):
        return "transport"
    if any(k in str(root).lower() for k in ("timed out", "timeout")):
        return "timeout"
    return "error"


class ToolsUnsupportedError(RuntimeError):
    """当前模型链上没有任何「可安全使用 tools」的模型。

    调用方（`/qa` 工具循环）捕获它后回落确定性路径。**不要**降级成
    「不发 tools 但照常请求」——那会让模型在看不见工具的情况下作答，
    用户却以为它查过了。明确失败、明确回落，才是可解释的行为。
    """


def tools_unsupported_models() -> set[str]:
    """配置项 `assistant_tools_unsupported_models` 的解析结果。

    来源是 2026-11-14 的实测探针（见 `config.py` 同名字段的注释）：
    `glm-z1-flash` 会返回 `finish_reason=tool_calls` 但 `tool_calls=[]`，
    所以按模型名硬排除，而不是运行时探测（探测会被这个假信号骗过）。
    """
    raw = str(getattr(settings, "assistant_tools_unsupported_models", "") or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


class LLMProvider:
    #: 蒸馏/摘要的块大小（字）：**实测能安全吃下且产出不劣化**的量级。
    #: 0 = 未声明 ⇒ 调用方用保守默认（20 万字以下按块切）。
    distill_block_chars: int = 0

    #: 单次请求能**收下**的正文上限（字）：与 `distill_block_chars` 是两回事 ——
    #: 后者是「产出不劣化」的块大小，这里是渠道侧「能收下」的硬上限。
    #: 0 = 未声明 ⇒ 全书摘要不敢整本投喂（fail closed，退回切块抽取）。
    max_context_chars: int = 0

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        raise NotImplementedError  # pragma: no cover - 接口

    def complete_message(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        stop_sequences: list[str] | None = None,
    ) -> dict:
        """结构化补全：返回 `{content, tool_calls, finish_reason, usage, model}`。

        基类兜底实现把 messages 拍平成 system / user 后走 `chat()`，**永远**
        返回空 `tool_calls`——没有 function calling 能力的 provider（本地兜底、
        只实现 chat 的渠道）因此天然走「不调用工具」这条路，调用方据此回落
        确定性路径，不需要 isinstance 判断。

        子类（OpenAI 兼容层）重写本方法以真正透传 `tools` / `tool_choice`。
        """
        system = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        user = "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") != "system"
        )
        return {
            "content": self.chat(system, user),
            "tool_calls": [],
            "finish_reason": "stop",
            "usage": None,
            "model": getattr(self, "model", "") or "",
        }

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

    def stream_chat_events(self, system: str, user: str, constraints: dict | None = None,
                           stop_sequences: list[str] | None = None):
        """流式续写的事件流：`("content", 文本)` / `("reasoning", 推理增量)`。

        子类（OpenAI 兼容层）实现真实流式；基类退化为一次性 `chat`。
        调用方需要「看到模型在想什么」时用本方法；只要正文时用 `stream_chat`。
        """
        yield ("content", self.chat(system, user, constraints=constraints))

    def stream_chat(self, system: str, user: str, constraints: dict | None = None,
                     stop_sequences: list[str] | None = None):
        """只要正文的兼容入口（过滤掉 reasoning 事件）。"""
        for kind, text in self.stream_chat_events(
            system, user, constraints=constraints, stop_sequences=stop_sequences
        ):
            if kind == "content":
                yield text

    def variant(self, *, thinking_off: bool = False, max_tokens: int | None = None):
        """返回一个「关思考 / 换输出预算」的变体 provider。

        机械改写不需要思考，但思考会吃掉同一份 token 预算（实测会整块返回空）。
        provider 是单例，不能原地改请求体，所以真正的实现（OpenAI 兼容层）
        用浅拷贝：只换 `extra_body` 与 `max_tokens`，其余共享。

        基类原样返回自己——不支持的渠道就当没要求变体。
        """
        return self

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


def render_constraints(constraints: dict | None) -> str:
    """把 constraints（已锁定角色 / 未兑现伏笔）渲染成 prompt 片段。

    生成与重写共用同一份措辞：
    - 生成走 `_apply_constraints`，追加在 user 末尾；
    - 重写不能追加在末尾——那会把锁定名单排在"待改正文"之后，模型会把它当成
      正文的一部分原样吐出来（2026-11-10 实测：落库段落末尾出现
      【本节完】+【本段已锁定角色】+【尚未兑现的伏笔】）。重写改为放进 system。
    """
    if not constraints:
        return ""
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
    return "\n".join(parts)


def _apply_constraints(user: str, constraints: dict | None) -> str:
    """把 constraints（已锁定角色 / 未兑现伏笔）真正序列化进 prompt。

    旧实现只把 constraints 传给 LocalFallbackLLMProvider，真实 OpenAI 兼容 provider
    签名收下却从不使用——于是「约束进入生成环」只在离线兜底路径成立，生产模型完全
    拿不到角色约束。现统一在 user 末尾追加（不动 system，避免击穿静态提示词缓存）。
    """
    section = render_constraints(constraints)
    if not section:
        return user
    return user + "\n\n" + section


class OpenAICompatibleChatProvider(LLMProvider):
    """OpenAI 兼容 /chat/completions 的公共实现（DashScope / OpenRouter 共用）。

    子类只需给出 api_key / base_url / model。extra_body 可追加厂商特有字段
    （如 OpenRouter 关闭 reasoning）。

    ADR-2026-10-12 增加 max_tokens / timeout / stream_timeout 三个字段，默认值与
    b58c763 的硬编码值**完全一致**（max_tokens=None 即「不写入请求体」；
    timeout=180 即 chat 路径 180 秒，2026-11-14 由 120 提到与 stream_timeout 同档；
    stream_timeout=180 即流式续写 180 秒——长文首字到达前的整个连接窗口必须留够，
    否则会被截断）。任何调用方未显式传这三个参数时，Writer 行为**绝对不变**。
    """

    label = "LLM"

    #: 429 时**同模型**重试的等待序列（秒）。默认空 = 旧行为（429 直接换模型）。
    #: MiniMax 五小时额度墙是账号级硬限额，换模型无益；只能退避后重试同一条。
    _RATE_LIMIT_BACKOFFS: tuple[float, ...] = ()

    #: 单模型 `max_tokens` 上限（子类按模型声明，未声明 = 不裁剪）。
    #: 模型链上不同模型的输出上限并不一致，同一个 `writer.max_tokens` 直接下发会被
    #: 小上限模型整条 400 拒掉——2026-11-13 实测第 4 章首轮：M3.1-Flash 失败后回退到
    #: `MiniMax-Text-01`，后者直接 400
    #: `invalid params, model[MiniMax-Text-01] does not support max tokens > 40000`，
    #: 重写轮因此被静默吞掉（正文与首轮逐字节相同）。
    _MODEL_MAX_TOKENS: dict[str, int] = {}

    #: 全书摘要这类「一次性长输出」在该渠道可用的输出上限（token）。
    #: None = 未声明（摘要按估算预算请求，撞上限再重试）。
    #: 声明后摘要会**首次就按上限收敛条目数**，避免白跑一轮再降质重抽。
    digest_output_tokens: int | None = None

    def _effective_max_tokens(self, model: str) -> int | None:
        """本次请求对**该模型**实际下发的 max_tokens（受 `_MODEL_MAX_TOKENS` 裁剪）。"""
        if self.max_tokens is None:
            return None
        cap = self._MODEL_MAX_TOKENS.get(model)
        if cap and self.max_tokens > cap:
            return cap
        return self.max_tokens

    def __init__(self, api_key: str, base_url: str, model: str, extra_body: dict | None = None,
                 models: list[str] | None = None,
                 max_tokens: int | None = None, timeout: int | None = None,
                 stream_timeout: int | None = None,
                 total_timeout: int | None = None,
                 first_byte_timeout: int | None = None,
                 extra_headers: dict | None = None) -> None:
        if not api_key:
            raise ValueError(f"{self.label} 需要 API key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = extra_body or {}
        # 厂商要求的额外请求头（opencode go 必须带 x-opencode-session 与浏览器 UA，
        # 否则 400 MissingSessionID / Cloudflare 403）。默认空 = 行为不变。
        self.extra_headers = dict(extra_headers or {})
        # None = 不在请求体里加 max_tokens（保留 Writer 原行为，调用方需明确给值才注入）
        self.max_tokens = max_tokens
        # 最近一次调用的 finish_reason（'length' = 被 max_tokens 截断）。
        # 仅供诊断/测试；provider 是单例，多线程下可能互相覆盖，但用途就是打日志。
        self.last_finish_reason: str | None = None
        # 最近一次**成功**调用实际使用的模型 id（可能来自模型链上切换后的位置）。
        # 与 last_finish_reason 同口径：仅供诊断留痕，不参与任何控制流。
        self.last_model: str | None = None
        # 最近一次调用的 usage（prompt / completion / total / reasoning）。
        # 2026-11-07：此前兼容层完全丢弃 usage，导致成本报表里 token 全为 null；
        # 这里只做解析留痕，不改返回内容，也不改请求体。
        self.last_usage: dict | None = None
        # 最近一次**流式**调用的增量留痕：请求/首帧/首正文耗时、reasoning 与
        # 正文字符数。MiniMax 流式不回 usage，`last_usage` 全空，「首字等待到底
        # 花在思考还是供应商排队」无从判断；这里在读取 SSE 时自行累计。
        # None = 这次调用没有任何可测信号（**不是**「0 个字符」）。
        self.last_stream_stats: dict | None = None
        # 最近一次**成功**非流式调用的耗时留痕：
        # `{provider, model, first_byte_ms, total_ms, bytes}`。
        # 成功调用此前只有一条总耗时日志，取不到「首字节 vs 总时间」的分布，
        # KG 抽取的 `first_byte_timeout` 定多少一直没有实测依据。这里按
        # 「请求发出 → 首个正文字节」「请求发出 → body 读完」记录，由调用方
        # （批抽）落进 `agent_runs.note`。每次 complete_message 开始时清空，
        # 失败调用不会残留上一次成功调用的数字。
        self.last_call_stats: dict | None = None
        # chat 路径默认 180s（2026-11-14 由 120s 提到与流式同档）：4000 字级单章
        # 实测一次生成需 180s 上下，120s 的同步入口必然读超时、零产出。
        # Assistant 仍显式传 timeout=60（诊断必须快速失败），不受此默认值影响。
        # 2026-11-14：默认值改从 settings 读（writer_timeout / writer_stream_timeout，
        # 默认均为 180），不再把秒数钉在函数签名里。
        self.timeout = (
            int(timeout) if isinstance(timeout, int) and timeout > 0
            else settings.writer_timeout
        )
        # stream_chat 路径默认 180s = b58c763 硬编码值；**不能**与 chat 共用默认 120s，
        # 否则长文流式续写会从 180s 截到 120s（QA 报告 3.1 节已记）。Assistant 不走流式
        # 但 build_assistant_provider 仍显式传 stream_timeout=60 保持防御性。
        self.stream_timeout = (
            int(stream_timeout) if isinstance(stream_timeout, int) and stream_timeout > 0
            else settings.writer_stream_timeout
        )
        # 非流式补全的**总时长硬顶**（含换模型与重试退避）：`self.timeout` 只是
        # 首字节之后的单次 recv 空闲超时；服务端慢速滴包会不断重置它，模型链 ×
        # 每模型 max_attempts 次重试还会相乘。2026-11-14 第 16 章实测一次重写
        # 静默挂 19 分钟，就是这两层都没有上限。默认值来自
        # settings.writer_total_timeout（960s）；Assistant / Distiller 走
        # build_provider，那里按各自 recv 超时×2 另算，不共用 Writer 预算。
        self.total_timeout = (
            int(total_timeout) if isinstance(total_timeout, int) and total_timeout > 0
            else settings.writer_total_timeout
        )
        # **首字节阶段**上限（秒）：只对「已经连上、但一个正文字节都还没来」的
        # 等待生效，取 `min(剩余总预算, 本值)`。None / 0 = 不做额外限制（沿用
        # 旧行为：首字节前由剩余总预算兜底）。
        #
        # 为什么必须由调用方按需传入、不能给默认值：正文生成的首字节等待实测
        # 100–305s 属正常区间，任何全局下限都会误杀章节生成。KG 抽取是秒级小
        # 任务，长时间无字节就是挂起，才需要这个早暴露的闸门。
        self.first_byte_timeout = (
            int(first_byte_timeout)
            if isinstance(first_byte_timeout, int) and first_byte_timeout > 0
            else None
        )
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

    def variant(self, *, thinking_off: bool = False, max_tokens: int | None = None):
        """变体：浅拷贝，只换 `extra_body` 与 `max_tokens`（不换身份）。

        关思考的发法：请求体加 `thinking: {"type": "disabled"}`；不认这个字段
        的厂商会静默忽略（最坏是没关上，不会报错）。
        """
        import copy as _copy

        new = _copy.copy(self)
        extra = dict(self.extra_body or {})
        if thinking_off:
            extra["thinking"] = {"type": "disabled"}
        new.extra_body = extra
        if max_tokens is not None:
            new.max_tokens = max_tokens
        return new

    def _model_chain(self) -> list[str]:
        return self._models

    def set_model_chain(self, models: list[str]) -> None:
        """覆盖候选模型链（**仅 KG 抽取独立配置使用**）：首位即首选模型。

        只为 `kg_extract.models` 这个独立槽服务——不写 settings、不碰 Writer
        单例。空列表 = 不改（调用方应在那之前就别调本方法）。
        """
        chain = [m.strip() for m in models if m and m.strip()]
        if not chain:
            return
        self.model = chain[0]
        self._models = [chain[0]] + [m for m in chain[1:] if m != chain[0]]

    def _complete(self, messages: list[dict], stop_sequences: list[str] | None = None,
                  budget_seconds: float | None = None) -> str:
        """纯文本补全 = `complete_message` 的薄包装。

        Writer 只依赖这个字符串接口。**行为必须与改造前逐字节一致**：
        同样的 payload、同样的模型链、同样的重试 / 429 换模型、同样的
        `strip_reasoning` 后 `.strip()`。所有结构化能力都放在
        `complete_message` 里，本方法不再持有任何逻辑。

        budget_seconds：仅跨渠道兜底用（见 `FallbackChannelProvider`），
        None = 用本实例的 `total_timeout`，与改造前一致。**None 时不传这个
        kwarg**：调用方可能替换了 `complete_message`（测试替身/子类），
        不能让新增参数变成对方签名上的硬依赖。
        """
        kwargs: dict = {"stop_sequences": stop_sequences}
        if budget_seconds is not None:
            kwargs["budget_seconds"] = budget_seconds
        message = self.complete_message(messages, **kwargs)
        return str(message.get("content") or "")

    def complete_message(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        stop_sequences: list[str] | None = None,
        budget_seconds: float | None = None,
    ) -> dict:
        """结构化补全：透传 `tools` / `tool_choice`，返回完整 message。

        返回 `{content, tool_calls, finish_reason, usage, model}`。
        **`tool_calls` 是判定「模型是否真的调了工具」的唯一依据**：
        `glm-z1-flash` 实测会给出 `finish_reason=tool_calls` 但
        `tool_calls=[]`，任何用 finish_reason 判定的写法都会静默错判。
        """
        payload_base = {
            "messages": messages,
            # 小说续写要克制的文风：温度压到 0.7，避免"漂亮但套路"的堆料输出
            "temperature": 0.7,
            "top_p": 0.9,
            **self.extra_body,
        }
        if self.max_tokens is not None:
            payload_base["max_tokens"] = self.max_tokens
        # v2.6：stop token 列表——模型遇到任一 token 时停止生成。
        # 优先用本次调用传的 stop_sequences（per-request），回退到单例 set 的。
        effective_stop = stop_sequences if stop_sequences is not None else self._stop_sequences
        if effective_stop:
            payload_base["stop"] = effective_stop
        if tools:
            payload_base["tools"] = tools
            if tool_choice:
                payload_base["tool_choice"] = tool_choice
        last_exc: Exception | None = None
        # 本次调用从头留痕：失败时不能留着上一次成功调用的耗时。
        self.last_call_stats = None
        started = time.monotonic()
        # 跨渠道兜底会把「链剩余额度」传进来；不传 = 本实例的 total_timeout，
        # writer / assistant / 默认路径行为完全不变。
        budget = (float(self.total_timeout) if budget_seconds is None
                  else max(1.0, min(float(budget_seconds), float(self.total_timeout))))
        # 整条链（换模型 + 每模型重试）共享同一个硬截止：总时长上限。
        deadline = started + budget
        # 带 tools 的请求**不能**落到实测不可用的模型上：那会返回
        # `finish_reason=tool_calls` + 空数组的假信号。整条链都不可用时
        # 明确抛 ToolsUnsupportedError，由调用方回落确定性路径。
        chain = self._model_chain()
        if tools:
            unsupported = tools_unsupported_models()
            chain = [m for m in chain if m not in unsupported]
            if not chain:
                raise ToolsUnsupportedError(
                    f"{self.label} 模型链 {self._model_chain()} 均不支持 function calling"
                )
        for model in chain:
            # 总时长已用尽：不再换模型/重试，直接按失败返回（调用方走回退路径）。
            if time.monotonic() >= deadline:
                break
            payload = {**payload_base, "model": model}
            mt = self._effective_max_tokens(model)
            if mt is not None:
                payload["max_tokens"] = mt
                if self.max_tokens is not None and mt != self.max_tokens:
                    logger.info(
                        "%s 模型 %s 上限 %s < 配置 %s，本次按 %s 请求（避免 400）",
                        self.label, model, mt, self.max_tokens, mt,
                    )
            body = json.dumps(payload).encode("utf-8")
            rl_attempt = 0
            max_attempts = max(
                _RETRIES_PER_MODEL, 1 + len(self._RATE_LIMIT_BACKOFFS)
            )
            for attempt in range(max_attempts):
                req = urllib.request.Request(f"{self.base_url}/chat/completions", data=body, method="POST")
                req.add_header("Authorization", f"Bearer {self.api_key}")
                req.add_header("Content-Type", "application/json")
                for hk, hv in self.extra_headers.items():
                    req.add_header(hk, hv)
                try:
                    # 本次尝试的计时起点放在**请求发出前**：含建连与供应商排队，
                    # 「首字节耗时」才是调用方真正等的那段。
                    attempt_started = time.monotonic()
                    remaining = deadline - time.monotonic()
                    if remaining <= 1:
                        last_exc = TimeoutError(
                            f"{self.label} 非流式补全超过总时长上限 {self.total_timeout}s"
                        )
                        break
                    # 首字节前的等待**只受硬顶约束**，不再用 `self.timeout`（180s）。
                    # 2026-11-14 第 18 章实测：MiniMax 一次重写 4 个尝试各卡在 180s
                    # 空闲超时上（"The read operation timed out"），总耗时 896.8s
                    # 仍无产出——而重写轮生成期本来就可能是几分钟，180s 会误杀
                    # 「慢但正常」的调用。首字节之后由 _read_body 的速率判据接管，
                    # 那里不需要也不该有长等待。
                    #
                    # 例外：调用方显式传了 `first_byte_timeout`（目前只有 KG 抽取）
                    # 时，首字节等待取 `min(剩余预算, 该值)`——否则「服务端握着连接
                    # 一个字节都不给」只能等整份总预算耗尽才报错。
                    first_wait = (
                        min(remaining, float(self.first_byte_timeout))
                        if self.first_byte_timeout
                        else remaining
                    )
                    with urllib.request.urlopen(
                        req, timeout=max(1, first_wait)
                    ) as resp:
                        raw, first_byte_at = self._read_body(resp, deadline)
                    data = json.loads(raw.decode("utf-8"))
                    self._note_usage(data.get("usage"))
                    choice = data["choices"][0]
                    self._note_finish_reason(
                        choice.get("finish_reason"), self.label, model, mt
                    )
                    message = choice.get("message") or {}
                    # 单次非流式调用耗时留痕：历史上没有这项统计，只能拿整章耗时
                    # 反推上界（p50/p90/max 见 settings.writer_total_timeout 注释）。
                    cost = time.monotonic() - started
                    # 首字节 vs 总耗时：`kg_extract_first_byte_timeout` 该定多少
                    # 此前没有实测依据（成功调用只记总耗时）。这里按「请求发出 →
                    # 首个正文字节」「请求发出 → body 读完」记录，由批抽落进
                    # `agent_runs.note.llm.timing`，不依赖日志轮转即可按批统计分布。
                    total_ms = int((time.monotonic() - attempt_started) * 1000)
                    first_byte_ms = (
                        int((first_byte_at - attempt_started) * 1000)
                        if first_byte_at is not None else None
                    )
                    self.last_call_stats = {
                        "provider": getattr(self, "label", type(self).__name__),
                        "model": model,
                        "first_byte_ms": first_byte_ms,
                        "total_ms": total_ms,
                        "bytes": len(raw),
                    }
                    # 字节数 + 平均速率一并留痕：这是「速率判据下限」的观测来源
                    # （此前只有整章耗时，取不到真实字节速率分布）。
                    logger.info(
                        "%s 非流式调用完成 %.1fs（model=%s，首字节 %s，响应 %d 字节，"
                        "平均 %.1fB/s）",
                        self.label, cost, model,
                        f"{first_byte_ms}ms" if first_byte_ms is not None else "未测到",
                        len(raw),
                        len(raw) / cost if cost > 0 else 0.0,
                    )
                    if cost > self.timeout * 2:
                        logger.warning(
                            "%s 非流式调用偏慢 %.1fs（model=%s，预算 %ds 的 %.0f%%）",
                            self.label, cost, model, self.total_timeout,
                            cost / self.total_timeout * 100,
                        )
                    # 留痕：本次实际应答的模型（可能是模型链上切换后的位置）。
                    self.last_model = model
                    return {
                        # 只对正文剥推理：tool_calls 是结构化字段，不能过 strip_reasoning
                        "content": strip_reasoning(
                            message.get("content") or ""
                        ).strip(),
                        "tool_calls": list(message.get("tool_calls") or []),
                        "finish_reason": choice.get("finish_reason"),
                        "usage": self.last_usage,
                        "model": model,
                    }
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    # 429/403（限流/无权限）：换下一个模型；网络抖动：同模型短退避重试
                    if _is_http_error(exc, _SWITCH_HTTP):
                        if _is_account_daily_limit(exc):
                            logger.warning(
                                "%s 账号免费日限额已用完（所有 :free 模型共享，换模型无益），直接失败", self.label
                            )
                            raise RuntimeError(f"{self.label} chat 调用失败: {last_exc}") from last_exc
                        # MiniMax 等单模型渠道：429 是同模型硬限额，按配置退避后重试同一条。
                        if (
                            _http_code(exc) == 429
                            and rl_attempt < len(self._RATE_LIMIT_BACKOFFS)
                        ):
                            wait = self._RATE_LIMIT_BACKOFFS[rl_attempt]
                            rl_attempt += 1
                            logger.warning(
                                "%s 模型 %s 429，%.0fs 后重试同一条（%d/%d）",
                                self.label, model, wait, rl_attempt,
                                len(self._RATE_LIMIT_BACKOFFS),
                            )
                            time.sleep(max(0.0, min(wait, deadline - time.monotonic())))
                            continue
                        logger.warning("%s 模型 %s 受限（%s），切换下一个候选模型", self.label, model, _http_code(exc))
                        break
                    if not _is_retryable(exc) or attempt == max_attempts - 1:
                        break
                    if deadline - time.monotonic() <= 1:
                        break
                    time.sleep(max(0.0, min(_backoff(attempt), deadline - time.monotonic())))
        # 链尾失败必须留痕：模型链上每个模型都失败时，调用方只看到一句
        # RuntimeError 是不够的——这次实测里 404（Text-01 已下线）与 400（max_tokens
        # 非法）都被压成「HTTP Error」，排查时无法区分。这里把整条链和最后错误打进日志。
        logger.error(
            "%s 模型链全部失败，候选=%s，最后错误: %s",
            self.label, self._model_chain(), last_exc,
        )
        raise RuntimeError(f"{self.label} chat 调用失败: {last_exc}") from last_exc

    def _read_body(self, resp, deadline: float) -> tuple[bytes, float | None]:
        """分块读响应体：**吞吐判据为主、总时长硬顶为备**。

        返回 ``(body, first_byte_at)``：``first_byte_at`` 是**首个正文字节**到达
        时的 ``time.monotonic()``（空响应为 None），供调用方计算首字节耗时。

        为什么不能直接 `resp.read()`：那是一次阻塞调用，`urlopen(timeout=)` 只约束
        **每次 recv 的空闲时间**；服务端慢速滴包（或分块传输里持续有心跳）时，
        每次 recv 都不超时，整个读取可以无限长。分块读 + 逐块重设 socket 超时，
        才能把总时长切成可检查的片段。

        为什么光有总时长不够（2026-11-14）：720s 硬顶把第 17 章一次「慢但正常」
        的重写掐断，终稿回落初稿、段均 19.4 违规；而 19 分钟卡死是服务端
        **每 1 秒滴 1 字节**——总时长上很长，速率上接近零。同一个尺子必然二选一，
        所以拆成两条：

        1. **吞吐判据（主）**：首字节到达后按 30s 滚动窗口统计到达字节数，
           低于 `_RATE_FLOOR_BPS` 立即判挂起并抛错。正常「攒完一次性到达」的
           响应跑不满一个窗口，不会误杀慢调用；
        2. **总时长硬顶（备）**：`total_timeout` 只在极端情况兜底。
        """
        buf: list[bytes] = []
        # `read(n)` 在 chunked 传输里会**内部循环**直到读满 n 字节才返回——单次
        # 调用仍然可以无限长。`read1(n)` 只返回当前已到达的一块，才能把「总时长」
        # 切成可检查的片段（实测：每 1s 滴 1 字节的服务端，read(65536) 挂满 120s）。
        reader = getattr(resp, "read1", None) or resp.read
        guard = _RateGuard(self.label)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.label} 读取响应超过总时长上限 {self.total_timeout}s"
                )
            # 首个正文字节到达前：只受硬顶约束（同 urlopen 的理由——生成期可能
            # 长达数分钟，180s 的 socket 空闲超时会误杀慢调用）；调用方显式传了
            # `first_byte_timeout` 时改用 min(剩余预算, 该值)，让「一个字节都不来」
            # 在分钟级暴露。首字节之后：空闲上限压到速率窗口内，服务端假活在
            # 一个窗口内就会被判死。
            if not guard.open:
                wait = (
                    min(remaining, float(self.first_byte_timeout))
                    if self.first_byte_timeout
                    else remaining
                )
                sock_timeout = max(0.5, wait)
            else:
                sock_timeout = max(
                    0.5,
                    min(float(self.timeout), remaining, _RATE_WINDOW_SECONDS),
                )
            try:
                resp.fp.raw._sock.settimeout(sock_timeout)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 —— 拿不到底层 socket 就退回原超时
                pass
            guard.check(time.monotonic())
            piece = reader(65536)
            if not piece:
                return b"".join(buf), guard.started
            buf.append(piece)
            guard.feed(len(piece), time.monotonic())

    def _note_usage(self, usage: dict | None) -> None:
        """解析并留痕本次调用的 token 用量（含推理 token）。

        兼容层此前只返回正文，``usage`` 被整块丢弃，成本报表里 prompt /
        completion / total 全是 null，无法判断「推理吃掉了多少」。
        这里仅记录，不改请求体、不改返回内容。
        """
        if not usage:
            return
        details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        self.last_usage = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "reasoning_tokens": details.get("reasoning_tokens"),
            "cached_tokens": prompt_details.get("cached_tokens"),
        }
        logger.info("%s 模型 %s usage: %s", self.label, self.model, self.last_usage)

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
             budget_seconds: float | None = None) -> str:
        """chat 调用。

        stop_sequences：v2.6 —— 本次调用独立的 stop token 列表（推荐用这个，
        避免和 provider 单例共享的 set_stop_sequences 互相覆盖）。
        budget_seconds：仅跨渠道兜底传「链剩余额度」，None = 本实例预算。
        """
        user = _apply_constraints(user, constraints)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # budget_seconds 只在兜底链显式下发时才传给 `_complete`：子类/测试替身
        # 可能仍按旧签名重写 `_complete(messages, stop_sequences=...)`，
        # 不能让新增参数变成它们签名上的硬依赖（None 时行为与改造前一致）。
        kwargs: dict = {"stop_sequences": stop_sequences}
        if budget_seconds is not None:
            kwargs["budget_seconds"] = budget_seconds
        return self._complete(messages, **kwargs)

    def stream_chat_events(self, system: str, user: str, constraints: dict | None = None,
                           stop_sequences: list[str] | None = None):
        """流式续写：stream=True 逐 delta 产出 `(kind, text)`。

        kind="content"：正文增量；kind="reasoning"：模型思考增量（只用于展示
        「模型还在想」，绝不拼进正文）。首字前先到的 reasoning 会让长文生成
        看起来「一直没动静」，这里把它们暴露给 SSE 层。
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
        # 本次调用从头留痕：重试/换模型都不沿用上一次的数字。
        self.last_stream_stats = None
        for model in self._model_chain():
            payload = {**payload_base, "model": model}
            mt = self._effective_max_tokens(model)
            if mt is not None:
                payload["max_tokens"] = mt
            body = json.dumps(payload).encode("utf-8")
            for attempt in range(_RETRIES_PER_MODEL):
                yielded = False
                finish_reason: str | None = None
                # —— 流式增量留痕（只记，不参与任何判定）——
                # 计时起点在**请求发出前**：含建连、供应商排队、首 token 生成，
                # 这样「首字等待」才能拆成 request_ms（到响应头）与后续差异。
                started = time.monotonic()
                request_ms: int | None = None
                first_frame_ms: int | None = None
                first_reasoning_ms: int | None = None
                first_content_ms: int | None = None
                reasoning_chars = 0
                reasoning_chars_before_content: int | None = None
                content_chars = 0
                usage_note: dict | None = None
                try:
                    req = urllib.request.Request(
                        f"{self.base_url}/chat/completions", data=body, method="POST"
                    )
                    req.add_header("Authorization", f"Bearer {self.api_key}")
                    req.add_header("Content-Type", "application/json")
                    for hk, hv in self.extra_headers.items():
                        req.add_header(hk, hv)
                    with urllib.request.urlopen(req, timeout=self.stream_timeout) as resp:
                        request_ms = _stream_ms(started)
                        # 推理块逐 token 到达，不能逐块简单替换——开标签会被
                        # token 边界切断（"<thi" + "nk>"）。过滤器对"可能是开
                        # 标签前缀"的尾巴持有不发，故不会漏出推理内容。
                        # 每次尝试新建：重试必须从干净状态重来。
                        rfilter = ReasoningFilter()
                        # 速率窗口与非流式共用同一份判据；每次尝试新建，
                        # 窗口不跨重试累计。
                        guard = _RateGuard(self.label)
                        for raw in _iter_stream_lines(resp, guard):
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
                            # 有些 OpenAI 兼容网关（Command Code 实测）会在正文之后
                            # 再发一帧 `{"choices": [], "usage": {...}}`：只带用量、
                            # 不带增量。旧写法直接取 [0] 会 IndexError，把已经在
                            # 流式输出的正文判成失败。空 choices 按「无增量」跳过。
                            choices = obj.get("choices")
                            choice0 = (
                                choices[0]
                                if isinstance(choices, list) and choices
                                else {}
                            )
                            if obj.get("usage"):
                                self._note_usage(obj["usage"])
                                usage_note = dict(self.last_usage or {})
                            if not isinstance(choice0, dict):
                                continue
                            if choice0.get("finish_reason"):
                                finish_reason = choice0["finish_reason"]
                            delta = choice0.get("delta") or {}
                            # 部分兼容层（MiniMax/DashScope 等）把思考放在
                            # `reasoning_content`，OpenRouter 系可能叫 `reasoning`；
                            # 两者都只计数、**绝不**混进正文。
                            reasoning_piece = delta.get("reasoning_content")
                            if not isinstance(reasoning_piece, str):
                                alt = delta.get("reasoning")
                                reasoning_piece = alt if isinstance(alt, str) else ""
                            piece = delta.get("content") or ""
                            if reasoning_piece or piece:
                                if first_frame_ms is None:
                                    first_frame_ms = _stream_ms(started)
                            if reasoning_piece:
                                reasoning_chars += len(reasoning_piece)
                                if first_reasoning_ms is None:
                                    first_reasoning_ms = _stream_ms(started)
                                # 只上报「还在思考」，不把思维链文本交给前端落库/展示。
                                yield ("reasoning", reasoning_piece)
                            if piece:
                                safe = rfilter.feed(piece)
                                if safe:
                                    if first_content_ms is None:
                                        # 用户真正看到首字的那一刻：把此前已到达的
                                        # 思考字符数一起定格，用于归因首字等待。
                                        first_content_ms = _stream_ms(started)
                                        inline = rfilter.dropped_chars
                                        reasoning_chars_before_content = (
                                            reasoning_chars + inline
                                            if (reasoning_chars or inline) else None
                                        )
                                    content_chars += len(safe)
                                    yielded = True
                                    yield ("content", safe)
                        tail = rfilter.flush()
                        if tail:
                            content_chars += len(tail)
                            yielded = True
                            yield ("content", tail)
                        if rfilter.saw_reasoning:
                            logger.info(
                                "%s 模型 %s 的流里含推理块，已剥离", self.label, model
                            )
                        self._note_finish_reason(
                            finish_reason, self.label, model, mt
                        )
                        inline_reasoning = rfilter.dropped_chars
                        reasoning_total = reasoning_chars + inline_reasoning
                        self.last_stream_stats = {
                            "model": model,
                            "attempt": attempt + 1,
                            "request_ms": request_ms,
                            "first_frame_ms": first_frame_ms,
                            "first_reasoning_ms": first_reasoning_ms,
                            "first_content_ms": first_content_ms,
                            "reasoning_chars_before_content": (
                                reasoning_chars_before_content
                            ),
                            "reasoning_chars": (
                                reasoning_total if reasoning_total else None
                            ),
                            "content_chars": content_chars if content_chars else None,
                            "reasoning_source": _reasoning_source(
                                reasoning_chars, inline_reasoning
                            ),
                            "usage": usage_note,
                        }
                        logger.info(
                            "%s 模型 %s 流式留痕：首帧=%sms 首正文=%sms "
                            "reasoning=%s字 正文=%s字 usage=%s",
                            self.label, model, first_frame_ms, first_content_ms,
                            self.last_stream_stats["reasoning_chars"],
                            self.last_stream_stats["content_chars"], usage_note,
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

    #: 单模型输出上限（token）。`glm-4-flash-250414` 拒绝 `writer.max_tokens: 65536`：
    #: 2026-11-13 实测 400 `{"code":"1210","message":"max_tokens参数非法：限制数值范围[1,16384]"}`；
    #: 回退到旧 `glm-4-flash` 虽接受该值，但会直接 `finish_reason=length` 拦腰截断
    #: （第 4 章只出 1,167 字符）。按模型裁到 16384，两条路都不会再 400/截断。
    #:
    #: `glm-4-flash`（无日期别名）：2026-11-21 全书摘要实测，请求 max_tokens=8000
    #: 时 usage.completion_tokens 停在 **4095** 且 finish_reason=length —— 渠道侧
    #: 实际输出上限是 4096。不裁的话「按预算请求 8000」会被静默压到 4096。
    _MODEL_MAX_TOKENS = {"glm-4-flash-250414": 16384, "glm-4-flash": 4096}

    @property
    def digest_output_tokens(self) -> int | None:
        """摘要输出上限：按当前模型的硬上限声明（未声明的模型返回 None）。"""
        return self._MODEL_MAX_TOKENS.get(str(self.model or ""))

    #: 蒸馏/摘要的块大小：2 万字。
    #:
    #: ⚠️ 这里**不是** context window 的限制（GLM-4-Flash 是 128K token），
    #: 而是免费档约 28K TPM 的配额：块一大，单次请求就超过一分钟配额 ⇒ 429。
    #: 放大块只会把 429 变多，不会更快。
    distill_block_chars = 20_000


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

    # 强制 adaptive thinking 的模型：禁传 thinking 参数，否则 400。
    # 2026-11-12：官方只剩正式版 `MiniMax-M3.1-Flash`，`-Preview` 名已归一成正式 id，
    # 因此用前缀匹配覆盖两者——漏掉正式 id 会被塞 thinking={"type":"disabled"} 直接 400。
    _ADAPTIVE_THINKING_MODELS = ("MiniMax-M3.1-Flash",)

    # 五小时额度墙是账号级硬限额，换模型无益：429 时同一条退避重试 3 次。
    # 2026-11-12 04:56 实测：五小时窗口内跑满约 15.5M token 后开始 429，
    # 当时 12 批角色声音 + 漂移全部直接判失败，档案缺条目。退避后重试能把
    # 「瞬时/窗口边界」的 429 吃掉；真正的额度耗尽仍会在 3 次后失败。
    _RATE_LIMIT_BACKOFFS = (2.0, 5.0, 12.0)

    #: 单模型输出上限（token）。`MiniMax-Text-01` 官方硬上限 40000，配置里
    #: `writer.max_tokens: 65536` 直接下发会被整条 400 拒掉：
    #: 2026-11-13 实测响应体
    #: `invalid params, model[MiniMax-Text-01] does not support max tokens > 40000 (2013)`。
    #: 只裁这一个模型，M3.1-Flash 仍需 65536 给 adaptive thinking 留余量。
    _MODEL_MAX_TOKENS = {"MiniMax-Text-01": 40000}

    #: M3.1 自适应思考认的档位（与 `llm/thinking_models.py` 的表同源）。
    _EFFORT_LEVELS = ("low", "medium", "high")

    def __init__(self, api_key: str, base_url: str, model: str,
                 models: list[str] | None = None,
                 reasoning_effort: str | None = None,
                 floor_enabled: bool = True, **kwargs) -> None:
        extra: dict = {}
        if model.startswith(self._ADAPTIVE_THINKING_MODELS):
            # 自适应思考模型：只能调档，关不掉（传 disabled 直接 400）。
            # 显式给了档位才把档位写进请求体；
            # 预算地板受 `floor_enabled` 控制：off = 严格按调用方的预算，一点都不抬。
            effort = (reasoning_effort or "").strip().lower()
            if effort:
                from app.llm.thinking_models import budget_floor

                if effort in self._EFFORT_LEVELS:
                    extra["reasoning_effort"] = effort
                else:
                    logging.getLogger(__name__).warning(
                        "MiniMax %s 不认档位 %r（可选 low/medium/high），已忽略",
                        model, effort,
                    )
                if floor_enabled:
                    floor = budget_floor("minimax", model, effort=effort)
                    if floor:
                        kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), floor[0])
                        kwargs["timeout"] = max(int(kwargs.get("timeout") or 0), floor[1])
                        kwargs["stream_timeout"] = max(
                            int(kwargs.get("stream_timeout") or 0), floor[1]
                        )
        else:
            # 老模型（M3/M2.x）会吐思维链，能关则关；关不掉由 _strip_thinking 兜底
            extra["thinking"] = {"type": "disabled"}
        super().__init__(api_key, base_url, model, extra_body=extra, models=models, **kwargs)


class OpenCodeGoChatProvider(OpenAICompatibleChatProvider):
    """opencode go（https://opencode.ai/inference/go/openai/v1）：按量计费的 OpenAI 兼容网关。

    2026-11-12 实测（deepseek-v4.1-flash，context 1,000,000 / max output 384,000）：
    - 形状与 OpenAI `/chat/completions` 一致，`Authorization: Bearer <OC_KEY>`；
    - **必带浏览器风格 User-Agent**：urllib 默认 UA 会被 Cloudflare 拦成 403 / error 1010；
    - **必带 `x-opencode-session`**：缺失直接 400 `MissingSessionID`（用于路由/计费）。
      任意稳定字符串即可，本类默认每个实例生成一个 `novel-<uuid>`。
    - 支持 `reasoning_effort`（low/high/max），低档延迟约 3s。
    """

    label = "OpenCodeGo"

    #: 蒸馏/摘要块大小：5 万字（本渠道实测能吃下 1M token 上下文，块可以开大）。
    distill_block_chars = 50_000
    #: 单次请求能收下的正文上限：实测 context window 1,000,000 token，
    #: 中文按 1 字≈1.5 token 折算保守取 150 万字；低于此数才敢整本投喂。
    max_context_chars = 1_500_000

    DEFAULT_BASE_URL = "https://opencode.ai/inference/go/openai/v1"
    DEFAULT_MODEL = "deepseek-v4.1-flash"
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None,
                 session_id: str | None = None,
                 models: list[str] | None = None,
                 thinking_off: bool | None = None,
                 floor_enabled: bool = True, **kwargs) -> None:
        headers = {
            "User-Agent": self._BROWSER_UA,
            "x-opencode-session": session_id or f"novel-{uuid.uuid4().hex}",
        }
        # 三态：None = 用渠道默认（本渠道默认**关**思考——deepseek 的思考 token
        # 与正文共用 max_tokens，不关就没有正文）；True/False = 用户显式选择。
        off = True if thinking_off is None else bool(thinking_off)
        extra: dict = {"thinking": {"type": "disabled"}} if off else {}
        if not off and floor_enabled:
            # 显式开思考：按实测「最坏情况」抬预算下限，否则正文会被思考吃光。
            from app.llm.thinking_models import budget_floor

            floor = budget_floor("opencode-go", model or self.DEFAULT_MODEL)
            if floor:
                kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), floor[0])
                kwargs["timeout"] = max(int(kwargs.get("timeout") or 0), floor[1])
                kwargs["stream_timeout"] = max(
                    int(kwargs.get("stream_timeout") or 0), floor[1]
                )
        super().__init__(
            api_key,
            base_url or self.DEFAULT_BASE_URL,
            model or self.DEFAULT_MODEL,
            extra_body=extra,
            models=models,
            extra_headers=headers,
            **kwargs,
        )


def block_chars_for_channel(channel: str | None = None) -> int:
    """查某个渠道的蒸馏块大小能力（字）。**不构造 provider**。

    用途：指纹计算（`narrative._block_chars_for_fingerprint`）需要在没有
    provider 实例的情况下知道块大小 —— 造实例要 key、要连网络，而算指纹
    不该有这些副作用。`channel=None` ⇒ 取蒸馏角色当前配置的渠道。

    这里的每个数字都取自对应 provider 类的 `distill_block_chars`（同一张
    能力表），所以「查表」与「造实例」必然一致。改了某个类的该属性，
    这张表要同步改，否则指纹算出的块大小与实际蒸馏不同 ⇒ 缓存不命中。
    """
    if not channel:
        try:
            channel = str(
                settings.get_provider_config("distiller").get("provider") or ""
            ).strip()
        except Exception:  # noqa: BLE001 —— 配置读不到不该让调用方炸
            channel = ""
    table = {
        "zhipu": ZhipuChatProvider,
        "opencode-go": OpenCodeGoChatProvider,
    }
    cls = table.get((channel or "").strip().lower())
    if cls is None:
        return 0
    return int(getattr(cls, "distill_block_chars", 0) or 0)


class CommandCodeChatProvider(OpenAICompatibleChatProvider):
    """Command Code Provider API（https://api.commandcode.ai/provider/v1）：OpenAI 兼容网关。

    2026-11-12 实测（Goat 套餐密钥，`/provider/v1/models` 与 `/chat/completions` 均 200）：
    - `Authorization: Bearer <COMMAND_CODE_GOAT_KEY>`；**UA 必须伪装**：Cloudflare 对
      `Python-urllib/*` 直接 403 / error 1010，浏览器 UA 与 `openai-python/*` 放行；
    - 模型 id 带厂商前缀（`deepseek/deepseek-v4.1-flash`），与 opencode go 的
      `deepseek-v4.1-flash` **不是同一个字符串**，切换 provider 时必须一起换 id；
    - 模型元数据只有 `context_length` / `supported_endpoints`，未暴露 max output、
      pricing、参数能力，这些要实测；
    - 额度口径与 opencode go 独立（同一模型的 `go` 套餐额度烧完不影响这里）。

    key 默认从环境变量 `COMMAND_CODE_GOAT_KEY` 读，**不落盘、不写进 providers.yaml**。
    """

    label = "CommandCode"

    DEFAULT_BASE_URL = "https://api.commandcode.ai/provider/v1"
    DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
    KEY_ENV = "COMMAND_CODE_GOAT_KEY"
    #: Cloudflare 会按 UA 拦：`Python-urllib/3.13` 直接 403 / error 1010，
    #: 浏览器 UA 与 `openai-python/*` 都放行（2026-11-12 实测）。
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None, models: list[str] | None = None,
                 **kwargs) -> None:
        key = api_key or os.environ.get(self.KEY_ENV) or ""
        if not key:
            raise ValueError(
                f"{self.KEY_ENV} 未设置：Command Code 的 key 只从环境变量读，"
                "不接受硬编码或写进配置文件。"
            )
        super().__init__(
            key,
            base_url or self.DEFAULT_BASE_URL,
            model or self.DEFAULT_MODEL,
            models=models,
            extra_headers={"User-Agent": self._BROWSER_UA},
            **kwargs,
        )


class GenericOpenAIChatProvider(OpenAICompatibleChatProvider):
    """通用 OpenAI 兼容接口（渠道名 "openai"）。

    不绑定任何厂商：只要端点实现了 `POST {base_url}/chat/completions` +
    `Authorization: Bearer <key>` 就能接（OpenAI 官方 / 自建 one-api /
    vLLM / LM Studio / LocalAI / 第三方中转站…）。

    与父类的唯一差别是 label（界面上要显示成「通用 OpenAI」而不是泛泛的
    "LLM"）——协议层完全复用，因此本渠道**不新增任何依赖**（仍是 stdlib urllib）。
    """

    label = "OpenAI"

    def __init__(self, api_key: str, base_url: str, model: str,
                 models: list[str] | None = None,
                 thinking_off: bool = False, **kwargs) -> None:
        # DeepSeek 等通用兼容端点吃 `thinking: {"type": "disabled"}`；
        # 默认不发（`bool(None) == False`，与加开关之前逐字节一致）。
        extra: dict = {"thinking": {"type": "disabled"}} if thinking_off else {}
        super().__init__(api_key, base_url, model, extra_body=extra or None,
                         models=models, **kwargs)


class LocalFallbackLLMProvider(LLMProvider):
    """确定性模板续写（开发/测试用，非生产质量）。

    把注入的 constraints（角色 + 未填坑）回显进续写正文，保证离线可验证
    M3.3 约束确实流进了生成环；同时拼接前情/检索上下文占位，模拟真实续写结构。
    """

    def chat(self, system: str, user: str, constraints: dict | None = None,
             stop_sequences: list[str] | None = None) -> str:
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
# 跨渠道兜底（仅 KG 抽取）
# ====================================================================


def _invoke_chat(prov, system: str, user: str,
                 constraints: dict | None, budget: float) -> str:
    """把「链剩余额度」传给渠道。

    只有 OpenAI 兼容层认识 `budget_seconds`；其余实现（本地模板等）走原签名，
    避免为了兜底改一圈无关 provider 的接口。
    """
    if isinstance(prov, OpenAICompatibleChatProvider):
        return prov.chat(system, user, constraints, budget_seconds=budget)
    return prov.chat(system, user, constraints)


class FallbackChannelProvider(LLMProvider):
    """KG 抽取专用：主渠道失败后按序切后备渠道（跨 provider，不只是换 model id）。

    为什么需要：现有 `models` 链只在**同一渠道实例内**换 model id，共用同一份
    `base_url` / `api_key`。MiniMax 对抽取类请求整条挂死时，链上换 id 救不回来，
    只能手动改 `KG_EXTRACT_PROVIDER` 绕开。这里在渠道层再包一层：主渠道抛
    「可切换」错误后，换另一个渠道（自己的 key / base_url / 模型链）重试。

    **触发面**：超时、连接类错误、429、5xx；400/401/403/404 与内容不可解析
    不切换，原样抛出（见 `_fallback_eligible`）。
    **预算**：整条链共享 `chain_timeout`，每个渠道拿到「剩余额度」与自身
    `total_timeout` 的较小值——最坏时长仍是一次尝试一个链级预算，不叠加。
    **记账**：每次调用的结果留在 `last_channel_stats`，由批抽写进
    `agent_runs.note.llm`（channel_used / fallback_used / channels）。

    `fallback_providers` 为空时**不会**构造本类，`get_kg_extract_provider()`
    直接返回原 provider，行为与改造前逐字节一致。
    """

    def __init__(self, channels, *, chain_timeout: float,
                 skipped: list[dict] | None = None) -> None:
        if not channels:
            raise ValueError("FallbackChannelProvider 至少需要一个渠道")
        self.channels = list(channels)  # [(channel_id, provider)]
        self.chain_timeout = float(chain_timeout)
        # 构建期就被跳过的渠道（未知渠道名 / 未配 key）：写进 channels[]，
        # 让「为什么没走 zhipu」在 note 里可见，而不是静默消失。
        self.skipped = [dict(item) for item in (skipped or [])]
        self.last_channel_stats: dict | None = None
        self._last_provider = self.channels[0][1]
        self.channel_name = self.channels[0][0]
        self.model = getattr(self._last_provider, "model", "") or ""
        # 对上层（note / 日志）看起来仍像「一个带预算的 provider」
        self.total_timeout = int(chain_timeout)
        self.first_byte_timeout = getattr(
            self._last_provider, "first_byte_timeout", None)

    # -- 诊断字段：委托给「最后应答 / 最后尝试」的渠道 ----------------
    @property
    def last_model(self):  # noqa: ANN201
        return (getattr(self._last_provider, "last_model", None)
                or getattr(self._last_provider, "model", None))

    @property
    def last_call_stats(self):  # noqa: ANN201
        return getattr(self._last_provider, "last_call_stats", None)

    def _model_chain(self) -> list[str]:
        chain = getattr(self._last_provider, "_model_chain", None)
        return list(chain()) if callable(chain) else []

    def chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        return self._dispatch(
            lambda prov, budget: _invoke_chat(prov, system, user, constraints, budget)
        )

    def _dispatch(self, invoke):  # noqa: ANN001, ANN202
        started = time.monotonic()
        records = [dict(item) for item in self.skipped]
        last_exc: Exception | None = None
        for index, (name, prov) in enumerate(self.channels):
            remaining = self.chain_timeout - (time.monotonic() - started)
            if remaining <= 1:
                records.append({
                    "provider": name, "outcome": "chain_timeout", "elapsed_ms": 0,
                    "error": "链级预算已用尽，未发起调用",
                })
                break
            attempt_started = time.monotonic()
            try:
                result = invoke(prov, remaining)
            except Exception as exc:  # noqa: BLE001 —— 分类后决定切不切
                elapsed = int((time.monotonic() - attempt_started) * 1000)
                records.append({
                    "provider": name,
                    "model": (getattr(prov, "last_model", None)
                              or getattr(prov, "model", None)),
                    "outcome": _channel_outcome(exc),
                    "elapsed_ms": elapsed,
                    "error": str(exc)[:500],
                })
                last_exc = exc
                if not _fallback_eligible(exc):
                    # 配置类错误（400/401/403/404）当场暴露：切渠道只会掩盖它
                    self.last_channel_stats = self._stats(None, records)
                    raise
                if index + 1 < len(self.channels):
                    # 每次**真实切换**一条 warning；最后一个渠道失败由下面的
                    # error 记完整链，不再多打一条「→ 无后备渠道」的假降级。
                    logger.warning(
                        "kg 抽取渠道降级：%s → %s（%s，%dms）：%s",
                        name, self.channels[index + 1][0], records[-1]["outcome"],
                        elapsed, str(exc)[:200],
                    )
                continue
            elapsed = int((time.monotonic() - attempt_started) * 1000)
            self._last_provider = prov
            records.append({
                "provider": name,
                "model": (getattr(prov, "last_model", None)
                          or getattr(prov, "model", None)),
                "outcome": "success",
                "elapsed_ms": elapsed,
            })
            self.last_channel_stats = self._stats(name, records)
            return result

        # 全渠道失败：错误信息带完整链，原错误原文保留在 records 里
        summary = "; ".join(f"{r['provider']}={r['outcome']}" for r in records)
        self.last_channel_stats = self._stats(None, records)
        logger.error("kg 抽取全部渠道失败：%s", summary)
        raise RuntimeError(
            f"KG 抽取全部渠道失败（{summary}）；最后一个错误：{last_exc}"
        ) from last_exc

    def _stats(self, used, records: list[dict]) -> dict:  # noqa: ANN001
        return {
            "channel_used": used,
            # 「降级」定义为**实际由非主渠道应答**；全败时没有应答渠道，算 False，
            # 走了哪些渠道仍可从 channels[] 看出来。
            "fallback_used": used is not None and used != self.channels[0][0],
            "channels": records,
        }


# ====================================================================
# Writer 单例（b58c763 行为完全保留）
# ====================================================================

_PROVIDER: LLMProvider | None = None


def _build_channel_provider(
    *,
    provider: str | None = None,
    total_timeout: int | None = None,
    first_byte_timeout: int | None = None,
) -> LLMProvider:
    """按 settings 构造渠道 provider（不缓存、不读写单例）。

    `provider` 为 None 时渠道取 `settings.llm_provider`，并额外应用 Writer 的
    预算地板与思考档（`writer_budget_floor` / `effective_thinking("writer", …)`）。
    显式传值只给 `kg_extract.provider` 用：让 KG 抽取走一条与写正文不同的渠道，
    且**完全不读** writer 的地板开关与思考档（预算由 kg_extract_* 独立控制）。

    渠道选择与模型链和历史实现逐字节一致。`total_timeout` / `first_byte_timeout`
    为 None 时**不覆盖**：总预算仍由 `settings.writer_total_timeout` 兜底、首字节
    仍只受总预算约束——即默认路径行为完全不变（writer/正文生成不受影响）。

    只有需要独立预算的调用方（当前是 KG 批抽）才显式传入这两个值。
    """
    channel = provider or settings.llm_provider
    # 超时从 settings 取（默认 180 / 180 = 现网值），不再依赖类默认参数。
    _timeouts = {
        "timeout": settings.writer_timeout,
        "stream_timeout": settings.writer_stream_timeout,
    }
    if total_timeout is not None:
        _timeouts["total_timeout"] = int(total_timeout)
    if first_byte_timeout is not None:
        _timeouts["first_byte_timeout"] = int(first_byte_timeout)
    # ---- Writer 角色的预算地板 + 思考档（仅默认路径；KG 抽取路径逐字节不变）----
    #
    # 为什么限定 `provider is None`：显式传 provider 的调用方是 KG 批抽，它的
    # 预算由 kg_extract_* 独立控制，不读 writer 的地板开关与思考档。
    is_writer = provider is None
    floor_on = bool(settings.writer_budget_floor) if is_writer else True
    max_tokens = settings.writer_max_tokens
    if is_writer and floor_on:
        from app.llm.thinking_models import FLOOR_FULL

        max_tokens = max(int(max_tokens or 0), FLOOR_FULL[0])
        _timeouts["timeout"] = max(int(_timeouts["timeout"] or 0), FLOOR_FULL[1])
        _timeouts["stream_timeout"] = max(
            int(_timeouts["stream_timeout"] or 0), FLOOR_FULL[1]
        )

    def _writer_think(model_name: str) -> dict:
        """Writer 的思考档翻译；KG 抽取路径不注入（返回空 dict）。"""
        if not is_writer:
            return {}
        return _thinking_kwargs(
            channel, model_name, settings.effective_thinking("writer", channel)
        )
    if channel == "openrouter" and settings.openrouter_api_key:
        return OpenRouterChatProvider(
            settings.openrouter_api_key,
            settings.openrouter_base_url,
            settings.openrouter_model,
            models=settings.openrouter_model_list,
            max_tokens=max_tokens,
            **_timeouts,
        )
    elif channel == "zhipu" and settings.zhipu_api_key:
        zhipu_models = settings.zhipu_model_list
        # 显式指定渠道（KG 抽取解耦）时，候选链首项即首选模型：此时链必须与库里
        # `llm.zhipu_models` 完全一致，不能再把主渠道默认的 `zhipu_model`
        # （glm-4-flash-250414）插到链首。未指定渠道时行为完全不变。
        zhipu_primary = zhipu_models[0] if provider and zhipu_models else settings.zhipu_model
        return ZhipuChatProvider(
            settings.zhipu_api_key,
            settings.zhipu_base_url,
            zhipu_primary,
            models=zhipu_models,
            max_tokens=max_tokens,
            **_timeouts,
        )
    elif channel == "siliconflow" and settings.siliconflow_api_key:
        return SiliconFlowChatProvider(
            settings.siliconflow_api_key,
            settings.siliconflow_base_url,
            settings.siliconflow_model,
            models=settings.siliconflow_model_list,
            max_tokens=max_tokens,
            **_timeouts,
        )
    elif channel == "minimax" and settings.minimax_api_key:
        _think = _writer_think(settings.minimax_model)
        return MiniMaxChatProvider(
            settings.minimax_api_key,
            settings.minimax_base_url,
            settings.minimax_model,
            models=settings.minimax_model_list,
            reasoning_effort=_think.get("reasoning_effort"),
            floor_enabled=floor_on,
            max_tokens=max_tokens,
            **_timeouts,
        )
    # ------------------------------------------------------------------
    # 通用 OpenAI 兼容渠道（第 8 步新增，判据与上面 5 个分支同形）。
    #
    # **仅当生效渠道 == "openai" 时命中**（默认路径下即 llm_provider；
    # KG 抽取可通过 kg_extract.provider 覆盖）；其余渠道（zhipu / siliconflow /
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
    elif channel == "openai" and settings.openai_api_key:
        _think = _writer_think(settings.openai_model)
        return GenericOpenAIChatProvider(
            settings.openai_api_key,
            settings.llm_base_url or settings.openai_base_url,
            settings.openai_model,
            models=settings.openai_model_list,
            thinking_off=bool(_think.get("thinking_off")),
            max_tokens=max_tokens,
            **_timeouts,
        )
    elif channel == "command-code":
        # Command Code：key 优先取设置页临时值，否则读环境变量
        # COMMAND_CODE_GOAT_KEY（类内部也会再兜一次）。
        key = (
            str(settings.command_code_api_key or "").strip()
            or os.environ.get(CommandCodeChatProvider.KEY_ENV, "").strip()
        )
        if key:
            return CommandCodeChatProvider(
                key,
                settings.command_code_base_url,
                settings.command_code_model,
                models=settings.command_code_model_list,
                max_tokens=max_tokens,
                **_timeouts,
            )
    elif channel == "ollama":
        # 本地模型：Ollama 不校验 Bearer，但父类要求非空 key，给占位串。
        return GenericOpenAIChatProvider(
            str(settings.local_llm_api_key or "").strip() or "ollama",
            settings.llm_base_url or settings.local_llm_base_url,
            settings.local_llm_model,
            models=settings.ollama_model_list,
            max_tokens=max_tokens,
            **_timeouts,
        )
    elif channel == "dashscope" and settings.dashscope_api_key:
        base = settings.llm_base_url or settings.dashscope_base_url
        return DashScopeChatProvider(
            settings.dashscope_api_key, base, settings.llm_model,
            max_tokens=max_tokens,
            **_timeouts,
        )
    else:
        # 未配置 key 或显式 local：降级本地兜底，保证离线可跑
        return LocalFallbackLLMProvider()


def get_llm_provider() -> LLMProvider:
    """Writer provider 单例：b58c763 时已有的唯一获取入口，签名/行为完全保留。

    新代码请走 ProviderRegistry.get('writer') 而不是直接调本函数。

    **默认路径不传任何预算**：总时长走 `settings.writer_total_timeout`（960s），
    首字节不设额外上限——与改造前逐字节一致（正文生成首字实测 100–305s 属正常）。
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    _PROVIDER = _build_channel_provider()
    return _PROVIDER


_KG_EXTRACT_PROVIDER: LLMProvider | None = None


def get_kg_extract_provider() -> LLMProvider:
    """KG 批抽**专用** provider 单例：渠道可独立配置，预算独立。

    渠道由 `settings.kg_extract_provider`（env `KG_EXTRACT_PROVIDER`）决定：

    - 空（默认）→ 跟随主渠道 `llm_provider`，与改造前完全一致；
    - `zhipu`  → 抽取走智谱（key / base_url 取 `zhipu_*`，模型链取库里
      `llm.zhipu_models`），写正文的渠道不受影响；
    - 其它非空值 → **配置错误**，抛 `ValueError`。绝不静默回退主渠道——
      回退会把 `KG_EXTRACT_PROVIDER=zhiphu` 这类笔误藏成「配置已生效」。

    预算与渠道无关：`total_timeout = kg_extract_total_timeout`（默认 300s）、
    `first_byte_timeout = kg_extract_first_byte_timeout`（默认 120s）。

    最坏总时长 = 外层 `RETRY_LIMIT + 1`（默认 3 次尝试）× 300s ≈ 15 分钟；
    首字节挂起时每次尝试在 120s 内先暴露，不再烧穿整份预算。
    改造前 KG 抽取沿用 Writer 的 960s：`agent_runs #205` 三次各烧满 ≈ 48 分钟。

    单独持有单例（不复用 `_PROVIDER`），避免任一角色的预算/渠道改动污染 Writer。

    **跨渠道兜底**：`settings.kg_extract_fallback_providers` 非空时，在上面这个
    单例外面再包一层 `FallbackChannelProvider`（整链共享
    `kg_extract_fallback_chain_timeout`，默认 300s）。为空（默认）时**不包**，
    返回的就是原 provider，路径与改造前逐字节一致。
    """
    global _KG_EXTRACT_PROVIDER
    if _KG_EXTRACT_PROVIDER is not None:
        return _KG_EXTRACT_PROVIDER
    override = (settings.kg_extract_provider or "").strip().lower()
    if override and override not in KG_EXTRACT_CHANNELS:
        allowed = "、".join(sorted(KG_EXTRACT_CHANNELS))
        raise ValueError(
            f"kg_extract.provider={settings.kg_extract_provider!r} 不是已知渠道"
            f"（可选：空 = 跟随主渠道，或 {allowed}）。不会回退到主渠道。"
        )
    _KG_EXTRACT_PROVIDER = _build_channel_provider(
        provider=override or None,
        total_timeout=settings.kg_extract_total_timeout,
        first_byte_timeout=settings.kg_extract_first_byte_timeout,
    )
    # 自定义模型链（`kg_extract.models`，默认空 = 用渠道自己的链）。
    # 只改这个单例；Writer 单例与 settings 上的渠道链一个字都不动。
    custom_chain = [
        m.strip() for m in (settings.kg_extract_models or "").split(",") if m.strip()
    ]
    if custom_chain and hasattr(_KG_EXTRACT_PROVIDER, "set_model_chain"):
        _KG_EXTRACT_PROVIDER.set_model_chain(custom_chain)

    primary_channel = override or (settings.llm_provider or "").strip().lower() or "primary"
    # 给 note 用：单渠道时也要能回答「这条抽取跑在哪个渠道」。
    _KG_EXTRACT_PROVIDER.channel_name = primary_channel
    _KG_EXTRACT_PROVIDER = _wrap_kg_fallbacks(
        _KG_EXTRACT_PROVIDER, primary_channel,
        chain_timeout=settings.kg_extract_fallback_chain_timeout,
    )
    return _KG_EXTRACT_PROVIDER


def _kg_fallback_channel_names() -> list[str]:
    """解析 `kg_extract.fallback_providers`（逗号或分号分隔，去重保序）。"""
    raw = str(settings.kg_extract_fallback_providers or "")
    names = [n.strip().lower() for n in raw.replace(";", ",").split(",") if n.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _wrap_kg_fallbacks(primary: LLMProvider, primary_channel: str,
                       *, chain_timeout: int) -> LLMProvider:
    """按配置给 KG 抽取 provider 包一层跨渠道兜底。

    列表为空时原样返回 `primary`，保证「关掉兜底 = 与改造前逐字节一致」。
    有后备渠道被跳过（未知名 / 缺 key）时仍包一层，只为把
    `channel_skipped_no_key` 留在 `note.llm.channels[]` 里（单渠道行为不变）。
    """
    names = _kg_fallback_channel_names()
    if not names:
        return primary
    channels: list[tuple[str, LLMProvider]] = [(primary_channel, primary)]
    skipped: list[dict] = []
    seen = {primary_channel}
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        if name not in KG_EXTRACT_CHANNELS:
            skipped.append({
                "provider": name, "outcome": "channel_skipped_no_key",
                "detail": "unknown_channel", "elapsed_ms": 0,
                "error": f"未知渠道 {name!r}，已跳过",
            })
            logger.warning("KG 抽取兜底渠道 %r 不是已知渠道，已跳过（不会静默改用其它渠道）",
                           name)
            continue
        prov = _build_channel_provider(
            provider=name,
            total_timeout=settings.kg_extract_total_timeout,
            first_byte_timeout=settings.kg_extract_first_byte_timeout,
        )
        if isinstance(prov, LocalFallbackLLMProvider):
            # 没配 key 时 builder 会回落本地模板。作为**后备**用它等于静默产出
            # 空抽取，必须跳过并留痕，而不是假装渠道可用。
            skipped.append({
                "provider": name, "outcome": "channel_skipped_no_key",
                "detail": "no_api_key", "elapsed_ms": 0,
                "error": f"渠道 {name!r} 未配置 API key，已跳过",
            })
            logger.warning("KG 抽取兜底渠道 %r 未配置 API key，已跳过", name)
            continue
        custom = [
            m.strip() for m in (settings.kg_extract_models or "").split(",") if m.strip()
        ]
        if custom and hasattr(prov, "set_model_chain"):
            prov.set_model_chain(custom)
        prov.channel_name = name
        channels.append((name, prov))

    if len(channels) <= 1 and not skipped:
        # 配了兜底但一个都用不上（或全被去重掉）：保持原路径
        return primary
    if skipped and len(channels) <= 1:
        # 有后备渠道被跳过（未知渠道名 / 未配 key）：仍包一层**只为留痕**——
        # `note.llm.channels[]` 要看得见 channel_skipped_no_key，而不是静默消失。
        # 只有一个可用渠道，行为与不包时一致。
        logger.warning(
            "KG 抽取配置了兜底渠道但全部被跳过（%s），本次仍走主渠道 %s",
            "、".join(str(item.get("provider")) for item in skipped), primary_channel)
    logger.info(
        "KG 抽取启用跨渠道兜底：%s（链级预算 %ss）",
        " → ".join(name for name, _ in channels), chain_timeout,
    )
    return FallbackChannelProvider(
        channels, chain_timeout=chain_timeout, skipped=skipped)


def reset_kg_extract_provider_cache() -> None:
    """只清 KG 抽取单例：改抽取渠道/模型链时调用。

    与 `reset_provider_cache()` 的区别：后者会连同 Writer 单例一起清掉。
    「模型设置」页改 KG 抽取配置时不该顺带重建写正文的 provider——虽然重建
    本身不改配置，但多一次无谓的单例重建会让「改抽取」看上去动了生成链路。
    """
    global _KG_EXTRACT_PROVIDER
    _KG_EXTRACT_PROVIDER = None


def reset_provider_cache() -> None:
    """清掉 Writer provider 单例缓存：下次 get_llm_provider() 按最新 settings 重建。

    仅影响 Writer；Assistant 单例由 reset_assistant_provider_cache() 管理，互不干扰。
    KG 抽取单例（`_KG_EXTRACT_PROVIDER`）也在这里一并清掉——它和 Writer 读同一份
    渠道配置（llm_provider / key / 模型链），配置热切换时必须同步重建，否则抽取会
    继续用旧渠道。
    """
    global _PROVIDER, _KG_EXTRACT_PROVIDER
    _PROVIDER = None
    _KG_EXTRACT_PROVIDER = None


def apply_runtime_config(overrides: dict[str, str | None]) -> None:
    """把持久化/前端传入的配置覆盖到 settings 并重置对应 role 的 provider 缓存（热切换）。

    overrides 的键为 settings 字段名（如 llm_provider / zhipu_api_key /
    assistant_model / distiller_model）；值为 None 的项跳过。
    - 涉及 assistant_* 字段：额外重置 Assistant 缓存；
    - 涉及 distiller_* 字段：额外重置 Distiller 缓存；
    - 涉及其它字段（含 writer_* / llm_provider / zhipu_api_key / openrouter_* 等）：
      走与 b58c763 一致的行为——重置 Writer 单例缓存（保持向后兼容）。
    - 同一调用里几类都改：三个角色各自重置，互不影响。
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


def _thinking_kwargs(provider: str, model: str, thinking: str) -> dict:
    """把角色级的「思考档位」翻译成 provider 构造参数。

    返回的 dict 直接 `**kwargs` 给构造器：`reasoning_effort` 或 `thinking_off`。
    拿不准就不返回任何键（保持官方默认，行为与加这个开关之前一致）。
    """
    value = (thinking or "").strip().lower()
    if not value:
        return {}
    prov = (provider or "").strip().lower()
    name = (model or "").strip()

    if prov == "minimax" and name.startswith("MiniMax-M3.1"):
        # M3.1：只能调档，关不掉
        if value == "off":
            logging.getLogger(__name__).info(
                "MiniMax M3.1 关不掉思考，已降级为最低档 low（而不是忽略这个设置）"
            )
            return {"reasoning_effort": "low"}
        if value in ("low", "medium", "high", "xhigh", "max"):
            return {"reasoning_effort": value}
        return {}

    if value == "off":
        # 能关的：老 M3（类里本来默认就关）与通用兼容渠道（DeepSeek）。
        # opencode-go 也收进来：它渠道层默认就关，显式「off」应得到同一结果，
        # 而不是被静默忽略（返回 {} 后靠默认值兜底，看起来对、读代码的人会误解）。
        if prov in ("minimax", "openai", "opencode-go"):
            return {"thinking_off": True}
    if value == "default":
        # 「开思考」= 显式不关。只对 opencode-go 显式处理（渠道层默认关），
        # 其它渠道的 "default" 保持原样（不传任何参数 ⇒ 渠道默认不变）。
        if prov == "opencode-go":
            return {"thinking_off": False}
    if prov == "opencode-go" and value in ("low", "medium", "high", "xhigh", "max"):
        # 档位语义 = 开思考（该渠道只认 reasoning_effort，关/开由 thinking_off 决定）。
        return {"thinking_off": False}
    return {}


def build_provider(provider: str, api_key: str, model: str, base_url: str = "",
                   max_tokens: int | None = None, timeout: int | None = None,
                   stream_timeout: int | None = None,
                   total_timeout: int | None = None,
                   thinking_off: bool | None = None,
                   reasoning_effort: str | None = None,
                   floor_enabled: bool | None = None) -> LLMProvider:
    """按渠道名直接构造 provider 实例（测试端点用，不走缓存/单例）。

    max_tokens / timeout / stream_timeout 是 ADR-2026-10-12 新增字段：
    - Assistant 工厂会显式传 timeout=60 / stream_timeout=60；
    - 其它调用方（settings.py 的连通性测试）不传 → 取 `settings.llm_probe_timeout`
      / `llm_stream_timeout`（默认 120 / 180 = 与 b58c763 逐秒一致，
      stream_timeout=180 是关键，不能与 chat 共用 120）。

    `reasoning_effort` / `floor_enabled` 由角色工厂（`build_role_provider`）传入：
    前者只对 MiniMax 自适应思考模型有意义，后者控制渠道内部是否抬预算地板；
    未传时与改造前逐字节一致。
    """
    if timeout is None or not isinstance(timeout, int) or timeout <= 0:
        timeout = settings.llm_probe_timeout
    if stream_timeout is None or not isinstance(stream_timeout, int) or stream_timeout <= 0:
        stream_timeout = settings.llm_stream_timeout
    # 非流式硬顶：本入口只服务 Assistant / Distiller / 连通性测试，一律按**本次
    # 的 recv 超时**推（×2），不能沿用 settings.writer_total_timeout——那是 Writer
    # 的 960s 预算，挂在诊断/QA 路径上会把「快速失败」变成「长等」。
    if total_timeout is None or not isinstance(total_timeout, int) or total_timeout <= 0:
        total_timeout = max(120, timeout * 2)
    if provider == "openrouter":
        url = base_url or settings.openrouter_base_url
        return OpenRouterChatProvider(api_key, url, model, models=[model],
                                       max_tokens=max_tokens, timeout=timeout,
                                       stream_timeout=stream_timeout,
                                       total_timeout=total_timeout)
    if provider == "zhipu":
        url = base_url or settings.zhipu_base_url
        return ZhipuChatProvider(api_key, url, model, models=[model],
                                  max_tokens=max_tokens, timeout=timeout,
                                  stream_timeout=stream_timeout,
                                  total_timeout=total_timeout)
    if provider == "siliconflow":
        url = base_url or settings.siliconflow_base_url
        return SiliconFlowChatProvider(api_key, url, model, models=[model],
                                        max_tokens=max_tokens, timeout=timeout,
                                        stream_timeout=stream_timeout,
                                        total_timeout=total_timeout)
    if provider == "minimax":
        url = base_url or settings.minimax_base_url
        return MiniMaxChatProvider(api_key, url, model, models=[model],
                                    reasoning_effort=reasoning_effort,
                                    floor_enabled=True if floor_enabled is None else bool(floor_enabled),
                                    max_tokens=max_tokens, timeout=timeout,
                                    stream_timeout=stream_timeout,
                                    total_timeout=total_timeout)
    if provider == "dashscope":
        url = base_url or settings.dashscope_base_url
        return DashScopeChatProvider(api_key, url, model,
                                      max_tokens=max_tokens, timeout=timeout,
                                      stream_timeout=stream_timeout,
                                      total_timeout=total_timeout)
    if provider == "opencode-go":
        # opencode Go 订阅通道：类里有默认端点与必需请求头（浏览器 UA +
        # x-opencode-session），这里只需要把三态 `thinking_off` 透传下去。
        return OpenCodeGoChatProvider(api_key, base_url or None, model, models=[model],
                                      thinking_off=thinking_off,
                                      floor_enabled=True if floor_enabled is None else bool(floor_enabled),
                                      max_tokens=max_tokens, timeout=timeout,
                                      stream_timeout=stream_timeout,
                                      total_timeout=total_timeout)
    if provider == "openai":
        # 通用 OpenAI 兼容渠道：任何实现了 /chat/completions + Bearer 鉴权的端点
        # （官方 / one-api / vLLM / LM Studio / 中转站…）。端点优先取调用方给的
        # base_url，否则回落 settings.openai_base_url（默认官方地址）。
        # 注意：**必须**在下面的 LocalFallback 兜底 return 之前——
        # 未知 provider 名会静默降级成本地兜底，那会让「渠道名拼错」表现为
        # 「配了但一直在用离线模板」，是极难排查的静默失效。
        url = base_url or settings.openai_base_url
        return GenericOpenAIChatProvider(api_key, url, model, models=[model],
                                         thinking_off=bool(thinking_off),
                                         max_tokens=max_tokens, timeout=timeout,
                                         stream_timeout=stream_timeout,
                                         total_timeout=total_timeout)
    if provider == "command-code":
        # key 可空：类内部会读环境变量 COMMAND_CODE_GOAT_KEY。
        url = base_url or settings.command_code_base_url
        return CommandCodeChatProvider(api_key or None, url, model, models=[model],
                                       max_tokens=max_tokens, timeout=timeout,
                                       stream_timeout=stream_timeout,
                                       total_timeout=total_timeout)
    if provider == "ollama":
        url = base_url or settings.local_llm_base_url
        return GenericOpenAIChatProvider(api_key or "ollama", url, model, models=[model],
                                         max_tokens=max_tokens, timeout=timeout,
                                         stream_timeout=stream_timeout,
                                         total_timeout=total_timeout)
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
        # master 把 `config.zhipu_model` 升级为 `glm-4-flash-250414`（2025-04 快照，
        # 7 类抽取全填 / event_type 多样化）时漏改本表与 PROVIDER_META，三处不一致。
        # 以 config 为运行时真值源，此处补齐（由 test_model_lists_are_consistent_* 守住）。
        "zhipu": "glm-4-flash-250414",
        "siliconflow": "Qwen/Qwen2.5-7B-Instruct",
        "minimax": "MiniMax-M3.1-Flash-Preview",
        "dashscope": "qwen-plus",
        "openai": "gpt-4o-mini",
        "opencode-go": "deepseek-v4.1-flash",
        "command-code": "deepseek/deepseek-v4.1-flash",
        "ollama": "qwen2.5:7b",
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
    # 两类渠道允许没有「已保存的 key」：
    #   command-code：key 由环境变量 COMMAND_CODE_GOAT_KEY 提供；
    #   ollama      ：本地模型不校验 Authorization。
    if not api_key and provider == "command-code":
        api_key = os.environ.get("COMMAND_CODE_GOAT_KEY", "").strip()
    if not api_key and provider != "ollama":
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
    # 预算地板（默认 on）：不足 FLOOR_FULL 一律抬到 32768/300，避免思考 token
    # 把正文/JSON 预算吃光；off = 严格按用户值，一个数字都不改。
    floor_on = bool(cfg.get("budget_floor", True))
    if floor_on:
        from app.llm.thinking_models import FLOOR_FULL

        kwargs["max_tokens"] = max(int(kwargs.get("max_tokens") or 0), FLOOR_FULL[0])
        kwargs["timeout"] = max(int(kwargs.get("timeout") or 0), FLOOR_FULL[1])
        kwargs["stream_timeout"] = max(
            int(kwargs.get("stream_timeout") or 0), FLOOR_FULL[1]
        )
    # 角色级思考档位：翻译成 reasoning_effort / thinking_off；空串 = 不加任何键。
    kwargs.update(_thinking_kwargs(provider, model, str(cfg.get("thinking") or "")))
    return build_provider(provider, api_key, model, base_url,
                          floor_enabled=floor_on, **kwargs)


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
