"""Agent 辅助语料清洗 —— **确定性**检测层（错字 / 断字 / 残句）。

## 这个模块回答什么问题

第 8 步的 `app/ingestion/clean.py` 只覆盖**固定模式**的脏文本（盗版站 SEO
水印，靠位置分布判据）。但真实语料里的脏不止那一类：

- **乱码 / 替换符**：编码转换失败留下的 `\ufffd`、非法控制符；
- **编码残留**：GBK 被按 UTF-8 硬解后留下的一串「非常见汉字」（部首、私用区、
  CJK 兼容/扩展区孤立字），或 `锟斤拷` / `ï»¿` 这类 mojibake；
- **断字 / 残句**：段落被异常换行从句子中间切断、单字成段、连续超短段落；
- **可疑错字**：相邻重复字（「他他」「的的」），以及全文只出现 1–2 次、
  且字形生僻（GB2312 无法编码）的孤立字。

这些判据里，**哪些能靠确定性规则做、哪些必须交给模型**是本模块的核心取舍：

| 判据 | 能否确定性下结论 | 是否交给模型复核 |
|---|---|---|
| `replacement_char` | 能（`\ufffd` 出现即脏） | 是（可顺带修上下文） |
| `illegal_control_char` | 能 | 是 |
| `encoding_residue` | 能（区段 + mojibake 标记） | 是 |
| `broken_line_cut` | **弱**（换行是否异常需看语境） | 是（模型判断是否该并回） |
| `duplicate_adjacent_char` | 能（限定在「本不该重复」的常用字集内） | 是 |
| `single_char_paragraph` | 弱（网文里「杀。」式独字段是正当风格） | **否 —— 仅提示** |
| `ultra_short_paragraph_run` | 弱 | **否 —— 仅提示** |
| `rare_char_low_frequency` | 弱（可能就是人名/专有名词/异体字） | **否 —— 仅提示** |

最后三行是刻意的**划线**：单字成段、连续短段、低频生僻字都可能是作者风格或
专有名词，**不该交给模型自动改**（模型很容易把「人名里的生僻字」当成错字）。
它们只产出 issue（提示，带证据），不产出 `suggested_edit`，更不会变成提案。

## 纪律

- **零第三方依赖、零模型**：只用标准库。同一输入两次扫描逐字节一致（可离线复现）。
- **只读**：不写任何业务表；本模块只返回 `TextFinding` 列表。
- **不复制既有判据**：水印检测（`app/corpus_health.py`）不在这里重写；本模块
  与它互补——它管「固定模式水印」，本模块管「语义/逐字级脏文本」。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# ====================================================================
# 取值域常量
# ====================================================================

#: severity 取值（与 `app.agents.diagnosis.ALLOWED_SEVERITIES` 同域）。
ALLOWED_SEVERITIES: tuple[str, ...] = ("info", "warn", "error")

# —— 判据名（每条 finding 必带，是溯源信息）——
RULE_REPLACEMENT_CHAR = "replacement_char"
RULE_ILLEGAL_CONTROL = "illegal_control_char"
RULE_ENCODING_RESIDUE = "encoding_residue"
RULE_BROKEN_LINE = "broken_line_cut"
RULE_SINGLE_CHAR_PARA = "single_char_paragraph"
RULE_ULTRA_SHORT_RUN = "ultra_short_paragraph_run"
RULE_DUP_ADJACENT = "duplicate_adjacent_char"
RULE_RARE_CHAR = "rare_char_low_frequency"

#: 全部判据（稳定顺序；扫描输出按此归组）。
ALL_RULES: tuple[str, ...] = (
    RULE_REPLACEMENT_CHAR,
    RULE_ILLEGAL_CONTROL,
    RULE_ENCODING_RESIDUE,
    RULE_BROKEN_LINE,
    RULE_SINGLE_CHAR_PARA,
    RULE_ULTRA_SHORT_RUN,
    RULE_DUP_ADJACENT,
    RULE_RARE_CHAR,
)

#: **可以安全自动修正**的判据（有确定性、无歧义的修正：删替换符、合并重复字）。
RULE_FIXABLE: frozenset[str] = frozenset({
    RULE_REPLACEMENT_CHAR,
    RULE_ILLEGAL_CONTROL,
    RULE_DUP_ADJACENT,
})

#: 允许交给模型复核的判据（= 有语料上下文时可判断的脏）。**不含**
#: 单字成段 / 连续短段 / 低频生僻字 —— 它们是提示类，见模块文档的划线表。
MODEL_ELIGIBLE_RULES: frozenset[str] = frozenset({
    RULE_REPLACEMENT_CHAR,
    RULE_ILLEGAL_CONTROL,
    RULE_ENCODING_RESIDUE,
    RULE_BROKEN_LINE,
    RULE_DUP_ADJACENT,
})

#: 仅提示、不建议自动改的判据（保留给上层做「不要自动改」的显式判断）。
HINT_ONLY_RULES: frozenset[str] = frozenset(
    set(ALL_RULES) - MODEL_ELIGIBLE_RULES
)

#: 每条 finding 的 fragment 展示上限（防爆响应体）。
MAX_FRAGMENT = 80

#: 断字判据的最小行长：太短的行（标题、独字段）不按「句子被切断」处理。
MIN_BROKEN_LINE_LEN = 8

#: 连续超短段落的判定：连续多少行「去掉空白后 <= MAX_SHORT_PARA_LEN 字」算异常。
ULTRA_SHORT_RUN_LEN = 3
MAX_SHORT_PARA_LEN = 2

#: 低频异常字：全文出现次数 <= 该值，且 GB2312 无法编码 → 候选。
RARE_MAX_OCCURRENCES = 2

#: 低频异常字检测的最小语料体量（字符）：体量太小频率统计无意义。
RARE_MIN_CORPUS_CHARS = 500

#: 低频异常字单次最多报几条（防响应体膨胀；完整清单可再扫）。
RARE_MAX_FINDINGS = 30

# ====================================================================
# 正则 / 字符集
# ====================================================================

#: U+FFFD 替换符（编码转换失败时被塞进来的那个「�」）。
_REPLACEMENT = "\ufffd"

#: 允许出现的控制符（换行/制表/回车）；其余 Cc 类控制符一律可疑。
_CONTROL_ALLOWED = frozenset({"\t", "\n", "\r"})

#: 连续汉字串（含扩展 A / 兼容区）——只在这些串内部枚举，避免跨标点噪声。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

#: 相邻重复汉字（2 次及以上）。
_DUP_RE = re.compile(r"([\u4e00-\u9fff\u3400-\u4dbf])\1+")

#: 句子终止/收尾标点：行尾出现这些 → 该行不是「被切断的半句」。
_LINE_TERMINAL = set("。！？…!?；;，,、：:「」『』“”\"'）)】》〉·—~")

#: 对话 / 新段起手符：下一行以这些开头 → 是正常换段，不是断字。
_LINE_LEAD_OK = set("“”「『\"'（(【《〈")

#: 章节标题起手（「第三章」这类）；下一行像标题就不判断字。
_CHAPTER_HEAD = re.compile(r"^第[0-9０-９一二三四五六七八九十百千零两]{1,8}[章节回卷篇部]")

#: GBK→UTF-8 类 mojibake 的**高置信标记**（多字符，避免单字母误伤）。
_MOJIBAKE_MARKERS: tuple[str, ...] = (
    "锟斤拷", "烫烫烫", "屯屯屯",
    "ï»¿", "â€", "Ã¢", "Ã©", "Ã¨", "Ã¯", "Ã³", "Ã±",
    "Â°", "Â·", "Â»", "Â«", "Â¡", "Â¿",
)

#: 非常见 Unicode 区段 → (起, 止, 名称, severity)。
#: PUA / 部首 / CJK 扩展 B+ 基本只可能是编码事故（warn）；兼容区与扩展 A
#: 也可能出现在正当的异体/古字里，故降为 info（仅提示）。
_UNCOMMON_RANGES: tuple[tuple[int, int, str, str], ...] = (
    (0x2E80, 0x2EFF, "CJK 部首补充", "warn"),
    (0x2F00, 0x2FDF, "康熙部首", "warn"),
    (0xE000, 0xF8FF, "私用区(PUA)", "warn"),
    (0xF900, 0xFAFF, "CJK 兼容表意文字", "info"),
    (0x3400, 0x4DBF, "CJK 扩展 A", "info"),
    (0x20000, 0x2FFFF, "CJK 扩展 B+", "warn"),
)

#: 「本不该连续重复」的常用字集：这些字连写两次（「他他」「的的」「是是」）
#: 几乎必然是错字。**刻意只收这一小撮**——中文里大量词天然叠字
#: （看看/慢慢/好好/天天/人人…），把叠字一律报错会产生成片的假阳。
_DUP_WATCH: frozenset[str] = frozenset(
    "的了是他她它我你您们在有和与及就都也而但却又再很太最不没把被让给对"
    "从向往到于因所为所以之其此该这那哪并则若即且或者们吧呢吗呀哦嗯"
)


# ====================================================================
# 数据结构
# ====================================================================


@dataclass(frozen=True)
class TextFinding:
    """一条确定性检测结论。

    字段与派单的验收口径一一对应：
    - `chunk_id` / `novel_id`：命中位置所属的分片与语料（定位）；
    - `start` / `end`：在**该分片文本内**的字符区间（可用 `text[start:end]` 复核）；
    - `fragment`：可疑片段（截断展示）；
    - `rule`：判据名（溯源，必填）；
    - `severity`：info | warn | error；
    - `detail`：人可读说明（含复核提示）；
    - `confidence`：确定性判据的把握度（0–1；**不是**概率，用于排序与门槛）。
    """

    chunk_id: int | None
    novel_id: int | None
    start: int
    end: int
    fragment: str
    rule: str
    severity: str
    detail: str
    confidence: float

    def __post_init__(self) -> None:
        if self.rule not in ALL_RULES:
            raise ValueError(
                f"finding.rule 必须是 {ALL_RULES} 之一，收到 {self.rule!r}"
            )
        if self.severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"finding.severity 必须是 {ALLOWED_SEVERITIES} 之一，收到 {self.severity!r}"
            )
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"非法字符区间 [{self.start}, {self.end})")

    @property
    def is_fixable(self) -> bool:
        """该判据是否有确定性、无歧义的修正。"""
        return self.rule in RULE_FIXABLE

    @property
    def is_model_eligible(self) -> bool:
        """该判据是否可交给模型复核（否则只提示）。"""
        return self.rule in MODEL_ELIGIBLE_RULES

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "novel_id": self.novel_id,
            "start": self.start,
            "end": self.end,
            "fragment": self.fragment,
            "rule": self.rule,
            "severity": self.severity,
            "detail": self.detail,
            "confidence": round(float(self.confidence), 4),
        }


def _mk(
    chunk_id: int | None,
    novel_id: int | None,
    start: int,
    end: int,
    text: str,
    rule: str,
    severity: str,
    detail: str,
    confidence: float,
) -> TextFinding:
    """构造 finding，fragment 取原文子串并截断。"""
    frag = text[start:end].replace("\n", "\\n")
    return TextFinding(
        chunk_id=chunk_id,
        novel_id=novel_id,
        start=start,
        end=end,
        fragment=frag[:MAX_FRAGMENT],
        rule=rule,
        severity=severity,
        detail=detail,
        confidence=confidence,
    )


# ====================================================================
# 单条判据实现
# ====================================================================


def detect_replacement_chars(
    text: str, chunk_id: int | None = None, novel_id: int | None = None
) -> list[TextFinding]:
    """乱码替换符 `\\ufffd`：成段出现即编码转换失败的直接证据（error）。

    按**连续段**归并成一条（一段 `����` 只报一次），位置区间覆盖整段。
    """
    out: list[TextFinding] = []
    if _REPLACEMENT not in text:
        return out
    i = 0
    n = len(text)
    while i < n:
        if text[i] == _REPLACEMENT:
            j = i
            while j < n and text[j] == _REPLACEMENT:
                j += 1
            out.append(_mk(
                chunk_id, novel_id, i, j, text, RULE_REPLACEMENT_CHAR, "error",
                f"检出 {j - i} 个替换符 U+FFFD（编码转换失败的残留），该处正文已不可读",
                0.95,
            ))
            i = j
        else:
            i += 1
    return out


def detect_control_chars(
    text: str, chunk_id: int | None = None, novel_id: int | None = None
) -> list[TextFinding]:
    """非法控制符（Cc 类里除 \\t \\n \\r 之外）：多为二进制/编码事故（warn）。"""
    out: list[TextFinding] = []
    for idx, ch in enumerate(text):
        if ch in _CONTROL_ALLOWED:
            continue
        if unicodedata.category(ch) == "Cc":
            out.append(_mk(
                chunk_id, novel_id, idx, idx + 1, text, RULE_ILLEGAL_CONTROL, "warn",
                f"检出非法控制符 U+{ord(ch):04X}，正常正文不应出现",
                0.8,
            ))
    return out


def detect_encoding_residue(
    text: str, chunk_id: int | None = None, novel_id: int | None = None
) -> list[TextFinding]:
    """编码残留：GBK 按 UTF-8 硬解留下的 mojibake 标记 / 非常见区段孤立字。"""
    out: list[TextFinding] = []

    # ① 高置信 mojibake 标记（多字符字面量，直接字符串搜）。
    for marker in _MOJIBAKE_MARKERS:
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx < 0:
                break
            out.append(_mk(
                chunk_id, novel_id, idx, idx + len(marker), text,
                RULE_ENCODING_RESIDUE, "error",
                f"检出编码残留标记「{marker}」（典型 GBK/UTF-8 错配或 BOM 被当正文）",
                0.9,
            ))
            start = idx + len(marker)

    # ② 非常见 Unicode 区段的连续串（部首 / PUA / 扩展区）。
    for lo, hi, name, severity in _UNCOMMON_RANGES:
        i = 0
        n = len(text)
        while i < n:
            if lo <= ord(text[i]) <= hi:
                j = i
                while j < n and lo <= ord(text[j]) <= hi:
                    j += 1
                out.append(_mk(
                    chunk_id, novel_id, i, j, text,
                    RULE_ENCODING_RESIDUE, severity,
                    f"检出 {j - i} 个「{name}」字符（U+{ord(text[i]):04X} 起）——"
                    f"{'几乎必为编码事故' if severity == 'warn' else '可能是异体/古字，也可能是编码残留'}",
                    0.75 if severity == "warn" else 0.4,
                ))
                i = j
            else:
                i += 1
    return out


def detect_broken_lines(
    text: str, chunk_id: int | None = None, novel_id: int | None = None
) -> list[TextFinding]:
    """断字 / 残句：段落被硬换行从句子中间切断（warn），以及单字/超短段（info）。

    断字的判据要**故意保守**（换行在正常文本里极其常见，判据松一点就会假阳
    一片）。只有当同时满足下列全部条件才报：

    - 当前行（去空白后）长度 ≥ `MIN_BROKEN_LINE_LEN`，且行尾是一个**汉字**
      （既不是标点、也不是数字/字母）——即「话说到一半被切断」；
    - 下一行非空，且以**汉字**起手（不是对话引号 / 括号 / 章节标题起手）；
    - 下一行长度 ≥ `MIN_BROKEN_LINE_LEN`（短行更像标题行尾，不判断字）。
    """
    out: list[TextFinding] = []
    lines = text.split("\n")
    # 预计算每行的 (起始偏移, 去空白后内容)
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1  # +1 for '\n'

    # ① 断字：相邻两行
    for i in range(len(lines) - 1):
        cur_raw = lines[i]
        cur = cur_raw.strip()
        nxt = lines[i + 1].strip()
        if len(cur) < MIN_BROKEN_LINE_LEN or not nxt:
            continue
        if len(nxt) < MIN_BROKEN_LINE_LEN:
            continue
        last = cur[-1]
        if not _is_cjk(last) or last in _LINE_TERMINAL:
            continue
        first = nxt[0]
        if first in _LINE_LEAD_OK or not _is_cjk(first):
            continue
        if _CHAPTER_HEAD.match(nxt):
            continue
        end = offsets[i] + len(cur_raw.rstrip())
        out.append(_mk(
            chunk_id, novel_id, end - 1, end, text, RULE_BROKEN_LINE, "warn",
            f"疑似断字：上一行以「{last}」硬结尾、未收标点，下一行直接续写"
            f"（「…{cur[-6:]}」/「{nxt[:6]}…」）",
            0.5,
        ))

    # ② 单字成段（仅提示）
    for i, ln in enumerate(lines):
        s = ln.strip()
        if len(s) == 1 and _is_cjk(s):
            out.append(_mk(
                chunk_id, novel_id, offsets[i] + (len(ln) - len(ln.lstrip())),
                offsets[i] + (len(ln) - len(ln.lstrip())) + 1,
                text, RULE_SINGLE_CHAR_PARA, "info",
                f"单字成段「{s}」——也可能是作者的强调式风格，仅提示，不建议自动改",
                0.3,
            ))

    # ③ 连续超短段落（仅提示）
    run_start = 0
    run_len = 0
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and len(s) <= MAX_SHORT_PARA_LEN:
            if run_len == 0:
                run_start = i
            run_len += 1
        else:
            if run_len >= ULTRA_SHORT_RUN_LEN:
                out.append(_mk(
                    chunk_id, novel_id, offsets[run_start], offsets[run_start] + 1,
                    text, RULE_ULTRA_SHORT_RUN, "info",
                    f"连续 {run_len} 段长度 ≤{MAX_SHORT_PARA_LEN} 字——可能是切分/换行事故，"
                    f"也可能是刻意排版，仅提示",
                    0.3,
                ))
            run_len = 0
    if run_len >= ULTRA_SHORT_RUN_LEN:
        out.append(_mk(
            chunk_id, novel_id, offsets[run_start], offsets[run_start] + 1,
            text, RULE_ULTRA_SHORT_RUN, "info",
            f"连续 {run_len} 段长度 ≤{MAX_SHORT_PARA_LEN} 字——可能是切分/换行事故，"
            f"也可能是刻意排版，仅提示",
            0.3,
        ))
    return out


def detect_duplicate_adjacent(
    text: str, chunk_id: int | None = None, novel_id: int | None = None
) -> list[TextFinding]:
    """相邻重复字（warn）：仅报「本不该重复」的常用字连写（「他他」「的的」）。

    限定在 `_DUP_WATCH` 字集内，避免把「看看」「慢慢」「天天」这类正当叠字报成错字。
    """
    out: list[TextFinding] = []
    for m in _DUP_RE.finditer(text):
        ch = m.group(1)
        if ch not in _DUP_WATCH:
            continue
        out.append(_mk(
            chunk_id, novel_id, m.start(), m.end(), text,
            RULE_DUP_ADJACENT, "warn",
            f"相邻重复字「{m.group(0)}」（「{ch}」连写 {m.end() - m.start()} 次）——"
            f"该字通常不叠写，疑似错字/输入重复",
            0.6,
        ))
    return out


def detect_rare_low_frequency(
    chunks: Sequence[tuple[int | None, int | None, str]],
    *,
    max_occurrences: int = RARE_MAX_OCCURRENCES,
    min_corpus_chars: int = RARE_MIN_CORPUS_CHARS,
    max_findings: int = RARE_MAX_FINDINGS,
) -> list[TextFinding]:
    """低频异常字（info，仅提示）：**在同一部语料内**统计。

    `chunks` 是 `[(chunk_id, novel_id, text), ...]`。判据：某汉字在全部给定分片
    里出现次数 ≤ `max_occurrences`，且 **GB2312 无法编码**（= 字形生僻：异体字、
    古字、被误转的编码位）。把出现位置定位回具体分片与偏移。

    **只提示、不建议自动改**——这类字极可能就是人名/专有名词/书名里的生僻字，
    自动改等于篡改专有名词。confidence 固定 0.3。
    """
    counter: Counter[str] = Counter()
    for _, _, t in chunks:
        if not t:
            continue
        for m in _CJK_RUN.finditer(t):
            counter.update(m.group(0))
    if sum(counter.values()) < min_corpus_chars:
        return []

    candidates = [
        ch for ch, n in counter.items()
        if n <= max_occurrences and _is_obscure_cjk(ch)
    ]
    candidates.sort(key=lambda ch: (counter[ch], ord(ch)))

    out: list[TextFinding] = []
    for ch in candidates[:max_findings]:
        for cid, nid, t in chunks:
            idx = (t or "").find(ch)
            if idx >= 0:
                out.append(_mk(
                    cid, nid, idx, idx + 1, t,
                    RULE_RARE_CHAR, "info",
                    f"低频生僻字「{ch}」（全语料出现 {counter[ch]} 次、GB2312 无法编码）——"
                    f"可能是错字/异体字，也可能是人名或专有名词，仅提示，不建议自动改",
                    0.3,
                ))
                break
    return out


# ====================================================================
# 字符工具
# ====================================================================


def _is_cjk(ch: str) -> bool:
    """是否汉字（基本区 / 扩展 A）。"""
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF)


def _is_obscure_cjk(ch: str) -> bool:
    """汉字且 GB2312 无法编码 → 生僻（异体/古字/编码残留）。

    GB2312 覆盖 6763 个**常用简体**汉字；一个汉字编不进 GB2312，说明它至少
    不在常用集里。这是不引入频率表（新依赖）前提下、可离线复现的「生僻」代理判据。
    """
    if not _is_cjk(ch):
        return False
    # 扩展 A 区一律视为生僻（GB2312 必不含）。
    if 0x3400 <= ord(ch) <= 0x4DBF:
        return True
    try:
        ch.encode("gb2312")
        return False
    except UnicodeEncodeError:
        return True


#: 确定性修正的把握度（用于 suggested_edit.confidence 与门槛）。
_FIX_CONFIDENCE: dict[str, float] = {
    RULE_REPLACEMENT_CHAR: 0.9,
    RULE_ILLEGAL_CONTROL: 0.7,
    RULE_DUP_ADJACENT: 0.5,
}


def deterministic_fix(text: str, rules_hit: Iterable[str]) -> str | None:
    """对命中的**可安全修正**判据做确定性修正；无改动返回 `None`。

    只处理 `RULE_FIXABLE`：
    - 删替换符；
    - 删非法控制符；
    - 合并「本不该重复」的相邻重复字。

    其余判据（编码残留 / 断字 / 单字段 / 低频字）**不做**确定性修正 ——
    它们的正确改法依赖语境，必须交人复核。返回 `None` 表示「没有安全的自动改法」。
    """
    rules = set(rules_hit)
    fixed = text
    if RULE_REPLACEMENT_CHAR in rules:
        fixed = fixed.replace(_REPLACEMENT, "")
    if RULE_ILLEGAL_CONTROL in rules:
        fixed = "".join(
            c for c in fixed
            if c in _CONTROL_ALLOWED or unicodedata.category(c) != "Cc"
        )
    if RULE_DUP_ADJACENT in rules:
        fixed = _DUP_RE.sub(
            lambda m: m.group(1) if m.group(1) in _DUP_WATCH else m.group(0),
            fixed,
        )
    return fixed if fixed != text else None


def fix_confidence(rules_hit: Iterable[str]) -> float:
    """确定性修正的把握度（取命中判据里最高的那个）。"""
    values = [_FIX_CONFIDENCE[r] for r in set(rules_hit) if r in _FIX_CONFIDENCE]
    return max(values) if values else 0.0


# ====================================================================
# 聚合入口
# ====================================================================


def scan_chunk(
    text: str, *, chunk_id: int | None = None, novel_id: int | None = None
) -> list[TextFinding]:
    """扫描单个分片（不含语料级判据）。确定性、纯函数。"""
    if not text:
        return []
    findings: list[TextFinding] = []
    findings += detect_replacement_chars(text, chunk_id, novel_id)
    findings += detect_control_chars(text, chunk_id, novel_id)
    findings += detect_encoding_residue(text, chunk_id, novel_id)
    findings += detect_broken_lines(text, chunk_id, novel_id)
    findings += detect_duplicate_adjacent(text, chunk_id, novel_id)
    return findings


def scan_corpus(
    chunks: Sequence[tuple[int | None, int | None, str]],
    *,
    include_rare: bool = True,
) -> list[TextFinding]:
    """扫描一组分片：逐分片判据 + 语料级低频异常字。

    输出顺序确定（先按分片顺序的各判据，再低频异常字），同一输入两次结果一致。
    `chunks` = `[(chunk_id, novel_id, text), ...]`。
    """
    findings: list[TextFinding] = []
    for cid, nid, text in chunks:
        findings += scan_chunk(text, chunk_id=cid, novel_id=nid)
    if include_rare:
        findings += detect_rare_low_frequency(chunks)
    return findings


def summarize(
    pieces: Sequence[str],
    *,
    sample_cap: int = 200,
    include_rare: bool = True,
) -> dict[str, Any]:
    """入库链路的轻量摘要：抽样扫描，只统计「待复核可疑点数量」。

    入库**不调模型**（要快、可离线复现），只跑本模块的确定性扫描，把结果计数
    写进 `raw_novels.clean_report.text_clean`，让用户在助手页按需触发
    `POST /api/assistant/clean-corpus`。为控制入库耗时，默认只扫前 `sample_cap`
    个分片并**显式标注是否抽样**（不静默少算）。
    """
    total = len(pieces)
    sample = list(pieces[:sample_cap]) if sample_cap and sample_cap > 0 else list(pieces)
    findings = scan_corpus(
        [(None, None, t) for t in sample], include_rare=include_rare
    )
    by_rule = Counter(f.rule for f in findings)
    by_severity = Counter(f.severity for f in findings)
    return {
        "scanned_chunks": len(sample),
        "total_chunks": total,
        "sampled": len(sample) < total,
        "pending_review_count": len(findings),
        "by_rule": dict(sorted(by_rule.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "note": (
            ("抽样" if len(sample) < total else "全量")
            + "确定性扫描（不含模型复核）；"
            "要复核修正请调 POST /api/assistant/clean-corpus"
        ),
    }


# ====================================================================
# 自检：常量相容性（import 时 O(1)，失败即 fail loud）
# ====================================================================

assert RULE_FIXABLE <= set(ALL_RULES)
assert MODEL_ELIGIBLE_RULES <= set(ALL_RULES)
assert HINT_ONLY_RULES == set(ALL_RULES) - MODEL_ELIGIBLE_RULES
assert set(_FIX_CONFIDENCE) <= RULE_FIXABLE
