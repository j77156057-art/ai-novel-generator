# -*- coding: utf-8 -*-
"""锁住两条新渠道的接入：Command Code（env key）与本地 Ollama（无 key）。

这两条一旦接错，表现都是「设置页显示已生效、实际落回本地模板」——
正是最难排查的静默失效，所以用测试把它们钉住。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.llm.provider import (  # noqa: E402
    CommandCodeChatProvider,
    GenericOpenAIChatProvider,
    build_provider,
    build_role_provider,
)
from app.routers.settings import PROVIDER_META  # noqa: E402


def test_command_code_uses_env_key(monkeypatch):
    monkeypatch.setenv("COMMAND_CODE_GOAT_KEY", "cc-test-key")
    p = build_provider("command-code", "", "deepseek/deepseek-v4.1-flash")
    assert isinstance(p, CommandCodeChatProvider)
    assert p.api_key == "cc-test-key"
    assert p.base_url == "https://api.commandcode.ai/provider/v1"
    assert p.model == "deepseek/deepseek-v4.1-flash"


def test_command_code_without_any_key_raises(monkeypatch):
    monkeypatch.delenv("COMMAND_CODE_GOAT_KEY", raising=False)
    try:
        build_provider("command-code", "", "deepseek/deepseek-v4.1-flash")
    except ValueError as exc:
        assert "COMMAND_CODE_GOAT_KEY" in str(exc)
    else:  # pragma: no cover - 没报错就是回归
        raise AssertionError("没有任何 key 时 Command Code 应明确报错")


def test_ollama_works_without_key():
    p = build_provider("ollama", "", "qwen2.5:7b")
    assert isinstance(p, GenericOpenAIChatProvider)
    assert p.base_url == "http://127.0.0.1:11434/v1"
    # 父类要求非空 key；本地渠道用占位串，Ollama 不校验 Authorization。
    assert p.api_key == "ollama"


def test_role_provider_does_not_fallback_for_new_channels(monkeypatch):
    monkeypatch.setattr(settings, "assistant_provider", "ollama")
    monkeypatch.setattr(settings, "assistant_api_key", "")
    monkeypatch.setattr(settings, "assistant_base_url", "")
    monkeypatch.setattr(settings, "assistant_model", "qwen2.5:7b")
    p = build_role_provider("assistant", stream_timeout=30)
    assert isinstance(p, GenericOpenAIChatProvider)

    monkeypatch.setenv("COMMAND_CODE_GOAT_KEY", "cc-role-key")
    monkeypatch.setattr(settings, "assistant_provider", "command-code")
    monkeypatch.setattr(settings, "assistant_model", "deepseek/deepseek-v4.1-flash")
    q = build_role_provider("assistant", stream_timeout=30)
    assert isinstance(q, CommandCodeChatProvider)


def test_meta_and_settings_defaults_are_consistent():
    """设置页下拉首项必须与运行时默认模型一致（否则选了就换模型）。"""
    for provider, model_field, models_field in (
        ("command-code", "command_code_model", "command_code_models"),
        ("ollama", "local_llm_model", "local_llm_models"),
    ):
        meta = PROVIDER_META[provider]
        default_model = str(getattr(settings, model_field))
        chain = [m.strip() for m in str(getattr(settings, models_field)).split(",") if m.strip()]
        assert meta["default_models"][0] == default_model
        assert meta["default_models"] == chain


def test_list_local_models_parses_ollama_tags(monkeypatch):
    """`/v1` 会自动还原成根地址再读 `/api/tags`，模型名直接变成候选。"""
    import json

    from app.routers import settings as settings_router

    seen: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "models": [{"name": "qwen3.6:35b-a3b"}, {"name": "qwen2.5:7b"}]
            }).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr(settings_router.urllib.request, "urlopen", fake_urlopen)
    out = settings_router.list_local_models("http://127.0.0.1:11434/v1")
    assert out["ok"] is True
    assert out["models"] == ["qwen3.6:35b-a3b", "qwen2.5:7b"]
    assert seen == ["http://127.0.0.1:11434/api/tags"]


def test_list_local_models_returns_readable_error(monkeypatch):
    from app.routers import settings as settings_router

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(settings_router.urllib.request, "urlopen", boom)
    out = settings_router.list_local_models("http://127.0.0.1:11434/v1")
    assert out["ok"] is False
    assert out["models"] == []
    assert "connection refused" in out["error"]


def test_command_code_stream_skips_empty_choices_frame(monkeypatch):
    """Command Code 会在正文后补一帧 `{"choices":[],"usage":...}`。

    旧实现直接取 choices[0] → IndexError，把已经流式收到的正文判成失败；
    这里锁住「空 choices 只当用量帧、不打断正文」。
    """
    import app.llm.provider as prov

    body = (
        b'data: {"choices":[{"delta":{"content":"\\u6b63\\u5e38"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    class FakeResponse:
        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read1(self, n):
            if self.sent:
                return b""
            self.sent = True
            return body

    monkeypatch.setattr(
        prov.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )
    p = prov.CommandCodeChatProvider(
        "k", "https://example.com/v1", "deepseek/deepseek-v4.1-flash", max_tokens=64
    )
    text = "".join(p.stream_chat("只回复正文", "请只回复：正常"))
    assert text == "正常"
    assert p.last_usage is not None
    assert p.last_usage.get("prompt_tokens") == 3
    assert p.last_usage.get("completion_tokens") == 2


def test_stream_events_expose_reasoning_without_polluting_content(monkeypatch):
    """reasoning_content 要作为事件上报，供界面显示「模型在想」，但不能进正文。"""
    import app.llm.provider as prov

    body = (
        b'data: {"choices":[{"delta":{"reasoning_content":"\\u60f3"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"\\u6b63\\u5e38"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )

    class FakeResponse:
        def __init__(self):
            self.sent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read1(self, n):
            if self.sent:
                return b""
            self.sent = True
            return body

    monkeypatch.setattr(
        prov.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )
    p = prov.CommandCodeChatProvider(
        "k", "https://example.com/v1", "deepseek/deepseek-v4.1-flash", max_tokens=64
    )
    events = list(p.stream_chat_events("s", "u"))
    assert ("reasoning", "想") in events
    assert ("content", "正常") in events

    # 兼容入口只吐正文：既有调用方（非流式汇总/测试桩）不受影响。
    p2 = prov.CommandCodeChatProvider(
        "k", "https://example.com/v1", "deepseek/deepseek-v4.1-flash", max_tokens=64
    )
    monkeypatch.setattr(
        prov.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )
    assert "".join(p2.stream_chat("s", "u")) == "正常"
