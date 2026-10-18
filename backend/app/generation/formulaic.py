"""套路化（模板化）检测：跨段 n-gram 重复 / 句首集中度 / 句长方差塌缩 / L1 摘要同质。

## 这个模块测什么、不测什么

它回答的是与 `engine._detect_ai_flavor` **不同层级**的问题：

- AI 腔检测看**用词**：文本里有没有「仿佛」「毫无疑问」这类套话；
- 本模块看**结构、节奏与叙事走位**：每段是不是都按同一个节拍写、句首是不是
  总是那几种、情节是不是在打转、段与段是不是越长越像。

两者互补，但**口径必须分开**（见下「为什么排除 AI 腔模式」）。

## 方法论：为什么用参考语料做基线，而不是绝对阈值

每个指标在算出「生成侧」数值的同时，都尽可能算出「参考语料侧」的同一数值：

- **绝对阈值是拍脑袋**。「CV < 0.3 就是塌缩」这类常数没有依据——不同作者、
  不同题材的正常节奏差异巨大，任何固定划线都会同时误报和漏报。
- **参考作者自己的分布才是「这个作者正常的多样性」**。项目选定参考语料
  （`Project.reference_novel_ids` → `NovelChunk`）意味着用户想学这位作者；
  他的句首集中度、句长变异就是「正常」的操作性定义。生成侧显著偏离参考侧，
  才是值得看的信号。
- **没有参考语料（或样本量不足）时，只报原始数值 + `verdict:
  "insufficient_baseline"`**，绝不给「正常/异常」的结论。诊断的第一纪律是
  不假装自己看到了其实没看到的东西。

样本量下限（`MIN_REFERENCE_CHUNKS` 等）是「数据不够就不下结论」的纪律，
**不是**对指标值拍正常/异常线——前者防噪声，后者正是本模块拒绝做的事。

## 为什么排除 AI 腔模式（口径分离）

`AI_FLAVOR_PATTERNS`（`engine.py`，32 个固定字面串）所在的文本跨度由既有的
AI 腔检测负责。跨段 n-gram 里若含这些串（或其子串），在本模块**一律排除**：

- 否则同一个问题会被两个检测器各计一次，两边数字互相污染（「模板句多了」
  可能只是「仿佛」用多了）；
- AI 腔是**补救层**（命中触发重写），本模块是**诊断层**（只让问题可见），
  职责与后续动作都不同，数字必须能分开对账。

排除是**按 n-gram 逐条**做的：只有与模式串有子串关系的 6-gram 被剔除，
同一句话里不含套话的其余 6-gram 照常计入。代价（已知局限）：常见短模式如
「仿佛」「似乎」「终于」「原来」会把它所在的 6-gram 整段抹掉，因此 M1 对
「嵌了套话的模板句」系统性低估——这是口径分离的自觉代价，不是实现疏漏。

## 四个指标与各自的已知局限

- **M1 跨段 n-gram 重复**：字符 6-gram 段内去重成集合，统计每个 n-gram 跨
  几个不同段。局限：① n 固定为 6，更短/更长的模板句看不见；② AI 腔排除
  压低召回（见上）；③ 高频功能词组合（「了一下」「的时候」）可能进报告，
  所以每条重复都带出现的段号列表，供人读例句。
- **M2 句首集中度**：按 。！？；…\\n 断句，句首剥引号/括号/破折号（含常见
  前导标点）后取前 2 字。局限：① 2 字粒度粗，「他说」「她问」这类对白句首
  天然集中，单看数值会误判，必须与参考基线对照；② 归一化熵在类别少时天然
  偏高（k=2 的均匀分布即 1.0），所以主指标取 top5 占比、熵只作佐证。
- **M3 句长方差塌缩**：每段句长列表的 mean/stdev/CV，窗口内看各段 mean 的
  极差与平均 CV。局限：① CV 会被「长短交替」的双峰分布掩盖（5/15 字交替
  CV 很高但节奏同样僵化）；② 句子数少的段 CV 不稳定，故要求可算段数下限；
  ③ 参考侧 chunk 不是连续叙事窗口，跨段极差的对照只是近似。
- **M4 L1 摘要同质**：相邻两篇 L1 段摘要的字符 4-gram 集合 Jaccard 相似度。
  局限：① 摘要是 LLM 压缩产物，同质可能来自「故事真在打转」也可能来自
  「压缩磨平了个性」，本模块无法区分；② 自身趋势指标，无参考基线，
  `verdict` 恒为 `insufficient_baseline`，必须结合 M1-M3 与人工阅读判断。

## 分层

- **纯函数层**：`m1_ngram_repetition` / `m2_sentence_onset` /
  `m3_sentence_length` / `m4_summary_homogeneity`，签名统一为
  `(texts, ...) -> dict`，不吃 db、不发 LLM，可单测。
- **取数层**：`analyze_formulaic(db, project_id, recent_n=10)`，从
  `Generation`（按 segment_no 降序取最近 recent_n 段，剔除空正文，再按正序）
  与 `NovelChunk`（`Project.reference_novel_ids` 对应语料，超限均匀抽样）取
  文本喂给纯函数层。**只读**：只 SELECT，不 commit、不写库、不改任何生成
  行为；参考语料的抽样事实会追加进各指标的 `baseline_note`，不静默。
"""

