"""test_verbatim_copy.py —— 逐字复制检测（v2.270/v2.271 修两个 bug）

## 背景

`_is_verbatim_copy` 是防止 LLM「直接抄原文当规则」的闸门。它一直有
两个 bug，但因为**规则和原文都过同一个 `_norm`**，错误在对比时抵消，
所以没有可见影响（v2.268 实测 0 命中）。

### bug 1：`_norm` 里的三引号吞掉了后面的代码（v2.270）

原代码写的是「三个连续双引号」开头的 replace 调用。那三个双引号开启了
**三引号字符串**，把紧跟的「逗号+空格+引号」一起吞进去。AST 解析出来
第一个参数是字符串【逗号+空格+引号】：

    s.replace(单引号, 空串).replace(逗号空格引号, 空串)   ← 实际行为
    s.replace(单引号, 空串).replace(左双引号, 空串)        ← 本意

也就是说这段代码在删「逗号+空格+引号」这个三字符序列，写了两遍，
**从来没处理过任何弯引号**。语法合法、语义完全错误。

### bug 2：句子切分丢掉终止标点（v2.271）

    re.split(r"[。！？…]", chunk_text)      # 句号被丢弃

于是规则 `徐凤年摇头微笑道：“黄蛮儿，不要闹。”`（带句号）永远匹配不上
切出来的句子 `徐凤年摇头微笑道：“黄蛮儿，不要闹`（无句号）。

改成 `re.findall(r"[^。！？…\n]+[。！？…]?", ...)` 保留终止符。
"""
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import _is_verbatim_copy  # noqa: E402

CHUNK = (
    "徐凤年摇头微笑道：“黄蛮儿，不要闹。”"
    "北凉王府龙盘虎踞于清凉山，千门万户，极土木之盛。"
    "他等了三分钟才走。"
    "“嗯。”她应了一声。"
)


def test_a_verbatim_with_period():
    """A: 带句号的整句复制能被检出（bug 2 的核心用例）。"""
    r = "徐凤年摇头微笑道：“黄蛮儿，不要闹。”"
    assert _is_verbatim_copy(r, CHUNK), "带句号的整句没被检出（切分丢标点）"


def test_b_verbatim_no_period():
    """B: 不带句号的整句复制也能检出。"""
    r = "北凉王府龙盘虎踞于清凉山，千门万户，极土木之盛"
    assert _is_verbatim_copy(r, CHUNK)


def test_c_wrapped_in_quotes():
    """C: 被引号包裹的整句，归一化后应检出（bug 1 的核心用例）。"""
    cases = [
        "\u2018徐凤年摇头微笑道：“黄蛮儿，不要闹。”\u2019",   # 弯单引号
        "\u201c北凉王府龙盘虎踞于清凉山，千门万户，极土木之盛\u201d",  # 弯双引号
        "'他等了三分钟才走。'",                                  # ASCII 单引号
    ]
    for c in cases:
        assert _is_verbatim_copy(c, CHUNK), f"引号包裹没检出: {c[:30]}"


def test_d_abstract_rules_not_flagged():
    """D: 抽象规则不能误判为逐字复制（这是误杀防护）。"""
    cases = [
        "用动作和神态描写表现人物情绪（证据：徐凤年摇头微笑道）",
        "通过环境铺陈营造氛围（证据：北凉王府龙盘虎踞于清凉山）",
        "用精确时间描写增强真实感（证据：他等了三分钟才走）",
    ]
    for c in cases:
        assert not _is_verbatim_copy(c, CHUNK), f"抽象规则被误判: {c[:30]}"


def test_e_too_short():
    """E: 过短文本不判逐字复制（< 8 字）。"""
    for c in ["短", "徐凤年", "说道", "嗯"]:
        assert not _is_verbatim_copy(c, CHUNK), f"过短被误判: {c!r}"


def test_f_not_in_chunk():
    """F: 不在原文里的句子不能判 True。"""
    r = "这是一句完全不在原文里的话，用来测试误报。"
    assert not _is_verbatim_copy(r, CHUNK)


def test_g_comma_quote_not_stripped():
    """G: `, "` 这个三字符序列不能被特殊处理（bug 1 的回归）。

    原代码因为三引号 bug，会把「逗号+空格+引号」整段删掉。
    现在改成逐个引号替换，逗号应当保留。

    验证方式：构造两个仅在逗号位置不同的句子。
    """
    chunk = "他说这句话的时候很平静。"
    # 带逗号的版本在原文里不存在，不该被判 True
    assert not _is_verbatim_copy("他说, 这句话的时候很平静", chunk)
    # 去掉逗号的版本与原文一致，应判 True
    assert _is_verbatim_copy("他说这句话的时候很平静", chunk)


def test_h_prefix_match():
    """H: 规则是原文句子的前缀（LLM 截断）也算逐字复制。"""
    r = "北凉王府龙盘虎踞于清凉山"   # 原句的前缀
    assert _is_verbatim_copy(r, CHUNK)


if __name__ == "__main__":
    tests = [
        ("A 带句号整句", test_a_verbatim_with_period),
        ("B 无句号整句", test_b_verbatim_no_period),
        ("C 引号包裹", test_c_wrapped_in_quotes),
        ("D 抽象规则不误判", test_d_abstract_rules_not_flagged),
        ("E 过短不判", test_e_too_short),
        ("F 不在原文不判", test_f_not_in_chunk),
        ("G 逗号不被误删", test_g_comma_quote_not_stripped),
        ("H 前缀匹配", test_h_prefix_match),
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
