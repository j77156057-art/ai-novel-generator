"""流式路径的段长闸门（2026-10-27）。

## 这份测试盯的是一件事

**界面主流程走的是流式接口**（前端只调 `/api/generate-stream`），
而段长闸门原来**只在同步接口与重生成接口里跑** ——
流式路径一次都没调用过 ⇒ 界面照收短段，**两条路径口径不一致**。

实测：验收库 9 段真实正文里 **5 段低于闸门下限**（目标 × 80%）。

## 改法与它的代价

改成「**先非流式生成 + 过闸门，再分块吐出**」（与候选池路径同一形状）：

- **为什么不再边生成边吐**：闸门要拿到**整段**才能量字数，
  而不够长时会**整段重写**（`text = grown`）——
  边吐边量的话，先吐出去的那半截会被改写后的正文**推翻**，
  界面就会显示「先一段、然后整段换掉」，比慢一点更糟。
- **代价（如实记下）**：牺牲「长文首字即显」——
  首字延迟从秒级变成「整段生成完」的时长。
  拿体验换「界面落库的段长与同步接口一致」。

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


class FakeStreamWriter:
    """记录收到的 (system, user)；不联网。

    `chat` 是**非流式**调用（闸门与生成都用它），
    `stream_chat` 是逐 delta 的流式调用。
    两条都记下来 ⇒ 能断言「流式路径到底走了哪条」。
    """

    def __init__(self, text: str, expanded: str | None = None):
        self.text = text
        self.expanded = expanded
        self.calls: list[tuple[str, str]] = []
        self.stream_calls = 0

    def chat(self, system, user, **kwargs):
        self.calls.append((system, user))
        # 段长闸门的扩写调用：user 末尾带「上一版长度不够」的回灌提示
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


def _db_and_project():
    from app.generation.timeline import plan_timeline, state_to_dict

    db = Session(create_engine("sqlite:///:memory:"))
    Base.metadata.create_all(db.bind)
    st = plan_timeline(3)
    for node in st.nodes.values():
        node.title = "寻找钥匙"
        node.entry_condition = "门锁着，钥匙遗失"
        node.exit_condition = "找到钥匙"
    p = Project(title="流式闸门", outline=["寻找钥匙"], target_length=3000,
                generation_length=1000, plot_timeline=state_to_dict(st))
    db.add(p)
    db.commit()
    return db, p


def _run_stream(db, p, writer, monkeypatch):
    monkeypatch.setattr(gen_engine, "_candidate_pool_size", lambda: 1)
    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: {})
    return list(gen_engine.generate_next_segment_stream(db, p, novel_ids=[], provider=writer))


# ====================================================================
# 1. 流式路径现在会过闸门（原来一次都没有）
# ====================================================================


def test_stream_path_runs_length_gate(monkeypatch):
    """正文偏短 ⇒ 流式路径**照样**扩写（原来照收短段）。"""
    db, p = _db_and_project()
    short = "短" * 100
    grown = "长" * 1200
    w = FakeStreamWriter(short, expanded=grown)

    events = _run_stream(db, p, w, monkeypatch)

    assert w.n_expand_calls == 1, [u[-40:] for _s, u in w.calls]
    done = next(e for e in events if e.get("type") == "done")
    # 落库的正文是**扩写后**那份（不是原来那份短的）
    assert done["length_report"]["cjk"] == _cjk(grown)
    assert done["length_report"]["below_floor"] is False
    assert done["cjk"] == _cjk(grown)


def test_stream_path_no_expand_when_long_enough(monkeypatch):
    """正文够长 ⇒ 不扩写（闸门不白花钱）。"""
    db, p = _db_and_project()
    long_text = "长" * 1200
    w = FakeStreamWriter(long_text)

    events = _run_stream(db, p, w, monkeypatch)

    assert w.n_expand_calls == 0
    done = next(e for e in events if e.get("type") == "done")
    assert done["length_report"]["cjk"] == _cjk(long_text)
    assert done["length_report"]["below_target"] is False


# ====================================================================
# 2. 走的是非流式生成（闸门要整段才能量长度）
# ====================================================================


def test_stream_path_uses_non_stream_generation(monkeypatch):
    """流式路径**不再**用 `stream_chat` 逐 delta 吐字。

    为什么：闸门不够长时会**整段重写** ⇒ 边吐边量的话，
    先吐出去的那半截会被推翻 ⇒ 界面显示「先一段、然后整段换掉」。
    """
    db, p = _db_and_project()
    w = FakeStreamWriter("长" * 1200)

    _run_stream(db, p, w, monkeypatch)

    assert w.stream_calls == 0, "流式路径不该再逐 delta 生成"


def test_stream_path_still_emits_chunks(monkeypatch):
    """**分块吐出**这件事没丢（前端按 chunk 增量渲染）。"""
    db, p = _db_and_project()
    w = FakeStreamWriter("长" * 1200)

    events = _run_stream(db, p, w, monkeypatch)

    chunks = [e for e in events if e.get("type") == "chunk"]
    assert chunks, "必须仍有 chunk 事件"
    assert "".join(c["text"] for c in chunks) == "长" * 1200


# ====================================================================
# 3. 与同步路径口径一致
# ====================================================================


def test_sync_and_stream_report_same_length(monkeypatch):
    """同步与流式两条路径**同一把尺** ⇒ 同样的正文 ⇒ 同样的长度报告。

    这是这次改动的目的：界面落库的段长与同步接口一致。

    注：同步与流式**各用各的项目** —— 同一个项目连跑两次会撞上
    时间线状态机（第二段需要审核，抛 409），那是状态机该有的行为，
    与本次改动无关。
    """
    text = "长" * 1200

    db_sync, p_sync = _db_and_project()
    w_sync = FakeStreamWriter(text)
    monkeypatch.setattr(gen_engine, "_candidate_pool_size", lambda: 1)
    monkeypatch.setattr(gen_engine, "_style_profile", lambda *a, **k: None)
    monkeypatch.setattr(gen_engine, "_enqueue_v3_extraction", lambda *a, **kw: {})
    monkeypatch.setattr(gen_engine, "_schedule_async_audit_kg", lambda *a, **kw: {})
    sync_out = gen_engine.generate_next_segment(db_sync, p_sync, novel_ids=[], provider=w_sync)

    db_stream, p_stream = _db_and_project()
    w_stream = FakeStreamWriter(text)
    events = _run_stream(db_stream, p_stream, w_stream, monkeypatch)
    done = next(e for e in events if e.get("type") == "done")

    assert sync_out["length"]["cjk"] == done["length_report"]["cjk"]
    assert sync_out["length"]["floor_cjk"] == done["length_report"]["floor_cjk"]
