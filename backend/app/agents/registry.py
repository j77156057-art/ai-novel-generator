"""Assistant Agent 工具注册表（ADR-2026-10-12 第 2 步）。

设计要点（与 b58c763 不变量对齐）：

- 角色锁定：Assistant 仅能引用 `read_only=True` 的工具；任何 `write_*` / `commit_*`
  工具被 Assistant 调用 → 注册表立即抛 `PermissionError`，**不**进入任何缓存。
- 工具实现 = 复用既有只读检索/查询入口（`hybrid_search` / `get_character` /
  `get_reminders` / Generation 选择查询 / Fact 选择查询 / plot_bible 读取 /
  `audit` 三类审核规则 / `log_audit` 日志规则），不复制 SQL、不引入平行
  ORM 路径、不复制第二套判据。
- 注册表是**进程级单例**，但**只**持有 ToolSpec（name / read_only / callable），
  不持有 db session。db session 由 router 通过 Depends(get_db) 注入，
  工具 callable 拿到的就是 router 已校验的 session。
- 失败隔离：invoke_tool(name, ...) 内部抛异常时**不**污染注册表；下一次的同 name
  调用照常重试（与 ProviderRegistry 的失败不入缓存同语义）。

助手页瘦身后新增的 4 个只读工具（`audit_corpus` / `audit_kg` /
`audit_generation` / `analyze_logs`）让 agent **自主**调用诊断、日志分析与
三类审核，而不再依赖前端页签。这些工具的 **callable 本身全部纯只读**，直接复用
audit / log_audit 的规则函数；「AgentRun + Evidence 两张审计表」的写入发生在
**router / 编排层**（`app.routers.assistant` 跑完工具后落盘），**不**在工具
callable 内部——所以工具在业务数据上是纯只读，进白名单是安全的。写操作（提案 /
调动生成）**不在**本表：它们走 `app.agents.pending_actions` 产出待确认卡片，
由用户点确认后调既有端点执行。

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
#
# 助手页瘦身后新增 4 个（audit_corpus / audit_kg / audit_generation /
# analyze_logs）：诊断、日志分析、三类审核从「前端页签」改为「agent 自主
# 调度」。这些工具的 **callable 本身全部纯只读**——它们直接复用 audit / log_audit
# 模块的规则函数，只读出违规项、不写任何业务数据。关于「AgentRun + Evidence
# 两张审计表」的写入：那**不是**发生在这些工具 callable 内部（callable 不碰
# 任何写路径），而是发生在 router / 编排层（app.routers.assistant 在跑完工具后
# 落盘 AgentRun 与 Evidence）。因此这些工具可以安全进白名单，让 agent 自己判断
# 何时需要；白名单同时也被 register() 当作运行时允许清单（见 §4.2① 的校验）。
ALLOWED_READ_ONLY_TOOLS: tuple[str, ...] = (
    "search_novel_chunks",
    "read_generation_segments",
    "get_character_profile",
    "get_plot_timeline",
    "get_fact_ledger",
    "get_reminders",
    "summarize_bible",
    "audit_corpus",
    "audit_kg",
    "audit_generation",
    "analyze_logs",
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
          3. spec.name 已在 _tools 中 → 重名也算错误（避免运行时调用的歧义）；
          4. spec.name 不在 ALLOWED_READ_ONLY_TOOLS 中 → fail-closed 白名单校验。
             这是 §4.2① 的核心修复：之前 ALLOWED_READ_ONLY_TOOLS 只在文档里
             列着、运行时从不读取，等于「信任注册」——任何新加的只读工具只要
             没命中黑名单前缀就能注册进来。现在它变成真正的运行时允许清单：
             未在清单里的工具一律拒绝，新增只读工具必须**先**进白名单。
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
        # §4.2① 运行时真白名单校验：未在 ALLOWED_READ_ONLY_TOOLS 里的工具一律
        # 拒绝（fail-closed），杜绝「注册即信任」绕过白名单的漏洞。
        if spec.name not in ALLOWED_READ_ONLY_TOOLS:
            raise PermissionError(
                f"工具 {spec.name!r} 不在只读白名单 ALLOWED_READ_ONLY_TOOLS 中；"
                "新增只读工具须先加入白名单"
            )
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


# ====================================================================
# 审核 / 日志分析：让 agent 自主调度（助手页瘦身后新增的 4 个只读工具）
# ====================================================================
#
# 复用优先，一个字都不复制判据：
# - 三类审核 → `app.agents.audit` 的 5 / 4 / 6 条规则（与
#   `POST /api/assistant/audit-*` 三个端点**同一个函数**）；
# - 日志分析 → `app.agents.log_audit.run_log_audit_rules`（与
#   `POST /api/assistant/analyze-logs` 同一个函数）。
#
# 复制出来的第二套判据一定会和第一套漂移：语料审核曾挖出 P1「空 novel_ids
# 静默广播全库」、KG 审核曾挖出「短路名搭长名便车误报共现」——这两个坑都
# 是在**端点侧**修的，若工具侧另写一份，它们会原样复现。


def _issues_to_dicts(report: Any) -> list[dict[str, Any]]:
    """DiagnosticReport → 九字段 issue dict 列表（与 diagnose 响应同构）。"""
    return [issue.to_dict() for issue in getattr(report, "issues", [])]


def _report_to_tool_result(
    report: Any, stats: dict[str, Any], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """规则报告 → 工具返回值（统一 `issues` / `count` / `rules_evaluated`）。

    三个审核工具 + 日志分析共用，保证 agent 拿到四份**结构同构**的结果：
    agent 的提示词里只需要写一套读法（「找 `issues[]`，每条有 rule/severity/
    suggestion」），而不是四套。
    """
    issues = _issues_to_dicts(report)
    result: dict[str, Any] = {
        "issues": issues,
        "count": len(issues),
        # findings 是 issues 的兼容别名：与 diagnose / analyze-logs 响应同构，
        # 前端一份渲染逻辑复用多处（直接删掉会静默变成空列表）。
        "findings": issues,
        "rules_evaluated": list(getattr(report, "rules_evaluated", [])),
        "stats": dict(stats or {}),
    }
    if extra:
        result.update(extra)
    return result


def _impl_audit_corpus(*, db: Any, project_id: int,
                       novel_ids: list[int] | None = None) -> dict[str, Any]:
    """语料审核（只读）：复用 `app.agents.audit.run_corpus_audit` 的 5 条规则。

    `novel_ids` 语义与 `POST /api/assistant/audit-corpus` 完全一致：
    - `None` → 全库语料（语料是全局的，不属于任何项目；project_id 只是
      审计锚点）；
    - 非空列表 → 只审这些；
    - **`[]` → 报 422，绝不广播全库**。

    最后一条是硬要求，不是保守取舍。历史 bug：`[]` 是 falsy，旧的
    `if novel_ids:` 让 `[]` 与 `None` 撞进同一分支，把「一本都没选」执行成
    「审全库」（真库 7941 分片时用户完全看不出来）。端点侧的
    `_require_novels` 已经拒了 `[]`，这里**独立地再拒一次**：工具可以被
    router 之外的路径直接调用，不能依赖调用方已经校验过。
    """
    from fastapi import HTTPException

    from app.agents.audit import run_corpus_audit

    if novel_ids is not None and not novel_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                "novel_ids 不能为空：要么不传该参数（审全部语料），"
                "要么至少给一个语料 id"
            ),
        )
    # 去重保序：同一个 id 传两遍不该让规则跑两遍
    scoped = list(dict.fromkeys(novel_ids)) if novel_ids is not None else None
    report, stats = run_corpus_audit(db, novel_ids=scoped)
    return _report_to_tool_result(
        report,
        stats,
        extra={"novel_ids": scoped},
    )


def _impl_audit_kg(*, db: Any, project_id: int) -> dict[str, Any]:
    """知识图谱审核（只读，按项目）：复用 `run_kg_audit` 的 4 条规则。

    只审 generated 图——参考语料图（`novel:{id}`）的卫生由 `audit_corpus`
    负责，两边不重叠。同一处问题在两个工具重复出现，agent 会当成两处毛病
    报给用户。

    project 不存在时**不**静默返回空 issues：那读起来就是「图谱很干净」。
    抛 404，与 `_require_project` 的口径一致。
    """
    from fastapi import HTTPException

    from app.agents.audit import run_kg_audit
    from app.models import Project

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    report, stats = run_kg_audit(db, project)
    return _report_to_tool_result(report, stats)


def _impl_audit_generation(*, db: Any, project_id: int) -> dict[str, Any]:
    """生成物审核（只读，按项目）：复用 `run_generation_audit` 的 6 条规则。

    零段落 / 空正文 / 段长离群 / AI 腔 / plot_bible 角色名单双向不一致。
    「像 AI」是风格判断，规则只给可量化信号，最终话语权在人。
    """
    from fastapi import HTTPException

    from app.agents.audit import run_generation_audit
    from app.models import Project

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    report, stats = run_generation_audit(db, project)
    return _report_to_tool_result(report, stats)


def _impl_analyze_logs(*, db: Any, project_id: int, status: str | None = None,
                       limit: int = 100) -> dict[str, Any]:
    """日志分析（只读）：复用 `run_log_audit_rules` 的 5 条规则。

    ## 为什么必须复用 router 的两个校验函数

    `status` / `limit` 在本工具里**直接调**
    `app.routers.assistant._validate_run_status` / `_validate_runs_limit`，
    而不是各写一份。同一个参数在两个入口给出不同结论，是本项目已经踩过的
    真实 bug（QA8 挖出 P2）：

    | limit | `GET /runs` | 原 `/analyze-logs` |
    |---|---|---|
    | `-1` | 400 | 200，且 SQLite `LIMIT -1` = 取全表 |
    | 非法 status | 400 | 200 + runs_analyzed=0（读起来像「没调用记录」） |

    静默失效比报错难查得多——「limit=-1 取全表」在生产上表现为一次慢查询，
    而不是一次明确的 400。工具侧再复制一份校验，等于把这个坑重新挖一遍。

    **延迟 import 是为了打断循环依赖**：`app.routers.assistant` 在模块顶层
    `from app.agents import invoke_tool`，而本模块是 `app.agents` 的一部分。
    函数体内 import 与既有 `_impl_*` 同一套做法（它们也都延迟 import
    `app.models` / `app.retrieval.search`）。

    抛出的 `HTTPException` 会原样冒泡到调用方：`assistant_qa` 的工具循环
    只对 `PermissionError` 转 403，其余异常记进 `tool_results[name]["error"]`
    ——参数非法时**明确报错**，不会被当成「没查到日志」静默通过。
    """
    from app.agents.log_audit import run_log_audit_rules
    from app.routers.assistant import _validate_runs_limit, _validate_run_status

    # 先校验后取数：非法输入不该先跑一遍 SQL 再失败。
    status = _validate_run_status(status)
    limit = _validate_runs_limit(limit)

    report, runs = run_log_audit_rules(db, project_id, status=status, limit=limit)
    return _report_to_tool_result(
        report,
        dict(report.tool_results),
        extra={
            "runs_analyzed": len(runs),
            "status_filter": status,
            "limit": limit,
        },
    )


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
    ToolSpec(
        name="audit_corpus",
        description=(
            "语料审核（只读）：复用 app.agents.audit 的 5 条规则——空/超短分片、"
            "重复分片、疑似注入串（水印）、缺 style_profile、未建 embedding。"
            "novel_ids 不传=全库语料，给列表=只审这些（空列表会被拒，不广播）。"
            "需要判断语料质量时调用。"
        ),
        read_only=True,
        callable=_impl_audit_corpus,
    ),
    ToolSpec(
        name="audit_kg",
        description=(
            "知识图谱审核（只读，按项目）：复用 app.agents.audit 的 4 条规则——"
            "孤儿节点、角色名冲突（与 diagnose 同口径）、已死角色挂未填坑、"
            "稳定共现但无边。怀疑图谱关系有遗漏或角色名混乱时调用。"
        ),
        read_only=True,
        callable=_impl_audit_kg,
    ),
    ToolSpec(
        name="audit_generation",
        description=(
            "生成物审核（只读，按项目）：复用 app.agents.audit 的 6 条规则——"
            "零段落、空正文、段长离群、AI 腔打分离群、plot_bible 角色名单"
            "双向不一致。用户问「这段写得怎么样 / 正文有没有问题」时调用。"
        ),
        read_only=True,
        callable=_impl_audit_generation,
    ),
    ToolSpec(
        name="analyze_logs",
        description=(
            "助手自身调用日志分析（只读）：复用 app.agents.log_audit 的 5 条规则——"
            "失败率异常、registry 越权拦截、本地兜底降级、空 evidence、耗时离群。"
            "status ∈ running/success/failed（非法值报错），limit ∈ [1,500]。"
            "用户问「助手最近是不是不正常 / 为什么变慢了」时调用。"
        ),
        read_only=True,
        callable=_impl_analyze_logs,
    ),
)


def _register_default_tools(reg: AssistantToolRegistry) -> None:
    """把默认白名单挂到注册表；供 get_tool_registry() 首次调用时触发。

    已存在则跳过（防御反复 reset 后再次注册的重名）。
    """
    for spec in _DEFAULT_SPECS:
        if spec.name not in reg:
            reg.register(spec)
