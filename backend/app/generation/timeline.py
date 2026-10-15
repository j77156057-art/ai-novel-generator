"""时间线规划器：把故事线拆成剧情节点（主干 + 支线），按段数预算驱动进度。

设计见 docs/timeline-pacing-design.md。

首轮范围（纯函数，可测，不依赖 LLM 判定）：
- PlotNode / TimelineState 数据结构
- plan_timeline：从章节预算反推主干节点数与支线额度
- advance：每段生成后按「段数预算」推进（写到 segment_budget 段 → done）
- timeline_progress：双进度（主线节点 + 支线额度）
- distill_branch_ratio：从语料章节情绪统计支线占比
- build_node_prompt：生成当前剧情点的提示块（不含整条 outline）

首轮不做（v2）：exit_condition 的 LLM 判定、高潮密度/长度的语料蒸馏。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 支线占比默认值（语料无统计时回退）
DEFAULT_BRANCH_RATIO = 0.2

#: 主干节点数上下限（防止过碎或过粗）
MIN_MAIN_NODES = 3
MAX_MAIN_NODES = 12

#: 首轮默认：平均每个主干节点占几段（v2 从语料高潮密度蒸馏）
DEFAULT_AVG_NODE_SEGMENTS = 5

#: 判定为「非主线推进」的章节情绪（用于蒸馏 branch_ratio）
_BRANCH_EMOTIONS = frozenset({"日常", "过渡", "支线", "闲笔"})


@dataclass
class PlotNode:
    """一个剧情点。主干串成主线，支线挂在主干下。"""
    id: str
    kind: str                    # "main" | "branch"
    title: str
    emotion: str                 # 爽|虐|甜|怒|悬疑（决定句长/对白密度）
    entry_condition: str         # 进入条件（注入提示词，告诉模型起点状态）
    exit_condition: str          # 完成条件（注入提示词，告诉模型写到什么程度算完成；首轮不参与程序判定）
    beats: list[str] = field(default_factory=list)   # 高潮内部节拍（跨多段展开）
    opens: list[str] = field(default_factory=list)   # 本节点开启的钩子
    closes: list[str] = field(default_factory=list)  # 本节点回收的钩子
    segment_budget: int = 1      # 本节点分到的段数（含高潮展开）
    parent: str | None = None    # 支线挂到哪个主干节点
    children: list[str] = field(default_factory=list)


@dataclass
class TimelineState:
    """时间线推进状态。nodes 含支线；order 只是主干推进顺序。"""
    nodes: dict[str, PlotNode]
    order: list[str]             # 主干节点推进顺序（不含支线）
    done: set[str] = field(default_factory=set)
    total_main: int = 0
    current_node: str | None = None
    current_beat: int = 0
    branch_budget: int = 0       # 支线额度段数（固定）
    branch_used: int = 0         # 已消耗支线额度段数
    seg_counts: dict[str, int] = field(default_factory=dict)  # 各节点已写段数（运行态）


def plan_timeline(
    total_segments: int,
    avg_node_segments: int = DEFAULT_AVG_NODE_SEGMENTS,
    branch_ratio: float = DEFAULT_BRANCH_RATIO,
) -> TimelineState:
    """从章节预算反推主干节点数与支线额度（纯函数，不产节点内容）。

    节点内容（标题/准入准出/节拍）由 LLM 另一次规划填入（§7.3），此处只搭骨架：
    预留 total_main 个占位主干节点 + 支线额度。

    total_segments = 总字数 / 每段字数
    branch_budget  = round(total_segments * branch_ratio)   支线篇幅额度（固定）
    主线可用段      = total_segments - branch_budget
    主干节点数      = clamp(主线可用段 / avg_node_segments, 3, 12)
    每节点 budget   = 主线可用段 / 主干节点数
    """
    total_segments = max(int(total_segments), 1)
    branch_budget = round(total_segments * branch_ratio)
    main_segments = max(total_segments - branch_budget, 1)

    n_main = main_segments // max(avg_node_segments, 1)
    n_main = max(MIN_MAIN_NODES, min(n_main, MAX_MAIN_NODES))
    # clamp 后重算每节点段数，保证总段数不超主线可用段
    seg_budget = max(main_segments // n_main, 1)

    nodes: dict[str, PlotNode] = {}
    order: list[str] = []
    for i in range(n_main):
        nid = f"n{i + 1}"
        nodes[nid] = PlotNode(
            id=nid, kind="main", title=f"主干节点{i + 1}",
            emotion="爽", entry_condition="", exit_condition="",
            beats=[], opens=[], closes=[], segment_budget=seg_budget,
            parent=None, children=[],
        )
        order.append(nid)

    return TimelineState(
        nodes=nodes, order=order, done=set(),
        total_main=n_main, current_node=order[0] if order else None,
        current_beat=0, branch_budget=branch_budget, branch_used=0,
    )


def advance(state: TimelineState, is_branch: bool = False) -> TimelineState:
    """每段生成后推进时间线（纯函数，返回新 state，不改入参）。

    - 支线段：branch_used += 1，不推进主线节点
    - 主干段：当前节点累计写满 segment_budget 段 → done，进入下一节点；
      高潮区按段数推进 current_beat（不越界 beats 长度）
    """
    st = TimelineState(
        nodes=dict(state.nodes),
        order=list(state.order),
        done=set(state.done),
        total_main=state.total_main,
        current_node=state.current_node,
        current_beat=state.current_beat,
        branch_budget=state.branch_budget,
        branch_used=state.branch_used,
        seg_counts=dict(state.seg_counts),
    )

    if is_branch:
        st.branch_used += 1
        return st

    node_id = st.current_node
    if node_id is None or node_id not in st.nodes:
        return st
    node = st.nodes[node_id]

    # 当前节点累计段数 +1
    seg_count = st.seg_counts.get(node_id, 0) + 1
    st.seg_counts[node_id] = seg_count

    # 高潮节拍：节点内按段数推进，不越界
    if node.beats and st.current_beat < len(node.beats) - 1:
        st.current_beat += 1

    # 写满 budget → done，进入下一主干节点
    if seg_count >= node.segment_budget:
        st.done.add(node_id)
        idx = st.order.index(node_id) if node_id in st.order else -1
        nxt = st.order[idx + 1] if 0 <= idx < len(st.order) - 1 else None
        st.current_node = nxt
        st.current_beat = 0

    return st


def force_complete_current(state: TimelineState) -> TimelineState:
    """强制把当前主干节点标 done，切到下一节点（v2：LLM 判定 exit_condition 达成时调用）。

    与 advance 的差别：不依赖 seg_count 是否满 segment_budget，由调用方
    （LLM 判定结果）决定是否提前 done。失败/超时/不确定 → 不调本函数，
    走 advance 的段数预算兜底。
    """
    st = TimelineState(
        nodes=dict(state.nodes),
        order=list(state.order),
        done=set(state.done),
        total_main=state.total_main,
        current_node=state.current_node,
        current_beat=state.current_beat,
        branch_budget=state.branch_budget,
        branch_used=state.branch_used,
        seg_counts=dict(state.seg_counts),
    )
    node_id = st.current_node
    if node_id is None or node_id not in st.nodes or node_id in st.done:
        return st
    st.done.add(node_id)
    idx = st.order.index(node_id) if node_id in st.order else -1
    nxt = st.order[idx + 1] if 0 <= idx < len(st.order) - 1 else None
    st.current_node = nxt
    st.current_beat = 0
    return st


def timeline_progress(state: TimelineState) -> dict:
    """双进度：主线（节点 done 比）+ 支线（额度消耗比）。分母都固定。"""
    main_done = sum(1 for nid in state.done if state.nodes.get(nid) and state.nodes[nid].kind == "main")
    main_progress = main_done / state.total_main if state.total_main else 0.0
    branch_progress = state.branch_used / state.branch_budget if state.branch_budget else 0.0
    return {
        "main_progress": round(main_progress, 4),
        "branch_progress": round(branch_progress, 4),
        "current_node": state.current_node,
        "current_beat": state.current_beat,
    }


def distill_branch_ratio(chapters_emotion: list[str]) -> float:
    """从语料章节情绪统计支线占比：非主线推进章节 / 总章节。

    无数据（空列表）回退 DEFAULT_BRANCH_RATIO。首轮只用情绪标签粗估，
    v2 接更精细的章节级标注。
    """
    if not chapters_emotion:
        return DEFAULT_BRANCH_RATIO
    branch = sum(1 for e in chapters_emotion if e in _BRANCH_EMOTIONS)
    return round(branch / len(chapters_emotion), 4)


def build_node_prompt(node: PlotNode, beat: int, progress: int | float | dict) -> str:
    """生成当前剧情点的提示块（不含整条 outline）。

    告诉模型：现在在哪个点、这个点写到什么程度算完成、当前在节拍哪一步、主线进度多少。
    exit_condition 是写作目标（首轮不参与程序判定，v2.1 起由 LLM judge 判定后
    提前 done），beats 是高潮内部结构。

    v2.2 节点粒度强化：
    - 强约束"写到 exit 即收尾，不要再展开其他剧情"——模型习惯写"开端"后接
      "中段"再"结尾"，必须用更明确措辞打断这种模式
    - 显式禁止引入未提及的角色——避免模型从 outline/样例"借鉴"未到场的角色
      （如把红楼梦风格的薛姨妈带进现代言情）

    v2.5 阶段 3：按 node.kind 区分
    - main：原措辞（按当前剧情点 + 主线进度推进）
    - branch：支线段——去主线进度、改硬约束（"写完回到父节点锚点"）
    """
    # progress 兼容：v2 之前传 dict，新版允许直接传 main_progress int/float
    if isinstance(progress, dict):
        main_pct = int(progress.get("main_progress", 0) * 100)
    else:
        main_pct = int(float(progress) * 100)

    # v2.5 阶段 3：支线分支——措辞完全独立
    if node.kind == "branch":
        return _build_branch_node_prompt(node)

    lines = [f"【当前剧情点】{node.title}"]
    if node.entry_condition:
        lines.append(f"起点状态：{node.entry_condition}")
    if node.exit_condition:
        # v2.2 强化措辞：把"推进到 X" 改成"本段只写到 X 就结束"
        # 原措辞"推进到 X"在 LLM 看来是"至少到 X"，可能写过头
        lines.append(
            f"本段写作目标：{node.exit_condition}"
            f"——**写到这件事完成就立即收尾**，不要再展开其他剧情"
        )
    if node.emotion:
        lines.append(f"本段情绪基调：{node.emotion}")
    if node.beats:
        cur = node.beats[min(beat, len(node.beats) - 1)]
        lines.append(f"高潮节拍进度：第{beat + 1}步「{cur}」/ 共{len(node.beats)}步")
    lines.append(f"主线进度：{main_pct}%")
    lines.append(
        "**硬约束**：\n"
        f"1. 只写「{node.title}」这一个剧情点，写完写作目标即停笔，"
        "严禁扩展到后续节点。\n"
        "2. **不要引入当前剧情点 entry/exit 未提及的角色**——"
        "模型习惯「顺手带出」会导致角色提前出场、剧情跳节点。\n"
        "3. 不要写出故事结局或大团圆——后续节点会写。"
    )
    return "\n".join(lines)


def _build_branch_node_prompt(node: PlotNode) -> str:
    """v2.5 阶段 3：支线节点的 prompt 措辞——与 main 分离。

    设计：
    - 标题前缀 "【支线剧情点】" 明确告诉模型这是支线（不是主干）
    - 不打"主线进度"——支线不算主线推进
    - 硬约束改："写完即回父节点锚点"——支线段写完后下一段回到父节点
      继续写（不是切到下一个 main 节点）
    - 不输出 beats——支线无节拍概念
    - 标注"支线是父节点 X 的展开"——避免模型把支线当成新主线的开端

    设计取舍：
    - 保持 entry/exit 注入（和 main 一致——让模型知道边界）
    - 硬约束措辞紧扣"支线特性"（回父节点、别展开）——这是 main 节点
      不需要强调的
    """
    lines = [f"【支线剧情点】{node.title}"]
    if node.parent:
        lines.append(f"（这是父节点「{node.parent}」的支线展开——不是新主线开端）")
    if node.entry_condition:
        lines.append(f"起点状态：{node.entry_condition}")
    if node.exit_condition:
        lines.append(
            f"本段写作目标：{node.exit_condition}"
            f"——**写到这件事完成就立即收尾**，不要再展开其他剧情"
        )
    if node.emotion:
        lines.append(f"本段情绪基调：{node.emotion}")
    lines.append(
        "**硬约束**：\n"
        f"1. 只写「{node.title}」这一个支线剧情点，写完写作目标即停笔，"
        "严禁扩展到任何其他剧情——下一段会回到父节点继续。\n"
        "2. **不要引入 entry/exit 未提及的角色**。\n"
        "3. 不要把支线演成新主线——支线是父节点的展开，不是开端。"
    )
    return "\n".join(lines)


# ── §11.3 DB 持久化：TimelineState ↔ JSON 互转 ──

#: 持久化 schema 版本。将来字段调整时 +1，load 时按版本分支兼容/降级。
TIMELINE_SCHEMA_VERSION = 1


def _node_to_dict(node: PlotNode) -> dict:
    return {
        "id": node.id, "kind": node.kind, "title": node.title,
        "emotion": node.emotion,
        "entry_condition": node.entry_condition, "exit_condition": node.exit_condition,
        "beats": list(node.beats), "opens": list(node.opens), "closes": list(node.closes),
        "segment_budget": node.segment_budget, "parent": node.parent,
        "children": list(node.children),
    }


def _node_from_dict(d: dict) -> PlotNode:
    return PlotNode(
        id=d["id"], kind=d.get("kind", "main"), title=d.get("title", ""),
        emotion=d.get("emotion", "爽"),
        entry_condition=d.get("entry_condition", ""), exit_condition=d.get("exit_condition", ""),
        beats=list(d.get("beats") or []), opens=list(d.get("opens") or []),
        closes=list(d.get("closes") or []),
        segment_budget=int(d.get("segment_budget") or 1),
        parent=d.get("parent"), children=list(d.get("children") or []),
    )


def state_to_dict(state: TimelineState) -> dict:
    """TimelineState → JSON 字典（§11.3 落库用）。

    只存推进相关字段：nodes（含已填实内容）、done、current_node、current_beat、
    branch_used、seg_counts。order/total_main/branch_budget 从 outline/项目参数
    复算，不存——避免 outline 改后状态和骨架对不上。
    """
    return {
        "version": TIMELINE_SCHEMA_VERSION,
        "nodes": {nid: _node_to_dict(n) for nid, n in state.nodes.items()},
        "done": sorted(state.done),  # set → list，JSON 友好
        "current_node": state.current_node,
        "current_beat": state.current_beat,
        "branch_used": state.branch_used,
        "seg_counts": dict(state.seg_counts),
    }


def state_from_dict(d: dict, *, total_segments: int) -> TimelineState | None:
    """JSON 字典 → TimelineState（§11.3 恢复用）。

    入参 `total_segments` 用于从骨架参数复算 total_main/order/branch_budget
    ——这三项与项目当前 target_length / generation_length / outline 强相关，
    不存是为了「调大纲或改总字数时不用手改 plot_timeline」。失败返回 None
    （调用方回退到首次规划路径）。
    """
    if not isinstance(d, dict):
        return None
    if d.get("version") != TIMELINE_SCHEMA_VERSION:
        return None  # 版本不匹配：降级到首次规划
    nodes_raw = d.get("nodes")
    done_raw = d.get("done") or []
    if not isinstance(nodes_raw, dict) or not nodes_raw:
        return None
    if not isinstance(done_raw, list):
        return None

    # 复算骨架（必须用最新 total_segments 调 plan_timeline，nodes 在其上覆盖）
    skeleton = plan_timeline(total_segments=total_segments)
    for nid, nd in nodes_raw.items():
        if not isinstance(nd, dict):
            continue
        # 保留骨架里的 segment_budget（与新 total_segments 同步），其余从存档覆盖
        node = _node_from_dict(nd)
        if nid in skeleton.nodes:
            node.segment_budget = skeleton.nodes[nid].segment_budget
        skeleton.nodes[nid] = node
    # 已 done 的节点若存档里有也保留（即使新骨架里没有——可能是 outline 缩短）
    for nid in done_raw:
        if nid not in skeleton.nodes and nid in nodes_raw:
            nd = nodes_raw[nid]
            if isinstance(nd, dict):
                skeleton.nodes[nid] = _node_from_dict(nd)
        skeleton.done.add(nid)

    cn = d.get("current_node")
    if cn in skeleton.nodes or cn is None:
        skeleton.current_node = cn
    skeleton.current_beat = int(d.get("current_beat") or 0)
    skeleton.branch_used = int(d.get("branch_used") or 0)
    sc = d.get("seg_counts") or {}
    if isinstance(sc, dict):
        skeleton.seg_counts = {k: int(v) for k, v in sc.items() if isinstance(v, (int, float))}
    return skeleton


# ── §7.3 LLM 节点规划：把占位节点填实（title/entry/exit/beats/emotion）──

_NODE_PLAN_SYSTEM = (
    "你是资深网文编辑。任务：把一条故事大纲切成 N 个**主干剧情点**，供分章写作时按点推进。\n"
    "要求：\n"
    "1. 恰好输出 N 个剧情点，按故事顺序。\n"
    "2. 每个点给出：\n"
    "   - title：这个点的短名（如「入职立足」）\n"
    "   - entry_condition：进入这个点时主角/世界的状态（一句）\n"
    "   - exit_condition：写到什么事件算完成这个点（一句，具体可判定的情节）\n"
    "   - emotion：爽|虐|甜|怒|悬疑 之一\n"
    "   - beats：若这个点是高潮，给出 2-4 步内部节拍（如铺垫/升温/爆发/余波）；非高潮给 []\n"
    "3. **不要写出结局**——最后一个点只到「最终冲突的爆发/揭开」，真正的结局留给后续。\n"
    "4. 严格按 JSON 输出，不要解释。\n"
    '格式：{"nodes":[{"title":"...","entry_condition":"...","exit_condition":"...",'
    '"emotion":"爽","beats":["铺垫","升温","爆发"]}]}'
)


def plan_nodes_with_llm(
    provider,
    outline: list[str],
    plot_bible: dict | None,
    n_main: int,
) -> list[PlotNode] | None:
    """调一次 LLM，把 outline 切成 n_main 个填实的剧情点（§7.3）。

    失败/超时/解析不出 → 返回 None（调用方回退到占位节点，不阻断生成）。
    """
    import json as _json

    outline_text = "\n".join(f"{i+1}. {o}" for i, o in enumerate(outline)) or "（无大纲）"
    synopsis = (plot_bible or {}).get("synopsis") or ""
    premise = (plot_bible or {}).get("premise") or ""
    user = (
        f"请把下面这条故事大纲切成 {n_main} 个主干剧情点：\n\n"
        f"【故事梗概】{synopsis or premise or '（无）'}\n\n"
        f"【大纲】\n{outline_text}\n\n"
        f"输出 {n_main} 个剧情点的 JSON。"
    )
    try:
        raw = provider.chat(_NODE_PLAN_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return None

    # 容错解析：剥离 ```json 包裹 / 找 {...} 块
    import re as _re
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    items = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        return None

    nodes: list[PlotNode] = []
    for i, it in enumerate(items[:n_main]):
        if not isinstance(it, dict):
            continue
        beats = it.get("beats") or []
        if not isinstance(beats, list):
            beats = []
        nodes.append(PlotNode(
            id=f"n{i + 1}", kind="main",
            title=str(it.get("title") or f"主干节点{i + 1}"),
            emotion=str(it.get("emotion") or "爽"),
            entry_condition=str(it.get("entry_condition") or ""),
            exit_condition=str(it.get("exit_condition") or ""),
            beats=[str(b) for b in beats if b],
            opens=[], closes=[], segment_budget=1,
            parent=None, children=[],
        ))
    return nodes or None


# ── v2.1：exit_condition 的 LLM 判定（§13 待定项落地） ──

_JUDGE_NODE_DONE_SYSTEM = (
    "你是严格的网文编辑。任务是判断「刚写完的这一段」是否已经达成了"
    "当前剧情点的退出条件。\n"
    "要求：\n"
    "1. 仔细对照退出条件描述，看正文里是否已经发生或达成。\n"
    "2. **宁严勿松**——只有退出条件明确达成才返回 true；还在推进中、"
    "铺垫中、隐约触及但未落实的，返回 false。\n"
    "3. 严格按 JSON 输出，不要解释。\n"
    '格式：{"done": true} 或 {"done": false}\n'
    '不要输出 true/false 之外的任何字段。'
)


def judge_node_completion(
    provider,
    node: PlotNode,
    recent_text: str,
) -> bool | None:
    """v2.1：调一次 LLM 判定当前节点的 exit_condition 是否已达成。

    返回值语义：
    - True  → 退出条件明确达成，调用方应 force_complete_current 切下一节点
    - False → 还没达成，走段数预算继续推进
    - None  → 判定失败/超时/解析不出，回退到段数预算（不阻断生成）

    失败兜底的设计动机：LLM 不可靠时不能让流程卡住；段数预算已经是
    兜底（§7.4），LLM 只是加速判定。
    """
    if not node.exit_condition:
        return None  # 没设 exit 就别让 LLM 瞎判
    if not recent_text or not recent_text.strip():
        return None

    # 节流：只给最近 N 字，避免 prompt 过长——LLM 判不完整段也没必要
    excerpt = recent_text[-1200:] if len(recent_text) > 1200 else recent_text
    user = (
        f"【当前剧情点】{node.title}\n"
        f"【退出条件】{node.exit_condition}\n"
        f"【最近正文】\n{excerpt}\n\n"
        "判断：退出条件是否已达成？"
    )
    try:
        raw = provider.chat(_JUDGE_NODE_DONE_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return None
    import json as _json
    import re as _re
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    v = data.get("done")
    if v is True:
        return True
    if v is False:
        return False
    return None  # 既不是 true 也不是 false → 兜底
