"""候选池接进引擎（`app/generation/engine.py`）

## 这一版测的是**末端择优**的新契约

候选池以前是「采 N 份 → 按**链前**的形态预览挑一份 → 那份走完整条链 → 落库」。
⇒ 落库的与挑中的**从来不是同一份**（链上 7 道工序都会改文本）。

现在改成：**采一份 → 走完整条链 → 用同一把尺量**成品** → 合格就落库，
不合格才采下一份**（拒绝采样跑在整条链上）。
⇒ **落库的那份，就是尺子判过的那份。**

覆盖：

1. `pool_size=1`（默认）→ **不进池**，一次模型调用不多，行为与从前完全一致；
2. 池开着 → 采样 → 走链 → 量成品 → 落库**第一份合格的**；
3. 第一份成品不合格 → 才采下一份、走下一条链（**按需**，不为用不上的候选花钱）；
4. 都不合格 → 落库**离目标最近**的一份，并如实标注偏离（下限兜底）；
5. 段长闸门在池内跑（打分/走链之前）—— 闸门会整段重写，放在后面口径就断了；
6. 一份采样失败 → 跳过它；全部失败 → 抛错（不吞错误）；
7. 候选池默认关闭（`settings.style_candidate_pool == 1`）。

用的是**假 provider**（不调真模型）与**假走链** —— 这里验的是接线与判据，
不是模型质量。
"""
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.generation.engine import (  # noqa: E402
    _candidate_pool_size,
    _judge_final_text,
    _run_pool_selection,
)


