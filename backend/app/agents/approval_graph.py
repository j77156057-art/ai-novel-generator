"""提案审批子图（LangGraph）——把「谁先谁后」画出来，能不能做仍由状态机说了算。

## 为什么单独一个模块

`app.agents.proposals` 是**状态事实**的唯一来源：`ALLOWED_TRANSITIONS` +
`validate_action` 决定「当前状态能不能做某个动作」；本模块只负责**编排
审批的三个动作**（approve / reject / apply）并把它们串成一张可编译的图。
这样既满足「引入 LangGraph 审批子图」的产品要求，又不会出现两份状态集合
互相漂移。

## 边界

- 只包三种动作：`approve` / `reject` / `apply`；未知动作走 `invalid` 节点
  抛 `InvalidTransition`（与状态机同一异常，路由映射为 409）。
- **不**包批量应用（`apply_selected`）与回滚（`restore_version`）：前者要
  「整批同事务」，拆进图里反而破坏原子性；后者是 Version 的动作，不是提案
  状态转移。
- Writer 主链路、工具白名单一字未动；本模块也**不**持有任何写工具。

## 数据库注入

图在模块级编译一次；请求级 Session 通过 LangGraph 的 `RunnableConfig`
（`config["configurable"]["db"]`）注入节点，不把 ORM 对象塞进 state。
"""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from app.agents import proposals as proposal_lib
from app.models import EditProposal

#: 子图支持的动作（与状态机的合法动作同名）。
GRAPH_ACTIONS: frozenset[str] = frozenset({"approve", "reject", "apply"})

#: 子图节点名（测试用来防止图与转移表漂移）。
GRAPH_NODES: tuple[str, ...] = ("approve", "reject", "apply", "invalid")


class ApprovalState(TypedDict, total=False):
    """图内状态：只放可序列化的最小集，不塞 ORM 对象。"""

    proposal_id: int
    action: str
    actor: str | None
    note: str | None
    status: str
    result: dict[str, Any] | None


def _db_from(config: RunnableConfig) -> Any:
    """从 RunnableConfig 取请求级 Session。"""
    configurable = (config or {}).get("configurable") or {}
    db = configurable.get("db")
    if db is None:
        raise RuntimeError("审批子图缺少 db：请通过 config.configurable 注入 Session")
    return db


def _load_proposal(state: ApprovalState, db: Any) -> EditProposal:
    """按 id 重新取提案（同一 Session 身份映射会复用对象）。"""
    proposal = db.get(EditProposal, state["proposal_id"])
    if proposal is None:
        raise proposal_lib.TargetMissing(f"提案不存在：{state['proposal_id']}")
    return proposal


def _approve_node(state: ApprovalState, config: RunnableConfig) -> ApprovalState:
    db = _db_from(config)
    proposal = _load_proposal(state, db)
    result = proposal_lib.approve_proposal(
        db, proposal=proposal, actor=state.get("actor")
    )
    return {"status": result["status"], "result": result}


def _reject_node(state: ApprovalState, config: RunnableConfig) -> ApprovalState:
    db = _db_from(config)
    proposal = _load_proposal(state, db)
    result = proposal_lib.reject_proposal(
        db,
        proposal=proposal,
        actor=state.get("actor"),
        note=state.get("note"),
    )
    return {"status": result["status"], "result": result}


def _apply_node(state: ApprovalState, config: RunnableConfig) -> ApprovalState:
    db = _db_from(config)
    proposal = _load_proposal(state, db)
    result = proposal_lib.apply_proposal(
        db, proposal=proposal, actor=state.get("actor")
    )
    return {"status": proposal.status, "result": result}


def _invalid_node(state: ApprovalState, config: RunnableConfig) -> ApprovalState:
    # 刻意不在图里另立判断：未知动作直接交给状态机抛同一个异常。
    raise proposal_lib.InvalidTransition("unknown", str(state.get("action")))


def _route(state: ApprovalState) -> str:
    action = str(state.get("action") or "")
    return action if action in GRAPH_ACTIONS else "invalid"


def _build_graph():
    builder = StateGraph(ApprovalState)
    builder.add_node("approve", _approve_node)
    builder.add_node("reject", _reject_node)
    builder.add_node("apply", _apply_node)
    builder.add_node("invalid", _invalid_node)
    builder.add_conditional_edges(
        START,
        _route,
        {name: name for name in GRAPH_NODES},
    )
    for name in GRAPH_NODES:
        builder.add_edge(name, END)
    return builder.compile()


#: 模块级编译一次：节点是无状态纯函数，请求差异全部走 config。
approval_graph = _build_graph()


def run_approval(
    db: Any,
    *,
    proposal: EditProposal,
    action: str,
    actor: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """执行一次审批转移，返回 `{status, result}`（异常语义与 proposals 一致）。"""
    state = approval_graph.invoke(
        {
            "proposal_id": proposal.id,
            "action": action,
            "actor": actor,
            "note": note,
        },
        config={"configurable": {"db": db}},
    )
    db.refresh(proposal)
    return {"status": state.get("status"), "result": state.get("result")}
