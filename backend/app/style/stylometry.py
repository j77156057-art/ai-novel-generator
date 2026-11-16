"""文体档案（stylometry）：把「风格」从形容词变成可测的数字。

## 为什么需要它

生成侧此前只有 `project.style = "起点"` 这种**平台标签**塞进系统提示。对模型来说
「起点」几乎不含信息——它既不是句长、也不是对白比例。于是「风格像不像参考语料」
这件事既无法量化、也无法验收，只能靠人肉读。本模块把文体拆成一批**纯统计特征**，
在参考语料上拟合出档案（profile），再让每个生成段算一个到档案的**距离**。

## 三条设计纪律

1. **零 LLM、纯离线**：全部是可机检的计数与比值，没有一处调用模型。
   于是档案可复现、可回归，验收不依赖「谁读着像」。
2. **阈值必须被标定，不能拍脑袋**。做法是把语料切成**与生成段同粒度**的单元，
   一部分当训练集拟合各特征的均值/标准差，另一部分当**留出集**算出「人写的单元
   离档案有多远」的经验分布。判定生成段时，报的是它在这个分布里的**分位**，
   而不是「超过某个我随手定的阈值」。
3. **特征必须有判别力才配留下**。一个特征若连「斗破苍穹 vs 红楼梦」都分不开，
   就不可能对「AI 腔 vs 人写」有判别力。`verify_stylometry.py` 用
   「同语料留出集距离 ≪ 跨语料距离」这条可证伪的命题把无用特征筛掉。

## 粒度为什么必须对齐

标点密度、对白占比这类是**比率**（尺度无关），但它们的**估计方差**随单元长度衰减。
拿 380 万字的整本小说估出的方差，去判一个 3,000 字的生成段，会把正常波动误判成漂移。
所以 `fit_profile(unit_chars=...)` 默认取生成长度，档案里也如实记下 `unit_chars`，
`style_distance` 会检查调用方给的文本长度是否落在可比区间内并在结果里标注。

## 与「风格平台」的关系

平台标签继续留在系统提示里（它毕竟承载了用户的显式选择），本模块产出的是
**静态文体基线块**：一组具体数字。两者不冲突——前者是意图，后者是可验收的目标。
静态块的文本由 `describe()` 生成且**不随段号变化**，因此不破坏系统提示的缓存友好性。
"""
from __future__ import annotations

import bisect
import math
import re
import statistics
from typing import Iterable, Sequence

import logging

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

PROFILE_VERSION = 2

#: 档案的**测量口径**版本：同一段文字被**怎么量**（分句规则、密度分母、特征集）。
#:
#: 与 `PROFILE_VERSION` 分开：那个管「档案长什么样（v1/v2 结构）」，这个管尺子。
#: 骨架来自 main 的同名常量，但**编号从 1 起独立算**：main 的 v4 把密度分母
#: 从「全字符」换成了「纯汉字」并加了 12 项标点/分段特征，本仓库还没搬那一版
#: ⇒ 不能挂 main 的号（挂 v4 等于声称两边量的是一把尺）。
#:
#: **以后任何会改变测量结果的改动都必须 +1**，并让 `pipeline.profile_fingerprint`
#: 跟着变：老档案于是被标成「口径失配」，由用户决定是否重蒸。
MEASUREMENT_VERSION = 1

#: percentile 计算要求的最小留出单元数。
#:
#: 分位是「在留出分布中的位置」，样本太少时这个位置没有意义：
#: - n 个样本只能给出 100/n 的档位（9 个 → 每档 11.1%）
#: - 任何超过最大值的新文本直接得 100%，无论超多少
#: 实测（豆棚闲话 11.2 万字 → 9 个留出单元）：同源文本 10/20 被判 percentile=100。
#: 提到 20 是为了让分位分辨率至少到 5%，且最大值有足够样本支撑。
#: 不足时 percentile 报 None，由调用方退回绝对阈值。
_MIN_HOLDOUT_FOR_PERCENTILE = 20

#: 句子终结符（含中文全角）。省略号按整串处理，避免「……」被算成 2 个句子。
SENT_END = "。！？!?…"
#: 会在重切时被吃掉的成对符号
_ELLIPSIS_RUN = re.compile(r"[…]{2,}|\.{3,}")
#: 连续汉字串。用一次 findall 而不是逐字符 match —— 百万字语料上后者慢两个数量级。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")

#: 引号内的对白（中文网文以 “” 为主，兼容「」『』与中文单引号 ‘’）
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"), ("‘", "’"))
#: 同字符开合的 ASCII 直引号：按出现顺序两两配对（第 1、2 个是一对，3、4 个是下一对）。
#: 2026-11-13 加：第 8 段重写把弯引号规范成了直引号，旧实现把 28.7% 对白读成 0。
_QUOTE_SAME = ('"', "'")
#: 全部引号字符（剥段首段尾、判断段落是否含对白用）
_QUOTE_CHARS = "“”「」『』‘’\"'"

_METAPHOR = re.compile(r"(像|如同|仿佛|彷佛|宛如|好似|犹如)")
#: 情绪直陈（贴标签式情绪描写，AI 腔典型）
_EMOTION = re.compile(r"(眼神里|瞳孔|眸中|闪过一丝|涌上心头|皆是|满是|无比|不禁|忍不住)")
#: 段尾拔高/升华（AI 腔典型结尾）
_ELEVATE = re.compile(r"(不只是|不仅|更是|原来|终于明白|从此|意味着|他不知道|很多年后|这一切)")

#: 需要计频的标点
_PUNCT = {
    "comma": "，",
    "period": "。",
    "excl": "！",
    "quest": "？",
    "ellipsis": "…",
    "pause": "、",
    "colon": "：",
    "semicolon": "；",
}

#: 中文作者归属研究里最经典的指纹 = 高频虚词频率。这里只取最稳的几个，
#: 不铺开成十几个（会稀释其余特征）。「的」与「了」是最强的两个。
_FUNC_WORDS = "的了着过是在不就都也又很这那"

#: 特征顺序即 `FEATURES`；保持固定顺序，档案与 z 向量才对得上。
FEATURES: tuple[str, ...] = (
    "句长均值",
    "句长CV",
    "句长p90",
    "短句占比",
    "长句占比",
    "段长CV",
    "段均句数",
    "对白字占比",
    "对白段占比",
    "逗号密度",
    "句号密度",
    "感叹号密度",
    "问号密度",
    "省略号密度",
    "比喻密度",
    "情绪直陈密度",
    "段尾升华率",
    "四字格密度",
    "短串占比",
    "虚词密度",
)

