"""单段复读/退化检测（repetition）测试。

守护的纪律（与 `backend/app/generation/repetition.py` 的模块 docstring 对应）：

  R1 **口径钉死**：五段真实正文的全部指标与 2026-11-12 诊断表逐项一致
     （断句、top_sentence_repeat、覆盖率、dup5/dup20、重复段落、结尾完整性）。
     任何一处口径漂移都会让这个测试先红，而不是等闸门上线后才发现数字对不上；
  R2 判定：四个正常段 → `ok`，gen4 → `degenerate`，且原因里能读到
     「多少句只有多少个不同句子」「循环占多少字」这类人话；
  R3 短文本（< 200 字）返回 `skipped_short`，**不是 ok**——「不判定」和
     「判正常」是两件事，混起来会让短段静默通过闸门；
  R4 边界：空串 / None / 纯空白不炸；纯对白「互不相同」判 ok、
     「三句循环」判退化；
  R5 阈值可注入且确实生效（默认值只在一处定义）；
  R6 `LoopWindow` 是解释性指标：正常段不产生循环窗，gen4 的循环窗落在
     正文后半段且唯一句占比很低。
"""

from __future__ import annotations

import json
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pytest  # noqa: E402

from app.generation.repetition import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    MAX_DUP20,
    MAX_REPEATED_SENTENCE_COVERAGE,
    MAX_TOP_SENTENCE_REPEAT,
    MIN_CHARS,
    VERDICT_DEGENERATE,
    VERDICT_OK,
    VERDICT_SKIPPED_SHORT,
    RepetitionThresholds,
    analyze_repetition,
    find_loop_window,
    measure_repetition,
    split_sentences,
)
from fixtures_repetition_segments import DEGENERATE_IDS, NORMAL_IDS, SEGMENTS  # noqa: E402

# 五段的期望指标：与 2026-11-12 诊断表（metrics_all.json / final_table.txt）逐项一致。
# 字段顺序：chars, sentences, unique_ratio, top_repeat, coverage, dup5, dup20,
#           duplicate_paragraph_occurrences, ends_complete
EXPECTED = {
    1: (1413, 93, 0.839, 9, 0.053, 0.227, 0.000, 0, True),
    2: (1036, 39, 1.000, 1, 0.000, 0.101, 0.000, 0, True),
    3: (1341, 63, 0.889, 8, 0.006, 0.065, 0.000, 0, True),
    4: (2273, 124, 0.468, 18, 0.525, 0.680, 0.566, 50, False),
    5: (1109, 67, 0.866, 5, 0.064, 0.186, 0.000, 0, True),
}


# ─────────────────────────────────────────────────────────────────────────────
# R1 口径钉死
# ─────────────────────────────────────────────────────────────────────────────


def test_split_sentences_keeps_closing_quote_on_next_sentence():
    """断句口径：句末 `。！？` 后断开，引号留在**下一句**句首（与诊断表一致）。"""
    text = "“你还能找到那个秘密吗？”那个身影问道。\n\n老李没有回答，他的目光紧紧地盯着那个身影。"
    assert split_sentences(text) == [
        "“你还能找到那个秘密吗？",
        "”那个身影问道。",
        "老李没有回答，他的目光紧紧地盯着那个身影。",
    ]


@pytest.mark.parametrize("gen_id", sorted(EXPECTED))
def test_pinned_metrics_match_diagnosis_table(gen_id: int):
    """五段真实正文的指标必须与诊断表逐项相等（口径漂移的回归闸）。"""
    metrics = measure_repetition(SEGMENTS[gen_id]["text"])
    got = (
        metrics.chars,
        metrics.sentences,
        metrics.unique_sentence_ratio,
        metrics.top_sentence_repeat,
        metrics.repeated_sentence_coverage,
        metrics.dup5,
        metrics.dup20,
        metrics.duplicate_paragraph_occurrences,
        metrics.ends_complete,
    )
    assert got == EXPECTED[gen_id]


