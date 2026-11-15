"""首段裁剪 + 字数纪律 + 时间线段数下限的回归测试。

锁住四件事（对应 2026-11-12 的三处修复）：

1. system 提示不再出现「每轮控制在指定字数内」，改成「不少于」并禁止
   「未写够就写【本节完】/总结句收束」；
2. 首段 user prompt 不注入 full relationships 表、三条伏笔、synopsis、direction；
   这些整篇走向延后到第 2 节，首段只给本节能切入的冲突；
3. 首段角色卡遮蔽「幕后人物 / 推动 X 计划」这类底牌；叙事档案剔除压缩趋势
   并加「篇幅与完整性优先于简洁倾向」；
4. `total_segments` 在目标字数小于单段预算时不再退化为「全书 1 段」。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.generation import engine as engine_mod  # noqa: E402
from app.generation.prompts import (  # noqa: E402
    build_system_prompt,
    build_user_prompt,
)
from app.style.narrative import describe_narrative  # noqa: E402

BIBLE = {
    "characters": [
        {"name": "沈砚", "role": "主人公", "motivation": "查清旧友苏晚的失踪"},
        {"name": "顾清和", "role": "幕后人物", "motivation": "用体面的名义推动归墟计划"},
    ],
    "relationships": [
        {"a": "沈砚", "b": "苏晚", "relation": "旧友"},
        {"a": "苏晚", "b": "顾清和", "relation": "失踪者与计划的推动者"},
    ],
    "foreshadowing": [
        "录音笔里提到第七个名字",
        "周明远对归墟计划的了解远超主编",
        "沈砚记忆的空白与失踪者时间线重合",
    ],
    "synopsis": "记者调查失踪案，逐渐卷入巨大阴谋。",
    "direction": "沈砚必须在失控前查清归墟计划。",
    "conflicts": ["记忆失控与追查真相的冲突", "身边人是否可信的冲突"],
}

PREMISE = (
    "我是一名刚毕业的记者，接到一个关于失踪人口的线索。"
    "深入调查时，我发现自己的记忆开始出现混乱。"
    "当我揭开真相的一角，却发现背后隐藏着一个巨大的阴谋。"
)

REMINDERS = {
    "character_constraints": BIBLE["characters"],
    "unresolved_plotholes": [],
}


def _user_prompt(opening: bool) -> str:
    prior = [] if opening else ["上一段摘要"]
    return build_user_prompt(
        prior,
        [],
        None,
        3000,
        BIBLE,
        PREMISE,
        prior_blocks=[] if opening else ["[近情·段 1] 上一段"],
    )


def test_system_prompt_requires_minimum_not_cap():
    system = build_system_prompt("测试项目", REMINDERS)
    assert "控制在" not in system
    assert "字数内" not in system
    assert "不少于" in system
    assert "未写够不要写【本节完】" in system
    assert "本节最多推进一条" in system


def test_opening_user_prompt_defers_arc_fields():
    opening = _user_prompt(opening=True)
    # 整篇走向 / 关系全表 / 三条伏笔全部延后（断言实际字段值，不误伤提示里的说明句）
    assert "整体梗概" not in opening
    assert BIBLE["synopsis"] not in opening
    assert BIBLE["direction"] not in opening
    for rel in BIBLE["relationships"]:
        assert f"{rel['a']}与{rel['b']}——{rel['relation']}" not in opening
    for item in BIBLE["foreshadowing"]:
        assert item not in opening
    # 首段只给本节可切入的冲突
    assert "本节可切入的冲突" in opening
    assert BIBLE["conflicts"][0] in opening
    # premise 只留高概念，讲到「真相 / 阴谋」的句子截掉
    assert "刚毕业的记者" in opening
    assert "巨大的阴谋" not in opening

    later = _user_prompt(opening=False)
    assert "整体梗概" in later
    assert BIBLE["synopsis"] in later
    assert BIBLE["direction"] in later
    for rel in BIBLE["relationships"]:
        assert f"{rel['a']}与{rel['b']}——{rel['relation']}" in later
    for item in BIBLE["foreshadowing"]:
        assert item in later
    assert "巨大的阴谋" in later


def test_opening_masks_character_spoilers_only_for_first_segment():
    opening = build_system_prompt("测试项目", REMINDERS, None, BIBLE, is_opening=True)
    assert "顾清和" in opening
    assert "幕后人物" not in opening
    assert "归墟计划" not in opening
    assert "（涉及后续揭底，本节不展开）" in opening
    # 不含剧透词的角色动机保留
    assert "查清旧友苏晚的失踪" in opening
    # 首段不列伏笔清单（三条伏笔在 system 里也不出现）
    assert "首段不列伏笔清单" in opening
    for item in BIBLE["foreshadowing"]:
        assert item not in opening

    later = build_system_prompt("测试项目", REMINDERS, None, BIBLE, is_opening=False)
    assert "幕后人物" in later
    assert "归墟计划" in later
    for item in BIBLE["foreshadowing"]:
        assert item in later


NARRATIVE = {
    "rules": {
        "文风漂移分析": [
            {"rule": "整体趋势：句式逐渐精简，描写减少细节，叙事结构紧凑"},
            {"rule": "整体趋势：作者偏爱多视角并进"},
        ],
        # 段落编排走四层选条，无「证据：」/无章号的条目会被硬过滤（2026-11-14 起），
        # 故按生产档案的形态补上证据与章号；断言点不变，仍校验该条不被误删。
        "段落编排": [{
            "rule": "短段切对话节奏（证据：“他把话头掐断，屋里静了一瞬。”）",
            "chapter_no": 3,
        }],
    },
    "forbidden": {},
}


def test_opening_drops_compression_trends_of_narrative():
    opening = describe_narrative(NARRATIVE, opening=True)
    later = describe_narrative(NARRATIVE, opening=False)

    assert "句式逐渐精简" not in opening
    assert "篇幅与完整性优先于简洁倾向" in opening
    # 非趋势维度不误删
    assert "短段切对话节奏" in opening
    assert "作者偏爱多视角并进" in opening

    assert "句式逐渐精简" in later
    assert "篇幅与完整性优先于简洁倾向" not in later


class _FakeProject:
    def __init__(self, target_length: int, generation_length: int):
        self.target_length = target_length
        self.generation_length = generation_length
        self.outline = ["第一幕"]


def test_total_segments_never_collapses_to_one():
    # p5 真实配置：5000 / 3000 → 原来算 1，现在保底 2
    assert engine_mod._total_segments(_FakeProject(5000, 3000)) == 2
    assert engine_mod._total_segments(_FakeProject(9000, 3000)) == 3
    assert engine_mod._total_segments(_FakeProject(0, 3000)) == 2
