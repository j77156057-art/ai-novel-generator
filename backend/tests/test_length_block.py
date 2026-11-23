# -*- coding: utf-8 -*-
"""生成侧硬判据 2：字数双向拦截——上限与下限用**同一个计数**（字符）。

2026-11-17 修的口径缺口：上限闸（`generation_gate` / `length_guard`）过去比
`hanzi_count`，下限（`repetition.analyze_length`）比 `char_count`。同一份稿子
两个数，标点与换行越多差得越大：项目 6 那两章实测 3,451 字符 / 2,737 汉字，
下限按字符判 79.3%（判短），上限按汉字要到 5,400 才拦——120% 实际等效 151%。
本文件把两边都钉在字符上，并专门造「汉字少、字符多」的稿子：只要上限还是
拿汉字比，这些用例必然失败。

锁住五件事：

1. 上限 = 目标 × 120%，严格大于才拦（等于上限放行）；
2. 上限与下限读同一个数：同一份稿子，`analyze_length.chars` == 闸门
   `metrics["正文字数"]` == `char_count(text)`；
3. 4,000 字符（预算 3,000 的 133%）必须拦，3,500 字符（117%）必须放行——
   这两个点都在旧口径的「汉字 120%」之外，专门抓 151% 那个洞；
4. 本项目两章的真实字数 3,451 / 2,728 字符落在放行区；
5. blocked 明细带段号 / 章标签 / 实测字符数 / 上限 / 压缩是否接受，且不落库。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app.generation.engine as engine  # noqa: E402
from app.generation.repetition import analyze_length  # noqa: E402
from app.models import Base, Generation, Project  # noqa: E402
from app.style.consistency import (  # noqa: E402
    char_count,
    generation_gate,
    length_limits,
)


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def project(db):
    p = Project(title="篇幅上限测试", generation_length=4000)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _text(n: int) -> str:
    """n 个汉字——字符数与汉字数 1:1，只用于落库/上限的纯边界值。"""
    return "字" * n


def _mixed(n: int) -> str:
    """n 个字符、汉字只占 3/7 的正文（``他说：“好。”`` 循环）。

    这是抓口径缺口的关键样本：汉字数约为字符数的 43%，按汉字比上限的旧实现
    会把下面 4,000 字符（汉字约 1,714）判成放行。
    """
    unit = "他说：“好。”"
    assert len(unit) == 7
    return (unit * (n // len(unit)) + unit)[:n]


def test_limit_is_target_times_existing_ratio():
    # 不新增常量：沿用 length_limits 的既有 120% 口径
    assert length_limits(4000)["max"] == 4800
    assert length_limits(3000)["max"] == 3600


def test_exactly_at_limit_passes():
    text = _mixed(4800)
    assert char_count(text) == 4800
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=4000), text,
        {"length_path": {"path": "compression", "limit": 4800}},
    )
    assert ev is None


def test_one_char_over_limit_blocks():
    text = _mixed(4801)
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=4000), text,
        {"length_path": {"path": "compression", "limit": 4800,
                         "compression_accepted": True}},
    )
    assert ev is not None
    assert ev["chars"] == 4801
    # 汉字远低于上限：证明判界量的是字符，不是汉字
    assert ev["hanzi"] < ev["limit"]
    assert ev["limit"] == 4800
    assert ev["path"] == "compression"
    assert ev["compression_accepted"] is True
    assert ev["still_over_limit"] is True


def test_4000_chars_at_133_percent_is_blocked():
    """预算 3,000 的 133%：旧口径下限已是 133%（字符），上限按汉字才 57% 放行。"""
    budget = 3000
    text = _mixed(4000)
    ratio = char_count(text) / budget
    assert ratio == pytest.approx(1.3333, abs=0.001)

    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=budget), text, None)
    assert ev is not None
    assert ev["chars"] == 4000
    assert ev["limit"] == 3600
    assert ev["hanzi"] < ev["limit"]


def test_3500_chars_at_117_percent_passes():
    """预算 3,000 的 117%：低于 120% 上限，必须放行（不是所有超预算都拦）。"""
    budget = 3000
    text = _mixed(3500)
    ratio = char_count(text) / budget
    assert ratio == pytest.approx(1.1667, abs=0.001)

    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=budget), text, None)
    assert ev is None


def test_real_chapter_lengths_are_inside_band():
    """本项目两章真实字数：3,451（115.0%）与 2,728（90.9%），都应在放行区。"""
    budget = 3000
    for n, want_ratio in ((3451, 1.1503), (2728, 0.9093)):
        text = _mixed(n)
        assert char_count(text) == n
        assert n / budget == pytest.approx(want_ratio, abs=0.001)
        assert engine._length_block_evidence(
            SimpleNamespace(generation_length=budget), text, None) is None
        report = analyze_length(text, budget)
        assert report.verdict == "ok"
        assert report.chars == n


def test_upper_and_lower_bounds_count_the_same_text():
    """同一份稿子、同一个计数：下限报告与上限闸读到的字符数必须相等。"""
    budget = 3000
    text = _mixed(3451)
    report = analyze_length(text, budget)
    gate = generation_gate(text, budget)
    assert report.chars == gate["metrics"]["正文字数"] == char_count(text) == 3451
    assert report.verdict == "ok"


def test_gate_blocks_4000_chars_with_few_hanzi():
    """闸门层同一道洞：汉字少、字符多也要按字符判违规。"""
    gate = generation_gate(_mixed(4000), 3000)
    assert gate["metrics"]["正文字数"] == 4000
    assert gate["metrics"]["汉字数"] < gate["length_limit"]["max"]
    hits = [v for v in gate["violations"] if v["metric"] == "正文字数"]
    assert len(hits) == 1
    assert hits[0]["value"] == 4000
    assert hits[0]["limit"] == {"max": 3600}
    assert gate["passed"] is False


def test_falls_back_to_project_budget_without_length_path():
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=3000), _text(3601), None)
    assert ev is not None
    assert ev["limit"] == 3600
    assert ev["path"] is None


def test_no_budget_and_no_path_does_not_guess():
    ev = engine._length_block_evidence(
        SimpleNamespace(generation_length=None), _text(9999), None)
    assert ev is None


def test_compression_uses_true_hard_limit(monkeypatch):
    """最终守卫传的 4800 是硬上限，不能再被乘成 5712（2026-11-23 真机问题）。"""
    calls: dict[str, str] = {}

    def fake_stream(provider, system, user):
        calls["system"] = system
        return [_text(4700)]

    monkeypatch.setattr(engine, "_stream_with_retry", fake_stream)
    got, rec = engine._compression_round(
        SimpleNamespace(), _text(5844), 4000, hard_limit=4800)

    assert "硬上限 4800 个字符" in calls["system"]
    assert rec["accepted"] is True
    assert rec["still_over_limit"] is False
    assert rec["limit"] == 4800
    assert char_count(got) == 4700


def test_compression_rejects_candidate_still_over_limit(monkeypatch):
    """「少了一点但仍超限」不再算压缩成功：必须重试，最终取压进上限的候选。"""
    replies = iter([_text(5000), _text(4700)])

    def fake_stream(provider, system, user):
        return [next(replies)]

    monkeypatch.setattr(engine, "_stream_with_retry", fake_stream)
    got, rec = engine._compression_round(
        SimpleNamespace(), _text(5844), 4000, hard_limit=4800)

    first = rec["attempts"][0]
    assert any(str(r).startswith("still_over_limit") for r in first["reasons"])
    assert rec["accepted"] is True
    assert rec["attempts"][1]["chars"] == 4700
    assert rec["still_over_limit"] is False
    assert char_count(got) == 4700


def test_compression_floor_is_limit_based_when_original_oversized(monkeypatch):
    """原文已超限时，85% 地板按上限算：压到 3000 才算过头，4700 可通过。"""
    replies = iter([_text(3000), _text(4700)])

    def fake_stream(provider, system, user):
        return [next(replies)]

    monkeypatch.setattr(engine, "_stream_with_retry", fake_stream)
    got, rec = engine._compression_round(
        SimpleNamespace(), _text(5844), 4000, hard_limit=4800)

    assert any(
        str(r).startswith("over_compressed") for r in rec["attempts"][0]["reasons"]
    )
    assert rec["accepted"] is True
    assert char_count(got) == 4700


def test_final_length_cap_passes_hard_limit(monkeypatch):
    """守卫必须把 cap 作为 hard_limit 传下去，而不是当成新的 max_chars。"""
    seen: dict[str, int | None] = {}

    def fake_round(provider, text, max_chars, dialogue_floor=None, hard_limit=None):
        seen["max_chars"] = max_chars
        seen["hard_limit"] = hard_limit
        return _text(4700), {"accepted": True}

    monkeypatch.setattr(engine, "_compression_round", fake_round)
    got, info = engine._enforce_length_cap(SimpleNamespace(), _text(5844), 4000)

    assert seen == {"max_chars": 4000, "hard_limit": 4800}
    assert info["applied"] is True
    assert info["still_over"] is False
    assert char_count(got) == 4700


def test_final_length_cap_respects_max_attempts(monkeypatch):
    """重写轮刚压过且失败时守卫只再试一次，不把同一提示重复两遍。"""
    calls = {"n": 0}

    def fake_round(provider, text, max_chars, dialogue_floor=None, hard_limit=None):
        calls["n"] += 1
        return _text(5000), {"accepted": True}

    monkeypatch.setattr(engine, "_compression_round", fake_round)
    _got, info = engine._enforce_length_cap(
        SimpleNamespace(), _text(5844), 4000, max_attempts=1)
    assert calls["n"] == 1
    assert info["attempts"] == 1
    assert info["still_over"] is True

    calls["n"] = 0
    _got, info = engine._enforce_length_cap(
        SimpleNamespace(), _text(5844), 4000)
    assert calls["n"] == 2
    assert info["attempts"] == 2


def test_commit_raises_and_persists_nothing(db, project):
    quality = {"length_path": {"path": "compression", "limit": 4800,
                               "compression_accepted": False}}
    with pytest.raises(engine.LengthBlocked) as ei:
        engine._commit_segment(
            db, project, _text(6378), "摘要",
            quality=quality, chapter_range="第24章《夜半敲门》")
    payload = ei.value.to_dict()
    assert payload["status"] == "blocked"
    assert payload["blocked_by"] == "length_over_limit"
    assert payload["segment_no"] == 1
    assert payload["chapter_label"] == "第24章《夜半敲门》"
    assert payload["chars"] == 6378
    assert payload["limit"] == 4800
    assert db.scalars(select(Generation)).all() == []


def test_commit_inside_limit_still_writes(db, project):
    gen, seg = engine._commit_segment(
        db, project, _text(4800), "摘要",
        quality={"length_path": {"path": "compression", "limit": 4800}},
        chapter_range="第24章《夜半敲门》",
    )
    assert seg == 1
    assert char_count(gen.text) == 4800
    assert gen.chapter_range == "第24章《夜半敲门》"
