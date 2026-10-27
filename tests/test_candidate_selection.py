"""候选打分（`app/generation/candidate.py`）

覆盖的判据：

1. 空文本 → 不合格；
2. 守恒判据：比**外部字数目标**缩水太多 → 不合格；膨胀太多 → 不合格；
3. 形态不在参考书分布带内 → 不合格（**带**取档案的 p10~p90）；
4. 没有档案 → 判不出形态，但**不判不合格**（档案是增强，不是生成的前提）；
5. N 的收敛：1 = 关闭，超上限被夹住；
6. **打分必须在校正之后**（用真实档案验证：机械校正后形态才进带）。

「**选哪一份**」不在本模块：选择发生在整条链跑完之后
（`engine._run_pool_selection` 的末端择优），对应用例见
`tests/test_candidate_pool_engine.py`。

第 6 条用的是**真实语料**：从参考书语料里切一段当档案，
再用生产代码 `merge_bare_dialogue` 校正一段「一行一句台词」的正文 ——
校正前应在带外、校正后应进带。这是本模块与机械校正**同一把尺**的证明。
"""
import pathlib
import shutil
import os
import tempfile
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.generation.candidate import (  # noqa: E402
    DEFAULT_POOL_SIZE,
    MAX_POOL_SIZE,
    normalize_pool_size,
    score_candidate,
)


# ── 造一个可控的「档案」 ────────────────────────────────────────────────
# 档案的 feat 结构：{"纯台词段占比": {mean, std, p10, p50, p90}}
def _profile(target=0.2, p10=0.0, p90=0.4):
    return {
        "version": 2,
        "buckets": {
            "medium": {
                "unit_chars": 3000,
                "feat": {
                    "纯台词段占比": {
                        "mean": target, "std": 0.1,
                        "p10": p10, "p50": target, "p90": p90,
                        "min": p10, "max": p90,
                    }
                },
            }
        },
    }


def _mixed_paragraphs(n=6):
    """n 段「叙述 + 台词」同段（形态好）。"""
    return [f"他看了看窗外的天色，心里盘算着接下来的事。“第{i}句话是这样的。”" for i in range(n)]


def _bare_paragraphs(n=6):
    """n 段纯台词（形态差：纯台词段占比 100%）。"""
    return [f"“第{i}句话。”" for i in range(n)]


def _text(paras):
    return "\n".join(paras)


# ── 1. 空文本 ────────────────────────────────────────────────────────────
def test_empty_text_rejected():
    c = score_candidate("", profile=_profile())
    assert not c.ok
    assert "空" in c.reject


# ── 2. 守恒判据 ──────────────────────────────────────────────────────────
def test_shrunk_candidate_rejected():
    """字数不足目标 → 不合格。

    基准是**字数目标**（外部给的可验收值），不是同伴中位数 ——
    拿同伴当基准会误杀形态最差的那份（纯台词稿天然短一半）。
    """
    good = _text(_mixed_paragraphs(8))
    tiny = _text(_mixed_paragraphs(2))
    c = score_candidate(tiny, profile=_profile(), baseline_chars=len(good))
    assert not c.ok
    assert "不足目标" in c.reject


def test_short_but_shape_good_is_not_killed_by_peer_median():
    """守恒判据的**基准是外部的字数目标**，不是同伴中位数。

    踩过的坑：拿同伴中位数当基准时，两份候选长度天然不同（形态差的稿子往往更短）
    ⇒ 正常的候选被判成「膨胀/缩水」而误杀，择优变成了「谁长谁赢」。
    ⇒ 同一个外部目标下：形态好的那份**不会**因为比同伴短而被杀，
    形态差的那份照样不合格（形态判据在起作用）。
    """
    short_good = _text(_mixed_paragraphs(3))     # 短、但形态好
    long_bad = _text(_bare_paragraphs(12))       # 形态差（纯台词 100%）
    target = 100                                 # 外部的字数目标（段长闸门用的那个）
    c = score_candidate(short_good, profile=_profile(), baseline_chars=target)
    assert c.ok, c.reject
    assert not score_candidate(long_bad, profile=_profile(),
                               baseline_chars=target).ok


def test_inflated_candidate_rejected():
    good = _text(_mixed_paragraphs(8))
    huge = _text(_mixed_paragraphs(40))
    c = score_candidate(huge, profile=_profile(), baseline_chars=len(good))
    assert not c.ok
    assert "超出目标" in c.reject


def test_independent_sampling_is_not_judged_as_rewrite_inflation():
    """**独立采样多写一点是正常的**，不能按「改写膨胀」那把尺判。

    真跑证据（N=3，Flash，目标 2400 字）：候选 2357 / 2811 / 2873 字。
    用 rewrite 的 15% 上限（2760）会把 2811 与 2873 那两份全判成「膨胀」——
    它们恰恰是**独立写出来的正常稿子**，不是谁改写出来的。

    ⇒ 候选池的上限必须比改写宽松（目标 + 60%）。
    """
    from app.generation.candidate import (
        MAX_LEN_OVERFLOW_ABS,
        MAX_LEN_OVERFLOW_RATIO,
    )

    # rewrite 的口径是 15%
    rewrite_limit = 2400 + max(45, int(2400 * 0.15))
    # 候选池的口径要宽得多
    pool_limit = 2400 + max(MAX_LEN_OVERFLOW_ABS, int(2400 * MAX_LEN_OVERFLOW_RATIO))
    assert pool_limit > rewrite_limit

    # 真跑里的 2811 字候选：按 rewrite 口径会被杀，按候选池口径应合格
    # （要造出足够长的文本：每段约 27 汉字，100 段 ≈ 2700 字）
    text = _text(_mixed_paragraphs(110))
    from app.generation.candidate import _cjk_len

    n_cjk = _cjk_len(text)
    assert n_cjk > rewrite_limit, f"样本要够长（{n_cjk}）才能验证这个判据"
    assert score_candidate(text, profile=_profile(), baseline_chars=2400).ok