#: 特征的通俗解释（面板/报告里用，避免读者对着「CV」发愣）
FEATURE_NOTES: dict[str, str] = {
    "句长均值": "一句话平均多少字",
    "句长CV": "句长参差程度（越小越整齐，AI 腔偏小）",
    "句长p90": "长句那一端有多长（分布形状，不只是均值）",
    "短句占比": "≤4 字的句子比例（节奏顿挫感）",
    "长句占比": "≥40 字的句子比例（绵长句偏好）",
    "段长CV": "段落长短参差程度（越小越像 AI 写等长段）",
    "段均句数": "一个自然段平均几句话",
    "对白字占比": "引号内的字数占正文比例",
    "对白段占比": "含对白的段落比例",
    "逗号密度": "每千字逗号数",
    "句号密度": "每千字句号数",
    "感叹号密度": "每千字感叹号数",
    "问号密度": "每千字问号数",
    "省略号密度": "每千字省略号数",
    "比喻密度": "每千字「像/如同/仿佛…」（AI 腔偏高）",
    "情绪直陈密度": "每千字贴标签式情绪词（AI 腔偏高）",
    "段尾升华率": "以总结/拔高收尾的段落比例（AI 腔偏高）",
    "四字格密度": "每千字被标点界定的四字串（成语代理指标）",
    "短串占比": "被标点界定的 ≤3 字短串比例（用词粒度代理指标）",
    "虚词密度": "每千字高频虚词（的/了/着/过/是/在…）——中文作者归属的经典指纹",
}

#: 代理指标——不是直接测量，只是结构性近似，判读时必须知道这一点
PROXY_FEATURES = frozenset({"四字格密度", "短串占比"})

#: 方案 C「features 长度归一化」——非密度类特征分组。
#: 这 11 个特征是「按字/句/段/run 统计」的值，在 1000 字 vs 6000 字段上
#: 统计波动大（CV/占比类尤其敏感），导致 z-score 系统性偏大。
#: 方案 C 对它们乘 exp(-|log(len/unit_chars)| * 0.3) 双向收敛，让跨长度可比。
NON_DENSITY_FEATURES = frozenset({
    "句长均值", "句长CV", "句长p90",
    "短句占比", "长句占比",
    "段长CV", "段均句数",
    "对白字占比", "对白段占比",
    "短串占比", "段尾升华率",
})

#: 密度类特征（per1k 已归一化）——不变
DENSITY_FEATURES = frozenset(FEATURES) - NON_DENSITY_FEATURES


def _length_scale_factor(text_chars: int, unit_chars: int) -> float:
    """按 log 长度比例缩放 features，让跨长度可比（方案 C）。

    1x → 1.0（基准）
    2x → 0.7（长文统计更稳定，缩 0.7 让短长文在同一长度基准下比较）
    0.5x → 1.3（短文统计波动大，扩 1.3 拉回基准）
    0.01x → 趋近 0（极端短文不缩放）

    unit_chars <= 0 退化返 1.0（不缩放）。
    """
    if unit_chars is None or unit_chars <= 0 or text_chars <= 0:
        return 1.0
    ratio = text_chars / unit_chars
    import math as _math
    return _math.exp(-abs(_math.log(ratio)) * 0.3)


# --------------------------------------------------------------------------- #
# 切分与计数
# --------------------------------------------------------------------------- #

def paragraphs(text: str) -> list[str]:
    """按换行切自然段，丢弃空白段。"""
    return [p.strip() for p in (text or "").split("\n") if p.strip()]


def sentences(para: str) -> list[str]:
    """把一段切成句子。终结符保留在句尾（它是文体信号本身，不能丢）。

    省略号先整体归一成一个终结符，否则「……」会切出 2~6 个空句。
    """
    if not para:
        return []
    s = _ELLIPSIS_RUN.sub("…", para)
    out: list[str] = []
    buf: list[str] = []
    for ch in s:
        buf.append(ch)
        if ch in SENT_END:
            seg = "".join(buf).strip()
            if seg:
                out.append(seg)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def split_sentences(text: str) -> list[str]:
    """蒸馏与生成**共用**的切句口径（R16 唯一真源）。

    逐字沿用 `narrative.build_deterministic_features` 的历史写法：
    在 ``。！？…`` 之后断开、句末标点保留、``strip()`` 后丢空串，且**不按段切**。

    不要改成 `sentences()` 的口径（它按段切、先归一省略号）：档案里的块级
    指标就是按这把尺算出来的，换尺会让历史档案与新的 `style_check` 不可比。
    """
    return [s.strip() for s in re.split(r"(?<=[。！？…])", text or "") if s.strip()]


#: 句长类指标的**计数口径版本**。v1 = `split_sentences` 原样计数（含纯标点片段）；
#: v2 = 只用「至少含一个汉字」的句子。换尺必须同时重算阈值，见
#: `consistency.GEN_GATE_VERSION = gen-gate-v19`。
SENTENCE_METRIC_SCOPE = "hanzi-only.v2"

#: 判断一个片段是不是「句子」的唯一标准：至少含一个汉字。
#: 为什么要这一步：`split_sentences` 每遇一个 `…` 就切一刀，中文的 `……`
#: 因此被切成两个单「…」片段；参考书每章中位 14 个这样的片段、约占「句数」
#: 的 9%，若照单全收，`短句占比`（≤4/≤5 字）量到的其实是标点碎片密度。
#: 收尾引号 `”` 同理。`split_sentences` 本身保持不动（档案块级指标由它算出，
#: 换切分口径会让历史档案与新指标不可比），只在**统计句长时**过滤。
_HANZI = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")


def cjk_len(text: str) -> int:
    """纯汉字数（标点、空格、换行不算）—— 全仓库唯一口径。

    章长统计（`style.chapter_stats`）按这个口径量「一章多少字」，
    与 `sentence_lens` 的 hanzi-only 口径同源，避免两处各数各的。
    """
    return len(_HANZI.findall(text or ""))


def sentence_lens(text: str) -> list[int]:
    """句长（含句末标点），只保留至少含一个汉字的句子。

    这是 v2 口径的唯一入口：`consistency.measure` 的 5 项句长指标、
    `generation_gate` 的 4 项句长指标、以及由它们推出的全部分位与阈值，
    都必须走这里，否则「尺子」和「阈值」会各用一套。
    """
    return [len(s) for s in split_sentences(text) if _HANZI.search(s)]


