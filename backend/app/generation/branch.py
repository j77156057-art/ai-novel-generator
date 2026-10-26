"""支线滚动触发器（v2.5）—— 当主线推进时按 KG 信号动态生成支线节点。

design §3 规划：支线节点滚动追加，挂到当前主干节点下。本模块做"是
否该切支线"+"切什么样的支线"的纯函数 + 一层包装。

数据流：
1. detect_kg_signal(db, project, written_segments) -> KgSignal
   - 查 KG source="generated"：相对 written_segments，新增 character 节点
     数 / 未回收 plothole 节点数 / 总事件数
2. should_roll_branch(state, signal, last_branch_at: int | None) -> bool
   - 纯函数：是否该切支线
   - 条件：branch_budget > branch_used AND (new_character > 0 OR
     open_plothole > 0) AND written_segments - last_branch_at >= MIN_GAP
3. plan_branch_node(provider, current_main_node, signal) -> PlotNode | None
   - 调一次 LLM 切一个支线剧情点（title/entry/exit/emotion/beats），
     沿用 §7.3 plan_nodes_with_llm 的容错模式
4. roll_branch(db, project, st, provider, written) -> tuple[TimelineState, dict]
   - 包装：检查 + 切 + 挂到 current_main.children + 切换 current_node +
     branch_used += 1 + 记录 last_branch_at + 落库

设计取舍：
- 触发条件只看 KG 信号：避免规则耦合（什么算"配角登场"由 KG 决定）
- 节流：MIN_GAP=2 段（不连续切支线，给主线呼吸空间）
- 失败兜底：LLM 切失败 → 不切（return state 不变）；KG 查询失败 →
  当作"无信号"，不切
- 注入 prompt 改造（build_node_prompt 区分 main/branch）属 v2.5 阶段 2，
  阶段 1 只做到"切节点 + 落库"，下一段 prompt 暂用 main 写法（功能
  不挂，支线标题/entry 由 next LLM 调用显式使用）
"""
from __future__ import annotations

import json as _json
import re as _re
from dataclasses import dataclass, field


#: 两次切支线之间最少间隔段数（节流：避免连续切支线导致主线停滞）
MIN_GAP_SEGMENTS = 2

#: 支线节点默认 segment_budget（支线段比主干短）
BRANCH_SEGMENT_BUDGET = 1


@dataclass
class KgSignal:
    """v2.5：当前段后的 KG 信号——驱动支线触发。

    字段均为相对值（不含历史值）——调用方从 db 实时统计。
    """
    new_characters: int = 0      # 本次 KG 中相对"已规划范围"的新角色数
    open_plotholes: int = 0      # 未回收钩子数
    recent_event_count: int = 0   # 近 N 段新产生的事件数（用于节流/健康度）

    def has_signal(self) -> bool:
        """判定是否有触发支线的 KG 信号。"""
        return self.new_characters > 0 or self.open_plotholes > 0


