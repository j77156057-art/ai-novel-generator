"""缩小改写范围：只改偏离最严重的一段连续区间，其余原文原样保留。"""
from __future__ import annotations

from app.generation.rewrite import FOCUS_MAX_CHARS, _focus_span, rewrite_style

#: 一句约 28 字，落在测试目标带 10~40 内 → 不该被选中。
_NORMAL = "他把窗户轻轻推开，夜风慢慢灌了进来，楼道里的灯还没有熄灭。"
#: 三句各 2 字，离目标带 8 字 → 最该被改写。
_DEVIANT = "他走。她看。灯灭。"


def _long_text(head: int = 70) -> str:
    return "\n".join([_NORMAL] * head + [_DEVIANT] + [_NORMAL] * 5)


def test_focus_span_selects_deviant_paragraph():
    text = _long_text()
    span = _focus_span(text, (10.0, 40.0))
    assert span is not None
    assert _DEVIANT in text[span[0]:span[1]]
    assert span[1] - span[0] <= FOCUS_MAX_CHARS


def test_focus_span_none_when_short():
    assert _focus_span(_NORMAL + "\n" + _DEVIANT, (10.0, 40.0)) is None


def test_rewrite_style_only_replaces_focus_window():
    text = _long_text()
    head = text[: text.index(_DEVIANT)]
    tail = text[text.index(_DEVIANT) + len(_DEVIANT):]

    class _Provider:
        def variant(self, **_kwargs):
            return self

        def chat(self, system: str, user: str, **_kwargs) -> str:
            return "他慢慢走过去。"

    out, info = rewrite_style(text, _Provider(), target_sentence_range=(10.0, 40.0))
    assert info["scope"] == "window"
    assert out.startswith(head)
    assert out.endswith(tail)
    assert _DEVIANT not in out
