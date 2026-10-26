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

#: **测量口径**版本：句子边界规则变了就必须 +1。
#:
#: 为什么与 `PROFILE_VERSION` 分开：那个管「档案长什么样（v1/v2 结构）」，
#: 这个管「同一段文字被**怎么量**」。2026-10-25 修过一个：分句器把闭合引号
#: 甩成独立句（真书 24% 的「句」是孤立的 `”`），句数虚增三成、平均句长压低三成，
#: 于是**老档案与运行时测量不是同一把尺**（基线 56.0 vs 实测 39.2，差 40%）。
#:
#: 它进档案指纹：口径一变，老档案自动失效并重拟 —— 不靠人记得去清缓存。
#:
#: v3（2026-10-25）：加了对白形态两个特征（对白段中位字数 / 纯台词段占比）。
#: 特征集一变，老档案里**没有**这两项，`z_scores` 会把缺失项算成 z=0 ——
#: 也就是「这两项从不偏离」。那是假的，必须让老档案整体失效重拟。
MEASUREMENT_VERSION = 3

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
#: 句子终结符 **及其后紧跟的闭合符号**。
#:
#: 中文里句末标点常在引号**内**（`…不可匹敌。”`、`「好。」`），所以闭合引号、
#: 括号必须跟着它的那一句。只按终结符切会把闭合引号甩成下一个「句」——
#: 实测（真书 990 章）：**24% 的「句」是孤立的 `”`（均长 2.2 字）**，
#: 句数被虚增三成、平均句长被压低三成，档案基线也跟着失真。
#:
#: 口径与 `app/ingestion/chunk.py` 的 `_SENT_END_RE` 对齐：同一个语料，
#: 切块与测量必须用同一套句子边界，否则两边说的不是同一件事。
_SENT_END_RE = re.compile(r"[。！？!?…]+[」』”’）)\]】》〕]*")
#: 连续汉字串。用一次 findall 而不是逐字符 match —— 百万字语料上后者慢两个数量级。
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")

#: 引号内的对白（中文网文以 “” 为主，兼容「」『』）
_QUOTE_PAIRS = (("“", "”"), ("「", "」"), ("『", "』"))

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
    "纯台词段占比",
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
    # ⚠️ 这条说明与实现的**口径不完全一致**，见下方注释。
    "纯台词段占比": "含对白的段落里「引号外几乎没有文字」的占比（越低=叙述越密）",
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
    "纯台词段占比",
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


def paragraph_spans(text: str) -> list[tuple[int, int]]:
    """按换行切自然段，返回每段在原文里的 `(start, end)` 字符跨度。

    与 `paragraphs()` 同源（同样的切法、同样的丢弃规则）——**必须一致**：
    诊断规则用它挑出「最碎的那一段」，修复闭环再用同一套切法把它定位回
    字符区间；两边切法一旦不同，修复就会改到别的段上去。
    """
    spans: list[tuple[int, int]] = []
    offset = 0
    for raw in (text or "").split("\n"):
        start = offset
        offset += len(raw) + 1  # +1 是被 split 吃掉的换行
        stripped = raw.strip()
        if not stripped:
            continue
        head = len(raw) - len(raw.lstrip())
        spans.append((start + head, start + head + len(stripped)))
    return spans


def sentences(para: str) -> list[str]:
    """把一段切成句子。终结符保留在句尾（它是文体信号本身，不能丢）。

    ## 闭合引号必须跟着它的那一句

    中文里句末标点常在引号**内**（`…不可匹敌。”`）。只按 `。！？…` 切，
    闭合引号会被甩成下一个「句」—— 真书实测 **24% 的「句」是孤立的 `”`**
    （均长 2.2 字）。后果不是「多算一句」，而是**句数虚增三成、平均句长压低三成**，
    档案基线、诊断判定、文体距离全部跟着失真。

    所以这里按「终结符 + 其后紧跟的闭合符号」整段切（与 `app/ingestion/chunk.py`
    的 `_SENT_END_RE` 同一口径）：切块与测量必须认同一个「句」的定义。

    省略号先整体归一成一个终结符，否则「……」会切出 2~6 个空句。
    """
    if not para:
        return []
    s = _ELLIPSIS_RUN.sub("…", para)
    out: list[str] = []
    start = 0
    for m in _SENT_END_RE.finditer(s):
        seg = s[start:m.end()].strip()
        if seg:
            out.append(seg)
        start = m.end()
    tail = s[start:].strip()
    if tail:
        out.append(tail)
    return out


