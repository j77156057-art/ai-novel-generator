# -*- coding: utf-8 -*-
"""超长正文的压缩轮与落库前字数守卫。

从 `engine.py` 抽出的「压缩 / 字数」簇：提示词、压缩轮、最终守卫。抽出的
目的是把篇幅逻辑集中在一处（与 `length_guard.py` 相邻），engine 只保留编排。

依赖约定：`_dialogue_ratio_of` / `_stream_with_retry` / `_strip_thinking` 仍住在
`engine.py`，这里**运行时惰性取**（`_engine()`）。原因有两个：

1. 避免 `engine -> compression -> engine` 的模块级循环导入；
2. 保留测试的 monkeypatch 锚点：测试仍可 `monkeypatch.setattr(engine,
   "_stream_with_retry", ...)` / `engine._compression_round`，本模块会读到补丁后的值。
"""
from __future__ import annotations

from typing import Any

from app.style.consistency import char_count, length_limits


def _engine() -> Any:
    """惰性取 engine 模块（循环导入与 monkeypatch 兼容）。"""
    from app.generation import engine

    return engine


def _compression_prompt(text: str, max_chars: int | None,
                        dialogue_floor: float | None = None,
                        hard_limit: int | None = None) -> tuple[str, str]:
    """超长压缩轮的提示：只做一件事——压进汉字硬上限。

    与普通重写不同，这里**允许删句、删段、删可割舍的情节**（篇幅问题，
    不是「禁止删字抬段均」那个问题），但不允许新增内容、不允许改写成新情节。

    2026-11-22（真机回归）：只超上限 28 字符时，模型把 4828 压到 3910，
    对白占比从 24.8% 掉到 16.1%。两处修正：
    - 目标从 `上限×0.95` 改为「上限 − 40」，并写明**只需删超出部分**；
    - 明确「对白是主干，优先删叙述」，且压缩后对白占比不得低于原文与硬下限。

    `hard_limit`（2026-11-23）：落库前的最终守卫已经知道真正的硬上限，
    不能再让这里按 `max_chars × 120%` 现算——那会把 4800 算成 5712，
    压缩目标直接超过闸门，导致「压缩已接受、终稿仍超限」被整章拒绝。
    """
    lim = length_limits(max_chars)
    ceiling = int(hard_limit) if hard_limit else int(lim["max"])
    cur = char_count(text)
    over = max(0, cur - ceiling)
    target = max(1, ceiling - 40)
    before_ratio = _engine()._dialogue_ratio_of(text)
    floor_txt = (
        f"，且不得低于硬下限 {float(dialogue_floor):.1%}"
        if isinstance(dialogue_floor, (int, float)) else ""
    )
    system = (
        "你是长篇小说的压缩编辑。任务只有一个：把正文压到字数上限以内。\n"
        f"当前 {cur} 个字符（含标点），硬上限 {ceiling} 个字符，"
        f"只需删约 {over + 40} 个字符，压到约 {target} 个即可；"
        "**不要大幅删减**。\n"
        "1. 允许删句、删段、删掉可以割舍的过场与次要情节；这是篇幅压缩，不是改写。\n"
        "2. 不允许新增任何内容，不允许扩写、补写、改写成新情节。\n"
        f"3. **对白是本章的主干**：优先删重复叙述、无信息量的动作与环境描写、"
        f"重复的情绪描写；关键对白、转折、伏笔、本章必须发生的事件要保留。"
        f"压缩后对白字占比不得低于原文（当前 {before_ratio:.1%}）{floor_txt}。\n"
        "4. 只输出压缩后的正文，不要输出任何解释或标记。"
    )
    return system, f"请压缩以下正文：\n\n{text}"


