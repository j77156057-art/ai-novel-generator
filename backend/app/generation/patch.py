# -*- coding: utf-8 -*-
"""局部指令 → 句子索引补丁：程序定范围，模型只写替换文本。

## 两级分工

**Tier A（范围词由程序解析）**：指令里出现位置锚点（段尾 / 结尾 / 最后一句 /
段首 / 开头 / 第一句 / 第 N 句）时，``resolve_scope`` 直接把词映射成编号区间，
把目标句逐句标出来交给模型。模型只回替换文本；即使它回了编号，编号也必须与
程序算的一致，否则 ``index_mismatch`` 失败关闭。**改哪一句不再由模型决定。**

**Tier B（含糊指令）**：``某处`` / ``此处`` / ``这一句`` 这类无法从指令本身
定位的说法，``resolve_scope`` 返回 None，仍按上一版行为由模型给编号。

## 多句范围的规则（刻意取最小解释）

- ``段尾 / 结尾 / 最后一句`` → **只取最后一句**；``段首 / 开头 / 第一句`` →
  只取第一句；``第 N 句`` → 只取第 N 句。
- 理由：这些词本身不带 N。「段尾」如果默认取「最后若干句」，N 无界，很容易
  滑向整段——这正是要修的缺陷。最小、可验证、可回滚的解释就是 1 句；用户要
  多句时写明确区间（``第 82 句到第 84 句``），由 ``resolve_scope`` 解析成多句
  跨度并继续校验 ``from <= to``、范围内、不覆盖全文。
- 因此 Tier A 只有「显式区间」这一条多句路径，不存在「默认取 N 句」。

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
``span_whole_segment``       编号范围覆盖全文
``replacement_missing``      缺 replacement 字段
``empty_patch``              replacement 为空
``replacement_whole_segment`` 长度与句数双判据都指向「整段」
``replacement_too_long``     相对所选范围明显过长
``no_change``                改写结果与原文该范围完全相同
``unparsable``               看起来是 JSON 但解析不了
"""
from __future__ import annotations

import json
import re

from app.generation.scope_guard import (
    HEAD_SCOPE_PATTERNS,
    NTH_SENTENCE_PATTERN,
    SENTENCE_NUM,
    TAIL_SCOPE_PATTERNS,
)

REPLACEMENT_SLACK = 60
REPLACEMENT_RATIO = 3
#: 「replacement 是整段」的长度判据：达到全文长度的 80%。
WHOLE_SEGMENT_LENGTH_RATIO = 0.8
#: 「replacement 是整段」的句数判据：达到全句数的 80%。
WHOLE_SEGMENT_SENTENCE_RATIO = 0.8

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_SENTENCE_END = "。！？!?"
_SENTENCE_CLOSERS = "”’\"'」』）)】"
_SENTENCE_RE = re.compile(
    rf"[^{_SENTENCE_END}]*[{_SENTENCE_END}]+[{_SENTENCE_CLOSERS}]*"
)

# —— Tier A：范围词 → 编号 ——
_TAIL_RE = re.compile("|".join(TAIL_SCOPE_PATTERNS))
_HEAD_RE = re.compile("|".join(HEAD_SCOPE_PATTERNS))
_SINGLE_NTH_RE = re.compile(NTH_SENTENCE_PATTERN)
#: 显式多句区间：第 N 句到第 M 句（到/至/破折号/波浪号均可）。
_RANGE_RE = re.compile(
    rf"第\s*({SENTENCE_NUM})\s*句\s*(?:到|至|—|–|-|~|～)\s*第?\s*({SENTENCE_NUM})\s*句"
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
    from_index: int, to_index: int, total: int, keyword: str
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
        "source": "program",
        "total_sentences": total,
    }


