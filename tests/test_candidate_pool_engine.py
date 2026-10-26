"""候选池接进引擎（`app/generation/engine.py`）

覆盖：

1. `pool_size=1`（默认）→ **一次模型调用不多**，行为与从前完全一致；
2. `pool_size>1` → 采 N 份、择优，返回最好的一份；
3. 择优用的是**形态**（参考书自己的分布带），不是别的；
4. 一份采样失败 → 跳过它，其余照常择优；
5. 全部采样失败 → 抛错（让上层看到真实错误，而不是空正文）；
6. 「先机械校正、再择优」：预览变换参与打分，但**返回的是原文**；
7. 候选池默认关闭（`settings.style_candidate_pool == 1`）。

用的是**假 provider**（不调真模型）—— 这里验的是接线与判据，不是模型质量。
"""
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.generation.engine import _candidate_pool_size, _generate_with_pool  # noqa: E402


class FakeProvider:
    """按顺序返回预设文本，并记录调用次数。"""

    def __init__(self, outputs=None, fail_at=None):
        self.outputs = list(outputs or [])
        self.fail_at = fail_at
        self.calls = 0
        self.seen = []

    def chat(self, system, user, constraints=None, stop_sequences=None, temperature=None):
        idx = self.calls
        self.calls += 1
        self.seen.append((system, user))
        if self.fail_at is not None and idx in self.fail_at:
            raise RuntimeError(f"模拟第 {idx} 次失败")
        if not self.outputs:
            return ""
        return self.outputs[idx % len(self.outputs)]


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


def _bare(n=6):
    """全是光台词 —— **机械校正救不回来**（没有叙述可并）⇒ 校正后仍 100%。"""
    return "\n".join(f"“第{i}句话。”" for i in range(n))


def _mixed(n=6):
    return "\n".join(f"他看了看窗外的天色，心里盘算着接下来的事。“第{i}句话。”" for i in range(n))


def _mixed_long(n=6):
    """与 `_bare` **字数接近**的「有叙述垫着」版本。

    为什么需要它：守恒判据拿「同伴字数中位数」当基准，
    两份候选字数差太多时，长的那份会被判成「膨胀」——
    那道判据是**防凭空扩写**的，不该被长度差本身触发。
    """
    base = "\n".join(f"“第{i}句话。”" for i in range(n))
    return "\n".join(
        f"他看了看窗外的天色，心里盘算着接下来的事。{base.split(chr(10))[i]}"
        for i in range(n)
    )


# ── 1. 默认关闭：一次调用不多 ────────────────────────────────────────────
def test_pool_disabled_uses_single_call():
    p = FakeProvider([_mixed(4)])
    text, info = _generate_with_pool(p, "sys", "user", pool_size=1, profile=_profile())
    assert p.calls == 1
    assert not info["applied"]
    assert text == _mixed(4)


# ── 2. 开启：采 N 份并择优 ────────────────────────────────────────────────
def test_pool_picks_qualified_candidate():
    # 第一份形态很差（纯台词），第二份形态好
    good = _mixed(6)
    p = FakeProvider([_bare(6), good])
    text, info = _generate_with_pool(p, "sys", "user", pool_size=2, profile=_profile(), target_chars=120)
    assert p.calls == 2
    assert info["applied"]
    assert text == good
    assert info["picked"] == 1
    assert info["n_qualified"] >= 1


# ── 2b. 阈值式：第一份就合格 ⇒ **不再采第二份** ─────────────────────────
def test_pool_stops_early_when_first_candidate_qualifies():
    """**阈值式拒绝采样**（清单 AC）：合格即停，不白花后续采样的钱。

    真跑依据：N=3 一次真跑 2033s vs 关闭候选池 268s（7.6 倍），
    而实测 Flash 经机械校正后 4/4 候选已在带内 ⇒ 多数情况第一份就合格。
    """
    good = _mixed_long(6)
    p = FakeProvider([good, _bare(6)])   # 第二份备着但用不上
    text, info = _generate_with_pool(p, "sys", "user", pool_size=3, profile=_profile(), target_chars=120)
    assert p.calls == 1, "第一份合格就不该再采"
    assert info["stopped_early"] is True
    assert text == good


