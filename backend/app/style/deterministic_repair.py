# -*- coding: utf-8 -*-
"""确定性返修：只做可证明安全的机械修复，不调模型。

覆盖第 2 章暴露的三类硬伤：

1. 引语缺句末标点、引号外多一个逗号（``道：“谁搁的”，没人应``）；
2. 同一段里两个说话人（``“……没一样能见光的……”许照说：“那你别搬……”``）；
3. 引号形态不统一（半角双引号 / 角引号混用）。

**不负责**删口头禅、改叙述密度或拉开场景差异——那些必须走模型定向返修。
所有函数都保证幂等、永不抛异常；返回的 ``info`` 只含计数与样本，不塞整段正文。
"""
from __future__ import annotations

import re

from app.style.consistency import hanzi_count
from app.style.quotes import detect_quote_style, normalize_quotes

#: 三类成对引号。半角直引号由 `normalize_quotes` 负责配对，不在这里处理。
_QUOTE_SPAN_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』")

#: 「引语 + 引号外标点」：只认行首或冒号后的引语，避免把「回了个“好”，」这种
#: 词语提及也当成对白去补句号。
_TAIL_RE = re.compile(
    r"(?P<prefix>^|[：:])"
    r"(?P<open>[“「『])"
    r"(?P<inner>[^”」』]{1,200})"
    r"(?P<close>[”」』])"
    r"(?P<tail>[，。])",
    re.M,
)

_END_PUNCT = ("。", "！", "？", "…", "—")

#: 说话动词：判断「引号之间那段」是不是归属语。
_SPEECH_VERB = re.compile(r"[说问道答喊叫嚷骂念嘱咐嘟囔]")

#: 疑问词：只在引语没有句末标点时用来决定补问号还是句号。
_Q_WORDS = (
    "吗", "呢", "么", "谁", "什么", "哪", "怎么", "为什么", "多少", "几",
    "是不是", "能不能", "要不要", "行不行", "对不对", "好不好", "有没有",
)

#: 已知的声纹口头禅与建议上限（超出才算问题）。这是**默认启发式**，
#: 不是判据：只用于把「太密」翻译成一条返修要求。
TIC_WORDS: tuple[tuple[str, int], ...] = (
    ("小的", 8),
    ("合同上写着呢", 1),
)

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUM = "零一二三四五六七八九十"

#: 「那七个字 / 这八个字」——带指示代词，说明是在回指前文某段引语。
_NUM_DECL_RE = re.compile(r"(?:那|这)([一二三四五六七八九十百千两\d]+)个字")

#: 回指窗口：引语和「那 N 个字」之间可能隔着几段删改动作（第 2 章实测
#: 第二处回指距离约 200 字），120 字会漏掉后一处。
_NUM_LOOKBACK = 400


def _is_question(inner: str) -> bool:
    text = (inner or "").strip()
    return any(word in text for word in _Q_WORDS)


def repair_dialogue_punctuation(text: str) -> tuple[str, dict]:
    """给缺句末标点的引语补标点，并去掉引号外多余的逗号/句号。

    只处理行首或冒号后的引语；引语内部已经以 ``。！？…—`` 收尾时，只去掉
    引号外那个重复的逗号/句号。
    """
    info: dict = {
        "fixed": 0,
        "dropped_tail": 0,
        "samples": [],
    }

    def repl(match: re.Match[str]) -> str:
        inner = match.group("inner")
        prefix = match.group("prefix")
        opening = match.group("open")
        closing = match.group("close")
        quote = f"{opening}{inner}{closing}"
        if inner.rstrip().endswith(_END_PUNCT):
            info["dropped_tail"] += 1
            if len(info["samples"]) < 5:
                info["samples"].append(match.group(0))
            return f"{prefix}{quote}"
        end = "？" if _is_question(inner) else "。"
        info["fixed"] += 1
        if len(info["samples"]) < 5:
            info["samples"].append(match.group(0))
        return f"{prefix}{opening}{inner}{end}{closing}"

    got = _TAIL_RE.sub(repl, text or "")
    info["changed"] = bool(info["fixed"] or info["dropped_tail"])
    return got, info


