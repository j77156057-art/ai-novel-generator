"""套路化检测（formulaic）测试：纯函数层为主 + 取数层接线验证。

守护的纪律（与 `backend/app/generation/formulaic.py` 的模块 docstring 对应）：

  F1 M1 跨段 n-gram：跨段计数正确、报告门槛生效、AI 腔模式被逐条排除
     （口径分离，不与 `_detect_ai_flavor` 重复计）、参考语料把「我们自己的
     模板」与「参考作者的习惯」区分开；
  F2 M2 句首集中度：集中 → top5 占比高，分散 → 熵高，方向可分辨；
  F3 M3 句长方差：句长完全一致 → CV 趋 0、跨段极差为 0，参差 → 相反；
  F4 M4 摘要同质：几乎相同的摘要 → Jaccard 高，收敛序列 → 趋势为正；
  F5 **无参考语料时 verdict 必须是 insufficient_baseline 且 baseline is
     None**——钉死「不拍绝对阈值」这条纪律；
  F6 边界：空列表、单段、纯标点、超长文本，不炸、不超时；
  F7 取数层（db，只读）：窗口取段、参考语料基线、M4 的 L1 优先/回落、
     超限抽样追认、缺失项目报错、跑完不写库。
"""

from __future__ import annotations

import itertools
import os
import sys
import time

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.generation.formulaic import (  # noqa: E402
    MIN_CROSS_SEGMENTS,
    MIN_SEGMENT_CHARS,
    analyze_formulaic,
    m1_ngram_repetition,
    m2_sentence_onset,
    m3_sentence_length,
    m4_summary_homogeneity,
    _overlaps_ai_flavor,
)
from app.models import Generation, NovelChunk, Project, RawNovel  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 构造材料（均不含 AI_FLAVOR_PATTERNS 串—— flavored 模板单独标注用途）
# ─────────────────────────────────────────────────────────────────────────────

#: 安全的共享模板句（跨段重复检测的主体）。
TEMPLATE_A = "他缓缓抬起右手，目光落在窗外的梧桐树上。"
TEMPLATE_B = "铜钥匙在锁孔里转了两圈才打开。"
#: 含 AI 腔模式「仿佛」的模板句（专用于口径分离测试）。
TEMPLATE_FLAVORED = "她仿佛明白了什么，转身离开柜台。"

#: 句首池：10 个字两两组合取前 24 个，保证 24 个互不相同的 2 字句首。
_ONSET_POOL = "甲乙丙丁戊己庚辛壬癸"
DISTINCT_ONSETS = [
    "".join(pair) for pair in itertools.product(_ONSET_POOL, repeat=2)
][:24]


def _sentences(onsets: list[str], body: str = "了些无关紧要的事情。") -> str:
    """把一组句首拼成一段文本（每句 = 句首 + 正文，以。结尾）。"""
    return "".join(o + body for o in onsets)


def _length_segment(lengths: list[int], ch: str = "啊") -> str:
    """按给定句长列表构造一段文本（每句 = ch×长度 + 。）。"""
    return "".join(ch * n + "。" for n in lengths)


# ─────────────────────────────────────────────────────────────────────────────
# F1：M1 跨段 n-gram 重复
# ─────────────────────────────────────────────────────────────────────────────


