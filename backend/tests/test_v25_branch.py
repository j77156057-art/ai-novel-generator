"""v2.5：支线滚动触发器测试。

覆盖：
- KgSignal.has_signal: 新角色/钩子计数判定
- should_roll_branch 纯函数:
  - budget 用完 → False
  - 无 KG 信号 → False
  - 节流：上次刚切过 → False
  - 全部满足 → True
- plan_branch_node: LLM 切支线 + 容错（```json/非 JSON/缺键/异常/无 title）
- attach_branch_to_state: 纯函数切 current_node + branch_used +1
- detect_kg_signal: SQL 查询 + 失败兜底
- roll_branch 包装: 完整链路（KG 查询 + should + plan + attach + 落库）

跑法：python tests/test_v25_branch.py
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
from app.generation.timeline import plan_timeline


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
    def __init__(self, reply: str = '{"title": "支线1", "entry_condition": "e", "exit_condition": "x", "emotion": "爽", "beats": []}', raise_exc: bool = False):
        self.reply = reply
        self.raise_exc = raise_exc
        self.call_count = 0

    def chat(self, system, user) -> str:
        self.call_count += 1
        if self.raise_exc:
            raise RuntimeError("mock LLM error")
        return self.reply


# ── 组 1：KgSignal + has_signal ──

def test_kg_signal():
    print("\n=== 组 1: KgSignal.has_signal ===")
    from app.generation.branch import KgSignal
    s = KgSignal()
    check("空信号 → False", s.has_signal(), False)
    s.new_characters = 1
    check("有 new_characters → True", s.has_signal(), True)
    s2 = KgSignal(open_plotholes=2)
    check("有 open_plotholes → True", s2.has_signal(), True)
    s3 = KgSignal(recent_event_count=5)
    check("仅 recent_event_count → False（不算触发）", s3.has_signal(), False)


# ── 组 2：should_roll_branch 纯函数 ──

def test_should_roll_branch():
    print("\n=== 组 2: should_roll_branch 纯函数 ===")
    from app.generation.branch import KgSignal, should_roll_branch

    st = plan_timeline(total_segments=10)  # branch_budget=2
    sig = KgSignal(new_characters=1)

    # 全满足
    check("全满足 → True", should_roll_branch(st, sig, None, 1), True)

    # budget 用完
    st2 = plan_timeline(total_segments=10)
    st2.branch_used = 2  # 用完
    check("budget 用完 → False", should_roll_branch(st2, sig, None, 1), False)

    # 无 KG 信号
    check("无 KG 信号 → False",
          should_roll_branch(st, KgSignal(), None, 1), False)

    # 节流：上次刚切过
    check("节流：current-written=2, last=1, gap=1 < 2 → False",
          should_roll_branch(st, sig, 1, 2), False)
    check("节流：current-written=3, last=1, gap=2 >= 2 → True",
          should_roll_branch(st, sig, 1, 3), True)
    check("节流：current-written=4, last=1, gap=3 >= 2 → True",
          should_roll_branch(st, sig, 1, 4), True)

    # current_node 为空
    st3 = plan_timeline(total_segments=10)
    st3.current_node = None
    check("current_node 为空 → False",
          should_roll_branch(st3, sig, None, 1), False)


# ── 组 3：plan_branch_node ──

def test_plan_branch_node():
    print("\n=== 组 3: plan_branch_node ===")
    from app.generation.branch import plan_branch_node
    from app.generation.timeline import PlotNode

    main = PlotNode(
        id="n1", kind="main", title="入职立足", emotion="爽",
        entry_condition="入职", exit_condition="升部门经理",
    )
    sig = KgSignal_factory(new_characters=1)

    p = FakeProvider('{"title": "薛姨妈来访", "entry_condition": "刚下班", "exit_condition": "薛姨妈离开", "emotion": "甜", "beats": ["寒暄", "告别"]}')
    from app.generation.branch import KgSignal
    res = plan_branch_node(p, main, KgSignal(new_characters=1))
    check("切到非 None", res is not None, True)
    check("kind=branch", res.kind, "branch")
    check("parent=n1", res.parent, "n1")
    check("title 正确", res.title, "薛姨妈来访")
    check("emotion 正确", res.emotion, "甜")
    check("beats 正确", list(res.beats), ["寒暄", "告别"])
    check("id 以 b- 开头", res.id.startswith("b-"), True)
    check("segment_budget=1", res.segment_budget, 1)

    # 容错
    p1 = FakeProvider('{"no_title": "x"}')
    check("缺 title → None", plan_branch_node(p1, main, KgSignal()), None)
    p2 = FakeProvider('{"title": ""}')
    check("title 空 → None", plan_branch_node(p2, main, KgSignal()), None)
    p3 = FakeProvider("not JSON")
    check("非 JSON → None", plan_branch_node(p3, main, KgSignal()), None)
    p4 = FakeProvider('```json\n{"title": "x", "entry_condition": "e", "exit_condition": "x", "emotion": "爽", "beats": []}\n```')
    check("```json 包裹 → 切到", plan_branch_node(p4, main, KgSignal()).title, "x")
    p5 = FakeProvider(raise_exc=True)
    check("provider 异常 → None", plan_branch_node(p5, main, KgSignal()), None)


def KgSignal_factory(new_characters: int = 0, open_plotholes: int = 0):
    from app.generation.branch import KgSignal
    return KgSignal(new_characters=new_characters, open_plotholes=open_plotholes)


# ── 组 4：attach_branch_to_state ──

def test_attach_branch_to_state():
    print("\n=== 组 4: attach_branch_to_state 纯函数 ===")
    from app.generation.branch import attach_branch_to_state
    from app.generation.timeline import PlotNode

    st = plan_timeline(total_segments=10)
    # 节点 n1 已有
    branch = PlotNode(
        id="b-n1-1", kind="branch", title="支线1", emotion="爽",
        entry_condition="e", exit_condition="x", segment_budget=1,
        parent="n1", children=[],
    )
    new_st = attach_branch_to_state(st, branch)
    check("current_node 切到支线", new_st.current_node, "b-n1-1")
    check("branch_used +1", new_st.branch_used, 1)
    check("n1.children 含 b-n1-1", "b-n1-1" in new_st.nodes["n1"].children, True)
    check("branch 已加到 nodes", "b-n1-1" in new_st.nodes, True)
    check("原 state 不变（纯函数）", st.current_node, "n1")
    check("原 branch_used 不变", st.branch_used, 0)

    # 再次 attach 不同支线
    branch2 = PlotNode(
        id="b-n1-2", kind="branch", title="支线2", emotion="爽",
        entry_condition="e", exit_condition="x", segment_budget=1,
        parent="n1", children=[],
    )
    # 但 current_node 现在是 b-n1-1（不是 n1）—— branch2.parent 是 b-n1-1
    branch2.parent = "b-n1-1"
    new_st2 = attach_branch_to_state(new_st, branch2)
    check("第二次切到 b-n1-2", new_st2.current_node, "b-n1-2")
    check("branch_used=2", new_st2.branch_used, 2)
    check("b-n1-1.children 含 b-n1-2", "b-n1-2" in new_st2.nodes["b-n1-1"].children, True)


# ── 组 5：detect_kg_signal SQL 查询 ──

def test_detect_kg_signal():
    print("\n=== 组 5: detect_kg_signal ===")
    from app.generation.branch import detect_kg_signal

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project
    KgNode = app.models.KgNode

    s = Sess()
    p = Project(title="v2.5 测", target_length=30000, generation_length=3000, mode="quick", outline=["a"]*6)
    s.add(p); s.commit(); s.refresh(p)

    # 无 KG → 空信号
    sig = detect_kg_signal(s, p, 1)
    check("无 KG → new_characters=0", sig.new_characters, 0)
    check("无 KG → open_plotholes=0", sig.open_plotholes, 0)
    check("无 KG → has_signal=False", sig.has_signal(), False)

    # 加一个新角色（last_chapter=1）
    s.add(KgNode(
        project_id=p.id, type="character", source="generated",
        props={"name": "林悠然", "mentions": 10, "speaking": True},
        first_chapter=1, last_chapter=1,
    ))
    # 加一个未回收钩子
    s.add(KgNode(
        project_id=p.id, type="plothole", source="generated",
        props={"cue": "谁在敲门", "question": "谁", "status": "open"},
        first_chapter=1, last_chapter=1,
    ))
    # 加一个已回收钩子（不应计入）
    s.add(KgNode(
        project_id=p.id, type="plothole", source="generated",
        props={"cue": "x", "question": "y", "status": "resolved"},
        first_chapter=1, last_chapter=1,
    ))
    s.commit()

    sig2 = detect_kg_signal(s, p, 1)
    check("有 KG → new_characters=1", sig2.new_characters, 1)
    check("有 KG → open_plotholes=1", sig2.open_plotholes, 1)
    check("有 KG → has_signal=True", sig2.has_signal(), True)

    # written=0 → 不查 KG（边界）
    sig3 = detect_kg_signal(s, p, 0)
    check("written=0 → 空信号", sig3.has_signal(), False)

    s.close()
    eng.dispose()


# ── 组 6：roll_branch 端到端（同步版）──

def test_roll_branch_e2e():
    print("\n=== 组 6: roll_branch 端到端 ===")
    from app.generation.branch import roll_branch
    from app.generation.engine import _persist_timeline_state, _load_timeline_state_for_branch

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project
    KgNode = app.models.KgNode

    s = Sess()
    p = Project(title="v2.5 e2e", target_length=30000, generation_length=3000, mode="quick", outline=["a"]*6)
    s.add(p); s.commit(); s.refresh(p)

    # 首次规划 + 落库
    st = plan_timeline(total_segments=10)
    st.nodes["n1"].title = "入职立足"
    st.nodes["n1"].exit_condition = "升至部门经理"
    st.nodes["n1"].emotion = "爽"
    st.nodes["n1"].segment_budget = 3
    _persist_timeline_state(s, p, st)
    s.refresh(p)

    # 加 KG 角色
    s.add(KgNode(
        project_id=p.id, type="character", source="generated",
        props={"name": "薛姨妈", "mentions": 5, "speaking": True},
        first_chapter=1, last_chapter=1,
    ))
    s.add(KgNode(
        project_id=p.id, type="plothole", source="generated",
        props={"cue": "谁", "question": "谁", "status": "open"},
        first_chapter=1, last_chapter=1,
    ))
    s.commit()
    s.refresh(p)

    # 调 roll_branch
    provider = FakeProvider('{"title": "薛姨妈来访", "entry_condition": "e", "exit_condition": "x", "emotion": "甜", "beats": []}')
    state = _load_timeline_state_for_branch(s, p, 0)
    new_state, info = roll_branch(s, p, state, provider, 1, last_branch_written=None)

    check("rolled=True", info["rolled"], True)
    check("called=True", info["called"], True)
    check("skipped=False", info["skipped"], False)
    check("branch_id 存在", "branch_id" in info, True)
    check("current_node 切到支线", new_state.current_node.startswith("b-"), True)
    check("branch_used=1", new_state.branch_used, 1)
    s.refresh(p)
    check("存档已落库（含支线）",
          "b-" in str(p.plot_timeline.get("current_node", "")),
          True)
    check("主干的 children 包含支线 id",
          any(c.startswith("b-") for c in p.plot_timeline["nodes"]["n1"].get("children", [])),
          True)

    # 边界：没 KG 信号（不切）
    s2 = Sess()
    p2 = s2.get(Project, p.id)
    # 清掉所有 generated KG
    from sqlalchemy import delete
    s2.execute(delete(KgNode).where(KgNode.project_id == p.id, KgNode.source == "generated"))
    s2.commit()
    state2 = _load_timeline_state_for_branch(s2, p2, 1)
    new_state2, info2 = roll_branch(s2, p2, state2, provider, 2, last_branch_written=None)
    check("无 KG 信号 → rolled=False", info2["rolled"], False)
    check("无 KG 信号 → reason=should_roll=False", info2["reason"], "should_roll=False")
    s2.close()

    # 边界：budget 用完
    s3 = Sess()
    p3 = s3.get(Project, p.id)
    pt = _copy_with_used_budget(p3, 2)  # branch_budget=2, used=2
    s3.add(p3); s3.commit(); s3.refresh(p3)
    state3 = _load_timeline_state_for_branch(s3, p3, 5)
    new_state3, info3 = roll_branch(s3, p3, state3, provider, 5, last_branch_written=None)
    check("budget 用完 → rolled=False", info3["rolled"], False)
    s3.close()

    s.close()
    eng.dispose()


def _copy_with_used_budget(p, used):
    """测试 helper：把存档里 branch_used 改到 used。"""
    import copy
    pt = copy.deepcopy(p.plot_timeline)
    pt["branch_used"] = used
    p.plot_timeline = pt
    return pt


# ── 跑全部 ──

if __name__ == "__main__":
    test_kg_signal()
    test_should_roll_branch()
    test_plan_branch_node()
    test_attach_branch_to_state()
    test_detect_kg_signal()
    test_roll_branch_e2e()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
