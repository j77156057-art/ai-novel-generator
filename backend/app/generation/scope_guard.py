# -*- coding: utf-8 -*-
"""局部改写范围校验：只提示、不拦截。

背景：用户说「只收拾段尾」，模型却把 2273 字整段重写成 1212 字（公共前缀 0、
公共后缀 0），接口照常返回成功。重写本身是允许的——用户可能真的想整段改——
所以这里只做**事后判定**，结果进响应 / ``done`` 事件 / 快照行，供统计模型
服从度；**不自动重试、不拒绝落库**。

判定是「两件事同时成立」：

1. 指令**声明了局部**（:func:`declared_local`）；
2. 实际结果**像整段重写**：新旧正文公共前缀与公共后缀都很短
   （:func:`detect_scope_mismatch`）。

单看任一条都会误报：用户说「整段重写」但保留了开头几句，不该提示；用户说
「改冷一点」而模型整段重写，也不该提示（指令本来就没限定范围）。
"""
from __future__ import annotations

import re

#: 「用户声明了局部」的关键词表。
#:
#: 选词理由（按语义分组）：
#: - **范围限定**：只 / 仅 / 保留 / 不要动 / 别动 / 不改 —— 直接表达「只改这部分、
#:   其余保留」；
#: - **位置锚点**：结尾 / 段尾 / 开头 / 段首 —— 以位置限定改动范围；
#: - **句子粒度**：最后一句 / 第一句 / 第 N 句 / 这一句 / 这两句 —— 明确到句；
#: - **局部指代**：某处 / 局部 / 此处 —— 无具体位置但明确「不是整段」。
#:
#: 「只 / 仅」是单字，会在「不仅」「只要」里误命中。这是刻意接受的：本判定
#: 必须**同时**满足「前后缀都很短」才提示；在那种情况下，一句「指令像限定
#: 局部、结果却像整段重写」的提示本身是合理的，不会阻断任何操作。
#: 位置锚点：段尾 / 段首。**与补丁范围解析共用同一份词表**——
#: `app.generation.patch.resolve_scope` 直接引用这两个元组把词映射成编号，
#: 不另抄一份（两套口径必漂）。
TAIL_SCOPE_PATTERNS = (r"结尾", r"段尾", r"最后一句")
HEAD_SCOPE_PATTERNS = (r"开头", r"段首", r"第一句")

#: 句号编号里的数字字符集；`NTH_SENTENCE_PATTERN` 的捕获组 1 就是它。
SENTENCE_NUM = r"[0-9一二三四五六七八九十百]+"
NTH_SENTENCE_PATTERN = rf"第\s*({SENTENCE_NUM})\s*句"

LOCAL_SCOPE_PATTERNS = (
    r"只",
    r"仅",
    r"保留",
    r"不要动",
    r"别动",
    r"不改",
    *TAIL_SCOPE_PATTERNS,
    *HEAD_SCOPE_PATTERNS,
    NTH_SENTENCE_PATTERN,
    r"某处",
    r"局部",
    r"此处",
    r"这一句",
    r"这两句",
)

_LOCAL_RE = re.compile("|".join(LOCAL_SCOPE_PATTERNS))

#: 公共前缀「短」的阈值（字符）。
#:
#: 依据：中文小说平均句长约 25–30 字，20 字不足一个短句——连开头一个短句都没
#: 原样保留，就很难说「只是局部改了改」。**刻意不用 0**：模型常保留开头几个
#: 字（人名、时间状语）或结尾标点，0 会把这类「伪局部」全部漏掉；而真正保留
#: 开头的局部改写，前缀通常是几十到几百字，不会被 20 误伤。
SCOPE_PREFIX_MAX = 20

#: 公共后缀「短」的阈值（字符）。取值依据同 :data:`SCOPE_PREFIX_MAX`。
SCOPE_SUFFIX_MAX = 20

#: 参与判定的最短正文（字符）。不足百字的多是碎片/测试段，前后缀占比失真，
#: 且「整段重写」在短文本上本来就是正常操作。
SCOPE_MIN_CHARS = 100


#: 「用户明确要求缩短」的关键词表。命中即**不**提示篇幅下跌——那是他要的。
#:
#: 分三组：
#: - **缩写动词**：精简 / 压缩 / 缩短 / 简化 / 瘦身 / 删减 / 砍掉 / 删掉 / 去掉 /
#:   缩写 / 略写 / 删繁就简 —— 直接表达「变短」；
#: - **字数意图**：控制字数 / 字数控制 / 减少字数 / 字数压|减|降 / 短一点 / 短些 /
#:   别太长 / 不要太长 —— 不一定是动词，但语义明确指向篇幅；
#: - **数值上限**：不超过 N 字 / 控制在 N 字 / 降到|减到|压到 N 字 —— 带数字的硬上限。
#: 与局部关键词不同，这里没有可以「误命中也不碍事」的单字；因此宁可窄一点：
#: 漏判只会多一条提示（不阻断），误判则会把用户真正想要的压缩当成异常。
_COMPRESS_PATTERNS = (
    r"精简",
    r"压缩",
    r"缩短",
    r"简化",
    r"瘦身",
    r"删减",
    r"砍掉",
    r"删掉",
    r"去掉",
    r"缩写",
    r"略写",
    r"删繁就简",
    r"控制(?:一下)?字数",
    r"字数控制",
    r"减少字数",
    r"字数(?:压|减|降)",
    r"短(?:一)?点",
    r"短些",
    r"别太长",
    r"不要太长",
    r"不超过\s*[0-9一二三四五六七八九十百千万]+\s*字",
    r"控制在\s*[0-9一二三四五六七八九十百千万]+\s*字",
    r"(?:降|减|压)到\s*[0-9一二三四五六七八九十百千万]+\s*字",
)