def test_m1_cross_segment_counts_and_threshold():
    """同一 6-gram 跨 2 段计 2、跨 3 段计 3；默认门槛 3 以下不进报告。"""
    assert MIN_CROSS_SEGMENTS == 3  # 规范口径：跨 ≥3 个不同段才计模板
    two_segs = [
        (1, TEMPLATE_A + "柜台后的老钟敲了三下。"),
        (2, TEMPLATE_A + "雨忽然停了。"),
    ]
    three_segs = two_segs + [(3, TEMPLATE_A + "猫从墙角走过。")]

    # 默认门槛（3）：跨 2 段的模板不进报告
    # min_segment_chars=0：本用例测的是**算法本身**（跨段计数与门槛），
    # 刻意绕过样本量守卫；守卫本身由下面的守卫专项用例覆盖。
    strict = m1_ngram_repetition(two_segs, min_segment_chars=0)
    assert strict["detail"]["repeated_ngrams"] == []
    assert strict["value"] == 0.0
    assert strict["verdict"] == "insufficient_baseline"  # 窗口不足 3 段

    # 门槛降到 2：可见，且跨段计数与段号列表正确
    loose = m1_ngram_repetition(two_segs, min_segments=2, min_segment_chars=0)
    hit = next(
        r for r in loose["detail"]["repeated_ngrams"] if r["ngram"] == "他缓缓抬起右"
    )
    assert hit["segment_count"] == 2
    assert hit["segments"] == [1, 2]

    # 跨 3 段：默认门槛下进报告
    three = m1_ngram_repetition(three_segs, min_segment_chars=0)
    hit3 = next(
        r for r in three["detail"]["repeated_ngrams"] if r["ngram"] == "他缓缓抬起右"
    )
    assert hit3["segment_count"] == 3
    assert hit3["segments"] == [1, 2, 3]
    assert hit3["length"] == 6

    # 总量口径自洽：repeated_ratio == 重复数 / distinct 数
    detail = three["detail"]
    assert three["value"] == pytest.approx(
        detail["repeated_ngram_count"] / detail["distinct_ngrams"]
    )
    assert three["value"] > 0


def test_m1_excludes_ai_flavor_patterns_per_ngram():
    """与 AI 腔模式有子串关系的 6-gram 逐条排除；同句其余 6-gram 照常计入。"""
    segs = [
        (1, TEMPLATE_FLAVORED + TEMPLATE_A + "柜台后的老钟敲了三下。"),
        (2, TEMPLATE_FLAVORED + TEMPLATE_A + "雨忽然停了。"),
        (3, TEMPLATE_FLAVORED + TEMPLATE_A + "猫从墙角走过。"),
    ]
    result = m1_ngram_repetition(segs, min_segment_chars=0)
    reported = {r["ngram"] for r in result["detail"]["repeated_ngrams"]}

    # 含「仿佛」的两个 6-gram 必须被排除（口径分离：归 _detect_ai_flavor 管）
    assert "她仿佛明白" not in reported
    assert "仿佛明白了" not in reported
    # 上报集合里不允许出现任何与 AI 腔模式有子串关系的 n-gram
    assert all(not _overlaps_ai_flavor(g) for g in reported)
    # 排除是逐条而非连坐：只有真含「仿佛」的两条被剔，计数如实上报
    assert result["detail"]["excluded_ai_flavor_ngrams"] == 2
    # 同一句里不含套话的其余 6-gram 不受连坐，照常上报
    assert "他缓缓抬起右" in reported
    assert "佛明白了什么" in reported


def test_m1_reference_distinguishes_own_template_from_author_habit():
    """跨段重复且参考语料里也有 = 作者习惯；参考里没有 = 我们自己的模板。"""
    segs = [
        (1, TEMPLATE_B + TEMPLATE_A + "柜台后的老钟敲了三下。"),
        (2, TEMPLATE_B + TEMPLATE_A + "雨忽然停了。"),
        (3, TEMPLATE_B + TEMPLATE_A + "猫从墙角走过。"),
    ]
    # 参考作者自己也高频用 TEMPLATE_A（2 个 chunk 都有），从不用 TEMPLATE_B
    reference = [
        TEMPLATE_A + "他把窗户推开一条缝。",
        TEMPLATE_A + "茶已经凉透了。",
        "街角的路灯闪了两下，夜色沉下来。",
    ]
    # top_k=50 取全量：报告列表默认只展示前 20 条（同跨段数按字典序截断），
    # 计数类口径（own_template_count 等）本就按全量算，不受展示上限影响。
    result = m1_ngram_repetition(segs, reference, top_k=50, min_segment_chars=0)
    by_ngram = {r["ngram"]: r for r in result["detail"]["repeated_ngrams"]}

    habit = by_ngram["他缓缓抬起右"]
    assert habit["also_in_reference"] is True
    assert habit["reference_chunk_count"] == 2

    own = by_ngram["铜钥匙在锁孔"]
    assert own["also_in_reference"] is False
    assert own["reference_chunk_count"] == 0

    assert result["baseline"] is not None
    assert result["verdict"] in {"ok", "watch"}
    assert result["detail"]["own_template_count"] >= 1
    assert result["detail"]["reference"]["chunks"] == 3


