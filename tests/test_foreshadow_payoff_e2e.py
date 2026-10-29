"""伏笔脏回收章的**端到端**验证（2026-10-29）。

覆盖三条真实路径（不是纯函数）：

1. **落库**（`batch_worker._persist_extraction`）—— 编出来的回收章不再进账；
2. **告警**（`judge.check_foreshadow_expiry`）—— `pf=0` 不再报假「超期」；
3. **读取**（`retrieval_v3.slice_for_prompt`）—— 老脏值不再送进切片；
4. **存量清理**（`foreshadow_cleanup.clean_all_foreshadows`）—— 一次性清干净，
   且**合法值一条不动**。

实测口径见 `docs/foreshadow-payoff-chapter-hygiene.md`。
"""
from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.db import Base  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import Generation, KgEvent, KgForeshadow, Project  # noqa: E402

#: 正文里真的写了「第 1 章」（阳性对照素材）。
_TEXT_WITH_CH1 = "他翻开卷宗。（第一章）卷首记着旧案。"
#: 正文里**没有**任何章号提及。
_TEXT_NO_CHAPTER = "他握刀站在城头，风很大，谁也没说话。"


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    session.add(Project(id=1, title="测试"))
    session.add(Generation(id=1, project_id=1, segment_no=1, text=_TEXT_WITH_CH1,
                           chapter_range="第1章"))
    session.add(Generation(id=2, project_id=1, segment_no=2, text=_TEXT_NO_CHAPTER,
                           chapter_range="第2章"))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _persist(session, foreshadows):
    """跑真实的落库路径（`_persist_extraction` 的伏笔那段）。"""
    from app.kg import batch_worker

    batch = [batch_worker._QueueItem(project_id=1, segment_no=1,
                                      text=_TEXT_NO_CHAPTER, chapter=1)]
    result = {"entities": [], "relations": [], "events": [], "event_links": [],
              "entity_knowledge": [], "state_changes": [], "foreshadows": foreshadows}
    batch_worker._persist_extraction(session, 1, result, batch)
    session.commit()


def _rows(session):
    return list(session.query(KgForeshadow).order_by(KgForeshadow.id).all())


# ── 1. 落库：编出来的回收章不���进账 ──────────────────────────────────────
def test_persist_drops_fabricated_payoff(db):
    """模型吐 `pf=50`（无正文证据）⇒ 落库时置空，不写进账。"""
    _persist(db, [{"name": "线索A", "status": "planted", "planted_chapter": 1,
                   "planned_payoff_chapter": 50}])
    row = _rows(db)[0]
    assert row.planned_payoff_chapter is None, row.planned_payoff_chapter


def test_persist_drops_zero_payoff(db):
    _persist(db, [{"name": "线索A", "status": "planted", "planted_chapter": 1,
                   "planned_payoff_chapter": 0}])
    assert _rows(db)[0].planned_payoff_chapter is None


def test_persist_keeps_payoff_with_evidence(db):
    """正文里真的写了「第 1 章」⇒ 这个回收章采信。"""
    from app.kg import batch_worker

    batch = [batch_worker._QueueItem(project_id=1, segment_no=1,
                                      text=_TEXT_WITH_CH1, chapter=1)]
    result = {"entities": [], "relations": [], "events": [], "event_links": [],
              "entity_knowledge": [], "state_changes": [],
              "foreshadows": [{"name": "线索A", "status": "planted",
                               "planted_chapter": None,
                               "planned_payoff_chapter": 1}]}
    batch_worker._persist_extraction(db, 1, result, batch)
    db.commit()
    assert _rows(db)[0].planned_payoff_chapter == 1


def test_persist_reuse_does_not_overwrite_with_bad_value(db):
    """复用行：编出来的值**不覆盖**账上已有的合法计划。"""
    from app.kg import batch_worker

    db.add(KgForeshadow(project_id=1, name="线索A", status="planted",
                        planted_chapter=1, planned_payoff_chapter=1))
    db.commit()
    batch = [batch_worker._QueueItem(project_id=1, segment_no=1,
                                      text=_TEXT_NO_CHAPTER, chapter=1)]
    result = {"entities": [], "relations": [], "events": [], "event_links": [],
              "entity_knowledge": [], "state_changes": [],
              "foreshadows": [{"name": "线索A", "status": "developing",
                               "planted_chapter": 1,
                               "planned_payoff_chapter": 50}]}
    batch_worker._persist_extraction(db, 1, result, batch)
    db.commit()
    row = _rows(db)[0]
    assert row.planned_payoff_chapter == 1, row.planned_payoff_chapter
    assert row.status == "developing"


# ── 2. 告警：pf=0 不再报假「超期」 ────────────────────────────────────────
def test_judge_no_false_expiry_for_zero_payoff(db):
    """`pf=0` 比任何当前章都小 ⇒ 修前报「已超期 9 章」，纯噪音。"""
    from app.kg.judge import check_foreshadow_expiry

    db.add(KgEvent(project_id=1, sequence=1, chapter=9, segment_no=2,
                   event_type="incident", summary="他在城头站了一夜"))
    db.add(KgForeshadow(project_id=1, name="线索A", status="planted",
                        planted_chapter=1, planned_payoff_chapter=0))
    db.commit()
    findings = check_foreshadow_expiry(db, 1, "", 2, current_chapter=9)
    assert findings == [], [f.message for f in findings]


