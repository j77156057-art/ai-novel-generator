"""Assistant Agent 工具注册表（ADR-2026-10-12 第 2 步）。

设计要点（与 b58c763 不变量对齐）：

- 角色锁定：Assistant 仅能引用 `read_only=True` 的工具；任何 `write_*` / `commit_*`
  工具被 Assistant 调用 → 注册表立即抛 `PermissionError`，**不**进入任何缓存。
- 工具实现 = 复用既有只读检索/查询入口（`hybrid_search` / `get_character` /
  `get_reminders` / Generation 选择查询 / Fact 选择查询 / plot_bible 读取），
  不复制 SQL、不引入平行 ORM 路径。
- 注册表是**进程级单例**，但**只**持有 ToolSpec（name / read_only / callable），
  不持有 db session。db session 由 router 通过 Depends(get_db) 注入，
  工具 callable 拿到的就是 router 已校验的 session。
- 失败隔离：invoke_tool(name, ...) 内部抛异常时**不**污染注册表；下一次的同 name
  调用照常重试（与 ProviderRegistry 的失败不入缓存同语义）。

不在本表范围：
- LangGraph 节点 / Deep Agents 编排：本轮禁用（见 ADR §3「Deep Agents 暂缓」）。
- 流式续写：Assistant 端不流式；不在本表暴露 stream_chat。
- Prompt / 章节 / 角色等业务表写入：EditorProposal/Version 已建模但**未**在本表
  提供任何写入端点；审批流在后续 ADR（ADR-2026-10-12 第 5 步第 4 步）开放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping


# ====================================================================
# 角色契约 / 黑名单 / 白名单
# ====================================================================

# 任何 Assistant 工具被 invoke 时，name 以这些前缀开头 → 立即拒绝。
# 这是「角色锁定」的最小动作：写工具就算被错误注册了，也**根本进不到** invoke 路径。
# 严禁把 write_/commit_ 工具以别名绕过——前缀规则直接作用于 invoke 时。
FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "write_",
    "commit_",
    "update_",
    "delete_",
    "patch_",
    "rollback_",
    "apply_proposal",
)

# 默认白名单：search_novel_chunks / read_generation_segments /
# get_character_profile / get_plot_timeline / get_fact_ledger / get_reminders /
# summarize_bible —— 与 task spec 一致。
ALLOWED_READ_ONLY_TOOLS: tuple[str, ...] = (
    "search_novel_chunks",
    "read_generation_segments",
    "get_character_profile",
    "get_plot_timeline",
    "get_fact_ledger",
    "get_reminders",
    "summarize_bible",
)


# ====================================================================
# ToolSpec
# ====================================================================

@dataclass(frozen=True)
class ToolSpec:
    """Assistant 工具声明：name / read_only / callable。

    callable 签名固定为 `(db, project_id: int, **kwargs) -> dict`：
    - db：路由层注入的 SQLAlchemy Session（router 已校验 project 存在）；
    - project_id：路由层校验过的整数（避免 Assistant 越权访问其它项目）；
    - **kwargs：工具自身需要的额外参数（query / segment_no / top_k…）。

    不持有 db session / 不持有 LLM 句柄：测试可独立构造 ToolSpec。
    """

    name: str
    description: str
    read_only: bool
    callable: Callable[..., dict]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("ToolSpec.name 不能为空")
        if not isinstance(self.read_only, bool):
            raise ValueError("ToolSpec.read_only 必须为 bool")
        if not callable(self.callable):
            raise ValueError("ToolSpec.callable 必须是可调用对象")


# ====================================================================
# 注册表（单例）
# ====================================================================

class AssistantToolRegistry:
    """Assistant 工具注册表：持有 name → ToolSpec 的只读映射。

    公开方法只有 register / invoke / resolve；外部**不**直接改 _tools，
    所有写入经 register，所有读取经 invoke / resolve。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ----- 注册 -----
    def register(self, spec: ToolSpec) -> None:
        """注册工具。

        拒绝规则（任何一条命中即抛 PermissionError，绝不写入）：
          1. spec.read_only is False（不论 name 是什么——读工具白名单是硬约束）；
          2. spec.name 以 FORBIDDEN_PREFIXES 任一前缀开头（黑名单前缀；
             防 write_* 用 alias 注册成 read_only=True 蒙混）；
          3. spec.name 已在 _tools 中 → 重名也算错误（避免运行时调用的歧义）。
        """
        if spec.read_only is False:
            raise PermissionError(
                f"Assistant 工具必须是 read_only=True；拒绝 {spec.name!r} "
                f"（write_* / commit_* 工具由后续审批流单独建模）"
            )
        for prefix in FORBIDDEN_PREFIXES:
            if spec.name.startswith(prefix):
                raise PermissionError(
                    f"Assistant 工具名 {spec.name!r} 命中黑名单前缀 {prefix!r}；"
                    "禁止把写工具注册进 Assistant 注册表"
                )
        if spec.name in self._tools:
            raise ValueError(f"工具 {spec.name!r} 已存在，禁止覆盖")
        self._tools[spec.name] = spec

    def register_many(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def names(self) -> list[str]:
        """已注册的工具名列表（按注册顺序；供端点 self-test / 调试用）。"""
        return list(self._tools.keys())

    def resolve(self, name: str) -> ToolSpec:
        """按 name 取 ToolSpec；未注册则 KeyError（不让 router 静默吞掉）。"""
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"工具 {name!r} 未注册到 Assistant 注册表") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    # ----- 调用 -----
    def invoke(self, name: str, /, db: Any, project_id: int, **kwargs: Any) -> dict:
        """按 name 调用工具：先校验角色，再透传 db / project_id / kwargs。

        校验：
          - name 以黑名单前缀开头 → PermissionError（双重门：即便有人绕开 register
            直接调用 invoke，仍会被拒绝）；
          - name 未注册 → KeyError；
          - spec.read_only is False → PermissionError（防御 register 时漏判）。

        异常**不**写入任何状态：invoke_tool 抛错时本注册表的状态保持不变。
        """
        for prefix in FORBIDDEN_PREFIXES:
            if name.startswith(prefix):
                raise PermissionError(
                    f"Assistant 拒绝调用黑名单前缀工具 {name!r}（{prefix!r}）"
                )
        spec = self.resolve(name)
        if spec.read_only is False:
            raise PermissionError(
                f"Assistant 拒绝非只读工具 {name!r}（spec.read_only=False）"
            )
        # 把 db / project_id 显式注入；kwargs 是工具自身的额外参数（如 query / k）。
        # 工具 callable 不会拿到 db / project_id 之外的"特权"——返回 dict，不持引用。
        return spec.callable(db=db, project_id=project_id, **kwargs)

    # ----- 测试 / 复位 -----
    def clear(self) -> None:
        """清空 _tools：仅供测试或重启时使用。生产路径不会调本方法。"""
        self._tools.clear()


