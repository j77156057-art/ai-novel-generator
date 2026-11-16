# -*- coding: utf-8 -*-
"""文风一致性校验 · **第一阶段（L1 影子记录）**。

设计依据：`.trae/documents/style-consistency-check-design.md`。本模块只做三件事：

1. 用确定性算法量出核心 8 项指标（加上 12 项展示指标，不判界）；
2. 用**本项目已落库正文**的最近 N 段生成版本化基线（`style_baselines`）；
3. 每次生成成功后写一条 `style_checks` 记录（`verdict='shadow'`）。

## 本阶段明确不做

- 不产出 pass / warn / fail（写了就会让以后的误报率统计口径变脏）；
- 不改 `generations.text`、不重写、不阻断生成或保存；
- 不做 UI 明细页、不做 L2（LLM 判定）。

## 口径说明（与设计文档 §2.1 的一处差异，必须知道）

指标计算复用了 `app.style.stylometry.features()`——与 `projects.style_profile`
的参考带**同一个函数、同一套定义**。因此：

- 「短句占比」是 **句长 ≤ 4 字**（stylometry 的定义），不是设计文档正文里写的
  ≤ 10 字。文档写「≤10 字（与档案同口径）」，但档案实际是 ≤4；为了保证
  `|z|` 能和已存的参考带比较，这里以**档案实际口径**为准。
- 句长把句末标点算进句子长度（`stylometry.sentences()` 保留终结符），
  与档案一致；设计文档 §2.1 的「去标点」写法与档案不同。
- 虚词表取 `stylometry._FUNC_WORDS`（12 个），不是文档列出的 8 个。

调用 `features(..., unit_chars=0)` 得到**未做长度缩放**的原始值：档案里的
非密度特征在拟合时乘过 `exp(-|ln(unit_chars/3000)|*0.3)`，比较参考带时用
`_reference_scale()` 还原同一个系数，否则 z 值不可比。

## 失败纪律

`record_segment_check()` **永不抛异常**：任何错误都吞掉、回滚并写日志。
校验器故障绝不能让生成失败（生成链路只加了一次调用，见 engine._commit_segment）。
"""
from __future__ import annotations

import contextvars
import hashlib
import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select

from app.models import Generation, StyleBaseline, StyleCheck
from app.style import stylometry

logger = logging.getLogger(__name__)

#: 校验器版本：口径或算法变更时必须同步 +1。
#: v2（2026-11-13）：新增 `dialogue` 对白画像（纯对白段 / 消息段 / 语气），
#: 与 `gen-gate-v4` 同源；旧 v1 记录没有这一段，不可与新记录混算。
CHECK_VERSION = "l1-shadow-v2"

#: 核心 8 项（唯一"有资格"进阈值的指标；本阶段只入库，不判界）。
CORE_METRICS: tuple[str, ...] = (
    "句长均值",
    "句长CV",
    "短句占比",
    "段均句数",
    "对白字占比",
    "逗号密度",
    "虚词密度",
    "四字格密度",
)

#: 展示项 = 其余 12 项，只记录不判界（零膨胀项在参考带里 p50=0，判界会恒亮）。
DISPLAY_METRICS: tuple[str, ...] = tuple(
    k for k in stylometry.FEATURES if k not in CORE_METRICS
)

#: 低于此字数不做判定（设计 §2.4：≤1200 字的单段不可判）。值仍然记录。
MIN_JUDGE_CHARS = 1200

#: 自身基线认为"充分"的最小段数（设计 §1.4）。达不到就写在基线行上。
MIN_SELF_SEGMENTS = 20

#: 基线窗口：用最近多少段（设计 §3.3）。
WINDOW_SEGMENTS = 30

#: 冷启动参考带取 short 桶（1500 字单元，n=800）。
REFERENCE_BUCKET = "short"

#: 参考带偏离口径：|z| ≥ 3 才写一句说明（设计 §3.2）。
REFERENCE_Z = 3.0

#: 短句的字长阈值（**全项目唯一值**）。
#:
#: 2026-11-15（v20）统一：此前 `measure()` / `stylometry.features()` 用 ≤4，
#: `generation_gate()` 用 ≤5，两个同名指标量的是两个东西。统一到 4 的理由：
#: 档案侧（`stylometry.features` → `projects.style_profile` 参考带、`style_baselines`、
#: all `style_checks` 历史）从 2026-10 起就是 ≤4 且已落库，改成 5 要重算所有项目的
#: 档案；生成侧只有 `generation_gate` 一处分支可改。而 `narrative.py` 的文笔量化
#: 审计有自己的 ≤5 定义、且参考值与实测值都由同一函数算出（内部自洽），不受影响。
#: 这个常量写进 `quality_json.gate_limits.short_sentence_max_len`，便于事后判读。
SHORT_SENTENCE_MAX_LEN = 4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 1. 指标
# --------------------------------------------------------------------------- #

def measure(text: str) -> dict[str, Any]:
    """量一段正文。**算不出的项给 None，不给 0**。

    Returns:
        {
          "chars": 去空白后的字数,
          "judgeable": 字数是否达到可判门槛,
          "core": {8 项: float | None},
          "display": {12 项: float | None},
          "missing": {指标: 原因} ∪ {"_check": 原因},
          "metric_scope": "stylometry.raw.v1",
        }
    """
    raw_text = text or ""
    chars = len("".join(raw_text.split()))
    result: dict[str, Any] = {
        "chars": chars,
        "judgeable": chars >= MIN_JUDGE_CHARS,
        "core": {k: None for k in CORE_METRICS},
        "display": {k: None for k in DISPLAY_METRICS},
        "missing": {},
        "metric_scope": "stylometry.raw.v1",
        "sentence_scope": stylometry.SENTENCE_METRIC_SCOPE,
    }
    if chars == 0:
        result["missing"]["_check"] = "正文为空，无指标可算"
        return result

    paras = stylometry.paragraphs(raw_text)
    # R16：切句改用蒸馏端口径（唯一真源 `stylometry.split_sentences`），
    # 否则 style_check 的句长类指标与档案里的块级指标永远对不上。
    #
    # v19（2026-11-15）：句长类 5 项改用 `stylometry.sentence_lens`——即只统计
    # 「至少含一个汉字」的句子。`split_sentences` 每遇一个 `…` 就切一刀，`……`
    # 会被切成两个单「…」片段，收尾引号 `”` 也会独立成句；参考书每章中位 14 个
    # 这类无汉字片段，占「句数」约 9%。旧口径下 `短句占比` 量到的主要是标点
    # 碎片密度，不是短句密度。切句函数本身不动（档案块级指标由它算出）。
    sents = stylometry.split_sentences(raw_text)
    vec = stylometry.features(raw_text, unit_chars=0)
    # `features()` 内部仍走 `sentences()`（档案 L1 就是它算的，不能改），
    # 所以句长类 5 项在这里用 v2 口径重算覆盖；密度类指标不受切句影响，保持原值。
    #
    # v20（2026-11-15）：`段均句数` 也换成 `sentence_lens`。v19 只换了 5 项，
    # 这一项还按 `split_sentences` 逐段数句，导致
    # `段长均值 ≈ 段均句数 × 句长均值` 跨两把尺——分子分母一个含标点碎片、
    # 一个不含，恒等式不成立，后面所有三角校核都会把误差算到正文头上。
    # 现在两处（本函数与 `generation_gate`）统一为 `paragraph_sentence_counts`：
    # 按**全文**切句再归属到段（同一句话不会因跨段被数两次），
    # 且 `sum(计数) == len(sentence_lens)`，与 `句长均值` 严格同一把尺。
    lens = stylometry.sentence_lens(raw_text)
    sent_lens = lens or [0]
    per_para = stylometry.paragraph_sentence_counts(raw_text) or [0]
    vec.update({
        "句长均值": statistics.fmean(sent_lens),
        "句长CV": stylometry._cv(sent_lens),
        "句长p90": stylometry._quantile(sorted(sent_lens), 0.90),
        # v20：短句字长统一为 ≤`SHORT_SENTENCE_MAX_LEN`（4），与档案
        # `stylometry.features` / `projects.style_profile` 同一阈值；
        # `generation_gate` 原来的 ≤5 一并收回。
        "短句占比": sum(1 for x in sent_lens if x <= SHORT_SENTENCE_MAX_LEN)
        / len(sent_lens),
        "长句占比": sum(1 for x in sent_lens if x >= 40) / len(sent_lens),
        "段均句数": statistics.fmean(per_para),
    })

    # 逐项判断"能不能算"——不能算的写 None，不留 0。
    computable = {k: True for k in stylometry.FEATURES}
    if not lens:
        for k in ("句长均值", "句长CV", "句长p90", "短句占比", "长句占比"):
            computable[k] = False
        result["missing"]["句长均值"] = "未切出含汉字的句子"
        result["missing"]["短句占比"] = "未切出含汉字的句子"
    elif len(lens) < 2:
        computable["句长CV"] = False
        result["missing"]["句长CV"] = f"只有 {len(lens)} 句，变异系数无定义"
    if not paras:
        computable["段均句数"] = False
        computable["段长CV"] = False
        computable["对白段占比"] = False
        result["missing"]["段均句数"] = "无自然段"

    for k in CORE_METRICS:
        result["core"][k] = float(vec[k]) if computable.get(k, True) else None
    for k in DISPLAY_METRICS:
        result["display"][k] = float(vec[k]) if computable.get(k, True) else None

    # 对白画像（v2）：与生成闸门 `dialogue_profile` 同一函数，影子记录也能看到
    # 「纯对白段 / 消息段 / 语气词」这些结构性项。
    result["dialogue"] = stylometry.dialogue_profile(raw_text)

    if not result["judgeable"]:
        result["missing"]["_check"] = (
            f"正文字数 {chars} < {MIN_JUDGE_CHARS}，单段不可判定（指标仍记录）"
        )
    return result


# --------------------------------------------------------------------------- #
# 1b. 生成侧硬闸门（gen-gate-v4）
# --------------------------------------------------------------------------- #
#
# 为什么放在这里：narrative.py 的 RENDER_PRESETS 正在被另一会话改结构型留席，
# 本轮不动它；但闸门必须和档案统计共用同一把尺，所以这里只**惰性 import**
# `build_deterministic_features`，不复制任何公式、不另立口径。
#
# 阈值来源（2026-11-13 复算：novel 7 全本 11,334 个 1,200 字段落块，同一函数、
# 同一口径；分位用线性插值）。旧 v2 的带是在这张表上**手挑**的，其中
# 「叙述单句段 ≤10%」与档案严重错位——参考书自己的 p05 就有 22.2%、p50 是 50%。
#
#   指标            p10     p25     p50     p75     p90
#   句长均值        16.1    —      19.8    22.0    24.5
#   句长CV          0.576   —      0.704   0.774   0.846
#   ≤5 短句         0.059   —      0.146   0.200   0.246
#   ≥40 长句        0.033   —      0.094   0.135   0.184
#   31–39 字句      0.050   —      0.109   0.146   0.184
#   段长均值        24.1   27.5    31.6    35.6    41.1
#   问号/千字       —       —      5.00    7.55    10.83
#   叙述单句段占比   0.280* 0.385   0.500   0.620   0.714  （*p05=0.222）
#   对白字占比       0.065 0.126   0.226   0.349   0.464
#
# **这张表是 2026-11-13 的历史快照，只用于对照，不要当现行值。**
# v19（句长类换干净口径）与 v20（段均句数/叙述单句段/短句字长）之后的现行值
# 见常量表 `ARCHIVE_BLOCK_PERCENTILES`。
#
# v3 起**不再手写带**：`limits_from_archive()` 按一条统一规则把分位翻成区间，
# 只有两个经用户指定的例外（段长均值下限、叙述单句段上限）。改阈值就改表 +
# 版本号，不再出现「注释写着档案 p10，实际填了一个手拍数」的情况。

