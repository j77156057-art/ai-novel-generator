"""通用 txt 编码探测：把任意网文 txt 字节解成正确的中文文本。

## 为什么需要

`novels.py` / `load_corpus.py` / `parse_txt.py` 三处曾**硬编码** `utf-8`，
导致用户放在 `Downloads/` 的三本真实网文一本都导不进来：

    《大奉打更人》            8.9 MB  GBK
    《斗破苍穹》             11.3 MB  GB18030
    《豪门宠婚，爵少你别闹》   2.9 MB  GBK

## 探测顺序与「可读性校验」

候选编码按优先级依次尝试：``utf-8-sig`` → ``utf-8`` → ``gb18030``。
其中 ``gb18030`` 是 GBK / GB2312 的**超集**，能覆盖国内网文 txt 的绝大多数情况，
所以不需要再单列 gbk / gb2312（把它们排在 gb18030 后面是多余的）。

**关键：不能只试「能不能解码」就收工。** 用错误编码去解字节流，可能「解得出」
但结果全是乱码 —— 例如把 UTF-16 当 utf-8 解（NUL 字节是合法 utf-8），
或把某些 GBK 字节流当 gb18030 解出满屏生僻字。因此每个候选结果都要过一遍
**中文可读性校验**（CJK 占比 / 替换符占比 / 控制符占比），通过才接受。

## 失败即报错，不静默

全部候选都失败时抛 :class:`EncodingDetectionError`，消息里带**文件名**与
**每个候选的失败原因**，绝不静默返回空串 —— 静默的空串会让「导入成功但语料为空」
这种故障极难排查。
"""
from __future__ import annotations

import codecs
import os
import string

#: 候选编码，**按优先级**排列：先剥 BOM 的 utf-8-sig，再纯 utf-8，最后 gb18030 兜底。
CANDIDATE_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030")

#: 替换符（U+FFFD）——解码出错时被塞进来的字符。
_REPLACEMENT_CHAR = "\ufffd"

#: 允许出现的控制符：制表/换行/回车（正文排版必需）。
_ALLOWED_CONTROL = frozenset({"\t", "\n", "\r"})

#: 可读性阈值（在**非空白字符**上计算占比）。
_MIN_CJK_RATIO = 0.25          # 中文语料：汉字应占绝对多数
_MIN_ASCII_LETTER_RATIO = 0.50  # 纯英文语料：字母应占多数
_MAX_REPLACEMENT_RATIO = 0.005  # 替换符占比上限（0.5%）
_MAX_CONTROL_RATIO = 0.002      # 非法控制符占比上限（0.2%）


class EncodingDetectionError(ValueError):
    """所有候选编码都解不出可读文本时抛出。

    消息里包含具体**文件名**与**每个候选的失败原因**，便于人一眼定位。
    """


def _is_cjk(ch: str) -> bool:
    """是否为汉字（含基本区 U+4E00–U+9FFF 与扩展 A U+3400–U+4DBF）。"""
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def _is_disallowed_control(ch: str) -> bool:
    """是否为「不该出现在正文里」的控制符（NUL / C0 / DEL / C1）。"""
    if ch in _ALLOWED_CONTROL:
        return False
    o = ord(ch)
    return o < 0x20 or 0x7F <= o <= 0x9F