# ====================================================================
# 模块级单例 + 工具函数
# ====================================================================

_registry: AssistantToolRegistry | None = None


def get_tool_registry() -> AssistantToolRegistry:
    """拿全局单例；首次调用构造，后续复用。

    测试代码如需隔离环境，用 reset_tool_registry() 显式丢弃。
    """
    global _registry
    if _registry is None:
        _registry = AssistantToolRegistry()
        _register_default_tools(_registry)
    return _registry


def reset_tool_registry() -> None:
    """丢弃全局注册表；下次 get_tool_registry() 重建（重建时再次挂默认工具）。"""
    global _registry
    _registry = None


def invoke_tool(name: str, /, db: Any, project_id: int, **kwargs: Any) -> dict:
    """便捷入口：get_tool_registry().invoke(name, db, project_id, **kwargs)。"""
    return get_tool_registry().invoke(name, db, project_id=project_id, **kwargs)


# ====================================================================
# 默认工具实现：直接复用既有只读入口；不复制 SQL
# ====================================================================

def _impl_search_novel_chunks(*, db: Any, project_id: int,
                              query: str, k: int = 5, novel_id: int | None = None) -> dict:
    """对单本参考小说做混合检索；novel_id 不传则取 project 的 reference_novel_ids 第一本。

    这是与 Engine._build_constraints 同一份检索；只是 limit 改成更小的 k 默认值，
    让 Assistant 端不至于一次性灌进几百段原文。
    """
    from app.models import Project
    from app.retrieval.search import hybrid_search

    proj: Project | None = db.get(Project, project_id)
    if proj is None:
        return {"novel_id": None, "hits": []}

    target_novel_id = novel_id
    if target_novel_id is None:
        refs = list(proj.reference_novel_ids or [])
        if not refs:
            return {"novel_id": None, "hits": [], "note": "项目未配置参考语料"}
        target_novel_id = int(refs[0])

    hits = hybrid_search(db, target_novel_id, query, k=k)
    return {"novel_id": target_novel_id, "query": query, "k": k, "hits": hits}


def _impl_read_generation_segments(*, db: Any, project_id: int,
                                   segment_no: int | None = None,
                                   limit: int = 5) -> dict:
    """读取已生成的段（默认最新 limit 段；指定 segment_no 时只取该段）。"""
    from sqlalchemy import select
    from app.models import Generation

    q = select(Generation).where(Generation.project_id == project_id)
    if segment_no is not None:
        q = q.where(Generation.segment_no == segment_no)
    q = q.order_by(Generation.segment_no.desc()).limit(limit)
    gens = list(db.scalars(q).all())
    return {
        "segments": [
            {
                "id": g.id,
                "segment_no": g.segment_no,
                "chapter_range": g.chapter_range,
                "text": g.text,
                "summary": g.summary,
            }
            for g in gens
        ],
        "count": len(gens),
    }


