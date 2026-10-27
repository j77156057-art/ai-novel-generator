"""beat 判据 3 的「参考书原文」接线（`engine._reference_corpus_text`）

## 为什么要有这个测试

`beat_insert._insert_beats` 有个 `corpus_text` 参数：拿它当**专名白名单**
（判据 3「不能造事实」的依据之一）。引擎侧最早漏了这个参数 ——
`corpus_text=corpus_text or None` 里的 `corpus_text` 在函数里**根本没定义**
⇒ 跑到 beat 插入那一步必然 NameError。

真跑（完整流程）才能暴露这种问题；单测必须把这条钉住。

## 2026-10-27：这个接线**曾经是假的**

复验时查出：`corpus_vocabulary()` 定义了却**从没被调用** ⇒ `corpus_text`
只影响一个诊断标签（`vocab_source=参考书原文`），而那条标签**在说谎** ——
docstring 宣称「语汇白名单优先用参考书原文（最强）」，实际什么都没有。

⇒ 现在真的接上：模型**自报的专名**若整串出现在参考书原文里 ⇒ 放行
（精确匹配，没有误杀风险）。诊断字段改名 `proper_noun_source`。

## 覆盖

1. `_reference_corpus_text` 把参考书的切块拼起来；
2. 没有参考书 → 空串（调用方退化成「上下文白名单」）；
3. 超长时按 `max_chars` 截断；
4. `_maybe_insert_beats` **真的把参考书原文传下去**（诊断里 `proper_noun_source` = 参考书原文）。
5. **参考书原文当专名白名单真的生效**（不在专名表、不在上下文里的专名，
   只要书里有就放行）。
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
        "老周低头擦着马镫。“今天风大。”",
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

    def __init__(self, out="老周停下手中的动作，抬头看了看来人。"):
        self.out = out
        self.calls = 0

    def chat(self, system, user, constraints=None, stop_sequences=None, temperature=None):
        self.calls += 1
        return self.out


# ── 1. 拼起参考书切块 ────────────────────────────────────────────────────
def test_reference_corpus_text_joins_chunks(db):
    txt = _reference_corpus_text(db, _Project(), [1])
    assert "老周低头擦着马镫" in txt
    assert "他抬头看了看天色" in txt
    assert txt.index("老周") < txt.index("他抬头")


# ── 2. 没有参考书 → 空串 ─────────────────────────────────────────────────
def test_reference_corpus_text_empty_without_ids(db):
    class _NoRef:
        reference_novel_ids = []

    # 显式给了 id → 就该取到（项目有没有参考书不影响）
    assert "老周低头擦着马镫" in _reference_corpus_text(db, _NoRef(), [1])
    # 项目也没有参考书、也没显式给 id → 空串（调用方退化成「上下文白名单」）
    assert _reference_corpus_text(db, _NoRef(), None) == ""
    assert _reference_corpus_text(db, _NoRef(), []) == ""


def test_reference_corpus_text_falls_back_to_project_ids(db):
    """`novel_ids` 传空 → 回落到**项目自己的**参考书（与 `_reference_plain_dialogue_ratio` 同口径）。"""
    txt = _reference_corpus_text(db, _Project(), [])
    assert "老周低头擦着马镫" in txt


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

    assert "proper_noun_source" in info, f"没走到专名白名单那一步：{info}"
    assert info["proper_noun_source"] == "参考书原文", (
        f"引擎没把参考书原文传下去（降级了）：{info}"
    )
    assert info.get("inserted", 0) >= 1, f"一条都没插进去：{info}"
    # beat 是**并进**那一段，不是另起一段
    assert len(new_text.split("\n")) == len(text.split("\n"))


# ── 5. 参考书原文当**专名白名单**真的生效 ─────────────────────────────
def test_corpus_allows_declared_noun_that_appears_in_reference_book():
    """模型自报的专名若**整串出现在参考书原文里** ⇒ 不是新造的 ⇒ 放行。

    这是 2026-10-27 补上的真接线（之前 `corpus_vocabulary()` 是死函数，
    `corpus_text` 只影响一个说谎的诊断标签）。
    """
    from app.style.beat_insert import check_no_new_facts

    corpus = "……老周低头擦着马镫，北荒王府的青灰马在栏里打了个响鼻……"
    beat = "老周停下手中的动作，抬手朝北荒王府的方向指了指。"

    # 不给 corpus：专名表里没有、上下文里也没有 ⇒ 判为可能造事实
    sus, _deg = check_no_new_facts(
        beat, "他在门口站着。", proper_nouns=set(), declared_nouns=["北荒王府"],
    )
    assert sus, "没有语料兜底时应当判可疑"

    # 给了 corpus：书里确实有「北荒王府」⇒ 放行
    sus2, _deg2 = check_no_new_facts(
        beat, "他在门口站着。", proper_nouns=set(), declared_nouns=["北荒王府"],
        corpus_text=corpus,
    )
    assert not sus2, f"书里有的专名不该被判成新事实：{sus2}"


def test_corpus_does_not_allow_invented_noun():
    """书里**没有**的专名 ⇒ 仍然拒（语料白名单不是万能通行证）。"""
    from app.style.beat_insert import check_no_new_facts

    corpus = "……老周低头擦着马镫，北荒王府的青灰马在栏里打了个响鼻……"
    sus, _deg = check_no_new_facts(
        "他把杯子推到一边。", "他在门口站着。", proper_nouns=set(),
        declared_nouns=["通州大营"], corpus_text=corpus,
    )
    assert sus, "书里没有的专名必须被拒"
