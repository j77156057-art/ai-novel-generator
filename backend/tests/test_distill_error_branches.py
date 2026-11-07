"""蒸馏失败归因的单元测试。

覆盖：
- 异常 → 桶：401/403、429、404、超时/连接失败、未知异常
- 块级归因：异常优先；离线兜底模板 → no_key；半截 JSON → truncated；散文 → protocol
- 报错文本：按原因分组、每类带下一步、不回显 api_key

跑法：python tests/test_distill_error_branches.py  或 pytest tests/test_distill_error_branches.py
"""
from __future__ import annotations

import sys
import urllib.error
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.llm.provider import LocalFallbackLLMProvider
from app.style.narrative import (
    _classify_chunk_failure,
    _classify_provider_error,
    _format_distill_failure,
)


def _runtime(msg: str) -> RuntimeError:
    return RuntimeError(f"GLM chat 调用失败: {msg}")


def test_provider_error_buckets():
    cases = [
        (_runtime("HTTP Error 401: Unauthorized"), "auth"),
        (_runtime("HTTP Error 403: Forbidden"), "auth"),
        (_runtime("Incorrect API key provided"), "auth"),
        (_runtime("HTTP Error 429: Too Many Requests"), "rate_limit"),
        (_runtime("rate limit exceeded, quota exhausted"), "rate_limit"),
        (_runtime("HTTP Error 404: model glm-xxx does not exist"), "model_not_found"),
        (urllib.error.URLError("connection refused"), "network"),
        (_runtime("<urlopen error [Errno 110] Connection timed out>"), "network"),
        (_runtime("getaddrinfo failed"), "network"),
        (_runtime("something exploded"), "unknown"),
    ]
    for exc, want in cases:
        got = _classify_provider_error(exc)
        assert got == want, f"{exc!r}: got={got} want={want}"


def test_chunk_failure_prefers_specific_exception():
    raws = [_runtime("connection refused"), _runtime("HTTP Error 401: Unauthorized")]
    assert _classify_chunk_failure(raws) == "auth"


def test_chunk_failure_offline_fallback_is_no_key():
    template = "【离线兜底·确定性续写】已锁定角色：（暂无锁定角色）。"
    assert _classify_chunk_failure([template]) == "no_key"
    # 文本里没有标记、但 provider 本身就是本地兜底实例，同样判 no_key
    assert _classify_chunk_failure(["任意文本"], LocalFallbackLLMProvider()) == "no_key"


def test_chunk_failure_truncated_vs_protocol():
    truncated = '{"人物描写手法": ["通过动作带出性格（证据：他攥紧拳头'
    assert _classify_chunk_failure([truncated], object()) == "truncated"

    prose = "这部小说的人物描写偏向白描，作者很少直接评价。"
    assert _classify_chunk_failure([prose], object()) == "protocol"

    empty = ""
    assert _classify_chunk_failure([empty], object()) == "protocol"


def test_format_groups_reasons_and_has_next_steps():
    msg = _format_distill_failure(
        4,
        {"no_key": 2, "truncated": 1, "network": 1},
        LocalFallbackLLMProvider(),
    )
    assert "4 个语料块全部未产出任何规则" in msg
    assert "没有可用的模型 API key" in msg and "2 块" in msg
    assert "被截断" in msg
    assert "网络不可达" in msg
    # 每类都带可执行的下一步
    assert msg.count("下一步：") == 3
    # 未出现的桶不渲染
    assert "限流" not in msg


def test_format_never_leaks_api_key():
    class FakeProvider:
        label = "GLM"
        model = "glm-4-flash"
        base_url = "https://example.invalid/v1"
        api_key = "DO-NOT-LEAK-THIS-VALUE"

    msg = _format_distill_failure(1, {"auth": 1}, FakeProvider())
    assert "DO-NOT-LEAK-THIS-VALUE" not in msg
    assert "glm-4-flash" in msg


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
