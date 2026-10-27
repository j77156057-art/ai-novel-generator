"""候选池的择优结论，到落库那一刻还成立吗（2026-10-27 起）。

## 这份测试盯的是三件事

1. **闸门在池内跑** —— 走链之前先过段长闸门 ⇒ 走链的对象 = 落库的对象。
   （原来闸门排在打分之后，被择优认可的稿子会被**整段重写**换掉。）
2. **上层不重复扩写** —— 池内已经过过闸门，流水线后面不能再跑一遍
   （第二次扩写 = 白花钱，而且两次结果不同 ⇒ 落库的又不是池选中的那份）。
3. **末端择优** —— 落库前用**同一把尺**量**成品**；不合格就换下一份候选。

外加一条**不能破的底线**：`pool_size = 1`（默认，候选池关着）时，
**默认路径逐字节不变** —— 池内闸门与末端判定都不得触发。

不依赖真实网络：provider 是假的（只记录收到的提示词），DB 用内存 sqlite。
"""

from __future__ import annotations

import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import Base  # noqa: E402
from app.generation import engine as gen_engine  # noqa: E402
from app.models import Project  # noqa: E402


def _cjk(text: str) -> int:
    return sum(1 for ch in (text or "") if "一" <= ch <= "鿿")


class FakeWriter:
    """记录收到的 (system, user)；不联网。"""

    def __init__(self, text: str, expanded: str | None = None):
        self.text = text
        self.expanded = expanded
        self.calls: list[tuple[str, str]] = []

    def chat(self, system, user, **kwargs):
        self.calls.append((system, user))
        # 段长闸门的扩写调用：user 末尾带「上一版长度不够」的回灌提示
        if "上一版长度不够" in (user or ""):
            return self.expanded if self.expanded is not None else self.text
        return self.text

    def summarize(self, text):
        return "摘要"

    def summarize_rollup(self, *args, **kwargs):
        return "摘要"

    @property
    def n_expand_calls(self) -> int:
        return sum(1 for _s, u in self.calls if "上一版长度不够" in (u or ""))


class SequencedWriter(FakeWriter):
    """每次**采样**返回下一段预置文本（扩写调用仍走 `expanded`）。"""

    def __init__(self, outputs, expanded=None):
        super().__init__(outputs[0] if outputs else "", expanded=expanded)
        self.outputs = list(outputs)
        self.sample_calls = 0

    def chat(self, system, user, **kwargs):
        if "上一版长度不够" in (user or ""):
            self.calls.append((system, user))
            return self.expanded if self.expanded is not None else self.text
        self.calls.append((system, user))
        idx = self.sample_calls
        self.sample_calls += 1
        if not self.outputs:
            return ""
        return self.outputs[idx % len(self.outputs)]


# ====================================================================
# 1. 默认路径（候选池关着）必须逐字节不变
# ====================================================================


def test_pool_disabled_keeps_default_path_untouched():
    """pool_size=1 ⇒ 不采样、不打分、不闸门、不判定（默认路径一个字节都不变）。"""
    w = FakeWriter("正文" * 50)
    text, info, payload = gen_engine._run_pool_selection(
        w, "sys", "user", pool_size=1, profile=None, target_chars=1000,
        run_chain=lambda t: (t, "摘要", {}),
    )
    assert info == {"applied": False, "reason": "候选池关闭"}
    assert payload == {}
    assert text == ""
    assert len(w.calls) == 0          # 一次生成调用都没有
    assert w.n_expand_calls == 0     # 闸门不在池内跑（由流水线自己那道负责）


def _db_and_project():
    from app.generation.timeline import plan_timeline, state_to_dict

    db = Session(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(db.bind)
    st = plan_timeline(3)
    for node in st.nodes.values():
        node.title = "寻找钥匙"
        node.entry_condition = "门锁着，钥匙遗失"
        node.exit_condition = "找到钥匙"
    p = Project(title="池口径", outline=["寻找钥匙"], target_length=3000,
                generation_length=1000, plot_timeline=state_to_dict(st))
    db.add(p)
    db.commit()
    return db, p


def _patch_common(monkeypatch, pool_size: int):
    monkeypatch.setattr(gen_engine, "_candidate_pool_size", lambda: pool_size)
    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: None)


# ====================================================================
# 2. 闸门在池内跑（走链之前）+ 上层不重复扩写
# ====================================================================


def test_sync_path_does_not_expand_twice(monkeypatch):
    """池内已过闸门 ⇒ 流水线后面那道闸门不能再跑（否则白花一次扩写）。"""
    db, p = _db_and_project()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeWriter(short, expanded=grown)

    _patch_common(monkeypatch, 2)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    # 扩写只发生了一次（池内那次）—— 池外那道闸门被跳过
    assert w.n_expand_calls == 1, [u[-40:] for _s, u in w.calls]
    assert out["candidate_pool"]["gated"] is True
    # 落库的正文就是走链的那份（扩写后）
    assert _cjk(out["text"]) == _cjk(grown)


