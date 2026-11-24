"""伏笔节奏规划器（规则版）。

## 它回答什么问题

「这条伏笔应该在第几章埋、第几章升级、第几章回收？」

规则只依赖两样东西：**全书章数** 和 **伏笔级别**。它不猜剧情，也不写正文，
只把「该在什么时候出现」排成一张可执行的表：

- 主线：第 1 章埋；揭晓窗口用台账的百分比（默认全书 90% 起、10% 跨度）；
  窗口之前按 20/40/60/80% 插 4 个升级点；窗口内分 3 层揭晓。
- 卷级：回收前 20 章埋，中间插 1 个升级点。
- 小节级：回收前 4 章埋，中间插 1 个升级点。

## 纪律

- **纯计算**：本模块不写库；写库由路由的 apply 负责，且必须 confirm。
- **可解释**：每条计划的 `issues` 说明为什么被调整（缺回收章、超全书、
  主线窗口偏早、同级未回收过多……）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.foreshadow_ledger import (
    MAIN_REVEAL_LAYERS,
    TIERS,
    _attr,
    entries_of,
    hint_lines,
    reveal_window,
    tier_of,
)
from app.models import ForeshadowLedger, Project

#: 章纲缺失时的兜底全书章数。
DEFAULT_TOTAL = 100

#: 同级同时未回收的上限（超过就提示收敛）。
MAX_OPEN = {"main": 1, "arc": 3, "chapter": 6}

#: 主线揭晓起点不应早于全书的这个比例。
MAIN_MIN_RATIO = 0.80

#: 卷级 / 小节级的埋设提前量（章）。
LEAD_CHAPTERS = {"arc": 20, "chapter": 4}


def total_chapters(project: Project) -> int:
    """全书章数：优先用章纲条数，没有章纲时按默认值。"""
    outline = project.outline if isinstance(project.outline, list) else []
    if outline:
        return max(len(outline), 1)
    return DEFAULT_TOTAL


def _clamp(value: Any, total: int) -> int:
    return max(1, min(int(value), total))


def plan_entry(entry: Any, total: int) -> dict:
    """给一条伏笔排节奏。返回 `{tier, issues, plan}`。"""
    tier = tier_of(entry)
    issues: list[str] = []

    if tier == "main":
        start, span = reveal_window(entry)
        if not start:
            start = max(1, round(total * 0.9))
            span = max(MAIN_REVEAL_LAYERS, round(total * 0.1))
            issues.append("未设置揭晓窗口，按全书 90%/10% 兜底")
        plant = _clamp(_attr(entry, "planted_chapter") or 1, total)
        start = _clamp(start, total)
        if start <= plant:
            start = min(total, plant + MAIN_REVEAL_LAYERS)
            issues.append("揭晓章不晚于埋设章，已挪到埋设之后")
        if start / total < MAIN_MIN_RATIO:
            issues.append(
                f"揭晓起点第 {start} 章偏早（低于全书 {int(MAIN_MIN_RATIO * 100)}%）"
            )
        if not str(_attr(entry, "truth") or "").strip():
            issues.append("主线缺真相")
        if not hint_lines(entry):
            issues.append("主线缺暗示点")

        beats: list[dict] = []
        if start - plant >= 6:
            for fraction in (0.2, 0.4, 0.6, 0.8):
                chapter = plant + round((start - plant) * fraction)
                beats.append({"chapter": _clamp(chapter, total), "kind": "hint"})
        layers = [
            {
                "chapter": _clamp(
                    start
                    + ((layer - 1) * span + MAIN_REVEAL_LAYERS - 1)
                    // MAIN_REVEAL_LAYERS,
                    total,
                ),
                "layer": layer,
            }
            for layer in range(1, MAIN_REVEAL_LAYERS + 1)
        ]
        return {
            "tier": tier,
            "issues": issues,
            "plan": {
                "plant_chapter": plant,
                "payoff_chapter": start,
                "span": span,
                "beats": beats,
                "reveal_layers": layers,
            },
        }

    # —— 卷级 / 小节级 ——
    payoff = _attr(entry, "planned_chapter")
    if payoff is None:
        payoff = (
            round(total * 0.35)
            if tier == "arc"
            else min(total, (_attr(entry, "planted_chapter") or 1) + 5)
        )
        issues.append("未设置回收章，按级别默认值兜底")
    try:
        payoff = int(payoff)
    except (TypeError, ValueError):
        payoff = total
        issues.append("回收章不是整数，按末章兜底")
    if payoff > total:
        issues.append(f"回收章第 {payoff} 章超出全书 {total} 章，已截到末章")
        payoff = total
    if payoff < 2:
        payoff = 2
        issues.append("回收章太靠前，已挪到第 2 章")

    lead = LEAD_CHAPTERS.get(tier, 4)
    plant = max(1, payoff - lead)
    if plant >= payoff:
        plant = max(1, payoff - 1)

    beats: list[dict] = []
    if payoff - plant >= 4:
        beats.append(
            {
                "chapter": _clamp(plant + round((payoff - plant) / 2), total),
                "kind": "hint",
            }
        )
    return {
        "tier": tier,
        "issues": issues,
        "plan": {
            "plant_chapter": plant,
            "payoff_chapter": payoff,
            "span": 0,
            "beats": beats,
            "reveal_layers": [{"chapter": payoff, "layer": 1}],
        },
    }


def build_plan(db: Session, project: Project) -> dict:
    """算整本项目的节奏表（不写库）。"""
    total = total_chapters(project)
    rows = entries_of(db, project.id, statuses=("open",))
    counts = {tier: 0 for tier in TIERS}
    planned: list[dict] = []
    for entry in rows:
        tier = tier_of(entry)
        counts[tier] = counts.get(tier, 0) + 1
        item = plan_entry(entry, total)
        planned.append(
            {
                "id": entry.id,
                "title": entry.title,
                "tier": item["tier"],
                "current": {
                    "planted_chapter": entry.planted_chapter,
                    "planned_chapter": entry.planned_chapter,
                },
                "plan": item["plan"],
                "issues": item["issues"],
            }
        )

    summary_issues = [
        f"{tier} 级同时有 {counts[tier]} 条未回收，建议不超过 {limit} 条"
        for tier, limit in MAX_OPEN.items()
        if counts.get(tier, 0) > limit
    ]
    return {
        "project_id": project.id,
        "total_chapters": total,
        "entries": planned,
        "summary": {"counts": counts, "issues": summary_issues},
    }


def apply_plan(db: Session, project: Project) -> dict:
    """把节奏表写回台账（调用方必须先确认）。"""
    report = build_plan(db, project)
    changed = 0
    for item in report["entries"]:
        row = db.get(ForeshadowLedger, item["id"])
        if row is None or row.project_id != project.id:
            continue
        plan = item["plan"]
        row.planted_chapter = plan["plant_chapter"]
        if item["tier"] == "main":
            # 主线补全窗口参数，避免下次又走兜底。
            if not row.total_chapters:
                row.total_chapters = report["total_chapters"]
            if row.reveal_ratio is None:
                row.reveal_ratio = 0.9
            if row.reveal_span_ratio is None:
                row.reveal_span_ratio = 0.1
        else:
            row.planned_chapter = plan["payoff_chapter"]
        row.beat_plan = plan["beats"]
        changed += 1
    db.commit()
    return {"applied": changed, **report}