from __future__ import annotations

import math
import re
from statistics import mean as _mean
from statistics import stdev as _stdev

from sqlalchemy import select

from app.generation.engine import AI_FLAVOR_PATTERNS
from app.generation.summary_tree import MISSING_L1, l1_map, l1_segment_nos
from app.models import Generation, NovelChunk, Project

# ─────────────────────────────────────────────────────────────────────────────
# 参数：全部是「口径 / 样本量」常数，不是「正常 / 异常」阈值。
# 前者决定数字怎么算、数据多到能下结论；后者正是本模块拒绝拍的东西。
# ─────────────────────────────────────────────────────────────────────────────

#: M1 的 n-gram 阶数。6 的来历：短于 6 的多是功能词组合（「了一下」），长于
#: 6 的模板句在 3000 字段落里重复概率骤降；6 约是「像一句模板」的下限。
NGRAM_N = 6

#: M1 报告门槛：n-gram 至少跨这么多不同段才进报告。3 = 「偶尔撞车」与「稳定
# 模板」的分界（2 段可能是巧合，3 段起步说明是配方）。
MIN_CROSS_SEGMENTS = 3

#: M4 的 n-gram 阶数：摘要只有 ~60 字，6-gram 在这么短的文本上几乎不会重复，
#: 降到 4 才能让「相邻摘要同质」可测。
SUMMARY_NGRAM_N = 4

#: 参考侧最少 chunk 数。低于此数，「参考分布」本身是噪声，只报数值不下判决。
MIN_REFERENCE_CHUNKS = 3

#: M2 一侧最少句首样本数。top5 占比 / 熵在极少句子上没有意义。
MIN_ONSET_SAMPLES = 20

#: M3 一侧最少「可算 CV 的段」数。少于 2 段算不出跨段极差，CV 也无从对照。
MIN_CV_SEGMENTS = 2

#: 参考语料抽样上限（按 (novel_id, idx) 均匀跨步）。纯资源bound：整库上千
#: chunks 全算 n-gram 集合要分钟级；200 个 chunk 对四个指标的分布估计已足够。
#: 抽样事实写进 `baseline_note`，`reference_chunk_count` 语义为「抽样内计数」。
REFERENCE_CHUNK_LIMIT = 200

#: 各指标 detail 里明细列表的最大条数（计数类口径不受裁剪影响）。
DETAIL_TOP_K = 20

#: 断句符：。！？；… + 换行（中文叙事文本的句子边界）。
_SENTENCE_DELIM_RE = re.compile(r"[。！？；…\n]+")

#: 句首需要剥掉的前导字符。规范要求「引号 / 括号 / 破折号」；这里把常见前导
#: 标点（，、：；…）一并纳入，否则「，，，」这类断句残留会被当成句首计入。
_ONSET_PREFIX_CHARS = (
    "“”‘’\"'「」『』（）()【】[]〔〕<>《》—–－-~～·・、，：；。！？… \t\r"
)

#: 判定「几乎全为标点 / 空白」的字符集合（M1 过滤用）。
_PUNCT_CHARS = frozenset(
    " \t\r\n，。！？；：、…—～·「」『』（）()【】[]〔〕<>《》\"'“”‘’|/\\_"
    "-－–—~`@#$%^&*+=<>"
)

# ─────────────────────────────────────────────────────────────────────────────
# AI 腔模式排除（口径分离：这 32 个串归 engine._detect_ai_flavor 管）
# ─────────────────────────────────────────────────────────────────────────────

#: AI 腔模式的最长长度。n-gram 短于它时才需要做「反向包含」比对（n-gram 整体
#: 落在某个模式串内部）；n=6 时长模式仅 5 字，反向分支天然不会命中，保留是
#: 为了 n 被调小时语义仍然正确。
_MAX_FLAVOR_LEN = max(len(p) for p in AI_FLAVOR_PATTERNS)

#: AI 腔模式的合并正则（惰性编译）：一次 C 级 search 代替 32 次 `in`。
_FLAVOR_RE: re.Pattern[str] | None = None


def _flavor_regex() -> re.Pattern[str]:
    """AI 腔模式合并正则（进程内惰性编译一次）。"""
    global _FLAVOR_RE
    if _FLAVOR_RE is None:
        _FLAVOR_RE = re.compile("|".join(re.escape(p) for p in AI_FLAVOR_PATTERNS))
    return _FLAVOR_RE


