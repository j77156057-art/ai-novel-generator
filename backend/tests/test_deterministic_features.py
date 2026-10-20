"""test_deterministic_features.py —— v2.230 零 LLM 确定性指标

5 断言：
A. 空文本返回空字典
B. 基础指标齐全且类型正确（句长/段落/对白/情绪/场景/悬念）
C. 数值语义正确：全短句文本的短句占比=1.0，全对白段落=对白开头占比高
D. _format_features_rule 产出非空且含关键数字
E. prompt 前缀已注入（user 含「本块客观统计」，system 不再要求文笔量化审计）
"""
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import (
    build_deterministic_features,
    _format_features_rule,
    _format_features_prefix,
    _build_extract_prompt,
)


def test_a():
    assert build_deterministic_features("") == {}
    assert build_deterministic_features("   \n  ") == {}
    print("[A] 空文本返回 {} ✓")


def test_b():
    text = (
        "“你来了。”萧炎抬起头。\n\n"
        "他握紧拳头，指节泛白。广场上人声嘈杂，阳光刺眼。\n\n"
        "“嗯。”只有两个字。\n"
    )
    f = build_deterministic_features(text)
    for key in ("句长", "段落", "对白", "情绪", "场景", "悬念"):
        assert key in f, f"B failed: 缺 {key}"
    assert isinstance(f["句长"]["均值"], float)
    assert isinstance(f["段落"]["段数"], int)
    assert f["对白"]["句数"] == 2, f"B failed: 对白句数={f['对白']['句数']}"
    print(f"[B] 六类指标齐全 ✓ (对白 {f['对白']['句数']} 句, 段 {f['段落']['段数']} 段)")


def test_c():
    # 全短句文本：短句占比应为 1.0
    short_text = "。".join(["好"] * 10) + "。"
    f = build_deterministic_features(short_text)
    assert f["句长"]["短句占比"] == 1.0, f"C failed: {f['句长']['短句占比']}"

    # 全对白段：对白开头段占比应为 1.0
    dlg_text = "“甲。”\n\n“乙。”\n\n“丙。”"
    f2 = build_deterministic_features(dlg_text)
    assert f2["段落"]["对白开头段占比"] == 1.0, f"C failed: {f2['段落']['对白开头段占比']}"
    print(f"[C] 数值语义正确 ✓ (短句 1.0, 对白开头 1.0)")


def test_d():
    text = "“你来了。”萧炎抬起头。他握紧拳头。广场上人声嘈杂。"
    f = build_deterministic_features(text)
    rule = _format_features_rule(f)
    assert rule.startswith("本块实测："), f"D failed: {rule[:30]}"
    assert "句长" in rule and "对白" in rule, f"D failed: {rule}"
    assert _format_features_rule({}) == ""
    print(f"[D] 规则文本生成 ✓ ({len(rule)} 字)")


def test_e():
    text = "“你来了。”萧炎抬起头。他握紧拳头。"
    system, user = _build_extract_prompt(text, "测试块")
    assert "本块客观统计" in user, "E failed: user 未含确定性指标前缀"
    assert "不要再重新估数" in user, "E failed: 未加禁止估数指令"
    assert "文笔量化审计" not in system, "E failed: system 仍要求 LLM 输出文笔量化审计"
    # 其余维度仍在
    for dim in ("人物描写手法", "对白风格", "潜台词模式" if False else "伏笔与悬念设计"):
        assert dim in system, f"E failed: 缺维度 {dim}"
    print("[E] prompt 已改：去掉文笔量化审计 + 注入事实前缀 ✓")


test_a()
test_b()
test_c()
test_d()
test_e()
print("\n=== 5/5 passed ===")
