# -*- coding: utf-8 -*-
"""锁住 provider 的两条接入行为（2026-11-12）。

1. MiniMax M3.1 正式 id 必须走 adaptive 分支——不能再被塞 thinking={"type":"disabled"}，
   否则真机 400；Preview 别名同样要命中。
2. opencode go 的必带 header：浏览器 UA + x-opencode-session，缺一个 403/400。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm.provider import (  # noqa: E402
    MiniMaxChatProvider,
    OpenCodeGoChatProvider,
    OpenAICompatibleChatProvider,
)

FAILED = []
PASSED = []


def check(name, cond):
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(name)
        print(f"  FAIL: {name}")


def test_minimax_formal_id_is_adaptive():
    for model in ("MiniMax-M3.1-Flash", "MiniMax-M3.1-Flash-Preview"):
        p = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", model)
        check(f"{model} 不注入 thinking=disabled", "thinking" not in p.extra_body)
    # 老模型仍要关思维链，保持原行为
    p = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", "MiniMax-M3")
    check("MiniMax-M3 仍注入 thinking=disabled",
          p.extra_body.get("thinking") == {"type": "disabled"})


def test_opencode_go_required_headers():
    p = OpenCodeGoChatProvider("oc_key")
    check("默认 base_url", p.base_url == "https://opencode.ai/inference/go/openai/v1")
    check("默认 model", p.model == "deepseek-v4.1-flash")
    ua = p.extra_headers.get("User-Agent", "")
    check("带浏览器 UA", ua.startswith("Mozilla/5.0") and "Python-urllib" not in ua)
    sid = p.extra_headers.get("x-opencode-session", "")
    check("带 x-opencode-session", sid.startswith("novel-"))

    p2 = OpenCodeGoChatProvider("oc_key")
    check("session 每个实例不同", p2.extra_headers["x-opencode-session"] != sid)

    p3 = OpenCodeGoChatProvider("oc_key", session_id="fixed-session")
    check("可显式指定 session", p3.extra_headers["x-opencode-session"] == "fixed-session")


def test_base_provider_headers_unchanged():
    p = OpenAICompatibleChatProvider("k", "https://example.com/v1", "m")
    check("基础类默认无额外 header", p.extra_headers == {})


def test_minimax_429_backoff_retry():
    """MiniMax 429 必须同模型退避重试（2/5/12s），不能直接判失败。

    2026-11-12 04:56 五小时额度墙：旧逻辑 429 → 切换候选模型 → 链上只有一个
    → 立即抛错，12 批角色声音 + 漂移全灭。这里用假 urlopen 锁住「同一请求重试」。
    """
    import io
    import json
    import urllib.error

    import app.llm.provider as prov

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self, n: int = -1) -> bytes:
            # 模拟 http.client.HTTPResponse.read(n)：按字节消费，读完返回 b""
            if n is None or n < 0:
                out, self._body = self._body, b""
                return out
            out, self._body = self._body[:n], self._body[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    real_urlopen = prov.urllib.request.urlopen
    real_sleep = prov.time.sleep
    try:
        # 连续两次 429，第三次成功
        state = {"calls": 0}
        sleeps: list[float] = []

        def fake_urlopen(req, timeout=None):
            state["calls"] += 1
            if state["calls"] <= 2:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}")
                )
            body = json.dumps({
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            return _Resp(body)

        prov.urllib.request.urlopen = fake_urlopen
        prov.time.sleep = lambda s: sleeps.append(s)
        p = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash")
        out = p.chat("s", "u")
        check("429 两次后同一条成功", out == "ok" and state["calls"] == 3)
        check("退避序列 2/5", sleeps == [2.0, 5.0])

        # 三次 429 全用光：共 4 次请求、退避 2/5/12，最后抛错（不再切换模型）
        state2 = {"calls": 0}
        sleeps2: list[float] = []

        def always_429(req, timeout=None):
            state2["calls"] += 1
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b"{}")
            )

        prov.urllib.request.urlopen = always_429
        prov.time.sleep = lambda s: sleeps2.append(s)
        p2 = MiniMaxChatProvider("k", "https://api.minimaxi.com/v1", "MiniMax-M3.1-Flash")
        raised = False
        try:
            p2.chat("s", "u")
        except RuntimeError:
            raised = True
        check("重试耗尽后抛错", raised and state2["calls"] == 4)
        check("退避序列 2/5/12", sleeps2 == [2.0, 5.0, 12.0])
    finally:
        prov.urllib.request.urlopen = real_urlopen
        prov.time.sleep = real_sleep


def main():
    test_minimax_formal_id_is_adaptive()
    test_opencode_go_required_headers()
    test_base_provider_headers_unchanged()
    test_minimax_429_backoff_retry()
    print(f"\n结果：{len(PASSED)}/{len(PASSED) + len(FAILED)} 通过")
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
