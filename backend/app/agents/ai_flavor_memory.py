"""AI 腔记忆的持久化读写（AI 腔反馈闭环的最底层基座）。

## 这块在闭环里的位置

助理 agent（Assistant）是主 agent：它调度 Writer（`app.generation.engine`）
生成文本；生成完毕后**由 Assistant 检查**产出、把本次 AI 腔问题总结成一句
人话，经本模块的 `upsert_memory()` 落库；下次生成前，编排层经
`load_memories()` 取回记忆、由 `render_memory_block()` 渲染成提示词块，
注入给 Writer 的**输入**（微调对象是下次的输入，不是重写本次文本）。
检查者与生成者由此分离——Writer 自检自改是本末倒置。

## 四条设计主线（为什么代码长这样）

1. **去重键 = (scope, project_id, issue_key)**。同一个问题模式在多少段里
   反复出现，都只累加一行 `hit_count`：表长由「问题种类」决定，而不是由
   「写作轮数」决定。没有这条，一个 300 段的长篇能把表写爆，提示词也会被
   同一句话复读撑爆。因此 `upsert_memory()` 是幂等的读-改-写：查得到就
   累加，查不到才插新行。

2. **有界膨胀，写入端与读取端同时设防**。只堵一端堵不住：写入端漏进一条
   长文，或读取端出现一个新调用方忘了裁剪，提示词都会被撑爆。所以
   - 写入端：`examples` 最多 `MAX_EXAMPLES` 条（新的挤掉最旧的），单条
     snippet 截断到 `MAX_SNIPPET_LEN`，`summary` 截断到
     `MAX_SUMMARY_LEN`，同一处证据（segment_no+offset）不重复记；
   - 读取端：`load_memories()` 按 `limit`（条数）与 `char_budget`（字符
     预算，按**渲染后行长**精确计算）双重裁剪。

3. **软开关 + 合并策略**。`active=False` 停用但不删（历史要能查）；停用是
   显式的人工决策，`upsert_memory()` **不复活**已停用的行，只累加计数。
   `load_memories()` 合并 global + 本项目两个来源，同一 `issue_key` 项目行
   胜出（项目级教训比全局通用教训更具体），避免同一条问题在提示词里出现两次。

## 方案库：先匹配复用，后自助规划（第 4 条主线）

问题账本只回答「哪里犯了 AI 腔」，不回答「当时怎么修好的」——于是每次同
模式复发，微调方法都要重新想一遍：LLM 成本与输出不确定性随复发次数线性
增长，而这个问题本身早就解决过了。本模块因此从「账本」升级为「方案库」：

- `match_memories()` 按 `issue_key` 批量匹配已有记忆（项目行胜出全局行，
  与 `load_memories()` 同款合并口径），命中项带上 `solution` 系列字段；
- `plan_solution()` 为某个模式规划一条微调方法文案。**模板优先**：32 个
  已知 AI 腔模式全部落在 `_PATTERN_TYPES` 的类别模板内——零成本、确定性、
  可单测；只有冷门模式（模板覆盖之外）且 llm 可用时才调一次 LLM；LLM
  不可用/抛错/返回空一律降级到通用兜底模板，绝不向上抛异常；
- `resolve_tuning()` 是自助规划的入口：命中且有方案 → 原样**复用**；
  方案被连续复发标记为可能无效 → **重新规划**；命中但没方案（老行/首见）
  → **规划并补上**；未命中 → **规划并 upsert 新行**。一次性 commit，
  失败时 rollback 并返回空结果，绝不影响调用方；
- 有效性反馈：方案被应用过（`times_reused >= 1`）后，同模式每复发一次
  `recurrences += 1`，达到 `INEFFECTIVE_RECURRENCES` 即打上
  `flagged_ineffective`——不是删除，是「下次别盲目复用，改为重新规划」。
  `load_memories(exclude_flagged=True)` 可把这些行从提示词渲染里滤掉。

## 事务边界

写函数**不 commit**：与 `prompt_overrides.set_active_override()` 同款约定，
由调用方（Assistant 的检查落库流程）持有事务边界，记忆写入与其它审计写入
共用同一个事务，一半失败整体回滚。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("agent.ai_flavor_memory")

# ====================================================================
# 常量（有界膨胀的全部数值集中在这里，调参只改这一处）
# ====================================================================

#: 合法 scope：global = 跨项目通用教训；project = 本项目私有教训。
SCOPES: tuple[str, ...] = ("global", "project")

#: 严重度排序权重（越小越靠前）。未知严重度排最后（`len(SEVERITY_RANK)`）。
SEVERITY_RANK: dict[str, int] = {"high": 0, "mid": 1, "low": 2}

#: 默认严重度：调用方不表态时按 mid 处理（不高估不低估）。
DEFAULT_SEVERITY: str = "mid"

#: 单条记忆最多保留的例句数。3 条的理由：证据的作用是「让人一眼看懂这条
#: 记忆指的是什么」，3 条足以覆盖不同出现位置；更多只是复读同一种模式，
#: 却会把每条记忆的渲染长度推高、挤占其它记忆的字符预算。
MAX_EXAMPLES: int = 3

#: 单条例句 snippet 的字符上限。例句是「证据」不是「原文归档」，200 字足够
#: 定位问题；防的是某次检测把整段正文塞进 examples 把库撑爆。
MAX_SNIPPET_LEN: int = 200

#: 单条记忆 summary 的字符上限（一句人话总结，不是原文归档）。取 200 的
#: 理由与 MAX_SNIPPET_LEN 同族：summary 会被 `_render_line` 原样拼进提示词
#: 覆写（`- 【severity】{summary}（累计 N 次；例：「…」）`），12 条上限下
#: 每条 200 字仍稳在读取端字符预算（DEFAULT_CHAR_BUDGET=2000）与
#: `prompt_overrides.MAX_OVERRIDE_LEN`（4000）之内。超长**截断**而非报错：
#: 报错会让闭环整体回滚、该项目自动微调永久静默罢工（QA F2）；截断只损失
#: 一句总结的尾巴，不影响记账连续性。
MAX_SUMMARY_LEN: int = 200

#: issue_key 长度上限，与模型列宽（String(128)）一致。超长直接报错而非
#: 截断——截断可能把两个不同的 key 截成同一个， silently 合并两种问题。
MAX_ISSUE_KEY_LEN: int = 128

#: load_memories 默认条数上限。12 条的依据：提示词块按每条约 60-80 字估算，
#: 12 条约 1000 字上下，与默认字符预算匹配，属于「提醒但不喧宾夺主」。
DEFAULT_LIMIT: int = 12

#: load_memories 默认字符预算（按渲染后文本计）。2000 字的量级参照：
#: 提示词覆写单块上限为 4000 字（prompt_overrides.MAX_OVERRIDE_LEN），
#: 记忆块作为附加说明不应占到一半。
DEFAULT_CHAR_BUDGET: int = 2000

#: 方案「可能无效」的复发阈值：方案被应用过之后，同一 issue_key 再复发达到
#: 这么多次，就标记 flagged_ineffective。取 3 的理由：单次复发可能是这段
#: 文本的巧合（同一个场景确实需要那个细节），连续三次复发才构成「这个修法
#: 没压住这个模式」的稳定信号；阈值取太小会让方案被偶发复发频繁误杀、
#: 白白重规划（LLM 成本 + 方案漂移），取太大则无效方案会继续污染提示词。
INEFFECTIVE_RECURRENCES: int = 3

#: 单条 solution 文案的长度上限（渲染时兜底截断，与 MAX_SUMMARY_LEN 同族）。
#: 模板渲染出的文案天然在这个量级；LLM 现规划的文案理论上无界，截断防的是
#: 一条啰嗦文案在渲染预算里挤掉其它记忆（QA F2 同款防御）。
MAX_SOLUTION_LEN: int = 200

#: LLM 规划时 user 提示里最多带的例句条数。2 条的理由：例句的作用是让模型
#: 看清「这个模式在本文具体长什么样」，2 条足够给出语感；更多只会拉长提示、
#: 推高延迟，对方案质量没有可感知的贡献。
MAX_PLAN_EXAMPLES: int = 2

#: LLM 规划时单条例句 snippet 的截断长度。例句只是「给模型看一眼」的样本，
#: 不是归档，80 字足够定位用法。
MAX_PLAN_SNIPPET_LEN: int = 80

#: 单模式严重度分档线（按「同一模式在本段的命中次数」）。与闭环侧
#: （`app.routers.assistant_generation._SEVERITY_HIGH_HITS` /
#: `_SEVERITY_MID_HITS`）**同一口径**：单段约 3000 字里同一模式复读 4 次以上
#: 属「明显破坏文风」；2-3 次是偶发的表述惯性；仅 1 次记档备查。
#: 分档线随「解析调优」改造从闭环移入本模块——严重度是记忆行/方案库的属性，
#: 由持有方案库的那一侧定档；两处各写一套分档线必漂移（本项目历史教训）。
_SEVERITY_HIGH_HITS: int = 4
_SEVERITY_MID_HITS: int = 2


# ====================================================================
# 方案库：模式分类 + 模板（plan_solution 的零成本路径）
# ====================================================================

#: 模式（issue_key）→ 问题类型。**必须覆盖**
#: `app.generation.engine.AI_FLAVOR_PATTERNS` 的全部 32 个模式——由
#: tests/test_ai_flavor_memory.py 交叉核对（import 引擎的清单做集合相等
#: 断言），漏一个模式那个测试就红。为什么不在这里 import 引擎清单做自检：
#: app.generation.engine 的导入链很重（retrieval / style / kg），记忆模块
#: 被 assistant_generation 路由在启动时导入，把它拖进来会拖慢启动并制造
#: 循环导入风险；核对放在测试层，成本与收益都更合理。
#:
#: 分类依据（同一「病根」的模式归一类，微调方法因此可以共享）：
#: - vague_simile：空泛类比。像是/似乎 还常披「不确定性」外衣，但本质仍是
#:   拿一个不在场的画面糊弄过去，与 仿佛/宛如/犹如 同病；
#: - emotion_cliche：套路化表情动作。写情绪不进入角色，共用一张表情包；
#: - mind_read：替人物下结论的内心独白判词（他知道，/她明白，）；
#: - verdict_cliche：评论腔与总结腔。作者跳出来评判或总结，而不是叙事；
#: - transition_cliche：套话转折与递进（就在这时/突然之间/不仅如此）；
#: - filler_adverb：无信息量的口头禅式副词（不禁/忍不住/更是/原来/终于/
#:   从此/意味着），删之不可惜（不可惜才有资格进这一类）。
_PATTERN_TYPES: dict[str, str] = {
    # vague_simile：空泛类比（5）
    "仿佛": "vague_simile",
    "宛如": "vague_simile",
    "犹如": "vague_simile",
    "像是": "vague_simile",
    "似乎": "vague_simile",
    # emotion_cliche：套路化表情动作（5）
    "心中充满了": "emotion_cliche",
    "眼中闪过一丝": "emotion_cliche",
    "嘴角勾起": "emotion_cliche",
    "嘴角微勾": "emotion_cliche",
    "深吸一口气": "emotion_cliche",
    # mind_read：替人物下结论（2）
    "他知道，": "mind_read",
    "她明白，": "mind_read",
    # verdict_cliche：评论腔 / 总结腔（10）
    "显然": "verdict_cliche",
    "毫无疑问": "verdict_cliche",
    "毫无疑问地": "verdict_cliche",
    "毋庸置疑": "verdict_cliche",
    "众所周知": "verdict_cliche",
    "总的来说": "verdict_cliche",
    "综上所述": "verdict_cliche",
    "值得一提的是": "verdict_cliche",
    "不难发现": "verdict_cliche",
    "由此可见": "verdict_cliche",
    # transition_cliche：套话转折与递进（3）
    "就在这时": "transition_cliche",
    "突然之间": "transition_cliche",
    "不仅如此": "transition_cliche",
    # filler_adverb：无信息量的口头禅式副词（7）
    "不禁": "filler_adverb",
    "忍不住": "filler_adverb",
    "更是": "filler_adverb",
    "原来": "filler_adverb",
    "终于": "filler_adverb",
    "从此": "filler_adverb",
    "意味着": "filler_adverb",
}

#: 问题类型 → 微调方法模板。模板必须含 {pattern} 与 {count}：Writer 要知道
#: 「具体躲什么」和「本文已经犯过几次」，只给类别名（如「别写套路化表情」）
#: 是正确但无用的话——落不到笔尖。文案方向 = 先指出病根、再给可执行替代。
_CATEGORY_TEMPLATES: dict[str, str] = {
    "vague_simile": (
        "「{pattern}」是空泛类比：删掉它，改写成只有这个场景能看见/听见/摸到的"
        "具体感官细节；事实本身够有力时直接写事实，不借比喻。（本文已命中 {count} 次）"
    ),
    "emotion_cliche": (
        "「{pattern}」是套路化表情动作：删掉它，换成只属于这个角色的身体反应——"
        "他在这个局面下会怎么呼吸、怎么动手，而不是共用一张表情包。"
        "（本文已命中 {count} 次）"
    ),
    "mind_read": (
        "「{pattern}」是替人物下结论：删掉这句判断，用动作、对话或后果让人物"
        "自己显露心思，读者自己会得出结论。（本文已命中 {count} 次）"
    ),
    "verdict_cliche": (
        "「{pattern}」是评论腔/总结腔：删掉这句评判，让事件自己说话；需要衔接时"
        "用事实过渡，不预告「接下来要转折了」。（本文已命中 {count} 次）"
    ),
    "transition_cliche": (
        "「{pattern}」是套话转折：删掉它，直接切入事件本身，用动作或场景切换完成"
        "过渡。（本文已命中 {count} 次）"
    ),
    "filler_adverb": (
        "「{pattern}」是无信息量的口头禅式副词：能删则删；删掉后句子力度不够，"
        "说明那里缺的是具体细节而不是副词。（本文已命中 {count} 次）"
    ),
}

#: 通用兜底模板：模板未覆盖的冷门模式 + LLM 不可用/失败时的降级文案。
#: 与类别模板同款约束：含 {pattern} 与 {count}，给可执行的第一步（先删掉）。
_FALLBACK_TEMPLATE: str = (
    "「{pattern}」被本文判定为 AI 腔问题（累计 {count} 次）：写这段时避开这个"
    "模式，先用更平实具体的写法替换；不清楚怎么改时，先删掉它再补事实细节。"
)

#: LLM 规划用的 system 提示：给模型一个收窄的人设，输出才会是「方法」而不是
#: 又一篇 AI 腔范文。
_PLAN_SYSTEM_PROMPT: str = (
    "你是资深中文小说编辑，专门帮作者消除 AI 腔。你的回答只给一条具体可执行的"
    "微调方法，一句话到两句话，告诉 Writer 下次写时怎么躲开这个模式。"
)


# ====================================================================
# 输入归一化 / 校验（全部集中在瘦函数里，upsert 主干因此保持可读）
# ====================================================================


def _validate_scope(scope: str) -> str:
    """归一并校验 scope；非法值抛 ValueError（让调用方尽早发现口径错误）。"""
    s = (scope or "").strip().lower()
    if s not in SCOPES:
        raise ValueError(f"scope={scope!r} 不合法，须为 {list(SCOPES)} 之一")
    return s


def _validate_project_id(scope: str, project_id: int | None) -> int | None:
    """global 行强制 project_id=None；project 行强制带 project_id。

    为什么强制：global 行挂上 project_id 会让「全局记忆」悄悄变成某个项目
    的私有记忆（别的项目读不到，本项目还以为是普适教训）；project 行缺
    project_id 则会在读取时因 `IS NULL` 匹配到 global 行，造成串读。
    """
    if scope == "global":
        return None
    if project_id is None:
        raise ValueError("scope='project' 必须提供 project_id")
    return int(project_id)


def _validate_issue_key(issue_key: str) -> str:
    """去重键的非空校验 + 长度硬闸（见 MAX_ISSUE_KEY_LEN 注释）。"""
    key = (issue_key or "").strip()
    if not key:
        raise ValueError("issue_key 不能为空（它是去重键）")
    if len(key) > MAX_ISSUE_KEY_LEN:
        raise ValueError(
            f"issue_key 超长（{len(key)} > {MAX_ISSUE_KEY_LEN}），"
            "请先由检测侧归一成短模式名"
        )
    return key


def _normalize_severity(severity: str) -> str:
    """严重度归一为小写；空值回落默认档。未知值原样保留（排序时排最后）。"""
    s = (severity or "").strip().lower()
    return s or DEFAULT_SEVERITY


def _normalize_summary(summary: str, *, issue_key: str = "") -> str:
    """一句人话总结：去空白；超长按 MAX_SUMMARY_LEN 截断并记日志。

    为什么截断而不是报错：summary 会被 `_render_line` 原样拼进提示词覆写，
    一条超长脏行就能把覆写顶出 `prompt_overrides.MAX_OVERRIDE_LEN` →
    `set_active_override` 抛错 → 闭环整体回滚 → 该项目自动微调永久静默
    罢工（QA F2）。截断只损失总结尾巴，记账与微调都保住。
    """
    s = (summary or "").strip()
    if len(s) > MAX_SUMMARY_LEN:
        logger.warning(
            "AI 腔记忆 summary 超长，已截断到 %d 字"
            "（issue_key=%r，原长 %d）",
            MAX_SUMMARY_LEN, issue_key, len(s),
        )
        s = s[:MAX_SUMMARY_LEN]
    return s


def _normalize_examples(examples: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把调用方传入的例句归一成 `[{segment_no, offset, snippet}]`。

    - snippet 截断到 MAX_SNIPPET_LEN（防长文撑库）；
    - 同一 (segment_no, offset) 只保留**最后**出现的一条（重跑检测时后写的
      信息更新，且不让同一处证据在列表里复读）；
    - 字段缺失给保守默认值，绝不因一条脏数据整批失败。
    """
    normalized: list[dict[str, Any]] = []
    for raw in examples or []:
        if not isinstance(raw, dict):
            continue
        seg = raw.get("segment_no")
        off = raw.get("offset")
        snippet = str(raw.get("snippet") or "")[:MAX_SNIPPET_LEN]
        normalized.append(
            {"segment_no": seg, "offset": off, "snippet": snippet}
        )
    # 去重保序：后面的挤掉前面的同一处证据
    deduped: list[dict[str, Any]] = []
    taken: set[tuple[Any, Any]] = set()
    for item in reversed(normalized):
        k = (item["segment_no"], item["offset"])
        if k in taken:
            continue
        taken.add(k)
        deduped.append(item)
    deduped.reverse()
    return deduped


