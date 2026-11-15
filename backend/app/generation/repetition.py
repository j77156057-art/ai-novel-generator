"""单段复读/退化检测（生成闸门的判据层，纯函数、零依赖、只读）。

## 这个模块解决什么

`formulaic.py` 看的是**跨段**的套路（M1 跨段 n-gram / M2 句首 / M3 句长 /
M4 摘要同质）；`engine._detect_ai_flavor` / `_should_rewrite` 看的是**用词**层面的
AI 腔。本模块补的是第三种、也是 2026-11-12 诊断里真正出问题的那一种：

> **单个段落内部，同一批句子以模板填充的方式循环，直到文本被写满或截断。**

它的判定对象是「**这一段自己**是否已经退化到不该落库」，与参考语料无关，
所以不需要基线、不发 LLM、不读库。

## 口径来源（必须与诊断脚本一致）

2026-11-12 的只读诊断用 `metrics_all.py` 扫了工作区全部 `.db`（284 行 / 77 段
去重文本），判定函数 `final_table.py::is_deg`。本模块**逐字复刻**那套口径：

- 断句：`re.split(r"(?<=[。！？])|\\n+", text)`，逐句 `strip()` 后丢弃空串。
  这个口径会把句末引号留在**下一句**（例如 `”那个身影问道。`），是诊断时
  真实使用的行为，**不要「顺手修好」**——修了就不是同一套数字了。
- 5 项指标：`top_sentence_repeat` / `repeated_sentence_coverage` /
  `dup5` / `dup20` / `ends_complete`，定义见 `measure_repetition`。
- 判定：`(top_sentence_repeat >= 10 and repeated_sentence_coverage >= 0.20)
  or dup20 >= 0.30`，即 `final_table.py::is_deg` 原样。

注意：`metrics.py`（诊断期另一个脚本）在「全段没有 。！？」时会退化为按行切分；
`metrics_all.py`（主表数据的真实来源）**没有**这个分支。本模块采用
`metrics_all.py` 口径——主表数字都出自它，口径必须跟数据对齐。

## 阈值是怎么来的（不是拍脑袋）

在两库 77 段去重文本上实测（`backend/novel_dev.db` 5 段 + 工作区其余库，
含 6 段已确认退化的 smoke 历史库）：

| 阈值 | 依据 |
|---|---|
| `MAX_TOP_SENTENCE_REPEAT = 10` + `MAX_REPEATED_SENTENCE_COVERAGE = 0.20`（**AND**） | 64 段 ≥200 字的正常文本里，`top_sentence_repeat` 最大到 **100**（满篇「他说。」这类短句口头禅），单看它会大面积误报；但它们的 `repeated_sentence_coverage` 最高只有 **0.075**。反过来单看覆盖率又会误伤长段里的合理呼应。两条**同时**越线才判退化，正好把「短句口头禅」挡在门外。 |
| `MAX_DUP20 = 0.30`（**OR** 分支） | 20 字跨度的重复只在大段复读时出现：正常文本 `dup20` 最大 **0.031**，与 0.30 差约 10 倍。OR 分支兜住「重复句种类多、单句不够长导致 top_n 上不去，但整体仍在大段复读」的形态（实测 `smoke_v223`：`top_n=4` 但 `rep_cov=0.297 / dup20=0.302`）。 |
| `dup5` **只报告、不判定** | 正常文本 `dup5` 最高 0.347，与退化段 0.68 有重叠，判别力不足；它是给人看的佐证，不是判据。 |
| `MIN_CHARS = 200` | 与 `formulaic.MIN_SEGMENT_CHARS` 同源：短段的 n-gram 宇宙太小，重复率天然虚高，此时**不判定**而不是判 ok。 |

阈值敏感性（同一批 77 段文本）：`top_n ∈ {8,10,12}` × `rep_cov ∈ {0.20,0.25,0.30}`
共 9 组组合，命中的**都是同一批 7 段**，说明判据处在一个稳定平台，取哪一组都不
改变结论——不是踩着某一条线挑出来的。

## 误报/漏报的实测情况（诚实记录）

- 主库 5 段：4 段正常判 `ok`、gen4 判 `degenerate`，**0 误报 0 漏报**。
  最接近误报的是 gen1（`top_n=9`，距 10 只差 1），但它的
  `rep_cov=0.053`、`dup20=0.000`，AND 结构把它挡住了——这也正是不能用 OR 的理由。
- 全库 77 段去重文本：命中 7 段，全部属于此前已确认的退化组（gen4 + 6 段历史
  smoke 库），64 段正常文本**无命中**。这 64 段没有逐段人工通读，因此只能证明
  「没有触发既有判据」，不能证明「绝对没有退化」。
- 已知漏报形态：**整段只有几句短句高频重复、但重复字数占比很低**（例如一句
  10 字的反应句出现 100 次、占正文 15%）。AND 结构会放过它——这是为压制
  「短句口头禅」误报付出的自觉代价，不是实现疏漏。

## 与生成链路的关系

本模块**不含任何处置动作**：不写库、不改文本、不决定要不要重生成。
处置在调用点（`engine.py` 的 P1/P2/P3）：标记 + 跳过 KG 抽取，不拒绝落库；
落库列与接口契约见文档《复读闸门插入点说明》。

## 2026-11-12 追加的两条判据（只标记、不拦截）

### A. 篇幅不足（`analyze_length`）

对象是**新生成整段**的字数相对**项目字数预算**（`projects.generation_length`）
的比例；`< 80%` 判 `short`。与 `scope_guard.detect_length_drop` **不是一回事**：
后者比较「同一段改写前后的原文」，适用于局部改写；本判据比较「正文 vs 预算」，
适用于整段生成。局部改写路径传 `is_rewrite=True`，本判据返回
`not_applicable`，跌幅仍由 `length_drop` 单独报告——两者不合并、不互相覆盖。
预算缺失（老项目没设）返回 `undetected`，**不得**当成 `ok`。
80% 与重写提示词里的「80%–120%」同源：提示词说应该写到 80% 以上，这里验实际。

### B. 梗概式推进 / 罗列式推进（`analyze_summary_style`）

可测量代理：一段文字如果**大量用「告诉/得知/明白/决定/意识到/发现/怀疑…」
把事件压成结论**（转述密度高），又**没有把读者放进现场**（场景要素少或对白少），
还出现**「找到/见到/拜访/联系/询问/走访 + 2~3 字人名」的连续会面**，就是梗概。
四路信号（均为固定词表/正则，可逐字复现）：

| 信号 | 阈值 | 750 字目标段实测 | 正常文本实测 |
|---|---|---|---|
| S1 转述动词密度 | `>= 12/千字` | **20.0** | 其余 59 段最大 5.36（p50 0.9） |
| S2 场景要素密度 | `< 4/千字` | 6.67（**未触发**） | p50 5.03；60 段中 21 段 <4 |
| S3 对白占比 | `< 5%` | **3.7%** | p50 22.5%（p1s1 1.2%） |
| S4 会面动词+人名 | `>= 2 次` | **3 次** | 60 段中除目标段外全部 ≤1 次 |

判定式：`(S1 或 S4) 且 (S2 或 S3)`。先要求「推进方式」证据（S1/S4），再要求
「展示缺失」（S2/S3），避免把「只是没对白/场景词少」的普通叙述误判成梗概。
阈值来源：

- S1 的 12 = 正常文本最大值的 2.24 倍（5.36 → 12），目标段 20 的 60%；
- S4 的 2：目标段 3 次，其余 59 段非退化文本 0 次（1 次留给正常的单次会面）；
- S3 的 5%：正常文本 p50 22.5%，最低 1.2%（p1s1），目标 3.7%——单独不判别，
  只与 S2 组成「展示缺失」；
- S2 的 4：60 段正常文本中 21 段 <4，说明**单看它几乎没有判别力**，所以它
  只作为 S3 的替代项，且整条规则必须先满足 S1/S4。

误报实测（2026-11-12，只读）：工作区 66 段去重历史正文中，6 段已被复读判据
判退化（排除），其余 **60 段里只有目标 750 字段命中，59 段正常文本 0 命中**。
主库 p1 三段、p5 第二段、旧 p5s1（1413 字）全部 `ok`；2273 字旧循环稿
`repetition=degenerate` 不变、`summary_style=ok`（0/4）。

已知漏报形态：**用「他去了 A，又去了 B」式的短句罗列、但会面动词不在词表里**
（如「安排」「吩咐」「拨通」），或通篇转述却恰好保留了 5% 以上对白。词表是
显式常量，扩词等于改判据，必须在提交里附新的误报实测。
"""


