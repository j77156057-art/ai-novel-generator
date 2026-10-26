"""Assistant provider 渠道 key 回退单测（providers.yaml 注释承诺的行为）。

背景（本文件存在的理由）：
    backend/config/providers.yaml 的 assistant 段写着

        api_key: ""            # 空时按 provider 渠道名去 settings 拿对应 *_api_key

    但修复前 Settings.get_provider_config('assistant') 直接返回 settings.assistant_api_key
    （恒为空），于是 build_assistant_provider() 恒定降级 LocalFallbackLLMProvider ——
    **Assistant 从来没有真正调用过模型**，而端点照样返回 200 + 结构完整的响应。

    这类缺陷的特点是「契约全绿、语义全错」：写链路/权限/并发都验过了，唯独没人验
    「模型到底有没有被调到」。所以这里的断言全部打在**实例类型与 api_key 取值**上，
    而不是响应结构上。

覆盖：
  1. assistant_api_key 空 + 渠道 key 有值 → 真实渠道 provider（非 LocalFallback）；
  2. assistant_api_key 显式配置 → 显式值优先，不被 legacy 覆盖；
  3. 换渠道（minimax / openrouter / siliconflow / dashscope）取对应渠道 key + base_url；
  4. 五渠道 key 全空 → 仍降级 LocalFallback，且不抛错；
  5. 未知渠道名 → 降级 LocalFallback（不抛 ValueError）；
  6. Writer 对照：同一环境下 get_llm_provider() 与 get_provider_config('writer')
     返回值/超时（120 / 180）完全不受本次修复影响。
"""

from __future__ import annotations

import os
import sys

# ─── bootstrap：与仓库其它测试一致，把 backend/ 加进 sys.path ────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
import app.llm.provider as llm_provider_mod  # noqa: E402
from app.llm.provider import (  # noqa: E402
    DashScopeChatProvider,
    LocalFallbackLLMProvider,
    MiniMaxChatProvider,
    OpenRouterChatProvider,
    SiliconFlowChatProvider,
    ZhipuChatProvider,
    build_assistant_provider,
    get_llm_provider,
)


# Assistant 显式字段 + 五渠道 legacy 字段 + Writer legacy 字段，一次性快照/还原，
# 避免本机 .env 里真 key 的存在与否影响断言（测试必须与环境无关）。
_SNAPSHOT_FIELDS = (
    "assistant_provider", "assistant_api_key", "assistant_base_url",
    "assistant_model", "assistant_max_tokens", "assistant_timeout",
    "zhipu_api_key", "zhipu_base_url",
    "openrouter_api_key", "openrouter_base_url",
    "siliconflow_api_key", "siliconflow_base_url",
    "minimax_api_key", "minimax_base_url",
    "dashscope_api_key", "dashscope_base_url",
    "llm_provider", "llm_model",
)


@pytest.fixture(autouse=True)
def _isolated_settings():
    """每个用例：清空所有相关 key + 重置双轨单例；结束后原样还原。"""
    snap = {f: getattr(settings, f) for f in _SNAPSHOT_FIELDS}

    # 起始状态：所有 key 置空 → 基线一定是 LocalFallback（与本机 .env 无关）
    for f in _SNAPSHOT_FIELDS:
        if f.endswith("_api_key") or f.endswith("_base_url") or f == "assistant_api_key":
            setattr(settings, f, "")
    settings.assistant_provider = ""
    settings.assistant_model = ""
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()

    yield

    for f, v in snap.items():
        setattr(settings, f, v)
    llm_provider_mod.reset_provider_cache()
    llm_provider_mod.reset_assistant_provider_cache()


# ---------------------------------------------------------------------
# 1. 渠道回退：assistant_api_key 为空 → 借 legacy 渠道 key
# ---------------------------------------------------------------------


