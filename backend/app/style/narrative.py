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
from datetime import datetime, timezone

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

#: 进度回调签名：`(阶段人话文本, 已完成数, 总数)`。
#:
#: 只用于**上报**蒸馏进度（后台任务的进度条），不参与任何蒸馏决策。
#: 不传时整个蒸馏链路一行不改、行为与加进度之前逐字节一致 —— 生成路径
#: (`project_narrative_safe`) 走的就是不传的那条路。
#:
#: `总数` 允许为 0，含义是「还没探明」：蒸馏要等语料加载、采样之后才知道
#: 一共有多少块。前端据此渲染不确定态，绝不拿 0 当分母。
ProgressCallback = Callable[[str, int, int], None]

#: 叙事档案的形状修订号。**任何会改变档案字段的改动都必须把它 +1。**
#:
#: 与 `style/pipeline.py` 的 `PIPELINE_SCHEMA` 同理：指纹只有 `source_ids + narrative_version`，
#: 管不到「档案长什么样」。加字段时老档案不会自动失效，前端拿到的摘要会永远少一块。
NARRATIVE_VERSION = 9

#: 采样比例：从全文章节中采样的百分比。
#: 603 章 × 30% = 181 章。原为 15%，2026-10-19 提高到 30%——
#: 理由是 15% 下长篇语料切出的留出单元太少（焚天录 588 万字 15% → 958 章但
#: 按 bucket 切分后 short/medium/long 留出数仍不足 20），percentile 不可用；
#: 30% 让留出样本量翻倍，同时覆盖面更充分。成本随之翻倍。
SAMPLE_RATIO = 0.30

#: 采样的字数下限：短篇/短语料若按比例采样会砍掉过多对白，
#: 导致角色声音维度失效（实测：10 章语料只提得出 1 个角色声音）。
#: 采样结果若低于此字数，则扩容到达到下限；若全量仍不足，则全取。
#: 30 万字 ≈ 100+ 章网文，足够覆盖角色对白与场景多样性。
MIN_SAMPLE_CHARS = 300_000

#: 每块最少章节数（太少提炼不出模式）
MIN_CHAPTERS_PER_CHUNK = 5

#: 每个蒸馏块的目标字数。
#:
#: 为什么按字数而不是章数：不同书章长差 3-5 倍（有的 1500 字/章，
#: 有的 8000 字/章）。固定章数会让某些书的块是别的书的 5 倍大，
#: prompt 要么撑爆上下文，要么信息量太少。
#:
#: 2 万字：实测该长度下模型能稳定输出 7 维规则且不截断；
#: 再往上（4 万字）rejected 明显上升。
TARGET_BLOCK_CHARS = 20_000


def _chapters_per_block(
    chapters: list[tuple[int, str]], target_chars: int,
) -> int:
    """估算「装够 target_chars 字需要多少章」。

    按中位章长算而不是均值——均值会被少数超长章拉高，
    导致块里装的章数偏少。
    """
    lens = sorted(len(t) for _, t in chapters if t)
    if not lens:
        return MIN_CHAPTERS_PER_CHUNK
    median = lens[len(lens) // 2]
    if median <= 0:
        return MIN_CHAPTERS_PER_CHUNK
    return max(MIN_CHAPTERS_PER_CHUNK, target_chars // median)

#: 场景类型关键词（用于场景均衡采样）。
#:
#: ## v2.286：从「题材词表」改成「叙事功能词表」
#:
#: 旧版是 6 类都市题材词（病房/宴会/办公室/车内/街道/家中），实测覆盖率
#: **2/6**：
#:
#:     武侠（北荒刀歌）→ 其他    玄幻（焚天录）→ 其他
#:     科幻        → 其他    历史        → 其他
#:     都市        → 办公室  校园        → 办公室（误判）
#:
#: 武侠/玄幻/科幻/历史 100% 落入「其他」，场景均衡采样对这些题材完全失效。
#: 而北荒刀歌本身就不被覆盖——这个项目从头到尾都在用它。
#:
#: ## 新判据：按「叙事的空间与动作功能」分，不按题材分
#:
#: 任何题材的小说都有这六类场景，因为它们对应六种不同的叙事功能：
#:
#:     对话/交涉  两人以上在交谈（信息交换、关系推进）
#:     移动/赶路  人物在空间中位移（转场、时间流逝）
#:     居所/日常  室内静止场景（人物状态、生活细节）
#:     对峙/冲突  身体或意志的对抗（张力顶点）
#:     集市/公共  人多场所（社会关系、信息扩散）
#:     独处/内省  单人无对白（心理、回忆、决断）
#:
#: 关键词都是跨题材的通用动作/空间词。武侠的「客栈」和科幻的「舰桥」
#: 都归「集会/公共」，玄幻的「山谷」和都市的「街道」都归「移动/赶路」。
SCENE_KEYWORDS = {
    # ⚠️ 每条只放**专属空间/物件名词**，不放通用叙事动词。
    #
    # 实测教训：首版把「说道/问道/笑道/开口」放进「对话/交涉」，
    # 结果该类均密度 2.33，其余五类全在 0.6 以下——它把别的类全挤掉了，
    # 78.9% 的章节都被判成「对话」。而「有人说话」不是场景特征，
    # 任何场景都会有人说活。场景由**发生在什么空间、涉及什么物件**决定。
    #
    # 同样地，「独处/内省」不放「想起/沉默」——那是任何章节都会有的心理描写，
    # 只放「独自/孤身/无人」这类**排他性**表述。
    "对话/交涉": ["议事厅", "大堂", "正厅", "书房", "宴席", "酒桌", "茶桌",
                "客座", "上首", "末座", "拱手", "抱拳", "行礼", "落座",
                "屏风", "案几", "堂下", "帐中"],
    "移动/赶路": ["赶路", "上路", "策马", "骑马", "驱车", "赶车", "步行",
                "小路", "大道", "山路", "山道", "官道", "驿道", "栈道",
                "城门", "渡口", "码头", "山门", "辕门", "官船", "马车",
                "行囊", "包袱", "干粮"],
    "居所/日常": ["院子", "庭院", "屋内", "房中", "厅堂", "卧房", "客栈",
                "酒楼", "府邸", "宅子", "宫殿", "洞府", "营帐", "舱室",
                "灶房", "廊下", "台阶", "床榻", "屏风", "窗棂", "灯烛"],
    "对峙/冲突": ["拔刀", "出剑", "亮刀", "交手", "过招", "厮杀", "劈下",
                "刺来", "刀锋", "剑光", "拳风", "掌风", "血溅", "重伤",
                "刀鞘", "剑鞘", "兵刃", "杀气", "杀意"],
    "集会/公共": ["众人", "人群", "围观", "满座", "集市", "街市", "广场",
                "校场", "拍卖场", "擂台", "告示", "榜文", "传开", "议论",
                "看客", "摊贩", "酒肆", "茶楼"],
    "独处/内省": ["独自", "孤身", "一个人", "无人处", "夜深", "四更",
                "檐下", "月下", "窗前", "灯下", "独坐", "枯坐"],
}
#: 叙事档案要覆盖的维度（与 writer-novel-style-distiller 的维度设计对齐）
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
    "高频场景模板",
    "潜台词模式",
    # —— v2.292 新增：战斗描写 ——
    # 实测缺口：570 条规则里只有 3 条真正讲「战斗怎么写」，其余 52 条只是
    # 提到兵器/打斗情节。不同作者写战斗差别极大——一招一式拆解、蒙太奇快切、
    # 环境破坏、旁观者侧写、心理博弈——这些差异现有维度抓不住。
    "战斗描写",
    # —— v2.283 新增：语序编排 ——
    # 现有维度讲「句子怎么写」（句长/节奏/段长），没有一个讲「信息按什么顺序给」。
    # 而顺序是比句长更本质的文风特征：同样一段场景，先写环境还是先写人、
    # 对白前垫动作还是后补动作，读起来是完全不同的作者。
    "语序编排",
]

#: 生成提示词里每个维度的自然语言标签。
#:
#: ## 为什么要分维度渲染（v2.290）
#:
#: 旧版把 13 个维度各取 2 条规则，用「；」「、」缝成**一段 894 字的话**：
#:
#:     参考作者的行文习惯是：用夸张的外貌反衬人物内在…；人物之间大量使用带地域
#:     色彩的称呼…；极短段占绝对主导…；空间切换以人物走位作为硬切标志…；
#:     铺垫、升温；来客提出安排→主人预先铺垫→目标者拒绝或僵持→…；反话正说、
#:     转移话题；场景开场先交代空间格局与社会声量…
#:
#: 维度边界完全消失。模型分不清哪句是节拍、哪句是场景套路、哪句是语序，
#: 只能抓住最显眼的动作链反复执行——实测五章里第 3/4/5 章全部困在
#: 「陈叔爬船」这同一个动作上（关键词「船」出现 36/26/40 次）。
#:
#: ## 为什么不恢复 v2.274 的 `【维度】` 小标题
#:
#: v2.274 测过三种格式，结论是「散文式」最好：
#:
#:     【维度】小标题      8331 字，58% 段落重复，正文里出现 83 个自造小标题
#:     `· 维度：` 列表      2657 字，10% 重复
#:     散文式一段话        2214 字，0% 重复
#:
#: 但那个结论针对的是**方括号标记**——模型把 `【人物描写手法】` 当成了
#: 「正文要写的小节标题」，逐条演示一遍然后循环。问题出在括号的形态，
#: 不是「分维度」本身。
#:
#: 所以这里用**不带任何标记符号**的自然语言标签（`人物——…`），
#: 每维独占一行。模型读到的是句子，不是待填的清单项。
DIM_LABELS: dict[str, str] = {
    "人物描写手法": "人物",
    "对白风格": "对白",
    "段落编排": "分段",
    "场景构建": "场景",
    "情绪表达": "情绪",
    "节奏控制": "节奏",
    "世界观呈现": "设定",
    "剧情编排模式": "情节",
    "伏笔与悬念设计": "伏笔",
    "节拍序列": "节拍",
    "高频场景模板": "场景套路",
    "潜台词模式": "潜台词",
    "战斗描写": "打斗",
    "语序编排": "语序",
}

#: 每个维度在提示词里的最大字数。超过就按句截断——
#: 一条规则动辄 80-200 字（M3 蒸馏），全放进去一句就占半屏。
_DIM_MAX_CHARS = 42

#: 每个维度注入提示词的规则条数。
#:
#: 蒸馏一次产出几千条规则，进提示词的却只有每维 2 条 —— 实测真书
#: 3158 条产出、51 条入提示（1.6%）。用户要求「让蒸馏产出更多被用上」，
#: 这里提到 5 条（每维 2 → 5）。
#:
#: **代价**：档案块变长，system 提示词跟着变长（静态块，命中缓存，
#: 每次生成多发几百 token）；每维行变长，模型照着「列技法」写的风险略升。
#: 所以上限 `_ARCHIVE_MAX_CHARS` 必须同步抬 —— 否则多出来的规则会把
#: 靠后的维度（潜台词/语序）整条挤掉，等于左手换右手。
RULES_PER_DIM = 5

#: 整个叙事档案块的字数上限。超出按 DIM_LABELS 的插入顺序丢弃靠后的维度。
#: 靠后的维度（潜台词/语序）对文风的边际贡献小于人物/对白，所以先丢它们。
#: 3000 = 13 维 × 5 条 × 42 字 + 标签与分隔符，够装下全部维度。
_ARCHIVE_MAX_CHARS = 3000


def _clip(text: str, limit: int = _DIM_MAX_CHARS) -> str:
    """把一条规则压到 limit 字以内，优先在**语义边界**断开。

    ## 为什么「→」排在标点前面

    动作链（场景套路维度）的步与步之间是「→」。被拦腰截断会得到
    「…达成有限共识或继续周旋→一」这种半句话 —— 模型照着写会写出断掉的链子。
    所以先找链子边界：断在步与步之间，再把尾巴的箭头去掉（「A→B→」→「A→B」），
    剩下的仍然是一条**完整可读**的链子前缀。
    """
    t = text.strip()
    if len(t) <= limit:
        return t
    head = t[:limit]
    # 从后往前找最近的边界，避免切在词中间
    for sep in ("→", "，", "、", "；", "。", "：", " "):
        i = head.rfind(sep)
        if i >= limit // 2:
            cut = head[:i].rstrip("→、，；。： ")
            return cut or head
    return head


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

#: 反向禁忌段在**渲染块**里的起始标记。
#:
#: 为什么要有这个常量：提示词体检要判断「禁用词有没有出现在**规则正文**里」，
#: 就必须把渲染块里那一段**切掉** —— 渲染块本身含禁用清单
#: （`【禁用转折词】禁止使用：然而、但是、不过…`），拿整块去比等于
#: **禁用清单和自己匹配**，恒真。实测：真书档案上这条检查必然误报。
FORBIDDEN_SECTION_MARKER = '【反向禁忌'


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
#:     语料 2《长夜司》833 章 → 采样 764 章 → 耗时 42 分钟
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
    TIER_FULL: "最高（完整蒸馏：按语料量百分比采样、全维度、含角色名归一化）",
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
            # 相对耗时档位（非实测分钟数，理由见 _COST_HINTS 注释）
            "cost_hint": _COST_HINTS[self.key],
        }


#: 快速档砍掉的维度。都是 v5+ 才引入的「增量能力」，对应 docs 里 v3 档的
#: 实测维度数 11（14 − 3）。砍这些 = 每块少几次 LLM 调用。
#
#: v2.283 加入「语序编排」：它和那三个同属增量能力（每 chunk 多一次调用），
#: 且它要求的「顺序判断」比「句长判断」更依赖模型能力——快速档本来就是
#: 「只验证管线通不通」，不需要它。
_FAST_TIER_DROP_DIMENSIONS = (
    "节拍序列", "高频场景模板", "潜台词模式", "语序编排", "战斗描写",
)


