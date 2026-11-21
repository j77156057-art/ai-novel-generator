"""实验性诊断的**用户授权**：凭据只能来自用户自己的话。

## 为什么要有这个模块

「实验性技能必须经用户明确同意才能用」这条纪律原先只写在工具描述与
`authorization_note` 里 —— 而 `allow_experimental` 是**模型自己传的参数**，
`diagnostic_tool` 直接把它透传给 `load_skill`。也就是说：模型说一声
「用户同意了」就能跑实验技能。纪律形同虚设。

## 凭据是什么

**用户这条消息的原话**。模型伪造不了它 —— 请求体里的 `query` 来自客户端，
不是模型写的。所以服务端只要在**用户的消息**里找明确授权，就能得到一个
模型绕不过去的凭据。

## 宁可漏，不可误

检测是**保守**的：必须同一分句里同时出现「同意类动词」和「实验」，
且该分句没有否定词。漏了会怎样？agent 会被拒一次，并把该说的话转述给用户
（拒绝理由里写清了要用户怎么说）；误判会怎样？模型就能自己放行实验技能 ——
那正是这道闸门要防的。所以方向是**宁可漏、不可误**。
"""
from __future__ import annotations

import re
from contextvars import ContextVar, Token

#: 用户明确同意的说法（动词）。
_CONSENT_VERBS: tuple[str, ...] = ("同意", "允许", "授权", "准许")

#: 明确指向「实验性技能」的词（强信号）。
_EXPERIMENT_WORDS: tuple[str, ...] = ("实验性", "试验性")

#: 「实验」要和这些词之一同时出现才算在说实验技能（弱信号）。
#: 「这段实验报告你同意吗」不是在授权实验技能 —— 弱信号要求带上"技能/诊断/能力"。
_EXPERIMENT_CONTEXT: tuple[str, ...] = ("技能", "诊断", "能力")

#: 否定词 —— 出现即**不**算同意。
_NEGATIONS: tuple[str, ...] = (
    "不同意", "不允许", "不授权", "不准", "不要", "不能", "别用", "别开",
    "禁止", "无需", "不必", "不用", "暂不", "先不", "没有同意", "没同意",
)

#: 分句边界。
#:
#: **只按句末标点断**（逗号不断）：授权是「就这件事」的表态，
#: 「实验性诊断，我同意了」是同一句里的一整件事 —— 按逗号断会把同意词和
#: 实验词拆散，真实的授权反而认不出来。否定词在**整句**里查，
#: 于是「不要用实验技能，同意用普通技能」这种整句否定的说法被正确拒掉。
_CLAUSE_SPLIT = re.compile(r"[。;；!！?？\n\r]+")

#: 本次请求里「用户是否授权了实验性诊断」。
#:
#: 用 ContextVar 而不是参数往下传：它是**服务端算出来的事实**，不该出现在
#: 工具参数里（模型能传的参数就等于模型能控的开关）。
_CONSENT: ContextVar[bool] = ContextVar("user_experimental_consent", default=False)

#: 拒绝时给执行方（agent）的话：说清缺什么、用户该怎么说。
EXPERIMENTAL_NOT_AUTHORIZED: str = (
    "实验性诊断技能需要**用户明确授权**，本次请求里没有找到。"
    "请让用户在消息里明确说一句「同意使用实验性诊断技能」后再试；"
    "或者改用不需要实验授权的技能。"
)


def user_granted_experimental(query: str | None) -> bool:
    """用户在这条消息里是否明确同意使用实验性诊断。"""
    text = str(query or "")
    if not text.strip():
        return False
    for clause in _CLAUSE_SPLIT.split(text):
        if not clause.strip():
            continue
        if any(neg in clause for neg in _NEGATIONS):
            continue
        if not any(verb in clause for verb in _CONSENT_VERBS):
            continue
        strong = any(word in clause for word in _EXPERIMENT_WORDS)
        weak = ('实验' in clause
                and any(ctx in clause for ctx in _EXPERIMENT_CONTEXT))
        if strong or weak:
            return True
    return False


def set_experimental_consent(granted: bool) -> Token:
    """在本次请求的上下文里记下授权结果（返回 token，用于还原）。"""
    return _CONSENT.set(bool(granted))


def reset_experimental_consent(token: Token) -> None:
    try:
        _CONSENT.reset(token)
    except ValueError:  # pragma: no cover - 跨上下文还原（不该发生）
        _CONSENT.set(False)


def experimental_consent_granted() -> bool:
    """本次请求里用户是否授权了（服务端算的，模型改不了）。"""
    return bool(_CONSENT.get())