def _overlaps_ai_flavor(ngram: str) -> bool:
    """n-gram 与任一 AI 腔模式是否有子串关系（**两头都比对**）。

    - 正向：n-gram 内含模式串（如 6-gram 里有「仿佛」）——最常见；
    - 反向：n-gram 整体落在模式串内部——只在 n 小于最长模式长度时可能。
    """
    if _flavor_regex().search(ngram):
        return True
    if len(ngram) < _MAX_FLAVOR_LEN:
        return any(ngram in p for p in AI_FLAVOR_PATTERNS)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 公共小工具（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _char_ngrams(text: str, n: int) -> list[str]:
    """字符 n-gram 列表（含重复，不去重）；文本短于 n 时返回空表。"""
    if n <= 0 or len(text) < n:
        return []
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def _is_punct_or_blank(s: str) -> bool:
    """是否几乎全为标点 / 空白（空串按 True 处理，调用方保证非空）。"""
    return all(c in _PUNCT_CHARS for c in s)


def _segment_ngram_set(text: str, n: int) -> tuple[set[str], set[str], set[str]]:
    """段内 n-gram 集合 + 两类被剔除集合（AI 腔 / 纯标点），均按 distinct 计。

    返回 `(kept, dropped_flavor, dropped_punct)`。剔除集合供报告说明
    「排除了多少、为什么」——口径分离要可见，不能静默吞掉。
    """
    kept: set[str] = set()
    dropped_flavor: set[str] = set()
    dropped_punct: set[str] = set()
    for g in _char_ngrams(text or "", n):
        if _is_punct_or_blank(g):
            dropped_punct.add(g)
        elif _overlaps_ai_flavor(g):
            dropped_flavor.add(g)
        else:
            kept.add(g)
    return kept, dropped_flavor, dropped_punct


def _split_sentences(text: str) -> list[str]:
    """按 。！？；…\\n 断句，去空白、丢空句。"""
    return [s.strip() for s in _SENTENCE_DELIM_RE.split(text or "") if s.strip()]


def _onset(sentence: str, k: int) -> str | None:
    """句首 k 字：先剥掉前导的引号 / 括号 / 破折号 / 标点，不足 k 字返回 None。"""
    s = sentence.lstrip(_ONSET_PREFIX_CHARS)
    if len(s) < k:
        return None
    return s[:k]


def _onset_distribution(texts: list[str], k: int) -> tuple[dict[str, int], int]:
    """一组文本的句首频率分布 + 因过短被跳过的句子数。"""
    counts: dict[str, int] = {}
    skipped = 0
    for t in texts:
        for sent in _split_sentences(t):
            o = _onset(sent, k)
            if o is None:
                skipped += 1
            else:
                counts[o] = counts.get(o, 0) + 1
    return counts, skipped


def _top_share(counts: dict[str, int], top_k: int) -> float | None:
    """频率最高的 top_k 个 key 的累计占比；无样本返回 None。"""
    total = sum(counts.values())
    if total == 0:
        return None
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    return sum(c for _, c in top) / total


def _normalized_entropy(counts: dict[str, int]) -> float | None:
    """归一化香农熵（H / log2(k)）。无样本 None；只有 1 个类别时 0.0。"""
    total = sum(counts.values())
    k = len(counts)
    if total == 0:
        return None
    if k <= 1:
        return 0.0
    h = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return h / math.log2(k)


def _sentence_lengths(text: str) -> list[int]:
    """一段文本的各句长度（字符数，断句后去空白）。"""
    return [len(s) for s in _split_sentences(text)]


def _length_stats(lengths: list[int]) -> dict | None:
    """一个单元的句长 mean / stdev / CV；句子数 < 2 时无法定义，返回 None。"""
    if len(lengths) < 2:
        return None
    m = _mean(lengths)
    sd = _stdev(lengths)
    return {"mean": m, "stdev": sd, "cv": (sd / m) if m > 0 else 0.0}


def _window_length_stats(units: list[tuple[int, str]]) -> dict:
    """窗口级句长统计：逐单元 CV、平均 CV、各单元 mean 的极差与 stdev。

    `units` 的标签对生成侧是 segment_no，对参考侧是 chunk 下标。
    """
    per_unit: list[dict] = []
    cvs: list[float] = []
    means: list[float] = []
    for label, text in units:
        lengths = _sentence_lengths(text)
        st = _length_stats(lengths)
        per_unit.append(
            {
                "label": label,
                "sentences": len(lengths),
                "mean": st["mean"] if st else None,
                "stdev": st["stdev"] if st else None,
                "cv": st["cv"] if st else None,
            }
        )
        if st:
            cvs.append(st["cv"])
            means.append(st["mean"])
    return {
        "per_unit": per_unit,
        "units_with_cv": len(cvs),
        "avg_cv": _mean(cvs) if cvs else None,
        # 极差 / stdev 至少需要 2 个可算单元；1 个单元时恒 0，报 None 而不是
        # 报一个「完美收敛」的假 0。
        "mean_spread": (max(means) - min(means)) if len(means) >= 2 else None,
        "means_stdev": _stdev(means) if len(means) >= 2 else None,
    }


