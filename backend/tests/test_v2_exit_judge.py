"""v2.1：exit_condition LLM 判定测试（不依赖真实 LLM）。

覆盖：
- judge_node_completion 的三类返回（True/False/None）的解析
  - 容错：```json 包裹 / 非 JSON / 缺 done 键 / done 是字符串 → 全部 None
  - 边界：node.exit_condition 为空 → 立刻 None
  - 边界：recent_text 空 → 立刻 None
  - 边界：provider.chat 异常 → None
- force_complete_current 纯函数行为：
  - 标 done 当前节点 + 切到下一主干节点 + 重置 current_beat
  - 当前节点已 done → noop
  - 已是最后一节点 → current_node=None
  - current_node 不在 order 里 → 按 noop 处理（保守）
- _judge_and_advance 集成行为（用 mock provider + 内存 sqlite + Project）：
  - judge=True → current_node 切到下一个、存档里 done 加一
  - judge=False → 只 advance 一次，seg_counts +1
  - judge=None → 只 advance 一次，seg_counts +1，不报错
  - 没 outline → skipped=True

跑法：python tests/test_v2_exit_judge.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.generation.timeline import (
    PlotNode, TimelineState,
    force_complete_current, judge_node_completion, plan_timeline,
)


PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    tag = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"{tag}  {label}: got={got!r} want={want!r}")


class FakeProvider:
    """可预设回复 + 标记被调次数的假 provider。"""

    def __init__(self, reply: str = '{"done": false}', raise_exc: bool = False):
        self.reply = reply
        self.raise_exc = raise_exc
        self.call_count = 0
        self.last_system = None
        self.last_user = None

    def chat(self, system: str, user: str) -> str:
        self.call_count += 1
        self.last_system = system
        self.last_user = user
        if self.raise_exc:
            raise RuntimeError("mock LLM error")
        return self.reply


# ── 组 1：judge_node_completion 三类返回 + 容错 ──

def test_judge_basic_returns():
    print("\n=== 组 1: judge_node_completion 三类返回 ===")
    node = PlotNode(
        id="n1", kind="main", title="入职", emotion="爽",
        entry_condition="刚入职", exit_condition="升部门经理",
        beats=[], opens=[], closes=[], segment_budget=2,
    )
    text = "林悠然通过试用期，升任部门经理。"

    # True
    p = FakeProvider('{"done": true}')
    check("True 解析", judge_node_completion(p, node, text), True)
    check("True 时 LLM 被调一次", p.call_count, 1)

    # False
    p = FakeProvider('{"done": false}')
    check("False 解析", judge_node_completion(p, node, text), False)

    # None（缺 done 键）
    p = FakeProvider('{"other": true}')
    check("缺 done 键 → None", judge_node_completion(p, node, text), None)

    # None（done 是字符串）
    p = FakeProvider('{"done": "yes"}')
    check("done 是 string → None", judge_node_completion(p, node, text), None)

    # None（裸 true/false 无 JSON 包裹 + 找不到 {} 块）
    p = FakeProvider("判断完成。")
    check("非 JSON → None", judge_node_completion(p, node, text), None)

    # ```json 包裹
    p = FakeProvider('```json\n{"done": true}\n```')
    check("```json 包裹 → True", judge_node_completion(p, node, text), True)

    # 异常
    p = FakeProvider(raise_exc=True)
    check("provider 异常 → None", judge_node_completion(p, node, text), None)

    # exit_condition 为空 → 不调 LLM
    no_exit_node = PlotNode(
        id="n1", kind="main", title="x", emotion="爽",
        entry_condition="", exit_condition="", segment_budget=2,
    )
    p = FakeProvider('{"done": true}')
    check("无 exit_condition → None（不调 LLM）", judge_node_completion(p, no_exit_node, text), None)
    check("无 exit 时 LLM 未被调", p.call_count, 0)

    # recent_text 空 → 不调 LLM
    p = FakeProvider('{"done": true}')
    check("recent_text 空 → None", judge_node_completion(p, node, ""), None)
    check("空文本时 LLM 未被调", p.call_count, 0)


# ── 组 2：force_complete_current 纯函数行为 ──

def test_force_complete_basic():
    print("\n=== 组 2: force_complete_current 行为 ===")
    st = plan_timeline(total_segments=12)
    check("初始 current_node = n1", st.current_node, "n1")
    check("初始 done 空", sorted(st.done), [])

    st2 = force_complete_current(st)
    check("force 后 current_node = n2", st2.current_node, "n2")
    check("force 后 done = [n1]", sorted(st2.done), ["n1"])
    check("force 后 current_beat = 0", st2.current_beat, 0)
    check("force 后 total_main 不变", st2.total_main, st.total_main)

    # 原 state 不变（纯函数）
    check("原 state current_node 仍是 n1", st.current_node, "n1")
    check("原 state done 仍空", sorted(st.done), [])

    # 重复 force 同一节点：先用 noop_node（手工把 n1 标 done）测 noop
    st_done = plan_timeline(total_segments=12)
    st_done.done.add("n1")
    st_done.current_node = "n1"
    st3 = force_complete_current(st_done)
    check("已 done 节点 force = noop（current 不变）", st3.current_node, "n1")
    check("已 done 节点 force = noop（done 不重复加）", sorted(st3.done), ["n1"])

    # 正常推进路径：force n1 后 current 是 n2，再 force n2 → n3（这是按设计走的）
    st4 = force_complete_current(st2)
    check("连续 force 推进：n2 → n3", st4.current_node, "n3")
    check("连续 force 推进：done=[n1,n2]", sorted(st4.done), ["n1", "n2"])


def test_force_complete_last_node():
    print("\n=== 组 3: force 最后一节点 → current_node=None ===")
    st = plan_timeline(total_segments=12)
    # 把 current 推到最后一节点
    for _ in range(len(st.order)):
        st = force_complete_current(st)
    check("全部 force 后 current_node = None", st.current_node, None)
    check("全部 force 后 done = 全部", sorted(st.done), sorted(st.order))


def test_force_complete_unknown_current():
    print("\n=== 组 4: current_node 不在 order/nodes（异常态）=== ")
    st = plan_timeline(total_segments=12)
    st.current_node = "nXXX"  # 异常态
    st2 = force_complete_current(st)
    check("未知 current_node 保持（不爆）", st2.current_node, "nXXX")
    check("未知 current_node 不加 done", sorted(st2.done), [])


# ── 组 5：_judge_and_advance 集成（引擎层） ──

def test_judge_and_advance_integration():
    print("\n=== 组 5: _judge_and_advance 集成 ===")
    # 内存 sqlite
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project

    from app.generation.engine import _judge_and_advance, _persist_timeline_state, _timeline_state_for

    def make_fresh_project(seg_budget: int = 2) -> int:
        """起一个新项目，填好 n1 节点（带 exit_condition），返回 project id。
        每个子用例独立一个新项目，避免互相污染存档。"""
        s = Sess()
        p = Project(
            title=f"v2.1 测 {os.urandom(2).hex()}", target_length=30000, generation_length=3000, mode="quick",
            outline=["a", "b", "c", "d", "e", "f"],
        )
        s.add(p); s.commit(); s.refresh(p)
        st = plan_timeline(total_segments=10)
        st.nodes["n1"].title = "入职立足"
        st.nodes["n1"].exit_condition = "升至部门经理"
        st.nodes["n1"].emotion = "爽"
        st.nodes["n1"].segment_budget = seg_budget
        _persist_timeline_state(s, p, st)
        pid = p.id
        s.close()
        return pid

    # --- 子用例 5.1：judge=True → 提前 done ---
    pid = make_fresh_project(seg_budget=3)  # budget=3 防 advance 自然 done 干扰
    s = Sess()
    p = s.get(Project, pid)
    class PTrue:
        call_count = 0
        def chat(self, *a, **kw):
            self.call_count += 1
            return '{"done": true}'
    pTrue = PTrue()
    r = _judge_and_advance(s, p, pTrue, "林悠然升至部门经理的剧情已经发生。", 0)
    check("5.1 judge=True 时 current_node 切到 n2", r["current_node"], "n2")
    check("5.1 done 含 n1", "n1" in r["done"], True)
    check("5.1 forced=True", r["forced"], True)
    check("5.1 LLM 被调一次", pTrue.call_count, 1)
    s.refresh(p)
    check("5.1 存档里 current_node=n2", p.plot_timeline.get("current_node"), "n2")
    s.close()

    # --- 子用例 5.2：judge=False → 只 advance，不 done（budget=3 防自然 done） ---
    pid = make_fresh_project(seg_budget=3)
    s = Sess()
    p = s.get(Project, pid)
    class PFalse:
        call_count = 0
        def chat(self, *a, **kw):
            self.call_count += 1
            return '{"done": false}'
    pFalse = PFalse()
    r = _judge_and_advance(s, p, pFalse, "还在铺垫期。", 0)
    check("5.2 judge=False 时 current_node 仍是 n1", r["current_node"], "n1")
    check("5.2 forced=False", r["forced"], False)
    check("5.2 advanced=True（正常 advance）", r["advanced"], True)
    check("5.2 judge 值是 False", r["judge"], False)
    s.refresh(p)
    check("5.2 存档 seg_counts.n1 = 1", p.plot_timeline.get("seg_counts", {}).get("n1"), 1)
    s.close()

    # --- 子用例 5.3：judge=None → 走兜底，advance 但不 done ---
    pid = make_fresh_project(seg_budget=3)
    s = Sess()
    p = s.get(Project, pid)
    class PBoom:
        def chat(self, *a, **kw):
            raise RuntimeError("LLM 挂了")
    r = _judge_and_advance(s, p, PBoom(), "无论什么 text", 0)
    check("5.3 LLM 异常 → judge=None", r["judge"], None)
    check("5.3 仍然 advanced=True", r["advanced"], True)
    check("5.3 不 done（current_node 仍是 n1）", r["current_node"], "n1")
    s.close()

    # --- 子用例 5.4：没 outline → skipped=True，不报错 ---
    pid = make_fresh_project(seg_budget=3)
    s = Sess()
    p = s.get(Project, pid)
    p.outline = None
    s.add(p); s.commit()
    r = _judge_and_advance(s, p, PBoom(), "text", 0)
    check("5.4 没 outline → skipped=True", r["skipped"], True)
    check("5.4 没 outline → LLM 不调", r["called"], False)
    s.close()

    # --- 子用例 5.5：当前节点无 exit_condition → LLM 不被调（节流） ---
    pid = make_fresh_project(seg_budget=3)
    s = Sess()
    p = s.get(Project, pid)
    # 手工清空 n1.exit_condition（SQLAlchemy 检测 in-place 改 dict 不变更，
    # 必须重新构造 dict 并赋值给 plot_timeline 才会被 update）
    import copy as _copy
    pt = _copy.deepcopy(p.plot_timeline)
    pt["nodes"]["n1"]["exit_condition"] = ""
    p.plot_timeline = pt
    s.add(p); s.commit(); s.refresh(p)
    pNoCall = PFalse()
    r = _judge_and_advance(s, p, pNoCall, "正文", 0)
    check("5.5 无 exit_condition → judge 不被调", pNoCall.call_count, 0)
    check("5.5 judge 值是 None（未调）", r["judge"], None)
    check("5.5 advanced=True（仍走正常 advance）", r["advanced"], True)
    s.close()

    eng.dispose()


# ── 跑全部 ──

if __name__ == "__main__":
    test_judge_basic_returns()
    test_force_complete_basic()
    test_force_complete_last_node()
    test_force_complete_unknown_current()
    test_judge_and_advance_integration()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
