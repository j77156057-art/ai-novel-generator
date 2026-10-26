"""OpenAICompatibleChatProvider 超时配置回归（QA 报告 3.1 节）。

之前在 ADR-2026-10-12 双轨重构里把 stream_chat 的硬编码 180s 与 chat 的硬编码 120s
都收敛到 `self.timeout` 默认 120——结果把 Writer 流式续写的连接窗口从 180s
悄悄截断到了 120s。修复后用独立 `self.stream_timeout` 默认 180 还原 b58c763 行为。

本文件 1 个 unit test 锁住「chat=120 / stream=180」这个不变量；以后再有人想
把它俩统一掉，测试会立刻报警。
"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request

# ─── bootstrap ──────────────────────────────────────────────────────────
# 与 test_provider_registry.py 同源：把 backend/ 加进 sys.path。
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

import app.llm.provider as llm_provider_mod  # noqa: E402
from app.llm.provider import ZhipuChatProvider  # noqa: E402


# ---------------- 假 urllib 响应体 ----------------

class _FakeChatResp:
    """模拟 chat 路径的 urlopen 返回：支持 with 上下文 + .read()。"""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeChatResp":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeStreamResp:
    """模拟 stream_chat 路径的 urlopen 返回：支持 with 上下文 + 迭代。"""

    def __init__(self, payload: bytes) -> None:
        # 保留行尾换行；urlopen 真实返回时也是「每次一行」语义
        self._lines = payload.splitlines(keepends=True)

    def __enter__(self) -> "_FakeStreamResp":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def __iter__(self):
        return iter(self._lines)


def _make_fake_urlopen(captured: list[int | None]):
    """返回 fake urlopen：识别 payload 里的 \"stream\": true 来路由 chat / stream 响应，
    并把所有调用收到的 timeout 关键字参数记到 captured 里。"""

    def _fake_urlopen(req, timeout=None, **kwargs):
        captured.append(timeout)
        body = req.data or b""
        if b'"stream": true' in body:
            # 两条 SSE delta + [DONE]，覆盖一般流式场景
            payload = (
                b'data: {"choices": [{"delta": {"content": "hello"}}]}\n\n'
                b'data: {"choices": [{"delta": {"content": " world"}}]}\n\n'
                b'data: [DONE]\n\n'
            )
            return _FakeStreamResp(payload)
        body_json = json.dumps(
            {"choices": [{"message": {"content": "fake-reply"}}]}
        ).encode("utf-8")
        return _FakeChatResp(body_json)

    return _fake_urlopen


# ---------------- 唯一测试 ----------------

def test_writer_default_chat_120s_and_stream_180s(monkeypatch):
    """Writer 默认：chat 走 120s，stream_chat 走 180s（b58c763 字节一致）。

    防回归：
      - 不许把 chat 与 stream_chat 都收敛到 self.timeout 默认 120（QA 报告 3.1）；
      - 不许把 stream_timeout 默认改成 120（会截断长文流式续写）。

    实现：monkeypatch 掉 urllib.request.urlopen，捕获每次调用的 timeout 关键字。
    """
    captured: list[int | None] = []
    monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(captured))

    # 用一个真正的 OpenAI 兼容 provider（继承 OpenAICompatibleChatProvider）
    w = ZhipuChatProvider("fake-key", "https://fake.api", "fake-model")

    # 默认值必须是 b58c763 硬编码值——这是不变量
    assert w.timeout == 120, f"chat timeout 默认应为 120s，实际 {w.timeout}"
    assert w.stream_timeout == 180, (
        f"stream_chat timeout 默认应为 180s，实际 {w.stream_timeout}；"
        "QA 报告 3.1 节已记录本回归——长文流式续写不可被 120s 截断"
    )

    # 触发 chat：应看到一次 timeout=120 调用
    captured.clear()
    w.chat("sys", "user")
    assert captured == [120], f"chat 路径应传 120s timeout，实际: {captured}"

    # 触发 stream_chat：应看到一次 timeout=180 调用
    # （stream_chat 是 generator，必须消费完才能跑完内部 urlopen）
    captured.clear()
    pieces = list(w.stream_chat("sys", "user"))
    assert pieces == ["hello", " world"], f"fake stream 应吐出 ['hello', ' world']，实际 {pieces!r}"
    assert captured == [180], f"stream_chat 路径应传 180s timeout，实际: {captured}"
