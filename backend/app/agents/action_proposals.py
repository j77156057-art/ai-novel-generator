"""**动作提案**（ActionProposal）：agent 提「做什么事」，人批了才执行。

## 为什么单独一张表

`EditProposal` 回答「**改哪些字**」；动作提案回答「**做什么事**」
（启动蒸馏 / 规划大纲 / 扩展规划 / 调动生成）。动作提案没有 before/after，
它的漂移判据是「**前提**」变了没有 —— 前提变了就置 `stale` + 409，
不静默重算、不覆盖。

详见 `docs/write-proposal-design.md`。

## 三条纪律

1. **只提不执行**：本模块只负责「建一条待批提案」与「判定前提是否还成立」。
   执行发生在人点确认之后（走 proposal 审批闸门），不在这里。
2. **前提必须可重算**：漂移检测要能在 apply 前**重算一遍**前提指纹并与建提案时
   的比对。所以前提的算法必须只依赖库里的事实 + 建提案时存下来的参数，
   不能依赖「当时的内存状态」。
3. **前提要能用人话说清**：`precondition_note` 写的是「这条提案的前提是什么」，
   人看到它才知道「凭什么过期了」。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

#: 动作类别。每一类对应**一个**前提算法与一个可执行的端点。
ACTION_KINDS: tuple[str, ...] = (
    "start_distill",
    "plan_outline",
    "extend_plan",
    "invoke_generation",
    # —— 闭环四类（片 3）——
    "close_acceptance",
    "apply_body_repair",
    "derived_rebuild",
    "unlock_generation",
)

#: 提案状态机。
#:
#:   pending ──approve──> approved ──apply──> applied
#:      │                     │
#:      └──reject──> rejected └──前提不符──> stale
#:
#: `stale` 与 `rejected`/`applied` 一样是**终态**：前提变了的提案不该再被应用
#: （要重新提一条）。
STATUSES: tuple[str, ...] = ("pending", "approved", "rejected", "applied", "stale")

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approve", "reject"}),
    "approved": frozenset({"apply", "stale"}),
    "rejected": frozenset(),
    "applied": frozenset(),
    "stale": frozenset(),
}

RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")


class ActionProposalError(Exception):
    """动作提案域的错误（映射成 HTTP 状态码由路由层负责）。"""

    def __init__(self, code: str, message: str = '') -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def _digest(payload: Any) -> str:
    """前提指纹：canonical JSON 的 sha256（键序稳定 → 同事实同指纹）。"""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _written_segments(db: Any, project: Any) -> int:
    from sqlalchemy import func, select

    from app.models import Generation
    return int(db.scalar(
        select(func.count(Generation.id)).where(Generation.project_id == project.id)) or 0)


# ----------------------------------------------------------------------
# 前提算法：每类动作各有一个
# ----------------------------------------------------------------------

def _precondition_start_distill(db: Any, project: Any,
                                 params: Mapping[str, Any]) -> tuple[str, str]:
    """启动蒸馏的前提：**语料**没被动过。

    用仓库既有的 `corpus_content_hash`（语料正文 + 清洗版本的规范指纹）：
    它已经被蒸馏库的复用查找当键用，另算一套会在「语料顺序/重切」时对不上。
    """
    from app.style.narrative import corpus_content_hash
    ids = [int(i) for i in (params.get('source_ids')
                            or (project.reference_novel_ids or []))]
    if not ids:
        raise ActionProposalError('missing_reference_corpus',
                                  '没有参考语料，起不了蒸馏。')
    fingerprint = corpus_content_hash(ids, db)
    return fingerprint, '参考语料的内容与清洗版本（动了就得重算）'


def _precondition_plan_outline(db: Any, project: Any,
                                params: Mapping[str, Any]) -> tuple[str, str]:
    """规划大纲的前提：**项目还没有大纲**，且设定/人物没变。

    「还没大纲」是前提本身 —— 一旦有人填了大纲，这条提案就过时了。
    """
    payload = {
        'has_outline': bool(_outline_text(project)),
        'premise': (project.premise or '').strip(),
        'bible': project.plot_bible or {},
    }
    return _digest(payload), '项目还没有剧情大纲，且设定与人物没变'


def _precondition_extend_plan(db: Any, project: Any,
                               params: Mapping[str, Any]) -> tuple[str, str]:
    """扩展规划的前提：**规划用量与已写段数**没推进。

    推进了就接不上：追加的剧情点会挂在已经走过的节点后面。
    """
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    payload = {
        'done': sorted(timeline.get('done') or []),
        'nodes': sorted((timeline.get('nodes') or {}).keys()),
        'written': _written_segments(db, project),
    }
    return _digest(payload), '规划用量与已写段数（推进了就接不上）'


def _precondition_invoke_generation(db: Any, project: Any,
                                    params: Mapping[str, Any]) -> tuple[str, str]:
    """调动生成的前提：**已写段数与当前节点**没变。

    续写落点是「当前段数之后」；期间多写了一段，这条「续写下一段」就指错地方了。
    """
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    payload = {
        'written': _written_segments(db, project),
        'current_node': timeline.get('current_node'),
        'done': sorted(timeline.get('done') or []),
    }
    return _digest(payload), '已写段数与当前剧情节点（变了就指错地方）'


def _body_rows(db: Any, project: Any) -> list[Any]:
    """项目的全部正文段（按段号、id 排序）—— 与验收/重建用同一套取法。"""
    from sqlalchemy import select

    from app.models import Generation
    return list(db.scalars(
        select(Generation).where(Generation.project_id == project.id)
        .order_by(Generation.segment_no, Generation.id)).all())


def _precondition_close_acceptance(db: Any, project: Any,
                                    params: Mapping[str, Any]) -> tuple[str, str]:
    """整段验收的前提：**当前正文**还没验收过（正文一变就得重验）。

    验收义务绑在**正文级**指纹上（`body_acceptance.body_fingerprint`，不含契约），
    所以前提里带的是这个指纹 —— 正文被人改过，这条提案就指错版本了。
    """
    from app.agents.body_acceptance import body_fingerprint
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    pending = timeline.get('pending_acceptance') or {}
    payload = {
        'body_fingerprint': body_fingerprint(_body_rows(db, project)),
        'accepted': pending.get('state') == 'accepted',
    }
    return _digest(payload), '当前正文的指纹，以及「还没验收通过」这一事实'


def _precondition_apply_body_repair(db: Any, project: Any,
                                     params: Mapping[str, Any]) -> tuple[str, str]:
    """定点修复的前提：**诊断报告**还在，且**目标段**的内容没变。

    两样都必须对：报告没了（诊断 run 被清）接不上；目标段被改过（别人先改了）
    就会按旧文本去改新文本。
    """
    from app.models import AgentRun, Generation
    run_id = params.get('diagnostic_run_id')
    generation_id = params.get('generation_id')
    if not run_id or not generation_id:
        raise ActionProposalError('body_repair_needs_scope',
                                  '定点修复要指明诊断 run 与目标正文段。')
    run = db.get(AgentRun, int(run_id))
    if run is None or int(run.project_id) != int(project.id):
        raise ActionProposalError('body_repair_diagnostic_missing',
                                  '找不到那次诊断（或不属于本项目），接不上这次修复。')
    target = db.get(Generation, int(generation_id))
    if target is None or int(target.project_id) != int(project.id):
        raise ActionProposalError('body_repair_target_missing',
                                  '找不到要修的那一段正文。')
    from app.agents.body_evidence import text_hash
    payload = {
        'diagnostic_run_id': int(run_id),
        'generation_id': int(generation_id),
        'content_hash': text_hash(target.text or ''),
        'ranges': params.get('ranges') or [],
    }
    return _digest(payload), '那次诊断与目标段的内容指纹（正文变了就接不上）'


def _precondition_derived_rebuild(db: Any, project: Any,
                                   params: Mapping[str, Any]) -> tuple[str, str]:
    """派生重建的前提：**派生数据还待重建**，且正文与契约没变。

    指纹用 `derived_rebuild.derived_fingerprint`（正文 + 冻结契约）——
    与重建模块自己算的是**同一个**哈希空间，另算一套会对不上。
    """
    from app.agents.derived_rebuild import derived_fingerprint
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    state = timeline.get('derived_state') or {}
    payload = {
        'derived_fingerprint': derived_fingerprint(db, int(project.id)),
        'kg': state.get('kg'),
        'facts': state.get('facts'),
    }
    return _digest(payload), '正文与冻结契约的指纹，以及派生数据是否已重建'


def _precondition_unlock_generation(db: Any, project: Any,
                                    params: Mapping[str, Any]) -> tuple[str, str]:
    """解锁的前提：派生数据**已重建**、验收**已通过**、正文指纹没变。

    三个条件缺一不可 —— 解锁是「放行生成」，前提松了就是把没审过的正文放出去。
    """
    from app.agents.derived_rebuild import derived_fingerprint
    timeline = project.plot_timeline if isinstance(project.plot_timeline, dict) else {}
    state = timeline.get('derived_state') or {}
    pending = timeline.get('pending_acceptance') or {}
    payload = {
        'derived_fingerprint': derived_fingerprint(db, int(project.id)),
        'kg': state.get('kg'),
        'facts': state.get('facts'),
        'acceptance': pending.get('state'),
    }
    return _digest(payload), '派生数据是否已重建、整段验收是否已通过、正文指纹'


def _outline_text(project: Any) -> str:
    outline = getattr(project, 'outline', None)
    if isinstance(outline, str):
        return outline.strip()
    if isinstance(outline, (list, tuple)):
        return '\n'.join(str(item) for item in outline if str(item).strip())
    return ''


#: 动作类别 → 前提算法。
PRECONDITIONS: dict[str, Callable[[Any, Any, Mapping[str, Any]], tuple[str, str]]] = {
    'start_distill': _precondition_start_distill,
    'plan_outline': _precondition_plan_outline,
    'extend_plan': _precondition_extend_plan,
    'invoke_generation': _precondition_invoke_generation,
    'close_acceptance': _precondition_close_acceptance,
    'apply_body_repair': _precondition_apply_body_repair,
    'derived_rebuild': _precondition_derived_rebuild,
    'unlock_generation': _precondition_unlock_generation,
}


def precondition_now(db: Any, project: Any, kind: str,
                     params: Mapping[str, Any] | None = None
                     ) -> tuple[str, str]:
    """重算某类动作在**此刻**的前提指纹（漂移检测用）。"""
    fn = PRECONDITIONS.get(kind)
    if fn is None:
        raise ActionProposalError('unknown_action_kind',
                                  f'未知的动作类型 {kind!r}；'
                                  f'合法取值 {list(ACTION_KINDS)}')
    return fn(db, project, params or {})


def check_precondition(db: Any, project: Any, proposal: Any) -> bool:
    """这条提案的前提**现在还成立吗**。

    不成立 → 调用方应把提案置 `stale` 并回 409（不静默重算、不覆盖）。
    """
    detail = proposal.detail_json if isinstance(proposal.detail_json, dict) else {}
    params = detail.get('precondition_params') or {}
    fingerprint, _note = precondition_now(db, project, proposal.kind, params)
    return fingerprint == (proposal.precondition_fingerprint or '')


def validate_action(current_status: str, action: str) -> None:
    """状态机闸门：`(当前状态, 动作)` 不合法就抛。"""
    allowed = ALLOWED_TRANSITIONS.get(current_status)
    if allowed is None:
        raise ActionProposalError('unknown_status', f'未知状态 {current_status!r}')
    if action not in allowed:
        raise ActionProposalError(
            'invalid_transition',
            f'状态 {current_status!r} 不能做 {action!r}'
            f'（允许：{sorted(allowed) or "无，终态"}）')


def build_action_proposal(db: Any, project: Any, *, kind: str, title: str,
                           detail: Mapping[str, Any] | None = None,
                           endpoint: str, method: str = 'POST',
                           body: Mapping[str, Any] | None = None,
                           params: Mapping[str, Any] | None = None,
                           risk_level: str = 'low',
                           run_id: int | None = None) -> Any:
    """建一条**待批**动作提案（只落提案表，不动任何业务表）。

    前提指纹在建提案时就算好存下来：apply 前要重算并比对。
    """
    from app.models import ActionProposal

    if kind not in PRECONDITIONS:
        raise ActionProposalError('unknown_action_kind',
                                  f'未知的动作类型 {kind!r}；'
                                  f'合法取值 {list(ACTION_KINDS)}')
    if not (title or '').strip():
        raise ActionProposalError('empty_title',
                                  '提案标题不能为空：人靠它判断要不要批。')
    if risk_level not in RISK_LEVELS:
        raise ActionProposalError('unknown_risk_level',
                                  f'risk_level 必须是 {list(RISK_LEVELS)} 之一')
    fingerprint, note = precondition_now(db, project, kind, params or {})
    detail_dict = dict(detail or {})
    # 前提参数要存下来：apply 前重算前提时需要同一份参数。
    detail_dict['precondition_params'] = dict(params or {})
    row = ActionProposal(
        project_id=int(project.id),
        run_id=run_id,
        kind=kind,
        title=title.strip(),
        detail_json=detail_dict,
        endpoint=endpoint,
        method=method,
        body_json=dict(body or {}),
        precondition_fingerprint=fingerprint,
        precondition_note=note,
        risk_level=risk_level,
        status='pending',
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ----------------------------------------------------------------------
# 执行器：apply 时真正干活的地方（**只有人点确认后**才走到这里）
# ----------------------------------------------------------------------

def _exec_plan_outline(db: Any, project: Any, proposal: Any, *,
                        actor: str | None = None,
                        background_tasks: Any = None) -> dict[str, Any]:
    """写大纲（**人批的那一版**，不是 agent 的原版）。

    写完之后，项目就有了大纲 → 下一次生成时引擎会据此规划剧情节点
    并冻结段契约（`engine._timeline_state_for` → `plan_nodes_with_llm`）。
    """
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    outline = body.get('outline')
    if not outline:
        raise ActionProposalError('empty_outline', '提案里没有大纲内容。')
    project.outline = outline
    db.commit()
    return {
        'outline_written': True,
        'outline_items': len(outline) if hasattr(outline, '__len__') else 1,
        'note': ('大纲已写入。下一步写正文时，引擎会据此规划剧情节点并冻结段契约'
                 '（段契约是正文审查的判据基准）。'),
    }


def _exec_start_distill(db: Any, project: Any, proposal: Any, *,
                        actor: str | None = None,
                        background_tasks: Any = None) -> dict[str, Any]:
    """启动文风蒸馏（后台跑，跟 `POST /api/projects/{id}/distill` 同一套作业）。"""
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    tier = str(body.get('tier') or getattr(project, 'narrative_tier', None) or 'full')
    from app.style.jobs import create_distill_run, run_distill_job
    run = create_distill_run(db, int(project.id), tier)
    if background_tasks is not None:
        background_tasks.add_task(run_distill_job, run.id, int(project.id), tier, False)
    return {
        'distill_run_id': run.id,
        'tier': tier,
        'background': background_tasks is not None,
        'note': '蒸馏已启动（后台跑）。完成后档案会落在蒸馏库里，可直接应用到项目。',
    }


def _exec_extend_plan(db: Any, project: Any, proposal: Any, *,
                       actor: str | None = None,
                       background_tasks: Any = None) -> dict[str, Any]:
    """扩展规划：在保存权威允许的范围内追加剧情点并写成新的段契约。"""
    from app.agents.plan_extension import extend_plan as _extend
    from app.llm.registry import get_provider_registry
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    count = int(body.get('count') or 1)
    provider = get_provider_registry().get('assistant')
    result = _extend(db, project, provider, count=count)
    if result.get('status') != 'extended':
        raise ActionProposalError('extend_plan_failed',
                                  str(result.get('message') or '扩展规划失败。'))
    return dict(result)


def _exec_invoke_generation(db: Any, project: Any, proposal: Any, *,
                            actor: str | None = None,
                            background_tasks: Any = None) -> dict[str, Any]:
    """调动生成：写下一段正文（引擎那一套，不另起逻辑）。"""
    from app.agents.workflow import generation_block
    from app.generation.engine import generate_next_segment
    # **动作提案不是后门**：生成同样要过工序链闸门。
    # 少了这一步，「有未批的修改 / 提示词体检不过 / 没有大纲」这些拦截
    # 就能从「批准一条动作提案」绕过去 —— 那正是闸门要防的事。
    block = generation_block(db, project)
    if block:
        raise ActionProposalError(
            'generation_blocked',
            str(block.get('message') or '工序链还没走完，先把上面的问题处理掉。'))
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    result = generate_next_segment(db, project, max_chars=body.get('max_chars'))
    return {
        'segment_no': result.get('segment_no'),
        'chars': len(result.get('text') or ''),
        'note': '已写下一段正文。',
    }


def _exec_close_acceptance(db: Any, project: Any, proposal: Any, *,
                           actor: str | None = None,
                           background_tasks: Any = None) -> dict[str, Any]:
    """整段验收：把全局验收义务关闭到**当前正文**（或诚实报告还差什么）。

    走的是既有那条链路（`body_acceptance.run_body_acceptance`），不另起逻辑：
    验收会发真实模型请求，证据不足时它自己不发请求、如实报 `coverage_incomplete`。
    """
    from app.agents.body_acceptance import run_body_acceptance
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    base = str(body.get('base_content_hash') or '')
    try:
        report = run_body_acceptance(db, int(project.id), base_content_hash=base)
    except PermissionError as exc:
        raise ActionProposalError('acceptance_forbidden', str(exc))
    except ValueError as exc:
        raise ActionProposalError('acceptance_failed', str(exc))
    status = str(report.get('status') or '')
    if status != 'accepted':
        raise ActionProposalError(
            'acceptance_not_closed',
            f'整段验收没通过（{status}）：'
            f'{(report.get("unresolved") or ["还差什么没查清"])[0]}')
    return {
        'status': status,
        'run_id': report.get('run_id'),
        'note': '整段验收已通过，全局验收义务已关闭到当前正文。',
    }


def _exec_apply_body_repair(db: Any, project: Any, proposal: Any, *,
                             actor: str | None = None,
                             background_tasks: Any = None) -> dict[str, Any]:
    """定点修复：**产出待批的修订提案**（不直接改正文）。

    这一步走的是既有写边界（`body_repair.create_body_repair`）：一次 Writer
    候选 + 一次独立 Assistant 复验，产物是一条**待批**的文本提案 ——
    人点批准并应用后，正文才真的变。
    """
    from app.agents.body_repair import create_body_repair
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    payload = {k: v for k, v in body.items() if k not in ('project_id',)}
    payload['project_id'] = int(project.id)
    try:
        result = create_body_repair(db, **payload)
    except PermissionError as exc:
        raise ActionProposalError('body_repair_forbidden', str(exc))
    except ValueError as exc:
        raise ActionProposalError('body_repair_failed', str(exc))
    return {
        'proposal_id': result.get('proposal_id'),
        'status': result.get('status'),
        'note': ('已生成定点修订候选（待批）。正文要等你批准并应用后才会变。'),
    }


def _exec_derived_rebuild(db: Any, project: Any, proposal: Any, *,
                           actor: str | None = None,
                           background_tasks: Any = None) -> dict[str, Any]:
    """派生重建：按当前正文重算摘要 / 完成度 / KG / 事实。

    **不解锁生成** —— 解锁是另一个显式动作（`unlock_generation`）。
    """
    from app.agents.derived_rebuild import rebuild_derived
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    base = str(body.get('base_fingerprint') or '')
    try:
        result = rebuild_derived(db, int(project.id), base_fingerprint=base)
    except PermissionError as exc:
        raise ActionProposalError('rebuild_forbidden', str(exc))
    except ValueError as exc:
        raise ActionProposalError('rebuild_failed', str(exc))
    if result.get('status') == 'stale':
        raise ActionProposalError('rebuild_failed',
                                  str(result.get('error_category') or '派生重建失败。'))
    return {
        'kg_status': result.get('kg_status'),
        'fact_status': result.get('fact_status'),
        'note': '派生数据已按当前正文重建（生成守卫仍拦着，解锁是下一个动作）。',
    }


def _exec_unlock_generation(db: Any, project: Any, proposal: Any, *,
                            actor: str | None = None,
                            background_tasks: Any = None) -> dict[str, Any]:
    """解除生成守卫：只在重建完成且验收已通过时放行。"""
    from app.agents.derived_rebuild import unlock_generation
    body = proposal.body_json if isinstance(proposal.body_json, dict) else {}
    base = str(body.get('base_fingerprint') or '')
    try:
        result = unlock_generation(db, int(project.id), base_fingerprint=base)
    except PermissionError as exc:
        raise ActionProposalError('unlock_forbidden', str(exc))
    except ValueError as exc:
        raise ActionProposalError('unlock_failed', str(exc))
    return {
        'generation_unlocked': bool(result.get('generation_unlocked')),
        'note': '生成守卫已解除，可以继续写正文了。',
    }


#: 动作类别 → 执行器。
EXECUTORS: dict[str, Callable[..., dict[str, Any]]] = {
    'plan_outline': _exec_plan_outline,
    'start_distill': _exec_start_distill,
    'extend_plan': _exec_extend_plan,
    'invoke_generation': _exec_invoke_generation,
    'close_acceptance': _exec_close_acceptance,
    'apply_body_repair': _exec_apply_body_repair,
    'derived_rebuild': _exec_derived_rebuild,
    'unlock_generation': _exec_unlock_generation,
}


def apply_action_proposal(db: Any, proposal: Any, *, actor: str | None = None,
                           background_tasks: Any = None) -> dict[str, Any]:
    """执行一条**已批准**的动作提案。

    顺序不能错：**先验前提，再干活**。前提不成立 → 置 `stale`（事实要留痕）
    → 抛 `stale_precondition`，由路由层映射成 409。
    """
    from app.models import Project
    from datetime import datetime, timezone
    validate_action(proposal.status, 'apply')
    project = db.get(Project, proposal.project_id)
    if project is None:
        raise ActionProposalError('project_not_found', '项目不存在。')
    if not check_precondition(db, project, proposal):
        proposal.status = 'stale'
        db.commit()
        raise ActionProposalError(
            'stale_precondition',
            '这条提案的前提已经变了（%s）—— 重新提一条。'
            % (proposal.precondition_note or '前提不符'))
    executor = EXECUTORS.get(proposal.kind)
    if executor is None:
        raise ActionProposalError('unknown_action_kind',
                                  f'未知的动作类型 {proposal.kind!r}')
    result = executor(db, project, proposal, actor=actor,
                      background_tasks=background_tasks)
    proposal.status = 'applied'
    proposal.applied_at = datetime.now(timezone.utc)
    proposal.applied_result_json = result
    db.commit()
    db.refresh(proposal)
    return result


#: 动作类别 → **这个动作自己**要打到哪（与「提案行存在哪」无关）。
#:
#: 端点由服务端说了算：agent 提卡时只给 kind，**不给端点** ——
#: 否则「agent 说要启动蒸馏、前端打到别处」这类漂移无从发现。
ACTION_ENDPOINTS: dict[str, str] = {
    'start_distill': '/api/projects/{project_id}/distill',
    'plan_outline': '/api/projects/{project_id}',
    'extend_plan': '/api/projects/{project_id}/plan-extension',
    'invoke_generation': '/api/assistant/invoke-generation',
    # —— 闭环四类：端点都是**既有**的那几个（不新开旁路）——
    'close_acceptance': '/api/assistant/body-acceptance',
    'apply_body_repair': '/api/assistant/body-repairs',
    'derived_rebuild': '/api/assistant/derived-rebuilds',
    'unlock_generation': '/api/assistant/derived-rebuilds',
}


def build_action_body(kind: str, project_id: int, *,
                      tier: str | None = None,
                      outline: list[str] | None = None,
                      count: int | None = None,
                      instruction: str | None = None,
                      max_chars: int | None = None,
                      db: Any = None,
                      project: Any = None,
                      repair: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """按动作类别拼出「动作自己的请求体」。

    每一类动作要的参数不同，**由服务端按 kind 决定**：
    agent 传的是意图（kind + 内容），不是请求体。

    闭环四类要带**指纹**（`base_content_hash` / `base_fingerprint`）——
    那是「用户批的是哪一版正文」的唯一凭据，必须由服务端在提卡时算好，
    不许模型自己填一个。
    """
    if kind == 'start_distill':
        return {'tier': str(tier or 'full'), 'force': False}
    if kind == 'plan_outline':
        if not outline:
            raise ActionProposalError('empty_outline',
                                      '规划大纲要给出大纲内容（每条一个剧情点）。')
        return {'outline': [str(item) for item in outline]}
    if kind == 'extend_plan':
        return {'count': int(count or 1)}
    if kind == 'invoke_generation':
        if not (instruction or '').strip():
            raise ActionProposalError('empty_instruction',
                                      '调动生成要说明写什么。')
        return {'project_id': int(project_id), 'action': 'generate_next',
                'instruction': str(instruction).strip(),
                **({'max_chars': int(max_chars)} if max_chars else {})}
    if kind == 'close_acceptance':
        if db is None or project is None:
            raise ActionProposalError('missing_context',
                                      '整段验收要由服务端算出当前正文指纹。')
        from app.agents.body_acceptance import body_fingerprint
        return {
            'project_id': int(project_id),
            'base_content_hash': body_fingerprint(_body_rows(db, project)),
            'confirm': True,
        }
    if kind == 'apply_body_repair':
        if not (repair or {}).get('diagnostic_run_id') or not (repair or {}).get('generation_id'):
            raise ActionProposalError('body_repair_needs_scope',
                                      '定点修复要指明诊断 run 与目标正文段。')
        if db is None or project is None:
            raise ActionProposalError('missing_context',
                                      '定点修复要由服务端算出目标段的内容指纹。')
        from app.models import Generation
        target = db.get(Generation, int(repair['generation_id']))
        if target is None or int(target.project_id) != int(project_id):
            raise ActionProposalError('body_repair_target_missing',
                                      '找不到要修的那一段正文。')
        from app.agents.body_evidence import text_hash
        payload = {k: v for k, v in dict(repair).items() if v is not None}
        payload.update(project_id=int(project_id),
                       base_content_hash=text_hash(target.text or ''))
        return payload
    if kind in ('derived_rebuild', 'unlock_generation'):
        if db is None or project is None:
            raise ActionProposalError('missing_context',
                                      '派生重建与解锁要由服务端算出当前正文指纹。')
        from app.agents.derived_rebuild import derived_fingerprint
        action = 'rebuild' if kind == 'derived_rebuild' else 'unlock'
        return {
            'project_id': int(project_id),
            'base_fingerprint': derived_fingerprint(db, int(project_id)),
            'action': action,
        }
    raise ActionProposalError('unknown_action_kind',
                              f'未知的动作类型 {kind!r}；合法取值 {list(ACTION_KINDS)}')


def collect_action_proposal_cards(tool_results: Mapping[str, Any],
                                 project_id: int) -> list[dict[str, Any]]:
    """从工具结果里把「动作提案」卡收进对话流（只收**可信**的那些）。

    与 `collect_body_repair_cards` 同一套口径：只认产出动作卡的工具
    （`propose_*` 前缀 —— 它们都只**构造**卡，不落库）、端点必须是动作提案
    自己的创建端点、body 里的 project_id 必须是本项目
    —— 跨项目的卡一律不收（提一张必然失败的卡 = 骗用户点一次）。
    """
    cards: list[dict[str, Any]] = []
    for key, value in (tool_results or {}).items():
        name = str(key).split(':', 1)[0]
        if not name.startswith('propose_') or not isinstance(value, dict):
            continue
        action = value.get('pending_action')
        if (isinstance(action, dict)
                and action.get('kind') == 'action_proposal'
                and action.get('endpoint') == '/api/assistant/action-proposals'
                and action.get('method') == 'POST'
                and (action.get('body') or {}).get('project_id') == project_id):
            cards.append(action)
    return cards


def action_proposal_to_dict(proposal: Any) -> dict[str, Any]:
    """统一出参形状（界面与 agent 工具共用）。"""
    detail = proposal.detail_json if isinstance(proposal.detail_json, dict) else {}
    return {
        'id': proposal.id,
        'project_id': proposal.project_id,
        'run_id': proposal.run_id,
        'kind': proposal.kind,
        'title': proposal.title,
        'detail': detail,
        'endpoint': proposal.endpoint,
        'method': proposal.method,
        'body': proposal.body_json or {},
        'precondition_fingerprint': proposal.precondition_fingerprint,
        'precondition_note': proposal.precondition_note,
        'risk_level': proposal.risk_level,
        'status': proposal.status,
        'created_at': proposal.created_at.isoformat() if proposal.created_at else None,
        'approved_at': proposal.approved_at.isoformat() if proposal.approved_at else None,
        'applied_at': proposal.applied_at.isoformat() if proposal.applied_at else None,
        'applied_result': proposal.applied_result_json or None,
    }


# 自检：常量之间相容（import 时跑，O(1)，失败即 fail loud）
assert set(ALLOWED_TRANSITIONS) == set(STATUSES), '状态集与转移表必须同步'
assert set(PRECONDITIONS) == set(ACTION_KINDS), '每类动作都要有前提算法'
assert set(EXECUTORS) == set(ACTION_KINDS), '每类动作都要有执行器'
assert set(ACTION_ENDPOINTS) == set(ACTION_KINDS), '每类动作都要有端点'
assert set(RISK_LEVELS) >= {'low', 'medium', 'high'}
