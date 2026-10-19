"""R2 结构归并回归测试（ADR：先红后绿）。

缺陷（2026-10 QA 实测复现）：`_SPEAK_RE` 的名字槽是非贪婪 `[一-鿿]{2,3}?`，
先试 2 字；而 `_SPEAK_VERB_PATTERN` 里的 `[一-鿿]道` 会吃掉名字末字 + 「道」：

    许七安道：“走吧。”  →  group(0)='许七安道'  group(1)='许七'
                            名字=许七（伪） + 动词=安道

`finditer` 在同一位置**不会**回退重试 3 字，所以没有第二个 match 能兜住正确的
3 字名——必须在说话归因循环里显式把 2 字碎片与后一字归并回 3 字名。

本文件三组钉子：
- T1（3 条，正例已转绿）：被切碎的 3 字真名必须整体归集，2 字碎片不得出现；
- T2（8 条，护栏）：归并**不得**复活历史伪形态（「宝玉笑」「林夜说」「宝玉听」）、
  不得吃掉 2 字方式副词首字（「林夜低」）、不得破坏 `_TIME_WORD_STOP` 的
  「许久」钉子，也不得把 2 字真名「许墨」并成别的；
- T3（5 条，待办）：R2 的结构归并候选已经构造正确（== 真名），但这 5 个真名的
  **末字不在 `_NAME_CHARS`**（孝/年/叔/薇/旧），`_valid_name` 拒收 3 字候选，
  端到端仍停在 2 字碎片。这是**字表缺口**、属方案 S（另行分派），本批纪律是
  「只上 R2、不动 `_valid_name`」，故此处钉住"结构已修、字表未补"的当前事实；
  等 S 补字后本组会自动翻绿，无需改测试。

纪律：先写、先在未修复的代码上跑出**红**，修完再跑出绿。
"""
from __future__ import annotations

import os
import sys

# ─── bootstrap：与仓库其它测试一致，把 backend/ 加进 sys.path ────────────
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402

from app.kg.build import extract_from_text  # noqa: E402
from app.kg.extractor import _SPEAK_RE, _valid_name  # noqa: E402


def _speaking(text: str) -> set[str]:
    """走 QA 复现时用的同一入口，返回被判为「说话人」的角色名集合。"""

    result, _ = extract_from_text(text, segment_no=1)
    return {n for n, c in result.characters.items() if c.speaking}


# ── T1：R2 已修好的 3 条正例 ─────────────────────────────────────────────
# （文本, 真名, 不得出现的 2 字碎片）
MERGED_OK = [
    ("许七安道：“走吧。”", "许七安", "许七"),
    ("宋廷风道：“嗯。”", "宋廷风", "宋廷"),
    ("孙尚书道：“罢了。”", "孙尚书", "孙尚"),
]


@pytest.mark.parametrize("text,full,frag", MERGED_OK)
def test_r2_merges_sliced_three_char_name(text: str, full: str, frag: str) -> None:
    """非贪婪名字槽切碎的 3 字真名必须归并回来，2 字碎片不得再入图。"""

    s = _speaking(text)
    assert full in s, f"{text!r} 未归并为 {full}，实际 speaking={sorted(s)}"
    assert frag not in s, f"{text!r} 仍切出碎片 {frag}"


# ── T2：护栏 —— 归并不得误伤 ─────────────────────────────────────────────
# （文本, 必须含, 必须不含, 是否要求 speaking 集合恰等于「必须含」）
GUARDS = [
    # 「笑/说」是说话动词 → 挡在 _GLUE_VERB_LEADS，不得复活「宝玉笑」「林夜说」
    ("宝玉笑道：“你来啦。”", ["宝玉"], [], True),
    ("林夜说道：“好。”", ["林夜"], [], True),
    # 「低声」是 2 字方式副词 → 首字「低」挡在 _SPEAK_ADV_LEAD_CHARS，不得吃成「林夜低」
    ("林夜低声说道：“好。”", ["林夜"], [], True),
    # 「听说」的「听」→ 不得复活「宝玉听」
    ("宝玉听说此事，笑道：“哦？”", ["宝玉"], [], False),
    # 3 字名本来就匹配得上，不受归并影响
    ("秦九歌说道：“走。”", ["秦九歌"], [], True),
    # 62609a5 回归钉子：时间词「许久」不得被标为说话人
    ("许久后低声道：“好。”", [], ["许久"], False),
    # 「说道」形态下 3 字名本就完整
    ("许七安说道：“走吧。”", ["许七安"], [], True),
    # 2 字真名不得被并成 3 字伪名
    ("许墨道：“嗯。”", ["许墨"], [], True),
]


@pytest.mark.parametrize("text,must,must_not,exact", GUARDS)
def test_r2_guards_no_collateral(
    text: str, must: list, must_not: list, exact: bool
) -> None:
    """归并只治「被切碎的 3 字名」，不得复活粘连形态、不得造新伪角色。"""

    s = _speaking(text)
    for n in must:
        assert n in s, f"{text!r} 丢了 {n}，实际 speaking={sorted(s)}"
    for n in must_not:
        assert n not in s, f"{text!r} 不该出现 {n}"
    if exact:
        assert s == set(must), f"{text!r} speaking 应为 {must}，实际 {sorted(s)}"


def test_r2_tightening_blocks_resurrected_fragment() -> None:
    """收紧护栏：2 字片段**本身**未过 `_valid_name` 时不得归并。

    「终于」被 `_NAME_WORD_STOP` 拒收（dd3b7d6 加的词）；不收紧时
    「终于知道」会借后一字「知」起死回生成伪角色「终于知」。
    """

    assert _speaking("终于知道原因了。") == set()


# ── T3：结构已修、字表未补的 5 条（方案 S 待办） ──────────────────────────
# 末字 孝/年/叔/薇/旧 不在 _NAME_CHARS，_valid_name 拒收 3 字候选。
PENDING_NAME_CHARS = [
    ("朱广孝说道：“是。”", "朱广孝", "朱广"),
    ("许新年道：“嗯。”", "许新年", "许新"),
    ("许二叔道：“别闹。”", "许二叔", "许二"),
    ("褚采薇道：“哼。”", "褚采薇", "褚采"),
    ("许辞旧道：“好。”", "许辞旧", "许辞"),
]


@pytest.mark.parametrize("text,full,frag", PENDING_NAME_CHARS)
def test_r2_candidate_is_real_name_blocked_by_name_chars(
    text: str, full: str, frag: str
) -> None:
    """R2 归并候选必须就是真名；端到端翻绿与否取决于 _NAME_CHARS（方案 S）。"""

    m = next(_SPEAK_RE.finditer(text))
    g1 = m.group(1)
    nxt = m.group(0)[len(g1):len(g1) + 1]
    # 3 字槽已命中时无需归并；2 字槽命中时才取后一字归并
    candidate = g1 if len(g1) == 3 else g1 + nxt
    assert candidate == full, (
        f"{text!r} 归并候选应为真名 {full}，实际 {candidate}（g0={m.group(0)!r}）"
    )

    s = _speaking(text)
    if _valid_name(full):
        # S 补字后走这条：真名入图、碎片消失
        assert full in s and frag not in s
    else:
        # 当前事实：末字不在 _NAME_CHARS，_valid_name 拒收，仍停在 2 字碎片
        assert full not in s
