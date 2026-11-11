# -*- coding: utf-8 -*-
"""局部指令 → 句子索引补丁：单句 / 多句 / 越界 / 整段拒绝 / 脏输出 / 旧路径。

对应 2026-11-12「程序切句编号、模型只回编号 + 改写句」方案。这里**不 mock
补丁逻辑**：真实切句、真实解析模型返回、真实按编号替换；引擎级用例真实走
``regenerate_segment_stream`` 与快照落库。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agents.dialog.snapshot import begin_snapshot  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import regenerate_segment_stream  # noqa: E402
from app.generation.patch import (  # noqa: E402
    PatchError,
    build_candidates_user_prompt,
    build_patch_user_prompt,
    clarify_span_limit,
    parse_candidates_reply,
    parse_patch_reply,
    resolve_scope,
    scope_from_selection,
    split_sentences,
)
from app.main import app as fastapi_app  # noqa: E402
from app.models import Generation, GenerationSnapshot, Project  # noqa: E402
from app.routers import assistant_generation as ag  # noqa: E402

SEG1 = "林夜推开木门。\n他说：“我回来了。”"
SEG2 = "第一句。第二句。第三句。"


def _assert_no_numbering_scaffold(prompt: str) -> None:
    """改写提示词不得含任何编号脚手架（不是禁止正文里本来就有的数字）。"""
    assert ">>" not in prompt
    assert "from_index" not in prompt
    assert "编号" not in prompt
    for line in prompt.splitlines():
        assert not re.match(r"^\s*\d+[.．、]\s", line), line


# ---------------------------------------------------------------------------
# 单元：切句、编号解析与替换
# ---------------------------------------------------------------------------


def test_split_sentences_keeps_quotes_and_tail_fragment():
    spans = split_sentences("他说：“走吧。”\n她还站着")
    assert [s["text"] for s in spans] == ["他说：“走吧。”", "她还站着"]
    # 句间换行不属于任何句子（替换不会吞掉段落间隔）
    assert "她还站着".startswith("她")


def test_single_sentence_scope_replaces_exactly_one():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("第2句", before)
    new_text, meta = parse_patch_reply("改过的第二句。", before, scope)
    assert new_text == "第一句。改过的第二句。第三句。"
    assert new_text.startswith("第一句。") and new_text.endswith("第三句。")
    applied = meta["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (2, 2)
    assert applied["before"] == "第二句。"
    assert applied["replacement"] == "改过的第二句。"


def test_multi_sentence_span_replaces_consecutive_sentences():
    before = "第一句。第二句。第三句。第四句。"
    scope = resolve_scope("第2句到第3句", before)
    new_text, meta = parse_patch_reply("改过的二。改过的三。", before, scope)
    assert new_text == "第一句。改过的二。改过的三。第四句。"
    applied = meta["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (2, 3)
    assert applied["before"] == "第二句。第三句。"


def test_empty_replacement_is_rejected():
    scope = resolve_scope("第2句", SEG2)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply('{"index": 2, "replacement": "   "}', SEG2, scope)
    assert ei.value.code == "empty_patch"


def test_replacement_missing_field_is_rejected():
    scope = resolve_scope("第2句", SEG2)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply('{"index": 2}', SEG2, scope)
    assert ei.value.code == "replacement_missing"


def test_replacement_that_is_whole_segment_is_rejected():
    before = "第一句。第二句。第三句。第四句。第五句。第六句。"
    scope = resolve_scope("第2句", before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply(before, before, scope)
    assert ei.value.code == "replacement_whole_segment"


def test_single_sentence_span_rejects_multi_sentence_replacement():
    before = "甲乙丙丁。戊己庚辛。壬癸。"
    scope = resolve_scope("第1句", before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply("一二。三四。五六。", before, scope)
    assert ei.value.code == "replacement_too_long"


def test_dirty_reply_with_fence_and_prose_is_repaired():
    reply = (
        "好的，这是补丁：\n```json\n"
        '{"replacement": "改过的第二句。"}\n'
        "```\n（只改了第二句）"
    )
    scope = resolve_scope("第2句", SEG2)
    new_text, meta = parse_patch_reply(reply, SEG2, scope)
    assert new_text == "第一句。改过的第二句。第三句。"
    assert meta["patch_count"] == 1
    # 位置完全由程序决定（Tier A），模型只提供替换文本。
    assert meta["stages"] == 1 and meta["scope_source"] == "program"


def test_broken_json_reply_is_unparsable():
    scope = resolve_scope("第2句", SEG2)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply('{"replacement": "改过的第二句。"', SEG2, scope)
    assert ei.value.code == "unparsable"


# ---------------------------------------------------------------------------
# 剥除行首编号（83. / 83、/ 83)/83）/ (83)）（D 组）
# ---------------------------------------------------------------------------


def test_leading_sentence_numbers_are_stripped():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("最后两句", before)  # 2..3
    new_text, meta = parse_patch_reply(
        "2. 改过的二。\n3. 改过的三。", before, scope
    )
    assert new_text == "第一句。改过的二。\n改过的三。"
    assert meta["applied"][0]["replacement"] == "改过的二。\n改过的三。"


def test_chinese_and_parenthesized_numbers_are_stripped():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("最后两句", before)
    replies = (
        "二、改过的二。\n三）改过的三。",
        "（二）改过的二。\n（三）改过的三。",
        "(2) 改过的二。\n3) 改过的三。",
    )
    for reply in replies:
        new_text, _ = parse_patch_reply(reply, before, scope)
        assert new_text.startswith("第一句。改过的二。"), reply
        assert "改过的三。" in new_text, reply


def test_numbering_only_replacement_fails_closed():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("最后两句", before)
    for reply in ("2. 3.", "2、3）", "（2）（3）", "2.\n3."):
        with pytest.raises(PatchError) as ei:
            parse_patch_reply(reply, before, scope)
        assert ei.value.code == "replacement_numbering_only", reply


def test_punctuation_only_replacement_fails_closed():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("最后两句", before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply("2. 。", before, scope)
    assert ei.value.code == "replacement_numbering_only"


def test_plain_replacement_is_not_stripped():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("第2句", before)
    new_text, _ = parse_patch_reply("他数到三。然后离开。", before, scope)
    assert new_text == "第一句。他数到三。然后离开。第三句。"


def test_decimal_is_not_mistaken_for_numbering():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("第2句", before)
    new_text, _ = parse_patch_reply("3.5 倍的差距。", before, scope)
    assert new_text == "第一句。3.5 倍的差距。第三句。"


# ---------------------------------------------------------------------------
# 改写提示词去编号：只给目标句分隔块，不给整段编号与 >> 标记（E 组）
# ---------------------------------------------------------------------------


def test_patch_prompt_contains_no_numbering_scaffolding():
    before = "第一句。\n第二句。\n第三句。"
    scope = resolve_scope("最后两句", before)
    prompt = build_patch_user_prompt(before, "把这两句改顺", scope)
    _assert_no_numbering_scaffold(prompt)
    assert "第一句。" not in prompt, "非目标句不得出现在改写提示词里"


def test_patch_prompt_contains_only_target_sentences():
    before = "第一句。\n第二句。\n第三句。\n第四句。"
    scope = resolve_scope("第2句到第3句", before)
    prompt = build_patch_user_prompt(before, "改顺", scope)
    assert "第二句。\n第三句。" in prompt
    assert "第一句。" not in prompt and "第四句。" not in prompt


def test_multi_sentence_replacement_keeps_sentence_count():
    before = "第一句。第二句。第三句。第四句。"
    scope = resolve_scope("第2句到第3句", before)
    for reply in ("只改了一句。", "一。二。三。"):
        with pytest.raises(PatchError) as ei:
            parse_patch_reply(reply, before, scope)
        assert ei.value.code == "replacement_sentence_mismatch", reply


def test_multi_sentence_replacement_restores_exactly():
    before = "开头。第二句。第三句。结尾。"
    scope = resolve_scope("第2句到第3句", before)
    replacement = "改过的二。改过的三。"
    new_text, meta = parse_patch_reply(replacement, before, scope)
    assert new_text == "开头。" + replacement + "结尾。"
    assert new_text.startswith("开头。") and new_text.endswith("结尾。")
    assert meta["numbering_stripped"] is False


def test_numbering_stripped_flag_reports_fallback_trigger():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("最后两句", before)
    new_text, meta = parse_patch_reply(
        "2. 改过的二。\n3. 改过的三。", before, scope
    )
    assert new_text == "第一句。改过的二。\n改过的三。"
    assert meta["numbering_stripped"] is True


def test_echoed_target_delimiters_are_stripped():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("第2句", before)
    reply = "【待改写片段开始】\n改过的第二句。\n【待改写片段结束】"
    new_text, meta = parse_patch_reply(reply, before, scope)
    assert new_text == "第一句。改过的第二句。第三句。"
    assert meta["numbering_stripped"] is False


# ---------------------------------------------------------------------------
# 含糊指令的候选澄清（F 组）：只收编号、程序截预览、越界/倒序/重叠丢弃
# ---------------------------------------------------------------------------


def test_candidates_prompt_has_no_replacement_slot():
    prompt = build_candidates_user_prompt(SEG2, "这一句不太顺")
    assert "1. 第一句。" in prompt and "3. 第三句。" in prompt
    assert '{"candidates": [{"from_index": 起始句编号' in prompt
    assert "不要输出 replacement" in prompt
    assert '>> ' not in prompt, "未选位置前不应标记目标句"


def test_clarify_span_limit_is_min_of_three_and_ten_percent():
    # 85 句：10% = 8，取更小的 3；20 句：2；不足 10 句时退化为 1。
    assert clarify_span_limit(85) == 3
    assert clarify_span_limit(20) == 2
    assert clarify_span_limit(9) == 1
    assert clarify_span_limit(0) == 1


def test_candidates_discard_out_of_range_reversed_whole_and_overlap():
    before = "句。" * 20  # limit = min(3, 2) = 2
    reply = (
        '{"candidates": ['
        '{"from_index": 1, "to_index": 99, "reason": "越界"},'
        '{"from_index": 8, "to_index": 7, "reason": "倒序"},'
        '{"from_index": 1, "to_index": 20, "reason": "覆盖全文"},'
        '{"from_index": 5, "to_index": 7, "reason": "超过局部上界"},'
        '{"from_index": 3, "to_index": 4, "reason": "有效"},'
        '{"from_index": 4, "to_index": 5, "reason": "与前者重叠"},'
        '{"from_index": 10, "to_index": 11, "reason": "有效二"}'
        "]}"
    )
    candidates = parse_candidates_reply(reply, before)
    assert [(c["from_index"], c["to_index"]) for c in candidates] == [(3, 4), (10, 11)]
    assert [c["id"] for c in candidates] == ["c1", "c2"]


def test_candidates_all_discarded_fails_closed():
    before = "句。" * 20
    reply = (
        '{"candidates": ['
        '{"from_index": 1, "to_index": 99},'
        '{"from_index": 9, "to_index": 3},'
        '{"from_index": 1, "to_index": 20}'
        "]}"
    )
    with pytest.raises(PatchError) as ei:
        parse_candidates_reply(reply, before)
    assert ei.value.code == "no_valid_candidate"


def test_candidate_preview_is_cut_from_source_not_from_model():
    before = "".join(f"第{i}句。" for i in range(1, 31))  # 30 句 → 上界 3
    reply = (
        '{"candidates": [{"from_index": 2, "to_index": 3, '
        '"preview": "模型瞎编的片段", "reason": "  这里  太平  "}]}'
    )
    candidates = parse_candidates_reply(reply, before)
    assert candidates[0]["preview"] == "第2句。第3句。"
    assert candidates[0]["reason"] == "这里 太平"


def test_scope_from_selection_rejects_range_not_in_candidates():
    before = "第一句。第二句。第三句。"
    candidates = parse_candidates_reply(
        '{"candidates": [{"from_index": 2, "to_index": 2, "reason": "x"}]}',
        before,
    )
    scope = scope_from_selection(candidates, 2, 2, 3)
    assert (scope["from_index"], scope["to_index"]) == (2, 2)
    assert scope["source"] == "user"
    with pytest.raises(PatchError) as ei:
        scope_from_selection(candidates, 1, 2, 3)
    assert ei.value.code == "selection_not_in_candidates"


# ---------------------------------------------------------------------------
# Tier A：范围词由程序解析成编号（段尾 / 段首 / 第 N 句 / 显式区间）
# ---------------------------------------------------------------------------


def test_scope_words_map_to_sentence_numbers():
    before = "第一句。第二句。第三句。"
    cases = {
        "收拾段尾": (3, 3),
        "结尾": (3, 3),
        "最后一句": (3, 3),
        "段首": (1, 1),
        "开头": (1, 1),
        "第一句": (1, 1),
        "第2句": (2, 2),
    }
    for word, expected in cases.items():
        scope = resolve_scope(word, before)
        assert scope is not None, word
        assert (scope["from_index"], scope["to_index"]) == expected, word
        assert scope["source"] == "program"


def test_chinese_numeral_index_is_parsed():
    before = "。".join(["句"] * 84) + "。"
    scope = resolve_scope("只改第八十四句，其余保留", before)
    assert scope is not None
    assert (scope["from_index"], scope["to_index"]) == (84, 84)


def test_explicit_multi_sentence_range_is_resolved():
    before = "第一句。第二句。第三句。第四句。"
    scope = resolve_scope("把第2句到第3句改一下，其余保留", before)
    assert scope is not None
    assert (scope["from_index"], scope["to_index"]) == (2, 3)


def test_scope_out_of_range_raises():
    with pytest.raises(PatchError) as ei:
        resolve_scope("只改第9句，其余保留", SEG2)
    assert ei.value.code == "index_out_of_range"


def test_ambiguous_scope_returns_none_for_tier_b():
    assert resolve_scope("只改某处，其余保留", SEG2) is None
    assert resolve_scope("只改这一句，其余保留", SEG2) is None
    assert resolve_scope("局部润色一下", SEG2) is None


def test_program_scope_accepts_plain_replacement_text():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("只收拾段尾，前面的内容不要动。", before)
    new_text, meta = parse_patch_reply("改过的第三句。", before, scope=scope)
    assert new_text == "第一句。第二句。改过的第三句。"
    assert meta["scope_source"] == "program"
    applied = meta["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (3, 3)


def test_program_scope_accepts_matching_json_indices():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("仅调整最后一句，其余内容保留不要动。", before)
    new_text, meta = parse_patch_reply(
        '{"from_index": 3, "to_index": 3, "replacement": "改过的第三句。"}',
        before,
        scope=scope,
    )
    assert new_text == "第一句。第二句。改过的第三句。"
    assert meta["applied"][0]["from_index"] == 3


def test_program_scope_rejects_mismatched_model_indices():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("仅调整最后一句，其余内容保留不要动。", before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply(
            '{"index": 2, "replacement": "改过的第二句。"}', before, scope=scope
        )
    assert ei.value.code == "index_mismatch"


def test_program_scope_rejects_whole_segment_replacement():
    before = "第一句。第二句。第三句。第四句。第五句。第六句。"
    scope = resolve_scope("只收拾段尾，前面的内容不要动。", before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply(before, before, scope=scope)
    assert ei.value.code == "replacement_whole_segment"


def test_program_scope_rejects_empty_replacement():
    before = "第一句。第二句。第三句。"
    scope = resolve_scope("只收拾段尾，前面的内容不要动。", before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply("   ", before, scope=scope)
    assert ei.value.code == "empty_patch"


# ---------------------------------------------------------------------------
# Tier A：带数量的位置词（最后两句 / 末三句 / 前两句 / 头三句 / 倒数第二句）
# ---------------------------------------------------------------------------


def test_quantity_scope_words_map_to_ranges():
    before = "句。" * 16  # 上界 max(2, 16 // 4) = 4
    cases = {
        "最后两句": (15, 16),
        "最后三句": (14, 16),
        "末两句": (15, 16),
        "前两句": (1, 2),
        "头三句": (1, 3),
        "最后2句": (15, 16),
        "前3句": (1, 3),
        "倒数第二句": (15, 15),
        "倒数第三句": (14, 14),
    }
    for word, expected in cases.items():
        scope = resolve_scope(word, before)
        assert scope is not None, word
        assert (scope["from_index"], scope["to_index"]) == expected, word
        assert scope["source"] == "program"


def test_quantity_works_on_short_segment():
    # 4 句的段：total // 4 == 1，仍靠「至少 2 句」的兜底允许「最后两句」。
    scope = resolve_scope("把最后两句收拾一下，其余保留", "句。" * 4)
    assert scope is not None
    assert (scope["from_index"], scope["to_index"]) == (3, 4)


def test_quantity_over_limit_fails_closed():
    before = "句。" * 16  # 上界 4 且必须少于全文
    for word in ("最后五句", "前九句", "最后一百句", "最后十六句"):
        with pytest.raises(PatchError) as ei:
            resolve_scope(word, before)
        assert ei.value.code == "scope_count_too_large", word


def test_explicit_range_beats_quantity_words():
    before = "句。" * 16
    scope = resolve_scope("把第2句到第3句改一下，最后两句不要动", before)
    assert scope is not None
    assert (scope["from_index"], scope["to_index"]) == (2, 3)


# ---------------------------------------------------------------------------
# 引擎级：分流 / 落库 / 失败留痕 / 整段回归
# ---------------------------------------------------------------------------


class StubProvider:
    """chat 按顺序返回给定回复（两段式需要两条），stream_chat 返回整段文本。"""

    def __init__(self, reply: str | list[str], stream_reply: str | None = None) -> None:
        self.replies = reply if isinstance(reply, list) else [reply]
        self.stream_reply = (
            stream_reply if stream_reply is not None
            else (self.replies[-1] if self.replies else "")
        )
        self.chat_calls = 0
        self.stream_calls = 0
        self.prompts: list[str] = []
        self.last_user = ""

    def chat(self, system, user, constraints=None, **kw):
        self.prompts.append(user)
        self.last_user = user
        index = min(self.chat_calls, len(self.replies) - 1)
        self.chat_calls += 1
        return self.replies[index]

    def stream_chat(self, system, user, constraints=None, **kw):
        self.stream_calls += 1
        yield self.stream_reply

    def summarize(self, content: str) -> str:
        return "测试摘要"


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _project(db) -> Project:
    # 参考书为空 = 自由生成，不触发蒸馏预检（与 test_rewrite_snapshot 同口径）。
    proj = Project(title="补丁测试", target_length=5000, mode="quick",
                   status="created", reference_novel_ids=[])
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _seed(db, project, no: int, text: str) -> Generation:
    gen = Generation(project_id=project.id, segment_no=no, text=text,
                     summary={"text": f"段{no}摘要"})
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _run(db, project, provider, instruction: str, selected_scope=None):
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    snap = begin_snapshot(db, project, gen, instruction=instruction)
    events = list(regenerate_segment_stream(
        db, project, 2, provider=provider, instruction=instruction,
        snapshot_id=snap.id, expected_before=snap.before_text,
        selected_scope=selected_scope,
    ))
    done = next(e for e in events if e.get("type") == "done")
    return snap, done


def test_local_instruction_patches_one_sentence(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider("改过的第三句。")

    snap, done = _run(db, project, provider, "只改最后一句，其余内容保留不要动。")

    assert provider.chat_calls == 1 and provider.stream_calls == 0, "局部指令必须走补丁路径"
    # Tier A：程序把「最后一句」解析成第 3 句；提示词只给目标句原文，无编号。
    assert "第三句。" in provider.last_user
    _assert_no_numbering_scaffold(provider.last_user)
    db.expire_all()
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == "第一句。第二句。改过的第三句。"
    assert done["patch"] and done["patch"]["patch_count"] == 1
    assert done["patch"]["scope_source"] == "program"
    applied = done["patch"]["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (3, 3)
    assert done["scope_mismatch"] is None and done["length_drop"] is None
    assert db.get(GenerationSnapshot, snap.id).status == "applied"


def test_quantity_scope_patches_last_two_sentences(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, "第一句。第二句。第三句。第四句。第五句。第六句。")
    provider = StubProvider("改过的第五句。改过的第六句。")

    _, done = _run(db, project, provider, "把最后两句改顺，其余内容保留不要动。")

    assert provider.chat_calls == 1 and provider.stream_calls == 0, "数量词必须走补丁路径"
    assert "第五句。" in provider.last_user and "第六句。" in provider.last_user
    _assert_no_numbering_scaffold(provider.last_user)
    db.expire_all()
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == "第一句。第二句。第三句。第四句。改过的第五句。改过的第六句。"
    applied = done["patch"]["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (5, 6)
    assert applied["before"] == "第五句。第六句。"
    assert done["scope_mismatch"] is None and done["length_drop"] is None


def test_model_index_mismatch_fails_closed(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider('{"index": 2, "replacement": "改过的第二句。"}')

    with pytest.raises(PatchError) as ei:
        _run(db, project, provider, "只改最后一句，其余内容保留不要动。")
    assert ei.value.code == "index_mismatch"

    db.expire_all()
    snap = db.scalars(
        select(GenerationSnapshot).order_by(GenerationSnapshot.id.desc())
    ).first()
    assert snap.status == "failed"
    assert snap.error_reason == "index_mismatch"
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == SEG2, "编号不一致不得改写正文"


def test_ambiguous_instruction_without_selection_fails_closed(db):
    """含糊指令不再让模型定位：没有 selected_scope 就直接失败关闭。"""
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider('{"candidates": [{"from_index": 2, "to_index": 2}]}')

    with pytest.raises(PatchError) as ei:
        _run(db, project, provider, "只改某处，其余内容保留不要动。")
    assert ei.value.code == "clarification_required"
    assert provider.chat_calls == 0, "未经澄清不得调用任何模型写正文"

    db.expire_all()
    snap = db.scalars(
        select(GenerationSnapshot).order_by(GenerationSnapshot.id.desc())
    ).first()
    assert snap.status == "failed"
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == SEG2, "含糊指令未澄清时正文一行不得改动"


def test_selected_scope_uses_tier_a_prompt_and_ignores_old_replacement(db):
    """选定区间后走 Tier A：同提示词、同校验，不复用澄清阶段的模型文本。"""
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    # 模型只回改写文本；澄清阶段那条「候选 + 旧替换文本」永远不会被使用。
    provider = StubProvider("改过的第二句。")
    candidates = parse_candidates_reply(
        '{"candidates": [{"from_index": 2, "to_index": 2, "reason": "这里平"}]}',
        SEG2,
    )
    scope = scope_from_selection(candidates, 2, 2, len(split_sentences(SEG2)))

    _, done = _run(
        db, project, provider, "只改某处，其余内容保留不要动。",
        selected_scope=scope,
    )

    assert provider.chat_calls == 1 and provider.stream_calls == 0
    # 改写提示词只含目标句原文，不含编号、也不含候选清单。
    assert "第二句。" in provider.last_user
    _assert_no_numbering_scaffold(provider.last_user)
    assert "candidates" not in provider.last_user
    assert done["patch"]["scope_source"] == "user"
    assert done["patch"]["stages"] == 1
    db.expire_all()
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == "第一句。改过的第二句。第三句。"


def test_selected_scope_still_applies_all_tier_a_validation(db):
    """选定区间不等于免校验：替换文本为空照样失败关闭。"""
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider("   ")
    candidates = parse_candidates_reply(
        '{"candidates": [{"from_index": 2, "to_index": 2}]}', SEG2
    )
    scope = scope_from_selection(candidates, 2, 2, len(split_sentences(SEG2)))

    with pytest.raises(PatchError) as ei:
        _run(db, project, provider, "只改某处，其余内容保留不要动。",
             selected_scope=scope)
    assert ei.value.code == "empty_patch"

    db.expire_all()
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == SEG2, "校验失败不得改动正文"


def test_local_patch_failure_marks_snapshot_failed_and_keeps_text(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    # 目标句是第 3 句，模型却回了第 2 句的编号 → 失败关闭。
    provider = StubProvider('{"index": 2, "replacement": "改过的第二句。"}')

    with pytest.raises(PatchError) as ei:
        _run(db, project, provider, "只改最后一句，其余内容保留不要动。")
    assert ei.value.code == "index_mismatch"

    db.expire_all()
    snap = db.scalars(
        select(GenerationSnapshot).order_by(GenerationSnapshot.id.desc())
    ).first()
    assert snap.status == "failed"
    assert snap.error_reason == "index_mismatch"
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == SEG2, "补丁失败不得回退成整段重写"


def test_whole_segment_instruction_keeps_legacy_path(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    rewritten = "全新的整段文字。"
    provider = StubProvider("{}", stream_reply=rewritten)

    _, done = _run(db, project, provider, "换一种语气")

    assert provider.stream_calls == 1 and provider.chat_calls == 0, "整段指令不能被分流"
    db.expire_all()
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == rewritten
    assert done["patch"] is None


# ---------------------------------------------------------------------------
# 路由级（G 组）：澄清 202 / 不写正文 / 幂等 / 两轮上限 / 选定后走 Tier A
# ---------------------------------------------------------------------------


def _md5(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _post(client, project_id: int, instruction: str, **extra):
    payload = {
        "project_id": project_id,
        "action": "regenerate_segment",
        "segment_no": 2,
        "instruction": instruction,
    }
    payload.update(extra)
    return client.post("/api/assistant/invoke-generation", json=payload)


def _clarify_rows(db, project, segment_no: int = 2):
    return db.scalars(
        select(GenerationSnapshot)
        .where(
            GenerationSnapshot.project_id == project.id,
            GenerationSnapshot.segment_no == segment_no,
        )
        .order_by(GenerationSnapshot.id)
    ).all()


def test_clarification_returns_candidates_and_writes_nothing(db, monkeypatch):
    """含糊指令：202 + 候选清单；正文 md5 不变、不建 pending、重试幂等。"""
    project = _project(db)
    _seed(db, project, 1, SEG1)
    gen = _seed(db, project, 2, SEG2)
    before_md5 = _md5(gen.text)
    provider = StubProvider(
        '{"candidates": ['
        '{"from_index": 1, "to_index": 1, "reason": "开头太虚", '
        '"preview": "模型瞎编"},'
        '{"from_index": 2, "to_index": 2, "reason": "这里平"}'
        "]}"
    )
    monkeypatch.setattr("app.llm.provider.get_llm_provider", lambda: provider)
    monkeypatch.setattr(ag, "_run_ai_flavor_feedback", lambda *a, **k: None)

    instruction = "只改某处，其余内容保留不要动。"
    with TestClient(fastapi_app) as client:
        first = _post(client, project.id, instruction)
        assert first.status_code == 202, first.text
        second = _post(client, project.id, instruction)
        assert second.status_code == 202, second.text
        body1, body2 = first.json(), second.json()

    assert body1["status"] == "needs_clarification"
    assert body1["code"] == "needs_clarification"
    assert body1["clarification_id"] == body2["clarification_id"]
    assert body1["candidates"] == body2["candidates"]
    assert provider.chat_calls == 1, "重复提交必须幂等，不得生成第二批候选"
    # preview 由程序按编号从原文截取，不采信模型复述。
    assert body1["candidates"][0]["preview"] == "第一句。"
    assert body1["candidates"][1]["reason"] == "这里平"

    db.expire_all()
    rows = _clarify_rows(db, project)
    assert len(rows) == 1
    assert rows[0].status == "needs_clarification"
    assert rows[0].clarification["round"] == 1
    assert all(r.status != "pending" for r in rows), "澄清阶段不得建 pending 快照"
    gen = db.get(Generation, gen.id)
    assert gen.text == SEG2
    assert _md5(gen.text) == before_md5, "澄清阶段正文 md5 必须不变"


def test_clarification_round_limit_fails_closed(db, monkeypatch):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    gen = _seed(db, project, 2, SEG2)
    before_md5 = _md5(gen.text)
    provider = StubProvider([
        '{"candidates": [{"from_index": 2, "to_index": 2, "reason": "一"}]}',
        '{"candidates": [{"from_index": 3, "to_index": 3, "reason": "二"}]}',
    ])
    monkeypatch.setattr("app.llm.provider.get_llm_provider", lambda: provider)
    monkeypatch.setattr(ag, "_run_ai_flavor_feedback", lambda *a, **k: None)

    instruction = "只改某处，其余内容保留不要动。"
    with TestClient(fastapi_app) as client:
        r1 = _post(client, project.id, instruction)
        assert r1.status_code == 202, r1.text
        r2 = _post(
            client, project.id, instruction,
            clarification_id=r1.json()["clarification_id"],
            clarify_reply="不是这里，是后面",
        )
        assert r2.status_code == 202, r2.text
        assert r2.json()["round"] == 2
        r3 = _post(
            client, project.id, instruction,
            clarification_id=r2.json()["clarification_id"],
            clarify_reply="还是不对",
        )
        assert r3.status_code == 422, r3.text
        assert r3.json()["detail"]["code"] == "clarify_round_limit"

    db.expire_all()
    gen = db.get(Generation, gen.id)
    assert gen.text == SEG2 and _md5(gen.text) == before_md5
    assert all(r.status != "pending" for r in _clarify_rows(db, project))


def test_selection_runs_tier_a_and_does_not_reuse_candidate_text(db, monkeypatch):
    """选定候选后走 Tier A：不复用澄清阶段模型给的任何替换文本。"""
    project = _project(db)
    _seed(db, project, 1, SEG1)
    gen = _seed(db, project, 2, SEG2)
    provider = StubProvider([
        '{"candidates": [{"from_index": 2, "to_index": 2, "reason": "平", '
        '"replacement": "不该复用的旧文本。"}]}',
        "改过的第二句。",
    ])
    monkeypatch.setattr("app.llm.provider.get_llm_provider", lambda: provider)
    monkeypatch.setattr(ag, "_run_ai_flavor_feedback", lambda *a, **k: None)

    instruction = "只改某处，其余内容保留不要动。"
    with TestClient(fastapi_app) as client:
        r1 = _post(client, project.id, instruction)
        assert r1.status_code == 202, r1.text
        clarification_id = r1.json()["clarification_id"]
        candidate = r1.json()["candidates"][0]
        r2 = _post(
            client, project.id, instruction,
            clarification_id=clarification_id,
            selected={
                "from_index": candidate["from_index"],
                "to_index": candidate["to_index"],
            },
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()

    assert body["patch"]["scope_source"] == "user"
    assert body["clarification_id"] == clarification_id
    text = body["segment"]["text"]
    assert text == "第一句。改过的第二句。第三句。"
    assert "不该复用的旧文本" not in text, "不得复用澄清阶段模型给的替换文本"
    # 改写提示词仍是不含编号的 Tier A 口径。
    _assert_no_numbering_scaffold(provider.last_user)

    db.expire_all()
    assert db.get(Generation, gen.id).text == text
    clar = db.get(GenerationSnapshot, clarification_id)
    assert clar.status == "clarified"