def analyze_text(text: str) -> dict:
    """对一段解码结果做中文可读性体检，返回各占比与是否接受的判定。

    Returns:
        ``{"chars","nonws","cjk_ratio","ascii_letter_ratio","replacement_ratio",
        "control_ratio","readable","reason"}``。占比均在**非空白字符**上计算
        （替换符例外：在全部字符上算，因为它本身就是「异常的噪声字符」）。
    """
    n = len(text)
    if n == 0:
        return {
            "chars": 0, "nonws": 0, "cjk_ratio": 0.0, "ascii_letter_ratio": 0.0,
            "replacement_ratio": 0.0, "control_ratio": 0.0,
            "readable": False, "reason": "空文本",
        }

    nonws = [c for c in text if not c.isspace()]
    n_nonws = len(nonws)
    cjk = sum(1 for c in nonws if _is_cjk(c))
    letters = sum(1 for c in nonws if c.isascii() and c in string.ascii_letters)
    replacement = text.count(_REPLACEMENT_CHAR)
    control = sum(1 for c in text if _is_disallowed_control(c))

    cjk_ratio = cjk / n_nonws if n_nonws else 0.0
    letter_ratio = letters / n_nonws if n_nonws else 0.0
    repl_ratio = replacement / n
    ctrl_ratio = control / n

    if repl_ratio > _MAX_REPLACEMENT_RATIO:
        reason = f"替换符占比 {repl_ratio:.4f} 过高（>{_MAX_REPLACEMENT_RATIO}）"
        readable = False
    elif ctrl_ratio > _MAX_CONTROL_RATIO:
        reason = f"非法控制符占比 {ctrl_ratio:.4f} 过高（>{_MAX_CONTROL_RATIO}）"
        readable = False
    elif cjk_ratio >= _MIN_CJK_RATIO or letter_ratio >= _MIN_ASCII_LETTER_RATIO:
        reason = "可读"
        readable = True
    else:
        reason = (
            f"既非中文（CJK {cjk_ratio:.3f} < {_MIN_CJK_RATIO}）"
            f"也非英文（字母 {letter_ratio:.3f} < {_MIN_ASCII_LETTER_RATIO}）"
        )
        readable = False

    return {
        "chars": n,
        "nonws": n_nonws,
        "cjk_ratio": round(cjk_ratio, 4),
        "ascii_letter_ratio": round(letter_ratio, 4),
        "replacement_ratio": round(repl_ratio, 6),
        "control_ratio": round(ctrl_ratio, 6),
        "readable": readable,
        "reason": reason,
    }


def decode_text(raw: bytes, filename: str | None = None) -> tuple[str, str]:
    """把原始字节解成文本，返回 ``(text, encoding)``。

    依次尝试 :data:`CANDIDATE_ENCODINGS`，并对每个候选结果做可读性校验；
    第一个「解码成功且可读」的候选即为结果。

    Args:
        raw: 文件的原始字节。
        filename: 可选文件名，仅用于异常消息，便于人定位是哪本坏了。

    Returns:
        ``(解码后的文本, 命中编码名)``。

    Raises:
        EncodingDetectionError: 所有候选都不可用；消息含文件名与各候选失败原因。
    """
    name = filename or "<bytes>"
    if not raw or not raw.strip():
        # 空文件不算错误：返回空串 + 默认编码，交由上层决定如何处理。
        return "", "utf-8"

    has_bom = raw.startswith(codecs.BOM_UTF8)
    attempts: list[str] = []
    for enc in CANDIDATE_ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as exc:
            attempts.append(f"{enc}: 解码失败（{type(exc).__name__}）")
            continue
        verdict = analyze_text(text)
        if verdict["readable"]:
            # 报告**实际**编码：utf-8-sig 也能解无 BOM 的 utf-8（只是不剥 BOM），
            # 此时如实报成 "utf-8" 更准确，避免「明明没 BOM 却说 utf-8-sig」的误导。
            label = "utf-8" if (enc == "utf-8-sig" and not has_bom) else enc
            return text, label
        attempts.append(f"{enc}: 可读性不达标（{verdict['reason']}）")

    detail = "；".join(attempts)
    raise EncodingDetectionError(
        f"无法识别 {name!r} 的文本编码（已尝试 {', '.join(CANDIDATE_ENCODINGS)}）：{detail}"
    )


def read_text_file(path: str) -> tuple[str, str]:
    """读文件并按 :func:`decode_text` 探测编码，返回 ``(text, encoding)``。"""
    with open(path, "rb") as f:
        raw = f.read()
    return decode_text(raw, filename=os.path.basename(path))