def test_dup5_is_reported_but_does_not_judge():
    """gen1 的 dup5=0.227（高于多数正常段）仍判 ok：dup5 只报告、不参与判定。"""
    report = analyze_repetition(SEGMENTS[1]["text"])
    assert report.metrics.dup5 == 0.227
    assert report.verdict == VERDICT_OK


# ─────────────────────────────────────────────────────────────────────────────
# R2 判定与人类可读原因
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("gen_id", NORMAL_IDS)
def test_normal_segments_pass(gen_id: int):
    report = analyze_repetition(SEGMENTS[gen_id]["text"])
    assert report.verdict == VERDICT_OK
    assert report.is_degenerate is False
    assert report.triggers == ()
    assert "未命中退化判据" in report.reason_text


def test_degenerate_segment_is_flagged_with_readable_reasons():
    report = analyze_repetition(SEGMENTS[4]["text"])

    assert report.verdict == VERDICT_DEGENERATE
    assert report.is_degenerate is True
    # 两条判据同时命中：单句高频 + 高覆盖；dup20 也越线
    assert any("top_sentence_repeat=18" in t for t in report.triggers)
    assert any("dup20=0.566" in t for t in report.triggers)

    text = report.reason_text
    assert "124 句只有 58 个不同句子" in text
    assert "最高频单句出现 18 次" in text
    assert "52.5%" in text
    assert "20-gram 重复率 0.566" in text
    assert "为模板循环" in text
    assert "疑似被截断" in text  # 结尾是「老李没有回答，」半句


def test_degenerate_report_is_json_serializable():
    """接口/日志回显要能直接 json 化（不夹带 dataclass/thresholds 对象）。"""
    payload = analyze_repetition(SEGMENTS[4]["text"]).to_dict()
    dumped = json.dumps(payload, ensure_ascii=False)
    assert payload["verdict"] == VERDICT_DEGENERATE
    assert payload["loop"]["chars"] > 1000
    assert "degenerate" in dumped


# ─────────────────────────────────────────────────────────────────────────────
# R3 短文本不判定
# ─────────────────────────────────────────────────────────────────────────────


def test_short_text_is_skipped_not_ok():
    """150 字的高重复文本 → skipped_short（不判定），绝不能被当成 ok。"""
    text = "他推开门，屋里的灯还亮着。" * 12  # 156 字
    report = analyze_repetition(text)
    assert report.metrics.chars < MIN_CHARS
    assert report.verdict == VERDICT_SKIPPED_SHORT
    assert report.verdict != VERDICT_OK
    assert str(MIN_CHARS) in report.reason_text


def test_text_just_over_min_chars_is_judged():
    """跨过 200 字门槛就恢复判定：同一循环文本加长后必须判退化。"""
    text = "他推开门，屋里的灯还亮着。" * 30  # 390 字，全部是同一句
    report = analyze_repetition(text)
    assert report.metrics.chars >= MIN_CHARS
    assert report.verdict == VERDICT_DEGENERATE


# ─────────────────────────────────────────────────────────────────────────────
# R4 空文本 / 纯对白
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", None, "   \n\n  ", "\n\n\n"])
def test_empty_text_does_not_crash(text):
    report = analyze_repetition(text)
    assert report.verdict == VERDICT_SKIPPED_SHORT
    assert report.metrics.chars < MIN_CHARS
    assert report.metrics.sentences == 0
    assert report.metrics.top_sentence_repeat == 0
    assert report.metrics.ends_complete is False
    assert report.loop is None


def test_pure_dialogue_all_distinct_is_ok():
    """纯对白但每句都不同 → 正常（对白密集不是退化）。"""
    tags = ["他问道", "她轻声说", "老人叹了口气", "孩子小声嘟囔", "那人压低声音说"]
    lines = [
        f"“第{i}件事，你打算怎么处理？”{tags[i % len(tags)]}。" for i in range(1, 26)
    ]
    report = analyze_repetition("\n".join(lines))
    assert report.metrics.chars > MIN_CHARS
    assert report.verdict == VERDICT_OK


