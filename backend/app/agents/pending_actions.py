"""待确认动作（pending action）的字段契约与构造器。

## 为什么单独成模块

助手页瘦身后只有一条对话流。agent 在回答里附上「结论 + 证据」，遇到需要
**写操作**（提提案 / 调动生成）时不自己动手，而是在对话流里插一张**待确认
卡片**；用户点确认后，前端拿卡片里服务端给好的 `endpoint` + `method` + `body`
原样发请求。

两个结构性理由让本模块独立于 `app/routers/assistant.py`：

1. **静态扫描约束**：`tests/test_assistant_readonly.py::test_assistant_router_has_no_write_path`
   对 `routers/assistant.py` 做源码级断言，要求 `"/proposals"` 与 `"/versions"`
   两个子串**不出现在该文件里**（assistant router 不得暴露 EditProposal /
   Version 写入端点）。把端点表放进 `assistant.py` 会直接打破这条既有回归。
2. **方向**：本模块只被 router 单向依赖，不 import 任何 router / 业务模块，
   与 `assistant_shared` 的分层约定一致。

## 契约（服务端负责拼好请求体）

```python
{
    "kind": "proposal" | "invoke_generation",
    "title": "一句话说明要做什么",
    "detail": {...},        # 前端渲染所需的全部字段（前端不自己拼请求）
    "endpoint": "/api/assistant/proposals",
    "method": "POST",
    "body": {...},          # 完整的请求体，前端原样回传
}
```

**服务端负责把请求体拼好**，前端不参与构造——避免前后端各拼一半导致漂移。

## 纪律：只「提出」，不执行

本模块**不**发任何 HTTP 请求、**不**碰 db。写操作的唯一执行路径仍是用户点
确认后由前端调既有端点（`POST /api/assistant/proposals` 创建 pending 提案，
再走 approve → apply；`POST /api/assistant/invoke-generation` 调动生成）。
`regenerate_segment` 会覆盖已有段落，属于危险动作，卡片用
`detail.destructive=True` + 文案双重标注，让前端能与「新建提案」区分开。

本轮只实现 `invoke_generation` 的**提出**：`proposal` 类型的 body 绕不开
`after_text`——那是用户要敲下的新正文，服务端替用户写就是越权
（见 `assistant_proposals.CreateProposalRequest` 的红线之二）。因此
`proposal` 的构造器保留并被测试覆盖，但**本轮不由 `/qa` 自动提出**。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

# ====================================================================
# 契约常量
# ====================================================================

#: 卡片 `kind` 的合法取值。前端按此分派渲染；新增类型必须同时补端点表。
#:
#: `choice` 是例外：它的端点**由决策点决定**（蒸馏档位打蒸馏端点、段长打项目
#: 端点…），所以它不在下面的 kind→端点表里 —— 端点仍然只由服务端给出
#: （决策点注册表 `app.agents.decision_points`），前端不参与构造。
PENDING_ACTION_KINDS: tuple[str, ...] = (
    "proposal", "invoke_generation", "ingest_attachment", "choice",
    "action_proposal")

#: kind → 用户确认后前端实际调用的端点。
#:
#: 唯一真源。卡片里的 `endpoint` 只能从这里取，不接受调用方传入——
#: 否则「agent 说要提案、前端打到 /invoke-generation」这类漂移无从发现。
_ENDPOINT_BY_KIND: Mapping[str, str] = {
    "ingest_attachment": "/api/assistant/attachments/ingest",
    "proposal": "/api/assistant/proposals",
    "invoke_generation": "/api/assistant/invoke-generation",
    # 动作提案：确认后**创建**一条待批的动作提案行（不是执行它）——
    # 执行仍然是人再点一次「批准 / 执行」之后的事。
    "action_proposal": "/api/assistant/action-proposals",
}

#: kind → HTTP 方法。本轮两类都是 POST（都创建新资源，不改既有资源）。
_METHOD_BY_KIND: Mapping[str, str] = {
    "ingest_attachment": "POST",
    "proposal": "POST",
    "invoke_generation": "POST",
    "action_proposal": "POST",
}


# ====================================================================
# 构造器
# ====================================================================


def build_pending_action(
    *,
    kind: str,
    title: str,
    detail: Mapping[str, Any],
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """组装一张待确认卡片，并对每个字段 fail loud 校验。

    校验不是形式主义：卡片是**服务端替前端做的决定**，一旦 kind 与 endpoint
    对不上、body 不是 dict、title 为空，用户点确认时才会以一个语义错乱的
    请求打出去——那时排查成本远高于在这里抛错。

    Args:
        kind: `PENDING_ACTION_KINDS` 之一。
        title: 一句话说明要做什么（用户据此判断要不要点确认）。
        detail: 前端渲染所需的全部字段。本函数**不**校验其内部结构
            （各 kind 自负），只要求是可 JSON 序列化的 mapping。
        body: 完整请求体，前端原样回传。

    Returns:
        符合契约的卡片 dict。

    Raises:
        ValueError: kind 未知、title 为空、detail/body 不是 mapping、
            或 body 含非 JSON 可序列化值。
    """
    if kind not in _ENDPOINT_BY_KIND:
        raise ValueError(
            f"未知的 pending action kind {kind!r}；"
            f"合法取值 {list(PENDING_ACTION_KINDS)}"
        )
    if not (title or "").strip():
        raise ValueError("pending action 的 title 不能为空：用户靠它判断要不要点确认")
    if not isinstance(detail, Mapping):
        raise ValueError(f"detail 必须是 mapping，收到 {type(detail).__name__}")
    if not isinstance(body, Mapping):
        raise ValueError(f"body 必须是 mapping，收到 {type(body).__name__}")

    payload = dict(body)
    try:
        import json

        json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"body 不可 JSON 序列化：{exc}") from exc

    return {
        "kind": kind,
        "title": title.strip(),
        "detail": dict(detail),
        "endpoint": _ENDPOINT_BY_KIND[kind],
        "method": _METHOD_BY_KIND[kind],
        "body": payload,
    }


# ====================================================================
# invoke_generation 卡片的提出（确定性，不调模型）
# ====================================================================
#
# 为什么用确定性关键词而不是让模型吐结构化输出：
# - 卡片会触发写操作，**误提**的代价是用户被无意义的确认弹窗打断；
#   漏提的代价只是少一次自动化（用户仍可自己发起），两者不对称 → 宁可漏不可错。
# - `AssistantInvokeGenerationRequest` 要求 `instruction` 非空且是
#   「用户原话/意图」。我们直接把用户 query 原样回传，恰好满足该契约，
#   也让审计链里的 `AgentRun.note` 留着用户自己的措辞。

#: 触发「重写已有段」的动词。命中后必须同时解析出段号，否则不提卡
#: （段号缺失 → 前端确认后必然 404，提卡等于骗用户点一次必然失败的按钮）。
_REWRITE_MARKERS: tuple[str, ...] = (
    "重写",
    "重生成",
    "重新生成",
    "重新写",
    "改写",
    "重做",
    "重写一遍",
)

#: 触发「续写下一段」的动词。刻意不含单字「写」——「这段写得好吗」是提问，
#: 不是指令；宁可漏提。
_CONTINUE_MARKERS: tuple[str, ...] = (
    "续写",
    "接着写",
    "继续写",
    "继续生成",
    "写下一段",
    "生成下一段",
    "下一段",
)

#: 阿拉伯数字段号：`第 3 段` / `第3段` / `segment 3` / `seg=3`。
_SEGMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*(\d+)\s*段"),
    re.compile(r"segment[\s_]*(\d+)", re.IGNORECASE),
    re.compile(r"seg\s*[=:]\s*(\d+)", re.IGNORECASE),
)

#: 汉字数字段号：`第三段` / `第十段`。中文语境下比阿拉伯数字更常见。
_CN_SEGMENT_PATTERN = re.compile(r"第\s*([零一二两三四五六七八九十]{1,3})\s*段")

_CN_DIGITS: Mapping[str, int] = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number_to_int(text: str) -> int | None:
    """汉字数字（1-99）→ int；无法解析时返回 None。

    只覆盖到 99：段号超过 99 的中文写法（第一百零三段）在现实里不存在，
    为它维护一套解析器不值得。解析不了就不提卡（fail closed）。
    """
    if not text:
        return None
    if "十" not in text:
        total = 0
        for ch in text:
            if ch not in _CN_DIGITS:
                return None
            total = total * 10 + _CN_DIGITS[ch]
        return total
    head, _, tail = text.partition("十")
    tens = 1 if head == "" else _CN_DIGITS.get(head)
    if tens is None:
        return None
    if tail == "":
        return tens * 10
    if len(tail) != 1 or tail not in _CN_DIGITS:
        return None
    return tens * 10 + _CN_DIGITS[tail]


def extract_segment_no(query: str) -> int | None:
    """从用户 query 里解析段号；解析不出返回 None。

    多个模式命中时取**最小**的段号：用户说「对比第 2 段和第 5 段」是在提问，
    不是在要求重写——真要重写时只会指一段。取最小只是确定性的兜底，
    真正的闸门是 `_REWRITE_MARKERS` 必须同时命中。
    """
    found: list[int] = []
    for pattern in _SEGMENT_PATTERNS:
        for match in pattern.finditer(query or ""):
            found.append(int(match.group(1)))
    for match in _CN_SEGMENT_PATTERN.finditer(query or ""):
        value = _cn_number_to_int(match.group(1))
        if value is not None:
            found.append(value)
    return min(found) if found else None


def collect_text_edit_cards(tool_results: Mapping[str, Any] | None,
                             project_id: int) -> list[dict[str, Any]]:
    """从工具结果里把「改写」卡收进对话流（只收**可信**的那些）。

    与 `collect_body_repair_cards` 同一套口径：只认产出改写卡的工具
    （`propose_text_edit` / `propose_text_fix` —— 两者都只**构造**卡，不落库）、
    端点必须是提案创建端点、body 里的 project_id 必须是本项目
    —— 跨项目的卡一律不收（提一张必然失败的卡 = 骗用户点一次）。

    ## 为什么按「工具名前缀」收而不是写死一个名字

    起草改写的工具有两个（自己起草 / 按规则修标点），它们产出的是**同一种卡**。
    写死名字的话，agent 换用另一个工具，这张卡就**静默丢了** ——
    实测踩过：agent 调了 `propose_text_fix`，卡生成了，用户却什么也没看见
    （对话里说「请在待确认卡中确认」，而界面上没有卡）。
    """
    cards: list[dict[str, Any]] = []
    for key, value in (tool_results or {}).items():
        name = str(key).split(':', 1)[0]
        if not name.startswith('propose_') or not isinstance(value, dict):
            continue
        action = value.get('pending_action')
        if (isinstance(action, dict) and action.get('kind') == 'proposal'
                and action.get('endpoint') == '/api/assistant/proposals'
                and action.get('method') == 'POST'
                and (action.get('body') or {}).get('project_id') == project_id):
            cards.append(action)
    return cards


def build_text_edit_proposal_action(
    *,
    project_id: int,
    proposal_type: str,
    target_kind: str,
    target_id: str,
    draft: str,
    reason: str = "",
    risk_level: str | None = None,
    from_issue: str | None = None,
    evidence: list[Mapping[str, Any]] | None = None,
    target_chars: int = 0,
) -> dict[str, Any]:
    """组装一张「改写」待确认卡（**不落库**），带一份**可编辑的草稿**。

    ## 为什么有草稿

    提案的 `after_text` **必须由用户填写** —— 服务端替用户写新正文就是越权
    （`assistant_proposals.py` 的红线二）。但 agent 能起草「改哪儿、改成什么」
    的大致样子，把它给用户当**草稿**，用户在卡上改或原样确认 —— 落库的是
    **用户确认时看到的那一版**，不是 agent 的原版。红线因此没破。

    `detail.draft_field` 告诉前端「body 里哪个字段是可编辑的草稿」——
    前端只覆盖**用户输入的那一个字段**，协议字段仍由服务端下发。
    """
    if not (draft or "").strip():
        raise ValueError('草稿不能为空：改写提案要说明改成什么')
    body: dict[str, Any] = {
        "project_id": int(project_id),
        "type": str(proposal_type),
        "target_kind": str(target_kind),
        "target_id": str(target_id),
        "after_text": str(draft),
        "reason": str(reason or ""),
    }
    if risk_level:
        body["risk_level"] = str(risk_level)
    if from_issue:
        # 关联的诊断结论：服务端已经按 issue id 取好了建议与证据，
        # 这里只把引用带上（写路径会用它再核一遍）。
        body["from_issue"] = str(from_issue)
    if evidence:
        body["evidence"] = [dict(e) for e in evidence]
    return build_pending_action(
        kind="proposal",
        title="改写一段内容（草稿可以先改）",
        detail={
            "type": str(proposal_type),
            "target_kind": str(target_kind),
            "target_id": str(target_id),
            "draft_field": "after_text",
            "draft_editable": True,
            "draft_notice": "这是助手的草稿，可以直接改；确认后才会存成待批提案。",
            "requires_confirmation": True,
            "from_issue": str(from_issue) if from_issue else "",
            # 原文多长：用户要能一眼看出「这一改是不是把大半段删了」。
            "target_chars": int(target_chars or 0),
        },
        body=body,
    )


def build_action_proposal_action(
    *,
    project_id: int,
    kind: str,
    title: str,
    detail: Mapping[str, Any] | None = None,
    action_endpoint: str,
    action_method: str = "POST",
    action_body: Mapping[str, Any] | None = None,
    params: Mapping[str, Any] | None = None,
    risk_level: str = "low",
    options: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """组装一张「动作提案」待确认卡（**不落库**）。

    这张卡的语义是「要不要提这个动作」：用户点确认 → 前端把 body 原样发给
    `/api/assistant/action-proposals` → 服务端**创建**一条 pending 的动作提案行。
    执行那个动作是**再点一次**的事（批准 → 执行），仍然不合成一键。

    为什么要「提」和「做」分成两次点击：审批是人的判断，执行是系统的动作；
    合成一次就取消了「先看清楚要做什么再决定」这一步。

    `options`：**需要用户决断的参数**（蒸馏用哪一档…）做成选项，跟这张卡
    合在一起而不是弹两张卡 —— 两张卡要人来对应，顺序一错就批错动作。
    每个选项的 `body_patch` 必须是**嵌好的**（作用在请求体的哪个位置由服务端
    决定），前端只做「合并 + 发送」，不参与构造。
    """
    body = {
        "project_id": int(project_id),
        "kind": str(kind),
        "title": str(title),
        "detail": dict(detail or {}),
        "action_endpoint": str(action_endpoint),
        "action_method": str(action_method),
        "action_body": dict(action_body or {}),
        "params": dict(params or {}),
        "risk_level": str(risk_level),
    }
    card_detail: dict[str, Any] = {
        "action_kind": kind,
        "risk_level": risk_level,
        "action_endpoint": action_endpoint,
        "requires_confirmation": True,
        **dict(detail or {}),
    }
    if options:
        card_detail["options"] = [dict(o) for o in options]
    return build_pending_action(
        kind="action_proposal",
        title=title,
        detail=card_detail,
        body=body,
    )


def propose_invoke_generation(
    *,
    project_id: int,
    query: str,
    has_segment: Callable[[int], bool],
) -> dict[str, Any] | None:
    """用户话里带明确生成指令时，提出一张 `invoke_generation` 待确认卡片。

    判定顺序（顺序本身是不变量）：

    1. **先查重写**：命中 `_REWRITE_MARKERS` 时必须解析出段号，且该段必须
       真实存在——否则不提卡。理由：`/invoke-generation` 对不存在的段号
       返回 404，提一张必然失败的卡等于骗用户点确认。
    2. **再查续写**：命中 `_CONTINUE_MARKERS` 且没有可解析的段号时，提
       `generate_next` 卡（段号由引擎分配，请求体里不能带 `segment_no`——
       `AssistantInvokeGenerationRequest` 的校验器会 422）。
    3. 都不命中 → 返回 None，调用方得到空列表。

    `has_segment` 由调用方注入（router 有 db，规则模块没有）：本模块保持
    纯函数、可离线测试，也避免在「判定是否提卡」这条路径上引入 db 依赖。

    Args:
        project_id: 路由层已校验存在的项目 id。
        query: 用户原话，原样进 `instruction`（审计链需要它）。
        has_segment: 段号 → 该段是否存在于本项目。

    Returns:
        卡片 dict，或 None（不该提卡）。
    """
    text = (query or "").strip()
    if not text:
        return None

    segment_no = extract_segment_no(text)

    if any(marker in text for marker in _REWRITE_MARKERS):
        if segment_no is None:
            # 「重写一下」但没说哪段 → 不提卡。前端确认后必然 422。
            return None
        if not has_segment(segment_no):
            # 段不存在 → 确认后必然 404，同样不提。
            return None
        body: dict[str, Any] = {
            "project_id": project_id,
            "action": "regenerate_segment",
            "segment_no": segment_no,
            "instruction": text,
        }
        return build_pending_action(
            kind="invoke_generation",
            title=f"重写第 {segment_no} 段（会覆盖该段现有正文，确认后才执行）",
            detail={
                "action": "regenerate_segment",
                "segment_no": segment_no,
                "project_id": project_id,
                # 危险动作标记：前端据此与「新建提案」做视觉区分，
                # 且必须配合文案（不能只靠颜色单独传达，见前端双编码约定）。
                "destructive": True,
                "risk_hint": "该段现有正文将被替换；git 可回滚，但请先确认内容",
                "instruction": text,
            },
            body=body,
        )

    if any(marker in text for marker in _CONTINUE_MARKERS) and segment_no is None:
        body = {
            "project_id": project_id,
            "action": "generate_next",
            "instruction": text,
        }
        return build_pending_action(
            kind="invoke_generation",
            title="续写下一段（确认后才执行）",
            detail={
                "action": "generate_next",
                "segment_no": None,
                "project_id": project_id,
                "destructive": False,
                "risk_hint": "新增一段，不覆盖任何现有正文",
                "instruction": text,
            },
            body=body,
        )

    return None