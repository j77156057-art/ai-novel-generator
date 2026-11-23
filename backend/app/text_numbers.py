"""中文数字解析 —— 全项目唯一实现。

小说正文/大纲里的年龄、年份、跨度常写成「二十三」「一百二」这类中文数字。
此前 `coherence`、`kg.bible`、`generation.progress_ledger` 各抄了一份算法，
数字表还互不一致（有的不认「两」「○」，有的不认「百」）。集中到这里后，
三处读同一个数字表，避免同一句话在不同检查里被解析成不同值。

只处理 0~999 的常见写法；解析不了返回 None（不猜）。
"""
from __future__ import annotations

#: 常见中文数字；`两` 与 `○` 是小说里会出现的异体写法，一并收进来。
_CN_DIGITS = {
    "零": 0, "〇": 0, "○": 0,
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def cn_number(s: str | None) -> int | None:
    """中文数字 → int（0~999）。解析不了返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            total += (section or 1) * 10
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section
