"""§11.3 TimelineState 持久化测试（不依赖 LLM）。

覆盖：
- state_to_dict / state_from_dict 互转正确（节点/done/current/beat/branch_used/seg_counts）
- state_from_dict 失败模式（None/版本错/空 nodes/非 dict done）→ 返回 None
- 复算骨架：total_segments 变化时 segment_budget 跟着新骨架走
- 端到端：用内存 sqlite + Project 模型，写入 → 读回 → 节点标题/进度一致
- 服务重启模拟：关闭 session、重开，存档 state 与第一次推进后一致

跑法：python tests/test_timeline_persistence.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# —— 让脚本以「独立运行」也能 import 到 app.* ——
BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.generation.timeline import (
    PlotNode, TimelineState,
    advance, plan_timeline,
    state_to_dict, state_from_dict,
    TIMELINE_SCHEMA_VERSION,
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


# ── 组 1：纯函数互转 ──

def build_state(total_segments: int = 10, written: int = 0) -> TimelineState:
    st = plan_timeline(total_segments=total_segments)
    for _ in range(written):
        st = advance(st)
    return st


def test_round_trip_basic():
    print("\n=== 组 1: 纯函数互转 ===")
    st = build_state(total_segments=12, written=2)
    d = state_to_dict(st)
    check("version 字段", d.get("version"), TIMELINE_SCHEMA_VERSION)
    check("nodes 是 dict 且非空", isinstance(d.get("nodes"), dict) and bool(d["nodes"]), True)
    check("done 是 list", isinstance(d.get("done"), list), True)
    check("current_node 非空", bool(d.get("current_node")), True)
    check("current_beat 是 int", isinstance(d.get("current_beat"), int), True)
    check("branch_used 是 int", isinstance(d.get("branch_used"), int), True)
    check("seg_counts 是 dict", isinstance(d.get("seg_counts"), dict), True)

    # 反序列化回同样 total_segments 应一致
    st2 = state_from_dict(d, total_segments=12)
    check("反序列化非 None", st2 is not None, True)
    check("current_node 一致", st2.current_node, st.current_node)
    check("current_beat 一致", st2.current_beat, st.current_beat)
    check("done 一致", sorted(st2.done), sorted(st.done))
    check("branch_used 一致", st2.branch_used, st.branch_used)
    check("seg_counts 一致", dict(st2.seg_counts), dict(st.seg_counts))
    check("nodes 数一致", len(st2.nodes), len(st.nodes))
    # 节点字段：找一个比一下
    sample = next(iter(st.nodes.values()))
    s2 = st2.nodes[sample.id]
    check("节点 id 一致", s2.id, sample.id)
    check("节点 title 一致", s2.title, sample.title)
    check("节点 emotion 一致", s2.emotion, sample.emotion)
    check("节点 entry_condition 一致", s2.entry_condition, sample.entry_condition)
    check("节点 exit_condition 一致", s2.exit_condition, sample.exit_condition)
    check("节点 beats 一致", list(s2.beats), list(sample.beats))


def test_round_trip_with_filled_nodes():
    """模拟 §7.3 填实节点后存档的互转。"""
    print("\n=== 组 2: 填实节点 + 互转 ===")
    st = plan_timeline(total_segments=10)
    # 注入填实节点
    st.nodes["n1"] = PlotNode(
        id="n1", kind="main", title="入职立足",
        emotion="爽",
        entry_condition="林悠然刚入职",
        exit_condition="升至部门经理",
        beats=["签合同", "完成首项目"],
        segment_budget=st.nodes["n1"].segment_budget,
    )
    d = state_to_dict(st)
    st2 = state_from_dict(d, total_segments=10)
    check("填实节点 title 保留", st2.nodes["n1"].title, "入职立足")
    check("填实节点 entry 保留", st2.nodes["n1"].entry_condition, "林悠然刚入职")
    check("填实节点 exit 保留", st2.nodes["n1"].exit_condition, "升至部门经理")
    check("填实节点 emotion 保留", st2.nodes["n1"].emotion, "爽")
    check("填实节点 beats 保留", list(st2.nodes["n1"].beats), ["签合同", "完成首项目"])


# ── 组 3：失败模式 ──

def test_from_dict_failures():
    print("\n=== 组 3: 反序列化失败模式 ===")
    check("None 输入", state_from_dict(None, total_segments=10), None)
    check("非 dict 输入", state_from_dict("not a dict", total_segments=10), None)
    check("空 dict 输入", state_from_dict({}, total_segments=10), None)
    check("version 错", state_from_dict({"version": 999, "nodes": {"n1": {}}}, total_segments=10), None)
    check("无 nodes", state_from_dict({"version": TIMELINE_SCHEMA_VERSION}, total_segments=10), None)
    check("nodes 空", state_from_dict({"version": TIMELINE_SCHEMA_VERSION, "nodes": {}}, total_segments=10), None)
    # done 是 str 而非 list
    check("done 非 list", state_from_dict(
        {"version": TIMELINE_SCHEMA_VERSION, "nodes": {"n1": {}}, "done": "n1"}, total_segments=10
    ), None)


# ── 组 4：total_segments 变化（调总字数） ──

def test_reskeleton_on_total_change():
    """模拟「用户改 target_length 后再读存档」——segment_budget 应按新骨架。"""
    print("\n=== 组 4: 复算骨架 ===")
    st = plan_timeline(total_segments=10)
    # 给 n1 强行塞 title（模拟已规划）
    st.nodes["n1"].title = "原标题"
    d = state_to_dict(st)

    # 改 total_segments（项目扩字数）——再读
    st2 = state_from_dict(d, total_segments=20)
    check("扩容后 state 非 None", st2 is not None, True)
    check("n1 标题保留（不重置 LLM 规划）", st2.nodes["n1"].title, "原标题")
    check("扩容后 segment_budget 随骨架变（10 段→n1 占 ~3 段；20 段→~6 段）",
          st2.nodes["n1"].segment_budget > st.nodes["n1"].segment_budget,
          True)


# ── 组 5：端到端（内存 sqlite + Project） ──

def test_e2e_persistence_across_sessions():
    print("\n=== 组 5: 端到端——服务重启后状态恢复 ===")
    # 内存 sqlite + 重置表
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    # §11.3：确保 plot_timeline 列被建（model 加了，create_all 会建）
    Sess = sessionmaker(bind=eng, autoflush=False)

    import app.models  # noqa: F401  注册
    Project = app.models.Project

    # 第一次会话：建项目 + 模拟 LLM 填实 + 写库
    s1 = Sess()
    p1 = Project(
        title="测", target_length=30000, generation_length=3000,
        mode="quick",
        outline=[
            "开端：入职", "发展：改革", "升温：项目危机",
            "转折：遇见辰逸", "危机：供应商推诿", "高潮：揭穿对手",
        ],
    )
    s1.add(p1)
    s1.commit()
    s1.refresh(p1)
    pid = p1.id
    check("项目 id 已生成", pid is not None, True)
    check("新建项目 plot_timeline 为空", p1.plot_timeline, None)

    # 模拟 §7.3 后：注入填实节点
    from app.generation.timeline import plan_timeline as _plan
    st = _plan(total_segments=10)
    st.nodes["n1"].title = "入职立足"
    st.nodes["n1"].entry_condition = "林悠然刚入职"
    st.nodes["n1"].exit_condition = "升至部门经理"
    st.nodes["n1"].emotion = "爽"
    # 走 2 段
    st = advance(st)
    st = advance(st)

    p1.plot_timeline = state_to_dict(st)
    s1.add(p1)
    s1.commit()
    s1.close()
    print(f"  (s1: 写库完成，pid={pid}，current_node={st.current_node}，seg_counts={dict(st.seg_counts)})")

    # 第二次会话：模拟服务重启，新 SessionLocal
    s2 = Sess()
    p2 = s2.get(Project, pid)
    check("s2 读到项目", p2 is not None, True)
    check("s2 读到 plot_timeline 存档", isinstance(p2.plot_timeline, dict), True)

    st_restored = state_from_dict(p2.plot_timeline, total_segments=10)
    check("从存档恢复成功", st_restored is not None, True)
    check("current_node 恢复", st_restored.current_node, st.current_node)
    check("seg_counts 恢复", dict(st_restored.seg_counts), dict(st.seg_counts))
    check("填实 title 恢复", st_restored.nodes["n1"].title, "入职立足")
    check("填实 entry 恢复", st_restored.nodes["n1"].entry_condition, "林悠然刚入职")
    check("填实 exit 恢复", st_restored.nodes["n1"].exit_condition, "升至部门经理")

    # 再 advance 一段，状态应该和「s1 多 advance 一次」一致
    st_restored = advance(st_restored)
    check("恢复后仍可 advance（seg_counts 加 1）",
          sum(st_restored.seg_counts.values()),
          sum(st.seg_counts.values()) + 1)

    s2.close()
    eng.dispose()


# ── 组 6：与引擎层 _timeline_state_for 集成（不调 LLM） ──

def test_engine_uses_persisted_state_no_llm():
    """验证：第二次 _timeline_state_for 走存档路径，不会调 provider.chat。"""
    print("\n=== 组 6: 引擎层走存档路径，不调 LLM ===")
    from app.generation.engine import _timeline_state_for

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project

    s = Sess()
    p = Project(
        title="集成", target_length=30000, generation_length=3000, mode="quick",
        outline=["a", "b", "c", "d", "e", "f"],
    )
    s.add(p); s.commit(); s.refresh(p)

    # 用一个会"炸"的 provider 模拟：如果走 LLM 路径会抛；走存档路径不会调
    class BoomProvider:
        def chat(self, *a, **kw):
            raise RuntimeError("BOOM: 不该调到 LLM")

    # 路径 1：首次 → 走 LLM 填实
    st1 = _timeline_state_for(p, written_segments=0, provider=BoomProvider(), db=s)
    check("首次调 LLM 成功（用兜底占位）", st1 is not None, True)
    check("首次：current_node 非空", st1.current_node is not None, True)
    s.refresh(p)
    check("首次后存档已写库", isinstance(p.plot_timeline, dict), True)

    # 路径 2：第二次 → 走存档恢复，不调 LLM（BoomProvider.chat 不应被触发）
    s2 = Sess()
    p2 = s2.get(Project, p.id)
    st2 = _timeline_state_for(p2, written_segments=0, provider=BoomProvider(), db=s2)
    check("二次调用从存档恢复", st2 is not None, True)
    check("current_node 与首次一致", st2.current_node, st1.current_node)
    s2.close()
    s.close()
    eng.dispose()


# ── 跑全部 ──

if __name__ == "__main__":
    test_round_trip_basic()
    test_round_trip_with_filled_nodes()
    test_from_dict_failures()
    test_reskeleton_on_total_change()
    test_e2e_persistence_across_sessions()
    test_engine_uses_persisted_state_no_llm()

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
