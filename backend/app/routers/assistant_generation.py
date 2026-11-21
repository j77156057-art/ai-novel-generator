"""Assistant 调动生成路由（第 7 步交付 4）：POST /api/assistant/invoke-generation。

## 这是全项目里唯一一个「Assistant 触发写业务表」的端点

助手默认**只能读**（qa / diagnose / runs / analyze-logs / audit-* 全部只读），
改正文/改图谱/改设定一律走 EditProposal 审批（ADR 第 4 步）。本端点是与
审批流并行的唯一例外：**用户显式指令助手去生成**，生成动作本身复用 Writer
的生成引擎（`app.generation.engine`），不另起一套写入逻辑。

三条硬约束（缺一不可，全部有测试守着）：

1. **显式指令**：请求体必须带非空 `instruction`（用户原话/意图，如
   「继续写下一段，写主角夜探祠堂」）。没有它 → 422。这样「生成」在
   API 层就是一次可追溯的用户动作，而不是任何内部逻辑顺带触发的副作用；
2. **复用引擎**：`generate_next` 走 `generate_next_segment`，
   `regenerate_segment` 走 `regenerate_segment_stream`（内部消费完流式
   事件再统一出参）。本文件**不**直接构造/写入 `Generation`——绕过引擎
   就意味着绕过角色锁定、未填坑注入、摘要树与图谱闭环，那是基线
   b58c763 的红线；
3. **留痕**：每次调用落一条 `role='assistant'` 的 AgentRun，`note` 写明
   「这次生成是助手调用的」，`model` 回显 **Writer 实际使用的模型**——
   事后翻审计表能回答「这段正文是谁、用什么模型写出来的」。

## 与 Writer 单例的关系

本端点**不**创建也不修改 Writer provider：引擎内部自己调
`get_llm_provider()`（与 POST /api/generate 完全同一条路径），这里只在
生成结束后读一次单例回显模型名。Writer 超时（chat 120s / stream 180s，
556c057 / c1a7179）同样原样继承——本文件不传自定义 provider。

## 工具注册表

`AssistantToolRegistry` **不注册**任何直写工具（write_*/commit_* 前缀
黑名单 + 只读白名单，见 `app.agents.registry`）。助手「调动生成」靠的是
这个受审计的 HTTP 端点，不是工具表里的后门。

## 生成后的 AI 腔反馈闭环（旁路，不动上面三条约束）

两个分支生成成功并提交后，都会调用 `_run_ai_flavor_feedback()`：由
**Assistant**（本端点，主 agent）检查 Writer（sub agent）刚产出的文本，
把本次 AI 腔问题交给 `ai_flavor_memory.resolve_tuning()` 按实际问题
解析——历史上沉淀过微调方法的模式直接**复用**，没沉淀过的**现场规划**
并写入 `ai_flavor_memories`；再读回合并后的记忆（排除已知无效的）、
渲染成覆写微调 `prompt_overrides` 的 constraint_note 槽位——微调对象是
「下次给 Writer 的输入」，不是重写本次文本（检查者与生成者必须分离）。
闭环检测零 LLM、解析模板优先（仅冷门模式惰性取 Assistant provider）、
自带事务与异常兜底：任一步失败只记日志，已落库的正文与已成功的响应
都不受影响（正文落库与闭环成功是两个独立的事实）。

两条护栏（均有回归测试钉死）：① 闭环检查的是**strip 后已落库的正文**
（regen 分支传 `gen.text`），provider 思维链里的模式不会幻记进记忆；
② 覆写写入前看来源：当前生效覆写来自人工/提案时跳过写入，闭环的自动
微调不顶掉用户审批过的决策（记忆仍正常记账）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import ai_flavor_memory
from app.agents.prompt_overrides import (
    MAX_OVERRIDE_LEN,
    PromptKey,
    get_active_override,
    set_active_override,
)
from app.db import get_db
from app.generation import chapter_labels
from app.generation.engine import (
    _detect_ai_flavor,
    _should_rewrite,
    generate_next_segment,
    regenerate_segment_stream,
)
from app.generation.repetition import parse_quality
from app.models import AgentRun, Evidence, Generation, Project
from app.routers.assistant_shared import (
    _build_evidence_row,
    _now_utc,
    _request_hash,
    _require_project,
)
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant_generation")

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

#: 助手可调用的两种生成动作。`Literal` + StrictRequest：传别的值 → 422，
#: 不静默按默认动作执行（「用户说要重写第 3 段、结果续写了一段」比报错糟）。
AssistantGenerationAction = Literal["generate_next", "regenerate_segment"]


# ====================================================================
# 请求体
# ====================================================================


class AssistantInvokeGenerationRequest(StrictRequest):
    """POST /api/assistant/invoke-generation 请求体。

    字段分三组：

    - **定位**：`project_id`；
    - **动作**：`action`（generate_next | regenerate_segment）、
      `segment_no`（仅 regenerate 必填）、`max_chars`（缺省用
      `project.generation_length`，与 /api/generate 同口径）；
    - **指令**：`instruction`（用户显式指令原文，非空）——这是「这次
      生成是用户要助手干的」的结构化证据，进 AgentRun 审计链。

    刻意**不**暴露 `novel_ids`：参考语料范围取项目自身配置
    （`project.reference_novel_ids`）。助手调动生成时改检索范围属于
    「顺带改生成配置」，不在本次授权里。
    """

    project_id: int
    action: AssistantGenerationAction
    #: 仅 regenerate_segment 必填；generate_next 必须不传（见 _validate）。
    segment_no: int | None = None
    #: 缺省 None → 引擎用 project.generation_length（不在这里复制默认值，
    #: 复制出来的第二套默认一定会和第一套漂移）。
    max_chars: int | None = None
    #: 用户显式指令（非空）。空串/纯空白 → 422。
    instruction: str
    #: 仅当上一次 409 返回 ``can_takeover=true`` 后由用户显式要求时传 true。
    #: 服务层仍会用条件 UPDATE 做 CAS：原持有者心跳没超时就不会抢走。
    takeover: bool = False
    #: 含糊指令澄清：上一次 202 返回的 ``clarification_id``。
    clarification_id: int | None = None
    #: 从候选清单里选定的区间。必须原样来自候选（服务端会再校验一次）。
    selected: dict[str, int] | None = None
    #: 「都不对」的补充说明；最多两轮，超过 → 422 ``clarify_round_limit``。
    clarify_reply: str | None = None

    @model_validator(mode="after")
    def _validate_action_fields(self) -> "AssistantInvokeGenerationRequest":
        """动作与字段的配套校验（fail loud，不静默忽略不匹配的字段）。"""
        if self.action == "regenerate_segment":
            if self.segment_no is None:
                raise ValueError("regenerate_segment 必须带 segment_no")
            if self.segment_no <= 0:
                raise ValueError("segment_no 必须为正整数")
        elif self.segment_no is not None:
            # generate_next 带 segment_no = 调用方对语义有误解。按默认
            # 动作执行会让他以为「指定段被续写了」，不如直接拒。
            raise ValueError("generate_next 不接受 segment_no（段号由引擎分配）")
        if not (self.instruction or "").strip():
            raise ValueError("instruction 不能为空：助手生成必须是显式用户指令")
        clarification_fields = (
            self.clarification_id, self.selected, self.clarify_reply
        )
        if self.action != "regenerate_segment" and any(
            value is not None for value in clarification_fields
        ):
            raise ValueError("澄清字段只适用于 regenerate_segment")
        if self.selected is not None and self.clarify_reply is not None:
            raise ValueError("selected 与 clarify_reply 不能同时提交")
        if self.clarification_id is None and any(
            value is not None for value in (self.selected, self.clarify_reply)
        ):
            raise ValueError("澄清后续请求必须带 clarification_id")
        if self.clarification_id is not None:
            if self.selected is None and not (self.clarify_reply or "").strip():
                raise ValueError(
                    "clarification_id 必须搭配 selected 或非空 clarify_reply"
                )
        if self.selected is not None:
            from_index = self.selected.get("from_index")
            to_index = self.selected.get("to_index")
            if not isinstance(from_index, int) or not isinstance(to_index, int):
                raise ValueError("selected 必须包含整数 from_index 与 to_index")
            if from_index < 1 or to_index < 1 or from_index > to_index:
                raise ValueError("selected 区间不合法")
        return self


# ====================================================================
# 内部小工具
# ====================================================================


def _writer_model_label() -> str:
    """Writer 单例实际使用的模型名（响应与 AgentRun.model 回显用）。

    生成结束后才读：引擎内部自己 `get_llm_provider()`，此时单例一定
    已构造且就是刚才那位。读不到（未配置 key 等）→ `(unknown)`；
    本地兜底 provider 没有 model/label 属性 → `(local_fallback)`——
    两种都不是正常生产路径，必须让看响应的人一眼看出不对劲。
    """
    from app.llm.provider import get_llm_provider

    try:
        provider = get_llm_provider()
    except Exception as exc:  # noqa: BLE001 - 回显失败不该把已完成的生成判失败
        logger.warning("invoke-generation 读取 Writer 模型名失败：%s", exc)
        return "(unknown)"
    label = getattr(provider, "model", "") or getattr(provider, "label", "") or ""
    return label or "(local_fallback)"


def _require_segment(db: Session, project_id: int, segment_no: int) -> Generation:
    """重生成目标段必须存在；不存在 → 404（不留半开的 AgentRun）。"""
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id, Generation.segment_no == segment_no
        )
    ).first()
    if gen is None:
        raise HTTPException(
            status_code=404, detail=f"段 {segment_no} 不存在，无法重新生成"
        )
    return gen


def _record_generation_evidence(
    db: Session, run_id: int, gen: Generation, text: str
) -> Evidence:
    """把「这段正文是这次助手调用产出的」落成 Evidence 行。

    kind="generation" 与诊断/问答的证据同族——助手日志详情页因此能
    顺着 Evidence 直接跳到生成段，不用再猜是哪一段。
    """
    row = _build_evidence_row(
        run_id, kind="generation", source="generated",
        ref_id=gen.id, snippet=(text or "")[:200], score=1.0,
    )
    db.add(row)
    db.flush()
    return row


# ====================================================================
# AI 腔反馈闭环（生成成功后的旁路：Assistant 检查 Writer 产出）
# ====================================================================
#
# 架构（用户拍板，勿绕）：助理 agent 是主 agent，Writer 是 sub agent。生成
# 成功后由 **Assistant** 检查刚产出的文本、把本次 AI 腔问题总结成一句人话
# 写进 `ai_flavor_memories`；再读回合并后的记忆、渲染成「本轮优先避免」
# 约束写入 `prompt_overrides` 的 constraint_note 槽位——下次生成时
# `engine._prompt_overrides()` 会把它拼进 Writer 的系统提示（注入通道已由
# 4227c73 打通，见 tests/test_prompt_override_wiring.py）。微调对象是
# 「下次给 Writer 的输入」，不是重写本次文本：检查者与生成者必须分离，
# Writer 自检自改是本末倒置。
#
# 三条硬约束（全部有测试守着）：
# 1. **闭环是旁路**：检测 / 解析 / 微调任一步抛异常，都吞掉并记日志，
#    绝不让 /invoke-generation 失败——正文早已落库，闭环只是附加动作；
# 2. **检测零 LLM、解析模板优先**：检测步只用 engine 的纯本地检测
#    （`_should_rewrite` / `_detect_ai_flavor`）+ DB 读写，不调
#    provider.chat、不 hybrid_search；解析步（resolve_tuning）对现有
#    模式模板优先，仅模板覆盖不到的冷门模式才惰性取 Assistant
#    provider——取 provider 的动作被推迟到真正被消费时（零成本纪律，
#    见 `_LazyLLMHandle`）；
# 3. **不改本次文本**：闭环只读 text，不碰 Generation / target.text。


#: AI 腔**记忆线**：单段检测 total 达到该值，才写记忆 + 微调 constraint_note。
#:
#: 与既有阈值的关系（两条线各管一件事，不许混用）：
#: - `engine._should_rewrite` 的告警线是 total >= 5，管「**这一段**要不要
#:   重写」——生成链路内的即时补救信号，代价至多一次重写调用；
#: - 本常量管「**这个项目**要不要沉淀一条长期教训」——记忆行会进
#:   `ai_flavor_memories` 表，并被 `load_memories()` 渲染进此后**每一次**
#:   生成的提示词。若按 5 记账：普通中文小说里「终于 / 原来 / 从此 /
#:   不禁」本就是高频叙事词，单段轻微命中 5-7 次很常见，表会被日常轻微
#:   命中刷爆，提示词也会被同一批教训复读撑爆——正是 ai_flavor_memory
#:   「有界膨胀」设计要防的事。
#: 取 8 的理由：告警线上方留出 60% 缓冲带（5-7 归「轻微」，只告警不记账），
#: 又不松到 10+ 把成段的系统性问题漏掉。参照系：单段 3000 字命中 8 次 ≈
#: 每 375 字一次 AI 腔套路，已远超正常叙述密度，属「Writer 系统性复读」。
_AI_FLAVOR_MEMORY_MIN_TOTAL = 8

#: 单模式严重度分档线（按「同一模式在本段的命中次数」，语义对齐
#: ai_flavor_memory.SEVERITY_RANK：high=明显破坏文风 / mid=润色级）：
#: 单段约 3000 字里同一模式复读 4 次以上属「明显破坏文风」；2-3 次是
#: 偶发的表述惯性；仅 1 次记档备查。
_SEVERITY_HIGH_HITS = 4
_SEVERITY_MID_HITS = 2

#: 每个模式单次最多带的例句数。写入端总闸是 ai_flavor_memory.MAX_EXAMPLES
#: （3 条，新的挤掉最旧的），这里取 2 是给后续段落的证据留合并空间——
#: 一次把 3 个坑位占满，后面的段落再命中同模式就永远带不上新例句。
_EXAMPLES_PER_PATTERN = 2

#: 「本轮优先避免」约束块的标题行。写进 constraint_note 槽位后，会拼在
#: Writer 系统提示的【续写纪律】之后，标题让 Writer 一眼知道这段约束的
#: 来历（历史检查沉淀的教训，不是用户本轮的新要求）。
_FLAVOR_OVERRIDE_HEADER = (
    "【本轮优先避免】以下是历史生成检查沉淀的 AI 腔教训，本轮写作请优先规避这些表达："
)

#: 覆写写入者标识（prompt_overrides.created_by）：审计时一眼看出这条覆写
#: 是闭环自动写的，还是人工提案批的。
_FLAVOR_OVERRIDE_CREATED_BY = "assistant_ai_flavor_feedback"

#: 注：单模式严重度分档（旧 `_severity_for_count`）已随「解析调优」改造
#: 移入 `ai_flavor_memory.resolve_tuning`——严重度是记忆行/方案库的属性，
#: 由持有方案库的那一侧按同一口径定档；闭环只负责把「本段命中次数」作为
#: evidence 交进去。两处各写一套分档线必漂移（本项目历史教训）。


def _pattern_examples(
    occurrences: list[dict] | None, segment_no: int | None
) -> list[dict]:
    """取该模式的前几条出现作为例句证据（字段口径见 `upsert_memory`）。

    只截取不解释：`_detect_ai_flavor` 返回的 occurrences 已带 offset 与
    snippet，这里补上 segment_no 即可直接喂给写入端。
    """
    picked: list[dict] = []
    for occ in (occurrences or [])[:_EXAMPLES_PER_PATTERN]:
        picked.append(
            {
                "segment_no": segment_no,
                "offset": occ.get("offset"),
                "snippet": str(occ.get("snippet") or ""),
            }
        )
    return picked


def _summarize_flavor_hits(
    total: int, hits: dict[str, int], occurrences: dict[str, list[dict]]
) -> str:
    """把检测结果归纳成一句人话（模式×次数 + 一条例句）。

    例：「AI 腔密集：全段命中 9 次（仿佛×5、宛如×4）；例：「…仿佛…」」。
    只报前 3 个模式：总结是「让人一眼看懂」，不是把 hits 全量复读；例句取
    top 模式的首次出现，证明这条总结有原文依据（不是凭空报数）。
    """
    top = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    head = "、".join(f"{pattern}×{count}" for pattern, count in top)
    example = ""
    for pattern, _count in top:
        first = (occurrences.get(pattern) or [None])[0]
        snippet = str((first or {}).get("snippet") or "").strip()
        if snippet:
            example = snippet
            break
    line = f"AI 腔密集：全段命中 {total} 次（{head}）"
    if example:
        line += f"；例：「{example}」"
    return line


def _render_flavor_constraint(db: Session, project_id: int) -> str:
    """读回合并后的记忆，渲染成 constraint_note 槽位的覆写文本。

    走 `load_memories()`（合并 global + 本项目、去重、按条数/字符预算裁剪）
    + `render_memory_block()`：覆写文本是「全部生效教训」的快照，不是只有
    本次命中——下次生成的 Writer 该知道这本账上到目前为止的所有教训。
    无记忆时返回空串（调用方据此跳过写入，不产生空覆写行）。

    防御既有脏数据：`upsert_memory` 入口的 summary 截断（治本）管未来写入，
    这里按 `MAX_OVERRIDE_LEN` 再截断（治标）兜底历史超长行——截断而不是
    让 `set_active_override` 的长度闸抛错：异常会令闭环整体回滚，该项目
    自动微调从此永久静默罢工（QA F2）。

    `exclude_flagged=True`：被有效性反馈标记为「疑似无效」的记忆不进提示词
    （判定与打标在 `ai_flavor_memory` 侧）——已知不靠谱的建议继续渲染只会
    污染 Writer 的输入；标记的语义是「这条建议试过还会犯」，不是「这个
    问题没发生过」，记账连续性不受影响。
    """
    items = ai_flavor_memory.load_memories(db, project_id, exclude_flagged=True)
    block = ai_flavor_memory.render_memory_block(items)
    if not block:
        return ""
    text = f"{_FLAVOR_OVERRIDE_HEADER}\n{block}"
    if len(text) > MAX_OVERRIDE_LEN:
        logger.warning(
            "AI 腔记忆渲染结果超长，已按 MAX_OVERRIDE_LEN 截断"
            "（project=%s, %d > %d，疑似历史脏数据）",
            project_id, len(text), MAX_OVERRIDE_LEN,
        )
        text = text[:MAX_OVERRIDE_LEN]
    return text


def _lazy_llm() -> Any | None:
    """惰性取 Assistant provider；取不到（未配置 key 等）返回 None。

    为什么惰性（零成本纪律）：`resolve_tuning` 对现有模式全部模板优先，
    只有模板覆盖不到的冷门模式才需要真 provider。若在调 resolve_tuning
    **之前**就取 provider，等于每次闭环都为几乎不会用到的 LLM 付一次
    构造/鉴权成本。取不到时返回 None 而非抛错：模板路径不依赖 provider，
    冷门路径由 resolve_tuning 自行决定降级——闭环是旁路，不该因为
    「LLM 没配」而整体罢工。
    """
    try:
        from app.llm.provider import get_assistant_provider

        return get_assistant_provider()
    except Exception as exc:  # noqa: BLE001 - 取不到 LLM 不该炸掉闭环
        logger.info(
            "AI 腔反馈闭环：Assistant provider 不可用，"
            "冷门模式将走纯模板降级：%s", exc,
        )
        return None


class _LazyLLMHandle:
    """惰性 LLM 句柄：resolve_tuning 无论把它当**可调用**还是当 **provider
    实例**消费都成立，且取 provider 的时刻被推迟到真正被消费的那一刻。

    为什么需要这个句柄（零成本纪律）：闭环对 LLM 的要求是「冷门模式才用」——
    现有模式全有模板，resolve_tuning 内部模板优先。若在调用 resolve_tuning
    之前就取 provider，等于每次闭环都为几乎不会用到的 LLM 付一次构造/鉴权
    成本。本句柄把 `_lazy_llm()` 的调用包上一层，对两种消费方式都惰性：

    - 当可调用：`handle()` → 真实 provider 或 None（取不到不炸，交回
      resolve_tuning 决定降级）；
    - 当 provider 实例：`handle.chat(...)` 等任意属性/方法 → 惰性取到后
      原样转调；取不到则抛 RuntimeError，让 resolve_tuning 的模板兜底
      逻辑接住（闭环外层还有异常隔离，不会影响生成主流程）。

    句柄内缓存首次解析结果：同一轮闭环里多次消费只取一次 provider。
    （契约的 `llm` 参数类型标注为 `Any`，两种消费方式都覆盖是刻意的——
    按实际读到的契要走，这里不猜。）
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._resolved = False
        self._provider: Any = None

    def _resolve(self) -> Any:
        """首次消费时才真取 provider；之后复用缓存（同轮闭环只取一次）。"""
        if not self._resolved:
            self._provider = self._factory()
            self._resolved = True
        return self._provider

    def __call__(self) -> Any:
        """可调用消费方式：返回真实 provider 或 None。"""
        return self._resolve()

    def __getattr__(self, name: str) -> Any:
        """provider 实例消费方式：惰性取到后原样转调（chat / model / …）。

        `_` 开头的名字直接拒绝：防止初始化期（实例属性尚未就绪）走进
        无限递归。provider 取不到时抛 RuntimeError 而非 AttributeError——
        这是「LLM 不可用」的事实，不该被 `hasattr` 静默吞成「没有 chat」。
        """
        if name.startswith("_"):
            raise AttributeError(name)
        provider = self._resolve()
        if provider is None:
            raise RuntimeError(
                f"Assistant provider 不可用（惰性获取失败），无法代理 {name!r}"
            )
        return getattr(provider, name)


