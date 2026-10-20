"""test_repeated_ads.py —— strip_repeated_ads 通用广告识别

数据：3 本真链路（雪中/斗破/大奉） + 3 个合成用例。

设计：3 条结构判据
1. 段长 5-80
2. 频次 >= 15
3. 不含叙词（人称/动作/心理/连接词 + 含 `...`/`…` 算叙词 + 白名单算叙词）

零误伤是底线——任何误伤合法内容都是 fail。
"""
import pathlib
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.corpus_health import (  # noqa: E402
    strip_repeated_ads,
    _is_narrative_paragraph,
    _REPEATED_ADS_WHITELIST,
)


def test_a_snow_real_data():
    """A: 雪中悍刀行 511 万字真链路——识别 2 个广告模板，零误伤。"""
    p = pathlib.Path(r"C:\Users\h'h'h\Downloads\雪中悍刀行.txt")
    if not p.exists():
        print("  (跳过：语料不存在)")
        return
    text = p.read_bytes().decode("gb18030", errors="ignore")
    cleaned, rep = strip_repeated_ads(text)

    # 必须识别这两个模板
    ad_texts = {ad["text"] for ad in rep["ads"]}
    assert "一秒记住【..info】，为您提供精彩小说阅读。" in ad_texts, \
        f"漏识别『一秒记住』: {ad_texts}"
    assert any("手机用户" in s for s in ad_texts), f"漏识别『手机用户』: {ad_texts}"

    # 必须不误伤合法叙事
    cleaned_count = cleaned.count("徐凤年点了点头。")
    original_count = text.count("徐凤年点了点头。")
    assert cleaned_count == original_count, \
        f"误伤: 徐凤年点了点头 {original_count} → {cleaned_count}"


def test_b_doupo_real_data():
    """B: 斗破苍穹 603 万字真链路——识别 1 个广告模板，零误伤武功效果。"""
    p = pathlib.Path(r"C:\Users\h'h'h\Downloads\斗破苍穹.txt")
    if not p.exists():
        print("  (跳过：语料不存在)")
        return
    text = p.read_bytes().decode("gbk", errors="ignore")
    cleaned, rep = strip_repeated_ads(text)

    ad_texts = {ad["text"] for ad in rep["ads"]}
    # 核心广告必须识别
    assert any("手机端阅读" in s and "zhuaji" in s for s in ad_texts), \
        f"漏识别『手机端阅读』: {ad_texts}"

    # 武功效果不能误伤
    for legit in ["“噗嗤！”", "“轰隆隆！”", "（未完待续）"]:
        a = text.count(legit)
        b = cleaned.count(legit)
        assert a == b, f"误伤: {legit!r} {a} → {b}"


def test_c_dafeng_real_data():
    """C: 大奉打更人 510 万字真链路——零误伤省略号/ps说明。"""
    p = pathlib.Path(r"C:\Users\h'h'h\Downloads\大奉打更人.txt")
    if not p.exists():
        print("  (跳过：语料不存在)")
        return
    text = p.read_bytes().decode("gbk", errors="ignore")
    cleaned, rep = strip_repeated_ads(text)

    # 省略号格式（v2.244+ 应被识别为合法）
    for legit in ["ps：先更后改。", "……"]:
        a = text.count(legit)
        b = cleaned.count(legit)
        assert a == b, f"误伤: {legit!r} {a} → {b}"


def test_d_synthetic_clean_text():
    """D: 干净文本应该原样返回。"""
    text = "这是第一章。\n这是第二章，徐凤年点了点头。\n这是第三章。"
    cleaned, rep = strip_repeated_ads(text)
    assert cleaned == text, f"干净文本被改动: {cleaned!r}"
    assert rep["removed"] == 0
    assert not rep["ads"]


def test_e_synthetic_clear_ad():
    """E: 合成一段明确广告（频次达标、无叙词）应被识别。

    真实语料段间有空行（\r\n\r\n）；测试用同格式才能被 _PARAGRAPH_RE 切分。
    """
    ad = "    一秒记住【..info】，为您提供精彩小说阅读。"
    # 段 = 1 行 + 空行（与雪中悍刀行真实结构一致）
    text = (ad + "\r\n\r\n") * 20 + ("这是正文。" + "\r\n") * 100
    cleaned, rep = strip_repeated_ads(text)
    assert ad.strip() not in cleaned, f"广告没被清掉: {cleaned[:200]!r}"
    assert any(a["text"] == ad.strip() for a in rep["ads"])


def test_f_verbatim_copy_not_misclassified():
    """F: 频次高但含叙词的段不删（叙事/对话）。"""
    p = "    主角说道：「你好。」\n"
    text = p * 20
    cleaned, rep = strip_repeated_ads(text)
    assert "「你好。」" in cleaned, f"对话被误删: {cleaned[:100]!r}"
    assert not rep["ads"]


def test_g_whitelist_protects_legit_high_freq():
    """G: 白名单段无论频次多高都跳过。"""
    # 模拟「（求收藏）」这种作者更新说明
    text = "    （求收藏）\n" * 30
    cleaned, rep = strip_repeated_ads(text)
    assert "（求收藏）" in cleaned, f"白名单段被误删"
    assert not rep["ads"]


def test_h_ellipsis_protected():
    """H: 中文省略号（U+2026）和英文三连点都算合法叙词。"""
    for legit in ["「……」", "“……”", "「...」", "「.........」"]:
        assert _is_narrative_paragraph(legit), \
            f"省略号没被识别为合法: {legit!r}"


def test_i_punctuation_in_ads_ok():
    """I: 广告模板可以含中文标点（v2.241 撤回后回归正确行为）。"""
    # 「。」是广告模板常见的结尾标点
    assert not _is_narrative_paragraph("一秒记住【..info】，为您提供精彩小说阅读。")


if __name__ == "__main__":
    tests = [
        ("A 雪中真链路", test_a_snow_real_data),
        ("B 斗破真链路", test_b_doupo_real_data),
        ("C 大奉真链路", test_c_dafeng_real_data),
        ("D 干净文本不动", test_d_synthetic_clean_text),
        ("E 合成广告必清", test_e_synthetic_clear_ad),
        ("F 高频对话不删", test_f_verbatim_copy_not_misclassified),
        ("G 白名单保护", test_g_whitelist_protects_legit_high_freq),
        ("H 省略号合法", test_h_ellipsis_protected),
        ("I 广告含标点合法", test_i_punctuation_in_ads_ok),
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