_COMPRESS_RE = re.compile("|".join(_COMPRESS_PATTERNS))

#: 篇幅跌幅提示线：重写后长度 < 原文 80%（即跌幅 > 20%）。
#:
#: 依据：与重写提示词里的 80%–120% 区间同一口径，提示词说「应该」、这里验「实际」，
#: 两边不会各说各话；20% 也大致相当于一段场景文字的量级，正常润色/改段尾不会
#: 掉这么多，掉到线下通常意味着模型重写了整段（实测两次：53%、37%）。
LENGTH_DROP_MIN_RATIO = 0.8


def declared_local(instruction: str) -> bool:
    """指令是否声明了「只改某一部分」。"""
    return bool(_LOCAL_RE.search(instruction or ""))


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def common_suffix_len(a: str, b: str, prefix_len: int = 0) -> int:
    """公共后缀长度（**非重叠**：最多算到公共前缀结束处）。"""
    limit = min(len(a), len(b)) - max(0, prefix_len)
    if limit <= 0:
        return 0
    i = 0
    while i < limit and a[len(a) - 1 - i] == b[len(b) - 1 - i]:
        i += 1
    return i


def declared_compression(instruction: str) -> bool:
    """指令是否明确要求「精简 / 压缩 / 缩短 / 控制字数」。"""
    return bool(_COMPRESS_RE.search(instruction or ""))


def detect_length_drop(
    instruction: str | None, before: str | None, after: str | None
) -> dict | None:
    """重写后篇幅跌幅 > 20% 时给提示（**与局部范围问题分开记账**）。

    与 :func:`detect_scope_mismatch` 拆成两个字段，是因为它们是两种不同的
    模型不服从：可以「首尾保留很多但整段被压掉一半」，也可以「首尾全丢但字数
    没怎么变」。混在一个字段里，以后统计不出模型到底栽在哪一类。

    Returns:
        None：用户明确要求缩短、文本太短、或跌幅未超过 20%；
        否则返回可 JSON 序列化的提示结构（含长度、比例、阈值）。
        同样**只提示**：调用方不得据此拒绝落库或自动重试。
    """
    text_before = before or ""
    text_after = after or ""
    if len(text_before) < SCOPE_MIN_CHARS or len(text_after) < SCOPE_MIN_CHARS:
        return None
    if declared_compression(instruction or ""):
        return None

    ratio = len(text_after) / len(text_before)
    if ratio >= LENGTH_DROP_MIN_RATIO:
        return None

    drop_ratio = 1 - ratio
    return {
        "before_chars": len(text_before),
        "after_chars": len(text_after),
        "ratio": round(ratio, 4),
        "drop_ratio": round(drop_ratio, 4),
        "min_ratio": LENGTH_DROP_MIN_RATIO,
        "message": (
            f"原文 {len(text_before)} 字，重写后 {len(text_after)} 字"
            f"（{ratio:.1%}，跌幅 {drop_ratio:.1%}），低于原文 "
            f"{LENGTH_DROP_MIN_RATIO:.0%} 的提示线。结果已正常保存；"
            "如果这次就是要精简，可在指令里写明「精简/压缩/控制字数」。"
        ),
    }


def detect_scope_mismatch(
    instruction: str | None, before: str | None, after: str | None
) -> dict | None:
    """判定「指令限定局部、结果却像整段重写」。

    Returns:
        None：未声明局部、文本太短、或确实保留了足够的前缀/后缀；
        否则返回可 JSON 序列化的提示结构（含前后缀长度、字数变化、阈值）。
        调用方只把它放进响应 / 事件 / 快照行，**不得**据此拦截落库。
    """
    text_before = before or ""
    text_after = after or ""
    match = _LOCAL_RE.search(instruction or "")
    if match is None:
        return None
    if len(text_before) < SCOPE_MIN_CHARS or len(text_after) < SCOPE_MIN_CHARS:
        return None

    prefix = common_prefix_len(text_before, text_after)
    suffix = common_suffix_len(text_before, text_after, prefix)
    if prefix > SCOPE_PREFIX_MAX or suffix > SCOPE_SUFFIX_MAX:
        return None

    delta = len(text_after) - len(text_before)
    return {
        "declared_local": True,
        "matched": match.group(0),
        "common_prefix_chars": prefix,
        "common_suffix_chars": suffix,
        "before_chars": len(text_before),
        "after_chars": len(text_after),
        "chars_delta": delta,
        "thresholds": {
            "prefix_max": SCOPE_PREFIX_MAX,
            "suffix_max": SCOPE_SUFFIX_MAX,
            "min_chars": SCOPE_MIN_CHARS,
        },
        "message": (
            f"你的指令限定了局部修改（命中「{match.group(0)}」），"
            f"但这次新旧正文只有开头 {prefix} 字、结尾 {suffix} 字相同"
            f"（{len(text_before)} 字 → {len(text_after)} 字），更像整段重写。"
            "结果已正常保存；如非本意，可撤销后重试。"
        ),
    }
