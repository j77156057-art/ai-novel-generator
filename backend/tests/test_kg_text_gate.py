# -*- coding: utf-8 -*-
"""写侧文本归属门（2026-11-19）验收。

锁死四件事：

1. **正文不逐字一致 → 隔离**：该段应有的 mention / event / relation 等行不落业务表，
   整行转存 ``kg_ingest_quarantine``（可查询、可统计、可回放）。
2. **正文逐字一致 → 行为不变**：正常数据一条不少，隔离区为空。
3. **同批污染**：批里有一段被确证不是本项目正文时，同批无法比对的段按
   ``batch_tainted`` 一并隔离；没 generation 的整批（历史/测试调用）仍然放行。
4. **门不抛异常、不阻断**：降级 alias 路径同样过门，且 0 新增时也提交隔离留痕。

全程内存库，真实库一行不动。
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.models  # noqa: F401,E402
from app.db import Base  # noqa: E402
from app.kg.batch_worker import (  # noqa: E402
    NO_GENERATION_ALERT_THRESHOLD, _QueueItem, _annotate_no_generation,
    _index_mentions_only, _persist_extraction,
    text_gate_no_generation_summary,
)
from app.models import (  # noqa: E402
    AgentRun, Generation, KgEntity, KgEvent, KgIngestQuarantine, KgMention,
    KgRelation, Project,
)


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, future=True)()


def _project(db) -> Project:
    proj = Project(title="文本归属门测试")
    db.add(proj)
    db.commit()
    return proj


def _gen(db, pid: int, seg: int, text: str) -> Generation:
    g = Generation(project_id=pid, segment_no=seg, text=text)
    db.add(g)
    db.commit()
    return g


def _ent(name: str, kind: str = "character") -> dict:
    return {"kind": kind, "canonical_name": name, "aliases": [], "attrs": {}}


def _result(*, entities, relations=(), events=(), states=(), knows=(),
            foreshadows=()):
    return {
        "entities": list(entities),
        "relations": list(relations),
        "events": list(events),
        "event_links": [],
        "entity_knowledge": list(knows),
        "state_changes": list(states),
        "foreshadows": list(foreshadows),
    }


def _count(db, model) -> int:
    return len(db.scalars(select(model)).all())


def _q(db) -> list[KgIngestQuarantine]:
    return db.scalars(select(KgIngestQuarantine)).all()


# ---------------------------------------------------------------------------
# 1. 整批别书正文 → 全部隔离，业务表零行
# ---------------------------------------------------------------------------
def test_full_mismatch_quarantines_every_artifact():
    db = _db()
    proj = _project(db)
    _gen(db, proj.id, 1, "小杨站在废弃的工厂前，记忆的碎片像纸屑。")
    batch = [_QueueItem(project_id=proj.id, segment_no=1,
                        text="许七安在监牢中醒来，发现自己穿越到了古代。", run_id=77)]
    result = _result(
        entities=[_ent("许七安"), _ent("佛门", "faction")],
        relations=[{"from": "许七安", "to": "佛门", "rel_type": "belongs_to",
                    "strength": 0.6}],
        events=[{"sequence": 1, "segment": 1, "event_type": "incident",
                 "summary": "许七安醒来", "subject": "许七安", "verb": "醒来",
                 "object": "", "location": ""}],
        states=[{"character": "许七安", "state_type": "location",
                 "new_value": "监牢"}],
        knows=[{"character": "许七安", "knows": "佛门",
                "known_kind": "entity", "learned_at_sequence": 1}],
        foreshadows=[{"name": "税银案", "status": "planted",
                      "planted_chapter": 1, "planned_payoff_chapter": 10}],
    )

    stats = _persist_extraction(db, proj.id, result, batch)

    assert _count(db, KgEntity) == 0, "整批别书正文不该建实体"
    assert _count(db, KgEvent) == 0
    assert _count(db, KgRelation) == 0
    assert _count(db, KgMention) == 0

    rows = _q(db)
    kinds = {r.kind for r in rows}
    assert {"entity", "event", "relation", "state_change",
            "entity_knowledge", "foreshadow"} <= kinds
    assert {r.reason for r in rows} == {"text_mismatch"}
    assert {r.run_id for r in rows} == {77}
    assert stats.text_gate["blocked_segments"] == {"1": "text_mismatch"}
    assert stats.text_gate["quarantined"]["event"] == 1
    assert stats.text_gate["quarantined"]["relation"] == 1
    # 门不是异常分支：函数正常返回，stats 仍可用
    assert stats.entities_upserted == 0


# ---------------------------------------------------------------------------
# 2. 正文逐字一致 → 正常落库，隔离区为空
# ---------------------------------------------------------------------------
def test_identical_text_passes_without_quarantine():
    db = _db()
    proj = _project(db)
    text = "李浩在办公室里改策划书，老李打来电话。"
    _gen(db, proj.id, 1, text)
    batch = [_QueueItem(project_id=proj.id, segment_no=1, text=text)]
    result = _result(
        entities=[_ent("李浩"), _ent("老李")],
        relations=[{"from": "李浩", "to": "老李", "rel_type": "ally",
                    "strength": 0.5}],
        events=[{"sequence": 1, "segment": 1, "event_type": "incident",
                 "summary": "李浩改策划书", "subject": "李浩", "verb": "改",
                 "object": "", "location": ""}],
    )

    stats = _persist_extraction(db, proj.id, result, batch)

    assert _count(db, KgEntity) == 2
    assert _count(db, KgEvent) == 1
    assert _count(db, KgRelation) == 1
    assert _count(db, KgMention) >= 1
    assert _q(db) == []
    assert stats.text_gate["blocked_segments"] == {}


# ---------------------------------------------------------------------------
# 3. 同批污染：确证段 text_mismatch，无法比对段 batch_tainted
# ---------------------------------------------------------------------------
def test_partial_batch_taints_unverifiable_segments():
    db = _db()
    proj = _project(db)
    _gen(db, proj.id, 1, "李浩在办公室工作。")
    _gen(db, proj.id, 2, "李浩回到家。")
    batch = [
        _QueueItem(project_id=proj.id, segment_no=1, text="李浩在办公室工作。"),
        _QueueItem(project_id=proj.id, segment_no=2, text="许七安在监牢中醒来。"),
        _QueueItem(project_id=proj.id, segment_no=3, text="许七安想起身世。"),
    ]
    result = _result(
        entities=[_ent("李浩"), _ent("许七安")],
        relations=[{"from": "李浩", "to": "许七安", "rel_type": "ally",
                    "strength": 0.5}],
        events=[
            {"sequence": 1, "segment": 1, "event_type": "incident",
             "summary": "李浩工作", "subject": "李浩", "verb": "工作"},
            {"sequence": 2, "segment": 2, "event_type": "incident",
             "summary": "许七安醒来", "subject": "许七安", "verb": "醒来"},
            {"sequence": 3, "segment": 3, "event_type": "incident",
             "summary": "许七安回忆", "subject": "许七安", "verb": "回忆"},
        ],
    )

    stats = _persist_extraction(db, proj.id, result, batch)

    # 段 1 正文一致 → 事件/提及照常落；段 2/3 → 隔离
    assert _count(db, KgEvent) == 1
    assert db.scalar(select(KgEvent)).segment_no == 1
    assert _count(db, KgRelation) == 0, "关系无段归属，批里有污染就不落"
    mentions = db.scalars(select(KgMention)).all()
    assert mentions and {m.segment_no for m in mentions} == {1}

    assert stats.text_gate["blocked_segments"] == {
        "2": "text_mismatch", "3": "batch_tainted"}
    rows = _q(db)
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r.kind, set()).add(r.reason)
    assert by_kind["event"] == {"text_mismatch", "batch_tainted"}
    assert by_kind["relation"] == {"text_mismatch"}
    assert by_kind["mention"] == {"text_mismatch", "batch_tainted"}


# ---------------------------------------------------------------------------
# 4. 全批都没有 generation → 放行（无法比对 ≠ 不一致）
# ---------------------------------------------------------------------------
def test_no_generation_still_passes():
    db = _db()
    proj = _project(db)
    batch = [_QueueItem(project_id=proj.id, segment_no=1,
                        text="林悠然推开门，顾辰逸在等她。")]
    result = _result(
        entities=[_ent("林悠然"), _ent("顾辰逸")],
        relations=[{"from": "林悠然", "to": "顾辰逸",
                    "rel_type": "romantic", "strength": 0.6}],
        events=[{"sequence": 1, "segment": 1, "event_type": "incident",
                 "summary": "林悠然推开门", "subject": "林悠然", "verb": "推开门"}],
    )

    stats = _persist_extraction(db, proj.id, result, batch)

    assert _count(db, KgEntity) == 2
    assert _count(db, KgEvent) == 1
    assert _count(db, KgRelation) == 1
    assert _q(db) == []
    assert stats.text_gate["blocked_segments"] == {}
    # 放行但留痕：段号/段清单可统计，供累计告警
    assert stats.text_gate["no_generation"] == {"count": 1, "segments": [1]}


# ---------------------------------------------------------------------------
# 5. 降级 alias 路径同样过门，且 0 新增也提交隔离留痕
# ---------------------------------------------------------------------------
def test_alias_only_path_gates_and_commits_trace():
    db = _db()
    proj = _project(db)
    _gen(db, proj.id, 1, "小杨站在废弃的工厂前。")
    # 库里已有实体：许七安（别书人名）
    db.add(KgEntity(project_id=proj.id, kind="character",
                    canonical_name="许七安", aliases=[], novel_id=None))
    db.commit()

    batch = [_QueueItem(project_id=proj.id, segment_no=1,
                        text="许七安在监牢中醒来。", run_id=99)]
    gate: dict = {}
    added = _index_mentions_only(db, proj.id, batch, gate_out=gate)

    assert added == 0
    assert _count(db, KgMention) == 0
    rows = _q(db)
    assert len(rows) == 1
    assert rows[0].kind == "mention"
    assert rows[0].reason == "text_mismatch"
    assert rows[0].run_id == 99
    assert rows[0].detail["phase"] == "alias_only"
    assert gate["blocked_segments"] == {"1": "text_mismatch"}


# ---------------------------------------------------------------------------
# 6. no_generation 累计：一批只算一次，超阈值才告警
# ---------------------------------------------------------------------------
def test_no_generation_cumulative_and_threshold():
    db = _db()
    proj = _project(db)

    def _run(seg: int, hit_seg: int) -> AgentRun:
        """模拟一行历史批抽（同一批 5 行 note 内容相同，批级 stats）。"""
        row = AgentRun(project_id=proj.id, role="kg_batch_extract",
                       status="success", request_hash=f"{seg}-{hit_seg}",
                       note=json.dumps({
                           "segment_no": seg,
                           "batch_stats": {"text_gate": {
                               "no_generation": {"count": 1,
                                                 "segments": [hit_seg]}}}},
                           ensure_ascii=False))
        db.add(row)
        db.commit()
        return row

    # 历史 3 段；每批另带一行未命中的段（9），验证不会重复计数
    for hit in (1, 2, 3):
        _run(hit, hit)
        _run(9, hit)
    # 本批：段 4/5 缺 generation，行已在库里 → 只能算一次（排除同批行）
    cur = {4: _run(4, 4), 5: _run(5, 5)}

    gate = {"blocked_segments": {}, "quarantined": {},
            "no_generation": {"count": 2, "segments": [4, 5]}}
    alert = _annotate_no_generation(db, proj.id, gate, cur)
    assert gate["no_generation"]["cumulative_count"] == 5
    assert gate["no_generation"]["cumulative_segments"] == [1, 2, 3, 4, 5]
    assert alert == {}, "累计 5 未超过阈值，不该告警"

    # 再来一段 → 累计 6 > 阈值 5 → 告警
    _run(7, 7)
    gate2 = {"blocked_segments": {}, "quarantined": {},
             "no_generation": {"count": 1, "segments": [8]}}
    alert2 = _annotate_no_generation(db, proj.id, gate2, {})
    assert gate2["no_generation"]["cumulative_count"] == 7
    assert alert2["level"] == "error"
    assert alert2["kind"] == "no_generation"
    assert alert2["threshold"] == NO_GENERATION_ALERT_THRESHOLD
    assert alert2["segments"] == [1, 2, 3, 4, 5, 7, 8]

    summary = text_gate_no_generation_summary(db, proj.id)
    assert summary["threshold"] == NO_GENERATION_ALERT_THRESHOLD
    assert summary["alert_projects"] == [proj.id]
    assert summary["projects"][0]["cumulative_count"] == 6
    assert summary["projects"][0]["segments"] == [1, 2, 3, 4, 5, 7]
    assert summary["projects"][0]["alert"] is True