TIER_REGISTRY: dict[str, NarrativeTier] = {
    TIER_FULL: NarrativeTier(
        key=TIER_FULL,
        name="完整",
        description="生产用。按语料量百分比采样，全维度，含语料自证的角色名归一化与全量伪角色清洗。最准也最慢。",
        sampling_mode="percentage",
        sample_ratio=SAMPLE_RATIO,
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


def _load_novel_chapters(db: Session, novel_id: int) -> list[tuple[int, str]]:
    """加载一本语料的全部章节，返回 [(chapter_no, text), ...]，按章节号排序。

    ## v2.288：真正按章节聚合，不再返回 chunk 列表

    旧实现查 `NovelChunk` 后直接返回，**每行是一个 chunk（约 1000 字）**，
    不是章节（约 5500 字）。函数名与行为不符，而这个不符造成了一个
    静默失效：

    下游 `_sample_chapters_v6` 用这些条目做「场景均衡采样」，
    拿 1000 字的 chunk 去算场景密度——各类型密度都在 0.1-0.4/千字，
    低于按 chunk 分布算出的 P70 线，于是 **100% 的 chunk 被判成「其他」**，
    场景均衡完全失效（实测 5323/5323）。

    改为按 `chapter_no` 聚合：同一章的多个 chunk 拼回完整正文。
    chunk 之间只有相邻同章且有充分长度的确切 suffix/prefix 重叠才裁剪；
    完整重复、短重复、跨章或缺失相邻 idx 的片段原样保留。

    `chapter_no` 为空的 chunk（解析异常）单独成组，保留原文不丢。
    """
    from app.models import NovelChunk
    from app.ingestion.clean import clean_consumption_text

    rows = db.scalars(
        select(NovelChunk)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.chapter_no, NovelChunk.idx)
    ).all()

    from app.retrieval.views import stitch_chunks
    grouped = {}
    for r in rows:
        key = r.chapter_no if r.chapter_no is not None else -int(r.id)
        grouped.setdefault(int(key), []).append(r)
    return [(k, stitch_chunks(v)[0]) for k, v in sorted(grouped.items())]


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
    sents = [s.strip() for s in re.split(r"(?<=[。！？…])", chunk_text) if s.strip()]
    sent_lens = [len(s) for s in sents] or [0]
    para_lens = [len(p) for p in paras]
    n = len(chunk_text)
    per1k = 1000.0 / n if n else 0.0

    # 对白：引号内容
    dialogues = re.findall(r"[“「]([^”」]{1,500})[”」]", chunk_text)
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

    return {
        "句长": {
            "均值": round(mean_sl, 1),
            "标准差": round(variance ** 0.5, 1),
            "短句占比": round(sum(1 for x in sent_lens if x <= 5) / len(sent_lens), 3),
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
    return (
        "【本块客观统计（系统计算，非模型推测）】\n"
        f"句长 {sl.get('均值')}±{sl.get('标准差')} 字，短句(≤5字) {sl.get('短句占比', 0):.1%}，"
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
        f"短句占 {sl.get('短句占比', 0):.1%}",
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
        "2. 规则描述后面括号里附**原文证据**——引用一句原文（≤20 字）证明这条规则存在\n"
        "3. 规则要**具体可操作**，例如「用连续动作描写紧张感」而不是「文笔好」\n"
        "4. 只提炼原文中**确实反复出现**的模式，不要编造\n"
        "5. 按以下维度分类输出\n\n"
        "**正确的规则示例**：\n"
        "  人物描写手法: [\"用动作和神态暗示情绪，不直接写'害怕/紧张'（证据：指尖不自觉收紧）\", "
        "\"对白后紧跟动作描写，不用'说道'收尾（证据：南宫爵沉声道）\"]\n"
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
    - 输出 JSON 体更小，截断概率从约 1.3% 降到 0.4%（北荒刀歌 990 章实测推算）
    """
    system = (
        "你是一位资深中文网文编辑，专精**人物与对话分析**。\n\n"
        "本任务只提炼 3 个维度，全部围绕**人物视角**：\n"
        "1. **人物描写手法**：作者怎么描写人物（动作/神态/外貌/心理）\n"
        "2. **对白风格**：作者怎么写对话（句长/敬语/称谓/动作搭配）\n"
        "3. **角色声音词典**：具体角色的说话习惯（口头禅/自称/语气/句长）\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，不要直接复制原文\n"
        "2. 规则后面括号里附**原文证据**——引用一句原文（≤20 字）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. **角色声音词典**里必须写出具体角色名，不能用「某角色」\n\n"
        "**正确示例**：\n"
        "  人物描写手法: [\"用动作暗示心理，不直接写内心独白（证据：指尖不自觉收紧）\"]\n"
        "  角色声音词典: [\"陆长风：口头禅「本世子」语气轻佻 句长短句 关系：对下属命令式（证据：本世子今日高兴）\"]\n\n"
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
        "本任务只提炼 7 个维度，全部围绕**叙事结构与文风技法**：\n"
        "1. **段落编排**：段长分布、段间过渡、对话与叙述的穿插\n"
        "2. **场景构建**：环境描写方式、空间切换手法\n"
        "3. **情绪表达**：情绪承载方式（动作/环境/直陈的取舍）\n"
        "4. **节奏控制**：情节推进的快慢、张弛节奏\n"
        "5. **世界观呈现**：设定如何透露给读者（对话/旁白/动作）\n"
        "6. **剧情编排模式**：伏笔埋设、冲突升级、转折手法\n"
        "7. **伏笔与悬念设计**：悬念如何设置、揭示、回收\n\n"
        "规则要求：\n"
        "1. 每条规则必须是**抽象的写作手法描述**，不要直接复制原文\n"
        "2. 规则后面括号里附**原文证据**——引用一句原文（≤20 字）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. 这 7 维关注的是「作者怎么写」而不是「写了什么」\n\n"
        "**正确示例**：\n"
        "  段落编排: [\"短段切对话节奏，长段铺环境氛围（证据：北荒王府龙盘虎踞于清凉山）\"]\n"
        "  节奏控制: [\"冲突升级通过对话长度骤变，从短对白跳到大段独白（证据：石昀说了一句……，然后……）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  段落编排: [\"北荒王府龙盘虎踞于清凉山，千门万户，极土木之盛。\"]\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "段落编排": ["抽象规则（证据：原文引用）", ...],\n'
        '  "场景构建": ["抽象规则（证据：原文引用）", ...],\n'
        '  "情绪表达": ["抽象规则（证据：原文引用）", ...],\n'
        '  "节奏控制": ["抽象规则（证据：原文引用）", ...],\n'
        '  "世界观呈现": ["抽象规则（证据：原文引用）", ...],\n'
        '  "剧情编排模式": ["伏笔埋设/冲突升级/转折手法（证据：原文引用）", ...],\n'
        '  "伏笔与悬念设计": ["悬念如何设置/揭示/回收（证据：原文引用）", ...]\n'
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


def _build_word_order_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造「语序编排」提取 prompt（v2.283 新增维度）。

    ## 为什么要这一维

    现有 14 维讲的是「句子怎么写」——句长多长、节奏快慢、段落多长。
    没有一维讲「信息按什么顺序给」。

    但顺序是比句长更本质的文风特征。同一个场景，两种写法：

        甲：风从山口灌进来，吹得火堆乱颤。沈砚抬头看了一眼，又低下去。
        乙：沈砚抬头看了一眼，又低下去。风从山口灌进来，吹得火堆乱颤。

    两段字面几乎一样，读起来的「作者」完全不同：甲先给环境（镜头从远到近），
    乙先给人（镜头跟着人物走）。这种差别，句长指标测不出来，但它决定读者
    是否觉得「像同一个作者写的」。

    ## 五个问题

    这五问覆盖了从场景级到句子级的顺序决策。每题都要模型给**可迁移**的
    描述，不给具体内容（与另外三个剧情型维度的约束一致）。
    """
    system = (
        "你是一位资深中文网文编辑，专精**叙事语序**分析。\n\n"
        "你要回答的是「作者把信息按什么顺序给读者」，不是「作者写了什么」。\n\n"
        "## 关键约束：只写顺序规则，不写内容\n\n"
        "你的输出会被用来指导**另一本小说**，那本书的人物地点事件都不同。所以：\n"
        "- 不要写具体人名、地名、物件名\n"
        "- 不要复述这段文字写了什么\n"
        "- 每条规则必须**可迁移**：换一组人物、换一个场景依然成立\n\n"
        "## 要回答的五个问题\n\n"
        "1. **场景开场顺序**：先给空间/环境，还是先给人物动作，还是先给声音，"
        "还是直接甩一句对白？\n"
        "2. **对白与动作的相对位置**：动作在前铺垫（「他把碗放下，说」）、"
        "动作在后补充（「他说，把碗放下」）、还是穿插在两句话之间？\n"
        "3. **段内信息顺序**：从宏观拉到细节（远景→近景），还是从细节推到宏观"
        "（一个动作→整个环境）？\n"
        "4. **场景切换方式**：空行硬切、用一句过渡交代时间/位移、还是靠人物走动带出？\n"
        "5. **时间推进方式**：顺叙为主、插叙补充往事、还是先给结果再回溯过程？\n\n"
        "## 正反例\n\n"
        "**正确的规则**（描述顺序，可迁移）：\n"
        "  场景开场先给一个声音或气味，再让主角的动作出现——读者先被刺激，"
        "才知道是谁在场\n"
        "  对白前置、动作后补：人物先开口，说完才给一个身体动作，"
        "让动作像是话的余震\n"
        "  段内从细节推远：先写一个具体物件的局部，再退到整个空间\n\n"
        "**错误的规则**（含具体内容，严禁）：\n"
        "  沈砚抬头看了一眼，风从山口灌进来，吹得火堆乱颤\n"
        "  先写北荒王府的门，再写陆长风站在门口\n\n"
        "## 输出格式（JSON）\n\n"
        '{\n'
        '  "语序编排": [\n'
        '    "规则描述（证据：原文引用 ≤20 字）",\n'
        '    ...\n'
        '  ]\n'
        '}\n'
        "每类问题给 1-2 条，总数 4-8 条。证据只用来证明这条规则存在，"
        "它本身不是规则内容。"
    )
    user = (
        f"以下是《{chunk_label}》的片段，请分析作者的语序编排习惯：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。**只写顺序规则，不要复述这段文字的内容。**"
    )
    return system, user


#: 战斗描写规则里的纯噪声前缀（模型照抄 prompt 模板留下的）。
#: 只剥「规则描述/规则内容/规则」三个通用词；
#: 「拆解粒度」「节奏」「视角」这类分类标签**保留**，它们说明规则讲的是哪个方面。
_COMBAT_NOISE_PREFIX_RE = re.compile(r"^(?:规则描述|规则内容|规则|写法)[：:]\s*")


def _build_combat_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造「战斗描写」提取 prompt（v2.292 新增维度）。

    ## 为什么要这一维

    实测当前 570 条规则里只有 3 条真正讲「战斗怎么写」：

        战斗采用招式解释、双方试探、暂停判断、再起杀招的层层递进，避免一次性宣泄
        战斗场景不预设全貌，而按距离变化逐层显露对手、兵器与杀招
        刺杀的快节奏被一段极长的内心追忆打断，用延时回忆把节奏拉回心理时间

    其余 52 条「战斗相关」规则只是提到兵器或打斗情节，讲的不是写法。

    而战斗场面的风格差异极大，同一场打斗可以有五种完全不同的写法：

        一招一式拆解   报招式名、拆招、防守反击，节奏慢但精确
        蒙太奇快切     动作碎片、短句并置、视角跳切，节奏极快
        环境破坏       借地形与器物损毁表现力量，不细写招式
        旁观者侧写     写旁观者的震惊反应，正面战斗一笔带过
        心理博弈       打之前的判断、试探、气势压制占大头

    这五种读者感受完全不同，但句长/节奏这些统计指标测不出来。
    缺了这一维，生成战斗段落时只能靠「场景构建」顺带写一句，
    写出来是通用网文打斗，不是这个作者的打斗。

    ## 约束（与另外三个剧情型维度一致）

    只写**手法结构**，不写具体内容：

    - 不写招式名（招式名是那本书的专有概念，换书无意义）
    - 不写人名地名门派名
    - 不复述这一场战斗发生了什么
    - 每条必须可迁移：换一组人物、换一种兵器依然成立
    """
    system = (
        "你是一位资深中文网文编辑，专精**战斗场面写法**分析。\n\n"
        "你要回答的是「作者怎么写打斗」，不是「这场打斗谁赢了」。\n\n"
        "## 关键约束：只写写法，不写内容\n\n"
        "你的输出会被用来指导**另一本小说**，那本书的人物、门派、招式、兵器全都不同。所以：\n"
        "- 不要写具体人名、地名、门派名、招式名、兵器专名\n"
        "- 不要复述这段文字里谁和谁打了、打了几个回合、结果如何\n"
        "- 每条规则必须**可迁移**：换一组人物、换一种兵器依然成立\n"
        "- 用「作者」作主语，不用「这场战斗」作主语\n\n"
        "## 要回答的八个问题\n\n"
        "1. **拆解粒度**：一招一式逐招写（报动作、报方位、报结果），"
        "还是整体快切（跳过过程只给关键节点），还是只写结果？\n"
        "2. **节奏**：一次交锋用几个句子？长短句如何交替？"
        "打斗中途会不会突然停顿（观察、对话、回忆）？\n"
        "3. **视角**：始终贴主角，还是切到对手，还是用旁观者视角侧写？"
        "会不会在打斗中切到远处的某个人？\n"
        "4. **力量如何表现**：靠招式效果、靠环境损毁（墙塌、地裂）、"
        "靠对手的反应（吐血、后退）、靠器物状态（刀卷刃）？\n"
        "5. **语言特征**：短句为主还是长句？用不用拟声词？"
        "用不用术语（内力、真气、境界）？会不会用古语或方言？\n"
        "6. **开打前怎么蓄势**：直接开打，还是先写对峙、试探、言语交锋、气势压制？\n"
        "7. **怎么收尾**：干净利落一刀结束，还是拖到力竭，还是被第三方打断？"
        "胜负揭晓后立刻转场，还是留一段余波？\n"
        "8. **战斗中还投喂什么信息**：借打斗交代人物关系、往事、设定，"
        "还是纯粹只写打？\n\n"
        "## 正反例\n\n"
        "**正确的规则**（描述写法，可迁移）：\n"
        "  打斗以极短句堆叠推进，每句只给一个动作，句子之间不加连词，"
        "用句读本身制造急促感\n"
        "  不写招式名称，只写对手看到的后果——兵器脱手、退了三步、"
        "袖口裂开，力量由后果反推\n"
        "  交锋中途插入一句旁观者的心理活动，把读者的注意力从动作拉到场面上，"
        "起到换气的作用\n"
        "  开打前用大段沉默和打量蓄势，真正交手只占整段的五分之一\n\n"
        "**错误的规则**（含具体内容，严禁）：\n"
        "  沈砚一刀劈在对方肩上，对方闷哼后退\n"
        "  两人交手十七招，陆长风使出一记回风舞柳\n"
        "  这场战斗最终以北漠刀客败退告终\n\n"
        "## 输出格式（JSON）\n\n"
        '{\n'
        '  "战斗描写": [\n'
        '    "规则描述（证据：原文引用 ≤20 字）",\n'
        '    ...\n'
        '  ]\n'
        '}\n'
        "每题给 1 条，总数 5-8 条。证据只用来证明这条规则存在，"
        "它本身不是规则内容。"
    )
    user = (
        f"以下是《{chunk_label}》的片段，请分析作者写战斗场面的手法：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。**只写写法，不要复述这段文字的战斗内容；"
        "不要出现具体人名、招式名、地名。**"
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
        "2. 规则后面括号里附**原文证据**——引用一句原文（≤20 字）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. 关注「作者怎么安排结构」而不是「写了什么剧情」\n"
        "5. 若这一块看不出一场完整冲突，就不要硬写「剧情编排模式」\n\n"
        "**正确示例**：\n"
        "  段落编排: [\"冲突段用短句短段压迫节奏，铺垫段用长段铺陈（证据：他一刀劈下。）\"]\n"
        "  伏笔与悬念设计: [\"用一句没说完的话收章留钩子（证据：可就在此时，门外——）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  剧情编排模式: [\"陆长风去了北荒。\"]\n\n"
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
        "2. 规则后面括号里附**原文证据**——引用一句原文（≤20 字）\n"
        "3. 只提炼原文中**确实反复出现**的模式\n"
        "4. 关注「作者怎么营造」而不是「营造出了什么」\n\n"
        "**正确示例**：\n"
        "  情绪表达: [\"愤怒不写『他很生气』，写手指关节发白（证据：指节捏得发白）\"]\n"
        "  世界观呈现: [\"设定借人物争吵带出，不用旁白解释（证据：你真当玄真山是善地？）\"]\n\n"
        "**错误示例（不要这样输出）**：\n"
        "  场景构建: [\"北荒王府很大。\"]\n\n"
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

    if not evidence or len(evidence) < 4:
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

    《北荒刀歌》 v2.268 实测：1223 条规则里，**0 条**被这个函数命中。

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


def _scene_scores(text: str) -> dict[str, float]:
    """各场景类型的密度得分（命中/千字）。纯函数，便于测试与采样复用。"""
    if not text:
        return {st: 0.0 for st in SCENE_KEYWORDS}
    per_k = 1000 / max(len(text), 1)
    return {
        st: sum(text.count(kw) for kw in kws) * per_k
        for st, kws in SCENE_KEYWORDS.items()
    }


def _scene_type(text: str) -> str:
    """识别章节的主要场景类型（相对判定，不依赖绝对阈值）。

    ## 为什么不用绝对阈值

    试过两版绝对阈值，都失败：

    - `count > 0`：任何章节只要出现一个「说道」就算「对话」，78.9% 挤在
      同一类
    - `density >= 1.0/千字`：词表改成专属空间词后各类密度都在 0.14-0.45，
      69% 落入「其他」

    **根本问题**：不同书的用词密度天差地别（网文重对话、纯文学重描写），
    一个固定阈值不可能对所有语料都合适。

    ## 相对判定

    拿这章自身的六个得分，与「本书所有章节的同一类型得分」比较：
    若某类得分排在全书的**前 30%**，就认为这章属于该类；都不够则「其他」。

    这样阈值是**语料自适应的**——本书整体对话多，对话类的判定线就自动抬高；
    某本书特有大量「山洞」描写，只要它在书内相对突出就能被识别。

    ## 为什么要 corpus_stats

    单独一行文本无法判断「密度 0.4 算不算高」。必须知道本书的分布。
    调用方（采样器）先算全书分布，再逐章判定；不传则退化为「取最高分」。
    """
    scores = _scene_scores(text)
    if not scores or max(scores.values()) <= 0:
        return "其他"
    # 无参照分布时：取最高分（旧行为，但仍然不判「其他」——
    # 采样阶段宁可多分一类也不要全落「其他」）
    return max(scores, key=lambda k: scores[k])


def scene_corpus_stats(chapters: list[tuple[int, str]]) -> dict[str, float]:
    """算全书各场景类型的判定阈值。

    ## v2.289：P70 只在「非零值」里取

    旧实现在**全部章节**的得分上取 P70。这在真实语料上恒为 0：

        一章主要属于「对峙/冲突」时，它的六类得分是
        {对话: 0, 移动: 0, 居所: 0, 对峙: 88.2, 集会: 0, 独处: 0}

        于是「对话」类的分布是 120 个值里 100 个 0、20 个正值，
        P70 正好落在 0 上。

    阈值全 0 → `classify_scene` 里 `if thr <= 0: continue` 把所有类跳过
    → **100% 的章节判成「其他」**。实测北荒刀歌 990 章全落「其他」。

    ## 正确做法

    1. 只在 `> 0` 的样本里取分位（零值代表「这章不属于该类」，不该进分布）
    2. 分位数用 P50 而非 P70：去掉零值后，样本是「属于该类的那批章节」，
       取中位即「典型水平」，超过它才算「这类特征明显」
    3. 另外返回「该类覆盖的章节比例」，供 `classify_scene` 在没人过线时
       兜底选最高分——保证不会全落「其他」
    """
    if not chapters:
        return {}
    per_type: dict[str, list[float]] = {st: [] for st in SCENE_KEYWORDS}
    for _ch, text in chapters:
        for st, v in _scene_scores(text).items():
            per_type[st].append(v)

    out: dict[str, float] = {}
    for st, vals in per_type.items():
        nonzero = sorted(v for v in vals if v > 0)
        if not nonzero:
            out[st] = 0.0
            continue
        # 非零样本的中位数——「典型属于该类的一章」的密度水平
        out[st] = nonzero[len(nonzero) // 2]
    return out


def dominant_scene(text: str) -> str | None:
    """判断「这段主要在写哪一类场景」，用于下一节的「必须换场景」约束。

    ## 为什么用场景类型，而不是抽关键词

    先试过两条路，都失败，记录在这里避免重复踩：

    **路线 1：抽高频名词**（字符 2-3 gram 频次 + 虚词过滤）
        实测输出 `['一声', '出一', '人抬', '手从']` —— 全是跨词碎片。
        字符 n-gram 没有分词，天然做不出词。第 3 章「船」出现 36 次，
        但分散在 船底/船舷/船板/船头/船篷 里，每个都不到阈值，
        反而「沈砚」以固定 2-gram 出现 25 次稳居第一，抽出的是人名。

    **路线 2：按首字聚合**（同名 diversity 低、场景物件 diversity 高）
        更糟：`['一声', '出半', '手从', '头发']` 排在最前，
        因为交叉词的 diversity 天然最高。

    **路线 3（采用）：直接用已修好的场景分类器。**
        `_scene_type` 是密度判据（命中/千字）+ 六类专属空间物件名词，
        实测第 3/4/5 章全部判成「对峙/冲突」——**这正是粘滞的信号本身**。
        给模型一句「上节是对峙/冲突，本节必须换一类」比给一串猜出来的
        名词更可用，也完全不依赖分词。

    ## 返回

    六类之一，或 None（无信号/判为「其他」时不加约束，避免瞎指挥）。
    """
    if not text or not text.strip():
        return None
    st = _scene_type(text)
    return None if st == "其他" else st


def classify_scene(text: str, thresholds: dict[str, float] | None = None) -> str:
    """按语料自适应阈值判定章节场景类型。

    `thresholds` 来自 `scene_corpus_stats()`。不传则用 `_scene_type`
    （无参照、取最高分）。
    """
    if not thresholds:
        return _scene_type(text)
    scores = _scene_scores(text)
    if max(scores.values(), default=0) <= 0:
        return "其他"

    # 第一轮：达到阈值（ratio >= 1）的类里取比率最高的
    best: str = "其他"
    best_ratio = 0.0
    for st, score in scores.items():
        thr = thresholds.get(st, 0.0)
        if thr <= 0:
            continue
        ratio = score / thr
        if ratio >= 1.0 and ratio > best_ratio:
            best_ratio = ratio
            best = st
    if best != "其他":
        return best

    # 第二轮（兜底）：没有任何类达到阈值时，取**相对自身阈值比率最高**的类。
    #
    # 为什么需要兜底：某些语料（如纯动作小说）六类密度都很低，
    # 全部低于阈值。此时判「其他」等于放弃分类，场景均衡退化成
    # 按对白排序——正是旧版「其他」占 69% 的那个失效。
    # 取相对最高至少能给出「相对最像哪类」，均衡采样仍然有区分度。
    for st, score in scores.items():
        thr = thresholds.get(st, 0.0)
        if thr <= 0:
            continue
        ratio = score / thr
        if ratio > best_ratio:
            best_ratio = ratio
            best = st
    # 比率过于接近 0 说明这类词一个都没出现，仍判「其他」
    return best if best_ratio > 0.05 else "其他"


def _sample_chapters_v6(
    chapters: list[tuple[int, str]], ratio: float = SAMPLE_RATIO
) -> list[tuple[int, str]]:
    """按百分比采样 + 对话密集优先 + 场景均衡。

    策略：
    1. 按 ratio 计算采样量（603 章 × 30% = 180 章）
    2. 按场景类型分组，每种场景至少采样几章（确保覆盖面）
    3. 剩余配额按对话密度排序填充（对白多的章节优先）
    """
    if not chapters:
        return []
    total_chars = sum(len(t) for _, t in chapters if t)

    # 短篇保护：若全语料字数就不足下限，直接全取（没有砍的空间）。
    if total_chars <= MIN_SAMPLE_CHARS:
        return list(chapters)

    # 目标章节数：按比例取，但保证采样字数不低于下限。
    # 平均章长估算需要多少章才够 MIN_SAMPLE_CHARS。
    avg_ch = total_chars / max(len(chapters), 1)
    ratio_target = int(len(chapters) * ratio)
    chars_target = int(MIN_SAMPLE_CHARS / max(avg_ch, 1))
    target = max(10, ratio_target, chars_target)
    if len(chapters) <= target:
        return list(chapters)

    def dialogue_ratio(text: str) -> float:
        if not text:
            return 0.0
        quote_chars = 0
        for q in ['"', '"', '"', "'", "'", "'"]:
            quote_chars += text.count(q)
        return quote_chars / max(len(text), 1)

    # 按场景类型分组
    scene_groups: dict[str, list[tuple[float, int, str]]] = {}
    # 先算全书分布，再逐章判定——阈值必须来自本书自身。
    # 固定阈值实测两版都失败：给「对话」类挤掉其它类（78.9%），
    # 或让 69% 落入「其他」。语料自适应是唯一能跨题材工作的做法。
    _thresholds = scene_corpus_stats(chapters)
    for ch_no, text in chapters:
        if not text:
            continue
        st = classify_scene(text, _thresholds)
        scene_groups.setdefault(st, []).append((dialogue_ratio(text), ch_no, text))

    # 每种场景至少取 3 章（对话密度最高的）
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
            picked.append((ch_no, text))
            picked_nos.add(ch_no)

    # 按章节号排序返回
    return sorted(picked, key=lambda x: x[0])


def _sample_chapters_fixed(
    chapters: list[tuple[int, str]], n_chapters: int
) -> list[tuple[int, str]]:
    """固定章数采样：均匀取 n_chapters 章，优先取后2/3（避开早期稚嫩期）。

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
    if tier.sampling_mode == "percentage":
        return _sample_chapters_v6(chapters, ratio=tier.sample_ratio or SAMPLE_RATIO)
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
    # 2 字门派名（词形规则对 2 字名豁免，避免误伤「秦嵩」这类人名，
    # 所以这几个常见的 2 字门派名单独列出。它们是通用词，不属于任何一本书）
    "明教", "魔教", "正派", "邪派", "魔门", "佛门",
    # 家族/门派/地名这类**集合性指称**本身不是角色。
    # 注意：具体书名（萧家/萧族/苍梧宗/乌坦）已移除——它们是某本书的专名，
    # 对别的书无效，而且每换一本书就得手加新词，黑名单会无限膨胀。
    # 改由 `_is_collective_entity()` 按**词形规则**判定（见下），
    # 任何书的「X家/X族/X宗/X门/X帮/X派」都自动命中。
    "家族",
    # 言说/情态副词（焚天录 30 章实测高频误判）
    "闻言", "闻言顿", "高级", "满是", "容易", "平淡", "强化", "成年",
    "苍老", "安静", "平静", "双眼", "周围", "后面", "人流", "强行", "平日",
    # 抽象名词 / 连词
    "关系", "安全", "能够", "毕竟",
    # 角色称谓/泛称：图谱按高频提取时会把它们当成角色节点；
    # 它们跨题材通用，不应占用 L2 角色档案名额。
    "江湖", "公子", "师父", "师傅", "王爷", "世子", "殿下", "皇帝",
    "陛下", "娘娘", "夫人", "小姐", "姑娘", "老爷", "先生",
    "前辈", "晚辈", "弟子", "徒弟", "主人", "少爷", "大人", "将军",
    "高手", "家伙", "许多", "春秋", "广陵",
})


#: 集合性实体的词尾后缀——「X宗/X家/X派」这类是门派/家族/势力名，
#: 不是角色名。**用词形规则而不是词表**，任何书都通用。
#:
#: ## 只收「强指示」后缀
#:
#: 首版把「山/城/国/殿/堂」也收进来了，实测误伤：
#:     秦嵩 → 以「山」结尾，被判成门派名（他是真角色）
#:     江慕山 / 王城 / 陈国 → 同样误伤
#:
#: 「山/城/国」在中文人名里极常见（山水、城池、国字都是取名用字），
#: 不能当后缀。「宗/族/派/帮/门/家/教/阁/堂/殿」才几乎是专属。
_COLLECTIVE_SUFFIXES = (
    # 门派 / 势力（强指示，人名几乎不用这些字结尾）
    "宗", "派", "帮", "教", "阁", "门",
    # 家族（「家」「族」在人名里基本不出现）
    "家族", "世家", "家", "族",
    # 场所类。「楼/庄/寺/观」在人名里极少见（不像「山/城/国」那样是取名常用字），
    # 所以收进来；「山/城/国/海/顶/岛」**不收**——它们在人名里太常见
    # （秦嵩 / 江慕山 / 王城 / 陈国 / 星宿海）。
    "府", "殿", "堂", "楼", "庄", "寺", "观", "庵", "阁",
)

#: 这些后缀**必须**配合长度 >= 3 才判定，因为 2 字时更像人名。
_COLLECTIVE_SUFFIX_MIN_LEN = 3


def _is_collective_entity(name: str) -> bool:
    """按词形判断「这是门派/家族/势力名，不是角色」。

    ## 为什么用规则不用词表

    旧版在黑名单里硬编码了 `萧家`/`萧族`/`苍梧宗`/`乌坦`（《焚天录》）。
    那只对一本书有效，换书就要手加，黑名单无限膨胀且永远追不上。

    ## 判据

    1. 2 字名一律不判（「小山」「阿城」「陈国」都是常见人名）
    2. 3 字以上，以强指示后缀结尾 → 实体
    3. 纯数字/含数字的名（「三十八」）→ 不是角色

    实测覆盖：苍梧宗 / 少林派 / 北荒王府 / 天香楼 / 青云门 / 慕容世家 /
    峨眉派 / 明教 / 华山派 / 铁掌帮 / 聚贤庄 / 大雄寺
    实测不误伤：陆长风 / 秦嵩 / 小宁 / 老周 / 石昀 / 齐休 / 江慕山
    """
    if not name:
        return False
    # 2 字名一律当人名（中文里 2 字的门派名极少，「明教」这类靠 _FAKE_CHARS 补）
    if len(name) <= 2:
        return False
    return any(name.endswith(suf) for suf in _COLLECTIVE_SUFFIXES)


def _is_fake_character(name: str) -> bool:
    """占位符 / 高频虚词 / 集合性实体 / 非人名判定。

    三层判据，从便宜到贵：

    1. 长度 / 占位符正则——「xxx」「【1】」直接拒
    2. 高频虚词黑名单——「相信」「冷笑」「双手」这类
    3. **词形规则**——「X宗/X家/X派」是门派家族不是角色

    第三层是 v2.287 新增。旧版把「萧家/萧族/苍梧宗/乌坦」硬编码进黑名单，
    那只对《焚天录》有效。词形规则对任何书通用，不需要为新书补词。
    """
    name = (name or "").strip()
    if not name or len(name) < 2:
        return True
    if _PLACEHOLDER_RE.match(name):
        return True
    if name in _FAKE_CHARS:
        return True
    return _is_collective_entity(name)


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
#: （《北荒刀歌》：陆长风、陆龙象；实践里「陆长」+「风」占 100%）——
#: 排除它会让最该扩展的名字永远扩不出来。「日月」同理（裴白的
#: 对手「江听雪」不受影响，但「日月」在武侠/仙侠里是常见名字用字），
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
    「陆长风」被抽成「陆长」，「楚惊蛰」被抽成「楚惊」，而完整名
    **从来不存在于图谱里**——`_resolve_name_artifacts` 的包含关系
    合并因此匹配不上（它要求两个名字都在图谱里）。

    ## 为什么改用「图谱节点作锚点 + 语料里向后扩展」

    试过直接从语料挖高频 3-4 字串：top 40 全是「陆长风笑」「世子殿下」
    「一秒记住」（盗版站广告），噪声压过信号，不可用。

    而**以图谱节点为前缀**在语料里看后继，信号极干净（《北荒刀歌》 512 万字实测）：
    - 陆长 → 陆长风 100%（19254/19256）
    - 楚惊 → 楚惊蛰 100%（1258/1258）
    - 陆北 → 陆北枝 100%（930/930）
    - 孟 → 孟豹 99% / 江听 → 江听雪 100% / 裴 → 裴白 100%
    - 顾长 → 顾长风 99% / 陆偃 → 陆偃兵 100%

    同时它天然不误伤完整名：「安小落」的后继多是标点/功能字，扩展率上不去。

    ## 参数取舍

    `frac=0.5`：扩展出的名字必须占该节点语料出现次数的一半以上。
    定 0.5 而不是更松，是因为要为「名字恰好是另一个名字前缀」的情况留余量——
    「赵」→「赵青锋」实测 50%（另 25% 是「赵敬城」），
    正好卡在边界上；取 0.5 会选中更高频的那个，取 0.3 则会一次扩出两个不同的人。
    `max_extra=2`：中文姓名通常 2-4 字，节点已占 2 字，最多再补 2 字。

    ## 已知局限

    - 同一前缀指向多个真角色时（赵 → 青锋 / 敬城）只取最高频的那个，
      另一个仍是碎片。要彻底解决需要「前缀 + 后缀双向收敛」，成本高。
    - 地名/帮派也会被扩展（玄真 → 玄真山 87%、青鲤 → 青鲤帮 90%）。
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

    v2.239。`_expand_names_from_corpus` 只能向后补（陆长 → 陆长风），
    救不了**缺首字**的残片。《北荒刀歌》真链路（v2.238）实测，LLM 直接
    输出了「长风」当角色名——主角陆长风以碎片形态混进了角色声音词典。

    ## 判据为什么是「前驱集中度」而不是别的

    probe 实测（《北荒刀歌》 512 万字全本）：

    尾部残片的前驱高度集中：
        长风 → 徐 99.5%（19353 次中 19254 次前面是「徐」）
        仙芝 → 王 100.0%   北枳 → 徐 99.6%   芝豹 → 陈 99.4%
        太阿 → 邓  93.5%   淳罡 → 李 100.0%  剑棠 → 顾 99.8%
        禄山 → 褚 99.4%    长卿 → 曹 99.5%

    完整名的前驱是全分散的（最高才 46.7%，且多是空格/标点）：
        陆长风 46.7%（空格）  楚惊蛰 16.2%  裴白 12.2%
        孟豹 14.3%          陆崇 15.3%    小宁 41.1%

    虚词也不会误扩：江湖 16.3% / 公子 11.7% / 师父 16.5%。

    50% 阈值把两组分得很开（残片最低 93.5%，完整名最高 46.7%）。

    ## 与向前扩展的配合

    两个方向互为补充，调用方先试向前（补尾字），失败再试向后（补首字）。
    像「孟 → 孟豹」这种中段残片两个方向都能补，向前优先（更常见）。

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
    （「长风」→「陆长风」）永远救不回来。本函数是统一入口：

    1. 先跑 `_expand_names_from_corpus`（补尾字，「陆长」→「陆长风」）
    2. 对没补上的，跑 `_expand_names_backward`（补首字，「长风」→「陆长风」）

    两步都失败的名字不出现在结果里（保持原样）。

    ## 为什么默认 frac 是 0.85 而不是 0.5

    `_extend_name_candidates` 的老契约是 `frac=0.15` 表示「**允许**的独立
    出现率」，等价于要求扩展率 >= 85%。这个阈值不是随便定的：

    测试语料里「莫雨道」出现 30 次、「莫雨蝶」20 次，`莫雨` 共 50 次。
    若阈值取 0.5，「莫雨道莫」正好 60% 过线 → 把一个真简称错误扩展成
    「莫雨道莫」。0.85 则要求扩展词压倒性主导，才认它是残片。

    《北荒刀歌》实测残片扩展率 93.5%~100%，0.85 仍然够用。

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
    # 焚天录 30 章实测：77 个 character 节点里只有 4 个 speaking=True，
    # 其中 2 个是伪角色（「满脸冷笑道」「冷笑一声」），而真角色
    # 萧薰儿/萧媚/萧宁/纳兰/萧家 全是 False —— 因为网文写法是
    # 「萧战笑道」之外还有「萧战叹了一口气，沉默了片刻，忽然道：」，
    # 动词与名字隔着状语，正则不命中。
    #
    # 叠加 `mentions >= 200` 兜底后，候选池只剩 5 个（焚天录前 30 章
    # 只有石昀 1265 次过线），二番位萧战 159 / 纳兰 139 / 萧宁 68 全落空
    # → 角色声音维度只产出 2 条。
    #
    # ## 为什么放弃「言说邻接率」判据
    #
    # 试过窗口式邻接率（前后 N 字内出现言说动词/引号），probe 实测
    # 在任何窗口宽度下都不可分：伪角色「冷笑」0.929、「萧族」0.400~0.900、
    # 「闻言」0.529，反而高于真角色「萧宁」0.077~0.128。
    # 原因是这些词本身就常出现在言说动词旁边，而真角色的高频出现是
    # 「石昀的」「石昀心头」这类所有格/主语位。
    #
    # ## 现在的判据
    #
    # 纯 mentions 排序 + `_FAKE_CHARS` 黑名单 + 子串归一（_resolve_name_artifacts）。
    # probe 实测焚天录 30 章：取前 30 名覆盖 7/7 真角色（石昀/萧战/纳兰/
    # 萧宁/萧媚/葛叶/萧薰儿），伪角色（家族/萧家/高级/闻言/乌坦/双眼…）
    # 由黑名单与后续 LLM 抽取时的空结果自然淘汰。
    #
    # 代价：候选池会含少量非人名词（谷尼/苍梧宗 是地名）。这是可接受的——
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
    # 语料自证补全：向前补尾字（陆长 → 陆长风）优先，
    # 失败的再向后补首字（长风 → 陆长风）。v2.239 加后向——
    # v2.238 真链路实测 LLM 直接输出了「长风」，主角以碎片形态混进角色声音。
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

    # A stored alias is not proof when the same token names several people.
    if canonical:
        expansions = {cn for cn, _ in canonical if cn != name and name in cn}
        if len(expansions) > 1:
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
            # Shared surnames/abbreviations cannot choose a person by length.
            unique = set(candidates)
            return next(iter(unique)) if len(unique) == 1 else None
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
    —— 《北荒刀歌》 990 章实测，29 条角色声音里 12 条是这类。
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

    《北荒刀歌》 v2.261 真链路实测，这几条的证据**都包含名字本身**：

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
#: 《北荒刀歌》实测（v2.261 真链路）：
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
    # v2.267：《北荒刀歌》真链路实测漏掉的姓氏——晋兰亭 / 种神通 / 喜意
    # 这些名字被送进 LLM 审计后误标成「普通名词」。
    "晋种喜赫轩澹令皇甫尉迟长孙宇文司徒端木独孤"
    # 其他常见但容易漏的复姓首字（配合 _COMPOUND_SURNAMES 使用）
    "万俟闻人呼延拓跋完颜耶律纳兰申屠太史仲孙钟离闾丘"
)

#: 复姓（网文里极常见，姓氏白名单只收单字会漏掉）。
#:
#: 《北荒刀歌》实测：赵 / 赫连 / 澹台 / 公孙 / 令狐 都是真角色，
#: 但因为首字不在单字姓氏表里，被廉价预过滤当成可疑送审，
#: 而 glm-4-flash 又把它们标成「普通名词」。
_COMPOUND_SURNAMES = frozenset({
    "赵", "赫连", "澹台", "公孙", "令狐", "慕容", "欧阳", "上官", "司马",
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
#: 来源：《北荒刀歌》 990 章实测，29 条角色声音里混进的 12 条垃圾 + 同类扩充。
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
    "江湖", "广陵", "春秋", "武当", "西楚", "北荒", "北莽", "离阳", "太安",
    "龙虎", "武帝", "徽山", "上阴", "昆仑", "西域", "中原", "南唐",
    # 情绪 / 副词（常被误认为人名）
    "可惜", "可怜", "无奈", "竟然", "居然", "果然", "忽然", "自然", "当然",
    "许多", "不少", "很多", "若干", "一些", "多少", "如此", "这般", "那样",
    "白衣", "青衣", "黑袍", "紫衣", "黄袍",
    # v2.264：《北荒刀歌》真链路实测漏网的高频虚词（图谱里排前 150 位，
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
#:   小宁： 自称「未提及」 口头禅「未提及」 句长混合 语气未提及
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
        # 《北荒刀歌》实测，小宁/许多/高手/裴长卿 四条都是这种空壳：
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
    # LLM 出长 JSON 时会发生复制粘贴错误：《北荒刀歌》实测，
    # 裴白 / 武当 / 西楚 / 可惜 四条规则的正文**完全相同**，
    # 内容却是秦嵩的台词（「我秦嵩有洁癖…」）。这种条目留着会
    # 让生成时把秦嵩的说话习惯安到裴白头上。
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
#: 早期版本问「像不像本书角色」，glm-4-flash 把陆崇（主角之父）、小宁
#: （女主）都标成可疑——理由是「普通名词，可能指人名的一部分」，自相矛盾。
#: 换成「能不能指人」+ 明确「不认识的但构词像人名的不要列」后，
#: 57 个名字里只标出 5 个（老人/红薯/洛阳/后来/南北），真角色零误伤。
_NAME_AUDIT_SYSTEM = (
    "你是中文语言助手。给你一批中文词语，判断哪些**不能用来指人**。\n\n"
    "不能指人的包括：\n"
    "- 时间副词：后来、此时、当时、片刻\n"
    "- 方位词：南北、东西、左右\n"
    "- 普通名词：老人、公子、家伙\n"
    "- 地名：洛阳、北荒、武当\n\n"
    "能指人的包括：\n"
    "- 任何「姓+名」结构（陆崇、小宁、宋岩）——即使你不认识这些名字\n"
    "- 任何绰号、代号、称谓代称（老和尚、红薯、老宋、褚胖子）\n"
    "- 任何像人名的双字/三字词\n\n"
    "**重要**：不认识的词，只要构词像人名（姓+名 / 名+字），就**不要**列出来。\n"
    "只列你有把握不是人名的。\n\n"
    '输出 JSON：{"not_person": ["词1", "词2"]}'
)


def _audit_character_names(
    provider, voices: list[dict], *, max_names: int = 80,
) -> dict:
    """用 LLM 粗筛角色名，**只标记不删除**（v2.265）。

    ## 为什么只标记不删

    这个审计是**启发式的**，会误判：《北荒刀歌》实测，「红薯」（书里是丫鬟
    的名字）被标为「普通名词」。如果直接据以删除，就会丢掉真角色。

    标进 `voice_name_audit` 字段供人工/前端复核，是正确的用途：
    57 个名字里只标出 5 个，人工看一眼就能定夺，比逐条审 57 个省事得多。

    ## 为什么问「能不能指人」而不是「是不是本书角色」

    后者需要知道这本书。glm-4-flash 记得主角陆长风，但对陆崇（他爹）、
    小宁（女主）记忆模糊 → 判成可疑。**把「不知道」误判成「不存在」**
    是小模型做常识判断的通病。

    「能不能用来指人」是纯语言常识，不需要书的知识——换这个问法后
    真角色零误伤。

    Args:
        provider: LLM provider。
        voices: 角色声音规则列表（每项含 "rule" 字段）。
        max_names: 一次最多审多少个名字（控制 prompt 长度）。

    Returns:
        ``{"suspicious": [...], "checked": N, "skipped": bool}``。
        **调用失败时返回空结果，不阻断蒸馏**。
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
        return {"suspicious": [], "checked": len(names), "skipped": True}

    # v2.265：先做**廉价预过滤**，只把「可疑候选」送给 LLM。
    #
    # 为什么：glm-4-flash 对「陆崇」「小宁」这类名字判断不稳定——同一批
    # 输入两次，一次全保留、一次标成「普通名词」。把明显是人名的送进去，
    # 只会制造噪声，浪费人工复核精力。
    #
    # 明显是人名的特征（满足任一即跳过 LLM）：
    # 1. 首字在百家姓白名单里（陆崇、小宁、宋岩、曹嵬、冯岭）——中文人名
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
        return {"suspicious": [], "checked": 0, "skipped": False,
                "note": "全部名字都是姓+名或具体化称谓，无需 LLM 审计"}

    batch = audit_candidates[:max_names]
    result = {"suspicious": [], "checked": len(batch),
              "skipped": len(names) > max_names}

    try:
        raw = provider.chat(_NAME_AUDIT_SYSTEM, "词语：\n" + "、".join(batch))
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "角色名审计调用失败（不阻断）: %s", exc
        )
        return result

    import re as _re
    import json as _json
    if not isinstance(raw, str):
        return result
    m = _re.search(r"\{.*\}", raw, _re.S)
    if not m:
        return result
    try:
        parsed = _json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return result

    bad = parsed.get("not_person") if isinstance(parsed, dict) else None
    if not isinstance(bad, list):
        return result

    # 只接受在本次清单里的词（防 LLM 编造）
    valid = [b for b in bad if isinstance(b, str) and b in seen]
    result["suspicious"] = valid
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
        "## 关键约束：只写结构，不写这场冲突的具体内容\n\n"
        "你的输出会被用来指导**另一本小说**，那本书的人物和事件完全不同。所以：\n"
        "- 不要写具体人名、地名、门派名、物件名\n"
        "- 不要写这场冲突发生了什么（谁质疑谁、谁掏出了什么、谁说了哪句话）\n"
        "- 每步的写法必须**可迁移**：换一组人物、换一个场景依然成立\n\n"
        "**正确的写法**：\n"
        "  铺垫 —— 用日常细节的轻微异常暗示不祥，不点破\n"
        "  升温 —— 对话逐渐变短、答非所问增多，环境音被刻意放大\n"
        "  爆发 —— 由一个物理动作（器物落地/门被推开）打断对峙\n"
        "**错误的写法**（含具体剧情，严禁）：\n"
        "  铺垫 —— 用陆长风的内心独白和日常投喂细节暗示湖底老魁并非普通囚徒\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "节拍序列": [\n'
        '    {"步骤": "铺垫", "写法": "抽象描述，不含人名和具体事件"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼一场典型冲突的节拍序列：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。**只写可迁移的结构，不要复述这场冲突的具体内容。**"
    )
    return system, user


def _build_scene_template_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造高频场景模板提取的 prompt。

    识别反复出现的场景类型，提炼环境细节、人物站位、典型动作。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**场景结构骨架**。\n\n"
        "## 关键约束：只写结构，不写内容\n\n"
        "你的输出会被用来指导**另一本小说**的写作，那本书的人物、地点、事件都不同。"
        "所以任何属于这本参考书的具体信息都是**污染**：\n"
        "- 不要写具体人名、地名、门派名、物件名\n"
        "- 不要写具体发生了什么（谁来找谁、说了什么话、因为什么争执）\n"
        "- 不要写环境细节（光线/气味/温度）—— 那是这本参考书的场景，不是模板\n\n"
        "只提炼「这类场景在结构上怎么走」：\n"
        "1. 场景类型（用**通用类别**：会面/对峙/独处/告别/交易/传讯/追捕）\n"
        "2. 参与结构（几方在场、谁是主谁是客、谁在暗处）\n"
        "3. 动作链（3-5 拍，每拍只写**动作类型**，如「来客抵达→主人接待→"
        "试探交谈→谈崩或谈成→一方离去」）\n"
        "4. 情绪走向（从什么基调到什么基调）\n\n"
        "**正确的动作链**：来客抵达→主人接待→试探交谈→达成或谈崩→一方离去\n"
        "**错误的动作链**（含具体剧情，严禁）：贵客抵达→王府开中门并铺陈仪仗→"
        "主人亲自迎接→以糖葫芦诱引小王爷→长辈无奈作罢\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "场景模板": [\n'
        '    {"场景": "会面", "参与结构": "...", "动作链": "...", "情绪走向": "..."}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼场景结构骨架：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。**只写结构，不要写这本参考书的具体人名地名事件。**"
    )
    return system, user


def _build_subtext_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造潜台词模式提取的 prompt。

    提炼「角色如何用反话/转移话题/沉默表达真实意图」。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼**对白潜台词手法**。\n\n"
        "## 关键约束：只写手法，不写内容\n\n"
        "你的输出会被用来指导**另一本小说**，那本书的人物和情节完全不同。"
        "所以：\n"
        "- 不要复述这段对话说了什么、谁对谁说了什么\n"
        "- 不要写具体人名\n"
        "- 写法描述必须是**可迁移的**：换成任何两个角色都成立\n\n"
        "分析角色如何用间接方式表达真实意图，只给手法名称 + 抽象写法：\n"
        "1. 反话正说（说「没事」其实有事）\n"
        "2. 转移话题（用问另一个问题回避）\n"
        "3. 沉默施压（不说话比说话更有力）\n"
        "4. 借题发挥（借他人之事说自己）\n"
        "5. 命令式关心（用命令语气表达担心）\n"
        "6. 答非所问（回应情绪而不是回应问题）\n\n"
        "**正确的写法**：「反话正说」—— 用字面相反的评价包装真实情绪，"
        "褒词贬用或贬词褒用，让听者察觉言外之意\n"
        "**错误的写法**（含具体剧情，严禁）：「反话正说」—— 秦嵩明明增肥，"
        "却说「瘦了，都瘦了一圈了」，借夸张的反话掩饰讨好意味\n\n"
        "输出格式（JSON）：\n"
        '{\n'
        '  "潜台词模式": [\n'
        '    {"模式": "反话正说", "写法": "抽象描述，不含人名和具体对话"}\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"以下是《{chunk_label}》的片段，请提炼对白潜台词手法：\n\n"
        f"{chunk_text}\n\n"
        "请按上述格式输出 JSON。**只写可迁移的手法，不要复述这段对话的内容。**"
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
            raws = _gather_chat(provider, all_prompts)
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


def _build_drift_prompt(chunks_info: list[tuple[str, str]], chunk_label: str) -> tuple[str, str]:
    """构造文风漂移对比的 prompt。

    传入多个时间段的文本片段，让 LLM 对比文风变化。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是对比**同一作者在不同时期的文风差异**。\n\n"
        "对比维度：\n"
        "1. 句长节奏变化（早期 vs 后期）\n"
        "2. 对白密度变化\n"
        "3. 描写细腻度变化\n"
        "4. 叙事结构复杂度变化\n"
        "5. 用词偏好变化\n\n"
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
        parts.append(f"【{period_name}】\n{text[:2000]}")
    user = (
        f"以下是《{chunk_label}》在不同时期的片段：\n\n"
        + "\n\n---\n\n".join(parts)
        + "\n\n请按上述维度对比输出 JSON。"
    )
    return system, user


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


# v2.226：并发蒸馏辅助函数
# 同步入口，内部 asyncio.run 跑 N 个 chat 真并发（限流由 semaphore 控制）
#
# v2.280：这里的 5 是**兜底值**，不再是实际用值。并发按渠道定：
# 各 provider 类的 `max_concurrent_hint` 是实测出来的安全上限（见 provider.py）。
# 之前全局写死 5 的后果：智谱真实上限是 2，用 5 跑等于全程 1/3 成功率 +
# 大量 429 重试；而 MiniMax 实测能到 15，用 5 又白白慢了 3.6 倍。
_DEFAULT_MAX_CONCURRENT = 5


def _resolve_concurrency(provider, requested: int | None = None) -> int:
    """决定本次蒸馏的并发数。优先级：显式参数 > 渠道实测值 > 全局兜底。

    渠道值取 `provider.max_concurrent_hint`（0 表示该渠道没测过）。
    另外允许 `settings.distill_max_concurrent` 强行覆盖（0 = 不覆盖），
    给用户留一个「我知道自己在做什么」的口子。
    """
    if requested and requested > 0:
        return requested
    try:
        from app.config import settings as _st
        forced = int(getattr(_st, "distill_max_concurrent", 0) or 0)
        if forced > 0:
            return forced
    except Exception:  # noqa: BLE001 —— 配置不可读不该让蒸馏失败
        pass
    hint = int(getattr(provider, "max_concurrent_hint", 0) or 0)
    return hint if hint > 0 else _DEFAULT_MAX_CONCURRENT


async def _gather_chat_async(
    provider, prompts: list[tuple[str, str, dict | None]],
    max_concurrent: int | None = None,
) -> list[str | Exception]:
    """异步 gather N 个 provider.async_chat 调用（受 semaphore 限流）。

    `max_concurrent=None` 时按渠道自动取值——**不要**在这里写死数字，
    详见 `_resolve_concurrency`。
    """
    max_concurrent = _resolve_concurrency(provider, max_concurrent)
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(system: str, user: str, constraints):
        async with sem:
            try:
                return await provider.async_chat(system, user, constraints=constraints)
            except Exception as exc:  # noqa: BLE001 —— 单条失败不阻断其余
                return exc

    tasks = [_one(s, u, c) for s, u, c in prompts]
    return await asyncio.gather(*tasks, return_exceptions=False)


def _gather_chat(
    provider, prompts: list[tuple[str, str, dict | None]],
    max_concurrent: int | None = None,
) -> list[str | Exception]:
    """同步入口：内部 asyncio.run 跑并发。

    用法：把串行 for 循环里的 provider.chat() 改为收集所有 (system, user, constraints)
    三元组到 prompts 列表，再调一次 _gather_chat 拿回 list[str | Exception]。

    返回顺序与 prompts 顺序一致；失败用 Exception 实例（不抛，调用方决定怎么处理）。
    """
    if not prompts:
        return []
    return asyncio.run(_gather_chat_async(provider, prompts, max_concurrent))


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


def _l2_candidate_reason(name: str, chapters: list[tuple[int, str]], names: list[str]) -> str | None:
    """Require individual context, never infer identity from a surname alone."""
    text = '\n'.join(t for _, t in chapters)
    longer = {n for n in names if n != name and name in n and n in text}
    # Corpus completions also catch canonical names missing from the KG index.
    if name in _COMPOUND_SURNAMES:
        longer.update(re.findall(re.escape(name) + r'[\u4e00-\u9fff]{1,2}(?=说道|说|问|笑|摇头|点头|走|看)', text))
    if len(longer) > 1:
        return 'ambiguous_reference'
    person = re.compile(re.escape(name) + r'(?:[，,]?)(?:说道|说|问|笑|摇头|点头|走|看|拿|站|坐|抬|低|转|望|心想|伸|叹|皱|轻声|沉声|冷声)')
    if not person.search(text):
        return 'no_person_evidence'
    return None


def _extract_character_profiles(
    provider,
    chapters: list[tuple[int, str]],
    char_names: list[str],
    *,
    novel_id: int,
    novel_title: str = "",
    max_characters: int = MAX_VOICE_CHARACTERS,
    audit: list[dict] | None = None,
) -> list[dict]:
    """逐角色抽取并规范化 L2 characters 档案。

    这是 `extract_narrative_profile` 的 L2 接线点，不依赖图谱角色节点：
    `char_names` 已由同一轮角色声音索引清洗/归一化得到，避免 L2 和
    `角色声音词典` 各自维护两套候选名。

    每个角色单独一次调用，原因是把 20-30 个角色塞进一个 prompt 会让
    每个角色只剩一句空话；单角色 prompt 才能完整填行为倾向、底线、七字段关系网。
    单个角色失败只跳过该角色，不阻断整本书的文风蒸馏。
    """
    from app.style.character_profile import (
        build_character_profile_prompt,
        normalize_character_profile,
    )

    out: list[dict] = []
    names = [n.strip() for n in char_names if isinstance(n, str) and n.strip()]
    eligible = []
    for name in names:
        reason = _l2_candidate_reason(name, chapters, names)
        if reason is None:
            eligible.append(name)
        elif audit is not None:
            audit.append({'name': name, 'novel_id': novel_id, 'status': 'rejected', 'reason': reason})
    for name in eligible[:max_characters]:
        selected = _find_character_chapters(chapters, name, max_chapters=6)
        # 避免把整章（长章可达数万字）全部塞进单角色 prompt；取命中角色
        # 附近的局部窗口，最多六段、总计约 6K 字，仍保留章号作来源依据。
        excerpts: list[str] = []
        source_windows: list[dict] = []
        chapter_numbers: list[int] = []
        budget = 6000
        for chapter_no, text in selected:
            pos = text.find(name)
            if pos < 0 or budget <= 0:
                continue
            left = max(0, pos - 450)
            right = min(len(text), pos + 750)
            excerpt = text[left:right]
            if len(excerpt) > budget:
                excerpt = excerpt[:budget]
            if excerpt:
                excerpts.append(f"[第{chapter_no}章]\n{excerpt}")
                source_windows.append({'chapter': int(chapter_no), 'start': left, 'end': left + len(excerpt), 'quote': excerpt})
                chapter_numbers.append(int(chapter_no))
                budget -= len(excerpt)
        if not excerpts:
            continue
        try:
            system, user = build_character_profile_prompt(
                name, excerpts, novel_title=novel_title
            )
            raw = provider.chat(system, user)
            parsed = _parse_extract_result(raw)
            if not isinstance(parsed, dict):
                continue
            # Prompt 顶层对象直接为档案；兼容模型包一层 characters/name 对象。
            if isinstance(parsed.get("characters"), list):
                parsed = next((item for item in parsed["characters"]
                               if isinstance(item, dict) and item.get("name") == name), {})
            elif isinstance(parsed.get(name), dict):
                parsed = parsed[name]
                parsed.setdefault("name", name)
            profile = normalize_character_profile(parsed, name, sources=[
                (ch, excerpt.split('\n', 1)[1]) for ch, excerpt in zip(chapter_numbers, excerpts)
            ])
            profile["novel_id"] = novel_id
            profile["source_windows"] = source_windows
            profile["source_chapters"] = chapter_numbers
            out.append(profile)
            if audit is not None:
                audit.append({'name': name, 'novel_id': novel_id, 'status': 'extracted', **profile['evidence_summary']})
        except Exception as exc:  # noqa: BLE001
            if audit is not None:
                audit.append({'name': name, 'novel_id': novel_id, 'status': 'failed', 'error_type': type(exc).__name__})
            logging.getLogger(__name__).warning(
                "L2 角色档案提取失败 novel_id=%s name=%s: %s",
                novel_id, name, exc,
            )
    return out


def extract_narrative_profile(
    db: Session,
    project,
    novel_ids: list[int],
    provider=None,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
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
    rejected = 0
    #: v2.265 角色名审计结果（只标记不删除，供人工复核）
    voice_audit: dict | None = None
    # L2 角色档案：每本参考书独立抽取，最终按角色名合并。
    character_profiles: list[dict] = []
    l2_audit: list[dict] = []
    total_chapters = 0

    # —— 进度上报的累计量（只在 progress 非 None 时有意义）——
    # blocks_done：已完成块数（跨语料累计）；
    # blocks_seen / books_seen：已探明的块数与语料数，用来估剩余语料的块数。
    total_books = len(novel_ids)
    blocks_done = 0
    blocks_seen = 0
    books_seen = 0
    est_total = 0

    if progress is not None:
        progress("正在准备参考语料", 0, 0)

    # —— 前置自检：参考语料图谱（v2.228）——
    # 角色声音维度依赖 KgNode(type="character", source=f"novel:{nid}")——
    # 这张图由 kg/build.py 的规则抽取器建（零 LLM），入口是
    # POST /api/projects/{id}/build-kg?novel_id=N。用户路径上它由「建图」按钮触发，
    # 但蒸馏是独立入口：只传 reference_novel_ids 直接蒸的话图谱压根不存在，
    # _build_character_name_index 拿到空候选池 → 角色声音维度静默产出 0 条。
    # 这里做前置补齐：缺图才建，已有则跳过（幂等，一次全量规则抽取，实测秒级）。
    _ensure_reference_graphs(db, project, novel_ids, progress=progress)

    for book_idx, nid in enumerate(novel_ids, start=1):
        chapters = _load_novel_chapters(db, nid)
        if not chapters:
            continue

        canonical: list[tuple[str, int]] = []
        alias: dict[str, str] = {}
        # 按档位配置采样：完整档走百分比 + 对话密集优先 + 场景均衡；
        # 平衡/快速档走固定章数均匀采样。
        sampled = sample_chapters_for_tier(chapters, cfg)
        total_chapters += len(sampled)

        # 分块：按**字数**装箱，不按章数。
        #
        # 旧版固定「每块 20 章」，当时 `_load_novel_chapters` 返回的是 chunk
        # （约 1000 字），20 个 = 2 万字/块。改成真章节后 20 章 = 11 万字/块，
        # prompt 会超长模型上下文。
        #
        # 现在按字数装箱：目标每块 TARGET_BLOCK_CHARS 字，最少 MIN_CHAPTERS_PER_CHUNK 章。
        # 这样对不同章长的书都成立（有的书一章 5000 字，有的 1500 字）。
        block_size = max(
            MIN_CHAPTERS_PER_CHUNK,
            _chapters_per_block(sampled, TARGET_BLOCK_CHARS),
        )
        chunk_size = block_size
        chunk_starts = list(range(0, len(sampled), chunk_size))
        n_chunks = len(chunk_starts)

        # 估算总块数：已探明语料的**平均块数** × 剩余未探明语料数。
        # 为什么是估算而不是精确值：精确值要先把每本语料都加载+采样一遍，
        # 那等于把最贵的一步做两次。估算法的性质是——跑到最后一本时剩余项
        # 归零，est_total 自动收敛成精确值，进度条不会停在 97%。
        # 下限兜底 `blocks_done + n_chunks`：后一本语料若比已探明的平均值厚重，
        # 估算值会小于实际会跑的块数，进度百分比就会越过 100%。这里保证
        # 「当前已跑 ≤ 总数」这条不变量成立，前端不必再做防御。
        books_seen += 1
        blocks_seen += n_chunks
        avg_chunks = blocks_seen / books_seen if books_seen else float(n_chunks)
        est_total = max(
            int(round(blocks_done + n_chunks + (total_books - book_idx) * avg_chunks)),
            blocks_done + n_chunks,
        )

        if progress is not None:
            progress(
                f"语料 {book_idx}/{total_books}：已采样 {len(sampled)} 章，"
                f"切 {n_chunks} 块",
                blocks_done,
                est_total,
            )

        # v2.227 (P2)：chunk 间并发 ——
        # 把每 chunk 的所有 prompt 收集到一个 flat 列表，一次 _gather_chat 跑并发
        # （限流 5 req/s 串行排队），然后按 (ci, kind) 切回每 chunk 处理。
        # 之前 chunk 间串行 = 60 chunk × 1 批 = 60 批，1.8x 提速
        # 改后 chunk 间并发 = 60 chunk × 1 批 = 1 批 300 prompt，理论 5x 提速
        chunk_specs: list[dict] = []  # 每 chunk 的 spec: {ci, text, chunk, cr, prompts, kinds}
        for ci, i in enumerate(chunk_starts):
            chunk = sampled[i : i + chunk_size]
            chunk_text = "\n\n".join(text for _, text in chunk if text)
            if not chunk_text:
                continue
            chapter_range = f"{chunk[0][0]}-{chunk[-1][0]}"

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
            # （北荒刀歌 990 章 30% 采样）证伪：
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

            if i == 0:
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

            if "语序编排" in has_dim:
                wo_system, wo_user = _build_word_order_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                chunk_prompts.append((wo_system, wo_user, None))
                chunk_prompt_kinds.append("word_order")

            if "战斗描写" in has_dim:
                cb_system, cb_user = _build_combat_prompt(chunk_text, f"语料{nid} 第{chunk[0][0]}-{chunk[-1][0]}章")
                chunk_prompts.append((cb_system, cb_user, None))
                chunk_prompt_kinds.append("combat")

            # 「人物塑造模板」**不在这里抽** —— 它是独立产物（按档位单独存放），
            # 抽它不必重蒸整本档案。见 app.style.character_template.extract_character_template。

            chunk_specs.append({
                "ci": ci,
                "text": chunk_text,
                "chunk": chunk,
                "cr": chapter_range,
                "prompts": chunk_prompts,
                "kinds": chunk_prompt_kinds,
            })

        # 拍平所有 prompt + meta，跑一次 gather
        flat_prompts: list[tuple[str, str, dict | None]] = []
        flat_meta: list[dict] = []  # 与 flat_prompts 一一对应
        for spec in chunk_specs:
            for kind, p in zip(spec["kinds"], spec["prompts"]):
                flat_prompts.append(p)
                flat_meta.append({"ci": spec["ci"], "kind": kind, "spec": spec})

        if flat_prompts:
            try:
                flat_raws = _gather_chat(provider, flat_prompts)
            except Exception as exc:  # noqa: BLE001
                logging.getLogger(__name__).warning(
                    "叙事提炼全 chunk 失败 novel_id=%s: %s", nid, exc
                )
                flat_raws = [Exception(str(exc))] * len(flat_prompts)
        else:
            flat_raws = []

        # 按 ci 桶回来
        bucketed: dict[int, dict] = {}
        for raw, meta in zip(flat_raws, flat_meta):
            ci = meta["ci"]
            if ci not in bucketed:
                spec = meta["spec"]
                bucketed[ci] = {
                    "raws": [],
                    "kinds": [],
                    "text": spec["text"],
                    "chunk": spec["chunk"],
                    "cr": spec["cr"],
                }
            bucketed[ci]["raws"].append(raw)
            bucketed[ci]["kinds"].append(meta["kind"])

        # 按 ci 顺序处理（保证输出可复现）
        for ci in sorted(bucketed.keys()):
            b = bucketed[ci]

            if progress is not None:
                progress(
                    f"语料 {book_idx}/{total_books}：提炼第 {ci + 1}/{n_chunks} 块",
                    blocks_done + ci + 1,
                    est_total,
                )

            chapter_range = b["cr"]
            chunk_text = b["text"]
            chunk = b["chunk"]

            # v2.230：「文笔量化审计」由确定性指标生成，完全不依赖 LLM。
            # 放在 extract 判空之前——即使这一块的 LLM 调用失败，硬指标仍然产出。
            # 这是该维度从 LLM 输出清单里移除后唯一的数据来源。
            _feats = build_deterministic_features(chunk_text)
            if _feats:
                all_rules["文笔量化审计"].append({
                    "rule": _format_features_rule(_feats),
                    "novel_id": nid,
                    "chapter_no": chunk[0][0],
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

            parsed: dict = {}
            for r in (raw_character, raw_narrative):
                if r is None:
                    continue
                p = _parse_extract_result(r)
                if isinstance(p, dict):
                    parsed.update(p)

            if not parsed:
                continue

            for dim in tier_dims:
                items = parsed.get(dim)
                if not isinstance(items, list):
                    continue
                for rule in items:
                    if not isinstance(rule, str) or not rule.strip():
                        continue
                    # 证据验证：伪规则在这里被拦掉
                    if not _validate_evidence(rule, chunk_text):
                        rejected += 1
                        continue
                    # 逐字复制检测：LLM 直接复制原文句子而非抽象规则，信息量极低
                    if _is_verbatim_copy(rule, chunk_text):
                        rejected += 1
                        continue
                    all_rules[dim].append({
                        "rule": rule.strip(),
                        "novel_id": nid,
                        "chapter_no": chunk[0][0],
                    })

            # —— v2.283：语序编排提取 ——
            # 结构与普通维度相同（字符串列表），但要过同样的两道校验：
            # 证据验证（没有原文支撑的规则是编的）+ 逐字复制检测。
            wo_raw = _raw_for("word_order")
            if wo_raw is not None:
                wo_parsed = _parse_extract_result(wo_raw)
                if isinstance(wo_parsed, dict):
                    wo_items = wo_parsed.get("语序编排") or []
                    if isinstance(wo_items, list):
                        for rule in wo_items:
                            if not isinstance(rule, str) or not rule.strip():
                                continue
                            if not _validate_evidence(rule, chunk_text):
                                rejected += 1
                                continue
                            if _is_verbatim_copy(rule, chunk_text):
                                rejected += 1
                                continue
                            all_rules["语序编排"].append({
                                "rule": rule.strip(),
                                "novel_id": nid,
                                "chapter_no": chunk[0][0],
                            })

            # —— v2.292：战斗描写提取 ——
            # 与语序编排同构：字符串列表，过证据验证 + 逐字复制检测。
            # 额外拦一道「招式名/专名」——蒸馏 prompt 已要求不写，
            # 但模型偶尔会漏，含专名的规则不可迁移，直接丢。
            cb_raw = _raw_for("combat")
            if cb_raw is not None:
                cb_parsed = _parse_extract_result(cb_raw)
                if isinstance(cb_parsed, dict):
                    cb_items = cb_parsed.get("战斗描写") or []
                    if isinstance(cb_items, list):
                        for rule in cb_items:
                            if not isinstance(rule, str) or not rule.strip():
                                continue
                            if not _validate_evidence(rule, chunk_text):
                                rejected += 1
                                continue
                            if _is_verbatim_copy(rule, chunk_text):
                                rejected += 1
                                continue
                            # 模型有时照抄 prompt 模板，输出「规则描述：xxx」。
                            # 这个前缀是噪声（渲染时已有「打斗——」标签）。
                            # 但「拆解粒度：」「节奏：」这类**分类标签要保留**——
                            # 它们说明了这条规则讲的是哪个方面，有信息量。
                            cleaned = _COMBAT_NOISE_PREFIX_RE.sub("", rule.strip())
                            if not cleaned:
                                continue
                            all_rules["战斗描写"].append({
                                "rule": cleaned,
                                "novel_id": nid,
                                "chapter_no": chunk[0][0],
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
                            all_forbidden[dim].append(item.strip())

            # —— v6：节拍序列提取 ——
            b_raw = _raw_for("beat")
            if b_raw is not None:
                b_parsed = _parse_extract_result(b_raw)
                if isinstance(b_parsed, dict):
                    beats = b_parsed.get("节拍序列") or []
                    for beat in beats:
                        if isinstance(beat, dict) and beat.get("步骤") and beat.get("写法"):
                            rule = f"{beat['步骤']}：{beat['写法']}"
                            if not _is_verbatim_copy(rule, chunk_text):
                                all_rules["节拍序列"].append({
                                    "rule": rule,
                                    "novel_id": nid,
                                    "chapter_no": chunk[0][0],
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
                            all_rules["高频场景模板"].append({
                                "rule": rule,
                                "novel_id": nid,
                                "chapter_no": chunk[0][0],
                            })

            # —— v5：潜台词模式提取 ——
            st_raw = _raw_for("subtext")
            if st_raw is not None:
                st_parsed = _parse_extract_result(st_raw)
                if isinstance(st_parsed, dict):
                    patterns = st_parsed.get("潜台词模式") or []
                    for pat in patterns:
                        if isinstance(pat, dict) and pat.get("模式") and pat.get("写法"):
                            rule = f"{pat['模式']}：{pat['写法']}"
                            if not _is_verbatim_copy(rule, chunk_text):
                                all_rules["潜台词模式"].append({
                                    "rule": rule,
                                    "novel_id": nid,
                                    "chapter_no": chunk[0][0],
                                })

        # 本语料的块已全部跑完：把块数计入累计，供下一本语料接着报进度。
        blocks_done += n_chunks

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

            voice_results = _extract_character_voices(
                provider, db, project, nid, chapters,
                char_names=[n for n, _ in canonical],
                voice_mode=cfg.voice_mode,
                cleaning=cfg.fake_character_cleaning,
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

        # —— L2：主要角色写作档案（独立于角色声音维度）——
        # 使用同一轮清洗后的 canonical 名称，但每个角色单独调用，避免角色
        # 在一个大 prompt 中互相稀释。L2 失败只影响该角色，不影响整本书其余维度。
        if progress is not None:
            progress(
                f"语料 {book_idx}/{total_books}：提取 L2 角色档案",
                blocks_done,
                est_total,
            )
        try:
            # canonical 可能因角色声音阶段异常而未赋值，L2 必须自带兜底，
            # 否则一个维度失败会连带跳过角色档案。
            l2_canonical = canonical if isinstance(canonical, list) else []
            if not l2_canonical:
                l2_canonical, _ = _build_character_name_index(
                    db, project, nid, corpus_text="", cleaning=cfg.fake_character_cleaning
                )
            l2_names = [n for n, _ in l2_canonical if not _is_fake_character(n)]
            character_profiles.extend(_extract_character_profiles(
                provider,
                chapters,
                l2_names,
                novel_id=nid,
                novel_title=getattr(project, "title", "") or "",
                max_characters=VOICE_MAIN_CHARACTERS,
                audit=l2_audit,
            ))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "L2 角色档案提取失败 novel_id=%s: %s", nid, exc
            )

        # —— v3：文风漂移分析（第三轮，对比早晚期）——
        if progress is not None:
            progress(
                f"语料 {book_idx}/{total_books}：文风漂移分析",
                blocks_done,
                est_total,
            )
        try:
            if len(chapters) >= 20:
                early = chapters[:5]
                late = chapters[-5:]
                early_text = "\n\n".join(t for _, t in early if t)[:2000]
                late_text = "\n\n".join(t for _, t in late if t)[:2000]
                drift_system, drift_user = _build_drift_prompt(
                    [("早期（前5章）", early_text), ("晚期（后5章）", late_text)],
                    f"语料{nid}",
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
                        all_rules["文风漂移分析"] = [{
                            "rule": (
                                f"整体趋势：{summary}；"
                                f"早期：{early_str}；"
                                f"晚期：{late_str}；"
                                f"最大变化：{max_change}"
                            ),
                            "novel_id": nid,
                            "chapter_no": 0,
                    }]
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "文风漂移分析失败 novel_id=%s: %s", nid, exc
            )

    if progress is not None:
        progress("正在汇总提炼出的规则", blocks_done, max(blocks_done, 1))

    return {
        "version": NARRATIVE_VERSION,
        "source_ids": sorted(int(i) for i in novel_ids),
        "narrative_version": NARRATIVE_VERSION,
        "tier": cfg.key,
        "tier_name": cfg.name,
        "tier_dimensions": tier_dims,
        "n_chapters": total_chapters,
        "rules": all_rules,
        "characters": character_profiles,
        "l2_audit": l2_audit,
        "forbidden": all_forbidden,
        "rejected": rejected,
        # v2.265：角色名审计（只标记不删除）。
        # 给出「模型认为这些词不像人名」，供前端/人工复核决定是否处理。
        # 不做自动删除——审计本身会误判（北荒刀歌「红薯」是丫鬟名却被标为普通名词）。
        "voice_name_audit": voice_audit or {"suspicious": [], "checked": 0,
                                            "skipped": False},
    }


def extract_character_template(
    db: Session,
    project: Any,
    novel_ids: list[int],
    *,
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
    provider=None,
) -> Any:
    """提炼**人物塑造模板**（独立产物，按档位分开存放）。

    ## 为什么它是独立的一次操作

    叙事档案一次完整档要 42 分钟；而模板只要**每块一次调用**（约 1/8 的量）。
    把模板混进叙事蒸馏，就等于「为了加这一块方法论，把整本档案重蒸一遍」——
    而旧档案其实**没坏**（它只是没有模板这一块）。所以模板单独抽、单独存。

    ## 复用哪套采样

    用**与叙事档案同一套**采样与分块（同一档位 → 同一批章节 → 同一份档位签名）。
    这样两条产物的 id 前缀一致，一眼能看出「这档的档案」与「这档的模板」配对。

    ## 存哪

    `character_templates` 表，id = `<档位>:<档位签名>:<正文哈希前 8>`（自带档位）。
    同一个 (语料, 档位, 档位配置, 正文) 永远只有一行；命中已有就直接应用，不重抽。
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
        block_size = max(
            MIN_CHAPTERS_PER_CHUNK,
            _chapters_per_block(sampled, TARGET_BLOCK_CHARS),
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

        # 专名表过滤（**没有表就整块不产** —— 宁可没有，不要泄漏）。
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


def narrative_fingerprint(
    source_ids: list[int], tier: str | NarrativeTier | None = None,
    *, corpus_hash: str | None = None,
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
        **({"corpus_hash": corpus_hash} if corpus_hash is not None else {}),
    }


def _source_ids_hash(source_ids: list[int]) -> str:
    """参考语料集合的稳定哈希（顺序归一），用于蒸馏库的复用查找键。"""
    import hashlib

    return hashlib.sha256(
        ",".join(str(i) for i in sorted(int(x) for x in source_ids)).encode("utf-8")
    ).hexdigest()


def corpus_content_hash(source_ids: list[int], db: Session) -> str:
    """Hash exact ordered chunk content and cleaning provenance.

    Read persisted chunks rather than trusting a report's stored digest. Include
    chapter/idx boundaries and cleaning versions so re-imports and pipeline
    upgrades invalidate both the project and shared-library cache.
    """
    import hashlib

    from app.models import RawNovel, NovelChunk
    from app.ingestion.clean import CONSUMPTION_CLEAN_VERSION
    h = hashlib.sha256(f"distill-corpus-binding-2:consumption-{CONSUMPTION_CLEAN_VERSION}".encode())
    for nid in sorted(set(int(i) for i in source_ids)):
        novel = db.get(RawNovel, nid)
        report = (novel.clean_report or {}) if novel else {}
        metadata = [nid, {key: report.get(key) for key in (
            "pipeline_version", "consumption_version", "cleaned_fingerprint")}]
        h.update(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        rows = db.execute(select(NovelChunk.chapter_no, NovelChunk.idx, NovelChunk.text)
                          .where(NovelChunk.novel_id == nid)
                          .order_by(NovelChunk.chapter_no, NovelChunk.idx, NovelChunk.id))
        for row in rows:
            h.update(json.dumps(list(row), ensure_ascii=False).encode("utf-8"))
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
            StyleDistill.narrative_version == NARRATIVE_VERSION,
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
            StyleDistill.narrative_version == NARRATIVE_VERSION,
            StyleDistill.status == "active",
        )
    )
    rules = profile.get("rules") or {}
    rule_count = sum(len(v) for v in rules.values() if isinstance(v, list))
    # 可读的档位键（与人物塑造模板的 id 同一套格式）：哪一档、哪次配置、哪一版语料。
    product_key = f"{cfg.key}:{_tier_signature(cfg)}:{(content_hash or '')[:8]}"
    if existing is not None:
        existing.style_narrative = profile
        existing.n_chapters = profile.get("n_chapters")
        existing.rule_count = rule_count
        existing.rejected = profile.get("rejected")
        existing.narrative_version = int(profile.get("narrative_version", NARRATIVE_VERSION))
        existing.product_key = product_key
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
        product_key=product_key,
    )
    db.add(row)


def project_narrative(
    db: Session,
    project,
    *,
    force: bool = False,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
) -> dict | None:
    """取项目当前该用的叙事档案；没有参考语料则返回 None。

    已存且指纹一致就直接返回——与 `style_profile` 同理，叙事档案进的是静态 system 块，
    每次生成都变会击穿提示词缓存。

    档位缺省时读`project.narrative_tier`（项目级偏好，已落库）；
    那个字段也为空则用默认完整档。

    Args:
        progress: 可选的进度回调，原样透传给 `extract_narrative_profile`
            （后台蒸馏任务的进度条用）。None 时行为与加进度之前完全一致。

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

    content_hash = corpus_content_hash(ids, db)
    want = narrative_fingerprint(ids, cfg, corpus_hash=content_hash)
    cached = project.style_narrative or {}
    profile = None

    if (not force and isinstance(cached, dict)
            and cached.get("narrative_version") == NARRATIVE_VERSION
            and cached.get("fingerprint") == want):
        # 命中项目缓存：一次 LLM 都不会调，进度条直接 100%，并把「没重算」写进 stage。
        if progress is not None:
            progress("命中缓存，本次无需重新蒸馏", 1, 1)
        profile = cached
        # 修复：缓存命中即直接 return。原设计还要调 _persist_style_distill 把
        # 档案"重落"到全局蒸馏库——但项目缓存命中 = profile 已经从某种途径
        # （含蒸馏库）落过全局库了（project.style_narrative 不可能凭空有内容），
        # 再调一次是冗余；而且 find_style_distill 内部用 db.scalar()，
        # FakeDB mock 测试会抛 AttributeError，被 project_narrative_safe 兜底
        # catch 后返回 None，覆盖了正确返回的 profile。
        return profile
    else:
        # ── 蒸馏库复用（跨项目共享层）── 同语料+同档位+同正文 已蒸馏过 → 直接复用。
        # force 时跳过（force = 用户明确要求重算，不复用任何缓存/库）。
        if not force:
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
                db, project, ids, max_chapters=max_chapters, tier=cfg, progress=progress
            )
            profile["fingerprint"] = want
            project.style_narrative = profile

    # —— 统一收尾：任何一次「完成的蒸馏」都把产物落进全局蒸馏库（幂等 upsert）——
    # 「蒸馏完成的加一个库」的核心：无论命中项目缓存、命中蒸馏库、还是真实提取，
    # 只要本次产出了可用档案，全局库里就有它，标着档位，任何项目可直接复用。
    if content_hash is None:
        content_hash = corpus_content_hash(ids, db)
    _persist_style_distill(db, project, ids, cfg, content_hash, profile)
    db.commit()
    return profile


def project_narrative_safe(
    db: Session,
    project,
    *,
    force: bool = False,
    max_chapters: int = 180,  # v6: 默认 30% × 600 章 ≈ 180
    tier: str | NarrativeTier | None = None,
    progress: ProgressCallback | None = None,
) -> dict | None:
    """生成链路上的安全包装。与 `style_profile_safe` 同理：失败只能降级为「不注入」，
    绝不能让整次生成失败。

    `progress` 只是透传（生成链路默认不传，即 None）：加这个参数**不改**生成路径
    的任何行为，纯粹为了让后台蒸馏任务能复用同一个函数并拿到进度。
    """
    try:
        return project_narrative(db, project, force=force, max_chapters=max_chapters,
                                 tier=tier, progress=progress)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "叙事档案不可用，本次生成跳过叙事档案块：%s: %s", type(exc).__name__, exc
        )
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


# ─────────────────────────────────────────────────────────────────────────
# 叙事档案 → 生成提示词的安全渲染
# ─────────────────────────────────────────────────────────────────────────

_EVIDENCE_TAIL_RE = re.compile(r"[（(]\s*证据\s*[:：].*?[）)]\s*", re.S)
_ACTION_CHAIN_RE = re.compile(r"动作链\s*=\s*([^\s]+)")
_VOICE_NAME_PREFIX_RE = re.compile(r"^[^\s：:]{1,8}\s*[：:]\s*")
# 「关系动态：…」字段：到下一个「代表台词」或行尾为止
_RELATION_FIELD_RE = re.compile(r"关系动态\s*[：:].*?(?=代表台词|$)")

# 这三个维度的规则正文写的是「参考语料里发生过什么」，不是「怎么写」。
# 蒸出来的条目形如「升温：陆崇说服陆龙象去玄真山学武」——原样注入 prompt 等于
# 把参考小说第 1 章的剧情喂给模型，模型会照着复读。
#
# v2.272 五章实测证据：5 章全在重写王府迎接道人那一段，第 4/5 段前 3 句几乎逐字相同。
# 而项目 premise/outline/plot_bible 全空——模型除了这 5 条节拍和 3 个场景模板，
# 没有任何「接下来该发生什么」的信息，于是每章都从第 1 章重启。
_PLOT_BEARING_DIMS = {"节拍序列", "高频场景模板", "潜台词模式"}


def _strip_evidence(text: str) -> str:
    """去掉规则尾部的「（证据：原文引用）」。

    证据原文是**参考语料的原句**，含参考小说的专有人名。它进 system prompt 有三个
    坏处：① 把参考语料的剧情与人物带进本项目——而 `kg/queries.py` 明确要求生成时
    只能用本书事实，参考语料角色不能当成本书角色；② 与【角色人设约束】互相矛盾
    （约束里说「不得凭空新增主要人物」，叙事档案里却列着几十个参考语料人物）；
    ③ 无谓膨胀提示词。

    实测（《北荒刀歌》 990 章蒸馏产物，每维注入前 3 条）：整条注入共出现 **255 处**
    参考语料人名；剥掉证据后 **0 处**。也就是说泄漏全部来自证据引文，规则本身的
    抽象描述是干净的——所以是剥证据，而不是放弃这些维度。
    """
    return _EVIDENCE_TAIL_RE.sub("", text).strip(" ；;，,")


#: 规则尾部的证据引文（与 `_EVIDENCE_TAIL_RE` 同形，只是把内容取出来而不剥掉）。
_EVIDENCE_CAPTURE_RE = re.compile(r"[（(]\s*证据\s*[:：](.*?)[）)]\s*", re.S)

#: 例句的长度上限（字）。太长会撑爆提示词，也容易被模型整句照抄。
EXAMPLE_MAX_CHARS = 25


def _extract_example(text: str) -> str:
    """从一条规则的尾部取出证据原句，当作**例句**。

    ## 为什么它可以当例子

    证据是**参考语料里的真实句子**（不是谁编的），而且蒸馏时已经要求
    「规则要具体可操作，例如『用连续动作描写紧张感』而不是『文笔好』」——
    证据正是这句话在原文里的样子。

    ## 取法

    取第一句、截到 `EXAMPLE_MAX_CHARS` 字。剩下的交给调用方脱敏。
    """
    m = _EVIDENCE_CAPTURE_RE.search(text or "")
    if not m:
        return ""
    ev = m.group(1).strip()
    for sep in ("。", "！", "？", "\n"):
        if sep in ev:
            ev = ev.split(sep, 1)[0].strip()
    if len(ev) > EXAMPLE_MAX_CHARS:
        ev = ev[:EXAMPLE_MAX_CHARS]
    return ev


def _strip_forbidden_evidence(forbidden: dict) -> dict:
    """禁止项也可能带「（证据：…）」尾巴，同样要剥掉——否则等于把原文句子
    列进「严禁使用的表达」，模型看到反而会记住这些词。

    只做剥除与去重保序，不改变结构（仍是 dict[str, list[str]]）。
    """
    out: dict = {}
    for dim, items in (forbidden or {}).items():
        if not isinstance(items, list):
            out[dim] = items
            continue
        seen: set = set()
        cleaned: list = []
        for x in items:
            t = _strip_evidence(str(x)) if isinstance(x, str) else x
            if not t or t in seen:
                continue
            seen.add(t)
            cleaned.append(t)
        if cleaned:
            out[dim] = cleaned
    return out


def _prompt_form(dim: str, text: str) -> str:
    """把一条规则转成「可安全进生成提示词」的形式。空串表示这条不该注入。"""
    t = _strip_evidence(text)
    if not t:
        return ""
    if dim == "节拍序列":
        # 「升温：陆崇说服陆龙象去玄真山学武」→「升温」。留下的才是手法骨架。
        return t.split("：", 1)[0].strip()
    if dim == "高频场景模板":
        # 只保留动作链（「迎接→议论→解释→等待→回应→离去」），
        # 丢掉环境/站位/基调 —— 那三样全是参考小说的具体场景。
        #
        # ## v2.281：动作链也要限长，否则「抽象」是假的
        #
        # 实测（M3 蒸馏，791 条高频场景模板 100% 命中 _ACTION_CHAIN_RE）：
        #
        #   原文  王府迎客：环境=北荒王府位于清凉山…站位=北荒王亲自出迎…
        #   动作链 贵客抵达→王府开中门并铺陈仪仗→主人亲自迎接→…
        #
        #   原文  院落收徒：…
        #   动作链 老神仙到来→发现小王爷拒绝拜师→陆崇蹲下循循善诱→
        #          以糖葫芦、武将前程等诱引→小王爷继续沉默并故意挑衅
        #
        # 第二条里「糖葫芦」「拒绝拜师」是**参考书的具体情节**，不是手法。
        # 智谱时代的规则只有 17 字，剥离后天然只剩标签；M3 的规则 231 字，
        # 剥离后仍是完整剧情——人名被 _redact 换成「某某」，但情节照旧注入。
        #
        # 判据用「箭头数」而非字数：真正的手法是 3-5 拍的短链
        # （迎接→议论→解释→离去），带具体情节的链会有 6+ 拍且每拍都长。
        m = _ACTION_CHAIN_RE.search(t)
        if not m:
            return ""
        chain = m.group(1).strip()
        beats = [b.strip() for b in chain.split("→") if b.strip()]
        if len(beats) < 2:
            return ""
        # v2.281：蒸馏 prompt 已改为「只写结构，不写内容」，产出的动作链
        # 本身就是抽象骨架（「来客抵达→主人接待→试探交谈→达成或谈崩→一方离去」），
        # 所以这里**完整保留**，不再截成首尾两拍。
        #
        # 之前截断是因为旧 prompt 要求「每个场景必须附原文中的具体细节」，
        # 模型输出的链裹着剧情（「以糖葫芦诱引小王爷」），只能靠丢中间拍来降噪。
        # 从源头修好后，截断反而会丢掉结构信息（「试探交谈」这一拍是骨架的核心）。
        #
        # 仍保留一道防线：链过长（>8 拍）说明模型没遵守约束，退回首尾两拍。
        if len(beats) > 8:
            return f"{beats[0]}→{beats[-1]}"
        return chain
    if dim == "潜台词模式":
        # 「反话正说：北荒王府都解释成傻人有傻福」→「反话正说」
        return t.split("：", 1)[0].strip()
    if dim == CHARACTER_VOICE_DIMENSION:
        # 「赵青锋：语气冷淡，带有嘲讽」→「语气冷淡，带有嘲讽」
        # 角色名是参考语料的人物，不能当成本书角色注入；要学的是声音特征本身。
        t = _VOICE_NAME_PREFIX_RE.sub("", t, count=1).strip()
        # 再剥「关系动态：对父亲赵敬城冷淡…」——它记的是**参考小说人物之间**的
        # 关系，本项目角色不同，这层信息既无用又是最大的人名载体。
        t = _RELATION_FIELD_RE.sub("", t).strip()
        return t
    return t


def _group_by_chapter(items: list) -> tuple[dict, list]:
    """把规则按章号分组，返回 (分组, 升序章号列表)。

    组内保持原始追加顺序（同一章里规则是有先后的，不能重排）。
    章号升序而不是「首次出现顺序」—— 后者依赖调用方恰好按章序追加，
    一旦某维的规则来自多个来源（如 chapter_no=0 的兜底条），成熟期就会算错。
    """
    groups: dict = {}
    for item in items:
        ch = item.get("chapter_no") if isinstance(item, dict) else None
        key = int(ch) if ch is not None else -1
        groups.setdefault(key, []).append(item)
    return groups, sorted(groups)


def _mature_chapters(chapters: list) -> list:
    """成熟期 = 章号较大的一半（早期章节文风会漂，不代表全书）。"""
    half = len(chapters) // 2
    return chapters[half:] or chapters


def _select_dim_rules(items: list, limit: int) -> list:
    """挑出注入提示词的手法规则：**成熟期优先**、跨章摊开。

    ## 为什么不取「追加顺序的前 N 条」

    规则是按块（=按章节顺序）追加的，所以「前 N 条」= **最早**的那几章。
    实测真书（《北荒刀歌》，采样 297 章）：注入提示词的 51 条规则里，
    13 个维度的前 2 条**全部来自第 1~11 章** —— 采样面的 **94%**
    （第 12 章之后）对提示词零影响。

    而采样器本来就该给**成熟期**文风（早期章节会漂，`_sample_chapters`
    的注释就是这么写的）。取最早 = 拿最生涩的那部分去代表全书。

    ## 现在的取法

    1. 只在**成熟期**里挑（该维有规则的章节按章号排序，取后一半）；
    2. 成熟期里**均匀取** limit 个不同的章（不是全挤在最后两章）：
       各维度取的章号会错开，提示词不会变成「同一个场景的说明书」；
    3. 每章取该章最早追加的那条（确定性：同一份档案永远选出同一批）。

    ## 旧档案怎么办

    早期档案的规则是**纯字符串**（没有 chapter_no），没法算成熟期。
    这类数据一律退回旧取法（按原顺序取前 limit 条），
    不因为「没有章号」就只剩一条。

    代价：**零模型调用**。规则早就在档案里，只是换了个挑法。
    """
    if limit <= 0 or not items:
        return []
    groups, chapters = _group_by_chapter(items)
    known = [c for c in chapters if c != -1]
    mature = _mature_chapters(known) if known else []
    picked_chapters: list = []
    if mature:
        if limit == 1:
            # 只取一条时，「摊开」无从谈起 —— 取成熟期的中点，
            # 而不是边界（最后一章可能是尾声，文风另有偏差）。
            picked_chapters = [mature[len(mature) // 2]]
        elif len(mature) <= limit:
            picked_chapters = list(mature)
        else:
            step = (len(mature) - 1) / (limit - 1)
            indexes = sorted({round(i * step) for i in range(limit)})
            picked_chapters = [mature[i] for i in indexes]
    picked = [groups[ch][0] for ch in picked_chapters]
    if len(picked) < limit:
        # 不够就按原顺序补足（没有章号的旧数据走的就是这条路）。
        chosen = {id(x) for x in picked}
        for item in items:
            if len(picked) >= limit:
                break
            if id(item) not in chosen:
                picked.append(item)
                chosen.add(id(item))
    return picked


def _select_beat_steps(items: list, limit: int) -> list:
    """挑出节拍骨架：**成熟期**一章的完整节拍弧，按步骤顺序输出。

    ## 为什么不能复用 _select_dim_rules

    节拍维度的单位不是「一条规则」，而是**一章的一条弧**：
    每章产出「铺垫 → 升温 → 爆发 → 余波 → 新钩子」5~7 步，
    追加顺序就是弧的顺序（渲染时用「→」串起来）。

    按「每章取最早追加的那条」挑，每章只剩第一步 → 弧塌成
    「铺垫 → 铺垫 → 铺垫」，顺序信息全丢。

    所以这里改成：按章号升序遍历成熟期各章的节拍，
    按步骤标签去重，凑满 limit 个不同步骤就停 ——
    得到的仍是一条完整弧，但取自成熟期而不是第 1 章。
    """
    if limit <= 0 or not items:
        return []
    groups, chapters = _group_by_chapter(items)
    mature = _mature_chapters(chapters)
    picked: list = []
    seen_labels: set[str] = set()
    for ch in mature:  # 升序：弧的先后不能乱
        for item in groups[ch]:
            raw = item.get("rule", "") if isinstance(item, dict) else str(item)
            label = raw.split("：", 1)[0].strip()
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            picked.append(item)
            if len(picked) >= limit:
                return picked
    return picked


def describe_narrative(
    profile: dict | None,
    max_rules_per_dim: int | None = None,
    *,
    include_voice: bool = True,
    dims: Sequence[str] | None = None,
    voice_character: str | None = None,
    redactor: Any = None,
    example_guard: Any = None,
) -> str:
    """把叙事档案翻成一段确定性文字，用于 system prompt 的静态【叙事档案】块。

    每个维度取 `max_rules_per_dim` 条（不给则用 `RULES_PER_DIM`），
    **取法不是「追加顺序的前 N 条」**——那是**最早**的几章，等于用最生涩的部分
    代表全书。改成：成熟期优先 + 跨章摊开（`_select_dim_rules`），
    节拍另走 `_select_beat_steps`（取一章的完整弧）。
    确定性意味着：同一份档案永远产出同一段文本，不随段号变化。

    ## v2.273：注入前做「去剧情化」

    这条链路上有两个独立的坑，都会让生成复读参考小说：

    1. **证据引文**——每条规则都以「（证据：原文引用）」结尾，证据是参考语料原句，
       含参考语料的专有人名。实测每维前 3 条共 255 处人名泄漏，剥掉后 0 处。
    2. **剧情型维度的正文本身**——节拍序列/高频场景模板/潜台词模式的规则写的是
       「参考小说第 1 章发生了什么」，不是「怎么写」。这三个维度只保留手法骨架
       （节拍标签、动作链、潜台词类型），具体人物地名一概丢弃。

    角色声音词典同理：保留「自称『本世子』/句长混合/语气嘲讽」这些声音特征，
    丢掉角色名前缀——名字属于参考小说，不能当成本书角色。

    ## v2.274：不要用【】把维度名渲染成小标题

    旧版每个维度前面有一行 `【人物描写手法】`。去剧情化把规则剥成了极短的抽象句
    （「通过外貌描写塑造人物形象」「环境描写细致入微」），配上全角方括号，
    整体看起来像**一份待填的任务清单**。

    实测（同一 prompt 只改档案渲染格式，要求写满 2400 字）：

        【维度】小标题      8331 字，58% 段落重复，正文里出现 83 个 `【人物描写手法·心理描写】` 之类的自造小标题
        `· 维度：` 列表      2657 字，10% 重复
        散文式一段话        2214 字，**0% 重复**

    模型把维度名当成了「正文要写的小节标题」，逐条演示一遍然后循环。
    所以这里不再输出任何方括号和分点符号，把手法揉成不带标记的自然段落。

    ## v2.276：include_voice 开关

    `角色声音词典` 是唯一一个「讲的是参考书角色怎么说话」的维度：

    - 原创小说：必须关闭。它的内容形如「自称『本世子』口头禅『如何办』」，
      模型会照着造一个「本世子」，等于把参考书角色写进用户的书。
      原创应由 `人物塑造模板` 提供方法论（P0-B 实现），不含任何具体角色。
    - 续写：必须开启。写的就是原书角色，这份词典是角色一致性锚点。
    - 角色聊天：开启，但只取被扮演的那一条（上游已过滤，这里拿到的就是单条）。
    """
    if not profile:
        return ""
    if max_rules_per_dim is None:
        max_rules_per_dim = RULES_PER_DIM
    # v2.282：从分层结构取 method 层。旧档案（只有 rules）由 normalize_layers
    # 整体归到 method，行为逐字节不变；新档案里 characters/world/timeline
    # 三层的实体内容**根本不会被读到**——这是分层的主要收益。
    from app.style.layers import method_rules

    rules = method_rules(profile)
    forbidden = profile.get("forbidden") or {}
    if not rules and not forbidden:
        return ""
    forbidden = _strip_forbidden_evidence(forbidden)

    # 参考语料的人物名：角色声音词典的每条都以「名字：」开头，前缀就是人物名。
    # 这份名单不需要访问图谱，自给自足。渲染完统一把这些人名涂掉——
    # 残留位置是「口头禅『徐橘子，真名士！』」「对赵珣的称呼」这类正文内部，
    # 逐个字段剥不干净，统一替换最稳。替换成「某某」而不是删掉，保持句子通顺。
    ref_names: set[str] = set()
    for item in (rules.get(CHARACTER_VOICE_DIMENSION) or []):
        raw = item.get("rule", "") if isinstance(item, dict) else str(item)
        m = _VOICE_NAME_PREFIX_RE.match(raw)
        if m:
            name = m.group(0).rstrip("：: ").strip()
            if 2 <= len(name) <= 5:
                ref_names.add(name)

    # —— 按维度收集手法短句（v2.290：每维一行，不再缝成一段）——
    # 每个维度压成一行：`标签——短句1；短句2`
    # 排除的维度：
    #   角色声音      —— 单独渲染（按角色一条，混进手法描述会把句子撑爆）
    #   文笔量化审计  —— 是统计数字，且分块统计彼此冲突（句长同时出现 53.0/44.0/50.2），
    #                    塞进散文只会让模型在四个矛盾数字之间摇摆
    #   文风漂移分析  —— 讲的是「作者前后期风格变了」，对单次生成没有可执行含义
    #   节拍序列      —— 单独渲染成「节拍骨架参考：铺垫 → 升温 → 对峙」。
    #                    手法行里再放一遍只会剩一个孤零零的「铺垫」标签，
    #                    既重复又比骨架那行信息少。
    _MAIN_EXCLUDE = {CHARACTER_VOICE_DIMENSION, "文笔量化审计", "文风漂移分析", "节拍序列"}
    all_dims = NARRATIVE_DIMENSIONS + ["文风漂移分析"]
    # `dims` 是**创作路径**给的维度白名单（`PROFILE_RULES[...]["narrative_dims"]`）。
    # None = 全部（原创/续写）；空元组 = 一个手法维度都不给（角色聊天 ——
    # 那个角色不需要「伏笔怎么埋」「打斗怎么写」这些写整本书的手法）。
    if dims is not None:
        allowed = set(dims)
        all_dims = [d for d in all_dims if d in allowed]
    phrases: list[str] = []          # 兼容旧变量名：现在是「每维一行」
    for dim in all_dims:
        if dim in _MAIN_EXCLUDE:
            continue
        items = rules.get(dim) or []
        if not items:
            continue
        label = DIM_LABELS.get(dim)
        if not label:
            continue
        rendered: list[str] = []
        seen: set[str] = set()
        for item in _select_dim_rules(items, max_rules_per_dim):
            raw = item.get("rule", "") if isinstance(item, dict) else str(item)
            t = _clip(_prompt_form(dim, raw))
            if not t or t in seen:
                continue
            seen.add(t)
            # 例句：证据原句脱敏后附在这条要求后面。
            # 没有 redactor（拿不到专名表）⇒ **不给例句** ——
            # 证据是参考书的原句，不脱敏会把参考小说的人物带进用户的小说。
            ex = _extract_example(raw)
            if ex and redactor is not None:
                try:
                    ex = redactor.redact(ex)
                except Exception:  # noqa: BLE001 —— 脱敏失败就不给例子
                    ex = ""
                ex = _clip(ex)
                # 兜底闸：专名表**保证不了覆盖**（实测表外仍有专名），
                # 所以再查一遍「判定前的 PMI 候选」——命中就整句丢掉。
                if ex and example_guard:
                    try:
                        if any(g and g in ex for g in example_guard):
                            ex = ""
                    except TypeError:  # pragma: no cover —— 候选集形状不对就跳过
                        pass
                if ex:
                    t = f"{t}（例：{ex}）"
            rendered.append(t)
        if not rendered:
            continue
        phrases.append(f"{label}——" + "；".join(rendered))

    # 节拍骨架单独保留（「铺垫 → 升温 → 爆发」是顺序信息，揉进散文会丢）
    beat = ""
    if dims is None or "节拍序列" in set(dims):
        for item in _select_beat_steps(rules.get("节拍序列") or [], max_rules_per_dim):
            raw = item.get("rule", "") if isinstance(item, dict) else str(item)
            t = _prompt_form("节拍序列", raw)
            if t:
                beat = (beat + " → " + t) if beat else t

    # 角色声音按角色各一条（不截断，角色多才有效），合成一句
    voices: list[str] = []
    seen_v: set[str] = set()
    voice_items = (rules.get(CHARACTER_VOICE_DIMENSION) or []) if include_voice else []
    if voice_character:
        # 角色聊天：只取**被扮演的那一个**角色的声音条目。
        # 之前这里不过滤 —— 角色说话方式一起来了，模型会把几个角色的腔调混着演。
        wanted = str(voice_character).strip()
        picked = []
        for item in voice_items:
            raw = item.get("rule", "") if isinstance(item, dict) else str(item)
            m = _VOICE_NAME_PREFIX_RE.match(raw)
            nm = m.group(0).rstrip("：: ").strip() if m else ""
            if nm == wanted:
                picked.append(item)
        voice_items = picked
    for item in voice_items[:20]:
        raw = item.get("rule", "") if isinstance(item, dict) else str(item)
        t = _prompt_form(CHARACTER_VOICE_DIMENSION, raw)
        if t and t not in seen_v:
            seen_v.add(t)
            voices.append(t)

    def _redact(text: str) -> str:
        """把参考语料的人物名替换成「某某」，长的优先（避免「赵」先吃掉「赵青锋」）。"""
        for nm in sorted(ref_names, key=len, reverse=True):
            if nm in text:
                text = text.replace(nm, "某某")
        return text

    parts: list[str] = []
    if phrases:
        # 总长封顶：靠后的维度（潜台词/语序）边际贡献小，先丢
        kept: list[str] = []
        used = 0
        for line in phrases:
            if used + len(line) > _ARCHIVE_MAX_CHARS and kept:
                break
            kept.append(line)
            used += len(line)
        body = _redact("\n".join(kept))
        parts.append(
            "【叙事手法（每行一种，讲的是怎么写，不是要写的内容）】\n"
            f"{body}\n"
            "以上是写作手法的整体倾向，请让它们自然体现在叙述里；"
            "**不要在正文中出现任何手法名称、小节标题、编号或方括号标记**，"
            "正文只能是小说叙述与对白。"
        )
        # v2.291：重复要「有新信息」，不限制次数
        #
        # 蒸馏产物里有一条真实文风特征：「冲突升级以同一动作的重复描写
        # 逐步加重力度，避免一次写尽」。这句是**作者的写法**，不该被否定。
        #
        # 首版写成「单节内同一个动作最多重复两次」，方向错了：
        # 一场战斗在一个区域打三章、两人对峙拉锯两章，都是长篇的正常写法。
        # 实测三章困在船上，真正原因是**没按大纲走**（大纲要求客栈/药铺/
        # 追兵），不是「重复」本身。机械限次会破坏作者的节奏感。
        #
        # 保留的是质量问题而不是次数问题：重复必须带来增量，
        # 这是对任何作者都成立的标准。
        parts.append(
            "同一个动作、同一处场景可以反复写，但每次重复都要带来新信息——"
            "新的变化、新的代价或新的认知；不能只是同一段描写换个说法再说一遍。"
        )
    if beat:
        parts.append(f"节拍骨架参考：{beat}（本节不必走完整条链，按情节需要取其中一两拍）。")
    if voices:
        vbody = _redact("；".join(voices))
        parts.append(f"人物说话方式的参考（学语气与句长，不要照抄台词）：{vbody}。")

    # 反向禁忌：具体的「不要用 X」比通用的「忌 AI 腔」更有效
    forbidden_lines: list[str] = []
    for dim in FORBIDDEN_DIMENSIONS:
        items = forbidden.get(dim) or []
        if not items:
            continue
        seen = set()
        unique = []
        for x in items:
            if x not in seen:
                seen.add(x)
                unique.append(x)
        forbidden_lines.append(f"【{dim}】禁止使用：{'、'.join(unique[:8])}")

    if not parts and not forbidden_lines:
        return ""
    out = []
    if parts:
        out.append("\n".join(parts))
    if forbidden_lines:
        out.append("【反向禁忌：参考作者从不使用以下表达，严禁出现在生成文本中】\n" + "\n".join(forbidden_lines))
    return "\n\n".join(out)
