# -*- coding: utf-8 -*-
"""局部指令 → 句子索引补丁：程序定范围，模型只写替换文本。

## 两级分工

**Tier A（范围词由程序解析）**：指令里出现位置锚点（段尾 / 结尾 / 最后一句 /
段首 / 开头 / 第一句 / 第 N 句，以及带数量的 最后两句 / 前两句 / 倒数第二句
等）时，``resolve_scope`` 直接把词映射成编号区间。**改写提示词不再出现任何
编号**：程序按编号取出目标句原文，只把这几句放进分隔块交给模型；模型只回
替换文本，改哪一句完全由程序决定。模型若仍然回了编号，编号必须与程序算的
一致，否则 ``index_mismatch`` 失败关闭。

**Tier B（含糊指令，先澄清候选）**：``某处`` / ``此处`` / ``这一句`` 这类无法
从指令本身定位的说法，``resolve_scope`` 返回 None。此时**不让模型直接写正文**，
先让模型列出 2–4 个候选区间（``build_candidates_user_prompt``，只回编号），
程序逐个校验并转成 ``{from,to,preview,reason}``，其中 preview 由程序按编号从
原文截取。用户选定后，再用 Tier A 的 ``build_patch_user_prompt`` 生成替换文本，
校验与拼接完全共用。模型没有「一边选位置一边写正文」的机会。

## 剥除行首编号（仅兜底）

改写提示词已经从根上不提供任何编号，模型本就不该写。但实测中模型仍可能
自行补 ``83.`` / ``84.``（或 ``83、`` / ``83)`` / ``（83）``，中文数字同样
认）。程序在 Tier A 第二步统一按行剥除行首编号，**剥完再做所有校验**；剥完
没有正文（只剩空白/标点）按 ``replacement_numbering_only`` 失败关闭。
``parse_patch_reply`` 的 ``numbering_stripped`` 会标记兜底是否被触发：验收
看的是「模型本来就没写编号」，不是「写了被剥掉」。

## 多句范围的规则

- 不带数量的位置词（``段尾 / 结尾 / 最后一句 / 段首 / 开头 / 第一句``）→
  **1 句**。理由：这些词本身不带 N，若默认取「最后若干句」，N 无界，很容易
  滑向整段——这正是要修的缺陷。
- 带数量的位置词（``最后两句 / 最后三句 / 末两句 / 前两句 / 头三句``）→ 程序
  解析出 **N 句**；``倒数第 N 句`` → 从段尾往前数第 N 句（单句）。中文数字与
  阿拉伯数字都认。
- 显式区间（``第 N 句到第 M 句``）**优先于所有位置词**：用户把编号写死时，
  不要再猜。
- **数量上界**：``N <= max(SCOPE_COUNT_MIN, total // SCOPE_COUNT_DIVISOR)``
  且 ``N < total``，超界抛 ``scope_count_too_large`` 失败关闭。判据：超过全文
  四分之一的句子被同时改写，就很难再说「其余保留」——这是「局部」与「整段」
  之间一条可验证的分界；至少允许 2 句，是因为 8 句以下的短段 ``total // 4``
  只有 0–1，不兜底的话「最后两句」在最需要它的短段上反而不可用。**超界直接
  失败，不静默截断**：按上界截断会让用户以为改了 N 句、实际只改了几分之一，
  比报错更糟。

## 拒绝路径：直接失败，不回退整段重写

回退会重新打开「整段原文 + 重写」的压缩口子，而且成功的整段重写与局部改写
正文不可区分。契约是：局部指令要么精确补丁，要么正文一行不动。失败留痕：
快照 ``failed + error_reason=code``、HTTP 422 ``{code,message}``。

## 错误码（各自独立，不共用）

``index_missing``            缺 from_index/to_index（或 index）
``index_invalid``            编号不是整数
``index_out_of_range``       编号越界
``index_reversed``           from_index > to_index
``index_mismatch``           Tier A：模型回的编号 ≠ 程序解析出的编号
``scope_count_too_large``    数量词超过上界（防止「最后一百句」滑回整段）
``span_whole_segment``       编号范围覆盖全文
``no_valid_candidate``       所有候选都被丢弃，澄清失败关闭
``selection_not_in_candidates`` 用户选定区间不在候选清单内
``clarification_required``   含糊指令未经澄清就进入改写（安全网）
``replacement_missing``      缺 replacement 字段
``empty_patch``              replacement 为空（或剥编号后没有正文）
``replacement_numbering_only`` 剥掉行首编号后只剩空白/标点，没有正文
``replacement_whole_segment`` 长度与句数双判据都指向「整段」
``replacement_too_long``     相对所选范围明显过长
``replacement_sentence_mismatch`` 多句跨度改写后句数与待改句数不一致
``no_change``                改写结果与原文该范围完全相同
``unparsable``               看起来是 JSON 但解析不了
"""
from __future__ import annotations