class FakeProvider:
    """按顺序返回预设文本，并记录调用次数。

    ## 为什么要区分「采样」与「扩写」两种调用

    段长闸门在字数不够时会回灌让模型重写整段（`_enforce_length` 里
    `text = grown`）⇒ 池的一次采样可能带出**额外的扩写调用**，它也会调 `chat`。

    原来的夹具把两种调用混在一起数 ⇒ 扩写调用把「预置文本」的序号打乱 ⇒
    断言测到的东西变了。现在：**采样**与**扩写**分开计数、分开取文本，
    `fail_at` 也按**采样序号**判定（扩写不参与失败模拟）。
    """

    def __init__(self, outputs=None, fail_at=None, expansion=None):
        self.outputs = list(outputs or [])
        self.fail_at = fail_at
        #: 闸门扩写时返回什么。默认 None ⇒ 返回空串
        #: （模拟「扩写没帮上忙」⇒ 闸门保留原稿，候选仍是原来那份）。
        self.expansion = expansion
        self.calls = 0        # 总调用数（采样 + 扩写）
        self.sample_calls = 0 # 采样调用数
        self.expand_calls = 0 # 闸门扩写调用数
        self.seen = []

    def chat(self, system, user, constraints=None, stop_sequences=None, temperature=None):
        self.calls += 1
        self.seen.append((system, user))
        # 闸门扩写的回灌提示里有这句（`_enforce_length` 里的 hint）
        if "上一版长度不够" in (user or ""):
            self.expand_calls += 1
            if self.expansion is not None:
                return self.expansion
            return ""
        idx = self.sample_calls
        self.sample_calls += 1
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

    为什么需要它：守恒判据拿「同伴字数中位数」当基准时，
    两份候选字数差太多，长的那份会被判成「膨胀」而误杀 ——
    那道判据是**防凭空扩写**的，不该被长度差本身触发。
    """
    base = "\n".join(f"“第{i}句话。”" for i in range(n))
    return "\n".join(
        f"他看了看窗外的天色，心里盘算着接下来的事。{base.split(chr(10))[i]}"
        for i in range(n)
    )


def _chain_stub(final_by_candidate):
    """假走链：按第几次被调用返回对应的成品。

    返回 (final_text, summary, chain_infos) —— 与 `_run_text_chain` 同形。
    """
    calls: list[str] = []

    def _run_chain(text: str):
        idx = len(calls)
        calls.append(text)
        final = final_by_candidate[idx] if idx < len(final_by_candidate) else final_by_candidate[-1]
        return final, "摘要", {"form_info": {}}

    _run_chain.calls = calls
    return _run_chain


# ── 1. 默认关闭：一次调用不多 ────────────────────────────────────────────
def test_pool_disabled_does_not_enter_the_pool():
    p = FakeProvider([_mixed(4)])
    text, info, payload = _run_pool_selection(
        p, "sys", "user", pool_size=1, profile=_profile(), run_chain=_chain_stub([_mixed(4)]))
    assert p.calls == 0, "池关着时一次模型调用都不该多"
    assert info == {"applied": False, "reason": "候选池关闭"}
    assert payload == {}
    assert text == ""


# ── 2. 落库的是「尺子判过的那份」 ─────────────────────────────────────────
def test_pool_commits_the_first_qualified_final():
    """第一份**成品**合格 ⇒ 落库它，且不再采样、不再走链。"""
    good = _mixed(6)
    p = FakeProvider([_bare(6)])
    chain = _chain_stub([good])

    text, info, _payload = _run_pool_selection(
        p, "sys", "user", pool_size=3, profile=_profile(), run_chain=chain)

    assert text == good, "落库的必须是成品（链跑完的那份），不是采样原稿"
    assert info["applied"] is True
    assert info["accepted"] is True
    assert info["n_tried"] == 1
    assert len(chain.calls) == 1, "合格就停，别再跑第二条链"
    assert p.sample_calls == 1, "合格就停，别再采样"


def test_pool_runs_the_next_chain_only_when_the_first_final_fails():
    """第一份成品不合格 ⇒ 才采下一份、走下一条链。"""
    bad, good = _bare(6), _mixed(6)
    p = FakeProvider([bad, good])
    chain = _chain_stub([bad, good])

    text, info, _payload = _run_pool_selection(
        p, "sys", "user", pool_size=3, profile=_profile(), run_chain=chain)

    assert text == good
    assert info["n_tried"] == 2
    assert info["picked"] == 1, "选中的是第二份候选"
    assert info["accepted"] is True
    assert p.sample_calls == 2
    # 每份候选的末端判定都留痕
    assert [a["ok"] for a in info["attempts"]] == [False, True]
    assert "形态不在参考书分布带内" in info["attempts"][0]["reject"]


def test_pool_falls_back_to_the_closest_final_when_none_qualifies():
    """都不合格 ⇒ 落库**离目标最近**的那份，并如实标注偏离（下限兜底）。"""
    far = _bare(6)                       # 纯台词 100% ⇒ 离目标最远
    near = "\n".join([_mixed(3), _bare(3)])   # 混合 ⇒ 离目标近一些
    p = FakeProvider([_bare(6), _bare(6)])
    chain = _chain_stub([far, near])

    text, info, _payload = _run_pool_selection(
        p, "sys", "user", pool_size=2, profile=_profile(), run_chain=chain)

    assert info["accepted"] is False
    assert "无合格候选" in info["reason"]
    assert info["n_tried"] == 2, "不合格就得把候选试完"
    assert text == near, "兜底要选**离目标最近**的那份，不是第一份"


# ── 3. 段长闸门在池内跑（打分与走链之前） ─────────────────────────────────
def test_length_gate_runs_inside_pool():
    """候选偏短 ⇒ 池内先扩写再走链（走链的对象 = 落库的对象）。"""
    short = "短" * 100                       # 100 字，远低于目标 1000
    grown = "长" * 1200                      # 扩写后的稿子
    p = FakeProvider([short], expansion=grown)
    chain = _chain_stub([grown])

    text, info, _payload = _run_pool_selection(
        p, "sys", "user", pool_size=2, profile=_profile(),
        target_chars=1000, run_chain=chain)

    assert p.expand_calls == 1            # 闸门确实在池内跑了
    assert info["gated"] is True           # 上层据此不再重复扩写
    assert info["length_report"]["cjk"] == len(grown)
    assert chain.calls == [grown], "走链的必须是**扩写后**那份"
    assert text == grown


def test_pool_without_target_does_not_run_gate():
    """没有字数目标就没法量 ⇒ 不跑闸门，也不假装跑过。"""
    p = FakeProvider(["正文" * 50])
    chain = _chain_stub(["正文" * 50])

    _text, info, _payload = _run_pool_selection(
        p, "sys", "user", pool_size=2, profile=_profile(),
        target_chars=None, run_chain=chain)

    assert p.expand_calls == 0
    assert info.get("gated") is False
    assert info.get("length_report") in ({}, None)


# ── 4. 失败策略 ───────────────────────────────────────────────────────────
def test_pool_skips_failed_sample():
    """一份采样失败 → 跳过它，继续下一份。"""
    good = _mixed(6)
    p = FakeProvider([_bare(6), good], fail_at={0})
    chain = _chain_stub([good])

    text, info, _payload = _run_pool_selection(
        p, "sys", "user", pool_size=2, profile=_profile(), run_chain=chain)

    assert text == good
    assert info["n_tried"] == 1
    assert info["picked"] == 1, "picked 是**采样序号**（第几次采样），不是候选序号"


def test_pool_all_failed_raises():
    """全部采样失败 → 抛错（让上层看到真实错误，而不是空正文）。"""
    p = FakeProvider([_mixed(4)], fail_at={0, 1})
    with pytest.raises(Exception):
        _run_pool_selection(p, "sys", "user", pool_size=2, profile=_profile(),
                            run_chain=_chain_stub([_mixed(4)]))


# ── 5. 末端那把尺：只判形态 ───────────────────────────────────────────────
def test_final_judge_flags_out_of_band_text():
    """落库形态跑出带外 ⇒ 末端判定如实报不合格。"""
    text = _bare(12)
    res = _judge_final_text(text, profile=_profile(), target_chars=120)
    assert res["checked"] is True
    assert res["ok"] is False
    assert "形态不在参考书分布带内" in res["reject"]


def test_final_judge_passes_for_in_band_text():
    """形态在带内 ⇒ 末端判定通过。

    注意这条尺的口径：纯台词段占比 = 「含引号的段里，光秃秃一句台词的占多少」。
    全部都是叙述+台词同段 ⇒ 占比 **0%**（也在带外！带是 10%~35%）；
    要落在带内，得**混合**：8 段含引号的段里 2 段是光台词 ⇒ 25%。
    """
    lines = [f"他抬头看了看窗外的雨（第{i}段叙述），然后说：“走吧。”" for i in range(6)]
    lines += ["“别动。”", "“客人？”"]          # 两段光秃秃的台词
    text = "\n".join(lines)
    res = _judge_final_text(text, profile=_profile(), target_chars=120)
    assert res["checked"] is True
    assert res["ok"] is True, res["reject"]
    assert res["form"]["value"] == 0.25


def test_final_judge_reports_length_without_judging_it():
    """字数只**报出**，不参与末端裁决（闸门已经管过长度）。

    为什么：文风改写会压缩 15%~25% ⇒ 链后的文本常落在目标的 60%~80%，
    正好压在池的守恒下限附近 ⇒ 用它裁决链后的文本会**系统性误杀**。
    """
    short = "字" * 60          # 目标 120 → 只有一半（守恒判据会判「字数不足」）
    res = _judge_final_text(short, profile=_profile(), target_chars=120)
    assert res["length_ratio"] == 0.5
    assert "字数不足" not in (res["reject"] or "")


# ── 6. 默认关闭 ───────────────────────────────────────────────────────────
def test_pool_size_default_is_one():
    from app.config import settings

    assert int(getattr(settings, "style_candidate_pool", 1) or 1) == 1
    assert _candidate_pool_size() == 1