# ── 3. 形态带 ────────────────────────────────────────────────────────────
def test_out_of_band_rejected_with_reason():
    # 纯台词段 100% → 远超 p90
    c = score_candidate(_text(_bare_paragraphs(6)), profile=_profile())
    assert not c.ok
    assert "分布带" in c.reject


def test_in_band_accepted():
    c = score_candidate(_text(_mixed_paragraphs(6)), profile=_profile())
    assert c.ok, c.reject
    assert c.score >= 0


# ── 4. 选择规则（择优/兜底）现在住在引擎里 ──────────────────────────────
# 「合格里选最近的」「都不合格时落最接近的那份」由
# `engine._run_pool_selection` 承担（因为它要看**成品**，不是原稿）——
# 对应用例见 `tests/test_candidate_pool_engine.py`。
# 本模块只负责**量**：给一份文本打分、判它合不合格。


# ── 5. 没有档案 ──────────────────────────────────────────────────────────
def test_no_profile_means_unjudgeable_not_不合格():
    t = _text(_bare_paragraphs(6))
    c = score_candidate(t, profile=None)
    assert c.ok, "没有档案时不应判不合格（档案是增强，不是生成的前提）"


# ── 8. N 的收敛 ──────────────────────────────────────────────────────────
def test_normalize_pool_size():
    assert normalize_pool_size(None) == 1
    assert normalize_pool_size(0) == 1
    assert normalize_pool_size(1) == 1
    assert normalize_pool_size(3) == 3
    assert normalize_pool_size(999) == MAX_POOL_SIZE
    assert normalize_pool_size("bogus") == 1
    assert 1 <= DEFAULT_POOL_SIZE <= MAX_POOL_SIZE


# ── 9. 与机械校正同一把尺（用真实语料与真实档案） ────────────────────────
@pytest.mark.skipif(
    not (pathlib.Path((os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())) / "novel-scratch/corpus-check/novel_dev.db").exists(),
    reason="需要验证语料库（语料不入库，缺省跳过）",
)
def test_same_ruler_as_mechanical_correction():
    """机械校正把形态推进带内 → 同一个打分器就判它合格。

    这条是本模块的核心承诺：**打分器与机械校正闸门同一把尺**。
    """
    from sqlalchemy import create_engine, text as sa_text

    from app.style.form import form_target, measure_form
    from app.style.form_merge import merge_bare_dialogue
    from app.style.stylometry import paragraphs

    scratch = pathlib.Path((os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())) / "novel-scratch"
    eng = create_engine(f"sqlite:///{(scratch / 'corpus-check/novel_dev.db').as_posix()}")
    with eng.connect() as conn:
        rows = conn.execute(sa_text(
            "SELECT c.text FROM novel_chunks c JOIN raw_novels n ON n.id = c.novel_id "
            "WHERE n.title = '《北荒刀歌》' ORDER BY c.idx LIMIT 400"
        )).fetchall()
    joined = "\n\n".join(r[0] or "" for r in rows)
    assert joined

    # 用生产代码切单元、算档案 target（与 `fit_profile` 同一口径）
    from app.style.stylometry import cut_units, features
    import statistics

    units = cut_units(joined, 3000, max_units=60)
    target = statistics.fmean([features(u)["纯台词段占比"] for u in units])
    p10 = min(features(u)["纯台词段占比"] for u in units)
    p90 = max(features(u)["纯台词段占比"] for u in units)
    prof = {
        "version": 2,
        "buckets": {"medium": {"unit_chars": 3000, "feat": {"纯台词段占比": {
            "mean": target, "std": 0.1, "p10": p10, "p50": target, "p90": p90,
            "min": p10, "max": p90,
        }}}},
    }

    # 造一段「一行一句台词」的正文（形态必然在带外，除非带很宽）
    bare_text = "\n".join(f"“第{i}句话是这样说的。”" for i in range(40))
    before = score_candidate(bare_text, profile=prof)
    # 机械校正（生产代码）
    new_paras, info = merge_bare_dialogue(
        [p for p in paragraphs(bare_text) if p.strip()], target
    )
    merged_text = "\n".join(new_paras)
    after = score_candidate(merged_text, profile=prof)

    # 校正前后的分数关系：校正后离目标更近
    assert after.score <= before.score, "机械校正后应更接近目标"
    # 同一个打分器（同一个 form_target / measure_form 口径）
    assert after.target["target"] == pytest.approx(target)
    # 实测值与 `band_check` 完全一致（长度归一化因子已除掉）
    from app.style.stylometry import band_check

    got = band_check(merged_text, prof, ["纯台词段占比"])["纯台词段占比"]
    assert after.form["value"] == pytest.approx(float(got["value"]))
    # **关键**：band_check 的值不等于 `measure_form` 的 unit_ratio（后者带长度因子）
    # —— 这正是本模块必须走 band_check 的原因。
    raw = measure_form(merged_text)["unit_ratio"]
    assert after.form["value"] != pytest.approx(raw)