def _compression_round(provider, text: str, max_chars: int | None,
                       dialogue_floor: float | None = None,
                       hard_limit: int | None = None) -> tuple[str, dict]:
    """两轮重写仍超限时追加的一次压缩轮；返回 (候选正文或原文, 留痕记录)。

    接受条件（2026-11-22 起）：
    1. **字符数比原文少**（这是压缩的目的）；
    2. **不能压过头**：原文未超限时不少于原文的 85%；原文已超限时地板
       按「硬上限的 85%」算（按原文算会让 6458→4700 被误判压过头）；
    3. **对白不能先被删**：对白字占比不低于原文的 95%，且原文达标时不低于硬下限；
    4. **必须真的压进上限**（2026-11-23）：仍超上限的候选一律拒收，
       不能把「少了一点但仍然超限」当成压缩成功——那正是 5844 > 4800
       被写库闸整章拒绝的直接原因。
    第 2/3/4 条不过就换更严格的提示重试一次；仍不过保留原文。
    """
    engine = _engine()
    limit = length_limits(max_chars)
    # 硬上限：最终守卫传入的 `hard_limit` 优先；其余调用沿用 `max_chars × 120%`。
    ceiling = int(hard_limit) if hard_limit else int(limit["max"])
    before = char_count(text)
    before_ratio = engine._dialogue_ratio_of(text)
    record: dict = {
        "round": "compression", "kind": "compression",
        "chars_before": before, "limit": ceiling,
        "dialogue_before": round(before_ratio, 4),
    }
    if before <= ceiling:
        min_after = max(int(ceiling * 0.85), int(before * 0.85))
    else:
        min_after = int(ceiling * 0.85)
    attempts: list[dict] = []
    for attempt in range(2):
        try:
            system, user = _compression_prompt(
                text, max_chars, dialogue_floor,
                hard_limit=ceiling if hard_limit else None,
            )
            if attempt:
                last_reasons = attempts[-1].get("reasons") or []
                must_delete_more = any(
                    r == "no_reduction" or str(r).startswith("still_over_limit")
                    for r in last_reasons
                )
                if must_delete_more:
                    system += (
                        "\n上一次仍超过上限：继续删，必须压到上限以内；"
                        "优先删重复叙述与无信息量的动作/环境描写，关键对白保留。"
                    )
                else:
                    system += (
                        "\n上一次压缩删得过多/删到了对白。这一次只删超出上限的部分，"
                        "保留所有关键对白；对白占比必须不低于原文。"
                    )
            rewritten = engine._strip_thinking(
                "".join(engine._stream_with_retry(provider, system, user)))
        except Exception as exc:  # noqa: BLE001 —— 压缩失败不阻断，保留原文
            attempts.append({"attempt": attempt + 1, "error": f"{type(exc).__name__}"})
            continue
        if not rewritten or len(rewritten) < len(text) * 0.3:
            attempts.append({"attempt": attempt + 1, "error": "empty_or_too_short"})
            continue
        after = char_count(rewritten)
        after_ratio = engine._dialogue_ratio_of(rewritten)
        reasons: list[str] = []
        if after >= before:
            reasons.append("no_reduction")
        if after < min_after:
            reasons.append(f"over_compressed（{after} < {min_after}）")
        if after > ceiling:
            reasons.append(f"still_over_limit（{after} > {ceiling}）")
        ratio_ok = after_ratio >= before_ratio * 0.95
        if before_ratio >= (dialogue_floor or 0) and dialogue_floor:
            ratio_ok = ratio_ok and after_ratio >= float(dialogue_floor)
        if not ratio_ok:
            reasons.append(
                f"dialogue_dropped（{before_ratio:.1%}→{after_ratio:.1%}）"
            )
        attempts.append({
            "attempt": attempt + 1, "chars": after,
            "dialogue": round(after_ratio, 4), "reasons": reasons,
        })
        if not reasons:
            record.update({
                "candidate_chars": after,
                "dialogue_after": round(after_ratio, 4),
                "attempts": attempts,
                "accepted": True, "discarded": False, "discard_reasons": [],
                "chars_after": after,
                "still_over_limit": after > ceiling,
            })
            return rewritten, record
    record.update({
        "failed": True, "error": "rejected_candidates",
        "reasons": attempts[-1].get("reasons") if attempts else [],
        "attempts": attempts,
        "accepted": False, "chars_after": before,
        "still_over_limit": before > ceiling,
    })
    return text, record


def _enforce_length_cap(provider, text: str, max_chars: int | None,
                        dialogue_floor: float | None = None,
                        max_attempts: int = 2) -> tuple[str, dict]:
    """落库前**最终**字数守卫：后处理（并段/切分/改写）可能把正文顶过上限。

    真机（2026-11-22 最终轮第 3 章）：压缩轮已接受，但之后的并段/切分又加回
    58 字，终稿 4858 > 4800 被整章拒绝。这里再压一轮（默认两次），压不回才交
    给 LengthBlocked。

    `max_attempts`（2026-11-23）：重写轮刚用同一目标压过并失败时，调用方传 1，
    避免守卫把完全相同的提示原样再跑两遍（最多白烧两次模型调用）。
    """
    cap = int(length_limits(max_chars)["max"])
    info = {"cap": cap, "before": char_count(text), "after": char_count(text),
            "attempts": 0, "applied": False}
    if info["before"] <= cap:
        return text, info
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        got, _rec = _engine()._compression_round(
            provider, text, max_chars,
            dialogue_floor=dialogue_floor,
            # 这里要的是「压进 cap」，不是「cap 再打 120%」：旧写法把 cap-40
            # 当 max_chars 传进去，被内部再乘 1.2，目标反而高过闸门。
            hard_limit=cap,
        )
        info["attempts"] = attempt
        if char_count(got) < char_count(text):
            text = got
            info["applied"] = True
        else:
            # 一轮没有任何减少：再试一次只会是同一份提示，直接收手。
            break
        if char_count(text) <= cap:
            break
    info["after"] = char_count(text)
    info["still_over"] = info["after"] > cap
    return text, info
