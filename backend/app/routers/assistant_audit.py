"""Assistant 审核路由：语料 / 知识图谱 / 生成物（第 7 步交付 3）。

端点（全部**只读**，与 /qa、/diagnose 同一套骨架）：
- POST /api/assistant/audit-corpus      语料审核：空/超短分片、重复分片、
                                        疑似注入串、缺 style_profile、
                                        未建 embedding 的分片；
- POST /api/assistant/audit-kg          图谱审核：孤儿节点、角色名冲突
                                        （与 diagnose 同口径）、已死角色
                                        挂未填坑、稳定共现无边；
- POST /api/assistant/audit-generation  生成物审核：零段落、空正文、段长
                                        离群、AI 腔量化、plot_bible 角色
                                        名单不一致。

三个端点共用 `_run_audit` 骨架：开 AgentRun → 跑确定性规则 → 落 Evidence
→ 模型后置解释 → 收尾。规则本身住在 `app.agents.audit`（纯计算、可离线
复现），本文件只做编排。

## project_id 的三种语义（刻意区分，不混用一个「全局」）

- audit-kg / audit-generation：**按项目**审（generated 图谱 / 该项目
  的生成段），project_id 既是被审对象也是审计锚点；
- audit-corpus：语料是**全局**的（raw_novels 不属于任何项目），project_id
  只是审计锚点（AgentRun.project_id 非空，见 models.py）；被审范围由
  `novel_ids` 决定——缺省 = 全库语料，显式给列表 = 只审这些（前端可先审
  项目引用的那几本）。这个分工写进 docstring，避免后人把「项目没引用的
  语料永远审不到」当成 bug、或把「审全库」当成越权。

## 权限与隔离（与 diagnose 完全一致）

- 请求体继承 `StrictRequest`（extra="forbid"）：任何写语义字段 422；
- 规则只 SELECT 业务表；Assistant provider 走
  `get_provider_registry().get('assistant')`，Writer 单例不被触碰；
- provider 失败 → 降级为纯规则结果（`(local_fallback)`），请求照常 200；
- `PermissionError`（角色锁被绕过）**不降级**：403 + run 记 failed，
  越权必须被拒绝并留痕。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.audit import run_corpus_audit, run_generation_audit, run_kg_audit
from app.db import get_db
from app.models import AgentRun, Evidence, RawNovel
from app.routers.assistant_shared import (
    _chat_with_assistant,
    _confidence_from_evidence,
    _now_utc,
    _request_hash,
    _require_project,
    evidence_rows_to_dicts,
    persist_evidence,
)
from app.schemas import StrictRequest

logger = logging.getLogger("router.assistant_audit")

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


# ====================================================================
# 请求体
# ====================================================================


class AssistantAuditCorpusRequest(StrictRequest):
    """POST /api/assistant/audit-corpus 请求体。

    `novel_ids` 缺省 = 全库语料；显式给列表 = 只审这些（语料 id 不存在
    时 404，不静默审个空集——「审了 0 本所以没问题」是最坏的静默失效）。
    """

    project_id: int
    novel_ids: list[int] | None = None
    query: str | None = None
    version_id: int | None = None


class AssistantAuditKGRequest(StrictRequest):
    """POST /api/assistant/audit-kg 请求体（按项目审 generated 图谱）。"""

    project_id: int
    query: str | None = None
    version_id: int | None = None


class AssistantAuditGenerationRequest(StrictRequest):
    """POST /api/assistant/audit-generation 请求体（按项目审生成段）。"""

    project_id: int
    query: str | None = None
    version_id: int | None = None


# ====================================================================
# 共用骨架：开 run → 规则 → 证据 → 模型后置 → 收尾
# ====================================================================


def _require_novels(db: Session, novel_ids: list[int] | None) -> list[int] | None:
    """校验显式给定的 novel_ids 全部存在；缺一个 → 404（fail loud）。

    静默跳过不存在的 id 会让「审了 3 本中的 1 本」看起来像「审了 3 本
    都没问题」——审核类端点最怕的就是这种漏审。
    """
    if novel_ids is None:
        return None
    missing = [nid for nid in novel_ids if db.get(RawNovel, nid) is None]
    if missing:
        raise HTTPException(status_code=404, detail=f"语料不存在：{missing}")
    # 去重保序：同一个 id 传两遍不该让规则跑两遍
    return list(dict.fromkeys(novel_ids))


def _run_audit(
    *,
    db: Session,
    project_id: int,
    body: StrictRequest,
    note: str,
    scope_summary: Callable[[dict[str, Any]], str],
    run_rules: Callable[[], tuple[Any, dict[str, Any]]],
    no_issue_answer: Callable[[dict[str, Any]], str],
) -> dict:
    """审核端点的共用编排（三个 /audit-* 只有取数与文案不同）。

    参数：
    - `note`：写进 AgentRun.note 的调用性质（如「语料审核（只读）」）；
    - `scope_summary`：这次审了多少对象的描述工厂，**在规则跑完后**用
      同一批 stats 构造——「审了多少」必须和规则实际看到的一致，分成
      两次取数会出现在「响应说审了 50 个、规则其实只看到 3 个」的
      窗口期；它同时进模型的 user prompt 与兜底文案；
    - `run_rules`：跑规则的闭包，返回 `(DiagnosticReport, stats)`；
    - `no_issue_answer`：零命中时的兜底文案工厂（各审核类型的
      「几条规则已执行」措辞不同）。

    失败隔离：
    - PermissionError → 403 + run 记 failed（越权不降级）；
    - 其余异常 → 500 + run 记 failed（规则 runner 内部已按规则隔离，
      能逃到这里说明取数本身坏了，不该伪装成「没问题」）；
    - provider 失败 → 纯规则降级（见 `_explain_audit_issues`）。
    """
    project = _require_project(db, project_id)
    request_hash = _request_hash(body)

    run = AgentRun(
        project_id=project.id,
        role="assistant",
        status="running",
        started_at=_now_utc(),
        finished_at=None,
        request_hash=request_hash,
        model="",
        note=note,
    )
    db.add(run)
    db.flush()
    run_id = run.id

    # ---- 1) 规则先行（确定性、只读）----
    try:
        report, stats = run_rules()
    except PermissionError:
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = "(blocked_by_registry)"
        db.commit()
        raise HTTPException(status_code=403, detail="Assistant 越权调用写工具")
    except Exception as exc:  # noqa: BLE001 - 取数坏了必须留痕，不静默降级
        logger.warning("assistant 审核规则跑批失败（%s）：%s", note, exc)
        run.status = "failed"
        run.finished_at = _now_utc()
        run.model = "(local_fallback)"
        run.note = f"{note}失败：{str(exc)[:120]}"
        db.commit()
        raise HTTPException(
            status_code=500, detail=f"{note}失败：{str(exc)[:200]}"
        )
    tool_results: dict[str, Any] = dict(report.tool_results)

    # ---- 2) 证据落库（去重；只写助手自己的审计表 Evidence）----
    evidence_rows = persist_evidence(db, run_id, report)

    # ---- 3) 模型后置：仅在规则命中或用户主动 query 时解释 ----
    issues = [i.to_dict() for i in report.issues]
    scope_text = scope_summary(stats)
    need_llm = bool(report.issues) or bool((getattr(body, "query", None) or "").strip())
    if need_llm:
        answer, model_label = _explain_audit_issues(
            project=project,
            body=body,
            issues=issues,
            scope_summary=scope_text,
            run_id=run_id,
        )
        # provider 失败时 model_label 为 "" → run.model 记 (local_fallback)
    else:
        model_label = ""
        answer = no_issue_answer(stats)

    run.status = "success"
    run.finished_at = _now_utc()
    run.model = model_label or "(local_fallback)"
    db.commit()

    return {
        "answer": answer,
        "issues": issues,
        # findings 兼容别名：与 diagnose 响应同构，前端一份渲染逻辑复用
        "findings": issues,
        "evidence": evidence_rows_to_dicts(evidence_rows),
        "confidence": _confidence_from_evidence(evidence_rows),
        "agent_run_id": run_id,
        "tool_results": tool_results,
        "rules_evaluated": list(report.rules_evaluated),
        "version_id": getattr(body, "version_id", None),
    }


def _explain_audit_issues(
    *,
    project: Any,
    body: Any,
    issues: list[dict],
    scope_summary: str,
    run_id: int,
) -> tuple[str, str]:
    """调 Assistant provider 为审核结论写解释性文字；失败 → 纯规则兜底。

    system 提示词明确三件事：结论必须引用规则给出的定位（novel_id /
    chunk_id / 段号 / 角色名），不得编造；「审核结论」与「推测」分开；
    只给建议不给修复动作——改正文/改图谱/改设定一律走 EditProposal
    审批，助手没有直接写权限（ADR 第 4 步）。
    """
    if issues:
        fallback = (
            f"审核命中 {len(issues)} 个问题（Assistant provider 不可用，"
            f"以下为纯规则结果，未做任何模型解释）："
            + "；".join(f"[{i['severity']}] {i['title']}" for i in issues)
        )
    else:
        fallback = (
            f"{scope_summary}，规则全部已执行，未命中任何已知问题模式；"
            f"Assistant provider 不可用，无法对证据做解释，请直接查看 evidence。"
        )
    system = (
        "你是小说创作平台的审核助手，负责解释语料/图谱/生成物的审核结论。"
        "只能基于用户给出的规则结论回答；禁止编造没有出现在结论里的定位信息"
        "（novel_id / chunk_id / 段号 / 角色名）。把「结论」与「推测」分开标注。"
        "你只解释与建议，不执行任何修复动作——修改正文/图谱/设定一律走提案审批。"
    )
    user = (
        f"项目：{project.title or '未命名'}\n"
        f"审核范围：{scope_summary}\n"
        f"规则命中（severity/rule/title/claim）：\n"
        f"{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
        f"用户补充说明：{getattr(body, 'query', None) or '(无)'}\n"
        f"AgentRun id：{run_id}（本次审核自身，不参与被审数据）"
    )
    return _chat_with_assistant(system=system, user=user, fallback=fallback)


# ====================================================================
# POST /api/assistant/audit-corpus
# ====================================================================


@router.post("/audit-corpus")
def assistant_audit_corpus(
    body: AssistantAuditCorpusRequest, db: Session = Depends(get_db)
) -> dict:
    """语料审核（**只读**）：分片卫生 + 注入串 + 文体档案 + embedding。

    判据（五条确定性规则，全部复用既有判断逻辑，不复制第二套）：
    空/超短分片、重复分片（sha256 分组）、疑似注入串（直接调
    `corpus_health.find_repeated_watermarks` 的位置分布判据）、缺
    style_profile、未建 embedding 的分片。每条 issue 都定位到
    novel_id / chunk_id，可被转成提案。

    `novel_ids` 缺省 = 全库语料；显式给列表 = 只审这些。单本体量超过
    水印扫描上限时该规则**显式跳过**并记进 tool_results，不静默。
    """
    novel_ids = _require_novels(db, body.novel_ids)

    def _rules() -> tuple[Any, dict[str, Any]]:
        return run_corpus_audit(db, novel_ids=novel_ids)

    def _no_issue_answer(stats: dict[str, Any]) -> str:
        return (
            f"已审核 {stats.get('novels_audited', 0)} 个语料 / "
            f"{stats.get('chunks_audited', 0)} 个分片，五条规则全部已执行，"
            f"未命中任何已知问题模式；未调用 Assistant 模型。"
        )

    def _scope_summary(stats: dict[str, Any]) -> str:
        base = (
            f"语料 {stats.get('novels_audited', 0)} 个 / "
            f"分片 {stats.get('chunks_audited', 0)} 个"
        )
        if novel_ids is not None:
            base += f"（指定 novel_ids={novel_ids}）"
        return base

    return _run_audit(
        db=db,
        project_id=body.project_id,
        body=body,
        note="语料审核（只读）",
        scope_summary=_scope_summary,
        run_rules=_rules,
        no_issue_answer=_no_issue_answer,
    )


# ====================================================================
# POST /api/assistant/audit-kg
# ====================================================================


@router.post("/audit-kg")
def assistant_audit_kg(
    body: AssistantAuditKGRequest, db: Session = Depends(get_db)
) -> dict:
    """知识图谱审核（**只读**，按项目，只审 generated 图）。

    判据（四条确定性规则）：孤儿节点、角色名冲突（**直接复用**
    `diagnosis.rule_name_conflict`，与诊断口径逐字节一致）、已死角色
    仍挂未填坑（哨兵状态表 × open 坑）、同段稳定共现但没有关系边。

    参考语料图（`novel:{id}`）的卫生由语料审核负责，两边不重叠——
    同一处问题在两个端点重复出现，用户会以为有两处毛病。
    """
    project = _require_project(db, body.project_id)

    def _rules() -> tuple[Any, dict[str, Any]]:
        return run_kg_audit(db, project)

    def _no_issue_answer(stats: dict[str, Any]) -> str:
        return (
            f"已审核项目 {stats.get('project_id')} 的 generated 图谱，"
            f"四条规则全部已执行，未命中任何已知问题模式；"
            f"未调用 Assistant 模型。"
        )

    def _scope_summary(stats: dict[str, Any]) -> str:
        return f"项目 {stats.get('project_id')} 的 generated 图谱"

    return _run_audit(
        db=db,
        project_id=body.project_id,
        body=body,
        note="图谱审核（只读）",
        scope_summary=_scope_summary,
        run_rules=_rules,
        no_issue_answer=_no_issue_answer,
    )


# ====================================================================
# POST /api/assistant/audit-generation
# ====================================================================


@router.post("/audit-generation")
def assistant_audit_generation(
    body: AssistantAuditGenerationRequest, db: Session = Depends(get_db)
) -> dict:
    """生成物审核（**只读**，按项目）。

    判据（五条确定性规则）：零段落、空正文、段长离群（相对
    `project.generation_length` 的 0.5×/2.0×）、AI 腔量化（有文风档案 →
    z 分数离群；无档案 → 套话密度启发式）、plot_bible 角色名单不一致
    （双向：计划了没写 / 写了没登记）。

    「像 AI」是风格判断，规则只提供可量化信号，最终话语权在人。
    """
    project = _require_project(db, body.project_id)

    def _rules() -> tuple[Any, dict[str, Any]]:
        return run_generation_audit(db, project)

    def _no_issue_answer(stats: dict[str, Any]) -> str:
        return (
            f"已审核 {stats.get('segments_audited', 0)} 个生成段"
            f"（单段目标 {stats.get('generation_length', 0)} 字），"
            f"五条规则全部已执行，未命中任何已知问题模式；"
            f"未调用 Assistant 模型。"
        )

    def _scope_summary(stats: dict[str, Any]) -> str:
        return (
            f"项目 {stats.get('project_id')} 的 "
            f"{stats.get('segments_audited', 0)} 个生成段"
            f"（单段目标 {stats.get('generation_length', 0)} 字）"
        )

    return _run_audit(
        db=db,
        project_id=body.project_id,
        body=body,
        note="生成物审核（只读）",
        scope_summary=_scope_summary,
        run_rules=_rules,
        no_issue_answer=_no_issue_answer,
    )