def dialogue_char_count(text: str) -> int:
    """引号内的字符总数（对白篇幅）。跨段引号按段内配对处理。"""
    n = 0
    for open_q, close_q in _QUOTE_PAIRS:
        i = 0
        while True:
            a = text.find(open_q, i)
            if a < 0:
                break
            b = text.find(close_q, a + 1)
            if b < 0:
                break
            n += b - a - 1
            i = b + 1
    return n


#: 「纯台词段」的判定阈值：把引号内容去掉以后，剩下的非标点字符不超过这个数，
#: 就算「光秃秃一句台词」（前面没有说话人的动作、神态、语气）。
_BARE_SEG_TAIL_MAX = 6


def _strip_quoted(text: str) -> str:
    """把成对引号及其内容整段去掉，只留下引号外的字。"""
    out = text
    for open_q, close_q in _QUOTE_PAIRS:
        while open_q in out and close_q in out:
            a = out.index(open_q)
            b = out.find(close_q, a + 1)
            if b < 0:
                out = out.replace(open_q, "")
            else:
                out = out[:a] + out[b + 1:]
    return out


#: 提示语动词（双字）——「XX道」「XX说道」「XX点头道」里的那个提示语。
_SPEECH_VERBS2 = (
    "说道", "问道", "答道", "笑道", "喝道", "开口道", "沉声道", "冷声道",
    "轻声道", "叹道", "怒道", "哑声道", "低声道", "柔声道", "解释道",
    "补充道", "反问道", "笑问道", "喊道", "喃喃道", "接过话头", "接话道",
)
#: 提示语动词（单字）。
_SPEECH_VERBS1 = "道问答笑喝吼骂叹应叫唤嘟嚷哼"


def _first_quote_pos(text: str) -> int:
    idxs = [text.index(q) for q, _ in _QUOTE_PAIRS if q in text]
    return min(idxs) if idxs else -1


def _strip_speech_tag(text: str) -> str:
    """剥掉引号前的「说话人 + 提示语」，返回剩下的部分。

    「徐骁问道：」→ 「徐骁」
    「徐凤年点头道：」→ 「徐凤年点头」
    「徐骁也不恼怒，乐呵呵道：」→ 「徐骁也不恼怒，乐呵呵」
    ⇒ 剩下的若还有字，那就是**真动作/神态**。
    """
    pos = _first_quote_pos(text)
    if pos < 0:
        return ""
    pre = _strip_quoted(text[:pos]).strip("：:，。！？…、；; \t　")
    if not pre:
        return ""
    rest = pre
    for v in sorted(_SPEECH_VERBS2, key=len, reverse=True):
        rest = rest.replace(v, "")
    if len(rest) <= 12:
        for ch in _SPEECH_VERBS1:
            if ch in rest:
                rest = rest.replace(ch, "")
                break
    return rest.strip()


def _is_speech_tag_only(text: str) -> bool:
    """这一段引号外的内容**只是说话人 + 提示语**（没有动作/神态/环境）。

    提示语 = 「XX道」「XX说道」「XX点头道」「XX笑道」这类。

    ## 判据：剥掉提示语后**几乎没剩什么**

    ⚠️ 关键：**不能只看「有没有提示语动词」** ——
    「徐骁也不恼怒，乐呵呵道：」也有提示语动词，但前面还有真动作
    （「也不恼怒，乐呵呵」），它是 C 类不是 A 类。
    ⇒ 判据是「剥掉提示语后剩下的 ≤ 2 字」（只剩人名）。
    """
    rest = _strip_speech_tag(text)
    return len(rest) <= 2


