"""重生成路径：段长闸门与「先过闸门再吐字」的形状（2026-10-27）。

## 这份测试盯的是两件事

1. **重生成与流式续写同一形状** —— 两条路径都用「先非流式生成 + 过闸门，
   再分块吐出」。理由见 `engine.py` 里那段注释：闸门不够长时会**整段重写**，
   边吐边量会让界面显示一份随后被推翻的稿子。
2. **重生成也报长度报告** —— `done` 事件带 `cjk` 与 `length_report`
   （与其它两条路径同款）。

不依赖真实网络：provider 是假的。
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
    return sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")


class FakeWriter:
    def __init__(self, text: str, expanded: str | None = None):
        self.text = text
        self.expanded = expanded
        self.calls: list[tuple[str, str]] = []
        self.stream_calls = 0

    def chat(self, system, user, **kwargs):
        self.calls.append((system, user))
        if "上一版长度不够" in (user or ""):
            return self.expanded if self.expanded is not None else self.text
        return self.text

    def stream_chat(self, system, user, **kwargs):
        self.stream_calls += 1
        self.calls.append((system, user))
        return iter([self.text])

    def summarize(self, text):
        return "摘要"

    def summarize_rollup(self, *args, **kwargs):
        return "摘要"

    @property
    def n_expand_calls(self) -> int:
        return sum(1 for _s, u in self.calls if "上一版长度不够" in (u or ""))


def _db_with_segment(text: str = "旧正文"):
    """一个项目 + 一段已存在的正文（重生成要替换它）。"""
    from app.generation.timeline import plan_timeline, state_to_dict

    db = Session(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(db.bind)
    st = plan_timeline(3)
    for node in st.nodes.values():
        node.title = "寻找钥匙"
        node.entry_condition = "门锁着，钥匙遗失"
        node.exit_condition = "找到钥匙"
    p = Project(title="重生成闸门", outline=["寻找钥匙"], target_length=3000,
                generation_length=1000, plot_timeline=state_to_dict(st))
    db.add(p)
    db.commit()
    # 重生成要求**段契约**（`planning.segment_contracts`）：没有它会 409
    # （「旧段缺少原始节点契约，请明确复核后重生成」）。
    timeline = dict(p.plot_timeline or {})
    planning = dict(timeline.get("planning") or {})
    planning["segment_contracts"] = {
        "1": {"node_id": "n1", "prompt": "寻找钥匙：门锁着，钥匙遗失。"}
    }
    timeline["planning"] = planning
    p.plot_timeline = timeline
    db.commit()
    # `_commit_segment` 返回 (行, 段号)
    gen, _seg_no = gen_engine._commit_segment(db, p, text, "旧摘要")
    db.refresh(p)
    return db, p, gen


def _run_regen(db, p, gen, writer, monkeypatch):
    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "rebuild_summary_tree", lambda *a, **kw: {})
    return list(gen_engine.regenerate_segment_stream(
        db, p, segment_no=gen.segment_no, novel_ids=[], provider=writer))


# ====================================================================
# 1. 过闸门（原来也有，但形状变了：先过闸门再吐）
# ====================================================================


def test_regenerate_runs_length_gate(monkeypatch):
    """正文偏短 ⇒ 重生成**照样**扩写。"""
    db, p, gen = _db_with_segment()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeWriter(short, expanded=grown)

    events = _run_regen(db, p, gen, w, monkeypatch)

    assert w.n_expand_calls == 1, [u[-40:] for _s, u in w.calls]
    done = next(e for e in events if e.get("type") == "done")
    assert done["length_report"]["cjk"] == _cjk(grown)
    assert done["cjk"] == _cjk(grown)


def test_regenerate_no_expand_when_long_enough(monkeypatch):
    """正文够长 ⇒ 不扩写（闸门不白花钱）。"""
    db, p, gen = _db_with_segment()
    w = FakeWriter("长" * 1200)

    events = _run_regen(db, p, gen, w, monkeypatch)

    assert w.n_expand_calls == 0
    done = next(e for e in events if e.get("type") == "done")
    assert done["length_report"]["below_target"] is False


# ====================================================================
# 2. 不再逐 delta 吐（闸门会整段重写 ⇒ 边吐边量会推翻已吐出的半截）
# ====================================================================


def test_regenerate_does_not_use_stream_chat(monkeypatch):
    """重生成**不再**用 `stream_chat` 逐 delta 生成。"""
    db, p, gen = _db_with_segment()
    w = FakeWriter("长" * 1200)

    _run_regen(db, p, gen, w, monkeypatch)

    assert w.stream_calls == 0, "重生成不该再逐 delta 生成"


def test_regenerate_emits_chunks_after_gate(monkeypatch):
    """chunk 事件照发，且**内容是过闸门后那份**（界面显示的 = 落库的）。"""
    db, p, gen = _db_with_segment()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeWriter(short, expanded=grown)

    events = _run_regen(db, p, gen, w, monkeypatch)

    chunks = [e for e in events if e.get("type") == "chunk"]
    assert chunks, "必须仍有 chunk 事件"
    joined = "".join(c["text"] for c in chunks)
    assert _cjk(joined) == _cjk(grown), "chunk 必须是扩写后那份，不是原来那份短的"
    # 而最终落库的正文也是同一份
    db.refresh(gen)
    assert _cjk(gen.text) == _cjk(grown)


def test_regenerate_persists_text_even_if_summary_tree_fails(monkeypatch):
    """摘要树重建炸了，**重写后的正文也必须已经落库**。

    原来正文是「蹭」`rebuild_summary_tree` 的 commit 存的 —— 它一旦抛错
    （摘要卷 LLM 请求失败等），整段重写就白付一次生成费用。
    """
    db, p, gen = _db_with_segment()
    w = FakeWriter("长" * 1200)

    def _boom(*a, **kw):
        raise RuntimeError("摘要卷 LLM 挂了")

    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "rebuild_summary_tree", _boom)

    try:
        list(gen_engine.regenerate_segment_stream(
            db, p, segment_no=gen.segment_no, novel_ids=[], provider=w))
    except RuntimeError:
        pass
    else:
        raise AssertionError("摘要树重建的异常应当向上抛")

    db.refresh(gen)
    assert _cjk(gen.text) == 1200, "摘要树重建失败不该把已经写好的正文丢掉"
