"""test_extract_split.py —— 拆分 prompt 契约测试

## 当前生效（v2.262）

主循环走 **v2.260 的两组**：
- 角色组 3 维（人物描写 / 对白风格 / 角色声音）
- 叙事组 7 维（段落 / 场景 / 情绪 / 节奏 / 世界观 / 剧情 / 伏笔）

## 已回退（v2.261）

曾把叙事组再拆为结构组 3 维 + 技法组 4 维。probe（单 chunk 3 轮）显示
+52% 规则、+72% 校验，但真链路（雪中 990 章 30% 采样）证伪：

    版本      耗时      规则   rejected  三个结构维度
    v2.260   25.4 min   2112     734    剧情126 伏笔121 段落108
    v2.261   34.5 min   1904     761    剧情 76 伏笔 72 段落 55

结构三维亚均 -40%。这三维彼此有依赖（判断段落编排需要看节奏与场景切换，
判断剧情编排需要伏笔/悬念上下文），拆开后模型失去判断依据，
大量过渡章/对话章写不出规则。

两个函数保留在代码里（测试仍覆盖），但主循环不再调用——若后续有
需要单独分析骨架/质感的场景可复用。相应断言标注「保留未接入」。
"""
import sys

sys.path.insert(0, r"C:/Users/h'h'h/WorkBuddy/2026-10-12-11-59-30/ai-novel-generator/worktrees/master/backend")

from app.style.narrative import (  # noqa: E402
    _build_extract_prompt,
    _build_extract_character_prompt,
    _build_extract_narrative_prompt,
    _build_extract_structure_prompt,
    _build_extract_technique_prompt,
    _EXTRACT_CHARACTER_DIMS,
    _EXTRACT_NARRATIVE_DIMS,
    _EXTRACT_STRUCTURE_DIMS,
    _EXTRACT_TECHNIQUE_DIMS,
)

CHUNK = "这是测试 chunk。\n" * 200  # 1KB
LABEL = "test-chunk"


def test_a_character_dims_cover_3():
    """A: 角色组维度集合 = 3 维。"""
    assert len(_EXTRACT_CHARACTER_DIMS) == 3
    assert "人物描写手法" in _EXTRACT_CHARACTER_DIMS
    assert "对白风格" in _EXTRACT_CHARACTER_DIMS
    assert "角色声音词典" in _EXTRACT_CHARACTER_DIMS


def test_b_narrative_dims_cover_7():
    """B: 叙事组维度集合 = 7 维。"""
    assert len(_EXTRACT_NARRATIVE_DIMS) == 7
    expected = {"段落编排", "场景构建", "情绪表达", "节奏控制",
                "世界观呈现", "剧情编排模式", "伏笔与悬念设计"}
    assert set(_EXTRACT_NARRATIVE_DIMS) == expected


def test_c_no_overlap():
    """C: 角色组和叙事组维度不重叠。"""
    assert set(_EXTRACT_CHARACTER_DIMS) & set(_EXTRACT_NARRATIVE_DIMS) == set()


def test_d_combined_covers_original_10():
    """D: 拆分后合并 = 原 extract prompt 的 10 维。"""
    # 原 _build_extract_prompt 含 10 维（角色声音词典 + 9 维 extract）
    combined = set(_EXTRACT_CHARACTER_DIMS) | set(_EXTRACT_NARRATIVE_DIMS)
    assert combined == {
        "人物描写手法", "对白风格", "角色声音词典",  # 角色组
        "段落编排", "场景构建", "情绪表达", "节奏控制",
        "世界观呈现", "剧情编排模式", "伏笔与悬念设计",  # 叙事组
    }


def test_d2_structure_technique_split():
    """D2: v2.261 把叙事组 7 维拆成结构 3 + 技法 4，不重不漏。"""
    assert len(_EXTRACT_STRUCTURE_DIMS) == 3
    assert len(_EXTRACT_TECHNIQUE_DIMS) == 4
    assert set(_EXTRACT_STRUCTURE_DIMS) == {"段落编排", "剧情编排模式", "伏笔与悬念设计"}
    assert set(_EXTRACT_TECHNIQUE_DIMS) == {"场景构建", "情绪表达", "节奏控制", "世界观呈现"}
    assert set(_EXTRACT_STRUCTURE_DIMS) & set(_EXTRACT_TECHNIQUE_DIMS) == set()
    assert (set(_EXTRACT_STRUCTURE_DIMS) | set(_EXTRACT_TECHNIQUE_DIMS)
            == set(_EXTRACT_NARRATIVE_DIMS))


def test_d3_all_three_groups_cover_10():
    """D3: 三组合并 = 原 10 维且不重叠。"""
    combined = (set(_EXTRACT_CHARACTER_DIMS)
                | set(_EXTRACT_STRUCTURE_DIMS)
                | set(_EXTRACT_TECHNIQUE_DIMS))
    assert len(combined) == 10
    assert combined == {
        "人物描写手法", "对白风格", "角色声音词典",
        "段落编排", "剧情编排模式", "伏笔与悬念设计",
        "场景构建", "情绪表达", "节奏控制", "世界观呈现",
    }