def _impl_get_character_profile(*, db: Any, project_id: int, name: str,
                                source: str = "generated") -> dict:
    """复用 kg.queries.get_character（按 name 找节点 + 出边展开）。"""
    from app.kg.queries import get_character

    result = get_character(db, project_id, name)
    if result is None:
        return {"name": name, "found": False, "note": f"角色 {name!r} 不存在或 source={source!r} 无该角色"}
    return {"name": name, "found": True, "profile": result, "source": source}


def _impl_get_plot_timeline(*, db: Any, project_id: int,
                            limit: int = 20) -> dict:
    """剧情时间线：按 segment_no 升序拉取 Generation.chapter_range + summary。"""
    from sqlalchemy import select
    from app.models import Generation

    gens = list(db.scalars(
        select(Generation)
        .where(Generation.project_id == project_id)
        .order_by(Generation.segment_no.asc())
        .limit(limit)
    ).all())
    return {
        "timeline": [
            {
                "segment_no": g.segment_no,
                "chapter_range": g.chapter_range,
                # summary 是 dict；Assistant 只取其中的短摘要字段避免膨胀
                "summary": (g.summary or {}).get("short") if isinstance(g.summary, dict) else None,
            }
            for g in gens
        ],
        "count": len(gens),
    }


def _impl_get_fact_ledger(*, db: Any, project_id: int,
                          type_: str | None = None,
                          limit: int = 50) -> dict:
    """事实账本（Fact 表）——按 type 过滤可选。"""
    from sqlalchemy import select
    from app.models import Fact

    q = select(Fact).where(Fact.project_id == project_id)
    if type_ is not None:
        q = q.where(Fact.type == type_)
    q = q.order_by(Fact.updated_at.desc()).limit(limit)
    facts = list(db.scalars(q).all())
    return {
        "facts": [
            {
                "id": f.id,
                "type": f.type,
                "key": f.key,
                "props": f.props,
                "aliases": f.aliases,
                "source": f.source,
                "confidence": f.confidence,
                "first_segment": f.first_segment,
                "last_segment": f.last_segment,
            }
            for f in facts
        ],
        "count": len(facts),
    }


def _impl_get_reminders(*, db: Any, project_id: int,
                        source: str = "generated", top_k: int = 50) -> dict:
    """复用 kg.queries.get_reminders。Assistant 端默认不封顶（top_k=None 不在本层暴露），

    但默认 50 与 REMINDER_TOP_K 一致，避免提示词膨胀（评审 A 的 Q5 复盘）。
    """
    from app.kg.queries import get_reminders

    return get_reminders(db, project_id, source=source, top_k=top_k)


def _impl_summarize_bible(*, db: Any, project_id: int) -> dict:
    """剧情圣经读取：plot_bible JSON 结构（characters / relationships / direction / …）。

    不复制提示词拼装逻辑：直接透传 plot_bible 内容，让 router 的诊断/QA
    自行决定怎么把它压给 Assistant 模型。
    """
    from app.models import Project

    proj: Project | None = db.get(Project, project_id)
    if proj is None:
        return {"plot_bible": None, "note": f"project {project_id} 不存在"}
    return {
        "plot_bible": proj.plot_bible,
        "premise": proj.premise,
        "outline": proj.outline,
    }


_DEFAULT_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="search_novel_chunks",
        description="对项目首本参考语料（或显式 novel_id）做混合检索，返回前 k 个分片。",
        read_only=True,
        callable=_impl_search_novel_chunks,
    ),
    ToolSpec(
        name="read_generation_segments",
        description="读取项目已生成的段（默认最新 limit 段；或按 segment_no 精确取）。",
        read_only=True,
        callable=_impl_read_generation_segments,
    ),
    ToolSpec(
        name="get_character_profile",
        description="按角色名取人设 + 关系出边（复用 kg.queries.get_character）。",
        read_only=True,
        callable=_impl_get_character_profile,
    ),
    ToolSpec(
        name="get_plot_timeline",
        description="按 segment_no 升序拉取 Generation 的时间线与短摘要。",
        read_only=True,
        callable=_impl_get_plot_timeline,
    ),
    ToolSpec(
        name="get_fact_ledger",
        description="读 Fact 表（可按 type 过滤），返回已裁决事实 + 别名。",
        read_only=True,
        callable=_impl_get_fact_ledger,
    ),
    ToolSpec(
        name="get_reminders",
        description="生成前提醒：未填坑 + 角色约束清单（复用 kg.queries.get_reminders）。",
        read_only=True,
        callable=_impl_get_reminders,
    ),
    ToolSpec(
        name="summarize_bible",
        description="读取 project.plot_bible / premise / outline，不做总结（命名沿用 task spec）。",
        read_only=True,
        callable=_impl_summarize_bible,
    ),
)


def _register_default_tools(reg: AssistantToolRegistry) -> None:
    """把默认白名单挂到注册表；供 get_tool_registry() 首次调用时触发。

    已存在则跳过（防御反复 reset 后再次注册的重名）。
    """
    for spec in _DEFAULT_SPECS:
        if spec.name not in reg:
            reg.register(spec)