def detect_kg_signal(db, project, written_segments: int) -> KgSignal:
    """v2.5：查 KG source="generated"，统计相对"已规划范围"的新角色/钩子数。

    Args:
        written_segments: 已写段数。新角色定义为 last_chapter == written_segments
        （即在最近一段才登场）。open_plotholes 状态 = open。

    Returns:
        KgSignal（含 new_characters / open_plotholes / recent_event_count）

    设计：
    - "新角色"用 last_chapter 等于最新段号——避免历史角色一直计入触发
    - "未回收钩子"是绝对值（不是 new），因为支线就是来"展开"这些钩子的
    - 失败兜底：KG 表不存在/异常 → 返回空信号（不会触发支线，不阻断）
    """
    try:
        from app.models import KgNode
        from sqlalchemy import select, func
    except Exception:  # noqa: BLE001
        return KgSignal()

    if written_segments <= 0:
        return KgSignal()

    sig = KgSignal()
    # 三个查询各自独立的 try/except——避免 status JSON 字段语法错被外层
    # 兜底吞掉整个 new_characters 查询。
    from app.models import KgNode
    from sqlalchemy import select, func

    # 1) 新角色
    try:
        lookback = max(1, MIN_GAP_SEGMENTS)
        new_char_rows = db.execute(
            select(func.count(KgNode.id))
            .where(
                KgNode.project_id == project.id,
                KgNode.type == "character",
                KgNode.source == "generated",
                KgNode.last_chapter >= written_segments - lookback + 1,
            )
        ).scalar()
        sig.new_characters = int(new_char_rows or 0)
    except Exception:  # noqa: BLE001
        pass  # 单个失败不影响其他查询

    # 2) 未回收钩子：status="open"
    # SQLAlchemy 2.0 删了 .astext，用 JSON 类型的 op("->>") 取字符串值
    try:
        open_rows = db.execute(
            select(func.count(KgNode.id))
            .where(
                KgNode.project_id == project.id,
                KgNode.type == "plothole",
                KgNode.source == "generated",
                KgNode.props.op("->>")("status") == "open",
            )
        ).scalar()
        sig.open_plotholes = int(open_rows or 0)
    except Exception:  # noqa: BLE001
        pass

    # 3) 近 N 段事件数
    try:
        recent_event_rows = db.execute(
            select(func.count(KgNode.id))
            .where(
                KgNode.project_id == project.id,
                KgNode.type == "event",
                KgNode.source == "generated",
                KgNode.last_chapter >= written_segments - lookback + 1,
            )
        ).scalar()
        sig.recent_event_count = int(recent_event_rows or 0)
    except Exception:  # noqa: BLE001
        pass

    return sig


def should_roll_branch(
    state, signal: KgSignal, last_branch_written: int | None,
    current_written: int, min_gap: int = MIN_GAP_SEGMENTS,
) -> bool:
    """v2.5 纯函数：是否该切支线。

    条件（全 AND）：
    1. branch_budget > branch_used（额度未用完）
    2. signal.has_signal()（有 KG 信号：新角色/未回收钩子）
    3. 节流：last_branch_written 为 None（从未切过）或
       (current_written - last_branch_written) >= min_gap
    4. 当前 current_node 存在（state 正常）

    Args:
        state: TimelineState
        signal: KgSignal
        last_branch_written: 上次切支线时的已写段数；None 表示从未切过
        current_written: 当前已写段数
        min_gap: 节流间隔（默认 MIN_GAP_SEGMENTS=2）

    Returns:
        bool

    测试：见 test_v25_branch.py
    """
    if state.branch_budget <= state.branch_used:
        return False
    if not signal.has_signal():
        return False
    if state.current_node is None:
        return False
    if last_branch_written is not None and (current_written - last_branch_written) < min_gap:
        return False
    return True


# ── §7.3-style 支线 LLM 切点 ──

_BRANCH_PLAN_SYSTEM = (
    "你是资深网文编辑。任务：在当前主干剧情点下生成一个**支线剧情点**（一段或几段）。\n"
    "要求：\n"
    "1. 紧扣当前主干节点——支线要服务于主线的某条支脉。\n"
    "2. 给出：\n"
    "   - title：支线短名（如「薛姨妈来访」）\n"
    "   - entry_condition：进入支线时主角/世界的状态\n"
    "   - exit_condition：写到什么事件算完成支线（一句，具体可判定）\n"
    "   - emotion：爽|虐|甜|怒|悬疑 之一\n"
    "   - beats：若支线有内部节拍，给出 2-3 步；无给 []\n"
    "3. **不要**写出主线后续——支线只展开当前主干的某个小切面。\n"
    "4. 严格按 JSON 输出，不要解释。\n"
    '格式：{"title":"...","entry_condition":"...","exit_condition":"...","emotion":"爽","beats":["..."]}'
)