from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# 阈值：常量是「判据」，不是「调参旋钮」；取值依据见模块 docstring 的实测表。
# ─────────────────────────────────────────────────────────────────────────────

#: 低于此字数不判定（短文本重复率虚高，与 formulaic.MIN_SEGMENT_CHARS 同源）。
MIN_CHARS = 200

#: 单一整句出现次数的上限。越过它**还不够**，必须同时满足覆盖率条件。
MAX_TOP_SENTENCE_REPEAT = 10

#: 重复句（出现 ≥2 次的句子）覆盖的正文比例上限。与上一条是 AND 关系。
MAX_REPEATED_SENTENCE_COVERAGE = 0.20

#: 20-gram 重复率上限。这是 OR 分支：单独越线即可判退化。
MAX_DUP20 = 0.30

#: 循环窗判定：后缀内「不同句子数 / 句子数」不超过此值视为模板循环。
#: 0.15 = 后缀里 ≥85% 的句子是重复句——「模板循环」而不是「有点重复」的强度。
#: 取 0.15 而非更松的 0.25：在 gen4 上，0.15 定位到 935 字处（人工标注的循环
#: 起点是 1008 字，多含 4 句过渡句），而 0.25 会定位到 782 字、把 226 字正常
#: 叙事也算进循环，说出「后 1491 字都是循环」这种与事实不符的话。
#: 本值只影响**解释文案**，不影响判定。
LOOP_MAX_UNIQUE_RATIO = 0.15

#: 循环窗最少句数。句子太少时「循环」可能只是正常的重复修辞。
LOOP_MIN_SENTENCES = 12

#: 5-gram 阶数（只报告，不参与判定）。
DUP5_N = 5

#: 20-gram 阶数（判据之一）。
DUP20_N = 20

#: 对外报告的浮点位数。与诊断表（`metrics_all.py` 的 `round(x, 3)`）一致；
#: 判定也基于取整后的值，否则会出现「表里显示 0.300、判定却说没过线」的对不上账。
ROUND_DIGITS = 3

#: 句末标点集合：出现在末尾即视为「结尾完整」。
_COMPLETE_ENDINGS = "。！？…”』」"

#: 断句正则——**与诊断脚本逐字一致**，改动即等于换口径（见模块 docstring）。
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])|\n+")

