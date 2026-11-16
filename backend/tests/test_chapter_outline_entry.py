"""章纲纯逻辑层测试：归一化 / 缺失标记 / 章号连续性 / 时间窗 / 权威外物件。

跑法：项目根 `python -m pytest backend/tests/test_chapter_outline_entry.py -q`
（pytest.ini 的 pythonpath=backend 让 `from app...` 直接可用；不需要建库）。
"""
from app.generation.chapter_outline import (
    chapter_status,
    extract_time_tokens,
    missing_chapters,
    normalize_outline,
    outline_after_entries,
    validate_chapter_range,
    validate_single_chapter,
)


# ── 归一化与缺失标记 ────────────────────────────────────────────────────


def test_normalize_list_keeps_empty_and_turns_none_into_blank():
    assert normalize_outline(["第1章：甲", None, "  "]) == ["第1章：甲", "", ""]


def test_normalize_json_string():
    assert normalize_outline('["第1章：甲", null]') == ["第1章：甲", ""]


def test_normalize_newline_string():
    assert normalize_outline("第1章：甲\n第2章：乙\n") == ["第1章：甲", "第2章：乙"]


def test_chapter_status_marks_beyond_end():
    outline = ["第1章：甲", ""]
    assert chapter_status(outline, 1) == "confirmed"
    assert chapter_status(outline, 2) == "missing"
    assert chapter_status(outline, 3) == "beyond_end"


def test_missing_chapters_includes_written_but_outlined_shortage():
    """43 章正文、40 条章纲：41–43 必须被列出来，而不是被长度截掉。"""
    outline = [f"第{i}章：内容" for i in range(1, 41)]
    assert missing_chapters(outline, written_chapters=43) == [41, 42, 43]


def test_missing_chapters_respects_range():
    outline = ["第1章：甲", "", "第3章：丙"]
    assert missing_chapters(outline, from_chapter=2, to_chapter=2) == [2]
    assert missing_chapters(outline, from_chapter=1, to_chapter=3) == [2]


def test_outline_after_entries_reports_auto_filled_missing():
    outline = ["第1章：甲"]
    result = outline_after_entries(
        outline,
        [{"chapter": 4, "entry": "第4章：丁"}, {"chapter": 3, "entry": None}],
    )
    assert result["outline"] == ["第1章：甲", "", "", "第4章：丁"]
    assert result["auto_filled_missing"] == [2]
    assert result["changed"] == [4]


# ── 章号连续性 ──────────────────────────────────────────────────────────


def test_continuous_range_passes():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：甲"}, {"chapter": 45, "entry": "第45章：乙"}],
        from_chapter=44, to_chapter=45, outline=[], written_chapters=43,
    )
    assert report["ok"] is True
    assert report["errors"] == []
    assert any(item["rule"] == "chapter_continuity"
               and item["status"] == "checked"
               for item in report["checks"])


def test_gap_is_hard_error_and_lists_chapters():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：甲"}, {"chapter": 46, "entry": "第46章：丙"}],
        from_chapter=44, to_chapter=46, outline=[],
    )
    assert report["ok"] is False
    gap = [e for e in report["errors"] if e["code"] == "chapter_gap"]
    assert gap and gap[0]["chapters"] == [45]


def test_duplicate_chapter_is_hard_error():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "甲"}, {"chapter": 44, "entry": "乙"}],
        from_chapter=44, to_chapter=44, outline=[],
    )
    assert any(e["code"] == "chapter_duplicate" for e in report["errors"])


def test_empty_string_is_error_but_null_is_explicit_missing():
    empty = validate_chapter_range(
        [{"chapter": 44, "entry": "   "}], from_chapter=44, to_chapter=44, outline=[],
    )
    assert any(e["code"] == "empty_entry" for e in empty["errors"])

    explicit = validate_chapter_range(
        [{"chapter": 44, "entry": None}], from_chapter=44, to_chapter=44, outline=[],
    )
    assert explicit["ok"] is True
    assert not [e for e in explicit["errors"] if e["code"] == "empty_entry"]


def test_out_of_range_entry_is_error():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "甲"}, {"chapter": 45, "entry": "乙"},
         {"chapter": 46, "entry": "丙"}],
        from_chapter=44, to_chapter=45, outline=[],
    )
    extra = [e for e in report["errors"] if e["code"] == "chapter_out_of_range"]
    assert extra and extra[0]["chapters"] == [46]


def test_prefix_mismatch_is_warning_with_position():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第7章：甲"}],
        from_chapter=44, to_chapter=44, outline=[],
    )
    assert report["ok"] is True
    warning = next(w for w in report["warnings"] if w["code"] == "chapter_prefix_mismatch")
    assert warning["found"] == "7"
    assert warning["position"]["start"] == 0
    assert "第7章" in warning["position"]["snippet"]


def test_editing_written_chapter_is_visible_warning():
    report = validate_chapter_range(
        [{"chapter": 30, "entry": "第30章：甲"}],
        from_chapter=30, to_chapter=30, outline=[], written_chapters=43,
    )
    assert any(w["code"] == "written_chapter_edit" for w in report["warnings"])


# ── 时间窗 ──────────────────────────────────────────────────────────────


def test_extract_time_tokens_normalizes_period():
    tokens = extract_time_tokens("晚上10点，次日早上7点，22:30")
    assert [t["minutes"] for t in tokens] == [22 * 60, 7 * 60, 22 * 60 + 30]


