"""beat 判据 3 的「参考书原文」接线（`engine._reference_corpus_text`）

## 为什么要有这个测试

`beat_insert._insert_beats` 有个 `corpus_text` 参数：拿它当**语汇白名单**
（判据 3「不能造事实」的最强依据）。引擎侧最早漏了这个参数 ——
`corpus_text=corpus_text or None` 里的 `corpus_text` 在函数里**根本没定义**
⇒ 跑到 beat 插入那一步必然 NameError。

真跑（完整流程）才能暴露这种问题；单测必须把这条钉住。

## 覆盖

1. `_reference_corpus_text` 把参考书的切块拼起来；
2. 没有参考书 → 空串（调用方退化成「上下文白名单」）；
3. 超长时按 `max_chars` 截断；
4. `_maybe_insert_beats` **真的把参考书原文传下去**（诊断里 `vocab_source` = 参考书原文）。
"""
import pathlib
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.db import Base  # noqa: E402
from app.generation.engine import _maybe_insert_beats, _reference_corpus_text  # noqa: E402
from app.models import NovelChunk, RawNovel  # noqa: E402


@pytest.fixture()
def db():
    """内存 sqlite + 少量语料（`raw_novels` 1 本，`novel_chunks` 3 块）。"""
    eng = create_engine("sqlite://", future=True)
    RawNovel.__table__.create(bind=eng)
    NovelChunk.__table__.create(bind=eng)
    session = sessionmaker(bind=eng, expire_on_commit=False, future=True)()
    novel = RawNovel(title="参考书")
    session.add(novel)
    session.flush()
    for i, t in enumerate([
        "老黄低头擦着马镫。“今天风大。”",
        "“那你说怎么办。”",
        "他抬头看了看天色。“再等等。”",
    ]):
        session.add(NovelChunk(novel_id=novel.id, idx=i, text=t))
    session.commit()
    yield session
    session.close()


class _Project:
    reference_novel_ids = [1]


class _FakeProvider:
    """回一句合格的 beat（动作叙述，无引号、10~30 字、不报数）。"""

    def __init__(self, out="老黄停下手中的动作，抬头看了看来人。"):
        self.out = out
        self.calls = 0

    def chat(self, system, user, constraints=None, stop_sequences=None, temperature=None):
        self.calls += 1
        return self.out


# ── 1. 拼起参考书切块 ────────────────────────────────────────────────────
def test_reference_corpus_text_joins_chunks(db):
    txt = _reference_corpus_text(db, _Project(), [1])
    assert "老黄低头擦着马镫" in txt
    assert "他抬头看了看天色" in txt
    assert txt.index("老黄") < txt.index("他抬头")


# ── 2. 没有参考书 → 空串 ─────────────────────────────────────────────────
def test_reference_corpus_text_empty_without_ids(db):
    class _NoRef:
        reference_novel_ids = []

    # 显式给了 id → 就该取到（项目有没有参考书不影响）
    assert "老黄低头擦着马镫" in _reference_corpus_text(db, _NoRef(), [1])
    # 项目也没有参考书、也没显式给 id → 空串（调用方退化成「上下文白名单」）
    assert _reference_corpus_text(db, _NoRef(), None) == ""
    assert _reference_corpus_text(db, _NoRef(), []) == ""


def test_reference_corpus_text_falls_back_to_project_ids(db):
    """`novel_ids` 传空 → 回落到**项目自己的**参考书（与 `_reference_plain_dialogue_ratio` 同口径）。"""
    txt = _reference_corpus_text(db, _Project(), [])
    assert "老黄低头擦着马镫" in txt


# ── 3. 超长截断 ──────────────────────────────────────────────────────────
def test_reference_corpus_text_truncates(db):
    txt = _reference_corpus_text(db, _Project(), [1], max_chars=10)
    assert len(txt) <= 10


# ── 4. 引擎把原文传下去（不许再出现「变量没定义」） ──────────────────────
def test_maybe_insert_beats_passes_corpus_text(db, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "style_beat_insert", True)

    # 全是光台词（A 类 100%）⇒ 目标（参考书自己的比例）低于 100% ⇒ 会有插入点
    text = "\n".join(f"“第{i}句。”" for i in range(4))
    new_text, info = _maybe_insert_beats(db, _Project(), text, _FakeProvider(), novel_ids=[1])

    assert "vocab_source" in info, f"没走到语汇白名单那一步：{info}"
    assert info["vocab_source"] == "参考书原文", (
        f"引擎没把参考书原文传下去（降级了）：{info}"
    )
    assert info.get("inserted", 0) >= 1, f"一条都没插进去：{info}"
    # beat 是**并进**那一段，不是另起一段
    assert len(new_text.split("\n")) == len(text.split("\n"))
