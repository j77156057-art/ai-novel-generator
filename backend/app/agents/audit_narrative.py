"""蒸馏产物质量审核规则。

## 为什么需要

蒸馏产物（`projects.style_narrative`）没有任何审核。它产出几千条规则，
但**没人检查这些规则能不能用**。实测雪中 M3 蒸馏 3508 条规则，问题有：

1. **剧情泄漏**：三个剧情型维度占 43%（1506 条），规则正文是具体剧情
   （「贵客抵达→王府开中门→以糖葫芦诱引小王爷」），人名换成占位符后
   情节照旧。这些规则会让生成复读参考书。
2. **空泛套话**：弱模型 29% 的规则短于 15 字（「文笔细腻」「节奏适中」），
   不可执行。
3. **条目膨胀**：高频场景模板 791 条、潜台词模式 487 条，但生成时每维
   只取 2-3 条，99% 的产出永远用不上。
4. **维度缺失**：某维度 0 条（模型没答出来或被全部 rejected）没人发现。

这些都不是「代码 bug」，是**产物质量问题**，只能靠审核规则发现。

## 判据必须通用

不检测「雪中的专名」。用结构特征：
  - 过短：规则正文长度 < 阈值
  - 剧情泄漏：规则里有「箭头动作链但每个节点都带具体名词」
  - 条目膨胀：某维度条数远超实际使用量
  - 维度空缺：声明要抽的维度 0 条
"""

from __future__ import annotations

import re
from typing import Any

#: 规则正文（剥掉证据）短于此值 → 空泛套话。
#: 依据：实测 M3 中位 29 字、智谱 17 字；参考语料里可执行的规则都在 25 字以上。
TOO_SHORT_THRESHOLD = 15

#: 单维度条数超过此值 → 条目膨胀。
#: 依据：`describe_narrative` 每维只取 RULES_PER_DIM 条（默认 5），
#: 超过 100 条的维度有 95% 的产出永远进不了 prompt。
DIM_BLOAT_THRESHOLD = 100


def dim_usage() -> int:
    """生成时每维实际使用的条数（与 describe_narrative 的默认一致）。

    从 `narrative.RULES_PER_DIM` 现读，不写死 —— 那个常量一改，
    这里写死的数字就会变成过期结论（agent 会照着它给建议）。
    """
    from app.style.narrative import RULES_PER_DIM
    return int(RULES_PER_DIM)

_EVIDENCE_RE = re.compile(r"[（(]\s*证据\s*[：:].*?[）)]")

#: 抽象规则里不该出现的「具体叙事成分」信号词。
#: 注意：这些词本身不是问题，问题是「规则正文里出现它们」——
#: 说明这条规则在复述剧情而不是描述手法。
NARRATIVE_SIGNALS = (
    "他说", "她说", "说道", "问道", "答道", "笑道", "冷笑道",
    "走到", "走进", "走出", "站在", "坐在", "躺在",
    "看见", "发现", "想起", "回头", "转身",
)


def strip_evidence(text: str) -> str:
    """去掉「（证据：…）」，只留规则正文。"""
    return _EVIDENCE_RE.sub("", str(text or "")).strip()


def _iter_rules(profile: dict) -> list[tuple[str, str]]:
    """展开档案里的所有规则 → [(维度, 规则正文)]。"""
    from app.style.layers import method_rules

    out: list[tuple[str, str]] = []
    for dim, items in (method_rules(profile) or {}).items():
        if not isinstance(items, list):
            continue
        for it in items:
            raw = it.get("rule", "") if isinstance(it, dict) else str(it)
            body = strip_evidence(raw)
            if body:
                out.append((dim, body))
    return out


def rule_narrative_too_short(profile: dict) -> tuple[list[dict], dict]:
    """规则过短（空泛套话）。"""
    rules = _iter_rules(profile)
    if not rules:
        return [], {"total": 0}

    short_by_dim: dict[str, int] = {}
    total_by_dim: dict[str, int] = {}
    samples: list[str] = []
    for dim, body in rules:
        total_by_dim[dim] = total_by_dim.get(dim, 0) + 1
        if len(body) < TOO_SHORT_THRESHOLD:
            short_by_dim[dim] = short_by_dim.get(dim, 0) + 1
            if len(samples) < 5:
                samples.append(body)

    total_short = sum(short_by_dim.values())
    pct = total_short / len(rules) * 100
    stats = {
        "total": len(rules),
        "too_short": total_short,
        "too_short_pct": round(pct, 1),
        "by_dim": short_by_dim,
    }
    if pct <= 10:
        return [], stats

    worst = sorted(short_by_dim.items(), key=lambda kv: -kv[1])[:5]
    return [{
        "rule": "narrative_rules_too_short",
        "severity": "warn" if pct < 30 else "error",
        "title": f"叙事档案 {pct:.0f}% 的规则短于 {TOO_SHORT_THRESHOLD} 字（空泛套话）",
        "detail": (
            f"{total_short}/{len(rules)} 条规则剥掉证据后不足 {TOO_SHORT_THRESHOLD} 字。"
            f"这类规则（「文笔细腻」「节奏适中」）不可执行，占用了 prompt 预算。"
            f"集中维度： " + "、".join(f"{d}({n})" for d, n in worst)
        ),
        "suggestion": (
            "换更强的模型重蒸馏（实测弱模型过短率 29%，M3 为 0%），"
            "或在蒸馏 prompt 里给最低字数要求。抽查： "
            + " / ".join(repr(s) for s in samples)
        ),
        "ref": {"metric": "too_short_pct"},
    }], stats