#: 判定结果的三种取值。
VERDICT_OK = "ok"
VERDICT_DEGENERATE = "degenerate"
VERDICT_SKIPPED_SHORT = "skipped_short"


# ─── 判据 A：篇幅不足（正文 vs 项目字数预算，不是 vs 改写前原文）──────────────

#: 低于预算的这个比例判 `short`。与重写提示词的 80%–120% 区间同源。
MIN_LENGTH_RATIO = 0.80

LENGTH_VERDICT_OK = "ok"
LENGTH_VERDICT_SHORT = "short"
LENGTH_VERDICT_NOT_APPLICABLE = "not_applicable"


# ─── 判据 B：梗概式推进（词表/正则固定，阈值依据见模块 docstring）─────────────

#: 低于此字数不判文体（句子太少，句均长与密度的方差过大）。
SUMMARY_STYLE_MIN_CHARS = 300

#: S1：转述/总结动词密度下限（每千字）。正常文本实测最大 5.36。
SUMMARY_VERB_MIN_PER_1K = 12.0

#: S2：场景要素密度上限（每千字）。正常文本 p50 为 5.03，单看无判别力，
#: 只作为 S3 的替代项，且必须与 S1/S4 同时成立。
SCENE_ELEMENT_MAX_PER_1K = 4.0

#: S3：对白占比上限。正常文本 p50 22.5%。
DIALOGUE_MAX_RATIO = 0.05

#: S4：会面动词 + 2~3 字人名（如「找到了程野，」）出现次数下限。
#: 正常文本（除目标段）实测 0 次；1 次留给正常的单次会面。
MEET_VERB_OBJECT_MIN_HITS = 2

SUMMARY_STYLE_VERDICT_OK = "ok"
SUMMARY_STYLE_VERDICT_FLAGGED = "summary_style"

#: 转述/总结动词表：把「发生过什么」压成一句结论，而不是把读者放进现场。
_SUMMARY_VERB_RE = re.compile(
    r"告诉|得知|明白|决定|意识到|发现|了解|怀疑|震惊|清楚|认为|原来|从此"
)

#: 会面推进句式：动词 + 2~3 字对象 + 句读。对象长度限制把
#: 「他找到了答案，」这类抽象宾语排除在外（答案/真相/线索都是 2 字，仍会中，
#: 故还要求整段出现 ≥2 次；单次会面不判）。
_MEET_VERB_OBJECT_RE = re.compile(
    r"(?:找到了|见到|拜访了|联系了|询问了|走访了)"
    r"([\u4e00-\u9fa5]{2,3})(?=[，。、！？；“”])"
)

#: 场景要素词表（环境/感官）：两字词优先，避免「目光/时光」被单字「光」充数。
_SCENE_ELEMENTS = (
    "灯光", "阳光", "月光", "光线", "影子", "阴影", "窗外", "门口", "房门",
    "风", "雨", "雪", "夜色", "声音", "响声", "气味", "冷风", "空气",
    "房间", "街道", "城市", "昏暗", "天空", "墙壁", "桌上", "椅子", "杯子",
    "烟", "雾", "云", "星", "月",
)

#: 对白：中文引号/直角引号/英文双引号内的内容。
_QUOTE_SPAN_RE = re.compile(r"[“「]([^”」]{1,400})[”」]|\"([^\"\n]{1,400})\"")


# ─────────────────────────────────────────────────────────────────────────────
# 参数与结果对象
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepetitionThresholds:
    """可注入的判据参数。

    默认值即生产判据；参数化的目的不是"调松"，而是让单测能构造边界样本
    （例如把 `max_dup20` 调到 0.99 验证「判据确实生效」），以及给后续
    灰度留一个**显式**入口——任何偏离默认的取值都必须在调用点写清楚。
    """

    min_chars: int = MIN_CHARS
    max_top_sentence_repeat: int = MAX_TOP_SENTENCE_REPEAT
    max_repeated_sentence_coverage: float = MAX_REPEATED_SENTENCE_COVERAGE
    max_dup20: float = MAX_DUP20
    loop_max_unique_ratio: float = LOOP_MAX_UNIQUE_RATIO
    loop_min_sentences: int = LOOP_MIN_SENTENCES
    dup5_n: int = DUP5_N
    dup20_n: int = DUP20_N
    round_digits: int = ROUND_DIGITS


#: 生产判据（不要在使用处重新构造一份，否则默认值会漂移）。
DEFAULT_THRESHOLDS = RepetitionThresholds()


@dataclass(frozen=True)
class RepetitionMetrics:
    """一段正文的重复度测量结果（字段即诊断表的那几列）。

    比例字段均已按 `round_digits` 取整；判定也基于取整后的值（见
    `ROUND_DIGITS` 的说明）。
    """

    chars: int
    sentences: int
    unique_sentences: int
    unique_sentence_ratio: float
    top_sentence: str
    top_sentence_repeat: int
    repeated_sentence_chars: int
    repeated_sentence_coverage: float
    dup5: float
    dup20: float
    paragraphs: int
    #: 出现 ≥2 次的段落的**总出现次数**（含首次），即诊断表的 `dup_paras` 口径。
    #: 注意不是「不同的重复段落个数」——gen4 里不同的重复段落只有 5 个，
    #: 但总出现次数是 50。两个数都有人问，这里只保留与诊断表一致的那个。
    duplicate_paragraph_occurrences: int
    ends_complete: bool
    ending: str
    tail: str


