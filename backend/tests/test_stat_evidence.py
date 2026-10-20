"""test_stat_evidence.py —— 统计型证据放行（v2.263）

## 问题

LLM 会把我们塞进 prompt 的**确定性指标**当成「原文证据」引用：

    节奏控制: ["通过短句和短段的使用，加快叙事节奏
              （证据：句长 61.1±36.4 字，短句(≤5字) 0.0%，段落 36 段...）"]

这段文字确实写在 prompt 里（`_format_features_prefix` 渲染），但不在
chunk_text 里。旧 `_validate_evidence` 判它「证据不存在」→ rejected += 1。

**这是合理引用被当成幻觉。** prompt 明确要求「解释作者为什么这样写」，
LLM 引用这些数字完全符合要求。

## 判据

1. 必须命中至少一个统计模式（数字+单位 / 指标名）
2. 剥掉统计片段后剩余中文 <= 8 字 → 判为统计

第 2 步是关键：让「他等了三分钟才走」这种含数字的真叙事被正确拒绝。
"""
import pathlib
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import (  # noqa: E402
    _is_statistical_evidence,
    _validate_evidence,
)

CORPUS = pathlib.Path(r"C:\Users\h'h'h\Downloads\雪中悍刀行.txt")
_TEXT = None


def chunk() -> str:
    global _TEXT
    if _TEXT is None:
        _TEXT = CORPUS.read_bytes().decode("gb18030", errors="ignore")
    start = _TEXT.find("第一章")
    return _TEXT[start:start + 6000]


def test_a_all_stat_kinds_pass():
    """A: 五种统计形态全部放行（±、%、字、千字、句）。"""
    cases = [
        # 最小/最大/百分比
        "（证据：句长 61.1±36.4 字，短句(≤5字) 0.0%，段落 36 段，段长均值 74.6 字）",
        # 段长/占比
        "（证据：段长均值 62.7 字，一两句短段占 95.2%，对白开头段占 9.5%）",
        # 对白统计
        "（证据：对白 14 句，平均 23.9 字，短对白(≤10字) 50.0%，长对白(≥50字) 14.3%）",
        # 密度型（只命中「/千字」——v2.263 初版漏掉的那类）
        "（证据：情绪直陈 0.67/千字，动作承载 0.0/千字）",
        "（证据：问号 1.33/千字，省略号 0.0/千字）",
    ]
    for c in cases:
        ev = c.strip("（）").replace("证据：", "")
        assert _is_statistical_evidence(ev), f"应判统计：{ev[:40]}"
        rule = f"某规则{c}"
        assert _validate_evidence(rule, chunk()), f"应放行：{ev[:40]}"


def test_b_fabricated_rejected():
    """B: 编造证据仍被拒（放行统计不能变成放行幻觉）。"""
    cases = [
        "角色总是微笑（证据：他露出了灿烂的笑容）",
        "节奏极快（证据：刀光一闪，人头落地）",
        "完全编造的规则（证据：从来没有人这样写过）",
        "世界观宏大（证据：三千大世界无尽星空）",
        "情绪克制（证据：他强忍着没有流泪）",
    ]
    for c in cases:
        assert not _validate_evidence(c, chunk()), f"不该放行：{c[:40]}"


def test_c_real_quotes_pass():
    """C: 原文真实引用照常放行。"""
    cases = [
        "用环境铺陈（证据：北凉王府龙盘虎踞于清凉山）",
        "以对话推进（证据：连堂堂大柱国北凉王都得蹲在那里好言相劝）",
    ]
    for c in cases:
        assert _validate_evidence(c, chunk()), f"应放行：{c[:40]}"


def test_d_numbers_in_narrative_rejected():
    """D: 含数字的真叙事不能被当统计放行。

    这是 v2.263 判据的关键测试——第一步「命中统计模式」不够，
    必须有第二步「剥掉统计后剩余中文极少」。
    """
    cases = [
        "精确时间（证据：他等了三分钟才走）",
        "人数具体（证据：两百骑如狼似虎的王府亲兵）",
        "短句写动作（证据：徐凤年笑着说好）",
    ]
    for c in cases:
        ev = c.split("证据：")[-1].rstrip("）")
        assert not _is_statistical_evidence(ev), f"不该判统计：{ev}"
        assert not _validate_evidence(c, chunk()), f"不该放行：{c[:40]}"


def test_e_indicator_names_alone():
    """E: 只有指标名、没有数字的也判统计（如「段长均值」「对白开头段占」）。"""
    cases = [
        "证据：段长均值明显偏短，一两句短段占绝大多数",
        "证据：短对白占比很高，长对白占比极低",
    ]
    for c in cases:
        ev = c.replace("证据：", "")
        assert _is_statistical_evidence(ev), f"应判统计：{ev[:40]}"


def test_f_empty_and_short():
    """F: 空证据、过短证据仍拒绝（不受本次改动影响）。"""
    assert not _validate_evidence("规则（证据：）", chunk())
    assert not _validate_evidence("规则（证据：他）", chunk())
    assert not _validate_evidence("", chunk())


if __name__ == "__main__":
    tests = [
        ("A 五种统计形态放行", test_a_all_stat_kinds_pass),
        ("B 编造证据仍拒", test_b_fabricated_rejected),
        ("C 原文引用照常放行", test_c_real_quotes_pass),
        ("D 数字叙事不当统计", test_d_numbers_in_narrative_rejected),
        ("E 纯指标名判统计", test_e_indicator_names_alone),
        ("F 空/短证据仍拒", test_f_empty_and_short),
    ]
    passed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ✓ {name}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {name}: {e}")
    print(f"\n{passed}/{len(tests)} pass")
    sys.exit(0 if passed == len(tests) else 1)
