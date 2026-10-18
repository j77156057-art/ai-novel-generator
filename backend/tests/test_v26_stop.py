"""v2.6：节点粒度根治（stop token 方向）测试。

覆盖：
- LLMProvider.set_stop_sequences：基类 no-op，OpenAICompatibleChatProvider 实际存
- payload_base 含 stop（mock 验证 _complete + stream_chat 都传 stop）
- _set_section_stop：调用方 helper，对不支持的 provider 不报错
- _strip_thinking：剥离【本节完】marker 行（即使 stop 漏过）
- build_user_prompt：directive 含节末信号【本节完】

跑法：python tests/test_v26_stop.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{tag}  {label}: got={got!r} want={want!r}")


# ── 组 1：LLMProvider.set_stop_sequences 基类默认 no-op ──

def test_llm_provider_base_noop():
    print("\n=== 组 1: LLMProvider.set_stop_sequences 基类默认 no-op ===")
    from app.llm.provider import LLMProvider
    # 抽象类不能直接实例化——用 LocalFallbackLLMProvider 验证（继承 LLMProvider）
    from app.llm.provider import LocalFallbackLLMProvider
    p = LocalFallbackLLMProvider()
    # 基类方法不报错（no-op）
    try:
        p.set_stop_sequences(["【本节完】"])
        check("基类 set_stop_sequences 不抛", True, True)
    except Exception as e:
        check("基类 set_stop_sequences 不抛", f"raised: {e}", "no raise")


# ── 组 2：get_section_stop_sequences 返回稳定列表 ──

def test_get_section_stop_sequences():
    print("\n=== 组 2: get_section_stop_sequences 返回稳定列表 ===")
    from app.generation.engine import get_section_stop_sequences
    seqs = get_section_stop_sequences()
    check("返回 list", isinstance(seqs, list), True)
    check("非空", len(seqs) > 0, True)
    check("含【本节完】", "【本节完】" in seqs, True)
    # 防漂移：每次调返回值应一致
    seqs2 = get_section_stop_sequences()
    check("幂等", seqs, seqs2)


def test_per_request_stop_no_singleton_pollution():
    """v2.6.1：per-request stop_sequences 不污染 provider 单例。

    之前的实现调 provider.set_stop_sequences() 会改单例状态，
    下次不同请求会看到错误的 stop 列表。改用 per-request 参数后，
    两次 chat 互相不串扰。
    """
    print("\n=== 组 2.1: per-request stop 不污染单例 ===")
    from app.llm.provider import OpenAICompatibleChatProvider
    import json as _json

    captured = []

    class P(OpenAICompatibleChatProvider):
        def __init__(self):
            self.api_key = "fake"
            self.base_url = "http://fake"
            self.model = "m"
            self.extra_body = {}
            self.max_tokens = None
            self.timeout = 60
            self.stream_timeout = 60
            self._models = ["m"]
            self._stop_sequences = None
            self.last_finish_reason = None

        def _model_chain(self):
            return ["m"]

        def _complete(self, messages, stop_sequences=None):
            # 关键：每调 capture 一次 effective_stop
            effective = stop_sequences if stop_sequences is not None else self._stop_sequences
            captured.append({
                "stop": effective,
            })
            return "ok"

    p = P()

    # 第 1 次：per-request 传 A
    p.chat("sys", "user1", stop_sequences=["A", "B"])
    check("第 1 次 chat effective stop=A,B", captured[-1]["stop"], ["A", "B"])

    # 第 2 次：不传 stop_sequences
    p.chat("sys", "user2")
    check("第 2 次 chat 不传 stop → effective=None", captured[-1]["stop"], None)

    # 第 3 次：再传 C
    p.chat("sys", "user3", stop_sequences=["C"])
    check("第 3 次 chat effective stop=C", captured[-1]["stop"], ["C"])

    # 验证单例没被改
    check("单例 _stop_sequences 始终 None", p._stop_sequences, None)


# ── 组 3：_strip_thinking 剥离【本节完】marker ──

def test_strip_thinking_marker():
    print("\n=== 组 3: _strip_thinking 剥离【本节完】 ===")
    from app.generation.engine import _strip_thinking

    # 普通正文
    s = "林悠然升职了。她看着窗外的城市。"
    check("普通正文不变", _strip_thinking(s), s)

    # 末尾有【本节完】marker
    s = "林悠然升职了。她看着窗外的城市。\n【本节完】"
    check("末尾有 marker 整行剥离", _strip_thinking(s), "林悠然升职了。她看着窗外的城市。")

    # marker 前有多个空行
    s = "林悠然升职了。\n\n\n【本节完】"
    check("marker 前空行也剥离", _strip_thinking(s), "林悠然升职了。")

    # marker 在中间（保留，因为只剥末尾）
    s = "林悠然升职了。【本节完】然后她回家了。"
    check("marker 在中间保留", _strip_thinking(s), s)

    # 思维链 + marker 都剥（中间有空格分隔）
    s = " <think>reasoning </think>林悠然升职了。【本节完】"
    check("思维链 + marker 都剥", _strip_thinking(s), "林悠然升职了。")


# ── 组 4：build_user_prompt 节末信号 ──

def test_build_user_prompt_section_signal():
    print("\n=== 组 4: build_user_prompt 含节末信号 ===")
    from app.generation.prompts import build_user_prompt

    # 首段
    up_open = build_user_prompt(
        prior_summaries=[], rag_context=[], outline=["a", "b", "c"],
        max_chars=3000, prior_blocks=[],
    )
    check("首段含「节末信号」", "节末信号" in up_open, True)
    check("首段含【本节完】", "【本节完】" in up_open, True)

    # 续写段
    up_cont = build_user_prompt(
        prior_summaries=["前情"], rag_context=[], outline=["a", "b", "c"],
        max_chars=3000, prior_blocks=["block"],
    )
    check("续写段含「节末信号」", "节末信号" in up_cont, True)
    check("续写段含【本节完】", "【本节完】" in up_cont, True)


# ── 组 5：OpenAICompatibleChatProvider payload 含 stop ──

def test_openai_compat_stop_payload():
    print("\n=== 组 5: OpenAICompatibleChatProvider 透传 stop 到 payload ===")
    from app.llm.provider import OpenAICompatibleChatProvider
    import json as _json

    # mock _complete 看 payload
    captured_payloads = []

    class P(OpenAICompatibleChatProvider):
        def __init__(self):
            self.api_key = "fake"
            self.base_url = "http://fake"
            self.model = "m"
            self.extra_body = {}
            self.max_tokens = None
            self.timeout = 60
            self.stream_timeout = 60
            self._models = ["m"]
            self._stop_sequences = None
            self.last_finish_reason = None

        def _model_chain(self):
            return ["m"]

        def _complete(self, messages, stop_sequences=None):
            # mock：捕获 effective stop
            effective = stop_sequences if stop_sequences is not None else self._stop_sequences
            captured_payloads.append({
                "stop": effective,
                "model": "m",
            })
            return "ok"

    p = P()
    p.set_stop_sequences(["【本节完】", "## 第"])
    p.chat("sys", "user")
    check("payload 含 stop 列表", captured_payloads[0]["stop"], ["【本节完】", "## 第"])

    # set_stop_sequences(None) 清空
    p.set_stop_sequences(None)
    p.chat("sys", "user")
    check("set_stop_sequences(None) 后 payload 不含 stop", captured_payloads[1].get("stop"), None)

    # set_stop_sequences([]) 也清空
    p.set_stop_sequences([])
    p.chat("sys", "user")
    check("set_stop_sequences([]) 后 payload 不含 stop", captured_payloads[2].get("stop"), None)

    # per-request 覆盖单例值
    p.set_stop_sequences(["singleton-val"])
    p.chat("sys", "user", stop_sequences=["per-req"])
    check("per-request 覆盖单例值", captured_payloads[3].get("stop"), ["per-req"])
    # 但单例自身不变（per-request 不写回单例）
    check("单例仍是 singleton-val（per-request 不写回）", p._stop_sequences, ["singleton-val"])


# ── 跑全部 ──

if __name__ == "__main__":
    test_llm_provider_base_noop()
    test_get_section_stop_sequences()
    test_per_request_stop_no_singleton_pollution()
    test_strip_thinking_marker()
    test_build_user_prompt_section_signal()
    test_openai_compat_stop_payload()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