@dataclass(frozen=True)
class LoopWindow:
    """退化后缀（模板循环窗）的位置与规模。

    「循环窗」= 满足「不同句子比 ≤ `loop_max_unique_ratio` 且句数 ≥
    `loop_min_sentences`」的**最长后缀**。它是给人读的原因，不是判据——
    判定只看 `RepetitionMetrics` 的四条线。
    """

    start_char: int
    chars: int
    sentences: int
    unique_sentences: int

    @property
    def unique_ratio(self) -> float:
        return (self.unique_sentences / self.sentences) if self.sentences else 0.0

    @property
    def share(self) -> float:
        total = self.start_char + self.chars
        return (self.chars / total) if total else 0.0


@dataclass(frozen=True)
class RepetitionReport:
    """检测结论：判定 + 指标 + 循环窗 + 人类可读原因。

    `verdict` 三态：

    - `ok`：够长且未命中判据；
    - `degenerate`：命中判据（`triggers` 说明命中了哪一条）；
    - `skipped_short`：正文短于 `min_chars`，**不判定**（不是 ok）。
    """

    verdict: str
    metrics: RepetitionMetrics
    loop: LoopWindow | None = None
    triggers: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    thresholds: RepetitionThresholds = field(default=DEFAULT_THRESHOLDS, repr=False)

    @property
    def is_degenerate(self) -> bool:
        return self.verdict == VERDICT_DEGENERATE

    @property
    def reason_text(self) -> str:
        """把所有原因拼成一句（日志/接口回显用）。"""
        return "；".join(self.reasons)

    def to_dict(self) -> dict:
        """扁平化输出（便于落库/接口返回；不含 thresholds 对象本身）。"""
        m = self.metrics
        return {
            "verdict": self.verdict,
            "chars": m.chars,
            "sentences": m.sentences,
            "unique_sentences": m.unique_sentences,
            "unique_sentence_ratio": m.unique_sentence_ratio,
            "top_sentence": m.top_sentence,
            "top_sentence_repeat": m.top_sentence_repeat,
            "repeated_sentence_chars": m.repeated_sentence_chars,
            "repeated_sentence_coverage": m.repeated_sentence_coverage,
            "dup5": m.dup5,
            "dup20": m.dup20,
            "paragraphs": m.paragraphs,
            "duplicate_paragraph_occurrences": m.duplicate_paragraph_occurrences,
            "ends_complete": m.ends_complete,
            "ending": m.ending,
            "loop": (
                {
                    "start_char": self.loop.start_char,
                    "chars": self.loop.chars,
                    "sentences": self.loop.sentences,
                    "unique_sentences": self.loop.unique_sentences,
                    "unique_ratio": round(self.loop.unique_ratio, ROUND_DIGITS),
                    "share": round(self.loop.share, ROUND_DIGITS),
                }
                if self.loop
                else None
            ),
            "triggers": list(self.triggers),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class LengthReport:
    """篇幅判据结论（正文 vs 项目字数预算）。

    四态：`ok` / `short` / `undetected`（预算缺失）/ `not_applicable`
    （局部改写路径，跌幅归 `scope_guard.detect_length_drop`）。
    """

    verdict: str
    chars: int
    budget: int | None
    ratio: float | None
    reason_text: str
    min_ratio: float = MIN_LENGTH_RATIO

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "chars": self.chars,
            "budget": self.budget,
            "ratio": self.ratio,
            "min_ratio": self.min_ratio,
            "reason_text": self.reason_text,
        }


@dataclass(frozen=True)
class SummaryStyleMetrics:
    """梗概化判据的原始指标（全部是固定词表/正则的可复现值）。"""

    chars: int
    sentences: int
    dialogue_ratio: float
    meet_verb_object_hits: int
    meet_verb_object_per_1k: float
    summary_verb_per_1k: float
    scene_element_per_1k: float


