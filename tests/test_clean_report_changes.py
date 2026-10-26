"""Cleaning completion describes actual edits, independently of model review."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from app.agents.ingest_clean import preview_ingest_clean, check_ingest_clean
from app.ingestion.clean import build_clean_report


def test_template_only_removal_reports_cleaned():
    result = preview_ingest_clean(text="第一章 风\n风来。一秒记住【abc.info】，为您提供精彩小说阅读。风去。")
    report = result["report"]
    assert report["removed_chars"] > 0
    assert report["detected_count"] == 0
    assert report["removed_spans"]
    assert report["cleaned"] is True


def test_equal_length_edit_is_still_cleaned():
    assert build_clean_report("甲", "乙", [])["cleaned"] is True
    assert build_clean_report("正文", "正文", [])["cleaned"] is False


def test_cleaning_and_semantic_review_are_independent(monkeypatch):
    from app.agents import ingest_clean
    monkeypatch.setattr(ingest_clean, "review_samples", lambda samples: {
        "model_participated": False, "status": "review_required", "reason": "provider_unavailable"})
    result = preview_ingest_clean(text="正文。一秒记住【abc.info】，为您提供精彩小说阅读。")
    review = check_ingest_clean(prepared=result)
    assert result["report"]["cleaned"] is True
    assert review["status"] == "review_required"
    assert review["model_participated"] is False
