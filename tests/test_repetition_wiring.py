# -*- coding: utf-8 -*-
"""复读闸门接线测试：四条写入路径都带标记，且不回填历史行。

用内存 SQLite + 可控 provider 桩，不连生产库、不跑真实模型。
真实库只用于守卫抽查（``.tmp_verify/post_guards.py``）。
"""
from __future__ import annotations

import json

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.generation.engine as engine
from app.agents.dialog.rollback import _restore_one
from app.agents.dialog.snapshot import finalize_snapshot, text_hash
from app.generation.repetition import (
    analyze_repetition,
    parse_quality,
    quality_payload,
)
from app.models import Base, Generation, GenerationSnapshot, Project
from fixtures_repetition_segments import SEGMENTS
from fixtures_summary_style import SUMMARY_750

DEGENERATE = SEGMENTS[4]["text"]
NORMAL = SEGMENTS[1]["text"]
SHORT = "他把刀放下，走出了门。"
SHORT_TRUNCATED = "他把刀放下，走出了"


# ───────────────────────────── 夹具 ─────────────────────────────

@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def project(db):
    p = Project(title="测试项目", generation_length=3000)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


class StubProvider:
    """可控 provider 桩：chat/stream_chat 都吐同一段文本。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, system, user, constraints=None, stop_sequences=None):
        return self.text

    def summarize(self, text: str) -> str:
        return "段摘要"

    def stream_chat(self, system, user, constraints=None, stop_sequences=None):
        yield self.text[:40]
        yield self.text[40:]


def _patch_common(monkeypatch, merge_calls: list):
    """把生成链路的外围依赖全部换成桩，只留落库与判据走真实代码。"""
    monkeypatch.setattr(engine, "_require_distill_ready", lambda db, p: None)
    monkeypatch.setattr(engine, "_rollup_summary_tree", lambda db, p, prov: {})
    monkeypatch.setattr(engine, "build_context", lambda db, p, ids=None, **kw: {
        "reminders": {}, "prior_summaries": [], "rag_context": "",
        "prior_blocks": [], "constraints": {},
    })
    from collections import defaultdict
    monkeypatch.setattr(engine, "_prompt_overrides",
                        lambda db, pid: defaultdict(lambda: None))
    monkeypatch.setattr(engine, "current_node_prompt",
                        lambda p, written, prov, db=None, **kw: None)
    monkeypatch.setattr(engine, "_guard_blocks", lambda p: ("", {}, "", {}))
    monkeypatch.setattr(engine, "_style_info", lambda p: {})
    monkeypatch.setattr(engine, "_style_profile", lambda db, p: None)
    monkeypatch.setattr(engine, "_style_narrative", lambda db, p: {})
    monkeypatch.setattr(engine, "_style_source_ids", lambda p: [])
    monkeypatch.setattr(engine, "_should_rewrite", lambda text, forbidden: (False, []))
    monkeypatch.setattr(engine, "_apply_era_guard",
                        lambda text, p, prov, no, reminders: (text, {}))
    monkeypatch.setattr(engine, "_judge_and_advance",
                        lambda db, p, prov, text, written, **kw: {"advanced": False})
    monkeypatch.setattr(engine, "_enqueue_v3_extraction",
                        lambda db, p, no, text, **kw: None)
    monkeypatch.setattr(engine, "_shadow_ingest", lambda p, no, text: None)
    monkeypatch.setattr(engine, "_load_timeline_state_for_branch",
                        lambda db, p, written: None)
    monkeypatch.setattr(engine, "_style_warning", lambda text, profile: None)
    monkeypatch.setattr(engine, "_schedule_async_audit_kg", lambda db, pid: None)

    def _merge(db, project_id, text, segment_no):
        merge_calls.append((project_id, segment_no, text))
        return {"merged": True, "facts": 0}

    monkeypatch.setattr(engine, "_merge_segment_into_graph", _merge)

    import app.generation.branch as branch
    monkeypatch.setattr(branch, "roll_branch",
                        lambda *a, **k: (None, {"rolled": False}))
    import app.generation.guard as guard
    monkeypatch.setattr(guard, "detect_unlisted_characters", lambda text, allowed: [])
    monkeypatch.setattr(guard, "collect_allowed_names",
                        lambda ctx_reminders=None, plot_bible=None: set())


def _patch_rewrite(monkeypatch):
    """P3 额外依赖：提示词组装、提醒、摘要树与 KG 重建。"""
    monkeypatch.setattr(engine, "get_reminders",
                        lambda db, pid, source="generated": {})
    monkeypatch.setattr(engine, "_build_generation_prompts",
                        lambda db, p, **kw: ("sys", "user", 3000))
    monkeypatch.setattr(engine, "_constraints_from", lambda reminders, p: {})
    monkeypatch.setattr(engine, "declared_local", lambda instr: False)
    monkeypatch.setattr(engine, "rebuild_summary_tree",
                        lambda db, p, prov: {"rebuilt": True})

    import app.kg.build as kg_build
    monkeypatch.setattr(kg_build, "rebuild_generated_kg",
                        lambda db, pid: {"rebuilt": True})
    import app.style.consistency as consistency
    monkeypatch.setattr(consistency, "record_segment_check",
                        lambda db, p, gen: None)


def _row(db, segment_no: int) -> Generation:
    return db.scalars(
        select(Generation).where(Generation.segment_no == segment_no)
    ).one()


# ───────────────────────── 判据 / 序列化 ─────────────────────────

def test_parse_quality_null_maps_to_undetected():
    q = parse_quality(None)
    assert q["repetition"]["verdict"] == "undetected"
    assert q["truncated"] is None
    # 坏 JSON 也按未检测，绝不能掉成 ok
    assert parse_quality("{oops")["repetition"]["verdict"] == "undetected"


def test_quality_payload_roundtrip_three_states():
    for verdict in ("ok", "degenerate", "skipped_short"):
        report = analyze_repetition(DEGENERATE if verdict == "degenerate" else NORMAL)
        payload = quality_payload(report)
        payload["repetition"]["verdict"] = verdict  # 只测形状转换
        parsed = parse_quality(json.dumps(payload, ensure_ascii=False))
        assert parsed["repetition"]["verdict"] == verdict


def test_quality_check_flags_and_truncation():
    payload, rep, warnings = engine._quality_check(DEGENERATE)
    assert rep["verdict"] == "degenerate"
    assert payload["truncated"] is True
    assert any("疑似复读" in w for w in warnings)
    assert any("截断" in w for w in warnings)

    payload, rep, warnings = engine._quality_check(NORMAL)
    assert rep["verdict"] == "ok"
    assert warnings == []

    # 短文本不判定退化，但截断标记独立生效（结尾停在半句）
    payload, rep, _ = engine._quality_check(SHORT_TRUNCATED)
    assert rep["verdict"] == "skipped_short"
    assert payload["truncated"] is True

    payload, rep, _ = engine._quality_check(SHORT)
    assert rep["verdict"] == "skipped_short"
    assert payload["truncated"] is False


# ───────────────────────── P1 / P2 ─────────────────────────

def test_p1_writes_quality_and_skips_kg_for_degenerate(db, project, monkeypatch):
    merge_calls = []
    _patch_common(monkeypatch, merge_calls)

    result = engine.generate_next_segment(db, project, provider=StubProvider(DEGENERATE))

    gen = _row(db, result["segment_no"])
    q = parse_quality(gen.quality_json)
    assert q["repetition"]["verdict"] == "degenerate"
    assert q["truncated"] is True
    assert result["repetition"]["verdict"] == "degenerate"
    assert result["warnings"]
    assert merge_calls == []          # 退化段未进 KG
    assert result["kg_update"]["skipped"] == "repetition_degenerate"


def test_p1_writes_ok_and_merges_kg_for_normal(db, project, monkeypatch):
    merge_calls = []
    _patch_common(monkeypatch, merge_calls)

    result = engine.generate_next_segment(db, project, provider=StubProvider(NORMAL))

    gen = _row(db, result["segment_no"])
    q = parse_quality(gen.quality_json)
    assert q["repetition"]["verdict"] == "ok"
    # 1413 / 3000 = 47.1% → 篇幅不足；梗概化不命中。两个字段都随 INSERT 落库。
    assert q["length"]["verdict"] == "short"
    assert q["summary_style"]["verdict"] == "ok"
    assert result["length"]["verdict"] == "short"
    assert result["summary_style"]["verdict"] == "ok"
    assert len(merge_calls) == 1
    assert result["kg_update"]["merged"] is True


def test_p1_writes_summary_style_mark_for_750_skeleton(db, project, monkeypatch):
    """750 字罗列式段：P1 落库时必须同时带「篇幅不足」和「梗概化」。"""
    _patch_common(monkeypatch, [])

    result = engine.generate_next_segment(
        db, project, provider=StubProvider(SUMMARY_750["text"]))

    q = parse_quality(_row(db, result["segment_no"]).quality_json)
    assert q["length"]["verdict"] == "short"
    assert q["summary_style"]["verdict"] == "summary_style"
    assert "meet_verb_object" in q["summary_style"]["signals"]
    assert result["summary_style"]["verdict"] == "summary_style"


def test_p2_stream_writes_quality_and_skips_kg(db, project, monkeypatch):
    merge_calls = []
    _patch_common(monkeypatch, merge_calls)

    events = list(engine.generate_next_segment_stream(
        db, project, provider=StubProvider(DEGENERATE)))
    done = [e for e in events if e["type"] == "done"][-1]

    gen = _row(db, done["segment_no"])
    q = parse_quality(gen.quality_json)
    assert q["repetition"]["verdict"] == "degenerate"
    assert q["summary_style"]["verdict"] == "ok"
    assert done["repetition"]["verdict"] == "degenerate"
    assert done["length"]["verdict"] == "short"
    assert done["summary_style"]["verdict"] == "ok"
    assert done["warnings"]
    assert merge_calls == []


# ───────────────────────── P3 两条子路径 ─────────────────────────

def _seed_segment(db, project, text: str) -> Generation:
    gen = Generation(
        project_id=project.id, segment_no=1, chapter_range="段1",
        text=text, summary={"text": "旧摘要"},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def test_p3_old_path_writes_quality(db, project, monkeypatch):
    _seed_segment(db, project, NORMAL)
    _patch_common(monkeypatch, [])
    _patch_rewrite(monkeypatch)

    events = list(engine.regenerate_segment_stream(
        db, project, 1, provider=StubProvider(DEGENERATE)))
    done = [e for e in events if e["type"] == "done"][-1]

    gen = _row(db, 1)
    assert gen.text == DEGENERATE
    q = parse_quality(gen.quality_json)
    assert q["repetition"]["verdict"] == "degenerate"
    # 改写路径不按整段预算报「篇幅不足」，跌幅归 detect_length_drop。
    assert q["length"]["verdict"] == "not_applicable"
    assert q["summary_style"]["verdict"] == "ok"
    assert done["repetition"]["verdict"] == "degenerate"
    assert done["length"]["verdict"] == "not_applicable"


def test_p3_snapshot_path_writes_quality_in_same_update(db, project, monkeypatch):
    gen = _seed_segment(db, project, NORMAL)
    snap = GenerationSnapshot(
        project_id=project.id, generation_id=gen.id, segment_no=1,
        batch_id="b-1", operation="rewrite", status="pending",
        before_text=NORMAL, before_summary={"text": "旧摘要"},
        before_hash=text_hash(NORMAL),
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)

    _patch_common(monkeypatch, [])
    _patch_rewrite(monkeypatch)

    events = list(engine.regenerate_segment_stream(
        db, project, 1, provider=StubProvider(DEGENERATE),
        snapshot_id=snap.id, expected_before=NORMAL))
    done = [e for e in events if e["type"] == "done"][-1]

    db.expire_all()
    gen = _row(db, 1)
    snap = db.get(GenerationSnapshot, snap.id)
    assert gen.text == DEGENERATE
    q = parse_quality(gen.quality_json)
    assert q["repetition"]["verdict"] == "degenerate"
    assert q["length"]["verdict"] == "not_applicable"
    assert q["summary_style"]["verdict"] == "ok"
    assert snap.status == "applied"
    assert done["repetition"]["verdict"] == "degenerate"


def test_finalize_snapshot_sets_quality_json(db, project):
    gen = _seed_segment(db, project, NORMAL)
    snap = GenerationSnapshot(
        project_id=project.id, generation_id=gen.id, segment_no=1,
        batch_id="b-2", operation="rewrite", status="pending",
        before_text=NORMAL, before_summary={"text": "旧摘要"},
        before_hash=text_hash(NORMAL),
    )
    db.add(snap)
    db.commit()

    payload = quality_payload(analyze_repetition(DEGENERATE))
    finalize_snapshot(
        db, snapshot_id=snap.id, generation_id=gen.id, segment_no=1,
        expected_before=NORMAL, new_text=DEGENERATE,
        new_summary={"text": "新"}, quality_json=json.dumps(payload, ensure_ascii=False),
    )
    db.commit()

    db.expire_all()
    assert parse_quality(_row(db, 1).quality_json)["repetition"]["verdict"] == "degenerate"


# ───────────────────────── P4 回滚 ─────────────────────────

def test_rollback_restores_text_but_never_writes_quality(db, project, monkeypatch):
    gen = _seed_segment(db, project, DEGENERATE)
    gen.quality_json = json.dumps(
        quality_payload(analyze_repetition(NORMAL)), ensure_ascii=False)
    db.commit()

    snap = GenerationSnapshot(
        project_id=project.id, generation_id=gen.id, segment_no=1,
        batch_id="b-3", operation="rewrite", status="applied",
        before_text=NORMAL, before_summary={"text": "回滚前摘要"},
        before_hash=text_hash(NORMAL), after_text=DEGENERATE,
        after_summary={"text": "重写后摘要"},
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)

    before_quality = gen.quality_json
    _restore_one(
        db, snap=snap, gen=gen, current_text=DEGENERATE,
        rollback_batch_id="rb-1",
        now=datetime.now(timezone.utc).replace(tzinfo=None),
        current_bible_hash=None,
    )
    db.commit()

    db.expire_all()
    gen = _row(db, 1)
    assert gen.text == NORMAL
    # 回滚只恢复正文/摘要，不判定、不动标记
    assert gen.quality_json == before_quality


# ───────────────────── 同事务：失败一起回退 ─────────────────────

def test_commit_failure_rolls_back_text_and_quality_together(db, project):
    def _boom(session):
        raise RuntimeError("commit failed")

    event.listen(db, "before_commit", _boom)
    try:
        with pytest.raises(RuntimeError):
            engine._commit_segment(
                db, project, DEGENERATE, "摘要",
                quality=quality_payload(analyze_repetition(DEGENERATE)),
            )
    finally:
        event.remove(db, "before_commit", _boom)
        db.rollback()

    rows = db.scalars(select(Generation)).all()
    assert rows == []          # 正文与标记一起没落库，没有半态

