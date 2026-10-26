"""时间线规划器 + 剧情节点（app/generation/timeline.py）。

覆盖：
1. plan_timeline 从段数反推节点数，clamp 在 [3,12]
2. 支线额度正确扣除（branch_budget = round(total*ratio)），不计入主线分母
3. advance 节点写到 segment_budget 段 → done，主线进度上升
4. 支线段 → branch_used 累加，主线进度不变
5. 高潮区 current_beat 按段数推进，不越界 beats 长度
6. branch_ratio 蒸馏：有语料统计时用蒸馏值，无则回退默认 0.2
7. build_node_prompt 只含当前节点信息，不含整条 outline
8. build_user_prompt 时间线模式：不注入整条大纲/synopsis/plot_bible/premise
9. plan_nodes_with_llm：LLM 把占位节点填实 + 各类容错

（这份原本是「脚本式断言」—— check() 只记数不抛，跑挂了也会显示通过。
 已改写成 pytest 用例：失败即抛，断言消息带标签订位。）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.generation.timeline import (
    DEFAULT_BRANCH_RATIO,
    PlotNode,
    TimelineState,
    advance,
    build_node_prompt,
    distill_branch_ratio,
    plan_timeline,
    timeline_progress,
)


def mk_state(n_main=5, seg_budget=3, branch_budget=4):
    """造一个 TimelineState：n_main 个主干节点，每节点 seg_budget 段。"""
    nodes = {}
    order = []
    for i in range(n_main):
        nid = f"n{i+1}"
        nodes[nid] = PlotNode(
            id=nid, kind="main", title=f"节点{i+1}",
            emotion="爽", entry_condition=f"进入{i+1}", exit_condition=f"完成{i+1}",
            beats=["铺垫", "升温", "爆发"] if i % 2 == 0 else [],
            opens=[], closes=[], segment_budget=seg_budget,
            parent=None, children=[],
        )
        order.append(nid)
    return TimelineState(
        nodes=nodes, order=order, done=set(),
        total_main=n_main, current_node=order[0], current_beat=0,
        branch_budget=branch_budget, branch_used=0,
    )


# ── 1. plan_timeline 节点数 clamp ──
def test_plan_timeline_clamps_node_count():
    # 大纲：10 段可用，每节点 5 段 → 2 个节点，被 clamp 到下限 3
    st = plan_timeline(total_segments=10, avg_node_segments=5, branch_ratio=0.2)
    main_count = sum(1 for n in st.nodes.values() if n.kind == "main")
    assert main_count == 3, f"节点数 clamp 下限 3: got={main_count}"
    # 100 段，每节点 5 段 → 20 个，被 clamp 到上限 12
    st = plan_timeline(total_segments=100, avg_node_segments=5, branch_ratio=0.2)
    main_count = sum(1 for n in st.nodes.values() if n.kind == "main")
    assert main_count == 12, f"节点数 clamp 上限 12: got={main_count}"
    # 30 段，每节点 5 段 → 30*(1-0.2)/5 = 4.8 → 4 个（中间区间不 clamp）
    st = plan_timeline(total_segments=30, avg_node_segments=5, branch_ratio=0.2)
    main_count = sum(1 for n in st.nodes.values() if n.kind == "main")
    assert main_count == 4, f"中间区间不 clamp: got={main_count}"


# ── 2. 支线额度扣除 + 不计入主线分母 ──
def test_branch_budget_deducted_and_excluded_from_main():
    st = plan_timeline(total_segments=30, avg_node_segments=5, branch_ratio=0.2)
    # 支线额度 = round(30 * 0.2) = 6
    assert st.branch_budget == 6, f"branch_budget: got={st.branch_budget}"
    assert st.total_main == 4, f"支线不计入 total_main: got={st.total_main}"


# ── 3. advance 节点写满 budget → done，主线进度上升 ──
def test_advance_fills_node_budget_without_completing():
    st = mk_state(n_main=5, seg_budget=3)
    assert timeline_progress(st)["main_progress"] == 0.0
    st = advance(st)  # n1 第 1 段
    st = advance(st)  # n1 第 2 段
    st = advance(st)  # n1 第 3 段 → 写满 budget，done
    assert st.current_node == "n1", f"预算用尽保留 n1: got={st.current_node}"
    assert "n1" not in st.done, "预算用尽不标 done"
    assert timeline_progress(st)["main_progress"] == 0.0, "无证据主线进度为零"


# ── 4. 支线段 → branch_used 累加，主线进度不变 ──
def test_branch_segment_only_accumulates_branch_used():
    st = mk_state(n_main=5, seg_budget=3)
    st = advance(st, is_branch=True)
    assert st.branch_used == 1, f"支线段 branch_used 累加: got={st.branch_used}"
    assert timeline_progress(st)["main_progress"] == 0.0, "支线段主线进度不变"
    assert st.current_node == "n1", f"支线段不推进节点: got={st.current_node}"


# ── 5. 高潮区 current_beat 按段数推进，不越界 ──
def test_current_beat_advances_but_never_exceeds():
    from app.generation.timeline import force_complete_current

    # n1 有 3 拍，seg_budget=3 → 每段进一拍，第 3 段后 done 时 beat 停在 2（不越界到 3）
    st = mk_state(n_main=5, seg_budget=3)
    st = advance(st)
    assert st.current_beat == 1, f"beat 第1段后=1: got={st.current_beat}"
    st = advance(st)
    assert st.current_beat == 2, f"beat 第2段后=2: got={st.current_beat}"
    st = advance(st)
    assert st.current_beat == 2, f"预算用尽 beat 不越界: got={st.current_beat}"
    assert st.current_node == "n1", f"没有证据留在 n1: got={st.current_node}"
    st.completion["n1"] = {"verdict": "supported",
                            "sources": [{"source_id": "generation:3", "quote": "完成1", "start": 0, "end": 3}]}
    st = force_complete_current(st)
    # 无 beats 的节点（n2 i%2!=0 → beats 空）：beat 保持 0
    st = advance(st)
    assert st.current_beat == 0, f"无 beats 节点 beat 保持 0: got={st.current_beat}"


# ── 6. branch_ratio 蒸馏 ──
def test_branch_ratio_distillation_and_default():
    # 语料统计：非高潮章节占比 → branch_ratio。10 章里 日常×3 + 过渡×1 = 4 → ratio 0.4
    chapters_emotion = ["爽", "日常", "虐", "日常", "爽", "过渡", "怒", "日常", "甜", "爽"]
    assert distill_branch_ratio(chapters_emotion) == 0.4, "从语料蒸馏 branch_ratio"
    assert distill_branch_ratio([]) == DEFAULT_BRANCH_RATIO, "无数据回退默认"
    assert DEFAULT_BRANCH_RATIO == 0.2, "DEFAULT_BRANCH_RATIO = 0.2"


# ── 7. build_node_prompt 只含当前节点，不含整条 outline ──
def test_node_prompt_contains_only_current_node():
    st = mk_state(n_main=5, seg_budget=3)
    node = st.nodes["n1"]
    prompt = build_node_prompt(node, beat=1, progress=timeline_progress(st))
    assert "节点1" in prompt, "提示词含当前节点标题"
    assert "完成1" in prompt, "提示词含退出条件"
    assert "升温" in prompt, "提示词含节拍位置"
    assert "节点2" not in prompt, "提示词不含后续节点标题"
    assert "节点4" not in prompt, "提示词不含节点4标题"
    # v2.2 节点粒度强化：build_node_prompt 末尾的硬约束必须出现
    assert "硬约束" in prompt, "v2.2 提示词含「硬约束」块"
    assert "严禁扩展到后续节点" in prompt, "v2.2 提示词含「严禁扩展到后续节点」"
    assert "不要引入" in prompt, "v2.2 提示词含「禁止引入未提及角色」"
    # progress 兼容：直接传 int/float 也行
    prompt2 = build_node_prompt(node, beat=0, progress=0.25)
    assert "主线进度：25%" in prompt2, "progress 兼容：传 float 也行"
    prompt3 = build_node_prompt(node, beat=0, progress=0)
    assert "主线进度：0%" in prompt3, "progress 兼容：传 0 也行"


# ── 8. build_user_prompt 时间线模式：不注入整条大纲/synopsis ──
def test_timeline_mode_prompt_strips_spoilers():
    from app.generation.prompts import build_user_prompt

    st = mk_state(n_main=5, seg_budget=3)
    node = st.nodes["n1"]

    plot_bible = {
        "synopsis": "主角最终成为CEO迎娶白富美的大结局走向",
        "direction": "后续两人感情走向圆满结局",
        "conflicts": ["家族背景差异"],
        "relationships": [{"a": "林悠然", "b": "顾辰逸", "relation": "互相吸引"}],
        "foreshadowing": ["辰逸父亲的秘密"],
    }
    # premise 含整条故事走向：premise 是用户填的"小说大概内容"，常含结局。
    # 时间线模式按设计剔除（与 plot_bible 一起），防止 LLM 第一段奔结局。
    premise = "都市职场女性林悠然在家族企业改革中周旋，与大学教授顾辰逸相知相守"
    outline = ["第一段 宣布改革", "第二段 项目危机", "第三段 贵人相助", "第四段 大结局圆满"]

    # 时间线模式：传 node_prompt
    up_tl = build_user_prompt(
        prior_summaries=[], rag_context=[], outline=outline, max_chars=3000,
        plot_bible=plot_bible, premise=premise,
        node_prompt=build_node_prompt(node, 0, timeline_progress(st)),
    )
    assert "宣布改革" not in up_tl, "时间线模式不含整条大纲-宣布改革"
    assert "大结局" not in up_tl, "时间线模式不含大结局走向"
    assert "迎娶白富美" not in up_tl, "时间线模式不含 synopsis 结局"
    # v2.2 节点粒度调优：时间线模式**剔除整个 plot_bible**——relationships/fores/conflicts
    # 任何一项都可能携带整条故事走向，注入后 LLM 会把后续剧情提前写进本段。
    assert "林悠然与顾辰逸" not in up_tl, "时间线模式不含人物关系（防剧透）"
    assert "人物关系" not in up_tl, "时间线模式不含人物关系字段"
    assert "辰逸父亲的秘密" not in up_tl, "时间线模式不含伏笔（防剧透）"
    assert "持续伏笔" not in up_tl, "时间线模式不含伏笔字段"
    assert "剧情冲突" not in up_tl, "时间线模式不含剧情冲突字段"
    # premise：用户给的故事大概，**时间线模式剔除**（同 plot_bible 一样携带整条走向）
    assert "相知相守" not in up_tl, "时间线模式不含 premise"
    assert "小说大概内容" not in up_tl, "时间线模式不含 premise 字段"
    assert "当前剧情进度" in up_tl, "时间线模式注入当前剧情点"

    # 原逻辑模式：不传 node_prompt（向后兼容）—— 仍注所有 plot_bible + premise
    up_orig = build_user_prompt(
        prior_summaries=[], rag_context=[], outline=outline, max_chars=3000,
        plot_bible=plot_bible, premise=premise, node_prompt=None,
    )
    assert "宣布改革" in up_orig, "原逻辑模式含整条大纲"
    assert "迎娶白富美" in up_orig, "原逻辑模式含 synopsis"
    assert "当前剧情进度" not in up_orig, "原逻辑模式不含剧情点块"


# ── 9. plan_nodes_with_llm：LLM 把占位节点填实（§7.3）──
class _FakeProvider:
    """返回预置 JSON 的假 provider，测解析与容错。"""

    def __init__(self, reply):
        self._reply = reply

    def chat(self, system, user, constraints=None):
        return self._reply


class _BoomProvider:
    def chat(self, system, user, constraints=None):
        raise RuntimeError("LLM 挂了")


OUTLINE = ["入职升职", "改革重组", "项目危机", "贵人相助", "揭穿对手", "家族秘密", "相守未来"]
BIBLE = {"synopsis": "职场女性在家族企业改革中周旋并与教授相守"}

# 正常返回：3 个填实节点
GOOD_REPLY = (
    '{"nodes":['
    '{"title":"入职立足","entry_condition":"悠然刚升部门经理","exit_condition":"改革通知下达，她得知部门将重组","emotion":"爽","beats":[]},'
    '{"title":"危机与援手","entry_condition":"项目因技术问题严重滞后","exit_condition":"辰逸提供技术支持，项目重回正轨","emotion":"虐","beats":["铺垫","升温","爆发"]},'
    '{"title":"当众揭穿","entry_condition":"对手发难，悠然被逼到墙角","exit_condition":"悠然当众揭穿对手算计，赢得认可","emotion":"爽","beats":[]}'
    ']}'
)


def test_plan_nodes_with_llm_fills_nodes():
    from app.generation.timeline import plan_nodes_with_llm

    nodes = plan_nodes_with_llm(_FakeProvider(GOOD_REPLY), OUTLINE, BIBLE, 3)
    assert nodes is not None and len(nodes) == 3, "LLM 规划出 3 个节点"
    assert nodes[0].title == "入职立足", "节点1标题填实"
    assert "重组" in nodes[0].exit_condition, "节点1 exit 条件填实"
    assert nodes[1].beats == ["铺垫", "升温", "爆发"], "节点2 带节拍"
    assert nodes[1].emotion == "虐", "节点2 情绪填实"
    assert [n.id for n in nodes] == ["n1", "n2", "n3"], "节点 id 按序"


def test_plan_nodes_with_llm_tolerates_bad_output():
    from app.generation.timeline import plan_nodes_with_llm

    # 容错：带 ```json 包裹
    wrapped = "```json\n" + GOOD_REPLY + "\n```"
    nodes2 = plan_nodes_with_llm(_FakeProvider(wrapped), OUTLINE, BIBLE, 3)
    assert nodes2 is not None and len(nodes2) == 3, "容错 ```json 包裹"

    # 容错：LLM 抛异常 → None（不阻断）
    assert plan_nodes_with_llm(_BoomProvider(), OUTLINE, BIBLE, 3) is None, "LLM 异常返回 None"

    # 容错：返回非 JSON → None
    assert plan_nodes_with_llm(_FakeProvider("我不会输出JSON"), OUTLINE, BIBLE, 3) is None, "非 JSON 返回 None"

    # 容错：JSON 但无 nodes 键 → None
    assert plan_nodes_with_llm(_FakeProvider('{"foo":1}'), OUTLINE, BIBLE, 3) is None, "无 nodes 键返回 None"
