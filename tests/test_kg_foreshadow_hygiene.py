"""伏笔进提示词的卫生契约（2026-10-28）。

## 背景

真实项目只写了 9 段正文，账本上就有 **117 个伏笔名**、切片把 97 条 active
一条不落地渲染进提示词（**2,639 字，占 user 提示词两成**）。
根因是抽取器**看不到已有的名字** ⇒ 每段给同一件事重新起一个说法。

## 本文件钉住的口径

- **抽取契约**：抽取时把「当前账上已有的未回收伏笔名」给模型，
  要求**同名复用**、**只抽本段新埋的**；提示里的 JSON 示例**不含具体章号**
  （模型会照抄示例的字面值 —— 实测 18 条 `planned_payoff_chapter=50`）。
- **归一化不变式**：说不通的回收章（`0` / 不晚于埋下章 / 非整数）⇒ 当作未知。
- **渲染层**：伏笔块**只列名字**（不陪标状态与回收章）；
  有**条数上限**（安全阀），超了**如实写出省略了几条**。
- 判据一律「宁可放过，不可误杀」：**不**做语义聚类合并。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.kg.extractor_v3 import (  # noqa: E402
    _EXTRACT_SYSTEM,
    known_foreshadow_block,
    normalize_extraction,
)
from app.kg.foreshadow_quality import (  # noqa: E402
    plausible_payoff_chapter,
    sanitize_payoff_chapter,
)
from app.kg.retrieval_v3 import format_slice_for_prompt  # noqa: E402


# ── 1. 归一化不变式：说不通的回收章 ⇒ 未知 ──────────────────────────────
def test_zero_payoff_chapter_is_rejected():
    """第 0 章不存在 —— 实测 97 条 active 里 72 条是这个值。"""
    assert plausible_payoff_chapter(0, 1) is False
    assert sanitize_payoff_chapter(0, 1) is None


def test_payoff_not_after_planted_is_rejected():
    """回收章必须晚于埋下章（同一章回收等于没埋）。"""
    assert plausible_payoff_chapter(3, 3) is False
    assert plausible_payoff_chapter(2, 3) is False
    assert plausible_payoff_chapter(4, 3) is True


def test_plausible_payoff_is_kept():
    assert plausible_payoff_chapter(50, 1) is True
    assert sanitize_payoff_chapter(50, 1) == 50


def test_none_and_non_int_are_rejected():
    assert plausible_payoff_chapter(None, 1) is False
    assert plausible_payoff_chapter("12", 1) is False
    assert plausible_payoff_chapter(True, 1) is False


def test_planted_unknown_still_checks_min_chapter():
    """埋下章未知时，只判「章号 ≥ 1」这条。"""
    assert plausible_payoff_chapter(12, None) is True
    assert plausible_payoff_chapter(0, None) is False


def test_normalize_sanitizes_payoff_chapter():
    raw = {"foreshadows": [
        {"name": "线索A", "status": "planted", "planted_chapter": 2, "planned_payoff_chapter": 0},
        {"name": "线索B", "status": "planted", "planted_chapter": 2, "planned_payoff_chapter": 1},
        {"name": "线索C", "status": "planted", "planted_chapter": 2, "planned_payoff_chapter": 12},
    ]}
    out = normalize_extraction(raw)["foreshadows"]
    assert [f["planned_payoff_chapter"] for f in out] == [None, None, 12], out


# ── 2. 抽取契约：把已有名字给模型 ────────────────────────────────────────
def test_known_foreshadow_block_lists_names():
    blk = known_foreshadow_block(["断刀的来历", "水门被内应打开后的走向"])
    assert "断刀的来历" in blk
    assert "水门被内应打开后的走向" in blk
    assert "原名字" in blk


def test_known_foreshadow_block_is_empty_when_no_names():
    """首次抽取没有名单 ⇒ 提示词与从前**逐字节相同**（不搭车任何行为变化）。"""
    assert known_foreshadow_block([]) == ""
    assert known_foreshadow_block(None) == ""
    assert known_foreshadow_block(["", "  "]) == ""


def test_extraction_prompt_has_no_copyable_chapter_number():
    """示例里**不能有具体章号** —— 模型会照抄（实测 18 条 planned_payoff=50）。"""
    assert '"planned_payoff_chapter": 50' not in _EXTRACT_SYSTEM
    assert '"planted_chapter": 1,' not in _EXTRACT_SYSTEM


def test_extraction_prompt_demands_name_reuse():
    assert "原名字" in _EXTRACT_SYSTEM
    assert "只抽本段新埋下的" in _EXTRACT_SYSTEM


def test_extraction_prompt_forbids_copying_example_values():
    assert "不要照抄" in _EXTRACT_SYSTEM


# ── 3. 渲染层：只列名字 + 上限如实说 ────────────────────────────────────
def _slice(foreshadows, omitted=0):
    return {
        "recent_entities": [],
        "character_states": [],
        "active_foreshadows": foreshadows,
        "foreshadows_omitted": omitted,
        "known_facts": [],
        "recent_events": [],
    }


def test_foreshadow_block_lists_names_only():
    sl = _slice([{"name": "断刀的来历", "status": "planted", "planted_chapter": 1,
                  "planned_payoff_chapter": 50}])
    out = format_slice_for_prompt(sl)
    assert "- 断刀的来历" in out, out
    # 不陪标状态与回收章（回收章 97 条里只有 2 条说得通，编出来的约束比没有更糟）
    assert "计划第" not in out, out
    assert "planted" not in out, out


def test_foreshadow_block_states_how_many_were_omitted():
    sl = _slice([{"name": "A", "status": "planted", "planted_chapter": 1}],
                omitted=37)
    out = format_slice_for_prompt(sl)
    assert "另有 37 条未列出" in out, out


def test_foreshadow_block_silent_when_nothing_omitted():
    sl = _slice([{"name": "A", "status": "planted", "planted_chapter": 1}], omitted=0)
    out = format_slice_for_prompt(sl)
    assert "未列出" not in out, out


def test_foreshadow_block_never_drops_a_name():
    """上限只砍**条数**，不砍某一条的**内容** —— 每条线索都还在。"""
    names = [f"线索{i}" for i in range(20)]
    sl = _slice([{"name": n, "status": "planted", "planted_chapter": 1} for n in names])
    out = format_slice_for_prompt(sl)
    for n in names:
        assert n in out, n