def _merge_examples(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并旧证据与新证据并压回 MAX_EXAMPLES 条上限（新的挤掉最旧的）。

    已存在的 (segment_no, offset) 不重复追加——同一处文本再次命中只是
    hit_count 的事，不该在证据列表里复读。
    """
    merged = list(existing or [])
    known = {(e.get("segment_no"), e.get("offset")) for e in merged}
    for item in incoming:
        k = (item.get("segment_no"), item.get("offset"))
        if k in known:
            continue
        merged.append(item)
        known.add(k)
    return merged[-MAX_EXAMPLES:]


# ====================================================================
# 写
# ====================================================================


def upsert_memory(
    db: Any,
    *,
    scope: str,
    project_id: int | None,
    issue_key: str,
    summary: str = "",
    hits_delta: int = 1,
    examples: list[dict[str, Any]] | None = None,
    segment_no: int | None = None,
    severity: str = DEFAULT_SEVERITY,
) -> Any:
    """幂等累加一条 AI 腔记忆；同一去重键**不产生重复行**。

    Args:
        db: SQLAlchemy Session（调用方持有事务，本函数不 commit）。
        scope: "global" | "project"。
        project_id: scope=project 时必填；scope=global 时强制忽略为 NULL。
        issue_key: 命中的 AI 腔模式 / 归一化问题类型，如 "仿佛"。
        summary: 一句人话总结。空串表示「只累加命中，不更新描述」——
            允许调用方在补刀命中时只报数。超长按 MAX_SUMMARY_LEN 截断
            （不报错：报错会令闭环回滚、自动微调静默罢工，见常量注释）。
        hits_delta: 本次命中次数，须 >= 0（0 = 仅刷新证据/描述）。
        examples: 本次例句 [{segment_no, offset, snippet}]。
        segment_no: 最近一次出现在哪段；None 表示不刷新该字段。
        severity: high | mid | low（未知值排序时排最后）。

    Returns:
        落库（已 flush，未 commit）的 `AIFlavorMemory` 行。

    Raises:
        ValueError: scope / project_id / issue_key / hits_delta 非法。

    为什么不复活已停用的行：`active=False` 是显式的人工停用决策，
    upsert 是自动累加通道，两者口径不同——自动通道悄悄改人工开关
    会让「停用」这个功能失效。停用的行照常累加 hit_count（历史连续），
    但不出现在 `load_memories()` 里；复活留给显式操作。
    """
    from sqlalchemy import select

    from app.models import AIFlavorMemory

    scope_n = _validate_scope(scope)
    pid = _validate_project_id(scope_n, project_id)
    key = _validate_issue_key(issue_key)
    if not isinstance(hits_delta, int) or hits_delta < 0:
        raise ValueError(f"hits_delta 须为非负整数，收到 {hits_delta!r}")
    sev = _normalize_severity(severity)
    summary_n = _normalize_summary(summary, issue_key=key)
    new_examples = _normalize_examples(examples)

    # 查重：global 行用 IS NULL 匹配（SQLAlchemy 的 == None 也会转 IS NULL，
    # 这里显式写 is_ 让「NULL 是去重键的一部分」这件事在代码里可见）。
    stmt = select(AIFlavorMemory).where(
        AIFlavorMemory.scope == scope_n,
        AIFlavorMemory.issue_key == key,
    )
    stmt = (
        stmt.where(AIFlavorMemory.project_id.is_(None))
        if pid is None
        else stmt.where(AIFlavorMemory.project_id == pid)
    )
    row = db.scalars(stmt.order_by(AIFlavorMemory.id.desc())).first()

    if row is None:
        row = AIFlavorMemory(
            scope=scope_n,
            project_id=pid,
            issue_key=key,
            summary=summary_n,
            hit_count=hits_delta,
            examples=_merge_examples(None, new_examples),
            last_segment_no=segment_no,
            severity=sev,
            active=True,
        )
        db.add(row)
    else:
        row.hit_count = (row.hit_count or 0) + hits_delta
        if summary_n:
            row.summary = summary_n
        row.severity = sev
        if segment_no is not None:
            # 直接刷新为「本次报告的段号」：段号由调用方按写作进度给出，
            # 续写/重生成可能回访更早的段，「只前进不后退」会记错最近出现位置。
            row.last_segment_no = segment_no
        row.examples = _merge_examples(row.examples, new_examples)
    db.flush()
    return row


# ====================================================================
# 读
# ====================================================================


def _severity_rank(severity: str) -> int:
    """严重度 → 排序权重；未知严重度排最后（见 SEVERITY_RANK 注释）。"""
    return SEVERITY_RANK.get((severity or "").lower(), len(SEVERITY_RANK))


def _recency_ts(dt: datetime | None) -> float:
    """updated_at → 排序用的时间戳；None 回落 0.0（排最后）。"""
    return dt.timestamp() if dt is not None else 0.0


def _sort_key(item: dict[str, Any]) -> tuple[int, int, int, float]:
    """排序键：严重度升序 → hit_count 降序 → last_segment_no 降序 → updated_at 降序。

    为什么严重度优先于 hit_count：high 档是「明显破坏文风」的问题
    （如高频机械比喻），mid 档多是润色级提醒。提示词预算有限时，先保证
    真问题在场。hit_count 是同一档内的主排序——反复出现说明 Writer 在
    系统性复读它，最该被提醒。
    """
    return (
        _severity_rank(item.get("severity", "")),
        -int(item.get("hit_count") or 0),
        -int(item.get("last_segment_no") or 0),
        -_recency_ts(item.get("updated_at")),
    )


def _row_to_item(row: Any) -> dict[str, Any]:
    """ORM 行 → 可直接渲染的字典（渲染函数只认这个结构，不认 ORM 对象）。

    除问题账本字段外，还带 solution 系列六个字段——`match_memories()` 的
    命中项与 `resolve_tuning()` 的三个返回列表都用这个形状，下游据此判断
    「能不能复用当年的方案」。老行（迁移前创建）这些列有服务端默认值，
    这里再 `or` 一层是双保险：NULL 行也能给出保守默认而不是把 None 泄给
    调用方。
    """
    return {
        "scope": row.scope,
        "project_id": row.project_id,
        "issue_key": row.issue_key,
        "summary": row.summary or "",
        "hit_count": int(row.hit_count or 0),
        "severity": (row.severity or "").lower(),
        "examples": list(row.examples or []),
        "last_segment_no": row.last_segment_no,
        "updated_at": row.updated_at,
        "solution": row.solution or "",
        "solution_origin": row.solution_origin or "",
        "times_reused": int(row.times_reused or 0),
        "last_reused_at": row.last_reused_at,
        "recurrences": int(row.recurrences or 0),
        "flagged_ineffective": bool(row.flagged_ineffective),
    }


def _render_line(item: dict[str, Any]) -> str:
    """单条记忆渲染成一行提示词文本（预算按这一行的实际长度计算）。"""
    sev = item.get("severity") or DEFAULT_SEVERITY
    # 正文优先用 solution：那才是「可执行的约束」，summary 只是「问题描述」。
    # 没有 solution 的老行回落到 summary，再回落到 issue_key——升级前的库
    # 与新建但未规划的行都不该渲染成空行。
    body = str(item.get("solution") or "").strip()
    if not body:
        body = str(item.get("summary") or item.get("issue_key") or "")
    # body 必须在此处再截一次：`upsert_memory` 入口的 MAX_SUMMARY_LEN 只管
    # 「未来写入」，管不到历史上被直接写库（绕过 upsert）的超长行；而
    # `load_memories` 的字符预算是**行间累计**，管不到单行内部——不截的话，
    # 一条 5000 字脏行能吃掉整个预算，把同一批里真正该提醒的教训全挤掉
    # （QA F2 的残留面：防住了「静默罢工」，没防住「一条脏行顶掉全部教训」）。
    # solution 同理会超长（LLM 现规划的文案无界），走同一条截断防线。
    if len(body) > MAX_SOLUTION_LEN:
        body = body[:MAX_SOLUTION_LEN] + "…"
    head = f"- 【{sev}】{body}"
    tail = f"（累计 {int(item.get('hit_count') or 0)} 次"
    examples = item.get("examples") or []
    if examples:
        snippet = str(examples[-1].get("snippet") or "").strip()
        if snippet:
            tail += f"；例：「{snippet}」"
    tail += "）"
    return head + tail


def render_memory_block(items: list[dict[str, Any]]) -> str:
    """把 `load_memories()` 的结果渲染成提示词块文本；空列表返回空串。

    只追加不解释：调用方把返回文本拼在给 Writer 的输入之后即可，
    列表为空时返回 ""——与 prompt_overrides 的「空覆写等价原行为」同款
    兼容底线：没有记忆时注入链路逐字节等于没有这块。
    """
    if not items:
        return ""
    return "\n".join(_render_line(it) for it in items)


def load_memories(
    db: Any,
    project_id: int | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
    char_budget: int = DEFAULT_CHAR_BUDGET,
    exclude_flagged: bool = False,
) -> list[dict[str, Any]]:
    """取回注入提示词用的记忆：合并 global + 本项目，去重、排序、裁剪。

    Args:
        db: SQLAlchemy Session。
        project_id: 当前项目 id；None 表示只取 global（无项目上下文时）。
        limit: 条数上限（须 >= 1）。
        char_budget: 字符预算，按**渲染后文本**的累计长度计算（须 >= 0）。
        exclude_flagged: 为 True 时滤掉 `flagged_ineffective`（方案可能无效）
            的行。闭环渲染用：一条被连续复发打标的方案如果继续进提示词，等于
            把「已知可能无效」的建议继续喂给 Writer——既污染提示词，又让
            「无效」信号永远静默。注意它只从**渲染结果**里滤掉，历史行照常
            在表里可查、可被 resolve_tuning 重新规划。

    Returns:
        可直接 `render_memory_block()` 的字典列表，元素结构见 `_row_to_item`。

    设计要点：

    - **合并与去重**：global 行与 project 行都可能是同一个 issue_key 的
      教训（先在别的项目踩过坑，本项目又踩）。同一 key 项目行胜出——
      项目级总结更具体；全局行被跳过，避免同一条问题在提示词里出现两遍。
    - **排序**：见 `_sort_key`（严重度 > 次数 > 最近性）。
    - **双重裁剪**：先按 `limit` 截条数，再按 `char_budget` 截字符。
      预算按渲染后行长精确计算，而不是按 summary 长度估算——估算会在
      「summary 短但例句长」时失准，提示词还是会被撑爆。
    - **首条必收**：若预算连第一条都装不下，仍然返回第一条。一个记忆都
      渲染不出来说明预算/数据口径出了问题，此时返回空块会把故障静默成
      「没有记忆」；收首条至少让调用方看到数据存在、去修预算。
    """
    from sqlalchemy import select

    from app.models import AIFlavorMemory

    if not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit 须为 >= 1 的整数，收到 {limit!r}")
    if not isinstance(char_budget, int) or char_budget < 0:
        raise ValueError(f"char_budget 须为非负整数，收到 {char_budget!r}")

    stmt = select(AIFlavorMemory).where(AIFlavorMemory.active.is_(True))
    if exclude_flagged:
        # IS 0 而非 = 0：SQLite 里 NULL 行不满足 = 0，会连「从没复发过」的
        # 老行一起误滤掉（迁移前的行本列有服务端默认值 0，但双保险走 IS）。
        stmt = stmt.where(AIFlavorMemory.flagged_ineffective.is_(False))
    if project_id is None:
        stmt = stmt.where(AIFlavorMemory.scope == "global")
    else:
        stmt = stmt.where(
            (AIFlavorMemory.scope == "global")
            | (
                (AIFlavorMemory.scope == "project")
                & (AIFlavorMemory.project_id == int(project_id))
            )
        )
    rows = list(db.scalars(stmt).all())

    # 项目行优先去重（字典保序：先放项目行，global 行同 key 即被跳过）
    project_rows = [r for r in rows if r.scope == "project"]
    global_rows = [r for r in rows if r.scope == "global"]
    merged: dict[str, dict[str, Any]] = {}
    for row in project_rows + global_rows:
        merged.setdefault(row.issue_key, _row_to_item(row))

    items = sorted(merged.values(), key=_sort_key)

    # 双重裁剪：条数 + 字符预算（预算按渲染后行长计）
    trimmed: list[dict[str, Any]] = []
    used = 0
    for item in items:
        if len(trimmed) >= limit:
            break
        cost = len(_render_line(item))
        if trimmed and used + cost > char_budget:
            continue  # 跳过装不下的，给后面更短的内存行机会
        trimmed.append(item)
        used += cost
    return trimmed


# ====================================================================
# 方案库：规划（plan_solution）+ 匹配复用（match_memories /
# resolve_tuning / note_recurrence）
# ====================================================================


def _extract_llm_text(raw: Any) -> str:
    """从 llm.chat 的返回值里取文本：约定是 str，防御性兼容带 .content 的对象。

    为什么防御：provider 接口是 `chat(system, user) -> str`，但 harness 传入
    的可能是包装对象（带 .content 的响应体）。取不到文本就返回空串，由调用方
    走兜底——绝不在这里抛异常。
    """
    if isinstance(raw, str):
        return raw.strip()
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content.strip()
    if raw is None:
        return ""
    return str(raw).strip()


def _build_plan_prompt(issue_key: str, count: int, examples: list | None) -> str:
    """拼 LLM 规划的 user 提示：模式名 + 命中次数 + 前两条例句 snippet。

    例句只截取前 MAX_PLAN_EXAMPLES 条、每条 MAX_PLAN_SNIPPET_LEN 字——它们
    的用途是让模型看清「这个模式在本文具体长什么样」，不是归档。
    """
    lines = [
        f"AI 腔模式：{issue_key}",
        f"本文命中次数：{count}",
    ]
    snippets: list[str] = []
    for raw in examples or []:
        if not isinstance(raw, dict):
            continue
        snippet = str(raw.get("snippet") or "").strip()
        if snippet:
            snippets.append(snippet[:MAX_PLAN_SNIPPET_LEN])
        if len(snippets) >= MAX_PLAN_EXAMPLES:
            break
    if snippets:
        lines.append("例句：")
        lines.extend(f"- {s}" for s in snippets)
    else:
        lines.append("例句：（暂无）")
    lines.append(
        "请给出一条具体可执行的微调方法，告诉 Writer 下次写时怎么躲开这个模式。"
        "只回方法本身，不要解释、不要举例。"
    )
    return "\n".join(lines)


def plan_solution(
    issue_key: str,
    *,
    count: int = 0,
    examples: list | None = None,
    llm: Any = None,
) -> tuple[str, str]:
    """为某个 AI 腔模式规划一条微调方法文案。返回 (solution_text, origin)。

    Args:
        issue_key: 命中的 AI 腔模式 / 归一化问题类型，如 "仿佛"。
        count: 本文命中次数，进模板的 {count} 占位（让 Writer 知道严重程度）。
        examples: 例句 [{segment_no, offset, snippet}]，LLM 路径取前两条。
        llm: 可选 LLM（约定 `chat(system, user) -> str`）。None = 零成本路径。

    Returns:
        (solution_text, origin)。origin ∈ {"template", "assistant_llm",
        "fallback"}：template = 类别模板渲染（零成本、确定性、可单测）；
        assistant_llm = 冷门模式现调 LLM 规划；fallback = 通用兜底。

    设计决策：**模板优先**。32 个已知模式全部有类别模板——渲染是纯函数，
    零网络、零成本、输出确定，单测可以逐字断言；只有模板覆盖之外的冷门模式
    才值得付一次 LLM 调用。LLM 不可用 / 调用抛错 / 返回空一律降级兜底：
    规划失败不该让整个闭环陪葬（QA F2 同款思路——静默罢工比一句兜底文案
    糟糕得多）。**本函数绝不抛异常**，任何一步失败都落到 fallback。
    """
    key = (issue_key or "").strip()
    n = count if isinstance(count, int) and count >= 0 else 0

    # 1) 模板优先：已知模式 → 类别模板渲染
    ptype = _PATTERN_TYPES.get(key)
    if ptype is not None:
        template = _CATEGORY_TEMPLATES.get(ptype, _FALLBACK_TEMPLATE)
        return template.format(pattern=key, count=n), "template"

    # 2) 冷门模式 + llm 可用 → 现调一次
    #
    # 可用性探测与调用必须同在 try 里：真实消费方（闭环的 _LazyLLMHandle）
    # 在 provider 取不到时**故意抛 RuntimeError 而不是 AttributeError**——
    # 它要的就是「LLM 不可用」这个事实被接住并降级，而不是被 hasattr 静默
    # 吞成「没有 chat」。getattr(x, "chat", None) 只吞 AttributeError，
    # 所以这里整段兜底：取不到 / 取到了调炸 / 返回空，全部落 fallback。
    try:
        chat = getattr(llm, "chat", None) if llm is not None else None
        if callable(chat):
            raw = chat(_PLAN_SYSTEM_PROMPT, _build_plan_prompt(key, n, examples))
            text = _extract_llm_text(raw)
            if text:
                return text, "assistant_llm"
            logger.warning(
                "plan_solution：LLM 返回空文本，降级兜底模板（issue_key=%r）", key
            )
    except Exception:
        # 不抛：LLM 渠道抖动 / provider 不可用都不该让闭环回滚；记异常栈后
        # 走兜底。
        logger.exception(
            "plan_solution：LLM 不可用或调用失败，降级兜底模板（issue_key=%r）", key
        )

    # 3) 兜底：llm=None / 不可用 / 失败 / 返回空
    return _FALLBACK_TEMPLATE.format(pattern=key, count=n), "fallback"


def _normalize_keys(issue_keys: Any) -> list[str]:
    """归一 issue_key 列表：转字符串、去空白、去重、保序，丢空值。"""
    keys: list[str] = []
    seen: set[str] = set()
    for raw in issue_keys or []:
        k = raw.strip() if isinstance(raw, str) else ""
        if not k or k in seen:
            continue
        seen.add(k)
        keys.append(k)
    return keys


def _match_rows(
    db: Any,
    project_id: int | None,
    issue_keys: Any,
    *,
    include_global: bool = True,
) -> dict[str, Any]:
    """按 issue_key 批量取「胜出行」：项目行胜出全局行，同 scope 取 id 最大。

    与 `load_memories()` 同款合并口径（项目级教训更具体），但这里返回 ORM 行
    而不是 item——`resolve_tuning()` 要在行上原地改 solution 系列字段。

    只匹配 `active=True` 的行：active=False 是显式的人工停用决策，复用它的
    方案等于悄悄复活人工开关（与 upsert 不复活停用行同款口径）。
    """
    from sqlalchemy import or_, select

    from app.models import AIFlavorMemory

    keys = _normalize_keys(issue_keys)
    if not keys:
        return {}

    stmt = select(AIFlavorMemory).where(
        AIFlavorMemory.active.is_(True),
        AIFlavorMemory.issue_key.in_(keys),
    )
    if project_id is None:
        # 无项目上下文：只可能有全局行
        stmt = stmt.where(AIFlavorMemory.scope == "global")
    else:
        pid = int(project_id)
        conds = [
            (AIFlavorMemory.scope == "project")
            & (AIFlavorMemory.project_id == pid)
        ]
        if include_global:
            conds.append(AIFlavorMemory.scope == "global")
        stmt = stmt.where(or_(*conds))

    rows = list(db.scalars(stmt).all())
    # 竞争裁决：项目行胜出全局行；同 scope 取 id 最大（与 upsert_memory 的
    # order_by(id.desc()).first() 同款「新行胜出」口径——历史重复行里最新的
    # 那条才代表当前认知）。
    best: dict[str, Any] = {}
    for row in rows:
        cur = best.get(row.issue_key)
        if cur is None:
            best[row.issue_key] = row
        elif cur.scope != "project" and row.scope == "project":
            best[row.issue_key] = row
        elif cur.scope == row.scope and row.id > cur.id:
            best[row.issue_key] = row
    return best


def match_memories(
    db: Any,
    project_id: int | None,
    issue_keys: Any,
    *,
    include_global: bool = True,
) -> dict:
    """按 issue_key 批量匹配已有记忆，返回 {"matched": [item...], "unmatched": [key...]}。

    Args:
        db: SQLAlchemy Session。
        project_id: 当前项目 id；None 表示无项目上下文（只匹配全局行）。
        issue_keys: 待匹配的 issue_key 可迭代对象（自动去重保序、丢空值）。
        include_global: False 时只查项目行（调用方明确不要全局教训时）。

    Returns:
        matched 的元素是 `_row_to_item` 形状（带 solution / solution_origin /
        times_reused / last_reused_at / recurrences / flagged_ineffective 六个
        新字段）；同一 issue_key 项目行胜出全局行。两个列表都保持入参顺序
        （顺序抖动会让同样的输入得到不同顺序的输出，难测也难对账）。
        空 issue_keys 时返回 {"matched": [], "unmatched": []}。
    """
    ordered_keys = _normalize_keys(issue_keys)
    if not ordered_keys:
        return {"matched": [], "unmatched": []}
    rows = _match_rows(
        db, project_id, ordered_keys, include_global=include_global
    )
    return {
        "matched": [_row_to_item(rows[k]) for k in ordered_keys if k in rows],
        "unmatched": [k for k in ordered_keys if k not in rows],
    }


def _resolve_count(value: Any) -> int:
    """evidence 的 count 归一：非 int / 负值一律按 0（不报错，只保守处理）。"""
    return value if isinstance(value, int) and value >= 0 else 0


def _severity_for_count(count: int) -> str:
    """单模式本段命中次数 → 严重度档位（high | mid | low）。

    档位语义与 `SEVERITY_RANK` 对齐——high 在 load_memories 排序里最优先，
    提示词预算紧张时也必须先提醒「明显破坏文风」的问题。分档线见
    `_SEVERITY_HIGH_HITS` / `_SEVERITY_MID_HITS` 注释。
    """
    if count >= _SEVERITY_HIGH_HITS:
        return "high"
    if count >= _SEVERITY_MID_HITS:
        return "mid"
    return "low"


def _density_summary(issue_key: str, count: int) -> str:
    """问题账本的 summary：一句人话总结 + 本段密度（与旧闭环同款格式）。

    旧闭环（改造前的逐模式 upsert）写的是「{pattern} 式 AI 腔：本段命中
    {count} 次（全段共 {total} 次）」；evidence 契约只带单模式 count、不带
    全段 total，故少尾巴那一句——全段密度由闭环返回体的 summary 字段另行
    承载（`_summarize_flavor_hits`），不在这一行里重复。
    """
    return f"{issue_key} 式 AI 腔：本段命中 {count} 次"


def _evidence_segment_no(examples: list | None) -> int | None:
    """从 evidence 的例句里取段号（第一条带 int segment_no 的例句）。"""
    for raw in examples or []:
        if isinstance(raw, dict):
            seg = raw.get("segment_no")
            if isinstance(seg, int):
                return seg
    return None


def _refresh_ledger(row: Any, *, count: int, examples: list | None) -> None:
    """问题账本侧的每轮刷新：hit_count +1、severity 定档、summary 写密度、
    last_segment_no 与 examples 合并更新。

    为什么 resolve_tuning 要管账本：改造前闭环逐模式调 `upsert_memory()`
    记账——hit_count 记的是「多少次检查命中过它」，本段密度写进 severity 与
    summary，last_segment_no 刷新为最近出现段；改造后这条写入路径整体移入
    本函数（闭环不再直接 upsert），账本口径必须原样扛过来。否则一次「升级」
    会把排序信号悄悄改没：severity 全落默认 mid → load_memories 的优先级
    排序废掉；last_segment_no 停更 → 最近性排序废掉。口径与旧闭环逐字节
    一致（旧闭环测试即锁，见 tests/test_assistant_ai_flavor_feedback.py）。
    """
    row.hit_count = (row.hit_count or 0) + 1
    row.severity = _severity_for_count(count)
    summary = _density_summary(row.issue_key, count)
    if summary:
        row.summary = summary
    seg = _evidence_segment_no(examples)
    if seg is not None:
        # 直接刷新为「本次报告的段号」：与 upsert_memory 同款口径——段号由
        # 调用方按写作进度给出，续写/重生成可能回访更早的段。
        row.last_segment_no = seg
    row.examples = _merge_examples(row.examples, _normalize_examples(examples))


def resolve_tuning(
    db: Any,
    project_id: int | None,
    evidence: dict,
    *,
    llm: Any = None,
) -> dict:
    """AI 腔问题的「先匹配复用、后自助规划」入口。

    Args:
        db: SQLAlchemy Session（本函数自己管事务：一次性 commit）。
        project_id: 当前项目 id。未命中的新模式按 scope="project" upsert，
            因此本项目上下文必填（None 时建不了项目行，整体按失败处理）。
        evidence: {issue_key: {"count": int, "examples": [{"segment_no",
            "offset", "snippet"}]}}，一次检测的命中明细。
        llm: 可选 LLM，只传给 plan_solution（模板覆盖内的模式永远不调它）。

    Returns:
        {"reused": [item], "replanned": [item], "planned": [item]}：
        - reused：命中且有方案且未打标 → 原样复用（times_reused += 1）；
        - replanned：命中但方案 flagged_ineffective → 重新规划、计数重置；
        - planned：命中但没方案（老行/首见）或未命中 → 规划并落库。
        任何异常都 rollback + 记日志 + 返回三个空列表——闭环是增强路径，
        绝不能因为它把调用方（生成/检查主流程）带崩。

    分支决策（对每个 evidence 里的 issue_key）：
    0. 问题账本先记这一轮（hit_count +1、severity 按本段密度定档、summary
       写密度、last_segment_no / examples 刷新）——改造前闭环逐模式 upsert
       记账，改造后这条路径移入本函数，口径原样扛过来（见 _refresh_ledger）。
    1. 命中且该行 times_reused >= 1（方案此前被应用过）→ recurrences += 1；
       达到 INEFFECTIVE_RECURRENCES → flagged_ineffective = True。
    2. 有方案且未打标 → 复用；已打标 → 重新规划；没方案 → 规划并补上；
       未命中 → 规划并 upsert 新行（scope="project"）。
    """
    empty: dict[str, list[dict[str, Any]]] = {
        "reused": [],
        "replanned": [],
        "planned": [],
    }
    if not isinstance(evidence, dict):
        logger.warning("resolve_tuning：evidence 不是 dict，已忽略（%r）", type(evidence))
        return empty
    keys = _normalize_keys(list(evidence.keys()))
    if not keys:
        return empty

    try:
        rows = _match_rows(db, project_id, keys)
        now = datetime.now(timezone.utc)
        reused: list[dict[str, Any]] = []
        replanned: list[dict[str, Any]] = []
        planned: list[dict[str, Any]] = []

        for key in keys:
            ev = evidence.get(key)
            ev = ev if isinstance(ev, dict) else {}
            count = _resolve_count(ev.get("count"))
            examples = ev.get("examples")
            row = rows.get(key)

            if row is None:
                # 未命中 → 规划并 upsert 新行（scope="project"）。
                # 账本字段（summary 密度 / severity 定档 / segment_no /
                # examples）与旧闭环逐模式 upsert 同口径——见 _refresh_ledger
                # 的说明：resolve_tuning 是改造后闭环唯一的记账写入路径。
                solution, origin = plan_solution(
                    key, count=count, examples=examples, llm=llm
                )
                row = upsert_memory(
                    db,
                    scope="project",
                    project_id=project_id,
                    issue_key=key,
                    summary=_density_summary(key, count),
                    hits_delta=1,
                    examples=examples,
                    segment_no=_evidence_segment_no(examples),
                    severity=_severity_for_count(count),
                )
                row.solution = solution
                row.solution_origin = origin
                row.times_reused = 1
                row.last_reused_at = now
                planned.append(_row_to_item(row))
                continue

            # 命中：问题账本先记这一轮（hit_count/severity/summary/例句），
            # 再走方案库的分支决策——记账与调优是两个事实，互不替代。
            _refresh_ledger(row, count=count, examples=examples)

            # 复发计数只统计「方案此前被应用过」的行——从未被应用过的方案
            # 没有「失效」可言，它的复发是规划滞后不是方案无效。
            if (row.times_reused or 0) >= 1:
                row.recurrences = (row.recurrences or 0) + 1
                if row.recurrences >= INEFFECTIVE_RECURRENCES:
                    row.flagged_ineffective = True

            if row.flagged_ineffective:
                # 方案已被连续复发判「可能无效」→ 换一条新的，计数重置，
                # 进入下一轮「应用 → 观察复发」循环。注意先记的复发随之
                # 归零：新方案的有效性要从头观察，不背旧账。
                solution, origin = plan_solution(
                    key, count=count, examples=examples, llm=llm
                )
                row.solution = solution
                row.solution_origin = origin
                row.times_reused = 1
                row.recurrences = 0
                row.flagged_ineffective = False
                row.last_reused_at = now
                replanned.append(_row_to_item(row))
            elif (row.solution or "").strip():
                # 复用：solution 一字不改——「复用当年的微调方法」字面兑现。
                row.times_reused = (row.times_reused or 0) + 1
                row.last_reused_at = now
                reused.append(_row_to_item(row))
            else:
                # 命中但没方案（升级前的老行 / 首见）→ 规划并补上。
                solution, origin = plan_solution(
                    key, count=count, examples=examples, llm=llm
                )
                row.solution = solution
                row.solution_origin = origin
                row.times_reused = 1
                row.last_reused_at = now
                planned.append(_row_to_item(row))

        db.commit()
        return {"reused": reused, "replanned": replanned, "planned": planned}
    except Exception:
        db.rollback()
        logger.exception(
            "resolve_tuning 失败，已回滚并返回空结果（project_id=%r，%d 个模式）",
            project_id,
            len(keys),
        )
        return empty


def note_recurrence(db: Any, project_id: int | None, issue_key: str) -> None:
    """单独给某条记忆记一次复发并按阈值打标（供测试与其它路径用）。

    与 `resolve_tuning()` 的第一步同款逻辑，但不做分支决策——用于「方案
    已应用过、这里只反馈一次复发」的轻量路径。未命中记忆时静默忽略
    （记 info 日志）：没有行可记不算错误。

    本函数自行 commit：它是独立入口（「单独」），不像 upsert_memory 那样活
    在调用方的事务里；调用后数据即落库，测试与其它路径都不必操心事务边界。
    """
    key = (issue_key or "").strip()
    if not key:
        return
    row = _match_rows(db, project_id, [key]).get(key)
    if row is None:
        logger.info(
            "note_recurrence：未命中记忆，忽略（issue_key=%r，project_id=%r）",
            key,
            project_id,
        )
        return
    row.recurrences = (row.recurrences or 0) + 1
    if row.recurrences >= INEFFECTIVE_RECURRENCES:
        row.flagged_ineffective = True
    db.commit()
