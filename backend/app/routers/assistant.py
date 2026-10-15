"""Assistant 只读检索 / 诊断 / 日志分析路由（ADR-2026-10-12 第 5 步第 2/3 步，
第 7 步增加日志读取与日志分析）。

端点：
- POST /api/assistant/qa         问答：拿证据 + 简短回答 + 置信度。
- POST /api/assistant/diagnose   结构化诊断：确定性规则先行，模型后置写解释。
- GET  /api/assistant/runs       助手读自己的日志：AgentRun 列表（只读）。
- GET  /api/assistant/runs/{id}  单条详情 + 该 run 的全部 Evidence（只读）。
- POST /api/assistant/analyze-logs  日志分析：对 AgentRun 历史跑确定性规则，
                                  产出 issues（失败率异常 / registry 拦截 /
                                  本地兜底降级 / 空 evidence / 耗时离群）。

诊断响应（第 3 步新增 `issues[]`）：
- 每条 issue 九个字段全填：`id` / `type` / `severity`(info|warn|error) /
  `title` / `claim` / `evidence[]` / `suggestion` / `rule`；
- 顶层保留 `answer` / `evidence[]` / `confidence` / `agent_run_id` / `version_id`；
- `findings` 是 `issues` 的兼容别名（第 2 步的调用方按 findings 读）。

/qa 响应新增 `pending_actions`（助手页瘦身后）：
- `list[dict]`，**缺省 `[]`** —— 既有响应契约不变，前端为空时完全不渲染该区域；
- 每项是一张待确认卡片的完整描述（`kind` / `title` / `detail` / `endpoint` /
  `method` / `body`），服务端把请求体拼好，前端点确认后原样回传；
- 本端点只**提出**，不执行任何写操作；执行仍走既有的提案创建端点与
  调动生成端点（见 `app.routers.assistant_proposals` /
  `app.routers.assistant_generation`），本文件不新增任何写入路径；
- 字段契约与 kind→端点映射住在 `app.agents.pending_actions`，**不**在本文件。
  原因：本文件受两处源码级静态断言约束
  （`test_assistant_readonly.py::test_assistant_router_has_no_write_path`、
  `test_assistant_diagnose.py::test_router_exposes_no_write_endpoints`），
  任何提案/版本端点的路径子串都不得出现在这里——端点表若写在本文件，
  会直接把这两条既有回归打红。

不变量：
- Assistant provider 走 `get_provider_registry().get('assistant')` —— Writer 单例
  **绝不能**被 Assistant 故障污染（失败时只回退到"只读检索关闭、诊断走本地规则"）。
- 响应必须包含 {answer, evidence[], confidence, agent_run_id}；
  evidence 项是 dict（含 ref_id / snippet / score / kind / source）。
- 端点**不**暴露任何写权限字段：请求体里只有 project_id / query / scope / version_id。
  任何 patch_json / target_table / target_id 等字段被请求体携带 → 422。
- 不引入 LangGraph 节点 / Deep Agents。
- 流式首字延迟相关超时（556c057 / c1a7179 合入的 180s）由 Writer 单例持有，Assistant 端
  不触发 stream_chat，故不会触碰 stream_timeout 默认值。

权限校验：
- router 内部以「调用方 = assistant role」为隐式假设（端点路径以 /api/assistant/ 开头）。
  工具白名单 = AssistantToolRegistry（write_*/commit_* 等被双重门拒绝）。
- project_id 必校验存在；不存在 → 404。
- 诊断规则本身住在 `app.agents.diagnosis`（纯计算、可离线复现）；
  router 只负责开 AgentRun、落 Evidence、调模型、拼响应。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import invoke_tool
from app.agents.diagnosis import run_diagnostic_rules
from app.agents.log_audit import (
    fetch_run_history,
    run_history_to_dicts,
    run_log_audit_rules,
    run_to_dict,
)
from app.db import get_db
from app.models import AgentRun, Evidence, Generation, Project
from app.routers.assistant_shared import (
    _build_evidence_row,
    _chat_with_assistant,
    _confidence_from_evidence,
    _ensure_assistant_provider,
    _now_utc,
    _request_hash,
    _require_project,
    evidence_rows_to_dicts,
    persist_evidence,
)
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant")

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


# ====================================================================
# 请求体 / 响应体
# ====================================================================

class _AssistantRequestBase(StrictRequest):
    """Assistant 端点请求体基类：禁用所有写权限字段。

    任何携带 write_* / commit_* / patch_* / update_* / delete_* / target_table /
    target_id / patch_json 等**写语义**字段的请求体都会被 pydantic v2 拒绝（422）。

    这是「权限即不变量」的写法：拼错字段名 / 试探越权请求都得 fail loud，
    与 schemas.StrictRequest 的 extra="forbid" 行为一致。
    """

    project_id: int
    # 问答场景：query；诊断场景：scope（待诊断的对象描述，如 "segment:7"）。
    query: str | None = None
    scope: str | None = None
    # version_id 留给后续 ADR 接 v0/v1/v2 视图；本轮**只**占位，不影响读写。
    version_id: int | None = None


class AssistantQARequest(_AssistantRequestBase):
    """POST /api/assistant/qa 请求体。"""

    # QA 场景必填 query；scope 可选（限定到某个段 / 角色 / 章节）。
    query: str  # type: ignore[assignment]


class AssistantDiagnoseRequest(_AssistantRequestBase):
    """POST /api/assistant/diagnose 请求体。"""

    # 诊断场景必填 scope（如 "all"、"segment:7"、"character:张三"）。
    scope: str  # type: ignore[assignment]


class AssistantAnalyzeLogsRequest(_AssistantRequestBase):
    """POST /api/assistant/analyze-logs 请求体（第 7 步交付 2）。

    只带「读哪段日志」与「用户想问什么」，不带任何写语义字段——继承
    `_AssistantRequestBase`（StrictRequest）后，patch_json / target_table
    之类一律 422。
    """

    # 分析范围过滤：status 缺省为全部（running 的历史也纳入统计口径，
    # 由规则自己决定怎么用）；limit 封顶取数，防日志量大时响应体爆炸。
    status: str | None = None
    limit: int = 100


# ====================================================================
# 内部小工具
# ====================================================================
# 全部收在 `app.routers.assistant_shared`（开 AgentRun / 落 Evidence /
# 调 Assistant provider 的骨架，被 qa|diagnose|runs|analyze-logs|audit-*|
# invoke-generation 共用），本文件顶部 import 后直接使用——既有测试按
# `assistant_router._ensure_assistant_provider()` 引用，仍解析到同一处。


# ====================================================================
# POST /api/assistant/qa
# ====================================================================


# ====================================================================
# POST /api/assistant/qa
# ====================================================================

@router.post("/qa")
def assistant_qa(body: AssistantQARequest, db: Session = Depends(get_db)) -> dict:
    """Assistant 问答：跑白名单工具，收集 Evidence，必要时调 Assistant 模型。

    失败隔离：
    - Assistant 模型构造/调用抛错 → 退化为「只返回检索 evidence + 兜底回答」，
      **不**让 Writer 单例受波及（异常原样冒泡前先持久化 evidence）。
    """
    project = _require_project(db, body.project_id)
    started_at = _now_utc()
    request_hash = _request_hash(body)

    # 1) 起 AgentRun（status=running；后续要么 success 要么 failed）
    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=started_at,
        finished_at=None,
        request_hash=request_hash,
        model="",  # 模型名待成功拿到 provider 时回填
    )
    db.add(run)
    db.flush()  # 拿到 run.id 给 evidence 外键用
    run_id = run.id

    # 2) 调用白名单工具：read_generation_segments + search_novel_chunks + get_reminders
    #    收集 evidence（不抛错即全部入 evidence 表；单工具失败时跳过那一项）
    evidence_rows: list[Evidence] = []
    tool_results: dict[str, Any] = {}
    for tool_name, kwargs in (
        ("read_generation_segments", {"limit": 5}),
        ("search_novel_chunks", {"query": body.query, "k": 5}),
        ("get_reminders", {}),
    ):
        try:
            result = invoke_tool(tool_name, db, project_id=project.id, **kwargs)
            tool_results[tool_name] = result
            # 工具结果 → evidence 行
            if tool_name == "read_generation_segments":
                for g in (result.get("segments") or [])[:5]:
                    snippet = (g.get("text") or "")[:200]
                    score = 0.6
                    row = _build_evidence_row(run_id, kind="generation",
                                               source="generation", ref_id=g.get("id", ""),
                                               snippet=snippet, score=score)
                    db.add(row); evidence_rows.append(row)
            elif tool_name == "search_novel_chunks":
                for h in (result.get("hits") or [])[:5]:
                    row = _build_evidence_row(run_id, kind="chunk",
                                               source="novel", ref_id=h.get("chunk_id", ""),
                                               snippet=h.get("text", "")[:200],
                                               score=float(h.get("score", 0.0)))
                    db.add(row); evidence_rows.append(row)
            elif tool_name == "get_reminders":
                # 提醒作为 evidence 也保留：决定置信度的旁证
                rem_count = (len(result.get("unresolved_plotholes") or [])
                             + len(result.get("character_constraints") or []))
                row = _build_evidence_row(
                    run_id, kind="reminder", source="kg",
                    ref_id="summary", snippet=f"{rem_count} 条提醒",
                    score=0.4,
                )
                db.add(row); evidence_rows.append(row)
        except PermissionError:
            # 黑名单工具被 Assistant 误调：直接 fail-fast（绝不静默）
            run.status = "failed"
            run.finished_at = _now_utc()
            run.model = "(blocked_by_registry)"
            db.commit()
            raise HTTPException(status_code=403, detail="Assistant 越权调用写工具")
        except Exception as exc:  # noqa: BLE001 - 单工具失败不拖垮整次调用
            logger.warning("assistant/qa 工具 %s 失败: %s", tool_name, exc)
            tool_results[tool_name] = {"error": str(exc)}

    # 3) 调 Assistant 模型：失败 → 仅返回 evidence + 兜底回答
    answer = ""
    model_label = ""
    try:
        provider = _ensure_assistant_provider()
        model_label = getattr(provider, "model", "") or getattr(provider, "label", "") or ""
        # 构造一个最小 system + user：把 evidence 拼进 user（不击穿 system 缓存）
        evidence_blob = "\n".join(
            f"- kind={r.kind} source={r.source} ref={r.ref_id} score={r.score} "
            f"snippet={(r.snippet or '')[:120]}"
            for r in evidence_rows[:5]
        ) or "(无 evidence)"
        system = "你是小说创作的诊断型助手。只能基于用户给出的 evidence 回答问题，"
        system += "禁止编造证据外的内容；无证据时明确说明。"
        user = (
            f"项目标题：{project.title or '未命名'}\n"
            f"问题：{body.query}\n\n"
            f"evidence:\n{evidence_blob}"
        )
        raw = provider.chat(system, user)
        answer = raw.strip() if isinstance(raw, str) else str(raw or "")
    except Exception as exc:  # noqa: BLE001 - Assistant 失败时仅返回 evidence + 兜底
        logger.warning("assistant/qa provider 失败：%s", exc)
        answer = "Assistant provider 不可用，仅返回检索证据。请基于以下 evidence 自行判断。"

    # 4) 收尾 AgentRun
    run.status = "success"
    run.finished_at = _now_utc()
    run.model = model_label or "(local_fallback)"
    db.commit()

    # 5) 待确认卡片：用户话里带明确生成指令时提出一张，**不执行**
    #    （执行由用户点确认后前端调既有端点，见 app.agents.pending_actions）
    pending_actions = _collect_pending_actions(db=db, project=project, query=body.query)

    # 6) 出参（每次都包含 answer / evidence[] / confidence / agent_run_id）
    return {
        "answer": answer,
        "evidence": evidence_rows_to_dicts(evidence_rows),
        "confidence": _confidence_from_evidence(evidence_rows),
        "agent_run_id": run_id,
        "tool_results": tool_results,
        "version_id": body.version_id,
        "pending_actions": pending_actions,
    }


def _collect_pending_actions(
    *, db: Session, project: Project, query: str
) -> list[dict[str, Any]]:
    """从用户 query 里收集「待用户确认」的写操作卡片。

    本函数只**提出**，绝不执行——助手页瘦身后，前端在对话流里渲染卡片，
    用户点确认才发请求。提案的创建 / 生成动作都走既有端点与既有状态机，
    这里不新增任何写入路径。

    判定规则在 `app.agents.pending_actions.propose_invoke_generation`
    （确定性关键词，不调模型）；本函数只负责注入它需要的 `has_segment`
    回调并做错误隔离：

    - `has_segment` 查的是本项目的段。**跨项目的段号提卡 = 提一张必然
      404 的卡**，等于骗用户点一次必然失败的按钮，所以只按 project_id 查。
    - 判定过程抛错（库读不到等）→ 记 warning 并返回空列表。提卡是增强，
      不该让一次问答整体 500；且空列表对前端是「不渲染该区域」，与既有
      契约一致。
    """
    from app.agents.pending_actions import propose_invoke_generation

    def _has_segment(segment_no: int) -> bool:
        return (
            db.scalars(
                select(Generation.id)
                .where(
                    Generation.project_id == project.id,
                    Generation.segment_no == segment_no,
                )
                .limit(1)
            ).first()
            is not None
        )

    try:
        action = propose_invoke_generation(
            project_id=project.id,
            query=query,
            has_segment=_has_segment,
        )
    except Exception as exc:  # noqa: BLE001 - 提卡失败不应拖垮问答
        logger.warning("assistant/qa 构造待确认卡片失败：%s", exc)
        return []
    return [action] if action else []


# ====================================================================
# POST /api/assistant/diagnose
# ====================================================================


@router.post("/diagnose")
def assistant_diagnose(body: AssistantDiagnoseRequest, db: Session = Depends(get_db)) -> dict:
    """Assistant 诊断：**规则先行、模型后置**。

    执行顺序（顺序本身就是不变量）：

    1. 跑确定性规则（`app.agents.diagnosis.run_diagnostic_rules`）—— 四条规则
       全部只读库内既有事实（KgNode 坑状态 / Fact 账本别名 / 哨兵状态表 /
       Generation 正文），不需要语言模型；
    2. 把规则产出的证据落成 Evidence 行（按 (kind, source, ref_id, snippet)
       去重：两条 issue 常引用同一条证据）；
    3. **仅当规则命中或请求带 query 时**才调 Assistant provider 写解释性文字。
       provider 构造/调用抛错 → 降级为纯规则结果，请求照常 200。

    响应顶层：`answer` / `issues[]` / `evidence[]` / `confidence` /
    `agent_run_id` / `version_id`，另附 `findings`（结构化 issue 的兼容别名）
    与 `tool_results`（排障用：各只读工具的原始返回）。

    失败隔离：
    - 单条规则失败 / 单个只读工具失败 / provider 失败 → 都不冒泡成 5xx，
      诊断是增强，不是主链路；
    - **例外**：`PermissionError`（角色锁被绕过）转 403 并把 run 记 failed ——
      越权必须被拒绝并留痕，降级会把它伪装成「没问题」；
    - 走 `get_provider_registry().get('assistant')`，Writer 单例不被触碰。
    """
    project = _require_project(db, body.project_id)
    started_at = _now_utc()
    request_hash = _request_hash(body)

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=started_at,
        finished_at=None,
        request_hash=request_hash,
        model="",
    )
    db.add(run)
    db.flush()
    run_id = run.id

    # ---- 1) 规则先行 ----
    # PermissionError（角色锁被绕过）**不降级**：转成 403 并把这次 run 记成
    # failed 后再抛，与 /qa 完全一致。降级在这里是错的 —— 规则取不到数据会被
    # 用户读成「没问题」，而真实原因是「Assistant 试图调写工具」，两种读法
    # 给出的行动建议完全相反。
    try:
        report = run_diagnostic_rules(db, project, scope=body.scope, query=body.query)
    except PermissionError:
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = "(blocked_by_registry)"
        db.commit()
        raise HTTPException(status_code=403, detail="Assistant 越权调用写工具")
    tool_results: dict[str, Any] = dict(report.tool_results)

    # ---- 2) 证据落库（去重；只写助手自己的审计表 Evidence）----
    evidence_rows = persist_evidence(db, run_id, report)

    # ---- 3) 模型后置：仅在规则命中或用户主动 query 时解释 ----
    issues = [i.to_dict() for i in report.issues]
    need_llm = bool(report.issues) or bool((body.query or "").strip())
    if need_llm:
        answer, model_label = _explain_issues(
            project=project,
            body=body,
            issues=issues,
            evidence_rows=evidence_rows,
            run_id=run_id,
        )
        # provider 失败时 model_label 为 "" → run.model 记 (local_fallback)
    else:
        model_label = ""
        answer = "规则化检测未命中任何已知问题模式（4 条规则全部已执行）；未调用 Assistant 模型。"

    run.status = "success"
    run.finished_at = _now_utc()
    run.model = model_label or "(local_fallback)"
    db.commit()

    return {
        "answer": answer,
        "issues": issues,
        # findings 保留为 issues 的兼容别名：第 2 步的调用方按 findings 读，
        # 直接删掉会静默变成空列表 —— 这类「静默失效」比报错更难查。
        "findings": issues,
        "evidence": evidence_rows_to_dicts(evidence_rows),
        "confidence": _confidence_from_evidence(evidence_rows),
        "agent_run_id": run_id,
        "tool_results": tool_results,
        "rules_evaluated": list(report.rules_evaluated),
        "version_id": body.version_id,
    }


def _explain_issues(
    *,
    project: Project,
    body: AssistantDiagnoseRequest,
    issues: list[dict],
    evidence_rows: list[Evidence],
    run_id: int,
) -> tuple[str, str]:
    """调 Assistant provider 为规则结论写解释性文字；失败则返回纯规则兜底文案。

    返回 `(answer, model_label)`：model_label 为空串表示「模型不可用，
    走的是纯规则降级」，调用方据此把 `AgentRun.model` 记成 `(local_fallback)`
    —— 事后翻审计表就能看出这次诊断到底有没有过模型。

    system 提示词明确三件事：不得编造 evidence 之外的事实；不得改写正文；
    结论与推测必须分开（PRD 第 3 条）。
    """
    if issues:
        fallback = (
            f"规则化检测命中 {len(issues)} 个问题（Assistant provider 不可用，"
            f"以下为纯规则结果，未做任何模型解释）："
            + "；".join(f"[{i['severity']}] {i['title']}" for i in issues)
        )
    else:
        # 带 query 但规则一条没命中是最常见的组合（用户在问规则管不到的事）。
        # 这时说「命中 0 个问题」是对的，但必须同时说明证据来自检索而非规则，
        # 否则用户会以为「没报问题 = 没问题」。
        fallback = (
            "规则化检测未命中任何已知问题模式（4 条规则全部已执行）；"
            "Assistant provider 不可用，无法对检索证据做解释，请直接查看 evidence。"
        )
    evidence_blob = "\n".join(
        f"- kind={r.kind} source={r.source} ref={r.ref_id} "
        f"snippet={(r.snippet or '')[:120]}"
        for r in evidence_rows[:8]
    ) or "(无本地证据)"
    system = (
        "你是小说创作诊断助手。只能基于用户给出的 evidence 与规则结论回答；"
        "禁止编造证据之外的内容，无证据时明确说明。"
        "把「结论」与「推测」分开标注。你不生产正文，也不得直接给出改写后的段落。"
    )
    user = (
        f"项目：{project.title or '未命名'}\n"
        f"scope：{body.scope}\n"
        f"规则命中（severity/rule/title/claim）：\n"
        f"{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
        f"evidence:\n{evidence_blob}\n\n"
        f"用户补充说明：{body.query or '(无)'}\n"
        f"AgentRun id：{run_id}（仅供你复述，不要据此编造内容）"
    )
    return _chat_with_assistant(system=system, user=user, fallback=fallback)


# ====================================================================
# 第 7 步：日志读取（GET /runs、GET /runs/{id}）与日志分析
# （POST /analyze-logs）
# ====================================================================

#: GET /runs 允许的 status 过滤值（与 AgentRun.status 的写入口径一致）。
_RUN_STATUSES: tuple[str, ...] = ("running", "success", "failed")

#: limit 的合法区间：日志可能很多，取数要封顶；下限 1 防 limit=0 的
#: 「看起来成功、其实什么都没取」请求。
_RUNS_LIMIT_MIN = 1
_RUNS_LIMIT_MAX = 500


def _validate_run_status(status: str | None) -> str | None:
    """校验 status 过滤值；非法值 fail loud（400）而不是静默返回空列表。

    静默把非法 status 当成「没有匹配的 run」返回，读起来就是「这段时间
    没有调用记录」——而真实原因是拼写错了。这类静默失效比报错难查得多。
    """
    if status is None or status == "":
        return None
    if status not in _RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status 必须是 {list(_RUN_STATUSES)} 之一，收到 {status!r}",
        )
    return status


def _validate_runs_limit(limit: int) -> int:
    """校验 limit；越界 fail loud（400），不悄悄截断到边界值。"""
    if limit < _RUNS_LIMIT_MIN or limit > _RUNS_LIMIT_MAX:
        raise HTTPException(
            status_code=400,
            detail=(
                f"limit 必须在 [{_RUNS_LIMIT_MIN}, {_RUNS_LIMIT_MAX}] 之间，"
                f"收到 {limit}"
            ),
        )
    return limit


@router.get("/runs")
def assistant_runs(
    project_id: int,
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> dict:
    """助手读自己的日志：AgentRun 列表（**只读**，连 AgentRun 都不开）。

    每条 run 带回 id / status / model / note / started_at / finished_at /
    duration_ms / request_hash / evidence_count —— 「这段时间助手做了什么」
    一屏可读，不需要再翻 Evidence 表。

    刻意不开 AgentRun：「读日志」这个动作如果也要被审计，走
    POST /analyze-logs（它开 run、落 Evidence、可被下次分析复核）。
    """
    _require_project(db, project_id)
    status = _validate_run_status(status)
    limit = _validate_runs_limit(limit)
    runs = fetch_run_history(db, project_id, status=status, limit=limit)
    items = run_history_to_dicts(runs)
    return {
        "runs": items,
        "count": len(items),
        "by_status": {
            s: sum(1 for r in items if r["status"] == s) for s in _RUN_STATUSES
        },
        "status_filter": status,
        "limit": limit,
    }


@router.get("/runs/{run_id}")
def assistant_run_detail(
    run_id: int,
    project_id: int | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """单条 run 详情 + 该 run 的全部 Evidence（**只读**）。

    `project_id` 可选：给了就做跨项目访问校验，不匹配时返回 404 而不是
    403 —— 403 会告诉调用方「这条 run 存在但不属于你」，用本端点探测
    别家项目的 run id 就成了可能；404 对两种情形（不存在 / 不属于你）
    给同一答案。

    Evidence 的 `score` 在 SQLite 上是 String 列（见 models.py 注释），
    出参统一转 float——调用方不该知道存储引擎的怪癖。
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="agent run 不存在")
    if project_id is not None and run.project_id != project_id:
        raise HTTPException(status_code=404, detail="agent run 不存在")

    evidence_rows = db.scalars(
        select(Evidence).where(Evidence.run_id == run_id).order_by(Evidence.id)
    ).all()
    run_dict = run_history_to_dicts([run_to_dict(run)])[0]
    run_dict["evidence_count"] = len(evidence_rows)
    return {
        "run": run_dict,
        "evidence": evidence_rows_to_dicts(evidence_rows),
    }


