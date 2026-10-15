"""v2.6 端到端 pipeline 自动化测试（正式化）。

v2.6 stop token 链路 + 诊断性 B 链路 + 节点粒度收紧 + 持久化的回归测试。
不依赖网络/智谱/OpenRouter——用 mock LLM provider。

跑法：python tests/test_e2e_v26_pipeline.py
"""
from __future__ import annotations

import os, sys, json, re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    ok = got == want
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{tag}  {label}: got={got!r} want={want!r}")


# ── 1. v2.6 stop token per-request 透传 ──

def test_stop_token_per_request_passthrough():
    sys.stdout.write(chr(10) + "=== 1. v2.6 stop token per-request 透传 ===" + chr(10))
    sys.stdout.flush()
    from app.llm.provider import OpenAICompatibleChatProvider

    captured = []

    class MockProvider(OpenAICompatibleChatProvider):
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
            effective = stop_sequences if stop_sequences is not None else self._stop_sequences
            captured.append({"stop": effective, "model": "m"})
            return "林悠然升职了。" + chr(10) + chr(10) + "【本节完】"

    p = MockProvider()
    from app.generation.engine import get_section_stop_sequences
    p.chat("sys", "user", stop_sequences=get_section_stop_sequences())

    expected = ["【本节完】", "## 第", chr(10) + "# ", chr(10) + "## "]
    check("stop_sequences 透传到 mock payload", captured[0]["stop"], expected)


# ── 2. _strip_thinking 剥离【本节完】marker ──

def test_strip_thinking_strips_marker():
    sys.stdout.write(chr(10) + "=== 2. _strip_thinking 剥【本节完】 ===" + chr(10))
    sys.stdout.flush()
    from app.generation.engine import _strip_thinking

    s = "林悠然升职了。她看着窗外的城市。" + chr(10) + "【本节完】"
    check("marker 整行剥离", _strip_thinking(s), "林悠然升职了。她看着窗外的城市。")

    s = "林悠然升职了。" + chr(10) + chr(10) + chr(10) + "【本节完】"
    check("marker 前多空行也剥离", _strip_thinking(s), "林悠然升职了。")


# ── 3. node_prompt 剔除全貌信息 ──

def test_node_prompt_excludes_spoilers():
    sys.stdout.write(chr(10) + "=== 3. node_prompt 剔除全貌 ===" + chr(10))
    sys.stdout.flush()
    from app.generation.prompts import build_user_prompt

    premise = "都市职场女性林悠然在家族企业改革中周旋，与大学教授顾辰逸相知相守"
    outline = [
        "开端：林悠然入职",
        "揭秘：辰逸父亲是公司董事长的秘密浮出水面",
        "结局：辰逸为悠然放弃家族期望",
    ]
    node_prompt = (
        "【当前剧情点】入职立足" + chr(10) +
        "起点状态：入职" + chr(10) +
        "本段写作目标：升至经理" + chr(10) +
        "本段情绪基调：爽" + chr(10) +
        "主线进度：0%" + chr(10) +
        "硬约束：1. 只写这一个剧情点 2. 不要引入未提及角色 3. 不要写结局"
    )

    user_p = build_user_prompt(
        prior_summaries=[], rag_context=[], outline=outline,
        max_chars=1500, plot_bible=None, premise=premise,
        prior_blocks=[], style_exemplars=None, rag_has_forbidden=False,
        node_prompt=node_prompt,
    )

    check("user_prompt 含【本节完】指令", "本节完" in user_p, True)
    check("user_prompt 不含 premise 剧透", "相知相守" in user_p, False)
    check("user_prompt 不含结局", "辰逸为悠然放弃" in user_p, False)
    check("user_prompt 不含揭秘", "父亲是公司" in user_p, False)


# ── 4. 诊断性 B 在 return dict / yield done ──

def test_diag_b_in_return():
    sys.stdout.write(chr(10) + "=== 4. 诊断性 B 在 return dict ===" + chr(10))
    sys.stdout.flush()
    engine_path = BACKEND / "app" / "generation" / "engine.py"
    content = engine_path.read_text(encoding="utf-8")
    n = content.count(chr(34) + "unlisted_characters" + chr(34))
    check("engine.py 至少 2 处含 unlisted_characters 字段（同步+流式）", n >= 2, True)


# ── 5. 重生成不调 _judge_and_advance ──

def test_regenerate_does_not_advance_state():
    """重生成不调 _judge_and_advance（重写不推进 state）。

    验证方式：regenerate_segment_stream 函数体不含 _judge_and_advance 调用。
    """
    sys.stdout.write(chr(10) + "=== 5. 重生成不推进 state ===" + chr(10))
    sys.stdout.flush()
    engine_path = BACKEND / "app" / "generation" / "engine.py"
    content = engine_path.read_text(encoding="utf-8")

    # 用字符串切（不靠正则匹配多行 def）：找 "def regenerate_segment_stream(" 起点
    sig = "def regenerate_segment_stream("
    idx = content.find(sig)
    if idx < 0:
        check("找到 regenerate_segment_stream 函数", False, True)
        return

    # 找下一个 def 函数定义（单行 "def xxx("）
    # 简单：用 "def " 紧跟行首（用 \n + def 找下一个顶层 def）
    start = idx
    next_def_idx = content.find(chr(10) + chr(10) + "def ", start)
    # 兜底：找下一个 \ndef xxx(
    if next_def_idx < 0:
        next_def_idx = content.find(chr(10) + "def ", start)
    end = next_def_idx if next_def_idx > 0 else len(content)
    func_body = content[start:end]

    # 只检查真调用（"= _judge_and_advance("），不匹配"不调"注释
    has_call = "= _judge_and_advance(" in func_body
    check("重生成不调 _judge_and_advance", has_call, False)


if __name__ == "__main__":
    test_stop_token_per_request_passthrough()
    test_strip_thinking_strips_marker()
    test_node_prompt_excludes_spoilers()
    test_diag_b_in_return()
    test_regenerate_does_not_advance_state()
    print(chr(10) + f"结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