def test_m1_no_reference_is_insufficient_baseline():
    """无参考语料：只报原始数值，verdict=insufficient_baseline，baseline=None。"""
    segs = [
        (1, TEMPLATE_A + "柜台后的老钟敲了三下。"),
        (2, TEMPLATE_A + "雨忽然停了。"),
        (3, TEMPLATE_A + "猫从墙角走过。"),
    ]
    result = m1_ngram_repetition(segs, min_segment_chars=0)
    assert result["value"] is not None and result["value"] > 0
    assert result["baseline"] is None
    assert result["verdict"] == "insufficient_baseline"
    assert result["detail"]["reference"] is None
    # 无参考时 also_in_reference / reference_chunk_count 必须是 None（不是 0/False）
    for row in result["detail"]["repeated_ngrams"]:
        assert row["also_in_reference"] is None
        assert row["reference_chunk_count"] is None


# ─────────────────────────────────────────────────────────────────────────────
# F2：M2 句首集中度
# ─────────────────────────────────────────────────────────────────────────────


def test_m2_onset_concentration_directions():
    """集中（少数句首刷屏）→ top5 占比高；分散（句首各异）→ 归一化熵高。"""
    repetitive = _sentences(["他说"] * 16 + DISTINCT_ONSETS[:8])
    diverse = _sentences(DISTINCT_ONSETS[:24])

    rep = m2_sentence_onset([(1, repetitive)])
    div = m2_sentence_onset([(1, diverse)])

    assert rep["detail"]["total_onsets"] == 24
    assert rep["detail"]["distinct_onsets"] == 9
    # top5 = 16 + 1 + 1 + 1 + 1 = 20 / 24
    assert rep["value"] == pytest.approx(20 / 24)
    assert div["value"] == pytest.approx(5 / 24)
    assert rep["value"] > div["value"]

    # 分散序列 24 个互异句首 → 归一化熵满分；集中序列熵被单一高频拉低
    assert div["detail"]["normalized_entropy"] == pytest.approx(1.0)
    assert rep["detail"]["normalized_entropy"] < div["detail"]["normalized_entropy"]
    # 顶部句首可读（对白句首集中是人要看的信号）
    assert rep["detail"]["top_onsets"][0] == ["他说", 16]


def test_m2_with_reference_watch_when_more_concentrated():
    """有参考基线：生成侧比参考作者更集中 → watch；反之 ok。"""
    repetitive = _sentences(["他说"] * 16 + DISTINCT_ONSETS[:8])
    diverse = _sentences(DISTINCT_ONSETS[:24])

    watch = m2_sentence_onset([(1, repetitive)], [diverse])
    assert watch["baseline"] == pytest.approx(5 / 24)
    assert watch["verdict"] == "watch"
    assert watch["detail"]["delta"] > 0

    ok = m2_sentence_onset([(1, diverse)], [diverse])
    assert ok["verdict"] == "ok"
    assert ok["detail"]["delta"] == pytest.approx(0.0)


def test_m2_no_reference_is_insufficient_baseline():
    """无参考语料：insufficient_baseline + baseline None（纪律钉死）。"""
    diverse = _sentences(DISTINCT_ONSETS[:24])
    result = m2_sentence_onset([(1, diverse)])
    assert result["value"] is not None
    assert result["baseline"] is None
    assert result["verdict"] == "insufficient_baseline"


# ─────────────────────────────────────────────────────────────────────────────
# F3：M3 句长方差塌缩
# ─────────────────────────────────────────────────────────────────────────────


def test_m3_uniform_lengths_collapse_to_zero():
    """句长完全一致 → 每段 CV=0、跨段 mean 极差=0、means stdev=0。"""
    segs = [
        (1, _length_segment([10, 10, 10, 10, 10])),
        (2, _length_segment([10, 10, 10, 10, 10], ch="吧")),
        (3, _length_segment([10, 10, 10, 10, 10], ch="嗯")),
    ]
    result = m3_sentence_length(segs)
    assert result["value"] == pytest.approx(0.0, abs=1e-12)
    assert result["detail"]["mean_spread"] == pytest.approx(0.0)
    assert result["detail"]["means_stdev"] == pytest.approx(0.0, abs=1e-12)
    assert result["detail"]["units_with_cv"] == 3