def paragraph_sentence_counts(text: str) -> list[int]:
    """每个自然段的「含汉字句数」，按**全文**切句后归属到段。

    与逐段 `sentence_lens` 的区别（v20）：上一段末尾没有句末标点时，
    逐段切会给它单独记一句、下一段再记一句，同一句话被数了两次；
    全文切句只记一次。因此这里满足
    ``sum(paragraph_sentence_counts(t)) == len(sentence_lens(t))``，
    `段均句数 × 句长均值` 才能还原 `段长均值`（三角校核的前提）。

    归属规则：句子的起始字符落在哪一段就记给哪一段；纯标点片段不计。
    """
    raw = text or ""
    starts: list[int] = []
    pos = 0
    for line in raw.split("\n"):
        if line.strip():
            starts.append(pos + len(line) - len(line.lstrip()))
        pos += len(line) + 1
    counts = [0] * len(starts)
    if not counts:
        return counts
    prev = 0
    for m in re.finditer(r"(?<=[。！？…])", raw):
        seg, seg_start = raw[prev:m.end()], prev
        prev = m.end()
        h = _HANZI.search(seg)
        if h:
            # 归属按「第一个汉字」算：句首常带着上一段的收尾引号/换行
            counts[max(0, bisect.bisect_right(starts, seg_start + h.start()) - 1)] += 1
    tail = raw[prev:]
    h = _HANZI.search(tail)
    if h:
        counts[max(0, bisect.bisect_right(starts, prev + h.start()) - 1)] += 1
    return counts


def dialogue_char_count(text: str) -> int:
    """引号内的字符总数（对白篇幅）。跨段引号按段内配对处理。

    2026-11-13：加认 ASCII 直引号（`"` / `'`）与中文单引号（‘’）。
    第 8 段的第二遍重写把中文弯引号统一换成了 ASCII 直引号，旧实现只认
    `“”/「」/『』`，于是同一段 28.7% 对白的正文被读成 0。直引号同字符开合，
    按出现顺序两两配对；落单的最后一个不计（无法确定是开口还是收尾）。
    """
    src = text or ""
    n = 0
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while True:
            a = src.find(open_q, i)
            if a < 0:
                break
            b = src.find(close_q, a + 1)
            if b < 0:
                break
            n += b - a - 1
            i = b + 1
    for q in _QUOTE_SAME:
        pos = [k for k, ch in enumerate(src) if ch == q]
        for a, b in zip(pos[0::2], pos[1::2]):
            n += b - a - 1
    return n


def strip_edge_quotes(text: str) -> str:
    """剥掉段首、段尾的引号字符（不动句子内部的引号）。

    用途：按段切句前先剥端点。`“好。”` 直接切会被 `。` 断成
    `['“好。', '”']` 两句，独立成行的短对白就数不进「对白单句段」；
    剥成 `好。` 之后正确判成 1 句。`他说，“好。”` → `他说，“好。`，
    仍然切出 2 句，不会被误判成单句段。
    """
    s = (text or "").strip()
    return s.strip(_QUOTE_CHARS) if s else ""


#: 对白里的语气词 / 句末助词（人物口吻的直接标记，不含「的/了」这类通用虚词）。
DIALOGUE_PARTICLES = "吧吗啊呢嘛哦呀哈啦呗"
#: 对白句首叹词——单独一类语气，不计入陈述句（「啊？」≠「我想想。」）。
INTERJECTIONS = frozenset("啊哎唉哦嗯嘿诶呀哈喂咦唔噢哟")
#: 对白句长只数「实字」：去掉空白与中英文标点后再数，避免逗号把 3 字句算成 4 字。
_DIALOGUE_TRIM = re.compile(r"[\s。！？…；，、：,.!?;:\"'“”‘’「」『』\-—～]+")


def dialogue_spans(text: str) -> list[str]:
    """引号内的对白内容（不含引号），与 `dialogue_char_count` 同一套配对规则。

    中文网文以 “” 为主，兼容「」『』‘’；ASCII 直引号同字符开合，按出现顺序
    两两配对（落单的最后一个不计）。
    """
    src = text or ""
    out: list[str] = []
    ranges: list[tuple[int, int]] = []
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while True:
            a = src.find(open_q, i)
            if a < 0:
                break
            b = src.find(close_q, a + 1)
            if b < 0:
                break
            out.append(src[a + 1:b])
            ranges.append((a, b))
            i = b + 1
    for q in _QUOTE_SAME:
        pos = [k for k, ch in enumerate(src) if ch == q]
        for a, b in zip(pos[0::2], pos[1::2]):
            out.append(src[a + 1:b])
            ranges.append((a, b))
    items = sorted(zip(ranges, out), key=lambda x: x[0])
    return [inner for _, inner in items]


def dialogue_sentences(text: str) -> list[str]:
    """对白句：引号内容按 `split_sentences` 再切句，去掉首尾标点后返回实字串。

    句长直接用 `len()`（即实字个数）；没有实字的片段（如单独的「……」）不计数。
    """
    out: list[str] = []
    for inner in dialogue_spans(text):
        for s in split_sentences(inner):
            core = _DIALOGUE_TRIM.sub("", s)
            if core:
                out.append(core)
    return out


def _pure_dialogue_paragraph(para: str) -> int | None:
    """若整段只有引号内容、没有任何叙述字，返回引号内实字数；否则 None。

    「只有引号内容」= 去掉全部引号段后，剩下的只有空白/标点。
    """
    rest_parts: list[str] = []
    pos = 0
    inner_chars = 0
    for a, b in _dialogue_ranges(para):
        rest_parts.append(para[pos:a])
        inner_chars += len(_DIALOGUE_TRIM.sub("", para[a + 1:b]))
        pos = b
    rest_parts.append(para[pos:])
    if inner_chars <= 0:
        return None
    if _DIALOGUE_TRIM.sub("", "".join(rest_parts)):
        return None
    return inner_chars


def _dialogue_ranges(text: str) -> list[tuple[int, int]]:
    """引号段起止（含引号本身），按出现顺序。"""
    src = text or ""
    ranges: list[tuple[int, int]] = []
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while True:
            a = src.find(open_q, i)
            if a < 0:
                break
            b = src.find(close_q, a + 1)
            if b < 0:
                break
            ranges.append((a, b + 1))
            i = b + 1
    for q in _QUOTE_SAME:
        pos = [k for k, ch in enumerate(src) if ch == q]
        for a, b in zip(pos[0::2], pos[1::2]):
            ranges.append((a, b + 1))
    return sorted(ranges)