import json
import re

from app.generation.scope_guard import (
    COUNTDOWN_PATTERN,
    HEAD_COUNT_PATTERN,
    HEAD_SCOPE_PATTERNS,
    NTH_SENTENCE_PATTERN,
    SENTENCE_NUM,
    TAIL_COUNT_PATTERN,
    TAIL_SCOPE_PATTERNS,
)

REPLACEMENT_SLACK = 60
REPLACEMENT_RATIO = 3
#: 数量词上界：``N <= max(SCOPE_COUNT_MIN, total // SCOPE_COUNT_DIVISOR)`` 且
#: ``N < total``。依据见模块头「数量上界」——四分之一是「局部/整段」之间
#: 可验证的分界，至少 2 句是短段兜底；**超界失败关闭，不截断**。
SCOPE_COUNT_MIN = 2
SCOPE_COUNT_DIVISOR = 4
#: 「replacement 是整段」的长度判据：达到全文长度的 80%。
WHOLE_SEGMENT_LENGTH_RATIO = 0.8
#: 「replacement 是整段」的句数判据：达到全句数的 80%。
WHOLE_SEGMENT_SENTENCE_RATIO = 0.8
#: 含糊指令的候选区间上界：单次最多 3 句，且不超过全文 10%（向下取整，
#: 至少 1 句）。依据见模块头「候选上界」。
CLARIFY_MAX_SENTENCES = 3
CLARIFY_MAX_RATIO = 0.10
CLARIFY_MAX_CANDIDATES = 4
#: 候选理由与预览的展示上限（字符）。preview 仍由程序从原文按编号截取，
#: 这里只做一行化与截断，不采信模型复述。
CLARIFY_MAX_REASON = 60
CLARIFY_MAX_PREVIEW = 160

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_SENTENCE_END = "。！？!?"
_SENTENCE_CLOSERS = "”’\"'」』）)】"
_SENTENCE_RE = re.compile(
    rf"[^{_SENTENCE_END}]*[{_SENTENCE_END}]+[{_SENTENCE_CLOSERS}]*"
)

# —— Tier A：范围词 → 编号 ——
_TAIL_RE = re.compile("|".join(TAIL_SCOPE_PATTERNS))
_HEAD_RE = re.compile("|".join(HEAD_SCOPE_PATTERNS))
_TAIL_COUNT_RE = re.compile(TAIL_COUNT_PATTERN)
_HEAD_COUNT_RE = re.compile(HEAD_COUNT_PATTERN)
_COUNTDOWN_RE = re.compile(COUNTDOWN_PATTERN)
_SINGLE_NTH_RE = re.compile(NTH_SENTENCE_PATTERN)
#: 显式多句区间：第 N 句到第 M 句（到/至/破折号/波浪号均可）。
_RANGE_RE = re.compile(
    rf"第\s*({SENTENCE_NUM})\s*句\s*(?:到|至|—|–|-|~|～)\s*第?\s*({SENTENCE_NUM})\s*句"
)
#: 行首编号：``83.`` / ``83．`` / ``83、`` / ``83)`` / ``83）`` / ``(83)`` /
#: ``（83）``。数字字符集与句子编号共用（中文 + 阿拉伯）。
#: 点号后紧跟数字的不剥（``3.14`` 是正文里的数，不是编号）。
_NUMBERING_RE = re.compile(
    rf"^\s*(?:[（(]\s*{SENTENCE_NUM}\s*[)）]"
    rf"|{SENTENCE_NUM}\s*[.．](?!\d)"
    rf"|{SENTENCE_NUM}\s*[、)）])\s*"
)


