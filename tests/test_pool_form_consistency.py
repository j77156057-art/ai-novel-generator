"""候选池的择优结论，到落库那一刻还成立吗（2026-10-27）。

## 这份测试盯的是三件事

1. **闸门在池内跑** —— 打分之前先过段长闸门 ⇒ 择优的对象 = 落库的对象。
   （原来闸门排在打分之后，被择优认可的稿子会被**整段重写**换掉。）
2. **上层不重复扩写** —— 池内已经过过闸门，流水线后面不能再跑一遍
   （第二次扩写 = 白花钱，而且两次结果不同 ⇒ 落库的又不是池选中的那份）。
3. **末端复判留痕** —— 落库前用同一把尺复判一次；失效了要**看得见**
   （`pool_info["final_recheck"]`），而不是静默。

外加一条**不能破的底线**：`pool_size = 1`（默认，候选池关着）时，
**默认路径逐字节不变** —— 池内闸门与末端复判都不得触发。

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


#: 一份**最简 v1 档案**：只需要这一项特征，带是 p10~p90。
#: unit_chars 故意设成测试正文的长度 ⇒ 长度归一化因子 ≈ 1（不缩放），
#: 于是「带内/带外」的判定就是字面意义上的占比判定。
def _profile(unit_chars: int) -> dict:
    return {
        "version": 1,
        "unit_chars": unit_chars,
        "feat": {
            "纯台词段占比": {"mean": 0.22, "p10": 0.10, "p90": 0.35},
        },
    }


def _bare_dialogue(lines: int = 12) -> str:
    """全是「光秃秃一句台词」的段落 ⇒ 纯台词段占比 100%。"""
    return "\n".join(f"“第{i}句台词。”" for i in range(lines))


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


# ====================================================================
# 1. 默认路径（候选池关着）必须逐字节不变
# ====================================================================


def test_pool_disabled_keeps_default_path_untouched():
    """pool_size=1 ⇒ 不评分、不闸门、不复判（默认路径一个字节都不变）。"""
    w = FakeWriter("正文" * 50)
    text, info = gen_engine._generate_with_pool(
        w, "sys", "user", pool_size=1, profile=None, target_chars=1000,
    )
    assert info == {"applied": False, "reason": "候选池关闭"}
    assert "gated" not in info
    assert "final_recheck" not in info
    assert len(w.calls) == 1          # 一次生成调用，没有多余的
    assert w.n_expand_calls == 0     # 闸门不在池内跑（由流水线自己那道负责）


def test_recheck_is_noop_when_pool_off():
    """池关着时没有「择优结论」可失效 ⇒ 复判不做（零成本）。"""
    pool_info = {"applied": False, "reason": "候选池关闭"}
    res = gen_engine._recheck_pool_verdict(
        pool_info, "正文" * 50, profile=_profile(100), target_chars=1000)
    assert res == {}
    assert "final_recheck" not in pool_info


# ====================================================================
# 2. 闸门在池内跑（打分之前）
# ====================================================================


def test_length_gate_runs_inside_pool():
    """候选偏短 ⇒ 池内先扩写再打分（打分对象 = 落库对象）。"""
    short = "短" * 100                       # 100 字，远低于目标 1000
    grown = "长" * 1200                      # 扩写后的稿子
    w = FakeWriter(short, expanded=grown)
    text, info = gen_engine._generate_with_pool(
        w, "sys", "user", pool_size=1 + 1, profile=None, target_chars=1000,
    )
    assert info["applied"] is True
    assert info["gated"] is True            # 上层据此不再重复扩写
    assert w.n_expand_calls == 1            # 闸门确实在池内跑了
    assert info["length_report"]["cjk"] == _cjk(grown)
    assert _cjk(text) == _cjk(grown)        # 池交出的是**扩写后**那份


def test_pool_without_target_does_not_run_gate():
    """没有字数目标就没法量 ⇒ 不跑闸门，也不假装跑过。"""
    w = FakeWriter("正文" * 50)
    _text, info = gen_engine._generate_with_pool(
        w, "sys", "user", pool_size=2, profile=None, target_chars=None,
    )
    assert w.n_expand_calls == 0
    assert info.get("length_report") in ({}, None)


# ====================================================================
# 3. 末端复判：失效要留痕
# ====================================================================


def test_final_recheck_flags_out_of_band_text():
    """落库形态跑出带外 ⇒ 复判如实记下（不阻断落库，但必须看得见）。"""
    text = _bare_dialogue(12)
    pool_info = {"applied": True, "gated": True}
    # 字数基准取正文自身长度 ⇒ 守恒判据必过，复判看的就**只有形态**
    res = gen_engine._recheck_pool_verdict(
        pool_info, text, profile=_profile(_cjk(text)), target_chars=_cjk(text))
    assert res["checked"] is True
    assert res["ok"] is False
    assert "形态不在参考书分布带内" in res["reject"]
    # 留痕在 pool_info 里（前端/日志能拿到）
    assert pool_info["final_recheck"] is res


def test_final_recheck_passes_for_in_band_text():
    """形态在带内 ⇒ 复判通过。

    注意这条尺的口径：纯台词段占比 = 「含引号的段里，光秃秃一句台词的占多少」。
    全部都是叙述+台词同段 ⇒ 占比 **0%**（也在带外！带是 10%~35%）；
    要落在带内，得**混合**：8 段含引号的段里 2 段是光台词 ⇒ 25%。
    """
    lines = [f"他抬头看了看窗外的雨（第{i}段叙述），然后说：“走吧。”" for i in range(6)]
    lines += ["“别动。”", "“客人？”"]          # 两段光秃秃的台词
    text = "\n".join(lines)
    pool_info = {"applied": True}
    res = gen_engine._recheck_pool_verdict(
        pool_info, text, profile=_profile(_cjk(text)), target_chars=_cjk(text))
    assert res["checked"] is True
    assert res["ok"] is True, res["reject"]
    # 25% 落在带内（10%~35%）
    assert res["form"]["value"] == 0.25


# ====================================================================
# 4. 整条生成路径：池开着时不再重复扩写
# ====================================================================


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


def test_sync_path_does_not_expand_twice(monkeypatch):
    """池内已过闸门 ⇒ 流水线后面那道闸门不能再跑（否则白花一次扩写）。"""
    db, p = _db_and_project()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeWriter(short, expanded=grown)

    monkeypatch.setattr(gen_engine, "_candidate_pool_size", lambda: 2)
    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: None)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    # 扩写只发生了一次（池内那次）—— 池外那道闸门被跳过
    assert w.n_expand_calls == 1, [u[-40:] for _s, u in w.calls]
    assert out["candidate_pool"]["gated"] is True
    # 落库的正文就是池交出来的那份（扩写后）
    assert _cjk(out["text"]) == _cjk(grown)


def test_sync_path_pool_off_still_runs_gate_after(monkeypatch):
    """默认路径（池关着）仍然由流水线自己那道闸门管长度（行为不变）。"""
    db, p = _db_and_project()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeWriter(short, expanded=grown)

    monkeypatch.setattr(gen_engine, "_candidate_pool_size", lambda: 1)
    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: None)

    out = gen_engine.generate_next_segment(db, p, novel_ids=[], provider=w)
    assert w.n_expand_calls == 1
    assert out["candidate_pool"]["applied"] is False
    assert _cjk(out["text"]) == _cjk(grown)
    # 池关着 ⇒ 没有复判（零成本）
    assert "final_recheck" not in out["candidate_pool"]