#: 2026-11-13 升 v5：加长度硬闸（汉字数 ≤ 目标字数 ×1.2），纯对白段/纯消息段
#: 降为软档，补极短纯对白段与叹词/感叹句配额。v3 的九项保留原值；
#: 对白项仍从**章级**分位推（生成单元与章同量级，4,000 字目标仍在同一档，
#: 而 v3 的九项沿用 1,200 字块级，两者不可混用）。
#: 2026-11-14 升 v6（只换判罚单位，**阈值与指标定义一字未改**）：
#: 单章只判结构合规（段长均值、极短纯对白段占比、段数）+ 汉字上限；
#: 文风指纹 11 项单章只记录，按卷与参考语料同长连续块对照看趋势（`volume_gate()`）。
#: 旧 v5 记录里这 11 项可能带 violations，不可与 v6 混算命中率。
#: 2026-11-14 升 v7（**阈值、指标定义、判罚单位一字未改**）：
#: 指纹清单加入「段均句数」（11→12 项，单章只记录），并把它作为确定性并段的
#: 硬约束上限（`merge_ultra_short_dialogue(spc_cap=...)`）。v6 记录里该指标
#: 本来就逐章存于 `style_gate.metrics`，所以卷均值照常可算，不需重跑历史。
#: 2026-11-14 升 v8（**阈值一字未改，只换判罚单位**）：
#: `段长均值` 从单章硬判移到卷级趋势——单章只判「极短纯对白段占比、段数」
#: 两项结构项 + 汉字上限；`段长均值` 继续逐章记录，并加入卷对照指标集
#: （12→13 项），按参考语料连续 14 章块均值分位看趋势，卷末章才判定。
#: v7 及以前的记录里 `段长均值` 带单章 violations，不可与 v8 直接对比命中率。
#: 2026-11-14 升 v9（**升版原因只有一条**：句长配额来源改为档案现推）：
#: 重写提示里的「句子分布配额」阈值从常量表 `GEN_GATE_LIMITS`（5.9%）改为
#: `active_full_limits()` 按当前参考书重推（8.9%），该比例会换算成绝对句数
#: 写进提示词，直接改变模型收到的指令与产物。判罚项、判罚阈值、退回值均未变。
#: 2026-11-14 升 v10（**升版原因只有一条**：极短纯对白段上限换口径）：
#: `极短纯对白段占比` 的上限从「参考书**全章**章级 p90」改为「**非零章**章级 p90」。
#: 全章口径下参考书 p90=0（6.57% 的章非零，其余为 0），拿 0 当上限等于每章必挂
#: 一项，没有判别力；非零章 p90 才有「参考书自己也在正常出现的量级」这个出处。
#: 判罚项、其余阈值、退回值机制均未变；`fallback_used`/`fallback_reason` 仍留痕。
#: 2026-11-14 升 v11（**升版原因只有一条**：提示与闸门口径对齐，判罚阈值本身未变）：
#: ①重写提示的「可长到」值改为 `min(原文汉字数 × 1.08, 4800)`，不再出现上浮 8%
#: 与硬上限 4800 并存的矛盾；②段数上限只留「汉字数 ÷ 21」一条折算，删掉按
#: 4,000 目标折算的 189 硬限，且折算值不低于档案推导的段数下限；③「不得靠删
#: 短句/对白/情节抬段均」改为按篇幅分情形：≤4800 保留禁令，>4800 以压缩为第一
#: 目标并明确允许删（篇幅要求，与段均无关）；④提示里的「极短纯对白段必须为 0」
#: 改为读当前闸门现推上限（v10 的非零章 p90），不写死 0；⑤句数配额补「约 N 句是
#: 中心值不是下限、句数不得让总字数突破 4800」。判罚项、判罚阈值、退回值均未变。
#: 2026-11-14 升 v12（**升版原因只有一条**：提示加对白配比参考线，判罚阈值未变）：
#: 卷 2 的 `对白字占比` 漂到 40.8%，而参考 p50 只有 26.9%。处理方式是**参考线**
#: 而非新判罚：把参考书章级 p25/p50/p75 与本卷实测写进提示，让模型自己收敛；
#: 不新增判罚项、不改任何既有阈值。分位由 engine 从档案现推（换参考书自动重推），
#: 无档案时退回配置/常量表并沿用 `fallback_used`/`fallback_reason` 留痕。
#: 2026-11-14 升 v13（**升版原因只有一条**：提示删掉无人执行的段数上限、加主因参考线）：
#: ①删除「段数上限 = max(档案下限, 汉字数÷21)」——该上限既不在闸门判、也不被执行，
#: 提示不该说没人执行的话；②段长缺口拆成 `段均句数 × 句长均值` 后，主因指标
#: （`句长均值`）以**参考线**进提示（p10/p50/p90 + 本书最近 14 章实测），非硬判罚；
#: ③`段长均值` 本身仍只记录、不设目标。判罚项与判罚阈值均未变。
#: 2026-11-15 升 v14（**升版原因只有一条**：提示里的三处写死值改为档案现推，
#: 判罚项与判罚阈值一字未改）：
#: ①「≤5 字短句至少 16 句 / ≥40 字长句至少 9 句」不再从常量表 `GEN_GATE_LIMITS`
#: 取块级 p10，改为「目标汉字数 × 参考书章级『句数 ÷ 汉字数』p50」折算总句数
#: （旧实现的字符/汉字 1.3 手写换算退出推导，仅在该项删档时退回并记 fallback），
#: 再乘参考书 **卷口径**短句/长句占比 p10（换参考书自动重推）；
#: ②「对白按每 10 句配比 5/2/2/2」改为按参考书**章级**对白陈述/问句/感叹/叹词
#: 分位折算到每 10 句（比例不再手写；配置项只作退回）；
#: ③`_VOICE_GENERIC_MARKERS` 无法由频次分位生成（频次分不出「泛称」与「高频主角」），
#: 降级为配置项 `voice_generic_markers`，原表作为退回值并在写死值审计文档登记。
#: 2026-11-15 升 v15（**判罚项与判罚阈值仍然一字未改**）：
#: ①删除提示里「≤5 字短句至少 N 句 / ≥40 字长句至少 M 句」的绝对句数下限——
#: v14 折算出的 30 / 20 在第 31 章实到约 17 / 8 句，既没被执行、闸门也不判，
#: 写着不执行的指令只会制造「已经在管」的假象；短句/长句只保留 p10–p90 区间；
#: ②截断稿识别与留痕：`finish_reason=length` / `ends_complete` 两条判据写进
#: `quality_json.truncation`，截断稿来源的终稿不再算合格章（`passed=False`），
#: 截断时优先按配置重生成、预算不足才降级压缩；
#: ③新增结构约束检测（`generation/structure_check.py`）：本章情节点覆盖 /
#: 越章材料 / 人物性别一致，三项全部由 `project.outline` 与角色卡推出，
#: 结果写 `quality_json.structure_check`，是否阻断由 `structure_guard_mode` 决定。
#: 2026-11-15 升 v16（**判罚项与判罚阈值仍然一字未改**）：
#: 把 `retrieval/brief` 的 `search_external_material` 接进初稿：素材轮用非流式
#: tools 取素材（重写轮/压缩轮不给），末轮成文仍走 chat / stream_chat；开关关闭
#: 或未配检索地址时工具不出现在列表里；每章重置次数与秒数预算，章末计数并入
#: `quality_json.brief_tool`。
#: 2026-11-15 升 v17（**判罚项与判罚阈值仍然一字未改**）：
#: 新增三条**只判不拦**判定，结果写 `quality_json` 的三个新字段：
#: - `short_sentence_check`：短句占比偏低，参考线为**章级**分位；
#: - `dialogue_ratio_check`：对白配比超过 p75，参考线为**章级** p25/p50/p75；
#: - `era_check`：时代越界扫描（`era_guard` 打开后恒为 report）。
#: 三条都不改 `passed`、不进 `violations`、不触发重写、不中断生成。
#: 2026-11-15 升 v18（**判罚项与判罚阈值仍然一字未改**）：
#: 1. 时代检查补「前现代」档位（数据在 `world_profile.json.period_lexicon`），
#:    只对 `enabled_baselines`（当前=现代）装载，只收制度机构/货币计量/纪年/
#:    官场称呼四类；不碰宗门/修炼/仙侠题材词。结果按 `violation_notes`
#:    里的 `period_mismatch` 单独计，仍只判不拦。
#: 2. 通道滥用聚合：`channel_hit_no_introducer` 单章达阈值或连续多章出现
#:    写 `quality_json.era_check.channel_abuse`，阈值走 config。
#: 2026-11-15 升 v19（**唯一的改动是「句长类指标的计数口径」以及由它推出的全部
#: 分位/阈值/常量，两者同一次完成**）：
#: 1. 口径：句长 5 项（句长均值 / 句长CV / 短句占比 / 长句占比 / 长句31_39占比）
#:    与句密度分子改用 `stylometry.sentence_lens`，即只统计「至少含一个汉字」的
#:    句子。原因：`split_sentences` 每遇 `…` 就切，`……` 会变成两个单「…」片段，
#:    收尾引号 `”` 也会独立成句；参考书每章中位 14 个这类片段、约占「句数」9%。
#:    旧口径下 `短句占比` 量到的主要是标点碎片密度，不是短句密度。
#: 2. 阈值：`ARCHIVE_BLOCK_PERCENTILES` / `GEN_GATE_LIMITS` 的句长类行、
#:    `_archive_gate_limits` 现推的章级分位、`rhythm_reference` 的短句/长句
#:    参考线、提示里的句数配额，全部按同一把尺重算。只改尺不改阈值会让判罚
#:    立刻全错，故绑死为一次变更。
#: 3. 新增 `quality_json.sentence_shape_check`（只判不拦）：中段拥挤度上限
#:    （参考书池化 p25–p75 窄带内句子占比的章级 p90）与上尾下限
#:    （≥40 字占比的章级 p10）。CV 下限本轮不上。
#: 4. `quality_json.gate_limits.sentence_scope` 记录口径版本；历史章不回改。
#: 判罚项与单章判罚阈值（`CHAPTER_GATE_METRICS` 两项结构项 + 汉字上限）未变。
#:
#: **换尺起点**：`SENTENCE_SCOPE_FROM_CHAPTER = 34`。第 23–33 章的 `quality_json`
#: 里没有 `sentence_scope`，其句长类数值是按 v1（含纯标点片段）算的，**不可与
#: v19 之后的新值直接比较**；跨章趋势一律按 `sentence_scope` 分组看，不混算。
#:
#: 2026-11-15 升 v20（**判罚项未变，只补齐两处遗留的跨尺**）：
#: 1. `段均句数` 换到与句长类同一把尺（`stylometry.paragraph_sentence_counts`：
#:    全文切句后按首字归属到段，`sum(计数) == len(sentence_lens)`）。
#:    v19 只换了 5 项，这一项还按 `split_sentences` 逐段数，导致
#:    `段长均值 ≈ 段均句数 × 句长均值` 跨两把尺、算不通（逐段切会把跨段的一句
#:    数两次）；同批把 `叙述单句段占比` 也换成同一把尺（`== 1` 而非 `<= 1`，
#:    纯标点段不计数）。
#: 2. 短句字长统一为 `SHORT_SENTENCE_MAX_LEN = 4`：`generation_gate` 原来的 ≤5
#:    收回，与 `measure()` / `stylometry.features()` / `projects.style_profile`
#:    对齐。选 4 不选 5 的唯一理由：档案侧已按 ≤4 落库（参考带、`style_baselines`、
#:    全部 `style_checks` 历史），改成 5 要重算所有项目的档案；生成侧只有一处分支。
#: 3. 阈值同批重算：`ARCHIVE_BLOCK_PERCENTILES` 的 `短句占比` / `段均句数` /
#:    `叙述单句段占比` 三行换了新值，`GEN_GATE_LIMITS` 由其自动翻带；章级分位
#:    由 `_archive_gate_limits` 运行时现推，自动跟随。
#: 4. `quality_json.gate_limits.short_sentence_max_len` 记录字长阈值，便于事后判读。
#: 历史章不回改；**第 23–33 章的 `短句占比` 因此是「含碎片 + ≤5 字」双重旧口径**，
#: 与 v20 之后既不可比、也不可换算（换算需要逐句重算）。
#:
#: 2026-11-15 升 v21（**判罚项、判罚阈值、`passed` 口径一字未改**）：
#: `sentence_shape_check` 增加 `verdict`，把「拥挤度」与「上尾」联判：
#: 上尾（≥40 字占比）低于章级 p10 时，「不拥挤」不再单独成立，记
#: `crowd_conclusion="voided"`（`voided_by="long_ratio_below_floor"`）；
#: `crowded` 仍然照旧成立（中段堆压与上尾是否够长是两件事）。
#: 仍是只判不拦：不进 violations、不触发重写、不改 passed。
#: 理由：第 40 章拥挤度 0.5196 达标、≥40 字占比 0.011（下限 0.0659），
#: 台账却把「拥挤度达标」记成变好——句子被整体压短时，中段占比低是机械结果。
#: 2026-11-15 升 v22（**判罚项未变，只改段数下限的口径**）：
#: `段数` 下限从「参考书章级 p10 的绝对段数」改为「每千字段数 × 目标字数 / 1000」。
#: 档案带写入 `per_1k = p10 ÷ 4,000 × 1000`（`source="reference_p10_per_1k"`），
#: 无参考书时用常量 `17.5 段/千字`（`source="constant_per_1k"`）。
#: 理由：4,000 字目标下 17.5×4 = 70，与项目 6 现行值一致；而此前 1,200 字目标也
#: 要求 70 段，空项目（无参考书）必判不合格，且没有参考带可判时重写块会格式化
#: 一个不存在的下限。历史章不回改：第 23–43 章只读回放后段数判定逐章一致。
GEN_GATE_VERSION = "gen-gate-v22"