def _run_ai_flavor_feedback(
    db: Session,
    project: Project,
    text: str,
    segment_no: int | None,
    instruction: str,
) -> dict | None:
    """AI 腔反馈闭环：检查本次产出 → 总结 → 解析调优 → 微调下次生成的输入。

    挂载点：`assistant_invoke_generation` 两个分支的主流程 `db.commit()`
    之后、`return` 之前（正文已落库，闭环是旁路）。

    四步（按序）：
    1. **检查**：`_should_rewrite` / `_detect_ai_flavor`（纯本地、零 LLM、
       零语料）。判据是 `total >= _AI_FLAVOR_MEMORY_MIN_TOTAL`——未达记忆
       线的轻微命中只告警不记账（见常量注释里与告警线 5 的关系）；
    2. **总结**：`_summarize_flavor_hits` 归纳「本次哪里犯了 AI 腔」
       （模式×次数 + 例句）；
    3. **解析调优**：`resolve_tuning` 按 evidence（模式 × 本段次数 + 例句）
       决定每个模式「复用老方案 / 重规划 / 新规划」——历史上沉淀过微调
       方法的模式直接复用（solution 一字不差、times_reused 累加），没
       沉淀过的现场规划并落库；模板优先，仅冷门模式惰性取 Assistant
       provider（见 `_LazyLLMHandle`）；
    4. **微调**：`_render_flavor_constraint` 读回合并后的记忆（排除已知
       无效的）渲染成约束，经 `set_active_override` 写入 constraint_note
       槽位（「同 key 先降旧再插新」的既有不变量由此保留，不直接
       db.add 绕开它）；内容与当前生效覆写逐字节相同时跳过，不在历史表里
       堆无意义的新行。当前生效覆写若来自人工/提案（`created_by` 不是本
       闭环的标识），跳过写入保留人工决策（治理口径见 `prompt_overrides`
       模块注释）——记忆仍正常写，记账与微调互不影响；

    事务与兜底：主流程提交后才开始；**记账与微调是两个事务边界**——
    `resolve_tuning` 自行 commit（观测事实先落袋为安），其后若渲染/覆写
    失败，只回滚未提交部分并 `logger.warning`，已记账的记忆行**不丢**
    （覆写是可从记忆重建的派生状态，丢掉观测才是真损失；下一轮重新
    resolve 会自愈补上覆写）。任一步抛异常都**绝不影响**已成功的生成
    ——正文落库与闭环成功是两个独立的事实。

    Returns:
        走完闭环（含未达阈值提前返回）时的摘要 dict；异常时返回 None。
        摘要键：`total` / `summary` / `alerted` / `override_written` /
        `reused` / `replanned` / `planned`——后三个让调用方看得见「这次
        是复用了老方案（reused）还是重规划/新规划了（replanned/planned）」；
        两个生成分支共用本函数构造返回值，同键同形状（两处各写一遍必漂移
        是这个项目的历史教训）。
    """
    try:
        # forbidden 传 None：本闭环只记账 AI 腔模式；反向禁忌是另一本账，
        # 且取 forbidden 要读叙事档案——与「零语料」纪律无关但不必要。
        alerted, reasons = _should_rewrite(text or "", None)
        flavor = _detect_ai_flavor(text or "")
        total = int(flavor.get("total") or 0)
        if total < _AI_FLAVOR_MEMORY_MIN_TOTAL:
            logger.info(
                "AI 腔反馈闭环：未达记忆线，不写记忆不微调"
                "（project=%s, segment=%s, total=%d < %d）",
                project.id, segment_no, total, _AI_FLAVOR_MEMORY_MIN_TOTAL,
            )
            return None

        hits: dict[str, int] = flavor.get("hits") or {}
        occurrences: dict[str, list[dict]] = flavor.get("occurrences") or {}
        summary = _summarize_flavor_hits(total, hits, occurrences)

        # 解析调优：把本次检测结果整理成 evidence，交给 resolve_tuning 决定
        # 每个模式「复用老方案 / 老方案不灵重规划 / 全新规划」——取代旧的
        # 「逐模式无脑 upsert 问题行 + 渲染时全量记忆一锅端」：该复用的复用，
        # 该规划的才规划。evidence 按命中次数降序（同次数按模式名），确定性
        # 顺序便于对账；examples 走 _pattern_examples（截到
        # _EXAMPLES_PER_PATTERN 条并补 segment_no，与记忆行 examples 列的
        # 既有口径一致，不是裸 occurrences）。
        evidence: dict[str, dict[str, Any]] = {}
        for pattern, count in sorted(hits.items(), key=lambda kv: (-kv[1], kv[0])):
            evidence[pattern] = {
                "count": count,
                "examples": _pattern_examples(
                    occurrences.get(pattern) or [], segment_no
                ),
            }

        # 惰性 LLM：句柄被 resolve_tuning 消费时才取 assistant provider
        # （见 _LazyLLMHandle）。现有模式全有模板、模板优先，实际不会触发
        # LLM——这就是为什么值得惰性：不为几乎不会用的模型付构造/鉴权成本。
        tuning = ai_flavor_memory.resolve_tuning(
            db, project.id, evidence, llm=_LazyLLMHandle(_lazy_llm)
        )
        reused: list[dict] = list(tuning.get("reused") or [])
        replanned: list[dict] = list(tuning.get("replanned") or [])
        planned: list[dict] = list(tuning.get("planned") or [])

        # 微调：读回合并后的记忆（含本次刚写的）渲染成约束写进槽位
        override_text = _render_flavor_constraint(db, project.id)
        override_written = False
        if override_text:
            current = get_active_override(db, project.id, PromptKey.CONSTRAINT)
            if current is not None and \
                    (current.created_by or "") != _FLAVOR_OVERRIDE_CREATED_BY:
                # 当前生效覆写不是本闭环写的（created_by 为 None 或提案标识
                # 等）：按 prompt_overrides 的治理口径，人工/提案来源的覆写是
                # 用户审批的决策，闭环不得顶掉（QA F1）。记忆照写——记账与
                # 微调是两件事；等人工覆写失效后，后续闭环再接手微调。
                logger.info(
                    "AI 腔反馈闭环：当前 constraint_note 覆写来源为 %r"
                    "（非本闭环 %r），跳过本次微调写入，保留人工覆写"
                    "（project=%s）",
                    current.created_by, _FLAVOR_OVERRIDE_CREATED_BY, project.id,
                )
            elif current is None or (current.override_text or "") != override_text:
                set_active_override(
                    db,
                    project_id=project.id,
                    prompt_key=PromptKey.CONSTRAINT,
                    override_text=override_text,
                    created_by=_FLAVOR_OVERRIDE_CREATED_BY,
                )
                override_written = True
        db.commit()
        logger.info(
            "AI 腔反馈闭环完成（project=%s, segment=%s, instruction=%s）：%s"
            "；重写告警判据：%s",
            project.id, segment_no, (instruction or "").strip()[:60], summary,
            "；".join(reasons) or "未告警",
        )
        return {
            "total": total,
            "summary": summary,
            "alerted": alerted,
            "override_written": override_written,
            # 解析结果摘要：让调用方看得见「这次是复用了老方案（reused）/
            # 重规划了不灵的旧方案（replanned）/ 规划了新方案（planned）」。
            # 两个生成分支共用本函数构造返回值，同键同形状——两处各写一遍
            # 必漂移是这个项目的历史教训。
            "reused": reused,
            "replanned": replanned,
            "planned": planned,
        }
    except Exception as exc:  # noqa: BLE001 —— 闭环是旁路，失败绝不能影响已成功的生成
        try:
            db.rollback()
        except Exception:  # noqa: BLE001 —— 回滚也失败就只能记日志，别把异常传出闭环
            logger.warning(
                "AI 腔反馈闭环回滚失败（project=%s），放弃本次闭环写入",
                project.id,
            )
        logger.warning(
            "AI 腔反馈闭环失败，已回滚半截写入（不影响本次生成，project=%s）：%s",
            project.id, exc,
        )
        return None