def plain_dialogue_ratio(paras: Sequence[str]) -> float:
    """**A 类**占比：含引号的段里，「真的只有台词」的占多少。

    与 `dialogue_segment_form` 的「纯台词段占比」**不是同一个数** ——
    后者把「说话人+提示语」（「徐骁问道：」）也算成了叙述，
    所以它测的是「这段有多少叙述」。

    本函数把提示语**剥掉**再判 ⇒ 对应「读者看到的是不是光秃秃一句台词」。

    实测（15 本真书 / 191905 个含引号段）：两把尺在 13/15 本书上差
    16~71 个百分点且方向不一致 ⇒ **跨书比较只能用同一把尺**。
    """
    quote = 0
    plain = 0
    for p in paras:
        if not any(q in p for q, _ in _QUOTE_PAIRS):
            continue
        quote += 1
        if _is_speech_tag_only(p):
            plain += 1
    return (plain / quote) if quote else 0.0


def dialogue_segment_form(paras: Sequence[str]) -> tuple[float, float]:
    """对白形态（**逐单元**口径）：(含对白段的平均段长, 纯台词段占比)。

    「含对白段」的口径与 `对白段占比` 一致：段里出现过任一成对引号就算。
    「纯台词段」= 去掉引号内容后只剩标点/空白 —— 也就是「光秃秃一句台词」。

    ## ⚠️ 「纯台词段占比」这个特征**实际测的是什么**（2026-10-25 查清）

    它的判定是「引号**外**剩的字符 ≤ 6 字」——
    而引号外**既可能是动作/神态，也可能是说话人+提示语**
    （「徐骁问道：」「徐凤年点头道：」只有 4~5 字）。

    ⇒ 它测的其实是「**这一段有多少叙述**」，
    分界线落在「≤6 字」上，**不是**「有没有动作」。

    实测各书「引号外剩余字数」的分布**形状差别极大**：
    局中局 74% 的对白段引号外是 **0 字**，雪中只有 5%、
    而雪中有 34% 是 21 字以上（长动作叙述）。

    ⇒ 与「剥掉提示语」的 A 类占比相比（`plain_dialogue_ratio`），
    两把尺在 **13/15 本书上差 16~71 个百分点**（雪中旧 22% / 新 9%；
    斩神旧 8% / 新 80%）—— **不是谁对谁错，是分界线不同**：
    旧尺按「字数 ≤6」切，新尺按「剥掉提示语后还剩不剩动作」切。

    ⇒ **跨书比较只能用同一把尺**（分界线不同 ⇒ 切出来的比例不可比）。
    两把尺**各有其用，不能相互替换**：
    - **旧尺**（本函数）：适合做**书级特征**（进档案、进判别力验证、进提示基线）
      —— 它稳定（15 本书各自用自己的带判自己，带内单元占比 81%~90%）、
      且判别力更好（与异书的 z 中位 1.01 vs 新尺 0.41）；
    - **新尺**（`plain_dialogue_ratio`）：适合做**段级形态判断**
      ——「读者看到的是不是光秃秃一句台词」。

    「含对白段的平均段长」这个数不受影响。

    ## 分工：逐单元 vs 样本级

    这个函数给的是**逐单元**统计（平均段长 + 纯台词比例），进特征向量的是
    纯台词比例（比例类特征逐单元算得稳）。**长度不进特征向量** ——
    逐单元的平均值再对单元取平均 = 均值的均值，一个 3000 字单元里通常只有
    十来段对白，遇到超长对白段就被拉爆（真书：逐单元均值 142 字）。
    对外展示的「典型对白段长度」用 `pooled_dialogue_form()` 的**中位数**
    （真书：样本 59 字 / 全书 47 字）。

    ## 为什么单列这两个特征

    真书实测：含对白段典型 47~61 字、纯台词段只占 **21%~25%**（多数对白段是
    「动作/神态 + 台词」写在同一段）；模型写的是典型 9~26 字、纯台词段
    **52%~87%**。这是「生成比参考书碎」的主要来源，而**句长、标点密度这些
    已有特征量不出来** —— 一段 20 字的纯台词在句长分布上完全正常。

    于是把它变成两个**可验收的数字**，跟句长一样进档案、进提示词基线块：
    模型拿到的目标不再是形容词，而是「对白段典型 47 字 / 纯台词段 21%」。
    """
    quote_lens: list[int] = []
    bare = 0
    for p in paras:
        if not any(q in p for q, _ in _QUOTE_PAIRS):
            continue
        quote_lens.append(len(p))
        rest = _strip_quoted(p)
        if len(rest.strip("：:，。！？…、；; \t　")) <= _BARE_SEG_TAIL_MAX:
            bare += 1
    if not quote_lens:
        return 0.0, 0.0
    return statistics.fmean(quote_lens), bare / len(quote_lens)


