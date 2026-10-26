"""Assistant 提案审批 / 应用 / 回滚路由（ADR-2026-10-12 第 5 步第 4 步）。

这是全项目第一条写入路径，路由层的职责被刻意压到最薄：
**校验 → 调 `app.agents.proposals` 的状态机 → 映射异常成 HTTP 状态码**。
所有业务语义（漂移检测、事务边界、patch 白名单）都在proposals.py 里，
这里不重复实现，只做「HTTP 世界的话」。

## 端点清单

| 方法 | 路径 | 作用 | 写业务表？ |
|---|---|---|---|
| POST | `/api/assistant/proposals` | 由 issue / 用户显式指令生成 pending 提案 | ❌ |
| GET | `/api/assistant/proposals` | 列表（按 project_id / status 过滤） | ❌ |
| GET | `/api/assistant/proposals/{id}` | 详情（含 diff、基线指纹、证据） | ❌ |
| POST | `/api/assistant/proposals/{id}/approve` | pending → approved | ❌ |
| POST | `/api/assistant/proposals/{id}/reject` | pending → rejected | ❌ |
| POST | `/api/assistant/proposals/{id}/apply` | approved → applied | ✅ **单条唯一入口** |
| POST | `/api/assistant/proposals/apply-selected` | 显式枚举的批量 approved → applied（整批同成同败） | ✅ 受控批量 |
| POST | `/api/assistant/versions/{id}/restore` | 从快照恢复（生成新 Version） | ✅ |
| GET | `/api/assistant/versions` | 版本历史 | ❌ |
| GET | `/api/assistant/versions/diff` | 两个版本的内容 diff | ❌ |
| GET | `/api/assistant/prompts/active` | 当前生效提示词来源 | ❌ |
| GET | `/api/assistant/prompts/versions` | 提示词覆写历史 | ❌ |

## 三条产品红线的落点

1. **Assistant 不能写**：`/apply` 与 `/restore` 的路径前缀虽在 `/api/assistant/`
   下，但它们是**用户审批通道**—— 请求体必须带 `proposal_id` / `version_id`
   与显式 `action`，且 proposal 必须先处于 `approved`。
   Assistant 模型既没有这两个端点的调用权（工具白名单里没有），也没有办法
   自己把提案推到 `approved`（approve 端点同样要求显式 action + actor）。
   请求体继承 `StrictRequest`：携带 `apply_all` / `proposal_ids` 之类批量
   语义字段 → 422，从协议层就杜绝「顺便把所有 pending 都应用」。

2. **创建必须用户显式指定**：`CreateProposalRequest` 要求
   `type` + `target_kind` + `target_id` + `after_text` 四者齐全。
   `from_issue` 只能提供 `reason` / `evidence` 的素材，**不能**替用户决定
   改哪儿、改成什么。

3. **漂移即 stale**：apply 前重算目标哈希，不一致 → 提案置 `stale` + 409。

## 与既有 Assistant 只读端点的关系

`/qa`、`/diagnose` 在 `routers/assistant.py`，本文件不碰它们。
两个router 共用 `/api/assistant` 前缀（FastAPI 允许，前缀不重复注册即可）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import proposals as proposal_lib
from app.agents.diagnosis import run_diagnostic_rules
from app.agents.prompt_overrides import (
    PROMPT_KEYS,
    active_prompt_report,
    list_override_versions,
)
from app.db import get_db
from app.models import AgentRun, EditProposal, Project, Version
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant_proposals")

router = APIRouter(prefix="/api/assistant", tags=["assistant-proposals"])


# ====================================================================
# 请求体
# ====================================================================


class CreateProposalRequest(StrictRequest):
    """POST /api/assistant/proposals 请求体。

    `type` / `target_kind` / `target_id` / `after_text` 四者必填 —— 这是
    「提案只能由用户显式指定改什么」的协议层落点。少任何一个 → 422。

    `from_issue` 是可选的**素材引用**：它只贡献 `reason` 与 `evidence`，
    不参与决定修改目标。用户可以在诊断 issue 的基础上快速起草，但仍必须
    自己敲下 `after_text`。
    """

    project_id: int
    type: Literal["text_edit", "prompt_edit", "fact_resolution", "corpus_text_edit"]
    target_kind: Literal[
        "generation_text", "project_plot_bible", "prompt_override", "novel_chunk_text",
    ]
    target_id: str
    after_text: str
    reason: str = ""
    # 影响范围 / 自定义 scope；`scope.patch` 会覆盖默认 patch 结构。
    scope: dict[str, Any] | None = None
    # 关联证据（结构化 dict 列表）；与 from_issue 合并。
    evidence: list[dict[str, Any]] | None = None
    # 关联诊断 issue 的 id（`run_diagnostic_rules` 产出的 issue.id）。
    from_issue: str | None = None
    # 显式指定风险等级；不给则按 type 取默认（fact_resolution=high…）。
    risk_level: Literal["low", "medium", "high"] | None = None
    # 关联的 AgentRun.id；不给则本端点自己开一条 status=success 的审计行。
    run_id: int | None = None
    created_by: str | None = None


class BodyRepairRequest(StrictRequest):
    project_id: int
    diagnostic_run_id: int
    generation_id: int
    # 单区间修订：start/end/conflict_source_id 三者齐全。
    start: int | None = None
    end: int | None = None
    conflict_source_id: str | None = None
    # 多区间（依赖链）修订：ranges 给出整组修订范围，共享同一违例根因。
    ranges: list[dict[str, Any]] | None = None
    dependency_note: str = ""
    objective: str
    preserve_source_ids: list[str]
    base_content_hash: str
    allow_delete: bool = False


@router.post('/body-repairs')
def create_local_body_repair(req: BodyRepairRequest, db: Session = Depends(get_db)):
    """Explicit write boundary: creates pending proposal and independent audit, never applies."""
    from app.agents.body_repair import create_body_repair
    payload = req.model_dump()
    if payload.get('ranges'):
        # 多区间修订：区间集合就是范围，单区间三件套不再适用。
        payload.pop('start', None)
        payload.pop('end', None)
        payload.pop('conflict_source_id', None)
    try:
        return create_body_repair(db, **payload)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class BodyAcceptanceRequest(StrictRequest):
    """POST /api/assistant/body-acceptance 请求体。

    `confirm` 必须为 True —— 整段验收会发起一次真实模型调用，客户端不能顺手触发。
    `base_content_hash` 是用户确认的**当前正文指纹**：正文变了就 409，不替用户验收别的版本。
    """

    project_id: int
    base_content_hash: str
    confirm: bool = False


@router.post('/body-acceptance')
def accept_current_body(req: BodyAcceptanceRequest, db: Session = Depends(get_db)):
    """整段验收：把全局验收义务关闭到当前正文（或诚实报告还差什么）。

    覆盖证据不足时**不发模型请求**，直接返回 `coverage_incomplete` 与未覆盖范围。
    """
    from app.agents.body_acceptance import run_body_acceptance
    if not req.confirm:
        raise HTTPException(status_code=422, detail='confirmation_required')
    try:
        report = run_body_acceptance(db, req.project_id, base_content_hash=req.base_content_hash)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409 if str(exc).startswith('stale') else 422,
                            detail=str(exc)) from exc
    return report


class DerivedRebuildRequest(StrictRequest):
    project_id: int
    base_fingerprint: str
    action: Literal['rebuild', 'unlock']


@router.post('/derived-rebuilds')
def rebuild_project_derived(req: DerivedRebuildRequest, db: Session = Depends(get_db)):
    """`rebuild`：重建摘要/completion/KG/事实，**不**解除生成守卫。

    `unlock`：仅当重建全部成功且回读通过后，才解除守卫放行生成。
    """
    from app.agents.derived_rebuild import rebuild_derived, unlock_generation
    try:
        if req.action == 'unlock':
            return unlock_generation(db, req.project_id, base_fingerprint=req.base_fingerprint)
        return rebuild_derived(db, req.project_id, base_fingerprint=req.base_fingerprint)
    except PermissionError as exc:
        raise HTTPException(403, detail=str(exc)) from exc
    except ValueError as exc:
        code = str(exc)
        if code == 'project_not_found':
            raise HTTPException(404, detail=code) from exc
        # unlock 的每一个失败码都表示「还没准备好」，不是「请求写错了」。
        if req.action == 'unlock' or code.startswith('stale'):
            raise HTTPException(409, detail=code) from exc
        raise HTTPException(422, detail=code) from exc


class ProposalActionRequest(StrictRequest):
    """approve / reject / apply 的请求体。

    `action` 必填且必须与路径动作一致 —— 「显式 action」是红线之一：
    同一个请求体不能被复用到另一个动作上（少写了 action 就该 422，
    而不是让服务端替调用方猜）。
    """

    action: Literal["approve", "reject", "apply"]
    actor: str | None = None
    note: str | None = None


class RestoreVersionRequest(StrictRequest):
    """POST /api/assistant/versions/{id}/restore 请求体。"""

    action: Literal["restore"]
    actor: str | None = None
    note: str | None = None


class ApplySelectedRequest(StrictRequest):
    """POST /api/assistant/proposals/apply-selected 请求体（第 10 步，受控批量应用）。

    **必须显式枚举** proposal id —— `proposal_ids` 是必填的整数列表，不接受
    任何通配符 / 隐式范围：`all` / `status` / `project_id` 之类字段会被
    `StrictRequest`（extra=forbid）直接 422。空列表同样 422（不把「没给 id」
    当成「全给」）。条数上限在端点里按 `MAX_BULK_APPLY` 校验。
    """

    action: Literal["apply"]
    proposal_ids: list[int]
    actor: str | None = None


# ====================================================================
# 异常 → HTTP 映射（唯一一处集中映射，不散落在各端点里）
# ====================================================================


def _raise_http(exc: Exception) -> None:
    """把proposals 域异常映射成 HTTPException 后原样抛出。

    映射表：

    | 异常 | 状态码 | 语义 |
    |---|---|---|
    | `PatchRejected` | 422 | patch 越界 / target 不匹配 / 空变更 |
    | `InvalidTransition` | 409 | 状态机拒绝（含重复 apply、stale 后再 apply） |
    | `TargetDrifted` | 409 | 目标已变，提案已置 stale |
    | `TargetMissing` | 404 | 目标行不存在或不属于本项目 |
    | `ProposalError` | 400 | 其它提案域错误 |
    | 其它异常 | 500 | 未预期错误（此时事务已由 apply/restore 回滚） |
    """
    if isinstance(exc, proposal_lib.PatchRejected):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if isinstance(exc, proposal_lib.InvalidTransition):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, proposal_lib.TargetDrifted):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale",
                "message": str(exc),
                "expected_hash": exc.expected_hash,
                "current_hash": exc.current_hash,
                "hint": "目标内容已被其它改动更新，请基于最新内容重新生成提案",
            },
        ) from exc
    if isinstance(exc, proposal_lib.TargetMissing):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, proposal_lib.BulkApplyConflict):
        raise HTTPException(status_code=409, detail=exc.report) from exc
    if isinstance(exc, proposal_lib.ProposalError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=500, detail=f"提案操作失败: {exc}") from exc


# ====================================================================
# 内部工具
# ====================================================================


def _require_project(db: Session, project_id: int) -> Project:
    proj = db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="project 不存在")
    return proj


def _get_proposal(db: Session, proposal_id: int) -> EditProposal:
    proposal = db.get(EditProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"提案 {proposal_id} 不存在")
    return proposal


def _require_run(db: Session, run_id: int, project_id: int) -> AgentRun:
    """取 AgentRun 并**校验它确实属于本项目**。

    跨项目 run_id 必须被拒：EditProposal 表上同时挂着 run_id（审计行）与
    project_id（归属），两者不一致会让「这条提案为什么提的」溯源到另一个
    项目的某次诊断调用 —— 回放审计时读到的是别的项目的数据。
    """
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"AgentRun {run_id} 不存在")
    if int(run.project_id) != int(project_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"AgentRun {run_id} 属于项目 {run.project_id}，不属于当前项目 "
                f"{project_id}；run_id 必须与 project_id 同属一个项目"
            ),
        )
    return run


def _open_run(db: Session, project_id: int, payload: dict[str, Any]) -> int:
    """为一次「创建提案」开一条 AgentRun 审计行并返回 id。

    `status="success"`：提案创建成功不代表 Assistant 做了什么，只代表
    「系统记下了这条待审建议」。`role="assistant"` 是硬约束——
    审计表里出现别的 role 说明有别的链路混进来了。
    """
    run = AgentRun(
        project_id=project_id,
        role="assistant",
        status="success",
        started_at=proposal_lib._now_utc(),
        finished_at=proposal_lib._now_utc(),
        request_hash=proposal_lib.request_hash(payload),
        model="(proposal_authoring)",
    )
    db.add(run)
    db.flush()
    return int(run.id)


def _collect_issue_material(
    db: Session, project: Project, issue_id: str
) -> tuple[str, list[dict[str, Any]]]:
    """按 issue.id 取诊断结论的 `suggestion` 与证据，作为提案的素材。

    **只取素材，不取修改目标** —— 返回的 reason 会被用户的 reason 覆盖
    （见 create_proposal 里的优先级），证据则追加到用户给的证据之后。
    找不到该 issue → 404：悄悄生成一条无根据的提案比报错危险得多。
    """
    report = run_diagnostic_rules(db, project, scope="all")
    for issue in report.issues:
        if issue.id == issue_id:
            return (
                issue.suggestion,
                [ev.to_dict() for ev in issue.evidence],
            )
    raise HTTPException(
        status_code=404,
        detail=f"诊断 issue {issue_id!r} 不存在（可用 scope='all' 重跑 /diagnose 查看全部 issue id）",
    )


# ====================================================================
# POST /api/assistant/proposals
# ====================================================================


@router.post("/proposals")
def create_proposal(
    body: CreateProposalRequest, db: Session = Depends(get_db)
) -> dict:
    """创建一条 `pending` 提案。**业务表零写入**。

    两种触发方式（都必须由用户显式指定改什么）：

    1. 用户直接给 `type` + `target_kind` + `target_id` + `after_text`；
    2. 用户给上述四项 + `from_issue`（诊断 issue id），issue 的 suggestion
       作为 reason 的**默认值**、证据并入 evidence—— 但改哪儿、改成什么
       仍然只来自请求体。

    创建后提案停在 `pending`：正文 / 剧情圣经 / 提示词都没有被改动，
    要生效必须再走 approve → apply 两步。
    """
    project = _require_project(db, body.project_id)

    reason = body.reason or ""
    evidence: list[dict[str, Any]] = list(body.evidence or [])
    if body.from_issue:
        issue_reason, issue_evidence = _collect_issue_material(
            db, project, body.from_issue
        )
        if not reason:
            reason = issue_reason
        known = {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in evidence}
        for ev in issue_evidence:
            key = json.dumps(ev, sort_keys=True, ensure_ascii=False)
            if key not in known:
                evidence.append(ev)
                known.add(key)

    run_id = (
        _require_run(db, body.run_id, project.id).id if body.run_id is not None
        else _open_run(db, project.id, body.model_dump(mode="json"))
    )

    try:
        proposal = proposal_lib.build_proposal(
            db,
            run_id=run_id,
            project_id=project.id,
            proposal_type=body.type,
            target_kind=body.target_kind,
            target_id=body.target_id,
            after_text=body.after_text,
            reason=reason,
            scope=body.scope,
            evidence=evidence,
            risk_level=body.risk_level,
            created_by=body.created_by,
            prompt_keys=PROMPT_KEYS,
        )
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 统一映射成 HTTP 状态码
        db.rollback()
        _raise_http(exc)

    db.refresh(proposal)
    return {
        "proposal": proposal_lib.proposal_to_dict(proposal),
        "next_action": "approve",
        "note": "提案已创建，业务内容未改动；需approve 后再 apply 才会生效",
    }


# ====================================================================
# GET /api/assistant/proposals
# ====================================================================


@router.get("/proposals")
def list_proposals(
    project_id: int,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """按项目列出提案；`status` 可选过滤（非法值→ 422）。"""
    _require_project(db, project_id)
    if status is not None and status not in proposal_lib.PROPOSAL_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status 必须是 {list(proposal_lib.PROPOSAL_STATUSES)} 之一",
        )
    q = select(EditProposal).where(EditProposal.project_id == project_id)
    if status is not None:
        q = q.where(EditProposal.status == status)
    rows = list(db.scalars(q.order_by(EditProposal.id.desc()).limit(limit)).all())
    return {
        "project_id": project_id,
        "status": status,
        "count": len(rows),
        "proposals": [proposal_lib.proposal_to_dict(r) for r in rows],
    }


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int, db: Session = Depends(get_db)) -> dict:
    """提案详情：含 unified diff、目标基线指纹、关联证据、生命周期时间戳。"""
    proposal = _get_proposal(db, proposal_id)
    return {"proposal": proposal_lib.proposal_to_dict(proposal)}


# ====================================================================
# POST /api/assistant/proposals/{id}/approve | reject
# ====================================================================


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int,
    body: ProposalActionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """pending → approved。**只改状态，不写任何业务表。**

    审批与落库刻意分成两个端点：审批是人的判断，落库是系统的动作。
    合在一起就等于给了「一键生效」，而「先看清楚 diff 再决定」正是这条
    流程存在的理由。
    """
    if body.action != "approve":
        raise HTTPException(
            status_code=422,
            detail=f"action 必须是 'approve'，收到 {body.action!r}",
        )
    proposal = _get_proposal(db, proposal_id)
    try:
        proposal_lib.validate_action(proposal.status, "approve")
        scope = proposal.scope_json or {}
        if (scope.get('independent_verification_required')
                and proposal_lib.repair_write_allowed(scope) is None):
            raise proposal_lib.InvalidTransition(proposal.status, 'repair_semantic_acceptance_required')
        proposal.status = "approved"
        proposal.approved_by = body.actor
        proposal.approved_at = proposal_lib._now_utc()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _raise_http(exc)
    db.refresh(proposal)
    return {
        "proposal": proposal_lib.proposal_to_dict(proposal),
        "next_action": "apply",
        "note": "已批准；业务内容仍未改动，需显式调用 apply 才会写入",
    }


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    body: ProposalActionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """pending → rejected（终态）。永不写业务表。"""
    if body.action != "reject":
        raise HTTPException(
            status_code=422,
            detail=f"action 必须是 'reject'，收到 {body.action!r}",
        )
    proposal = _get_proposal(db, proposal_id)
    try:
        proposal_lib.validate_action(proposal.status, "reject")
        proposal.status = "rejected"
        proposal.approved_by = body.actor
        proposal.approved_at = proposal_lib._now_utc()
        if body.note:
            proposal.reason = f"{proposal.reason or ''}\n[reject] {body.note}".strip()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _raise_http(exc)
    db.refresh(proposal)
    return {
        "proposal": proposal_lib.proposal_to_dict(proposal),
        "note": "已拒绝；提案为终态，不可再approve / apply",
    }


# ====================================================================
# POST /api/assistant/proposals/{id}/apply —— 唯一允许写业务表的入口
# ====================================================================


@router.post("/proposals/{proposal_id}/apply")
def apply_proposal(
    proposal_id: int,
    body: ProposalActionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """approved → applied：写业务表 + 写 Version 快照 + 置 applied（同一事务）。

    - 必须先 approve：pending 直接 apply → 409（业务表零写入）。
    - 目标漂移：提案创建后目标被改过 → 置 `stale` + 409，要求重新生成提案，
      **绝不**静默重算或覆盖。
    - 事务原子：任一步失败 → 整体回滚，不产生半版本。
    - 幂等：已applied 再 apply → 409（状态机拒绝），不重复写 Version。

    请求体里出现 `apply_all` / `proposal_ids` 之类批量语义字段 → 422
    （`StrictRequest` 的 extra=forbid）。
    """
    if body.action != "apply":
        raise HTTPException(
            status_code=422,
            detail=f"action 必须是 'apply'，收到 {body.action!r}",
        )
    proposal = _get_proposal(db, proposal_id)
    try:
        result = proposal_lib.apply_proposal(db, proposal=proposal, actor=body.actor)
    except Exception as exc:  # noqa: BLE001 - apply 内部已自行回滚
        _raise_http(exc)
    db.refresh(proposal)
    result["proposal"] = proposal_lib.proposal_to_dict(proposal)
    return result


# ====================================================================
# POST /api/assistant/proposals/apply-selected —— 受控批量应用（第 10 步）
# ====================================================================


@router.post("/proposals/apply-selected")
def apply_selected_proposals(
    body: ApplySelectedRequest, db: Session = Depends(get_db)
) -> dict:
    """受控批量应用：**显式枚举**的若干 `approved` 提案，整批同成同败。

    「一本 500 万字小说可能上千处修正、逐条点不现实」的解法 —— 但每一道能
    被滥用的口子都封死：

    - **必须显式枚举**：`proposal_ids` 必填、非空；通配符字段（`all` / `status` /
      `project_id` …）因 `StrictRequest` 一律 422；
    - **上限封顶**：条数 > `MAX_BULK_APPLY` → 422，不支持一次放行一大批；
    - **每条单独漂移校验**：任一目标漂移 → **整批不写**，并逐条报告「哪条 stale」；
    - **整批同一事务**：任一步失败 → 全回滚，不留半批次、不留半版本；
    - **每条仍写 Version**：整批可逐条回滚，与单条 apply 同一条快照通道。

    它不是新的写入旁路：内部走的仍是 `proposal_lib.apply_selected`（复用单条
    apply 的核心 `_apply_one_no_commit`），状态机与漂移检测与 `/apply` 完全一致。
    """
    if body.action != "apply":
        raise HTTPException(
            status_code=422,
            detail=f"action 必须是 'apply'，收到 {body.action!r}",
        )
    ids = list(dict.fromkeys(body.proposal_ids))
    if not ids:
        raise HTTPException(
            status_code=422,
            detail="proposal_ids 不能为空；批量应用必须**显式枚举**要应用的提案 id",
        )
    if len(ids) > proposal_lib.MAX_BULK_APPLY:
        raise HTTPException(
            status_code=422,
            detail=(
                f"单批最多 {proposal_lib.MAX_BULK_APPLY} 条，收到 {len(ids)} 条；"
                "请分批显式枚举（不支持通配符 / 隐式范围）"
            ),
        )

    proposals = []
    missing = []
    for pid in ids:
        row = db.get(EditProposal, pid)
        if row is None:
            missing.append(pid)
        else:
            proposals.append(row)
    if missing:
        raise HTTPException(status_code=404, detail=f"提案不存在：{missing}")

    result = proposal_lib.apply_selected(db, proposals=proposals, actor=body.actor)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    result["proposal_ids"] = ids
    return result


# ====================================================================
# GET /api/assistant/versions | POST /api/assistant/versions/{id}/restore
# ====================================================================


@router.get("/versions")
def list_versions(
    project_id: int,
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """版本历史（含回滚产生的版本）；`kind` 可选过滤 text|prompt|bible|mixed。"""
    _require_project(db, project_id)
    if kind is not None and kind not in proposal_lib.VERSION_KINDS:
        raise HTTPException(
            status_code=422,
            detail=f"kind 必须是 {list(proposal_lib.VERSION_KINDS)} 之一",
        )
    q = select(Version).where(Version.project_id == project_id)
    if kind is not None:
        q = q.where(Version.kind == kind)
    rows = list(db.scalars(q.order_by(Version.id.asc()).limit(limit)).all())
    return {
        "project_id": project_id,
        "kind": kind,
        "count": len(rows),
        "versions": [proposal_lib.version_to_dict(r) for r in rows],
    }


@router.get("/versions/diff")
def diff_versions(
    from_id: int,
    to_id: int,
    db: Session = Depends(get_db),
) -> dict:
    """两个 Version 的内容差异（`from_id` → `to_id`）。

    存在即返回，不存在 404 —— 顺序反了也照样能看出差异方向。
    """
    src = db.get(Version, from_id)
    dst = db.get(Version, to_id)
    if src is None or dst is None:
        missing = from_id if src is None else to_id
        raise HTTPException(status_code=404, detail=f"Version {missing} 不存在")
    return {
        "from_version": proposal_lib.version_to_dict(src),
        "to_version": proposal_lib.version_to_dict(dst),
        "diff": proposal_lib.version_diff(src, dst),
    }


@router.post("/versions/{version_id}/restore")
def restore_version(
    version_id: int,
    body: RestoreVersionRequest,
    db: Session = Depends(get_db),
) -> dict:
    """从 Version 快照恢复目标内容；**恢复本身也生成一条新 Version**。

    - 历史不丢：既有 Version 行既不删也不改，回滚只是追加；
    - 同事务：业务表 + 新 Version + 提案置 rolled_back 一起提交；
    - 请求体出现 `restore_all` / `version_ids` 之类批量字段 → 422。
    """
    if body.action != "restore":
        raise HTTPException(
            status_code=422,
            detail=f"action 必须是 'restore'，收到 {body.action!r}",
        )
    version = db.get(Version, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail=f"Version {version_id} 不存在")
    try:
        result = proposal_lib.restore_version(db, version=version, actor=body.actor)
    except Exception as exc:  # noqa: BLE001 - restore 内部已自行回滚
        _raise_http(exc)
    db.refresh(version)
    result["restored_from"] = proposal_lib.version_to_dict(version)
    if result.get("proposal_id") is not None:
        proposal = db.get(EditProposal, result["proposal_id"])
        if proposal is not None:
            result["proposal"] = proposal_lib.proposal_to_dict(proposal)
    return result


# ====================================================================
# GET /api/assistant/prompts/active | /prompts/versions
# ====================================================================


@router.get("/prompts/active")
def prompts_active(project_id: int, db: Session = Depends(get_db)) -> dict:
    """当前生效的提示词来源：每个可覆写键是 `runtime_override` 还是 `code_default`。

    这是「改提示词到底有没有生效」的唯一权威答案 —— 不去猜、不看代码，
    直接问这个端点。
    """
    _require_project(db, project_id)
    blocks = active_prompt_report(db, project_id)
    return {
        "project_id": project_id,
        "prompt_keys": list(PROMPT_KEYS),
        "blocks": blocks,
        "has_override": any(b["source"] == "runtime_override" for b in blocks),
        "note": "source=code_default 表示未设置运行时覆写，代码默认提示词原样生效",
    }


@router.get("/prompts/versions")
def prompts_versions(project_id: int, db: Session = Depends(get_db)) -> dict:
    """提示词覆写操作历史（含已失效行）；与 `/versions` 的内容快照并列互补。"""
    _require_project(db, project_id)
    rows = list_override_versions(db, project_id)
    return {
        "project_id": project_id,
        "prompt_keys": list(PROMPT_KEYS),
        "count": len(rows),
        "overrides": rows,
    }