def find_dialogue_punctuation_issues(text: str) -> list[str]:
    """只诊断：返回需要补/去标点的引语片段（不改正文）。"""
    out: list[str] = []
    for match in _TAIL_RE.finditer(text or ""):
        inner = match.group("inner")
        if inner.rstrip().endswith(_END_PUNCT):
            if match.group("tail") == "，":
                out.append(match.group(0))
        else:
            out.append(match.group(0))
    return out


def _paragraph_blocks(text: str) -> tuple[list[str], str]:
    """按原文的分段习惯切块，并返回重建时该用的分隔符。

    引擎落库用空行分段（``\\n\\n``），导出稿可能用单换行（``\\n``）。
    这里两种都认：**不要**把单换行文本当成一整段，否则拆双人同段会把
    整章当成一段去切。
    """
    src = (text or "").strip()
    if not src:
        return [], "\n"
    if re.search(r"\n\s*\n", src):
        blocks = [b.strip() for b in re.split(r"\n\s*\n", src) if b.strip()]
        return blocks, "\n\n"
    return [b.strip() for b in src.split("\n") if b.strip()], "\n"


def _span_ranges(paragraph: str) -> list[tuple[int, int, str]]:
    return [
        (m.start(), m.end(), m.group(0)[1:-1])
        for m in _QUOTE_SPAN_RE.finditer(paragraph)
    ]


def _split_once(paragraph: str) -> list[str] | None:
    """在一个安全边界把段落拆成两段；拆不了返回 None。

    安全边界只有三种：
    - 两段引语之间什么都没有；
    - 中间是 ``许照说：`` 这类「归属语 + 冒号」（归属归后半段）；
    - 中间是 ``他说。`` 这类完整归属短句（归属归前半段）。

    第一段引语必须以句末标点收尾，否则视为同一句话被叙述打断，不拆。
    """
    spans = _span_ranges(paragraph)
    if len(spans) < 2:
        return None
    for i in range(len(spans) - 1):
        a_start, a_end, a_inner = spans[i]
        b_start, b_end, _b_inner = spans[i + 1]
        if not a_inner.rstrip().endswith(_END_PUNCT):
            continue
        sep = paragraph[a_end:b_start]
        tight = sep.strip()
        if not tight:
            return [paragraph[:b_start].strip(), paragraph[b_start:].strip()]
        if tight.endswith(("：", ":")) and _SPEECH_VERB.search(tight):
            # 归属语跟着后半段走：前半段只留上一位说话人的台词。
            return [paragraph[:a_end].strip(), paragraph[a_end:].strip()]
        if (
            tight[-1] in "。！？"
            and len(tight) <= 12
            and _SPEECH_VERB.search(tight)
        ):
            cut = a_end + len(sep)
            return [paragraph[:cut].strip(), paragraph[cut:].strip()]
    return None


def _split_paragraph(paragraph: str) -> list[str]:
    pieces = [paragraph]
    while True:
        changed = False
        out: list[str] = []
        for piece in pieces:
            split = _split_once(piece)
            if split is None:
                out.append(piece)
            else:
                out.extend(split)
                changed = True
        pieces = out
        if not changed:
            return [p for p in pieces if p]


def split_multi_speaker_paragraphs(text: str) -> tuple[str, dict]:
    """把「两个说话人挤在同一段」拆成独立段落（纯机械，不改字）。"""
    info: dict = {
        "before_paras": 0,
        "split": 0,
        "after_paras": 0,
        "samples": [],
    }
    blocks, sep = _paragraph_blocks(text or "")
    info["before_paras"] = len(blocks)
    if not blocks:
        return text or "", info
    out: list[str] = []
    for block in blocks:
        parts = _split_paragraph(block)
        if len(parts) > 1:
            info["split"] += 1
            if len(info["samples"]) < 5:
                info["samples"].append(block[:80])
        out.extend(parts)
    info["after_paras"] = len(out)
    info["changed"] = info["split"] > 0
    return sep.join(out), info


