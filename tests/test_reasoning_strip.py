"""推理块剥离的回归测试。

真实事故（2026-10-14）：模型在生成里吐了 `<think>…</think>`，**826/1662 字
（49%）是思考过程**，被原样存进了小说正文。三臂对照 12 段零复现 → 间歇性，
故这里不依赖"能不能复现"，直接用**事故样本**做输入。

重点覆盖流式：开标签会被 token 边界切断（`"<thi"` + `"nk>"`），逐块简单
替换会把标签前半截漏给用户——那是最容易写错的地方。
"""
from __future__ import annotations

from app.llm.reasoning import ReasoningFilter, strip_reasoning

# 事故样本：段3 的真实开头（截取）
REAL = (
    "<think>\n"
    "好的，我现在需要处理用户提供的这个改写任务。首先，我得仔细阅读用户的要求，"
    "确保完全理解每一个细节。用户希望将一段AI生成的文本改写成更接近真人写的网文…"
    "接下来，我需要通读用户提供的原文，找出所有需要修改的地方。"
    "</think>\n"
    "小杨的指甲在生锈的铁皮上划出刺耳声响。笔记本被风掀起一角。"
)


class TestStripReasoning:
    def test_real_incident_sample_is_cleaned(self):
        out = strip_reasoning(REAL)
        assert "<think>" not in out and "</think>" not in out
        assert "改写任务" not in out and "通读用户提供的原文" not in out
        assert out.startswith("小杨的指甲")
        # 原本 49% 的推理内容必须整段消失
        assert len(out) < len(REAL) * 0.6

    def test_no_reasoning_block_is_untouched(self):
        plain = "他站起来，推开门走了出去。"
        assert strip_reasoning(plain) == plain

    def test_multiple_blocks_all_removed(self):
        out = strip_reasoning("A<think>思考1</think>B<think>思考2</think>C")
        assert out == "AB C".replace(" ", "") or out == "ABC"

    def test_unclosed_block_truncated_at_open_tag(self):
        """流被截断、闭标签没来：从 <think> 到文末都是推理内容。"""
        out = strip_reasoning("正文开头<think>思考到一半被截断了")
        assert "思考" not in out
        assert out == "正文开头"

    def test_empty_and_none_like_inputs(self):
        assert strip_reasoning("") == ""
        assert strip_reasoning(None) is None  # type: ignore[arg-type]


class TestReasoningFilterStreaming:
    @staticmethod
    def _run(pieces: list[str]) -> tuple[str, bool]:
        f = ReasoningFilter()
        got = "".join(f.feed(p) for p in pieces)
        got += f.flush()
        return got, f.saw_reasoning

    def test_token_split_open_tag(self):
        """开标签被切成 "<thi" + "nk>"：不能把前半截漏出去。"""
        out, saw = self._run(["正文", "<thi", "nk>", "思考内容", "</think>", "正文续"])
        assert out == "正文正文续"
        assert saw is True

    def test_token_split_close_tag(self):
        out, saw = self._run(["A", "<think>", "思考", "</thi", "nk>", "B"])
        assert out == "AB"
        assert saw is True

    def test_no_reasoning_has_zero_latency_penalty(self):
        """无推理块时应当**逐块原样吐出**（不破坏「首字即显」）。"""
        f = ReasoningFilter()
        assert f.feed("小杨的指甲") == "小杨的指甲"
        assert f.feed("在生锈的铁皮上") == "在生锈的铁皮上"
        assert f.flush() == ""

    def test_trailing_partial_open_tag_held_back_then_emitted(self):
        """结尾是 '<' 这种半个开标签：先持有，流结束后原样吐出。"""
        f = ReasoningFilter()
        assert f.feed("正文<") == "正文"
        assert f.flush() == "<"

    def test_unclosed_reasoning_discarded(self):
        out, saw = self._run(["正文", "<think>", "思考了一半"])
        assert out == "正文"
        assert saw is True

    def test_char_by_char_split(self):
        """极端情况：逐字符到达。"""
        out, saw = self._run(list(REAL))
        assert out.strip() == "小杨的指甲在生锈的铁皮上划出刺耳声响。笔记本被风掀起一角。"
        assert saw is True
        assert "<think>" not in out

class TestNewlineAfterThinkBlock:
    """回归：`</think>` 之后的换行常作为**独立一次 feed** 到达。

    我第一版修复是在闭标签那行做 `buf.lstrip("\\r\\n")` —— 看着对，实际无效：
    逐 token 到达时，闭标签出现的那一刻缓冲区里还没有那个换行，它在**下一次**
    feed 才到，于是原样漏出去，落库正文以一个空行开头（写接线测试时才发现）。
    正确做法是留一个「刚闭合、正在跳换行」的状态。
    """

    def test_newline_as_separate_feed_is_swallowed(self):
        f = ReasoningFilter()
        out = "".join(f.feed(p) for p in ["A", "<think>", "思考", "</think>"])
        out += f.feed("\n")          # 独立一次
        out += f.feed("\n")          # 再来一个
        out += f.feed("B")
        out += f.flush()
        assert out == "AB"

    def test_leading_indent_after_think_is_preserved(self):
        """只吃换行，**不吃缩进** —— 缩进可能是正文排版的一部分。"""
        f = ReasoningFilter()
        out = "".join(f.feed(p) for p in ["<think>", "思考", "</think>", "\n", "  缩进正文"])
        out += f.flush()
        assert out == "  缩进正文", "缩进不该被吃掉"

    def test_skip_flag_clears_after_first_real_char(self):
        f = ReasoningFilter()
        assert f.feed("<think>") == ""
        f.feed("思考</think>")
        assert f.feed("\n") == ""
        assert f.feed("正") == "正"   # 跳过后恢复正常
        assert f.feed("\n") == "\n"  # 此后的换行必须保留
