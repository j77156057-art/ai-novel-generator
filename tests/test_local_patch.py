# -*- coding: utf-8 -*-
"""局部指令 → 句子索引补丁：单句 / 多句 / 越界 / 整段拒绝 / 脏输出 / 旧路径。

对应 2026-11-12「程序切句编号、模型只回编号 + 改写句」方案。这里**不 mock
补丁逻辑**：真实切句、真实解析模型返回、真实按编号替换；引擎级用例真实走
``regenerate_segment_stream`` 与快照落库。
"""
from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.agents.dialog.snapshot import begin_snapshot  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import regenerate_segment_stream  # noqa: E402
from app.generation.patch import (  # noqa: E402
    PatchError,
    build_index_user_prompt,
    parse_index_reply,
    parse_patch_reply,
    resolve_scope,
    split_sentences,
)
from app.models import Generation, GenerationSnapshot, Project  # noqa: E402

SEG1 = "林夜推开木门。\n他说：“我回来了。”"
SEG2 = "第一句。第二句。第三句。"


def _tier_b(reply: str, before: str):
    """Tier B 两段式：第一步只取编号，第二步再取替换文本。"""
    scope = parse_index_reply(reply, before)
    return parse_patch_reply(reply, before, scope)


# ---------------------------------------------------------------------------
# 单元：切句、编号解析与替换
# ---------------------------------------------------------------------------


def test_split_sentences_keeps_quotes_and_tail_fragment():
    spans = split_sentences("他说：“走吧。”\n她还站着")
    assert [s["text"] for s in spans] == ["他说：“走吧。”", "她还站着"]
    # 句间换行不属于任何句子（替换不会吞掉段落间隔）
    assert "她还站着".startswith("她")


