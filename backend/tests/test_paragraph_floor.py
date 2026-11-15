# -*- coding: utf-8 -*-
"""v22 段数下限：随目标字数线性折算 + 重写块缺阈值时优雅降级。

三条要锁住的事：

1. `_gate_hard_block` 在「段数违规 + 无 active_full_limits」时不得格式化 None
   （旧代码在此抛 TypeError，重写轮整轮崩掉、正文仍旧稿）；
2. 段数下限 = 每千字段数 × 目标字数 / 1000：4,000 字仍得 70（项目 6 现行值），
   1,200 字得 21，来源（reference_p10_per_1k / constant_per_1k）随 limit 记录；
3. 没有段数阈值的带原样返回，不臆造下限。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.generation import engine  # noqa: E402
from app.style import consistency  # noqa: E402


def _gate_info(limit: dict) -> dict:
    """只构造一条「段数」违规，模拟无参考书时的闸门输出。"""
    return {
        "passed": False,
        "skipped": False,
        "metrics": {"段数": 12},
        "violations": [{"metric": "段数", "value": 12, "limit": limit}],
        "length_limit": {"target": 1200, "max": 1440},
    }


def test_hard_block_skips_metric_without_any_limit():
    # 无 active_full_limits：退回 GEN_GATE_LIMITS（里面根本没有「段数」）
    consistency._ACTIVE_FULL.set(None)
    block = engine._gate_hard_block(_gate_info({}))
    # 没有阈值时只留在 violations：不抛异常，也不能出现格式化后的 None
    assert "自然段数" not in block
    assert "None" not in block


def test_hard_block_renders_floor_from_violation_limit():
    # 判罚带随项目目标折算过：阈值取违规项自带的 limit，不依赖 full 带
    consistency._ACTIVE_FULL.set(None)
    block = engine._gate_hard_block(
        _gate_info({"min": 21.0, "source": "constant_per_1k"})
    )
    assert "自然段数 ≥21 段" in block


def test_paragraph_floor_scales_with_target():
    band = {"段数": {"min": 70.0, "per_1k": 17.5, "source": "reference_p10_per_1k"}}
    at_4000 = consistency.paragraph_floor_limits(band, 4000)["段数"]
    at_1200 = consistency.paragraph_floor_limits(band, 1200)["段数"]
    assert at_4000["min"] == 70.0  # 项目 6 现行值，不许变
    assert at_1200["min"] == 21.0  # 1,200 字目标
    assert at_1200["source"] == "reference_p10_per_1k"
    assert at_1200["per_1k"] == 17.5
    # 幂等：拿折算结果再折算一次，不会二次缩放
    again = consistency.paragraph_floor_limits({"段数": at_1200}, 1200)["段数"]
    assert again["min"] == 21.0
    assert again["source"] == "reference_p10_per_1k"


def test_paragraph_floor_derives_constant_from_absolute_floor():
    # 无参考书：常量带只有绝对下限 70，反推 17.5 段/千字
    row = consistency.paragraph_floor_limits({"段数": {"min": 70.0}}, 1200)["段数"]
    assert row["min"] == 21.0
    assert row["source"] == "constant_per_1k"
    assert row["per_1k"] == 17.5


def test_paragraph_floor_does_not_invent_limit():
    # 没有段数键 → 原样返回（不在 active_full_limits 里塞默认值）
    no_key = {"句长均值": {"min": 14.5}}
    assert consistency.paragraph_floor_limits(no_key, 1200) is no_key
    # 下限为 0 等于不设限，同样不造数
    zero = {"段数": {"min": 0.0}}
    assert consistency.paragraph_floor_limits(zero, 1200) is zero


def test_archive_band_carries_per_1k():
    pct = {"段数": {"p10": 70.0, "p50": 95.0, "p90": 128.0}}
    judge, full = consistency.gate_limits_from_percentiles(pct)
    assert full["段数"]["source"] == "reference_p10_per_1k"
    assert full["段数"]["per_1k"] == 17.5
    assert judge["段数"]["min"] == 70.0
    assert consistency.paragraph_floor_limits(judge, 1200)["段数"]["min"] == 21.0


def test_gate_uses_scaled_floor_for_short_target():
    # 1,200 字目标 + 5 段：旧口径要求 70 段必挂，新口径下限 21 仍是违规，
    # 但阈值与来源必须写清，且不依赖任何参考书。
    consistency._ACTIVE_JUDGE.set(None)
    consistency._ACTIVE_FULL.set(None)
    para = "他推门进来，屋里没人应声，桌上的茶还冒着热气。" * 12
    text = "\n".join([para] * 5)
    gate = consistency.generation_gate(text, 1200)
    assert gate["skipped"] is False
    assert gate["metrics"]["段数"] == 5
    violations = {v["metric"]: v for v in gate["violations"]}
    assert "段数" in violations
    assert violations["段数"]["limit"]["min"] == 21.0
    assert violations["段数"]["limit"]["source"] == "constant_per_1k"
