"""LocalFallbackLLMProvider stop_sequences 签名对齐（防回归）。

v2.6.1 把 stop_sequences 改成 per-request chat 参数后，
LocalFallbackLLMProvider（engine 离线兜底 provider）漏同步签名——
任何「无 LLM key 跑 generate」场景都会 500。本测试守这条修复。

跑法：python tests/test_llm_fallback_signature.py
"""
from __future__ import annotations

import os, sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

def check(label, got, want):
    """断言版 check —— 失败即抛。

    （原来的脚本版只记数不抛：跑挂了也会显示通过。）
    """
    assert got == want, f"{label}: got={got!r} want={want!r}"


def test_fallback_accepts_stop_sequences():
    from app.llm.provider import LocalFallbackLLMProvider
    fb = LocalFallbackLLMProvider()
    # 不传 stop
    out_no = fb.chat("sys", "user", constraints={"characters": ["林悠然"]})
    check("无 stop 返回非空字符串", isinstance(out_no, str) and len(out_no) > 0, True)
    # 传 stop（关键回归点）
    out_with = fb.chat("sys", "user", constraints=None,
                         stop_sequences=["【本节完】", "## 第"])
    check("带 stop_sequences 不抛异常", isinstance(out_with, str) and len(out_with) > 0, True)
    # 传 None stop + 同样 constraints 与无 stop 对比（确认 stop 参数不影响输出）
    out_a = fb.chat("sys", "user", constraints={"characters": ["林悠然"]}, stop_sequences=None)
    out_b = fb.chat("sys", "user", constraints={"characters": ["林悠然"]})  # 无 stop
    check("stop=None 与无 stop 输出一致", out_a, out_b)
    # 传空 list 也正常
    out_empty = fb.chat("sys", "user", constraints=None, stop_sequences=[])
    check("stop=[] 不抛", isinstance(out_empty, str), True)


def test_fallback_engine_chat_no_500():
    from app.llm.provider import LocalFallbackLLMProvider
    from app.generation.engine import get_section_stop_sequences
    # 模拟 engine 真实调用形态（per-request stop_sequences）
    fb = LocalFallbackLLMProvider()
    text = fb.chat("sys", "user",
                   constraints={"characters": ["林悠然"], "plotholes": []},
                   stop_sequences=get_section_stop_sequences())
    check("engine 形态调用成功", isinstance(text, str) and len(text) > 0, True)