def test_empty_assistant_key_falls_back_to_zhipu_channel_key():
    """核心回归用例：assistant_api_key 空 + zhipu_api_key 有值 → ZhipuChatProvider。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""      # 显式留空（providers.yaml 的默认态）
    settings.assistant_model = "glm-4-flash"
    settings.zhipu_api_key = "zhipu-legacy-key"

    provider = build_assistant_provider()

    assert not isinstance(provider, LocalFallbackLLMProvider), (
        "Assistant 仍在降级本地兜底 —— 模型从来没被调用"
    )
    assert isinstance(provider, ZhipuChatProvider)
    assert provider.api_key == "zhipu-legacy-key"
    assert provider.model == "glm-4-flash"


def test_empty_assistant_base_url_falls_back_to_channel_base_url():
    """base_url 同样回退：空 → 渠道自己的 base_url（不是默认 dashscope 地址）。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""
    settings.assistant_base_url = ""
    settings.zhipu_api_key = "zhipu-legacy-key"
    settings.zhipu_base_url = "https://open.bigmodel.cn/api/paas/v4"

    provider = build_assistant_provider()

    assert isinstance(provider, ZhipuChatProvider)
    assert provider.base_url == "https://open.bigmodel.cn/api/paas/v4"


# ---------------------------------------------------------------------
# 2. 显式配置优先，不被 legacy 覆盖
# ---------------------------------------------------------------------


def test_explicit_assistant_key_wins_over_legacy_channel_key():
    """assistant_api_key 显式配置时，legacy 渠道 key **不得**覆盖它。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "explicit-assistant-key"
    settings.assistant_base_url = "https://assistant.example.com/v4"
    settings.zhipu_api_key = "zhipu-legacy-key"
    settings.zhipu_base_url = "https://open.bigmodel.cn/api/paas/v4"

    provider = build_assistant_provider()

    assert isinstance(provider, ZhipuChatProvider)
    assert provider.api_key == "explicit-assistant-key"
    assert provider.base_url == "https://assistant.example.com/v4"


def test_explicit_empty_assistant_base_url_only_keeps_explicit_key():
    """显式只配 api_key、base_url 留空 → key 用显式的，url 用渠道的（字段独立回退）。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "explicit-assistant-key"
    settings.assistant_base_url = ""
    settings.zhipu_api_key = "zhipu-legacy-key"
    settings.zhipu_base_url = "https://open.bigmodel.cn/api/paas/v4"

    provider = build_assistant_provider()

    assert isinstance(provider, ZhipuChatProvider)
    assert provider.api_key == "explicit-assistant-key"
    assert provider.base_url == "https://open.bigmodel.cn/api/paas/v4"


# ---------------------------------------------------------------------
# 3. 换渠道 → 取对应渠道 key / base_url
# ---------------------------------------------------------------------


@pytest.mark.parametrize("channel,key_field,url_field,expected_cls", [
    ("minimax", "minimax_api_key", "minimax_base_url", MiniMaxChatProvider),
    ("openrouter", "openrouter_api_key", "openrouter_base_url", OpenRouterChatProvider),
    ("siliconflow", "siliconflow_api_key", "siliconflow_base_url", SiliconFlowChatProvider),
    ("dashscope", "dashscope_api_key", "dashscope_base_url", DashScopeChatProvider),
    ("zhipu", "zhipu_api_key", "zhipu_base_url", ZhipuChatProvider),
])
def test_channel_switch_reads_matching_legacy_key(channel, key_field, url_field, expected_cls):
    """渠道名决定读哪个 legacy 字段：不会串到别的渠道去。"""
    settings.assistant_provider = channel
    settings.assistant_api_key = ""
    settings.assistant_base_url = ""
    setattr(settings, key_field, f"{channel}-legacy-key")
    setattr(settings, url_field, f"https://{channel}.example.com/v1")

    provider = build_assistant_provider()

    assert isinstance(provider, expected_cls), f"渠道 {channel} 应构造 {expected_cls.__name__}"
    assert provider.api_key == f"{channel}-legacy-key"
    assert provider.base_url == f"https://{channel}.example.com/v1"


def test_only_one_channel_has_key_and_others_stay_empty():
    """只有目标渠道配了 key 时，其它渠道 key 为空也不影响 —— 不会被误读成有 key。"""
    settings.assistant_provider = "minimax"
    settings.assistant_api_key = ""
    settings.minimax_api_key = "minimax-only-key"

    provider = build_assistant_provider()

    assert isinstance(provider, MiniMaxChatProvider)
    assert provider.api_key == "minimax-only-key"


# ---------------------------------------------------------------------
# 4. 全空 → 降级（不抛错）
# ---------------------------------------------------------------------


def test_all_channels_empty_still_degrades_to_local_fallback():
    """五个渠道 key 全空 → LocalFallbackLLMProvider，且不抛任何异常。"""
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""
    # 五渠道 key 已在 fixture 里全置空

    provider = build_assistant_provider()

    assert isinstance(provider, LocalFallbackLLMProvider)


