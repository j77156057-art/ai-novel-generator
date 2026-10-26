"""三种生成模式的上下文策略。**唯一的差异集中定义处。**

## 为什么需要这个模块

同一份参考语料，在三个功能里必须扮演不同角色，而旧代码只有一条组装链路
（`build_context` + `build_system_prompt`），把三件事混成了一件。

v2.274 五章实测的 user prompt 构成（实 measurement，不是估算）：

    【检索上下文】4839 字  70.4%   ← 雪中原句，按「剧情相关」检索而来
    【风格样例】  1144 字  16.6%   ← 雪中原句，按采样而来
    【前情提要】   255 字   3.8%   ← 真正决定「写到哪了」
    【大纲】       276 字   4.0%
    【续写指令】   158 字   2.3%

后果：大纲写「北漠」，正文写「北莽」16 次、「北凉」13 次；段 1/3/4/5 全部从
「风沙刮过横水城城墙」重启；段 2 起凭空出现大纲里没有的「萧家」「萧烈」「阿三」。

根因一句话：**检索上下文用「本书剧情」去检索「参考书剧情」，语义上必然失败，
命中的是无关章节（实测第 167、512 章），唯一效果是把参考书的世界观和人物灌进 prompt。**

## 三种模式的分界线

| | 原创 original | 续写 continuation | 角色聊天 roleplay |
|---|---|---|---|
| 目标 | 写全新故事 | 接作者原文往下写 | 扮演单个角色对话 |
| 原文正文 | ❌ 不可见 | ✅ 可见（前后文） | ⚠️ 只见该角色台词 |
| 参考角色名 | ❌ 不可见 | ✅ 可见 | ⚠️ 只见被扮演者 |
| 参考地名门派 | ❌ 不可见 | ✅ 可见 | ❌ 不可见 |
| 用户剧情 | ✅ 唯一来源 | ⚠️ 与原文合并 | ❌ |

一句话：**原创 = 方法可见、实体不可见；续写 = 全可见但锚定原文；
角色聊天 = 单实体可见、其余全部不可见。**
"""

from __future__ import annotations

from typing import Literal

#: 三种模式
ContextProfile = Literal["original", "continuation", "roleplay"]

PROFILES: tuple[str, ...] = ("original", "continuation", "roleplay")

#: 默认模式。老调用方（未显式指定）走原创——它的可见范围最小，
#: 出错时最多是「参考资料变少」，不会把参考书的世界观悄悄灌进用户的小说。
DEFAULT_PROFILE: ContextProfile = "original"

#: 「写法参考」路径：参考书是**学写法**用的，不是「要接着写的故事」。
#:
#: 这类路径下整本参考书都是合法的风格材料 —— 不存在「故事的后续」要防，
#: 所以可见性策略在这条路径上不设边界（用户显式设了 `reference_cutoffs` 仍然优先）。
#:
#: 续写与角色聊天**不在此列**：那两条路径下参考书就是用户正在读/续的那本，
#: 让模型看见后面的章节等于剧透。
STYLE_REFERENCE_PATHS: frozenset[str] = frozenset({"original"})


#: 叙事维度在这三种模式下的可见性。
#:
#: `角色声音词典` 是唯一一个「讲的是参考书角色怎么说话」的维度：
#: 原创里绝不能进（会把参考角色带进来），续写里必须进（写的就是原书角色），
#: 角色聊天里只取被扮演的那一个。
#:
#: `文笔量化审计` 分块统计互相矛盾（句长同时是 53.0/44.0/50.2），
#: 任何模式都不注入——只会让模型在四个数字之间摇摆。
#: 续写时若需要数字锚定，用 L1 统计指纹（stylometry），那份是全篇汇总的。
METHOD_DIMS: tuple[str, ...] = (
    "人物描写手法",
    "对白风格",
    "段落编排",
    "场景构建",
    "情绪表达",
    "节奏控制",
    "世界观呈现",
    "剧情编排模式",
    "伏笔与悬念设计",
    "节拍序列",
    "高频场景模板",
    "潜台词模式",
    "语序编排",
)