# ====================================================================
# 含糊指令：候选澄清（2026-11-12）
# ====================================================================
#
# Tier B（某处/此处/这一句…）不再让模型自己决定改哪里：第一轮只让它列
# 候选区间（只回编号，不回正文），程序按编号从原文截取预览，以 202 返回给
# 用户选。选定后按该区间走 Tier A 的同一份改写提示词与全部校验。
#
# 澄清阶段**不写正文**：不建 pending 快照、不碰 generations.text，只留一条
# status='needs_clarification' 的 GenerationSnapshot。它不带 pending，因此
# 既不占该段的写租约，也不会被 rollback 命中。同一（项目/段/正文哈希/
# 指令/轮次/补充说明）的重复提交命中 fingerprint 直接复用上次候选，
# 不会因为重试生成第二批。

#: 最多澄清两轮：首轮候选 + 一次「都不对」补充说明。再问就是无限追问。
_CLARIFY_MAX_ROUNDS = 2

_CLARIFY_SYSTEM = (
    "你是中文小说编辑部的定位助手。用户会给出一个不指明具体位置的改写要求，"
    "你的唯一任务是：阅读已编号的正文，指出最值得改写的 2–4 个连续句子区间。"
    "你只输出编号与一句理由，不改写正文、不解释、不输出任何其他文字。"
)