#: 句长口径 v2 的生效起点章号（第 33 章及以前按 v1 统计，不回改、不重算）。
#: 这个数字会写进每章的 `quality_json.gate_limits.sentence_scope_from_chapter`，
#: 便于以后回答「哪一章前后不可比」。
SENTENCE_SCOPE_FROM_CHAPTER = 34

#: 长度硬闸：汉字数相对目标字数的上限倍数。
#: 口径用**汉字数**（`hanzi_count`），与 `projects.generation_length` 同一单位；
#: 不把标点、空白、换行算进字数。
LENGTH_LIMIT_RATIO = 1.2

#: 未配置 `generation_length` 时的默认目标字数（与 models.Project 默认值一致）。
#:
#: 2026-11-13 由 3,000 提到 4,000：以 id=23（角色声音校准版，5,224 汉字）为底稿
#: 做定向压缩时，3,000（上限 3,600）压不下 5,224 字而不伤情节——实测 5,224 → 3,244
#: 的那一版（id=24）靠删对白之间的叙述句才压到线内，对白结构反而退化
#: （极短纯对白段 16.0%、消息段字数中位 15）。保留对白之间叙述的压缩下限约
#: 4,000，故目标放宽到 4,000、硬上限 4,800。
DEFAULT_TARGET_LENGTH = 4000

#: 段数下限的折算基准（汉字数）。参考书档案里的「段数 p10」是在整章上量的
#: **绝对段数**（项目 6 的参考书 = 70），而平台生成单元的目标字数是 4,000；
#: 把 p10 折成「每千字段数」时用它作基准（p10 ÷ 4）。这样 4,000 字目标下折算值
#: 恒等于档案 p10（项目 6 仍是 70），更短的目标按同一比例下降（1,200 字 → 21），
#: 不会出现「1,200 字目标也要求 70 段」这种永远凑不到的绝对下限。
PARAGRAPH_FLOOR_BASIS_CHARS = 4000

#: 无参考书（或档案里没有段数分位）时的**退回系数**：段/千字。
#: 17.5 = 项目 6 现行下限 70 段 ÷ 4,000 字 × 1,000，与参考书推导值同源；
#: 手写的只有「无档案时用这个数」这一件事，不是新增判据。
PARAGRAPH_FLOOR_FALLBACK_PER_1K = 17.5


def hanzi_count(text: str) -> int:
    """正文汉字数。**只用于文风指标**（段均/句长/对白占比等），不参与篇幅判界。"""
    return sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")


def char_count(text: str) -> int:
    """正文字符数——篇幅预算的**唯一口径**（`len()` 原样，含标点与换行）。

    2026-11-17 统一：此前上限（120%）比汉字、下限（80%）比字符，同一份稿子
    两个数（换书验证实测一章 3,451 字符 / 2,737 汉字，汉字只占 79.3%），
    上限按汉字量，120% 等效 151% 才拦。现在 `analyze_length` /
    `generation_gate` / `_rewrite_rounds` / `length_guard` 全部走这一个计数。
    """
    return len(text or "")


def length_limits(target_chars: int | None = None) -> dict[str, int]:
    """目标字数 → {目标, 上限}；上限取目标 ×`LENGTH_LIMIT_RATIO`（向下取整）。"""
    target = int(target_chars or DEFAULT_TARGET_LENGTH)
    return {"target": target, "max": int(target * LENGTH_LIMIT_RATIO)}


#: 极短纯对白段的判定上限（汉字数）——与 `GEN_GATE_LIMITS` 的定义同口径。
ULTRA_SHORT_DIALOGUE_MAX_HANZI = 4

_QUOTE_PAIRS = {"“": "”", "「": "」", "『": "』", '"': '"'}
_QUOTE_CHARS = "“”\"「」『』"


def is_pure_dialogue_paragraph(para: str) -> bool:
    """整段是否**只有一句引号对白**（引号外没有任何叙述/归属）。"""
    s = (para or "").strip()
    if len(s) < 2:
        return False
    close = _QUOTE_PAIRS.get(s[0])
    if not close or not s.endswith(close):
        return False
    return not any(ch in s[1:-1] for ch in _QUOTE_CHARS)


def _speaker_from(para: str, names: list[str]) -> str:
    """从相邻叙述段里取一个已知角色名做归属；取不到就返回空串（不臆造人名）。"""
    for name in sorted(names, key=len, reverse=True):
        if name and name in para:
            return name
    return ""


def _para_sentence_count(para: str) -> int:
    """段内句数（v20 起与闸门同一把尺：只数含汉字的句子）。

    只用于判邻居形态（是不是多句叙述段）；`spc_before` 与并段上限用
    `len(stylometry.sentence_lens(全文))`——那才是与闸门 `段均句数` 一致的口径，
    且并段只改段数、不改全文句数，可以直接用来预测并完之后的段均。
    """
    return len(stylometry.sentence_lens(stylometry.strip_edge_quotes(para)))


def merge_ultra_short_dialogue(
    text: str, names: list[str] | None = None, max_hanzi: int | None = None,
    spc_cap: float | None = None,
) -> tuple[str, dict]:
    """把「整段只有引号、且 ≤ 极短上限汉字」的对白段**确定性并入相邻段**。

    极短上限默认取常量 4；调用方可用 `max_hanzi` 传入按参考档案推出的值
    （见 `engine._archive_rhythm`：纯对白段汉字数 P10，夹在 [2,12]）。
    推导值最终与常量取较大值——闸门仍按 ≤4 统计极短段，向下放宽会留下判不过的段。

    为什么必须做成后处理：`prompts` 里已经写了「极短纯对白段必须为 0」，
    第 4 章实测仍稳定产出 13.7%（21 段 / 146 段）——模型并不遵守这条。这里
    不依赖模型，按固定规则合并，合并后由闸门重算指标。

    ## 合并目标的选择（2026-11-14 改）

    并段的算术是「段数 -1、句数不变」，所以段均句数只升不降；而**并进一个单句
    叙述段会把该段从 1 句变成 2 句——等于主动压掉参考书双峰结构里的「单句段」
    峰**（第 16 章实测：并段把叙述单句段 28.9% → 17.4%，段均句数 1.61 → 2.13，
    已越过参考 p90≈2.10）。故优先级改为：

    1. 相邻**非单句**叙述段（剥引号后 ≥2 句）→ 并进去不会吃掉任何单句段；
       前后都可用时取汉字更短的那段（少让一个段变得更长）。
       能取到已知角色名就补 `某某问/说：`，取不到就只并段，不臆造人名；
    2. 相邻**纯对白段** → 并两个对白不改变叙述单句段的计数；
    3. 只剩单句叙述段或无处可并 → **不合并**，留给重写轮
       （记 `skipped_single_neighbor`）。

    ## 段均句数硬约束

    每次合并前算 `总句数 / (段数 - 1)`，超过 `spc_cap`（参考语料章级 p90，
    由调用方传入）就停止本函数后续所有合并并记 `stopped_by_spc_cap`。
    宁可让「段长均值」不达标（那是记录项），也不许为了抬段均好看而把段均句数
    推过参考上沿。

    Args:
        text: 正文。
        names: 允许使用的角色名（通常传 `appeared_names`）；用于补归属。
        max_hanzi: 极短纯对白段的汉字上限（档案 P10，夹 [2,12]）。
        spc_cap: 段均句数上限（参考章级 p90）；None 表示不设上限。

    Returns:
        `(新正文, 统计)`。统计只含计数与样本，不把整段正文塞进 quality_json。
    """
    paras = [p.strip() for p in stylometry.paragraphs(text or "") if p.strip()]
    limit = int(max_hanzi) if isinstance(max_hanzi, int) and max_hanzi > 0 else ULTRA_SHORT_DIALOGUE_MAX_HANZI
    # 闸门统计「极短纯对白段」的口径仍固定为 ≤ULTRA_SHORT_DIALOGUE_MAX_HANZI（见
    # stylometry.dialogue_profile）。推导值只向上适配（档案对白更长时并得更狠），
    # 不向下放宽——否则 3–4 字的段会留下来继续判不过闸门，白跑一轮重写。
    limit = max(limit, ULTRA_SHORT_DIALOGUE_MAX_HANZI)
    cap = float(spc_cap) if isinstance(spc_cap, (int, float)) and spc_cap and spc_cap > 0 else None
    # v20：句数用**全文**口径（`sentence_lens`），与闸门 `段均句数` 同一把尺。
    # 并段只减少段数、不改变全文句数，所以这个总数可以直接预测并完之后的段均，
    # 不会因为「按段切句把跨段的一句话数两次」而虚高。
    total_sents = len(stylometry.sentence_lens(text))
    info: dict[str, Any] = {
        "rule": "ultra_short_dialogue_merge",
        "max_hanzi": limit,
        "spc_cap": round(cap, 3) if cap else None,
        "before_paras": len(paras),
        "spc_before": round(total_sents / len(paras), 3) if paras else None,
        "merged": 0,
        "attributed": 0,
        # 被合并掉的**形态**只有一种：极短纯对白段（本函数只搬它）。
        # 下面按接收段形态拆分，用来区分「初稿自然偏碎」与「并段在压段长」：
        # - prev/next：并入多句叙述段（不会吃掉单句段峰）；
        # - prev_dlg/next_dlg：两个纯对白段对并；
        # - skipped_single_neighbor：只剩单句叙述段邻居，按设计**不并**。
        "merged_into": {"prev": 0, "next": 0, "prev_dlg": 0, "next_dlg": 0},
        "skipped_single_neighbor": 0,
        "stopped_by_spc_cap": 0,
        "samples": [],
    }
    if not paras:
        info["after_paras"] = 0
        info["spc_after"] = None
        return text, info

    known = [str(n) for n in (names or []) if n and len(str(n)) >= 2]
    out: list[str] = []
    i = 0
    while i < len(paras):
        p = paras[i]
        ultra = is_pure_dialogue_paragraph(p) and hanzi_count(p) <= limit
        if not ultra:
            out.append(p)
            i += 1
            continue

        # 每并一次段数 -1、句数不变，段均句数只升不降：这一条越线，后面只会更越线，
        # 直接停止全部合并（剩余极短段交给重写轮）。
        if cap is not None and total_sents / max(1, len(paras) - info["merged"] - 1) > cap:
            info["stopped_by_spc_cap"] += 1
            out.extend(paras[i:])
            break

        prev_p = out[-1] if out else None
        next_p = paras[i + 1] if i + 1 < len(paras) else None
        # 邻居类型按**原始段落**判，不按已合并后的 out[-1] 判：否则
        # 「叙述 + 第一句对白」并完就变成叙述，紧接着的第二句对白会被再补一次
        # 人名（两个角色的问答会被错记成同一个人自问自答）。
        prev_dlg = i > 0 and is_pure_dialogue_paragraph(paras[i - 1])
        next_dlg = next_p is not None and is_pure_dialogue_paragraph(next_p)
        # 「非单句叙述段」才是首选目标：并进去不会把任何段的句数从 1 抬到 ≥2。
        prev_open = prev_p is not None and not prev_dlg and _para_sentence_count(prev_p) >= 2
        next_open = next_p is not None and not next_dlg and _para_sentence_count(next_p) >= 2

        target = ""
        if prev_open and next_open:
            # 两段都能并时取更短的那段，别把原本就长的段再拉长。
            target = "prev" if hanzi_count(prev_p) <= hanzi_count(next_p) else "next"
        elif prev_open:
            target = "prev"
        elif next_open:
            target = "next"
        elif prev_dlg:
            target = "prev_dlg"
        elif next_dlg:
            target = "next_dlg"

        if target == "prev":
            speaker = _speaker_from(prev_p, known)
            if speaker:
                body = p[1:-1]
                verb = "问" if ("？" in body or "?" in body) else "说"
                out[-1] = f"{prev_p}{speaker}{verb}：{p}"
                info["attributed"] += 1
            else:
                out[-1] = prev_p + p
            i += 1
        elif target == "next":
            out.append(p + next_p)
            i += 2
        elif target == "prev_dlg":
            out[-1] = prev_p + p
            i += 1
        elif target == "next_dlg":
            out.append(p + next_p)
            i += 2
        else:
            # 前后只剩单句叙述段（或无处可并）：不合并，保住参考书双峰结构里的
            # 「单句段」峰；该段由重写轮处理。
            out.append(p)
            info["skipped_single_neighbor"] += 1
            i += 1
            continue

        info["merged"] += 1
        info["merged_into"][target] = info["merged_into"].get(target, 0) + 1
        if len(info["samples"]) < 5:
            info["samples"].append(p[:12])

    info["after_paras"] = len(out)
    info["spc_after"] = round(total_sents / len(out), 3) if out else None
    return "\n\n".join(out), info