PROFILE_RULES: dict[str, dict] = {
    # ── 原创：只给方法，不给任何实体 ─────────────────────────────
    "original": {
        # 检索上下文：彻底关闭。
        # 用本书剧情检索参考书剧情不成立——两本书的人物事件本来无关，
        # BM25 只能命中「刀」「城」这类常见字，拿到的是无关章节原文。
        # 文风学习交给脱敏样例（保留句法、专名换占位符）。
        "rag": False,
        # 风格样例：脱敏版（专名 → 【人名N】/【地名N】/【门派N】）
        "style_exemplars": "redacted",
        # 维度可见性
        "narrative_dims": METHOD_DIMS,
        # 角色声音词典不进（它讲的是参考书角色）
        "include_voice": False,
        # 改成「人物塑造模板」——从参考语料归纳的方法论，不含任何专名台词
        "include_character_template": True,
        # 角色与世界观：只能来自用户（plot_bible / outline / premise）
        "characters_from": "user",
        "world_from": "user",
        # 生成图谱（自己写过的段落抽取的事实）：可用，这是本书自己的连续性
        "kg_from_generated": True,
        # 参考语料图谱（地名/门派/角色）：不可用
        "kg_from_reference": False,
    },
    # ── 续写：全都可见，但锚定原文 ───────────────────────────────
    "continuation": {
        "rag": True,
        "style_exemplars": "raw",
        "narrative_dims": METHOD_DIMS,
        "include_voice": True,
        "include_character_template": False,
        "characters_from": "reference",
        "world_from": "reference",
        "kg_from_generated": True,
        # 参考语料图谱进 prompt，作为「既有设定」——这次允许，因为写的就是这本书
        "kg_from_reference": True,
        # 情节可调，但三样冻结（用户决策）：
        #   人物关系 / 世界观 / 既有事实（前 N-1 章已发生的死亡、获得、身份揭露）
        "frozen_facts": ("relations", "world", "events_before_n"),
    },
    # ── 角色聊天：只看得见一个角色 ───────────────────────────────
    "roleplay": {
        # v2.293：只做**角色定向检索**，不做剧情检索。
        #
        # 原设计是 `False`，理由「角色不该知道相关剧情」——这个判断对剧情成立，
        # 但对角色自己的往事不成立：被扮演的角色需要记得自己说过什么、
        # 二十年前杀过谁、跟眼前这个人有什么旧账，否则开口全是空话。
        #
        # 实测（probe_strategies_real.py）：按人名做 BM25 精确检索，
        # 宋庭鹭/温华/红薯/褚禄山 四个角色 4/4 条都真正命中该角色出场的原文。
        # 用错策略就会跑偏：拿整段剧情当 query 检索，命中的是「和剧情相关的章节」，
        # 而不是「这个角色的章节」。
        "rag": "character",
        # 样例只给被扮演角色自己说过的台词
        "style_exemplars": "single_character_lines",
        "narrative_dims": (),
        "include_voice": True,          # 只取被扮演的那一条（上游过滤）
        "include_character_template": False,
        "characters_from": "single",
        # 世界观不给：角色不知道自己是小说人物，也不需要背设定文档
        "world_from": None,
        "kg_from_generated": True,      # 该角色的经历
        "kg_from_reference": False,
    },
}


def rules_for(profile: ContextProfile | str) -> dict:
    """取某种模式的规则。未知模式退回默认（原创），不抛异常。

    退回默认而不是报错，是因为这个值会从 API 一路传进来；一个拼错的
    profile 名不该让整次生成失败，退到可见范围最小的模式最安全。
    """
    return PROFILE_RULES.get(str(profile), PROFILE_RULES[DEFAULT_PROFILE])


def normalize_profile(profile: ContextProfile | str | None) -> ContextProfile:
    """把外部传入的 profile 规范成合法值。None / 未知值 → 默认原创。"""
    p = str(profile or "").strip().lower()
    return p if p in PROFILES else DEFAULT_PROFILE  # type: ignore[return-value]


#: 故事锚定在参考书上时对应的创作路径。
#:
#: `story_authority='reference_anchor'` 的含义就是「这段正文接在参考书的故事后面」，
#: 而续写（continuation）是**唯一**会打开参考书原文可见性的路径 —— 原创只给方法、
#: 角色聊天只给单个角色，都拿不到「这本书的故事」。
ANCHORED_PATH: ContextProfile = "continuation"


def resolve_profile(project, profile: ContextProfile | str | None) -> ContextProfile:
    """把「这次生成走哪条创作路径」解析成一条**确定**的路径。

    ## 为什么需要这个函数

    路径（original / continuation / roleplay）是**请求级**参数，
    而「这段故事是不是锚定在某本参考书上」（`story_authority`）是**项目级**事实。
    以前两者各说各的：项目里明明记着「锚定在某本书上、故事读到第 5 章」，
    生成时若没显式传 `profile=continuation`，引擎就按原创路径跑 ——
    参考书原文被整本挡掉，模型拿到的是「自己的故事」，而不是「接在那本书后面」。

    实测症状：界面上把「故事读到第几章」填好、边界也存进去了，
    生成时却一点原文都没进去（检索上下文与风格样例都是空）。

    ## 规则

    - 请求里**显式写了**路径 → 尊重显式值（调用方最清楚自己要什么）。
    - 没写 → 按项目的锚定方式推导：`reference_anchor` → 续写路径；
      否则 → 原创路径（可见范围最小的那条：出错最多是「参考资料变少」，
      不会把参考书的世界观悄悄灌进用户的小说）。

    ## 幂等

    解析出来的路径再传进来，还是同一个值 —— 所以路由层与引擎层各自调一次是安全的。
    """
    if profile is not None and str(profile).strip():
        return normalize_profile(profile)
    authority = str(getattr(project, "story_authority", "") or "").strip()
    if authority == "reference_anchor":
        return ANCHORED_PATH
    return DEFAULT_PROFILE
