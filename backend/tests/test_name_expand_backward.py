"""test_name_expand_backward.py —— 尾部残片补全（v2.239）

背景：v2.238 真链路（雪中悍刀行 990 章）产出的角色声音里，主角
徐凤年以碎片「凤年」出现——LLM 直接输出了缺首字的名字。
`_expand_names_from_corpus` 只能补尾字（徐凤 → 徐凤年），救不了。

真实语料 probe 结果（512 万字全本）：
  残片前驱高度集中：凤年→徐 99.5% / 仙芝→王 100% / 淳罡→李 100%
  完整名前驱分散：徐凤年 46.7%（空格）/ 王仙芝 16.2% / 李淳罡 12.2%
  虚词也不误扩：江湖 16.3% / 公子 11.7% / 师父 16.5%
"""
import pathlib
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import (  # noqa: E402
    _enhance_names_with_corpus,
    _expand_names_backward,
)

CORPUS_PATH = pathlib.Path(r"C:\Users\h'h'h\Downloads\雪中悍刀行.txt")
_TEXT: str | None = None


def corpus() -> str:
    global _TEXT
    if _TEXT is None:
        _TEXT = CORPUS_PATH.read_bytes().decode("gb18030", errors="ignore")
    return _TEXT


def test_a_backward_fragments():
    """A: 9 个尾部残片全部补回首字（真实语料）。"""
    if not CORPUS_PATH.exists():
        print("  (跳过：语料不存在)")
        return
    frags = ["凤年", "仙芝", "北枳", "芝豹", "太阿", "淳罡", "剑棠", "禄山", "长卿"]
    expect = ["徐凤年", "王仙芝", "徐北枳", "陈芝豹", "邓太阿",
              "李淳罡", "顾剑棠", "褚禄山", "曹长卿"]
    out = _expand_names_backward(frags, corpus(), frac=0.85, max_extra=1)
    missed = [(f, e) for f, e in zip(frags, expect) if out.get(f, ("", 0))[0] != e]
    assert not missed, f"未补全：{missed}"


def test_b_full_names_untouched():
    """B: 完整名的前驱是分散的，绝不能被"扩展"。"""
    if not CORPUS_PATH.exists():
        print("  (跳过：语料不存在)")
        return
    fulls = ["徐凤年", "王仙芝", "徐北枳", "陈芝豹", "邓太阿",
             "李淳罡", "顾剑棠", "褚禄山", "曹长卿", "徐骁", "姜泥"]
    out = _expand_names_backward(fulls, corpus(), frac=0.85, max_extra=1)
    wrong = [f for f in fulls if f in out]
    assert not wrong, f"完整名被误扩：{[(w, out[w][0]) for w in wrong]}"


def test_c_garbage_untouched():
    """C: 称谓/地名/泛称不会被补成"名字"。"""
    if not CORPUS_PATH.exists():
        print("  (跳过：语料不存在)")
        return
    garbage = ["江湖", "公子", "师父", "皇帝", "家伙", "许多", "高手", "武当", "西楚"]
    out = _expand_names_backward(garbage, corpus(), frac=0.85, max_extra=1)
    assert not out, f"虚词被误扩：{list(out.keys())}"


def test_d_unified_handles_both_directions():
    """D: 统一入口同时处理两个方向，前向优先。

    徐凤 → 徐凤年（前向补尾字）
    凤年 → 徐凤年（后向补首字）
    两者都指向同一个完整名，这是正确行为。
    """
    if not CORPUS_PATH.exists():
        print("  (跳过：语料不存在)")
        return
    names = ["徐凤", "王仙", "凤年", "仙芝", "徐凤年", "江湖"]
    out = _enhance_names_with_corpus(names, corpus())
    assert out.get("徐凤") == "徐凤年", f"前向失败：{out.get('徐凤')}"
    assert out.get("凤年") == "徐凤年", f"后向失败：{out.get('凤年')}"
    assert out.get("王仙") == "王仙芝", f"前向失败：{out.get('王仙')}"
    assert out.get("仙芝") == "王仙芝", f"后向失败：{out.get('仙芝')}"
    assert "徐凤年" not in out, "完整名不该出现在映射里"
    assert "江湖" not in out, "虚词不该出现在映射里"


def test_e_high_threshold_prevents_overreach():
    """E: frac=0.85 挡住「莫雨道」这种 60% 的巧合。

    合成语料：「莫雨道」30 次 + 「莫雨蝶」20 次 = 莫雨 共 50 次。
    「莫雨道」占 60%，frac=0.5 会误扩成「莫雨道莫」，0.85 不会。
    这是 v2.234 老契约 `_extend_name_candidates(frac=0.15)` 的等价阈值
    （1 - 0.15 = 0.85），不能为了让后向扩展"更宽松"就降到 0.5。
    """
    text = "莫雨道" * 30 + "莫雨蝶，" * 20
    out = _enhance_names_with_corpus(["莫雨"], text)
    assert "莫雨" not in out, f"60% 巧合被误扩：{out.get('莫雨')}"


def test_f_min_samples():
    """F: 出现次数 < 20 不做扩展（集中度不可信）。"""
    text = "凤年，" * 5
    out = _expand_names_backward(["凤年"], text, frac=0.85, max_extra=1)
    assert not out, f"样本不足仍扩展：{out}"


if __name__ == "__main__":
    tests = [
        ("A 尾部残片补首字", test_a_backward_fragments),
        ("B 完整名不误扩", test_b_full_names_untouched),
        ("C 虚词不误扩", test_c_garbage_untouched),
        ("D 双向统一入口", test_d_unified_handles_both_directions),
        ("E 高阈值防误扩", test_e_high_threshold_prevents_overreach),
        ("F 样本下限", test_f_min_samples),
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