#: 低于此字数不判（与 measure 的 MIN_JUDGE_CHARS 同源；短文本分布不稳）。
GEN_GATE_MIN_CHARS = MIN_JUDGE_CHARS

#: novel 7 块级分位（同一函数、同一口径；见上方表）。
#:
#: v20 重算（2026-11-15）：`短句占比` 改 ≤4 字、`段均句数` 与 `叙述单句段占比`
#: 改走 `stylometry.paragraph_sentence_counts`（全文切句后归属到段）。
#: 三项与本表其余行的口径现已完全一致，可直接用于
#: `段长均值 ≈ 段均句数 × 句长均值` 的三角校核。旧值见 v20 changelog。
#:
#: `段均句数` 不在九项判界指标里，只用于**软档观测**与段长诊断：口径为
#: `stylometry.paragraph_sentence_counts`——全文切句、再按句子起始字符归属到段，
#: 与「叙述单句段」和「句长均值」是同一把尺（`sum(计数) == 句数`）。
ARCHIVE_BLOCK_PERCENTILES: dict[str, dict[str, float]] = {
    "句长均值": {"p10": 18.2, "p25": 19.92, "p50": 22.0, "p75": 24.34, "p90": 26.78},
    "句长CV": {"p10": 0.497, "p25": 0.543, "p50": 0.593, "p75": 0.646, "p90": 0.703},
    "短句占比": {"p10": 0.0, "p25": 0.0, "p50": 0.029, "p75": 0.054, "p90": 0.08},
    "长句占比": {"p10": 0.037, "p25": 0.062, "p50": 0.104, "p75": 0.150, "p90": 0.205},
    "长句31_39占比": {"p10": 0.057, "p25": 0.086, "p50": 0.122, "p75": 0.163,
                      "p90": 0.204},
    "段长均值": {"p10": 24.1, "p25": 27.5, "p50": 31.6, "p75": 35.6, "p90": 41.1},
    "问号每千字": {"p10": 1.48, "p25": 2.85, "p50": 5.00, "p75": 7.55, "p90": 10.83},
    "叙述单句段占比": {"p05": 0.222, "p25": 0.385, "p50": 0.500,
                       "p75": 0.621, "p90": 0.719},
    "对白字占比": {"p10": 0.065, "p25": 0.126, "p50": 0.226,
                   "p75": 0.349, "p90": 0.464},
    "段均句数": {"p10": 1.162, "p25": 1.286, "p50": 1.444,
                 "p75": 1.647, "p90": 1.867},
}


def _first_positive_pct(row: dict[str, float]) -> float:
    """分位行里第一个大于 0 的分位。

    v19 起 `短句占比` 在**块级**干净口径下 p10 = 0（>10% 的 1,200 字块没有
    ≤5 字句），直接拿 p10 当下限等于「不许有短句」。改用第一个非零分位。
    v20 把短句字长收到 ≤4 之后 p25 也变成 0（>25% 的块没有任何 ≤4 字句），
    因此现行下限落到 p50 = 2.9%；这条规则本身不变，只是取的档位跟着口径走。
    """
    for key in ("p10", "p25", "p50", "p75", "p90"):
        v = row.get(key)
        if isinstance(v, (int, float)) and float(v) > 0:
            return float(v)
    return 0.0


def limits_from_archive(
    table: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, float]]:
    """按一条统一规则把档案分位翻成闸门带。

    - 双侧指标：p10–p90（只留档案中间 80%，两端各 10% 视为离群）；
    - 只设下限：p10；只设上限：p90；
    - `短句占比` 下限取**第一个非零分位**（v19：干净口径下块级 p10 = 0；
      v20 短句改 ≤4 后 p10/p25 都是 0，下限落到 p50 = 2.9%）；
    - 两个例外（用户 2026-11-13 指定）：段长均值下限取 p25（p10=24.1 恰好等于
      被否掉的旧地板 24），叙述单句段上限取 p75（v20 起 p90=73.0%，等于不设限）。
    """
    t = table or ARCHIVE_BLOCK_PERCENTILES
    return {
        "句长均值": {"min": t["句长均值"]["p10"], "max": t["句长均值"]["p90"]},
        "句长CV": {"min": t["句长CV"]["p10"]},
        "短句占比": {
            "min": _first_positive_pct(t["短句占比"]),
            "max": t["短句占比"]["p90"],
        },
        "长句占比": {"min": t["长句占比"]["p10"], "max": t["长句占比"]["p90"]},
        "长句31_39占比": {
            "min": t["长句31_39占比"]["p10"],
            "max": t["长句31_39占比"]["p90"],
        },
        "段长均值": {"min": t["段长均值"]["p25"], "max": t["段长均值"]["p90"]},
        "叙述单句段占比": {"max": t["叙述单句段占比"]["p75"]},
        "对白字占比": {"min": t["对白字占比"]["p10"]},
        "问号每千字": {"max": t["问号每千字"]["p90"]},
        # 观测项（2026-11-14）：只记录、不判单章；上限同时是确定性并段的硬约束
        # （`merge_ultra_short_dialogue(spc_cap=...)`）。
        "段均句数": {"min": t["段均句数"]["p10"], "max": t["段均句数"]["p90"]},
    }


#: 章级对白档案（2026-11-13 实测：novel 7 全本 3,170 章，`stylometry.dialogue_profile`
#: 同一函数；分位线性插值）。
#:
#: 为什么单列一张表：生成单元约 3,000 字，与「章」同量级；而 1,200 字块级会把
#: 一场群聊切成两半，纯对白段/消息段这类**场景结构**指标在块级被稀释到几乎全 0。
#: 新增项一律只从这张表取数，不与上面九项的块级表混用。
ARCHIVE_CHAPTER_PERCENTILES: dict[str, dict[str, float]] = {
    "对白句长中位": {"p10": 4.0, "p25": 5.0, "p50": 7.0, "p75": 9.0, "p90": 11.5},
    "对白≤4占比": {"p10": 0.206, "p25": 0.262, "p50": 0.349,
                   "p75": 0.457, "p90": 0.581},
    "对白5–9占比": {"p10": 0.136, "p25": 0.182, "p50": 0.232,
                    "p75": 0.288, "p90": 0.345},
    "对白10–19占比": {"p10": 0.100, "p25": 0.165, "p50": 0.235,
                      "p75": 0.297, "p90": 0.348},
    "对白≥20占比": {"p10": 0.045, "p25": 0.098, "p50": 0.157,
                    "p75": 0.214, "p90": 0.271},
    "纯对白段占比": {"p10": 0.0, "p25": 0.0, "p50": 0.0,
                     "p75": 0.0, "p90": 0.026},
    "极短纯对白段占比": {"p10": 0.0, "p25": 0.0, "p50": 0.0,
                         "p75": 0.0, "p90": 0.0},
    "纯对白段字数中位": {"p10": 3.0, "p25": 8.0, "p50": 15.0,
                         "p75": 27.0, "p90": 51.5},
    "语气词密度千字": {"p10": 1.65, "p25": 2.9, "p50": 4.69,
                       "p75": 7.04, "p90": 9.871},
    "含助词对白句占比": {"p10": 0.035, "p25": 0.071, "p50": 0.122,
                         "p75": 0.179, "p90": 0.244},
    "对白问句占比": {"p10": 0.053, "p25": 0.101, "p50": 0.159,
                     "p75": 0.214, "p90": 0.266},
    "对白感叹句占比": {"p10": 0.0, "p25": 0.018, "p50": 0.045,
                       "p75": 0.091, "p90": 0.158},
    "对白陈述句占比": {"p10": 0.633, "p25": 0.708, "p50": 0.782,
                       "p75": 0.848, "p90": 0.905},
    "对白叹词起头占比": {"p10": 0.0, "p25": 0.0, "p50": 0.026,
                         "p75": 0.053, "p90": 0.079},
    "消息段字数中位": {"p10": 28.5, "p25": 32.0, "p50": 36.0,
                       "p75": 41.0, "p90": 45.0},
    "纯消息段占比": {"p10": 0.0, "p25": 0.0, "p50": 0.0,
                     "p75": 0.0, "p90": 0.065},
    "消息含助词占比": {"p10": 0.067, "p25": 0.118, "p50": 0.191,
                       "p75": 0.281, "p90": 0.371},
    "消息平均间隔": {"p10": 0.62, "p25": 0.84, "p50": 1.2,
                     "p75": 1.697, "p90": 2.34},
}