def test_dialogue_tag_repeat_flags_only_in_short_text():
    """已知的过敏感区：同一对白标签高频重复，只在短文本里会触发判据。

    断句把 `”他问道。` 切成独立一句，25 份同样的标签只占 125 字——放在 550 字
    的短段里覆盖率 22%（越过 0.20），放在 2000 字以上的真实段长里只有 6%。
    这是沿用诊断口径（覆盖率按全文算）的自觉取舍，**不是**该段的循环退化；
    之所以保留这个测试，是因为闸门上线前必须知道这条边界存在。
    """
    lines = [f"“第{i}件事，你打算怎么处理？”他问道。" for i in range(1, 26)]
    short = "\n".join(lines)
    assert analyze_repetition(short).verdict == VERDICT_DEGENERATE

    padded = short + "\n\n" + "\n".join(
        f"院子里的第{i}棵梧桐落了三片叶子，台阶上的水痕又深了一分。" for i in range(1, 41)
    )
    assert analyze_repetition(padded).verdict == VERDICT_OK


def test_pure_dialogue_loop_is_degenerate():
    """三句对白循环 30 遍 → 退化（对白循环与叙述循环同样处理）。"""
    unit = (
        "“你还能找到那个秘密吗？”那个身影问道。\n"
        "老李没有回答，他的目光紧紧地盯着那个身影。\n"
        "那个身影看着老李，他的脸上露出了一个冷笑。\n"
    )
    report = analyze_repetition(unit * 30)
    assert report.verdict == VERDICT_DEGENERATE
    assert report.metrics.top_sentence_repeat >= 20
    assert report.metrics.repeated_sentence_coverage > 0.9


# ─────────────────────────────────────────────────────────────────────────────
# R5 阈值可注入
# ─────────────────────────────────────────────────────────────────────────────


def test_default_thresholds_are_the_documented_values():
    assert DEFAULT_THRESHOLDS == RepetitionThresholds()
    assert (MIN_CHARS, MAX_TOP_SENTENCE_REPEAT, MAX_REPEATED_SENTENCE_COVERAGE, MAX_DUP20) == (
        200,
        10,
        0.20,
        0.30,
    )


def test_injected_thresholds_take_effect():
    """把三条线抬到不可能的位置 → gen4 也必须判 ok（证明判定真的读参数）。"""
    loose = RepetitionThresholds(
        max_top_sentence_repeat=10_000,
        max_repeated_sentence_coverage=1.0,
        max_dup20=1.0,
    )
    report = analyze_repetition(SEGMENTS[4]["text"], loose)
    assert report.verdict == VERDICT_OK
    # 指标本身不随阈值变化
    assert report.metrics.top_sentence_repeat == 18
    # 默认阈值下同一条文本仍然判退化
    assert analyze_repetition(SEGMENTS[4]["text"]).verdict == VERDICT_DEGENERATE


# ─────────────────────────────────────────────────────────────────────────────
# R6 循环窗（解释性指标）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("gen_id", NORMAL_IDS)
def test_normal_segments_have_no_loop_window(gen_id: int):
    assert find_loop_window(SEGMENTS[gen_id]["text"]) is None


def test_degenerate_segment_loop_window_covers_tail():
    loop = find_loop_window(SEGMENTS[4]["text"])
    assert loop is not None
    # 循环窗从正文中段开始，覆盖后半段，且句种极少
    assert 800 <= loop.start_char <= 1100
    assert loop.chars > 1200
    assert loop.share > 0.5
    assert loop.unique_ratio <= 0.15
    assert loop.sentences >= 60


def test_loop_window_requires_enough_sentences():
    """句数不足 `loop_min_sentences` 时不构造循环窗（避免把短文本的重复当循环）。"""
    assert find_loop_window("同一句话。" * 5) is None
