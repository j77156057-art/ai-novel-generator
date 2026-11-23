"""跨渠道陈旧模型链 / 单次失败留痕的回归。

背景（2026-11-23）：项目 11 生成后处理出现 10 次「MiniMax 模型链全部失败，
最后错误: HTTP Error 400」。真因是 legacy `llm.models` 里留着上一个渠道的
`deepseek/deepseek-v4.1-flash`，启动加载时被映射进 MiniMax 的候选链——第二个
候选在 MiniMax 端点必然 400，把首个模型的真实错误盖掉。
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from urllib.error import HTTPError

from app.config import settings
from app.llm.provider import (
    MiniMaxChatProvider,
    _http_error_brief,
    _thinking_off_allowed,
    reset_provider_cache,
)
from app.routers.settings import load_persisted_llm_config


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """只服务 `load_persisted_llm_config` 的 `db.execute(select(...))`。"""

    def __init__(self, rows) -> None:
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return _FakeResult(self._rows)


def _restore(snapshot: dict) -> None:
    for name in [n for n in vars(settings) if n not in snapshot]:
        delattr(settings, name)
    vars(settings).update(snapshot)
    reset_provider_cache()


def test_writer_explicit_model_ignores_stale_legacy_chain():
    """writer 显式选过模型时，legacy `llm.models` 不得污染该渠道候选链。"""
    snapshot = dict(vars(settings))
    try:
        settings.llm_provider = "command-code"
        settings.minimax_model = "MiniMax-M3.1-Flash"
        settings.minimax_models = "MiniMax-M3.1-Flash-Preview"
        settings.minimax_api_key = "test-key"

        rows = [
            SimpleNamespace(key="llm.provider", value="minimax"),
            SimpleNamespace(key="llm.model", value="MiniMax-M3.1-Flash-Preview"),
            # 上一个渠道留下的陈旧链。
            SimpleNamespace(key="llm.models", value="deepseek/deepseek-v4.1-flash"),
            SimpleNamespace(key="writer.provider", value="minimax"),
            SimpleNamespace(key="writer.model", value="MiniMax-M3.1-Flash-Preview"),
        ]
        load_persisted_llm_config(_FakeSession(rows))

        assert settings.llm_provider == "minimax"
        assert settings.minimax_model == "MiniMax-M3.1-Flash-Preview"
        # 陈旧链没有被映射进 minimax_models。
        assert "deepseek" not in settings.minimax_models

        reset_provider_cache()
        chain = settings.minimax_model_list
        assert all("deepseek" not in m for m in chain), chain
    finally:
        _restore(snapshot)


def test_legacy_chain_still_applies_without_writer_model():
    """没有 writer 显式模型时，legacy 链的原有行为保持不变。"""
    snapshot = dict(vars(settings))
    try:
        settings.llm_provider = "command-code"
        settings.command_code_models = "deepseek/deepseek-v4.1-flash"
        rows = [
            SimpleNamespace(key="llm.provider", value="command-code"),
            SimpleNamespace(key="llm.models", value="deepseek/deepseek-v4.1-flash,other/model"),
        ]
        load_persisted_llm_config(_FakeSession(rows))
        assert settings.command_code_models == "deepseek/deepseek-v4.1-flash,other/model"
    finally:
        _restore(snapshot)


def test_thinking_off_is_not_sent_to_minimax_m31():
    """MiniMax-M3.1 关不掉思考：不发 disabled，改为压到最低档 low。"""
    assert _thinking_off_allowed("MiniMax-M3.1-Flash-Preview") is False

    prov = MiniMaxChatProvider("test-key", "http://127.0.0.1:9/v1", "MiniMax-M3.1-Flash-Preview")
    variant = prov.variant(thinking_off=True, max_tokens=16000)
    assert "thinking" not in (variant.extra_body or {})
    assert variant.extra_body.get("reasoning_effort") == "low"
    assert variant.max_tokens == 16000

    # 即使写手角色配的是 high，校正/改写变体也必须压到 low。
    high = MiniMaxChatProvider(
        "test-key", "http://127.0.0.1:9/v1", "MiniMax-M3.1-Flash-Preview",
        reasoning_effort="high",
    )
    assert high.variant(thinking_off=True).extra_body.get("reasoning_effort") == "low"

    # 老 M3 是 off_capable，仍按旧行为发 disabled，不加 effort。
    assert _thinking_off_allowed("MiniMax-M3") is True
    old = MiniMaxChatProvider("test-key", "http://127.0.0.1:9/v1", "MiniMax-M3")
    old_variant = old.variant(thinking_off=True)
    assert old_variant.extra_body.get("thinking") == {"type": "disabled"}
    assert "reasoning_effort" not in old_variant.extra_body


def test_rewrite_style_retries_without_temperature_on_type_error():
    """老 provider 的 chat 不接 temperature：文风改写不能因此整段放弃。"""
    from app.generation.rewrite import rewrite_style

    class _NoTempProvider:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, system: str, user: str):  # 只接受旧签名
            self.calls += 1
            return "他慢慢走进屋里看了一圈。桌上摆着一只旧茶杯。窗外雨下得很小。他坐下来一直没说话。"

    provider = _NoTempProvider()
    text = "他走进屋里看了看。桌上放着一只旧茶杯。窗外雨下得很小。他坐下没说话。"
    _out, info = rewrite_style(text, provider)
    assert provider.calls == 1
    assert info.get("reason") != "调用失败: TypeError"


def test_http_error_brief_reads_body_once():
    """400 的响应体要进错误摘要，且 body 只读一次后仍能被限流识别复用。"""
    err = HTTPError(
        "http://example.test/chat/completions",
        400,
        "Bad Request",
        {},
        io.BytesIO(b'{"error":{"message":"invalid max_tokens"}}'),
    )
    brief = _http_error_brief(err)
    assert "HTTP 400" in brief
    assert "invalid max_tokens" in brief

    # 第二次读取走缓存，不返回空串。
    assert "invalid max_tokens" in _http_error_brief(err)