def pooled_dialogue_form(texts: Iterable[str]) -> dict:
    """样本级对白形态汇总：把所有单元里的对白段**汇总起来**再统计。

    返回::

        {"n_segments": int, "seg_len_median": float, "seg_len_mean": float, "bare_ratio": float}

    ## 为什么必须汇总，而不是逐单元算再平均

    逐单元算「对白段平均字数」再对单元取平均，等于**均值的均值**：
    一个 3000 字单元里通常只有十来段对白，样本少时逐单元均值波动很大，
    遇到一个超长对白段就被拉爆。真书实测同一份语料：

    | 口径 | 平均字数 | 中位字数 |
    |---|---|---|
    | 逐单元均值 → 取平均（400 单元） | 142.2 | 112.3 |
    | 汇总全部对白段（400 单元） | 109.6 | **59.0** |
    | 汇总全部对白段（全书 1742 单元） | 80.3 | **47.0** |

    中位数稳（59 / 53 / 47），均值不稳（110 / 94 / 80）—— 所以
    **对外展示的「典型对白段长度」取汇总后的中位数**，均值只作参考。

    另一个坑：`max_units_per_text=400` 只取全书前 21%，而这本书的
    早期章节对白段更长（全书中位 47 vs 前 400 单元 59）。这是取样偏差，
    档案的其它数字（句长 67 等）也带同一个偏差 —— 要改得改取样策略，
    不能只给对白这一项打补丁。
    """
    lens: list[int] = []
    bare = 0
    for t in texts:
        for p in paragraphs(t):
            if not any(q in p for q, _ in _QUOTE_PAIRS):
                continue
            lens.append(len(p))
            rest = _strip_quoted(p)
            if len(rest.strip("：:，。！？…、；; \t　")) <= _BARE_SEG_TAIL_MAX:
                bare += 1
    if not lens:
        return {"n_segments": 0, "seg_len_median": 0.0, "seg_len_mean": 0.0, "bare_ratio": 0.0}
    return {
        "n_segments": len(lens),
        "seg_len_median": float(statistics.median(lens)),
        "seg_len_mean": float(statistics.fmean(lens)),
        "bare_ratio": bare / len(lens),
    }


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
    dlg_paras = sum(1 for p in paras if any(q in p for q, _ in _QUOTE_PAIRS))
    bare_seg_ratio = dialogue_segment_form(paras)[1]

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
        "纯台词段占比": bare_seg_ratio,
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
        # 样本级对白形态汇总（v1 路径的对应位置）
        "dialogue_form": pooled_dialogue_form(train),
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