def resolve_scope(instruction: str, before_text: str) -> dict | None:
    """Tier A：把范围词解析成编号区间；无法定位返回 None（交给 Tier B）。

    解析顺序：显式区间 → 段尾 → 段首 → 第 N 句。词表来自
    ``scope_guard``（``TAIL_SCOPE_PATTERNS`` / ``HEAD_SCOPE_PATTERNS`` /
    ``NTH_SENTENCE_PATTERN``），不另造一套。
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


def _range_label(scope: dict) -> str:
    fi, ti = scope["from_index"], scope["to_index"]
    return f"第 {fi} 句" if fi == ti else f"第 {fi} 句到第 {ti} 句"


def build_patch_user_prompt(
    before_text: str, instruction: str, scope: dict | None = None
) -> str:
    spans = split_sentences(before_text)
    if scope is None:
        numbered = _render_numbered(spans)
        return (
            "下面是一段已定稿的正文，已按句末标点切成编号句子。请做**局部改写**："
            "只改写【本次要求】指定的连续若干句，其余句子一个字都不要改。\n\n"
            f"【本次要求】\n{(instruction or '').strip()}\n\n"
            f"【编号原文】（共 {len(spans)} 句）\n{numbered}\n\n"
            "【输出格式（必须严格遵守）】\n"
            "只输出一个 JSON 对象，不要代码块、不要解释、不要任何前后缀文字：\n"
            '{"from_index": 起始句编号, "to_index": 结束句编号, '
            '"replacement": "改写后的这几句"}\n'
            f"- from_index <= to_index，且都在 1 到 {len(spans)} 之间；"
            "范围不能覆盖全文；\n"
            "- replacement 只写该编号范围内的句子改写结果，不要带编号，"
            "不含范围外的内容；\n"
            "- 长度与原文那几句接近，不得输出整段正文，不得顺便压缩或扩写其他句子。"
        )

    numbered = _render_numbered(spans, scope)
    label = _range_label(scope)
    start, end = scope["from_index"], scope["to_index"]
    target_lines = "\n".join(
        f"{i}. {spans[i - 1]['text']}" for i in range(start, end + 1)
    )
    return (
        "下面是一段已定稿的正文，已按句末标点切成编号句子。请做**局部改写**。\n\n"
        f"【本次要求】\n{(instruction or '').strip()}\n\n"
        f"【目标范围（由程序按指令解析，不可更改）】\n"
        f"{label}（共 {end - start + 1} 句）：\n{target_lines}\n\n"
        f"【编号原文】（共 {len(spans)} 句；>> 标记的是本次唯一允许改写的句子）\n"
        f"{numbered}\n\n"
        "【输出格式（必须严格遵守）】\n"
        "只输出改写后的句子文本：不要编号、不要解释、不要代码块。\n"
        f"不要输出 {label} 以外的任何句子，不要输出整段正文。\n"
        "如果输出 JSON，只能是 "
        f'{{"from_index": {start}, "to_index": {end}, "replacement": "..."}}，'
        "编号必须与目标范围一致，否则本次改写作废。"
    )


def _clean_json_text(reply: str) -> str:
    text = (reply or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _load_payload(reply: str) -> dict:
    raw = _clean_json_text(reply)
    try:
        payload = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 模型返回不可控，统一转补丁错误
        raise PatchError("unparsable", "模型返回不是可解析的 JSON。") from exc
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception as exc:  # noqa: BLE001
            raise PatchError("unparsable", "补丁 JSON 是字符串，里面也不是对象。") from exc
    if not isinstance(payload, dict):
        raise PatchError("unparsable", "补丁 JSON 不是对象。")
    return payload


def _as_index(value, name: str) -> int:
    if isinstance(value, bool):
        raise PatchError("index_invalid", f"{name} 不是整数。")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise PatchError("index_invalid", f"{name} 不是整数。")


def _parse_indices(payload: dict, total: int) -> tuple[int, int]:
    from_value = payload.get("from_index", payload.get("index",
                   payload.get("start")))
    to_value = payload.get("to_index", payload.get("index", payload.get("end")))
    if from_value is None or to_value is None:
        raise PatchError(
            "index_missing",
            "补丁缺少 from_index/to_index（或 index）：模型没有给出要改哪几句。",
        )
    from_index = _as_index(from_value, "from_index")
    to_index = _as_index(to_value, "to_index")
    if not (1 <= from_index <= total and 1 <= to_index <= total):
        raise PatchError(
            "index_out_of_range",
            f"编号越界：from_index={from_index}, to_index={to_index}，"
            f"有效范围 1..{total}。",
        )
    if from_index > to_index:
        raise PatchError(
            "index_reversed",
            f"from_index={from_index} 大于 to_index={to_index}。",
        )
    if from_index == 1 and to_index == total:
        raise PatchError(
            "span_whole_segment",
            f"编号范围 1..{total} 覆盖全文：这不是局部改写。",
        )
    return from_index, to_index


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


def _tier_a_replacement(reply: str, scope: dict) -> str:
    """Tier A 的返回解析：接受纯替换文本，或带编号的 JSON（编号须一致）。"""
    raw = (reply or "").strip()
    if not raw:
        raise PatchError("empty_patch", "模型返回为空。")
    text = raw
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise PatchError("empty_patch", "模型返回为空。")

    if not text.startswith("{"):
        return text
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
    return _parse_replacement(payload)


def parse_patch_reply(
    reply: str, before_text: str, scope: dict | None = None
) -> tuple[str, dict]:
    """解析模型返回并替换，返回 ``(new_text, meta)``。

    ``scope`` 非 None 时为 Tier A：编号由程序给定，只从返回里取替换文本；
    为 None 时沿用 Tier B（模型给编号）。

    所有无法安全落地的情况都抛 :class:`PatchError`；调用方应失败并留痕，
    不得回退整段重写。
    """
    spans = split_sentences(before_text)
    if scope is None:
        payload = _load_payload(reply)
        from_index, to_index = _parse_indices(payload, len(spans))
        replacement = _parse_replacement(payload)
        scope_source = "model"
        scope_keyword = None
    else:
        from_index = scope["from_index"]
        to_index = scope["to_index"]
        replacement = _tier_a_replacement(reply, scope)
        scope_source = "program"
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
        "scope_source": scope_source,
        "scope_keyword": scope_keyword,
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