def dialogue_profile(text: str) -> dict[str, float | int | None]:
    """对白专项画像（2026-11-13 对白校准）。

    与 `generation_gate` / 报告共用同一把尺。所有比值都是「对白句」或「段」的
    占比，密度以去空白字符数为分母（/千字），与 `features()` 的密度口径一致。

    Returns（部分）：
        对白句数 / 对白句长中位 / 对白≤4占比 / 对白5–9占比 /
        对白10–19占比 / 对白≥20占比 /
        纯对白段占比 / 极短纯对白段占比（≤4 字纯对白段 / 全部段）/
        纯对白段字数中位 / 语气词密度千字 / 含助词对白句占比 /
        对白问句占比 / 对白感叹句占比 / 对白陈述句占比 / 对白叹词起头占比 /
        消息段字数中位 / 纯消息段占比 / 消息含助词占比 / 消息平均间隔
    """
    src = text or ""
    paras = paragraphs(src)
    total = len("".join(src.split()))
    per1k = 1000.0 / total if total else 0.0
    sents = dialogue_sentences(src)
    lens = [len(s) for s in sents]
    n_sent = len(lens)

    def _share(pred) -> float | None:
        return sum(1 for x in lens if pred(x)) / n_sent if n_sent else None

    # 语气类别：用带标点的原始句尾判断，问号/感叹号必须保留。
    q = e = st = 0
    for inner in dialogue_spans(src):
        for s in split_sentences(inner):
            raw = s.strip()
            if not _DIALOGUE_TRIM.sub("", raw):
                continue
            tail = raw[-1]
            if tail in "？?":
                q += 1
            elif tail in "！!":
                e += 1
            else:
                st += 1
    interj = sum(1 for s in sents if s[0] in INTERJECTIONS)

    pure_chars: list[int] = []
    for p in paras:
        c = _pure_dialogue_paragraph(p)
        if c is not None:
            pure_chars.append(c)
    short_pure = sum(1 for c in pure_chars if c <= 4)
    particles = sum(src.count(ch) for ch in DIALOGUE_PARTICLES)

    # 消息段：含引号的自然段（群聊消息、带说话人的对白行都算）。
    msg_idx = [i for i, p in enumerate(paras) if _dialogue_ranges(p)]
    # 消息段字数 = 整段去空白字符数（含「北河散人：」这类说话人前缀），
    # 因为「谁说的、什么口吻」本身就是这段的字数来源。
    msg_lens = [len("".join(paras[i].split())) for i in msg_idx]
    msg_paras = [paras[i] for i in msg_idx]
    msg_with_particle = sum(
        1 for p in msg_paras if any(ch in p for ch in DIALOGUE_PARTICLES)
    )
    gaps: list[int] = []
    for a, b in zip(msg_idx, msg_idx[1:]):
        gaps.append(max(b - a - 1, 0))
    pure_msg = 0
    for p in msg_paras:
        c = _pure_dialogue_paragraph(p)
        if c is not None:
            pure_msg += 1

    def _med(vals: list[float]) -> float | None:
        return statistics.median(vals) if vals else None

    return {
        "对白句数": n_sent,
        "对白句长中位": _med(lens),
        "对白句长均值": round(statistics.fmean(lens), 2) if lens else None,
        "对白≤4占比": _round(_share(lambda x: x <= 4)),
        "对白5–9占比": _round(_share(lambda x: 5 <= x <= 9)),
        "对白10–19占比": _round(_share(lambda x: 10 <= x <= 19)),
        "对白≥20占比": _round(_share(lambda x: x >= 20)),
        "纯对白段占比": round(len(pure_chars) / len(paras), 3) if paras else None,
        "极短纯对白段占比": (
            round(short_pure / len(paras), 3) if paras else None
        ),
        "纯对白段字数中位": _med([float(c) for c in pure_chars]),
        "语气词密度千字": round(particles * per1k, 2),
        "含助词对白句占比": (
            round(sum(1 for s in sents if any(ch in s for ch in DIALOGUE_PARTICLES))
                  / n_sent, 3) if n_sent else None
        ),
        "对白问句占比": _round(q / n_sent) if n_sent else None,
        "对白感叹句占比": _round(e / n_sent) if n_sent else None,
        "对白陈述句占比": _round(st / n_sent) if n_sent else None,
        "对白叹词起头占比": _round(interj / n_sent) if n_sent else None,
        "消息段字数中位": _med([float(x) for x in msg_lens]),
        "纯消息段占比": (
            round(pure_msg / len(msg_paras), 3) if msg_paras else None
        ),
        "消息含助词占比": (
            round(msg_with_particle / len(msg_paras), 3) if msg_paras else None
        ),
        "消息平均间隔": (
            round(statistics.fmean(gaps), 2) if gaps else None
        ),
    }


def _round(x: float | None) -> float | None:
    return round(x, 3) if x is not None else None


def _runs(text: str) -> list[int]:
    """被非汉字字符界定的连续汉字串，返回各自长度。

    这是「四字格 / 短串」两个代理指标的底座：不用成语词典，只看**结构性**的
    汉字串长度分布。好处是零依赖、可复现；代价是它无法区分成语与恰好四字的
    普通词（如「我不知道你」），所以本模块**只把它当作分布信号**，不做单点判读。
    """
    return [len(m) for m in _CJK_RUN.findall(text)]


def _cv(values: Sequence[float]) -> float:
    """变异系数 = 标准差 / 均值。均值为 0 时返回 0（而不是 inf）。"""
    vals = [v for v in values]
    if len(vals) < 2:
        return 0.0
    m = statistics.fmean(vals)
    if m <= 0:
        return 0.0
    return statistics.pstdev(vals) / m


# --------------------------------------------------------------------------- #
# 特征向量
# --------------------------------------------------------------------------- #