#: 对白专项硬闸门（2026-11-13 v5）：全部从章级分位取，规则与 v3 一致——
#: 下限 p10、上限 p90（不手定）。
#:
#: v5 调整：
#: - `纯对白段占比` / `纯消息段占比` **退出硬判**，只进软档：长消息独立成段不算碎，
#:   总占比会把「一条 40 字的消息单列」和「一句『嗯。』单列」算成同一件事；
#: - 硬判保留 `极短纯对白段占比`（段内只有引号且极短）与 `消息段字数中位`；
#: - 补 `对白感叹句占比` / `对白叹词起头占比` 的下限。
#:
#: 后两项的档案 p10 都是 0（10% 的章本来没有感叹句/叹词），直接拿 p10 当下限
#: 等于不设限，与「不要再往 0 压」相悖。故取**第一个非零分位**：
#: 感叹句 p25=1.8%，叹词起头 p50=2.6%。这条替代规则写在这里，便于随时改回。
def _first_positive(key: str) -> float:
    pct = ARCHIVE_CHAPTER_PERCENTILES[key]
    for field in ("p10", "p25", "p50", "p75", "p90"):
        if pct.get(field, 0) > 0:
            return float(pct[field])
    return 0.0


DIALOGUE_GATE_LIMITS: dict[str, dict[str, float]] = {
    "极短纯对白段占比": {
        "max": ARCHIVE_CHAPTER_PERCENTILES["极短纯对白段占比"]["p90"],
    },
    "消息段字数中位": {"min": ARCHIVE_CHAPTER_PERCENTILES["消息段字数中位"]["p10"]},
    "对白≥20占比": {"min": ARCHIVE_CHAPTER_PERCENTILES["对白≥20占比"]["p10"]},
    "对白感叹句占比": {"min": _first_positive("对白感叹句占比")},
    "对白叹词起头占比": {"min": _first_positive("对白叹词起头占比")},
}

GEN_GATE_LIMITS: dict[str, dict[str, float]] = {
    **limits_from_archive(),
    **DIALOGUE_GATE_LIMITS,
}

# --------------------------------------------------------------------------- #
# 1c. 判罚单位（2026-11-14 定稿）：单章只判结构合规，文风指纹按卷看趋势
# --------------------------------------------------------------------------- #
#
# 依据：用参考语料自己的**逐章**分位（novel 7，3,169 章，同一把尺）判「跨章是否
# 稳定」。逐章分位显示，句长CV 这类指标本书与参考书**不同源**：已写 12–13 章
# 按参考带全部不达标，100% 失败的线没有判别力。故改为三层：
#
#   1) 单章硬判 = 结构合规（越界就是排版坏了，与题材无关）：
#      汉字上限（`length_limits`）、极短纯对白段占比、最少段数；
#      `段长均值` 自 v8 起退出单章硬判，改为卷级趋势（见 3）；
#   2) 文风指纹 13 项：单章只记录实测值，**不进 violations**；
#   3) 按卷判：卷均值 vs 参考语料同长度连续块的卷均值，看差距**趋势**——
#      差距稳定＝这本书的文风指纹，逐卷扩大才是真的在漂（`volume_gate()`）。
#
# 阈值不写死：`limits_from_chapter_percentiles()` 从参考语料章级分位推结构带；
# 卷对照的参考均值/离散度同样现推（`volume_reference()` 由 engine 抽样给出）。
CHAPTER_GATE_METRICS: tuple[str, ...] = ("极短纯对白段占比", "段数")

#: 文风指纹 13 项：单章只记录、不判违规；按卷与参考卷均值对照看趋势。
#: `段均句数`（2026-11-14 加入）不是判界项，但它是「单句段」与「段均长」之间的
#: 桥梁：并段会同时抬升段均长、段均句数，并把单句段压下去，必须单独可见。
#: `段长均值`（v8 加入）：退出单章硬判后仍在指纹里逐章记录，卷末按参考语料
#: 连续 14 章块均值的分位看趋势——单章偏短是排版选择，连续多卷同向偏移才是漂。
FINGERPRINT_METRICS: tuple[str, ...] = (
    "句长CV", "短句占比", "长句占比", "长句31_39占比", "叙述单句段占比",
    "对白字占比", "问号每千字", "消息段字数中位", "对白≥20占比",
    "对白感叹句占比", "对白叹词起头占比", "段均句数", "段长均值",
)

#: 卷长（章）的**默认/退回值**。优先由 engine 从项目大纲的卷结构解析；解析不到
#: 才退回这里（再退回 `settings.volume_chapters`）。卷 1 = 1–14 章（用户指定）；
#: 参考语料用同样的连续块推卷均值，两边同单位才可比。这不是阈值。
VOLUME_CHAPTERS = 14

#: 运行时闸门带（由 engine 按项目参考语料抽样算出后写入；未写入时用常量表）。
_ACTIVE_JUDGE = contextvars.ContextVar("gen_gate_judge_limits", default=None)
_ACTIVE_FULL = contextvars.ContextVar("gen_gate_full_limits", default=None)
#: 对白配比**参考线**（v12，非判罚）：参考书章级「对白字占比」p25/p50/p75，
#: 外加本书最近一卷实测值。由 engine 现推写入，提示只读不判。
_ACTIVE_DIALOGUE_REF = contextvars.ContextVar("gen_gate_dialogue_reference", default=None)
#: 主因指标参考线（v13，非判罚）：参考书卷口径「句长均值」p10/p50/p90，
#: 外加本书最近一卷实测均值。段长均值本身仍只记录、不设目标。
_ACTIVE_SENTENCE_REF = contextvars.ContextVar("gen_gate_sentence_reference", default=None)
#: 句长/短句/长句节奏参考（v14，非判罚）：参考书**卷口径**的
#: `短句占比`/`长句占比` p10/p50/p90 与 `句长均值` p50（折算总句数用），
#: 外加本书最近一卷实测均值。提示据此折算绝对句数，不再读常量表。
_ACTIVE_RHYTHM_REF = contextvars.ContextVar("gen_gate_rhythm_reference", default=None)
#: 对白句类配比参考（v14，非判罚）：参考书**章级**对白陈述/问句/感叹/叹词起头
#: 分位，提示折算成「每 10 句」的句数，替代手写的 5/2/2/2。
_ACTIVE_MIX_REF = contextvars.ContextVar("gen_gate_dialogue_mix_reference", default=None)

#: 档案缺数据/无参考书时的退回值：单章只判两项结构项，值仍是 v5 的常量表。
#: `段数` 没有对应常量表项（v5 不判段数），退回值与推导规则**同源**：
#: 70 = 本书参考书（novel 7，560 章）的**章级 p10**，即
#: `_archive_gate_limits` 里 `percentile_row(段数)["p10"]` 的口径；此前写死 100
#: 与推导值不一致。只在 `_ACTIVE_JUDGE` 未写入（无参考书/有效章不足/推导异常）
#: 时生效，并由 engine 在 `quality_json.gate_limits.fallback_used` 显式留痕。
#: v22 起 70 是 **4,000 字基准**下的常量：`generation_gate` 会按项目目标字数
#: 折算（`paragraph_floor_limits`，17.5 段/千字 → 1,200 字目标为 21 段），
#: 不再让短目标套用 4,000 字的绝对下限。
CHAPTER_GATE_LIMITS: dict[str, dict[str, float]] = {
    k: GEN_GATE_LIMITS[k] for k in CHAPTER_GATE_METRICS if k in GEN_GATE_LIMITS
}
CHAPTER_GATE_LIMITS.setdefault("段数", {"min": 70.0})


def active_gate_limits() -> dict[str, dict[str, float]]:
    """当前生效的**单章**判罚带（只有 `CHAPTER_GATE_METRICS` 两项结构项）。"""
    return _ACTIVE_JUDGE.get() or CHAPTER_GATE_LIMITS


def active_full_limits() -> dict[str, dict[str, float]]:
    """当前生效的全量参考带（15 项，供重写提示引用，不直接判单章）。"""
    return _ACTIVE_FULL.get() or GEN_GATE_LIMITS


def set_gate_limits(judge, full=None) -> None:
    """设置本 context 的闸门带。由 `engine` 在每次生成开始时调用。

    用 `contextvars` 而不是模块级可变全局：生成跑在线程里，两个项目并发生成时
    不会互相覆盖；同一线程的下一次生成会重新 set。
    """
    if judge:
        _ACTIVE_JUDGE.set(judge)
    if full:
        _ACTIVE_FULL.set(full)


def active_dialogue_reference() -> dict[str, Any] | None:
    """当前生效的对白配比参考线（p25/p50/p75 + 本书实测）；未写入返回 None。"""
    return _ACTIVE_DIALOGUE_REF.get()


def set_dialogue_reference(ref: dict[str, Any] | None) -> None:
    """设置本 context 的对白配比参考线。与 `set_gate_limits` 同生命周期。"""
    if ref:
        _ACTIVE_DIALOGUE_REF.set(ref)


def active_sentence_reference() -> dict[str, Any] | None:
    """当前生效的「句长均值」参考线（p10/p50/p90 + 本书实测）；未写入返回 None。"""
    return _ACTIVE_SENTENCE_REF.get()


def set_sentence_reference(ref: dict[str, Any] | None) -> None:
    """设置本 context 的句长均值参考线。与 `set_gate_limits` 同生命周期。"""
    if ref:
        _ACTIVE_SENTENCE_REF.set(ref)


def active_rhythm_reference() -> dict[str, Any] | None:
    """当前生效的短句/长句/句长节奏参考；未写入返回 None。"""
    return _ACTIVE_RHYTHM_REF.get()


def set_rhythm_reference(ref: dict[str, Any] | None) -> None:
    """设置本 context 的短句/长句节奏参考。与 `set_gate_limits` 同生命周期。"""
    if ref:
        _ACTIVE_RHYTHM_REF.set(ref)


def active_dialogue_mix_reference() -> dict[str, Any] | None:
    """当前生效的对白句类配比参考；未写入返回 None。"""
    return _ACTIVE_MIX_REF.get()


def set_dialogue_mix_reference(ref: dict[str, Any] | None) -> None:
    """设置本 context 的对白句类配比参考。与 `set_gate_limits` 同生命周期。"""
    if ref:
        _ACTIVE_MIX_REF.set(ref)


def percentile_row(values: Iterable[float]) -> dict[str, float]:
    """一组数值 → p10/p25/p50/p75/p90；少于 2 个样本返回空（不臆造）。"""
    sv = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if len(sv) < 2:
        return {}
    out: dict[str, float] = {}
    for q in (0.10, 0.25, 0.50, 0.75, 0.90):
        v = _quantile(sv, q)
        if v is not None:
            out[f"p{int(round(q * 100))}"] = round(v, 4)
    return out


def _row_first_positive(row: dict[str, float]) -> float:
    for field in ("p10", "p25", "p50", "p75", "p90"):
        v = row.get(field)
        if v and v > 0:
            return float(v)
    return 0.0