def test_backwards_time_is_hard_error_with_position():
    report = validate_chapter_range(
        [
            {"chapter": 44, "entry": "第44章《夜审》：晚上10点，林川进了值班室。"},
            {"chapter": 45, "entry": "第45章《清晨》：早上7点，林川离开值班室。"},
        ],
        from_chapter=44, to_chapter=45, outline=[],
    )
    assert report["ok"] is False
    error = next(e for e in report["errors"] if e["code"] == "time_window_backwards")
    assert error["chapter"] == 45
    assert error["previous_token"] == "晚上10点"
    assert error["token"] == "早上7点"
    assert "早上7点" in error["position"]["snippet"]


def test_forward_marker_suppresses_backwards_error():
    report = validate_chapter_range(
        [
            {"chapter": 44, "entry": "第44章：晚上10点，林川进了值班室。"},
            {"chapter": 45, "entry": "第45章：第二天早上7点，林川离开值班室。"},
        ],
        from_chapter=44, to_chapter=45, outline=[],
    )
    assert not [e for e in report["errors"] if e["code"] == "time_window_backwards"]


def test_same_time_is_warning():
    report = validate_chapter_range(
        [
            {"chapter": 44, "entry": "第44章：晚上9点，甲。"},
            {"chapter": 45, "entry": "第45章：晚上9点，乙。"},
        ],
        from_chapter=44, to_chapter=45, outline=[],
    )
    assert report["ok"] is True
    assert any(w["code"] == "time_window_same_without_marker"
               for w in report["warnings"])


def test_same_time_marker_suppresses_warning():
    report = validate_chapter_range(
        [
            {"chapter": 44, "entry": "第44章：晚上9点，甲。"},
            {"chapter": 45, "entry": "第45章：同一时间，乙在另一处。"},
        ],
        from_chapter=44, to_chapter=45, outline=[],
    )
    assert not [w for w in report["warnings"]
                if w["code"] == "time_window_same_without_marker"]


def test_no_time_tokens_is_visible_skip_not_pass():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：林川去了值班室。"}],
        from_chapter=44, to_chapter=44, outline=[],
    )
    assert "time_window_between_chapters:no_absolute_time_tokens_in_range" \
        in report["skipped_checks"]
    skipped = [c for c in report["checks"]
               if c["rule"] == "time_window_between_chapters"]
    assert skipped and skipped[0]["status"] == "skipped"


def test_node_time_mismatch_is_warning():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：早上7点，林川进了值班室。"}],
        from_chapter=44, to_chapter=44, outline=[],
        node_windows=[{
            "node_id": "n9", "chapter_start": 44, "chapter_end": 50,
            "title": "夜审", "entry_condition": "时间：晚上10点",
            "exit_condition": "",
        }],
    )
    warning = next(w for w in report["warnings"]
                   if w["code"] == "time_window_node_mismatch")
    assert warning["node_id"] == "n9"
    assert warning["token"] == "早上7点"
    assert warning["node_token"] == "晚上10点"


# ── 权威外物件 ──────────────────────────────────────────────────────────

_PREMISE = "医院值班室的金属托盘里放着一把镊子和几卷纱布。"


def test_quoted_unknown_term_is_hard_error_with_term_and_position():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：他拿起「录音笔」，走进值班室。"}],
        from_chapter=44, to_chapter=44, outline=[], premise=_PREMISE,
    )
    assert report["ok"] is False
    error = next(e for e in report["errors"]
                 if e["code"] == "authority_quoted_term_unknown")
    assert error["term"] == "录音笔"
    assert "录音笔" in error["position"]["snippet"]
    assert error["position"]["start"] >= 0


def test_quoted_term_found_in_premise_passes():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：他拿起「镊子」，走进值班室。"}],
        from_chapter=44, to_chapter=44, outline=[], premise=_PREMISE,
    )
    assert not [e for e in report["errors"]
                if e["code"] == "authority_quoted_term_unknown"]


def test_quoted_term_found_in_kg_passes():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：他拿起「听诊器」。"}],
        from_chapter=44, to_chapter=44, outline=[], premise="",
        kg_names=["听诊器"],
    )
    assert not [e for e in report["errors"]
                if e["code"] == "authority_quoted_term_unknown"]


def test_suffix_object_is_warning_with_term_and_position():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：他拿起一把铜牌，走进值班室。"}],
        from_chapter=44, to_chapter=44, outline=[], premise=_PREMISE,
    )
    assert report["ok"] is True
    warning = next(w for w in report["warnings"]
                   if w["code"] == "authority_item_suspect")
    assert warning["term"] == "铜牌"
    assert "铜牌" in warning["position"]["snippet"]


def test_empty_authority_is_visible_skip():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：他拿起「录音笔」。"}],
        from_chapter=44, to_chapter=44, outline=[], premise="",
    )
    assert "authority_quoted:authority_empty_or_too_thin" in report["skipped_checks"]
    assert "authority_suffix:authority_empty_or_too_thin" in report["skipped_checks"]
    assert not [e for e in report["errors"]
                if e["code"] == "authority_quoted_term_unknown"]


# ── 回执完整性 ──────────────────────────────────────────────────────────


def test_report_always_lists_every_rule():
    report = validate_chapter_range(
        [{"chapter": 44, "entry": "第44章：林川去了值班室。"}],
        from_chapter=44, to_chapter=44, outline=[],
    )
    rules = {item["rule"] for item in report["checks"]}
    assert {
        "chapter_continuity",
        "time_window_between_chapters",
        "time_window_node",
        "authority_quoted",
        "authority_suffix",
    } <= rules
    assert all(item["status"] in {"checked", "skipped"} for item in report["checks"])


def test_single_chapter_wrapper_matches_range_rules():
    report = validate_single_chapter(
        44, "第44章：晚上10点，甲。", outline=[], written_chapters=43,
    )
    assert report["ok"] is True
    assert any(item["rule"] == "chapter_continuity" for item in report["checks"])