def profile_band(profile: dict, feature: str, bucket: str | None = None) -> dict:
    """档案里某个特征**自己的分布**（mean / std / p10 / p50 / p90 / min / max）。

    v2 档案按 bucket 存（短/中/长各一套，粒度不同统计量也不同），
    v1 档案把 feat 平铺在顶层。两边都取得到；缺就返回空 dict。

    选桶口径与 `style_distance` 一致（短文本要落到 short/medium 那一套，
    拿错桶就等于拿另一把尺量）。

    **为什么要有这个入口**：凡是「拿参考书自己的分布当尺子」的地方都要它 ——
    诊断规则判「比 90% 的人类样本还碎」、改写闸门判「这段偏离到值得动一次改写」。
    各写一份就会出现「两处选桶不一样 → 同一段文字两个结论」。
    """
    buckets = profile.get("buckets") or {}
    if bucket:
        obj = buckets.get(bucket) or buckets.get("medium") or {}
    else:
        obj = buckets.get("medium") or {}
    feat = (obj or {}).get("feat") or profile.get("feat") or {}
    return feat.get(feature) or {}


def band_check(text: str, profile: dict, names: Sequence[str]) -> dict[str, dict]:
    """量一段文本在指定特征上的位置（相对档案自己的分布）。

    返回 ``{特征名: {"value": 实测值, "band": 档案该特征自己的分布, "bucket": 桶名}}``。

    ## 为什么要有这个共用入口

    凡是「拿参考书自己的分布当尺子」的地方都要它 —— 诊断规则判「比 90% 的人类
    样本还碎」、改写闸门判「这段偏离到值得动一次改写」。各写一份就会出现
    「两处选桶不一样 → 同一段文字两个结论」。

    ## 长度归一化要**除掉**（方案 C 的坑）

    `features()` 对非密度特征乘了 `exp(-|log(len/unit)|*0.3)`（让跨长度可比）。
    但档案里的分布是在 **unit 长度**上拟的（缩放 ≈ 1），而一段 1000 字的文本
    会被缩到 0.72 —— 拿它跟档案的 p10 比，会把「句长本来就够」的长句文本
    判成「比 90% 的人类样本还碎」。

    所以这里把文本这一侧的长度因子**除掉**，两边才是同一把尺：
    实测值 = `features()` 的值 ÷ 该文本的长度因子（密度类特征不缩放，不动）。

    选桶口径与 `style_distance` 一致（短文本要落到 short/medium 那一套）。
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
        bucket_name = pick_bucket(len(text), base_unit)
        bucket = profile["buckets"].get(bucket_name) or profile["buckets"].get("medium")
        if not bucket:
            return out
    else:
        bucket_name = "v1"
        bucket = profile
    unit = int(bucket.get("unit_chars") or base_unit or 3000)
    vec = features(text, unit_chars=unit)
    scale = _length_scale_factor(len(text), unit)
    feat = bucket.get("feat") or profile.get("feat") or {}
    for name in names:
        band = feat.get(name) or {}
        if not band:
            continue
        raw = float(vec.get(name) or 0.0)
        value = raw / scale if (name in NON_DENSITY_FEATURES and scale) else raw
        out[name] = {"value": value, "band": band, "bucket": bucket_name}
    return out


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
    dform = profile.get("dialogue_form") or {}
    if not feat and profile.get("version") == 2:
        medium = (profile.get("buckets") or {}).get("medium") or {}
        feat = medium.get("feat") or {}
        dform = medium.get("dialogue_form") or {}
    if not feat:
        return ""
    unit = int(
        (profile.get("_meta") or {}).get("base_unit_chars")
        or profile.get("unit_chars")
        or 0
    )

    def st(k: str) -> dict:
        return feat.get(k) or {}

    def band(key: str, scale: float = 1.0, unit: str = "", *, digits: int = 0) -> str:
        """一个**连续型**指标的「p10~p90 区间（均值做参考）」。

        为什么给区间而不是「均值±std」：±std 是**分布形状**，
        而模型要的是「落在哪一带算合格」——那正是 p10~p90
        （与 `band_check()` 判带内用的是同一对分位点，提示词说的区间 = 验收的区间）。

        **只给连续型指标**：比例型（对白占比）与稀疏计数（感叹号/省略号）
        的 p10~p90 会退化成「怎么写都合格」或「下限是 0」——
        那不是区间，是废话。它们一律走 `mean_only()`。
        """
        s = st(key)
        lo = s.get("p10")
        hi = s.get("p90")
        mean = s.get("mean")
        if lo is None or hi is None:
            return ""
        lo_n = float(lo) * scale
        hi_n = float(hi) * scale
        if abs(hi_n - lo_n) < 0.05:
            return f"{_n(lo_n, digits)}{unit}"
        out = f"{_n(lo_n, digits)}~{_n(hi_n, digits)}{unit}"
        if mean is not None:
            out += f"（均值 {_n(float(mean) * scale, digits)}）"
        return out

    def mean_only(key: str, scale: float = 1.0, unit: str = "", *, digits: int = 0) -> str:
        """比例型/稀疏型指标：**只给均值**（不给区间）。

        理由：这类指标的 p10~p90 覆盖了九成样本，给了等于没给
        （实测「对白字占比」的 p10~p90 是 2.7%~35.8%）。
        """
        s = st(key)
        mean = s.get("mean")
        if mean is None:
            return ""
        return f"{_n(float(mean) * scale, digits)}{unit}"

    lines: list[str] = []
    _sl = band("句长均值", 1.0, " 字")
    _seg = band("段均句数", 1.0, " 句", digits=1)
    # 比例型 → 只给均值（见 band() 的说明）
    _dlg = mean_only("对白字占比", 100.0, "%")
    _dlgp = mean_only("对白段占比", 100.0, "%")
    lines.append(
        f"【句长】一句 {_sl}"
        + (
            f" · 参差度 CV {_n(st('句长CV')['mean'], 2)}"
            if st("句长CV").get("mean") is not None
            else ""
        )
    )
    lines.append(
        f"【每段句数】{_seg}"
        + (
            f" · 段落长短要参差（CV {_n(st('段长CV')['mean'], 2)}）"
            if st("段长CV").get("mean") is not None
            else ""
        )
    )
    lines.append(
        f"【对白】对白占正文 {_dlg}"
        + (f"（{_dlgp} 的段落含对白）" if _dlgp else "")
    )
    # 对白形态：数字取**样本级汇总**（典型长度=中位数，纯台词占比=比例）。
    # 老档案没有 dialogue_form → 不渲染这行，别显示 0。
    if dform.get("n_segments"):
        lines.append(
            f"【对白段】典型 {_n(dform['seg_len_median'])} 字"
            f" · 纯台词段（只有台词、前面没写动作/神态）"
            f"不超过 {_n(dform['bare_ratio'] * 100)}%"
        )
    _comma = band("逗号密度")
    _period = band("句号密度")
    _quest = band("问号密度")
    # 感叹号/省略号是**稀疏计数**（多数段落 0 个）⇒ 区间下限是 0，没信息量
    _excl = mean_only("感叹号密度", digits=1)
    _ell = mean_only("省略号密度", digits=1)
    _punct = []
    for label, v in (("逗号", _comma), ("句号", _period), ("！", _excl),
                     ("？", _quest), ("…", _ell)):
        if v:
            _punct.append(f"{label} {v}")
    if _punct:
        lines.append("【标点】每千字：" + " · ".join(_punct))
    lines.append(
        f"【上限（超出即偏 AI 腔）】比喻 ≤{_n(st('比喻密度')['p90'])}/千字"
        f" · 情绪直陈 ≤{_n(st('情绪直陈密度')['p90'])}/千字"
        f" · 段尾拔高 ≤{_n(st('段尾升华率')['p90'] * 100)}% 的段落"
    )
    lines.append(
        f"【用词粒度】四字格 {_n(st('四字格密度')['mean'])}/千字"
        f" · ≤3 字短串占 {_n(st('短串占比')['mean'] * 100)}%"
    )
    head = (
        f"【文体基线】以下数字来自参考语料的统计（每单元约 {unit} 字），"
        f"不是形容词，请照着写而不是照着想象。\n"
        f"带区间的指标：**参考书自己就落在这一带**，落在里面就算合格（不必取均值）。"
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
        # 样本级对白形态汇总（不是逐单元特征，所以单独放）
        "dialogue_form": pooled_dialogue_form(train),
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