def test_sync_path_pool_off_still_runs_gate_after(monkeypatch):
    """默认路径（池关着）仍然由流水线自己那道闸门管长度（行为不变）。"""
    db, p = _db_and_project()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeWriter(short, expanded=grown)

    _patch_common(monkeypatch, 1)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    assert w.n_expand_calls == 1
    assert out["candidate_pool"]["applied"] is False
    assert _cjk(out["text"]) == _cjk(grown)
    # 池关着 ⇒ 没有末端判定（零成本）
    assert "final_recheck" not in out["candidate_pool"]


def test_default_path_never_judges(monkeypatch):
    """默认路径**不触发**末端判定（连尺子都不拿出来量）。"""
    db, p = _db_and_project()
    w = FakeWriter("正文" * 50)

    def _boom(*a, **kw):  # pragma: no cover —— 被调用就说明默认路径变了
        raise AssertionError("默认路径不该触发末端判定")

    monkeypatch.setattr(gen_engine, "_judge_final_text", _boom)
    _patch_common(monkeypatch, 1)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    assert out["candidate_pool"] == {"applied": False, "reason": "候选池关闭"}


# ====================================================================
# 3. 末端择优：落库的那份 = 尺子判过的那份
# ====================================================================


def test_sync_path_commits_the_qualified_final(monkeypatch):
    """第一份成品不合格 ⇒ 换第二份；**落库的是第二份**（不是第一份）。

    这里把尺子换成「文本里带 GOOD 才算合格」的假尺子 ——
    验的是**接线**（判决发生在链之后、决定落库哪一份），尺子本身另有单测。
    """
    db, p = _db_and_project()
    w = SequencedWriter(["坏稿子", "好稿子 GOOD"])

    def _judge(text, **kw):
        ok = "GOOD" in (text or "")
        return {"checked": True, "ok": ok,
                "reject": "" if ok else "形态不在参考书分布带内",
                "score": 0.0 if ok else 0.5, "form": {}}

    monkeypatch.setattr(gen_engine, "_judge_final_text", _judge)
    _patch_common(monkeypatch, 2)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    assert out["text"] == "好稿子 GOOD", "落库的必须是**尺子判过的那份**"
    pool = out["candidate_pool"]
    assert pool["accepted"] is True
    assert pool["n_tried"] == 2
    assert pool["picked"] == 1
    assert [a["ok"] for a in pool["attempts"]] == [False, True]


def test_sync_path_pool_fallback_reports_deviation(monkeypatch):
    """都不合格 ⇒ 落库最接近的一份，并如实标注偏离（不假装成功）。"""
    db, p = _db_and_project()
    w = SequencedWriter(["甲稿子", "乙稿子"])

    def _judge(text, **kw):
        # 甲更接近目标（分更小）⇒ 兜底时选它
        score = 0.1 if "甲" in (text or "") else 0.4
        return {"checked": True, "ok": False, "reject": "形态不在参考书分布带内",
                "score": score, "form": {}}

    monkeypatch.setattr(gen_engine, "_judge_final_text", _judge)
    _patch_common(monkeypatch, 2)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    assert out["text"] == "甲稿子"
    pool = out["candidate_pool"]
    assert pool["accepted"] is False
    assert "无合格候选" in pool["reason"]


def test_stream_path_emits_exactly_the_committed_text(monkeypatch):
    """流式路径：吐出来的字与落库的**逐字相同**（池路径先择优、再吐字）。"""
    db, p = _db_and_project()
    w = SequencedWriter(["甲稿子", "乙稿子 GOOD"])

    def _judge(text, **kw):
        ok = "GOOD" in (text or "")
        return {"checked": True, "ok": ok,
                "reject": "" if ok else "形态不在参考书分布带内",
                "score": 0.0 if ok else 0.5, "form": {}}

    monkeypatch.setattr(gen_engine, "_judge_final_text", _judge)
    _patch_common(monkeypatch, 2)

    events = list(gen_engine.generate_next_segment_stream(
        db, p, novel_ids=[], provider=w))
    chunks = "".join(e.get("text", "") for e in events if e.get("type") == "chunk")
    done = [e for e in events if e.get("type") == "done"][0]
    # 落库的那份（从库里读，别信事件里的自述）
    from app.models import Generation

    committed = db.query(Generation).filter(
        Generation.project_id == p.id).order_by(Generation.id.desc()).first().text
    assert chunks == committed, "流式吐出来的必须是落库的那一份"
    assert committed == "乙稿子 GOOD"
    assert done["candidate_pool"]["accepted"] is True
