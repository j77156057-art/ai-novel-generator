"""提示词三块噪声的修复契约（2026-10-28）。

## 背景

拿发布版**真实**提示词渲染出来量，发现三块噪声：

1. 【近期出场实体】里的 `context_window` **20/20 全是半句**（窗口从正文中间截开）
   ⇒ 又费 token 又不可扫读，还可能让模型照抄半截话。
2. KG 抽来的「未填伏笔」**19 条里 17 条是对话碎片**（`“你把我当什么？”`……）
   ⇒ 照原样进了 system 提示的约束块。
3. 字面量 `None` 进了提示词（「人物关系：沈砚与沈铁山——None」）。

## 本文件钉住的口径

- 实体 context 压成**一句完整的话**：完整句优先、尾部残句丢掉、
  起点落在句中就砍掉开头那个残从句、同块内重复片段只留一处。
- 伏笔过滤**只作用于 KG 抽来的**；**用户自己在剧情圣经里写的伏笔一个字都不动**。
- 空关系**整条不列**（不渲染成「——None」）。
- 判据一律「宁可放过，不可误杀」：说不清的一律留下。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.kg.foreshadow_quality import (  # noqa: E402
    looks_like_dialogue_fragment,
    split_quality,
)
from app.kg.retrieval_v3 import (  # noqa: E402
    _compact_context,
    format_slice_for_prompt,
)
from app.generation.prompts import build_user_prompt, merged_constraint_items  # noqa: E402


# ── 1. 对话碎片判据 ──────────────────────────────────────────────────────
def test_question_mark_is_dialogue():
    assert looks_like_dialogue_fragment("你把我当什么？") is True


def test_speech_colon_quote_is_dialogue():
    assert looks_like_dialogue_fragment("那人弯腰看向脚边：磨了做什么？") is True


def test_leading_quote_is_dialogue():
    assert looks_like_dialogue_fragment("“还是留在这里？”") is True


def test_plain_statement_is_kept():
    assert looks_like_dialogue_fragment("断刀的刀镡上刻着一个沈砚不认识的姓") is False


def test_empty_is_kept():
    assert looks_like_dialogue_fragment("") is False
    assert looks_like_dialogue_fragment(None) is False


def test_split_quality_partitions():
    keep, junk = split_quality(["你把我当什么？", "断刀的来历", "“柳家的人。”"])
    assert keep == ["断刀的来历"]
    assert junk == ["你把我当什么？", "“柳家的人。”"]


# ── 2. 伏笔过滤：只动 KG 的，不动 bible 的 ───────────────────────────────
def test_kg_dialogue_fragments_are_dropped():
    reminders = {
        "unresolved_plotholes": [
            {"question": "你把我当什么？"},
            {"question": "还是留在这里？"},
            {"question": "断刀的刀镡上刻着一个沈砚不认识的姓"},
        ],
    }
    _chars, fores = merged_constraint_items(reminders, None)
    assert fores == ["断刀的刀镡上刻着一个沈砚不认识的姓"], fores


def test_bible_foreshadow_is_never_touched():
    """用户在剧情圣经里写的伏笔**一个字都不动** —— 哪怕它长得像问句。

    那是用户的权威构思，模型自己写的问句才可能是抽取器的误判。
    """
    reminders = {"unresolved_plotholes": [{"question": "你把我当什么？"}]}
    bible = {"foreshadowing": ["父亲还会回来吗？"]}
    _chars, fores = merged_constraint_items(reminders, bible)
    assert "父亲还会回来吗？" in fores, fores
    assert "你把我当什么？" not in fores, fores


def test_bible_foreshadow_order_is_preserved():
    reminders = {"unresolved_plotholes": [{"question": "KG 抽的悬念"}]}
    bible = {"foreshadowing": ["第一条", "第二条"]}
    _chars, fores = merged_constraint_items(reminders, bible)
    assert fores == ["第一条", "第二条", "KG 抽的悬念"], fores


# ── 3. 实体 context 压成完整句 ──────────────────────────────────────────
def test_context_keeps_complete_sentence():
    raw = "周郎中推门进来，肩上沾着河雾，手里还提着一只湿漉漉的药箱。他反手关门，从门缝里看了一眼街口。"
    out = _compact_context(raw)
    assert out.endswith("。"), out
    assert out == "他反手关门，从门缝里看了一眼街口。", out


def test_context_drops_trailing_fragment():
    """窗口尾巴断在半句 ⇒ 那半句丢掉，只留前面完整的。"""
    raw = "他反手关门，从门缝里看了一眼街口，才把药箱放到柜上。\n\n沈"
    out = _compact_context(raw)
    assert out == "他反手关门，从门缝里看了一眼街口，才把药箱放到柜上。", out
    assert "沈" not in out, out


def test_context_trims_leading_broken_clause():
    """窗口**起点**落在句中 ⇒ 砍掉开头那个残从句（而不是整句作废）。

    真实样本（验收库）：`刀镡` 的窗口是
    「了一眼街口，才把药箱放到柜上。\\n\\n沈砚坐在柜台后，正用麻布擦刀镡，那半个陌生的字被水泡过，边缘发暗…」
    ⇒ 后半句没有句号（被截断），能用的只有前半句 —— 砍掉「看了一眼街口，」后
    剩下的「才把药箱放到柜上。」仍然是一句能读的话。
    """
    raw = "了一眼街口，才把药箱放到柜上。"
    out = _compact_context(raw)
    assert out == "才把药箱放到柜上。", out
    assert not out.startswith("了"), out


def test_context_prefers_the_last_complete_sentence():
    """有多个完整句时取**最后**那个（离 mention 最近的那段）。"""
    raw = "了一眼街口，才把药箱放到柜上。\n\n沈砚坐在柜台后，正用麻布擦刀镡。"
    out = _compact_context(raw)
    assert out == "沈砚坐在柜台后，正用麻布擦刀镡。", out


def test_context_has_no_newline():
    """原文的段落空行不许进提示词 —— 一行就是一行。"""
    raw = "周郎中抬手，乌木秤杆从袖中滑出。\n\n“二十年前死在北漠王庭的那个人，叫什么？”"
    out = _compact_context(raw)
    assert "\n" not in out and "  " not in out, out


def test_context_is_bounded():
    long_sentence = "他" + "走了很久" * 30 + "。"
    out = _compact_context(long_sentence, limit=40)
    assert len(out) <= 41, len(out)


def test_context_empty_for_blank():
    assert _compact_context("") == ""
    assert _compact_context("   ") == ""


# ── 4. 实体块渲染：说明行 + 同块去重 ───────────────────────────────────
def _slice_with_contexts(contexts: list[str]) -> dict:
    return {
        "recent_entities": [
            {"name": f"名{i}", "kind": "item", "context": c, "mention_count": 3}
            for i, c in enumerate(contexts)
        ],
        "character_states": [],
        "active_foreshadows": [],
        "known_facts": [],
        "recent_events": [],
    }


def test_entity_block_dedups_same_context():
    same = "周郎中抬手，乌木秤杆从袖中滑出。"
    out = format_slice_for_prompt(_slice_with_contexts([same, same]))
    assert out.count(same) == 1, out
    # 第二条只留名字与类型
    assert "- 名1（item）\n" in out or out.rstrip().endswith("- 名1（item）"), out


def test_entity_block_has_explanation_line():
    out = format_slice_for_prompt(_slice_with_contexts(["他反手关门，从门缝里看了一眼街口。"]))
    assert "只作指代参考" in out, out


def test_entity_block_never_prints_literal_none():
    out = format_slice_for_prompt(_slice_with_contexts([None, ""]))
    assert "None" not in out, out


# ── 5. 空关系整条不列 ───────────────────────────────────────────────────
def test_empty_relationship_is_not_rendered():
    bible = {
        "relationships": [
            {"a": "沈砚", "b": "沈铁山", "relation": None},
            {"a": "沈砚", "b": "周郎中", "relation": "  "},
            {"a": "沈砚", "b": "柳惊寒", "relation": "none"},
        ],
    }
    out = build_user_prompt([], [], None, 3000, plot_bible=bible)
    assert "None" not in out, out
    assert "人物关系" not in out, out


def test_real_relationship_is_rendered():
    bible = {"relationships": [{"a": "沈砚", "b": "沈铁山", "relation": "父子"}]}
    out = build_user_prompt([], [], None, 3000, plot_bible=bible)
    assert "人物关系：沈砚与沈铁山——父子" in out, out
