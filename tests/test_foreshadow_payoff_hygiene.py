"""伏笔「计划回收章」脏数据清理（2026-10-29）。

## 背景（发布版冒烟库实测）

`kg_foreshadows.planned_payoff_chapter` 里 140 条有 **108 条是编造的**：
`0` × 84、`50` × 22、`30` × 1、`12` × 1。危害：

- **83 条假告警** —— judge 的 `check_foreshadow_expiry` 按
  「`pf < current_chapter` ⇒ 超期」判，`pf=0` 比任何当前章都小
  ⇒ 报出「计划第 0 章回收，已超期 9 章仍未揭晓」。
- 界面显示「埋@3 → 计划 50」——正文里**一个字都找不到**这个章号。

## 本文件钉住的口径

- **判据是证据，不是数值上界**：`planned_payoff_chapter` 是「**计划**回收章」，
  指向**将来**的章是合法且有用的（长篇里伏笔几十章后才收是常态）⇒
  **不能**用「≤ 已写章数」当上界（那会杀掉整条路线图功能）。
  判据 = 数值不变式（≥1、晚于埋下章）**+ 正文证据**（这个章号正文里出现过）。
- **正文证据只认「章」「节」**：「第一回」= 第一次，不是章节。
- **状态敏感**：`paid_off`/`dropped` 的 `pf == planted` 是合法的同章回收，**不清**。
- **置空而不是猜**：说不通 ⇒ 当作未知（`NULL`），不猜一个数补上。
- **三处同一把尺**：落库 / 读取 / 存量清理。
- **阳性对照**：匹配器必须能命中真章号（否则「零证据」是假的）。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.kg.chapter_evidence import (  # noqa: E402
    mentioned_chapters,
    parse_chinese_number,
    payoff_chapter_in_text,
)
from app.kg.foreshadow_cleanup import clean_foreshadow_row  # noqa: E402
from app.kg.foreshadow_quality import (  # noqa: E402
    plausible_payoff_chapter,
    sanitize_payoff_chapter,
)


# ── 1. 中文数字解析 ───────────────────────────────────────────────────────
def test_parse_chinese_numbers():
    assert parse_chinese_number("十二") == 12
    assert parse_chinese_number("三十") == 30
    assert parse_chinese_number("一百三十") == 130
    assert parse_chinese_number("十") == 10
    assert parse_chinese_number("五") == 5
    assert parse_chinese_number("12") == 12
    assert parse_chinese_number("abc") is None
    assert parse_chinese_number("") is None


# ── 2. 阳性对照：匹配器能命中真章号 ───────────────────────────────────────
def test_mentions_real_chapter_numbers():
    """阳性对照：真章号必须命中（否则「正文零证据」是假的）。"""
    assert mentioned_chapters("（第一章。）") == {1}
    assert mentioned_chapters("（第二章。）") == {2}
    assert mentioned_chapters("白马出凉州第一百三十章八叉") == {130}
    assert mentioned_chapters("第十二章") == {12}
    assert mentioned_chapters("第 12 章") == {12}
    assert mentioned_chapters("第三章") == {3}


def test_section_also_counts():
    assert mentioned_chapters("第五节") == {5}


def test_hui_is_not_a_chapter():
    """⚠️「回」是**次数**，不是章节 —— 「第二回」不该被当成「第 2 章」。"""
    assert mentioned_chapters("停过两回，第一回是傍晚，第二回是换防以后") == set()
    assert payoff_chapter_in_text(2, "停过两回，第二回是换防以后") is False


# ── 3. 证据判据 ───────────────────────────────────────────────────────────
def test_payoff_needs_text_evidence():
    """数值合法但正文没这个章号 ⇒ 不采信。"""
    assert plausible_payoff_chapter(8, 1) is True          # 纯数值：合法
    assert plausible_payoff_chapter(8, 1, text="（第一章。）") is False
    # 埋下章未知时只判「≥1」+ 证据 ⇒ 正文写了第 1 章就采信。
    assert plausible_payoff_chapter(1, None, text="（第一章。）") is True


def test_payoff_may_point_to_future_chapter():
    """⚠️ 计划回收章可以指向**尚未写到**的将来章（路线图信息，不能杀掉）。"""
    assert plausible_payoff_chapter(60, 1) is True
    assert plausible_payoff_chapter(60, 1, text="到第六十章才见分晓") is True


def test_zero_chapter_always_rejected():
    assert plausible_payoff_chapter(0, 1) is False
    assert sanitize_payoff_chapter(0, 1) is None


def test_payoff_not_after_planted_is_rejected():
    assert plausible_payoff_chapter(3, 3) is False
    assert plausible_payoff_chapter(2, 3) is False
    assert plausible_payoff_chapter(4, 3) is True


def test_settled_row_may_payoff_in_same_chapter():
    """已回收的行记的是当时的计划 ⇒ 同章回收合法（`closed=True` 跳过该判据）。"""
    assert plausible_payoff_chapter(1, 1) is False
    assert plausible_payoff_chapter(1, 1, closed=True) is True
    assert plausible_payoff_chapter(4, 4, closed=True) is True


def test_closed_row_still_rejects_zero_chapter():
    """已收尾的行只判「章号 ≥ 1」—— `0` 不是章号（第 0 章不存在）。"""
    assert plausible_payoff_chapter(0, 1, closed=True) is False
    assert plausible_payoff_chapter(0, 1, closed=True, text="") is False


def test_none_and_non_int_rejected():
    assert plausible_payoff_chapter(None, 1) is False
    assert plausible_payoff_chapter("12", 1) is False
    assert plausible_payoff_chapter(True, 1) is False


# ── 4. 清洗：状态敏感 + 置空不猜 ─────────────────────────────────────────
class _Row:
    """最小 KgForeshadow 替身（只带清洗用到的字段）。"""

    def __init__(self, pf, planted, status):
        self.planned_payoff_chapter = pf
        self.planted_chapter = planted
        self.status = status


def test_clean_row_clears_fabricated_payoff():
    row = _Row(50, 3, "planted")
    assert clean_foreshadow_row(row, text="（第一章。）") is True
    assert row.planned_payoff_chapter is None


def test_clean_row_clears_zero_payoff():
    row = _Row(0, 1, "developing")
    assert clean_foreshadow_row(row, text="（第一章。）") is True
    assert row.planned_payoff_chapter is None


def test_clean_row_keeps_settled_same_chapter():
    """已回收的同章回收 ⇒ **一条都不许清**。"""
    row = _Row(1, 1, "paid_off")
    assert clean_foreshadow_row(row, text="") is False
    assert row.planned_payoff_chapter == 1


def test_clean_row_clears_zero_payoff_on_dropped_row():
    """已丢弃的线索：`pf=0` 是纯脏值（第 0 章不存在）⇒ 清。

    ⚠️ `dropped` 与 `paid_off` 语义不同：丢弃的线索**从未回收**，
    没有「同章回收」这层豁免。
    """
    row = _Row(0, 4, "dropped")
    assert clean_foreshadow_row(row, text="") is True
    assert row.planned_payoff_chapter is None


def test_clean_row_keeps_dropped_row_with_valid_payoff():
    """已丢弃但回收章合法（≥1）⇒ 历史记录照旧，不重新裁决。"""
    row = _Row(8, 4, "dropped")
    assert clean_foreshadow_row(row, text="") is False
    assert row.planned_payoff_chapter == 8


def test_clean_row_keeps_evidenced_payoff():
    row = _Row(1, None, "planted")
    assert clean_foreshadow_row(row, text="（第一章。）") is False
    assert row.planned_payoff_chapter == 1


def test_clean_row_no_op_when_null():
    row = _Row(None, 1, "planted")
    assert clean_foreshadow_row(row, text="（第一章。）") is False
    assert row.planned_payoff_chapter is None


def test_clean_row_without_text_clears_everything():
    """正文取不到 ⇒ 证据判据不放行 ⇒ 置空（宁可当未知，不留编的值）。"""
    row = _Row(8, 1, "planted")
    assert clean_foreshadow_row(row, text="") is True
    assert row.planned_payoff_chapter is None