def features(text: str, *, unit_chars: int = 3000) -> dict[str, float]:
    """一段文本的文体特征向量。全部是可机检的计数与比值。

    方案 C「长度归一化」(v2.15+)：对 11 个非密度类特征（按字/句/段/run 统计）
    乘 exp(-|log(len/unit_chars)| * 0.3) 双向收敛，让跨长度可比——治本
    percentile 100% 触发。12 个密度类特征（per1k 已归一）不变。

    unit_chars 默认 3000（与 generation_length 对齐）。unit_chars <= 0
    时 scale=1.0（不缩放），保证旧调用兼容。
    """
    text = text or ""
    n = len(text)
    paras = paragraphs(text)
    if n == 0 or not paras:
        return {k: 0.0 for k in FEATURES}

    per1k = 1000.0 / n

    sents: list[str] = []
    for p in paras:
        sents.extend(sentences(p))
    sent_lens = [len(s) for s in sents] or [0]

    para_lens = [len(p) for p in paras]
    sents_per_para = [len(sentences(p)) for p in paras]

    dlg_chars = dialogue_char_count(text)
    dlg_paras = sum(1 for p in paras if any(ch in p for ch in _QUOTE_CHARS))

    elevate = 0
    for p in paras:
        tail = p[-45:]
        if _ELEVATE.search(tail):
            elevate += 1

    runs = _runs(text)
    n_runs = max(len(runs), 1)

    short_sent = sum(1 for L in sent_lens if L <= 4)
    long_sent = sum(1 for L in sent_lens if L >= 40)

    vec = {
        "句长均值": statistics.fmean(sent_lens),
        "句长CV": _cv(sent_lens),
        "句长p90": _quantile(sorted(sent_lens), 0.90),
        "短句占比": short_sent / len(sent_lens),
        "长句占比": long_sent / len(sent_lens),
        "段长CV": _cv(para_lens),
        "段均句数": statistics.fmean(sents_per_para),
        "对白字占比": dlg_chars / n,
        "对白段占比": dlg_paras / len(paras),
        "逗号密度": text.count(_PUNCT["comma"]) * per1k,
        "句号密度": text.count(_PUNCT["period"]) * per1k,
        "感叹号密度": text.count(_PUNCT["excl"]) * per1k,
        "问号密度": text.count(_PUNCT["quest"]) * per1k,
        "省略号密度": text.count(_PUNCT["ellipsis"]) * per1k,
        "比喻密度": len(_METAPHOR.findall(text)) * per1k,
        "情绪直陈密度": len(_EMOTION.findall(text)) * per1k,
        "段尾升华率": elevate / len(paras),
        "四字格密度": sum(1 for r in runs if r == 4) * per1k,
        "短串占比": sum(1 for r in runs if r <= 3) / n_runs,
        "虚词密度": sum(text.count(w) for w in _FUNC_WORDS) * per1k,
    }
    # 方案 C：11 个非密度特征按 log 长度缩放
    scale = _length_scale_factor(n, unit_chars)
    for k in NON_DENSITY_FEATURES:
        if k in vec:
            vec[k] = vec[k] * scale
    return {k: float(vec[k]) for k in FEATURES}


# --------------------------------------------------------------------------- #
# 档案拟合（train / holdout）
# --------------------------------------------------------------------------- #

