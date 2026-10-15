"""v2.6 诊断性 B：检测白名单外的角色名（只告警，不替换）。

设计动机：
- 模型正常创作时会引入"小角色"（出场几章就消失），这些**应该**被允许
- 项目初期的"核心角色"白名单**不**可能覆盖所有角色——误伤正常创作
- 真正要拦截的是"明显走样"（如 few-shot 风格样例诱导把红楼梦薛姨妈混入现代言情）
- 这种"走样"的根因在 few-shot，**不在**白名单

结论：白名单"诊断性"——只检测+告警，**不替换**。正文保持原样。
对模型生成零影响（告警是生成后的旁路读，不反馈给模型）。

白名单收集：见 collect_allowed_names（项目锁定角色 + 剧情圣经角色）
"""
from __future__ import annotations

import re
from typing import Iterable


def _normalize_name(name: str) -> str:
    """归一化：去空白，保留原字面。

    角色名匹配用字面（不转拼音/不模糊）——避免"林"单独匹配这种误伤。
    """
    return name.strip()


def detect_unlisted_characters(
    text: str,
    allowed_names: Iterable[str],
) -> list[str]:
    """v2.6 诊断性 B：扫正文，**只返回**白名单外的角色名列表——不替换。

    严格"白名单外=可疑"——返回值供 logger / API 诊断用。
    调用方（engine.py）负责：logger.warning + 把列表塞进 return dict。
    正文**始终**原样返回（v2.6 这条只观测，不纠错）。

    启发式：候选 = 长度 2-6 的连续中文字符串（覆盖绝大多数角色名），
    排除白名单（去重后短字）剩下的就是"未在白名单中的疑似角色"。

    注意：这会有"假阳性"——把"咖啡馆见"这种普通词组也当"角色"报。
    接受假阳性的原因：
    - 用户明确接受"诊断性"语义——告警多了不可怕，怕的是漏报
    - 真要精确：用 jieba NER 切人名（半 B 完整版 = 加 jieba 依赖）

    Args:
        text: LLM 生成的正文
        allowed_names: 白名单（项目锁定角色 + 剧情圣经角色）

    Returns:
        排序去重的"未授权候选角色名"列表（按长度倒序，长名优先）

    设计取舍：
    - 不真删——避免破坏剧情连贯（误伤让正文变难看）
    - 大小写敏感——中文角色名用字面匹配
    - 短名（≤1 字）不过滤（误伤高）
    - allowed_names 为空时返回 []（无"白名单"概念，保守不报）
    """
    allowed_set: set[str] = set()
    for n in allowed_names:
        n = _normalize_name(n)
        if n and len(n) > 1:
            allowed_set.add(n)

    if not allowed_set or not text:
        return []

    # 候选：2-6 字连续中文字符串（粗启发式）
    candidates = re.findall(r"[\u4e00-\u9fff]{2,6}", text)

    # 未授权：白名单外
    unlisted: set[str] = set()
    for name in candidates:
        if name not in allowed_set:
            unlisted.add(name)

    # 按长度倒序（长名先报——更可能是真角色名）
    return sorted(unlisted, key=lambda s: -len(s))


def collect_allowed_names(
    ctx_reminders: dict | None = None,
    plot_bible: dict | None = None,
    node_prompt: str | None = None,
) -> list[str]:
    """v2.6 诊断性 B：收集所有"允许"的角色名（白名单源）。

    三个来源：
    1. ctx_reminders["characters"] —— 项目锁定角色（build_context 返回的）
    2. plot_bible["characters"] —— 剧情圣经里的角色（多字典形态）
    3. node_prompt 字符串里出现的名字（粗略，从 entry/exit 文字扫描）

    返回去重后的名字列表（保持原顺序）。
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(name: str) -> None:
        n = _normalize_name(name)
        if n and n not in seen and len(n) > 1:
            seen.add(n)
            out.append(n)

    # 来源 1：ctx_reminders["characters"]
    if ctx_reminders:
        chars = ctx_reminders.get("characters") or []
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, str):
                    _add(c)
                elif isinstance(c, dict):
                    # 形态 {name, mentions, ...} 或 {canonical, alias, ...}
                    _add(c.get("name") or c.get("canonical") or "")

    # 来源 2：plot_bible["characters"]（多种形态：list[str] / list[dict]）
    if plot_bible:
        chars = plot_bible.get("characters") or []
        if isinstance(chars, list):
            for c in chars:
                if isinstance(c, str):
                    _add(c)
                elif isinstance(c, dict):
                    _add(c.get("name") or "")

    return out