class PatchError(ValueError):
    """补丁无法安全落地。`code` 会进快照 error_reason 与 HTTP 422。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def split_sentences(text: str) -> list[dict]:
    """按句末标点切分，返回 ``[{start, end, text}]``（1-based 编号即下标+1）。

    句间空白/换行不属于任何句子：替换只动句子本身，段落间隔原样保留。
    末尾没有句末标点的残句也算一句（p5 段 2 的结尾就是这种）。
    """
    spans: list[dict] = []
    for match in _SENTENCE_RE.finditer(text):
        start, end = match.start(), match.end()
        while start < end and text[start].isspace():
            start += 1
        if start < end:
            spans.append({"start": start, "end": end, "text": text[start:end]})
    tail = spans[-1]["end"] if spans else 0
    rest = text[tail:]
    if rest.strip():
        start = tail + (len(rest) - len(rest.lstrip()))
        end = tail + len(rest.rstrip())
        spans.append({"start": start, "end": end, "text": text[start:end]})
    return spans


def _sentence_count(text: str) -> int:
    return sum(text.count(ch) for ch in _SENTENCE_END)


def _render_numbered(spans: list[dict], scope: dict | None = None) -> str:
    lines: list[str] = []
    for i, span in enumerate(spans, 1):
        mark = ""
        if scope is not None and scope["from_index"] <= i <= scope["to_index"]:
            mark = ">> "
        lines.append(f"{mark}{i}. {span['text']}")
    return "\n".join(lines)


def _strip_leading_numbering(text: str) -> str:
    """按行剥掉行首编号（``83.`` / ``83、`` / ``（83）`` …），其余原样保留。"""
    lines = [_NUMBERING_RE.sub("", line, count=1) for line in text.splitlines()]
    return "\n".join(lines).strip()


def _has_body_text(text: str) -> bool:
    """是否还有「正文」：中文/字母至少一个（纯数字、纯标点都不算）。"""
    return any(ch.isalpha() for ch in text)


def _cn_to_int(token: str) -> int:
    """把 ``第 N 句`` 里的 N 转成整数；支持阿拉伯数字与「八十四」这类中文数字。"""
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    digits = {
        "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100}
    total = 0
    current = 0
    for ch in token:
        if ch in digits:
            current = digits[ch]
        elif ch in units:
            unit = units[ch]
            total += (current or 1) * unit
            current = 0
        else:
            raise PatchError("index_invalid", f"句子编号不是可解析的数字：{token!r}")
    return total + current


def _bounded_scope(
    from_index: int, to_index: int, total: int, keyword: str | None,
    *, source: str = "program",
) -> dict:
    if not (1 <= from_index <= total and 1 <= to_index <= total):
        raise PatchError(
            "index_out_of_range",
            f"指令解析出的编号越界：{from_index}..{to_index}，有效范围 1..{total}。",
        )
    if from_index > to_index:
        raise PatchError(
            "index_reversed",
            f"指令解析出的编号反了：{from_index} > {to_index}。",
        )
    if from_index == 1 and to_index == total:
        raise PatchError(
            "span_whole_segment",
            f"指令解析出的范围 1..{total} 覆盖全文：这不是局部改写。",
        )
    return {
        "from_index": from_index,
        "to_index": to_index,
        "keyword": keyword,
        "source": source,
        "total_sentences": total,
    }


def _count_scope(
    total: int, count: int, keyword: str, *, from_tail: bool
) -> dict:
    """数量词 → 编号区间；超上界失败关闭（**不截断**）。"""
    if count < 1:
        raise PatchError(
            "index_out_of_range", f"指令解析出的句数不是正数：{count}。"
        )
    limit = max(SCOPE_COUNT_MIN, total // SCOPE_COUNT_DIVISOR)
    if count > limit or count >= total:
        raise PatchError(
            "scope_count_too_large",
            f"指令要求改 {count} 句，超过上界 {limit} 句（全文 {total} 句；"
            f"上界 = max({SCOPE_COUNT_MIN}, total // {SCOPE_COUNT_DIVISOR})，"
            "且必须少于全文）。已失败关闭，不会截断成上界。",
        )
    from_index = total - count + 1 if from_tail else 1
    to_index = total if from_tail else count
    return _bounded_scope(from_index, to_index, total, keyword)


def resolve_scope(instruction: str, before_text: str) -> dict | None:
    """Tier A：把范围词解析成编号区间；无法定位返回 None（交给 Tier B）。

    解析顺序（先具体后笼统）：显式区间 → 倒数第 N 句 → 段尾数量词 →
    段首数量词 → 段尾 → 段首 → 第 N 句。词表来自 ``scope_guard``
    （``TAIL_SCOPE_PATTERNS`` / ``HEAD_SCOPE_PATTERNS`` / ``NTH_SENTENCE_PATTERN``
    以及数量词三条），不另造一套。
    """
    text = (instruction or "").strip()
    if not text:
        return None
    spans = split_sentences(before_text)
    total = len(spans)
    if total == 0:
        return None

    match = _RANGE_RE.search(text)
    if match:
        return _bounded_scope(
            _cn_to_int(match.group(1)),
            _cn_to_int(match.group(2)),
            total,
            match.group(0),
        )
    match = _COUNTDOWN_RE.search(text)
    if match:
        index = total - _cn_to_int(match.group(1)) + 1
        return _bounded_scope(index, index, total, match.group(0))
    match = _TAIL_COUNT_RE.search(text)
    if match:
        return _count_scope(
            total, _cn_to_int(match.group(1)), match.group(0), from_tail=True
        )
    match = _HEAD_COUNT_RE.search(text)
    if match:
        return _count_scope(
            total, _cn_to_int(match.group(1)), match.group(0), from_tail=False
        )
    match = _TAIL_RE.search(text)
    if match:
        return _bounded_scope(total, total, total, match.group(0))
    match = _HEAD_RE.search(text)
    if match:
        return _bounded_scope(1, 1, total, match.group(0))
    match = _SINGLE_NTH_RE.search(text)
    if match:
        index = _cn_to_int(match.group(1))
        return _bounded_scope(index, index, total, match.group(0))
    return None


def clarify_span_limit(total: int) -> int:
    """单个候选最多覆盖多少句：``min(3, total // 10)``，至少 1 句。

    3 句限制「一个完整叙事节拍」；10% 防止短段里 3 句占比过大。向下取整
    是保守取法：85 句时 10% 为 8，取更小的 3；20 句时为 2；不足 10 句时
    退化为单句候选，仍可用。
    """
    if total <= 0:
        return 1
    return max(1, min(CLARIFY_MAX_SENTENCES, int(total * CLARIFY_MAX_RATIO)))


def build_candidates_user_prompt(
    before_text: str, instruction: str, note: str | None = None,
) -> str:
    """含糊指令第一轮：只让模型列候选区间，不给它写正文的机会。

    与改写提示词不同，这一步**需要**编号清单——模型只回编号，预览由程序
    按编号从原文截取。返回的编号永远不会进入正文。
    """
    spans = split_sentences(before_text)
    numbered = _render_numbered(spans)
    limit = clarify_span_limit(len(spans))
    note_block = (
        f"\n【补充说明】\n{note.strip()}\n" if (note and note.strip()) else ""
    )
    return (
        "下面是一段已定稿的正文，已按句末标点切成编号句子。"
        "本次只做一件事：列出最可能需要改写的候选区间，不要写任何改写内容。\n\n"
        f"【本次要求】\n{(instruction or '').strip()}\n{note_block}\n"
        f"【编号原文】（共 {len(spans)} 句）\n{numbered}\n\n"
        "【候选规则】\n"
        f"- 给出 2 到 {CLARIFY_MAX_CANDIDATES} 个候选，每个是连续的 "
        "from_index..to_index 区间；\n"
        f"- 每个候选最多 {limit} 句（全文 10% 与 3 句取更小），"
        "不要覆盖全文；\n"
        "- 候选之间不得重叠；\n"
        "- reason 用一句中文说明为什么这里值得改，不超过 40 字。\n\n"
        "【输出格式（必须严格遵守）】\n"
        "只输出一个 JSON 对象，不要代码块、不要解释、不要任何前后缀文字：\n"
        '{"candidates": [{"from_index": 起始句编号, '
        '"to_index": 结束句编号, "reason": "..."}]}\n'
        "- 不要输出 replacement，不要输出任何正文或改写结果；"
        "改写文本由用户选定区间后单独生成。"
    )


#: 待改写片段的起止标记。改写提示词里**不出现任何句子编号**，只靠这两个
#: 标记圈出允许改写的原文；模型若把标记一并回显，解析时先剥掉。
_TARGET_BEGIN = "【待改写片段开始】"
_TARGET_END = "【待改写片段结束】"


def build_patch_user_prompt(
    before_text: str, instruction: str, scope: dict
) -> str:
    """Tier A 改写提示词：只给待改句原文，不给任何编号脚手架。

    位置已由 ``resolve_scope`` 解析成编号，程序按编号取原句；模型不需要也
    不应该看到整段编号列表或 ``>>`` 目标标记——那等于请它抄编号。这里只把
    目标区间原文放进分隔块，问「只返回改写后的文本」。
    """
    spans = split_sentences(before_text)
    start = spans[scope["from_index"] - 1]["start"]
    end = spans[scope["to_index"] - 1]["end"]
    target_text = before_text[start:end]
    return (
        "下面是一段已定稿正文中的几句话。请按【本次要求】改写，"
        "并且只返回改写后的文本。\n\n"
        f"【本次要求】\n{(instruction or '').strip()}\n\n"
        "【待改写的句子】\n"
        f"{_TARGET_BEGIN}\n{target_text}\n{_TARGET_END}\n\n"
        "【输出要求】\n"
        "- 逐句对应改写，保持句子数量不变；\n"
        "- 只返回改写后的文本，不要解释、不要代码块、"
        "不要添加任何列表标记或前后缀；\n"
        "- 不要输出待改写片段之外的任何句子。"
    )


def _as_index(value, name: str) -> int:
    if isinstance(value, bool):
        raise PatchError("index_invalid", f"{name} 不是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise PatchError("index_invalid", f"{name} 不是整数。")


def _parse_replacement(payload: dict) -> str:
    if "replacement" not in payload:
        raise PatchError("replacement_missing", "补丁缺少 replacement 字段。")
    value = payload.get("replacement")
    if not isinstance(value, str):
        raise PatchError("replacement_missing", "replacement 不是字符串。")
    text = value.strip()
    if not text:
        raise PatchError("empty_patch", "replacement 为空。")
    return text


def _validate_replacement(
    replacement: str, span_text: str, *, before_text: str,
    span_sentences: int, total_sentences: int,
) -> None:
    if replacement == span_text:
        raise PatchError("no_change", "改写结果与原文该范围完全相同。")
    # 多句跨度：句数必须一致。落库按字符区间整段替换，机械上不存在还原
    # 歧义；这条约束的作用是锁住局部边界——不允许 2 句悄悄扩成 3–4 句，
    # 也不允许合并成 1 句后失去逐句对应关系。连续句末标点由 split_sentences
    # 视为一句，避免「？！」被误数。
    if span_sentences >= 2:
        replacement_sentences = len(split_sentences(replacement))
        if replacement_sentences != span_sentences:
            raise PatchError(
                "replacement_sentence_mismatch",
                f"待改范围是 {span_sentences} 句，改写后是 "
                f"{replacement_sentences} 句；多句跨度要求句数一致。",
            )
    # 整段判据：长度与句数同时逼近全文，才判「replacement 是整段」。
    length_is_whole = len(replacement) >= WHOLE_SEGMENT_LENGTH_RATIO * len(before_text)
    sentences_is_whole = (
        _sentence_count(replacement)
        >= WHOLE_SEGMENT_SENTENCE_RATIO * total_sentences
    )
    if length_is_whole and sentences_is_whole:
        raise PatchError(
            "replacement_whole_segment",
            f"replacement 有 {len(replacement)} 字、"
            f"{_sentence_count(replacement)} 句，判定为整段而非局部改写。",
        )
    limit = max(len(span_text) * REPLACEMENT_RATIO, len(span_text) + REPLACEMENT_SLACK)
    if len(replacement) > limit:
        raise PatchError(
            "replacement_too_long",
            f"replacement 有 {len(replacement)} 字，所选范围只有 "
            f"{len(span_text)} 字。",
        )
    if span_sentences <= 1 and _sentence_count(replacement) >= 3:
        raise PatchError(
            "replacement_too_long",
            "只选了一句，replacement 却是多句：不像局部改写。",
        )


def _check_model_indices(payload: dict, scope: dict) -> None:
    """Tier A：模型若回了编号，必须与程序解析出的编号完全一致。"""
    keys = ("index", "from_index", "to_index", "start", "end")
    if not any(key in payload for key in keys):
        return
    from_value = payload.get("from_index", payload.get("index",
                   payload.get("start")))
    to_value = payload.get("to_index", payload.get("index", payload.get("end")))
    if from_value is None or to_value is None:
        raise PatchError(
            "index_missing",
            "目标范围已由程序确定，模型返回的编号字段却不完整。",
        )
    from_index = _as_index(from_value, "from_index")
    to_index = _as_index(to_value, "to_index")
    if (from_index, to_index) != (scope["from_index"], scope["to_index"]):
        raise PatchError(
            "index_mismatch",
            f"模型给的编号是 {from_index}..{to_index}，"
            f"程序解析出的目标范围是 {scope['from_index']}..{scope['to_index']}；"
            "两者不一致，拒绝按模型编号改写。",
        )


def _strip_target_delimiters(text: str) -> str:
    """模型若把分隔块标记一并回显，先取标记之间的内容。"""
    if _TARGET_BEGIN in text and _TARGET_END in text:
        text = text.split(_TARGET_BEGIN, 1)[1].split(_TARGET_END, 1)[0]
    return text.strip()


def _tier_a_replacement(reply: str, scope: dict) -> tuple[str, bool]:
    """Tier A 的返回解析：接受纯替换文本，或带编号的 JSON（编号须一致）。

    返回 ``(替换文本, 是否触发了行首编号剥离)``。编号剥离只是兜底：正常情况
    下改写提示词里没有任何编号，模型本来就不该写；``numbering_stripped=True``
    说明模型仍然写了，验收时要如实报告，不能把「剥掉了」当通过。
    """
    raw = (reply or "").strip()
    if not raw:
        raise PatchError("empty_patch", "模型返回为空。")
    text = raw
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _strip_target_delimiters(text)
    if not text:
        raise PatchError("empty_patch", "模型返回为空。")

    if not text.startswith("{"):
        replacement = text
    else:
        # 看起来是 JSON 就必须解析成功；解析失败不能把半截 JSON 当正文拼进去。
        try:
            payload = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            raise PatchError("unparsable", "模型返回的 JSON 无法解析。") from exc
        if isinstance(payload, str):
            payload = {"replacement": payload}
        if not isinstance(payload, dict):
            raise PatchError("unparsable", "补丁 JSON 不是对象。")
        _check_model_indices(payload, scope)
        replacement = _parse_replacement(payload)

    had_numbering = any(
        _NUMBERING_RE.match(line) for line in replacement.splitlines()
    )
    stripped = _strip_leading_numbering(replacement)
    if not stripped or not _has_body_text(stripped):
        raise PatchError(
            "replacement_numbering_only",
            "替换文本剥掉行首编号后只剩空白/标点，没有正文。",
        )
    return stripped, had_numbering


def _clean_any_json_text(reply: str) -> str:
    """从可能带说明/代码块的返回里抠出 JSON 对象或数组。"""
    text = (reply or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    starts = [i for i in (text.find("["), text.find("{")) if i != -1]
    if starts:
        start = min(starts)
        ends = [i for i in (text.rfind("]"), text.rfind("}")) if i > start]
        if ends:
            text = text[start:max(ends) + 1]
    return text


def _load_candidates_payload(reply: str):
    raw = _clean_any_json_text(reply)
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 模型返回不可控
        raise PatchError("unparsable", "模型返回不是可解析的 JSON。") from exc
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            raise PatchError("unparsable", "候选 JSON 是字符串，里面也不是对象。") from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("candidates", "ranges", "options", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        if any(key in payload for key in ("from_index", "index", "start")):
            return [payload]
    raise PatchError("unparsable", "候选 JSON 既不是数组，也没有 candidates 列表。")


def _candidate_indices(item: dict, total: int) -> tuple[int, int] | None:
    """取候选编号；任何一项不合法都返回 None（该候选被丢弃，不整体失败）。"""
    from_value = item.get("from_index", item.get("index", item.get("start")))
    to_value = item.get("to_index", item.get("index", item.get("end")))
    if from_value is None or to_value is None:
        return None
    try:
        from_index = _as_index(from_value, "from_index")
        to_index = _as_index(to_value, "to_index")
    except PatchError:
        return None
    if not (1 <= from_index <= total and 1 <= to_index <= total):
        return None
    if from_index > to_index:
        return None
    if from_index == 1 and to_index == total:
        return None
    return from_index, to_index


def _preview_span(
    before_text: str, spans: list[dict], from_index: int, to_index: int,
) -> str:
    """预览必须由程序从原文按编号截取，不采信模型复述。"""
    start = spans[from_index - 1]["start"]
    end = spans[to_index - 1]["end"]
    text = " ".join(before_text[start:end].split())
    if len(text) > CLARIFY_MAX_PREVIEW:
        text = text[:CLARIFY_MAX_PREVIEW - 1].rstrip() + "…"
    return text


def parse_candidates_reply(reply: str, before_text: str) -> list[dict]:
    """解析含糊指令第一轮的候选区间列表。

    越界、倒序、覆盖全文、超过局部上界、空区间、重复、与已接受候选重叠的
    候选一律丢弃；全部丢弃时抛 ``no_valid_candidate`` 失败关闭。preview 由
    程序按编号从原文截取，reason 只做截断，二者都不会进入正文。
    """
    spans = split_sentences(before_text)
    total = len(spans)
    if total == 0:
        raise PatchError("no_valid_candidate", "正文没有可编号的句子。")
    items = _load_candidates_payload(reply)
    limit = clarify_span_limit(total)
    accepted: list[dict] = []
    seen: set[tuple[int, int]] = set()
    discarded = 0
    for item in items:
        if not isinstance(item, dict):
            discarded += 1
            continue
        indices = _candidate_indices(item, total)
        if indices is None:
            discarded += 1
            continue
        from_index, to_index = indices
        if to_index - from_index + 1 > limit:
            discarded += 1
            continue
        if (from_index, to_index) in seen:
            discarded += 1
            continue
        if any(
            not (to_index < prev["from_index"] or from_index > prev["to_index"])
            for prev in accepted
        ):
            discarded += 1
            continue
        seen.add((from_index, to_index))
        reason = " ".join(str(item.get("reason") or "").split())
        accepted.append({
            "id": f"c{len(accepted) + 1}",
            "from_index": from_index,
            "to_index": to_index,
            "preview": _preview_span(before_text, spans, from_index, to_index),
            "reason": reason[:CLARIFY_MAX_REASON],
        })
        if len(accepted) >= CLARIFY_MAX_CANDIDATES:
            break
    if not accepted:
        raise PatchError(
            "no_valid_candidate",
            f"模型给出的 {len(items)} 个候选全部越界/倒序/重叠/超过局部上界"
            f"（最多 {limit} 句），已失败关闭。",
        )
    return accepted


def scope_from_selection(
    candidates: list[dict], from_index: int, to_index: int, total: int,
) -> dict:
    """用户选定的区间必须原样来自候选清单，不允许前端自造范围。"""
    for item in candidates:
        if (item.get("from_index"), item.get("to_index")) == (from_index, to_index):
            return _bounded_scope(from_index, to_index, total, None, source="user")
    raise PatchError(
        "selection_not_in_candidates",
        f"选定区间 {from_index}..{to_index} 不在本次候选清单内。",
    )


def parse_patch_reply(
    reply: str, before_text: str, scope: dict
) -> tuple[str, dict]:
    """解析模型返回并替换，返回 ``(new_text, meta)``。

    ``scope`` 必须由程序给定（Tier A 直接解析，Tier B 由第一步选定）：
    只从返回里取替换文本，编号不一致即失败关闭。

    所有无法安全落地的情况都抛 :class:`PatchError`；调用方应失败并留痕，
    不得回退整段重写。
    """
    spans = split_sentences(before_text)
    from_index = scope["from_index"]
    to_index = scope["to_index"]
    replacement, numbering_stripped = _tier_a_replacement(reply, scope)
    scope_source = scope.get("source", "program")
    scope_keyword = scope.get("keyword")

    start = spans[from_index - 1]["start"]
    end = spans[to_index - 1]["end"]
    span_text = before_text[start:end]
    _validate_replacement(
        replacement, span_text,
        before_text=before_text,
        span_sentences=to_index - from_index + 1,
        total_sentences=len(spans),
    )

    new_text = before_text[:start] + replacement + before_text[end:]
    meta = {
        "patch_count": 1,
        # Tier B 两段式：1 = 程序解析范围，2 = 模型先选位置、再单独写替换。
        "stages": 2 if scope_source == "model" else 1,
        "scope_source": scope_source,
        "scope_keyword": scope_keyword,
        # 兜底剥离是否被触发；验收关心的仍是模型原始返回有没有编号。
        "numbering_stripped": numbering_stripped,
        "applied": [{
            "from_index": from_index,
            "to_index": to_index,
            "before": span_text,
            "replacement": replacement,
            "before_chars": len(span_text),
            "replacement_chars": len(replacement),
        }],
        "sentence_count": len(spans),
        "before_chars": len(before_text),
        "after_chars": len(new_text),
        "replaced_chars": len(span_text),
        "replacement_chars": len(replacement),
    }
    return new_text, meta