def _row_or_default(pct, key: str, field: str, fallback) -> float:
    v = (pct.get(key) or {}).get(field)
    return round(float(v), 4) if v is not None else float(fallback or 0.0)


def paragraph_floor_limits(
    limits: dict[str, dict[str, float]] | None,
    target_chars: int | None,
) -> dict[str, dict[str, float]] | None:
    """把 `段数` 下限按目标字数线性折算；其余指标原样。

    规则：`min = per_1k × 目标字数 / 1000`。

    - 档案推导的带里已带 `per_1k`（`source="reference_p10_per_1k"`）→ 直接折算；
    - 只有绝对下限（如无参考书时的常量带 70）→ 按
      `min ÷ PARAGRAPH_FLOOR_BASIS_CHARS × 1000` 反推系数并标
      `source="constant_per_1k"`，保证同一份常量在短目标下也按比例下降；
    - 没有 `段数`、没有下限、或下限为 0 → **原样返回，不臆造阈值**。
      这类情况闸门不判它（`generation_gate` 跳过 None 分位），重写块也走
      `_gate_hard_block` 的降级分支，绝不会把 None 送进 `format()`。

    幂等：每次都按 `per_1k` 重算 `min`，重复调用不会二次缩放。
    """
    if not limits:
        return limits
    row = limits.get("段数")
    if not row:
        return limits
    per_1k = row.get("per_1k")
    source = str(row.get("source") or "")
    if per_1k is None:
        lo = row.get("min")
        if not lo:
            return limits
        per_1k = float(lo) * 1000.0 / float(PARAGRAPH_FLOOR_BASIS_CHARS)
        source = "constant_per_1k"
    target = int(target_chars or PARAGRAPH_FLOOR_BASIS_CHARS)
    lo = round(float(per_1k) * target / 1000.0, 4)
    out = dict(limits)
    out["段数"] = {
        **row,
        "min": lo,
        "lo": lo,
        "source": source,
        "per_1k": round(float(per_1k), 4),
        "target": target,
        "basis_chars": PARAGRAPH_FLOOR_BASIS_CHARS,
    }
    return out


def limits_from_chapter_percentiles(
    pct: dict[str, dict[str, float]],
    ultra_short_max: float | None = None,
) -> dict[str, dict[str, float]]:
    """章级分位 → 单章参考带（规则与 `limits_from_archive` 一致，换成章级样本）。

    两项例外沿用用户 2026-11-13 指定：段长均值下限取 p25（结构地板）、
    叙述单句段上限取 p75；对白感叹/叹词取第一个非零分位（避免 p10=0 等于不设限）。
    缺项退回常量表，不手拍。

    `ultra_short_max`：极短纯对白段占比上限，由 engine 按**非零章 p90** 现推后传入
    （2026-11-14 / v10）。不传时保持旧行为（全章 p90），供无档案的调用点使用；
    全章 p90 在参考书 6.57% 章节非零时恒为 0，不能当门槛，故生成路径必定传入。
    """
    def d(key: str, field: str):
        return (GEN_GATE_LIMITS.get(key) or {}).get(field)

    # v22：段数下限＝「每千字段数 × 目标字数 / 1000」。档案分位只给出绝对段数，
    # 这里把 p10 折成每千字系数（÷4，基准 4,000 字），由
    # `paragraph_floor_limits()` 按项目目标字数现算；4,000 字目标下仍等于 p10。
    para_p10 = (pct.get("段数") or {}).get("p10")
    if para_p10 is None:
        # 分位里没有段数（无参考书 / 样本不含该指标）：不臆造阈值，沿用旧退回。
        para_limit: dict[str, float] = {
            "min": _row_or_default(pct, "段数", "p10", d("段数", "min")),
        }
    else:
        para_p10 = round(float(para_p10), 4)
        para_limit = {
            "min": para_p10,
            "lo": para_p10,
            "per_1k": round(para_p10 * 1000.0 / PARAGRAPH_FLOOR_BASIS_CHARS, 4),
            "source": "reference_p10_per_1k",
            "basis_chars": PARAGRAPH_FLOOR_BASIS_CHARS,
        }

    return {
        "句长均值": {
            "min": _row_or_default(pct, "句长均值", "p10", d("句长均值", "min")),
            "max": _row_or_default(pct, "句长均值", "p90", d("句长均值", "max")),
        },
        "句长CV": {"min": _row_or_default(pct, "句长CV", "p10", d("句长CV", "min"))},
        "短句占比": {
            "min": _row_or_default(pct, "短句占比", "p10", d("短句占比", "min")),
            "max": _row_or_default(pct, "短句占比", "p90", d("短句占比", "max")),
        },
        "长句占比": {
            "min": _row_or_default(pct, "长句占比", "p10", d("长句占比", "min")),
            "max": _row_or_default(pct, "长句占比", "p90", d("长句占比", "max")),
        },
        "长句31_39占比": {
            "min": _row_or_default(pct, "长句31_39占比", "p10", d("长句31_39占比", "min")),
            "max": _row_or_default(pct, "长句31_39占比", "p90", d("长句31_39占比", "max")),
        },
        "段长均值": {
            "min": _row_or_default(pct, "段长均值", "p25", d("段长均值", "min")),
            "max": _row_or_default(pct, "段长均值", "p90", d("段长均值", "max")),
        },
        "叙述单句段占比": {
            "max": _row_or_default(pct, "叙述单句段占比", "p75",
                                   d("叙述单句段占比", "max")),
        },
        "对白字占比": {
            "min": _row_or_default(pct, "对白字占比", "p10", d("对白字占比", "min")),
        },
        "问号每千字": {
            "max": _row_or_default(pct, "问号每千字", "p90", d("问号每千字", "max")),
        },
        # 观测项（2026-11-14）：章级 p10–p90 现推；不判单章，只做记录 +
        # 并段硬约束的上限。推不出时退回块级档案表（同源，不是手拍数）。
        "段均句数": {
            "min": _row_or_default(pct, "段均句数", "p10",
                                   ARCHIVE_BLOCK_PERCENTILES["段均句数"]["p10"]),
            "max": _row_or_default(pct, "段均句数", "p90",
                                   ARCHIVE_BLOCK_PERCENTILES["段均句数"]["p90"]),
        },
        "极短纯对白段占比": {
            # v10：优先用「非零章 p90」（engine 传入）；无档案时退回常量表。
            "max": (
                round(float(ultra_short_max), 4)
                if ultra_short_max is not None
                else _row_or_default(pct, "极短纯对白段占比", "p90",
                                     d("极短纯对白段占比", "max"))
            ),
        },
        # 结构项（2026-11-14）：最少段数＝参考语料章级 p10。段数少说明章节被写成了
        # 大段块状排版，与题材无关，越界就是排版坏了，故进单章硬判。
        # v22：这里给的是 4,000 字基准下的绝对下限 + `per_1k`，实际判定值由
        # `paragraph_floor_limits()` 按项目目标字数折算（见上方注释）。
        "段数": para_limit,
        "消息段字数中位": {
            "min": _row_or_default(pct, "消息段字数中位", "p10",
                                   d("消息段字数中位", "min")),
        },
        "对白≥20占比": {
            "min": _row_or_default(pct, "对白≥20占比", "p10", d("对白≥20占比", "min")),
        },
        "对白感叹句占比": {
            "min": round(_row_first_positive(pct.get("对白感叹句占比") or {})
                         or float(d("对白感叹句占比", "min") or 0.0), 4),
        },
        "对白叹词起头占比": {
            "min": round(_row_first_positive(pct.get("对白叹词起头占比") or {})
                         or float(d("对白叹词起头占比", "min") or 0.0), 4),
        },
    }


def volume_block_means(
    blocks: Iterable[Iterable[dict]],
    metrics: tuple[str, ...] = FINGERPRINT_METRICS,
    window: int = VOLUME_CHAPTERS,
) -> list[dict[str, float]]:
    """参考语料**连续 `window` 章**的块 → 每块的 13 项均值（卷均值的参照样本）。

    必须用连续块而不是「等距抽 14 章」：后者跨越几百章，均值方差被人为压小，
    我们自己的 14 章卷反而更容易被判成离群。窗口不通长（不足 `window` 章）的
    块直接丢弃，不拿短窗凑（与「窗口不足不判」同一纪律）。
    """
    out: list[dict[str, float]] = []
    for block in blocks:
        rows = list(block)
        if len(rows) < window:
            continue
        mean_row: dict[str, float] = {}
        for key in metrics:
            vals = [m.get(key) for m in rows]
            if any(v is None for v in vals):
                continue
            mean_row[key] = sum(float(v) for v in vals) / len(rows)
        if mean_row:
            out.append(mean_row)
    return out


def volume_reference(volume_rows: Iterable[dict[str, float]]) -> dict[str, dict[str, float]]:
    """参考语料卷均值的分布（每项 p10/p25/p50/p75/p90）。样本 <2 项返回空。

    注意口径：这里是**连续 14 章块均值**的分位（块均值的离散度天然比单章小），
    与 `limits_from_chapter_percentiles` 的**单章**分位不是同一把尺、不能互换。
    单章约束（如并段的 `spc_cap`）一律用单章 p90；本函数只服务卷均值对照。
    """
    rows = list(volume_rows)
    return {name: percentile_row([r.get(name) for r in rows])
            for name in FINGERPRINT_METRICS}


def gate_limits_from_percentiles(
    chapter_pct: dict[str, dict[str, float]],
    ultra_short_max: float | None = None,
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, float]]]:
    """章级分位 → (单章结构判罚带, 全量参考带)。"""
    full = limits_from_chapter_percentiles(chapter_pct, ultra_short_max=ultra_short_max)
    judge = {k: full[k] for k in CHAPTER_GATE_METRICS if k in full}
    return judge, full


