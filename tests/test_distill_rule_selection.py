"""蒸馏产物**注入哪几条规则**的选择逻辑。

## 为什么这条要单独钉住

蒸馏一次要跑 ~150 次模型调用、产出几千条规则，但真正进 system prompt 的
只有每维前 2 条。取哪 2 条，直接决定「42 分钟蒸出来的东西有没有被用上」。

实测真书（雪中悍刀行，采样 297 章）暴露的两个问题：

1. **取法按追加顺序 = 只用最早的几章** —— 13 个维度的前 2 条全部来自
   第 1~11 章，采样面的 94%（第 12 章之后）对提示词零影响。
   而且早期章节文风会漂，拿最生涩的部分代表全书是错的。
2. **节拍维度是「弧」不是「条」** —— 每章产出「铺垫→升温→爆发→余波→新钩子」
   5~7 步，按「每章取第一条」挑会把弧塌成「铺垫 → 铺垫 → 铺垫」，
   顺序信息全丢。

要守住的：
- 成熟期优先、跨章摊开（不是全挤在最后两章）；
- 每章取最早追加的那条（确定性：同一份档案永远选出同一批）；
- 节拍取一条**完整弧**，步骤标签不重复、顺序不乱；
- 边界：空输入、limit<=0、不认字段的旧数据都不能炸。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def _rules(chapters, per_chapter=1, dim='人物描写手法'):
    """造一份「每章 per_chapter 条规则」的规则表。"""
    items = []
    for ch in chapters:
        for k in range(per_chapter):
            items.append({
                'rule': f'第{ch}章第{k + 1}条手法',
                'novel_id': 1,
                'chapter_no': ch,
            })
    return {dim: items}


# ====================================================================
# 1) 手法维度：成熟期优先 + 跨章摊开
# ====================================================================

def test_empty_and_zero_limit_return_nothing():
    from app.style.narrative import _select_dim_rules
    assert _select_dim_rules([], 3) == []
    assert _select_dim_rules(_rules([1, 2, 3])['人物描写手法'], 0) == []
    assert _select_dim_rules(_rules([1, 2, 3])['人物描写手法'], -1) == []


def test_picks_from_the_mature_half_not_the_earliest_chapters():
    """1~20 章里取 2 条，必须落在后一半（第 11~20 章）。"""
    from app.style.narrative import _select_dim_rules
    items = _rules(range(1, 21))['人物描写手法']
    picked = _select_dim_rules(items, 2)
    chapters = [i['chapter_no'] for i in picked]
    assert len(picked) == 2
    assert all(ch > 10 for ch in chapters), chapters


def test_spreads_across_different_chapters():
    """不能全挤在最后两章：limit=3 要落到 3 个不同的章上。"""
    from app.style.narrative import _select_dim_rules
    items = _rules(range(1, 41))['人物描写手法']
    picked = _select_dim_rules(items, 3)
    chapters = [i['chapter_no'] for i in picked]
    assert len(set(chapters)) == 3, chapters
    # 摊开但仍然偏后：最后一章不该被跳过，起点也不该回到最早期。
    assert max(chapters) >= 30, chapters
    assert min(chapters) >= 21, chapters


def test_takes_the_first_rule_appended_in_each_chapter():
    """同章多条时取最早追加的那条 —— 确定性来源。"""
    from app.style.narrative import _select_dim_rules
    items = _rules(range(1, 21), per_chapter=3)['人物描写手法']
    picked = _select_dim_rules(items, 2)
    for item in picked:
        assert item['rule'].endswith('第1条手法'), item


def test_same_input_gives_the_same_pick():
    from app.style.narrative import _select_dim_rules
    items = _rules(range(1, 31))['人物描写手法']
    a = [i['rule'] for i in _select_dim_rules(items, 3)]
    b = [i['rule'] for i in _select_dim_rules(items, 3)]
    assert a == b


def test_chapters_are_sorted_numerically_not_by_first_appearance():
    """规则来源可能乱序追加（兜底条 chapter_no=0 排在最后）——
    成熟期必须按章号算，不能按「谁先出现」。"""
    from app.style.narrative import _select_dim_rules
    items = _rules([30, 10, 20, 5, 15, 25])['人物描写手法']
    picked = _select_dim_rules(items, 1)
    # 按章号排：成熟期 = [20, 25, 30]，取中点 25。
    # 若按「首次出现顺序」算，成熟期会变成 [5, 15, 25]，取到 15 —— 错。
    assert [i['chapter_no'] for i in picked] == [25]


def test_single_pick_takes_the_middle_of_the_mature_range():
    """只取一条时取成熟期中点，不取边界（最后一章可能是尾声）。"""
    from app.style.narrative import _select_dim_rules
    items = _rules(range(1, 21))['人物描写手法']
    picked = _select_dim_rules(items, 1)
    chapters = [i['chapter_no'] for i in picked]
    assert len(chapters) == 1
    assert 11 <= chapters[0] <= 20, chapters


def test_rules_without_chapter_no_fall_back_to_the_old_order():
    """旧档案的规则是纯字符串，没有 chapter_no —— 算不出成熟期，
    这时必须退回旧取法（按原顺序取前 N 条），不能只剩一条。"""
    from app.style.narrative import _select_dim_rules
    items = ['第一条手法', '第二条手法', '第三条手法']
    picked = _select_dim_rules(items, 2)
    assert picked == ['第一条手法', '第二条手法']


def test_rules_without_chapter_no_still_work():
    """没有 chapter_no 字段的 dict 也不能抛异常。"""
    from app.style.narrative import _select_dim_rules
    items = [{'rule': '没有章号的规则'}]
    picked = _select_dim_rules(items, 2)
    assert len(picked) == 1


# ====================================================================
# 2) 节拍维度：取一条完整弧，不能塌成「铺垫 → 铺垫」
# ====================================================================

def _beats(chapters):
    """每章一条 5 步弧：铺垫 / 升温 / 爆发 / 余波 / 新钩子。"""
    steps = ['铺垫', '升温', '爆发', '余波', '新钩子']
    items = []
    for ch in chapters:
        for s in steps:
            items.append({
                'rule': f'{s}：抽象写法（证据：原文）',
                'novel_id': 1,
                'chapter_no': ch,
            })
    return {'节拍序列': items}


def test_beat_selection_returns_a_full_arc_not_the_first_step_of_each_chapter():
    from app.style.narrative import _select_beat_steps
    items = _beats(range(1, 21))['节拍序列']
    picked = _select_beat_steps(items, 3)
    labels = [i['rule'].split('：', 1)[0] for i in picked]
    assert labels == ['铺垫', '升温', '爆发'], labels


def test_beat_selection_takes_the_mature_period():
    """弧必须来自成熟期（第 11~20 章），不是第 1 章。"""
    from app.style.narrative import _select_beat_steps
    items = _beats(range(1, 21))['节拍序列']
    picked = _select_beat_steps(items, 5)
    assert picked
    assert all(i['chapter_no'] > 10 for i in picked), \
        [i['chapter_no'] for i in picked]


def test_beat_steps_are_not_repeated():
    """去重：多章的同一步骤只出现一次，链条不塌。"""
    from app.style.narrative import _select_beat_steps
    items = _beats(range(1, 21))['节拍序列']
    labels = [i['rule'].split('：', 1)[0] for i in _select_beat_steps(items, 5)]
    assert len(labels) == len(set(labels)), labels


# ====================================================================
# 3) 端到端：渲染出来的档案块
# ====================================================================

def test_describe_narrative_uses_mature_chapters_and_keeps_the_beat_order():
    from app.style.narrative import describe_narrative

    rules = {}
    rules.update(_rules(range(1, 21)))
    rules.update(_beats(range(1, 21)))
    profile = {'rules': rules}

    text = describe_narrative(profile, include_voice=False)
    assert text
    # 手法维度用的是成熟期的规则：第 11~20 章里出现的措辞。
    assert '第1章第1条手法' not in text
    assert any(f'第{ch}章第1条手法' in text for ch in range(11, 21))
    # 节拍是「铺垫 → 升温 → 爆发」这样一条有序的链，不是「铺垫 → 铺垫」。
    assert '铺垫 → 升温 → 爆发' in text, text
    assert '铺垫 → 铺垫' not in text, text


def test_beat_is_not_rendered_twice():
    """节拍只在「节拍骨架参考」那行出现一次 ——
    手法行里再放一遍只会剩一个孤零零的标签。"""
    from app.style.narrative import describe_narrative

    rules = {}
    rules.update(_rules(range(1, 21)))
    rules.update(_beats(range(1, 21)))
    text = describe_narrative({'rules': rules}, include_voice=False)
    assert text.count('铺垫') == 1, text
    assert '节拍骨架参考：铺垫 → 升温 → 爆发' in text, text


def test_describe_narrative_is_deterministic():
    from app.style.narrative import describe_narrative

    rules = {}
    rules.update(_rules(range(1, 21)))
    rules.update(_beats(range(1, 21)))
    profile = {'rules': rules}
    assert describe_narrative(profile, include_voice=False) == \
        describe_narrative(profile, include_voice=False)


# ====================================================================
# 5) 注入用量：每维几条
# ====================================================================

def test_the_default_injects_five_rules_per_dimension():
    """用户要求「让蒸馏产出更多被用上」：每维 2 → 5 条。

    钉住的是**默认口径**（`RULES_PER_DIM`）—— 调用方不传参数时用它。
    """
    from app.style.narrative import RULES_PER_DIM
    assert RULES_PER_DIM == 5, RULES_PER_DIM


def test_every_dimension_survives_the_archive_budget():
    """每维 5 条时档案块会变长 —— 靠后的维度（潜台词/语序）不能被上限挤掉。

    上限与条数必须配套：条数抬了而上限没抬，等于左手换右手
    （多出来的规则把别人的位置占了）。
    """
    from app.style.narrative import DIM_LABELS, describe_narrative

    dims = ["人物描写手法", "对白风格", "段落编排", "场景构建", "情绪表达",
            "节奏控制", "世界观呈现", "剧情编排模式", "伏笔与悬念设计", "语序编排"]
    rules = {}
    for dim in dims:
        rules[dim] = [
            {"rule": f"第{i}条{dim}手法：这是第{i}条用来验证注入条数的手法规则文本，"
                       f"长度足够长以免被截断掉。", "novel_id": 1, "chapter_no": i}
            for i in range(1, 9)
        ]
    text = describe_narrative({'rules': rules}, include_voice=False)
    for dim in dims:
        label = DIM_LABELS[dim]
        assert f"{label}——" in text, f"维度「{dim}」被档案块上限挤掉了"
    # 每维确实进了 5 条（用；分隔数判断）
    line = next(ln for ln in text.split("\n")
                if ln.startswith(DIM_LABELS[dims[0]] + "——"))
    assert line.count("；") == 4, line


def test_a_long_rule_is_cut_at_a_step_boundary_not_mid_step():
    """动作链被拦腰截断会得到半句话（模型照着写会写出断链）。

    截断点必须落在「→」这种步边界上，并把尾巴的箭头去掉。
    """
    from app.style.narrative import _clip
    chain = ("来客主动靠近→主方接待并观察→双方试探交谈→一方提出条件或交换"
             "→达成有限共识或继续周旋→一方主动离开")
    out = _clip(chain, 42)
    assert not out.endswith("→"), out
    # 最后一步必须是**完整的一步**，不是被拦腰切下来的残片
    # （旧行为会切出「…继续周旋→一」这种一个字的碎片）。
    last_step = out.rsplit("→", 1)[-1]
    assert len(last_step) >= 4, out
    assert out.count("→") >= 2, out
    assert len(out) <= 42