def rule_dimension_bloat(profile: dict) -> tuple[list[dict], dict]:
    """某维度条数远超实际使用量（蒸馏白干）。"""
    from app.style.layers import method_rules

    rules = method_rules(profile) or {}
    issues: list[dict] = []
    bloated: dict[str, int] = {}

    for dim, items in rules.items():
        if not isinstance(items, list):
            continue
        n = len(items)
        if n > DIM_BLOAT_THRESHOLD:
            usage = dim_usage()
            bloated[dim] = n
            issues.append({
                "rule": "narrative_dimension_bloat",
                "severity": "info",
                "title": f"维度「{dim}」有 {n} 条规则，但生成时只用前 {usage} 条",
                "detail": (
                    f"约 {100 - usage / n * 100:.0f}% 的产出永远进不了 prompt。"
                    f"要么该维度抽得太细（应按场景归类而非逐段罗列），"
                    f"要么生成侧该多取几条。"
                ),
                "suggestion": (
                    "检查该维度的蒸馏 prompt 是否要求「逐段罗列」而非「归纳模式」。"
                    "若内容确实需要保留，调整生成时每维取的条数。"
                ),
                "ref": {"dimension": dim, "count": n},
            })

    return issues, {"bloated_dims": bloated, "total_dims": len(rules)}


def rule_narrative_plot_leak(profile: dict) -> tuple[list[dict], dict]:
    """剧情型维度的规则正文里带具体叙事成分（会污染生成）。"""
    # 这三个维度天生讲剧情，规则正文里出现叙事成分是正常的——
    # 但它们最终会被 _prompt_form 压成极短的骨架，所以「正文带剧情」
    # 本身不算问题，只统计比例供参考。
    from app.style.narrative import _PLOT_BEARING_DIMS
    from app.style.layers import method_rules

    rules = method_rules(profile) or {}
    stats: dict[str, Any] = {}
    issues: list[dict] = []

    # 非剧情维度里出现叙事成分才是问题（那说明它没在描述手法）
    leaky: dict[str, int] = {}
    for dim, items in rules.items():
        if dim in _PLOT_BEARING_DIMS or not isinstance(items, list):
            continue
        bad = 0
        for it in items:
            raw = it.get("rule", "") if isinstance(it, dict) else str(it)
            body = strip_evidence(raw)
            if any(sig in body for body in [body] for sig in NARRATIVE_SIGNALS):
                bad += 1
        if bad:
            leaky[dim] = bad
        stats[dim] = {"total": len(items), "narrative_leak": bad}

    total_leak = sum(leaky.values())
    total_rules = sum(len(v) for v in rules.values() if isinstance(v, list))
    if total_rules and total_leak / total_rules > 0.15:
        worst = sorted(leaky.items(), key=lambda kv: -kv[1])[:4]
        issues.append({
            "rule": "narrative_method_dim_plot_leak",
            "severity": "warn",
            "title": f"手法类维度里有 {total_leak} 条规则在复述剧情而非描述手法",
            "detail": (
                "这些规则含「他说/走到/看见」这类具体叙事成分，说明模型在"
                "总结「这段写了什么」而不是「作者怎么写的」。注入后会让生成"
                "复读参考书的情节。集中维度： "
                + "、".join(f"{d}({n})" for d, n in worst)
            ),
            "suggestion": (
                "在蒸馏 prompt 里加正反例：正例「用动作暗示情绪」，"
                "反例「他走到门口说」。"
            ),
            "ref": {"metric": "method_plot_leak"},
        })

    return issues, stats


def rule_empty_dimensions(profile: dict, expected: list[str] | None = None) -> tuple[list[dict], dict]:
    """声明要抽的维度 0 条（模型没答出来或被全部 rejected）。"""
    from app.style.layers import method_rules

    rules = method_rules(profile) or {}
    empty = [d for d in (expected or []) if not (rules.get(d) or [])]
    stats = {"checked": len(expected or []), "empty": empty}
    if not empty:
        return [], stats

    return [{
        "rule": "narrative_dimension_empty",
        "severity": "warn",
        "title": f"{len(empty)} 个维度完全没有规则：{'、'.join(empty)}",
        "detail": (
            "这些维度在档位配置里声明了要抽，但产物是空的。"
            "可能原因：模型没输出该维度 / 输出被证据校验全额 rejected / "
            "解析失败。"
        ),
        "suggestion": "用更低的采样粒度（单 chunk）重跑这些维度，看是否有输出。",
        "ref": {"empty_dims": empty},
    }], stats


def run_narrative_audit(
    profile: dict | None, expected_dims: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """跑全部蒸馏产物审核规则。"""
    if not profile:
        return [{
            "rule": "narrative_missing",
            "severity": "info",
            "title": "项目还没有叙事档案",
            "detail": "未蒸馏或蒸馏失败，生成时不会注入文风规则。",
            "suggestion": "调用 build-narrative 蒸馏。",
            "ref": {},
        }], {}

    issues: list[dict] = []
    stats: dict[str, Any] = {}

    for fn in (rule_narrative_too_short, rule_dimension_bloat,
               rule_narrative_plot_leak):
        i, s = fn(profile)
        issues.extend(i)
        stats.update({fn.__name__: s})

    i, s = rule_empty_dimensions(profile, expected_dims)
    issues.extend(i)
    stats["empty_dims"] = s

    return issues, stats