def volume_gate(
    chapter_metrics: list[dict[str, Any]],
    reference: dict[str, dict[str, float]],
    previous_rel: dict[str, float] | None = None,
    window: int = VOLUME_CHAPTERS,
) -> dict[str, Any]:
    """卷判：本卷均值 vs 参考语料同长连续块均值，看差距**趋势**。

    - 不足 `window` 章 → `status="incomplete_volume"`，只报样本数，不做对照；
    - 判据是趋势不是绝对值：`rel`（相对差）与上一卷比，变化超过参考语料自己
      卷间摆动的**一半**（`(p90-p10)/p50/2`）才算扩大/收窄，否则记 `stable`。
      一半这个系数取自「差距落在参考自身正常摆动内即视为同一指纹」，不是手拍百分比；
    - 指标集为 `FINGERPRINT_METRICS`（13 项，含 `段长均值`）：单章只记录，
      这里才判趋势；容差 `(p90-p10)/p50/2` 取自参考语料自己的卷间摆动。
    - 只给结论，不改正文、不阻断生成（卷末才有完整卷，改也改不到已落的章）。
    """
    rows = [m for m in chapter_metrics if m]
    if len(rows) < window:
        return {
            "window": window, "size": len(rows), "status": "incomplete_volume",
            "metrics": {}, "drift": [], "reasons": [],
        }
    labels = {
        "句长CV": "句长CV", "短句占比": "≤4字短句占比", "长句占比": "≥40字长句占比",
        "长句31_39占比": "31–39字句占比", "叙述单句段占比": "叙述单句段占比",
        "对白字占比": "对白字占比", "问号每千字": "问号/千字",
        "消息段字数中位": "消息段字数中位", "对白≥20占比": "≥20字对白句占比",
        "对白感叹句占比": "对白感叹句占比", "对白叹词起头占比": "对白叹词起头占比",
        "段长均值": "段均字数", "段均句数": "段均句数",
    }
    out_metrics: dict[str, dict[str, Any]] = {}
    drift: list[str] = []
    for key in FINGERPRINT_METRICS:
        vals = [m.get(key) for m in rows]
        if any(v is None for v in vals):
            continue
        mean = round(sum(float(v) for v in vals) / len(rows), 4)
        row = reference.get(key) or {}
        ref_mean = row.get("p50")
        if not ref_mean:
            continue
        signed = (mean - float(ref_mean)) / float(ref_mean)
        abs_diff = round(abs(mean - float(ref_mean)), 4)
        # `rel_diff` 是**差值幅度**（无符号），只用于趋势比较 `delta`；
        # 对外表述一律用 `rel_signed` + `direction`，否则「低 78%」会被读成「高 78%」。
        rel = round(abs_diff / float(ref_mean), 4)
        lo, hi = row.get("p10"), row.get("p90")
        # 参考语料自己的卷间摆动幅度（R90）的一半 = 「差距变化还算稳定」的容差。
        spread = ((float(hi) - float(lo)) / float(ref_mean)) if lo is not None and hi is not None else None
        tol = round(spread / 2, 4) if spread else None
        trend = "baseline"
        delta = None
        if previous_rel is not None and key in previous_rel:
            delta = round(rel - float(previous_rel[key]), 4)
            if tol is not None:
                if delta > tol:
                    trend = "expanding"
                elif delta < -tol:
                    trend = "narrowing"
                else:
                    trend = "stable"
            else:
                trend = "stable" if abs(delta) < 1e-9 else (
                    "expanding" if delta > 0 else "narrowing")
            if trend == "expanding":
                suffix = f"（阈值 {tol:.1%}）" if tol is not None else ""
                drift.append(
                    f"{labels.get(key, key)} 卷间差距扩大：相对差 "
                    f"{previous_rel[key]:.1%} → {rel:.1%}{suffix}"
                )
        out_metrics[key] = {
            "mean": mean, "ref_mean": round(float(ref_mean), 4),
            "abs_diff": abs_diff, "rel_diff": rel,
            "rel_signed": round(signed, 4),
            "direction": "above" if signed > 0 else ("below" if signed < 0 else "flat"),
            "ref_band": {"p10": lo, "p90": hi},
            "inside": (lo is None or mean >= float(lo)) and (hi is None or mean <= float(hi)),
            "trend": trend, "delta": delta, "tolerance": tol,
        }
    status = "drift" if drift else ("baseline" if previous_rel is None else "ok")
    return {
        "window": window, "size": len(rows), "status": status,
        "metrics": out_metrics, "drift": drift,
        "reasons": drift,
    }


#: 软档观测的指标集：九项判界指标 + 对白校准项 + `段均句数`（只观测、永不判界）。
SOFT_OBSERVE_METRICS: tuple[str, ...] = (
    "句长均值", "句长CV", "短句占比", "长句占比", "长句31_39占比",
    "段长均值", "叙述单句段占比", "对白字占比", "问号每千字", "段均句数",
    "对白句长中位", "对白≤4占比", "对白5–9占比", "对白10–19占比",
    "对白≥20占比", "纯对白段占比", "纯对白段字数中位",
    "语气词密度千字", "含助词对白句占比", "对白问句占比", "对白感叹句占比",
    "对白陈述句占比", "对白叹词起头占比", "消息段字数中位", "纯消息段占比",
    "消息含助词占比", "消息平均间隔",
)


def _archive_pct(key: str) -> dict[str, float]:
    """取指标分位：对白项走章级表，其余走块级表。"""
    return ARCHIVE_CHAPTER_PERCENTILES.get(key) or ARCHIVE_BLOCK_PERCENTILES.get(key) or {}


#: 软档观测阈值：相对档案中位的偏离幅度超过 20% 就记录。
#:
#: 注意这是**观测阈值**，不是判界阈值——它不参与 `passed`、不触发重写。选 20%
#: 是因为段 12 对白 16.2% 相对中位 22.6% 低了 28%，必须让人看见；而 ±20% 又能
#: 挡掉正常抖动（档案各指标 p25 距中位的幅度分布在 10%–44%，中位约 23%）。
SOFT_DEVIATION_RATIO = 0.20


