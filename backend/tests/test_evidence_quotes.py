"""test_evidence_quotes.py —— 引号归一化（v2.263 附带修的真 bug）

## 问题

LLM 输出证据时常把原文用**引号包裹**，且引号类型五花八门：

    证据：徐凤年‘自嘲道：贪生怕死而已’     ← 弯单引号 U+2018 / U+2019
    证据：“徐凤年摇头微笑道”                ← 弯双引号 U+201C / U+201D
    证据：'徐凤年摇头微笑道'                ← ASCII 单引号
    证据："徐凤年摇头微笑道"                ← ASCII 双引号

原文里没有引号。旧 `_norm` 只剥离 ASCII 单引号 + 弯双引号，
**漏了弯单引号 `‘’`** —— 于是这类证据永远匹配不上，被当成幻觉拒掉。

雪中悍刀行 v2.268 实测：**259 条规则**（占 extract 维度规则的 12%）
因此被误拒。这是 rejected 从 734 降到 355 的主因（68%）。

## 注意

这个 bug 是 v2.263 重写 `_validate_evidence` 时**顺手修掉**的——
当时本意只是整理代码，没意识到它是 rejected 的主因。commit 里把它
记成「统计证据放行」，主因记错了，此处补上。
"""
import pathlib
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import _validate_evidence  # noqa: E402

CORPUS = pathlib.Path(r"C:\Users\h'h'h\Downloads\雪中悍刀行.txt")
_TEXT = None


def chunk() -> str:
    global _TEXT
    if _TEXT is None:
        _TEXT = CORPUS.read_bytes().decode("gb18030", errors="ignore")
        start = _TEXT.find("第一章")
        _TEXT = _TEXT[start:start + 6000]
    return _TEXT


def test_a_curly_single_quotes():
    """A: 弯单引号包裹的证据能匹配（旧版漏了这个，259 条被误拒）。"""
    cases = [
        "用环境铺陈（证据：\u2018北凉王府龙盘虎踞于清凉山\u2019）",
        "环境描写细致（证据：\u2018北凉王府龙盘虎踞于清凉山\u2019\u2019）",
    ]
    for c in cases:
        assert _validate_evidence(c, chunk()), f"弯单引号没归一化: {c[:40]}"


def test_b_curly_double_quotes():
    """B: 弯双引号包裹的证据能匹配。"""
    case = "用环境铺陈（证据：\u201c北凉王府龙盘虎踞于清凉山\u201d）"
    assert _validate_evidence(case, chunk()), "弯双引号没归一化"


def test_c_ascii_quotes():
    """C: ASCII 引号也能匹配。"""
    for c in [
        "用环境铺陈（证据：'北凉王府龙盘虎踞于清凉山'）",
        '用环境铺陈（证据："北凉王府龙盘虎踞于清凉山"）',
    ]:
        assert _validate_evidence(c, chunk()), f"ASCII 引号没归一化: {c[:40]}"


def test_d_mixed_quotes():
    """D: 混合引号（外层弯单、内层 ASCII）也能匹配。"""
    # 外层弯单、内层 ASCII：归一化后应拼成 "北凉王府龙盘虎踞于清凉山"
    case = "环境铺陈（证据：\u2018北凉王府龙盘\u2019'虎踞于清凉山')"
    assert _validate_evidence(case, chunk()), "混合引号没正确归一化"


def test_e_quotes_still_reject_fabricated():
    """E: 加了引号归一化，但不能放行编造内容（关键回归）。"""
    cases = [
        "编造规则（证据：\u2018他露出了灿烂的笑容\u2019）",
        "编造规则（证据：\u201c从来没有人这样写过\u201d）",
        "编造规则（证据：'三千大世界无尽星空'）",
    ]
    for c in cases:
        assert not _validate_evidence(c, chunk()), f"编造内容被放行: {c[:40]}"


def test_f_no_quotes_still_works():
    """F: 不带引号的证据照常匹配（不因归一化改动而破坏）。"""
    case = "用环境铺陈（证据：北凉王府龙盘虎踞于清凉山）"
    assert _validate_evidence(case, chunk())


if __name__ == "__main__":
    tests = [
        ("A 弯单引号", test_a_curly_single_quotes),
        ("B 弯双引号", test_b_curly_double_quotes),
        ("C ASCII 引号", test_c_ascii_quotes),
        ("D 混合引号", test_d_mixed_quotes),
        ("E 编造内容仍拒", test_e_quotes_still_reject_fabricated),
        ("F 无引号照常", test_f_no_quotes_still_works),
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