def test_empty_provider_degrades_to_local_fallback():
    """assistant_provider 为空（未启用 Assistant）→ LocalFallback。"""
    settings.assistant_provider = ""
    settings.zhipu_api_key = "zhipu-legacy-key"  # 有 key 也不启用

    assert isinstance(build_assistant_provider(), LocalFallbackLLMProvider)


def test_local_provider_degrades_even_with_channel_key():
    """provider='local' 显式降级：即便渠道 key 存在也不联网。"""
    settings.assistant_provider = "local"
    settings.zhipu_api_key = "zhipu-legacy-key"

    assert isinstance(build_assistant_provider(), LocalFallbackLLMProvider)


def test_unknown_channel_returns_empty_and_degrades():
    """未知渠道名：回退表查不到 → 空串 → 降级，绝不抛 ValueError。"""
    settings.assistant_provider = "not-a-real-channel"
    settings.assistant_api_key = ""
    settings.zhipu_api_key = "zhipu-legacy-key"

    cfg = settings.get_provider_config("assistant")
    assert cfg["api_key"] == ""
    assert isinstance(build_assistant_provider(), LocalFallbackLLMProvider)


def test_legacy_channel_field_helper_directly():
    """直测私有 helper：五个渠道全覆盖 + 未知渠道返回空串。"""
    settings.zhipu_api_key = "zk"
    settings.openrouter_api_key = "ok"
    settings.siliconflow_api_key = "sk"
    settings.minimax_api_key = "mk"
    settings.dashscope_api_key = "dk"
    settings.zhipu_base_url = "zu"
    settings.openrouter_base_url = "ou"
    settings.siliconflow_base_url = "su"
    settings.minimax_base_url = "mu"
    settings.dashscope_base_url = "du"

    for channel, key, url in (
        ("zhipu", "zk", "zu"),
        ("openrouter", "ok", "ou"),
        ("siliconflow", "sk", "su"),
        ("minimax", "mk", "mu"),
        ("dashscope", "dk", "du"),
    ):
        assert settings._legacy_channel_field(channel, "api_key") == key
        assert settings._legacy_channel_field(channel, "base_url") == url

    assert settings._legacy_channel_field("nope", "api_key") == ""
    assert settings._legacy_channel_field("nope", "base_url") == ""
    assert settings._legacy_channel_field("", "api_key") == ""


# ---------------------------------------------------------------------
# 6. 端点级回归：/qa 必须真的调到模型 chat（不是静默走本地兜底）
# ---------------------------------------------------------------------


def test_qa_endpoint_really_reaches_assistant_chat(monkeypatch):
    """`/api/assistant/qa` 拿到真实渠道 provider 后，必须真的调用它的 chat。

    这条用例是本次缺陷的**永久哨兵**：修复前 provider 是 LocalFallback，
    chat 一样「成功返回」（返回的是本地模板），端点照样 200 —— 所以只有把
    「被调用的是哪一个 chat」记下来，才能把「模型没被调到」从隐形缺陷变成红灯。
    """
    from app.agents.registry import reset_tool_registry
    from app.db import SessionLocal, ensure_schema
    from app.llm.registry import reset_provider_registry
    from app.models import Project
    from app.routers import assistant as assistant_router

    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = ""      # providers.yaml 的默认态（留空 = 借渠道 key）
    settings.assistant_base_url = ""
    settings.assistant_model = "glm-4-flash"
    settings.zhipu_api_key = "unit-test-key"

    calls: list[tuple[str, str]] = []

    def fake_chat(self, system: str, user: str, constraints: dict | None = None) -> str:
        # 真 ReAct 循环的协议是「每轮一个决策 JSON」。这条用例守的是
        # 「provider 确实被调到、答案确实来自模型」，所以这里返回一个**合规的
        # final 决策**：第 1 轮就收尾 → 仍只调用 1 次 chat，断言口径不变。
        # （改造前这里返回纯文本；纯文本在新协议下是 invalid_decision，
        #  会触发一次修复重试并降级——那不是这条用例要测的东西。）
        calls.append((system, user))
        return (
            '{"thought": "证据已足够", '
            '"final": "【模型回答】基于 evidence，主角当前最大的困境是身份可能暴露。"}'
        )

    monkeypatch.setattr(ZhipuChatProvider, "chat", fake_chat)

    ensure_schema()
    db = SessionLocal()
    try:
        proj = Project(title="哨兵项目", target_length=3000, mode="quick", status="created")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        reset_provider_registry()
        llm_provider_mod.reset_assistant_provider_cache()
        reset_tool_registry()

        resp = assistant_router.assistant_qa(
            body=assistant_router.AssistantQARequest(
                project_id=proj.id, query="主角当前最大的困境是什么？"
            ),
            db=db,
        )
    finally:
        db.close()
        reset_tool_registry()
        reset_provider_registry()
        llm_provider_mod.reset_assistant_provider_cache()

    assert len(calls) == 1, f"ZhipuChatProvider.chat 应被调用 1 次，实际 {len(calls)} 次"
    system, user = calls[0]
    assert "助手" in system
    assert "主角当前最大的困境是什么？" in user
    # 返回的是模型的答案，而不是本地兜底模板
    assert resp["answer"] == "【模型回答】基于 evidence，主角当前最大的困境是身份可能暴露。"
    assert "离线兜底" not in resp["answer"]