def soft_deviations(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """软档观测：相对档案中位偏离超过 `SOFT_DEVIATION_RATIO` 就记一条。

    这是「硬闸门看不见的偏移」的补充：硬闸门只挡档案 p10/p90 级别的离群，
    比如段 12 对白 16.2% 高于下限 6.5% 不算违规，但比档案中位 22.6% 低 28%——
    这类偏移要让人看得见，而不是判它错。不进 `passed`、不进重写。

    `deviation` = `(value - p50) / p50`，方向由 `direction` 给出；`band` 只作
    参考（档案 p25–p75），判断用不用再看人。
    """
    out: list[dict[str, Any]] = []
    for key in SOFT_OBSERVE_METRICS:
        pct = _archive_pct(key)
        p25, p50, p75 = pct.get("p25"), pct.get("p50"), pct.get("p75")
        value = metrics.get(key)
        if value is None or p50 is None:
            continue
        value = float(value)
        # 中位为 0 的项（纯对白段 / 极短纯对白段）：比率没有意义，按「只要出现
        # 就记一条」处理，`deviation` 记绝对值，`absolute_only` 标明。
        absolute_only = p50 == 0
        if absolute_only:
            if value <= 0:
                continue
            dev = value
        else:
            dev = (value - p50) / p50
            if abs(dev) <= SOFT_DEVIATION_RATIO:
                continue
        out.append({
            "metric": key,
            "value": value,
            "median": p50,
            "band": [p25, p75] if p25 is not None and p75 is not None else None,
            "deviation": round(dev, 3),
            "direction": "above" if dev > 0 else "below",
            "absolute_only": absolute_only,
        })
    return out


def generation_gate(
    text: str, max_chars: int | None = None,
    gate_limits: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """生成侧**单章**硬闸门：默认只判 `CHAPTER_GATE_METRICS` 两项结构项 + 长度硬闸。

    文风指纹 13 项（`FINGERPRINT_METRICS`）单章**只记录**、不进 `violations`：
    实测值全部写进 `metrics`/`soft_deviations`，差距按卷看趋势（`volume_gate()`）。
    `段长均值` 自 v8 起也在指纹里，单章偏短不再判违规，只在卷末看趋势。

    长度硬闸（v5）用**正文字符数**（`char_count`，含标点与换行）判：超过
    `max_chars ×1.2` 即违规，与文风指标并列进 `violations`、同样触发重写。
    `max_chars` 为 None 时回退项目默认 3,000。2026-11-17 前上限比汉字、
    下限比字符，同一份稿子两个数，120% 实际等效 151%，现统一到字符。

    `gate_limits` 缺省取 `active_gate_limits()`（由 engine 按参考语料章级分位设置）。
    v22 起 `段数` 下限在这里按 `max_chars` 目标字数现折算（`paragraph_floor_limits`），
    4,000 字目标仍等于档案 p10；无参考书时从常量带 70 反推 17.5 段/千字。

    **永不抛异常**：任何故障都返回 passed=True（放行）——生成不能因为闸门本身
    失败而中断。`skipped=True` 表示字数不足/算不出，调用方不要把它当成“通过”
    去写正常日志。
    """
    limit_len = length_limits(max_chars)
    judge_limits = paragraph_floor_limits(
        gate_limits if gate_limits is not None else active_gate_limits(),
        limit_len["target"],
    )
    out: dict[str, Any] = {
        "version": GEN_GATE_VERSION,
        "passed": True,
        "skipped": False,
        "chars": len("".join((text or "").split())),
        "hanzi": hanzi_count(text),
        "length_limit": limit_len,
        "metrics": {},
        "violations": [],
        "reasons": [],
        "soft_deviations": [],
        "judged_metrics": list(judge_limits.keys()),
    }
    try:
        if out["chars"] < GEN_GATE_MIN_CHARS:
            out["skipped"] = True
            return out
        from app.style.narrative import build_deterministic_features

        feats = build_deterministic_features(text)
        if not feats:
            out["skipped"] = True
            return out
        sl = feats.get("句长") or {}
        pa = feats.get("段落") or {}
        su = feats.get("悬念") or {}
        # v19：长度分布一律走 `stylometry.sentence_lens`（只计含汉字的句子）。
        # 换尺与换阈值是同一次改动：本函数同时供 `_archive_gate_limits` 推分位，
        # 若只改这里不改常量表，闸门值与阈值会出自两把尺。
        lens = stylometry.sentence_lens(text)
        mean = statistics.fmean(lens) if lens else 0.0
        sd = statistics.pstdev(lens) if len(lens) > 1 else 0.0
        long40 = sum(1 for x in lens if x >= 40) / len(lens) if lens else 0.0
        metrics = {
            "句长均值": round(mean, 2),
            "句长CV": round((sd / mean) if mean else 0.0, 3),
            "短句占比": (
                round(sum(1 for x in lens if x <= SHORT_SENTENCE_MAX_LEN) / len(lens), 3)
                if lens else 0.0
            ),
            "长句占比": round(long40, 3),
            "段长均值": round(float(pa.get("段长均值") or 0.0), 1),
            "问号每千字": round(float(su.get("问号每千字") or 0.0), 2),
        }
        # 观测项：只给重写提示当「当前值」引用，不参与判界（阈值仍是六项判界项）。
        # 与档案/`measure()` 同一把尺：`split_sentences` 切句、`paragraphs` 切段、
        # `dialogue_char_count` 量对白，分母同为去空白字符数。
        paras = stylometry.paragraphs(text)
        n_chars = out["chars"] or 1
        # 单句段拆两类（2026-11-13）：原始语料里的单句段主要是独立成行的短对白
        # （“好。”“嗯？”），与叙述句单独成段不是一回事——前者要保留、后者要压。
        # 只有**叙述单句段**进判界；对白单句段只报数，不设上限。
        # 切句前先剥段首尾引号：`split_sentences('“好。”')` 会把收尾的 `”` 切成
        # 独立一句（2 句），独立短对白因此永远数不进单句段。
        # v20：与句长类指标同一把尺、同一次切分（`paragraph_sentence_counts`：
        # 全文切句后归属到段）。改成 `== 1` 而不是 `<= 1`：干净口径下纯标点段
        #（“……”）会得到 0 句，它既不是单句段也不是多句段，不该被算进
        #「叙述单句段」。旧口径下非空段至少切出 1 段，故 `<= 1` 与 `== 1` 等价。
        counts = stylometry.paragraph_sentence_counts(text)
        single = [p for p, c in zip(paras, counts) if c == 1]
        dlg_single = [
            p for p in single if stylometry.strip_edge_quotes(p) != p.strip()
        ]
        nar_single = [p for p in single if p not in dlg_single]
        metrics.update({
            # 结构项（2026-11-14）：段数进硬判（排版碎/块化的地板），必须可见。
            "段数": len(paras),
            "短句每千字": round(
                sum(1 for x in lens if 3 <= x <= 8) / n_chars * 1000, 2
            ),
            "长句31_39占比": (
                round(sum(1 for x in lens if 31 <= x <= 39) / len(lens), 3)
                if lens else 0.0
            ),
            "叙述单句段占比": (
                round(len(nar_single) / len(paras), 3) if paras else 0.0
            ),
            "对白单句段占比": (
                round(len(dlg_single) / len(paras), 3) if paras else 0.0
            ),
            "对白字占比": round(stylometry.dialogue_char_count(text) / n_chars, 3),
            # 段长诊断项：段均字数不够时，先看是不是「一段只有一句」。
            # v20：与 `measure()` / 句长类同一把尺（`paragraph_sentence_counts`），
            # 否则 `段长均值 ≈ 段均句数 × 句长均值` 不成立。
            "段均句数": round(statistics.fmean(counts), 2) if counts else 0.0,
        })
        # 对白校准（v4）：同一把尺量「碎」与「平」。全部进 metrics（闸门可见），
        # 其中四项已校准、进硬判；其余只进软档（阈值见 DIALOGUE_GATE_LIMITS 注释）。
        dlg_profile = stylometry.dialogue_profile(text)
        for key, value in dlg_profile.items():
            if key == "对白句数":
                continue
            metrics[key] = value
        metrics["汉字数"] = out["hanzi"]
        # 篇幅口径（2026-11-17 统一）：预算一律量**字符数**，与下限
        # （`analyze_length` 的 chars）同一个数。汉字数只作文风指标保留。
        metrics["正文字数"] = char_count(text)
        out["metrics"] = metrics
        # 长度硬闸（v5）：正文字数超过目标 ×1.2 直接判违规，与文风指标同一份
        # violations / reasons，因此同样会触发重写。
        if metrics["正文字数"] > limit_len["max"]:
            reason = (
                f"正文总字数 {metrics['正文字数']} 高于上限 {limit_len['max']}"
                f"（目标 {limit_len['target']} 字的 {LENGTH_LIMIT_RATIO:g} 倍）"
            )
            out["violations"].append({
                "metric": "正文字数",
                "value": metrics["正文字数"],
                "limit": {"max": limit_len["max"]},
                "reason": reason,
            })
            out["reasons"].append(reason)
        # 软档观测：只记录「偏离档案四分位带」的项，不影响 passed、不进重写。
        out["soft_deviations"] = soft_deviations(metrics)
        labels = {
            "句长均值": "句长平均",
            "句长CV": "句长CV",
            "短句占比": "≤4字短句占比",
            "长句占比": "≥40字长句占比",
            "长句31_39占比": "31–39字句占比",
            "段长均值": "段均字数",
            "段数": "自然段数",
            "叙述单句段占比": "叙述单句段占比",
            "对白字占比": "对白字占比",
            "问号每千字": "问号/千字",
            "纯对白段占比": "纯对白段占比",
            "纯消息段占比": "纯消息段占比",
            "极短纯对白段占比": "极短纯对白段占比",
            "消息段字数中位": "消息段字数中位",
            "对白≥20占比": "≥20字对白句占比",
            "对白感叹句占比": "对白感叹句占比",
            "对白叹词起头占比": "对白叹词起头占比",
        }
        for key, limit in judge_limits.items():
            value = metrics.get(key)
            if value is None:  # 算不出（如整章无对白）不判，不让 None 参与比较
                continue
            lo, hi = limit.get("min"), limit.get("max")
            reason = ""
            if lo is not None and value < lo:
                reason = f"{labels[key]} {value} 低于下限 {lo}"
            elif hi is not None and value > hi:
                reason = f"{labels[key]} {value} 高于上限 {hi}"
            if reason:
                out["violations"].append({
                    "metric": key,
                    "value": value,
                    "limit": limit,
                    "reason": reason,
                })
                out["reasons"].append(reason)
        out["passed"] = not out["violations"]
    except Exception:  # noqa: BLE001 —— 闸门故障必须放行，不能弄挂生成
        logger.warning("generation_gate 计算失败，按放行处理", exc_info=True)
        out.update({"passed": True, "skipped": True, "violations": [], "reasons": []})
    return out


# --------------------------------------------------------------------------- #
# 2. 基线
# --------------------------------------------------------------------------- #

def _quantile(sorted_vals: list[float], q: float) -> float | None:
    """线性插值分位（与 numpy 默认口径一致）。"""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _summarize(values: Iterable[float]) -> dict[str, Any] | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    mean = statistics.fmean(vals)
    std = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return {
        "n": len(vals),
        "p1": _quantile(vals, 0.01),
        "p10": _quantile(vals, 0.10),
        "p50": _quantile(vals, 0.50),
        "p90": _quantile(vals, 0.90),
        "p99": _quantile(vals, 0.99),
        "mean": mean,
        "std": std,
    }


def _sample_rows(db, project_id: int, exclude_generation_id: int | None) -> list[Generation]:
    """本项目最近的已落库正文（按 id 倒序；排除正在校验的这一段，防自证）。"""
    stmt = (
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.id.desc())
        .limit(WINDOW_SEGMENTS)
    )
    if exclude_generation_id is not None:
        stmt = stmt.where(Generation.id != exclude_generation_id)
    rows = list(db.scalars(stmt).all())
    return [r for r in rows if (r.text or "").strip()]


def ensure_baseline(db, project_id: int,
                    exclude_generation_id: int | None = None) -> StyleBaseline | None:
    """取/建本项目自身基线。样本集没变就复用最新版本，变了才升版本号。

    Returns:
        StyleBaseline；本项目没有可用的已写正文时返回 None（不写检查记录）。
    """
    rows = _sample_rows(db, project_id, exclude_generation_id)
    if not rows:
        return None

    sample_ids = [r.id for r in rows]
    latest = db.scalars(
        select(StyleBaseline)
        .where(StyleBaseline.project_id == project_id)
        .order_by(StyleBaseline.baseline_version.desc())
        .limit(1)
    ).first()
    if latest is not None:
        latest_ids = (latest.metrics_json or {}).get("sample_generation_ids") or []
        # 精确相同（正常滚动），或本次样本只是最新基线的子集（回填校验一段
        # 已落库正文时，那一段本来就在基线里）——都复用最新版本，不升版本号。
        if sample_ids == latest_ids or set(sample_ids) <= set(latest_ids):
            return latest

    measured = [measure(r.text or "") for r in rows]
    core = {
        k: _summarize((m["core"].get(k) for m in measured)) for k in CORE_METRICS
    }
    segments = sorted(int(r.segment_no) for r in rows)
    source_range = (
        f"p{project_id} 段{segments[0]}–段{segments[-1]}"
        f"（{len(rows)} 段，最近 {WINDOW_SEGMENTS} 段窗口）"
    )
    insufficient = None
    if len(rows) < MIN_SELF_SEGMENTS:
        insufficient = (
            f"样本量不足：{len(rows)} 段 < {MIN_SELF_SEGMENTS} 段；"
            "分位只是冷启动初值，不得据此产出判定"
        )

    baseline = StyleBaseline(
        project_id=project_id,
        baseline_version=(latest.baseline_version + 1) if latest else 1,
        window_segments=WINDOW_SEGMENTS,
        sample_count=len(rows),
        source="self",
        source_range=source_range,
        insufficient_reason=insufficient,
        metrics_json={
            "core": core,
            "sample_generation_ids": sample_ids,
            "metric_scope": "stylometry.raw.v1",
        },
        created_at=_utcnow(),
    )
    db.add(baseline)
    db.commit()
    db.refresh(baseline)
    return baseline


# --------------------------------------------------------------------------- #
# 3. 参考带偏离说明（只写说明，不产出判定）
# --------------------------------------------------------------------------- #

def _reference_scale(unit_chars: int | None) -> float:
    """还原档案拟合时对非密度特征乘的长度系数（与 stylometry 同一公式）。"""
    if not unit_chars or unit_chars <= 0:
        return 1.0
    return math.exp(-abs(math.log(unit_chars / 3000.0)) * 0.3)


def reference_deviation(project, metric: dict[str, Any]) -> dict[str, Any] | None:
    """相对参考带 `buckets.short` 的 |z| ≥ 3 偏离清单。

    没有 `style_profile` 的项目返回 None（不臆造参考带）。
    """
    profile = getattr(project, "style_profile", None) or {}
    bucket = (profile.get("buckets") or {}).get(REFERENCE_BUCKET) or {}
    feat = bucket.get("feat") or {}
    if not feat:
        return None

    unit_chars = bucket.get("unit_chars")
    scale = _reference_scale(unit_chars)
    deviations = []
    for k in CORE_METRICS:
        value = metric["core"].get(k)
        stats_ = feat.get(k) or {}
        std = float(stats_.get("std") or 0.0)
        mean = float(stats_.get("mean") or 0.0)
        if value is None or std <= 0:
            continue
        scaled = value * scale if k in stylometry.NON_DENSITY_FEATURES else value
        z = (scaled - mean) / std
        if abs(z) >= REFERENCE_Z:
            deviations.append({
                "metric": k,
                "z": round(z, 2),
                "value": round(value, 4),
                "band_mean": round(mean, 4),
                "band_std": round(std, 4),
            })
    return {
        "bucket": REFERENCE_BUCKET,
        "unit_chars": unit_chars,
        "scale": round(scale, 4),
        "z_threshold": REFERENCE_Z,
        "deviations": deviations,
        "disclaimer": "仅供参照，不作为判据",
    }


# --------------------------------------------------------------------------- #
# 4. 写记录
# --------------------------------------------------------------------------- #

def _note(metric: dict[str, Any]) -> str:
    if metric["chars"] == 0:
        return "这一段没有正文，未记录指标。"
    if not metric["judgeable"]:
        return (
            f"这一段太短（不足 {MIN_JUDGE_CHARS} 字），还不够判断风格是否一致；"
            "已经记录，等写满几段后再一起看。"
        )
    return "影子记录：已按本项目已写正文的分布记录本段指标，本阶段不作判定。"


def record_segment_check(db, project, generation: Generation) -> int | None:
    """生成成功后写一条影子记录。**永不抛异常**。

    调用点：`engine._commit_segment()` 的 `db.commit()` 之后（正文已落库）。
    校验失败只写日志，不影响生成结果——这是第一阶段的硬约束。
    """
    try:
        text = generation.text or ""
        metric = measure(text)
        baseline = ensure_baseline(
            db, project.id, exclude_generation_id=generation.id
        )
        if baseline is None:
            logger.info(
                "style shadow skipped: project=%s generation=%s 无已写正文可作基线",
                project.id, generation.id,
            )
            return None

        row = StyleCheck(
            project_id=project.id,
            scope="segment",
            generation_id=generation.id,
            agent_run_id=None,  # Writer 路径不写 agent_runs
            segment_no=generation.segment_no,
            chapter_range=generation.chapter_range,
            text_md5=hashlib.md5(text.encode("utf-8")).hexdigest(),
            check_version=CHECK_VERSION,
            baseline_id=baseline.id,
            baseline_version=baseline.baseline_version,
            verdict="shadow",  # 第一阶段：绝不写 pass/warn/fail
            metric_json={
                "chars": metric["chars"],
                "judgeable": metric["judgeable"],
                "core": metric["core"],
                "display": metric["display"],
                "dialogue": metric.get("dialogue") or {},
                "metric_scope": metric["metric_scope"],
            },
            missing_json=metric["missing"] or None,
            evidence_json={
                "reference": reference_deviation(project, metric),
            },
            note=_note(metric),
            created_at=_utcnow(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        # 第四层：把「规则声称 vs 本段实测」反馈落库（权重表）。
        # 与校验器同一条纪律——失败只记日志，不影响生成与已写入的影子记录。
        try:
            from app.style.narrative import record_narrative_feedback

            record_narrative_feedback(db, project, text, metric=metric)
        except Exception:  # noqa: BLE001
            logger.warning("narrative rule feedback failed (ignored)", exc_info=True)
        return row.id
    except Exception:  # noqa: BLE001 —— 校验器绝不能拖垮生成
        logger.warning("style shadow check failed (ignored)", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