def _clarify_fingerprint(
    *, project_id: int, segment_no: int, before_hash: str,
    instruction: str, round_no: int, note: str | None,
) -> str:
    """澄清幂等键：同一请求（含轮次与补充说明）重复提交必须命中同一个值。"""
    payload = "\x1f".join([
        str(project_id), str(segment_no), before_hash,
        (instruction or "").strip(), str(round_no), (note or "").strip(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _clarify_payload(
    snap, *, instruction: str, round_no: int, limit: int
) -> dict:
    """202 响应体：``status='needs_clarification'`` 与 200 成功可一眼区分。"""
    data = snap.clarification if isinstance(snap.clarification, dict) else {}
    return {
        "status": "needs_clarification",
        "code": "needs_clarification",
        "clarification_id": snap.id,
        "segment_no": snap.segment_no,
        "instruction": instruction,
        "round": round_no,
        "max_rounds": _CLARIFY_MAX_ROUNDS,
        "candidates": data.get("candidates") or [],
        "limit": limit,
        "rounds_left": max(0, _CLARIFY_MAX_ROUNDS - round_no),
        "before_hash": snap.before_hash,
        "message": (
            "这句指令没有指明要改哪里：请从下列候选区间里选一个，"
            "或点「都不对」补充一句说明。"
        ),
    }


def _run_clarification(
    db: Session,
    project: Project,
    target: Generation,
    *,
    instruction: str,
    round_no: int,
    note: str | None,
    request_hash: str,
) -> JSONResponse:
    """跑一轮候选澄清并返回 HTTP 202（**不写正文、不建 pending**）。"""
    from app.agents.dialog.snapshot import (
        begin_clarification,
        find_clarification_by_fingerprint,
        text_hash,
    )
    from app.generation.patch import (
        PatchError,
        build_candidates_user_prompt,
        clarify_span_limit,
        parse_candidates_reply,
        split_sentences,
    )

    before_text = target.text or ""
    before_hash = text_hash(before_text)
    limit = clarify_span_limit(len(split_sentences(before_text)))
    fingerprint = _clarify_fingerprint(
        project_id=project.id, segment_no=target.segment_no,
        before_hash=before_hash, instruction=instruction,
        round_no=round_no, note=note,
    )
    # 幂等前置：同一请求重复提交（重试/双击）直接复用上次候选，不调模型。
    existing = find_clarification_by_fingerprint(
        db, project_id=project.id, segment_no=target.segment_no,
        fingerprint=fingerprint,
    )
    if existing is not None:
        data = existing.clarification if isinstance(existing.clarification, dict) else {}
        return JSONResponse(
            status_code=202,
            content=_clarify_payload(
                existing, instruction=instruction,
                round_no=int(data.get("round") or round_no), limit=limit,
            ),
        )

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=_now_utc(),
        finished_at=None,
        request_hash=request_hash,
        model="",
        note=(
            f"这次是助手发起的澄清（segment_no={target.segment_no}"
            f", round={round_no}）：{instruction[:120]}"
        ),
    )
    db.add(run)
    db.flush()
    run_id = run.id
    db.commit()  # 先落 run，避免把长调用包在事务里

    from app.llm.provider import get_llm_provider

    try:
        provider = get_llm_provider()
        raw = provider.chat(
            _CLARIFY_SYSTEM,
            build_candidates_user_prompt(before_text, instruction, note),
        )
        candidates = parse_candidates_reply(raw, before_text)
    except PatchError as exc:
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = "(unknown)"
        run.note = f"助手发起的澄清失败（{exc.code}）：{str(exc)[:120]}"
        db.commit()
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": str(exc),
                    "segment_no": target.segment_no},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - provider 报错/超时同样要留痕
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = "(unknown)"
        run.note = f"助手发起的澄清失败：{str(exc)[:120]}"
        db.commit()
        raise HTTPException(
            status_code=500, detail=f"候选澄清失败：{str(exc)[:200]}"
        ) from exc

    snap = begin_clarification(
        db, project, target,
        instruction=instruction,
        round_no=round_no,
        candidates=candidates,
        fingerprint=fingerprint,
        note=note,
        agent_run_id=run_id,
    )
    run.status = "success"
    run.finished_at = _now_utc()
    run.model = _writer_model_label()
    db.commit()
    return JSONResponse(
        status_code=202,
        content=_clarify_payload(
            snap, instruction=instruction, round_no=round_no, limit=limit
        ),
    )


# ====================================================================
# POST /api/assistant/invoke-generation
# ====================================================================


def _generate_next_with_chapter(db: Session, project: Project,
                                max_chars: int | None) -> dict:
    """助手入口写下一段前先分配章号。

    只做「下一章 = 库内最大章号 + 1」的分配，**不做任何段号推断**：
    `resolve` 失败就把异常抛出去，绝不回落到 `段N`。
    """
    plan = chapter_labels.resolve(db, project, None)
    return generate_next_segment(
        db, project, max_chars=max_chars, chapter_range=plan["label"])


@router.post("/invoke-generation", response_model=None)
def assistant_invoke_generation(
    body: AssistantInvokeGenerationRequest, db: Session = Depends(get_db)
) -> dict | JSONResponse:
    """助手调动生成（**唯一**允许助手触发写业务表的端点；显式用户指令）。

    执行顺序：

    1. 校验项目（404）与目标段（regenerate 时 404）；
    2. 开 AgentRun（role=assistant，note=「这次生成是助手调用的」）；
    3. 调 `app.generation.engine`——角色锁定 / 未填坑注入 / 摘要树 /
       图谱闭环全部走既有链路，本端点不碰 Generation 写入；
    4. 落一条 generation Evidence 指向产出的段；
    5. 收尾 AgentRun：`model` 回显 Writer 实际模型，`note` 保留调用
       性质说明；失败 → run 记 failed 并返回 5xx（生成是用户明确要的
       结果，不降级成「空响应但 200」）。
    6. AI 腔反馈闭环（旁路）：检查本次产出的 AI 腔问题、沉淀记忆并微调
       下次生成的输入；自带事务与异常兜底，失败绝不影响本次已成功的
       生成（见 `_run_ai_flavor_feedback`）。

    `instruction` 原样进 AgentRun.request_hash 与响应 `answer`；`regenerate_segment`
    分支还会把它下发给引擎，渲染成提示词里的【本次重写要求】指令区（2026-11-11），
    响应里的 `instruction_injected` 回报它是否真的出现在发出的 prompt 中。
    """
    project = _require_project(db, body.project_id)
    target: Generation | None = None
    snapshot = None
    # 用户从候选清单选定（或程序解析出）的区间；None = 整段重写。
    selected_scope: dict | None = None
    # 实际下发进 Tier A 改写提示词的指令：澄清选定时用澄清行保存的原指令，
    # 保证「同一份提示词」，与这次的 body.instruction 无关。
    instruction = body.instruction
    if body.action == "regenerate_segment":
        # 先校验再开 run：目标段不存在时不留一条空跑的审计记录
        target = _require_segment(db, project.id, body.segment_no)

        if body.clarification_id is not None:
            # —— 澄清后续：选定区间走 Tier A / 「都不对」补一轮 ——
            from app.agents.dialog.snapshot import find_clarification, text_hash

            clar = find_clarification(db, body.clarification_id)
            if (
                clar is None
                or clar.project_id != project.id
                or clar.segment_no != body.segment_no
                or clar.status not in ("needs_clarification", "clarified")
            ):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "clarification_not_found",
                        "message": "澄清记录不存在、不属于该段，或已失效。",
                        "segment_no": body.segment_no,
                    },
                )
            if text_hash(target.text) != clar.before_hash:
                # 发起澄清后正文被别人改过：旧候选的编号已不可信。
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "clarification_stale",
                        "message": "澄清发起后正文已被改过，请重新发起本次改写。",
                        "segment_no": body.segment_no,
                    },
                )
            data = clar.clarification if isinstance(clar.clarification, dict) else {}
            instruction = (clar.instruction or body.instruction).strip()
            if body.selected is not None:
                from app.generation.patch import (
                    PatchError,
                    scope_from_selection,
                    split_sentences,
                )

                try:
                    selected_scope = scope_from_selection(
                        data.get("candidates") or [],
                        body.selected["from_index"],
                        body.selected["to_index"],
                        len(split_sentences(target.text or "")),
                    )
                except PatchError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={"code": exc.code, "message": str(exc),
                                "segment_no": body.segment_no},
                    ) from exc
            else:
                round_no = int(data.get("round") or 1) + 1
                if round_no > _CLARIFY_MAX_ROUNDS:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "clarify_round_limit",
                            "message": (
                                f"已经澄清过 {_CLARIFY_MAX_ROUNDS} 轮仍未选定区间；"
                                "本次失败关闭。请改用更明确的指令"
                                "（如「最后两句」「第 5 句」）再试。"
                            ),
                            "segment_no": body.segment_no,
                        },
                    )
                return _run_clarification(
                    db, project, target,
                    instruction=instruction,
                    round_no=round_no,
                    note=body.clarify_reply,
                    request_hash=_request_hash(body),
                )
        else:
            # —— 没有澄清上下文：含糊指令必须**先澄清**，不写正文 ——
            from app.generation.patch import PatchError, resolve_scope
            from app.generation.scope_guard import declared_local

            if declared_local(instruction):
                try:
                    resolved = resolve_scope(instruction, target.text or "")
                except PatchError as exc:
                    # 明确的局部范围但不合法（如「最后五句」超上界）：
                    # 这是 Tier A 的失败，不该降级成澄清。
                    raise HTTPException(
                        status_code=422,
                        detail={"code": exc.code, "message": str(exc),
                                "segment_no": body.segment_no},
                    ) from exc
                if resolved is None:
                    return _run_clarification(
                        db, project, target,
                        instruction=instruction,
                        round_no=1,
                        note=None,
                        request_hash=_request_hash(body),
                    )

        # —— 两段式写入第一段：调模型**前**写 pending 快照（独立短事务）——
        # 同段已有在途重写时抛 SnapshotBusy（部分唯一索引兜底）→ 409。
        # 放在开 AgentRun 之前：被并发挡下的请求根本没跑模型，不该留一条
        # 「running → failed」的空审计（与上面 404 先校验同一纪律）。
        from app.agents.dialog.snapshot import SnapshotBusy, begin_snapshot

        try:
            snapshot = begin_snapshot(
                db, project, target,
                instruction=instruction,
                takeover=body.takeover,
            )
        except SnapshotBusy as exc:
            # detail 已在服务层算好（含 can_takeover / 原持有者 / 停跳秒数 /
            # 可选动作）；这里不再拼字段，避免把 SQL/约束错误原文漏给用户。
            raise HTTPException(status_code=409, detail=exc.detail) from exc

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=_now_utc(),
        finished_at=None,
        request_hash=_request_hash(body),
        model="",
        note=(
            f"这次生成是助手调用的（action={body.action}"
            + (f", segment_no={body.segment_no}" if body.segment_no else "")
            + f"）：{body.instruction.strip()[:120]}"
        ),
    )
    db.add(run)
    db.flush()
    run_id = run.id
    if snapshot is not None:
        # 把 AgentRun 挂到快照上（失败排查时能顺着快照找到这次 run）。
        snapshot.agent_run_id = run_id
        db.commit()

    writer_model = ""
    try:
        if body.action == "generate_next":
            result = _generate_next_with_chapter(db, project, body.max_chars)
            gen = db.scalars(
                select(Generation)
                .where(
                    Generation.project_id == project.id,
                    Generation.segment_no == result["segment_no"],
                )
            ).first()
            if gen is None:  # 引擎承诺落库；真没有就是引擎坏了，不伪装成功
                raise RuntimeError("引擎未返回已落库的生成段")
            _record_generation_evidence(db, run_id, gen, result.get("text") or "")
            writer_model = _writer_model_label()
            run.status = "success"
            run.finished_at = _now_utc()
            run.model = writer_model
            db.commit()
            # AI 腔反馈闭环（旁路）：主流程已落库；闭环只读本次正文、写记忆
            # 并微调下次生成的输入，自带事务与异常兜底，失败不影响本次成功。
            segment_out = _segment_out(gen)
            flavor_feedback = _run_ai_flavor_feedback(
                db, project, result.get("text") or "",
                result["segment_no"], body.instruction,
            )
            return {
                "answer": (
                    f"已按你的指令续写第 {result['segment_no']} 段"
                    f"（{len(result.get('text') or '')} 字，"
                    f"Writer 模型 {writer_model}）。"
                ),
                "agent_run_id": run_id,
                "action": body.action,
                "writer_model": writer_model,
                "segment": segment_out,
                "chars": len(result.get("text") or ""),
                "summary": result.get("summary") or "",
                "kg_update": result.get("kg_update") or {},
                "summary_tree": result.get("summary_tree") or {},
                # 闭环摘要（旁路观测）：记账/微调做了什么、异常时为 None。
                # 没有它，调用方无从得知「这次自动微调到底发生了什么」。
                "ai_flavor_feedback": flavor_feedback,
                # 复读闸门（D7）：落库仍是 200，只把结论带回来。
                "repetition": result.get("repetition"),
                "length": result.get("length"),
                "summary_style": result.get("summary_style"),
                "warnings": result.get("warnings") or [],
                "debug": {
                    "system_prompt": result.get("system_prompt") or "",
                    "user_prompt": result.get("user_prompt") or "",
                    "constraints": result.get("constraints") or {},
                    "rag_context": result.get("rag_context") or [],
                },
            }

        # ---- regenerate_segment：消费引擎的流式生成器（不另起写入）----
        pieces: list[str] = []
        done: dict[str, Any] = {}
        for event in regenerate_segment_stream(
            db,
            project,
            body.segment_no,
            max_chars=body.max_chars,
            # 用户的重写要求真正下发进生成提示（引擎侧渲染到
            # 【本次重写要求】指令区）；空串时提示词与改动前逐字节一致。
            # 澄清选定时用的是澄清行保存的原指令（同一份提示词）。
            instruction=instruction,
            # 两段式写入第二段：正文 CAS + 快照 applied 在引擎那次提交里一起落。
            snapshot_id=snapshot.id,
            expected_before=snapshot.before_text,
            # 澄清选定的区间：引擎直接走 Tier A 改写提示词与全部校验，
            # 不再让模型定位，也不复用澄清阶段任何模型文本（那阶段只回编号）。
            selected_scope=selected_scope,
        ):
            if event.get("type") == "chunk":
                pieces.append(event.get("text") or "")
            elif event.get("type") == "done":
                done = event
        if not done:
            raise RuntimeError("重生成流式结束但没有产出 done 事件")
        text = "".join(pieces)
        instruction_injected = bool(done.get("instruction_injected"))
        gen_id = done.get("id")
        gen = db.get(Generation, gen_id) if gen_id else None
        if gen is None:
            # 引擎的 done 事件带回了落库段 id；取不到只能是引擎/库出了问题，
            # 判失败而不伪装成功（「响应说重写了、库里还是旧文」最坏）。
            raise RuntimeError("重生成结束但未能取回目标段")
        _record_generation_evidence(db, run_id, gen, text)
        writer_model = _writer_model_label()
        run.status = "success"
        run.finished_at = _now_utc()
        run.model = writer_model
        db.commit()
        # 澄清行一次性收口：正文已按选定区间落库，旧候选不再可用。
        # best-effort：留痕失败不影响已经成功的正文（与闭环同一纪律）。
        if body.clarification_id is not None:
            from app.agents.dialog.snapshot import mark_clarification_clarified

            mark_clarification_clarified(db, body.clarification_id)
        # AI 腔反馈闭环（旁路）：与 generate_next 分支同一函数，不各写一遍
        # （两处各写一遍必漂移是这个项目的历史教训）。
        segment_out = _segment_out(gen)
        # 闭环检查的是**已落库正文**（gen.text），不是流式拼接的原文 text：
        # 引擎落库前做过 _strip_thinking（engine.regenerate_segment_stream），
        # provider 吐思维链时原文里的 AI 腔模式会被闭环当成正文问题写进记忆、
        # 再进提示词——幻记（QA F3）。generate_next 分支传 result["text"]，
        # 与落库同源，不受此影响。
        flavor_feedback = _run_ai_flavor_feedback(
            db, project, gen.text, body.segment_no, body.instruction
        )
        # 回答只在指令**确实进了 prompt** 时才声称「已按你的指令」；
        # 空指令保持改动前的原文案（逐字一致），带指令但未注入则如实说明。
        if (instruction or "").strip() and not instruction_injected:
            answer = (
                f"已重写第 {body.segment_no} 段"
                f"（{len(text)} 字，Writer 模型 {writer_model}），"
                "但你的重写要求没能进入这次生成，建议重试这一段。"
            )
        else:
            answer = (
                f"已按你的指令重写第 {body.segment_no} 段"
                f"（{len(text)} 字，Writer 模型 {writer_model}）。"
            )
        return {
            "answer": answer,
            "agent_run_id": run_id,
            "action": body.action,
            "writer_model": writer_model,
            "segment": segment_out,
            "chars": len(text),
            "summary": done.get("summary") or "",
            "summary_tree": done.get("summary_tree") or {},
            # 指令是否真的进了发出的 prompt（空指令恒为 False，与改前一致）。
            "instruction_injected": instruction_injected,
            # 局部指令 vs 整段重写的提示（含公共前后缀长度/字数变化）。
            # None = 未命中；命中也只提示，不拦截、不重试（结果已落库）。
            "scope_mismatch": done.get("scope_mismatch"),
            # 篇幅跌幅 >20% 的提示（与 scope_mismatch 分列；用户要求精简时为 None）。
            "length_drop": done.get("length_drop"),
            # 句子级补丁明细（仅局部指令路径）：锚点/替换长度、补丁句数。
            # 整段重写路径为 None。
            "patch": done.get("patch"),
            # 闭环摘要（旁路观测）：与 generate_next 分支同键同形状，
            # 异常/未达记忆线时为 None。前端据此提示「本次已沉淀 AI 腔教训」。
            "ai_flavor_feedback": flavor_feedback,
            # 复读闸门（D7）：与 generate_next 分支同键同形。
            "repetition": done.get("repetition"),
            "length": done.get("length"),
            "summary_style": done.get("summary_style"),
            "warnings": done.get("warnings") or [],
            # 撤销入口：这段正文现在可以退回到 snapshot 的 before_text。
            "snapshot_id": snapshot.id if snapshot is not None else None,
            "batch_id": snapshot.batch_id if snapshot is not None else None,
            # 本次改写来自哪次澄清（None = 非澄清路径）。
            "clarification_id": body.clarification_id,
        }
    except Exception as exc:  # noqa: BLE001 - 生成失败必须留痕，不静默 200
        logger.warning("invoke-generation 失败（project=%s）：%s", project.id, exc)
        # 快照留痕（正文未变）：模型报错/超时/中断时把 pending 置 failed。
        # 引擎在流式中断时已标过一次，这里是兜底（只对 pending 生效，幂等）。
        if snapshot is not None:
            from app.agents.dialog.snapshot import fail_snapshot

            fail_snapshot(db, snapshot.id, type(exc).__name__)
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = writer_model or "(unknown)"
        run.note = f"这次生成是助手调用的（失败）：{str(exc)[:120]}"
        db.commit()
        # 并发改正文（另一个入口在生成期间写过该段）→ 409，不是 500。
        from app.agents.dialog.snapshot import ConcurrentEdit

        if isinstance(exc, ConcurrentEdit):
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc),
                        "segment_no": getattr(exc, "segment_no", body.segment_no)},
            ) from exc
        # 局部补丁失败（锚点找不到 / 多处命中 / 返回脏了）→ 422 + 可解释原因。
        # 引擎不回退整段重写，正文一行未动；调用方可原样重试或改成整段指令。
        from app.generation.patch import PatchError

        if isinstance(exc, PatchError):
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": str(exc),
                        "segment_no": body.segment_no},
            ) from exc
        # 篇幅硬上限：压缩后仍超 120% → 422 + blocked 明细，正文一行未落。
        from app.generation.length_guard import LengthBlocked

        if isinstance(exc, LengthBlocked):
            raise HTTPException(
                status_code=422, detail=exc.to_dict()) from exc
        # 时间线规划闸门：引擎已给出可解释的 409（needs_planning / needs_review），
        # 保留状态码与结构化 detail，不降级成 500。
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=500, detail=f"助手调动生成失败：{str(exc)[:200]}"
        )


def _segment_out(gen: Generation) -> dict:
    """生成段出参（与 GET /api/generations/{pid} 的形状一致，前端复用）。"""
    return {
        "id": gen.id,
        "project_id": gen.project_id,
        "segment_no": gen.segment_no,
        "chapter_range": gen.chapter_range,
        "text": gen.text,
        "summary": gen.summary,
        # 与 GET /api/generations/{pid} 同形：NULL → verdict="undetected"。
        "quality": parse_quality(gen.quality_json),
    }