def plan_branch_node(provider, current_main_node, signal: KgSignal) -> "PlotNode | None":
    """v2.5：调一次 LLM 切一个支线剧情点。

    入参 current_main_node: 当前主干 PlotNode（用其 title/exit 作为 prompt 锚点）
    入参 signal: KG 信号（new_characters + open_plotholes 拼成 prompt 上下文）

    返回：PlotNode(kind="branch", parent=current_main_node.id) 或 None（失败）
    """
    import json as _json
    import re as _re

    ctx_lines = []
    if signal.new_characters > 0:
        ctx_lines.append(f"最近有 {signal.new_characters} 个新角色登场")
    if signal.open_plotholes > 0:
        ctx_lines.append(f"还有 {signal.open_plotholes} 个未回收的悬念")
    ctx = "；".join(ctx_lines) or "（无明显信号，支线由你按主线需要自由生成）"

    user = (
        f"【当前主干剧情点】{current_main_node.title}\n"
        f"【主干退出条件】{current_main_node.exit_condition or '（未设）'}\n"
        f"【KG 信号】{ctx}\n\n"
        "为这个主干节点生成一个支线剧情点（紧扣上文，给一个具体可判定的小事件）："
    )
    try:
        raw = provider.chat(_BRANCH_PLAN_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return None
    m = _re.search(r"\{.*\}", raw, _re.DOTALL)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None

    title = str(data.get("title") or "").strip()
    if not title:
        return None

    # 必须 import PlotNode（避免循环 import）
    from app.generation.timeline import PlotNode
    beats = data.get("beats") or []
    if not isinstance(beats, list):
        beats = []
    return PlotNode(
        id=f"b-{current_main_node.id}-{int(__import__('time').time())}",
        kind="branch",
        title=title,
        emotion=str(data.get("emotion") or "爽"),
        entry_condition=str(data.get("entry_condition") or ""),
        exit_condition=str(data.get("exit_condition") or ""),
        beats=[str(b) for b in beats if b],
        opens=[], closes=[],
        segment_budget=BRANCH_SEGMENT_BUDGET,
        parent=current_main_node.id,
        children=[],
    )


def attach_branch_to_state(state, branch: "PlotNode") -> "TimelineState":
    """v2.5 纯函数：把支线节点挂到当前主干 children，并切换 current_node 到支线。

    注意：
    - 不改 main 节点（只 append children）
    - 把 current_node 切到支线 id
    - branch_used += 1（占一段预算）
    - 不动 current_beat（支线无节拍概念，但保留字段以兼容）
    """
    from app.generation.timeline import TimelineState

    # copy
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
        completion=dict(state.completion),
        last_judged_segment=state.last_judged_segment,
        planning=dict(state.planning),
    )
    # 把支线加到 nodes（不在 order 里——order 是主干推进顺序）
    st.nodes[branch.id] = branch
    # 挂到当前主干的 children
    if st.current_node and st.current_node in st.nodes:
        main = st.nodes[st.current_node]
        if branch.id not in main.children:
            main.children = list(main.children) + [branch.id]
    # 切 current_node 到支线，branch_used + 1
    st.current_node = branch.id
    st.branch_used = st.branch_used + 1
    return st


def roll_branch(
    db, project, state, provider, written: int, last_branch_written: int | None
) -> tuple:
    """v2.5 包装：检查 + 切 + 挂 + 切 current_node + 落库。

    Returns:
        (new_state, info_dict)
        info_dict = {"called": bool, "rolled": bool, "skipped": True/False, "reason": str}

    设计：
    - 失败兜底完全静默（与 _judge_and_advance 同风格）——LLM/KG 失败
      不阻断主流程
    - 不传 db/None → 跳过
    """
    info = {"called": False, "rolled": False, "skipped": True, "reason": "no_signal"}

    if db is None or not project.outline or not state.current_node:
        info["reason"] = "no_db_or_outline_or_current"
        return state, info

    sig = detect_kg_signal(db, project, written)
    info["called"] = True
    info["signal"] = {
        "new_characters": sig.new_characters,
        "open_plotholes": sig.open_plotholes,
        "recent_event_count": sig.recent_event_count,
    }

    if not should_roll_branch(state, sig, last_branch_written, written):
        info["skipped"] = True
        info["reason"] = "should_roll=False"
        return state, info

    # 切支线
    cur_main = state.nodes[state.current_node]
    branch = plan_branch_node(provider, cur_main, sig)
    if branch is None:
        info["skipped"] = True
        info["reason"] = "plan_branch returned None"
        return state, info

    new_state = attach_branch_to_state(state, branch)
    info["rolled"] = True
    info["skipped"] = False
    info["reason"] = "rolled"
    info["branch_id"] = branch.id
    info["branch_title"] = branch.title

    # 落库
    from app.generation.engine import _persist_timeline_state
    _persist_timeline_state(db, project, new_state)
    return new_state, info