def cut_units(text: str, unit_chars: int, *, max_units: int | None = None) -> list[str]:
    """把长文切成 `unit_chars` 上下的单元，**尽量落在段落边界**。

    落在段落边界是必须的：硬切会把一句话拦腰截断，句长与段长统计随即失真。
    """
    paras = paragraphs(text)
    units: list[str] = []
    buf: list[str] = []
    size = 0
    truncated = False
    for p in paras:
        if size and size + len(p) + 1 > unit_chars:
            units.append("\n".join(buf))
            buf, size = [], 0
            if max_units and len(units) >= max_units:
                truncated = True
                break
        buf.append(p)
        size += len(p) + 1
    if buf and not truncated:
        units.append("\n".join(buf))
    # 过短的尾巴（< 半个单元）会让 CV、对白占比这类统计严重偏斜，单独剔掉。
    # 这一步必须走完——提前 return 会让截断路径漏掉过滤，静默混进脏样本。
    floor = max(1, unit_chars // 2)
    return [u for u in units if len(u) >= floor]


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _summarize(values: list[float]) -> dict[str, float]:
    vals = sorted(values)
    m = statistics.fmean(vals) if vals else 0.0
    sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    return {
        "mean": float(m),
        "std": float(sd),
        "p10": float(_quantile(vals, 0.10)),
        "p50": float(_quantile(vals, 0.50)),
        "p90": float(_quantile(vals, 0.90)),
        "min": float(vals[0]) if vals else 0.0,
        "max": float(vals[-1]) if vals else 0.0,
    }


def fit_profile(
    texts: Iterable[str],
    *,
    unit_chars: int = 3000,
    train_ratio: float = 0.7,
    max_units_per_text: int | None = 400,
    label: str = "",
) -> dict:
    """在参考语料上拟合文体档案。

    返回结构（可直接 JSON 落库）::

        {
          "version": 1,
          "unit_chars": 3000,
          "n_train": 62, "n_holdout": 27,
          "feat": { 特征名: {mean,std,p10,p50,p90,min,max}, ... },
          "holdout": {
             "mean_abs_z": {mean,std,p50,p90,max},   # 留出集到档案的距离分布
             "p90_abs_z":   {...},
             "unit_mean_abs_z": [ ... 每个留出单元的值 ... ],
          },
        }

    `holdout` 是最关键的一块：它给出「**人写的**单元离档案有多远」的经验分布。
    没有它，判定阈值只能拍脑袋；有了它，生成段的距离可以报成**分位**。
    """
    units: list[tuple[str, str]] = []  # (来源标签, 文本)
    for i, t in enumerate(texts):
        for u in cut_units(t, unit_chars, max_units=max_units_per_text):
            units.append((f"src{i}", u))

    if len(units) < 4:
        raise ValueError(
            f"语料太少无法拟合（切出 {len(units)} 个单元，至少需要 4 个）——"
            f"unit_chars={unit_chars} 是不是比语料还长？"
        )

    # 按**单元序号**取模切分，而不是按文本切分：一本小说只有一两个单元时，
    # 按文本切会让 holdout 为空或只剩某本书，训练/留出就不可比了。
    train = [u for i, (_, u) in enumerate(units) if i % 10 < int(train_ratio * 10)]
    holdout = [u for i, (_, u) in enumerate(units) if i % 10 >= int(train_ratio * 10)]
    if not train:
        train, holdout = units, []
    if not holdout and len(train) > 2:
        train, holdout = train[:-1], train[-1:]

    train_vecs = [features(u) for u in train]
    feat: dict[str, dict[str, float]] = {}
    for k in FEATURES:
        feat[k] = _summarize([v[k] for v in train_vecs])

    profile: dict = {
        "version": PROFILE_VERSION,
        "label": label,
        "unit_chars": int(unit_chars),
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_units": len(units),
        "feat": feat,
        "holdout": {},
    }

    if holdout:
        d = [metric_mean_abs_z(features(u), profile) for u in holdout]
        profile["holdout"] = {
            "mean_abs_z": _summarize(d),
            "unit_mean_abs_z": [round(x, 4) for x in d],
        }
        # 每个特征各自的留出波动 —— 用来判断「某特征这个 z 值算不算大」
        feat_holdout: dict[str, dict[str, float]] = {}
        hvecs = [features(u) for u in holdout]
        for k in FEATURES:
            zs = [
                abs((v[k] - feat[k]["mean"]) / feat[k]["std"]) if feat[k]["std"] > 0 else 0.0
                for v in hvecs
            ]
            feat_holdout[k] = {
                "mean": float(statistics.fmean(zs)) if zs else 0.0,
                "p90": float(_quantile(sorted(zs), 0.90)),
                "max": float(max(zs)) if zs else 0.0,
            }
        profile["holdout"]["feat_abs_z"] = feat_holdout
    return profile


# --------------------------------------------------------------------------- #
# 距离度量
# --------------------------------------------------------------------------- #

def z_scores(vec: dict[str, float], profile: dict) -> dict[str, float]:
    """逐特征 z 值。std 为 0 时给 0（该特征在语料里恒定，不构成偏离依据）。"""
    out: dict[str, float] = {}
    feat = profile.get("feat") or {}
    for k in FEATURES:
        st = feat.get(k) or {}
        sd = float(st.get("std") or 0.0)
        mean = float(st.get("mean") or 0.0)
        out[k] = (float(vec.get(k, 0.0)) - mean) / sd if sd > 0 else 0.0
    return out


def metric_mean_abs_z(vec: dict[str, float], profile: dict) -> float:
    """距离 = 各特征 |z| 的均值。

    取均值而不是平方和（欧氏）：欧氏会被单个离群特征主导（标点密度偶尔飙一次
    就把总分拉爆），而我们关心的是「整体像不像」，不是「有没有一项特别离谱」。
    单项离谱由 `worst` 单独报出来，不混进总分。
    """
    z = z_scores(vec, profile)
    return statistics.fmean([abs(v) for v in z.values()]) if z else 0.0


def style_distance(text: str, profile: dict) -> dict:
    """一段文本相对档案的距离，并给出**对照留出集的分位**。

    `percentile` 的含义：在参考语料**人写的**留出单元里，有多大比例的距离
    ≤ 本段。distance 越大 percentile 越大：100% = 比所有人类样本都更偏离；
    0% = 比所有人类样本都更接近。**这个分位才是可判读的结论**，
    `mean_abs_z` 本身没有绝对含义。

    PROFILE_VERSION=2 时（方案 B「对数粒度 profile」）—— 按 text 长度
    选最匹配的 bucket 算 distance，解决「粒度不对齐时距离系统性偏大」的
    老 bug。详见 docs/style_log_buckets_design.md。
    """
    base_unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )
    # 方案 C「长度归一化」：vec 用 text 选中的 bucket 的 unit_chars 算
    # —— text 走 long bucket → 用 6000 缩（scale=0.81）→ z 更小
    # v1 path 用 base_unit 兜底（profile.feat 是 base unit 切的）
    base_unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )
    # 方案 B 路径：v2 profile 有 buckets 字段 → 选 bucket
    if profile.get("version") == 2 and profile.get("buckets"):
        bucket_name = pick_bucket(len(text or ""), base_unit)
        bucket = profile["buckets"].get(bucket_name) or profile["buckets"].get("medium")
        if bucket is None:
            # 极端：v2 但 buckets 都空 —— 退回 medium bucket 结构
            bucket = {"unit_chars": base_unit, "feat": {}, "holdout": {}}
        # 方案 C：vec 用 bucket.unit_chars 算（fit 时也是用这值算 feat）
        vec = features(text, unit_chars=int(bucket.get("unit_chars") or base_unit or 3000))
    else:
        # v1 path
        bucket_name = "v1"
        bucket = profile
        vec = features(text, unit_chars=base_unit if base_unit > 0 else 3000)

    z = z_scores(vec, bucket)
    abs_z = {k: abs(v) for k, v in z.items()}
    total = statistics.fmean(list(abs_z.values())) if abs_z else 0.0

    ranked = sorted(abs_z.items(), key=lambda kv: kv[1], reverse=True)
    holdout = bucket.get("holdout") or {}
    hold = holdout.get("mean_abs_z") or {}
    # `unit_mean_abs_z` 是 `holdout` 的平级字段（见 `fit_profile`），不在 `mean_abs_z` 里面
    dist = holdout.get("unit_mean_abs_z") or []
    percentile: float | None = None
    if len(dist) >= _MIN_HOLDOUT_FOR_PERCENTILE:
        below = sum(1 for d in dist if d <= total)
        percentile = 100.0 * below / len(dist)
    elif dist:
        # 留出单元太少（< _MIN_HOLDOUT_FOR_PERCENTILE）：分位被粗化到几乎没有分辨率。
        # 9 个样本只能给 0/11.1/22.2/…/100 这些档，且「超过最大留出值」直接跳到 100%
        # ——实测豆棚闲话 9 个留出单元时，同源文本 10/20 被判 percentile=100。
        # 这时报分位是在骗人：不报（None），由调用方退回绝对阈值判断。
        logger.debug(
            "holdout 样本不足（%d < %d），percentile 置 None",
            len(dist), _MIN_HOLDOUT_FOR_PERCENTILE,
        )

    unit_chars = int(bucket.get("unit_chars") or 0)
    return {
        "chars": len(text or ""),
        "unit_chars": unit_chars,
        "bucket": bucket_name,  # v2 才有意义（"short"/"medium"/"long"）
        # 粒度不对齐时距离会系统性偏大或偏小，必须如实标注而不是静默给个数字
        "scale_ratio": round(len(text or "") / base_unit, 2) if base_unit else None,
        "mean_abs_z": round(total, 3),
        "max_abs_z": round(max(abs_z.values()), 3) if abs_z else 0.0,
        "percentile": None if percentile is None else round(percentile, 1),
        "z": {k: round(v, 3) for k, v in z.items()},
        "worst": [(k, round(v, 2)) for k, v in ranked[:5]],
        "holdout_ref": {
            "p50": round(float(hold.get("p50") or 0.0), 3),
            "p90": round(float(hold.get("p90") or 0.0), 3),
            "max": round(float(hold.get("max") or 0.0), 3),
        } if dist else None,
        "features": {k: round(float(vec.get(k, 0.0)), 3) for k in FEATURES},
    }


