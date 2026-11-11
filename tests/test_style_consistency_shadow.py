# -*- coding: utf-8 -*-
"""文风一致性校验 · 第一阶段（L1 影子记录）的护栏测试。

对应 ``.trae/documents/style-consistency-check-design.md`` 与本次落地约束：

  T1 算不出的指标必须是 None + 原因，不能填 0（短文本 / 单句）
  T2 字数不足 1200 时 judgeable=False，指标仍记录
  T3 写入的记录 verdict 只能是 'shadow'，且不动 generation.text
  T4 基线带 sample_count 与「样本量不足」原因，样本集不变时复用同一版本
  T5 校验器自身报错时返回 None、不抛异常（绝不能拖垮生成）
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.engine import generate_next_segment  # noqa: E402
from app.models import Generation, Project, StyleBaseline, StyleCheck  # noqa: E402
from app.style.consistency import measure, record_segment_check  # noqa: E402

PARA = (
    "雨下了一整夜，巷口的灯忽明忽暗。他站在屋檐下，手里攥着那封没有署名的信。"
    "“你终于来了。”她说。他没有回答，只是把信递了过去。"
    "她低头看了很久，指尖在纸边停住，像在确认某个不愿意被确认的字。"
)


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
    proj = Project(title="文风影子测试", target_length=5000, mode="quick",
                   status="created", reference_novel_ids=[])
    db.add(proj)
    db.commit()
    db.refresh(proj)
    return proj


def _gen(db, project, segment_no: int, text: str) -> Generation:
    gen = Generation(project_id=project.id, segment_no=segment_no, text=text,
                     summary={"text": f"段{segment_no}摘要"})
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _long_text(repeat: int = 20) -> str:
    return "\n".join(PARA for _ in range(repeat))


# --------------------------------------------------------------------------- #
# T1 / T2：指标可算性
# --------------------------------------------------------------------------- #

def test_single_sentence_cv_is_none_not_zero():
    m = measure("他推开门。")
    assert m["core"]["句长CV"] is None
    assert "句长CV" in m["missing"]
    # 密度类指标在同样文本上是真 0，应保留为 0（不是"算不出"）
    assert m["core"]["逗号密度"] == 0.0


def test_empty_text_has_no_fabricated_values():
    m = measure("   \n  ")
    assert m["chars"] == 0
    assert all(v is None for v in m["core"].values())
    assert m["missing"]["_check"]


def test_short_text_not_judgeable_but_metrics_recorded():
    m = measure(PARA)
    assert m["judgeable"] is False
    assert m["core"]["句长均值"] is not None
    assert "_check" in m["missing"]


def test_long_text_judgeable():
    m = measure(_long_text())
    assert m["chars"] >= 1200
    assert m["judgeable"] is True
    assert all(v is not None for v in m["core"].values())


# --------------------------------------------------------------------------- #
# T3 / T4：落库
# --------------------------------------------------------------------------- #

def test_record_shadow_check(db):
    proj = _project(db)
    _gen(db, proj, 1, _long_text())
    gen = _gen(db, proj, 2, _long_text())
    before = gen.text

    cid = record_segment_check(db, proj, gen)
    assert cid is not None

    row = db.get(StyleCheck, cid)
    assert row.verdict == "shadow"
    assert row.verdict not in ("pass", "warn", "fail")
    assert row.generation_id == gen.id
    assert row.agent_run_id is None
    assert row.baseline_version == 1
    assert row.metric_json["judgeable"] is True

    db.refresh(gen)
    assert gen.text == before, "影子记录不得改动正文"

    base = db.get(StyleBaseline, row.baseline_id)
    assert base.source == "self"
    assert base.sample_count == 1
    assert base.insufficient_reason, "样本不足必须写在基线行上"


def test_baseline_reused_then_versioned(db):
    proj = _project(db)
    _gen(db, proj, 1, _long_text())
    gen2 = _gen(db, proj, 2, _long_text(10))

    first = record_segment_check(db, proj, gen2)
    second = record_segment_check(db, proj, gen2)
    b1 = db.get(StyleCheck, first)
    b2 = db.get(StyleCheck, second)
    assert b1.baseline_id == b2.baseline_id, "样本集没变不应升版本"

    # 新增一段后样本集变化 → 升版本（旧基线保留，可追溯）
    _gen(db, proj, 3, _long_text(10))
    third = record_segment_check(db, proj, gen2)
    b3 = db.get(StyleCheck, third)
    assert b3.baseline_version == b1.baseline_version + 1
    assert len(list(db.scalars(select(StyleBaseline)).all())) == 2


def test_no_baseline_when_project_has_no_text(db):
    proj = _project(db)
    gen = _gen(db, proj, 1, _long_text())
    assert record_segment_check(db, proj, gen) is None


# --------------------------------------------------------------------------- #
# T5：失败纪律
# --------------------------------------------------------------------------- #

class _BrokenSession:
    def scalars(self, *a, **k):
        raise RuntimeError("schema missing")

    def add(self, *a, **k):
        raise RuntimeError("schema missing")

    def commit(self):
        raise RuntimeError("schema missing")

    def rollback(self):
        pass


def test_failure_is_swallowed():
    gen = Generation(project_id=999, segment_no=1, text=_long_text(),
                     summary={"text": "x"})
    gen.id = 1
    proj = Project(id=999, title="坏库", target_length=1000, mode="quick",
                   status="created", reference_novel_ids=[])
    assert record_segment_check(_BrokenSession(), proj, gen) is None


# --------------------------------------------------------------------------- #
# T6：生成链路真的接上了（且不阻断）
# --------------------------------------------------------------------------- #

class _StubProvider:
    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, system, user, constraints=None, **kw):
        return self.text

    def summarize(self, content: str) -> str:
        return "测试摘要"

    def stream_chat(self, system, user, constraints=None, **kw):
        yield self.text


def test_generation_path_writes_shadow_row(db):
    proj = _project(db)
    stub = _StubProvider(_long_text())

    first = generate_next_segment(db, proj, provider=stub)
    assert first["segment_no"] == 1
    assert list(db.scalars(select(StyleCheck)).all()) == [], \
        "第一段没有已写正文可作基线，不应写记录"

    second = generate_next_segment(db, proj, provider=stub)
    rows = list(db.scalars(select(StyleCheck)).all())
    assert len(rows) == 1
    assert rows[0].generation_id is not None
    assert rows[0].segment_no == second["segment_no"]
    assert rows[0].verdict == "shadow"