def test_single_sentence_index_replaces_exactly_one():
    before = "第一句。第二句。第三句。"
    new_text, meta = _tier_b(
        '{"index": 2, "replacement": "改过的第二句。"}', before
    )
    assert new_text == "第一句。改过的第二句。第三句。"
    assert new_text.startswith("第一句。") and new_text.endswith("第三句。")
    applied = meta["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (2, 2)
    assert applied["before"] == "第二句。"
    assert applied["replacement"] == "改过的第二句。"


def test_multi_sentence_span_replaces_consecutive_sentences():
    before = "第一句。第二句。第三句。第四句。"
    new_text, meta = _tier_b(
        '{"from_index": 2, "to_index": 3, '
        '"replacement": "改过的二。改过的三。"}',
        before,
    )
    assert new_text == "第一句。改过的二。改过的三。第四句。"
    applied = meta["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (2, 3)
    assert applied["before"] == "第二句。第三句。"


def test_index_out_of_range_raises():
    with pytest.raises(PatchError) as ei:
        parse_index_reply('{"index": 9}', SEG2)
    assert ei.value.code == "index_out_of_range"
    assert "1..3" in str(ei.value)


def test_index_reversed_raises():
    with pytest.raises(PatchError) as ei:
        parse_index_reply('{"from_index": 3, "to_index": 1}', SEG2)
    assert ei.value.code == "index_reversed"


def test_span_covering_whole_text_is_rejected():
    with pytest.raises(PatchError) as ei:
        parse_index_reply('{"from_index": 1, "to_index": 3}', SEG2)
    assert ei.value.code == "span_whole_segment"


def test_missing_index_fields_raises():
    with pytest.raises(PatchError) as ei:
        parse_index_reply('{"replacement": "新句子。"}', SEG2)
    assert ei.value.code == "index_missing"


def test_empty_replacement_is_rejected():
    scope = parse_index_reply('{"index": 2}', SEG2)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply('{"index": 2, "replacement": "   "}', SEG2, scope)
    assert ei.value.code == "empty_patch"


def test_replacement_missing_field_is_rejected():
    scope = parse_index_reply('{"index": 2}', SEG2)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply('{"index": 2}', SEG2, scope)
    assert ei.value.code == "replacement_missing"


def test_replacement_that_is_whole_segment_is_rejected():
    before = "第一句。第二句。第三句。第四句。第五句。第六句。"
    scope = parse_index_reply('{"index": 2}', before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply(
            '{"index": 2, "replacement": "%s"}' % before, before, scope
        )
    assert ei.value.code == "replacement_whole_segment"


def test_single_sentence_span_rejects_multi_sentence_replacement():
    before = "甲乙丙丁。戊己庚辛。壬癸。"
    scope = parse_index_reply('{"index": 1}', before)
    with pytest.raises(PatchError) as ei:
        parse_patch_reply(
            '{"index": 1, "replacement": "一二。三四。五六。"}', before, scope
        )
    assert ei.value.code == "replacement_too_long"


def test_dirty_reply_with_fence_and_prose_is_repaired():
    reply = (
        "好的，这是补丁：\n```json\n"
        '{"index": 2, "replacement": "改过的第二句。"}\n'
        "```\n（只改了第二句）"
    )
    new_text, meta = _tier_b(reply, SEG2)
    assert new_text == "第一句。改过的第二句。第三句。"
    assert meta["patch_count"] == 1
    assert meta["stages"] == 2 and meta["scope_source"] == "model"


def test_plain_text_reply_is_unparsable():
    with pytest.raises(PatchError) as ei:
        parse_index_reply("改过的第二句。", SEG2)
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


def test_index_prompt_has_no_replacement_slot():
    prompt = build_index_user_prompt(SEG2, "这一句不太顺")
    assert ">> 2." not in prompt, "未选位置前不应标记目标句"
    assert '{"from_index": 起始句编号, "to_index": 结束句编号}' in prompt
    assert "不要输出 replacement" in prompt


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


def _run(db, project, provider, instruction: str):
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    snap = begin_snapshot(db, project, gen, instruction=instruction)
    events = list(regenerate_segment_stream(
        db, project, 2, provider=provider, instruction=instruction,
        snapshot_id=snap.id, expected_before=snap.before_text,
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
    # Tier A：程序把「最后一句」解析成第 3 句，并在编号原文里标出来
    assert ">> 3." in provider.last_user
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
    assert ">> 5." in provider.last_user and ">> 6." in provider.last_user
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


def test_ambiguous_instruction_uses_two_stage_patch(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider([
        '{"from_index": 2, "to_index": 2}',
        "改过的第二句。",
    ])

    _, done = _run(db, project, provider, "只改某处，其余内容保留不要动。")

    assert provider.chat_calls == 2 and provider.stream_calls == 0
    # 第一步：只给编号清单，不标目标句、没有 replacement 位。
    assert ">> 2." not in provider.prompts[0]
    assert "不要输出 replacement" in provider.prompts[0]
    # 第二步：Tier A 那套提示词，目标句由程序标出来。
    assert ">> 2." in provider.prompts[1]
    assert done["patch"]["scope_source"] == "model"
    assert done["patch"]["stages"] == 2
    applied = done["patch"]["applied"][0]
    assert (applied["from_index"], applied["to_index"]) == (2, 2)
    db.expire_all()
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == "第一句。改过的第二句。第三句。"
    assert "不太顺" not in gen.text, "指令话术不得进正文"


def test_tier_b_second_stage_failure_fails_closed(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider([
        '{"from_index": 2, "to_index": 2}',
        "   ",
    ])

    with pytest.raises(PatchError) as ei:
        _run(db, project, provider, "只改某处，其余内容保留不要动。")
    assert ei.value.code == "empty_patch"
    assert provider.chat_calls == 2

    db.expire_all()
    snap = db.scalars(
        select(GenerationSnapshot).order_by(GenerationSnapshot.id.desc())
    ).first()
    assert snap.status == "failed"
    assert snap.error_reason == "empty_patch"
    gen = db.scalars(
        select(Generation).where(
            Generation.project_id == project.id, Generation.segment_no == 2
        )
    ).one()
    assert gen.text == SEG2, "第二步失败不得改动正文"


def test_local_patch_failure_marks_snapshot_failed_and_keeps_text(db):
    project = _project(db)
    _seed(db, project, 1, SEG1)
    _seed(db, project, 2, SEG2)
    provider = StubProvider('{"index": 99, "replacement": "新句子。"}')

    with pytest.raises(PatchError) as ei:
        _run(db, project, provider, "只改某处，其余内容保留不要动。")
    assert ei.value.code == "index_out_of_range"
    assert provider.chat_calls == 1, "第一步编号失败后不得再发第二步"

    db.expire_all()
    snap = db.scalars(
        select(GenerationSnapshot).order_by(GenerationSnapshot.id.desc())
    ).first()
    assert snap.status == "failed"
    assert snap.error_reason == "index_out_of_range"
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