def test_d4_structure_technique_shorter_than_narrative():
    """D4: v2.261 两组都比 v2.260 叙事组短（再拆的收益）。"""
    narr_sys, _ = _build_extract_narrative_prompt(CHUNK, LABEL)
    for fn, name in [
        (_build_extract_structure_prompt, "结构组"),
        (_build_extract_technique_prompt, "技法组"),
    ]:
        new_sys, _ = fn(CHUNK, LABEL)
        assert len(new_sys) < len(narr_sys), \
            f"{name} ({len(new_sys)}) 应短于 v2.260 叙事组 ({len(narr_sys)})"


def test_d5_structure_prompt_focus():
    """D5: 结构组只讲骨架（段落/剧情/伏笔），不含技法维度。"""
    sys_, _ = _build_extract_structure_prompt(CHUNK, LABEL)
    assert "结构" in sys_
    assert "段落编排" in sys_ and "剧情编排模式" in sys_ and "伏笔与悬念设计" in sys_
    assert "场景构建" not in sys_
    assert "世界观呈现" not in sys_


def test_d6_technique_prompt_focus():
    """D6: 技法组只讲质感（场景/情绪/节奏/世界观），不含结构维度。"""
    sys_, _ = _build_extract_technique_prompt(CHUNK, LABEL)
    assert "场景构建" in sys_ and "情绪表达" in sys_
    assert "伏笔与悬念设计" not in sys_
    assert "剧情编排模式" not in sys_


def test_e_character_prompt_shorter():
    """E: 角色组 prompt 短于原版（视野更集中）。"""
    new_sys, _ = _build_extract_character_prompt(CHUNK, LABEL)
    old_sys, _ = _build_extract_prompt(CHUNK, LABEL)
    assert len(new_sys) < len(old_sys), \
        f"角色组 prompt ({len(new_sys)}) 应当比原版 ({len(old_sys)}) 短"


def test_f_narrative_prompt_about_same():
    """F: 叙事组 prompt 与原版差不多（7 维本来就在原版里）。"""
    new_sys, _ = _build_extract_narrative_prompt(CHUNK, LABEL)
    old_sys, _ = _build_extract_prompt(CHUNK, LABEL)
    # 叙事组应该接近原版（±30%）
    ratio = len(new_sys) / len(old_sys)
    assert 0.7 < ratio < 1.3, f"叙事组/原版 = {ratio:.2f}，超出 ±30%"


def test_g_character_prompt_mentions_focus():
    """G: 角色组 prompt 强调「人物/对话」专注。"""
    sys, _ = _build_extract_character_prompt(CHUNK, LABEL)
    assert "人物" in sys
    assert "对话" in sys
    # 不应再让 LLM 关注「节奏」「伏笔」等无关维度
    assert "伏笔" not in sys or "不承担" in sys  # 应不含伏笔维度定义


def test_h_narrative_prompt_mentions_focus():
    """H: 叙事组 prompt 强调「结构/技法」专注。"""
    sys, _ = _build_extract_narrative_prompt(CHUNK, LABEL)
    assert "结构" in sys or "技法" in sys
    # 不应让 LLM 关注角色声音
    assert "角色声音" not in sys.replace("角色声音词典", "").replace("角色声音词典相关", "")


def test_i_outputs_are_valid_tuple():
    """I: 返回值是 (system, user) 字符串元组。"""
    for fn in (_build_extract_character_prompt, _build_extract_narrative_prompt):
        sys_, usr = fn(CHUNK, LABEL)
        assert isinstance(sys_, str) and len(sys_) > 100
        assert isinstance(usr, str) and len(usr) > 100


if __name__ == "__main__":
    tests = [
        ("A 角色组 3 维", test_a_character_dims_cover_3),
        ("B 叙事组 7 维", test_b_narrative_dims_cover_7),
        ("C 不重叠", test_c_no_overlap),
        ("D 合并=原 10 维", test_d_combined_covers_original_10),
        ("D2 结构3+技法4·保留未接入", test_d2_structure_technique_split),
        ("D3 三组合并=10 维·保留未接入", test_d3_all_three_groups_cover_10),
        ("D4 再拆后更短·保留未接入", test_d4_structure_technique_shorter_than_narrative),
        ("D5 结构组焦点·保留未接入", test_d5_structure_prompt_focus),
        ("D6 技法组焦点·保留未接入", test_d6_technique_prompt_focus),
        ("E 角色组 prompt 短", test_e_character_prompt_shorter),
        ("F 叙事组 prompt 相当", test_f_narrative_prompt_about_same),
        ("G 角色组焦点", test_g_character_prompt_mentions_focus),
        ("H 叙事组焦点", test_h_narrative_prompt_mentions_focus),
        ("I 返回值元组", test_i_outputs_are_valid_tuple),
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