# --------------------------------------------------------------------------- #
# 确定性文字描述（进系统提示的静态块）
# --------------------------------------------------------------------------- #

def _n(x: float, digits: int = 1) -> str:
    """紧凑数字：去掉无意义的小数尾巴，避免 3.0 这种噪音。"""
    s = f"{x:.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def describe(profile: dict, *, max_features: int | None = None) -> str:
    """把档案翻成一段**确定性**文字，用于系统提示的静态文体基线块。

    刻意只写数字与目标区间，不写「生动」「细腻」这类形容词——形容词无法验收，
    而数字可以（步骤 5 的验收指标直接量同一批特征）。

    确定性意味着：同一份档案永远产出同一段文本，不随段号/时间变化，
    因此放进静态 system 块不会破坏 prompt 缓存。
    """
    # 方案 B 兼容：v2 profile 是 buckets 嵌套，主档用 medium bucket.feat
    feat = profile.get("feat") or {}
    if not feat and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        feat = medium.get("feat") or {}
    if not feat:
        return ""
    unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )

    def st(k: str) -> dict:
        return feat.get(k) or {}

    lines: list[str] = []
    lines.append(
        f"句长 {_n(st('句长均值')['mean'])}±{_n(st('句长均值')['std'])} 字"
        f"（参差度 CV {_n(st('句长CV')['mean'], 2)}）"
        f" · 每段 {_n(st('段均句数')['mean'])} 句"
        f" · 段长 CV {_n(st('段长CV')['mean'], 2)}"
    )
    lines.append(
        f"对白占正文 {_n(st('对白字占比')['mean'] * 100)}%"
        f"（{_n(st('对白段占比')['mean'] * 100)}% 的段落含对白）"
    )
    lines.append(
        "每千字标点："
        f"，{_n(st('逗号密度')['mean'])} · 。{_n(st('句号密度')['mean'])}"
        f" · ！{_n(st('感叹号密度')['mean'])} · ？{_n(st('问号密度')['mean'])}"
        f" · …{_n(st('省略号密度')['mean'])}"
    )
    lines.append(
        f"上限（超出即偏 AI 腔）：比喻 ≤{_n(st('比喻密度')['p90'])}/千字"
        f" · 情绪直陈 ≤{_n(st('情绪直陈密度')['p90'])}/千字"
        f" · 段尾拔高 ≤{_n(st('段尾升华率')['p90'] * 100)}% 的段落"
    )
    lines.append(
        f"用词粒度：四字格 {_n(st('四字格密度')['mean'])}/千字"
        f" · ≤3 字短串占 {_n(st('短串占比')['mean'] * 100)}%"
    )
    head = (
        f"【文体基线】以下数字来自参考语料的统计（每单元约 {unit} 字），"
        f"不是形容词，请照着写而不是照着想象："
    )
    return head + "\n" + "\n".join(lines)


def feature_table(profile: dict) -> list[dict]:
    """特征清单（面板/报告用）：均值、区间、留出波动、是否代理指标。

    v2 兼容：bucket 嵌套时取 medium bucket 的 feat + holdout。
    """
    feat = profile.get("feat") or {}
    hold = ((profile.get("holdout") or {}).get("feat_abs_z") or {})
    if not feat and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        feat = medium.get("feat") or {}
        hold = ((medium.get("holdout") or {}).get("feat_abs_z") or {})
    rows = []
    for k in FEATURES:
        st = feat.get(k) or {}
        h = hold.get(k) or {}
        rows.append({
            "name": k,
            "note": FEATURE_NOTES.get(k, ""),
            "mean": round(float(st.get("mean") or 0.0), 3),
            "std": round(float(st.get("std") or 0.0), 3),
            "p10": round(float(st.get("p10") or 0.0), 3),
            "p90": round(float(st.get("p90") or 0.0), 3),
            "holdout_p90_z": round(float(h.get("p90") or 0.0), 3) if h else None,
            "proxy": k in PROXY_FEATURES,
        })
    return rows


# --------------------------------------------------------------------------- #
# 方案 B「对数粒度 profile」(PROFILE_VERSION=2)
#
# 8 轮真链路冒烟 + 1 轮 probe + 并行 v2.12 实测确认：fit_profile(unit_chars=3000)
# 切 3000 字/单元统计 mean/std，但生成段 6264 字（2.09x）features 在不同长度
# 上算密度的 z 系统性偏大，触发 percentile 100%。
#
# 修法：fit 3 套 bucket（base/2 + base + base*2），style_distance 按 text
# 长度选最匹配 bucket。
# 设计：docs/style_log_buckets_design.md
# --------------------------------------------------------------------------- #

#: bucket 选档阈值（基于 text_chars / base_unit_chars 比值）。
#: < 0.6 → short，0.6-1.7 → medium，>= 1.7 → long。
#: 留 0.6-1.7 给 medium 覆盖绝大多数「与 unit_chars 粒度接近」的段。
BUCKET_THRESHOLDS = (0.6, 1.7)


def pick_bucket(text_chars: int, base_unit_chars: int) -> str:
    """按 text 长度选最匹配的 bucket 名。

    base_unit_chars 是 profile fit 时的「主档」unit_chars（即 generation_length）。
    阈值 BUCKET_THRESHOLDS = (0.6, 1.7)：
    - text_chars / base_unit_chars < 0.6 → "short"（短文）
    - 0.6 <= text_chars / base_unit_chars < 1.7 → "medium"（主档，对齐旧版）
    - text_chars / base_unit_chars >= 1.7 → "long"（长文）

    base_unit_chars <= 0 时降级返回 "medium"（最稳的兜底）。
    """
    if base_unit_chars <= 0:
        return "medium"
    ratio = text_chars / base_unit_chars
    if ratio < BUCKET_THRESHOLDS[0]:
        return "short"
    if ratio < BUCKET_THRESHOLDS[1]:
        return "medium"
    return "long"