def test_m3_varied_lengths_keep_variance():
    """句长参差 → CV 显著大于 0、跨段 mean 有极差。"""
    segs = [
        (1, _length_segment([4, 6, 8, 10, 12])),
        (2, _length_segment([10, 14, 18, 22, 26])),
        (3, _length_segment([20, 24, 28, 32, 36])),
    ]
    result = m3_sentence_length(segs)
    # 各段 mean = 8 / 18 / 28 → 极差 20、stdev 10
    assert result["detail"]["mean_spread"] == pytest.approx(20.0)
    assert result["detail"]["means_stdev"] == pytest.approx(10.0)
    assert result["value"] > 0.1
    assert result["value"] < 0.5


def test_m3_with_reference_watch_when_flatter():
    """有参考基线：生成侧平均 CV 低于参考作者 → watch（节奏更塌）。"""
    gen_segs = [
        (1, _length_segment([4, 6, 8, 10, 12])),
        (2, _length_segment([10, 14, 18, 22, 26])),
        (3, _length_segment([20, 24, 28, 32, 36])),
    ]
    # 参考 chunk 句长高度参差 → CV 明显高于生成侧
    reference = [
        _length_segment([2, 6, 10, 14, 18, 22, 26, 30]),
        _length_segment([2, 6, 10, 14, 18, 22, 26, 30], ch="吧"),
        _length_segment([2, 6, 10, 14, 18, 22, 26, 30], ch="嗯"),
    ]
    result = m3_sentence_length(gen_segs, reference)
    assert result["baseline"] is not None
    assert result["baseline"] > result["value"]
    assert result["verdict"] == "watch"


def test_m3_no_reference_is_insufficient_baseline():
    """无参考语料：insufficient_baseline + baseline None（纪律钉死）。"""
    segs = [
        (1, _length_segment([10, 10, 10, 10, 10])),
        (2, _length_segment([10, 10, 10, 10, 10])),
    ]
    result = m3_sentence_length(segs)
    assert result["value"] is not None
    assert result["baseline"] is None
    assert result["verdict"] == "insufficient_baseline"


# ─────────────────────────────────────────────────────────────────────────────
# F4：M4 L1 摘要同质
# ─────────────────────────────────────────────────────────────────────────────

_SUM_A = "林深把铜钥匙藏进门后的铁盒，雨下了整夜。"
_SUM_B = "陈舟在码头清点货箱，海雾浓得化不开。"
_SUM_C = "沈雀在阁楼抄完最后一页账，灯花爆了一声。"
_SUM_D = "老赵把船缆系牢，潮水开始涨了。"


def test_m4_identical_summaries_jaccard_one():
    """两篇完全相同的摘要 → Jaccard=1.0；单对时趋势无定义（None）。"""
    result = m4_summary_homogeneity([(1, _SUM_A), (2, _SUM_A)])
    assert result["value"] == pytest.approx(1.0)
    assert result["detail"]["trend"] is None
    # M4 永远无参考基线：verdict 恒 insufficient_baseline、baseline 恒 None
    assert result["verdict"] == "insufficient_baseline"
    assert result["baseline"] is None
    assert "人工阅读" in result["baseline_note"]


def test_m4_converging_sequence_trend_positive():
    """前两对疏离、最后一对几乎相同 → 平均 Jaccard 被拉高、趋势为正。"""
    items = [
        (1, _SUM_A),
        (2, _SUM_B),
        (3, _SUM_A),                      # 与第 1 篇相同
        (4, _SUM_A + "他又检查了一遍锁。"),  # 与第 3 篇几乎相同
    ]
    result = m4_summary_homogeneity(items)
    pairs = result["detail"]["pairs"]
    assert len(pairs) == 3
    assert pairs[0]["jaccard"] < 0.2          # A vs B：互不相干
    assert pairs[2]["jaccard"] > 0.5          # A vs A+尾巴：高度重合
    assert result["value"] > 0.2              # 一对高度重合把均值拉过 0.2
    assert result["detail"]["trend"] > 0      # 后半窗比前半窗更像