def test_judge_still_reports_real_overdue(db):
    """真的有证据、且确实过了计划章 ⇒ 照报不误（别把真告警也杀了）。"""
    from app.kg.judge import check_foreshadow_expiry

    db.add(KgEvent(project_id=1, sequence=1, chapter=9, segment_no=2,
                   event_type="incident", summary="他在城头站了一夜"))
    db.add(KgForeshadow(project_id=1, name="线索A", status="planted",
                        planted_chapter=None, planned_payoff_chapter=1))
    db.commit()
    findings = check_foreshadow_expiry(db, 1, "", 2, current_chapter=9)
    assert len(findings) == 1, [f.message for f in findings]
    assert "计划第 1 章回收" in findings[0].message


# ── 3. 读取：老脏值不再进切片 ────────────────────────────────────────────
def test_slice_drops_legacy_dirty_payoff(db):
    """老账本里的脏值，**不必等清理**就不该出现在切片里。"""
    from app.kg.retrieval_v3 import slice_for_prompt

    db.add(KgForeshadow(project_id=1, name="线索A", status="planted",
                        planted_chapter=1, planned_payoff_chapter=50))
    db.add(KgForeshadow(project_id=1, name="线索B", status="planted",
                        planted_chapter=1, planned_payoff_chapter=0))
    db.commit()
    sl = slice_for_prompt(db, 1, 1)
    payoffs = {f["name"]: f["planned_payoff_chapter"] for f in sl["active_foreshadows"]}
    assert payoffs == {"线索A": None, "线索B": None}, payoffs


# ── 4. 存量清理：清脏值、合法值一条不动 ──────────────────────────────────
def test_cleanup_clears_dirty_and_keeps_valid(db):
    from app.kg.foreshadow_cleanup import clean_all_foreshadows

    db.add(KgForeshadow(project_id=1, name="脏-零", status="planted",
                        planted_chapter=1, planned_payoff_chapter=0))
    db.add(KgForeshadow(project_id=1, name="脏-五十", status="developing",
                        planted_chapter=3, planned_payoff_chapter=50))
    db.add(KgForeshadow(project_id=1, name="合法-有证据", status="planted",
                        planted_chapter=None, planned_payoff_chapter=1))
    db.add(KgForeshadow(project_id=1, name="合法-同章回收", status="paid_off",
                        planted_chapter=1, planned_payoff_chapter=1))
    db.add(KgForeshadow(project_id=1, name="合法-无回收章", status="planted",
                        planted_chapter=2, planned_payoff_chapter=None))
    db.commit()

    st = clean_all_foreshadows(db)
    assert st["cleaned"] == 2, st
    assert st["kept"] == 2, st

    vals = {r.name: r.planned_payoff_chapter for r in _rows(db)}
    assert vals["脏-零"] is None
    assert vals["脏-五十"] is None
    assert vals["合法-有证据"] == 1
    assert vals["合法-同章回收"] == 1
    assert vals["合法-无回收章"] is None


def test_cleanup_clears_zero_on_dropped_row(db):
    """已丢弃的线索 `pf=0` ⇒ 清（第 0 章不存在）。

    ⚠️ `dropped`（已丢弃、从未回收）与 `paid_off`（已回收）语义不同：
    只有 `paid_off` 有「同章回收」豁免。
    """
    from app.kg.foreshadow_cleanup import clean_all_foreshadows

    db.add(KgForeshadow(project_id=1, name="已丢弃-零", status="dropped",
                        planted_chapter=4, planned_payoff_chapter=0))
    db.commit()
    st = clean_all_foreshadows(db)
    assert st["cleaned"] == 1, st
    assert _rows(db)[0].planned_payoff_chapter is None


def test_cleanup_is_idempotent(db):
    """清理跑两遍，第二次不该再改任何东西（启动时每次都跑）。"""
    from app.kg.foreshadow_cleanup import clean_all_foreshadows

    db.add(KgForeshadow(project_id=1, name="脏-零", status="planted",
                        planted_chapter=1, planned_payoff_chapter=0))
    db.commit()
    first = clean_all_foreshadows(db)
    second = clean_all_foreshadows(db)
    assert first["cleaned"] == 1, first
    assert second["cleaned"] == 0, second


def test_cleanup_leaves_bible_foreshadows_alone(db):
    """⚠️ 用户在**剧情圣经**里手写的伏笔一个字都不许动。

    清理只碰 `kg_foreshadows`（机器抽取的表）——`plot_bible.foreshadowing`
    是另一条路（用户写的，是权威）。
    """
    from app.db import SessionLocal  # noqa: F401 —— 确认 db 模块可导入
    from app.kg.foreshadow_cleanup import clean_all_foreshadows

    project = db.query(Project).filter(Project.id == 1).one()
    project.plot_bible = {"foreshadowing": ["用户自己写的伏笔", {"name": "另一条", "status": "planted"}]}
    db.commit()

    clean_all_foreshadows(db)
    db.refresh(project)
    assert project.plot_bible["foreshadowing"] == [
        "用户自己写的伏笔", {"name": "另一条", "status": "planted"},
    ]