def find_multi_speaker_paragraphs(text: str) -> list[str]:
    """只诊断：返回能安全拆开的「双人同段」样本。"""
    blocks, _sep = _paragraph_blocks(text or "")
    out: list[str] = []
    for block in blocks:
        if len(_split_paragraph(block)) > 1:
            out.append(block[:120])
    return out


def normalize_quote_style(text: str) -> tuple[str, dict]:
    """把混用的引号统一到本书主导形态（半角一律转中文弯引号）。"""
    style = detect_quote_style(text or "")
    target = style if style in ("curly", "corner") else "curly"
    got, replaced = normalize_quotes(text or "", target)
    return got, {"target": target, "replaced": replaced, "changed": bool(replaced)}


def _cn_to_int(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return None
    total = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    return total + current


def _int_to_cn(value: int) -> str:
    """1–99 转中文数字（只用于「N 个字」这种小数字）。"""
    if value <= 10:
        return _CN_NUM[value]
    if value < 20:
        return "十" + _CN_NUM[value - 10]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _CN_NUM[tens] + "十" + ("" if ones == 0 else _CN_NUM[ones])
    return str(value)


def _number_mismatches(text: str) -> list[tuple[re.Match[str], int, int, str]]:
    src = text or ""
    out: list[tuple[re.Match[str], int, int, str]] = []
    for match in _NUM_DECL_RE.finditer(src):
        declared = _cn_to_int(match.group(1))
        if not declared or declared > 100:
            continue
        window = src[max(0, match.start() - _NUM_LOOKBACK):match.start()]
        spans = list(_QUOTE_SPAN_RE.finditer(window))
        if not spans:
            continue
        inner = spans[-1].group(0)[1:-1]
        actual = hanzi_count(inner)
        if not actual or actual == declared:
            continue
        out.append((match, declared, actual, inner))
    return out


def find_number_word_mismatch(text: str) -> list[dict]:
    """诊断「那 N 个字」与前文最近一处引语的字数是否一致。

    只认带「那/这」的回指句式，并且要求前 120 字内确实有引语；没有引语
    或字样本身不是回指（如「备注写了四个字：电费再议」）都不报。
    """
    src = text or ""
    return [
        {
            "declared": declared,
            "actual": actual,
            "quote": inner[:40],
            "snippet": src[max(0, match.start() - 20):match.end() + 10],
        }
        for match, declared, actual, inner in _number_mismatches(src)
    ]


def repair_number_word_mismatch(text: str) -> tuple[str, dict]:
    """把「那七个字」改成与前文引语实际字数一致的中文数字。"""
    src = text or ""
    info: dict = {"fixed": 0, "samples": []}
    if not src:
        return src, info
    parts: list[str] = []
    cursor = 0
    for match, declared, actual, inner in _number_mismatches(src):
        parts.append(src[cursor:match.start(1)])
        parts.append(_int_to_cn(actual))
        cursor = match.end(1)
        info["fixed"] += 1
        if len(info["samples"]) < 5:
            info["samples"].append(
                f"{match.group(0)} → {match.group(0)[:-3]}{_int_to_cn(actual)}个字"
            )
    if not info["fixed"]:
        return src, info
    parts.append(src[cursor:])
    info["changed"] = True
    return "".join(parts), info



def find_tics(text: str) -> list[dict]:
    """诊断高频口头禅（只报超过建议上限的词）。"""
    src = text or ""
    out: list[dict] = []
    for word, limit in TIC_WORDS:
        count = src.count(word)
        if count > limit:
            out.append({"word": word, "count": count, "limit": limit})
    return out


def deterministic_repair(text: str) -> tuple[str, dict]:
    """跑完整确定性修复链，返回 ``(新正文, 明细)``。

    顺序：补标点 → 校正字数回指 → 拆双人同段 → 统一引号。四步都幂等，
    重复执行不会继续改字。
    """
    before = text or ""
    out, punct = repair_dialogue_punctuation(before)
    out, numbers = repair_number_word_mismatch(out)
    out, split = split_multi_speaker_paragraphs(out)
    out, quotes = normalize_quote_style(out)
    info = {
        "changed": out != before,
        "punctuation": punct,
        "numbers": numbers,
        "multi_speaker": split,
        "quotes": quotes,
    }
    return out, info
