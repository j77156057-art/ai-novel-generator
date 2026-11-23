"""归属校正的重试口径：400 不重试，空输出才重试。

背景（2026-11-23）：MiniMax 返回 `HTTP 400: thinking.type="disabled" ... not
allowed` 时，`correct_attribution` 按「分块 × 3 次尝试」白跑 9 次、耗时 22 秒。
4xx 是参数/配置错，重试同一条必然同样失败，应该第一下就停手。
"""
from __future__ import annotations

from app.style import attribution_fix


class _RaisingProvider:
    def __init__(self, message: str) -> None:
        self.message = message
        self.calls = 0

    def chat(self, system: str, user: str, **_kwargs) -> str:
        self.calls += 1
        raise RuntimeError(self.message)


_TEXT = "他推开门，屋里没有人。\n" * 50  # > MIN_CHARS(200)，切成单块
_EXAMPLES = ["许照道：“又停电了。”"]


def test_non_retryable_classification():
    assert attribution_fix._non_retryable(
        RuntimeError("MiniMax chat 调用失败: HTTP Error 400: Bad Request"))
    assert attribution_fix._non_retryable(RuntimeError("HTTP 404: not found"))
    assert not attribution_fix._non_retryable(RuntimeError("HTTP 429: too many"))
    assert not attribution_fix._non_retryable(TimeoutError("read timed out"))


def test_attribution_fails_fast_on_http_400():
    provider = _RaisingProvider(
        "MiniMax chat 调用失败: HTTP Error 400: Bad Request"
    )
    out, info = attribution_fix.correct_attribution(
        _TEXT, provider,
        target_rate=0.3, convention="front_dao", examples=_EXAMPLES,
    )
    assert out == _TEXT
    assert info["applied"] is False
    assert "400" in info["reason"]
    assert provider.calls == 1, f"4xx 不应重试，实际调用 {provider.calls} 次"


def test_attribution_still_retries_transient_errors(monkeypatch):
    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, system: str, user: str, **_kwargs) -> str:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("read timed out")
            return "许照道：“又停电了。”" * 5

    monkeypatch.setattr(attribution_fix.time, "sleep", lambda *_: None)
    provider = _Flaky()
    _out, info = attribution_fix.correct_attribution(
        _TEXT, provider,
        target_rate=0.3, convention="front_dao", examples=_EXAMPLES,
    )
    assert provider.calls >= 2
    assert "调用失败" not in str(info.get("reason") or "")
