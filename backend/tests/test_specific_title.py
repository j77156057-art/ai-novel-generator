"""test_specific_title.py —— 具体化称呼不被当泛称拦掉（v2.264）

## 问题

v2.237 的 `_VOICE_STOP_WORDS` 把「和尚」「儒生」这类泛称收进去是对的，
但**顺带把「老和尚」「老夫子」「老儒生」也拦掉了**——它们在原文里是
真角色，作者整本书就这么称呼，从不具名。

雪中悍刀行 v2.261 真链路实测，这几条角色声音的证据**都包含名字本身**：

    老和尚：口头禅「老衲」语气深沉（证据：老和尚轻声道："问佛不如问己。"）
    老夫子：语言文雅，富有哲理（证据：老夫子笑了笑，叮嘱着说...）
    老儒生：口头禅「少朴」，语气严肃（证据：老儒生笑眯眯道："少朴，喝一杯？"）
    洪姨  ：口头禅丰富，语气平和（证据：洪姨嗑着瓜子...）

抽取是准确的，是过滤过度。

## 判据

三种形态：
1. 修饰前缀 + 称谓词根：老和尚 / 小和尚 / 老掌柜 / 老夫子
2. 姓氏 + 称谓：洪姨 / 王叔（首字必须在姓氏白名单里）
3. 职业名词：铁匠 / 屠夫 / 掌柜

## 必须不误伤

「公子」「王爷」末字是「子」「爷」，若只看末字会误判成「姓氏+称谓」，
所以形态 2 要求首字是姓氏白名单。

「和尚」「道士」本身是泛称（指任意僧人/道人），只有加前缀才具体化。
"""
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import (  # noqa: E402
    _is_specific_title,
    _is_valid_voice_char_name,
)


def test_a_prefix_plus_root():
    """A: 修饰前缀 + 称谓词根 → 具体化称呼。"""
    cases = ["老和尚", "老夫子", "老儒生", "老道士", "小和尚",
             "老掌柜", "大小姐"]
    for c in cases:
        assert _is_specific_title(c), f"应判具体化：{c}"
        assert _is_valid_voice_char_name(c), f"应放行：{c}"


def test_b_surname_plus_title():
    """B: 姓氏 + 称谓 → 具体化称呼（洪姨 / 王叔）。"""
    for c in ["洪姨", "王叔", "李伯", "赵婆"]:
        assert _is_specific_title(c), f"应判具体化：{c}"
        assert _is_valid_voice_char_name(c), f"应放行：{c}"


def test_c_occupation():
    """C: 职业名词直接当称呼 → 具体化（铁匠 / 屠夫）。"""
    for c in ["铁匠", "屠夫", "掌柜", "船夫"]:
        assert _is_specific_title(c), f"应判具体化：{c}"
        assert _is_valid_voice_char_name(c), f"应放行：{c}"


def test_d_generic_titles_rejected():
    """D: 纯泛称仍被拦（关键回归——不能因为放宽而漏垃圾）。"""
    cases = ["公子", "师父", "皇帝", "王爷", "高手", "家伙", "殿下",
             "世子", "将军", "陛下", "大人", "前辈"]
    for c in cases:
        assert not _is_specific_title(c), f"不该判具体化：{c}"
        assert not _is_valid_voice_char_name(c), f"不该放行：{c}"


def test_e_bare_religious_titles_rejected():
    """E: 「和尚」「道士」本身是泛称，只有加前缀才具体化。

    这是形态 3 的关键边界：`_OCCUPATION_TITLES` 只收职业（铁匠），
    不收宗教身份（和尚/道士）。
    """
    for c in ["和尚", "道士", "道长", "儒生", "夫子"]:
        assert not _is_specific_title(c), f"裸称谓不该具体化：{c}"
        assert not _is_valid_voice_char_name(c), f"不该放行：{c}"


def test_f_adverbs_and_generic_nouns():
    """F: 时间副词 / 方位词 / 泛称名词仍被拦。

    这些是 v2.264 补进 `_VOICE_STOP_WORDS` 的（雪中真链路漏网的
    高频虚词，在图谱里排前 150 位所以会进候选池）。
    """
    cases = ["后来", "南北", "东西", "老人", "老者", "此时", "片刻", "良久"]
    for c in cases:
        assert not _is_specific_title(c), f"不该判具体化：{c}"
        assert not _is_valid_voice_char_name(c), f"不该放行：{c}"


def test_g_real_names_unaffected():
    """G: 真角色名不受影响。"""
    for c in ["徐凤年", "王仙芝", "李淳罡", "褚禄山", "姜泥", "徐骁",
              "曹长卿", "邓太阿", "徐北枳", "轩辕青锋"]:
        assert _is_valid_voice_char_name(c), f"误杀真角色：{c}"


def test_h_short_and_invalid():
    """H: 过短/含结构标记的名字仍拒绝。"""
    assert not _is_valid_voice_char_name("老")
    assert not _is_valid_voice_char_name("")
    assert not _is_valid_voice_char_name("老和尚（证据")
    assert not _is_valid_voice_char_name("老和尚：自称")


if __name__ == "__main__":
    tests = [
        ("A 前缀+词根", test_a_prefix_plus_root),
        ("B 姓氏+称谓", test_b_surname_plus_title),
        ("C 职业名词", test_c_occupation),
        ("D 纯泛称仍拦", test_d_generic_titles_rejected),
        ("E 裸宗教称谓仍拦", test_e_bare_religious_titles_rejected),
        ("F 副词/方位词仍拦", test_f_adverbs_and_generic_nouns),
        ("G 真角色不误杀", test_g_real_names_unaffected),
        ("H 过短/结构标记仍拒", test_h_short_and_invalid),
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
