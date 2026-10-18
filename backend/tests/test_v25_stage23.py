"""v2.5 阶段 2/3 测试。

v2.5 阶段 2：流式版补 v2.5 集成
- generate_next_segment_stream 调 _merge_segment_into_graph + roll_branch
- yield done 事件含 kg_update + branch_roll
- regenerate_segment_stream 不调 KG/reroll（语义：替换不算新增）

v2.5 阶段 3：build_node_prompt 区分 main/branch
- main 节点：原措辞（"【当前剧情点】" + 主线进度）
- branch 节点："【支线剧情点】" + 不打主线进度 + 标注 parent

跑法：python tests/test_v25_stage23.py
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


# ── 组 1：build_node_prompt 区分 main/branch ──

def test_build_node_prompt_branch_vs_main():
    print("\n=== 组 1: build_node_prompt 区分 main/branch ===")
    from app.generation.timeline import build_node_prompt, PlotNode

    # main 节点
    main_node = PlotNode(
        id="n1", kind="main", title="入职立足", emotion="爽",
        entry_condition="刚入职", exit_condition="升至部门经理",
        beats=["铺垫", "升温"], segment_budget=2,
    )
    main_prompt = build_node_prompt(main_node, beat=0, progress=0)
    check("main 提示词含「当前剧情点」", "当前剧情点" in main_prompt, True)
    check("main 提示词含「主线进度」", "主线进度" in main_prompt, True)
    check("main 提示词含 beats", "节拍" in main_prompt, True)
    check("main 提示词含「严禁扩展到后续节点」", "后续节点" in main_prompt, True)
    check("main 提示词不含「支线剧情点」", "支线剧情点" in main_prompt, False)

    # branch 节点
    branch_node = PlotNode(
        id="b-n1-1", kind="branch", title="薛姨妈来访", emotion="甜",
        entry_condition="下班", exit_condition="薛姨妈离开",
        beats=[], segment_budget=1, parent="n1", children=[],
    )
    branch_prompt = build_node_prompt(branch_node, beat=0, progress=0)
    check("branch 提示词含「支线剧情点」", "支线剧情点" in branch_prompt, True)
    check("branch 提示词含 parent 标注", "父节点「n1」" in branch_prompt, True)
    check("branch 提示词含「不是新主线开端」", "不是新主线开端" in branch_prompt, True)
    check("branch 提示词不含「主线进度」", "主线进度" in branch_prompt, False)
    check("branch 提示词不含 beats（无节拍概念）", "节拍" in branch_prompt, False)
    check("branch 提示词含「回到父节点继续」", "回到父节点" in branch_prompt, True)
    check("branch 提示词不含「后续节点会写」（支线无后续节点）",
          "后续节点会写" in branch_prompt, False)
    check("branch 提示词含 entry/exit", "起点状态" in branch_prompt, True)
    check("branch 提示词含写作目标", "写作目标" in branch_prompt, True)

    # branch 节点无 parent（边缘）：标题行不含「父节点」标注，但硬约束
    # 里仍提到"父节点"概念（"回到父节点继续""是父节点的展开"）——这是
    # 故意保留的措辞，让模型知道支线段写完要回父节点
    branch_no_parent = PlotNode(
        id="b-x-1", kind="branch", title="孤立支线", emotion="爽",
        entry_condition="e", exit_condition="x", segment_budget=1, parent=None,
    )
    bp = build_node_prompt(branch_no_parent, beat=0, progress=0)
    check("无 parent 支线不含 parent 标注行（不含「父节点「None」」）",
          "父节点「None」" in bp, False)
    check("无 parent 支线仍报「支线」", "支线" in bp, True)
    # 硬约束里仍提到"父节点"——验证这点（确保支线概念清晰）
    check("无 parent 支线硬约束仍含「父节点」措辞（回父节点）",
          "回到父节点" in bp, True)

    # progress 不影响 branch 措辞（branch 不读 progress）
    bp2 = build_node_prompt(branch_node, beat=0, progress=0.5)
    check("branch 提示词不受 progress 影响（无 main_pct）",
          "50%" in bp2 or "主线进度" in bp2, False)


# ── 组 2：generate_next_segment_stream 调 KG + roll_branch ──

def test_stream_v25_integration():
    print("\n=== 组 2: generate_next_segment_stream 调 v2.5 ===")
    from app.generation.engine import generate_next_segment_stream
    from unittest.mock import patch

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project
    KgNode = app.models.KgNode

    s = Sess()
    p = Project(title="v25s23", target_length=30000, generation_length=3000, mode="quick", outline=["a"]*6)
    s.add(p); s.commit(); s.refresh(p)

    # 提前存 plot_timeline（让 LLM 不去调 plan_nodes_with_llm）
    from app.generation.engine import _persist_timeline_state
    from app.generation.timeline import plan_timeline
    st = plan_timeline(total_segments=10)
    st.nodes["n1"].title = "入职立足"
    st.nodes["n1"].exit_condition = "升至部门经理"
    st.nodes["n1"].emotion = "爽"
    st.nodes["n1"].segment_budget = 3
    _persist_timeline_state(s, p, st)
    s.refresh(p)

    # KG 加新角色 + 钩子（让 roll_branch 触发）
    s.add(KgNode(
        project_id=p.id, type="character", source="generated",
        props={"name": "新配角", "mentions": 5, "speaking": True},
        first_chapter=1, last_chapter=1,
    ))
    s.add(KgNode(
        project_id=p.id, type="plothole", source="generated",
        props={"cue": "?", "question": "?", "status": "open"},
        first_chapter=1, last_chapter=1,
    ))
    s.commit()
    s.refresh(p)

    class FakeStreamingProvider:
        def __init__(self):
            self.chat_calls = []
            self.judge_calls = []
        def chat(self, system, user, constraints=None):
            # judge_node_completion 调 chat 时 user 含"判断"和"退出条件"
            if "退出条件" in user and ("判断" in user or "本段写作" in user):
                self.judge_calls.append(user[:50])
                return '{"done": false}'
            self.chat_calls.append(user[:80])
            return "流式段内容"  # v2.5 测试用更"现实"的文本：含角色+钩子
        def stream_chat(self, system, user, constraints=None, stop_sequences=None, **kw):
            self.chat_calls.append(user[:80])
            # 文本含 "新配角" 触发 KG 抽取，钩子"神秘人"触发 plothole
            yield "新配角走进了公司。她看起来很神秘。神秘人到底是谁？"
        def summarize(self, text): return f"sum({len(text)})"
        def summarize_rollup(self, texts, level): return f"roll({level})"

    provider = FakeStreamingProvider()
    with patch("app.generation.engine._style_narrative", return_value=None):
        chunks = list(generate_next_segment_stream(s, p, novel_ids=None, provider=provider))
    check("至少一个 chunk", any(c.get("type") == "chunk" for c in chunks), True)
    done = chunks[-1]
    check("done 事件含 node_judge", "node_judge" in done, True)
    check("done 事件含 kg_update", "kg_update" in done, True)
    check("done 事件含 branch_roll", "branch_roll" in done, True)
    check("kg_update 是 dict（_merge_segment_into_graph 返回值）", isinstance(done.get("kg_update"), dict), True)
    # rolled=True 需要真 LLM 抽取到角色/钩子（RuleBasedExtractor 词典外
    # 不识别）——本测试只验"流式版调了 v2.5 集成链路"（kg_update + branch_roll
    # 字段在），不验"实际切支线"（那是真智谱 + 真文本的事）
    check("branch_roll.called=True（roll_branch 被调）", done.get("branch_roll", {}).get("called"), True)
    check("branch_roll 含 signal 字段（KG 探针跑过）", "signal" in (done.get("branch_roll") or {}), True)
    check("judge_node_completion 被调", len(provider.judge_calls) >= 1, True)
    s.close()
    eng.dispose()


# ── 组 3：regenerate_segment_stream 不调 v2.5 ──

def test_regenerate_does_not_call_v25():
    print("\n=== 组 3: regenerate_segment_stream 不调 v2.5 ===")
    from app.generation.engine import regenerate_segment_stream, _persist_timeline_state
    from app.generation.timeline import plan_timeline
    from unittest.mock import patch
    from sqlalchemy.orm import sessionmaker

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    from app.models import Generation
    Generation.__table__.create(bind=eng, checkfirst=True)
    Sess = sessionmaker(bind=eng, autoflush=False)
    import app.models  # noqa: F401
    Project = app.models.Project

    # 一个 session 跑完——避开 detached 问题
    s = Sess()
    p = Project(title="v25-regen", target_length=30000, generation_length=3000, mode="quick", outline=["a"]*6)
    s.add(p); s.flush()

    # 先存 plot_timeline（让 node_prompt 能算）
    st = plan_timeline(total_segments=10)
    st.nodes["n1"].title = "n1"
    st.nodes["n1"].exit_condition = "x"
    st.nodes["n1"].segment_budget = 2
    _persist_timeline_state(s, p, st)
    s.refresh(p)

    # 写一段
    g = Generation(project_id=p.id, segment_no=1, text="原文", summary={"text": "old"},
                   chapter_range="段1")
    s.add(g); s.commit()

    class FP:
        def chat(self, system, user, constraints=None):
            return "重生成段"
        def stream_chat(self, system, user, constraints=None, stop_sequences=None, **kw):
            yield "重生成段"
        def summarize(self, text): return "new sum"
        def summarize_rollup(self, texts, level): return "roll"

    with patch("app.generation.engine._style_narrative", return_value=None):
        chunks = list(regenerate_segment_stream(s, p, segment_no=1, novel_ids=None, provider=FP()))
    done = chunks[-1]
    # regenerate 不应 yield "kg_update" 或 "branch_roll" 字段（v2.5 不接 regenerate）
    check("regenerate done 不含 kg_update", "kg_update" in done, False)
    check("regenerate done 不含 branch_roll", "branch_roll" in done, False)
    s.close()
    eng.dispose()


# ── 跑全部 ──

if __name__ == "__main__":
    test_build_node_prompt_branch_vs_main()
    test_stream_v25_integration()
    test_regenerate_does_not_call_v25()
    print(f"\n结果：{PASS} 通过 / {FAIL} 失败 / {PASS + FAIL} 总数")
    sys.exit(0 if FAIL == 0 else 1)
