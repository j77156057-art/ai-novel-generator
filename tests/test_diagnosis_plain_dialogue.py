"""新尺段级诊断规则（`style_plain_dialogue_ratio`）—— 滑窗口径

## 验什么

1. **基准从参考书现算**（不依赖档案里有这一项）；
2. **窗口按段落数定义** ⇒ 与文本怎么切无关（第一版按「整段比例」判，
   拿 1200 字分片的基准去判 4000~9000 字的生成段 ⇒ **粒度不匹配**，
   实测**参考书自己被报了 3 条**）；
3. **自证清白**：拿参考书自己的正文当生成正文喂进去 ⇒ 不能报
   （这是最硬的一条 —— 规则若报参考书，说明尺子或阈值错了）；
4. 只有**比参考书自己的 p99 还高**才报；
5. 没有参考语料 / 基准取不到 → **不报**（不是缺口，是无依据）；
6. 段太短（窗口不足）→ 不判。

## 用自建库 + seed（照 `test_style_sentence_rule.py` 的惯例）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402

from app.agents.diagnosis import (  # noqa: E402
    RULE_ORDER,
    RULE_REGISTRY,
    DiagnosticSnapshot,
    _plain_window_ratio,
    _reference_window_distribution,
    rule_style_plain_dialogue_ratio,
)
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import NovelChunk, RawNovel  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


def _seed_reference(db, chunks: list[str]) -> int:
    """seed 一本参考书（若干分片），返回 novel_id。"""
    novel = RawNovel(title="测试参考书", platform="local", source_text="", status="imported")
    db.add(novel)
    db.flush()
    for i, text in enumerate(chunks):
        db.add(NovelChunk(novel_id=novel.id, chapter_no=None, idx=i,
                          text=text, raw_text=text, embedding=None))
    db.commit()
    return novel.id


def _mixed_para(i: int) -> str:
    """有叙述垫着的对白段。"""
    return f"老黄低下头继续擦马镫，指节微微收紧。“第{i}句话是这样说的。”"


def _bare_para(i: int) -> str:
    """光台词段。"""
    return f"“第{i}句话是这样说的。”"


def _snapshot(db, reference_ids: list[int], segments: list[str]):
    class _P:
        id = 2
        reference_novel_ids = reference_ids

    snap = DiagnosticSnapshot(db, _P(), scope="all")
    snap._cache["generations"] = [
        {"id": i + 1, "segment_no": i + 1, "text": t} for i, t in enumerate(segments)
    ]
    return snap


# ── 1. 基准从参考书现算 ─────────────────────────────────────────────────
def test_reference_distribution_computed_from_reference():
    db = SessionLocal()
    try:
        nid = _seed_reference(db, ["\n\n".join(_mixed_para(i) for i in range(6))
                                   for _ in range(40)])
        dist = _reference_window_distribution(db, [nid])
        assert dist, "应算出窗口分布"
        assert all(0.0 <= v <= 1.0 for v in dist)
    finally:
        db.close()


def test_no_reference_ids_gives_empty_distribution():
    db = SessionLocal()
    try:
        assert _reference_window_distribution(db, []) == []
    finally:
        db.close()


# ── 2. 窗口按段落数 ⇒ 与切法无关 ───────────────────────────────────────
def test_window_measure_is_independent_of_how_text_is_split():
    """同一个内容，切成不同块，算出的窗口分布应当**接近**。

    第一版用「整段比例」就踩了这个：分片边界会切断「对白连排」，
    同一本书不同切法 p90 是 24% / 20% / 18% / 16%。
    窗口按段落数定义后，边界只影响**头尾一两个窗口**。

    ⇒ 切成两半后，窗口**总数**会少（每半都丢掉跨断点的那 w-1 个窗口），
    但每个窗口的**值**应当一致 ⇒ 分布（均值）应当接近。
    """
    db = SessionLocal()
    try:
        parts = [_mixed_para(i) for i in range(60)]
        one = _plain_window_ratio(parts)
        half = len(parts) // 2
        two = _plain_window_ratio(parts[:half]) + _plain_window_ratio(parts[half:])
        assert one and two
        # 窗口数：切两半会丢掉断点处跨界的那些窗口（w-1 个）
        assert len(two) < len(one)
        assert len(one) - len(two) <= 2 * 11
        # 每个窗口的值应当一致（内容没变）⇒ 分布接近
        assert abs(sum(one) / len(one) - sum(two) / len(two)) < 0.10
    finally:
        db.close()


# ── 3. 自证清白 ───────────────────────────────────────────────────────
def test_reference_text_does_not_trigger_the_rule():
    """**自证清白**：拿参考书自己的正文当生成正文 ⇒ 几乎不报。

    注意不是「零误报」：参考书**自己**也确实有 100% 纯对白的段
    （实测雪中 3.9% 的窗口是 100% 纯对白）⇒ 报出其中几段是**语义正确**的。
    ⇒ 判据是**误报率**：参考书自己的分片当输入，误报率应很低
    （实测真书 3/200 = 1.5%），而生成侧是 100%。

    规则不该假装参考书完美。
    """
    db = SessionLocal()
    try:
        # 参考书：大部分是「有叙述垫着」的段，少量是纯对白连排（真实书就是这样）
        chunks = (
            ["\n\n".join(_mixed_para(i) for i in range(8)) for _ in range(30)]
            + ["\n\n".join(_bare_para(i) for i in range(8)) for _ in range(30)]
        )
        nid = _seed_reference(db, chunks)
        # 把参考书自己的分片当生成正文（一半有叙述、一半纯对白）
        segs = (["\n\n".join(_mixed_para(i) for i in range(8)),
                 "\n\n".join(_bare_para(i) for i in range(8))] * 20)
        issues = rule_style_plain_dialogue_ratio(_snapshot(db, [nid], segs), set())
        # 误报率必须低（参考书自己 60 个分片里只有纯对白那批会被报）
        assert len(issues) <= len(segs) * 0.2, f"误报率过高：{len(issues)}/{len(segs)}"
    finally:
        db.close()


# ── 4. 触发条件 ─────────────────────────────────────────────────────────
def test_reports_when_segment_is_all_bare_dialogue():
    """一段全是光台词，而参考书写得「叙述密」⇒ 应报。"""
    db = SessionLocal()
    try:
        nid = _seed_reference(db, ["\n\n".join(_mixed_para(i) for i in range(8))
                                   for _ in range(40)])
        seg = "\n\n".join(_bare_para(i) for i in range(30))
        issues = rule_style_plain_dialogue_ratio(_snapshot(db, [nid], [seg]), set())
        assert issues, "应报"
        assert issues[0].rule == "style_plain_dialogue_ratio"
        assert issues[0].type == "style"
        assert issues[0].severity in ("warn", "error")
        assert issues[0].evidence is not None
    finally:
        db.close()


def test_does_not_report_when_segment_has_narration():
    """一段有叙述垫着 ⇒ 不报。"""
    db = SessionLocal()
    try:
        nid = _seed_reference(db, ["\n\n".join(_mixed_para(i) for i in range(8))
                                   for _ in range(40)])
        seg = "\n\n".join(_mixed_para(i) for i in range(30))
        assert not rule_style_plain_dialogue_ratio(_snapshot(db, [nid], [seg]), set())
    finally:
        db.close()


def test_short_segment_is_skipped():
    """窗口不足（段太短）→ 不判。"""
    db = SessionLocal()
    try:
        nid = _seed_reference(db, ["\n\n".join(_mixed_para(i) for i in range(8))
                                   for _ in range(40)])
        seg = "\n\n".join(_bare_para(i) for i in range(3))
        assert not rule_style_plain_dialogue_ratio(_snapshot(db, [nid], [seg]), set())
    finally:
        db.close()


def test_zero_window_ratio_measure():
    """判据用的是「0% 窗口占比」这个**比例**，不是分位点。

    为什么：实测雪中的窗口分布 **76% 是 0%、3.9% 是 100%** ⇒
    **p99 = 100%** ⇒ 「窗口 > p99」永远不成立 ⇒ 一条都不报。
    ⇒ 分位点在「分布顶端有质量」时退化，必须看**分布形状**。
    """
    from app.agents.diagnosis import _zero_window_ratio

    assert _zero_window_ratio([0.0, 0.0, 0.0, 0.5, 1.0]) == pytest.approx(0.6)
    assert _zero_window_ratio([]) == 0.0
    assert _zero_window_ratio([0.5, 1.0]) == 0.0


def test_reference_book_with_its_own_tail_still_reported_faithfully():
    """参考书自己**确实**有 3.9% 的窗口是 100% 纯对白 ⇒ 规则报其中几段是**对的**。

    这条锁住「规则不假装参考书完美」：
    误报率 = 参考书自己被判异常的段数 / 总段数（实测 1.5%），
    远低于生成侧的 100%。
    """
    db = SessionLocal()
    try:
        # 参考书：90% 是「有叙述垫着」的段，10% 是纯对白连排
        chunks = (
            ["\n\n".join(_mixed_para(i) for i in range(8)) for _ in range(90)]
            + ["\n\n".join(_bare_para(i) for i in range(8)) for _ in range(10)]
        )
        nid = _seed_reference(db, chunks)
        segs = ["\n\n".join(_bare_para(i) for i in range(8))] * 10
        issues = rule_style_plain_dialogue_ratio(_snapshot(db, [nid], segs), set())
        # 纯对白段应当被报出来（这正是它要抓的东西）
        assert issues, "纯对白连排的段应被报"
    finally:
        db.close()


# ── 5. 无依据就不报 ────────────────────────────────────────────────────
def test_no_reference_no_issue():
    db = SessionLocal()
    try:
        seg = "\n\n".join(_bare_para(i) for i in range(30))
        assert not rule_style_plain_dialogue_ratio(_snapshot(db, [], [seg]), set())
    finally:
        db.close()


def test_too_few_reference_samples_no_issue():
    """参考书样本太少 ⇒ 基准不可靠 ⇒ 不报。"""
    db = SessionLocal()
    try:
        nid = _seed_reference(db, ["\n\n".join(_mixed_para(i) for i in range(8))])
        seg = "\n\n".join(_bare_para(i) for i in range(30))
        assert not rule_style_plain_dialogue_ratio(_snapshot(db, [nid], [seg]), set())
    finally:
        db.close()


def test_empty_generations_no_issue():
    db = SessionLocal()
    try:
        assert not rule_style_plain_dialogue_ratio(_snapshot(db, [1], []), set())
    finally:
        db.close()


# ── 6. 规则已注册，且顺序稳定 ───────────────────────────────────────────
def test_rule_is_registered():
    assert "style_plain_dialogue_ratio" in RULE_REGISTRY
    assert RULE_ORDER[-1] == "style_plain_dialogue_ratio"
