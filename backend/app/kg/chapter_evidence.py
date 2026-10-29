"""伏笔「计划回收章」的第二级判据：正文证据。

## 为什么需要「正文证据」这一级

第一级判据（`plausible_payoff_chapter`）只管**数值不变式**：
章号 ≥ 1、晚于埋下章、不超过已写到的章。它挡得住「明显非法」的值，
挡不住「**数值合法但凭空来的**」值。

实测（2026-10-29，发布版冒烟库 140 条伏笔）：

- `pf=0`：**84 条**（第 0 章不存在）；
- `pf=50/30/12`：**24 条**（超上界；其中 18 条恰好等于抽取提示 JSON 示例的
  字面值 50 ⇒ 模型照抄示例，`docs/journey_glm_versions_compare.json` 里
  能直接看到模型把示例整行抄进结果）；
- ⇒ 108 条脏值，**全书正文里对它们的章号零证据**。

所以本模块提供「这个章号在正文里真的出现过吗」的判据，
让上层能把**凭空来的**值也判成未知（置空，而不是猜一个数补上）。

## 正文证据长什么样

真章号在正文里长这样（**阳性对照**：参考书语料 10122 块里 151 处命中）：

```
（第一章。）        （第二章。）        白马出凉州第一百三十章八叉
```

⇒ 认「**章**」「**节**」，阿拉伯数字（`第 12 章`）与中文数字（`第十二章`）都认。

## ⚠️ 一个坑：「回」不是章节

生成正文里有这么一句（实测）：

> 「那支商队在城外停过两回，**第一回**是傍晚，**第二回**是换防以后。」

这里的「回」是「**次数**」，不是章节。若把「回」也当章号证据，
「第二回」就会被当成「第 2 章」——凭空给一个不存在的回收章号背书。

⇒ **只认「章」「节」，不认「回」。**
"""

from __future__ import annotations

import re

#: 汉字数字 → 阿拉伯数字的构造规则（0~9999，够章号用了）。
_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

#: 汉字数字的单位（十/百/千）。
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

#: 正文里「第 N 章 / 第 N 节」的写法：阿拉伯数字或中文数字。
#:
#: **不含「回」** —— 见模块文档：「第一回」是「第一次」，不是章节。
_CHAPTER_MENTION = re.compile(
    r"第\s*([0-9]{1,4}|[零〇一两二三四五六七八九十百千]{1,6})\s*[章节]"
)


def parse_chinese_number(text: str) -> int | None:
    """把中文数字解析成整数。解析不了返回 None。

    >>> parse_chinese_number("十二")
    12
    >>> parse_chinese_number("一百三十")
    130
    >>> parse_chinese_number("三")
    3
    >>> parse_chinese_number("十")
    10
    >>> parse_chinese_number("二十")
    20
    >>> parse_chinese_number("零")
    0
    >>> parse_chinese_number("abc") is None
    True
    """
    s = (text or "").strip()
    if not s:
        return None
    # 纯阿拉伯数字
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    total = 0
    section = 0
    number = 0
    seen = False
    for ch in s:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
            seen = True
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            # 「十二」：单位前面没数字 ⇒ 前面是 1（「十」本身 = 10）
            if number == 0:
                number = 1
            section += number * unit
            number = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + section + number


def mentioned_chapters(text: str) -> set[int]:
    """正文里提到过哪些章号？

    只认「第 N 章」「第 N 节」（阿拉伯数字或中文数字），
    **不认「回」**（「第一回」= 第一次，不是章节）。

    >>> sorted(mentioned_chapters("（第一章。）他说，第三章再说"))
    [1, 3]
    >>> sorted(mentioned_chapters("第一百三十章"))
    [130]
    >>> mentioned_chapters("停过两回，第一回是傍晚，第二回是换防以后")
    set()
    >>> sorted(mentioned_chapters("第 12 章"))
    [12]
    """
    out: set[int] = set()
    for m in _CHAPTER_MENTION.finditer(text or ""):
        n = parse_chinese_number(m.group(1))
        if n is not None and n >= 1:
            out.add(n)
    return out


def payoff_chapter_in_text(chapter, text: str) -> bool:
    """这个回收章号在正文里真的出现过吗？

    >>> payoff_chapter_in_text(12, "（第一章。）")
    False
    >>> payoff_chapter_in_text(1, "（第一章。）")
    True
    >>> payoff_chapter_in_text(2, "停过两回，第二回是换防以后")
    False
    >>> payoff_chapter_in_text(12, "第十二章")
    True
    """
    if not isinstance(chapter, int) or isinstance(chapter, bool):
        return False
    return chapter in mentioned_chapters(text or "")
