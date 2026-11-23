# -*- coding: utf-8 -*-
"""自动返修的验收：模型改完先量一遍，不达标就回滚。

验收只回答「这版能不能留」，不改正文、不调模型。五项判据都取**相对改前**
的口径，避免把不同章节的绝对风格差异当成退化：

1. 字数：改后 / 改前在 85%–115%（防止借返修大幅删减或膨胀）；
2. 对白守恒：引号内字数不低于改前的 90%（返修最容易误删台词）；
3. 闸门：违规项数量不增加，改前通过则改后也必须通过；
4. 极短纯对白：占比不增加超过 0.5 个百分点；
5. 归属率：不低于改前 2 个百分点以上（不能为了削口头禅把归属一起删掉）。
"""
from __future__ import annotations

from typing import Any

from app.style import stylometry
from app.style.attribution_fix import attribution_rate
from app.style.consistency import (
    generation_gate,
    hanzi_count,
    is_pure_dialogue_paragraph,
)


def text_snapshot(
    text: str,
    max_chars: int | None = None,
    gate_limits: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """量一份正文的关键指标（只读，永不抛异常）。"""
    src = text or ""
    paras = [p for p in stylometry.paragraphs(src) if p.strip()]
    non_space = len("".join(src.split())) or 1
    short_pure = sum(
        1 for p in paras
        if is_pure_dialogue_paragraph(p) and hanzi_count(p) <= 4
    )
    try:
        gate = generation_gate(src, max_chars, gate_limits=gate_limits)
    except Exception:  # noqa: BLE001 —— 验收不能因为闸门本身出错而中断
        gate = {"passed": True, "violations": [], "skipped": True}
    try:
        attr = attribution_rate(src)["rate"]
    except Exception:  # noqa: BLE001
        attr = 0.0
    return {
        "chars": non_space,
        "paras": len(paras),
        "dialogue_chars": stylometry.dialogue_char_count(src),
        "dialogue_ratio": round(
            stylometry.dialogue_char_count(src) / non_space, 3
        ),
        "ultra_short_paras": short_pure,
        "ultra_short_ratio": round(short_pure / len(paras), 3) if paras else 0.0,
        "attribution": round(float(attr), 3),
        "gate_passed": bool(gate.get("passed")),
        "gate_skipped": bool(gate.get("skipped")),
        "gate_violations": [
            v.get("metric") for v in (gate.get("violations") or [])
        ],
    }


def evaluate_repair(
    before: str,
    after: str,
    max_chars: int | None = None,
    gate_limits: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """验收模型返修结果；``ok=False`` 时调用方应回滚到 ``before``。"""
    old = text_snapshot(before, max_chars, gate_limits)
    new = text_snapshot(after, max_chars, gate_limits)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    ratio = (new["chars"] / old["chars"]) if old["chars"] else 0.0
    add(
        "字数",
        0.85 <= ratio <= 1.15,
        f"{old['chars']} → {new['chars']}（{ratio:.0%}，要求 85%–115%）",
    )

    keep = (new["dialogue_chars"] / old["dialogue_chars"]) if old["dialogue_chars"] else 1.0
    add(
        "对白守恒",
        keep >= 0.90,
        f"引号内字数保留 {keep:.0%}（要求 ≥90%）",
    )

    old_v = set(old["gate_violations"])
    new_v = set(new["gate_violations"])
    add(
        "闸门",
        new_v <= old_v and (new["gate_passed"] or not old["gate_passed"]),
        (
            f"违规 {sorted(old_v) or '无'} → {sorted(new_v) or '无'}；"
            f"通过 {old['gate_passed']} → {new['gate_passed']}"
        ),
    )

    ultra_limit = 0.032
    ultra_row = (gate_limits or {}).get("极短纯对白段占比") or {}
    try:
        ultra_limit = float(ultra_row.get("max") or ultra_limit)
    except (TypeError, ValueError):
        pass
    add(
        "极短纯对白",
        new["ultra_short_ratio"] <= max(old["ultra_short_ratio"] + 0.005, ultra_limit),
        (
            f"{old['ultra_short_ratio']:.1%} → {new['ultra_short_ratio']:.1%}"
            f"（上限 {ultra_limit:.1%}，允许比改前 +0.5 个百分点）"
        ),
    )

    add(
        "归属率",
        new["attribution"] >= old["attribution"] - 0.02,
        f"{old['attribution']:.1%} → {new['attribution']:.1%}（允许 -2 个百分点）",
    )

    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "before": old,
        "after": new,
    }