def test_pool_keeps_sampling_when_not_qualified():
    """不合格就继续采 —— 这是拒绝采样的另一半。"""
    p = FakeProvider([_bare(6), _bare(6), _mixed_long(6)])
    text, info = _generate_with_pool(p, "sys", "user", pool_size=3, profile=_profile(), target_chars=120)
    assert p.calls == 3
    assert info["stopped_early"] is True
    assert text == _mixed_long(6)


def test_length_baseline_is_the_target_not_peer_median():
    """守恒判据的基准是**外部的字数目标**，不是同伴中位数。

    踩过的坑：拿同伴中位数当基准时，两份候选长度天然不同
    （形态差的稿子往往更短）⇒ 正常的候选被判成「膨胀」而误杀。
    正确基准 = 段长闸门用的那个 `target_chars`。
    """
    short = _bare(6)          # 18 汉字
    long_ = _mixed_long(6)     # 102 汉字
    p = FakeProvider([short, long_])
    text, info = _generate_with_pool(p, "sys", "user", pool_size=2,
                                     profile=_profile(), target_chars=120)
    # 长的那份不该被判成「膨胀」而淘汰
    assert info["candidates"][1]["reject"] == ""
    assert text == long_


# ── 3. 择优看的是形态 ─────────────────────────────────────────────────────
def test_pool_rejects_out_of_band_candidate():
    bad = _bare(6)
    p = FakeProvider([bad])
    text, info = _generate_with_pool(p, "sys", "user", pool_size=1, profile=_profile())
    # pool_size=1 走短路，不打分 —— 但直接测打分器会更清楚
    from app.generation.candidate import score_candidate

    c = score_candidate(bad, profile=_profile())
    assert not c.ok


# ── 4. 一份失败不影响其余 ────────────────────────────────────────────────
def test_pool_skips_failed_sample():
    """一份采样失败 → 跳过它，其余照常择优。"""
    good = _mixed(6)
    p = FakeProvider([_bare(6), good], fail_at={0})
    text, info = _generate_with_pool(p, "sys", "user", pool_size=2, profile=_profile(), target_chars=120)
    assert p.calls == 2
    assert text == good
    # 第一份失败 ⇒ 只剩一份合格候选（没得择优，但也**不该丢内容**）
    assert info["n_candidates"] == 1
    assert info["n_qualified"] == 1


# ── 5. 全部失败 → 抛错 ────────────────────────────────────────────────────
def test_pool_all_failed_raises():
    p = FakeProvider([_mixed(4)], fail_at={0, 1})
    with pytest.raises(Exception):
        _generate_with_pool(p, "sys", "user", pool_size=2, profile=_profile(), target_chars=120)


# ── 6. 预览变换参与打分，但返回原文 ──────────────────────────────────────
def test_preview_scores_transformed_but_returns_original():
    """候选本身是纯台词；预览把它机械并一遍 → 形态进带 → 应判合格。

    返回的必须是**原文**（真正的校正由流水线自己的工序做）。
    """
    from app.generation.candidate import select_best

    bare = _bare(8)

    def preview(t: str) -> str:
        from app.style.form_merge import merge_bare_dialogue
        from app.style.stylometry import paragraphs

        new_paras, _ = merge_bare_dialogue(paragraphs(t), 0.2)
        return "\n".join(new_paras)

    picked, info = select_best([bare], profile=_profile(), preview=preview)
    assert picked == bare, "必须返回原文，不是预览结果"


# ── 7. 默认关闭 ───────────────────────────────────────────────────────────
def test_pool_size_default_is_one():
    from app.config import settings

    assert int(getattr(settings, "style_candidate_pool", 1) or 1) == 1
    assert _candidate_pool_size() == 1
