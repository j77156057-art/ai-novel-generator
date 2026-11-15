"""OpenAICompatibleChatProvider 超时配置回归（QA 报告 3.1 节）。

当前契约（2026-11-14 裁决，分三层，别再按「一个数字管所有」理解）：
  - `timeout`（默认 settings.writer_timeout=180，由 120 上调）：**单次响应内**
    的读/速率窗口基准，不再是 chat 的 socket 超时；
  - `total_timeout`（默认 settings.writer_total_timeout=960）：非流式补全的
    **总硬顶**，跨换模型/重试共享；urlopen 拿到的是剩余预算；
  - `stream_timeout`（默认 settings.writer_stream_timeout=180）：流式 urlopen
    超时，长文首字到达前的连接窗口必须留够。

2026-11-14 同时把 chat 由 120 上调到 180。断言的**口径**因此从「urlopen 收到
硬编码 180」改为「跟随 settings + 默认档位 180/180 + 总硬顶 + 显式覆盖逐调用
校验」——不是放宽：socket 超时字段的归属本身在那次裁决里变了。Assistant 由
`build_assistant_provider` 显式传 timeout/stream_timeout=60 与 total_timeout=120。
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
from app.config import settings  # noqa: E402
from app.llm.provider import ZhipuChatProvider  # noqa: E402


# ---------------- 假 urllib 响应体 ----------------

class _FakeChatResp:
    """模拟 chat 路径的 urlopen 返回：支持 with 上下文 + 分块 read。

    `provider._read_body` 先取 `getattr(resp, "read1", None) or resp.read`，
    再以 `reader(65536)` 分块读，所以假响应必须实现**带 size 的** read。
    """

    def __init__(self, body: bytes) -> None:
        self._body = body
        self._pos = 0

    def __enter__(self) -> "_FakeChatResp":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def read(self, n: int = -1) -> bytes:
        chunk = (
            self._body[self._pos:] if n is None or n < 0
            else self._body[self._pos:self._pos + n]
        )
        self._pos += len(chunk)
        return chunk

    def read1(self, n: int = -1) -> bytes:
        return self.read(n)


class _FakeStreamResp(_FakeChatResp):
    """模拟 stream_chat 路径的 urlopen 返回：分块 read + 行迭代。

    `provider._iter_stream_lines` 也走 `read1(65536)`（不是 `for line in resp`），
    所以与 chat 假响应同源；`__iter__` 保留给「按行读」的老路径做兼容。
    """

    def __enter__(self) -> "_FakeStreamResp":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def __iter__(self):
        yield from self._body.splitlines(keepends=True)


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
    """Writer 默认超时：chat 与 stream 同档 180s，且都从 settings 读（不钉在签名里）。

    新契约（2026-11-14 裁决，见 `provider.OpenAICompatibleChatProvider.__init__`
    与 `config.writer_timeout` 注释）：
      - chat 默认 = `settings.writer_timeout`（默认 180，由 120 上调：4000 字级
        单章实测一次生成需 180s 上下，120s 的同步入口必然读超时、零产出）；
      - stream 默认 = `settings.writer_stream_timeout`（默认 180，长文流式续写
        不可被 120s 截断）；
      - 两者同档，但**必须各自取值**——不许收敛成同一个硬编码常量；
      - Assistant 角色由 `build_assistant_provider` **显式**传 60，不靠默认值。

    实现：monkeypatch 掉 urllib.request.urlopen，捕获每次调用的 timeout 关键字。
    """
    captured: list[int | None] = []
    monkeypatch.setattr(urllib.request, "urlopen", _make_fake_urlopen(captured))

    # 用一个真正的 OpenAI 兼容 provider（继承 OpenAICompatibleChatProvider）
    w = ZhipuChatProvider("fake-key", "https://fake.api", "fake-model")

    # 默认值 = settings 的配置值，且配置默认仍是 180/180
    assert w.timeout == settings.writer_timeout, (
        f"chat timeout 默认应跟随 settings.writer_timeout，"
        f"实际 {w.timeout} vs {settings.writer_timeout}"
    )
    assert w.stream_timeout == settings.writer_stream_timeout, (
        f"stream_chat timeout 默认应跟随 settings.writer_stream_timeout，"
        f"实际 {w.stream_timeout} vs {settings.writer_stream_timeout}"
    )
    assert (settings.writer_timeout, settings.writer_stream_timeout) == (180, 180), (
        "Writer 默认档位必须是 180/180（2026-11-14 由 120 上调，"
        "120s 在 4000 字级单章下必然读超时）"
    )

    # 触发 chat：socket 超时是「总硬顶的剩余预算」（≈writer_total_timeout），
    # 单次 180 已不再直接传给 urlopen——首字节前的长等待由总硬顶兜（见
    # provider.complete_message 的 deadline 与 config.writer_total_timeout 注释）。
    captured.clear()
    w.chat("sys", "user")
    assert len(captured) == 1, f"chat 应只发一次请求，实际: {captured}"
    assert settings.writer_total_timeout - 10 <= captured[0] <= settings.writer_total_timeout, (
        f"chat 的 urlopen 超时应取总硬顶剩余预算（≈{settings.writer_total_timeout}s），"
        f"实际: {captured}"
    )

    # 触发 stream_chat：应看到一次 timeout=180 调用
    # （stream_chat 是 generator，必须消费完才能跑完内部 urlopen）
    captured.clear()
    pieces = list(w.stream_chat("sys", "user"))
    assert pieces == ["hello", " world"], f"fake stream 应吐出 ['hello', ' world']，实际 {pieces!r}"
    assert captured == [180], f"stream_chat 路径应传 180s timeout，实际: {captured}"

    # 显式覆盖优先（Assistant 走 60s：诊断必须快速失败；工厂同时把
    # total_timeout 压到 max(120, timeout*2)=120，见 build_assistant_provider）
    captured.clear()
    a = ZhipuChatProvider("fake-key", "https://fake.api", "fake-model",
                          timeout=60, stream_timeout=60, total_timeout=120)
    assert (a.timeout, a.stream_timeout, a.total_timeout) == (60, 60, 120)
    a.chat("sys", "user")
    assert len(captured) == 1 and 110 <= captured[0] <= 120, (
        f"显式 total_timeout 应约束 chat 的 socket 超时（≈120s），实际: {captured}"
    )
    captured.clear()
    list(a.stream_chat("sys", "user"))
    assert captured == [60], f"显式 stream_timeout 应透传到 urlopen，实际: {captured}"