# ---------------------------------------------------------------------
# 5. Writer 对照组：本次修复不得改变 Writer 任何行为
# ---------------------------------------------------------------------


def test_writer_config_unchanged_by_assistant_fix():
    """get_provider_config('writer') 返回的字段集合（含 `_fallback_to_legacy` 标记）。

    契约在 2026-10-26 **有意**扩了一个字段：`thinking`（思考档位）。
    为什么它不算破坏这条红线：

    - 它是**新增的可选字段**，默认空串 ⇒ `_thinking_kwargs()` 返回 `{}`，
      请求体一个字段都不多发 —— **Writer 的实际行为不变**（下面那条断言钉的就是这个：
      `_fallback_to_legacy` 仍为 True，工厂仍走原单例）；
    - 它存在的理由是对正文有正收益：实测 M3.1 的 effort=low 让 2400 字续写
      从「66.7 秒、正文为空（预算被思考吃满）」变成「36 秒、正文完整」。

    ⇒ 红线是「**行为**不变」，不是「字段集合永不增长」。新增字段必须默认不生效。
    """
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = "zhipu-legacy-key"

    cfg = settings.get_provider_config("writer")

    assert set(cfg) == {
        "provider", "api_key", "base_url", "model",
        "max_tokens", "timeout", "thinking", "_fallback_to_legacy",
    }
    # writer_* 默认全空 → 委托标记必须为 True（工厂据此走 get_llm_provider() 单例）
    assert cfg["_fallback_to_legacy"] is True
    # 新增字段默认空 ⇒ 不往请求体加任何东西（行为不变的关键）
    assert cfg["thinking"] == ""


def test_writer_singleton_keeps_its_own_key_and_timeouts():
    """硬红线：Writer 单例仍用 llm_provider 渠道的 key，超时是 **Writer 自己的**。

    对照组意义：Assistant 借用 legacy **字段**读 key，不得反过来把 Writer 的
    单例、key 或超时改成 Assistant 的 60 / 60。

    超时值**不写死**：写手基线（`backend/config/providers.yaml` 的 writer 块）
    是可调的 —— 2026-10-25 从 120 抬到 300（adaptive thinking 模型单段要
    112~141 秒，120 会撞线）。断言的是「Writer 用自己的基线值」，不是某个数字。
    """
    settings.llm_provider = "zhipu"
    settings.zhipu_api_key = "writer-channel-key"
    settings.zhipu_model = "glm-4-flash"
    # Assistant 侧独立配置（显式 key，避免与 Writer 共用同一字符串造成假阳性）
    settings.assistant_provider = "zhipu"
    settings.assistant_api_key = "explicit-assistant-key"

    writer = get_llm_provider()
    assistant = build_assistant_provider()

    assert isinstance(writer, ZhipuChatProvider)
    assert writer.api_key == "writer-channel-key"
    assert writer.timeout == settings.writer_timeout
    assert writer.stream_timeout == 180

    assert isinstance(assistant, ZhipuChatProvider)
    assert assistant.api_key == "explicit-assistant-key"
    assert assistant.timeout == 60
    assert assistant.stream_timeout == 60

    # 双侧实例互相独立（不是同一个对象）
    assert writer is not assistant
    # Writer 单例仍然被 _PROVIDER 缓存（行为未变）
    assert get_llm_provider() is writer
