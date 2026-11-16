"""把 build_context 的输出按 profile 裁剪。

## 为什么单独一层

`build_context` 负责「能拿到什么」，这个模块负责「这次该看到什么」。
把它拆开而不是在 build_context 里到处 if profile == ...，是因为：

1. build_context 有 5 个消费方（generate / stream / loop / plan / regenerate），
   每个都要按 profile 裁剪，散着写必然漏
2. 裁剪规则本身需要单测，藏在 build_context 中间测不到
3. 将来加第四种模式，只改这里

## 本模块实现了什么

1. **原创模式关掉检索上下文与原文样例**（P0-A）。实测原始 prompt 里这两块占 87%
   （4839 + 1144 字），把「写到哪了」（前情提要 255 字）压到 3.8%，直接导致：
   - 大纲写「北漠」正文写「北莽」（世界观被替换）
   - 段 1/3/4/5 全部从「风沙刮过横水城城墙」重启（跨段重写）
2. **脱敏样例**（`redacted`）：专名换成等长占位符，保留句法/节奏/标点。
   已有专名表就用；**没有可用的专名表就不给样例**（给原文等于把参考书世界观
   灌进用户的小说，比不给更糟）。

## 还没做的

**人物塑造模板**（`include_character_template`）只声明了、**没有消费方**：
`profiles.py` 写着它为 True，本模块把它塞进 ctx 的 `_include_character_template`，
但全仓没有任何地方读它（`narrative.describe_narrative` 的注释里说「P0-B 实现」
—— P0-B 未落地）。

后果：原创模式拿到 method 层手法 + 脱敏样例 + 统计指纹，**唯独没有「怎么塑造
人物」的方法论** —— 那正是模板该补的那块。现状是「声明了但没接线」。
"""
from __future__ import annotations

from typing import Any

from app.generation.profiles import normalize_profile, rules_for


def apply_profile(
    ctx: dict[str, Any],
    profile: str | None,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """按 profile 裁剪 build_context 的产物。

    只做**减法**：删掉这次不该看见的东西，不新增任何内容。
    因此这个函数永远不会让上下文变得「比原来更脏」。

    返回新 dict（不原地改，避免调用方持有的旧引用被静默影响）。
    warnings 收集被裁掉了什么，供 /generate-plan 展示——用户要能看见
    「为什么这次没有检索上下文」，否则会以为功能坏了。
    """
    prof = normalize_profile(profile)
    rules = rules_for(prof)
    out = dict(ctx)
    w = warnings if warnings is not None else []

    # ── 检索上下文：只有「剧情检索」才写进 rag_context ─────────
    #
    # v2.293：rag 的取值从 bool 扩成三态：
    #   False       不检索（原创）
    #   True        剧情检索（续写）→ 结果进 rag_context
    #   "character" 角色定向检索（角色聊天）→ 结果进 rag_items，
    #               **不进** rag_context
    #
    # 所以这里清空的条件是「不是剧情检索」，而不是「falsy」。
    # 角色聊天走 `not True` → 清空剧情上下文，但它自己的角色片段
    # 由 build_context 放进 rag_items，由 prompts 分块渲染。
    if rules.get("rag") is not True:
        had = len(ctx.get("rag_context") or [])
        if had:
            chars = sum(len(t) for t in (ctx.get("rag_context") or []))
            w.append(
                f"[{prof}] 已关闭检索上下文：原 {had} 条 / {chars} 字。"
                "参考语料的原文会把它的世界观与人物带进本书，"
                "而「用本书剧情检索参考书剧情」本身并不成立。"
            )
        out["rag_context"] = []
        out["rag_has_forbidden"] = False

    # ── 风格样例：按 profile 处理 ────────────────────────────
    mode = rules.get("style_exemplars")
    if mode is False or mode is None:
        out["style_exemplars"] = []
    elif mode == "redacted":
        # 脱敏样例：保留原文的句法、节奏、标点习惯，只把专有名词换成等长占位符。
        #
        # 为什么必须给样例：实测句长是文风里唯一「数字指令完全无效」的指标。
        # 指令写 60.3±20.8，不给样例时生成 14.6 字（−76%），给了原文时 18.0。
        # 统计数字对模型没有约束力，只有可读样例能承载「长句怎么断」。
        #
        # 为什么必须脱敏：原文自带世界观。实测大纲写「北漠」，正文写「北莽」16 次。
        # 脱敏把专名换成等长占位符（＃1 / ＃1＃），模型学得到句法，学不到名字。
        examples = ctx.get("style_exemplars") or []
        table = ctx.get("entity_table") or {}
        from app.style.entity_store import entity_entries
        if not examples:
            out["style_exemplars"] = []
        elif not entity_entries(table):
            # 没有**可用的**专名表就不给原文——给了等于把参考书世界观灌进来，比不给更糟。
            # 注意不能只看表是不是空字典：表的元数据键会让空表看起来"有内容"，
            # 而 Redactor 对空表是空操作（原文原样返回）。
            w.append(
                f"[{prof}] 风格样例已移除：专名表无可用实体（未构建或判定失败），无法脱敏。"
                "原文样例会把参考语料的世界观带进本书（实测大纲写「北漠」、"
                "正文写「北莽」）。文风暂由统计指纹与叙事手法承载。"
            )
            out["style_exemplars"] = []
        else:
            from app.style.entities import Redactor

            red = Redactor(table)
            redacted = []
            for ex in examples:
                text = str(ex.get("text") or "")
                if not text:
                    continue
                new_ex = dict(ex)
                new_ex["text"] = red.redact(text)
                new_ex["redacted"] = True
                redacted.append(new_ex)
            out["style_exemplars"] = redacted
            if redacted:
                w.append(
                    f"[{prof}] 风格样例已脱敏：{len(redacted)} 条，"
                    f"替换 {red.size} 个专名为等长占位符（保留句法/节奏/标点，"
                    "去掉参考语料的人物地名门派物件）。"
                )
    elif mode == "single_character_lines":
        # 角色聊天：只留被扮演角色的台词（上游 _filter_single_character 处理）
        out["style_exemplars"] = [
            ex for ex in (ctx.get("style_exemplars") or [])
            if ex.get("character_filtered")
        ]

    # ── 角色声音词典：原创不给 ──────────────────────────────
    # 它在叙事档案里，由 build_system_prompt 按 rules["include_voice"] 控制，
    # 这里把「要不要」放进 ctx 供下游读取，避免两处各判一次导致不一致。
    out["_include_voice"] = bool(rules.get("include_voice"))
    out["_include_character_template"] = bool(rules.get("include_character_template"))
    out["_profile"] = prof
    return out