def _ngram_set(text: str, n: int) -> set[str]:
    """字符 n-gram 集合；文本短于 n 时退化为 {整段文本}（空文本为空集）。"""
    t = text or ""
    if len(t) < n:
        return {t} if t else set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度；任一为空集返回 0.0（「都没内容」不同于「完全重合」）。"""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# M1 跨段 n-gram 重复（模板句）
# ─────────────────────────────────────────────────────────────────────────────


def m1_ngram_repetition(
    segments: list[tuple[int, str]],
    reference_chunks: list[str] | None = None,
    *,
    n: int = NGRAM_N,
    min_segments: int = MIN_CROSS_SEGMENTS,
    top_k: int = DETAIL_TOP_K,
) -> dict:
    """M1：跨段 n-gram 重复——「最近 N 段是不是在用同一批模板句」。

    算法：每段取字符 `n`-gram 并**段内去重成集合**（过滤 AI 腔模式与纯标点），
    统计每个 n-gram 出现在多少个不同段里；跨段数 ≥ `min_segments` 的进报告，
    按 (跨段数, 长度) 降序。总量口径 `repeated_ratio = 重复 n-gram 数 /
    全部 distinct n-gram 数`。参考侧对 chunks 做**同口径**计算（chunk 视为
    「段」），得到参考侧 repeated_ratio 做对照。

    精化（有参考语料时）：每条重复额外标注它在参考语料里出现的 chunk 数——
    **跨段重复且参考语料里罕见/没有的，才是「我们自己的模板」**；参考作者自己
    也高频用的是他的习惯，不该由我们报警。区分为 `also_in_reference`（bool）
    + `reference_chunk_count`（int，无参考语料时为 None——「没数据」与
    「数据说没有」必须分开）。

    Args:
        segments: `[(segment_no, text), ...]`，正序。
        reference_chunks: 参考语料正文列表；None / 空 = 无基线。
        n: n-gram 阶数。
        min_segments: 跨段报告门槛。
        top_k: 报告列表最大条数。

    Returns:
        标准指标 dict：`value`（repeated_ratio）、`baseline`、`baseline_note`、
        `verdict`、`detail`。
    """
    # —— 生成侧：段内集合 + 跨段计数 ——
    seg_sets: dict[int, set[str]] = {}
    dropped_flavor: set[str] = set()
    dropped_punct: set[str] = set()
    for seg_no, text in segments:
        kept, d_flavor, d_punct = _segment_ngram_set(str(text or ""), n)
        seg_sets[int(seg_no)] = kept
        dropped_flavor |= d_flavor
        dropped_punct |= d_punct

    cross: dict[str, set[int]] = {}
    for seg_no, kept in seg_sets.items():
        for g in kept:
            cross.setdefault(g, set()).add(seg_no)
    total_distinct = len(cross)
    repeated = {g: sorted(nos) for g, nos in cross.items() if len(nos) >= min_segments}
    ratio = (len(repeated) / total_distinct) if total_distinct else None

    # —— 参考侧：同口径（chunk 视为「段」）——
    ref_cross: dict[str, set[int]] = {}
    for idx, chunk in enumerate(reference_chunks or []):
        kept, d_flavor, d_punct = _segment_ngram_set(str(chunk or ""), n)
        dropped_flavor |= d_flavor
        dropped_punct |= d_punct
        for g in kept:
            ref_cross.setdefault(g, set()).add(idx)
    ref_total_distinct = len(ref_cross)
    ref_repeated_count = sum(1 for nos in ref_cross.values() if len(nos) >= min_segments)
    ref_ratio = (ref_repeated_count / ref_total_distinct) if ref_total_distinct else None
    has_ref = bool(reference_chunks)

    # —— 报告列表（计数类口径不受 top_k 裁剪）——
    rows: list[dict] = []
    for g, nos in sorted(
        repeated.items(), key=lambda kv: (-len(kv[1]), -len(kv[0]), kv[0])
    ):
        # 有参考语料时，缺席 = 0 个 chunk 命中（「数据说没有」）；无参考语料时
        # 才是 None（「没数据」）——两者必须分开，否则「我们自己的模板」会被
        # 误读成「参考侧情况不明」。
        ref_hit_count = len(ref_cross.get(g, ())) if has_ref else None
        rows.append(
            {
                "ngram": g,
                "length": len(g),
                "segment_count": len(nos),
                "segments": nos,
                "also_in_reference": (bool(ref_hit_count) if has_ref else None),
                "reference_chunk_count": ref_hit_count,
            }
        )
        if len(rows) >= top_k:
            break

    # 「我们自己的模板」= 跨段重复且参考语料里没有（按全量 repeated 计，不截断）
    own_template_count = (
        sum(1 for g in repeated if not ref_cross.get(g)) if has_ref else None
    )

    # —— 三态 verdict：两侧样本量都够 + 两个 ratio 都算得出来才判 ok/watch ——
    gen_ok = len(seg_sets) >= min_segments and total_distinct > 0
    ref_ok = has_ref and len(reference_chunks or []) >= MIN_REFERENCE_CHUNKS and ref_total_distinct > 0
    if not gen_ok or not ref_ok or ratio is None or ref_ratio is None:
        verdict = "insufficient_baseline"
    else:
        # 方向判定：生成侧模板率高于参考作者 → watch。不给倍数常数——
        # 参考分布本身就是标尺，差值在 detail.delta 里给人看。
        verdict = "watch" if ratio > ref_ratio else "ok"

    if not has_ref:
        note = (
            "项目未选参考语料（reference_novel_ids 为空或语料无正文），无参考侧"
            "分布可对照；repeated_ratio 仅报原始数值，不作正常/异常判断。"
        )
    else:
        note = (
            f"参考侧 {len(reference_chunks or [])} 个 chunk 同口径计算（chunk 视为"
            f"「段」，跨 {min_segments} 段以上计模板），distinct n-gram "
            f"{ref_total_distinct} 个；「这个作者正常的多样性」以此分布为标尺。"
        )
        if len(reference_chunks or []) < MIN_REFERENCE_CHUNKS:
            note += (
                f"样本量 < {MIN_REFERENCE_CHUNKS}，参考分布不稳定，只报数值不下判决。"
            )
        if not gen_ok:
            note += (
                f"生成侧不足 {min_segments} 段或无可用 n-gram，跨段重复无从谈起。"
            )

    return {
        "value": ratio,
        "baseline": (ref_ratio if has_ref else None),
        "baseline_note": note,
        "verdict": verdict,
        "detail": {
            "n": n,
            "min_segments": min_segments,
            "distinct_ngrams": total_distinct,
            "repeated_ngram_count": len(repeated),
            "repeated_ratio": ratio,
            "repeated_ngrams": rows,
            "own_template_count": own_template_count,
            "excluded_ai_flavor_ngrams": len(dropped_flavor),
            "excluded_punct_ngrams": len(dropped_punct),
            "delta": (ratio - ref_ratio) if (ratio is not None and ref_ratio is not None) else None,
            "reference": (
                {
                    "chunks": len(reference_chunks or []),
                    "distinct_ngrams": ref_total_distinct,
                    "repeated_ngram_count": ref_repeated_count,
                    "repeated_ratio": ref_ratio,
                }
                if has_ref
                else None
            ),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# M2 句首集中度
# ─────────────────────────────────────────────────────────────────────────────


def m2_sentence_onset(
    segments: list[tuple[int, str]],
    reference_chunks: list[str] | None = None,
    *,
    top_k: int = 5,
    onset_len: int = 2,
    report_top: int = DETAIL_TOP_K,
) -> dict:
    """M2：句首集中度——「每段开头句式是不是总是那几种」。

    算法：按 。！？；…\\n 断句，句首剥前导字符后取前 `onset_len` 字，对窗口内
    全部句首构建频率分布；指标为 **top5 占比**（主指标，越高越集中）+ **归一化
    香农熵**（佐证，越低越集中）。参考侧同口径对照。

    Args:
        segments: `[(segment_no, text), ...]`，正序。
        reference_chunks: 参考语料正文列表；None / 空 = 无基线。
        top_k: top 占比的 k（默认 5）。
        onset_len: 句首取字数（默认 2）。
        report_top: detail 里 top 句首列表的最大条数。

    Returns:
        标准指标 dict：`value` = top5 占比。
    """
    gen_counts, gen_skipped = _onset_distribution(
        [str(t or "") for _, t in segments], onset_len
    )
    ref_counts, ref_skipped = _onset_distribution(
        [str(c or "") for c in (reference_chunks or [])], onset_len
    )
    gen_total = sum(gen_counts.values())
    ref_total = sum(ref_counts.values())
    gen_share = _top_share(gen_counts, top_k)
    ref_share = _top_share(ref_counts, top_k)
    gen_entropy = _normalized_entropy(gen_counts)
    ref_entropy = _normalized_entropy(ref_counts)
    has_ref = bool(reference_chunks)

    gen_ok = gen_total >= MIN_ONSET_SAMPLES
    ref_ok = ref_total >= MIN_ONSET_SAMPLES
    if not gen_ok or not ref_ok or gen_share is None or ref_share is None:
        verdict = "insufficient_baseline"
    else:
        # 方向判定：生成侧句首比参考作者更集中 → watch。
        verdict = "watch" if gen_share > ref_share else "ok"

    if not has_ref:
        note = (
            "无参考语料，句首集中度只有原始数值；top5 占比与归一化熵需与参考"
            "作者同口径对照才有「集中/分散」的意义。"
        )
    else:
        note = (
            f"参考侧 {len(reference_chunks or [])} 个 chunk 同口径计算，句首样本 "
            f"{ref_total} 个；对照的是「这个作者正常的句首集中度」。"
        )
        if not ref_ok:
            note += (
                f"参考侧句首样本 < {MIN_ONSET_SAMPLES}（或生成侧不足），"
                "分布不稳定，只报数值不下判决。"
            )

    return {
        "value": gen_share,
        "baseline": (ref_share if has_ref else None),
        "baseline_note": note,
        "verdict": verdict,
        "detail": {
            "onset_len": onset_len,
            "top_k": top_k,
            "total_onsets": gen_total,
            "distinct_onsets": len(gen_counts),
            "skipped_short_sentences": gen_skipped,
            "top_onsets": [
                [o, c]
                for o, c in sorted(gen_counts.items(), key=lambda kv: (-kv[1], kv[0]))[
                    :report_top
                ]
            ],
            "normalized_entropy": gen_entropy,
            "delta": (gen_share - ref_share)
            if (gen_share is not None and ref_share is not None)
            else None,
            "reference": (
                {
                    "chunks": len(reference_chunks or []),
                    "total_onsets": ref_total,
                    "distinct_onsets": len(ref_counts),
                    "top_share": ref_share,
                    "normalized_entropy": ref_entropy,
                    "skipped_short_sentences": ref_skipped,
                }
                if has_ref
                else None
            ),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# M3 句长方差塌缩
# ─────────────────────────────────────────────────────────────────────────────


def m3_sentence_length(
    segments: list[tuple[int, str]],
    reference_chunks: list[str] | None = None,
) -> dict:
    """M3：句长方差塌缩——「节奏是不是越来越趋同，每段都一个腔调」。

    算法：每段算句长列表的 mean / stdev / 变异系数 CV；窗口级看**各段 mean 的
    极差与 stdev**（跨段节奏是否趋同）与**平均 CV**（段内节奏是否塌缩）。主
    指标取平均 CV（方差塌缩的最直接读数）；参考侧同口径对照。

    已知局限：CV 会被「长短交替」的双峰分布掩盖；参考侧 chunk 不是连续叙事
    窗口，跨段极差的对照只是近似——两者都在 detail / baseline_note 里可见。

    Args:
        segments: `[(segment_no, text), ...]`，正序。
        reference_chunks: 参考语料正文列表；None / 空 = 无基线。

    Returns:
        标准指标 dict：`value` = 平均 CV（段内），detail 另带跨段极差/stdev。
    """
    gen_stats = _window_length_stats([(int(no), str(t or "")) for no, t in segments])
    ref_stats = _window_length_stats(
        [(i, str(c or "")) for i, c in enumerate(reference_chunks or [])]
    )
    gen_avg_cv = gen_stats["avg_cv"]
    ref_avg_cv = ref_stats["avg_cv"]
    has_ref = bool(reference_chunks)

    gen_ok = gen_stats["units_with_cv"] >= MIN_CV_SEGMENTS
    ref_ok = ref_stats["units_with_cv"] >= MIN_CV_SEGMENTS
    if not gen_ok or not ref_ok or gen_avg_cv is None or ref_avg_cv is None:
        verdict = "insufficient_baseline"
    else:
        # 方向判定：生成侧平均 CV 低于参考作者 → 节奏更塌 → watch。
        # 注意 CV 低本身不是罪（有的作者本就句子整齐），所以必须对照参考侧。
        verdict = "watch" if gen_avg_cv < ref_avg_cv else "ok"

    if not has_ref:
        note = (
            "无参考语料，句长方差只有原始数值；「整齐」与「塌缩」取决于这位"
            "作者正常的句长变异，没有参考分布就无法区分。"
        )
    else:
        note = (
            f"参考侧 {len(reference_chunks or [])} 个 chunk 同口径计算，可算 CV 的"
            f"单元 {ref_stats['units_with_cv']} 个；对照的是「这个作者正常的句长"
            "变异」。参考 chunk 非连续叙事窗口，跨段极差对照为近似。"
        )
        if not ref_ok or not gen_ok:
            note += (
                f"一侧可算 CV 的单元 < {MIN_CV_SEGMENTS}，样本不足，"
                "只报数值不下判决。"
            )

    return {
        "value": gen_avg_cv,
        "baseline": (ref_avg_cv if has_ref else None),
        "baseline_note": note,
        "verdict": verdict,
        "detail": {
            "units_with_cv": gen_stats["units_with_cv"],
            "per_segment": gen_stats["per_unit"],
            "mean_spread": gen_stats["mean_spread"],
            "means_stdev": gen_stats["means_stdev"],
            "delta": (gen_avg_cv - ref_avg_cv)
            if (gen_avg_cv is not None and ref_avg_cv is not None)
            else None,
            "reference": (
                {
                    "chunks": len(reference_chunks or []),
                    "units_with_cv": ref_stats["units_with_cv"],
                    "avg_cv": ref_avg_cv,
                    "mean_spread": ref_stats["mean_spread"],
                    "means_stdev": ref_stats["means_stdev"],
                }
                if has_ref
                else None
            ),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# M4 L1 摘要同质（故事打转）
# ─────────────────────────────────────────────────────────────────────────────


def m4_summary_homogeneity(
    items: list[tuple[int, str]],
    *,
    n: int = SUMMARY_NGRAM_N,
) -> dict:
    """M4：L1 摘要同质——「相邻两篇段摘要是不是越讲越像，故事在打转」。

    算法：相邻两篇摘要的字符 `n`-gram 集合 Jaccard 相似度；指标为**平均相邻
    Jaccard** + **趋势**（前后半窗均值之差，正值 = 越来越像）。

    这是**自洽性度量，无参考基线**：`baseline` 恒为 None，`verdict` 恒为
    `insufficient_baseline`。摘要是 LLM 压缩产物，同质可能源于「故事真在打转」
    也可能源于「压缩磨平了个性」，本模块无法区分——必须结合 M1-M3 与人工
    阅读判断（见 `baseline_note`）。

    Args:
        items: `[(段号, 摘要文本), ...]`，按段号升序。
        n: n-gram 阶数（摘要很短，默认 4）。

    Returns:
        标准指标 dict：`value` = 平均相邻 Jaccard，detail 含逐对明细与趋势。
    """
    sets = [(label, _ngram_set(str(text or ""), n)) for label, text in items]
    pairs: list[dict] = []
    for (l_from, s_from), (l_to, s_to) in zip(sets, sets[1:]):
        pairs.append({"from": l_from, "to": l_to, "jaccard": _jaccard(s_from, s_to)})
    values = [p["jaccard"] for p in pairs]
    avg = _mean(values) if values else None
    trend: float | None = None
    if len(values) >= 2:
        half = len(values) // 2
        # 后半窗取多（奇数条时）：趋势看「后来是否更像」，宁让新数据权重略高。
        trend = _mean(values[half:]) - _mean(values[:half])

    if avg is None:
        note = (
            "L1 摘要不足两篇，无法计算相邻相似度。此为自身趋势指标，无参考基线，"
            "需结合 M1-M3 与人工阅读判断。"
        )
    else:
        note = (
            "L1 摘要是 LLM 压缩产物，「同质」可能源于故事真在打转，也可能源于"
            "压缩磨平个性，本指标无法区分；此为自身趋势度量，无参考基线，"
            "verdict 恒为 insufficient_baseline，需结合 M1-M3 与人工阅读判断。"
        )

    return {
        "value": avg,
        "baseline": None,
        "baseline_note": note,
        "verdict": "insufficient_baseline",
        "detail": {
            "n": n,
            "n_summaries": len(items),
            "pairs": pairs,
            "avg_jaccard": avg,
            "trend": trend,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 取数层（薄，只读）
# ─────────────────────────────────────────────────────────────────────────────

#: 汇总说明：一句话钉死「这是诊断不是判决」。
_OVERALL_NOTE = (
    "这是诊断不是判决：ok/watch 只表示「与参考语料分布的对照方向」，不是质量"
    "评分；insufficient_baseline 表示参考基线缺失或样本不足，此时只有原始数值"
    "可信。请结合各指标明细与人工阅读下结论。"
)


def _load_reference_texts(db, project: Project) -> tuple[list[str], dict]:
    """按 `Project.reference_novel_ids` 取参考语料正文（超限均匀跨步抽样）。

    Returns:
        `(texts, info)`。info 记录 `total_chunks` / `sampled`，供
        `baseline_note` 追认抽样事实（不静默）。
    """
    ids: list[int] = []
    for raw in project.reference_novel_ids or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not ids:
        return [], {"total_chunks": 0, "sampled": False}

    texts = db.scalars(
        select(NovelChunk.text)
        .where(NovelChunk.novel_id.in_(ids))
        .order_by(NovelChunk.novel_id, NovelChunk.idx, NovelChunk.id)
    ).all()
    texts = [t for t in texts if (t or "").strip()]
    total = len(texts)
    sampled = False
    if total > REFERENCE_CHUNK_LIMIT:
        step = total / REFERENCE_CHUNK_LIMIT
        texts = [texts[int(i * step)] for i in range(REFERENCE_CHUNK_LIMIT)]
        sampled = True
    return texts, {"total_chunks": total, "sampled": sampled}


def _recent_l1_items(
    project: Project, generations: list, recent_n: int
) -> tuple[list[tuple[int, str]], str, int]:
    """取最近 recent_n 篇 L1 段摘要：优先 `Project.summary_tree`，退回
    `Generation.summary["text"]`（按 segment_no 升序）。

    缺失占位（`MISSING_L1`）与空摘要一律跳过并计数——它们是数据质量痕迹，
    不是「故事同质」的信号，混进 Jaccard 会伪造出「连续两篇一模一样」。

    Returns:
        `([(段号, 摘要), ...], source, skipped_missing)`。
    """
    tree = project.summary_tree if isinstance(project.summary_tree, dict) else {}
    l1 = l1_map(tree) if tree else {}
    if l1:
        items: list[tuple[int, str]] = []
        skipped = 0
        for seg in l1_segment_nos(tree)[-recent_n:]:
            text = str(l1.get(str(seg)) or "").strip()
            if not text or text == MISSING_L1:
                skipped += 1
                continue
            items.append((seg, text))
        if items:
            return items, "summary_tree_l1", skipped

    items = []
    skipped = 0
    for g in generations:  # 已按 segment_no 升序
        raw = g.summary if isinstance(g.summary, dict) else {}
        text = str(raw.get("text") or "").strip()
        if not text or text == MISSING_L1:
            skipped += 1
            continue
        items.append((int(g.segment_no), text))
    return items[-recent_n:], "generation_summary_fallback", skipped


def analyze_formulaic(db, project_id: int, *, recent_n: int = 10) -> dict:
    """对一个项目跑套路化检测，返回诊断报告（**只读**，零 LLM）。

    取数：`Generation` 按 segment_no 降序取最近 `recent_n` 段（剔除空正文后
    再按正序排）；`NovelChunk` 取 `reference_novel_ids` 对应语料（超
    `REFERENCE_CHUNK_LIMIT` 均匀抽样，抽样事实写进各指标 `baseline_note`）；
    M4 取最近 `recent_n` 篇 L1 摘要（优先 `summary_tree`，退回
    `Generation.summary["text"]`）。**只 SELECT，不 commit、不写库、不改任何
    生成行为。**

    Args:
        db: SQLAlchemy Session（只读用途）。
        project_id: 项目 id。
        recent_n: 分析窗口大小（最近多少段）。

    Returns:
        `{project_id, window_segments, generated_chars, has_reference,
        metrics: {m1..m4}, overall: {note}}`。

    Raises:
        ValueError: 项目不存在。
    """
    project = db.get(Project, int(project_id))
    if project is None:
        raise ValueError(f"项目不存在：project_id={project_id}")
    recent_n = max(1, int(recent_n))

    rows = db.scalars(
        select(Generation)
        .where(Generation.project_id == project.id)
        .order_by(Generation.segment_no.desc(), Generation.id.desc())
        .limit(recent_n)
    ).all()
    # 降序取回 → 反转为正序；空正文的段不进窗口（它不产生任何可测信号）
    rows = [r for r in reversed(rows) if (r.text or "").strip()]
    segments = [(int(r.segment_no), r.text) for r in rows]
    generated_chars = sum(len(t) for _, t in segments)

    ref_texts, ref_info = _load_reference_texts(db, project)

    metrics: dict[str, dict] = {
        "m1_ngram_repetition": m1_ngram_repetition(segments, ref_texts),
        "m2_sentence_onset": m2_sentence_onset(segments, ref_texts),
        "m3_sentence_length": m3_sentence_length(segments, ref_texts),
    }
    summaries, m4_source, m4_skipped = _recent_l1_items(project, rows, recent_n)
    m4 = m4_summary_homogeneity(summaries)
    m4["detail"]["source"] = m4_source
    m4["detail"]["skipped_missing"] = m4_skipped
    metrics["m4_summary_homogeneity"] = m4

    # 抽样事实追认进三个有参考基线的指标（纯函数层不知道取数层抽过样）
    if ref_info["sampled"]:
        suffix = (
            f"（参考语料全库共 {ref_info['total_chunks']} 个 chunk，受资源上限"
            f" {REFERENCE_CHUNK_LIMIT} 约束已均匀抽样；reference_chunk_count "
            "为抽样内计数，非全库计数）"
        )
        for key in ("m1_ngram_repetition", "m2_sentence_onset", "m3_sentence_length"):
            metrics[key]["baseline_note"] += suffix

    return {
        "project_id": project.id,
        "window_segments": [no for no, _ in segments],
        "generated_chars": generated_chars,
        "has_reference": bool(ref_texts),
        "metrics": metrics,
        "overall": {"note": _OVERALL_NOTE},
    }