@dataclass(frozen=True)
class SummaryStyleReport:
    """梗概式推进结论：`ok` / `summary_style` / `skipped_short`。"""

    verdict: str
    metrics: SummaryStyleMetrics
    signals: tuple[str, ...] = ()
    reason_text: str = ""

    def to_dict(self) -> dict:
        m = self.metrics
        return {
            "verdict": self.verdict,
            "signals": list(self.signals),
            "reason_text": self.reason_text,
            "metrics": {
                "chars": m.chars,
                "sentences": m.sentences,
                "dialogue_ratio": m.dialogue_ratio,
                "meet_verb_object_hits": m.meet_verb_object_hits,
                "meet_verb_object_per_1k": m.meet_verb_object_per_1k,
                "summary_verb_per_1k": m.summary_verb_per_1k,
                "scene_element_per_1k": m.scene_element_per_1k,
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# 测量（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def split_sentences(text: str) -> list[str]:
    """按诊断口径断句：句末 `。！？` 后断开、连续换行断开，逐句 strip 去空。

    这是**对外承诺的口径**（单测直接钉住它）。`”` 会留在下一句句首，
    与诊断表里的 `”那个身影问道。` 完全一致。
    """
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _char_ngram_dup_ratio(text: str, n: int) -> float:
    """字符 n-gram 重复率 = 出现 >1 次的 n-gram 次数 / 全部 n-gram 次数。

    与诊断脚本相同：**不去重、含换行**、分母是滑动窗口总数。
    """
    if n <= 0 or len(text) < n:
        return 0.0
    grams = [text[i : i + n] for i in range(len(text) - n + 1)]
    if not grams:
        return 0.0
    counter = Counter(grams)
    dup = sum(c for c in counter.values() if c > 1)
    return dup / len(grams)


def measure_repetition(
    text: str,
    thresholds: RepetitionThresholds = DEFAULT_THRESHOLDS,
) -> RepetitionMetrics:
    """算 12 项重复度指标（不做判定）。

    口径与诊断脚本 `metrics_all.py::analyze_text` 逐项一致：

    - `chars`：`len(text)`（含换行，不 strip）；
    - `sentences` / `unique_sentences`：`split_sentences` 后的句数 / 去重句数；
    - `unique_sentence_ratio`：去重句数 / 句数（无句子时为 0.0）；
    - `top_sentence_repeat`：出现次数最多的那一句的次数；
    - `repeated_sentence_chars`：出现 ≥2 次的句子累计字符数（按出现次数加总）；
    - `repeated_sentence_coverage`：上者 / `chars`；
    - `dup5` / `dup20`：5-gram / 20-gram 重复率（只报告与判据两种用途）；
    - `paragraphs` / `duplicate_paragraph_occurrences`：按空行切段后的段数，
      以及其中「出现 ≥2 次的段落」的总出现次数（含首次，诊断表 `dup_paras` 口径）；
    - `ends_complete`：末字符是否属于 `。！？…”』」`。
    """
    raw = text or ""
    sentences = split_sentences(raw)
    counter = Counter(sentences)
    total = len(sentences)
    rounds = thresholds.round_digits

    unique = len(counter)
    unique_ratio = (unique / total) if total else 0.0
    if counter:
        top_sentence, top_repeat = counter.most_common(1)[0]
    else:
        top_sentence, top_repeat = "", 0

    repeated_chars = sum(len(s) * c for s, c in counter.items() if c > 1)
    coverage = (repeated_chars / len(raw)) if raw else 0.0

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip()]
    para_counter = Counter(paragraphs)
    duplicate_paragraph_occurrences = sum(c for c in para_counter.values() if c > 1)

    ending = raw.rstrip()[-1:] if raw.strip() else ""
    tail = raw.rstrip()[-20:] if raw.strip() else ""

    return RepetitionMetrics(
        chars=len(raw),
        sentences=total,
        unique_sentences=unique,
        unique_sentence_ratio=round(unique_ratio, rounds),
        top_sentence=top_sentence,
        top_sentence_repeat=top_repeat,
        repeated_sentence_chars=repeated_chars,
        repeated_sentence_coverage=round(coverage, rounds),
        dup5=round(_char_ngram_dup_ratio(raw, thresholds.dup5_n), rounds),
        dup20=round(_char_ngram_dup_ratio(raw, thresholds.dup20_n), rounds),
        paragraphs=len(paragraphs),
        duplicate_paragraph_occurrences=duplicate_paragraph_occurrences,
        ends_complete=bool(ending) and ending in _COMPLETE_ENDINGS,
        ending=ending,
        tail=tail,
    )


def _sentence_offsets(text: str, sentences: list[str]) -> list[int]:
    """每句在原文中的起始偏移（顺序 find，重复句各归各位）。"""
    offsets: list[int] = []
    cursor = 0
    for s in sentences:
        idx = text.find(s, cursor)
        if idx < 0:  # 理论上不会发生（句子来自原文切分）；保守回退到游标
            idx = cursor
        offsets.append(idx)
        cursor = idx + len(s)
    return offsets


def find_loop_window(
    text: str,
    thresholds: RepetitionThresholds = DEFAULT_THRESHOLDS,
    *,
    metrics: RepetitionMetrics | None = None,
) -> LoopWindow | None:
    """定位模板循环窗（最长退化后缀）；没有则返回 None。

    定义：找到最早的下标 i，使得 `sentences[i:]` 的句数 ≥ `loop_min_sentences`
    且「不同句子数 / 句数」≤ `loop_max_unique_ratio`。等价于「最长的那段
    越往后越像复读的尾巴」。

    这是**解释性指标**，不参与判定——判据只看整段的四项数值。
    """
    raw = text or ""
    sentences = split_sentences(raw)
    n = len(sentences)
    min_sents = max(1, int(thresholds.loop_min_sentences))
    if n < min_sents:
        return None

    start_index: int | None = None
    for i in range(0, n - min_sents + 1):
        tail = sentences[i:]
        if len(set(tail)) / len(tail) <= thresholds.loop_max_unique_ratio:
            start_index = i
            break
    if start_index is None:
        return None

    offsets = _sentence_offsets(raw, sentences)
    start_char = offsets[start_index]
    tail = sentences[start_index:]
    return LoopWindow(
        start_char=start_char,
        chars=len(raw) - start_char,
        sentences=len(tail),
        unique_sentences=len(set(tail)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 判定 + 人类可读原因
# ─────────────────────────────────────────────────────────────────────────────


def _fmt_pct(v: float) -> str:
    """0.525 → 52.5%（给人读比例，不给 0.525 这种需要换算的数字）。"""
    return f"{v * 100:.1f}%"


def _clip(s: str, limit: int = 36) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def analyze_repetition(
    text: str,
    thresholds: RepetitionThresholds = DEFAULT_THRESHOLDS,
) -> RepetitionReport:
    """检测一段正文是否已退化为复读，并给出**人类可读**的原因。

    这是闸门要用的一站式入口：返回判定（`ok` / `degenerate` /
    `skipped_short`）+ 指标 + 循环窗 + 原因列表。**不产生任何副作用**。

    Args:
        text: 待检测正文（`None` 按空串处理）。
        thresholds: 判据参数；生产调用请用默认值（`DEFAULT_THRESHOLDS`）。

    Returns:
        `RepetitionReport`。`verdict == "skipped_short"` 表示正文短于
        `min_chars`、**未做判定**——调用方不得把它当成 `ok`。
    """
    metrics = measure_repetition(text, thresholds)
    if metrics.chars < thresholds.min_chars:
        return RepetitionReport(
            verdict=VERDICT_SKIPPED_SHORT,
            metrics=metrics,
            thresholds=thresholds,
            reasons=(
                f"正文仅 {metrics.chars} 字 < {thresholds.min_chars} 字门槛，"
                "重复率在短文本上天然虚高，不下结论",
            ),
        )

    loop = find_loop_window(text, thresholds, metrics=metrics)

    triggers: list[str] = []
    reasons: list[str] = []

    # ── 分支 A（AND）：单句高频 **且** 重复句覆盖正文 ≥20% ──
    # 两条必须同时成立：单看前者会被「满篇『他说。』」这类短句口头禅误报
    # （实测正常文本单句最高重复 100 次但覆盖率仅 <20%）。
    if (
        metrics.top_sentence_repeat >= thresholds.max_top_sentence_repeat
        and metrics.repeated_sentence_coverage >= thresholds.max_repeated_sentence_coverage
    ):
        triggers.append(
            f"top_sentence_repeat={metrics.top_sentence_repeat}"
            f">={thresholds.max_top_sentence_repeat} 且 "
            f"repeated_sentence_coverage={metrics.repeated_sentence_coverage}"
            f">={thresholds.max_repeated_sentence_coverage}"
        )
        reasons.append(
            f"最高频单句出现 {metrics.top_sentence_repeat} 次："
            f"「{_clip(metrics.top_sentence)}」"
        )
        reasons.append(
            f"重复句覆盖 {_fmt_pct(metrics.repeated_sentence_coverage)} 的正文"
            f"（{metrics.repeated_sentence_chars}/{metrics.chars} 字）"
        )

    # ── 分支 B（OR）：长跨度复读 ──
    if metrics.dup20 >= thresholds.max_dup20:
        triggers.append(
            f"dup20={metrics.dup20}>={thresholds.max_dup20}"
        )
        reasons.append(
            f"20-gram 重复率 {metrics.dup20}（长跨度复读，排除「偶发短句习惯」）"
        )

    if not triggers:
        reasons.append(
            f"未命中退化判据：top_sentence_repeat={metrics.top_sentence_repeat}"
            f"<{thresholds.max_top_sentence_repeat} 或 "
            f"repeated_sentence_coverage={metrics.repeated_sentence_coverage}"
            f"<{thresholds.max_repeated_sentence_coverage}"
            f"；dup20={metrics.dup20}<{thresholds.max_dup20}"
            f"（句数 {metrics.sentences}，唯一句 {metrics.unique_sentences}，"
            f"唯一率 {metrics.unique_sentence_ratio}）"
        )
        return RepetitionReport(
            verdict=VERDICT_OK,
            metrics=metrics,
            loop=loop,
            triggers=(),
            reasons=tuple(reasons),
            thresholds=thresholds,
        )

    # 判退化后补充整体形态（这两条对「是不是真循环」最有说服力）
    reasons.insert(
        0,
        f"全段 {metrics.sentences} 句只有 {metrics.unique_sentences} 个不同句子"
        f"（唯一率 {metrics.unique_sentence_ratio}）",
    )
    if metrics.duplicate_paragraph_occurrences:
        reasons.append(
            f"逐字相同的段落共出现 {metrics.duplicate_paragraph_occurrences} 次"
            "（含首次，按空行切段统计）"
        )
    if loop is not None:
        reasons.append(
            f"后 {loop.chars} 字（{loop.sentences} 句 / 仅 "
            f"{loop.unique_sentences} 个不同句子，占全文 {_fmt_pct(loop.share)}）"
            "为模板循环"
        )
    if not metrics.ends_complete:
        reasons.append(
            f"结尾停在「{_clip(metrics.tail, 20)}」，末字符非句末标点，疑似被截断"
        )

    return RepetitionReport(
        verdict=VERDICT_DEGENERATE,
        metrics=metrics,
        loop=loop,
        triggers=tuple(triggers),
        reasons=tuple(reasons),
        thresholds=thresholds,
    )


def analyze_length(
    text: str,
    budget: int | None,
    *,
    is_rewrite: bool = False,
    min_ratio: float = MIN_LENGTH_RATIO,
) -> LengthReport:
    """篇幅判据：正文相对**项目字数预算**（`projects.generation_length`）。

    与 `scope_guard.detect_length_drop` 的分工（不要合并）：

    - 本函数：`chars / budget < 80%` → `short`。用于**新生成的整段**。
    - `detect_length_drop`：改写后相对**改写前原文**跌幅 >20% → 提示。
      用于**局部改写**，此时本函数传 `is_rewrite=True`，返回
      `not_applicable`，两者各报各的。

    Args:
        text: 正文（`None` 按空串）。
        budget: 项目字数预算；`None`/`<=0` → `undetected`（不得当成 ok）。
        is_rewrite: 是否为改写既有段落的路径。
        min_ratio: 判定线（生产用默认 0.80）。

    Returns:
        `LengthReport`，verdict ∈ {ok, short, undetected, not_applicable}。
    """
    chars = len(text or "")
    if is_rewrite:
        return LengthReport(
            verdict=LENGTH_VERDICT_NOT_APPLICABLE,
            chars=chars,
            budget=None,
            ratio=None,
            reason_text=(
                "改写路径不按整段字数预算判定；篇幅跌幅由 "
                "scope_guard.detect_length_drop 单独报告"
            ),
            min_ratio=min_ratio,
        )
    if not budget or int(budget) <= 0:
        return LengthReport(
            verdict=READ_VERDICT_UNDETECTED,
            chars=chars,
            budget=None,
            ratio=None,
            reason_text=(
                "未检测：项目未设置 generation_length（字数预算），"
                "无法计算篇幅比例"
            ),
            min_ratio=min_ratio,
        )
    budget = int(budget)
    # 判定用**未取整**的比例：2399/3000=0.79967 必须判 short，
    # 若先取整成 0.800 就会漏判。取整只用于展示。
    ratio_raw = chars / budget
    ratio = round(ratio_raw, ROUND_DIGITS)
    if ratio_raw < min_ratio:
        return LengthReport(
            verdict=LENGTH_VERDICT_SHORT,
            chars=chars,
            budget=budget,
            ratio=ratio,
            reason_text=(
                f"篇幅不足：正文 {chars} 字，仅为字数预算 {budget} 字的 "
                f"{_fmt_pct(ratio)}（<{_fmt_pct(min_ratio)}）"
            ),
            min_ratio=min_ratio,
        )
    return LengthReport(
        verdict=LENGTH_VERDICT_OK,
        chars=chars,
        budget=budget,
        ratio=ratio,
        reason_text=(
            f"篇幅达标：正文 {chars} 字，达到字数预算 {budget} 字的 "
            f"{_fmt_pct(ratio)}（>={_fmt_pct(min_ratio)}）"
        ),
        min_ratio=min_ratio,
    )


def analyze_summary_style(text: str) -> SummaryStyleReport:
    """梗概式推进 / 罗列式推进判据（只标记，不拦截）。

    判定式：`(S1 或 S4) 且 (S2 或 S3)`——先要有「推进方式」证据，再要有
    「展示缺失」。四路信号与阈值见模块 docstring 的实测表：

    - S1 转述动词密度 ≥ `SUMMARY_VERB_MIN_PER_1K`（固定动词表）；
    - S2 场景要素密度 < `SCENE_ELEMENT_MAX_PER_1K`（固定场景词表）；
    - S3 对白占比 < `DIALOGUE_MAX_RATIO`（引号内字符 / 全文字符）；
    - S4 会面动词 + 2~3 字对象 ≥ `MEET_VERB_OBJECT_MIN_HITS` 次。

    误报实测（2026-11-12）：工作区 60 段非退化历史正文 + 主库 5 段，
    只有目标 750 字段命中，其余 0 命中。正文短于 `SUMMARY_STYLE_MIN_CHARS`
    返回 `skipped_short`（不判定，不是 ok）。
    """
    raw = text or ""
    chars = len(raw)
    sentences = split_sentences(raw)
    per_1k = max(chars, 1) / 1000.0

    quotes = [a or b for a, b in _QUOTE_SPAN_RE.findall(raw)]
    dialogue_ratio = (
        round(sum(len(q) for q in quotes) / chars, ROUND_DIGITS) if chars else 0.0
    )
    meet_hits = len(_MEET_VERB_OBJECT_RE.findall(raw))
    metrics = SummaryStyleMetrics(
        chars=chars,
        sentences=len(sentences),
        dialogue_ratio=dialogue_ratio,
        meet_verb_object_hits=meet_hits,
        meet_verb_object_per_1k=round(meet_hits / per_1k, ROUND_DIGITS),
        summary_verb_per_1k=round(
            len(_SUMMARY_VERB_RE.findall(raw)) / per_1k, ROUND_DIGITS),
        scene_element_per_1k=round(
            sum(raw.count(w) for w in _SCENE_ELEMENTS) / per_1k, ROUND_DIGITS),
    )

    if chars < SUMMARY_STYLE_MIN_CHARS:
        return SummaryStyleReport(
            verdict=VERDICT_SKIPPED_SHORT,
            metrics=metrics,
            reason_text=(
                f"正文仅 {chars} 字 < {SUMMARY_STYLE_MIN_CHARS} 字门槛，"
                "不判文体"
            ),
        )

    s1 = metrics.summary_verb_per_1k >= SUMMARY_VERB_MIN_PER_1K
    s2 = metrics.scene_element_per_1k < SCENE_ELEMENT_MAX_PER_1K
    s3 = metrics.dialogue_ratio < DIALOGUE_MAX_RATIO
    s4 = metrics.meet_verb_object_hits >= MEET_VERB_OBJECT_MIN_HITS

    signals: list[str] = []
    reasons: list[str] = []
    if s1:
        signals.append("summary_verb_density")
        reasons.append(
            f"转述动词 {metrics.summary_verb_per_1k}/千字"
            f"（>={SUMMARY_VERB_MIN_PER_1K}）"
        )
    if s2:
        signals.append("scene_element_density")
        reasons.append(
            f"场景要素 {metrics.scene_element_per_1k}/千字"
            f"（<{SCENE_ELEMENT_MAX_PER_1K}）"
        )
    if s3:
        signals.append("dialogue_ratio")
        reasons.append(
            f"对白占比 {_fmt_pct(metrics.dialogue_ratio)}"
            f"（<{_fmt_pct(DIALOGUE_MAX_RATIO)}）"
        )
    if s4:
        signals.append("meet_verb_object")
        reasons.append(
            f"会面动词+人名 {metrics.meet_verb_object_hits} 次"
            f"（>={MEET_VERB_OBJECT_MIN_HITS}）"
        )

    if (s1 or s4) and (s2 or s3):
        return SummaryStyleReport(
            verdict=SUMMARY_STYLE_VERDICT_FLAGGED,
            metrics=metrics,
            signals=tuple(signals),
            reason_text="梗概式推进：" + "、".join(reasons),
        )
    detail = (
        f"转述动词 {metrics.summary_verb_per_1k}/千字、"
        f"场景要素 {metrics.scene_element_per_1k}/千字、"
        f"对白占比 {_fmt_pct(metrics.dialogue_ratio)}、"
        f"会面动词+人名 {metrics.meet_verb_object_hits} 次"
    )
    return SummaryStyleReport(
        verdict=SUMMARY_STYLE_VERDICT_OK,
        metrics=metrics,
        signals=tuple(signals),
        reason_text=f"未命中梗概化判据：{detail}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 落库 / 回读（`generations.quality_json`）
#
# 判据到这里就结束了；下面两个函数只做**形状转换**，不参与判定：
# - `quality_payload`：报告 → 列里的 JSON（写入侧）；
# - `parse_quality`：列值 → 读接口的 `quality`（读取侧）。
# ─────────────────────────────────────────────────────────────────────────────

#: 读取侧专用：列值为 NULL（历史行 / 回滚恢复行 / 接线前写入）时的 verdict。
#: **永远不会被写入库**——存进去的只有上面三态。
READ_VERDICT_UNDETECTED = "undetected"

_UNDETECTED_REASON = "未检测：这段写作时还没有复读闸门，不代表内容正常"


def quality_payload(
    report: RepetitionReport,
    checked_at: str | None = None,
    *,
    length: LengthReport | None = None,
    summary_style: SummaryStyleReport | None = None,
) -> dict:
    """把报告压成落库/响应共用的最小结构（方案稿 §3.1）。

    `truncated` 与 verdict **独立**：短文本也会因 `ends_complete=False`
    被打上截断标记（短文本不判定退化，但仍可能被截断）。

    `length` / `summary_style` 是 2026-11-12 追加的两个**独立**字段：
    调用点传了才写（生产路径 P1/P2/P3 一律传），读取侧缺失会映射成
    `undetected`。三者不合并成一个布尔。
    """
    m = report.metrics
    payload = {
        "repetition": {
            "verdict": report.verdict,
            "triggers": list(report.triggers),
            "reason_text": report.reason_text,
            "top_sentence_repeat": m.top_sentence_repeat,
            "repeated_sentence_coverage": m.repeated_sentence_coverage,
            "dup20": m.dup20,
        },
        "truncated": not m.ends_complete,
        "checked_at": checked_at or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }
    if length is not None:
        payload["length"] = length.to_dict()
    if summary_style is not None:
        payload["summary_style"] = summary_style.to_dict()
    return payload


def _undetected_length() -> dict:
    return {
        "verdict": READ_VERDICT_UNDETECTED,
        "chars": None,
        "budget": None,
        "ratio": None,
        "min_ratio": MIN_LENGTH_RATIO,
        "reason_text": "未检测：这段写作时还没有篇幅判据，不代表篇幅合理",
    }


def _undetected_summary_style() -> dict:
    return {
        "verdict": READ_VERDICT_UNDETECTED,
        "signals": [],
        "reason_text": "未检测：这段写作时还没有梗概化判据，不代表文体正常",
        "metrics": None,
    }


def parse_quality(raw: str | None) -> dict:
    """读取侧：把 `generations.quality_json` 转成接口里的 `quality`。

    `NULL` / 空串 / 坏 JSON **一律**映射成 `verdict="undetected"`（未检测），
    绝不返回 `ok`——「没测过」不能被读成「正常」。返回形状与写入侧一致，
    前端只需读 `quality.repetition.verdict` / `quality.length.verdict` /
    `quality.summary_style.verdict`。

    `repetition` / `length` / `summary_style` 三个字段各自独立兜底：老行
    （只有 repetition、没有新字段）不会因为缺键而丢掉已有的复读结论。
    """
    empty = {
        "repetition": {
            "verdict": READ_VERDICT_UNDETECTED,
            "triggers": [],
            "reason_text": _UNDETECTED_REASON,
        },
        "length": _undetected_length(),
        "summary_style": _undetected_summary_style(),
        "truncated": None,
        "checked_at": None,
        # 老行 / 未检测：一致性层没有结论时是 None，不给空数组（空数组会被读成「查过，零问题」）
        "coherence": None,
    }
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    rep = data.get("repetition")
    if not isinstance(rep, dict) or not rep.get("verdict"):
        return empty
    length = data.get("length")
    if not isinstance(length, dict) or not length.get("verdict"):
        length = _undetected_length()
    style = data.get("summary_style")
    if not isinstance(style, dict) or not style.get("verdict"):
        style = _undetected_summary_style()
    return {
        "repetition": rep,
        "length": length,
        "summary_style": style,
        "truncated": data.get("truncated"),
        "checked_at": data.get("checked_at"),
        # 一致性层（说话人/时间线/指代/伏笔）。老行没有这个键 → None（未检测），
        # 与「检测过且零问题」区分开，读取侧不需要额外判断。
        "coherence": data.get("coherence"),
    }
