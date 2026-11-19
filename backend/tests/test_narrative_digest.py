"""蒸馏档案摘要：空档案、注入内容、截断。"""
from __future__ import annotations

from app.style.narrative import narrative_digest


def test_empty_profile_returns_empty():
    assert narrative_digest(None) == ""
    assert narrative_digest({}) == ""


def test_digest_contains_forbidden_and_voice_rules():
    profile = {
        "rules": {},
        "forbidden": {"禁用转折词": ["不禁", "顿时"]},
    }
    text = narrative_digest(profile, max_chars=500)
    assert "不禁" in text and "顿时" in text


def test_digest_truncates_to_max_chars():
    profile = {"rules": {}, "forbidden": {"禁用转折词": ["禁" * 500]}}
    text = narrative_digest(profile, max_chars=40)
    assert len(text) <= 41  # 40 + 省略号
    assert text.endswith("…")
