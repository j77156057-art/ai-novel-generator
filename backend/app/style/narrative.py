"""叙事档案（narrative）：用 LLM 从参考语料提炼 L2/L3 认知层规则，附证据账本。

## 为什么需要它

`style/stylometry.py` 产出的是 L1 表层统计（句长、标点密度、对白占比）——这些数字能
验收「像不像」，但**教不会模型怎么写**。小说的 L2 结构层（开头钩子形态、段落编排模式、
章末收束惯例）和 L3 认知层（人物描写手法、对白习惯、爽点节奏、主角依恋机制、
剧情编排模式）一项都没覆盖。这些维度必须由 LLM 深度阅读后提炼成可注入的规则。

## 与 stylometry 的分工

- `stylometry.py`：纯统计、零 LLM、离线可复现，产出数字档案
- `narrative.py`：LLM 提炼、需要 API key、产出结构化规则文档，落库到 `style_narrative` 列

两者**独立缓存、独立失效**：`style_profile` 的指纹是 `source_ids + unit_chars + version + schema`；
`style_narrative` 的指纹是 `source_ids + narrative_version`。改了提炼逻辑只需把
`narrative_version` +1，不需要碰统计档案的指纹逻辑。

## 证据账本（防幻觉伪规则）

每条规则必须附原文证据。LLM 提炼时容易编造「这个作者其实不这么写」的伪规则，
强制附证据是唯一有效的防幻觉手段。证据段落必须真实存在于语料中，
合并时做删除测试：删掉某条规则后对照原文验证是否成立。

## 不做全文覆盖

L3 深读只覆盖代表性章节（20~30 章），剩下的是重复模式。同时避开早期文风漂移问题——
长篇作者文风是演化的，早期章节的写法不能代表成熟期。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import statistics
from datetime import datetime, timezone

from dataclasses import dataclass, replace
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

#: 切句口径的唯一真源在 `stylometry.split_sentences`（R16）。
from app.config import settings
from app.style import stylometry

#: 进度回调签名：`(阶段人话文本, 已完成数, 总数)`。
#:
#: 只用于**上报**蒸馏进度（后台任务的进度条），不参与任何蒸馏决策。
#: 不传时整个蒸馏链路一行不改、行为与加进度之前逐字节一致 —— 生成路径
#: (`project_narrative_safe`) 走的就是不传的那条路。
#:
#: `总数` 允许为 0，含义是「还没探明」：蒸馏要等语料加载、采样之后才知道
#: 一共有多少块。前端据此渲染不确定态，绝不拿 0 当分母。
ProgressCallback = Callable[[str, int, int], None]

#: 心跳回调签名：`(本批已完成数, 本批总数, 阶段前缀)`。
#:
#: 与 `ProgressCallback` 的分工：进度回调报「块」，块级进度要等一本语料的全部
#: 并发调用回来才动；心跳按**每一条 LLM 调用返回**触发，只回答「后台线程还活着
#: 吗、这一批完成了几个」。阶段前缀由提炼侧给出人话（如「语料 2/2：并发提炼中」），
#: 后台任务负责拼上「（已完成 37/92）」并按间隔节流写 `stage`。
#:
#: 不传心跳时整条链路行为与引入心跳之前逐字节一致（生成路径就是这条）。
HeartbeatCallback = Callable[[int, int, str], None]

#: 叙事档案的形状修订号。**任何会改变档案字段的改动都必须把它 +1。**
#:
#: 与 `style/pipeline.py` 的 `PIPELINE_SCHEMA` 同理：指纹只有 `source_ids + narrative_version`，
#: 管不到「档案长什么样」。加字段时老档案不会自动失效，前端拿到的摘要会永远少一块。
NARRATIVE_VERSION = 7

#: 采样比例：从全文章节中采样的百分比。
#: 603 章 × 30% = 181 章。原为 15%，2026-10-19 提高到 30%——
#: 理由是 15% 下长篇语料切出的留出单元太少（斗破 588 万字 15% → 958 章但
#: 按 bucket 切分后 short/medium/long 留出数仍不足 20），percentile 不可用；
#: 30% 让留出样本量翻倍，同时覆盖面更充分。成本随之翻倍。
SAMPLE_RATIO = 0.30

#: 采样的字数下限：短篇/短语料若按比例采样会砍掉过多对白，
#: 导致角色声音维度失效（实测：10 章语料只提得出 1 个角色声音）。
#: 采样结果若低于此字数，则扩容到达到下限；若全量仍不足，则全取。
#: 30 万字 ≈ 100+ 章网文，足够覆盖角色对白与场景多样性。
MIN_SAMPLE_CHARS = 300_000

#: 采样的字数下限：短篇/短语料若按比例采样会砍掉过多对白，
#: 导致角色声音维度失效（实测：10 章语料只提得出 1 个角色声音）。
#: 采样结果若低于此字数，则扩容到达到下限；若全量仍不足，则全取。
#: 30 万字 ≈ 100+ 章网文，足够覆盖角色对白与场景多样性。
MIN_SAMPLE_CHARS = 300_000

#: 每块最少章节数（太少提炼不出模式）
MIN_CHAPTERS_PER_CHUNK = 5

#: 系统提示里「反向禁忌」块的标题前缀（prompt_audit 用它判定这一块是否存在）。
#: 批 7 前置：`agents/prompt_audit.py` 直接 import 这个名字。
FORBIDDEN_SECTION_MARKER = "【反向禁忌"

#: 每个蒸馏块的目标字数（保守兜底值，不含渠道覆盖）。
#: 按字数而不是章数：不同书章长差 3-5 倍，固定章数会让块大小失真。
TARGET_BLOCK_CHARS = 20_000


def _chapters_per_block(
    chapters: list[tuple[int, str]], target_chars: int,
) -> int:
    """估算「装够 target_chars 字需要多少章」。

    按中位章长算而不是均值——均值会被少数超长章拉高，导致块里装的章数偏少。
    """
    lens = sorted(len(t) for _, t in chapters if t)
    if not lens:
        return MIN_CHAPTERS_PER_CHUNK
    median = lens[len(lens) // 2]
    if median <= 0:
        return MIN_CHAPTERS_PER_CHUNK
    return max(MIN_CHAPTERS_PER_CHUNK, target_chars // median)


def _resolve_block_chars(provider, requested: int | None = None) -> int:
    """决定本次蒸馏每个块的目标字数。优先级：显式参数 > 用户配置 > 渠道实测 > 兜底。

    渠道值取 `provider.distill_block_chars`（0 = 该渠道没实测过）。
    用户可通过 `settings.distill_block_chars` 强行覆盖（0 = 不覆盖）。
    """
    if requested and requested > 0:
        return requested
    forced = _configured_block_chars()
    if forced > 0:
        return forced
    hint = int(getattr(provider, "distill_block_chars", 0) or 0)
    return hint if hint > 0 else TARGET_BLOCK_CHARS


def _configured_block_chars() -> int:
    """用户显式配置的块大小覆盖（0 = 不覆盖）。配置不可读时返回 0。"""
    try:
        from app.config import settings as _st
        return int(getattr(_st, "distill_block_chars", 0) or 0)
    except Exception:  # noqa: BLE001 —— 配置不可读不该让蒸馏失败
        return 0


def _block_chars_for_fingerprint() -> int:
    """算指纹用的块大小——不构造 provider，但与 `_resolve_block_chars` 同源。

    块大小只有两个来源：① `settings.distill_block_chars`（显式覆盖）；
    ② 渠道类的 `distill_block_chars`（实测能力）。两者都不依赖实例。
    """
    forced = _configured_block_chars()
    if forced > 0:
        return forced
    try:
        from app.llm.provider import block_chars_for_channel
    except Exception:  # noqa: BLE001
        return TARGET_BLOCK_CHARS
    return block_chars_for_channel() or TARGET_BLOCK_CHARS


#: 证据句下限（2026-11-12）。短于 20 字的证据在章内无法唯一定位，只能做两端锚定，
#: 等于没有锚点。抽取提示词已同步改成「20–60 字，少于 20 字不作为证据」，
#: 这里再硬卡一次：不足 20 字的规则**不落库**，不靠事后模糊匹配兜。
#: 与 `app/retrieval/catalog_config.EVIDENCE_MIN_CHARS` 同值；两边都改才算改。
EVIDENCE_MIN_CHARS = 20

#: 场景类型关键词（用于场景均衡采样，确保每种场景都有足够样本）
SCENE_KEYWORDS = {
    "病房/医疗": ["手术室", "病房", "医院", "医生", "护士", "病床", "抢救", "急救"],
    "宴会/社交": ["宴会", "晚宴", "酒会", "宾客", "包厢", "宴席", "酒席"],
    "办公室/商务": ["办公室", "会议室", "总裁", "秘书", "文件", "合同", "签字", "股权"],
    "车内/私密": ["车里", "车内", "驾驶座", "副驾驶", "后座", "车窗"],
    "户外/街道": ["街头", "马路", "巷子", "公园", "咖啡厅", "餐厅"],
    "家中/起居": ["客厅", "卧室", "厨房", "书房", "沙发", "床上"],
}

#: **已停用**的维度：不再提取，但历史档案里的行原样保留、仍可读可回查。
#:
#: 高频场景模板（2026-11-13 停用，**有意为之，不是丢数据**）：
#: 档案里 3,448 条中标题唯一的有 3,371 条（97.8%），实际是逐章剧情摘要而不是
#: 「反复出现的场景模板」；建卡硬过滤只淘汰 53 条（全是模糊词），说明它不是被
#: 过滤掉的，而是**本身就没有可复用的信号**。渲染侧每轮只取 1 条，而
#: `chunk_meta.summary` + `scene_labels` 已覆盖同一用途。停用后每块少 1 次调用。
#:
#: 下一个看到维度清单少一项的人，先看这条，别当成漏抽。
DEPRECATED_DIMENSIONS = ("高频场景模板",)

#: 叙事档案要覆盖的维度（与 writer-novel-style-distiller 的维度设计对齐）。
#: 已停用的维度不在此列，见 `DEPRECATED_DIMENSIONS`。
NARRATIVE_DIMENSIONS = [
    "人物描写手法",
    "对白风格",
    "段落编排",
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
    # —— v3 新增维度 ——
    "剧情编排模式",
    "角色声音词典",
    "伏笔与悬念设计",
    "文笔量化审计",
    # —— v5 新增维度 ——
    "节拍序列",
    "潜台词模式",
]

#: 角色声音提取：从图谱取高频角色名，单独分析说话风格。
#: 这些角色的「口头禅/自称/敬语/句长」直接影响生成时角色是否「像本人」。
CHARACTER_VOICE_DIMENSION = "角色声音词典"

#: 反向禁忌维度：作者**从不使用**的表达模式。这是防 AI 腔的关键——
#: LLM 更容易遵循具体的「不要用 X」而不是通用的「忌 AI 腔」。
#: 参考 writer-novel-style-distiller 的「反向禁忌清单」设计 [调研结论]。
FORBIDDEN_DIMENSIONS = [
    "禁用转折词",      # 如「就在这时」「突然」
    "禁用情绪直陈",    # 如「心中充满了」「眼中闪过一丝」
    "禁用段尾拔高",    # 如「他知道」「她明白」+ 总结句
    "禁用解释性对白",  # 如对话交代世界观/设定
]


# ====================================================================
# 蒸馏档位（tier）
# ====================================================================
#
# ## 为什么要有档位
#
# 完整档是最贵的一档：按语料量百分比采样（全章数的 15%）、全维度、还要做
# 角色名归一化这种重活。docs 里唯一一次计时是 833 章语料跑 42 分钟——量级在
# 「几十分钟」这个级别，用户不可能每次都等这个。试跑、冒烟、短篇根本不需要
# 角色名归一化。于是把蒸馏能力分档，让用户按场景选：生产用完整档、试跑用平衡档、
# 冒烟用快速档。
#
# 档位差异只落在**四个可实现的轴**上（采样策略 / 维度子集 / 角色名归一化 /
# 伪角色清洗强度），每一条都对应 docs/style-distillation-tiers.md 里实测到的
# 能力跃迁，不是随手编的标签。
#
# ## 档位只影响蒸馏，不影响其它
#
# Writer 生成路径、audit 规则、log_audit 规则一概不读 tier——本模块之外的任何
# 代码都不得引入 tier 分支。


#: 第一档（完整）：生产用。百分比采样 + 全维度 + 语料自证的角色名归一化。
TIER_FULL = "full"
#: 第二档（平衡）：试跑/中篇。固定 30 章 + 全维度，不做角色名归一化。
TIER_BALANCED = "balanced"
#: 第三档（快速）：冒烟/短篇。固定 30 章 + 精简维度，清洗全关。
TIER_FAST = "fast"

#: 默认档位。**必须是完整档**——不传 tier 时行为与引入档位之前逐字节一致
#: （SAMPLE_RATIO=0.15 / 全维度 / 开归一化），不破坏已验证过的产出。
DEFAULT_TIER = TIER_FULL


#: 各档的**相对**耗时档位提示。
#:
#: ## 为什么三档都不给「约 N 分钟」
#:
#: docs/style-distillation-tiers.md 全文只有**一处**计时（第 81 行）：
#!
#:     语料 2《大奉打更人》833 章 → 采样 764 章 → 耗时 42 分钟
#:
#: 也就是说：唯一有实测支撑的耗时是**语料 2（833 章）**的 42 分钟。
#! 语料 1（603 章）那段**从头到尾没有任何计时记录**，「64 分钟」在 docs 里
#! 零命中（grep 到的 64 都是 764 / 2644 / 421 这类别的数字）。
#:
#: 曾一度把完整档写成「实测 603 章语料约 64 分钟」——那是把 brief 里的二手
#: 分钟数配上 docs 里语料 1 的章数拼出来的组合，仓库里查不到出处，而且
#: **603 章 64 分钟比 833 章 42 分钟还慢**，与docs 唯一的计时直接矛盾。
#! 界面上的耗时提示是给用户的时间承诺，不能挂在一个自相矛盾的数上。
#:
#: 相对档位（最高/中等/最低）是从**配置事实**推导的（采样量 + 维度数 +
#: 角色声音调用次数），不会撒谎。将来有了某一档某一语料的真实计时，
#: 再往这里补「约 N 分钟（语料 X，N 章，实测）」并注明出处。
#:
#: 键是档位 key，新增档位时必须在这里补一项，否则 `to_public_dict` 会抛 KeyError
#: ——宁可响亮地失败，也不要静默给错提示。
_COST_HINTS: dict[str, str] = {
    TIER_FULL: "最高（完整蒸馏：读全本、全维度、含角色名归一化）",
    TIER_BALANCED: "中等（固定 30 章，维度齐全，省掉角色名归一化）",
    TIER_FAST: "最低（固定 30 章 + 精简维度，只验证管线）",
}


@dataclass(frozen=True)
class NarrativeTier:
    """一个蒸馏档位的完整配置。

    刻意做成**显式注册表**而不是散落的 if/else 常量：档位是要展示给用户选的
    （前端拿 name + description 直接渲染），也是要被测试逐项断言的
    （"三档真的不同" 不能只比对标签）。散落的 if/else 没法列举、没法渲染。
    """

    key: str
    name: str
    description: str
    #: "percentage" → 按 `sample_ratio` 采样；"fixed_chapters" → 固定 `fixed_chapters` 章
    sampling_mode: str
    #: sampling_mode == "percentage" 时生效
    sample_ratio: float | None = None
    #: sampling_mode == "fixed_chapters" 时生效
    fixed_chapters: int | None = None
    #: 本档要提炼的维度白名单（**子集**，不是全集——全集就是"没做子集"）
    dimensions: tuple[str, ...] = ()
    #: 是否启用「语料词频自证」的角色名归一化（小落→安小落、宫爵→南宫爵）
    normalize_character_names: bool = False
    #: 伪角色清洗强度："full"（虚词黑名单 + 频率自证）/ "partial"（仅黑名单）/ "none"
    fake_character_cleaning: str = "none"
    #: 角色声音提取方式："targeted"（主要角色逐个 + 配角分批）/ "single_batch"（单批浅层）
    voice_mode: str = "targeted"
    #: 采样章数的**显式覆盖**（None = 用档位原本的采样口径）。
    #:
    #: 语义：0 = 全本（整本语料全部灌进去）；N > 0 = 最多取 N 章。
    #: 这是「助理按用户要求改采样量」的唯一入口——它必须进指纹，
    #: 否则把 833 章改成 200 章后仍会命中旧档案（静默返回旧结果）。
    sample_cap: int | None = None

    @property
    def dimension_count(self) -> int:
        """本档维度数（测试断言用，避免各处硬编码数字）。"""
        return len(self.dimensions)

    def fingerprint_payload(self) -> dict[str, Any]:
        """进入指纹的**有效配置**——改任一项都必须让旧缓存失效。"""
        return {
            "tier": self.key,
            "sampling_mode": self.sampling_mode,
            "sample_ratio": self.sample_ratio,
            "fixed_chapters": self.fixed_chapters,
            "dimensions": list(self.dimensions),
            "normalize_character_names": self.normalize_character_names,
            "fake_character_cleaning": self.fake_character_cleaning,
            "voice_mode": self.voice_mode,
            "sample_cap": self.sample_cap,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """给 API / 前端展示用的形状（不含内部实现细节）。"""
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "sampling_mode": self.sampling_mode,
            "sample_ratio": self.sample_ratio,
            "fixed_chapters": self.fixed_chapters,
            "dimension_count": self.dimension_count,
            "dimensions": list(self.dimensions),
            "normalize_character_names": self.normalize_character_names,
            "fake_character_cleaning": self.fake_character_cleaning,
            # 采样覆盖：0 = 全本；None = 按档位默认口径
            "sample_cap": self.sample_cap,
            # 相对耗时档位（非实测分钟数，理由见 _COST_HINTS 注释）
            "cost_hint": _COST_HINTS[self.key],
        }


#: 快速档砍掉的两个维度（都在 v5 引入的增量能力里）。砍这两个 = 每块少 2 次
#: LLM 调用，维度数 13 → 11（与停用「高频场景模板」之前的快速档同为 11 维）。
_FAST_TIER_DROP_DIMENSIONS = ("节拍序列", "潜台词模式")


TIER_REGISTRY: dict[str, NarrativeTier] = {
    TIER_FULL: NarrativeTier(
        key=TIER_FULL,
        name="完整",
        description="生产用。读全本（全书每一章），全维度，含语料自证的角色名归一化与全量伪角色清洗。最准也最慢。",
        sampling_mode="percentage",
        # 1.0 = 全本。完整档不再按 30% 抽样——抽样只能看到每个故事弧的片段，
        # 长篇的铺垫/回收经常隔着几百章，抽掉一半就断链。比例轴保留是为了
        # 让 sample_cap / 其它档位仍能复用它；完整档固定取满。
        sample_ratio=1.0,
        dimensions=tuple(NARRATIVE_DIMENSIONS),
        normalize_character_names=True,
        fake_character_cleaning="full",
        voice_mode="targeted",
    ),
    TIER_BALANCED: NarrativeTier(
        key=TIER_BALANCED,
        name="平衡",
        description="试跑 / 中篇用。固定 30 章，维度与完整档齐，不做角色名归一化（简称可能有噪声）。",
        sampling_mode="fixed_chapters",
        fixed_chapters=30,
        dimensions=tuple(NARRATIVE_DIMENSIONS),
        normalize_character_names=False,
        fake_character_cleaning="partial",
        voice_mode="targeted",
    ),
    TIER_FAST: NarrativeTier(
        key=TIER_FAST,
        name="快速",
        description="冒烟 / 短篇用。固定 30 章，精简维度，角色名不归一化也不清洗。只验证管线通不通。",
        sampling_mode="fixed_chapters",
        fixed_chapters=30,
        dimensions=tuple(
            d for d in NARRATIVE_DIMENSIONS if d not in _FAST_TIER_DROP_DIMENSIONS
        ),
        normalize_character_names=False,
        fake_character_cleaning="none",
        voice_mode="single_batch",
    ),
}


def resolve_tier(tier: str | None) -> NarrativeTier:
    """把用户传的档位 key 解析成配置对象。

    Args:
        tier: 档位 key；None / 空串 → 默认档（完整档）。

    Returns:
        对应的 NarrativeTier。

    Raises:
        ValueError: 未知档位 key。**不静默回落默认档**——用户选了「快速」却
            悄悄跑成完整档，是本项目已经踩过两次的同类坑（静默返回错结果）。
    """
    if not tier:
        return TIER_REGISTRY[DEFAULT_TIER]
    key = str(tier).strip().lower()
    if key not in TIER_REGISTRY:
        valid = "、".join(sorted(TIER_REGISTRY))
        raise ValueError(f"未知蒸馏档位 {tier!r}，可选：{valid}")
    return TIER_REGISTRY[key]


def tier_with_sampling(tier: NarrativeTier, sample_cap: int | None) -> NarrativeTier:
    """给档位叠加「采样章数覆盖」。

    ``sample_cap`` 语义：``0`` = 全本（整本语料全部灌进去）；``N > 0`` = 最多
    取 N 章；``None`` = 不改。覆盖值会进指纹，改完必须重跑蒸馏才会生效。
    """
    if sample_cap is None:
        return tier
    cap = int(sample_cap)
    if cap < 0:
        raise ValueError("采样章数不能为负（0 表示全本）")
    return replace(tier, sample_cap=cap)


def list_tiers() -> list[dict[str, Any]]:
    """按「完整 → 平衡 → 快速」的顺序列出全部档位（前端选档用）。"""
    order = [TIER_FULL, TIER_BALANCED, TIER_FAST]
    return [TIER_REGISTRY[k].to_public_dict() for k in order if k in TIER_REGISTRY]


def _tier_signature(tier: NarrativeTier) -> str:
    """档位配置的稳定短哈希。

    进指纹用：改了某档的维度白名单 / 采样章数 / 清洗强度，旧缓存必须失效
    （配置变了却复用旧结果 = 又一次静默返回旧结果）。
    """
    blob = json.dumps(tier.fingerprint_payload(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ====================================================================
# 断点续跑：块级完成标记
# ====================================================================
#
# 蒸馏 400+ 分钟里几乎全部时间花在「每块一次 LLM 调用」。块级产出原来只活在
# `extract_narrative_profile` 的内存累加器里，进程一没就全部作废。这一组函数
# 把「每块的净产出」在块处理完的当下写进 `distill_chunks`，续跑时回灌并跳过。
#
# 复用的前提是**输入完全一致**：scope 把语料集合、档位配置、提炼版本号、采样
# 上限揉成哈希。任一项变了就查不到旧块——宁可重跑，也不能拼出「一半旧档 + 一半
# 新档」的档案（那是没有任何报错的静默错误）。

#: 辅助阶段的合成 chunk_key 前缀（角色声音 / 文风漂移不是「块」，但同样值得
#: 跨轮跳过）。带这个前缀的行不计入「已完成块数」。
_AUX_KEY_PREFIX = "aux_"


def distill_resume_scope(
    source_ids: list[int], cfg: NarrativeTier, max_chapters: int = 180
) -> str:
    """一次蒸馏断点的作用域哈希（只有 scope 相同才允许复用已完成块）。"""
    blob = json.dumps(
        {
            "source_ids": sorted(int(i) for i in source_ids),
            "tier_signature": _tier_signature(cfg),
            "narrative_version": NARRATIVE_VERSION,
            "max_chapters": int(max_chapters),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _chunk_key(novel_id: int, chapter_range: str, chunk_text: str) -> str:
    """块的稳定标识：同一 scope 下同一段章节正文永远得到同一个 key。

    采样是确定性的（本模块无 random），所以「同样的输入 → 同样的块」成立；
    正文一改（重新导入/清洗），key 就变，旧块不会被错用。
    """
    blob = f"{int(novel_id)}|{chapter_range}|{chunk_text}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_done_chunks(db: Session, project_id: int, scope: str) -> dict[str, dict]:
    """读本 scope 下所有已完成块（key → payload）。读失败按「没有断点」处理。"""
    try:
        from app.models import DistillChunk

        rows = db.scalars(
            select(DistillChunk).where(
                DistillChunk.project_id == project_id,
                DistillChunk.scope == scope,
            )
        ).all()
        return {str(r.chunk_key): (r.payload or {}) for r in rows}
    except Exception as exc:  # noqa: BLE001 —— 断点读不到就退化成全新跑
        logging.getLogger(__name__).warning(
            "读取蒸馏断点失败 project_id=%s: %s", project_id, exc
        )
        return {}


def _chunk_provenance(db: Session, project_id: int, scope: str) -> dict[str, str]:
    """读断点块的来源（`provider` 列）；老行没记过 → `"未知"`。

    用途只有一个：续跑时把「这批断点是谁产的」写进日志。跨 provider 续跑时，
    若来源分布出现多代，说明早先的块不是当前网关产的，排查证据质量时要分开看。
    """
    try:
        from app.models import DistillChunk

        rows = db.execute(
            select(DistillChunk.chunk_key, DistillChunk.provider).where(
                DistillChunk.project_id == project_id,
                DistillChunk.scope == scope,
            )
        ).all()
        return {str(k): (p or "未知") for k, p in rows}
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "读取断点来源失败 project_id=%s: %s", project_id, exc
        )
        return {}


def _save_done_chunk(
    db: Session,
    project_id: int,
    scope: str,
    novel_id: int,
    chunk_key: str,
    chapter_range: str,
    payload: dict,
    provider: str | None = None,
    endpoint: str | None = None,
) -> None:
    """落一个块的完成标记（幂等 upsert）。

    失败只记日志：存断点是增强，不能因为存断点失败就把整次蒸馏搞挂。

    `provider` / `endpoint` 记录「这块是谁产的」：同 scope 下可能跨 provider 续跑，
    没有这两列就分不清哪些块来自旧网关。老行保持 NULL，不回填。
    """
    try:
        from app.models import DistillChunk

        existing = db.scalar(
            select(DistillChunk).where(
                DistillChunk.project_id == project_id,
                DistillChunk.scope == scope,
                DistillChunk.chunk_key == chunk_key,
            )
        )
        if existing is not None:
            existing.payload = payload
            existing.provider = provider
            existing.endpoint = endpoint
        else:
            start = end = None
            parts = (chapter_range or "").split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                start, end = int(parts[0]), int(parts[1])
            db.add(
                DistillChunk(
                    project_id=project_id,
                    scope=scope,
                    novel_id=int(novel_id),
                    chunk_key=chunk_key,
                    chapter_start=start,
                    chapter_end=end,
                    payload=payload,
                    provider=provider,
                    endpoint=endpoint,
                )
            )
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "写入蒸馏断点失败 key=%s: %s", chunk_key, exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _clear_scope_chunks(db: Session, project_id: int, scope: str) -> None:
    """清掉本 scope 的断点（force 重算时调用，避免拿半成品拼新档案）。"""
    try:
        from app.models import DistillChunk

        db.query(DistillChunk).filter(
            DistillChunk.project_id == project_id, DistillChunk.scope == scope
        ).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "清理蒸馏断点失败 project_id=%s: %s", project_id, exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _clear_stale_scope_chunks(db: Session, project_id: int, keep_scope: str) -> int:
    """换档/换语料后清掉旧 scope 的断点，返回删除行数。

    scope 由「语料集合 + 档位 + 章数上限」算得：任一项变了，旧块就永远不会
    再被续跑读到，留着只会残留过期语料、并在排查时混淆「这批切块哪来的」。
    只删同项目的其它 scope，不动本次 scope 的断点。
    """
    try:
        from app.models import DistillChunk

        n = int(db.query(DistillChunk).filter(
            DistillChunk.project_id == project_id,
            DistillChunk.scope != keep_scope,
        ).delete(synchronize_session=False))
        db.commit()
        if n:
            logging.getLogger(__name__).info(
                "清理换档残留蒸馏断点 project_id=%s keep=%s 删除=%s",
                project_id, keep_scope, n)
        return n
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "清理换档残留蒸馏断点失败 project_id=%s: %s", project_id, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def count_done_chunks(db: Session, project_id: int, scope: str) -> int:
    """已完成的主提炼块数（不含辅助阶段），供中断回执报「已保留 X/Y 块」。"""
    try:
        from app.models import DistillChunk

        return int(
            db.scalar(
                select(func.count(DistillChunk.id)).where(
                    DistillChunk.project_id == project_id,
                    DistillChunk.scope == scope,
                    ~DistillChunk.chunk_key.like(f"{_AUX_KEY_PREFIX}%"),
                )
            )
            or 0
        )
    except Exception:  # noqa: BLE001 —— 回执里的数字缺失不该影响通知本身
        return 0


def _apply_chunk_payload(
    payload: dict,
    all_rules: dict[str, list[dict]],
    all_forbidden: dict[str, list[str]],
) -> None:
    """把已完成块的净产出回灌进累加器（不做二次校验——入账时已校验过）。"""
    for dim, items in (payload.get("rules") or {}).items():
        if isinstance(items, list):
            all_rules.setdefault(str(dim), []).extend(items)
    for dim, items in (payload.get("forbidden") or {}).items():
        if isinstance(items, list):
            all_forbidden.setdefault(str(dim), []).extend(items)


def _merge_rows_into_chapters(rows) -> list[tuple[int, str]]:
    """把同一 ``chapter_no`` 的块按 ``idx`` 顺序拼回整章。

    入参 ``rows`` 必须已按 ``(chapter_no, idx, id)`` 排好序；本函数只做聚合，
    不再排序。``chapter_no`` 为空的历史数据退化为用 ``idx`` 当章号。
    """
    chapters: list[list] = []
    for row in rows:
        ch_no = row.chapter_no if row.chapter_no is not None else row.idx
        ch_no = int(ch_no)
        text = row.text or ""
        if chapters and chapters[-1][0] == ch_no:
            if text:
                prev = chapters[-1][1]
                chapters[-1][1] = f"{prev}\n\n{text}" if prev else text
        else:
            chapters.append([ch_no, text])
    return [(int(ch_no), str(text)) for ch_no, text in chapters]


def _load_novel_chapters(db: Session, novel_id: int) -> list[tuple[int, str]]:
    """加载一本语料的全部章节，返回 [(chapter_no, text), ...]，按章节号排序。

    ``novel_chunks.idx`` 是**章内块序号**（每章从 0 重数），不能单独作为全书
    排序键；这里按 ``(chapter_no, idx)`` 排序后，再把同一章的块拼回整章。
    这样返回的每个元素才是一章完整正文，采样、漂移首尾章与 ``chapter_range``
    都以真实章节为单位。
    """
    from app.models import NovelChunk

    rows = db.scalars(
        select(NovelChunk)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(
            func.coalesce(NovelChunk.chapter_no, NovelChunk.idx),
            NovelChunk.idx,
            NovelChunk.id,
        )
    ).all()
    return _merge_rows_into_chapters(rows)


def _sample_chapters(
    chapters: list[tuple[int, str]],
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
) -> list[tuple[int, str]]:
    """从全部章节中均匀采样代表性章节。

    优先取后半部分（成熟期文风），避免早期文风漂移问题。
    采样量不足时全部返回。
    """
    if len(chapters) <= max_chapters:
        return chapters
    # 取后 2/3（避开早期稚嫩期），再均匀采样
    start = len(chapters) // 3
    pool = chapters[start:]
    if len(pool) <= max_chapters:
        return pool
    step = len(pool) / max_chapters
    picks = sorted({int(i * step) for i in range(max_chapters)})
    return [pool[i] for i in picks]


def build_deterministic_features(chunk_text: str) -> dict:
    """v2.230：零 LLM 的确定性文体指标。

    这些数字 L1 stylometry 在整本书上算过一遍（20 个特征），但叙事蒸馏
    分块跑时手上没有块级数字，只能让 LLM「读一遍再描述风格」。结果是
    LLM 用形容词复述本来可以精确计算的东西（「句子长短交错」「对白较多」）。

    这里给每个块算一份硬指标，两个用途：
    1. 作为事实前缀塞进 extract prompt——LLM 只解释「为什么这样写」，
       不再从零发现数字，输出有锚点、不会瞎编
    2. 「文笔量化审计」维度直接由它生成（零 LLM），该维度不再让 LLM 输出

    指标选择原则：只保留**能明确区分作者指纹**的，不加装饰性统计。

    Args:
        chunk_text: 单个块的正文。

    Returns:
        可直接 JSON 序列化的字典。空文本时返回空字典。
    """
    if not chunk_text or not chunk_text.strip():
        return {}

    paras = [p.strip() for p in chunk_text.split("\n") if p.strip()]
    if not paras:
        return {}
    sents = stylometry.split_sentences(chunk_text)
    sent_lens = [len(s) for s in sents] or [0]
    para_lens = [len(p) for p in paras]
    n = len(chunk_text)
    per1k = 1000.0 / n if n else 0.0

    # 对白：引号内容。
    # 不加长度上限：旧写法 `{1,500}` 会让超过 500 字的整段对白**一条都不匹配**，
    # 直接从「对白句数 / 平均长度 / 占正文比」里消失（P2-2 实测对白占比偏低）。
    # 该修正只影响此后新蒸馏的块；已有档案里的历史块数字保持原样，不重算。
    dialogues = re.findall(r"[“「]([^”」]+)[”」]", chunk_text)
    dlg_lens = [len(d) for d in dialogues]
    dlg_chars = sum(dlg_lens)

    # 情绪：直陈（形容词贴标签）vs 动作承载（生理反应）
    emo_words = ("愤怒", "惊讶", "高兴", "难过", "紧张", "痛苦", "兴奋",
                 "失望", "恐惧", "悲伤", "惊恐", "恼怒", "羞愧", "欣慰")
    phys_words = ("咬牙", "握拳", "瞳孔", "呼吸", "脸色", "脚步", "手指",
                  "背脊", "心头一", "身子一", "嗓子", "喉咙")
    emo_cnt = sum(chunk_text.count(w) for w in emo_words)
    phys_cnt = sum(chunk_text.count(w) for w in phys_words)

    # 场景：地点词 / 环境词
    loc_words = ("广场", "大厅", "房间", "街道", "院子", "殿", "阁", "楼",
                 "树林", "山", "城", "巷")
    env_words = ("阳光", "月光", "灯火", "空气", "气息", "风声", "雨",
                 "光线", "阴影", "烛火")
    loc_cnt = sum(chunk_text.count(w) for w in loc_words)
    env_cnt = sum(chunk_text.count(w) for w in env_words)

    # 段落形态
    short_paras = sum(1 for p in paras if len(re.findall(r"[。！？…]", p)) <= 2)
    dlg_lead = sum(1 for p in paras if p.startswith(("“", "「")))

    mean_sl = sum(sent_lens) / len(sent_lens)
    variance = sum((x - mean_sl) ** 2 for x in sent_lens) / len(sent_lens)
    short_runs = re.findall(r"1+", ",".join("1" if x <= 5 else "0" for x in sent_lens))

    # 口径警告（短句率差三倍的根因）：
    # 这里的「短句」= 整句长度 <= 5，len(s) 含句末标点；不是全文统计里常见的
    # 「<15 字」。novel 7 实测：块级 ≤5 中位 14.7%；全文 <15 为 41.0%。
    # 差三倍是「阈值 5 vs 15」和「块级中位 vs 全文汇总」两个口径叠加的结果，
    # 两者不可直接对账。保留 ≤15 一列只为以后比较，不替换 ≤5。
    le5 = sum(1 for x in sent_lens if x <= 5) / len(sent_lens)
    le15 = sum(1 for x in sent_lens if x <= 15) / len(sent_lens)
    return {
        "句长": {
            "均值": round(mean_sl, 1),
            "标准差": round(variance ** 0.5, 1),
            "短句占比": round(le5, 3),
            "短句15占比": round(le15, 3),
            "最长连续短句": max((len(r) for r in short_runs), default=0),
        },
        "段落": {
            "段数": len(paras),
            "段长均值": round(sum(para_lens) / len(para_lens), 1),
            "一两句短段占比": round(short_paras / len(paras), 3),
            "对白开头段占比": round(dlg_lead / len(paras), 3),
        },
        "对白": {
            "句数": len(dialogues),
            "平均长度": round(dlg_chars / len(dlg_lens), 1) if dlg_lens else 0,
            "短对白占比": round(sum(1 for x in dlg_lens if x <= 10) / len(dlg_lens), 3) if dlg_lens else 0,
            "长对白占比": round(sum(1 for x in dlg_lens if x >= 50) / len(dlg_lens), 3) if dlg_lens else 0,
            "占正文比": round(dlg_chars / n, 3) if n else 0,
        },
        "情绪": {
            "直陈密度每千字": round(emo_cnt * per1k, 2),
            "动作承载密度每千字": round(phys_cnt * per1k, 2),
            "直陈除以动作": round(emo_cnt / phys_cnt, 2) if phys_cnt else None,
        },
        "场景": {
            "地点词每千字": round(loc_cnt * per1k, 2),
            "环境词每千字": round(env_cnt * per1k, 2),
        },
        "悬念": {
            "问号每千字": round(chunk_text.count("？") * per1k, 2),
            "省略号每千字": round(chunk_text.count("…") * per1k, 2),
        },
    }


def _format_features_prefix(feats: dict) -> str:
    """把确定性指标渲染成 prompt 里的事实前缀（紧凑、可读）。"""
    if not feats:
        return ""
    sl, pa, dl, em, sc, su = (
        feats.get("句长", {}), feats.get("段落", {}), feats.get("对白", {}),
        feats.get("情绪", {}), feats.get("场景", {}), feats.get("悬念", {}),
    )
    ratio = em.get("直陈除以动作")
    ratio_txt = f"，情绪直陈/动作承载 = {ratio}" if ratio is not None else ""
    short_txt = f"短句(≤5字) {sl.get('短句占比', 0):.1%}"
    if sl.get("短句15占比") is not None:
        short_txt += f"（≤15字 {sl.get('短句15占比', 0):.1%}）"
    return (
        "【本块客观统计（系统计算，非模型推测）】\n"
        f"句长 {sl.get('均值')}±{sl.get('标准差')} 字，{short_txt}，"
        f"最长连续短句 {sl.get('最长连续短句')} 句\n"
        f"段落 {pa.get('段数')} 段，段长均值 {pa.get('段长均值')} 字，"
        f"一两句短段占 {pa.get('一两句短段占比', 0):.1%}，对白开头段占 {pa.get('对白开头段占比', 0):.1%}\n"
        f"对白 {dl.get('句数')} 句，平均 {dl.get('平均长度')} 字，"
        f"短对白(≤10字) {dl.get('短对白占比', 0):.1%}，长对白(≥50字) {dl.get('长对白占比', 0):.1%}，"
        f"占正文 {dl.get('占正文比', 0):.1%}\n"
        f"情绪直陈 {em.get('直陈密度每千字')}/千字，动作承载 {em.get('动作承载密度每千字')}/千字{ratio_txt}\n"
        f"地点词 {sc.get('地点词每千字')}/千字，环境词 {sc.get('环境词每千字')}/千字\n"
        f"问号 {su.get('问号每千字')}/千字，省略号 {su.get('省略号每千字')}/千字\n"
        "以上数字已精确算出，**不要再重新估数**；你的任务是解释作者为什么这样写、"
        "这样写在叙事上起了什么作用。\n\n"
    )


def _format_features_rule(feats: dict) -> str:
    """把确定性指标渲染成一条「文笔量化审计」规则文本。

    在注入 prompt 时，这条规则与其他维度并列展示。措辞刻意保留具体数字——
    LLM 拿到「1-2 句短段占 92.4%」比拿到「段落简短有力」有用得多。

    Args:
        feats: `build_deterministic_features` 的返回值。

    Returns:
        单行规则字符串；feats 为空时返回空串。
    """
    if not feats:
        return ""
    sl = feats.get("句长", {})
    pa = feats.get("段落", {})
    dl = feats.get("对白", {})
    em = feats.get("情绪", {})
    su = feats.get("悬念", {})
    parts = [
        f"句长 {sl.get('均值')}±{sl.get('标准差')} 字",
        f"短句(≤5字)占 {sl.get('短句占比', 0):.1%}",
    ]
    if sl.get("短句15占比") is not None:
        parts.append(f"短句(≤15字)占 {sl.get('短句15占比', 0):.1%}")
    parts += [
        f"一两句短段占 {pa.get('一两句短段占比', 0):.1%}",
        f"对白开头段占 {pa.get('对白开头段占比', 0):.1%}",
        f"对白占正文 {dl.get('占正文比', 0):.1%}",
        f"短对白占 {dl.get('短对白占比', 0):.1%}",
        f"情绪直陈 {em.get('直陈密度每千字')}/千字",
        f"动作承载 {em.get('动作承载密度每千字')}/千字",
        f"问号 {su.get('问号每千字')}/千字",
        f"省略号 {su.get('省略号每千字')}/千字",
    ]
    ratio = em.get("直陈除以动作")
    if ratio is not None:
        parts.append(f"直陈/动作比 {ratio}")
    return "本块实测：" + "，".join(parts)


def _build_extract_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造单块提炼的 system/user prompt（v2.260 拆分前的原版）。

    ## v2.260 拆分

    14 个维度塞进 1 个 prompt 让模型注意力分散、输出易截断。v2.260 把它
    拆成 2 组：

    - `_build_extract_character_prompt`：3 维（人物描写 / 对白风格 / 角色声音）
    - `_build_extract_narrative_prompt`：7 维（段落编排 / 场景构建 / 情绪表达 /
      节奏控制 / 世界观呈现 / 剧情编排模式 / 伏笔与悬念设计）

    这个函数保留作为**回退路径**（拆分模式被禁用时用）。
    """
    system = (
        "你是一位资深中文网文编辑，擅长分析作者的写作风格与叙事手法。"
        "你的任务是从给定文本中提炼**可复用的叙事规则**，而不是总结剧情或复制原文。\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，用你自己的话概括作者「怎么写的」，"
        "**绝对不要直接复制原文句子作为规则**\n"
        "2. 规则描述后面括号里附**原文证据**——引用一句原文（20–60 字，少于 20 字不作为证据）证明这条规则存在\n"
        "3. 规则要**具体可操作**，例如「用连续动作描写紧张感」而不是「文笔好」\n"
        "4. 只提炼原文中**确实反复出现**的模式，不要编造\n"
        "5. 按以下维度分类输出\n\n"
        "**正确的规则示例**：\n"
        "  人物描写手法: [\"用动作和神态暗示情绪，不直接写'害怕/紧张'（证据：他指尖不自觉收紧，连呼吸都放轻了，生怕被人听见）\", "
        "\"对白后紧跟动作描写，不用'说道'收尾（证据：南宫爵沉声道：'你确定要现在动手？'说完已按住剑柄）\"]\n"
        "**错误的规则示例（不要这样输出）**：\n"
        "  人物描写手法: [\"小钟这话，可是壮着胆子说的。\", \"听言，南宫爵的黑眸顿时涌起了一阵冷厉。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "人物描写手法": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "对白风格": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "段落编排": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "场景构建": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "情绪表达": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "节奏控制": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "世界观呈现": ["抽象规则描述（证据：原文引用）", ...],\n'
        '  "剧情编排模式": ["伏笔如何埋设与回收（证据：原文引用）", "冲突如何升级（证据：原文引用）", ...],\n'
        '  "角色声音词典": ["具体角色名：说话习惯/口头禅/自称（证据：原文引用）", "具体角色名：说话习惯/口头禅/自称（证据：原文引用）", ...],\n'
        '  "伏笔与悬念设计": ["悬念如何设置与揭示（证据：原文引用）", ...]\n'
        '}\n'
    )
    feats = build_deterministic_features(chunk_text)
    prefix = _format_features_prefix(feats)
    user = (
        f"以下是《{chunk_label}》的片段，请提炼叙事规则：\n\n"
        f"{prefix}"
        f"【正文】\n{chunk_text}\n\n"
        "请按上述维度输出 JSON。每条规则必须是抽象描述+原文证据，不要复制原文作为规则。"
    )
    return system, user


# ──────────────────────────────────────────────────────────────────────
# v2.260：extract prompt 拆分为「角色组」和「叙事组」
# ──────────────────────────────────────────────────────────────────────
#: 角色组维度（3 维）：人物描写 / 对白风格 / 角色声音
_EXTRACT_CHARACTER_DIMS = (
    "人物描写手法",
    "对白风格",
    "角色声音词典",
)

#: 叙事组维度（7 维）：段落编排 / 场景构建 / 情绪表达 / 节奏控制 /
#: 世界观呈现 / 剧情编排模式 / 伏笔与悬念设计
_EXTRACT_NARRATIVE_DIMS = (
    "段落编排",
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
    "剧情编排模式",
    "伏笔与悬念设计",
)

#: 结构组维度（3 维，v2.261 从叙事组再拆）：怎么搭架子、埋钩子。
#: 这三维都回答「故事骨架怎么长出来的」——段落怎么切、主线怎么编、
#: 悬念怎么埋/揭/收。
_EXTRACT_STRUCTURE_DIMS = (
    "段落编排",
    "剧情编排模式",
    "伏笔与悬念设计",
)

#: 技法组维度（4 维，v2.261 从叙事组再拆）：怎么营造氛围、透设定。
#: 这四维都回答「场景怎么让人有感觉」——环境怎么铺、情绪怎么载、
#: 节奏怎么走、世界观怎么透。
_EXTRACT_TECHNIQUE_DIMS = (
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
)


def _build_extract_character_prompt(
    chunk_text: str, chunk_label: str,
) -> tuple[str, str]:
    """角色组（3 维）提炼 prompt（v2.260 拆分新增）。

    把「人物描写 / 对白风格 / 角色声音」3 维从原 extract prompt 拆出来。
    这 3 维都关注**人物与对话**——模型视野更集中，输出更精。

    ## 与原版的差异

    - 只输出 3 个维度（不是 10 个），模型注意力不再被 7 个叙事维度分散
    - prompt 短 ~40%（约 530 字而不是 880 字），可塞更长 chunk
    - 输出 JSON 体更小，截断概率从约 1.3% 降到 0.4%（雪中 990 章实测推算）
    """
    system = (
        "你是一位资深中文网文编辑，专精**人物与对话分析**。\n\n"
        "本任务只提炼 3 个维度，全部围绕**人物视角**：\n"
        "1. **人物描写手法**：作者怎么描写人物（动作/神态/外貌/心理）\n"
        "2. **对白风格**：作者怎么写对话（句长/敬语/称谓/动作搭配）\n"
        "3. **角色声音词典**：具体角色的说话习惯（口头禅/自称/语气/句长）\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，不要直接复制原文\n"
        "2. 规则后面括号里附**原文证据**——引用一句原文（20–60 字，少于 20 字不作为证据）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. **角色声音词典**里必须写出具体角色名，不能用「某角色」\n"
        "5. 证据必须是原文中**连续的一整段**，不能把两句拼在一起，不能用省略号跳过中间内容\n\n"
        "**正确示例**：\n"
        "  人物描写手法: [\"用动作和神态暗示情绪，不直接写『害怕/紧张』（证据：他指尖不自觉收紧，连呼吸都放轻了，生怕被人听见）\"]\n"
        "  角色声音词典: [\"徐凤年：口头禅「本世子」语气轻佻 句长短句 关系：对下属命令式（证据：南宫爵沉声道：『你确定要现在动手？』说完已按住剑柄）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  人物描写手法: [\"小钟这话，可是壮着胆子说的。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "人物描写手法": ["抽象规则（证据：原文引用）", ...],\n'
        '  "对白风格": ["抽象规则（证据：原文引用）", ...],\n'
        '  "角色声音词典": ["具体角色名：说话习惯/口头禅/语气/句长（证据：原文引用）", ...]\n'
        '}\n'
    )
    feats = build_deterministic_features(chunk_text)
    prefix = _format_features_prefix(feats)
    user = (
        f"以下是《{chunk_label}》的片段，请提炼人物与对话相关规则：\n\n"
        f"{prefix}"
        f"【正文】\n{chunk_text}\n\n"
        "请按上述 3 维输出 JSON。"
    )
    return system, user


def _build_extract_narrative_prompt(
    chunk_text: str, chunk_label: str,
) -> tuple[str, str]:
    """叙事组（7 维）提炼 prompt（v2.260 拆分新增）。

    把「段落编排 / 场景构建 / 情绪表达 / 节奏控制 / 世界观呈现 /
    剧情编排模式 / 伏笔与悬念设计」7 维从原 extract prompt 拆出来。

    ## 与原版的差异

    - 只输出 7 个维度（不是 10 个），不再受 3 个角色维度稀释
    - 视野聚焦「结构与技法」——场景怎么搭、节奏怎么走、世界观怎么透
    - prompt 长度与原版相当（这 7 维关联紧密，不宜再拆）
    """
    system = (
        "你是一位资深中文网文编辑，专精**结构与技法分析**。\n\n"
        "本任务提炼 7 个维度的规则，并附 **3 个块级字段**：\n"
        "1. **段落编排**：段长分布、段间过渡、对话与叙述的穿插\n"
        "2. **场景构建**：环境描写方式、空间切换手法\n"
        "3. **情绪表达**：情绪承载方式（动作/环境/直陈的取舍）\n"
        "4. **节奏控制**：情节推进的快慢、张弛节奏\n"
        "5. **世界观呈现**：设定如何透露给读者（对话/旁白/动作）\n"
        "6. **剧情编排模式**：伏笔埋设、冲突升级、转折手法\n"
        "7. **伏笔与悬念设计**：悬念如何设置、揭示、回收\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，不要直接复制原文\n"
        "2. 规则后面括号里附**原文证据**——引用一句原文（20–60 字，少于 20 字不作为证据）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. 这 7 维关注的是「作者怎么写」而不是「写了什么」\n"
        "5. 证据必须是原文中**连续的一整段**，不能把两句拼在一起，不能用省略号跳过中间内容\n"
        "6. 另附三个**块级字段**（不是规则，不要写成多条句子）：\n"
        "   - primary_scene：本段**主场景，有且只有一个**，从下面 14 个 key 里选：\n"
        "     combat / daily / groupchat / trade / cultivate / travel / interrogate /\n"
        "     reveal / intimacy / farewell / banquet / misunderstand / rescue / night\n"
        "     过渡段、设定说明段没有明确场景时，写 \"none\"，不要硬套一个场景。\n"
        "   - secondary_scenes：**0–1 个**次场景，没有就写空数组 []。\n"
        "     **不要为了凑数而上报标签**——只有确实贯穿本段才写；\n"
        "   - 章节摘要：**80–120 字**，只写本段发生了什么（人物、地点、冲突、结果），\n"
        "     不评价、不抒情、不写「本段展现了…」这类分析句\n"
        "   - 人物别名：本段出现的角色，「规范名」用最常见的完整姓名，\n"
        "     「称呼」列文中用过的简称/称号/代称；**不要收**「那名男子」「安保队长」这类泛称\n\n"
        "**正确示例**：\n"
        "  段落编排: [\"短段切对话节奏，长段铺环境氛围（证据：北凉王府龙盘虎踞于清凉山，千门万户，极土木之盛，一眼望不到尽头）\"]\n"
        "  节奏控制: [\"冲突升级通过对话长度骤变，从短对白跳到大段独白（证据：他话音还没落下，屋里的灯忽然灭了，院外传来一声极轻的脚步声）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  段落编排: [\"北凉王府龙盘虎踞于清凉山，千门万户，极土木之盛。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "段落编排": ["抽象规则（证据：原文引用）", ...],\n'
        '  "场景构建": ["抽象规则（证据：原文引用）", ...],\n'
        '  "情绪表达": ["抽象规则（证据：原文引用）", ...],\n'
        '  "节奏控制": ["抽象规则（证据：原文引用）", ...],\n'
        '  "世界观呈现": ["抽象规则（证据：原文引用）", ...],\n'
        '  "剧情编排模式": ["伏笔埋设/冲突升级/转折手法（证据：原文引用）", ...],\n'
        '  "伏笔与悬念设计": ["悬念如何设置/揭示/回收（证据：原文引用）", ...],\n'
        '  "primary_scene": "daily",\n'
        '  "secondary_scenes": [],\n'
        '  "章节摘要": "80–120 字，只写发生了什么",\n'
        '  "人物别名": [{"规范名": "宋书航", "称呼": ["书航", "宋师兄"]}]\n'
        '}\n'
    )
    feats = build_deterministic_features(chunk_text)
    prefix = _format_features_prefix(feats)
    user = (
        f"以下是《{chunk_label}》的片段，请提炼叙事结构与文风相关规则：\n\n"
        f"{prefix}"
        f"【正文】\n{chunk_text}\n\n"
        "请按上述 7 维输出 JSON。"
    )
    return system, user


def _build_extract_structure_prompt(
    chunk_text: str, chunk_label: str,
) -> tuple[str, str]:
    """结构组（3 维）提炼 prompt（v2.261 从叙事组再拆）。

    ## 为什么再拆

    v2.260 把 10 维拆成「角色组 3 + 叙事组 7」，真链路 rejected 947→734、
    规则 1661→2112。但叙事组仍是 7 维，probe 显示 prompt 836 字、单次输出
    1408 字。再拆一层能把每组压到 3-4 维，视野进一步聚焦。

    ## 拆法

    叙事组 7 维本质是两类问题：

    - **结构**（本组，3 维）：故事的骨架怎么搭
      - 段落编排：段怎么切、怎么转场
      - 剧情编排模式：主线怎么编、冲突怎么升
      - 伏笔与悬念设计：钩子怎么埋、怎么揭、怎么收

    - **技法**（`_EXTRACT_TECHNIQUE_DIMS`，4 维）：场景怎么让人有感觉
      - 场景构建 / 情绪表达 / 节奏控制 / 世界观呈现

    「结构」是宏观叙事骨架，「技法」是微观质感——两者关注的判断依据不同，
    混在一个 prompt 里会互相稀释。
    """
    system = (
        "你是一位资深中文网文编辑，专精**叙事结构分析**。\n\n"
        "本任务只提炼 3 个维度，全部围绕**故事骨架怎么搭**：\n"
        "1. **段落编排**：段落长度分布、段间如何过渡、对话段与叙述段如何穿插\n"
        "2. **剧情编排模式**：主线如何推进、冲突如何升级、转折点怎么安排\n"
        "3. **伏笔与悬念设计**：钩子如何埋设、悬念如何悬置、答案如何回收\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，不要直接复制原文\n"
        "2. 规则后面括号里附**原文证据**——引用一句原文（20–60 字，少于 20 字不作为证据）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. 关注「作者怎么安排结构」而不是「写了什么剧情」\n"
        "5. 若这一块看不出一场完整冲突，就不要硬写「剧情编排模式」\n\n"
        "**正确示例**：\n"
        "  段落编排: [\"冲突段用短句短段压迫节奏，铺垫段用长段铺陈（证据：他一刀劈下。）\"]\n"
        "  伏笔与悬念设计: [\"用一句没说完的话收章留钩子（证据：可就在此时，门外——）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  剧情编排模式: [\"徐凤年去了北凉。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "段落编排": ["抽象规则（证据：原文引用）", ...],\n'
        '  "剧情编排模式": ["主线推进/冲突升级/转折手法（证据：原文引用）", ...],\n'
        '  "伏笔与悬念设计": ["悬念如何设置/悬置/回收（证据：原文引用）", ...]\n'
        '}\n'
    )
    feats = build_deterministic_features(chunk_text)
    prefix = _format_features_prefix(feats)
    user = (
        f"以下是《{chunk_label}》的片段，请提炼叙事结构相关规则：\n\n"
        f"{prefix}"
        f"【正文】\n{chunk_text}\n\n"
        "请按上述 3 维输出 JSON。"
    )
    return system, user


def _build_extract_technique_prompt(
    chunk_text: str, chunk_label: str,
) -> tuple[str, str]:
    """技法组（4 维）提炼 prompt（v2.261 从叙事组再拆）。

    见 `_build_extract_structure_prompt` 的拆分说明。

    这 4 维回答「场景怎么让人有感觉」——环境怎么铺、情绪怎么载、
    节奏怎么走、设定怎么透。它们的共同点是**微观质感**，与结构组的
    宏观骨架是两类判断。
    """
    system = (
        "你是一位资深中文网文编辑，专精**场景与情绪技法分析**。\n\n"
        "本任务只提炼 4 个维度，全部围绕**场景怎么让人有感觉**：\n"
        "1. **场景构建**：环境描写方式（视觉/听觉/嗅觉）、空间如何切换、物件如何参与叙事\n"
        "2. **情绪表达**：情绪如何承载（动作/环境/直陈的取舍），哪些情绪作者选择不直说\n"
        "3. **节奏控制**：情节推进的快慢、张弛如何交替、紧张感用什么手法加码\n"
        "4. **世界观呈现**：设定如何透露给读者（对话/旁白/动作/道具），而不是直接解释\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，不要直接复制原文\n"
        "2. 规则后面括号里附**原文证据**——引用一句原文（20–60 字，少于 20 字不作为证据）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. 关注「作者怎么营造」而不是「营造出了什么」\n\n"
        "**正确示例**：\n"
        "  情绪表达: [\"愤怒不写『他很生气』，写手指关节发白（证据：指节捏得发白）\"]\n"
        "  世界观呈现: [\"设定借人物争吵带出，不用旁白解释（证据：你真当龙虎山是善地？）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  场景构建: [\"北凉王府很大。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "场景构建": ["抽象规则（证据：原文引用）", ...],\n'
        '  "情绪表达": ["抽象规则（证据：原文引用）", ...],\n'
        '  "节奏控制": ["抽象规则（证据：原文引用）", ...],\n'
        '  "世界观呈现": ["抽象规则（证据：原文引用）", ...]\n'
        '}\n'
    )
    feats = build_deterministic_features(chunk_text)
    prefix = _format_features_prefix(feats)
    user = (
        f"以下是《{chunk_label}》的片段，请提炼场景与情绪技法相关规则：\n\n"
        f"{prefix}"
        f"【正文】\n{chunk_text}\n\n"
        "请按上述 4 维输出 JSON。"
    )
    return system, user


def _build_forbidden_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造反向禁忌提取的 system/user prompt。

    反向禁忌 = 作者**从不使用**的表达模式。LLM 更容易遵循具体的
    「不要用 X」而不是通用的「忌 AI 腔」。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**这个作者从不使用的表达模式**"
        "（反向禁忌清单）。这些禁忌将在 AI 续写时注入提示词，防止生成 AI 腔。\n\n"
        "要求：\n"
        "1. 每条禁忌必须是**具体的词句模式**，不是抽象描述\n"
        "2. 只列原文中**确实没有出现**的模式（如果原文中出现了，就不是禁忌）\n"
        "3. 按以下维度分类\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "禁用转折词": ["就在这时", "突然之间"],\n'
        '  "禁用情绪直陈": ["心中充满了", "眼中闪过一丝"],\n'
        '  "禁用段尾拔高": ["他知道，", "她明白，"],\n'
        '  "禁用解释性对白": ["你知道吗？", "我告诉你"]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼这个作者**从不使用**的表达模式：\n\n"
        f"{chunk_text}\n\n"
        "请按上述维度输出 JSON。只列原文中没有出现的模式。"
    )
    return system, user


def _parse_extract_result(raw: str) -> dict:
    """解析 LLM 返回的 JSON，容错处理。"""
    import json
    import re

    # 尝试直接解析
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass

    # 尝试提取 JSON 块（LLM 有时会包在 ```json ... ``` 里）
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass

    # 尝试提取 { ... } 块
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass

    return {}


#: 统计型证据的识别模式（v2.263）。
#:
#: LLM 有时把我们自己塞进 prompt 的**确定性指标**当成「原文证据」引用：
#:
#:     节奏控制: ["通过短句和短段的使用，加快叙事节奏
#:                （证据：句长 61.1±36.4 字，短句(≤5字) 0.0%，段落 36 段...）"]
#:
#: 这段文字**确实写在 prompt 里**（`_format_features_prefix` 渲染的），
#: 但**不在 chunk_text 里**——所以旧逻辑判它「证据不存在」→ rejected += 1。
#:
#: 这是**合理的引用行为被当成幻觉**。LLM 的解释性任务本来就是「用这些
#: 数字说明作者为什么这样写」，它引用这些数字是符合要求的。
#:
#: ## 判据演化
#:
#: v2.263 初版要求「命中 >= 2 个统计模式」，实测漏掉了一整类：
#:
#:     情绪直陈 0.67/千字，动作承载 0.0/千字   ← 只命中「/千字」一个模式
#:
#: 改为**统计特征密度**判据：统计片段去掉后剩余的中文内容极少 → 是统计。
#: 这样密度型（x/千字）也覆盖，同时「他等了三分钟才走」这类真叙事
#: 因为剩余中文多而被正确拒绝。
_STAT_PATTERNS = (
    re.compile(r"\d+\.\d+\s*±\s*\d+"),            # 61.1±36.4
    re.compile(r"\d+(?:\.\d+)?\s*%"),               # 95.2%
    re.compile(r"\d+(?:\.\d+)?\s*字"),              # 62.7 字
    re.compile(r"\d+(?:\.\d+)?\s*/\s*千字"),        # 0.67/千字
    re.compile(r"\d+(?:\.\d+)?\s*句"),              # 对白 14 句
    re.compile(r"(?:段数|段长均值|短段占|对白开头段占|短句占比|直陈密度|"
               r"动作承载密度|地点词每千字|环境词每千字|问号每千字|"
               r"省略号每千字|平均长度|短对白|长对白|占正文)"),  # 指标名
)

#: 含这些词说明是真叙事句，不是纯统计。
_NARRATIVE_HINT = (
    "说", "道", "笑", "看", "想", "走", "跑", "站", "坐", "拿", "放",
    "他", "她", "我", "你", "们", "这", "那", "了", "着", "之",
)


def _is_statistical_evidence(evidence: str) -> bool:
    """判断一条证据是不是「引用了系统统计指标」而非编造。

    判据分两步：

    1. **必须命中至少一个统计模式**（数字+单位 / 指标名）——
       纯叙事句不会有「x/千字」「段长均值」这类东西。
    2. **剥掉统计片段后，剩余中文极少**（<= 8 字）——
       说明整句几乎都是在报数字。

    第 2 步是关键：「他等了三分钟才走」命中「数字+单位」但剩余
    「他等了三分钟才走」剥不完全，剩余中文仍多 → 正确拒绝。
    """
    if not any(p.search(evidence) for p in _STAT_PATTERNS):
        return False

    residual = evidence
    for p in _STAT_PATTERNS:
        residual = p.sub("", residual)
    # 去掉标点/空白/数字符号，只留中文看还剩什么
    residual = re.sub(r"[\s，、。：；：（）()【】%±/\d.+-]", "", residual)
    if len(residual) <= 8:
        return True
    # 剩余较长时，含叙事词就认为不是纯统计
    return not any(w in residual for w in _NARRATIVE_HINT)


def _validate_evidence(rule: str, chunk_text: str) -> bool:
    """验证规则中的证据是否真实存在于原文中。

    从规则字符串中提取「证据：xxx」部分，检查 xxx 是否在原文中出现。
    如果规则本身就是原文中的句子（LLM 直接引用了原文），也直接验证。

    这是防幻觉的最后一道闸——LLM 编造的伪规则在这里被拦掉。

    ## v2.263：统计型证据放行

    LLM 引用**我们自己塞进 prompt 的确定性指标**（`_format_features_prefix`
    渲染的「句长 61.1±36.4 字」等）时，这些文字不在 chunk_text 里，
    旧逻辑把它当幻觉拒掉。实测这是 rejected 的主要来源之一。

    这类引用是**符合 prompt 要求的行为**（prompt 明确要求「解释作者
    为什么这样写」），不该拒绝。用 `_is_statistical_evidence` 识别后放行。
    """
    import re as _re

    # 尝试提取「证据：xxx」部分
    m = _re.search(r"证据[：:]\s*(.+?)[）)]?\s*$", rule)
    if m:
        evidence = m.group(1).strip()
    else:
        # LLM 可能直接引用了原文句子作为规则，没有加「证据：」前缀
        evidence = rule.strip()

    if not evidence or len(evidence) < EVIDENCE_MIN_CHARS:
        # 2026-11-12：短证据不落库。短于 20 字的引文在章内无法唯一定位，
        # 事后只能做两端锚定（弱锚点），模型看错上下文比没有更糟。
        # 历史档案不重蒸，由检索侧的章窗口兜底；新档案必须从源头卡住。
        return False

    # 归一化：去空白 + 统一引号（LLM 常用单引号，原文用双引号）
    def _norm(s: str) -> str:
        s = _re.sub(r"\s+", "", s)
        for q in ("'", "\u2018", "\u2019", "\u201c", "\u201d", '"'):
            s = s.replace(q, "")
        return s

    if _norm(evidence) in _norm(chunk_text):
        return True

    # v2.263：统计型证据放行（引用了 prompt 里的确定性指标，不是幻觉）
    return _is_statistical_evidence(evidence)


def _is_verbatim_copy(rule: str, chunk_text: str) -> bool:
    """检测规则是否就是原文句子的逐字复制（没有抽象化）。

    LLM 有时不按 prompt 要求输出抽象规则，而是直接复制原文句子。
    这种规则作为「叙事手法」注入提示词，信息量极低。
    检测方法：规则文本（去引号归一化后）是否是原文中的某个完整句子。

    ## 现状：对当前规则格式近乎失效

    判据是「规则 == 某个单句，或规则是某个单句的前缀」。但当前 prompt
    要求输出 `抽象描述（证据：原文引用）` 的格式，规则长度 30-200 字，
    永远不等于一个 10-50 字的单句，也不会是它的前缀。

    雪中悍刀行 v2.268 实测：1223 条规则里，**0 条**被这个函数命中。

    保留它是因为：如果哪天 prompt 改回「只输出规则不附证据」，
    这个检查会重新有用。**它是失效，不是错误，不要因为 0 命中就删掉。**

    ## v2.270：修一个语义完全错误的 _norm

    原代码写的是「三个连续双引号」开头的 replace 调用：

        s.replace(单引号, 空串).replace(三个双引号, 空串)

    那三个双引号开启了一个**三引号字符串**，把紧跟的「逗号+空格+引号」
    一起吞进去。AST 解析出来第一个参数是字符串【逗号+空格+引号】：

        s.replace(逗号空格引号, 空串)

    也就是说这段代码在**删掉『逗号+空格+引号』这个三字符序列**，
    写了两遍，而**从来没有处理过任何弯引号**。

    语法合法、语义完全错误——不报错，只是做了件毫无意义的事。

    `_validate_evidence` 里有一份同样的副本，在 v2.263 重写该函数时
    顺手绕过了（改成显式 for 循环），但错误原因一直没被识别，
    当时 commit 里记的是「统计证据放行」。

    本版把两份归一化统一成同一个写法。
    """
    import re

    def _norm(s: str) -> str:
        s = re.sub(r"\s+", "", s)
        for q in ("'", "\u2018", "\u2019", "\u201c", "\u201d", '"'):
            s = s.replace(q, "")
        return s

    rule_norm = _norm(rule)
    if len(rule_norm) < 8:
        return False
    # 检查规则是否是原文中的某个句子。
    # v2.271：切分时**保留终止标点**。原来用 re.split 会把「。」丢掉，
    # 导致带句末标点的整句复制永远匹配不上（规则有「。」，句子没有）。
    sentences = re.findall(r"[^。！？…\n]+[。！？…]?", chunk_text)
    for sent in sentences:
        sent_norm = _norm(sent)
        if sent_norm and rule_norm == sent_norm:
            return True
        # 规则是原文句子的前缀（LLM 截断了句子）
        if len(sent_norm) > len(rule_norm) and sent_norm.startswith(rule_norm):
            return True
    return False


#: 角色声音提取的目标角色数上限。百万字小说有几百个角色，
#: 取高频前 N 个（主角 + 重要配角 + 关键反派）。
#: 清洗后的规范名池（去伪角色/去分词截断）没有名额浪费，可以放宽到 30。
MAX_VOICE_CHARACTERS = 30

#: 主要角色逐个提取的数量上限（前 N 个高频角色单独提取，深度分析）
VOICE_MAIN_CHARACTERS = 8

#: 配角分批提取的数量（每批 N 个角色，1 次 LLM 调用）
VOICE_BATCH_SIZE = 7


def _scene_type(text: str) -> str:
    """识别章节的主要场景类型。返回 SCENE_KEYWORDS 的键，无匹配返回「其他」。"""
    best_type = "其他"
    best_count = 0
    for scene_type, keywords in SCENE_KEYWORDS.items():
        count = sum(text.count(kw) for kw in keywords)
        if count > best_count:
            best_count = count
            best_type = scene_type
    return best_type


def _sample_chapters_v6(
    chapters: list[tuple[int, str]],
    ratio: float = SAMPLE_RATIO,
    cap: int | None = None,
) -> list[tuple[int, str]]:
    """按百分比采样 + 对话密集优先 + 场景均衡。

    ``chapters`` 必须是**真实章节**（一个 ``chapter_no`` 一条完整正文），由
    ``_load_novel_chapters`` 按章聚合后传入；不再是 ``novel_chunks`` 的块行。

    策略：
    1. 按 ratio 计算采样量（603 章 × 30% = 180 章）
    2. 按场景类型分组，每种场景至少采样几章（确保覆盖面）
    3. 剩余配额按对话密度排序填充（对白多的章节优先）

    ``cap`` 是用户显式指定的采样章数（0 = 全本），优先级高于比例与字数下限——
    用户说「只取 200 章」就取 200 章，不再偷偷加到 300。
    """
    if not chapters:
        return []
    if cap is not None and int(cap) <= 0:
        return list(chapters)
    total_chars = sum(len(t) for _, t in chapters if t)

    # 短篇保护：若全语料字数就不足下限，直接全取（没有砍的空间）。
    if total_chars <= MIN_SAMPLE_CHARS:
        return list(chapters)

    # 目标章节数：按比例取，但保证采样字数不低于下限。
    # 平均章长估算需要多少章才够 MIN_SAMPLE_CHARS。
    avg_ch = total_chars / max(len(chapters), 1)
    ratio_target = int(len(chapters) * ratio)
    chars_target = int(MIN_SAMPLE_CHARS / max(avg_ch, 1))
    if cap is not None:
        target = min(len(chapters), max(1, int(cap)))
    else:
        target = max(10, ratio_target, chars_target)
    if len(chapters) <= target:
        return list(chapters)

    def dialogue_ratio(text: str) -> float:
        if not text:
            return 0.0
        quote_chars = 0
        for q in ("'", "\u2018", "\u2019", "\u201c", "\u201d", '"'):
            quote_chars += text.count(q)
        return quote_chars / max(len(text), 1)

    # 按场景类型分组
    scene_groups: dict[str, list[tuple[float, int, str]]] = {}
    for ch_no, text in chapters:
        if not text:
            continue
        st = _scene_type(text)
        scene_groups.setdefault(st, []).append((dialogue_ratio(text), ch_no, text))

    # 每种场景至少取 3 章（对话密度最高的）。
    # chapters 已按章聚合，正常情况下 chapter_no 唯一；picked_nos 只用于防御
    # 调用方传入未聚合的块行，避免同一章被重复计入。
    picked: list[tuple[int, str]] = []
    picked_nos: set[int] = set()
    per_scene_min = 3
    for st, items in scene_groups.items():
        items.sort(key=lambda x: -x[0])
        for _, ch_no, text in items[:per_scene_min]:
            if ch_no not in picked_nos:
                picked.append((ch_no, text))
                picked_nos.add(ch_no)

    # 剩余配额：从全部章节按对话密度排序填充
    remaining = target - len(picked)
    if remaining > 0:
        all_scored = sorted(
            [(dialogue_ratio(t), ch_no, t) for ch_no, t in chapters if t and ch_no not in picked_nos],
            key=lambda x: -x[0],
        )
        for _, ch_no, text in all_scored[:remaining]:
            # 防御未聚合输入：同一章只收一次，避免重复块虚增覆盖。
            if ch_no in picked_nos:
                continue
            picked.append((ch_no, text))
            picked_nos.add(ch_no)

    # 按章节号排序返回
    return sorted(picked, key=lambda x: x[0])


def _sample_chapters_fixed(
    chapters: list[tuple[int, str]], n_chapters: int
) -> list[tuple[int, str]]:
    """固定章数采样：均匀取 n_chapters 章，优先取后2/3（避开早期稚嫩期）。

    ``chapters`` 是真实章节（一个 ``chapter_no`` 一条完整正文）。"取后 2/3"
    指按章节号跳过全书前 1/3 的**章节**，再从剩余章节里均匀采样；不再是按
    ``novel_chunks`` 块行跳过前 1/3（那会误把各章开头块当成"早期章节"）。

    平衡档 / 快速档用这个（docs 记载v3/v5 时代就是固定 30 章）。
    与 `_sample_chapters_v6` 的区别：**不做**对话密度排序、**不做**场景均衡——
    那是完整档才付得起的成本。
    """
    if not chapters:
        return []
    target = max(1, int(n_chapters))
    if len(chapters) <= target:
        return list(chapters)
    # 取后 2/3（避开早期稚嫩期），再均匀采样
    start = len(chapters) // 3
    pool = chapters[start:]
    if len(pool) <= target:
        return list(pool)
    step = len(pool) / target
    picks = sorted({min(int(i * step), len(pool) - 1) for i in range(target)})
    return [pool[i] for i in picks]


def sample_chapters_for_tier(
    chapters: list[tuple[int, str]], tier: NarrativeTier
) -> list[tuple[int, str]]:
    """按档位配置采样章节——档位在采样轴上的唯一入口。"""
    cap = tier.sample_cap
    if tier.sampling_mode == "percentage":
        return _sample_chapters_v6(
            chapters, ratio=tier.sample_ratio or SAMPLE_RATIO, cap=cap
        )
    # 采样覆盖只作用于比例采样档；固定章数档保持档位定义（30 章）。
    return _sample_chapters_fixed(chapters, tier.fixed_chapters or 30)


#: 占位符：LLM 直接照抄 prompt 示例（「角色A」「具体角色名」）
_PLACEHOLDER_RE = re.compile(r"^(角色[A-Za-z0-9]|具体角色名)")

#: 高频中文虚词/动词——图谱会把它们误抽成「角色」节点，混进声音词典
_FAKE_CHARS = frozenset({
    # 言说/动作/情绪（常被当成角色名）
    "相信", "喜欢", "冷声", "冷笑", "冷嗤", "冷怒", "厉声", "沉默",
    "摇头", "点头", "惊讶", "愤怒", "高兴", "难过", "紧张", "放松",
    "微笑", "皱眉", "叹气", "哭泣", "大喊", "低声", "转身", "离开",
    "进入", "出现", "消失", "突然", "忽然", "立刻", "马上",
    "解释", "安慰", "怀孕", "越是", "成天", "强势", "全都", "管她",
    "胡乱", "胡说", "明知", "别瞎", "干嘛", "苍白", "满脸", "索性",
    "清楚", "明白", "明显", "上前", "松开", "后悔", "能有", "时光",
    "包括", "别忘", "月光", "连动", "健康", "知道儿",
    "简直", "听言", "充满", "双手", "上去", "幸福", "呼吸",
    "勾起", "越来", "公司", "成为", "薄唇", "尤其", "家里", "别人", "莫名",
    # 泛指 / 集合 / 家族
    "时间", "任何", "所有人", "大家", "众人", "某人", "无人", "许多人", "南家",
    # 家族/门派/地名（斗破类玄幻高频，靠「X家/X宗」出现在图谱但非具体角色）
    "家族", "萧家", "萧族", "云岚宗", "乌坦",
    # 言说/情态副词（斗破 30 章实测高频误判）
    "闻言", "闻言顿", "高级", "满是", "容易", "平淡", "强化", "成年",
    "苍老", "安静", "平静", "双眼", "周围", "后面", "人流", "强行", "平日",
    # 抽象名词 / 连词
    "关系", "安全", "能够", "毕竟",
})


def _is_fake_character(name: str) -> bool:
    """占位符 / 高频虚词 / 非人名判定。"""
    name = (name or "").strip()
    if not name or len(name) < 2:
        return True
    if _PLACEHOLDER_RE.match(name):
        return True
    return name in _FAKE_CHARS


#: 纯中文串（姓名扩展时用来排除含标点/字母的候选）
_CJK_RE = re.compile(r"[\u4e00-\u9fa5]+")


def _resolve_name_artifacts(
    pairs: list[tuple[str, int]], text: str, frac: float = 0.15
) -> dict[str, str | None]:
    """用语料词频的**包含关系**识别分词截断产生的伪角色名。

    不依赖词表，语料自证。对每对 A ⊂ B（A 更短）：
    - A 几乎只作为 B 的子串出现（独立出现 ≤ frac）→ A 是截断，归一到 B
      「宫爵」(9984) ⊂ 「南宫爵」(9982)：独立出现 2 次 → 宫爵 ≡ 南宫爵
      「小落」(12937) ⊂ 「安小落」(12145)：独立 792 次(6%) → 小落 → 安小落
    - B 频次远低于 A（≤ frac × A）→ B 是过度切分（名字+相邻字），丢弃
      「冷月清」(5) ⊃ 「冷月」(603) → 丢弃「冷月清」

    返回 {名字: 归一目标名 or None}；None 表示丢弃。未涉及的短名/长名不是 dict 的 key。
    """
    if not text:
        return {}
    counts = {n: text.count(n) for n, _ in pairs if n}
    names = sorted(counts, key=len, reverse=True)
    resolved: dict[str, str | None] = {}
    for long_name in names:
        n_long = counts[long_name]
        if n_long == 0:
            continue
        # 包含在长名里的短名，按频次降序（主导者优先）
        contained = sorted(
            ((counts[s], s) for s in names
             if len(s) < len(long_name) and s in long_name and counts[s] > 0),
            reverse=True,
        )
        for n_short, short_name in contained:
            # 短名独立出现比例过低 → 截断产物，归一到长名
            if n_short - n_long <= frac * n_short:
                resolved.setdefault(short_name, long_name)
            # 长名几乎只由短名支撑 → 过度切分产物（名字+相邻字），归回短名
            if n_long <= frac * n_short:
                resolved.setdefault(long_name, short_name)
    return resolved


#: 不能延续人名的字符：功能字 / 高频动词字 / 标点。
#: 用于「名字+1字」扩展判定——只有排除这些字之后剩下的高频后继字，
#: 才可能是真实姓名的后半截；否则「安小落」+「的」会被误判成名字。
#:
#: 2026-10-19 调整：从集合里移出「年」和「日」「月」。
#: 原集合把「年月日分秒」整组当时间字排除，但「年」常是真名末字
#: （雪中悍刀行：徐凤年、徐龙象；实践里「徐凤」+「年」占 100%）——
#: 排除它会让最该扩展的名字永远扩不出来。「日月」同理（李淳罡的
#: 对手「邓太阿」不受影响，但「日月」在武侠/仙侠里是常见名字用字），
#: 保留「时分秒」作时间量词足够挡住「三点」「五分」这类误判。
_NAME_CONT_STOP = frozenset(
    "的了是在和就都也不有我你他她它这那一上下里外说着过要会能得地吗呢吧啊呀哦嗯"
    "把被让给对从向与及或者而但因所以然后于其为之又再还只很更最没无到起来去"
    "们个件事点心天时月分秒多少大小老新好，。！？、；：“”‘’（）《》…—~ "
    "0123456789abcdefghijklmnopqrstuvwxyz"
    + chr(32) + chr(9) + chr(10) + chr(13)
)


def _extend_name_candidates(
    names: list[str], text: str, frac: float = 0.15
) -> dict[str, str]:
    """找回被截断的真实姓名：N 的高频后继构成全名 N+c。

    语料自证，不需要词表：
    - 「莫泽」共 1800 次，其中 1781 次其实是「莫泽修」，独立出现仅 1.1% → 截断
    - 「安小落」共 12145 次，后继多为标点/功能字（安小落，/安小落的），
      独立出现 94% → 不是截断，保持原样

    返回 {短名: 全名}。

    ## 与 `_expand_names_from_corpus` 的参数语义差异（重要）

    本函数的 `frac` 是**「允许独立出现的比例」**（越小越严格）：
    `(total - best_count) <= frac * total` 等价于「后继占比 ≥ 1 - frac」。
    默认 0.15 → 要求后继占比 ≥ 85%。

    `_expand_names_from_corpus` 的 `frac` 是**「扩展率下限」**（越大越严格）。
    默认 0.5 → 要求后继占比 ≥ 50%。

    这里做语义转换，保持两个函数各自的调用方契约不变。
    """
    if not text:
        return {}
    # 语义转换：旧 frac（容忍的独立率）→ 新 frac（要求的扩展率）
    min_rate = max(0.0, 1.0 - frac)
    result = _expand_names_from_corpus(
        names, text, frac=min_rate, max_extra=1
    )
    return {short: new for short, (new, _rate) in result.items()}


def _expand_names_from_corpus(
    names: list[str], text: str, *, frac: float = 0.5, max_extra: int = 2,
) -> dict[str, tuple[str, float]]:
    """用**语料词频自证**把图谱里的截断名扩展回完整名。

    v2.234。解决图谱抽取器的硬伤：它在分词粒度上抽名字，于是
    「徐凤年」被抽成「徐凤」，「王仙芝」被抽成「王仙」，而完整名
    **从来不存在于图谱里**——`_resolve_name_artifacts` 的包含关系
    合并因此匹配不上（它要求两个名字都在图谱里）。

    ## 为什么改用「图谱节点作锚点 + 语料里向后扩展」

    试过直接从语料挖高频 3-4 字串：top 40 全是「徐凤年笑」「世子殿下」
    「一秒记住」（盗版站广告），噪声压过信号，不可用。

    而**以图谱节点为前缀**在语料里看后继，信号极干净（雪中悍刀行 512 万字实测）：
    - 徐凤 → 徐凤年 100%（19254/19256）
    - 王仙 → 王仙芝 100%（1258/1258）
    - 徐北 → 徐北枳 100%（930/930）
    - 陈芝 → 陈芝豹 99% / 邓太 → 邓太阿 100% / 李淳 → 李淳罡 100%
    - 顾剑 → 顾剑棠 99% / 徐偃 → 徐偃兵 100%

    同时它天然不误伤完整名：「安小落」的后继多是标点/功能字，扩展率上不去。

    ## 参数取舍

    `frac=0.5`：扩展出的名字必须占该节点语料出现次数的一半以上。
    定 0.5 而不是更松，是因为要为「名字恰好是另一个名字前缀」的情况留余量——
    「轩辕」→「轩辕青锋」实测 50%（另 25% 是「轩辕敬城」），
    正好卡在边界上；取 0.5 会选中更高频的那个，取 0.3 则会一次扩出两个不同的人。
    `max_extra=2`：中文姓名通常 2-4 字，节点已占 2 字，最多再补 2 字。

    ## 已知局限

    - 同一前缀指向多个真角色时（轩辕 → 青锋 / 敬城）只取最高频的那个，
      另一个仍是碎片。要彻底解决需要「前缀 + 后缀双向收敛」，成本高。
    - 地名/帮派也会被扩展（龙虎 → 龙虎山 87%、鱼龙 → 鱼龙帮 90%）。
      形态是对的（补齐了完整称谓），但语义上不是人。这类由 `_is_fake_character`
      黑名单 + LLM 抽取时返回空结果自然淘汰。

    Args:
        names: 图谱里的候选名（通常是 character 节点）。
        text: 语料全文。为空时返回 {}。
        frac: 扩展率下限。
        max_extra: 最多向后扩展几个字。

    Returns:
        {原名: (扩展后全名, 扩展率)}；未达阈值的名字不出现在结果里。
    """
    if not text or not names:
        return {}

    from collections import Counter

    result: dict[str, tuple[str, float]] = {}
    n_text = len(text)

    for name in names:
        n_name = len(name)
        if n_name < 2:
            continue

        total = 0
        # (扩展字数, 候选串) → 计数
        counters = {k: Counter() for k in range(1, max_extra + 1)}

        start = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            start = i + 1
            total += 1
            for k in range(1, max_extra + 1):
                j = i + n_name + k
                if j > n_text:
                    continue
                nxt_ch = text[i + n_name]
                if nxt_ch in _NAME_CONT_STOP:
                    continue
                cand = text[i:j]
                if not _CJK_RE.fullmatch(cand):
                    continue
                counters[k][cand] += 1

        if total < 20:   # 样本太少，扩展率不可信
            continue

        # 优先采纳更长（更完整）的扩展
        for k in range(max_extra, 0, -1):
            if not counters[k]:
                continue
            cand, cnt = counters[k].most_common(1)[0]
            rate = cnt / total
            if rate >= frac:
                result[name] = (cand, round(rate, 3))
                break

    return result


def _expand_names_backward(
    names: list[str], text: str, *, frac: float = 0.5, max_extra: int = 1,
) -> dict[str, tuple[str, float]]:
    """用**前驱字集中度**把尾部残片补回完整名。

    v2.239。`_expand_names_from_corpus` 只能向后补（徐凤 → 徐凤年），
    救不了**缺首字**的残片。雪中悍刀行真链路（v2.238）实测，LLM 直接
    输出了「凤年」当角色名——主角徐凤年以碎片形态混进了角色声音词典。

    ## 判据为什么是「前驱集中度」而不是别的

    probe 实测（雪中悍刀行 512 万字全本）：

    尾部残片的前驱高度集中：
        凤年 → 徐 99.5%（19353 次中 19254 次前面是「徐」）
        仙芝 → 王 100.0%   北枳 → 徐 99.6%   芝豹 → 陈 99.4%
        太阿 → 邓  93.5%   淳罡 → 李 100.0%  剑棠 → 顾 99.8%
        禄山 → 褚 99.4%    长卿 → 曹 99.5%

    完整名的前驱是全分散的（最高才 46.7%，且多是空格/标点）：
        徐凤年 46.7%（空格）  王仙芝 16.2%  李淳罡 12.2%
        陈芝豹 14.3%          徐骁 15.3%    姜泥 41.1%

    虚词也不会误扩：江湖 16.3% / 公子 11.7% / 师父 16.5%。

    50% 阈值把两组分得很开（残片最低 93.5%，完整名最高 46.7%）。

    ## 与向前扩展的配合

    两个方向互为补充，调用方先试向前（补尾字），失败再试向后（补首字）。
    像「陈芝 → 陈芝豹」这种中段残片两个方向都能补，向前优先（更常见）。

    Args:
        names: 候选名（通常来自 `_build_character_name_index` 的 pool）。
        text: 参考语料全文。
        frac: 扩展出的全名必须占该名出现次数的比例下限。
        max_extra: 最多向前补几个字。中文姓氏 1 字，复姓 2 字。

    Returns:
        {原名: (补全后的名字, 集中度)}；未达阈值的不出现在结果里。
    """
    if not text or not names:
        return {}

    from collections import Counter

    result: dict[str, tuple[str, float]] = {}
    n_text = len(text)

    for name in names:
        if len(name) < 2:
            continue

        total = 0
        counters = {k: Counter() for k in range(1, max_extra + 1)}

        start = 0
        while True:
            i = text.find(name, start)
            if i < 0:
                break
            start = i + 1
            total += 1
            for k in range(1, max_extra + 1):
                j = i - k
                if j < 0:
                    continue
                prev_ch = text[j]
                if prev_ch in _NAME_CONT_STOP:
                    continue
                cand = text[j:i + len(name)]
                if not _CJK_RE.fullmatch(cand):
                    continue
                counters[k][cand] += 1

        if total < 20:      # 样本太少，集中度不可信
            continue

        # 优先采纳更长（复姓）的扩展
        for k in range(max_extra, 0, -1):
            if not counters[k]:
                continue
            cand, cnt = counters[k].most_common(1)[0]
            rate = cnt / total
            if rate >= frac:
                result[name] = (cand, round(rate, 3))
                break

    return result


def _enhance_names_with_corpus(
    names: list[str], text: str, *, frac: float = 0.85, max_extra: int = 1,
) -> dict[str, str]:
    """把图谱候选名里所有能补全的残片统一补全（向前失败则向后）。

    v2.239。原有 `_extend_name_candidates` 只做向后补，缺首字的残片
    （「凤年」→「徐凤年」）永远救不回来。本函数是统一入口：

    1. 先跑 `_expand_names_from_corpus`（补尾字，「徐凤」→「徐凤年」）
    2. 对没补上的，跑 `_expand_names_backward`（补首字，「凤年」→「徐凤年」）

    两步都失败的名字不出现在结果里（保持原样）。

    ## 为什么默认 frac 是 0.85 而不是 0.5

    `_extend_name_candidates` 的老契约是 `frac=0.15` 表示「**允许**的独立
    出现率」，等价于要求扩展率 >= 85%。这个阈值不是随便定的：

    测试语料里「莫雨道」出现 30 次、「莫雨蝶」20 次，`莫雨` 共 50 次。
    若阈值取 0.5，「莫雨道莫」正好 60% 过线 → 把一个真简称错误扩展成
    「莫雨道莫」。0.85 则要求扩展词压倒性主导，才认它是残片。

    雪中悍刀行实测残片扩展率 93.5%~100%，0.85 仍然够用。

    Args:
        names: 候选名列表。
        text: 语料全文。
        frac: 扩展率 / 集中度下限（默认 0.85，与老 `_extend_name_candidates` 对齐）。
        max_extra: 最多补几个字。1 字可覆盖绝大多数中文姓名。

    Returns:
        {残片: 完整名}。
    """
    if not text or not names:
        return {}
    forward = _expand_names_from_corpus(names, text, frac=frac, max_extra=max_extra)
    out: dict[str, str] = {n: full for n, (full, _r) in forward.items()}

    remaining = [n for n in names if n not in out]
    if remaining:
        backward = _expand_names_backward(remaining, text, frac=frac, max_extra=1)
        for n, (full, _r) in backward.items():
            out[n] = full
    return out


def _build_character_name_index(
    db, project, nid: int, corpus_text: str = "", min_mentions: int = 10,
    cleaning: str = "full",
) -> tuple[list[tuple[str, int]], dict[str, str]]:
    """构建规范角色名索引，返回 (canonical, alias)。

    - canonical: [(规范名, 提及次数)] 按提及次数降序，已剔除伪角色
    - alias: {简称: 规范名}，分词截断的归一映射（「小落」→「安小落」）

    图谱的 character 节点很脏：513 个里既有「时间/任何/相信」这类高频虚词，
    也有「小落/安小/南宫/宫爵」这类分词截断。不清洗的话，角色声音提取会把
    提名额全浪费在伪角色上，LLM 输出的简称也无法归并到全名。

    Args:
        cleaning: 清洗强度，对应档位配置。
            - ``"full"``：虚词黑名单 + 频率自证（`_resolve_name_artifacts` /
              `_extend_name_candidates`）。完整档用。
            - ``"partial"``：只过虚词黑名单，不做频率自证。平衡档用——
              简称仍有噪声，但不会把名额浪费在「时间/相信」上。
            - ``"none"``：不清洗，只按提及次数排序取前N。快速档用。
    """
    try:
        from app.models import KgNode

        rows = db.scalars(
            select(KgNode).where(
                KgNode.project_id == project.id,
                KgNode.type == "character",
                KgNode.source == f"novel:{nid}",
            )
        ).all()
    except Exception:  # noqa: BLE001
        return [], {}

    raw: list[tuple[str, int, bool]] = []
    for r in rows:
        props = r.props or {}
        name = props.get("name")
        if not name or len(name) < 2:
            continue
        raw.append((name, props.get("mentions") or 0, props.get("speaking") is True))

    # 候选池判据（v2.229）：mentions 排序 + 黑名单，不再用 speaking 布尔值。
    #
    # ## 为什么废弃 speaking
    #
    # `speaking` 是「至少命中一次『名字+言说动词』」的一次性布尔值
    # （kg/extractor.py::_SPEAK_RE，要求名字与动词**紧邻**）。
    # 斗破 30 章实测：77 个 character 节点里只有 4 个 speaking=True，
    # 其中 2 个是伪角色（「满脸冷笑道」「冷笑一声」），而真角色
    # 萧薰儿/萧媚/萧宁/纳兰/萧家 全是 False —— 因为网文写法是
    # 「萧战笑道」之外还有「萧战叹了一口气，沉默了片刻，忽然道：」，
    # 动词与名字隔着状语，正则不命中。
    #
    # 叠加 `mentions >= 200` 兜底后，候选池只剩 5 个（斗破前 30 章
    # 只有萧炎 1265 次过线），二番位萧战 159 / 纳兰 139 / 萧宁 68 全落空
    # → 角色声音维度只产出 2 条。
    #
    # ## 为什么放弃「言说邻接率」判据
    #
    # 试过窗口式邻接率（前后 N 字内出现言说动词/引号），probe 实测
    # 在任何窗口宽度下都不可分：伪角色「冷笑」0.929、「萧族」0.400~0.900、
    # 「闻言」0.529，反而高于真角色「萧宁」0.077~0.128。
    # 原因是这些词本身就常出现在言说动词旁边，而真角色的高频出现是
    # 「萧炎的」「萧炎心头」这类所有格/主语位。
    #
    # ## 现在的判据
    #
    # 纯 mentions 排序 + `_FAKE_CHARS` 黑名单 + 子串归一（_resolve_name_artifacts）。
    # probe 实测斗破 30 章：取前 30 名覆盖 7/7 真角色（萧炎/萧战/纳兰/
    # 萧宁/萧媚/葛叶/萧薰儿），伪角色（家族/萧家/高级/闻言/乌坦/双眼…）
    # 由黑名单与后续 LLM 抽取时的空结果自然淘汰。
    #
    # 代价：候选池会含少量非人名词（谷尼/云岚宗 是地名）。这是可接受的——
    # 角色声音提取对每个候选都要跑一次 LLM，LLM 对非人名词会返回空，
    # 不产出规则；真角色的声音不会被挤掉（MAX_VOICE_CHARACTERS=30 够装）。
    # 相比「漏掉女主/二番位」这种不可接受的信息损失，多花几次 LLM 调用是
    # 划算的取舍。
    candidates = [(n, m) for n, m, _ in raw if m >= min_mentions]

    if cleaning == "none":
        # 快速档：不清洗。伪角色会进候选，靠后续 MAX_VOICE_CHARACTERS 截断兜底。
        return sorted(candidates, key=lambda p: -p[1]), {}

    pool = [(n, m) for n, m in candidates if not _is_fake_character(n)]
    if cleaning == "partial":
        # 平衡档：只过虚词黑名单，不做频率自证（不做子串归一/后缀扩展）。
        return sorted(pool, key=lambda p: -p[1]), {}

    resolved = _resolve_name_artifacts(pool, corpus_text)
    # 语料自证补全：向前补尾字（徐凤 → 徐凤年）优先，
    # 失败的再向后补首字（凤年 → 徐凤年）。v2.239 加后向——
    # v2.238 真链路实测 LLM 直接输出了「凤年」，主角以碎片形态混进角色声音。
    extended = _enhance_names_with_corpus([n for n, _ in pool], corpus_text)

    # 合并归一映射。优先级：子串判据 > 扩展判据。
    # 子串判据说「这个短名只由更长名字支撑」是更硬的证据，可判「丢弃」；
    # 已经判丢的（如「夏诺柔」⊂「夏诺」）不能再被扩展成「夏诺柔声」这种假名。
    alias: dict[str, str | None] = dict(resolved)
    for name, full in extended.items():
        if name not in alias:
            alias[name] = full

    # 规范名 = 短名替换为全名，丢弃被判为过度切分的名字；
    # 提及次数取组内最大（全名继承了短名的热度，排序才合理）
    merged: dict[str, int] = {}
    for name, mentions in pool:
        target = alias.get(name, name)
        if target is None:
            continue
        merged[target] = max(merged.get(target, 0), mentions)
    kept = sorted(merged.items(), key=lambda p: -p[1])
    return kept, {k: v for k, v in alias.items() if v}


def _normalize_character_name(
    name: str,
    canonical: list[tuple[str, int]] | None = None,
    alias: dict[str, str] | None = None,
) -> str | None:
    """归一化角色名：过滤占位符/伪角色，并把简称映射到规范全名。

    - 占位符/高频虚词（角色A、具体角色名、相信、时间）→ None
    - 「小落」→「安小落」、「宫爵」→「南宫爵」
      优先用 alias（语料词频自证的截断映射），其次退回子串匹配
    """
    name = (name or "").strip()
    if _is_fake_character(name):
        return None

    # 1) 词频自证的截断映射（最可靠）
    if alias and name in alias:
        return alias[name]

    # 2) 已是规范名 → 原样返回
    if canonical:
        if name in {cn for cn, _ in canonical}:
            return name
        # 3) 退回子串匹配：后缀优先（中文常取后几字作简称），再取最短
        candidates = [
            cn for cn, _ in canonical
            if len(cn) > len(name) and name in cn
        ]
        if candidates:
            suffix = [c for c in candidates if c.endswith(name)]
            return min(suffix or candidates, key=len)
    return name


def _is_valid_voice_char_name(char_name: str) -> bool:
    """判断「角色声音」条目里冒号前那段能不能当角色名。

    v2.237。修两类 LLM 输出污染：

    ## 1. 规则文本被当成角色名

    LLM 偶尔把 extract 输出里**另一维度的规则**塞进「角色声音词典」数组。
    `_build_voice_rule`/`_dedup_character_voices` 用 `rule.split("：")[0]`
    取前半段当名字，于是拿到：

        通过角色的语言和行动，展现其个性特点（证据

    这类名字含「（证据」，长度超 20 字，一眼不是人名。

    ## 2. 称谓/虚词

    江湖/公子/师父/皇帝/家伙/许多/高手/广陵/春秋/武当/王爷/白衣/西楚/可惜/后者
    —— 雪中悍刀行 990 章实测，29 条角色声音里 12 条是这类。
    它们能通过 `_is_fake_character`（黑名单只覆盖了一部分），但在角色声音
    维度必须更严：角色名应当像个人名。

    ## 判据（宁可漏，不可放垃圾进提示词）

    - 长度 2-4 字（中文姓名常态）
    - 纯中文
    - 不含标点/括号/「证据」/「未提及」等结构化标记
    - 不在 `_FAKE_CHARS` 黑名单
    - 不在 `_VOICE_STOP_WORDS`（角色声音专用的称谓/泛称表）

    Args:
        char_name: 从规则里切出来的候选角色名。

    Returns:
        True 表示可以当作角色名保留。
    """
    name = (char_name or "").strip()
    if not name:
        return False
    # 长度：中文姓名 2-4 字。超过基本是句子片段。
    if not (2 <= len(name) <= 4):
        return False
    # 必须纯中文
    if not _CJK_RE.fullmatch(name):
        return False
    # 结构化标记（LLM 把规则文本塞进来时的特征）
    if any(tok in name for tok in ("证据", "未提及", "规则", "维度", "：")):
        return False
    if _is_fake_character(name):
        return False
    # v2.264：带修饰前缀的具体称呼（老和尚/老夫子/洪姨）是真角色，
    # 不能因为词根是泛称就拦掉。见 `_is_specific_title` 的说明。
    if _is_specific_title(name):
        return True
    return name not in _VOICE_STOP_WORDS


def _is_specific_title(name: str) -> bool:
    """判断是不是「带修饰前缀的具体称呼」——在本书里当唯一指代用。

    ## 为什么需要

    v2.237 建立的 `_VOICE_STOP_WORDS` 把「和尚」「儒生」这类泛称收进去是
    对的，但**顺带把「老和尚」「老夫子」这类具体称呼也拦掉了**。它们在
    原文里是真角色——作者整本书就这么称呼，从不具名。

    雪中悍刀行 v2.261 真链路实测，这几条的证据**都包含名字本身**：

        老和尚：口头禅「老衲」语气深沉
                （证据：老和尚轻声道："问佛不如问己。"）
        老夫子：语言文雅，富有哲理
                （证据：老夫子笑了笑，叮嘱着说...）
        老儒生：口头禅「少朴」，语气严肃
                （证据：老儒生笑眯眯道："少朴，喝一杯？"）

    ## 判据

    **修饰前缀 + 称谓词根**：

    - 前缀：老 / 小 / 大 / 阿 / 二 / 三 / 四 / 五
    - 词根：收在 `_SPECIFIC_TITLE_ROOTS`（和尚/道士/儒生/夫子/姨/叔/铁匠…）

    「老和尚」≠「和尚」：前者指本书里那唯一一个老和尚，后者泛指任意僧人。

    ## 不覆盖的

    「老人」「老者」「后来」仍是泛称/虚词——不在 `_SPECIFIC_TITLE_ROOTS`
    里（「人」「者」刻意不列），所以继续被 `_VOICE_STOP_WORDS` 拦截。

    Args:
        name: 候选角色名。

    Returns:
        True 表示这是具体化称呼，可以当角色名保留。
    """
    if len(name) < 2:
        return False
    # 形态 1：修饰前缀 + 称谓词根（老和尚 / 小和尚 / 大小姐 / 老掌柜）
    if len(name) >= 3 and name[0] in _SPECIFIC_TITLE_PREFIXES:
        if name[1:] in _SPECIFIC_TITLE_ROOTS:
            return True
    # 形态 2：姓氏 + 称谓（洪姨 / 王叔 / 李掌柜）。
    # 首字必须是姓氏白名单里的字——否则「公子」「王爷」会被误判
    # （「子」「爷」做末字，但它们不是姓氏）。
    if len(name) == 2 and name[-1] in _SPECIFIC_TITLE_ROOTS:
        return name[0] in _SURNAME_LIKE
    # 形态 3：职业名词直接当称呼（铁匠 / 屠夫 / 船夫）。
    # 这类词在网文里常指「本书里那个铁匠」，是唯一指代。
    # 但「和尚」「道士」这类**宗教/身份泛称**不在此列——它们永远是泛称，
    # 所以单列一份职业表，不放宽整个 _SPECIFIC_TITLE_ROOTS。
    return name in _OCCUPATION_TITLES


#: 「具体化称谓」——带修饰前缀、在本书里当唯一指代用的称呼。
#:
#: v2.264。之前把「和尚」「老者」这类泛称放进 `_VOICE_STOP_WORDS` 是对的，
#: 但**连「老和尚」「老夫子」「老儒生」也一起拦掉了**——那些在原文里是
#: 真角色，作者整本书就这么称呼，从不具名。
#:
#: 雪中悍刀行实测（v2.261 真链路）：
#:
#:     老和尚：口头禅「老衲」语气深沉（证据：老和尚轻声道："问佛不如问己。"）
#:     老夫子：语言文雅，富有哲理（证据：老夫子笑了笑，叮嘱着说...）
#:     老儒生：口头禅「少朴」，语气严肃（证据：老儒生笑眯眯道："少朴，喝一杯？"）
#:     洪姨  ：口头禅丰富，语气平和（证据：洪姨嗑着瓜子...）
#:
#: 四条证据**都包含名字本身**，说明抽取准确——是我们的过滤过度了。
#:
#: 判据：**泛称 + 修饰前缀**（老/小/大/阿/二）后成为唯一指代。
#: 「老和尚」≠「和尚」：前者在本书里只有一个人，后者泛指任意僧人。
#:
#: 注意：不是所有「老+X」都算。「老人」「老者」仍是泛称（前面常接数量
#: 词「一位老人」），所以它们继续留在 `_VOICE_STOP_WORDS` 里。
_SPECIFIC_TITLE_PREFIXES = ("老", "小", "大", "阿", "二", "三", "四", "五")

#: 允许带前缀具体化的称谓词根。
#:
#: 只列那些**加上前缀后在网文里确实会成为角色代称**的：
#: 老和尚 / 老道士 / 老儒生 / 小和尚 / 大小姐 / 阿婆 …
#: 不列「人」「者」（老人/老者 仍是泛称）。
_SPECIFIC_TITLE_ROOTS = frozenset({
    # 复数词根（老和尚 / 老道士 / 老儒生 / 老夫子 / 大小姐）
    "和尚", "道士", "道长", "尼姑", "儒生", "夫子", "先生", "小姐", "少爷",
    # 亲属称谓（洪姨 / 王叔 / 李伯）——这些字做末字时几乎不可能是泛称
    "姨", "叔", "伯", "婆", "婶",
    # 职业（铁匠 / 屠夫 / 掌柜）——网文里常指「本书那个铁匠」
    "掌柜", "账房", "铁匠", "木匠", "屠夫", "船夫", "车夫", "渔夫",
})

#: 可以直接当角色称呼的**职业名词**。
#:
#: 与 `_SPECIFIC_TITLE_ROOTS` 的区别：那是「加前缀才成为具体称呼」的词根
#: （老和尚），这是「本身就能唯一指代」的职业（铁匠）。
#: `和尚`/`道士` 刻意不在表里——它们永远是泛称，指任意僧人/道人。
_OCCUPATION_TITLES = frozenset({
    "铁匠", "木匠", "屠夫", "船夫", "车夫", "渔夫", "掌柜", "账房",
    "更夫", "马夫", "伙夫", "轿夫",
})

#: 「形态 2」的姓氏白名单——只有这些字开头才算「姓+称谓」。
#:
#: 为什么要白名单：`公子`末字是「子」、`王爷`末字是「爷」，
#: 若只看末字会被误判成「姓氏+称谓」。用阴性表排除泛称首字。
_SURNAME_LIKE = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双"
    "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
    "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空"
    # v2.267：雪中悍刀行真链路实测漏掉的姓氏——晋兰亭 / 种神通 / 喜意
    # 这些名字被送进 LLM 审计后误标成「普通名词」。
    "晋种喜赫轩澹令皇甫尉迟长孙宇文司徒端木独孤"
    # 其他常见但容易漏的复姓首字（配合 _COMPOUND_SURNAMES 使用）
    "万俟闻人呼延拓跋完颜耶律纳兰申屠太史仲孙钟离闾丘"
)

#: 复姓（网文里极常见，姓氏白名单只收单字会漏掉）。
#:
#: 雪中悍刀行实测：轩辕 / 赫连 / 澹台 / 公孙 / 令狐 都是真角色，
#: 但因为首字不在单字姓氏表里，被廉价预过滤当成可疑送审，
#: 而 glm-4-flash 又把它们标成「普通名词」。
_COMPOUND_SURNAMES = frozenset({
    "轩辕", "赫连", "澹台", "公孙", "令狐", "慕容", "欧阳", "上官", "司马",
    "诸葛", "东方", "皇甫", "尉迟", "长孙", "宇文", "司徒", "端木", "独孤",
    "南宫", "西门", "夏侯", "万俟", "闻人", "呼延", "赫连", "拓跋", "宇文",
    "完颜", "耶律", "爱新", "纳兰", "申屠", "太史", "仲孙", "钟离", "闾丘",
})

#: 角色声音维度专用的非人名表。
#:
#: 与 `_FAKE_CHARS` 的区别：这个表专门收「在叙事里高频出现、形态像名字、
#: 但语义上是称谓/地名/泛称」的词。它们在别处（如场景维度）可能有用，
#: 但绝不能当角色名去提取声音。
#:
#: **只收纯泛称**——带修饰前缀的具体称呼（老和尚/老夫子）会被
#: `_is_specific_title` 放行，不该在这里。
#:
#: 来源：雪中悍刀行 990 章实测，29 条角色声音里混进的 12 条垃圾 + 同类扩充。
_VOICE_STOP_WORDS = frozenset({
    # 称谓
    "公子", "师父", "师傅", "皇帝", "王爷", "殿下", "世子", "将军", "先生",
    "夫人", "小姐", "姑娘", "老爷", "少爷", "夫人", "陛下", "大人", "前辈",
    "姑娘", "掌柜", "老丈", "少年", "少女", "老者", "和尚", "道士", "道长",
    "儒生", "夫子", "书生", "秀才", "尼姑", "婆婆", "大爷", "大娘",
    # 泛称 / 集合
    "家伙", "高手", "众人", "所有人", "此人", "那人", "后者", "前者", "对方",
    "众人", "旁人", "别人", "某人", "有人", "无人",
    # 地名 / 门派 / 朝代
    "江湖", "广陵", "春秋", "武当", "西楚", "北凉", "北莽", "离阳", "太安",
    "龙虎", "武帝", "徽山", "上阴", "昆仑", "西域", "中原", "南唐",
    # 情绪 / 副词（常被误认为人名）
    "可惜", "可怜", "无奈", "竟然", "居然", "果然", "忽然", "自然", "当然",
    "许多", "不少", "很多", "若干", "一些", "多少", "如此", "这般", "那样",
    "白衣", "青衣", "黑袍", "紫衣", "黄袍",
    # v2.264：雪中悍刀行真链路实测漏网的高频虚词（图谱里排前 150 位，
    # 因此会进候选池，但语义上是时间副词 / 方位词 / 泛称）
    "后来", "此前", "此时", "当时", "此时此", "片刻", "良久", "须臾",
    "南北", "东西", "左右", "前后", "上下", "内外", "远近", "高低",
    "老人", "老者", "老小", "大小", "小小",
})


#: 角色声音规则的「模板骨架」词。
#:
#: 这些词是 prompt 指定的输出格式（自称/口头禅/句长/语气/节奏/关系动态/
#: 代表台词），本身不含任何角色信息。LLM 什么都没提取到时会把它们填满
#: 「未提及」，产出一条 60 字的空壳规则：
#:
#:   姜泥： 自称「未提及」 口头禅「未提及」 句长混合 语气未提及
#:         节奏未提及 关系动态：未提及具体关系动态 代表台词：「未提及」
#:
#: 删掉「未提及」三个字后骨架还剩 40+ 字，光看长度判不出是空壳，
#: 必须把骨架词也一起剔除，剩下的才算实质内容。
_VOICE_TEMPLATE_TOKENS = (
    "未提及", "具体关系动态", "关系动态", "代表台词", "口头禅",
    "自称", "句长混合", "句长短句", "句长长句", "句长",
    "语气", "节奏", "混合", "停顿", "抢话", "欲言又止",
)


def _dedup_character_voices(
    voices: list[dict],
    canonical: list[tuple[str, int]] | None = None,
    alias: dict[str, str] | None = None,
    clean_fake: bool = True,
) -> list[dict]:
    """角色声音去重 + 角色名归一化。

    处理两类问题：
    1. 占位符/伪角色（角色A、时间、相信）→ 丢弃
    2. 同一角色的不同称呼（安小落/小落、南宫爵/宫爵）→ 合并到规范全名
       （alias 给语料自证的截断映射，canonical 给规范名列表）

    Args:
        clean_fake: 是否过滤伪角色/占位符。快速档（伪角色清洗强度 = none）
            传False —— 该档本就接受伪角色混入，省掉这层过滤的收益
            （规则更少）与成本不成正比，但也**不会**把「角色A」这种
            prompt 示例照抄当成真角色。
            canonical/alias 传空时，本函数退化为「只按同名字段合并」。
    """
    # 第一轮：过滤占位符 + 简称归一化 + 角色名合法性
    valid = []
    for v in voices:
        rule = v.get("rule", "")
        # 提取角色名（格式：「角色名：...」）
        if "：" not in rule:
            continue
        char_name = rule.split("：")[0].strip()
        # v2.237：角色名合法性校验。防两类污染——
        # (a) LLM 把另一维度的规则文本塞进角色声音数组，切出来是
        #     「通过角色的语言和行动，展现其个性特点（证据」这种句子片段；
        # (b) 称谓/地名/泛称（江湖/公子/师父/皇帝/家伙/许多/高手…）被当成人名。
        # 校验放在归一化之前：非法名字不必再走 alias 映射。
        if not _is_valid_voice_char_name(char_name):
            continue
        # v2.237：整条全是「未提及」占位符的直接丢弃。
        # 雪中悍刀行实测，姜泥/许多/高手/曹长卿 四条都是这种空壳：
        #   自称「未提及」 口头禅「未提及」 句长混合 语气未提及 节奏未提及
        #   关系动态：未提及 代表台词：「未提及」
        # 它对生成毫无信息量，却占掉 MAX_VOICE_CHARACTERS 的名额。
        #
        # 判据不能只看「未提及」出现次数：删掉这三个字后模板骨架
        # （自称「」口头禅「」句长混合语气节奏…）还剩 40+ 字，长度判不出空壳。
        # 改用「实质内容占比」：模板词全部剔除后，剩余内容太短即为空壳。
        body = rule.split("：", 1)[1] if "：" in rule else ""
        stripped = body
        for tpl in _VOICE_TEMPLATE_TOKENS:
            stripped = stripped.replace(tpl, "")
        stripped = stripped.strip(" ，、。：\n\t「」【】()（）[]")
        if len(stripped) < 10:
            continue
        if clean_fake:
            normalized = _normalize_character_name(char_name, canonical, alias)
            if normalized is None:
                continue
        else:
            normalized = char_name
        # 替换归一化后的名字
        v = dict(v)
        v["rule"] = normalized + "：" + rule.split("：", 1)[1]
        valid.append(v)

    # 第二轮：合并同一角色的不同称呼
    # 先按角色名分组（同名保留信息最丰富的）
    by_name: dict[str, dict] = {}
    for v in valid:
        rule = v.get("rule", "")
        char_name = rule.split("：")[0].strip()
        if char_name in by_name:
            if len(rule) > len(by_name[char_name].get("rule", "")):
                by_name[char_name] = v
        else:
            by_name[char_name] = v
    # 再做子串合并：如果 A ⊂ B，合并到 B
    # 按名字长度降序处理，确保长名字先入
    merged: dict[str, dict] = {}
    for char_name in sorted(by_name.keys(), key=len, reverse=True):
        v = by_name[char_name]
        # 检查是否已被更长名字包含
        canonical = char_name
        for existing in merged.keys():
            if char_name != existing and char_name in existing:
                canonical = existing
                break
        if canonical in merged:
            # 保留信息更丰富的
            existing_rule = merged[canonical].get("rule", "")
            new_rule = v.get("rule", "")
            if len(new_rule) > len(existing_rule):
                merged[canonical] = v
        else:
            merged[canonical] = v

    # 第三轮（v2.237）：剔除「同一份声音内容被复制给多个人名」的条目。
    #
    # LLM 出长 JSON 时会发生复制粘贴错误：雪中悍刀行实测，
    # 李淳罡 / 武当 / 西楚 / 可惜 四条规则的正文**完全相同**，
    # 内容却是褚禄山的台词（「我褚禄山有洁癖…」）。这种条目留着会
    # 让生成时把褚禄山的说话习惯安到李淳罡头上。
    #
    # 判据：规则的「正文部分」（冒号之后，去掉人名）完全相同的多条里，
    # 只保留第一条（顺序来自 valid，即 LLM 原始输出顺序，通常第一条是
    # 正确归属的那条）。
    seen_bodies: set[str] = set()
    deduped: list[dict] = []
    for v in merged.values():
        rule = v.get("rule", "")
        body = rule.split("：", 1)[1].strip() if "：" in rule else rule.strip()
        # 只对足够长的正文做这个判断：短正文（如「语气冷淡」）撞车是正常的
        if len(body) >= 40:
            if body in seen_bodies:
                continue
            seen_bodies.add(body)
        deduped.append(v)
    return deduped


#: 角色名审计的 system prompt（v2.265）。
#:
#: 关键设计：只问「能不能用来指人」，**不问「是不是本书的角色」**。
#: 后者需要知道这本书，小模型没这个知识；前者是纯语言常识，它能判断。
#:
#: 早期版本问「像不像本书角色」，glm-4-flash 把徐骁（主角之父）、姜泥
#: （女主）都标成可疑——理由是「普通名词，可能指人名的一部分」，自相矛盾。
#: 换成「能不能指人」+ 明确「不认识的但构词像人名的不要列」后，
#: 57 个名字里只标出 5 个（老人/红薯/洛阳/后来/南北），真角色零误伤。
_NAME_AUDIT_SYSTEM = (
    "你是中文语言助手。给你一批中文词语，判断哪些**不能用来指人**。\n\n"
    "不能指人的包括：\n"
    "- 时间副词：后来、此时、当时、片刻\n"
    "- 方位词：南北、东西、左右\n"
    "- 普通名词：老人、公子、家伙\n"
    "- 地名：洛阳、北凉、武当\n\n"
    "能指人的包括：\n"
    "- 任何「姓+名」结构（徐骁、姜泥、宋岩）——即使你不认识这些名字\n"
    "- 任何绰号、代号、称谓代称（老和尚、红薯、老宋、褚胖子）\n"
    "- 任何像人名的双字/三字词\n\n"
    "**重要**：不认识的词，只要构词像人名（姓+名 / 名+字），就**不要**列出来。\n"
    "只列你有把握不是人名的。\n\n"
    '输出 JSON：{"not_person": ["词1", "词2"]}'
)


def _audit_character_names(
    provider, voices: list[dict], *, batch_size: int = 60,
    max_names: int | None = None,
) -> dict:
    """用 LLM 粗筛角色名，**只标记不删除**（v2.265）。

    ## 为什么只标记不删

    这个审计是**启发式的**，会误判：雪中悍刀行实测，「红薯」（书里是丫鬟
    的名字）被标为「普通名词」。如果直接据以删除，就会丢掉真角色。

    标进 `voice_name_audit` 字段供人工/前端复核，是正确的用途：
    57 个名字里只标出 5 个，人工看一眼就能定夺，比逐条审 57 个省事得多。

    ## 为什么问「能不能指人」而不是「是不是本书角色」

    后者需要知道这本书。glm-4-flash 记得主角徐凤年，但对徐骁（他爹）、
    姜泥（女主）记忆模糊 → 判成可疑。**把「不知道」误判成「不存在」**
    是小模型做常识判断的通病。

    「能不能用来指人」是纯语言常识，不需要书的知识——换这个问法后
    真角色零误伤。

    ## 全量覆盖，不静默截断（2026-11-12）

    旧实现取 `audit_candidates[:80]` 后把 `skipped` 置真——417 条声音只审了
    80 个名字。现在默认按 `batch_size` 分批审**全部候选**，`checked / batches /
    failed_batches` 如实回报；单批失败只记失败，不阻断其余批次。

    ## `max_names`：调用方显式限额（2026-11-15）

    `max_names=None`（生产调用）保持全量覆盖；显式传入正整数时按上限截断
    候选并把 `skipped` 置真——这是**调用方主动要求**的限额，不是静默截断。
    旧签名缺这个参数，导致按旧契约调用的一侧直接 `TypeError` 崩掉。

    Args:
        provider: LLM provider。
        voices: 角色声音规则列表（每项含 "rule" 字段）。
        batch_size: 每批送审多少个名字（控制单次 prompt 长度）。
        max_names: 送审候选上限；None = 全量。

    Returns:
        ``{"suspicious": [...], "checked": N, "candidates": N,
        "batches": N, "failed_batches": N, "skipped": bool}``。
        **调用失败时如实记录，不阻断蒸馏**。
    """
    names: list[str] = []
    seen: set[str] = set()
    for v in voices:
        rule = v.get("rule", "")
        if "：" not in rule:
            continue
        nm = rule.split("：")[0].strip()
        if nm and nm not in seen:
            seen.add(nm)
            names.append(nm)

    if len(names) < 3:
        return {"suspicious": [], "checked": len(names), "candidates": len(names),
                "batches": 0, "failed_batches": 0, "skipped": True}

    # v2.265：先做**廉价预过滤**，只把「可疑候选」送给 LLM。
    #
    # 为什么：glm-4-flash 对「徐骁」「姜泥」这类名字判断不稳定——同一批
    # 输入两次，一次全保留、一次标成「普通名词」。把明显是人名的送进去，
    # 只会制造噪声，浪费人工复核精力。
    #
    # 明显是人名的特征（满足任一即跳过 LLM）：
    # 1. 首字在百家姓白名单里（徐骁、姜泥、宋岩、曹嵬、冯岭）——中文人名
    #    最常见的形态就是姓+名，这条覆盖 90% 以上
    # 2. 已在具体化称谓表里（老和尚、洪姨、铁匠）
    #
    # 只有不满足这两条的才送审（后来、南北、老人、洛阳、红薯）。
    def _looks_like_person(n: str) -> bool:
        """廉价判断：构词像人名就跳过 LLM 审计。

        三条：单字姓开头 / 复姓开头 / 具体化称谓。
        """
        if not n:
            return False
        if n[0] in _SURNAME_LIKE:
            return True
        if len(n) >= 2 and n[:2] in _COMPOUND_SURNAMES:
            return True
        return _is_specific_title(n)

    audit_candidates = [n for n in names if not _looks_like_person(n)]

    if not audit_candidates:
        return {"suspicious": [], "checked": 0, "candidates": 0,
                "batches": 0, "failed_batches": 0, "skipped": False,
                "note": "全部名字都是姓+名或具体化称谓，无需 LLM 审计"}

    # 调用方显式限额（None = 全量）：截断候选并如实标 skipped。
    truncated = False
    if max_names is not None and max_names > 0 and len(audit_candidates) > max_names:
        audit_candidates = audit_candidates[:max_names]
        truncated = True

    step = max(1, batch_size)
    batches = [audit_candidates[i:i + step]
               for i in range(0, len(audit_candidates), step)]
    result = {"suspicious": [], "checked": 0, "candidates": len(audit_candidates),
              "batches": len(batches), "failed_batches": 0, "skipped": truncated}
    suspicious: list[str] = []

    import re as _re
    import json as _json
    for batch in batches:
        # `checked` 记**送审数**（含失败批次），失败另由 `failed_batches` 如实回报；
        # 这样「调用失败」的返回里 checked 不会假性归零。
        result["checked"] += len(batch)
        try:
            raw = provider.chat(_NAME_AUDIT_SYSTEM, "词语：\n" + "、".join(batch))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "角色名审计调用失败（不阻断，本批 %d 个名字）: %s", len(batch), exc
            )
            result["failed_batches"] += 1
            continue
        if not isinstance(raw, str):
            continue
        m = _re.search(r"\{.*\}", raw, _re.S)
        if not m:
            continue
        try:
            parsed = _json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            continue

        bad = parsed.get("not_person") if isinstance(parsed, dict) else None
        if not isinstance(bad, list):
            continue

        # 只接受在本次清单里的词（防 LLM 编造）
        suspicious.extend(b for b in bad if isinstance(b, str) and b in seen)

    result["suspicious"] = list(dict.fromkeys(suspicious))
    return result


def _build_beat_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造节拍序列提取的 prompt。

    提炼「一场典型冲突的 5~7 步节拍」——从套路名到具体写法。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**一场典型冲突/高潮的节拍序列**。\n\n"
        "不是总结剧情，而是提炼**作者怎么写一场冲突的结构**。例如：\n"
        "- 铺垫：用什么细节暗示冲突即将爆发\n"
        "- 升温：如何逐步加压（对话变短/动作变急/环境变压抑）\n"
        "- 爆发：冲突顶点如何处理（谁说最后一句话/什么动作打断）\n"
        "- 余波：爆发后如何收束（沉默/转身离去/一句冷话）\n"
        "- 新钩子：如何引出下一场冲突\n\n"
        "证据要求：每步单独给一个「证据」字段，必须是从**给定文本逐字摘抄的连续 20–60 字**；"
        "少于 20 字不作为证据，不能拼接两句，不能用省略号跳过中间内容。\n"
        "章号要求：每步还要给「章」字段——这件事发生在第几章，必须是标题「第X-Y章」范围内的整数。\n"
        "**禁止**把「证据」写成「原文引用」「原文连续 20–60 字」「略」这类占位说明，"
        "必须是真实存在的那句话。\n\n"
        "输出格式（JSON，示例仅示范结构，证据必须来自给定文本）：\n"
        '{\n'
        '  "节拍序列": [\n'
        '    {"步骤": "铺垫", "章": 11, "写法": "用环境细节先压一层不安，不点破", "证据": "楼道里的声控灯忽明忽暗，他敲了三下门，里面一直没动静"},\n'
        '    {"步骤": "升温", "章": 12, "写法": "对话变短，只留问句", "证据": "「谁？」他贴着门问。屋里安静了几秒，才传来椅子腿刮地板的声音。"},\n'
        '    {"步骤": "爆发", "章": 12, "写法": "用一个动作打断对话", "证据": "门忽然向内开了一条缝，一只手从缝隙里伸出来，抓向他的衣领。"},\n'
        '    {"步骤": "余波", "章": 13, "写法": "不写结果，写旁观者的沉默", "证据": "走廊里没人说话，只有那盏灯还在响，一明一暗地照着他空掉的手。"},\n'
        '    {"步骤": "新钩子", "章": 13, "写法": "用一句反常台词收尾", "证据": "「你终于来了。」门后的人说，「他们等你很久了。」"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼一场典型冲突的节拍序列：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。每步必须附原文证据。"
    )
    return system, user


def _build_scene_template_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造高频场景模板提取的 prompt。

    识别反复出现的场景类型，提炼环境细节、人物站位、典型动作。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**高频场景的写作模板**。\n\n"
        "识别文中反复出现的场景类型（如病房/宴会/办公室对峙/车内独处/手术室门口），"
        "每个场景提炼：\n"
        "1. 环境细节（光线/声音/气味/温度）\n"
        "2. 人物站位（谁站着/谁坐着/距离多远）\n"
        "3. 典型动作链（进入→对峙→动作→离场）\n"
        "4. 情绪基调（压抑/紧张/暧昧/冰冷）\n\n"
        "证据要求：每个场景必须有「证据」字段，是从**给定文本逐字摘抄的连续 20–60 字**；"
        "少于 20 字不作为证据，不能拼接两句，不能用省略号。\n"
        "**禁止**把「证据」写成「原文连续 20–60 字」「原文引用」「略」这类占位说明。\n\n"
        "输出格式（JSON，示例仅示范结构，证据必须来自给定文本）：\n"
        '{\n'
        '  "场景模板": [\n'
        '    {"场景": "病房", "环境": "消毒水味、走廊白光、监护仪滴答", "站位": "病人靠床，家属背对门站着", "动作链": "进门→压低声音→看仪器→退到窗边", "基调": "压抑", "证据": "消毒水的味道混着走廊的白光涌进来，监护仪的滴答声一下一下敲在墙上"},\n'
        '    {"场景": "宴会", "环境": "水晶灯、碰杯声、香水味", "站位": "主人居中，客人围成半圈", "动作链": "迎客→寒暄→敬酒→找角落", "基调": "热闹里带试探", "证据": "水晶灯下人影晃动，酒杯碰在一起的声音一阵接一阵，谁也听不清谁在说什么"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼高频场景模板：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。每个场景必须附原文中的具体细节。"
    )
    return system, user


def _build_subtext_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造潜台词模式提取的 prompt。

    提炼「角色如何用反话/转移话题/沉默表达真实意图」。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**对白潜台词的写法**。\n\n"
        "分析角色如何用间接方式表达真实意图：\n"
        "1. 反话/正话反说（说「没事」其实有事）\n"
        "2. 转移话题（用问另一个问题回避）\n"
        "3. 沉默/停顿（不说话比说话更有力）\n"
        "4. 借题发挥（借他人之事说自己）\n"
        "5. 命令式关心（用命令语气表达担心）\n\n"
        "证据要求：每个模式必须有「证据」字段，是从**给定文本逐字摘抄的连续 20–60 字**；"
        "少于 20 字不作为证据，不能拼接两句，不能用省略号。\n"
        "**禁止**把「证据」写成「原文连续 20–60 字」「原文引用」「略」这类占位说明。\n\n"
        "输出格式（JSON，示例仅示范结构，证据必须来自给定文本）：\n"
        '{\n'
        '  "潜台词模式": [\n'
        '    {"模式": "反话正说", "写法": "嘴上说没事，手上动作暴露", "证据": "「我挺好的。」她说着，把杯子放回桌上，杯底磕出一声轻响。"},\n'
        '    {"模式": "沉默施压", "写法": "不接话，只重复对方的问句", "证据": "「你真要这么做？」他没抬头，又慢慢问了一遍：「你真要这么做？」"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼对白潜台词模式：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。每个模式必须附原文证据。"
    )
    return system, user


def _build_kg_events_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造「每块 KG 事件」抽取 prompt（第 6 个调用，默认关闭）。

    为什么单独一次调用而不是塞进主提炼：主提炼输出的是「怎么写」的抽象规则，
    事件抽取输出的是「发生了什么」的事实清单，两者在提示词里互相稀释。
    做成开关（`with_kg_events`）是为了先拿主流程的数据，再决定是否默认开。

    字段与 `kg/build.py` 的 timeline 节点对齐：章号 + 事件 + 人物 + 地点 + 后果。
    """
    system = (
        "你是一位中文小说的事件抽取器。任务：从给定片段中抽出**剧情事件**，"
        "供故事线时间线使用。\n\n"
        "规则：\n"
        "1. 只抽**推动剧情的事实**（谁做了什么、结果如何），不抽氛围描写、心理活动、设定说明\n"
        "2. 每块最多 12 条，按时间顺序排列；同一件事不要拆成多条\n"
        "3. 「章」必须是标题「第X-Y章」范围内的整数\n"
        "4. 「人物」只写规范全名，不要写「他」「那名男子」这类代称\n"
        "5. 「证据」必须是从给定文本逐字摘抄的连续 20–60 字，禁止写占位说明\n"
        "6. 没有明显剧情事件的过渡块，返回空数组 `[]`，不要硬凑\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "事件": [\n'
        '    {"章": 12, "事件": "宋书航在楼道被伏击，对方抢走玉简", "人物": ["宋书航", "羽柔子"], "地点": "公寓楼道", "后果": "玉简丢失，主线目标转为追查", "证据": "他刚走到三楼拐角，一道黑影从消防通道里闪出来，抬手就朝他怀里的布包抓去"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请抽取剧情事件：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。"
    )
    return system, user


def _build_character_voice_prompt(
    chunk_text: str, character_names: list[str], chunk_label: str
) -> tuple[str, str]:
    """构造角色声音提取的 prompt。

    针对已知的高频角色名，分析每个角色的说话风格 + 关系动态。
    """
    names_str = "、".join(character_names)
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中分析**特定角色的说话风格**。\n\n"
        "对每个角色，分析：\n"
        "1. 自称/称呼习惯（如「我」「本座」「老子」「人家」）\n"
        "2. 口头禅/高频用语（列出 2-3 个具体词句）\n"
        "3. 句长偏好（短句干脆/长句绵密/混合）\n"
        "4. 语气特征（命令式/试探式/撒娇式/冷淡式/嘲讽式...）\n"
        "5. 对白节奏（抢话/停顿/欲言又止/一口气说完）\n"
        "6. **关系动态**：对不同人说话方式的变化（如「对A温柔，对B冷淡」）\n"
        "7. **代表性台词**：摘录 1-2 句最能体现该角色声音的对白原文\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        f'  "{character_names[0]}": {{\n'
        '    "自称": "...",\n'
        '    "口头禅": ["词1", "词2"],\n'
        '    "句长": "短句/长句/混合",\n'
        '    "语气": "...",\n'
        '    "节奏": "...",\n'
        '    "关系动态": "对X如何，对Y如何",\n'
        '    "代表台词": ["台词1", "台词2"]\n'
        '  },\n'
        f'  "{character_names[1] if len(character_names) > 1 else "其他角色"}": {{...}}\n'
        '}\n\n'
        "**重要**：JSON 的 key 必须是上面列出的真实角色名，不要用「角色A」「角色B」等占位符。"
    )
    user = (
        f"以下是《{chunk_label}》的片段，请分析以下角色的说话风格：{names_str}\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。只分析列出的角色，不要分析其他角色。"
        "每个角色必须包含关系动态和代表台词。"
    )
    return system, user


def _find_character_chapters(
    chapters: list[tuple[int, str]], char_name: str, max_chapters: int = 8
) -> list[tuple[int, str]]:
    """从全文章节里找出该角色出场最多的章节。

    不是用统一的采样章节，而是针对每个角色检索——
    配角可能只在特定章节大量出场，统一采样会漏掉。
    """
    scored = []
    for ch_no, text in chapters:
        count = text.count(char_name)
        if count >= 2:  # 至少出现 2 次（配角跨章累计出场多，单章可能只有 2 次）
            scored.append((count, ch_no, text))
    scored.sort(key=lambda x: -x[0])
    return [(ch_no, text) for _, ch_no, text in scored[:max_chapters]]


def _build_voice_rule(char_name: str, voice: dict) -> str | None:
    """从 LLM 返回的 voice dict 构建规则字符串。"""
    parts = [f"{char_name}："]
    if voice.get("自称") and voice["自称"] != "无":
        parts.append(f"自称「{voice['自称']}」")
    if voice.get("口头禅"):
        habits = voice["口头禅"]
        if isinstance(habits, list):
            valid = [h for h in habits if h and h != "无"]
            if valid:
                parts.append(f"口头禅「{'、'.join(str(h) for h in valid[:3])}」")
        elif habits != "无":
            parts.append(f"口头禅「{habits}」")
    if voice.get("句长") and voice["句长"] != "无":
        parts.append(f"句长{voice['句长']}")
    if voice.get("语气") and voice["语气"] != "无":
        parts.append(f"语气{voice['语气']}")
    if voice.get("节奏") and voice["节奏"] != "无":
        parts.append(f"节奏{voice['节奏']}")
    if voice.get("关系动态") and voice["关系动态"] != "无":
        parts.append(f"关系动态：{voice['关系动态']}")
    if voice.get("代表台词"):
        lines = voice["代表台词"]
        if isinstance(lines, list):
            valid = [l for l in lines if l and l != "无"]
            if valid:
                parts.append(f"代表台词：「{'」「'.join(str(l)[:25] for l in valid[:2])}」")
    if len(parts) <= 1:
        return None
    return " ".join(parts)


def _find_voice_for_char(parsed: dict, char_name: str) -> dict | None:
    """从 LLM 返回的 dict 里找角色声音。

    LLM 可能用简称（「宫爵」而非「南宫爵」）作为 key。
    先精确匹配，再模糊匹配（char_name 包含 key 或 key 包含 char_name）。
    """
    voice = parsed.get(char_name)
    if isinstance(voice, dict):
        return voice
    # 模糊匹配
    for key, val in parsed.items():
        if not isinstance(val, dict):
            continue
        if char_name in key or key in char_name:
            return val
    return None


def _extract_character_voices_single_batch(
    provider, nid: int, chapters: list[tuple[int, str]], char_names: list[str]
) -> list[dict]:
    """快速档的角色声音提取：全部角色挤在**一次** LLM 调用里。

    对应 docs 里 v3 档的「单批浅层」：只求有个粗略的说话风格印象，不做
    逐角色的专属章节检索。这是快速档省下大量调用的关键（完整档约 11 次调用，
    这里1 次）。
    """
    if not char_names or not chapters:
        return []
    merged_text: list[str] = []
    for char_name in char_names:
        char_chapters = _find_character_chapters(chapters, char_name, max_chapters=2)
        merged_text.extend(text for _, text in char_chapters)
    voice_text = "\n\n".join(merged_text)[:8000]
    if not voice_text:
        return []
    try:
        system, user = _build_character_voice_prompt(voice_text, char_names, f"语料{nid}")
        parsed = _parse_extract_result(provider.chat(system, user))
        if not isinstance(parsed, dict):
            return []
        results: list[dict] = []
        for char_name in char_names:
            voice = _find_voice_for_char(parsed, char_name)
            if not isinstance(voice, dict):
                continue
            rule = _build_voice_rule(char_name, voice)
            if rule:
                results.append({"rule": rule, "novel_id": nid, "chapter_no": 0})
        return results
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "角色声音单批提取失败 novel_id=%s: %s", nid, exc
        )
        return []


def _extract_character_voices(
    provider, db, project, nid: int, chapters: list[tuple[int, str]],
    char_names: list[str] | None = None,
    voice_mode: str = "targeted",
    cleaning: str = "full",
    on_done: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """混合方案：主要角色逐个提取，配角分批提取。

    - 前 VOICE_MAIN_CHARACTERS 个高频角色：逐个提取（专属章节，深度分析）
    - 剩余角色：分批提取（每批 VOICE_BATCH_SIZE 个，混合章节）
    - 总共约 8 + 3 = 11 次调用

    char_names 由 _build_character_name_index 清洗后给出（已剔除伪角色与
    分词截断的简称）——直接用图谱原始顺序会把提名额浪费在「时间/任何/冷声」
    这类伪角色上，且提取出的简称无法归并到全名。

    Args:
        voice_mode: "targeted"（完整/平衡档，逐个 + 分批）或
            "single_batch"（快速档，一次调用搞定，对应 docs 的 v3「单批浅层」）。
        cleaning: 传给 _build_character_name_index 的清洗强度。
        on_done: 心跳回调 `(已完成, 本批总数)`，透传给内部 `_gather_chat`。
            这一批是 8+4=12 个并发调用，是整条蒸馏里除主提炼外唯一可能连续
            十几分钟没有进度写入的段落——不传心跳就会被看护误判成卡住。
            单批模式（快速档）只有 1 次调用，不需要心跳。
    """
    if voice_mode == "single_batch":
        if char_names is None:
            canonical, _ = _build_character_name_index(
                db, project, nid, cleaning=cleaning
            )
            char_names = [n for n, _ in canonical]
        char_names = [n for n in char_names if n and len(n) >= 2][:MAX_VOICE_CHARACTERS]
        return _extract_character_voices_single_batch(
            provider, nid, chapters, char_names
        )

    if char_names is None:
        char_names, _ = _build_character_name_index(db, project, nid, cleaning=cleaning)
        char_names = [n for n, _ in char_names]
    char_names = [
        n for n in char_names if n and len(n) >= 2
    ][:MAX_VOICE_CHARACTERS]

    if not char_names or not chapters:
        return []

    results: list[dict] = []
    main_chars = char_names[:VOICE_MAIN_CHARACTERS]
    side_chars = char_names[VOICE_MAIN_CHARACTERS:]

    # v2.227：收集主角 + 配角批的所有 prompt → 一次 _gather_chat 跑并发
    # 之前是 8+3 = 11 次串行 provider.chat，11x 提速
    all_prompts: list[tuple[str, str, dict | None]] = []
    all_prompt_meta: list[dict] = []  # 与 prompts 顺序对应，记录每个 prompt 是主角/配角 + 哪批

    # —— 主要角色：逐个提取 ——
    for char_name in main_chars:
        char_chapters = _find_character_chapters(chapters, char_name)
        if not char_chapters:
            continue
        voice_text = "\n\n".join(text for _, text in char_chapters)[:6000]
        if not voice_text:
            continue
        system, user = _build_character_voice_prompt(
            voice_text, [char_name], f"语料{nid}"
        )
        all_prompts.append((system, user, None))
        all_prompt_meta.append({
            "kind": "main",
            "char_name": char_name,
            "first_chapter": char_chapters[0][0],
        })

    # —— 配角：分批提取 ——
    side_batches: list[list[str]] = []
    for i in range(0, len(side_chars), VOICE_BATCH_SIZE):
        batch = side_chars[i : i + VOICE_BATCH_SIZE]
        # 收集这批角色的出场章节
        batch_texts = []
        for char_name in batch:
            char_chapters = _find_character_chapters(chapters, char_name, max_chapters=3)
            if char_chapters:
                batch_texts.extend(text for _, text in char_chapters)
        if not batch_texts:
            continue
        voice_text = "\n\n".join(batch_texts)[:12000]
        system, user = _build_character_voice_prompt(
            voice_text, batch, f"语料{nid}"
        )
        all_prompts.append((system, user, None))
        all_prompt_meta.append({"kind": "side", "batch": batch})
        side_batches.append(batch)

    # 一次 gather 跑所有 prompt
    if all_prompts:
        try:
            raws = _gather_chat(provider, all_prompts, on_done=on_done)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "角色声音批量提取失败: %s", exc
            )
            raws = []
    else:
        raws = []

    # 按 meta 解析每个结果
    for raw, meta in zip(raws, all_prompt_meta):
        if isinstance(raw, Exception):
            logging.getLogger(__name__).warning(
                "角色声音提取失败 %s: %s", meta, raw
            )
            continue
        parsed = _parse_extract_result(raw)
        if not isinstance(parsed, dict):
            continue
        if meta["kind"] == "main":
            char_name = meta["char_name"]
            voice = _find_voice_for_char(parsed, char_name)
            if not isinstance(voice, dict):
                continue
            rule = _build_voice_rule(char_name, voice)
            if rule:
                results.append({
                    "rule": rule,
                    "novel_id": nid,
                    "chapter_no": meta["first_chapter"],
                })
        else:  # side batch
            for char_name in meta["batch"]:
                voice = _find_voice_for_char(parsed, char_name)
                if not isinstance(voice, dict):
                    continue
                rule = _build_voice_rule(char_name, voice)
                if rule:
                    results.append({
                        "rule": rule,
                        "novel_id": nid,
                        "chapter_no": 0,
                    })

    return results


def _build_drift_prompt(chunks_info: list[tuple[str, str]], chunk_label: str,
                        metrics_text: str = "") -> tuple[str, str]:
    """构造文风漂移对比的 prompt。

    `chunks_info` 是**整段全文**（早期前 5 章 / 晚期后 5 章），不再截 2000 字——
    不到一章的样本得不出「对白密度变化」这种需要跨章统计的结论。
    `metrics_text` 是同一时期首/末 10 块的确定性统计，要求模型解释数字而不是
    凭观感猜（历史上它曾给出与 635 个量化块相反的「对白密度升高」）。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是对比**同一作者在不同时期的文风差异**。\n\n"
        "对比维度：\n"
        "1. 句长节奏变化（早期 vs 后期）\n"
        "2. 对白密度变化\n"
        "3. 描写细腻度变化\n"
        "4. 叙事结构复杂度变化\n"
        "5. 用词偏好变化\n\n"
        "硬性要求：\n"
        "- 每个结论必须引用下面给出的客观统计数字，说明方向（变大/变小）与幅度"
        "（约几个百分点 / 几个字）。\n"
        "- 如果文本读感与统计数字冲突，**以统计数字为准**，并解释为什么读起来会不同。\n"
        "- 不许出现没有任何数字支撑的纯观感描述。\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "漂移总结": "整体趋势描述",\n'
        '  "早期特点": ["特点1", "特点2"],\n'
        '  "后期特点": ["特点1", "特点2"],\n'
        '  "最大变化": "最显著的变化维度"\n'
        '}\n'
    )
    parts = []
    for period_name, text in chunks_info:
        parts.append(f"【{period_name}】\n{text}")
    user = f"以下是《{chunk_label}》在不同时期的全文片段：\n\n" + "\n\n---\n\n".join(parts)
    if metrics_text:
        user += "\n\n" + metrics_text
    user += "\n\n请按上述维度对比输出 JSON，所有结论都要引用上面的统计数字。"
    return system, user


def _format_drift_metrics(early_feats: dict, late_feats: dict) -> str:
    """把首/末 10 块的确定性指标渲染成漂移 prompt 的对照数据。"""
    if not early_feats or not late_feats:
        return ""

    def row(label, getter):
        e, l = getter(early_feats), getter(late_feats)
        if e is None or l is None:
            return None
        return f"- {label}：早期 {e} → 晚期 {l}"

    rows = [
        row("对白占正文比", lambda f: f"{f['对白']['占正文比']:.1%}"),
        row("对白平均长度（字）", lambda f: f["对白"]["平均长度"]),
        row("句长均值（字）", lambda f: f["句长"]["均值"]),
        row("短句(≤5字)占比", lambda f: f"{f['句长']['短句占比']:.1%}"),
        row("段长均值（字）", lambda f: f["段落"]["段长均值"]),
        row("一两句短段占比", lambda f: f"{f['段落']['一两句短段占比']:.1%}"),
        row("情绪直陈/千字", lambda f: f["情绪"]["直陈密度每千字"]),
        row("动作承载/千字", lambda f: f["情绪"]["动作承载密度每千字"]),
        row("问号/千字", lambda f: f["悬念"]["问号每千字"]),
        row("省略号/千字", lambda f: f["悬念"]["省略号每千字"]),
    ]
    rows = [r for r in rows if r]
    return (
        "【系统实测：首 10 块 vs 末 10 块（确定性统计，非模型推测）】\n"
        + "\n".join(rows)
        + "\n以上数字是精确计算的结果，结论必须与它们一致。"
    )


def resolve_distill_provider():
    """蒸馏链路用哪个 provider：**优先 distiller 角色**，没配则回落 Writer。

    ## 为什么优先 distiller

    用户在「模型设置」里给蒸馏单独配了一个角色（第三条轨道），意图是
    「提炼文风用一个更便宜/更适合长文的模型，生成用另一个」。但蒸馏链路之前
    取的是 Writer 的 `get_llm_provider()` —— 那个 distiller 角色配了等于没配，
    界面上的「蒸馏模型」是个摆设。这里把它接上。

    ## 为什么必须有回落（老用户行为零变化）

    `build_distiller_provider()` 在**两种**情况下会返回 `LocalFallbackLLMProvider`：
    没填 provider、或没填 api_key。而从未配过 distiller 的用户就落在这两种里。
    这时若直接用兜底模型去蒸馏，产出会从「用户配的 Writer 模型」退化成
    本地模板 —— 一个**静默的质量倒退**（不报错，只是结果变差）。

    所以判定条件是「解析出来的是不是 LocalFallback」：是 → 说明用户压根没配
    distiller，沿用改动前的 `get_llm_provider()`，行为逐字节不变；
    不是 → 说明用户真的配了，用它。

    ## 为什么不在模块顶层 import

    `app.llm` 依赖 `app.config`，而 config 会在 settings 变更时热重建；
    函数内 import 保证每次调用都拿到最新的工厂，也避免 `app.style` 与
    `app.llm` 之间出现顶层循环导入。
    """
    from app.llm import get_distiller_provider
    from app.llm.provider import LocalFallbackLLMProvider, get_llm_provider

    try:
        candidate = get_distiller_provider()
    except Exception as exc:  # noqa: BLE001 —— 配置异常不该让蒸馏链路整个炸掉
        logging.getLogger(__name__).warning(
            "distiller provider 解析失败，回落 Writer provider：%s: %s",
            type(exc).__name__, exc,
        )
        candidate = None

    # LocalFallback = 没配 distiller（见 docstring），此时绝不能"用了个兜底模型"，
    # 而是要走改动前的那条路。
    if candidate is not None and not isinstance(candidate, LocalFallbackLLMProvider):
        return candidate
    return get_llm_provider()


# ====================================================================
# 蒸馏失败的归因（把「全部调用失败」拆成用户能动手的几类）
# ====================================================================
#
# 修复前：所有块都失败时统一报「API key 无效 / 已限流 / 网络不通？」。
# 实测很大一部分失败既不是 key 也不是网络，而是**模型返回的内容不满足蒸馏的
# JSON 协议**：没配 key 时的本地兜底模板、thinking 模型吃满 max_tokens 后被截断
# 的半截 JSON。用户照着提示去查 key 和网络，永远查不到根因。
#
# 归因桶：
#   no_key          —— 没有真实模型可用，拿到的是本地兜底模板
#   auth            —— 401 / 403，key 无效或无权限
#   rate_limit      —— 429 / 配额用尽
#   model_not_found —— 404，模型名不存在
#   network         —— 超时 / 连接被拒 / DNS
#   truncated       —— 输出像 JSON 但不完整（典型：被 max_tokens 截断）
#   protocol        —— 有返回，但既不是 JSON 也没有任何预期维度
#   unknown         —— 其余异常（保留原始信息，交给日志）

#: 失败桶 → 人话标签。
_FAILURE_LABELS: dict[str, str] = {
    "no_key": "没有可用的模型 API key（走的是本地兜底模板）",
    "auth": "API key 无效或无权限",
    "rate_limit": "模型限流 / 配额用尽",
    "model_not_found": "模型名不存在",
    "network": "网络不可达 / 超时",
    "truncated": "模型输出被截断（JSON 不完整）",
    "protocol": "模型返回的不是约定格式的 JSON",
    "unknown": "未归类的调用失败",
}

#: 失败桶 → 可执行的下一步（报错里逐条附上，不再让用户猜）。
_FAILURE_NEXT_STEPS: dict[str, str] = {
    "no_key": "在 .env 配置 WRITER_API_KEY 或 DISTILLER_API_KEY，或在「模型设置」里"
              "给蒸馏单独选一个模型；本地兜底模板只产出占位文本，不产出蒸馏规则。",
    "auth": "核对 distiller / writer 的 api_key 与 base_url 是否配套（换渠道后 key "
            "也要换），并确认该 key 未被禁用。",
    "rate_limit": "稍后重试或换一个模型 / 账号；免费档的日限额按账号计，"
                  "同渠道换其它免费模型无效。",
    "model_not_found": "核对 config/providers.yaml 与 .env 里的模型名（含大小写与"
                       "后缀），换成该渠道确实提供的模型。",
    "network": "检查 base_url 是否可达（curl 一下），确认代理 / 防火墙 / DNS；"
               "自建服务确认端口已启动。",
    "truncated": "调高 distiller.max_tokens（providers.yaml 或 DISTILLER_MAX_TOKENS），"
                 "或换一个不把思维链写进 content 的模型。",
    "protocol": "换一个指令遵循更好的模型，并确认它没把推理过程混进 content；"
                "提示词要求纯 JSON，返回散文 / 模板即属此类。",
    "unknown": "看后端日志里的原始异常（日志会打出 provider 的具体报错），据此再定位。",
}

#: 报错里列桶的固定顺序（保证同一组失败每次输出一致）。
_FAILURE_ORDER: tuple[str, ...] = (
    "no_key", "auth", "rate_limit", "model_not_found",
    "network", "truncated", "protocol", "unknown",
)


def _classify_provider_error(exc: Exception) -> str:
    """把一个 provider 调用异常归到可操作的桶里。

    provider 会把底层异常包成 `RuntimeError("<label> chat 调用失败: <原异常>")`，
    HTTPError 的 `.code` 在那之后已丢失，所以只能同时看异常类型与文本。
    """
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(k in text for k in (
        "401", "403", "unauthorized", "forbidden", "invalid api key",
        "incorrect api key", "authentication", "invalid_api_key",
    )):
        return "auth"
    if any(k in text for k in (
        "429", "rate limit", "rate_limit", "too many requests", "quota",
        "free-models-per-day", "限流",
    )):
        return "rate_limit"
    if any(k in text for k in ("404", "model not found", "does not exist", "not_found")):
        return "model_not_found"
    if any(k in text for k in (
        "urlerror", "timed out", "timeout", "connection refused",
        "connection reset", "connection aborted", "broken pipe", "getaddrinfo",
        "name or service not known", "network is unreachable",
        "nodename nor servname", "ssl",
    )):
        return "network"
    return "unknown"


def _classify_chunk_failure(raws: list, provider=None) -> str:
    """一块语料为什么没产出任何规则。

    真异常优先于「有文本但解析不出来」——前者是调用层面失败，后者是协议层面失败。
    多个 prompt 同块失败时，取信息量最大的那个桶（auth > rate_limit > 404 > network）。
    """
    errors = [r for r in raws if isinstance(r, Exception)]
    if errors:
        buckets = [_classify_provider_error(e) for e in errors]
        for wanted in ("auth", "rate_limit", "model_not_found", "network"):
            if wanted in buckets:
                return wanted
        return buckets[0]

    texts = [r for r in raws if isinstance(r, str)]
    if any("离线兜底" in t for t in texts):
        return "no_key"
    if provider is not None:
        # 延迟 import：app.llm 依赖 app.config，顶层 import 会形成循环。
        from app.llm.provider import LocalFallbackLLMProvider

        if isinstance(provider, LocalFallbackLLMProvider):
            return "no_key"
    # 有内容、且以 JSON 结构开头却解析失败 → 多半是被 max_tokens 截断的半截 JSON
    if any(t.lstrip().startswith("{") or "```json" in t for t in texts):
        return "truncated"
    return "protocol"


def _describe_provider(provider) -> str:
    """报错里描述本次用的模型（**绝不回显 api_key**）。"""
    label = getattr(provider, "label", None) or type(provider).__name__
    bits = [str(label)]
    model = getattr(provider, "model", None)
    base_url = getattr(provider, "base_url", None)
    if model:
        bits.append(f"model={model}")
    if base_url:
        bits.append(f"base_url={base_url}")
    return " / ".join(bits)


def _format_distill_failure(
    failed: int, reasons: dict[str, int], provider=None
) -> str:
    """把「N 块全失败」渲染成按原因分组的报错文本。"""
    lines = [f"蒸馏失败：{failed} 个语料块全部未产出任何规则。"]
    for key in _FAILURE_ORDER:
        count = reasons.get(key, 0)
        if not count:
            continue
        lines.append(
            f"- {_FAILURE_LABELS[key]}：{count} 块。下一步：{_FAILURE_NEXT_STEPS[key]}"
        )
    if provider is not None:
        lines.append(f"本次使用的蒸馏模型：{_describe_provider(provider)}")
    return "\n".join(lines)


# v2.226：并发蒸馏辅助函数
# 同步入口，内部 asyncio.run 跑 N 个 chat 真并发（限流由 semaphore 控制）
# 智谱免费档 ~5 req/s，限流 5；OR nemotron 50 req/day 用尽后可降到 2
_DEFAULT_MAX_CONCURRENT = 5


async def _gather_chat_async(
    provider, prompts: list[tuple[str, str, dict | None]],
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    on_done: Callable[[int, int], None] | None = None,
) -> list[str | Exception]:
    """异步 gather N 个 provider.async_chat 调用（受 semaphore 限流）。

    `on_done`：每有一条 prompt 返回就回调一次，入参是 `(已完成数, 本批总数)`。
    **只用来打「心跳」**（证明后台线程还活着、顺便让人看到这一批的计数），不改
    任何提炼逻辑、更不改返回顺序。没有它的话，一本语料的所有 prompt 是一次性
    gather 的，中间几十分钟没有任何进度写入——看护会把它误判成「卡住」（见
    `app/style/jobs.py` 的 stall 判据）。

    计数在事件循环的单线程里自增，天然无竞态；返回（含异常）都会触发回调。
    """
    sem = asyncio.Semaphore(max_concurrent)
    total = len(prompts)
    done = 0

    async def _one(system: str, user: str, constraints):
        nonlocal done
        async with sem:
            try:
                return await provider.async_chat(system, user, constraints=constraints)
            except Exception as exc:  # noqa: BLE001 —— 单条失败不阻断其余
                return exc
            finally:
                done += 1
                if on_done is not None:
                    try:
                        on_done(done, total)
                    except Exception:  # noqa: BLE001 —— 心跳失败不阻断蒸馏
                        pass

    tasks = [_one(s, u, c) for s, u, c in prompts]
    return await asyncio.gather(*tasks, return_exceptions=False)


def _bind_heartbeat(
    heartbeat: HeartbeatCallback | None, prefix: str
) -> Callable[[int, int], None] | None:
    """把人话阶段前缀绑到心跳上，交给 `_gather_chat` 的 `on_done`。

    提炼侧知道「这是第几本语料、当前在干什么」，后台任务只负责拼计数与写库；
    前缀在这里绑死，回调签名就能保持 `(已完成, 总数)` 这种最小形状。
    """
    if heartbeat is None:
        return None

    def _beat(done: int, total: int) -> None:
        heartbeat(done, total, prefix)

    return _beat


def _gather_chat(
    provider, prompts: list[tuple[str, str, dict | None]],
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    on_done: Callable[[int, int], None] | None = None,
) -> list[str | Exception]:
    """同步入口：内部 asyncio.run 跑并发。

    用法：把串行 for 循环里的 provider.chat() 改为收集所有 (system, user, constraints)
    三元组到 prompts 列表，再调一次 _gather_chat 拿回 list[str | Exception]。

    返回顺序与 prompts 顺序一致；失败用 Exception 实例（不抛，调用方决定怎么处理）。
    """
    if not prompts:
        return []
    return asyncio.run(_gather_chat_async(provider, prompts, max_concurrent, on_done))


def _stream_chunks(
    provider,
    chunk_specs: list[dict],
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    on_done: Callable[[int, int], None] | None = None,
    on_chunk: Callable[[dict, list], None] | None = None,
) -> None:
    """按**块**流式执行：某块的 prompt 全部返回就立刻回调，不再等整本 gather。

    ## 为什么不能再用一次大 gather（2026-11-12 五小时额度墙的教训）

    旧实现把一个语料所有块的 5 个 prompt 拍平成几百条，一次
    `asyncio.gather` 等**全部**返回后才逐块处理、逐块 `_save_done_chunk`。
    于是 04:56 撞上 MiniMax 五小时额度墙时，一整本书已经跑完的 160 块也
    一起丢在内存里（`distill_chunks` 一行没写），只能从头再来。

    现在每个块是一个独立协程：它自己的 5 条调用一返回，就立刻把这块交给
    `on_chunk` 解析并落库。撞墙最多损失**在飞的十几块**，已完成块全部保住。

    `max_concurrent` 仍然限制**单条 LLM 调用**的并发（不是块数），与旧口径
    一致；`on_done` 仍是每有一条调用返回就触发的心跳。
    """

    if not chunk_specs:
        return
    asyncio.run(
        _stream_chunks_async(provider, chunk_specs, max_concurrent, on_done, on_chunk)
    )


async def _stream_chunks_async(
    provider,
    chunk_specs: list[dict],
    max_concurrent: int,
    on_done: Callable[[int, int], None] | None,
    on_chunk: Callable[[dict, list], None] | None,
) -> None:
    sem = asyncio.Semaphore(max_concurrent)
    total = sum(len(s["prompts"]) for s in chunk_specs)
    done = 0

    async def _call(prompt: tuple[str, str, dict | None]):
        nonlocal done
        async with sem:
            try:
                return await provider.async_chat(
                    prompt[0], prompt[1], constraints=prompt[2]
                )
            except Exception as exc:  # noqa: BLE001 —— 单条失败只回传给本块
                return exc
            finally:
                done += 1
                if on_done is not None:
                    try:
                        on_done(done, total)
                    except Exception:  # noqa: BLE001 —— 心跳失败不阻断蒸馏
                        pass

    async def _one_chunk(spec: dict):
        raws = await asyncio.gather(*(_call(p) for p in spec["prompts"]))
        if on_chunk is not None:
            try:
                on_chunk(spec, list(raws))
            except Exception as exc:  # noqa: BLE001 —— 单块解析/落库失败不阻断其余块
                logging.getLogger(__name__).warning(
                    "蒸馏块处理失败 novel_id=%s range=%s: %s",
                    spec.get("nid"), spec.get("cr"), exc,
                )

    await asyncio.gather(*(_one_chunk(s) for s in chunk_specs))


def _ensure_reference_graphs(
    db: Session,
    project,
    novel_ids: list[int],
    progress: ProgressCallback | None = None,
) -> dict[int, int]:
    """v2.228：确保参考语料图谱存在，缺哪本建哪本。

    为什么必须在蒸馏入口做：`_build_character_name_index` 的唯一数据源是
    `KgNode(type="character", source=f"novel:{nid}")`。这张图来自
    `kg/build.py::build_graph`（纯规则抽取，零 LLM）。若用户没点过「建图」
    就直接蒸馏，候选池为空 → 角色声音维度静默产出 0 条，而其余 13 个维度
    正常产出 —— 故障无声无息，只能靠人肉发现「角色声音那栏是空的」。

    幂等性：已有 character 节点的语料直接跳过，不重建（避免覆盖用户在
    图上做的任何后续加工）。只有 0 个 character 节点时才建。

    Args:
        progress: 可选的进度回调，建图前报一次。

    Returns:
        {novel_id: 建出的 character 节点数}；跳过的语料记 -1（未重建）。

    Raises:
        不抛：建图失败只记 warning，交给后续维度各自兜底（参考语料图谱是
        角色声音维度的增强，不该让整轮蒸馏因为一本语料的分片异常而失败）。
    """
    from app.models import KgNode

    stats: dict[int, int] = {}
    logger = logging.getLogger(__name__)

    for nid in novel_ids:
        try:
            existing = db.scalar(
                select(func.count()).select_from(KgNode).where(
                    KgNode.project_id == project.id,
                    KgNode.type == "character",
                    KgNode.source == f"novel:{nid}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("参考语料图谱自检失败 novel_id=%s: %s", nid, exc)
            continue

        if existing:
            stats[nid] = -1  # 已有图，未重建
            continue

        if progress is not None:
            progress(f"参考语料 {nid} 缺角色图谱，正在补建", 0, 0)

        try:
            from app.kg.build import build_graph

            built = build_graph(db, project.id, novel_id=nid)
            n_char = int(built.get("characters", 0) or 0)
            stats[nid] = n_char
            if n_char == 0:
                logger.warning(
                    "参考语料 %s 建图成功但抽出 0 个角色（语料可能无对白）", nid
                )
        except Exception as exc:  # noqa: BLE001 —— 建图失败不阻断蒸馏
            logger.warning("参考语料 %s 建图失败: %s", nid, exc)
            stats[nid] = 0

    return stats


#: 14 个 L2 场景 key（与 `retrieval/catalog_config.py` 的 SCENE_LEXICON 对齐）。
#: 蒸馏时由模型标注，检索时只作**先验/软信号**，不当 ground truth 用。
SCENE_LABEL_KEYS = (
    "combat", "daily", "groupchat", "trade", "cultivate", "travel",
    "interrogate", "reveal", "intimacy", "farewell", "banquet",
    "misunderstand", "rescue", "night",
)

_SCENE_LABEL_ALIASES = {
    "战斗对峙": "combat", "日常琐事": "daily", "群聊多声部": "groupchat",
    "交易交换": "trade", "修炼疗伤": "cultivate", "赶路移动": "travel",
    "审讯盘问": "interrogate", "揭示爆料": "reveal", "情感亲密": "intimacy",
    "离别告别": "farewell", "宴席聚会": "banquet", "误会冲突": "misunderstand",
    "救援脱困": "rescue", "夜谈独处": "night",
}


def _norm_scene_label(value: Any) -> str | None:
    """把模型给的场景标签归一到 14 个 ASCII key；不认识返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text in SCENE_LABEL_KEYS:
        return text
    if text in _SCENE_LABEL_ALIASES:
        return _SCENE_LABEL_ALIASES[text]
    low = text.lower()
    return low if low in SCENE_LABEL_KEYS else None


def _parse_chunk_meta(
    parsed: dict, novel_id: int, chapter_range: str
) -> tuple[dict, list[str]]:
    """从主提炼结果里取三个块级字段，**逐字段容错**。

    返回 `(meta, missing_fields)`。任何字段缺失/类型不对只记名字，
    不抛异常、不拖垮同块的其他字段与规则落库。
    """
    missing: list[str] = []
    meta: dict = {
        "novel_id": int(novel_id),
        "chapter_range": chapter_range,
        "primary_scene": "",
        "secondary_scenes": [],
        "scene_labels": [],
        "summary": "",
        "characters": [],
    }

    # 主场景：有且只有一个；"none" 表示过渡/设定说明段，不算缺失。
    raw_primary = parsed.get("primary_scene")
    primary: str | None = None
    if isinstance(raw_primary, str):
        text = raw_primary.strip()
        if text.lower() == "none":
            primary = "none"
        else:
            primary = _norm_scene_label(text)
    # 兼容上一版提示词的「场景标签」数组：只取第一个当主场景。
    if primary is None and isinstance(parsed.get("场景标签"), list):
        legacy = parsed["场景标签"]
        primary = _norm_scene_label(legacy[0]) if legacy else None
    if primary is None:
        missing.append("primary_scene")

    # 次场景：0–1 个；没有就是空数组，不许凑数。
    secondary: list[str] = []
    raw_secondary = parsed.get("secondary_scenes")
    if isinstance(raw_secondary, list):
        for item in raw_secondary:
            key = _norm_scene_label(item)
            if not key or key == primary or key in secondary:
                continue
            secondary.append(key)
        secondary = secondary[:1]

    meta["primary_scene"] = primary or ""
    meta["secondary_scenes"] = secondary
    # 兼容字段：主 + 次（旧消费方读 scene_labels 时仍能拿到）。
    meta["scene_labels"] = (
        ([primary] if primary and primary != "none" else []) + secondary
    )

    raw_summary = parsed.get("章节摘要")
    if isinstance(raw_summary, str) and raw_summary.strip():
        meta["summary"] = raw_summary.strip()
    else:
        missing.append("章节摘要")

    raw_chars = parsed.get("人物别名")
    if isinstance(raw_chars, list):
        for item in raw_chars:
            if not isinstance(item, dict):
                continue
            name = str(item.get("规范名") or "").strip()
            if not name:
                continue
            aliases = [
                str(a).strip() for a in (item.get("称呼") or [])
                if str(a).strip()
            ]
            meta["characters"].append({"name": name, "aliases": aliases[:8]})
        if not meta["characters"]:
            missing.append("人物别名")
    else:
        missing.append("人物别名")

    return meta, missing


def _parse_kg_events(
    parsed: dict, novel_id: int, chapter_range: str, chapter_nos: list[int]
) -> tuple[list[dict], list[str]]:
    """解析每块 KG 事件（可选第 6 个调用）。逐条容错。"""
    missing: list[str] = []
    raw = parsed.get("事件")
    if not isinstance(raw, list):
        return [], ["kg_events"]
    events: list[dict] = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("事件") or "").strip()
        if not desc:
            continue
        chapter_no: int | None = None
        try:
            chapter_no = int(item.get("章"))
        except (TypeError, ValueError):
            chapter_no = None
        chapter_ok = chapter_no is not None and chapter_no in chapter_nos
        if not chapter_ok:
            missing.append("kg_events.章")
            chapter_no = chapter_nos[0] if chapter_nos else None
        events.append({
            "novel_id": int(novel_id),
            "chapter_range": chapter_range,
            "chapter_no": chapter_no,
            "chapter_guess_ok": chapter_ok,
            "event": desc,
            "characters": [
                str(c).strip() for c in (item.get("人物") or [])
                if str(c).strip()
            ],
            "location": str(item.get("地点") or "").strip(),
            "impact": str(item.get("后果") or "").strip(),
            "evidence": str(item.get("证据") or "").strip(),
        })
    return events, missing


def extract_narrative_profile(
    db: Session,
    project,
    novel_ids: list[int],
    provider=None,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
    heartbeat: HeartbeatCallback | None = None,
    resume: bool = False,
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
    with_kg_events: bool = False,
) -> dict:
    """从参考语料提炼叙事档案。

    Args:
        tier: 档位 key（"full"/"balanced"/"fast"）或 NarrativeTier 对象。
            None →默认完整档（与引入档位之前行为逐字节一致）。
        progress: 可选的进度回调 `progress(stage, current, total)`，用于后台
            蒸馏任务的进度条。**None 时行为与加进度之前完全一致**——采样、
            维度、provider 选择、返回结构一项都不变。
            单位是「块（chunk）」：一本语料切成若干块，每块一次 `provider.chat`，
            这就是用户能感知到的天然粒度。
        heartbeat: 可选的心跳回调 `(已完成, 本批总数, 阶段前缀)`，**每有一条
            LLM 请求返回就打一次**。只为后台看护的心跳（`progress_at`）与一句
            人话计数服务——块级进度在一次 gather 里是攒着报的，几十分钟没有写入
            会被误判成卡住。不参与任何提炼决策。
        resume: 是否复用 `distill_chunks` 里已完成的块（断点续跑）。True 时
            已完成块不建 prompt、不调 LLM，直接回灌净产出；False（默认）时
            行为与引入续跑之前逐字节一致——生成路径走的就是不传的那条。
        max_concurrent: 单本语料的 LLM 并发数（默认 5，按智谱免费档调过）。
            付费模型可调高；只是并发窗口，不影响采样、维度与产出结构。
        with_kg_events: 是否每块多跑一次「KG 事件抽取」（第 6 个调用）。
            默认关闭；开启后单块 6 次调用，产出 `kg_events` 顶层字段。
            做成开关是为了先看主流程数据，再决定是否默认开。

    返回结构（可直接 JSON 落库到 `Project.style_narrative`）：
    {
      "version": 1,
      "source_ids": [...],
      "narrative_version": 1,
      "tier": "full",
      "n_chapters": 30,
      "rules": {
        "人物描写手法": [
          {"rule": "规则文本（证据：原文句子）", "novel_id": 1, "chapter_no": 5},
          ...
        ],
        ...
      },
      "rejected": 3,  # 因证据不通过被拒的规则数
    }

    Raises:
        ValueError: tier 是未知档位 key（不静默回落默认档）。
    """
    if provider is None:
        provider = resolve_distill_provider()

    cfg = tier if isinstance(tier, NarrativeTier) else resolve_tier(tier)
    # 本档要提炼的维度（白名单子集）。全维度档下它就等于 NARRATIVE_DIMENSIONS。
    tier_dims = [d for d in NARRATIVE_DIMENSIONS if d in cfg.dimensions]
    has_dim = set(tier_dims)

    all_rules: dict[str, list[dict]] = {
        dim: [] for dim in tier_dims + ["文风漂移分析"]
    }
    all_forbidden: dict[str, list[str]] = {dim: [] for dim in FORBIDDEN_DIMENSIONS}
    #: 块级附加字段（场景标签 / 章节摘要 / 人物别名）与 KG 事件。
    #: 逐字段容错：某个字段解析失败只记 field_missing，不影响规则落库。
    all_chunk_meta: list[dict] = []
    all_kg_events: list[dict] = []
    field_missing: dict[str, int] = {}
    rejected = 0
    #: v2.265 角色名审计结果（只标记不删除，供人工复核）
    voice_audit: dict | None = None
    total_chapters = 0
    # —— 失败块统计（P1-2 修复：LLM 全挂不许静默 success）——
    # chunks_attempted：真正发起了主提炼调用的块数（空块不计）；
    # failed_chunks：其中 chat 抛错或返回不可解析 JSON 的块数。
    # 附加维度（禁忌/节拍/场景/潜台词/角色声音/漂移）的失败不计入——
    # 它们是增强，单独失败不该判整次蒸馏失败（与原行为一致，仍只记日志）。
    chunks_attempted = 0
    failed_chunks = 0
    #: 失败块的原因分桶（见 _classify_chunk_failure / _format_distill_failure）。
    #: 只在「全部块都失败」时用来生成分原因的报错；部分失败仍然只记计数。
    failed_reasons: dict[str, int] = {}

    # —— 进度上报的累计量（只在 progress 非 None 时有意义）——
    # blocks_done：已完成块数（跨语料累计）；
    # blocks_seen / books_seen：已探明的块数与语料数，用来估剩余语料的块数。
    total_books = len(novel_ids)
    blocks_done = 0
    blocks_seen = 0
    books_seen = 0
    est_total = 0

    # —— 断点续跑：先读本 scope 下已完成的块 ——
    # 只有 resume=True 才回灌；False 时整条链路与引入续跑之前一致。
    scope = distill_resume_scope(novel_ids, cfg, max_chapters)
    # 换档/换语料 → 旧 scope 的切块清掉，避免下一次蒸馏读到过期语料。
    _clear_stale_scope_chunks(db, project.id, scope)
    done_chunks: dict[str, dict] = (
        _load_done_chunks(db, project.id, scope) if resume else {}
    )
    #: 起始进度 = 已保留的主提炼块数。不归零——续跑的进度条要从上次停的地方接着走。
    blocks_done = sum(1 for k in done_chunks if not k.startswith(_AUX_KEY_PREFIX))
    if resume and done_chunks:
        prov: dict[str, int] = {}
        for src in _chunk_provenance(db, project.id, scope).values():
            prov[src] = prov.get(src, 0) + 1
        logging.getLogger(__name__).info("断点来源分布：%s", prov)

    if progress is not None:
        progress("正在准备参考语料", 0, 0)
    if blocks_done and progress is not None:
        progress(
            f"继续上次：已保留 {blocks_done} 块，跳过已完成部分",
            blocks_done,
            0,
        )

    # —— 前置自检：参考语料图谱（v2.228）——
    # 角色声音维度依赖 KgNode(type="character", source=f"novel:{nid}")——
    # 这张图由 kg/build.py 的规则抽取器建（零 LLM），入口是
    # POST /api/projects/{id}/build-kg?novel_id=N。用户路径上它由「建图」按钮触发，
    # 但蒸馏是独立入口：只传 reference_novel_ids 直接蒸的话图谱压根不存在，
    # _build_character_name_index 拿到空候选池 → 角色声音维度静默产出 0 条。
    # 这里做前置补齐：缺图才建，已有则跳过（幂等，一次全量规则抽取，实测秒级）。
    _ensure_reference_graphs(db, project, novel_ids, progress=progress)

    novel_texts: dict[int, str] = {}
    for book_idx, nid in enumerate(novel_ids, start=1):
        chapters = _load_novel_chapters(db, nid)
        if not chapters:
            continue
        # 角色声音的出场频次按**全书**精确计数（不是按采样块），供渲染层按用途排序。
        novel_texts[int(nid)] = "\n".join(t for _, t in chapters if t)

        # 按档位采样：完整档走百分比 + 对话密集优先 + 场景均衡；
        # 平衡/快速档走固定章数均匀采样。
        sampled = sample_chapters_for_tier(chapters, cfg)
        total_chapters += len(sampled)

        # 分块：固定 5 个真实章节一块（= MIN_CHAPTERS_PER_CHUNK）。
        # 20 章一块在完整档读全本后单块可达 16 万字，超出多数模型上下文；
        # 5 章一块把单块压到约 3~4 万字，同时保留足够的跨章上下文。
        # 章已按 chapter_no 聚合，块内 chapter_range 单调递增。
        chunk_size = MIN_CHAPTERS_PER_CHUNK
        chunk_starts = list(range(0, len(sampled), chunk_size))
        n_chunks = len(chunk_starts)

        # —— 先切块并算稳定 key（只做切片 + 拼接，不调 LLM，开销可忽略）——
        # 必须先切完才知道哪些块已完成，才能算出本次真正要跑的块数。
        prepared: list[dict] = []
        for ci, i in enumerate(chunk_starts):
            chunk = sampled[i : i + chunk_size]
            chunk_text = "\n\n".join(text for _, text in chunk if text)
            if not chunk_text:
                continue
            chapter_range = f"{chunk[0][0]}-{chunk[-1][0]}"
            prepared.append({
                "ci": ci,
                "chunk": chunk,
                "text": chunk_text,
                "cr": chapter_range,
                "key": _chunk_key(nid, chapter_range, chunk_text),
            })

        # 已完成块回灌：净产出直接进累加器，不再建 prompt、不再调 LLM。
        # 失败块（上次没写进断点）会落到 pending 里，这次重新尝试。
        pending_chunks: list[dict] = []
        restored_here = 0
        for spec in prepared:
            stored = done_chunks.get(spec["key"])
            if stored is None:
                pending_chunks.append(spec)
                continue
            _apply_chunk_payload(stored, all_rules, all_forbidden)
            if isinstance(stored.get("meta"), dict):
                all_chunk_meta.append(stored["meta"])
            if isinstance(stored.get("kg_events"), list):
                all_kg_events.extend(stored["kg_events"])
            rejected += int(stored.get("rejected") or 0)
            chunks_attempted += 1
            restored_here += 1

        # 估算总块数：已探明语料的**平均块数** × 剩余未探明语料数。
        # 为什么是估算而不是精确值：精确值要先把每本语料都加载+采样一遍，
        # 那等于把最贵的一步做两次。估算法的性质是——跑到最后一本时剩余项
        # 归零，est_total 自动收敛成精确值，进度条不会停在 97%。
        # 下限兜底取「已完成 + 本次还要跑的块数」：后一本语料若比已探明的平均值
        # 厚重，估算值会小于实际要跑的块数，进度百分比就会越过 100%。这里保证
        # 「当前已跑 ≤ 总数」这条不变量成立，前端不必再做防御。
        books_seen += 1
        blocks_seen += n_chunks
        avg_chunks = blocks_seen / books_seen if books_seen else float(n_chunks)
        remaining_new = len(pending_chunks)
        est_total = max(
            int(
                round(
                    blocks_done
                    + remaining_new
                    + (total_books - book_idx) * avg_chunks
                )
            ),
            blocks_done + remaining_new,
        )

        if progress is not None:
            resume_note = (
                f"，继续上次（已保留 {restored_here} 块）" if restored_here else ""
            )
            progress(
                f"语料 {book_idx}/{total_books}：已采样 {len(sampled)} 章，"
                f"切 {n_chunks} 块{resume_note}",
                blocks_done,
                est_total,
            )

        # v2.227 (P2)：chunk 间并发 ——
        # 把每 chunk 的所有 prompt 收集到一个 flat 列表，一次 _gather_chat 跑并发
        # （限流 5 req/s 串行排队），然后按 (ci, kind) 切回每 chunk 处理。
        # 之前 chunk 间串行 = 60 chunk × 1 批 = 60 批，1.8x 提速
        # 改后 chunk 间并发 = 60 chunk × 1 批 = 1 批 300 prompt，理论 5x 提速
        chunk_specs: list[dict] = []  # 每 chunk 的 spec: {ci, text, chunk, cr, key, prompts, kinds}
        for piece in pending_chunks:
            ci = piece["ci"]
            chunk = piece["chunk"]
            chunk_text = piece["text"]
            chapter_range = piece["cr"]

            chunk_prompts: list[tuple[str, str, dict | None]] = []
            chunk_prompt_kinds: list[str] = []
            # v2.260：把原 extract（10 维）拆为「角色组（3 维）」 + 「叙事组（7 维）」两次调用。
            # 模型视野从 10 维分散 → 3 维集中 / 7 维集中，输出更精。
            ec_system, ec_user = _build_extract_character_prompt(
                chunk_text, f"语料{nid} 第{chapter_range}章"
            )
            chunk_prompts.append((ec_system, ec_user, None))
            chunk_prompt_kinds.append("extract_character")

            # v2.262：回退到 v2.260 的「角色组 + 叙事组」两组。
            #
            # v2.261 曾把叙事组再拆为「结构组（3 维）+ 技法组（4 维）」，
            # probe（单 chunk 3 轮）显示 +52% 规则、+72% 校验，但真链路
            # （雪中 990 章 30% 采样）证伪：
            #
            #   版本      耗时      规则   rejected  三个结构维度
            #   v2.260   25.4 min   2112     734    剧情126 伏笔121 段落108
            #   v2.261   34.5 min   1904     761    剧情 76 伏笔 72 段落 55
            #
            # 结构三维亚均 -40%。原因是这三维彼此有依赖：判断「段落编排」
            # 需要同时看节奏快慢与场景切换，「剧情编排」需要伏笔/悬念的
            # 上下文。拆开后模型失去了判断依据，大量过渡章/对话章写不出规则。
            #
            # 「概念上能分组」不等于「拆开后更好」——probe 只用一个开篇
            # 章节（叙事密度最高）验证，是选择偏差。
            en_system, en_user = _build_extract_narrative_prompt(
                chunk_text, f"语料{nid} 第{chapter_range}章"
            )
            chunk_prompts.append((en_system, en_user, None))
            chunk_prompt_kinds.append("extract_narrative")

            # 反向禁忌只取全书第一块：这是「作者从不用的表达」的全局结论，
            # 每块都跑一遍既贵又容易被局部片段带偏。这里必须用块序号 ci 判断；
            # 旧代码误用了上一个循环遗留的 i（= 最后一块的起始下标），
            # 导致多块语料永远不构造这条 prompt，FORBIDDEN_DIMENSIONS 恒为空。
            if ci == 0:
                f_system, f_user = _build_forbidden_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                chunk_prompts.append((f_system, f_user, None))
                chunk_prompt_kinds.append("forbidden")

            if "节拍序列" in has_dim:
                b_system, b_user = _build_beat_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                chunk_prompts.append((b_system, b_user, None))
                chunk_prompt_kinds.append("beat")

            if "高频场景模板" in has_dim:
                s_system, s_user = _build_scene_template_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                chunk_prompts.append((s_system, s_user, None))
                chunk_prompt_kinds.append("scene")

            if "潜台词模式" in has_dim:
                st_system, st_user = _build_subtext_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                chunk_prompts.append((st_system, st_user, None))
                chunk_prompt_kinds.append("subtext")

            if with_kg_events:
                k_system, k_user = _build_kg_events_prompt(
                    chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章"
                )
                chunk_prompts.append((k_system, k_user, None))
                chunk_prompt_kinds.append("kg_events")

            chunk_specs.append({
                "ci": ci,
                "nid": nid,
                "text": chunk_text,
                "chunk": chunk,
                "cr": chapter_range,
                "key": piece["key"],
                "prompts": chunk_prompts,
                "kinds": chunk_prompt_kinds,
            })

        def _consume_chunk(spec: dict, raws: list) -> None:
            """一块的 5 个维度都回来了：解析 → 并入累加器 → 立刻落库并 commit。

            由 `_stream_chunks` 在**每块自己的调用全部返回后**立即回调，不再
            等整本语料 gather。块解析/落库异常由调用方兜住，不影响其余块。
            """
            nonlocal blocks_done, rejected, chunks_attempted, failed_chunks
            ci = spec["ci"]
            b = {
                "raws": raws,
                "kinds": spec["kinds"],
                "text": spec["text"],
                "chunk": spec["chunk"],
                "cr": spec["cr"],
                "key": spec["key"],
            }
            # 每处理完一块 blocks_done 就 +1（含失败块——它也算「跑过」）。
            # blocks_done 起始值已含续跑回灌的块数，所以进度条不会归零。
            blocks_done += 1
            if progress is not None:
                progress(
                    f"语料 {book_idx}/{total_books}：提炼第 {ci + 1}/{n_chunks} 块",
                    blocks_done,
                    est_total,
                )

            chapter_range = b["cr"]
            chunk_text = b["text"]
            chunk = b["chunk"]
            chunk_key = b["key"]

            # 2026-11-12：本块的**真实** novel_chunks 行与整章文本，一次查好。
            # 旧档案只写 chunk[0][0]（蒸馏块起始章），证据实际章最多偏 4 章，
            # 检索侧只能拿 5 章窗口硬找。新档案逐条写 chunk_id + evidence_chapter_no。
            block_rows, chapter_norm = _load_block_chunk_rows(
                db, nid, [ch for ch, _ in chunk]
            )

            def _anchor_fields(rule: str) -> dict:
                """证据 → 真实块 id + 证据实际章号的落库字段。"""
                cid, ech = _resolve_evidence_anchor(
                    _rule_evidence(rule), block_rows, chapter_norm
                )
                return {
                    "chunk_id": cid,
                    "distill_chunk_key": chunk_key,
                    "chapter_range": chapter_range,
                    "evidence_chapter_no": ech if ech is not None else chunk[0][0],
                }

            # 本块净产出先收在局部：解析成功才一次性并入累加器 + 落断点。
            # 失败块什么都不写（续跑时会被重新尝试），因此 failed_chunks 不会跨轮累加。
            chunk_rules: dict[str, list[dict]] = {}
            chunk_forbidden: dict[str, list[str]] = {}
            chunk_rejected = 0

            # v2.230：「文笔量化审计」由确定性指标生成，完全不依赖 LLM。
            # 放在 extract 判空之前——即使这一块的 LLM 调用失败，硬指标仍然产出。
            # 这是该维度从 LLM 输出清单里移除后唯一的数据来源。
            _feats = build_deterministic_features(chunk_text)
            if _feats:
                _feat_rule = _format_features_rule(_feats)
                chunk_rules.setdefault("文笔量化审计", []).append({
                    "rule": _feat_rule,
                    "novel_id": nid,
                    "chapter_no": chunk[0][0],
                    **_anchor_fields(_feat_rule),
                })

            # 按 kind 取出 raw
            def _raw_for(kind):
                if kind not in b["kinds"]:
                    return None
                idx = b["kinds"].index(kind)
                v = b["raws"][idx]
                return None if isinstance(v, Exception) else v

            # v2.262：回到 v2.260 的「角色组 + 叙事组」两个 prompt，
            # 合并成一个 parsed dict（两组维度不重叠，合并即完整 10 维）。
            raw_character = _raw_for("extract_character")
            raw_narrative = _raw_for("extract_narrative")

            # —— 失败块统计（P1-2 修复：LLM 全挂不许静默 success）——
            # master 侧把各块 prompt 拍平后一次 _gather_chat 并发发出，单块不再有
            # 「这一块 chat 抛错」的边界；等价口径 = 本块的两组主提炼 prompt
            # （角色组 / 叙事组，无条件构造）都拿不到可用结果。
            # chunks_attempted 计「真正发起了主提炼调用的块数」，failed_chunks 计
            # 其中解析不出任何预期维度的块数——与串行版的判定语义一致。
            chunks_attempted += 1
            expected_dims = set(tier_dims) | set(FORBIDDEN_DIMENSIONS)

            parsed: dict = {}
            for r in (raw_character, raw_narrative):
                if r is None:
                    continue
                p = _parse_extract_result(r)
                if isinstance(p, dict):
                    parsed.update(p)

            # 不可用输出 = 调用失败：`_parse_extract_result` 对垃圾输入返回 {}
            # （空 dict 也过 isinstance 检查），或返回不含任何预期维度键的 dict。
            # 不计失败的话，「LLM 全程返回兜底文案」会静默 success + 0 规则。
            if not parsed or not (set(parsed) & expected_dims):
                failed_chunks += 1
                # 记录本块的真实失败原因（异常类型 / 离线兜底 / 协议不符），
                # 供「全部块失败」时按原因分组报错。
                block_raws = [
                    b["raws"][b["kinds"].index(k)]
                    for k in ("extract_character", "extract_narrative")
                    if k in b["kinds"]
                ]
                reason = _classify_chunk_failure(block_raws, provider)
                failed_reasons[reason] = failed_reasons.get(reason, 0) + 1
                # 失败块不写断点（续跑时重试），但**本块的确定性硬指标仍要保留**——
                # 「文笔量化审计」不依赖 LLM，没有理由跟着失败一起丢。
                for dim, items in chunk_rules.items():
                    all_rules.setdefault(dim, []).extend(items)
                return  # 失败块不落断点（续跑时重试），本块到此结束

            # —— 块级附加字段（场景标签 / 章节摘要 / 人物别名）——
            # 逐字段容错：缺哪个就记 field_missing，其余字段照常落库。
            chunk_meta, meta_missing = _parse_chunk_meta(parsed, nid, chapter_range)
            for name in meta_missing:
                field_missing[name] = field_missing.get(name, 0) + 1

            # —— 可选：KG 事件（第 6 个调用）——
            chunk_kg: list[dict] = []
            if with_kg_events:
                kg_raw = _raw_for("kg_events")
                kg_parsed = (
                    _parse_extract_result(kg_raw) if kg_raw is not None else None
                )
                if isinstance(kg_parsed, dict):
                    chunk_kg, kg_missing = _parse_kg_events(
                        kg_parsed, nid, chapter_range, [ch for ch, _ in chunk]
                    )
                    for name in kg_missing:
                        field_missing[name] = field_missing.get(name, 0) + 1
                else:
                    field_missing["kg_events"] = (
                        field_missing.get("kg_events", 0) + 1
                    )

            for dim in tier_dims:
                items = parsed.get(dim)
                if not isinstance(items, list):
                    continue
                for rule in items:
                    if not isinstance(rule, str) or not rule.strip():
                        continue
                    # 证据验证：伪规则在这里被拦掉
                    if not _validate_evidence(rule, chunk_text):
                        chunk_rejected += 1
                        continue
                    # 逐字复制检测：LLM 直接复制原文句子而非抽象规则，信息量极低
                    if _is_verbatim_copy(rule, chunk_text):
                        chunk_rejected += 1
                        continue
                    chunk_rules.setdefault(dim, []).append({
                        "rule": rule.strip(),
                        "novel_id": nid,
                        "chapter_no": chunk[0][0],
                        **_anchor_fields(rule),
                    })

            # —— 反向禁忌提取 ——
            f_raw = _raw_for("forbidden")
            if f_raw is not None:
                f_parsed = _parse_extract_result(f_raw)
                if isinstance(f_parsed, dict):
                    for dim in FORBIDDEN_DIMENSIONS:
                        items = f_parsed.get(dim)
                        if not isinstance(items, list):
                            continue
                        for item in items:
                            if not isinstance(item, str) or not item.strip():
                                continue
                            chunk_forbidden.setdefault(dim, []).append(item.strip())

            # —— v6：节拍序列提取 ——
            b_raw = _raw_for("beat")
            if b_raw is not None:
                b_parsed = _parse_extract_result(b_raw)
                if isinstance(b_parsed, dict):
                    beats = b_parsed.get("节拍序列") or []
                    for beat in beats:
                        if isinstance(beat, dict) and beat.get("步骤") and beat.get("写法"):
                            raw_rule = f"{beat['步骤']}：{beat['写法']}"
                            # 独立「证据」字段优先；模型若仍把证据写在写法里，
                            # 走原来的内联格式（_rule_evidence 能直接取到）。
                            ev = str(beat.get("证据") or "").strip()
                            rule = (
                                f"{_rule_body(raw_rule)}（证据：{ev}）" if ev else raw_rule
                            )
                            beat_ch = None
                            try:
                                beat_ch = int(beat.get("章"))
                            except (TypeError, ValueError):
                                beat_ch = None
                            if beat_ch not in {ch for ch, _ in chunk}:
                                beat_ch = None
                                field_missing["beat_chapter_no"] = (
                                    field_missing.get("beat_chapter_no", 0) + 1
                                )
                            if (_evidence_long_enough(rule)
                                    and not _is_verbatim_copy(rule, chunk_text)):
                                chunk_rules.setdefault("节拍序列", []).append({
                                    "rule": rule,
                                    "novel_id": nid,
                                    "chapter_no": chunk[0][0],
                                    "beat_chapter_no": beat_ch,
                                    **_anchor_fields(rule),
                                })

            # —— v5：高频场景模板提取 ——
            s_raw = _raw_for("scene")
            if s_raw is not None:
                s_parsed = _parse_extract_result(s_raw)
                if isinstance(s_parsed, dict):
                    scenes = s_parsed.get("场景模板") or []
                    for scene in scenes:
                        if isinstance(scene, dict) and scene.get("场景"):
                            rule = (
                                f"{scene['场景']}：环境={scene.get('环境','')} "
                                f"站位={scene.get('站位','')} "
                                f"动作链={scene.get('动作链','')} "
                                f"基调={scene.get('基调','')}"
                            )
                            ev = str(scene.get("证据") or "").strip()
                            if ev:
                                rule = f"{rule}（证据：{ev}）"
                            if not _evidence_long_enough(rule):
                                continue
                            chunk_rules.setdefault("高频场景模板", []).append({
                                "rule": rule,
                                "novel_id": nid,
                                "chapter_no": chunk[0][0],
                                **_anchor_fields(rule),
                            })

            # —— v5：潜台词模式提取 ——
            st_raw = _raw_for("subtext")
            if st_raw is not None:
                st_parsed = _parse_extract_result(st_raw)
                if isinstance(st_parsed, dict):
                    patterns = st_parsed.get("潜台词模式") or []
                    for pat in patterns:
                        if isinstance(pat, dict) and pat.get("模式") and pat.get("写法"):
                            raw_rule = f"{pat['模式']}：{pat['写法']}"
                            ev = str(pat.get("证据") or "").strip()
                            rule = (
                                f"{_rule_body(raw_rule)}（证据：{ev}）" if ev else raw_rule
                            )
                            if (_evidence_long_enough(rule)
                                    and not _is_verbatim_copy(rule, chunk_text)):
                                chunk_rules.setdefault("潜台词模式", []).append({
                                    "rule": rule,
                                    "novel_id": nid,
                                    "chapter_no": chunk[0][0],
                                    **_anchor_fields(rule),
                                })

            # —— 块完成：并入累加器 + 落断点 ——
            # 顺序很重要：先并入再落库，落库失败也只是一条 warning，不影响本次产出；
            # 反过来则可能出现「库里记了完成、内存里没这份规则」的静默丢数据。
            for dim, items in chunk_rules.items():
                all_rules.setdefault(dim, []).extend(items)
            for dim, items in chunk_forbidden.items():
                all_forbidden.setdefault(dim, []).extend(items)
            all_chunk_meta.append(chunk_meta)
            all_kg_events.extend(chunk_kg)
            rejected += chunk_rejected
            _save_done_chunk(
                db,
                project.id,
                scope,
                nid,
                chunk_key,
                chapter_range,
                {
                    "rules": chunk_rules,
                    "forbidden": chunk_forbidden,
                    "rejected": chunk_rejected,
                    "meta": chunk_meta,
                    "kg_events": chunk_kg,
                },
                provider=getattr(provider, "label", None),
                endpoint=getattr(provider, "base_url", None),
            )

        # 按块流式跑：每块自己的 5 条调用一返回就回调 `_consume_chunk` 并入 +
        # 落库 + commit。撞额度墙时只损失在飞的块，已完成块全部保住。
        if chunk_specs:
            _stream_chunks(
                provider,
                chunk_specs,
                max_concurrent=max_concurrent,
                on_done=_bind_heartbeat(
                    heartbeat, f"语料 {book_idx}/{total_books}：并发提炼中"
                ),
                on_chunk=_consume_chunk,
            )

        # 本语料的块已全部跑完。块数已在逐块处理时累加（含续跑回灌的块），
        # 所以这里不再按 n_chunks 整体补记——否则续跑时会把已完成块重复计数。

        # —— v5：角色声音提取（针对性：每个角色检索出场最多的章节）——
        if progress is not None:
            progress(
                f"语料 {book_idx}/{total_books}：提取角色声音",
                blocks_done,
                est_total,
            )
        try:
            # v6.6：从图谱取规范角色名 + 语料自证的简称映射。
            # 图谱的 character 节点含大量高频虚词（时间/任何/相信）与分词截断
            # （小落/安小/宫爵/南宫），清洗后才能当作规范名。
            #
            # 清洗强度与是否归一化**由档位决定**：
            # - 完整档：全量清洗（黑名单 + 语料词频自证）+ 归一化简称
            # - 平衡档：仅黑名单，不归一化（简称保留原样）
            # - 快速档：不清洗，也不归一化
            canonical, alias = _build_character_name_index(
                db, project, nid, corpus_text="", cleaning=cfg.fake_character_cleaning
            )
            if cfg.fake_character_cleaning == "full":
                # 只有完整档需要语料全文做频率自证（重活：拼全文 + O(n²) 包含判定）。
                corpus_text = "\n".join(t for _, t in chapters if t)
                canonical, alias = _build_character_name_index(
                    db, project, nid, corpus_text, cleaning="full"
                )
            # 未开启归一化的档位：canonical/alias 一律传空，让
            # _dedup_character_voices 退化为「只做同名合并，不做简称归并」。
            norm_canonical = canonical if cfg.normalize_character_names else []
            norm_alias = alias if cfg.normalize_character_names else {}
            clean_fake = cfg.fake_character_cleaning != "none"

            voice_key = f"{_AUX_KEY_PREFIX}voice_{nid}"
            stored_voice = done_chunks.get(voice_key)
            # 只有「有内容」的断点才算完成。04:56 撞额度墙时 12 批声音调用
            # 全部 429，旧代码仍把空列表写成已完成，续跑会直接跳过、永远补不回。
            if stored_voice is not None and stored_voice.get("voices"):
                # 上次已完成：跳过这一轮的全部角色声音调用，直接用存下来的原始结果。
                # 存的是**去重前**的 LLM 原始产出——去重依赖本轮重算的 canonical/alias，
                # 存归一化后的结果会让档位或语料微调后对不上名字。
                voice_results = list(stored_voice.get("voices") or [])
            else:
                voice_results = _extract_character_voices(
                    provider, db, project, nid, chapters,
                    char_names=[n for n, _ in canonical],
                    voice_mode=cfg.voice_mode,
                    cleaning=cfg.fake_character_cleaning,
                    on_done=_bind_heartbeat(
                        heartbeat, f"语料 {book_idx}/{total_books}：提取角色声音"
                    ),
                )
                # 空结果不落断点：多数是限流/全部单条失败，不是「这本书没有声音」。
                if voice_results:
                    _save_done_chunk(
                        db, project.id, scope, nid, voice_key, "",
                        {"voices": voice_results},
                        provider=getattr(provider, "label", None),
                        endpoint=getattr(provider, "base_url", None),
                    )
            # 去重 + 角色名归一化（小落→安小落、宫爵→南宫爵、过滤角色A/时间）
            voice_results = _dedup_character_voices(
                voice_results, norm_canonical, norm_alias, clean_fake=clean_fake
            )
            all_rules[CHARACTER_VOICE_DIMENSION].extend(voice_results)
            # 对整个角色声音词典做最终去重：批量路径（维度循环）
            # 也会产出「小落：...」这类简称记录，必须在这里再归一化一次才能合并
            all_rules[CHARACTER_VOICE_DIMENSION] = _dedup_character_voices(
                all_rules[CHARACTER_VOICE_DIMENSION], norm_canonical, norm_alias,
                clean_fake=clean_fake,
            )
            # v2.265：LLM 粗筛角色名，**只标记不删除**。
            # 结果写进最终 profile 的 voice_name_audit 字段供人工复核。
            if voice_audit is None:
                voice_audit = _audit_character_names(
                    provider, all_rules[CHARACTER_VOICE_DIMENSION]
                )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "角色声音提取失败 novel_id=%s: %s", nid, exc
            )

        # —— v3：文风漂移分析（第三轮，对比早晚期）——
        if progress is not None:
            progress(
                f"语料 {book_idx}/{total_books}：文风漂移分析",
                blocks_done,
                est_total,
            )
        drift_key = f"{_AUX_KEY_PREFIX}drift_{nid}"
        stored_drift = done_chunks.get(drift_key)
        try:
            # 空断点同样不算完成（撞墙时的空结论会让续跑永远缺这一条）。
            if stored_drift is not None and stored_drift.get("rules"):
                # 上次已完成：跳过这一次 LLM 调用，回放存下来的漂移结论。
                # 断点键本身按书隔离（aux_drift_{nid}），回放必须**追加**而不是
                # 整键赋值——后者会让后一本书的结论覆盖掉前一本书的。
                for item in stored_drift.get("rules") or []:
                    if isinstance(item, dict) and item.get("novel_id") is None:
                        # 极老断点可能没写 novel_id：按本断点的书补上，否则
                        # 渲染侧无法按书过滤、也无法在多书档案里标出归属。
                        item = {**item, "novel_id": nid}
                    all_rules["文风漂移分析"].append(item)
            elif len(chapters) >= 20:
                early = chapters[:5]
                late = chapters[-5:]
                # 整章全文，不截 2000 字：不到一章的样本撑不起「对白密度」结论。
                early_text = "\n\n".join(t for _, t in early if t)
                late_text = "\n\n".join(t for _, t in late if t)
                # 首/末 10 块（≈50 章）的系统实测指标，要求模型解释数字而非凭观感猜。
                early_feats = build_deterministic_features(
                    "\n\n".join(t for _, t in chapters[:50] if t)
                )
                late_feats = build_deterministic_features(
                    "\n\n".join(t for _, t in chapters[-50:] if t)
                )
                drift_system, drift_user = _build_drift_prompt(
                    [("早期（前5章全文）", early_text), ("晚期（后5章全文）", late_text)],
                    f"语料{nid}",
                    _format_drift_metrics(early_feats, late_feats),
                )
                drift_raw = provider.chat(drift_system, drift_user)
                drift_parsed = _parse_extract_result(drift_raw)
                if isinstance(drift_parsed, dict):
                    # 容错：key 名可能不一致（LLM 输出格式不稳定）
                    summary = (
                        drift_parsed.get("漂移总结")
                        or drift_parsed.get("整体趋势")
                        or drift_parsed.get("总结")
                        or drift_parsed.get("变化总结")
                    )
                    if summary:
                        early_feats = (
                            drift_parsed.get("早期特点")
                            or drift_parsed.get("早期")
                            or []
                        )
                        late_feats = (
                            drift_parsed.get("后期特点")
                            or drift_parsed.get("晚期")
                            or []
                        )
                        max_change = (
                            drift_parsed.get("最大变化")
                            or drift_parsed.get("最显著的变化")
                            or ""
                        )
                        if isinstance(early_feats, list):
                            early_str = "、".join(str(x) for x in early_feats[:5])
                        else:
                            early_str = str(early_feats)[:200]
                        if isinstance(late_feats, list):
                            late_str = "、".join(str(x) for x in late_feats[:5])
                        else:
                            late_str = str(late_feats)[:200]
                        entry = {
                            "rule": (
                                f"整体趋势：{summary}；"
                                f"早期：{early_str}；"
                                f"晚期：{late_str}；"
                                f"最大变化：{max_change}"
                            ),
                            "novel_id": nid,
                            "chapter_no": 0,
                        }
                        # 按书保留：每本书一条，后一本不许覆盖前一本。
                        all_rules["文风漂移分析"].append(entry)
                        _save_done_chunk(
                            db, project.id, scope, nid, drift_key, "",
                            # 断点按书存，只存本次这一条——回放时才不会把别的书带进来。
                            {"rules": [entry]},
                            provider=getattr(provider, "label", None),
                            endpoint=getattr(provider, "base_url", None),
                        )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "文风漂移分析失败 novel_id=%s: %s", nid, exc
            )

    if progress is not None:
        progress("正在汇总提炼出的规则", blocks_done, max(blocks_done, 1))

    # —— 静默空结果护栏（P1-2 修复）——
    # 语料为空 / 所有块全部调用失败，都不许以「成功」收场：
    # 二者的表现都是「蒸馏完成但 0 条规则」，用户无从分辨是语料问题、
    # key 问题还是限流，比直接失败难排查得多。
    if total_chapters == 0:
        raise ValueError(
            "参考语料没有可蒸馏的章节（语料为空或尚未导入完成），未产出任何规则"
        )
    if chunks_attempted and failed_chunks == chunks_attempted:
        raise ValueError(_format_distill_failure(failed_chunks, failed_reasons, provider))

    # 角色声音的渲染排序索引：出场频次 + 非人标记（只进档案顶层，不改规则条目）。
    voice_stats, voice_non_human = build_voice_stats(
        all_rules.get(CHARACTER_VOICE_DIMENSION) or [], novel_texts
    )
    return {
        "version": NARRATIVE_VERSION,
        "source_ids": sorted(int(i) for i in novel_ids),
        "narrative_version": NARRATIVE_VERSION,
        "tier": cfg.key,
        "tier_name": cfg.name,
        "tier_dimensions": tier_dims,
        "n_chapters": total_chapters,
        "rules": all_rules,
        "forbidden": all_forbidden,
        "rejected": rejected,
        # 部分块失败时 >0：任务仍算成功，但前端/日志能看见「有块被跳过」
        "failed_chunks": failed_chunks,
        # 声音排序索引：渲染层按用途（原创/续写/对话）用不同口径取前 N 条。
        "voice_stats": voice_stats,
        "voice_non_human": voice_non_human,
        # v2.265：角色名审计（只标记不删除）。
        # 给出「模型认为这些词不像人名」，供前端/人工复核决定是否处理。
        # 不做自动删除——审计本身会误判（雪中「红薯」是丫鬟名却被标为普通名词）。
        "voice_name_audit": voice_audit or {"suspicious": [], "checked": 0,
                                            "skipped": False},
        # —— 块级附加索引（2026-11-12）——
        # 每条带 chapter_range，可按章检索；场景标签只是模型的软标注，
        # 后续要用锚点原文抽检一致率，不能直接当 ground truth。
        "chunk_meta": all_chunk_meta,
        "kg_events": all_kg_events,
        # 逐字段容错账本：某个字段缺了多少次。全 0 表示 5 个字段全齐。
        "field_missing": field_missing,
    }


def narrative_fingerprint(
    source_ids: list[int], tier: str | NarrativeTier | None = None
) -> dict:
    """叙事档案的失效判据：语料集合 + 提炼版本号 + **档位**。

    ## 为什么档位必须在指纹里（本项目已踩过两次的同类坑）

    之前指纹只按 `source_ids` + `narrative_version` 算。用户从完整档切到快速档时，
    指纹**没变** → `project_narrative` 认为缓存有效 → 直接返回完整档的旧结果。
    用户「选了快速档」却拿到完整档产出，**且没有任何报错**——最难查的那类 bug。

    所以指纹里除了档位 key，还带档位**配置的哈希**（`_tier_signature`）：
    改了某档的维度白名单 / 采样章数 / 清洗强度，旧缓存同样会失效。

    Args:
        source_ids: 参考语料 novel_id 列表。
        tier: 档位 key 或配置对象；None → 默认完整档。
    """
    cfg = tier if isinstance(tier, NarrativeTier) else resolve_tier(tier)
    return {
        "source_ids": sorted(int(i) for i in source_ids),
        "narrative_version": NARRATIVE_VERSION,
        "tier": cfg.key,
        "tier_signature": _tier_signature(cfg),
    }


def _source_ids_hash(source_ids: list[int]) -> str:
    """参考语料集合的稳定哈希（顺序归一），用于蒸馏库的复用查找键。"""
    import hashlib

    return hashlib.sha256(
        ",".join(str(i) for i in sorted(int(x) for x in source_ids)).encode("utf-8")
    ).hexdigest()


def corpus_content_hash(source_ids: list[int], db: Session) -> str:
    """语料**正文内容**的稳定 sha256 —— 蒸馏库复用判据的最后一块拼图。

    ## 为什么需要它（defect-C 回归修复）

    蒸馏库若只按 `source_ids` 复用，会有个隐蔽 bug：同一本语料被重新导入 / 清洗后
    **正文变了**，但 novel_id 没变，于是复用查找仍会命中旧档案 → 产出 stale 文风、
    且**没有任何报错**。把正文内容也揉进哈希，正文没变才命中，正文变了就当未命中
    → 重新蒸馏，这正是 defect-C 要防的静默退化。

    ## 归一方式

    按 novel_id 升序遍历；每本按 (chapter_no, idx) 升序取 chunk text，逐段 strip 后
    拼成大字符串再 sha256。段间加分隔符，避免「甲乙」与「甲+乙」撞哈希；首尾空白差异
    不影响结果。
    """
    import hashlib

    h = hashlib.sha256()
    for nid in sorted(int(i) for i in source_ids):
        chapters = _load_novel_chapters(db, nid)
        for _, text in chapters:
            h.update((text or "").strip().encode("utf-8"))
            h.update(b"\x1f")
    return h.hexdigest()


def find_style_distill(
    db: Session,
    source_ids: list[int],
    tier_key: str,
    tier_signature: str,
    content_hash: str,
) -> "StyleDistillType | None":
    """在蒸馏库里找一条**可复用**的档案（active 态，四键全中）。

    命中即代表「同样语料 + 同样档位 + 同样正文」曾经蒸馏过，可以直接复用，
    省掉一次分钟级 LLM。未命中（None）才需要真正提炼。
    """
    from app.models import StyleDistill

    return db.scalar(
        select(StyleDistill).where(
            StyleDistill.source_ids_hash == _source_ids_hash(source_ids),
            StyleDistill.tier == tier_key,
            StyleDistill.tier_signature == tier_signature,
            StyleDistill.content_hash == content_hash,
            StyleDistill.status == "active",
        )
    )


def _persist_style_distill(
    db: Session,
    project,
    source_ids: list[int],
    cfg: "NarrativeTier",
    content_hash: str,
    profile: dict,
) -> None:
    """把一次蒸馏产物写进「蒸馏库」。

    幂等 upsert：**同输入（source_ids + tier + tier_signature + content_hash）在 active
    态下至多一行**。先按四键查已有 active 行：

    - 有 → 原地更新 `style_narrative` 与各摘要字段（force 重蒸馏时就是「覆盖旧档」，
      不会又长出一行重复档案）；
    - 无 → 插入新行。

    这样「第二个项目复用同款语料」时只会 FIND 到已有行、不会重复落库，满足
    「相同输入 → 1 条 active 行」的不变量。

    注意：本函数**只**负责写库，不碰 `project.style_narrative`——那是调用方的职责
    （`project_narrative` 负责把档案同时落到项目缓存与库里）。
    """
    from app.models import StyleDistill

    sid_hash = _source_ids_hash(source_ids)
    existing = db.scalar(
        select(StyleDistill).where(
            StyleDistill.source_ids_hash == sid_hash,
            StyleDistill.tier == cfg.key,
            StyleDistill.tier_signature == _tier_signature(cfg),
            StyleDistill.content_hash == content_hash,
            StyleDistill.status == "active",
        )
    )
    rules = profile.get("rules") or {}
    rule_count = sum(len(v) for v in rules.values() if isinstance(v, list))
    if existing is not None:
        existing.style_narrative = profile
        existing.n_chapters = profile.get("n_chapters")
        existing.rule_count = rule_count
        existing.rejected = profile.get("rejected")
        existing.narrative_version = int(profile.get("narrative_version", NARRATIVE_VERSION))
        existing.applied_at = datetime.now(timezone.utc)
        return

    row = StyleDistill(
        source_ids=sorted(int(i) for i in source_ids),
        source_ids_hash=sid_hash,
        tier=cfg.key,
        tier_signature=_tier_signature(cfg),
        content_hash=content_hash,
        narrative_version=int(profile.get("narrative_version", NARRATIVE_VERSION)),
        status="active",
        style_narrative=profile,
        n_chapters=profile.get("n_chapters"),
        rule_count=rule_count,
        rejected=profile.get("rejected"),
        created_by_project_id=getattr(project, "id", None),
        created_at=datetime.now(timezone.utc),
        applied_at=datetime.now(timezone.utc),
    )
    db.add(row)


def _usable_narrative(profile) -> bool:
    """档案是否有可注入内容（有规则或反向禁忌）。空壳不算「能用」。"""
    return (isinstance(profile, dict)
            and bool(profile.get("rules") or profile.get("forbidden")))


def _covers_sources(archive_ids, want_ids) -> bool:
    """档案来源是否覆盖当前参考书（相等或超集）。

    只放行「项目收窄参考书」：档案按 [5, 6] 蒸馏、项目现在只读 [5]，渲染侧
    `filter_rules_by_source` 会把书 6 的条目滤掉，档案仍然可用。
    反向（项目新增了书、档案没覆盖）不放行——那会静默少一本书的规则。
    """
    archive = {int(i) for i in (archive_ids or [])}
    want = {int(i) for i in (want_ids or [])}
    return bool(want) and want.issubset(archive)


def reusable_narrative(db: Session, project, ids=None, cfg=None,
                       cached=None) -> dict | None:
    """生成路径**不蒸馏**就能用上的档案；判据只有这一份。

    预检（`generation/engine._require_distill_ready`）与取档
    （`project_narrative`）都调它。此前两边各写一套：取档侧已放宽到「超集可
    复用」，预检侧仍要求指纹完全相等，收窄参考书后预检永远先 raise，放宽的
    代码根本走不到（2026-11-12 修复）。

    取档顺序（全部只读）：
    1. 项目缓存：指纹完全相等 → 原样复用（与旧行为一致，空壳档案也算就绪）。
    2. 项目缓存：来源覆盖当前参考书且内容可用 → 复用（渲染侧按当前参考书过滤）。
    3. 蒸馏库：来源覆盖当前参考书、active、且**按档案自己的 source_ids** 重算的
       正文 hash 与库里记录一致 → 复用。
    4. 都没有 → None（调用方按「不注入」处理）。

    ## content_hash 的口径（预检当初对不上的原因）

    库里行的 `content_hash` 是**按那一行自己的 source_ids** 算的：两行都是
    `[5, 6]` → `5a3437…`。项目收窄到 `[5]` 后若拿 `corpus_content_hash([5])`
    （`b36a30…`）去比，永远不相等——那不是「语料变了」，而是「比错了集合」。
    正确口径是 `corpus_content_hash(row.source_ids) == row.content_hash`：
    它验证的是「这一行当初蒸馏的语料正文至今没变」，与项目现在读几本书无关。
    多出来的那本书由渲染侧来源过滤处理。
    这笔 hash 只在**项目缓存不可用**时才付（约 1 秒/来源集合，只读）。
    """
    if ids is None:
        ids = [int(i) for i in (project.reference_novel_ids or [])]
    ids = [int(i) for i in ids]
    if not ids:
        return None
    if cfg is None:
        cfg = resolve_tier(getattr(project, "narrative_tier", None))
        cap = getattr(project, "narrative_sample_chapters", None)
        if cap is not None:
            cfg = tier_with_sampling(cfg, int(cap))
    if cached is None:
        cached = project.style_narrative or {}
    want = narrative_fingerprint(ids, cfg)
    # 指纹完全相等：与旧行为一致，直接算就绪（哪怕档案内容为空壳）。
    if isinstance(cached, dict) and cached.get("fingerprint") == want:
        return cached
    return _degraded_narrative(db, project, ids, cfg, want, cached)


def _degraded_narrative(db: Session, project, ids: list[int], cfg,
                        want: dict, cached) -> dict | None:
    """`reusable_narrative` 的降级取档部分：**绝不**同步蒸馏。

    修的是「tier 与缓存指纹不匹配就静默起一次几十分钟全量蒸馏」：
    生成请求里没有等蒸馏的预算，静默起蒸馏只会把请求挂到超时。

    判据见 `reusable_narrative`（唯一一份）；这里只负责具体查找与 WARN。
    每次降级都 WARN 写明「期望档位 / 实际档位 / 未触发蒸馏」，不静默。
    """
    log = logging.getLogger(__name__)
    if _usable_narrative(cached):
        fp = cached.get("fingerprint") or {}
        cached_ids = sorted(int(i) for i in (fp.get("source_ids") or []))
        # 允许「档案来源覆盖当前参考书」：项目把 refs 从多本收窄到其中一本后，
        # 档案仍是按原集合蒸馏的。渲染侧会按项目当前参考书集合过滤
        # （`filter_rules_by_source`），所以这里可以继续用这份档案；若坚持
        # 指纹完全相等，就会从「跨书规则」直接退化成「整份档案消失」。
        # 反向（项目新增了书、档案没覆盖）不放行。
        if _covers_sources(cached_ids, ids):
            log.warning(
                "生成路径叙事档案降级：项目 %s 期望 tier=%s（指纹 %s），"
                "实际使用项目缓存 tier=%s（指纹 %s，来源 %s）；按当前参考书 %s "
                "做渲染侧来源过滤，未触发同步蒸馏。"
                "如需目标档位请先 POST /api/projects/%s/distill。",
                getattr(project, "id", None), cfg.key, want.get("tier_signature"),
                fp.get("tier"), fp.get("tier_signature"), cached_ids, ids,
                getattr(project, "id", None),
            )
            return cached

    from app.models import StyleDistill

    # 不能按 source_ids_hash 精确查：收窄后的项目与超集行哈希不同，会漏掉。
    # 库表是跨项目共享的小表，按 active 全取后在内存里筛「来源覆盖」。
    rows = db.scalars(
        select(StyleDistill).where(StyleDistill.status == "active")
        .order_by(StyleDistill.applied_at.desc(), StyleDistill.id.desc())
    ).all()
    #: 同一来源集合只算一次 hash；口径是**档案自己的** source_ids，
    #: 不是项目当前 ids（后者会让超集行永远对不上）。
    hashes: dict[tuple[int, ...], str] = {}
    for row in rows:
        if not _usable_narrative(row.style_narrative):
            continue
        row_ids = sorted(int(i) for i in (row.source_ids or []))
        if not _covers_sources(row_ids, ids):
            continue
        key = tuple(row_ids)
        if key not in hashes:
            hashes[key] = corpus_content_hash(row_ids, db)
        # 正文 hash 必须一致，避免把「语料被重新导入/清洗后」的旧档案当新的用。
        if row.content_hash != hashes[key]:
            continue
        log.warning(
            "生成路径叙事档案降级：项目 %s 期望 tier=%s，蒸馏库命中 tier=%s"
            "（来源 %s）；未触发同步蒸馏。如需目标档位请先 "
            "POST /api/projects/%s/distill。",
            getattr(project, "id", None), cfg.key, row.tier, row_ids,
            getattr(project, "id", None),
        )
        return dict(row.style_narrative)
    return None


def project_narrative(
    db: Session,
    project,
    *,
    force: bool = False,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
    persist_library: bool = True,
    content_hash: str | None = None,
    sample_chapters: int | None = None,
    heartbeat: HeartbeatCallback | None = None,
    resume: bool = False,
) -> dict | None:
    """取项目当前该用的叙事档案；没有参考语料则返回 None。

    已存且指纹一致就直接返回——与 `style_profile` 同理，叙事档案进的是静态 system 块，
    每次生成都变会击穿提示词缓存。

    档位缺省时读`project.narrative_tier`（项目级偏好，已落库）；
    那个字段也为空则用默认完整档。

    Args:
        progress: 可选的进度回调，原样透传给 `extract_narrative_profile`
            （后台蒸馏任务的进度条用）。None 时行为与加进度之前完全一致。
        persist_library: 是否把产物落进全局蒸馏库（幂等 upsert）。
            **False = 生成路径专用**：命中项目缓存时纯读返回——不算
            `corpus_content_hash`（全语料加载+sha256）、不写库。生成链路
            每次请求会调 3 次（engine.build_context + generate_next_segment
            ×2），缓存命中还算哈希+写库是纯浪费，还给 SQLite 平添写锁竞争。
        content_hash: 调用方已算好的 `corpus_content_hash`（如
            `start_distill` 的缓存预测里算过一次）。传了就不重算——
            同一条提交链路里「预测算一次 + 后台再算一次」是重复劳动。
        resume: 是否复用 `distill_chunks` 里已完成的块（断点续跑）。
            **只对后台蒸馏任务传 True**；生成路径不传，行为与引入续跑之前一致。
            `force=True` 会顺带清掉本 scope 的块级断点——用户明确要求重算时
            不能拿上一次的半成品拼出新档案。`resume=True` 时不清：续跑必须
            先读到断点，清了就等于整轮重跑。

    Raises:
        ValueError: tier / project.narrative_tier 是未知档位 key。
    """
    ids = [int(i) for i in (project.reference_novel_ids or [])]
    if not ids:
        return None

    cfg_tier = tier
    if cfg_tier is None:
        cfg_tier = getattr(project, "narrative_tier", None)
    cfg = cfg_tier if isinstance(cfg_tier, NarrativeTier) else resolve_tier(cfg_tier)

    # 项目级采样覆盖（助理可在对话里改：200 章 / 全本）。
    # 必须叠在 cfg 上再算指纹——否则「833 → 200 章」会命中旧档案，静默返回旧结果。
    cap = sample_chapters
    if cap is None:
        cap = getattr(project, "narrative_sample_chapters", None)
    if cap is not None and not isinstance(cfg_tier, NarrativeTier):
        cfg = tier_with_sampling(cfg, int(cap))

    want = narrative_fingerprint(ids, cfg)
    cached = project.style_narrative or {}
    profile = None

    if force and not resume:
        # 用户明确要求重算（且不是续跑）：清掉本 scope 的块级断点，否则「重算」会
        # 跳过已完成块，拿上一次的半成品拼出一份看起来全新的档案。
        # resume=True 时**绝不能清**：续跑的全部价值就是复用这些断点，先清再读
        # 等于每次都从零开始。清理只对非 resume 的整轮重跑生效。
        _clear_scope_chunks(
            db, project.id, distill_resume_scope(ids, cfg, max_chapters)
        )

    if not force and isinstance(cached, dict) and cached.get("fingerprint") == want:
        # 命中项目缓存：一次 LLM 都不会调，进度条直接 100%，并把「没重算」写进 stage。
        if progress is not None:
            progress("命中缓存，本次无需重新蒸馏", 1, 1)
        if not persist_library:
            # 生成路径：纯读。不算 content_hash（全量加载语料+sha256）、
            # 不碰蒸馏库——档案没变，重复落库除了刷新 applied_at 没有任何意义。
            return cached
        profile = cached
        # 合并说明（两侧各有一次修复，这里取二者之长）：
        #   - master 侧的顾虑成立：`_persist_style_distill` / `find_style_distill` 内部用
        #     `db.scalar()`，在 FakeDB mock 下会抛 AttributeError；若异常逸出，
        #     `project_narrative_safe` 会把它 catch 成 None，**把已算好的档案丢掉**。
        #   - HEAD 侧的顾虑同样成立：项目缓存命中时若直接 return，全局蒸馏库永远不会
        #     出现这一行（`project.style_narrative` 可能来自旧版本或直接写入），
        #     用户就看不到「蒸馏完成的库」。
        # 所以：不在此 early return，而是 fall through 到下面的统一收尾；并把收尾里的
        # 落库改成**非致命**（见下方 try/except）——档案永远不会因为「写库失败」被丢。
    else:
        if not persist_library:
            # 生成路径：只做「降级或拒绝」，**绝不**在这里同步蒸馏。
            # 以前指纹不匹配会一路走到 extract_narrative_profile（分钟级），
            # 生成请求被静默挂到超时；现在改为复用可用旧档并 WARN，或返回
            # None 让调用方按「不注入」处理。
            # 预检与取档共用 `reusable_narrative`：判据只有一份，避免「预检
            # 要求完全相等、取档已放宽」这种改一份漏一份的分叉。
            profile = reusable_narrative(db, project, ids, cfg, cached=cached)
            if profile is None:
                logging.getLogger(__name__).warning(
                    "生成路径拒绝同步蒸馏：项目 %s 期望 tier=%s（指纹 %s），"
                    "项目缓存与蒸馏库都没有可用档案。请先调用 "
                    "POST /api/projects/%s/distill 完成蒸馏后再生成。",
                    getattr(project, "id", None), cfg.key,
                    want["tier_signature"], getattr(project, "id", None),
                )
            return profile

        # ── 蒸馏库复用（跨项目共享层）── 同语料+同档位+同正文 已蒸馏过 → 直接复用。
        # force 时跳过（force = 用户明确要求重算，不复用任何缓存/库）。
        if not force:
            if content_hash is None:
                content_hash = corpus_content_hash(ids, db)
            existing = find_style_distill(
                db, ids, cfg.key, want["tier_signature"], content_hash
            )
            if existing is not None and isinstance(existing.style_narrative, dict):
                reused = dict(existing.style_narrative)
                reused["fingerprint"] = want
                project.style_narrative = reused
                existing.applied_at = datetime.now(timezone.utc)
                db.commit()
                if progress is not None:
                    progress("命中蒸馏库，直接复用", 1, 1)
                profile = reused
                # 命中的库行已存在，下面统一 upsert 仅刷新 applied_at，无副作用。
        if profile is None:
            # 没命中任何缓存/库（或 force 强制重算）→ 真实提取。
            profile = extract_narrative_profile(
                db, project, ids, max_chapters=max_chapters, tier=cfg, progress=progress,
                heartbeat=heartbeat, resume=resume,
            )
            profile["fingerprint"] = want
            project.style_narrative = profile

    # 四层选条缓存：蒸馏收尾算一次，渲染时只查表（书 7 全量约 10s，
    # 之后单次渲染 ~40ms）。失败不影响档案本身。
    if isinstance(profile, dict):
        ensure_rule_scores(db, profile, ids)
        # 节拍骨架缓存：只对超长节拍做确定性压缩（不调模型），没有超长条目时不写字段。
        ensure_beat_skeletons(db, profile, ids)
        try:
            project.style_narrative = profile
        except Exception:  # noqa: BLE001
            pass

    # —— 统一收尾：任何一次「完成的蒸馏」都把产物落进全局蒸馏库（幂等 upsert）——
    # 「蒸馏完成的加一个库」的核心：无论命中项目缓存、命中蒸馏库、还是真实提取，
    # 只要本次产出了可用档案，全局库里就有它，标着档位，任何项目可直接复用。
    if persist_library:
        # 落库是**增强**，不是产出：写库失败绝不能让已算好的档案被丢掉
        # （外层 `project_narrative_safe` 会把逸出的异常 catch 成 None，等于白蒸一次）。
        # 这条 try/except 同时解决了 master 侧报告的 FakeDB mock 场景。
        try:
            if content_hash is None:
                content_hash = corpus_content_hash(ids, db)
            _persist_style_distill(db, project, ids, cfg, content_hash, profile)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "蒸馏库落库失败（档案仍返回，不丢本次产出）: %s", exc
            )
    return profile


def project_narrative_safe(
    db: Session,
    project,
    *,
    force: bool = False,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
    persist_library: bool = True,
    content_hash: str | None = None,
) -> dict | None:
    """生成链路上的安全包装。与 `style_profile_safe` 同理：失败只能降级为「不注入」，
    绝不能让整次生成失败。

    `progress` 只是透传（生成链路默认不传，即 None）：加这个参数**不改**生成路径
    的任何行为，纯粹为了让后台蒸馏任务能复用同一个函数并拿到进度。
    `persist_library` / `content_hash` 同理透传给 `project_narrative`。
    """
    try:
        return project_narrative(db, project, force=force, max_chapters=max_chapters,
                                 tier=tier, progress=progress,
                                 persist_library=persist_library,
                                 content_hash=content_hash)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "叙事档案不可用，本次生成跳过叙事档案块：%s: %s", type(exc).__name__, exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


#: 声音词典里「整名就是一个职务/称谓」的封闭词表。
#:
#: 为什么不能只靠后缀判断：中文称谓没有稳定的构词后缀。「钟姨」是书里的
#: 唯一指代（真角色），「王爷」「王妃」「国师」却只是身份；两者同形。反过来
#: 「南总」是称呼、也可能是某人的唯一指代。所以这里只做**整名匹配**，
#: 且只收「不能单独指认某个具体人」的词；带修饰的具体称呼由
#: `_is_specific_title` 放行，不在本表管辖内。
_VOICE_ROLE_ONLY = frozenset({
    "公主", "王妃", "国师", "武夫", "司天", "董事长",
    "总裁", "总经理", "董事", "皇帝", "太子", "王爷", "将军", "夫人",
})

#: 地名后缀：只对 2–3 字名生效（4 字以上更可能是复姓全名）。
_PLACE_SUFFIXES = ("州", "城", "府", "县", "镇", "关", "京", "都")

#: 职务后缀：中文商业/官场里「姓 + 职务」的常见写法（南总、李董）。
_OFFICE_SUFFIXES = ("总", "董")

#: 单条角色声音的形态上下限。低于下限基本是「名字 + 一句话」的半成品，
#: 高于上限则是多条规则粘连（LLM 把两段输出拼在一起）。
_VOICE_RULE_MIN_CHARS = 40
_VOICE_RULE_MAX_CHARS = 400

#: 「证据型薄条目」（名字 + 单个声音字段 + 一句原文证据）的字符下限。
#:
#: targeted 配音对配角只提得出这一种形态（柴杏儿 53 字、李灵素 33 字）；
#: 它们有真名和原文出处，不是半句截断。理论最短（名字 + 字段 + 10 字证据）
#: 约 22 字，这里取 30 是为了拦住「（证据：好。）」这类占位，实测最短的
#: 合法证据条目是 33 字。
_VOICE_EVIDENCED_MIN_CHARS = 30

#: 证据正文至少这么长才算真引了原文，否则只是「（证据：无）」。
_VOICE_EVIDENCE_MIN_CHARS = 10

#: 一条角色声音至少要覆盖两个声音字段，否则只是「语气冷静」这类空话。
#: 带真实证据的薄条目降为 1 个字段（见 `_voice_item_reject_reason`）。
_VOICE_FIELDS = ("自称", "口头禅", "句长", "语气", "节奏", "关系动态", "代表台词")

#: A′（2026-11-14）泛称排除词：职务、称谓、群像、种属。
#:
#: 只在「字段不足 + 定向对白证据」这条新放行路径上使用——这些条目此前一律
#: 按「字段不足」剔除，所以本表哪怕误判，也不会改变既有保留集合。
#:
#: 来源与退回（v14，2026-11-15）：此前是代码常量，现降级为配置项
#: `settings.voice_generic_markers`——配置非空时优先用配置，否则用下面这张
#: **退回表**。它**无法由参考书频次分位生成**：频次分不出「职务/群像泛称」与
#: 「高频具体角色」（主角同样高频），也没有可判别的分位信号；按上级口径不硬造，
#: 改为配置化并在 `.trae/documents/写死值审计-2026-11-14.md` 登记一条。
_VOICE_GENERIC_MARKERS_FALLBACK = (
    "大师", "婆婆", "郡主", "女侠", "布政使", "宦官", "密探", "探子", "僧人",
    "道首", "老道", "道长", "妇人", "金刚", "护法", "捕头", "县令", "公主",
    "公公", "贵妃", "阿姨", "和尚", "太妃", "秘书", "皇后", "国舅", "老鸨",
    "王爷", "干尸", "妈妈", "男子", "少爷", "师兄", "族老", "算仙", "战士",
    "尊者", "岛主", "灵皇", "女修", "书生", "邪魔", "真人", "老祖", "魔君",
    "劫仙", "兽修", "仙子", "少女", "天人", "大帝", "上人", "仙姬", "分身",
    "道姑", "金卦", "君主", "前辈", "系统", "道人", "老板娘", "姑娘", "元君",
    "罗汉", "菩萨", "男人", "尚书", "郎中", "经纪", "银锣", "皇子", "御史",
    "长老", "围观者", "小女孩", "校长", "保镖", "狱卒", "婶婶", "二哥", "二叔",
    "吏员", "店小二", "林父", "林母", "莫父", "莫母", "何母", "宋妈妈",
    "监正", "平阳郡主", "飞燕女侠", "守门宦官", "黑袍密探", "黑袍探子",
    "盘树僧人", "大理寺丞", "九尾公主", "绑架男人", "陌生男人", "红毛男人",
    "白领男子", "年轻妈妈", "年轻妇人", "金发女修", "金发少女", "提刀书生",
    "海胆战士", "楚姓族老", "六品天人", "白毛兽修", "胖球分身", "苏氏绳结",
    "修聊系统", "墨家天志", "大霸宋", "善尸", "海王", "虎妖", "邪魔熊", "戏精马",
)

#: 单字泛称后缀：卒、兵、僧、道、婆、父、母、帝、后、妃…… 只用于新放行路径。
_VOICE_GENERIC_SUFFIXES = frozenset(
    "卒兵女男人僧道士婆婶爷叔姨哥姐妹父母官宦后妃帝王娘汉妇童孩者辈"
    "尊仙魔妖兽鬼神佛侠首使监令吏员军长主客生尸号"
)


def _voice_generic_markers() -> tuple[str, ...]:
    """泛称排除词：配置优先，缺省用退回表（v14 起不再是唯一来源）。"""
    configured = getattr(settings, "voice_generic_markers", None)
    if configured:
        cleaned = tuple(str(x).strip() for x in configured if str(x).strip())
        if cleaned:
            return cleaned
    return _VOICE_GENERIC_MARKERS_FALLBACK


def _is_generic_voice_name(name: str) -> bool:
    """名字本身是职务/称谓/群像/种属泛称，而不是某个具体角色的指代。"""
    if not name:
        return True
    if any(marker in name for marker in _voice_generic_markers()):
        return True
    return name[-1] in _VOICE_GENERIC_SUFFIXES


def _has_targeted_dialogue(rule: str) -> bool:
    """证据段有成对引号对白，且证据点名该角色本人。

    A′（2026-11-14）：`_VOICE_FIELDS` 从门槛降为加分项后，字段不足的条目
    靠这条判据放行——既要有引号对白，也要能确认是对白属于这个角色，
    否则「某某：语气冷静（证据：“滚！”）」这类泛称条目会重新混进来。
    """
    name, _, body = rule.partition("：")
    name = name.strip()
    evidence = _voice_evidence_text(body)
    if not name or not evidence:
        return False
    return name in evidence and bool(_VOICE_QUOTE_RE.search(evidence))


def _voice_name_reject_reason(name: str, names: list[str],
                              truncations: frozenset[str] | None = None) -> str | None:
    """角色名是否该被丢掉；返回原因（None = 保留）。

    判据全部可解释，不是黑名单拍脑袋：

    1. `_is_valid_voice_char_name`：2–4 个汉字、纯中文、非伪角色/泛称；
    2. 整名等于职务/称谓词（`_VOICE_ROLE_ONLY`）；
    3. 2–3 字且以地名后缀结尾（云州、洛城）；
    4. 2–3 字且以职务后缀结尾（南总、李董）；
    5. **截断**：与同一份词典里另一个更长的名字构成前缀关系
       （楚元→楚元缜）。后缀同形不判截断——「仙子」「前辈」这类
       书名/称谓本身可能就是独立指代（如「仙子」与「六修仙子」）。
       词典里没有全名时这条抓不到——那需要蒸馏阶段落库的别名映射，
       见 `_normalize_character_name`。
    """
    if not _is_valid_voice_char_name(name):
        return "非法人名"
    if name in _VOICE_ROLE_ONLY:
        return "职务/称谓"
    if len(name) <= 3 and name.endswith(_PLACE_SUFFIXES):
        return "地名"
    if 2 <= len(name) <= 3 and name.endswith(_OFFICE_SUFFIXES):
        return "职务/称谓"
    if truncations is not None:
        if name in truncations:
            return "疑似截断"
        return None
    for other in names:
        if other == name or not (2 <= len(other) <= 4):
            continue
        if other.startswith(name):
            return f"疑似截断（{other}）"
    return None


def _voice_truncation_index(names: list[str]) -> frozenset[str]:
    """一次算好「哪些短名是别的长名的前缀」，避免逐条 O(n²) 比对。

    只收前缀：中文名截断通常丢尾字（楚元→楚元缜、苏氏阿→苏氏阿十六）；
    后缀同形更多是独立称谓（仙子/六修仙子、前辈/白前辈），不能一刀切。
    """
    found: set[str] = set()
    for other in names:
        if not (2 <= len(other) <= 4):
            continue
        for k in (2, 3):
            if k < len(other):
                found.add(other[:k])
    return frozenset(found)


def _voice_names(items: list) -> list[str]:
    return [
        it.get("rule", "").split("：", 1)[0].strip()
        for it in items
        if isinstance(it, dict) and "：" in it.get("rule", "")
    ]


def _evidence_len(body: str) -> int:
    """条目里「（证据：…）」的正文长度；没有证据标记返回 0。"""
    m = re.search(r"证据\s*[:：]\s*(.+?)\s*[）)]?\s*$", body)
    return len(m.group(1).strip()) if m else 0


#: 说话证据里的动作词。用户定的口径：说/道/问/喊/答/笑 任一即可。
_SPEECH_VERBS = ("说", "道", "问", "喊", "答", "笑")

#: 成对引号对白（「」/“”/‘’ 都算）。「代表台词：」也落在这个形态里，
#: 所以宋书航/白前辈/羽柔子这种「自称 + 代表台词」的条目不会被误杀。
_VOICE_QUOTE_RE = re.compile(r"[“「‘][^”」’]{2,}[”」’]")


def _voice_evidence_text(body: str) -> str:
    """取「证据：…」正文；没有证据标记时返回空串。"""
    m = re.search(r"证据\s*[:：]\s*(.+?)\s*[）)]?\s*$", body, re.S)
    return m.group(1).strip() if m else ""


def _has_speaking_evidence(rule: str) -> bool:
    """条目是否带「说话证据」：证据段有说话动作，或条目内有成对引号对白。

    只做读取侧判定，不改已落库条目。没有说话证据的条目不能证明「这个人会说话」，
    在提示词里会退化成外貌/身份描述——按用户口径一律不进提示词（记为
    `non_human`：保留在库里，但不占提示词配额）。
    """
    _, _, body = rule.partition("：")
    evidence = _voice_evidence_text(body)
    if evidence and any(v in evidence for v in _SPEECH_VERBS):
        return True
    return bool(_VOICE_QUOTE_RE.search(rule))


def _voice_item_reject_reason(item, names: list[str],
                              truncations: frozenset[str] | None = None) -> str | None:
    """一条角色声音为什么不该进提示词（None = 保留）。"""
    rule = item.get("rule", "") if isinstance(item, dict) else ""
    if "：" not in rule:
        return "形态不符"
    name, body = rule.split("：", 1)
    reason = _voice_name_reject_reason(name.strip(), names, truncations)
    if reason:
        return reason
    evidence_len = _evidence_len(body)
    has_evidence = evidence_len >= _VOICE_EVIDENCE_MIN_CHARS
    min_chars = _VOICE_EVIDENCED_MIN_CHARS if has_evidence else _VOICE_RULE_MIN_CHARS
    if not (min_chars <= len(rule) <= _VOICE_RULE_MAX_CHARS):
        return "长度越界"
    if rule.count("「") != rule.count("」"):
        return "引号未闭合"
    fields = sum(1 for f in _VOICE_FIELDS if f in body)
    if fields < 2 and not (fields == 1 and has_evidence):
        # A′（2026-11-14）：`_VOICE_FIELDS` 从门槛降为加分项。字段不足不再
        # 直接判死，改看「定向对白证据」：证据里既有成对引号对白，也点名了
        # 这个角色本人。泛称（职务/称谓/群像）即使证据命中也不放行。
        if _has_targeted_dialogue(rule):
            return "泛称" if _is_generic_voice_name(name.strip()) else None
        return "字段不足"
    # 用户口径（2026-11-12）：没有说话证据的条目一律不进提示词。它是
    # 「不会说话的非人角色/泛称」，保留在库里，但不占提示词配额。
    if not _has_speaking_evidence(rule):
        return "无说话证据"
    return None


def audit_voice_items(items: list) -> dict:
    """按判据统计角色声音条目的保留/剔除情况（供测试与人工核对）。"""
    names = _voice_names(items)
    truncations = _voice_truncation_index(names)
    report = {"total": len(items), "kept": 0, "rejected": {}}
    for it in items:
        reason = _voice_item_reject_reason(it, names, truncations)
        if reason:
            report["rejected"][reason] = report["rejected"].get(reason, 0) + 1
        else:
            report["kept"] += 1
    return report


def clean_voice_items(items: list) -> list:
    """剔除职务词/地名/截断名与形态不完整的角色声音条目（保持原顺序）。"""
    names = _voice_names(items)
    truncations = _voice_truncation_index(names)
    return [it for it in items if _voice_item_reject_reason(it, names, truncations) is None]



def filter_rules_by_source(profile: dict | None,
                           allowed_novel_ids: set[int] | list[int] | None) -> dict | None:
    """只留下 `novel_id` 在允许集合内的规则；无来源字段的条目按保留处理。

    续写项目只应看到续写书自己的角色口吻；原创项目按当前参考书集合取。
    禁忌词是纯字符串、没有来源，不做过滤。
    """
    if not profile or not allowed_novel_ids:
        return profile
    allowed = {int(x) for x in allowed_novel_ids}

    def keep(item) -> bool:
        if not isinstance(item, dict):
            return True
        nid = item.get("novel_id")
        return nid is None or int(nid) in allowed

    rules = {
        dim: [it for it in (items or []) if keep(it)]
        for dim, items in (profile.get("rules") or {}).items()
    }
    return {**profile, "rules": rules}


#: 首段降权的「压缩倾向」词。参考语料的后期趋势常是「越写越短」，开篇若照着
#: 「句式逐渐精简 / 减少细节 / 结构紧凑 / 短段推进」写，会把第一节写成梗概。
_OPENING_COMPRESSION_MARKERS = (
    "精简", "减少细节", "紧凑", "短段", "简洁", "压缩", "削减", "简略", "概括",
)

#: 首段渲染时插在叙事档案最前的优先级说明。
_OPENING_LENGTH_PRIORITY = (
    "【篇幅优先】本节是开篇：篇幅与完整性优先于简洁倾向，不要把本节写成梗概或短摘要。"
)


def _drop_compression_trends(rules: dict) -> dict:
    """首段：剔除「文风漂移分析」里指向压缩的趋势句子（档案本身不改）。"""
    items = rules.get("文风漂移分析") or []
    kept = []
    for item in items:
        text = item.get("rule", "") if isinstance(item, dict) else str(item)
        if not text:
            kept.append(item)
            continue
        # 只切掉压缩类的句子，不整条丢：同一条漂移里常同时有「对白变多」这类
        # 仍然有用的观察，整条剔除会把整本书的漂移结论删空。
        sentences = re.split(r"(?<=[。；！？])", text)
        kept_sentences = [
            s for s in sentences
            if not any(marker in s for marker in _OPENING_COMPRESSION_MARKERS)
        ]
        cleaned = "".join(kept_sentences).strip()
        if not cleaned:
            continue
        kept.append({**item, "rule": cleaned} if isinstance(item, dict) else cleaned)
    return {**rules, "文风漂移分析": kept}


#: 去近重复用的标点/空白表（只保留文字与数字，避免标点差异骗过去重）。
_DEDUPE_STRIP_RE = re.compile(r"[\s，。、；：！？“”‘’\"'（）()【】\[\]—…·,.;:!?~～]+")


def _rule_text(item) -> str:
    return item.get("rule", "") if isinstance(item, dict) else str(item)


#: 反向节奏判定：不再按字面词表命中，只看规则里给出的**数值**是否与档案分位相反。
#:
#: 判定口径（`audit` = `_parse_audit_metrics(profile)`，档案缺该指标 → 不判）：
#:   - 句长类：规则声称 c 字，档案中位 a → c < 0.6a 或 c > 1.8a 判矛盾；
#:   - 占比类：c 为百分数 → c > max(2a, a+20) 或 c < max(0, a/2-10) 判矛盾。
#: 只解析「证据：」之前的正文（证据原文里的数字不参与判定）；命中只删该分句。
#: 一条数字都没有的规则不判——没有矛盾证据就原样保留（退回值 = 不删）。
_PACING_CLAIMS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"短句\s*[(（]\s*[≤<]\s*15[^)）]{0,4}[)）][^。；，]{0,6}?(\d+(?:\.\d+)?)\s*[%％]"
    ), "短句15占比"),
    (re.compile(
        r"短句(?!\s*[(（]?\s*[≤<]\s*15)[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*[%％]"
    ), "短句占比"),
    (re.compile(r"长句[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*[%％]"), "长句占比"),
    (re.compile(r"短段[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*[%％]"), "短段占比"),
    (re.compile(r"对白开头段?[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*[%％]"), "对白开头段占比"),
    (re.compile(r"短对白[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*[%％]"), "短对白占比"),
    (re.compile(r"(?<!短)对白[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*[%％]"), "对白占比"),
    (re.compile(r"句长[^。；，]{0,8}?(\d+(?:\.\d+)?)\s*字"), "句长"),
)

#: 「证据：」之后是原文引用，里面的数字不是规则主张，不能参与判定。
_EVIDENCE_SPLIT = re.compile(r"证据\s*[：:]")


def _pacing_claims_conflict(text: str, audit: dict[str, float]) -> bool:
    """规则正文里的数值是否与档案分位相反（无数字 / 无该指标 → False）。"""
    head = _EVIDENCE_SPLIT.split(text or "", maxsplit=1)[0]
    for pattern, metric in _PACING_CLAIMS:
        a = audit.get(metric)
        if not isinstance(a, (int, float)) or a <= 0:
            continue
        for m in pattern.finditer(head):
            c = float(m.group(1))
            if metric == "句长":
                if c < 0.6 * a or c > 1.8 * a:
                    return True
            elif c > max(2 * a, a + 20) or c < max(0.0, a / 2 - 10):
                return True
    return False


#: 指标名 → 档案字段。只认**指标名**（档案与闸门里本来就叫这些名字），不再列举
#: 「一两句短段」「大量使用短段落」这类具体措辞；是否矛盾一律由档案值决定。
_PACING_METRIC_NAMES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"极短(?:纯)?对白"), "极短纯对白段占比"),
    (re.compile(r"(?:叙述)?单句成段|单句段"), "叙述单句段占比"),
)

#: 档案里该指标 ≤2% 视为「基本不出现」。此时任何建议使用它的规则，都是在建议
#: 做参考书自己几乎不做的事——按与档案自身统计矛盾判掉。
_PACING_NEAR_ZERO = 0.02


def _archive_pacing_bound(metric: str) -> float | None:
    """档案该指标的章级上界：`GEN_GATE_LIMITS` 由档案章级分位推出，缺失返回 None。"""
    from app.style.consistency import GEN_GATE_LIMITS

    row = GEN_GATE_LIMITS.get(metric)
    if not isinstance(row, dict):
        return None
    val = row.get("max")
    return float(val) if isinstance(val, (int, float)) else None


def _pacing_metric_conflict(text: str, audit: dict[str, float]) -> bool:
    """无数字的祈使句是否建议了档案里「基本不出现」的指标。

    与 `_pacing_claims_conflict` 分工：带百分数的分句有具体数值可比，走数值判定；
    这里只处理「建议使用极短对白/单句成段」这种没有数字的祈使句——拿档案自己的
    实测上界（`GEN_GATE_LIMITS`，非手拍值）判：上界 ≤ `_PACING_NEAR_ZERO` 才算
    矛盾，档案本来就常用该指标时规则保留。
    """
    head = _EVIDENCE_SPLIT.split(text or "", maxsplit=1)[0]
    if re.search(r"\d+(?:\.\d+)?\s*[%％]", head):
        return False
    for pattern, metric in _PACING_METRIC_NAMES:
        if not pattern.search(head):
            continue
        bound = _archive_pacing_bound(metric)
        if bound is not None and bound <= _PACING_NEAR_ZERO:
            return True
    return False


def _drop_reverse_pacing_rules(
    rules: dict, audit: dict[str, float] | None = None,
) -> dict:
    """全程：剔除与档案自身统计**矛盾**的节奏分句（档案本身不改）。

    旧实现按 `_REVERSE_PACING_MARKERS` 字面命中删句——换一本书就会漏配（新书用
    别的措辞）或误删（同一措辞在新书里是正确指导）。现在两条判据都只看档案：

    1. 带数的分句：句长/占比落在档案分位反方向（`_pacing_claims_conflict`）；
    2. 不带数的祈使句：建议使用档案里「基本不出现」的指标
       （`_pacing_metric_conflict`，指标名 → `GEN_GATE_LIMITS` 的档案上界）。

    同一条里正常的观察原样保留。`audit` 为空时第 1 条不判，第 2 条照常生效
    （它只依赖档案上界，不依赖本档的量化审计块）。
    `文笔量化审计` 是硬锚不是规则，整维原样透传（其中短段锚点由
    `_render_audit_block(include_short_para=False)` 单独摘掉）。
    """
    out: dict = {}
    for dim, items in (rules or {}).items():
        if dim == "文笔量化审计":
            out[dim] = items
            continue
        kept = []
        for item in items or []:
            text = _rule_text(item)
            if not text:
                kept.append(item)
                continue
            # 只切「证据：」之前的规则正文：证据是原文引用，按句号切开再删句会
            # 留下「”）」「不过，你的墓碑…。）」这类残片，它们仍算一条规则、还会
            # 参与选条。正文删空则整条丢掉，证据原样跟随保留的正文。
            head = _EVIDENCE_SPLIT.split(text, maxsplit=1)[0]
            if not head.strip():
                kept.append(item)
                continue
            sentences = re.split(r"(?<=[。；！？])", head)
            kept_sentences = [
                s for s in sentences
                if not _pacing_metric_conflict(s, audit or {})
                and not (audit and _pacing_claims_conflict(s, audit))
            ]
            cleaned = "".join(kept_sentences).strip()
            if not cleaned:
                continue
            cleaned += text[len(head):]
            if cleaned == text:
                kept.append(item)
            else:
                kept.append(
                    {**item, "rule": cleaned} if isinstance(item, dict) else cleaned
                )
        out[dim] = kept
    return out


def _rule_dedupe_key(item) -> str:
    """近重复判定键：去标点后取前 30 字。"""
    return _DEDUPE_STRIP_RE.sub("", _rule_text(item))[:30]


def _rule_quality(item) -> tuple[int, int, int]:
    """证据质量：有成对引号 > 证据更长 > 规则更完整。"""
    text = _rule_text(item)
    has_quote = 1 if re.search(r"[“\"「][^”\"」]{2,}[”\"」]", text) else 0
    evid = 0
    m = re.search(r"证据[：:]\s*(.+)$", text, re.S)
    if m:
        evid = len(m.group(1).strip())
    return has_quote, evid, min(len(text), 160)


def _select_rules_by_chapter(items: list, limit: int) -> list:
    """按章号分层选条：把全书切成 `limit` 个区间，每区间挑证据最好的一条。

    旧行为是 `items[:limit]`——按落库顺序取，结果 60 条里 57 条来自前 796 章。
    分层后每条规则落在不同的章段上，维度内规则铺满全书；同一维度内先去近重复，
    避免 6 条结构性重复占掉配额。
    """
    if limit <= 0 or not items:
        return []
    entries = []
    for idx, it in enumerate(items):
        ch = it.get("chapter_no") if isinstance(it, dict) else None
        entries.append((idx, it, ch if isinstance(ch, int) and ch > 0 else None))
    positioned = [e for e in entries if e[2] is not None]
    # 章号覆盖不足（老档案/辅助条目）时保持原顺序，避免选条逻辑把内容选空。
    if len(positioned) < max(4, limit):
        return items[:limit]

    lo = min(e[2] for e in positioned)
    hi = max(e[2] for e in positioned)
    span = max(hi - lo + 1, 1)
    n = min(limit, len(positioned))
    seen: set[str] = set()
    picked: list[tuple[int, object, int]] = []

    def _best(cands, allow_dup=False):
        ranked = sorted(
            cands,
            key=lambda e: (
                -_rule_quality(e[1])[0],
                -_rule_quality(e[1])[1],
                -_rule_quality(e[1])[2],
                e[2],
                e[0],
            ),
        )
        for e in ranked:
            key = _rule_dedupe_key(e[1])
            if not allow_dup and key in seen:
                continue
            if key:
                seen.add(key)
            return e
        return None

    for b in range(n):
        b_lo = lo + span * b // n
        b_hi = lo + span * (b + 1) // n - 1
        if b == n - 1:
            b_hi = hi
        cands = [e for e in positioned if b_lo <= e[2] <= b_hi]
        chosen = _best(cands) if cands else None
        if chosen is None:
            # 该区间没有条目（或都被去重掉）：从全局按证据质量补一条。
            chosen = _best(positioned)
        if chosen is not None:
            picked.append(chosen)

    picked.sort(key=lambda e: (e[2], e[0]))
    return [e[1] for e in picked]


# ====================================================================
# 选条四层：硬过滤 → 四维打分 → 配额内 MMR → 生成后反馈
# ====================================================================
#
# ## 为什么需要第四层
#
# 前三层保证「选进来的单条更硬」；第四层负责让档案学会「这条模型到底用不用得上」。
# 反馈权重按**项目**落库（`narrative_rule_weights`），不是内存里的临时系数。
#
# ## 性能约定
#
# 渲染时只做「查缓存 + 硬过滤 + 小池 MMR」，打分本身（尤其对照组相似度）
# 由 `build_rule_score_cache` 离线算好、写进档案顶层 `rule_scores`。
# 缓存缺失时退化为不跑对照组（discriminability=0.5），并在统计里标记。

RULE_SCORE_VERSION = 3

#: 硬过滤/降级标记位（存进缓存时压成一个整数）。
FLAG_NO_EVIDENCE = 1
FLAG_NO_CHAPTER = 2
FLAG_TOO_LONG = 4
FLAG_VAGUE = 8
FLAG_NON_HUMAN = 16
FLAG_TRUNCATED = 32
FLAG_GENERIC = 64
FLAG_VERIFY_MISMATCH = 128
FLAG_VERIFIED = 256

_HARD_FILTER_MASK = (
    FLAG_NO_EVIDENCE | FLAG_NO_CHAPTER | FLAG_TOO_LONG | FLAG_VAGUE
    | FLAG_NON_HUMAN | FLAG_TRUNCATED | FLAG_GENERIC
)

_FLAG_REASONS: tuple[tuple[int, str], ...] = (
    (FLAG_NO_EVIDENCE, "无证据（规则里没有「证据：」原文）"),
    (FLAG_NO_CHAPTER, "无章号（无法证明这条在书里真实出现）"),
    (FLAG_TOO_LONG, "规则过长"),
    (FLAG_VAGUE, "含模糊词（可以/有时/或许/往往）"),
    (FLAG_NON_HUMAN, "牵涉非人角色或无说话证据"),
    (FLAG_TRUNCATED, "牵涉截断名/伪角色"),
    (FLAG_GENERIC, "三本书通用套话（书 5、书 6 同样成立）"),
    (FLAG_VERIFY_MISMATCH, "量化对账不通过"),
)


@dataclass(frozen=True)
class RuleScoringConfig:
    """四层选条的阈值与权重。改这里必须同时 +1 `RULE_SCORE_VERSION`。"""

    # ── 第一层 硬过滤 ──
    max_rule_chars: int = 280
    min_evidence_chars: int = 8
    vague_markers: tuple[str, ...] = ("可以", "有时", "或许", "往往")
    near_dup_similarity: float = 0.80

    # ── 第二层 四维权重（顺序：可执行 / 区分 / 证据 / 可验证）──
    weights: tuple[float, float, float, float] = (0.35, 0.28, 0.22, 0.15)
    #: 与对照书（5、6）的 2-gram 内容相似度；两本都 ≥ 该值 = 通用套话。
    #: 阈值来自实测分布：书 7 规则对两本对照书的中位相似度约 0.27，
    #: 0.45 只留下「换词也还是同一句话」的 top 一档。
    control_similarity: float = 0.45

    # ── 第三层 MMR ──
    mmr_lambda: float = 0.72
    mmr_pool_factor: int = 6
    coverage_bonus: float = 0.08

    # ── 聚类（证据章号散布）──
    spread_similarity: float = 0.60

    # ── 第四层 反馈 ──
    feedback_hit_step: float = 0.05
    feedback_miss_step: float = 0.10
    weight_min: float = 0.2
    weight_max: float = 2.0


DEFAULT_RULE_SCORING = RuleScoringConfig()

_EVIDENCE_RE = re.compile(r"证据[：:]\s*(.+)$", re.S)
_PUNCT_RE = re.compile(r"[\s，。、；：！？“”‘’\"'（）()【】\[\]—…·,.;:!?~～]+")


def _rule_body(item) -> str:
    """去掉「证据：…」尾巴的规则正文（判可执行性/区分度用）。"""
    text = _rule_text(item)
    return _EVIDENCE_RE.sub("", text).strip()


def _rule_evidence(item) -> str:
    m = _EVIDENCE_RE.search(_rule_text(item))
    return m.group(1).strip() if m else ""


def _evidence_long_enough(rule: str) -> bool:
    """落库前的通用长度门槛：证据不足 20 字一律不落库。

    主提取路径走 `_validate_evidence`（长度 + 原文包含双重校验）；节拍 / 场景模板 /
    潜台词这三类结构化规则没有走那条路，但同样受「证据 20–60 字」约束，这里补齐。
    """
    return len(_rule_evidence(rule).rstrip("）) ")) >= EVIDENCE_MIN_CHARS


def _norm_evidence(s: str) -> str:
    """证据定位用的归一化：去空白/标点/引号差异，只比字。"""
    return _PUNCT_RE.sub("", s or "")


#: 多段引文的分隔：旧档案里常见「原文写“A”又写“B”」这种拼接证据。
_QUOTE_SPLIT_RE = re.compile(r"[“”‘’「」『』【】\"]+")
#: 证据按句切分的边界。归一化前先切，再逐句去标点匹配。
_EVIDENCE_SENTENCE_RE = re.compile(r"[。！？；!?;]+")
#: 证据「核心」的最短长度：证据开头几个字被模型改写（补主语、换称呼）时，
#: 跳过开头 1–6 字再匹配；14 字连续原文在章内的区分度足够，不会锚错块。
_EVIDENCE_CORE_MIN_CHARS = 14
_EVIDENCE_JOIN_RE = re.compile(
    r"(?:原文写|原文是|原文|又写|并写|以及|叙述接|前面紧接|紧接着|后接|"
    r"—{2,}|／|/|\|)+"
)


def _evidence_segments(evidence: str) -> list[str]:
    """把拼接证据切成可单独定位的引文段，长的在前。

    新蒸馏要求证据是**一段连续原文**，正常不会走到这里；但 LLM 偶尔仍会输出
    「引文 A + 转述 + 引文 B」，整串匹配必然失败。切段后只认长度 ≥12 字的段，
    并保证每个段仍是原文的子串——宁可锚到其中一段，也不写一个假 chunk_id。
    """
    parts: list[str] = []
    for chunk in _QUOTE_SPLIT_RE.split(evidence or ""):
        parts.extend(_EVIDENCE_JOIN_RE.split(chunk))
    seen: set[str] = set()
    out: list[str] = []
    for part in sorted((_norm_evidence(p) for p in parts), key=len, reverse=True):
        if len(part) >= 12 and part not in seen:
            seen.add(part)
            out.append(part)
    return out


def _resolve_evidence_anchor(
    evidence: str,
    block_rows: list[tuple[int, int, str]],
    chapter_norm: list[tuple[int, str]],
) -> tuple[int | None, int | None]:
    """定位证据落在哪个真实块、哪一章。

    2026-11-12：档案里必须写 `chunk_id`（真实 `novel_chunks.id`）和
    `evidence_chapter_no`（证据实际所在章）。旧档案把 `chapter_no` 写成
    「蒸馏块的起始章」，导致证据最多偏 4 章，事后只能靠 5 章窗口硬找。

    Args:
        evidence: `证据：` 后面的原文引文。
        block_rows: `[(novel_chunks.id, chapter_no, 归一化文本)]`，按 (章, idx) 排好。
        chapter_norm: `[(chapter_no, 归一化整章文本)]`，证据跨块时兜底拿章号。

    Returns:
        `(novel_chunk_id, chapter_no)`；块级定位不到但章级能定位时 id 为 None；
        都定位不到返回 `(None, None)`。
    """
    ev = _norm_evidence(evidence)
    if len(ev) < 8:
        return None, None
    # 候选顺序：整串 → 引号/转述切段 → **按句切分**。
    # 最后一步专治「模型把相邻两句拼成一句证据」：去标点归一化救不了拼接
    # （两个原句中间没有可对齐的边界），逐句重试才能命中其中一句。
    candidates = [ev] + [s for s in _evidence_segments(evidence) if s != ev]
    for sentence in _EVIDENCE_SENTENCE_RE.split(evidence):
        sent = _norm_evidence(sentence)
        # 少于 10 字的句子区分度太低（「他说。」这种能命中半本书），不单独作为候选。
        if len(sent) >= 10 and sent not in candidates:
            candidates.append(sent)
    for cand in candidates:
        for cid, chno, ntext in block_rows:
            if cand in ntext:
                return cid, chno
    for cand in candidates:
        for chno, ntext in chapter_norm:
            if cand in ntext:
                return None, chno
    # 最后一档：证据开头几个字被改写过（补主语 / 换称呼），整串和整句都对不上，
    # 但跳过后半段仍是原文。只跳过开头、不动结尾——结尾被截断时通常是模型
    # 把两句连写，那种情况上一档的逐句切分已经处理。
    cores: list[str] = []
    for cand in candidates:
        for skip in range(1, 7):
            core = cand[skip:]
            if len(core) >= _EVIDENCE_CORE_MIN_CHARS and core not in cores:
                cores.append(core)
    for core in cores:
        for cid, chno, ntext in block_rows:
            if core in ntext:
                return cid, chno
    for core in cores:
        for chno, ntext in chapter_norm:
            if core in ntext:
                return None, chno
    return None, None


def _load_block_chunk_rows(
    db: Session, novel_id: int, chapter_nos: list[int]
) -> tuple[list[tuple[int, int, str]], list[tuple[int, str]]]:
    """取本蒸馏块的 `novel_chunks` 行与整章文本，供证据定位。

    只在本块处理时查一次：块内每条规则复用，不按规则发查询。
    """
    from app.models import NovelChunk

    rows = db.scalars(
        select(NovelChunk)
        .where(
            NovelChunk.novel_id == novel_id,
            NovelChunk.chapter_no.in_(chapter_nos),
        )
        .order_by(NovelChunk.chapter_no, NovelChunk.idx, NovelChunk.id)
    ).all()
    block_rows = [
        (int(r.id), int(r.chapter_no), _norm_evidence(r.text or "")) for r in rows
    ]
    by_chapter: dict[int, list[str]] = {}
    for r in rows:
        by_chapter.setdefault(int(r.chapter_no), []).append(r.text or "")
    chapter_norm = [
        (ch, _norm_evidence("".join(parts))) for ch, parts in by_chapter.items()
    ]
    return block_rows, chapter_norm


def _rule_score_key(item, dim: str) -> str:
    raw = f"{dim}\x00{_rule_text(item)}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:20]


def _rule_text_hash(item) -> str:
    return hashlib.sha1(_rule_text(item).encode("utf-8")).hexdigest()[:20]


#: 相似度归一化时剔掉的高频虚词/框架词。保留「短句/对白/段落/动作」这类
#: 真正承载规则内容的词，避免「人物」「用」把任何两条规则都拉到一起。
_STOP_CHARS = frozenset(
    "的地得了着是在用通过让人物角色对话句子段落写作作者本书全文规则证据"
    "时和与及或而为以对把将被从到于之其这那一个种些里中上下后前再又也都很"
    "更最可要会能需应该就还只"
)


def _grams(text: str, n: int = 2) -> frozenset[str]:
    """规则内容的 n-gram 集合（默认 2-gram + 去虚词）。

    3-gram 对中文改写太脆：同一手法换两个词就几乎零重叠（实测中位 0.15）。
    2-gram 去虚词后中位 0.27，能区分「大致同题」与「换词复述」。
    """
    clean = "".join(
        ch for ch in _PUNCT_RE.sub("", text) if ch not in _STOP_CHARS
    )
    if len(clean) <= n:
        return frozenset([clean]) if clean else frozenset()
    return frozenset(clean[i:i + n] for i in range(len(clean) - n + 1))


def _containment(a: frozenset[str], b: frozenset[str]) -> float:
    """包含度：短的一侧有多少比例出现在长的里（对证据长度差异稳健）。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / min(len(a), len(b))


class _GramIndex:
    """3-gram 倒排索引：为一条新规则找最相似的对照规则。"""

    def __init__(self, texts: list[str]):
        self.postings: dict[str, list[int]] = {}
        self.grams: list[frozenset[str]] = []
        for i, text in enumerate(texts):
            g = _grams(_rule_body(text) or text)
            self.grams.append(g)
            for gram in g:
                self.postings.setdefault(gram, []).append(i)

    def best_containment(self, grams: frozenset[str]) -> float:
        if not grams or not self.postings:
            return 0.0
        counts: dict[int, int] = {}
        # 只查出现次数最少的 12 个 gram：常见 gram 的倒排太长，收益低。
        for gram in sorted(grams, key=lambda g: len(self.postings.get(g, ())))[:12]:
            for idx in self.postings.get(gram, ()):
                counts[idx] = counts.get(idx, 0) + 1
        best = 0.0
        for idx, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:8]:
            best = max(best, _containment(grams, self.grams[idx]))
            if best >= 0.95:
                break
        return best


def _cluster_deciles(items: list, dim: str,
                     config: RuleScoringConfig = DEFAULT_RULE_SCORING) -> dict[int, int]:
    """把同维度近义规则聚成簇，返回「每条规则所属簇覆盖了几个十分位」。

    证据散布不是单条规则自己的章号（一条只有一个章号），而是**同一说法在全书
    重复出现的跨度**。书 7 的 3,172 章切成 10 个十分位，簇覆盖 ≥10 个说明这个
    写法贯穿全书；只在 2 个十分位出现说明是局部桥段。
    """
    chapters = [
        it.get("chapter_no") if isinstance(it, dict) else None for it in items
    ]
    valid = [c for c in chapters if isinstance(c, int) and c > 0]
    if not valid:
        return {}
    lo, hi = min(valid), max(valid)
    span = max(hi - lo + 1, 1)
    reps: list[tuple[frozenset[str], set[int]]] = []
    index: dict[str, list[int]] = {}
    out: dict[int, int] = {}

    for pos, item in enumerate(items):
        ch = chapters[pos]
        grams = _grams(_rule_body(item) or _rule_text(item))
        if not grams:
            out[pos] = 1
            continue
        counts: dict[int, int] = {}
        for gram in sorted(grams, key=lambda g: len(index.get(g, ())))[:10]:
            for cid in index.get(gram, ()):
                counts[cid] = counts.get(cid, 0) + 1
        best_id, best_sim = None, 0.0
        for cid, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
            sim = _containment(grams, reps[cid][0])
            if sim > best_sim:
                best_id, best_sim = cid, sim
        if best_id is not None and best_sim >= config.spread_similarity:
            reps[best_id][1].add((ch - lo) * 10 // span if isinstance(ch, int) and ch > 0 else -1)
            for gram in grams:
                index.setdefault(gram, []).append(best_id)
            out[pos] = best_id
        else:
            cid = len(reps)
            reps.append((grams, {(ch - lo) * 10 // span if isinstance(ch, int) and ch > 0 else -1}))
            for gram in grams:
                index.setdefault(gram, []).append(cid)
            out[pos] = cid

    deciles = {cid: len({d for d in ds if d >= 0}) for cid, (_, ds) in enumerate(reps)}
    return {pos: max(1, deciles[cid]) for pos, cid in out.items()}


def _parse_audit_metrics(profile: dict) -> dict[str, float]:
    """从「文笔量化审计」条目里取全书分块实测的中位数。

    这些条目每块一条（book 7 = 635 条），中位数代表全书常态，用来对账
    「规则里声称的数字」。没有任何条目时返回空 dict（可验证性按 0.5 中性处理）。
    """
    items = ((profile or {}).get("rules") or {}).get("文笔量化审计") or []
    patterns = {
        "句长": r"句长\s*([\d.]+)\s*(?:±|\+|字)",
        "短句占比": r"短句(?:\(≤5字\))?占\s*([\d.]+)\s*%",
        "短句15占比": r"短句\(≤15字\)占\s*([\d.]+)\s*%",
        "长句占比": r"长句占\s*([\d.]+)\s*%",
        "短段占比": r"一两句短段占\s*([\d.]+)\s*%",
        "对白占比": r"对白占正文\s*([\d.]+)\s*%",
        "对白开头段占比": r"对白开头段占\s*([\d.]+)\s*%",
        "短对白占比": r"短对白占\s*([\d.]+)\s*%",
        "情绪直陈": r"情绪直陈\s*([\d.]+)\s*/千字",
        "动作承载": r"动作承载\s*([\d.]+)\s*/千字",
        "问号": r"问号\s*([\d.]+)\s*/千字",
        "省略号": r"省略号\s*([\d.]+)\s*/千字",
    }
    buckets: dict[str, list[float]] = {k: [] for k in patterns}
    for item in items:
        text = _rule_text(item)
        for key, pattern in patterns.items():
            m = re.search(pattern, text)
            if m:
                buckets[key].append(float(m.group(1)))
    return {k: statistics.median(v) for k, v in buckets.items() if v}


def _numeric_claims(text: str) -> list[dict]:
    """规则正文里对量化审计字段的声称。只认能对账的字段。"""
    patterns = (
        ("句长", r"句长\s*([\d.]+)\s*字"),
        ("句长", r"平均\s*([\d.]+)\s*字"),
        ("短句占比", r"短句(?:\(≤5字\))?占(?:比)?\s*([\d.]+)\s*%"),
        ("短句15占比", r"短句\(≤15字\)占(?:比)?\s*([\d.]+)\s*%"),
        ("长句占比", r"长句占(?:比)?\s*([\d.]+)\s*%"),
        ("对白占比", r"(?<!短)对白(?:字)?占(?:正文|比)?\s*([\d.]+)\s*%"),
        ("段均句数", r"段均句数\s*([\d.]+)"),
        ("动作承载", r"动作承载\s*([\d.]+)\s*/?千字"),
    )
    claims = []
    for metric, pattern in patterns:
        for m in re.finditer(pattern, text):
            claims.append({"metric": metric, "value": float(m.group(1)),
                           "raw": m.group(0)})
    return claims


def _claim_tolerance(metric: str, value: float) -> float:
    if metric in ("短句占比", "短句15占比", "长句占比", "对白占比"):
        return max(3.0, abs(value) * 0.2)
    if metric == "句长":
        return 2.5
    if metric == "段均句数":
        return 0.35
    return max(0.5, abs(value) * 0.25)


def _verify_rule(item, audit: dict[str, float]) -> tuple[float, int, str | None]:
    """可验证性：有数字声称就必须真对账；对不上降级并记原因。"""
    text = _rule_text(item)
    claims = _numeric_claims(text)
    if claims:
        if not audit:
            return 0.5, 0, "无量化审计数据可对账，按中性处理"
        checked = []
        for claim in claims:
            actual = audit.get(claim["metric"])
            if actual is None:
                continue
            ok = abs(claim["value"] - actual) <= _claim_tolerance(claim["metric"], claim["value"])
            checked.append((claim, actual, ok))
        if not checked:
            return 0.5, 0, "声称的数字不在审计字段内，无法核对"
        bad = [(c, a) for c, a, ok in checked if not ok]
        if bad:
            claim, actual = bad[0]
            return 0.05, FLAG_VERIFY_MISMATCH, (
                f"降级：声称{claim['metric']} {claim['value']:g}，"
                f"审计实测 {actual:g}，对不上"
            )
        claim, actual, _ = checked[0]
        return 0.95, FLAG_VERIFIED, (
            f"核对通过：{claim['metric']} {claim['value']:g} ≈ 审计 {actual:g}"
        )
    # 没有数字，但提到可量化审计的维度 → 部分可验证
    if any(k in text for k in ("句长", "对白", "段落", "段长", "动作", "标点", "短句", "长句")):
        return 0.55, 0, None
    return 0.25, 0, None


_IMPERATIVE_RE = re.compile(
    r"(^|[。；，])(不要|不得|避免|必须|务必|应|须|优先|尽量|用|以|通过|保持|控制|"
    r"增加|减少|省略|插入|采用|选择|把|将|让|先把|直接)"
)
_CONCRETE_MARKERS = (
    "句长", "字数", "占比", "比例", "对白", "短句", "长句", "段落", "段长", "标点",
    "称谓", "自称", "口头禅", "用词", "场景", "动作", "语气", "节奏",
)
_VAGUE_ADJECTIVES = ("生动", "流畅", "自然", "细腻", "丰富", "有张力", "感染力",
                     "恰到好处", "巧妙", "精彩")


def _execution_score(text: str) -> float:
    """可执行性：祈使句式 + 具体参数加分；只有形容词的「语言生动」低分。"""
    body = _rule_body({"rule": text} if isinstance(text, str) else text)
    score = 0.35
    if _IMPERATIVE_RE.search(body):
        score += 0.2
    params = 0
    if re.search(r"\d+(\.\d+)?\s*(%|字|句|段|次|个|/千字)", body):
        params += 1
    if any(k in body for k in _CONCRETE_MARKERS):
        params += 1
    score += min(params, 2) * 0.15
    if params == 0 and any(w in body for w in _VAGUE_ADJECTIVES):
        score -= 0.2
    if body.startswith(("人物", "作者", "本书", "文中", "全书", "角色")) and params == 0:
        score -= 0.1
    return max(0.0, min(1.0, score))


def _evidence_score(item, deciles: int) -> float:
    """证据强度：引号对白证据 + 说法在全书十分位上的散布。"""
    evidence = _rule_evidence(item)
    has_quote = bool(re.search(r"[“\"「][^”\"」]{2,}[”\"」]", evidence or _rule_text(item)))
    quote_part = 1.0 if has_quote else 0.25
    spread_part = min(max(deciles, 1) / 10.0, 1.0)
    return 0.45 * quote_part + 0.55 * spread_part


def _distinctiveness(text: str, indexes: dict[int, _GramIndex] | None,
                     config: RuleScoringConfig = DEFAULT_RULE_SCORING) -> tuple[float, bool]:
    """区分度：与对照书（5、6）逐本比，两本都成立 = 通用套话。"""
    if not indexes:
        return 0.5, False
    grams = _grams(_rule_body({"rule": text}) if isinstance(text, str) else text)
    sims = [idx.best_containment(grams) for idx in indexes.values()]
    if not sims:
        return 0.5, False
    generic = min(sims) >= config.control_similarity
    return max(0.05, 1.0 - max(sims)), generic


def _score_rule(item, dim: str, *, deciles: int, audit: dict[str, float],
                indexes: dict[int, _GramIndex] | None,
                config: RuleScoringConfig = DEFAULT_RULE_SCORING) -> dict:
    """给一条规则打四维分（0–1），并给出硬过滤标记。"""
    text = _rule_text(item)
    evidence = _rule_evidence(item)
    flags = 0
    if len(_PUNCT_RE.sub("", text)) > config.max_rule_chars:
        flags |= FLAG_TOO_LONG
    if len(_PUNCT_RE.sub("", evidence)) < config.min_evidence_chars:
        flags |= FLAG_NO_EVIDENCE
    ch = item.get("chapter_no") if isinstance(item, dict) else None
    if not isinstance(ch, int) or ch <= 0:
        flags |= FLAG_NO_CHAPTER
    if any(w in text for w in config.vague_markers):
        flags |= FLAG_VAGUE
    if dim == CHARACTER_VOICE_DIMENSION:
        if _voice_item_reject_reason(item, _voice_names([item])) is not None:
            flags |= FLAG_NON_HUMAN
        name = _voice_item_name(item)
        if name and re.search(r"[A-Za-z]|[0-9]|的|了|是|在", name):
            flags |= FLAG_TRUNCATED

    execution = _execution_score(text)
    distinct, generic = _distinctiveness(text, indexes, config)
    if generic:
        flags |= FLAG_GENERIC
    evidence_score = _evidence_score(item, deciles)
    verifiable, vflag, note = _verify_rule(item, audit)
    flags |= vflag
    total = (
        config.weights[0] * execution
        + config.weights[1] * distinct
        + config.weights[2] * evidence_score
        + config.weights[3] * verifiable
    )
    return {
        "execution": round(execution, 3),
        "distinctiveness": round(distinct, 3),
        "evidence": round(evidence_score, 3),
        "verifiability": round(verifiable, 3),
        "total": round(total, 3),
        "flags": flags,
        "deciles": deciles,
        "note": note,
    }


def build_rule_score_cache(profile: dict, control_profile: dict | None = None,
                           config: RuleScoringConfig = DEFAULT_RULE_SCORING) -> dict:
    """离线算全量打分并返回可写进档案顶层的缓存（零 LLM）。

    对照组：`control_profile` 是书 5/6 的档案（id=3），按 `novel_id` 拆成两本，
    **两本都高度相似才算通用套话**——一本书里的俗套不足以否定书 7 的风格。
    """
    indexes: dict[int, _GramIndex] | None = None
    if control_profile:
        by_book: dict[int, list[str]] = {}
        for items in ((control_profile.get("rules") or {}).values()):
            for item in items or []:
                nid = item.get("novel_id") if isinstance(item, dict) else None
                if isinstance(nid, int):
                    by_book.setdefault(nid, []).append(_rule_text(item))
        if len(by_book) >= 2:
            indexes = {nid: _GramIndex(texts) for nid, texts in by_book.items()}

    audit = _parse_audit_metrics(profile)
    scores: dict[str, list[int]] = {}
    shortlist: dict[str, list[list[int]]] = {}
    notes: dict[str, str] = {}
    generic: list[str] = []
    generic_rules: list[list] = []
    feedback_rules: list[dict] = []
    stats = {"total": 0, "generic": 0, "hard_rejected": 0,
             "verified": 0, "verify_mismatch": 0, "by_dim": {}}

    for dim, items in (profile.get("rules") or {}).items():
        if dim in (CHARACTER_VOICE_DIMENSION, "文风漂移分析"):
            continue
        items = items or []
        deciles = _cluster_deciles(items, dim, config)
        dim_stats = {"total": len(items), "generic": 0, "hard_rejected": 0,
                     "verified": 0, "verify_mismatch": 0}
        dim_rows: list[list[int]] = []
        for pos, item in enumerate(items):
            key = _rule_score_key(item, dim)
            scored = _score_rule(
                item, dim, deciles=deciles.get(pos, 1), audit=audit,
                indexes=indexes, config=config,
            )
            flags = scored["flags"]
            compact = [
                int(scored["execution"] * 100), int(scored["distinctiveness"] * 100),
                int(scored["evidence"] * 100), int(scored["verifiability"] * 100),
                int(flags), int(scored["deciles"]),
            ]
            scores[key] = compact
            dim_rows.append([
                pos, _rule_text_hash(item), int(scored["total"] * 1000),
                compact[2], int(flags), int(scored["deciles"]),
            ])
            if scored.get("note"):
                notes[key] = scored["note"]
            if flags & FLAG_GENERIC:
                generic.append(key)
                if dim != "文笔量化审计":
                    generic_rules.append([dim, _rule_text(item),
                                          item.get("chapter_no") if isinstance(item, dict) else None])
                dim_stats["generic"] += 1
            if flags & _HARD_FILTER_MASK:
                dim_stats["hard_rejected"] += 1
            if flags & FLAG_VERIFIED:
                dim_stats["verified"] += 1
            if flags & FLAG_VERIFY_MISMATCH:
                dim_stats["verify_mismatch"] += 1
            if dim != "文笔量化审计" and _numeric_claims(_rule_text(item)):
                feedback_rules.append({"dim": dim, "text": _rule_text(item)})
        # 渲染只用「按分数排好的前 200 条」：硬过滤/打分的全量结果留在
        # `rule_scores` 里供审计，但渲染不必为 3,000 条各算一次哈希。
        dim_rows.sort(key=lambda r: (-r[2], r[0]))
        shortlist[dim] = dim_rows[:200]
        stats["by_dim"][dim] = dim_stats
        stats["total"] += dim_stats["total"]
        stats["generic"] += dim_stats["generic"]
        stats["hard_rejected"] += dim_stats["hard_rejected"]
        stats["verified"] += dim_stats["verified"]
        stats["verify_mismatch"] += dim_stats["verify_mismatch"]

    return {
        "version": RULE_SCORE_VERSION,
        "config": {
            "max_rule_chars": config.max_rule_chars,
            "near_dup_similarity": config.near_dup_similarity,
            "weights": list(config.weights),
            "control_similarity": config.control_similarity,
            "mmr_lambda": config.mmr_lambda,
        },
        "scores": scores,
        "shortlist": shortlist,
        "generic": generic,
        "generic_rules": generic_rules,
        "notes": notes,
        "audit_metrics": {k: round(v, 4) for k, v in audit.items()},
        "feedback_rules": feedback_rules,
        "stats": stats,
    }


def ensure_rule_scores(db, profile: dict | None, source_ids=None) -> dict | None:
    """确保档案带当前版本的四层选条缓存（蒸馏收尾时调用一次）。

    对照组取**另一套 source_ids** 的 active 档案（书 5/6 之于书 7）。找不到
    对照组也照样建缓存——区分度按 0.5 中性处理，但硬过滤/可执行/证据/可验证
    照常生效，不会静默退回旧选法。
    """
    if not isinstance(profile, dict) or not _usable_narrative(profile):
        return profile
    if profile.get("rule_score_version") == RULE_SCORE_VERSION:
        return profile
    control = None
    try:
        from app.models import StyleDistill

        source_set = set(source_ids or [])
        for row in db.scalars(select(StyleDistill).where(
                StyleDistill.status == "active")).all():
            other = getattr(row, "style_narrative", None)
            if not isinstance(other, dict) or not other.get("rules"):
                continue
            if set(row.source_ids or []) == source_set:
                continue
            control = other
            break
    except Exception:  # noqa: BLE001 —— 对照组建不起来不影响主缓存
        logging.getLogger(__name__).debug("查找对照档案失败", exc_info=True)
    try:
        cache = build_rule_score_cache(profile, control)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("构建四层选条打分缓存失败（已忽略）", exc_info=True)
        return profile
    profile.update({
        "rule_scores": cache["scores"],
        "rule_shortlist": cache["shortlist"],
        "rule_score_version": RULE_SCORE_VERSION,
        "rule_score_config": cache["config"],
        "rule_score_stats": cache["stats"],
        "rule_score_notes": cache["notes"],
        "generic_rule_hashes": cache["generic"],
        "generic_rules": cache["generic_rules"],
        "audit_metrics": cache["audit_metrics"],
        "feedback_rules": cache["feedback_rules"],
    })
    return profile


def _cached_score(item, dim: str, profile: dict,
                  config: RuleScoringConfig = DEFAULT_RULE_SCORING) -> dict | None:
    raw = (profile.get("rule_scores") or {}).get(_rule_score_key(item, dim))
    if not raw or len(raw) < 6:
        return None
    w = config.weights
    return {
        "execution": raw[0] / 100.0,
        "distinctiveness": raw[1] / 100.0,
        "evidence": raw[2] / 100.0,
        "verifiability": raw[3] / 100.0,
        "total": round(
            w[0] * raw[0] / 100 + w[1] * raw[1] / 100
            + w[2] * raw[2] / 100 + w[3] * raw[3] / 100, 3
        ),
        "flags": int(raw[4]),
        "deciles": int(raw[5]),
        "note": (profile.get("rule_score_notes") or {}).get(_rule_score_key(item, dim)),
    }


def _reason_for_flags(flags: int) -> str | None:
    for bit, reason in _FLAG_REASONS:
        if flags & bit:
            return reason
    return None


#: 同维度矛盾判定：出现左侧词 vs 右侧词的两条规则视为对撞。
_CONFLICT_POLES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("短句", "短段", "断句", "简洁"), ("长句", "长段", "铺陈", "绵长")),
    (("多对白", "对白密集", "对白为主"), ("少对白", "对白稀疏", "对白较少")),
    (("快节奏", "节奏快", "推进快", "加速"), ("慢节奏", "节奏慢", "放缓", "延宕")),
    (("克制", "留白"), ("直白", "外化", "写满")),
)


def _conflict_pole(text: str) -> int:
    body = _rule_body(text)
    for i, (left, right) in enumerate(_CONFLICT_POLES):
        if any(w in body for w in left):
            return i * 2 + 1
        if any(w in body for w in right):
            return i * 2 + 2
    return 0


def _in_conflict(a: str, b: str) -> bool:
    pa, pb = _conflict_pole(a), _conflict_pole(b)
    return bool(pa and pb and pa != pb and (pa + 1) // 2 == (pb + 1) // 2)


def select_rules_with_audit(
    items: list, limit: int, *, dim: str, profile: dict,
    config: RuleScoringConfig = DEFAULT_RULE_SCORING, audit: bool = False,
) -> tuple[list, list[dict]]:
    """四层选条：硬过滤 → 打分排序 → 对撞/去重 → MMR 取 `limit` 条。

    返回 `(selected, decisions)`。`decisions` 仅在 `audit=True` 时逐条生成，
    渲染路径传 False 避免为 3,000 条规则各建一个 dict。
    """
    if limit <= 0 or not items:
        return [], []

    weights = profile.get("rule_weights") or {}
    entries = []
    decisions: list[dict] = []
    short = (profile.get("rule_shortlist") or {}).get(dim)
    if short and not audit:
        # 快速路径：只碰已排好序的前 200 条，不再为全维度算哈希。
        for row in short:
            pos, text_hash, total_milli, evidence_int, flags, deciles = row
            if flags & _HARD_FILTER_MASK:
                continue
            item = items[pos]
            weight = float(weights.get(text_hash, 1.0) or 1.0) if weights else 1.0
            entries.append({
                "pos": pos,
                "item": item,
                "ch": item.get("chapter_no") if isinstance(item, dict) else None,
                "scores": {
                    "total": total_milli / 1000.0,
                    "evidence": evidence_int / 100.0,
                    "flags": flags,
                    "deciles": deciles,
                },
                "weight": weight,
                "total": (total_milli / 1000.0) * weight,
                "fallback": False,
                "grams": None,
            })
    else:
        for pos, item in enumerate(items):
            scored = _cached_score(item, dim, profile, config)
            fallback = False
            if scored is None:
                fallback = True
                scored = _score_rule(
                    item, dim, deciles=1, audit=profile.get("audit_metrics") or {},
                    indexes=None, config=config,
                )
            weight = float(weights.get(_rule_text_hash(item), 1.0) or 1.0)
            entry = {
                "pos": pos,
                "item": item,
                "ch": item.get("chapter_no") if isinstance(item, dict) else None,
                "scores": scored,
                "weight": weight,
                "total": scored["total"] * weight,
                "fallback": fallback,
                "grams": None,
            }
            entries.append(entry)
            if audit:
                decisions.append({
                    "item": item, "dim": dim, "pos": pos,
                    "chapter_no": entry["ch"], "scores": scored,
                    "weight": weight, "total": entry["total"],
                    "selected": False,
                    "reason": _reason_for_flags(scored["flags"]) if scored["flags"] else None,
                    "stage": "hard" if scored["flags"] & _HARD_FILTER_MASK else "candidate",
                })

    survivors = [
        e for e in entries if not (e["scores"]["flags"] & _HARD_FILTER_MASK)
    ]
    if not survivors:
        return [], decisions
    survivors.sort(key=lambda e: (-e["total"], e["pos"]))

    # 池子只取前 limit×6：近重复/矛盾对撞不需要看完全部 3,000 条。
    pool_size = max(limit * config.mmr_pool_factor, 24)
    pool = survivors[:pool_size]

    def _grams_of(entry) -> frozenset[str]:
        if entry["grams"] is None:
            entry["grams"] = _grams(_rule_body(entry["item"]))
        return entry["grams"]

    # 第一层补刀：与更强候选语义相似度 ≥0.8 的近重复淘汰。
    deduped: list[dict] = []
    for entry in pool:
        g = _grams_of(entry)
        dup = next(
            (other for other in deduped
             if _containment(g, _grams_of(other)) >= config.near_dup_similarity),
            None,
        )
        if dup is not None:
            if audit:
                row = next((d for d in decisions if d["pos"] == entry["pos"]), None)
                if row:
                    row["stage"] = "near_dup"
                    row["reason"] = (
                        f"与已选条近重复（相似度 ≥ {config.near_dup_similarity:.0%}）"
                    )
            continue
        deduped.append(entry)

    # 同维矛盾显式对撞：留证据更多、覆盖更广的一条。
    resolved: list[dict] = []
    for entry in deduped:
        text = _rule_text(entry["item"])
        clash = next((o for o in resolved if _in_conflict(text, _rule_text(o["item"]))), None)
        if clash is None:
            resolved.append(entry)
            continue
        better = entry if (
            (entry["scores"]["evidence"], entry["scores"]["deciles"], entry["total"])
            >= (clash["scores"]["evidence"], clash["scores"]["deciles"], clash["total"])
        ) else clash
        loser = clash if better is entry else entry
        if audit:
            row = next((d for d in decisions if d["pos"] == loser["pos"]), None)
            if row:
                row["stage"] = "conflict"
                row["reason"] = "与同维另一条矛盾，保留证据更多/覆盖更广的一条"
        if better is entry:
            resolved.remove(clash)
            resolved.append(entry)

    # 第三层 MMR：每选一条，压掉与它相近的候选，避免 60 条说同一件事。
    selected: list[dict] = []
    used_deciles: set[int] = set()
    remaining = list(resolved)
    while remaining and len(selected) < limit:
        best, best_rank = None, None
        for entry in remaining:
            base = min(entry["total"], 1.0)
            decile = min(entry["ch"], 10) if isinstance(entry["ch"], int) and entry["ch"] > 0 else 0
            coverage = config.coverage_bonus if decile not in used_deciles else 0.0
            redundancy = 0.0
            if selected:
                g = _grams_of(entry)
                redundancy = max(
                    _containment(g, _grams_of(chosen)) for chosen in selected
                )
            rank = config.mmr_lambda * (base + coverage) - (1 - config.mmr_lambda) * redundancy
            if best_rank is None or rank > best_rank:
                best, best_rank = entry, rank
        remaining.remove(best)
        selected.append(best)
        if isinstance(best["ch"], int) and best["ch"] > 0:
            used_deciles.add(min(best["ch"], 10))

    if audit:
        chosen_pos = {e["pos"] for e in selected}
        for row in decisions:
            if row["selected"] or row["stage"] not in ("candidate", "hard"):
                continue
            if row["pos"] in chosen_pos:
                row["selected"] = True
                row["reason"] = None
            elif row["stage"] == "candidate":
                row["stage"] = "mmr"
                row["reason"] = "通过硬过滤，但 MMR 排名在配额之外"
            elif row["stage"] == "hard":
                row["reason"] = row["reason"] or "硬过滤淘汰"
    return [e["item"] for e in selected], decisions


# ====================================================================
# 节拍序列：跨书骨架化 + 同书放宽
# ====================================================================
#
# 节拍序列的规则天然偏长（「先…再…然后…最后…」），且常带具体人名/招式/证物。
# 直接放宽进原创档会把参考书的专名带进新书（跨书串味），所以：
#   - 原创：280 字上限不放宽；超长条目必须先骨架化（去专名、只留结构节点）；
#   - 续写：同书可放宽到 480 字（同一本书的专名不是串味，是上下文）；
#     跨书仍走骨架化。
# 骨架化优先用离线小模型改写（脚本批量生成后落库），没有改写结果时退回
# 确定性压缩。两条路径都必须通过同一套校验：长度、结构节点、专名黑名单。

BEAT_DIMENSION = "节拍序列"
BEAT_SKELETON_MAX_CHARS = 280
BEAT_CONTINUATION_MAX_CHARS = 480
#: 骨架缓存的形状版本。与 `RULE_SCORE_VERSION` 同理：它是**选条缓存**，
#: 不是蒸馏产出的字段，改它只需重算骨架，不必让整份档案失效（NARRATIVE_VERSION 不动）。
BEAT_SKELETON_VERSION = 1
#: 骨架条目的执行分折价系数。**只折价、不重算**：骨架是原文的压缩，
#: 重算 `_execution_score` 会因为「正文变短」把执行难度判低/判高，两条路都错。
#: 沿用原条目的 execution 再乘 0.95，保证同一条件下骨架总分**永远低于原文**
#: （总分对 execution 线性、权重为正），骨架化不会变成刷分路径。
BEAT_SKELETON_SCORE_FACTOR = 0.95
_BEAT_NODE_RE = re.compile(
    r"(?:^|[，。；：])\s*(先|再|然后|接着|随后|最后|最终)"
)
_BEAT_SPEECH_NAME_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4})(?:吼道|叫道|笑道|问道|答道|喝道|喊道|沉声道|冷声道|"
    r"低声道|轻声道|开口道|点头|摇头|皱眉|冷笑|大喝|暴喝|道[：:，。])"
)
_GENERIC_ROLE_WORDS = frozenset({
    "前辈", "小友", "老夫", "女子", "少女", "大叔", "老师", "真君", "散人",
    "道友", "姑娘", "少年", "男子", "它们", "我们", "你们", "他们", "她们",
    "众人", "对方", "自己", "有人", "那人", "此人", "大家", "主角", "对手",
    "同伴", "旁观者", "旁人",
})


def _beat_body(text: str) -> str:
    # 「证据：…」前面通常还有一个「（」，剥掉尾巴后要一并清掉这个孤立括号。
    return _EVIDENCE_RE.sub("", text or "").rstrip("（( \t").strip()


def _beat_label(body: str) -> tuple[str, str]:
    """拆出「爆发：/升温：」这类节拍类型标签。"""
    label, sep, rest = body.partition("：")
    if sep and 1 <= len(label) <= 10 and not re.search(r"[，。；！？]", label):
        return label.strip(), rest.strip()
    return "", body


def _beat_nodes(body: str) -> list[str]:
    """按出现顺序取**去重后**的结构节点（先/再/然后/最后…）。

    原文里「先…再…先…再」很常见，旧写法返回重复节点，校验时会要求骨架
    把这些重复词一个不落地复现，正常改写必然被判「丢节点」。结构节点只关心
    「用过哪几种」，不关心用了几次。
    """
    seen: list[str] = []
    for m in _BEAT_NODE_RE.finditer(body):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def _beat_name_candidates(text: str) -> set[str]:
    """从「某某道/某某冷笑」这类说话归属里抽候选名，补 KG 名单的漏。"""
    return {m.group(1) for m in _BEAT_SPEECH_NAME_RE.finditer(text or "")}


def _replace_terms(text: str, terms: dict) -> str:
    """专名 → 角色/事物。

    主角**由 `terms["protagonists"]` 指定**，不能按名字长度排序猜测——旧写法
    会把最长的名字（常是「某某的下属」这类长别名）当主角，骨架的角色关系直接错位。
    其余人物按在原文中首次出现的顺序依次分配对手/同伴/旁观者；名字仍按长度
    倒序替换，避免「宋书」先吃掉「宋书航」。
    """
    names = [n for n in (terms.get("characters") or []) if n]
    protagonists = {n for n in (terms.get("protagonists") or []) if n}
    ordered = sorted(set(names) | protagonists, key=len, reverse=True)

    assigned: dict[str, str] = {}
    for name in ordered:
        if name in _GENERIC_ROLE_WORDS:
            continue
        if name in protagonists:
            assigned[name] = "主角"
    extras = [
        n for n in ordered
        if n not in assigned and n not in _GENERIC_ROLE_WORDS and len(n) >= 2
    ]
    extras.sort(key=lambda n: (text.find(n) if text.find(n) >= 0 else 10 ** 9, -len(n)))
    roles = ("对手", "同伴", "旁观者")
    for i, name in enumerate(extras):
        assigned[name] = roles[i] if i < len(roles) else "旁观者"

    for name in ordered:
        role = assigned.get(name)
        if role:
            text = text.replace(name, role)
    for item in sorted(set(terms.get("items") or []), key=len, reverse=True):
        if item and len(item) >= 3 and item not in _GENERIC_ROLE_WORDS:
            text = text.replace(item, "某一事物")
    return text


def skeletonize_beat_rule(text: str, terms: dict, *,
                          limit: int = BEAT_SKELETON_MAX_CHARS) -> str | None:
    """确定性骨架化：去证据、去引号/书名、专名换角色，只留结构节点。"""
    body = _beat_body(text)
    if len(body) <= limit:
        return body
    label, rest = _beat_label(body)
    names = list(terms.get("characters") or [])
    names += list(_beat_name_candidates(body))
    names += list(terms.get("protagonists") or [])
    rest = _replace_terms(rest, {**terms, "characters": names})
    rest = re.sub(r"[“「『][^”」』]*[”」』]", "某一细节", rest)
    rest = re.sub(r"《[^》]*》|【[^】]*】|［[^］]*］", "某一事物", rest)
    rest = re.sub(r"[（(][^）)]*[）)]", "", rest)

    clauses = [c.strip(" ，。；：、\n\t")
               for c in re.split(r"[，。；：、\n]", rest)]
    clauses = [c for c in clauses if c]
    picked: list[str] = []
    for clause in clauses:
        if not picked:
            picked.append(clause)
        elif clause.startswith(("先", "再", "然后", "接着", "随后", "最后", "最终", "同时")):
            picked.append(clause)
    if len(picked) < 2:
        picked = clauses[:4]

    def render(parts: list[str]) -> str:
        joined = "；".join(parts)
        return f"{label}：{joined}" if label else joined

    skeleton = render(picked)
    while len(skeleton) > limit and len(picked) > 2:
        picked.pop()
        skeleton = render(picked)
    return skeleton if len(skeleton) <= limit else None


def validate_beat_skeleton(original: str, skeleton: str, terms: dict, *,
                           limit: int = BEAT_SKELETON_MAX_CHARS) -> str | None:
    """返回不合格原因；None = 通过。

    校验三件事：长度、结构节点没丢、没有遗留/新增专名。
    「新增专名」用 KG 人物/物品名单 + 原文说话归属候选名做黑名单；模型若
    凭空造名，只要不在黑名单里就会漏检，因此提示词同时禁止新增专名，
    并在批量生成后人工抽检。
    """
    if not skeleton or not skeleton.strip():
        return "骨架为空"
    if len(skeleton) > limit:
        return f"骨架 {len(skeleton)} 字，超过 {limit}"
    if "证据" in skeleton:
        return "骨架里还留着证据段"
    if re.search(r"[“”「」『』《》【】]", skeleton):
        return "骨架里还留着引号/书名号"
    body = _beat_body(original)
    want = _beat_nodes(body)
    lost = [node for node in want if node not in skeleton]
    if lost:
        return f"丢了结构节点：{'/'.join(lost)}"
    forbidden = set(terms.get("characters") or [])
    # 物品只查 3 字及以上：KG 的 item 里混着「环境」「资源」这类通用名词，
    # 它们出现在骨架里是正常表达，不是跨书专名。真正的功法/法宝名基本 ≥3 字。
    forbidden |= {n for n in (terms.get("items") or []) if len(n) >= 3}
    forbidden |= _beat_name_candidates(body)
    forbidden -= _GENERIC_ROLE_WORDS
    leaked = sorted((n for n in forbidden if n and n in skeleton), key=len, reverse=True)
    if leaked:
        return f"还有专名：{'、'.join(leaked[:3])}"
    return None


def build_beat_skeletons(profile: dict, terms: dict, *,
                         rewriter=None,
                         limit: int = BEAT_SKELETON_MAX_CHARS) -> dict:
    """为超长节拍生成骨架，返回 {skeletons, skipped, stats}。

    `rewriter(body, hint)` 可选；不传则用确定性压缩。改写结果必须过
    `validate_beat_skeleton`，不过就丢进 skipped，宁缺毋滥。
    """
    items = ((profile or {}).get("rules") or {}).get(BEAT_DIMENSION) or []
    skeletons: dict[str, str] = {}
    skipped: dict[str, str] = {}
    seen: set[str] = set()
    stats = {"long": 0, "ok": 0, "skipped": 0, "rewritten": 0}
    for item in items:
        text = _rule_text(item)
        if not text or _beat_char_len(text) <= limit:
            # 用与硬过滤相同的口径判断「超长」：证据占比高的条目正文可能很短，
            # 但整条规则仍被 FLAG_TOO_LONG 淘汰，必须一并骨架化。
            continue
        key = _rule_score_key(item, BEAT_DIMENSION)
        if key in seen:
            continue
        seen.add(key)
        stats["long"] += 1
        skeleton = None
        if rewriter is not None:
            try:
                skeleton = rewriter(_beat_body(text), "")
                stats["rewritten"] += 1
            except Exception:  # noqa: BLE001 —— 改写失败退回确定性压缩
                skeleton = None
        if skeleton is not None:
            reason = validate_beat_skeleton(text, skeleton, terms, limit=limit)
            if reason:
                # 一次重试，把不合格原因回传给改写器。
                try:
                    retried = rewriter(_beat_body(text), reason)
                    stats["rewritten"] += 1
                    skeleton = retried if not validate_beat_skeleton(
                        text, retried, terms, limit=limit) else None
                except Exception:  # noqa: BLE001
                    skeleton = None
            if skeleton is None:
                skipped[key] = reason
        if skeleton is None and rewriter is None:
            skeleton = skeletonize_beat_rule(text, terms, limit=limit)
            reason = validate_beat_skeleton(text, skeleton or "", terms, limit=limit)
            if reason:
                skipped[key] = reason
                skeleton = None
        if skeleton:
            skeletons[key] = skeleton
            stats["ok"] += 1
        else:
            stats["skipped"] += 1
    return {"skeletons": skeletons, "skipped": skipped, "stats": stats}


def build_beat_terms(db, profile: dict | None = None, source_ids=None) -> dict:
    """收集骨架化用的专名表：KG 人物/物品 + 声音词典里的名字。

    `protagonists` 取 KG 里 mention_count 最高的 2 个角色，供确定性兜底指定主角；
    LLM 改写路径不依赖它，但仍用它做校验黑名单。
    """
    characters: list[str] = []
    items: list[str] = []
    protagonists: list[str] = []
    if db is not None:
        try:
            from app.models import KgEntity

            ids = [int(x) for x in (source_ids or [])]
            query = select(KgEntity).where(KgEntity.kind.in_(("character", "item")))
            if ids:
                query = query.where(KgEntity.novel_id.in_(ids))
            rows = db.scalars(query).all()
            chars = [r for r in rows if r.kind == "character"]
            for row in rows:
                target = characters if row.kind == "character" else items
                name = (row.canonical_name or "").strip()
                if 2 <= len(name) <= 8:
                    target.append(name)
                for alias in (row.aliases or []):
                    if isinstance(alias, str) and 2 <= len(alias) <= 8:
                        target.append(alias.strip())
            ranked = sorted(
                (r for r in chars if 2 <= len(r.canonical_name or "") <= 8),
                key=lambda r: -(r.mention_count or 0),
            )
            protagonists = [
                r.canonical_name.strip() for r in ranked
                if r.canonical_name.strip() not in _GENERIC_ROLE_WORDS
            ][:2]
        except Exception:  # noqa: BLE001 —— KG 不可用不影响确定性骨架
            logging.getLogger(__name__).debug("读取 KG 专名失败", exc_info=True)
    for name in ((profile or {}).get("voice_stats") or {}):
        bare = name.split("|", 1)[-1].strip()
        if 2 <= len(bare) <= 8:
            characters.append(bare)
    return {
        "characters": sorted(set(characters)),
        "items": sorted(set(items)),
        "protagonists": protagonists,
    }


def ensure_beat_skeletons(db, profile: dict | None, source_ids=None, *,
                          rewriter=None) -> dict | None:
    """确保档案带当前版本的节拍骨架缓存（蒸馏收尾时调用一次）。

    与 `ensure_rule_scores` 分开：打分缓存和骨架缓存各有版本，改其中一个
    不必让另一个重算。没有超长节拍时不写任何字段，档案形状保持不变。
    """
    if not isinstance(profile, dict):
        return profile
    if profile.get("beat_skeleton_version") == BEAT_SKELETON_VERSION:
        return profile
    terms = build_beat_terms(db, profile, source_ids)
    built = build_beat_skeletons(profile, terms, rewriter=rewriter)
    if built["stats"]["long"] == 0:
        return profile
    profile.update({
        "beat_skeletons": built["skeletons"],
        "beat_skeleton_skipped": built["skipped"],
        "beat_skeleton_version": BEAT_SKELETON_VERSION,
        "beat_skeleton_stats": built["stats"],
    })
    return profile


def _beat_char_len(text: str) -> int:
    """节拍长度口径：与硬过滤一致（去标点、含证据）。

    `_beat_body` 只用于骨架化（去掉「证据：」）；判断「是否超长」必须用
    `FLAG_TOO_LONG` 打标时的同一口径，否则会出现「缓存说超长、这里说不超长」
    的分叉，超长条目会绕过骨架化直接带着专名进池。
    """
    return len(_PUNCT_RE.sub("", text or ""))


def _select_beat_items(
    items: list,
    limit: int,
    *,
    mode: str,
    allowed_novel_ids,
    profile: dict,
    config: RuleScoringConfig = DEFAULT_RULE_SCORING,
) -> list:
    """节拍序列专用选条：原创只收 ≤280；续写同书放宽到 480，跨书走骨架。

    席位口径（用户 2026-11-12 定稿）：

    - 普通席位：按四维总分排序，取前 `limit * mmr_pool_factor`（下限 24）条
      做近重复压制与 MMR；
    - 结构席位（走过骨架化的条目）：原创 1、续写 2，**从普通候选池外单独取**，
      不占普通池的 24 条名额；不足时由普通席位补位，反之亦然；
    - 骨架条目的 execution 沿用原分 ×0.95，不重算，保证总分不高于原文；
    - 总配额与字数上限都不因留席而改变。
    """
    skeletons = profile.get("beat_skeletons") or {}
    allowed = {int(x) for x in (allowed_novel_ids or [])}
    items_novels = {
        int(it["novel_id"]) for it in items
        if isinstance(it, dict) and isinstance(it.get("novel_id"), int)
    }
    if not allowed and len(items_novels) == 1:
        allowed = set(items_novels)

    candidates = []
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        text = _rule_text(item)
        if not text:
            continue
        cached = _cached_score(item, BEAT_DIMENSION, profile, config)
        if cached is None:
            continue
        # 硬过滤与风格规则同一套，只对 TOO_LONG 网开一面（走骨架/同书放宽）。
        if cached["flags"] & (_HARD_FILTER_MASK & ~FLAG_TOO_LONG):
            continue
        novel_id = item.get("novel_id")
        same_book = bool(allowed) and isinstance(novel_id, int) and int(novel_id) in allowed
        too_long = bool(cached["flags"] & FLAG_TOO_LONG)
        raw_len = _beat_char_len(text)
        relax_limit = BEAT_CONTINUATION_MAX_CHARS if mode == RENDER_MODE_CONTINUATION else 0
        can_relax = mode == RENDER_MODE_CONTINUATION and same_book and raw_len <= relax_limit

        effective = text
        used_skeleton = False
        if too_long and not can_relax:
            effective = skeletons.get(_rule_score_key(item, BEAT_DIMENSION)) or ""
            used_skeleton = True
            if not effective:
                continue
            if len(effective) > BEAT_SKELETON_MAX_CHARS:
                # 骨架按原样渲染进提示词，直接用原始长度卡 280。
                continue
        elif not too_long and raw_len > BEAT_SKELETON_MAX_CHARS:
            # 未打 TOO_LONG 但正文已超 280：理论上不会发生（口径已对齐），
            # 仍然拦下并记录，避免将来改阈值时静默放行。
            logging.getLogger(__name__).debug(
                "节拍条目长度口径不一致：%s 字但未打 TOO_LONG", raw_len)
            continue

        flags = cached["flags"] & ~FLAG_TOO_LONG
        if effective == text:
            scores = dict(cached)
            scores["flags"] = flags
            total = cached["total"]
        else:
            # 骨架条目沿用原条目的 execution 分 ×0.95，**不重算**：
            # 重算会因为压缩后正文变短而判出一个新分数，与原文不可比；
            # 折价保证同一条件下骨架总分不高于原文，骨架化不能变成刷分路径。
            exec_score = round(
                float(cached["execution"]) * BEAT_SKELETON_SCORE_FACTOR, 4
            )
            scores = dict(cached)
            scores["execution"] = exec_score
            scores["flags"] = flags
            w = config.weights
            total = (
                w[0] * exec_score + w[1] * scores["distinctiveness"]
                + w[2] * scores["evidence"] + w[3] * scores["verifiability"]
            )
        candidates.append({
            "pos": pos,
            "item": dict(item, rule=effective, _beat_skeleton=used_skeleton),
            "text": effective,
            "ch": item.get("chapter_no"),
            "scores": scores,
            "total": total,
        })

    # 与通用选条一致：只对得分靠前的候选做近重复/MMR。全量 2,800 条两两
    # 算包含度是 O(n²)，实测单次要 120 秒；节拍维度不能拖垮生成路径。
    candidates.sort(key=lambda e: (-e["total"], e["pos"]))

    # —— 席位拆分 ——
    # 结构型（走过骨架化）条目单独留席：配额来自 `RenderPreset.structural_seats`
    # （原创 1、续写 2、角色对话 0）。骨架条目的 execution 折价后排在 24 条池外，
    # 若与普通条目抢同一批席位就永远选不中；留席后它们从池外单独取，
    # **不占**普通席位的 24 条池子名额。总配额不变。
    struct_seats = resolve_render_preset(mode).structural_seats
    struct_limit = max(0, min(struct_seats, limit))
    normal_limit = limit - struct_limit
    structural = [e for e in candidates if e["item"].get("_beat_skeleton")]
    normal = [e for e in candidates if not e["item"].get("_beat_skeleton")]

    used_deciles: set[int] = set()
    taken: set[int] = set()
    selected: list[dict] = []

    def _take(cands: list[dict], n: int) -> list[dict]:
        """近重复 + MMR 选 n 条。`taken` 里的条目不重复选，总配额由调用方控制。"""
        if n <= 0:
            return []
        # 与通用选条一致：只对得分靠前的候选做近重复/MMR。
        pool = [e for e in cands if e["pos"] not in taken][
            : max(n * config.mmr_pool_factor, 24)
        ]
        deduped: list[dict] = []
        for entry in pool:
            g = _grams(entry["text"])
            if any(_containment(g, _grams(o["text"])) >= config.near_dup_similarity
                   for o in deduped):
                continue
            deduped.append(entry)

        picked: list[dict] = []
        remaining = list(deduped)
        while remaining and len(picked) < n:
            best, best_rank = None, None
            for entry in remaining:
                base = min(entry["total"], 1.0)
                decile = min(entry["ch"], 10) if isinstance(entry["ch"], int) and entry["ch"] > 0 else 0
                coverage = config.coverage_bonus if decile not in used_deciles else 0.0
                redundancy = 0.0
                if selected or picked:
                    g = _grams(entry["text"])
                    redundancy = max(
                        _containment(g, _grams(o["text"]))
                        for o in (selected + picked)
                    )
                rank = config.mmr_lambda * (base + coverage) - (1 - config.mmr_lambda) * redundancy
                if best_rank is None or rank > best_rank:
                    best, best_rank = entry, rank
            remaining.remove(best)
            picked.append(best)
            if isinstance(best["ch"], int) and best["ch"] > 0:
                used_deciles.add(min(best["ch"], 10))
        return picked

    def _commit(entries: list[dict]) -> None:
        selected.extend(entries)
        taken.update(e["pos"] for e in entries)

    # 普通席位：24 条池内近重复 + MMR；结构席位：池外按总分取。
    _commit(_take(normal, normal_limit))
    _commit(_take(structural, struct_limit))
    # 互相补位：一边候选不够时用另一边补齐，总配额不变（不扩池、不放宽字数）。
    if len(selected) < limit:
        _commit(_take(structural, limit - len(selected)))
    if len(selected) < limit:
        _commit(_take(normal, limit - len(selected)))
    return [entry["item"] for entry in selected]


# ====================================================================
# 渲染用途（mode）与配额
# ====================================================================
#
# ## 为什么按用途分三套
#
# 同一份档案在三种生成场景里要读的东西不一样：
#   - 原创：要把「作者怎么写」学像，文风维度是主体，声音只要够定称呼/自称/口头禅；
#   - 续写：既要接文风，也要接人物与时间线，三者并重；
#   - 角色对话：要把某个角色说话的方式演像，声音词典是主体，文风退为辅助。
#
# 旧实现是「每维 3 条 + 声音 20 条」一套通吃，等于让三种场景读同一份配比。
# 这里把三套配额**显式注册**成配置（不在渲染逻辑里散落 if/else），
# 调用方必须显式传 `mode`；传错直接报错，不静默落回默认。

RENDER_MODE_ORIGINAL = "original"
RENDER_MODE_CONTINUATION = "continuation"
RENDER_MODE_DIALOGUE = "dialogue"


@dataclass(frozen=True)
class RenderPreset:
    """一种生成用途的渲染配额。"""

    key: str
    name: str
    #: 未在 `dim_limits` 里单独指定的维度取几条
    per_dim: int
    #: 维度级覆盖：{维度名: 条数}
    dim_limits: dict[str, int]
    #: 角色声音渲染条数
    voice_count: int
    #: 反向禁忌每组最多渲染几条
    forbidden_per_group: int
    #: 整块提示词的字数上限（含禁忌）
    char_limit: int
    #: 声音排序口径："address"（称呼体系）/ "frequency"（出场频次）/ "imitation"（可模仿度）
    voice_order: str
    #: 首段是否额外压低压缩类漂移句子（原创开篇沿用旧行为）
    drop_compression: bool = True
    #: 结构型（走过骨架化）条目的留席数：原创 1、续写 2、角色对话 0。
    #: 骨架条目 execution 折价后必然排在 24 条候选池外，不给留席就永远选不中；
    #: 留席从池外单独取，**不占**普通席位名额，总配额与字数上限都不因此改变。
    structural_seats: int = 0

    def limit_for(self, dim: str) -> int:
        return self.dim_limits.get(dim, self.per_dim)


RENDER_PRESETS: dict[str, RenderPreset] = {
    RENDER_MODE_ORIGINAL: RenderPreset(
        key=RENDER_MODE_ORIGINAL,
        name="原创",
        per_dim=2,
        dim_limits={
            # 文风主体：对白/节奏/段落/情绪/场景/人物给足
            "人物描写手法": 4,
            "对白风格": 5,
            "段落编排": 4,
            "场景构建": 4,
            "情绪表达": 4,
            "节奏控制": 4,
            "剧情编排模式": 3,
            # 非文风维度让位
            "世界观呈现": 1,
            "伏笔与悬念设计": 1,
            "节拍序列": 1,
            "高频场景模板": 1,
            "潜台词模式": 1,
        },
        # 声音只要够定称呼/自称/口头禅的一致性，不占大份
        voice_count=6,
        forbidden_per_group=8,
        char_limit=9000,
        voice_order="address",
        structural_seats=1,
    ),
    RENDER_MODE_CONTINUATION: RenderPreset(
        key=RENDER_MODE_CONTINUATION,
        name="续写",
        per_dim=3,
        dim_limits={
            # 文风维度
            "人物描写手法": 5,
            "对白风格": 4,
            "段落编排": 3,
            "场景构建": 3,
            "情绪表达": 3,
            "节奏控制": 3,
            # 时间线（用户口径：节拍序列、剧情编排模式、伏笔与悬念、文风漂移）
            "节拍序列": 4,
            "剧情编排模式": 4,
            "伏笔与悬念设计": 4,
            # 其余辅助
            "世界观呈现": 2,
            "高频场景模板": 1,
            "潜台词模式": 2,
        },
        voice_count=12,
        forbidden_per_group=6,
        char_limit=10000,
        voice_order="frequency",
        structural_seats=2,
    ),
    RENDER_MODE_DIALOGUE: RenderPreset(
        key=RENDER_MODE_DIALOGUE,
        name="角色对话",
        per_dim=1,
        dim_limits={
            # R36-1（2026-11-12 用户定）：角色对话**只取对白维度 + 声音**。
            # 场景、节奏、情绪、段落等一律不进——对话要像本人说话，不是像作者写景。
            "对白风格": 4,
        },
        voice_count=30,
        forbidden_per_group=4,
        char_limit=7000,
        voice_order="imitation",
    ),
}


def resolve_render_preset(mode: str | None) -> RenderPreset:
    """解析渲染用途。传错必须响亮失败——静默落回原创会让角色对话读错配比。"""
    key = mode or RENDER_MODE_ORIGINAL
    preset = RENDER_PRESETS.get(key)
    if preset is None:
        valid = "、".join(f"{k}（{v.name}）" for k, v in RENDER_PRESETS.items())
        raise ValueError(f"未知的叙事档案渲染用途：{mode!r}；可选：{valid}")
    return preset


#: 角色声音条目的「名字：」前缀（`老周：自称多省略…`）。
#: 角色名册与角色对话按它取「这条是谁的声音」。批 8 前置：`routers/roleplay.py` 直接读它。
_VOICE_NAME_PREFIX_RE = re.compile(r"^[^\s：:]{1,8}\s*[：:]\s*")


def _voice_item_name(item) -> str:
    rule = item.get("rule", "") if isinstance(item, dict) else ""
    if "：" not in rule:
        return ""
    return rule.split("：", 1)[0].strip()


def _voice_stat(item, stats: dict) -> dict | None:
    """取某条声音的频次统计。key 先按 `novel_id|名字`，再退回纯名字。"""
    if not stats:
        return None
    name = _voice_item_name(item)
    if not name:
        return None
    nid = item.get("novel_id") if isinstance(item, dict) else None
    if nid is not None and f"{int(nid)}|{name}" in stats:
        return stats[f"{int(nid)}|{name}"]
    return stats.get(name)


def build_voice_stats(items: list, texts_by_novel: dict[int, str]) -> tuple[dict, list[str]]:
    """用**语料精确计数**给每条声音补出场频次，并标出不会说话的非人条目。

    只产读取侧索引（进档案顶层 `voice_stats` / `voice_non_human`），
    不改规则条目本身，也不产生任何 LLM 调用。

    Returns:
        (stats, non_human_names)
        stats: {"<novel_id>|<名字>": {"mentions": int, "non_human": bool}}
    """
    stats: dict[str, dict] = {}
    non_human: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = _voice_item_name(item)
        if not name:
            continue
        nid = item.get("novel_id") if isinstance(item, dict) else None
        text = texts_by_novel.get(int(nid)) if nid is not None else None
        key = f"{int(nid)}|{name}" if nid is not None else name
        if key in seen:
            continue
        seen.add(key)
        flag = not _has_speaking_evidence(
            item.get("rule", "") if isinstance(item, dict) else str(item)
        )
        stats[key] = {
            "mentions": text.count(name) if text else 0,
            "non_human": flag,
        }
        if flag:
            non_human.append(name)
    return stats, non_human


def _voice_address_score(item) -> int:
    """原创用：这条声音定义了多少「怎么称呼别人」的信息。"""
    rule = item.get("rule", "") if isinstance(item, dict) else ""
    return rule.count("称") * 100 + (40 if "关系动态" in rule else 0)


def _voice_imitation_score(item) -> int:
    """角色对话用：这条声音有多少可模仿的说话特征（越具体越高）。"""
    rule = item.get("rule", "") if isinstance(item, dict) else ""
    score = 0
    for marker, weight in (("自称", 30), ("口头禅", 40), ("句长", 25),
                           ("语气", 20), ("节奏", 20), ("代表台词", 25)):
        if marker in rule:
            score += weight
    score += min(_evidence_len(rule), 200) // 5
    return score


def _select_voice_items(items: list, stats: dict, *, limit: int,
                        order: str = "frequency") -> list:
    """按用途排序角色声音，取前 `limit` 条。

    排序口径：
      - address：称呼体系（原创）——定义了多少「称人」方式优先
      - imitation：可模仿度（角色对话）——自称/口头禅/句长/语气/代表台词优先
      - frequency：出场频次（续写）——语料精确计数优先
    没有频次索引（旧档案）时保持原顺序，行为与改动前一致。
    """
    if not items:
        return []
    if not stats:
        return items[:limit]
    decorated = []
    for idx, item in enumerate(items):
        stat = _voice_stat(item, stats) or {}
        mentions = int(stat.get("mentions") or 0)
        if order == "address":
            primary = _voice_address_score(item)
        elif order == "imitation":
            primary = _voice_imitation_score(item)
        else:
            primary = mentions
        decorated.append((primary, mentions, -idx, item))
    decorated.sort(reverse=True)
    ordered = [d[3] for d in decorated]

    # 硬性保底：全书出场频次最高的 3 个名字（主角 + 主要配角）必进，
    # 不受当前用途排序口径影响。旧实现按落库顺序取前 20，宋书航/羽柔子/
    # 白前辈（书内频次最高的一档）会被挤出去。
    guaranteed: list = []
    locked: set[str] = set()
    for item in sorted(items, key=lambda it: -int((_voice_stat(it, stats) or {}).get("mentions") or 0)):
        name = _voice_item_name(item)
        if not name or name in locked:
            continue
        if len(guaranteed) >= min(3, limit):
            break
        guaranteed.append(item)
        locked.add(name)
    rest = [it for it in ordered if _voice_item_name(it) not in locked]
    return (guaranteed + rest)[:limit]


def _voice_mapping_sources(profile: dict) -> list[str]:
    """项目角色 → 参考声音映射里的参考角色名，保持映射表顺序。"""
    mapping = profile.get("voice_mapping") if isinstance(profile, dict) else None
    if not isinstance(mapping, list):
        return []
    names: list[str] = []
    for row in mapping:
        if not isinstance(row, dict):
            continue
        name = str(row.get("source_character") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _promote_mapped_voices(selected: list, items: list, profile: dict,
                           limit: int) -> list:
    """映射里的参考角色必须出现在最终词典里，否则映射指向空气。

    先按映射顺序放进参考条目，再用原排序结果补足 `limit`。
    """
    source_names = _voice_mapping_sources(profile)
    if not source_names:
        return selected
    by_name: dict[str, dict] = {}
    for item in items:
        name = _voice_item_name(item)
        if name and name not in by_name:
            by_name[name] = item
    promoted: list = []
    seen: set[str] = set()
    for name in source_names:
        item = by_name.get(name)
        if item is not None and name not in seen:
            promoted.append(item)
            seen.add(name)
    for item in selected:
        if len(promoted) >= limit:
            break
        name = _voice_item_name(item)
        if name and name not in seen:
            promoted.append(item)
            seen.add(name)
    return promoted[:limit]


def _render_voice_mapping(profile: dict, available_names: set[str]) -> list[str]:
    """渲染在【角色声音词典】前的项目映射；只列真正出现在词典里的参考角色。"""
    mapping = profile.get("voice_mapping") if isinstance(profile, dict) else None
    if not isinstance(mapping, list):
        return []
    rows: list[str] = []
    for row in mapping:
        if not isinstance(row, dict):
            continue
        character = str(row.get("character") or "").strip()
        source = str(row.get("source_character") or "").strip()
        borrow = str(row.get("borrow_point") or "").strip()
        if not character or not source or not borrow:
            continue
        if source not in available_names:
            continue
        rows.append(f"  · {character} ← {source}：{borrow}")
    if not rows:
        return []
    return [
        "【本项目角色声音映射（只借类型，不借人名）】",
        "  下列“角色声音词典”是参考素材，不是本项目人设；按上面的映射借用类型。",
        *rows,
    ]


#: 规则声称的指标 → 生成正文实测字段（`style.consistency.measure`）。
#: 百分比字段在 measure 里是 0–1 小数，比较前 ×100。
_FEEDBACK_METRIC_MAP = {
    "句长": "句长均值",
    "短句占比": "短句占比",
    "长句占比": "长句占比",
    "对白占比": "对白字占比",
    "段均句数": "段均句数",
}

_FEEDBACK_PERCENT_METRICS = {"短句占比", "长句占比", "对白占比"}


def load_rule_weights(db, project_id: int) -> dict[str, float]:
    """读某项目的规则反馈权重（rule_hash → weight）。读不到返回空 dict。"""
    from app.models import NarrativeRuleWeight

    rows = db.scalars(
        select(NarrativeRuleWeight).where(
            NarrativeRuleWeight.project_id == project_id
        )
    ).all()
    return {r.rule_hash: float(r.weight or 1.0) for r in rows}


def attach_rule_weights(db, profile: dict | None, project_id: int | None) -> dict | None:
    """把反馈权重挂到档案副本上（不改 `project.style_narrative` 本身）。

    返回**浅拷贝**：`project_narrative` 返回的可能是项目缓存那个 dict，直接原地
    加键会污染缓存（下次渲染拿到过期权重），也可能被 SQLAlchemy 当成脏数据。
    """
    if not profile or not project_id:
        return profile
    try:
        weights = load_rule_weights(db, project_id)
    except Exception:  # noqa: BLE001 —— 权重是增强，读不到照常渲染
        logging.getLogger(__name__).debug("读取叙事规则反馈权重失败", exc_info=True)
        return profile
    if not weights:
        return profile
    merged = dict(profile)
    merged["rule_weights"] = weights
    return merged


def _bump_rule_weight(db, project_id: int, rule_hash: str, *, hit: bool,
                      reason: str, config: RuleScoringConfig) -> None:
    from app.models import NarrativeRuleWeight

    row = db.scalars(
        select(NarrativeRuleWeight).where(
            NarrativeRuleWeight.project_id == project_id,
            NarrativeRuleWeight.rule_hash == rule_hash,
        )
    ).first()
    if row is None:
        row = NarrativeRuleWeight(
            project_id=project_id, rule_hash=rule_hash, weight=1.0,
            hits=0, misses=0,
        )
    if hit:
        row.hits = int(row.hits or 0) + 1
        row.weight = min(config.weight_max, float(row.weight or 1.0) + config.feedback_hit_step)
    else:
        row.misses = int(row.misses or 0) + 1
        row.weight = max(config.weight_min, float(row.weight or 1.0) - config.feedback_miss_step)
    row.last_reason = reason
    row.updated_at = datetime.now(timezone.utc)
    db.add(row)


def record_narrative_feedback(db, project, text: str, *, metric: dict | None = None,
                              config: RuleScoringConfig = DEFAULT_RULE_SCORING) -> int:
    """第四层：按生成正文实测指标调整规则权重，**落库**（零 LLM）。

    只核对「档案里真的有数字声称」的规则（书 7 这类规则只有 26 条，逐段跑
    成本可忽略）；没有数字的规则无法判定是否遵守，不调权。

    返回本次调整的规则条数。任何异常都不外抛——反馈是增强，不能拖垮生成。
    """
    profile = getattr(project, "style_narrative", None)
    if not isinstance(profile, dict):
        return 0
    try:
        if metric is None:
            from app.style.consistency import measure

            metric = measure(text or "")
        core = (metric or {}).get("core") or {}
        if not core:
            return 0
        generated: dict[str, float] = {}
        for metric_name, value in core.items():
            if value is None:
                continue
            if metric_name in ("短句占比", "长句占比", "对白字占比"):
                generated[metric_name] = float(value) * 100.0
            else:
                generated[metric_name] = float(value)

        rows: list[dict] = profile.get("feedback_rules") or []
        if not rows:
            # 老档案没有预计算索引：只扫有数字声称的条目（不遍历全部 3 万条）。
            for dim, items in (profile.get("rules") or {}).items():
                if dim in (CHARACTER_VOICE_DIMENSION, "文风漂移分析", "文笔量化审计"):
                    continue
                for item in items or []:
                    if _numeric_claims(_rule_text(item)):
                        rows.append({"dim": dim, "text": _rule_text(item)})

        updated = 0
        seen: set[str] = set()
        for row in rows:
            text_rule = row.get("text") or ""
            claims = _numeric_claims(text_rule)
            if not claims:
                continue
            rule_hash = hashlib.sha1(text_rule.encode("utf-8")).hexdigest()[:20]
            if rule_hash in seen:
                continue
            seen.add(rule_hash)
            checked = []
            for claim in claims:
                key = _FEEDBACK_METRIC_MAP.get(claim["metric"])
                actual = generated.get(key) if key else None
                if actual is None:
                    continue
                ok = abs(claim["value"] - actual) <= _claim_tolerance(
                    claim["metric"], claim["value"]
                )
                checked.append((claim, actual, ok))
            if not checked:
                continue
            bad = [(c, a) for c, a, ok in checked if not ok]
            if bad:
                claim, actual = bad[0]
                reason = (
                    f"模型未遵守：规则声称{claim['metric']} {claim['value']:g}，"
                    f"本段实测 {actual:.2f}，降权"
                )
                _bump_rule_weight(db, project.id, rule_hash, hit=False,
                                  reason=reason, config=config)
            else:
                claim, actual, _ = checked[0]
                reason = (
                    f"模型遵守：{claim['metric']} {claim['value']:g} ≈ 本段 {actual:.2f}，升权"
                )
                _bump_rule_weight(db, project.id, rule_hash, hit=True,
                                  reason=reason, config=config)
            updated += 1
        if updated:
            db.commit()
        return updated
    except Exception:  # noqa: BLE001 —— 反馈失败绝不拖垮生成
        logging.getLogger(__name__).warning("叙事规则反馈权重更新失败（已忽略）", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def _trim_to_budget(text: str, limit: int) -> str:
    """按总字数上限截断（在最后一个换行处切，避免切碎半条规则）。"""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < max(1, limit // 2):
        cut = limit
    return text[:cut].rstrip() + "\n…（已达本用途的字数上限，余下条目从略）"


#: 续写/角色对话用的 KG 时间线契约（KG 侧接口形状待定，先固定读取键）：
#:
#:   {
#:     "current_chapter": int,                      # 必填：当前写到第几章
#:     "events": [{"chapter_no": int, "summary": str}],  # 按章号排序，可乱序传入
#:     "open_plot_holes": [str],                    # 未回收伏笔
#:     "character_states": [{"name": str, "state": str}],  # 关键人物状态
#:     "relations": [str],                          # 角色对话要的关系摘要
#:   }
#:
#: 「时间线有两个来源」：文风侧的节拍/剧情编排/伏笔/漂移在档案里，KG 侧的
#: 事件序列由这里传入。续写缺 KG 部分必须报错，不能静默降级成只有文风。


def _render_kg_timeline(timeline: dict | None, preset: RenderPreset) -> str:
    """把 KG 侧时间线渲染成提示词块（原创不渲染，避免串味）。"""
    if preset.key == RENDER_MODE_ORIGINAL:
        return ""
    if not timeline:
        if preset.key == RENDER_MODE_CONTINUATION:
            raise ValueError(
                "续写渲染必须提供 KG 时间线（timeline）："
                "当前写到第几章、此前事件、未回收伏笔、关键人物状态；"
                "缺少 KG 侧会让续写只剩文风、接不上这本书走到哪了。"
            )
        return ""
    current = timeline.get("current_chapter")
    if preset.key == RENDER_MODE_CONTINUATION:
        if not isinstance(current, int) or current <= 0:
            raise ValueError("续写渲染的 timeline 必须包含正整数 current_chapter")

    lines: list[str] = []
    if preset.key == RENDER_MODE_CONTINUATION:
        events = [
            e for e in (timeline.get("events") or [])
            if isinstance(e, dict) and isinstance(e.get("chapter_no"), int)
            and (current is None or e["chapter_no"] <= current)
        ]
        events.sort(key=lambda e: e["chapter_no"])
        if events:
            lines.append(f"【剧情时间线（KG）：此前事件，截至第 {current} 章】")
            # 最近的 40 条足够接上上下文；更早的由档案里的剧情编排模式承担。
            for e in events[-40:]:
                lines.append(f"  · 第 {e['chapter_no']} 章：{e.get('summary', '')}")
        elif not (timeline.get("events") or []):
            # KG 空项目（尚未抽取任何剧情）：如实写明，避免模型把「没有时间线」
            # 误读成「可以自由编造此前事件」。生成侧 `_narrative_timeline` 会返回
            # 最小空时间线，而不是 None（None 在续写模式下会直接抛错）。
            lines.append(
                "【剧情时间线（KG）】尚无已抽取剧情：本书还没有可用的 KG 事件，"
                "续写请依据基线人物与大纲推进，不要编造未发生过的事件。"
            )
        holes = [h for h in (timeline.get("open_plot_holes") or []) if h]
        if holes:
            lines.append("【未回收伏笔（KG）】" + "；".join(holes[:12]))
        states = [
            s for s in (timeline.get("character_states") or [])
            if isinstance(s, dict) and s.get("name")
        ]
        if states:
            lines.append("【关键人物状态（KG）】")
            for s in states[:12]:
                lines.append(f"  · {s['name']}：{s.get('state', '')}")
    else:
        # 角色对话：只要人物近期状态与关系，不铺整条事件序列。
        states = [
            s for s in (timeline.get("character_states") or [])
            if isinstance(s, dict) and s.get("name")
        ]
        if states:
            lines.append("【人物近况（KG）：对话时保持一致】")
            for s in states[-8:]:
                lines.append(f"  · {s['name']}：{s.get('state', '')}")
        relations = [r for r in (timeline.get("relations") or []) if r]
        if relations:
            lines.append("【人物关系（KG）】" + "；".join(relations[:10]))
    return "\n".join(lines)


#: 量化审计常驻块：中位数 1 条 + 跨章实测 3 条（用户口径 3–5 条）。
AUDIT_BLOCK_ITEMS = 4


def _audit_item_body(item) -> str:
    text = _rule_text(item).strip()
    prefix = "本块实测："
    return text[len(prefix):] if text.startswith(prefix) else text


def _pick_audit_items(items: list, picks: int) -> list:
    """按章号分位取代表性实测块——开篇、中段、后段各留一个硬锚。"""
    positioned = []
    for it in items:
        ch = it.get("chapter_no") if isinstance(it, dict) else None
        if isinstance(ch, int) and ch > 0 and _rule_text(it).strip():
            positioned.append((ch, it))
    if not positioned or picks <= 0:
        return []
    positioned.sort(key=lambda pair: pair[0])
    n = min(picks, len(positioned))
    if n == 1:
        return [positioned[len(positioned) // 2][1]]
    idxs = [round(i * (len(positioned) - 1) / (n - 1)) for i in range(n)]
    chosen, seen = [], set()
    for idx in idxs:
        if idx in seen:
            continue
        seen.add(idx)
        chosen.append(positioned[idx][1])
    return chosen


def _format_audit_median(audit: dict[str, float],
                         include_short_para: bool = True) -> str:
    """把全书块级中位数压成一行可对账的硬锚。

    `include_short_para=False` 时摘掉「一两句短段 X%」——它会让模型按
    「大多数段只有一两句」去写，与段长/极短纯对白段硬指标反向。
    """
    if not audit:
        return ""
    parts = []
    if "句长" in audit:
        parts.append(f"句长 {audit['句长']:.1f} 字")
    if "短句占比" in audit:
        parts.append(f"短句(≤5字)占比 {audit['短句占比']:.1f}%")
    if "短句15占比" in audit:
        parts.append(f"短句(≤15字)占比 {audit['短句15占比']:.1f}%")
    if include_short_para and "短段占比" in audit:
        parts.append(f"一两句短段 {audit['短段占比']:.1f}%")
    if "对白占比" in audit:
        parts.append(f"对白占正文 {audit['对白占比']:.1f}%")
    if "对白开头段占比" in audit:
        parts.append(f"对白开头段 {audit['对白开头段占比']:.1f}%")
    if "短对白占比" in audit:
        parts.append(f"短对白 {audit['短对白占比']:.1f}%")
    if "情绪直陈" in audit:
        parts.append(f"情绪直陈 {audit['情绪直陈']:.2f}/千字")
    if "动作承载" in audit:
        parts.append(f"动作承载 {audit['动作承载']:.2f}/千字")
    if "问号" in audit:
        parts.append(f"问号 {audit['问号']:.2f}/千字")
    if "省略号" in audit:
        parts.append(f"省略号 {audit['省略号']:.2f}/千字")
    return "；".join(parts)


_SHORT_PARA_CLAUSE_RE = re.compile(
    r"(?:一两句短段|短段占比?)\s*[\d.]+%[，；]?"
)


def _strip_short_para_clause(text: str) -> str:
    """摘掉审计条目里的「一两句短段 X%」，保留同一条的其他实测数字。"""
    return _SHORT_PARA_CLAUSE_RE.sub("", text).strip("；， ")


def _render_audit_block(rules: dict, limit: int = AUDIT_BLOCK_ITEMS,
                        include_short_para: bool = True) -> str:
    """量化审计独立注入块：**不参与选条**（它不是风格规则）。

    这一维是全书唯一能被程序核对的东西：硬过滤里「必须有引号证据」和
    「跨书通用套话」两条判据本来就不适用于它，所以按 3–5 条常驻注入，
    不参加 MMR、不占风格规则配额。首行是全书中位数（最稳的锚），
    其余按章号分位取实测块，让模型看到「开篇 / 中段 / 后段」的数字都在同一量级。

    `include_short_para=False`：全书与分章两处都摘掉「一两句短段 X%」，
    其余硬锚（句长 / 短句 / 对白 / 问号）照常保留。
    """
    items = (rules or {}).get("文笔量化审计") or []
    if not items:
        return ""
    audit = _parse_audit_metrics({"rules": {"文笔量化审计": items}})
    median_line = _format_audit_median(audit, include_short_para=include_short_para)
    chapter_items = _pick_audit_items(items, max(limit - 1, 0))

    lines = [
        "【文笔量化审计：程序逐块实测的硬锚（非模型推测，写作时按此对标）】"
    ]
    if median_line:
        lines.append(f"  · 全书 {len(items)} 块中位数：{median_line}")
    for it in chapter_items:
        body = _audit_item_body(it)
        if not include_short_para:
            body = _strip_short_para_clause(body)
        if body:
            # 历史块存的是「短句占 X%」，没带阈值；渲染时统一补上 ≤5 字，
            # 否则同一块里中位数写了阈值、分章实测没写，模型仍会按常识理解。
            body = re.sub(r"短句占(?!\s*[（(])", "短句(≤5字)占", body)
            lines.append(f"  · 第 {it['chapter_no']} 章实测：{body}")
    if len(lines) == 1:
        return ""
    lines.append(
        "  口径：短句按整句≤5字（含句末标点）计，不是全文统计常用的<15字口径，勿混用。"
    )
    # 观测值 vs 目标值（2026-11-14）：这一段是「参考语料长什么样」的描述，
    # 模型曾把中位数当成本章要凑的比值来读，与用户提示里的绝对句数配额打架
    # （第 10 章实测：中位 14.7%，配额下限 5.3%，两个数差近三倍，结果两个都没执行）。
    lines.append(
        "  **以上是观测分布，不是目标值、也不是配额**：它只描述参考语料的样子；"
        "本章实际执行标准以用户提示中的配额数字为准，没有配额时才按各指标带内处理。"
    )
    lines.append("  以上数字由程序算出，不要再重新估数。")
    return "\n".join(lines)


def describe_narrative(profile: dict | None, max_rules_per_dim: int | None = None,
                       *, allowed_novel_ids: set[int] | list[int] | None = None,
                       opening: bool = False,
                       mode: str = RENDER_MODE_ORIGINAL,
                       timeline: dict | None = None) -> str:
    """把叙事档案翻成一段确定性文字，用于 system prompt 的静态【叙事档案】块。

    `mode` 决定配额（原创/续写/角色对话，见 `RENDER_PRESETS`），调用方必须显式传；
    传错直接抛 ValueError。`max_rules_per_dim` 是可选的全局硬上限（旧测试用），
    None = 完全按 preset。

    `opening=True`（首段）：在来源过滤之后，再剔除「文风漂移分析」里的压缩趋势，
    并在块首加一句「篇幅与完整性优先于简洁倾向」。档案本身不改。

    `timeline`：KG 侧的剧情时间线（见 `_render_kg_timeline` 的契约）。续写必填，
    缺了直接 ValueError；角色对话只用人物近况与关系；原创忽略（避免串味）。
    """
    preset = resolve_render_preset(mode)
    timeline_block = _render_kg_timeline(timeline, preset)
    if not profile:
        return timeline_block
    # 来源隔离：续写项目只留续写书自己的规则；原创项目按当前参考书集合取。
    profile = filter_rules_by_source(profile, allowed_novel_ids)
    rules = profile.get("rules") or {}
    # 全程生效：剔除与档案分位**数值相反**的节奏分句（旧实现按字面词表命中，
    # 换书会漏配/误删；第 3/4 章曾直接吃到「极短对白 + 单句成段 + 短段 86%」）。
    rules = _drop_reverse_pacing_rules(rules, audit=_parse_audit_metrics(profile))
    if opening:
        rules = _drop_compression_trends(rules)
    forbidden = profile.get("forbidden") or {}
    if not rules and not forbidden:
        return ""

    # 短段锚点（「一两句短段 X%」）同上：全程不注入，其余硬锚保留。
    audit_block = _render_audit_block(rules, include_short_para=False)
    lines: list[str] = []
    all_dims = NARRATIVE_DIMENSIONS + ["文风漂移分析"]
    voice_stats = profile.get("voice_stats") or {}
    for dim in all_dims:
        if dim == "文笔量化审计":
            # 量化审计不是风格规则：它没有引号证据、天生跨书通用，过不了为风格
            # 规则设计的硬过滤。改走下面的独立常驻块，不参与 MMR / 不占配额。
            continue
        items = rules.get(dim) or []
        if dim == CHARACTER_VOICE_DIMENSION:
            # 清污：职务词/地名/截断名、无说话证据的非人条目不进提示词。
            items = clean_voice_items(items)
        if not items:
            continue
        if dim == CHARACTER_VOICE_DIMENSION:
            limit = preset.voice_count
        elif dim == "文风漂移分析":
            # 每本书一条结论，条数 = 参考书数量，不套每维配额。
            limit = len(items)
        else:
            limit = preset.limit_for(dim)
            if max_rules_per_dim is not None:
                limit = min(limit, max_rules_per_dim)
        # 多书档案：给每条标出语料，否则几本书的趋势并排却分不清归属。
        # 只含一本书时保持原样（逐字不变）。
        drift_books = {
            it.get("novel_id") for it in items
            if isinstance(it, dict) and it.get("novel_id") is not None
        }
        tag_book = dim == "文风漂移分析" and len(drift_books) > 1
        # 非声音/非漂移维度走四层选条（硬过滤 → 四维打分 → MMR）。旧的分层选条
        # 只解决覆盖与证据，不解决「重要性」；打分缓存由蒸馏落库时预计算。
        if dim == CHARACTER_VOICE_DIMENSION:
            selected = _select_voice_items(
                items, voice_stats, limit=limit, order=preset.voice_order
            )
            selected = _promote_mapped_voices(selected, items, profile, limit)
        elif dim == "文风漂移分析":
            selected = items[:limit]
        elif dim == BEAT_DIMENSION:
            # 节拍：原创 280 不放宽；续写同书 480；跨书/原创超长走骨架化。
            selected = _select_beat_items(
                items, limit, mode=mode,
                allowed_novel_ids=allowed_novel_ids, profile=profile,
            )
        else:
            selected, _ = select_rules_with_audit(
                items, limit, dim=dim, profile=profile
            )
        if not selected:
            continue
        if dim == CHARACTER_VOICE_DIMENSION:
            available = {_voice_item_name(it) for it in selected}
            lines.extend(_render_voice_mapping(profile, available))
        lines.append(f"【{dim}】")
        for item in selected:
            if isinstance(item, dict):
                rule_text = item.get("rule", "")
                if tag_book and item.get("novel_id") is not None:
                    rule_text = f"[语料 {item['novel_id']}] {rule_text}"
                lines.append(f"  · {rule_text}")
            else:
                lines.append(f"  · {item}")

    # 反向禁忌：具体的「不要用 X」比通用的「忌 AI 腔」更有效
    forbidden_lines: list[str] = []
    for dim in FORBIDDEN_DIMENSIONS:
        items = forbidden.get(dim) or []
        if not items:
            continue
        # 去重保序
        seen = set()
        unique = []
        for x in items:
            if x not in seen:
                seen.add(x)
                unique.append(x)
        forbidden_lines.append(
            f"【{dim}】禁止使用：{'、'.join(unique[:preset.forbidden_per_group])}"
        )

    if not lines and not forbidden_lines and not timeline_block and not audit_block:
        return ""
    parts = []
    if opening:
        # 放在最前：先立「篇幅优先」，再读后面的手法规则，避免整块被读成「写得越短越好」。
        parts.append(_OPENING_LENGTH_PRIORITY)
    if audit_block:
        # 紧跟篇幅句：硬锚要最先被读到，也避免被尾部裁剪吃掉。
        parts.append(audit_block)
    if lines:
        parts.append("【叙事档案：从参考语料提炼，生成时请遵循以下叙事手法】\n" + "\n".join(lines))
    if timeline_block:
        parts.append(timeline_block)
    if forbidden_lines:
        parts.append("【反向禁忌：参考作者从不使用以下表达，严禁出现在生成文本中】\n" + "\n".join(forbidden_lines))
    # 总字数上限：只压「叙事档案」正文，保留篇幅优先句与反向禁忌块。
    text = "\n".join(parts)
    if len(text) > preset.char_limit and lines:
        others = sum(len(p) + 1 for p in parts if not p.startswith("【叙事档案"))
        budget = max(1, preset.char_limit - others)
        idx = next(i for i, p in enumerate(parts) if p.startswith("【叙事档案"))
        parts[idx] = _trim_to_budget(parts[idx], budget)
        text = "\n".join(parts)
    return _trim_to_budget(text, preset.char_limit)


def extract_character_template(
    db: Session,
    project: Any,
    novel_ids: list[int],
    *,
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
    provider=None,
) -> Any:
    """提炼人物塑造模板（独立产物，按档位分开存放）。

    为什么独立：叙事档案一次完整档要 42 分钟，而模板只要每块一次调用
    （约 1/8 的量）。单独抽、单独存，不需要为了加这一块方法论把整本档案重蒸。

    复用与叙事档案同一套采样与分块（同一档位 → 同一批章节 → 同一档位签名），
    两条产物的 id 前缀一致，一眼能看出「这档的档案」与「这档的模板」配对。

    存 `character_templates` 表，id = `<档位>:<档位签名>:<正文哈希前 8>`。
    同一个 (语料, 档位, 档位配置, 正文) 永远只有一行；命中已有就直接应用。
    """
    from app.style.character_template import (MAX_TEMPLATE_RULES, apply_template,
                                             entity_free_rules, find_template,
                                             parse_character_template, save_template)
    from app.style.character_template import build_character_template_prompt

    cfg = tier if isinstance(tier, NarrativeTier) else resolve_tier(tier)
    ids = [int(i) for i in (novel_ids or [])]
    if not ids:
        raise ValueError('missing_reference_corpus')
    content_hash = corpus_content_hash(ids, db)
    source_ids_hash = _source_ids_hash(ids)
    tier_sig = _tier_signature(cfg)

    existing = find_template(db, tier=cfg.key, tier_signature=tier_sig,
                             content_hash=content_hash)
    if existing is not None:
        apply_template(db, project, existing)
        if progress is not None:
            progress("命中已有的人物塑造模板，无需重抽", 1, 1)
        return existing

    if provider is None:
        provider = resolve_distill_provider()

    kept_rules: list[str] = []
    rejected_total = 0
    blocks_done = 0
    blocks_seen = 0
    books_seen = 0
    est_total = 0
    total_books = len(ids)

    for book_idx, nid in enumerate(ids, start=1):
        chapters = _load_novel_chapters(db, nid)
        if not chapters:
            continue
        sampled = sample_chapters_for_tier(chapters, cfg)
        # 块大小跟渠道走（与叙事档案同一把尺），见 `_resolve_block_chars`。
        block_chars = _resolve_block_chars(provider)
        block_size = max(
            MIN_CHAPTERS_PER_CHUNK,
            _chapters_per_block(sampled, block_chars),
        )
        chunk_starts = list(range(0, len(sampled), block_size))
        n_chunks = len(chunk_starts)
        books_seen += 1
        blocks_seen += n_chunks
        avg_chunks = blocks_seen / books_seen if books_seen else float(n_chunks)
        est_total = max(int(round(blocks_done + n_chunks + (total_books - book_idx) * avg_chunks)),
                        blocks_done + n_chunks)

        prompts: list[tuple[str, str, dict | None]] = []
        chunk_texts: list[str] = []
        for i in chunk_starts:
            chunk = sampled[i: i + block_size]
            chunk_text = "\n\n".join(t for _, t in chunk if t)
            if not chunk_text:
                continue
            system, user = build_character_template_prompt(
                chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
            prompts.append((system, user, None))
            chunk_texts.append(chunk_text)
        if not prompts:
            continue

        try:
            raws = _gather_chat(provider, prompts)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "人物塑造模板全 chunk 失败 novel_id=%s: %s", nid, exc)
            raws = [Exception(str(exc))] * len(prompts)

        raw_rules: list[str] = []
        for raw, chunk_text in zip(raws, chunk_texts):
            if isinstance(raw, Exception):
                continue
            raw_rules.extend(parse_character_template(raw))

        # 专名表过滤（没有表就整块不产 —— 宁可没有，不要泄漏）。
        entity_names: list[str] = []
        try:
            from app.style.entity_store import ensure_table, entity_entries
            entity_names = entity_entries(ensure_table(db, nid, provider=provider))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "人物塑造模板：专名表不可用 novel_id=%s（%s），本块不产", nid, exc)
            entity_names = []
        if not entity_names:
            rejected_total += len(raw_rules)
        else:
            kept, dropped = entity_free_rules(raw_rules, entity_names)
            rejected_total += dropped
            kept_rules.extend(kept)

        blocks_done += n_chunks
        if progress is not None:
            progress(f"语料 {book_idx}/{total_books}：提炼人物塑造模板",
                     blocks_done, est_total)

    # 去重（不同块常给出同一条做法）+ 总量上限（档案不该无限膨胀）。
    unique: list[str] = []
    seen: set[str] = set()
    for rule in kept_rules:
        if rule in seen:
            continue
        seen.add(rule)
        unique.append(rule)
    rules = unique[:MAX_TEMPLATE_RULES]

    row = save_template(
        db, project, tier=cfg.key, tier_signature=tier_sig,
        content_hash=content_hash, source_ids=ids, source_ids_hash=source_ids_hash,
        rules=rules, n_chunks=blocks_done, rejected=rejected_total)
    apply_template(db, project, row)
    if progress is not None:
        progress("人物塑造模板已就绪", blocks_done, max(blocks_done, 1))
    return row
