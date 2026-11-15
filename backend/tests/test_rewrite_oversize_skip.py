# -*- coding: utf-8 -*-
"""超长初稿的重写调度：直接压缩，不再先跑整章重写。

锁的是调度契约，不是压缩质量：
- 初稿已超上限且开关默认开时，`_rewrite_rounds` 一次重写都不调，直接压缩；
- 开关关掉时回到旧行为（先重写，仍超再压缩）。
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.generation import engine  # noqa: E402


def _prepare(monkeypatch, *, skip_oversize: bool) -> dict[str, int]:
    calls = {"rewrite": 0, "compress": 0}
    monkeypatch.setattr(
        engine, "settings",
        SimpleNamespace(
            rewrite_max_rounds=2,
            rewrite_stop_without_gain=True,
            rewrite_skip_oversize=skip_oversize,
        ),
    )
    monkeypatch.setattr(engine, "length_limits", lambda max_chars: {"max": 4800, "min": 3300})
    monkeypatch.setattr(engine, "_should_rewrite", lambda text, forbidden: (True, ["too_long"]))
    monkeypatch.setattr(engine, "_coherence_reasons", lambda *a, **k: [])

    def fake_rewrite(*args, **kwargs):
        calls["rewrite"] += 1
        return "字" * 4700, {"round": 1}

    def fake_compression(provider, text, max_chars):
        calls["compress"] += 1
        return "字" * 4000, {
            "kind": "compression",
            "round": "compression",
            "accepted": True,
            "hanzi_after": 4000,
        }

    monkeypatch.setattr(engine, "_rewrite_with_evidence", fake_rewrite)
    monkeypatch.setattr(engine, "_compression_round", fake_compression)
    return calls


class _Provider:
    def summarize(self, text: str) -> str:
        return "summary"


def test_oversize_draft_skips_rewrite_and_compresses(monkeypatch):
    calls = _prepare(monkeypatch, skip_oversize=True)
    text, _summary, _info, rounds, path = engine._rewrite_rounds(
        None, None, {}, _Provider(), "字" * 5000, "old",
        {}, {"passed": False, "reasons": ["too_long"], "metrics": {}},
    )

    assert calls["rewrite"] == 0
    assert calls["compress"] == 1
    assert len(text) == 4000
    assert path["path"] == "compression"
    assert path["rewrite_skipped"] == "oversize"
    assert path["still_over_limit"] is False
    assert len(rounds) == 1


def test_toggle_off_keeps_old_rewrite_first_order(monkeypatch):
    calls = _prepare(monkeypatch, skip_oversize=False)
    text, _summary, _info, _rounds, path = engine._rewrite_rounds(
        None, None, {}, _Provider(), "字" * 5000, "old",
        {}, {"passed": False, "reasons": ["too_long"], "metrics": {}},
    )

    assert calls["rewrite"] >= 1
    assert path["rewrite_skipped"] is None
    assert len(text) <= 4800