@router.post("/analyze-logs")
def assistant_analyze_logs(
    body: AssistantAnalyzeLogsRequest, db: Session = Depends(get_db)
) -> dict:
    """日志分析：对某项目的 AgentRun 历史跑确定性规则，产出 issues。

    **这是只读分析**：不写任何业务表（Generation / Fact / KgNode 一概
    不碰），只写自己的审计表（AgentRun + Evidence）。issues 仍走现有
    diagnose 的九字段结构，可被转成提案（EditProposal.from_issue）。

    执行顺序（顺序本身就是不变量，与 diagnose 一致）：

    1. 开 AgentRun（role=assistant，note 标明「日志分析」）；
    2. 跑 `app.agents.log_audit.run_log_audit_rules` 五条确定性规则
       （失败率异常 / registry 拦截 / 本地兜底降级 / 空 evidence /
       耗时离群）——**把自己排除在被分析历史之外**，否则
       `empty_evidence_runs` 会把「这次分析自己」报成问题；
    3. 规则产出的 run 证据落 Evidence 行（kind="run"，去重）；
    4. **仅当规则命中或请求带 query 时**才调 Assistant provider 写解释；
       provider 失败 → 降级为纯规则结果，请求照常 200；
    5. 收尾 AgentRun（success）。

    失败隔离与 diagnose 相同：单规则失败不拖垮整次分析（记
    `tool_results["rule_errors"]`）；PermissionError 不可能出现在本
    路径（规则只 SELECT agent_runs / evidence，不经工具注册表），
    故不设 403 分支——没有写工具可越权。
    """
    project = _require_project(db, body.project_id)
    # status / limit 与 `GET /runs` 共用同一套校验，**在开 AgentRun 之前**跑：
    # 非法输入不该留下审计行（否则日志里会出现一条「参数错了但查过」的记录，
    # 反而污染被分析的历史）。
    # 修复前本端点缺这两个校验：status 拼错 → 200 且 runs_analyzed=0（静默空跑，
    # 读起来像「这段时间没有调用记录」）；limit=-1 → SQLite `LIMIT -1` = 无上限，
    # 直接取全表。同一参数在 /runs 400、在这里 200，属该补的一致性。
    _validate_run_status(body.status)
    _validate_runs_limit(body.limit)
    started_at = _now_utc()
    request_hash = _request_hash(body)

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=started_at,
        finished_at=None,
        request_hash=request_hash,
        model="",
        note="日志分析（只读）",
    )
    db.add(run)
    db.flush()
    run_id = run.id

    # ---- 1) 规则先行（把自己排除在外）----
    try:
        report, runs = run_log_audit_rules(
            db,
            project.id,
            status=body.status,
            limit=body.limit,
            exclude_run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 - 取数失败也要留痕再降级
        # 规则引擎本身只做 SELECT；真到此地说明库出了问题。降级为「0 条
        # 可分析」并明说，而不是 500 —— 日志分析是增强，不是主链路。
        logger.warning("assistant/analyze-logs 规则跑批失败：%s", exc)
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = "(local_fallback)"
        run.note = f"日志分析失败：{str(exc)[:120]}"
        db.commit()
        raise HTTPException(
            status_code=500, detail=f"日志分析失败：{str(exc)[:200]}"
        )
    tool_results: dict[str, Any] = dict(report.tool_results)

    # ---- 2) 证据落库（kind="run"，去重）----
    evidence_rows = persist_evidence(db, run_id, report)

    # ---- 3) 模型后置：仅在规则命中或用户主动 query 时解释 ----
    issues = [i.to_dict() for i in report.issues]
    need_llm = bool(report.issues) or bool((body.query or "").strip())
    if need_llm:
        answer, model_label = _explain_log_issues(
            project=project,
            body=body,
            issues=issues,
            runs_analyzed=len(runs),
            run_id=run_id,
        )
    else:
        model_label = ""
        answer = (
            f"已分析 {len(runs)} 条历史调用，五条规则全部已执行，未命中任何"
            f"已知异常模式；未调用 Assistant 模型。"
        )

    run.status = "success"
    run.finished_at = _now_utc()
    run.model = model_label or "(local_fallback)"
    db.commit()

    return {
        "answer": answer,
        "issues": issues,
        # findings 兼容别名：与 diagnose 响应同构，前端一份渲染逻辑两处复用
        "findings": issues,
        "evidence": evidence_rows_to_dicts(evidence_rows),
        "confidence": _confidence_from_evidence(evidence_rows),
        "agent_run_id": run_id,
        "tool_results": tool_results,
        "rules_evaluated": list(report.rules_evaluated),
        # 「分析了多少条」必须和规则看到的是同一批（run_log_audit_rules
        # 把被分析历史一起返回，就是为了这里对得上号）
        "runs_analyzed": len(runs),
        "version_id": body.version_id,
    }


def _explain_log_issues(
    *,
    project: Project,
    body: AssistantAnalyzeLogsRequest,
    issues: list[dict],
    runs_analyzed: int,
    run_id: int,
) -> tuple[str, str]:
    """调 Assistant provider 为日志分析结论写解释性文字。

    与 `_explain_issues`（诊断）同款契约：provider 失败 →
    `(fallback, "")`，调用方据此把 AgentRun.model 记成 `(local_fallback)`。

    system 提示词明确两件事：结论必须引用规则给出的 run id，不得编造
    没有出现在 issues 里的 run；不得给出「直接改库」类建议——日志分析
    的产出物是 issue 与提案，不是修复动作本身。
    """
    if issues:
        fallback = (
            f"日志分析命中 {len(issues)} 个问题（Assistant provider 不可用，"
            f"以下为纯规则结果，未做任何模型解释）："
            + "；".join(f"[{i['severity']}] {i['title']}" for i in issues)
        )
    else:
        fallback = (
            f"已分析 {runs_analyzed} 条历史调用，五条规则全部已执行，未命中"
            f"任何已知异常模式；Assistant provider 不可用，无法对检索证据"
            f"做解释，请直接查看 evidence。"
        )
    system = (
        "你是小说创作平台的运维诊断助手，负责解释助手调用日志的分析结论。"
        "只能基于用户给出的规则结论回答；禁止编造没有出现在结论里的 run id"
        "或调用记录。把「结论」与「推测」分开标注。你只解释与建议，"
        "不执行任何修复动作。"
    )
    user = (
        f"项目：{project.title or '未命名'}\n"
        f"分析范围：最近 {body.limit} 条"
        f"（status 过滤：{body.status or '全部'}，实际分析 {runs_analyzed} 条）\n"
        f"规则命中（severity/rule/title/claim）：\n"
        f"{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
        f"用户补充说明：{body.query or '(无)'}\n"
        f"AgentRun id：{run_id}（本次分析自身，不参与被分析历史）"
    )
    return _chat_with_assistant(system=system, user=user, fallback=fallback)
