"""伏笔条目的**质量判据**：哪些条目其实是对话碎片，不该当悬念进提示词。

## 为什么要有这个

实测（2026-10-28，验收库 6 段真实正文 + 发布版生产代码）：
KG 抽取的「未填伏笔」**19 条里 14 条是对话碎片**
（`“你把我当什么？”`、`还是留在这里？`、`身后跟的是什么？`……）——
抽取器把**台词**当成了悬念。⇒ 它们照原样进了 system 提示的约束块：
白占位置，还可能让模型误以为那些是「要在后续慢慢推进的悬念」。

## 判据只判「像不像对话」，不判「是不是好悬念」

宁可**放过**（模型多看一条无害），不可**误杀**（误杀 = 模型丢了一条真约束）。
⇒ 只在**高置信**的对话特征上判不合格：

1. **含问号**（？/?）—— 疑问句是对话；
2. **含「冒号 + 引号」**（：“）—— 那是有人**说话**；
3. **以引号开头**（“「『）—— 引号里是台词。

其它一律放过 —— 包括「他一路没怎么说话。」这种**说不清**是不是悬念的
（放过 = 只多占一行；误杀 = 真约束丢了）。

## 两个使用点，一条规则

- **渲染层**（`generation/prompts.py`）：只过滤 **KG 抽取来的**伏笔，
  **用户自己在剧情圣经里写的伏笔一个字都不动**（那是权威）。
- **抽取层**（`kg/batch_worker.py`）：命中时**只留痕**（计数 + 警告日志），
  **不删、不改账本** —— 账本里的东西还在，只是提示词不再拿它当约束。
"""

from __future__ import annotations

#: 以这些符号开头 ⇒ 引号里是台词。
_QUOTE_HEADS = ("“", "「", "『", "\"", "'")

#: 「冒号 + 引号」⇒ 有人在说话（中文/英文冒号都行）。
_SPEECH_MARKS = ("：“", ":\"", "：'", ":\"")

#: 疑问句标志。
_QUESTION_MARKS = ("？", "?")


def looks_like_dialogue_fragment(text: str | None) -> bool:
    """这条伏笔其实是一句**对话/台词**吗？（高置信才判 True）

    >>> looks_like_dialogue_fragment("“你把我当什么？”")
    True
    >>> looks_like_dialogue_fragment("那人弯腰看向脚边的磨刀石：“磨了做什么？")
    True
    >>> looks_like_dialogue_fragment("断刀的刀镡上刻着一个沈砚不认识的姓")
    False
    >>> looks_like_dialogue_fragment("周郎中留下一句「刀别离手」")
    False
    """
    s = (text or "").strip()
    if not s:
        return False
    if any(m in s for m in _QUESTION_MARKS):
        return True
    if any(m in s for m in _SPEECH_MARKS):
        return True
    if s.startswith(_QUOTE_HEADS):
        return True
    return False


def split_quality(texts: list[str]) -> tuple[list[str], list[str]]:
    """把一组伏笔条目分成 (保留, 疑似对话碎片)。"""
    keep: list[str] = []
    junk: list[str] = []
    for t in texts or []:
        (junk if looks_like_dialogue_fragment(t) else keep).append(t)
    return keep, junk
