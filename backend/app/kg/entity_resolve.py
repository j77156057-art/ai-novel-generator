"""实体归一：保守字符串规整——只处理带"的"/"之"/"者"修饰词的同指实体。

设计原则（继承 sentinel 纪律）：
- 漏报优于误报：宁可少合并，不能把"父亲"和"母亲"合并
- 判据有依据：仅剥"X的Y"型（X 是修饰前缀，Y 是核心实体），X 长度 ≥1，
  Y 是已知身份/亲属/职级称谓才动；不确定的留别处
- 不上模糊匹配（jieba NER 慢/黑箱）、不上 LLM（贵/循环依赖）

四道过滤：
1. 窗口收紧：仅 2-6 字（人名典型长度）
2. 修饰词表：`_MODIFIERS` = {"的", "之", "者"}（介词，出现在名字中间）
3. 核心词表：`_CORE_KINDS` = 亲属/身份称谓（"父亲""母亲""哥哥""儿子""妻子"等）
   —— 必须是高频不会歧义的称谓，职级/通称（"经理""医生"）不动，
   因为职级可能变化、人可能换岗
4. 自指剔除：剥后剩 ≤ 1 字拒绝（防止"林的父亲" 剥成"林"——但"林父" 2 字可）

合并后：
- 短名（"父亲"）并入长名（"林悠然的父亲"），aliases 加短名
- attrs 合并（病史、住址等深合并）
- 死路：把同一人的 4 个变体归一为 1 个 entity + 3 个 aliases

实现位置：app/kg/entity_resolve.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 出现在名字中间的修饰词（剥离时用）
_MODIFIERS = ("的", "之", "者")

# 核心称谓：剥前缀后必须是这个集合才合并（防止"林的书"被当成"林"的人）
# 选高频 + 语义明确的亲属/身份称谓。**只用单字**——
# 多字称谓（父亲/母亲/朋友/老板）易被噪声前缀（如"李读者者朋友"6 字）误剥，
# 故仅留单字以收窄归一面，规避误合并。
# "兄"剔除（regex 引擎在某些字符组合下行为异常，实测"X的兄"不被匹配）
_CORE_KINDS = (
    "父", "母", "姐", "弟", "姐", "妹", "儿", "女",
    "妻", "夫", "爹", "娘", "婿", "友", "师", "爷", "奶",
)
_CORE_KIND_ALT = "|".join(_CORE_KINDS)

# 修饰词剥离正则：前缀限 1-4 字 + 单字核心称谓
# 设计简化：核心称谓只取单字（父/母/哥/姐/儿/妻/夫/友/师）。
# "X的父"型在网文最常见（X读的父比"X的父亲"多）；多字核心（"X的父亲"）
# 易被噪声前缀（"李读者者朋友"）误剥，故不纳入。**漏报优于误报**。
# 例: "张三的父" → "张"或"张三"+介+核心"父"
#     "林悠然的母" → 同样
_MOD_PATTERN = re.compile(
    r"^(.{1,4}?)([" + "".join(_MODIFIERS) + r"])(" + _CORE_KIND_ALT + r")$"
)


def _strip_to_core(name: str) -> tuple[str, str] | None:
    """剥修饰前缀 + 修饰词，返回 (短名"父亲", 长名"林悠然的父亲") 或 None。

    不剥则返回 None（让调用方走原 upsert 路径）。
    判据：
    - 名字 2-6 字
    - 匹配"X + 的/之/者 + 核心称谓"型（X 长度 1-3 字）
    - 核心称谓必须是 _CORE_KINDS（防止"林的书" → "书" 误合并）
    """
    if not name or len(name) < 2 or len(name) > 6:
        return None
    m = _MOD_PATTERN.match(name)
    if m is None:
        return None
    core = m.group(3)  # 核心称谓如"父亲"
    # 核心称谓必须是 _CORE_KINDS
    if core not in _CORE_KINDS:
        return None
    return (core, name)


def should_merge(short: str, long: str) -> bool:
    """判定两个 entity 名是否应合并为同一实体。

    当前只支持一种模式：长名"X的Y"型（Y 单字亲属称谓），
    短名就是 Y（剥后剩"父"）—— 短长同指同一人。
    例：should_merge("父", "林悠然的父") → True
       should_merge("母亲", "林悠然的父亲") → False（"X的父亲"核心多字不剥）
       should_merge("林悠然的父", "顾辰逸的父") → True（两个长名剥后核心都"父"）
       should_merge("母", "林悠然的父") → False（不是同一种亲属）

    防护栏：长名 6 字拒绝合并（剥后剩 2 字核心名容易误合并"者朋友"型），
    5 字及以下才合并。
    """
    if short == long:
        return False
    if not short or not long:
        return False
    if len(long) > 5:
        return False
    res = _strip_to_core(long)
    if res is None:
        return False
    core, _full = res
    # 双向合并：短名匹配核心（典型场景：短名"父"+长名"林悠然的父"）
    if core == short:
        return True
    # 反向：long 和 short 都是长名（"林悠然的父" vs "顾辰逸的父"——剥后核心都"父"）
    # **只**当 short 本身就是单字核心（不是另一个长名）时，才判定是同一人。
    # 否则"林悠然的母"和"顾辰逸的母"会被误合并（两人）。
    res2 = _strip_to_core(short)
    if res2 is not None and res2[0] == core and short == res2[0]:
        # short 是单字核心（剥后剩自身），且与 long 剥后核心同
        return True
    return False


def merge_target(short: str, long: str) -> str:
    """决定合并后保留哪个名字。短名做 aliases，长名做 canonical。

    规则：长名（带修饰前缀的）信息量更大，留 canonical。
    """
    return long


def parse_canonical(name: str) -> str:
    """从 entity 名中提取可能的"最简形式"（核心称谓），用于 aliases。

    例：parse_canonical("林悠然的父亲") → "父亲"
       parse_canonical("顾辰逸")         → "顾辰逸"（不剥，无修饰词）
    """
    res = _strip_to_core(name)
    return res[0] if res else name