def fit_profile_v2(
    texts: Iterable[str],
    *,
    unit_chars: int = 3000,
    train_ratio: float = 0.7,
    max_units_per_text: int | None = 400,
    label: str = "",
) -> dict:
    """对数粒度 profile fit：3 套 unit_chars（base/2 + base + base*2）。

    返回 profile schema (PROFILE_VERSION=2)：
        {
            "version": 2,
            "label": "...",
            "_meta": {"base_unit_chars": 3000, "schema": "log_buckets_v1"},
            "buckets": {
                "short":  {unit_chars: 1500, n_train, n_holdout, n_units, feat, holdout},
                "medium": {unit_chars: 3000, ...},
                "long":   {unit_chars: 6000, ...},
            }
        }

    每个 bucket 单独跑 fit 的核心逻辑（cut_units + train/holdout + feat + holdout 分布）。
    短语料切不出某 bucket（< 4 单元）时跳过该 bucket，不报错。

    退化：若 medium 切不出（极端短），保留至少 medium 桶用 short 配置。
    """
    base = int(unit_chars)
    bucket_units: dict[str, int] = {
        "short": max(500, base // 2),
        "medium": base,
        "long": base * 2,
    }

    # 切 3 套单元
    bucket_texts: dict[str, list[str]] = {b: [] for b in bucket_units}
    for t in texts:
        for bname, uc in bucket_units.items():
            for u in cut_units(t, uc, max_units=max_units_per_text):
                bucket_texts[bname].append(u)

    profile: dict = {
        "version": PROFILE_VERSION,
        "label": label,
        "_meta": {
            "base_unit_chars": base,
            "schema": "log_buckets_v1",
        },
        "buckets": {},
    }

    for bname, units in bucket_texts.items():
        if len(units) < 4:
            # 短语料切不出该 bucket —— 跳过
            continue
        bucket_profile = _fit_single_bucket(
            units,
            unit_chars=bucket_units[bname],
            train_ratio=0.5,  # 50/50 切分：bucket 单元数少时留出也能保留足够样本
        )
        profile["buckets"][bname] = bucket_profile

    # 退化：medium 都没切出来，用 short 配置当 medium
    if "medium" not in profile["buckets"] and "short" in profile["buckets"]:
        profile["buckets"]["medium"] = dict(profile["buckets"]["short"])
        profile["buckets"]["medium"]["unit_chars"] = base

    return profile


def _fit_single_bucket(
    units: list[str],
    *,
    unit_chars: int,
    train_ratio: float,
) -> dict:
    """单个 bucket 的 fit（与 fit_profile v1 同款逻辑，返回扁平 dict）。"""
    train = [u for i, u in enumerate(units) if i % 10 < int(train_ratio * 10)]
    holdout = [u for i, u in enumerate(units) if i % 10 >= int(train_ratio * 10)]
    if not train:
        train, holdout = units, []
    if not holdout and len(train) > 2:
        train, holdout = train[:-1], train[-1:]

    train_vecs = [features(u, unit_chars=unit_chars) for u in train]
    feat: dict[str, dict[str, float]] = {}
    for k in FEATURES:
        feat[k] = _summarize([v[k] for v in train_vecs])

    bucket: dict = {
        "version": PROFILE_VERSION,
        "unit_chars": int(unit_chars),
        "n_train": len(train),
        "n_holdout": len(holdout),
        "n_units": len(units),
        "feat": feat,
        "holdout": {},
    }

    if holdout:
        d = [metric_mean_abs_z(features(u, unit_chars=unit_chars), bucket) for u in holdout]
        bucket["holdout"] = {
            "mean_abs_z": _summarize(d),
            "unit_mean_abs_z": [round(x, 4) for x in d],
        }
        feat_holdout: dict[str, dict[str, float]] = {}
        hvecs = [features(u, unit_chars=unit_chars) for u in holdout]
        for k in FEATURES:
            zs = [
                abs((v[k] - feat[k]["mean"]) / feat[k]["std"])
                if feat[k]["std"] > 0 else 0.0
                for v in hvecs
            ]
            feat_holdout[k] = {
                "mean": float(statistics.fmean(zs)) if zs else 0.0,
                "p90": float(_quantile(sorted(zs), 0.90)),
                "max": float(max(zs)) if zs else 0.0,
            }
        bucket["holdout"]["feat_abs_z"] = feat_holdout
    return bucket


def band_check(text: str, profile: dict, names: Sequence[str]) -> dict[str, dict]:
    """量一段文本在指定特征上的位置（相对档案自己的分布）。

    返回 ``{特征名: {"value": 实测值, "band": 档案该特征自己的分布, "bucket": 桶名}}``。

    凡是「拿参考书自己的分布当尺子」的地方都要它 —— 诊断规则判「比 90% 的人类
    样本还碎」、改写闸门判「这段偏离到值得动一次改写」。各写一份就会出现
    「两处选桶不一样 ⇒ 同一段文字两个结论」。

    长度归一化要**除掉**：`features()` 对非密度特征乘了
    `exp(-|log(len/unit)|*0.3)`（让跨长度可比），但档案里的分布是在 unit 长度
    上拟的（缩放 ≈ 1）。一段 1000 字的文本会被缩到 0.72，拿它跟档案的 p10 比，
    会把「句长本来就够」的长句文本判成「比 90% 的人类样本还碎」。
    所以这里把文本这一侧的长度因子除掉，两边才是同一把尺。
    """
    out: dict[str, dict] = {}
    if not profile or not text or not text.strip():
        return out
    base_unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )
    if profile.get("version") == 2 and profile.get("buckets"):
        bucket_name = pick_bucket(cjk_len(text), base_unit)
        bucket = profile["buckets"].get(bucket_name) or profile["buckets"].get("medium")
        if not bucket:
            return out
    else:
        bucket_name = "v1"
        bucket = profile
    unit = int(bucket.get("unit_chars") or base_unit or 3000)
    vec = features(text, unit_chars=unit)
    # 长度因子也用**纯汉字数**（与 `features()` 内部一致）——
    # 否则「同一段文本只改排版」会让归一化后的读数漂。
    scale = _length_scale_factor(cjk_len(text), unit)
    feat = bucket.get("feat") or profile.get("feat") or {}
    for name in names:
        band = feat.get(name) or {}
        if not band:
            continue
        raw = float(vec.get(name) or 0.0)
        value = raw / scale if (name in NON_DENSITY_FEATURES and scale) else raw
        out[name] = {"value": value, "band": band, "bucket": bucket_name}
    return out
