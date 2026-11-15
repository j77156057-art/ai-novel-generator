# -*- coding: utf-8 -*-
"""「只判不拦」的三项新判据：对白配比 / 时代越界 / 短句占比。

锁住的是**不拦**这个契约本身，而不是阈值好不好：

1. 返回值必须有 ``mode="report"``；
2. 不得出现 ``passed`` / ``violations`` 字段（这两个键属于闸门，出现即意味着
   这些判据可能被当成判罚项）；
3. 口径层级必须是章级，参考线取 ``gate_limits.chapter_percentiles``；档案推不出
   时才退回配置/常量，且 ``source`` 必须写明退到了哪一级。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.generation import engine  # noqa: E402


def _limits(dialogue: dict | None, short: dict | None = None) -> dict:
    """只造 ``_dialogue_ratio_check`` / ``_short_sentence_check`` 会读的键。"""
    return {
        "chapters": 560,
        "chapter_percentiles": {
            "对白字占比": dialogue or {},
            "短句占比": short or {},
        },
    }


def _report_only(payload: dict) -> None:
    assert payload["mode"] == "report"
    assert "passed" not in payload
    assert "violations" not in payload
    assert "只判不拦" in payload["note"]


# ─── 对白配比 ────────────────────────────────────────────────────────────


def test_dialogue_ratio_above_p75_uses_chapter_percentiles():
    got = engine._dialogue_ratio_check(
        SimpleNamespace(id=6),
        {"对白字占比": 0.406},
        # 取第 34 章 quality_json 里的实际档案分位（560 章）
        _limits({"p25": 0.1747, "p50": 0.2545, "p75": 0.345}),
    )
    _report_only(got)
    assert got["level"] == "chapter"
    assert got["source"] == "archive_chapter"
    assert got["chapters"] == 560
    assert got["value"] == 0.406
    assert got["above_p75"] is True


def test_dialogue_ratio_inside_band_is_not_flagged():
    got = engine._dialogue_ratio_check(
        SimpleNamespace(id=6),
        {"对白字占比": 0.200},
        _limits({"p25": 0.1747, "p50": 0.2545, "p75": 0.345}),
    )
    _report_only(got)
    assert got["above_p75"] is False


def test_dialogue_ratio_missing_metric_reports_none():
    got = engine._dialogue_ratio_check(
        SimpleNamespace(id=6), {},
        _limits({"p25": 0.1747, "p50": 0.2545, "p75": 0.345}),
    )
    _report_only(got)
    assert got["value"] is None
    assert got["above_p75"] is None


def test_dialogue_ratio_without_archive_marks_fallback_source():
    got = engine._dialogue_ratio_check(
        SimpleNamespace(id=6), {"对白字占比": 0.30}, _limits(None)
    )
    _report_only(got)
    # 配置优先，其次常量表；两级都必须如实标出来源，不能冒充 archive_chapter
    assert got["source"] in {"config", "constant"}
    assert got["chapters"] is None
    assert got["p25"] <= got["p50"] <= got["p75"]


# ─── 时代越界 ────────────────────────────────────────────────────────────


def test_era_check_quality_is_report_only_and_caps_samples():
    era_meta = {
        "enabled": True,
        "status": "active",
        "baseline": "modern",
        "baseline_source": "project_profile",
        "mentions": 18,
        "counts": {"period_mismatch": 2},
        "note_counts": {"needs_authorization": {"period_mismatch": 2}},
        "violations": [{"word": f"词{i}"} for i in range(7)],
        "records": [{"word": f"记录{i}"} for i in range(7)],
        "record_counts": {"channel_hit_no_introducer": 1},
    }
    got = engine._era_check_quality(
        era_meta, {"prompt_injected": True, "chars": 321, "status": "injected"},
        {"abused": False},
    )
    _report_only(got)
    assert got["status"] == "active"
    assert got["mentions"] == 18
    assert got["violation_counts"] == {"period_mismatch": 2}
    assert got["violation_notes"] == {"needs_authorization": {"period_mismatch": 2}}
    assert len(got["samples"]) == 5
    assert len(got["records"]) == 5
    assert got["record_counts"] == {"channel_hit_no_introducer": 1}
    assert got["channel_abuse"] == {"abused": False}
    assert got["prompt_injected"] is True
    assert got["prompt_chars"] == 321


def test_era_check_quality_passes_scan_disabled_status():
    got = engine._era_check_quality(
        {"enabled": True, "status": "scan_disabled", "counts": {}}, {}
    )
    _report_only(got)
    assert got["status"] == "scan_disabled"
    assert got["samples"] == []
    assert got["channel_abuse"] == {}


# ─── 短句占比（口径层级回归）──────────────────────────────────────────────


def test_short_sentence_check_level_is_chapter():
    got = engine._short_sentence_check(
        {"短句占比": 0.015},
        _limits(None, {"p10": 0.0199, "p50": 0.048, "p90": 0.085}),
    )
    _report_only(got)
    assert got["level"] == "chapter"
    assert got["source"] == "archive_chapter"
    assert got["below_p10"] is True