def test_m4_distinct_summaries_stay_low():
    """四篇互不相干 → 平均 Jaccard 低、趋势接近 0。"""
    items = [(1, _SUM_A), (2, _SUM_B), (3, _SUM_C), (4, _SUM_D)]
    result = m4_summary_homogeneity(items)
    assert result["value"] < 0.2
    assert abs(result["detail"]["trend"]) < 0.2


# ─────────────────────────────────────────────────────────────────────────────
# F6：边界与规模
# ─────────────────────────────────────────────────────────────────────────────


def test_edge_empty_inputs_all_metrics():
    """空文本列表：四个指标都不炸，全部 insufficient_baseline + baseline None。"""
    for fn in (m1_ngram_repetition, m2_sentence_onset, m3_sentence_length):
        result = fn([])
        assert result["value"] is None, fn.__name__
        assert result["baseline"] is None
        assert result["verdict"] == "insufficient_baseline"
    assert m4_summary_homogeneity([])["value"] is None


def test_edge_single_segment_and_pure_punctuation():
    """单段：跨段重复无从谈起；纯标点：无可用的 n-gram / 句首口径。"""
    single = m1_ngram_repetition([(1, TEMPLATE_A)], min_segment_chars=0)
    assert single["value"] == 0.0
    assert single["verdict"] == "insufficient_baseline"

    punct = [
        (1, "，，，。。。！！"),
        (2, "……；；；？？"),
        (3, "、、、。。。"),
    ]
    m1 = m1_ngram_repetition(punct, min_segment_chars=0)
    assert m1["value"] is None  # 全部 n-gram 被纯标点规则滤掉
    assert m1["detail"]["excluded_punct_ngrams"] > 0
    assert m1["verdict"] == "insufficient_baseline"

    m2 = m2_sentence_onset(punct)
    assert m2["value"] is None  # 断句残留被句首剥离规则滤掉
    assert m2["verdict"] == "insufficient_baseline"

    m3 = m3_sentence_length(punct)
    assert m3["value"] is None  # 每段不足 2 句，CV 无定义
    assert m3["verdict"] == "insufficient_baseline"


