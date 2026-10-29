"""描写要素：心理描写 / 场景描写（2026-10-29）。

## 用户反馈

> 「ai生成的文本很少有心理，场景描写，你直接去搜一下小说描写要素这一类，把这些坑补上」

## 取证（发布版冒烟库，参考书语料 vs 生成文本，每千字）

| 描写类型 | 生成 | 参考书 | 结论 |
|---|---|---|---|
| 外貌 | 25.31 | 13.12 | 多 1.9 倍 |
| 动作 | 22.26 | 8.63 | 多 2.6 倍 |
| 细节 | 11.06 | 1.75 | 多 6.3 倍 |
| 环境 | 19.33 | 12.69 | 多 1.5 倍 |
| **直陈心理** | **1.03** | **3.06** | **缺 2/3** |
| **语言提示语** | 11.92 | 18.49 | 缺 35% |

⇒ **心理描写真的缺**（不是场景缺 —— 场景反而超标）；
生成文本把「心理」全部换成了「身体反应」（3.10 vs 参考 0.46，多 6.7 倍）。

## 根因（比「缺要求」更深）

蒸馏蒸出的「人物描写手法」规则是：

    用动作和神态暗示情绪，**不直接写**'害怕/紧张'（证据：指尖不自觉收紧）

这条规则**有原文证据、过了证据闸门**，于是被当成「作者的手法」注入提示词。
但真书里作者**既**用动作暗示**也**直陈心理（1250 : 189 ≈ 6.7 : 1）
—— 蒸馏只抽到了「不写心理」那一面。

⇒ 修法两条：
1. **蒸馏加「心理描写手法」维度**，要求同时抽两面 + 给占比；
2. **提示词加可执行指引**，并划清与「禁止情绪直陈」的边界。

## 本文件钉住的口径

- 心理判据**能量出差距**（生成 vs 参考书）；
- 指引必须**可执行**（带真书范例的模式），不是「要写心理」；
- 频次**对齐参考书**（每 2~3 段一处），不能写「每段一处」（实测写滥 3 倍）；
- 与「禁止情绪直陈」的**边界要写清**（禁形容词贴标签，不禁具体心理活动）；
- 蒸馏维度**要求两面并存 + 占比**（只写一面就是漏了作者的写法）。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.generation.description_elements import (  # noqa: E402
    PSYCH_PATTERNS,
    SCENE_PATTERNS,
    count_psych,
    has_psych,
    psych_density,
    render_psych_guide,
    render_scene_guide,
)


# ======================================================================
# 1. 判据：能量出「生成 vs 参考书」的差距
# ======================================================================

def test_心理判据认得真书写法():
    """判据必须认得参考书里的心理描写（否则量出来的差距是假的）。"""
    # 这四句都来自参考书原文
    assert count_psych("赵北山心中轻轻叹息，黄小满若能稍稍聪慧一些") >= 1
    assert count_psych("老道士闻言，笑容古怪，心中咂舌") >= 1
    assert count_psych("起先她以为是自己胆小，但越长大胆子越大") >= 1
    assert count_psych("强忍揍人的念头") >= 1


def test_心理判据不误判纯动作():
    """纯动作/环境不该被算成心理（否则密度虚高）。"""
    assert count_psych("他推开门，走了进去。") == 0
    assert count_psych("雨落在石阶上，雾很浓。") == 0


def test_密度计算():
    text = "他心中叹息。心里一沉。" * 10
    d = psych_density([text])
    assert d > 0
    # 每 10 字约 2 处 ⇒ 200 处/千字量级
    assert 100 < d < 400, d


def test_has_psych():
    assert has_psych("他心中叹息") is True
    assert has_psych("他走过去") is False


# ======================================================================
# 2. 指引：必须可执行（带真书范例），不是「要写心理」
# ======================================================================

def test_心理指引含五种可执行模式():
    guide = render_psych_guide()
    for name, _demo in PSYCH_PATTERNS:
        assert name in guide, f"缺模式：{name}"
    # 每条都带范例（真书原句），不是空泛要求
    assert "心中轻轻叹息" in guide
    assert "以为是自己胆小" in guide


def test_心理指引的频次对齐参考书():
    """⚠️ 不能是「每段至少一处」—— 实测那样会写到参考书的 3 倍。"""
    guide = render_psych_guide()
    assert "每 2~3 段" in guide, guide
    assert "每段至少" not in guide, guide
    assert "不要连写好几处" in guide


def test_场景指引含四种模式():
    guide = render_scene_guide()
    for name, _demo in SCENE_PATTERNS:
        assert name in guide, f"缺模式：{name}"
    assert "独立成段" in guide


def test_场景指引要求环境起作用():
    guide = render_scene_guide()
    assert "起作用" in guide


# ======================================================================
# 3. 提示词接线：边界必须写清（否则与「禁止情绪直陈」打架）
# ======================================================================

def test_提示词里划清了情绪直陈与心理活动的边界():
    """实测：只写「禁止情绪直陈」会把心理描写一起禁掉
    （生成文本直陈心理只有参考书 1/3、身体反应是 6.7 倍）。"""
    from app.generation.prompts import build_system_prompt

    prompt = build_system_prompt(
        "测试", {"character_constraints": [], "unresolved_plotholes": []})
    # 原有的禁令还在
    assert "禁止情绪直陈" in prompt
    # 但明确说「不禁止写具体的心理活动」
    assert "不禁止" in prompt and "心理活动" in prompt
    # 并给出频次
    assert "每 2~3 段" in prompt


def test_提示词里含心理与场景指引():
    from app.generation.prompts import build_system_prompt

    prompt = build_system_prompt(
        "测试", {"character_constraints": [], "unresolved_plotholes": []})
    assert "【心理描写】" in prompt
    assert "【场景描写】" in prompt
    assert "心中直陈" in prompt
    assert "独立成段" in prompt


# ======================================================================
# 4. 蒸馏：新维度必须要求「两面并存 + 占比」
# ======================================================================

def test_蒸馏有心理描写维度():
    from app.style.narrative import DIM_LABELS, NARRATIVE_DIMENSIONS

    assert "心理描写手法" in NARRATIVE_DIMENSIONS
    assert DIM_LABELS.get("心理描写手法")


def test_蒸馏心理维度要求两面并存():
    """⚠️ 这是根因修复：只抽「不写心理」那一面会让生成反向劣化。"""
    from app.style.narrative import _build_psych_prompt

    sysp, _user = _build_psych_prompt("他心中叹息，指节发白。", "测试 第1章")
    assert "直陈心理" in sysp
    assert "身体反应" in sysp
    assert "只写一面" in sysp          # 明确说「只写一面就是漏了」
    assert "占比" in sysp
    assert "心理写法占比" in sysp


def test_蒸馏心理维度禁止具体内容():
    from app.style.narrative import _build_psych_prompt

    sysp, _user = _build_psych_prompt("x", "y")
    assert "不要写具体人名" in sysp
    assert "不要复述" in sysp


def test_蒸馏调度挂上了心理维度():
    """⚠️ 只在维度表里加名字不够 —— 调度与收集都要挂。"""
    import inspect

    from app.style import narrative

    src = inspect.getsource(narrative)
    assert '"心理描写手法" in has_dim' in src
    assert 'chunk_prompt_kinds.append("psych")' in src
    assert 'ps_raw = _raw_for("psych")' in src
    assert "_PSYCH_NOISE_PREFIX_RE" in src


def test_蒸馏产物带心理占比字段():
    import inspect

    from app.style import narrative

    src = inspect.getsource(narrative)
    assert '"psych_ratio"' in src
    assert "psych_ratios" in src