def test_edge_long_text_does_not_blow_up():
    """超长文本（5 段 × ~5.4 万字）：不炸、不超时，且高度重复可测。"""
    unit = TEMPLATE_A + "她说了一句无关紧要的话。"
    big = unit * 2000
    segs = [(i, big + f"第{i}段的尾巴。") for i in range(1, 6)]

    t0 = time.monotonic()
    m1 = m1_ngram_repetition(segs, min_segment_chars=0)
    m2 = m2_sentence_onset(segs)
    m3 = m3_sentence_length(segs)
    elapsed = time.monotonic() - t0

    assert elapsed < 20.0, f"三个指标耗时 {elapsed:.1f}s，超限"
    assert m1["value"] > 0.3
    assert m1["detail"]["repeated_ngrams"][0]["segment_count"] == 5
    assert m2["value"] is not None
    assert m3["value"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# F7：取数层（db，只读）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    """独立临时库上的一个 Session；用完只清本测试碰过的四张表。"""
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        with engine.begin() as conn:
            for table in ("generations", "novel_chunks", "raw_novels", "projects"):
                conn.execute(text(f"DELETE FROM {table}"))


def _seed_project(db, *, reference_novel_ids=None, summary_tree=None) -> Project:
    project = Project(
        title="套路化检测测试项目",
        target_length=5000,
        reference_novel_ids=list(reference_novel_ids or []),
        summary_tree=summary_tree,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _seed_generation(
    db, project_id: int, segment_no: int, body: str, summary: str
) -> Generation:
    gen = Generation(
        project_id=project_id,
        segment_no=segment_no,
        text=body,
        summary={"text": summary},
    )
    db.add(gen)
    db.commit()
    db.refresh(gen)
    return gen


def _seed_novel_with_chunks(db, texts: list[str]) -> int:
    novel = RawNovel(title="参考语料", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for idx, t in enumerate(texts):
        db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=idx, text=t))
    db.commit()
    return novel.id


def _gen_body(tag: str) -> str:
    """一段生成正文：共享模板 + 12 句同句首（「他说」）同句长的对白。"""
    # 20 句（而非 12 句）：让总长越过 MIN_SEGMENT_CHARS=200，夹具才是真实段长量级
    # （产品 generation_length 默认 3000 字，171 字的"段"本就失真）。
    return TEMPLATE_A + "".join(f"他说{'啊' * 8}。" for _ in range(20)) + f"第{tag}段的尾巴。"


def _ref_chunk(j: int) -> str:
    """一个参考 chunk：24 个互异句首 + 句长递变的句子。"""
    return "".join(
        onset + "啊" * (4 + i + j) + "。" for i, onset in enumerate(DISTINCT_ONSETS)
    )


def test_analyze_no_reference_all_insufficient(db):
    """无参考语料：has_reference=False，四个指标全部 insufficient_baseline。"""
    project = _seed_project(db)
    for i in range(1, 4):
        _seed_generation(db, project.id, i, _gen_body(str(i)), f"第{i}段摘要。")

    report = analyze_formulaic(db, project.id, recent_n=10)

    assert report["project_id"] == project.id
    assert report["has_reference"] is False
    assert report["window_segments"] == [1, 2, 3]
    assert report["generated_chars"] > 0
    for key, metric in report["metrics"].items():
        assert metric["verdict"] == "insufficient_baseline", key
        assert metric["baseline"] is None, key
    # 无参考时原始数值仍然算得出来（诊断可见，只是不下判决）
    assert report["metrics"]["m1_ngram_repetition"]["value"] > 0
    assert report["metrics"]["m2_sentence_onset"]["value"] == pytest.approx(1.0)
    assert "诊断不是判决" in report["overall"]["note"]


def test_analyze_with_reference_gives_ok_watch(db):
    """有参考语料：M1/M2/M3 拿到可靠基线（ok/watch），M4 恒 insufficient。"""
    novel_id = _seed_novel_with_chunks(db, [_ref_chunk(j) for j in range(3)])
    project = _seed_project(db, reference_novel_ids=[novel_id])
    for i in range(1, 4):
        _seed_generation(db, project.id, i, _gen_body(str(i)), f"第{i}段摘要。")

    report = analyze_formulaic(db, project.id, recent_n=10)

    assert report["has_reference"] is True
    m1 = report["metrics"]["m1_ngram_repetition"]
    m2 = report["metrics"]["m2_sentence_onset"]
    m3 = report["metrics"]["m3_sentence_length"]
    m4 = report["metrics"]["m4_summary_homogeneity"]
    assert m1["verdict"] in {"ok", "watch"}
    assert m1["baseline"] is not None
    # 生成侧句首全压在「他说」上（top5=1.0），参考侧 24 个互异句首 → watch
    assert m2["verdict"] == "watch"
    assert m2["baseline"] < m2["value"]
    # 生成侧句长整齐（CV≈0.18），参考侧句长递变（CV≈0.4）→ watch
    assert m3["verdict"] == "watch"
    assert m3["baseline"] > m3["value"]
    # M4 是无参考基线的自洽性度量：verdict 恒 insufficient_baseline
    assert m4["verdict"] == "insufficient_baseline"
    assert m4["baseline"] is None
    assert m4["detail"]["source"] == "generation_summary_fallback"
    assert m4["value"] is not None


def test_analyze_window_and_m4_l1_source(db):
    """recent_n 决定窗口；M4 优先读 summary_tree 的 L1 链。"""
    tree = {
        "l1": {
            "1": _SUM_A,
            "2": _SUM_B,
            "3": _SUM_C,
            "4": _SUM_D,
            "5": _SUM_A + "他又检查了一遍锁。",
        },
        "l2": {},
        "l3": {"text": "", "through_volume": 0},
        "params": {"volume_size": 8},
    }
    project = _seed_project(db, summary_tree=tree)
    for i in range(1, 6):
        _seed_generation(db, project.id, i, _gen_body(str(i)), f"第{i}段摘要。")

    report = analyze_formulaic(db, project.id, recent_n=3)

    assert report["window_segments"] == [3, 4, 5]
    m4 = report["metrics"]["m4_summary_homogeneity"]
    assert m4["detail"]["source"] == "summary_tree_l1"
    assert m4["detail"]["n_summaries"] == 3
    assert [p["from"] for p in m4["detail"]["pairs"]] == [3, 4]


def test_analyze_is_read_only(db):
    """取数层只读：跑完不写库（段数不变），可反复调用结果一致。"""
    project = _seed_project(db)
    for i in range(1, 4):
        _seed_generation(db, project.id, i, _gen_body(str(i)), f"第{i}段摘要。")

    first = analyze_formulaic(db, project.id)
    second = analyze_formulaic(db, project.id)
    count = db.execute(
        text("SELECT COUNT(*) FROM generations WHERE project_id = :p"),
        {"p": project.id},
    ).scalar()

    assert count == 3
    assert first == second


def test_analyze_sampling_note_when_corpus_over_limit(db):
    """参考语料超过 REFERENCE_CHUNK_LIMIT：均匀抽样，且抽样事实进 note。"""
    texts = [f"第{i}段参考正文，内容各不相同，用于抽样测试。" for i in range(205)]
    novel_id = _seed_novel_with_chunks(db, texts)
    project = _seed_project(db, reference_novel_ids=[novel_id])
    for i in range(1, 4):
        _seed_generation(db, project.id, i, _gen_body(str(i)), f"第{i}段摘要。")

    report = analyze_formulaic(db, project.id)
    assert report["has_reference"] is True
    for key in ("m1_ngram_repetition", "m2_sentence_onset", "m3_sentence_length"):
        note = report["metrics"][key]["baseline_note"]
        assert "抽样" in note, key
        assert "205" in note, key


def test_analyze_missing_project_raises(db):
    """项目不存在：明确报错，不返回一份空报告冒充成功。"""
    with pytest.raises(ValueError, match="项目不存在"):
        analyze_formulaic(db, 999999)


# ─────────────────────────────────────────────────────────────────────────────
# M1 最差段长守卫（MIN_SEGMENT_CHARS）
#
# 背景：`repeated_ratio` 的分母随段长萎缩，短段窗口下跨段偶发碰撞占比虚高。
# 主理人在 novel_demo.db 实测：约 175 字的短段把项目 1 的比值从 0.0037 抬到
# 0.0260（虚高 7 倍）；全部约 130 字的窗口得到 0.90，而够长段数为 0——纯假象。
# 以下用例钉死守卫行为：不给误导性数值，而不是「把数值修正到正确」。
# ─────────────────────────────────────────────────────────────────────────────


def _long_seg(seg_no: int, template: str, total: int = 260) -> tuple[int, str]:
    """构造一段 ≥ MIN_SEGMENT_CHARS 的正文。

    补白按段号取名（`甲{seg_no}` 反复），保证**跨段的补白 n-gram 互不相同**，
    不引入额外的跨段重复、污染被测的 template 计数。
    """
    fill_unit = f"甲{seg_no}丁"          # 4 字，段间互异
    body = template
    while len(body) < total:
        body += fill_unit
    return seg_no, body


def test_guard_short_only_window_refuses_to_give_comparable_value():
    """纯短段窗口（每段 130 字）：不给可比数值，绝不复现那个虚高的 0.9。

    真实数据上这种窗口曾算出 repeated_ratio=0.9015——纯粹是偶发碰撞，
    因为每段 distinct 6-gram 宇宙只有约 125 个。
    """
    short = [(i, TEMPLATE_A + "乙" * 110) for i in range(1, 6)]
    assert all(len(t) < MIN_SEGMENT_CHARS for _n, t in short)

    out = m1_ngram_repetition(short)

    assert out["verdict"] == "insufficient_baseline"
    assert out["value"] is None, (
        f"短段窗口不得给出可比数值（会误导跨项目比较），实际 {out['value']}")
    d = out["detail"]
    assert d["qualifying_segments"] == 0
    assert d["excluded_short_segments"] == 5
    assert d["median_segment_chars"] < MIN_SEGMENT_CHARS
    assert str(MIN_SEGMENT_CHARS) in out["baseline_note"]


def test_guard_mixed_window_equals_long_only_window():
    """核心断言：长短段混合的结果必须与「只用够长段」完全一致。

    这是本修复的验收标准——短段不再影响任何数值。若哪天有人把守卫去掉，
    这条会立刻变红。
    """
    long_segs = [
        _long_seg(1, TEMPLATE_A + "柜台后的老钟敲了三下。"),
        _long_seg(2, TEMPLATE_A + "雨忽然停了。"),
        _long_seg(3, TEMPLATE_A + "猫从墙角走过。"),
    ]
    short_segs = [(91, TEMPLATE_A + "丙" * 100), (92, TEMPLATE_A + "丁" * 100)]

    mixed = m1_ngram_repetition(long_segs + short_segs)
    long_only = m1_ngram_repetition(long_segs)

    assert mixed["value"] == long_only["value"]
    assert mixed["detail"]["repeated_ngram_count"] == long_only["detail"]["repeated_ngram_count"]
    assert mixed["detail"]["distinct_ngrams"] == long_only["detail"]["distinct_ngrams"]
    # 混合窗口额外记录被排除的短段数量，且这些短段的段号不出现在报告里
    assert mixed["detail"]["excluded_short_segments"] == 2
    assert mixed["detail"]["qualifying_segments"] == 3
    reported_nos = {no for r in mixed["detail"]["repeated_ngrams"] for no in r["segments"]}
    assert not (reported_nos & {91, 92}), "被守卫排除的短段不得出现在重复报告里"


def test_guard_too_few_long_segments_is_insufficient():
    """够长段不足 MIN_CROSS_SEGMENTS：跨段重复无从谈起，只报不下判决。"""
    two_long = [
        _long_seg(1, TEMPLATE_A + "柜台后的老钟敲了三下。"),
        _long_seg(2, TEMPLATE_A + "雨忽然停了。"),
    ]
    out = m1_ngram_repetition(two_long)
    assert out["verdict"] == "insufficient_baseline"
    assert out["detail"]["qualifying_segments"] == 2
    assert f"仅 2 段" in out["baseline_note"] or "2 段" in out["baseline_note"]


def test_guard_threshold_plateau_is_stable():
    """平台稳定性：在 200/400 两个守卫线下结果一致。

    主理人在真实项目 1 上实测 200–600 全给同一值（0.003711），说明短段是
    失真全部来源、区间内取值不影响结论。这里用合成数据复现该性质。
    """
    segs = [
        _long_seg(1, TEMPLATE_A + "柜台后的老钟敲了三下。", total=520),
        _long_seg(2, TEMPLATE_A + "雨忽然停了。", total=520),
        _long_seg(3, TEMPLATE_A + "猫从墙角走过。", total=520),
        (9, TEMPLATE_A + "戊" * 260),   # 260 字：过 200，不过 400
    ]
    at_200 = m1_ngram_repetition(segs, min_segment_chars=200)
    assert at_200["detail"]["qualifying_segments"] == 4

    at_400 = m1_ngram_repetition(segs, min_segment_chars=400)
    assert at_400["detail"]["qualifying_segments"] == 3
    # 那条 260 字的段不含 TEMPLATE_A 之外的额外重复，剔除它不改变「模板跨几段」
    assert at_200["detail"]["repeated_ngram_count"] == at_400["detail"]["repeated_ngram_count"]


def test_guard_applies_to_reference_side_symmetrically():
    """参考侧同口径过滤：短参考单元不计入参考分布，且排除数可对账。"""
    gen = [
        _long_seg(1, TEMPLATE_A + "柜台后的老钟敲了三下。"),
        _long_seg(2, TEMPLATE_A + "雨忽然停了。"),
        _long_seg(3, TEMPLATE_A + "猫从墙角走过。"),
    ]
    ref_long = ["庚" * 300, "辛" * 300, "壬" * 300]
    ref_short = ["癸" * 120, "子" * 120]

    out = m1_ngram_repetition(gen, ref_long + ref_short)

    ref = out["detail"]["reference"]
    assert ref["qualifying_chunks"] == 3
    assert ref["excluded_short_chunks"] == 2
    # 短参考单元不足 MIN_REFERENCE_CHUNKS 时不下判决（这里恰好 3 个够长，够）
    assert out["verdict"] in {"ok", "watch", "insufficient_baseline"}
