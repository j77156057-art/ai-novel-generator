"""Assistant 日志读取与分析规则引擎（第 7 步交付 2）。

## 这个模块回答什么问题

助手要「能读取并分析自己的日志」：`agent_runs` 表里记着每一次 Assistant
调用的成败、用的模型、耗时与证据条数。本模块对这些历史跑**确定性规则**，
产出结构化 issue（与 `app.agents.diagnosis` 完全同构的九字段 schema），
使「最近助手是不是不正常」变成一个可复核的结论，而不是靠人翻表。

## 规则清单（全部只读；判据全部来自 agent_runs / evidence 两表）

| rule | severity | 判据 |
|---|---|---|
| `run_failure_rate_anomaly` | warn / error | 失败率超过阈值（样本量不足不判——3 个 run 里挂 1 个不代表异常） |
| `registry_blocked_attempts` | error | 存在 `model == "(blocked_by_registry)"` 的 run：Assistant 侧发生过被工具注册表拦截的越权尝试 |
| `local_fallback_degradation` | warn | 存在 `model == "(local_fallback)"` 的 run：这些调用没有真正过模型，结论只是规则/检索结果 |
| `empty_evidence_runs` | info / warn | 有 run 的 evidence 条数为 0：无证据的结论不可复核 |
| `run_duration_outliers` | warn | 耗时离群（均值 + 3σ，样本足够时）或超过绝对上限（诊断路径不该慢） |

## 纪律

- **纯只读**：本模块只 SELECT `agent_runs` / `evidence`，不写任何表；
  「分析」这件事本身要被审计，由 router 另开一条 AgentRun（role=assistant）。
- **规则先行、模型后置**：与 diagnose 同一原则——先跑确定性规则，
  只有命中或请求带 query 才调 Assistant provider 写解释；provider 失败
  降级为纯规则结果。
- **可复现**：同一份历史跑两次，issue 集合与 id 逐字节相同
  （`make_issue_id` 确定性 id；issue 顺序 = 规则注册顺序）。
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from app.agents.diagnosis import (
    DiagnosticIssue,
    DiagnosticReport,
    IssueEvidence,
    make_issue_id,
)
from app.models import AgentRun, Evidence

# ------------------------------------------------------------------
# 阈值常量（都是「宁可漏报不可误报」取向的显式值，改必须带理由）
# ------------------------------------------------------------------

#: 失败率判定所需的最小样本量。少于这个数，一次失败就把失败率推到 25%+，
#: 而小样本的失败率波动极大——不判。
MIN_RUNS_FOR_RATE = 5

#: 失败率超过该值 → warn；再超过 FAILURE_RATE_ERROR → error。
FAILURE_RATE_WARN = 0.3
FAILURE_RATE_ERROR = 0.6

#: 单 run 耗时绝对上限（秒）。Assistant 路径的 provider timeout 默认 60s
#: （providers.yaml），超过 30s 的「成功」run 说明链路已经在拖——
#: 哪怕统计上不离群也值得标出来。
DURATION_ABS_WARN_SECONDS = 30.0

#: 耗时离群的统计判据：超过 `均值 + 该倍数 × 标准差`。3σ 是常规离群定义；
#: 样本量不足 MIN_RUNS_FOR_STATS 时不做统计判据（只用绝对上限）。
DURATION_SIGMA = 3.0
MIN_RUNS_FOR_STATS = 5

#: 空 evidence run 数达到该值 → 从 info 升为 warn。
EMPTY_EVIDENCE_WARN_COUNT = 3

#: AgentRun.model 上的语义标记（与 routers/assistant.py 的写入口径一致）。
#: 这两个字符串是「这次 run 到底有没有过模型 / 有没有被拦」的唯一机器可读
#: 信号——刻意不新增列：标记写在 model 上是第 2/3 步建立的既有约定，
#: 日志分析按同一约定读，不引入第二套真源。
MARKER_BLOCKED = "(blocked_by_registry)"
MARKER_LOCAL_FALLBACK = "(local_fallback)"

#: run 证据的 source 标签（Evidence.source 列；与业务数据来源区分）。
RUN_SOURCE = "agent_runs"

_MAX_SNIPPET = 200


# ------------------------------------------------------------------
# 取数：run 历史 + 每条的 evidence 条数（一次查完，避免 N+1）
# ------------------------------------------------------------------


def _as_utc(value: datetime | None) -> datetime | None:
    """naive datetime 按 UTC 解释（SQLite 的 DATETIME 丢 tzinfo；

    我们写入的一律是 `datetime.now(timezone.utc)`，所以「无 tzinfo」
    等价于 UTC。显式补上而不是假设，是因为混用 naive/aware 做减法会
    直接抛 TypeError——那会让整个分析端点 500）。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _duration_seconds(run: Any) -> float | None:
    """run 的耗时（秒）；未完成（finished_at 为空）返回 None。"""
    started = _as_utc(getattr(run, "started_at", None))
    finished = _as_utc(getattr(run, "finished_at", None))
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def fetch_run_history(
    db: Any,
    project_id: int,
    *,
    status: str | None = None,
    limit: int = 100,
    exclude_run_id: int | None = None,
    role: str = "assistant",
) -> list[dict]:
    """取某项目的 AgentRun 历史（新的在前），附带每条的证据条数与耗时。

    `status` 可选过滤（非法值由调用方校验，这里原样透传给 SQL）。
    `limit` 封顶取数——日志可能很多，分析端点不需要全量。
    `exclude_run_id` 排除某条 run——分析端点用它把**自己**（正在 running、
    evidence 还为 0 的那条）排除在被判历史之外，否则 `empty_evidence_runs`
    会把「这次分析自己」报成问题，而 `runs_analyzed` 也会把自己算进去。
    `role` 默认 "assistant"：agent_runs 现在是多角色表（KG 批抽 worker 也写
    role=kg_batch_extract 的行），assistant 的日志/分析只看自己的角色——
    否则批抽的 failed 会进助手失败率，两边的可观测性互相污染。

    返回的 dict 是 GET /api/assistant/runs 与分析规则共用的**唯一形状**：
    id / project_id / role / status / model / note / started_at / finished_at /
    duration_ms / request_hash / evidence_count。
    """
    q = select(AgentRun).where(AgentRun.project_id == project_id)
    if role:
        q = q.where(AgentRun.role == role)
    if status:
        q = q.where(AgentRun.status == status)
    if exclude_run_id is not None:
        q = q.where(AgentRun.id != exclude_run_id)
    q = q.order_by(AgentRun.id.desc()).limit(limit)
    runs = list(db.scalars(q).all())

    # evidence 条数：一次 GROUP BY 查完，不对每条 run 各发一次查询
    counts: dict[int, int] = {}
    if runs:
        run_ids = [r.id for r in runs]
        rows = db.execute(
            select(Evidence.run_id, func.count(Evidence.id))
            .where(Evidence.run_id.in_(run_ids))
            .group_by(Evidence.run_id)
        ).all()
        counts = {int(run_id): int(count) for run_id, count in rows}

    out: list[dict] = []
    for r in runs:
        duration = _duration_seconds(r)
        stop_reason, degraded_reason = _react_failure_reason(r)
        out.append(
            {
                "id": r.id,
                "project_id": r.project_id,
                "role": r.role,
                "status": r.status,
                "model": r.model or "",
                "note": r.note,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "duration_ms": int(duration * 1000) if duration is not None else None,
                "request_hash": r.request_hash,
                "evidence_count": counts.get(r.id, 0),
                # ReAct 之后的失败原因（从 trace_json 取）。**没有它，日志规则
                # 只会说「失败率高」却说不出「为什么」** —— 而失败原因就写在
                # 轨迹里，不取出来等于让用户自己去翻 JSON。
                "stop_reason": stop_reason,
                "degraded_reason": degraded_reason,
            }
        )
    return out


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def run_history_to_dicts(runs: list[dict]) -> list[dict]:
    """把 `fetch_run_history` 的输出转成 JSON 安全的出参（datetime → iso）。

    单独成函数而不是在 fetch 里格式化：分析规则需要 datetime 对象算耗时，
    出参需要 iso 串，两个消费方要的形状不同。
    """
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "status": r["status"],
            "model": r["model"],
            "note": r.get("note"),
            "started_at": _iso(r.get("started_at")),
            "finished_at": _iso(r.get("finished_at")),
            "duration_ms": r.get("duration_ms"),
            "request_hash": r.get("request_hash"),
            "evidence_count": r.get("evidence_count", 0),
        }
        for r in runs
    ]


def run_to_dict(run: Any) -> dict:
    """单条 AgentRun → `fetch_run_history` 的同款 dict（`evidence_count` 占位 0）。

    给 GET /api/assistant/runs/{run_id} 用：详情端点只需要一条 run，走带
    GROUP BY 的批量取数是浪费；`evidence_count` 由调用方按详情页实际查到的
    Evidence 行数覆盖（占位 0 只是保持形状一致，不会是最终出参）。
    """
    duration = _duration_seconds(run)
    return {
        "id": run.id,
        "project_id": run.project_id,
        "role": run.role,
        "status": run.status,
        "model": run.model or "",
        "note": run.note,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_ms": int(duration * 1000) if duration is not None else None,
        "request_hash": run.request_hash,
        "evidence_count": 0,
    }


# ------------------------------------------------------------------
# 证据小工具
# ------------------------------------------------------------------


def _react_failure_reason(run: Any) -> tuple[str, str]:
    """从 run 的轨迹里取 ReAct 的收尾原因与降级原因（取不到就都空）。

    为什么要有它：ReAct 之后，**失败的 run 的 `model` 字段记的是真实模型名**
    （模型没报错，是答案无据/没收敛），标记不再写在 model 里。规则若还教用户
    「看 model 是不是 (local_fallback)」，就会把人引到错的地方。
    """
    raw = getattr(run, "trace_json", None)
    if not raw:
        return "", ""
    try:
        trace = json.loads(raw)
    except (TypeError, ValueError):
        return "", ""
    if not isinstance(trace, dict):
        return "", ""
    return (str(trace.get("stop_reason") or ""),
            str(trace.get("degraded_reason") or ""))


def _run_evidence(run: dict, snippet: str, score: float = 0.5) -> IssueEvidence:
    """把一条 run 历史打包成 issue 证据（kind="run"）。"""
    return IssueEvidence(
        kind="run",
        source=RUN_SOURCE,
        ref_id=str(run.get("id") or ""),
        snippet=snippet[:_MAX_SNIPPET],
        score=score,
    )


def _run_label(run: dict) -> str:
    """run 的短描述（进 evidence snippet）：`run {id} {status} {model} {耗时}`。"""
    duration_ms = run.get("duration_ms")
    dur = f"{duration_ms / 1000:.1f}s" if duration_ms is not None else "未完成"
    return (
        f"run {run.get('id')} status={run.get('status')} "
        f"model={run.get('model') or '(空)'} 耗时={dur}"
    )


# ------------------------------------------------------------------
# 规则 ①：run_failure_rate_anomaly
# ------------------------------------------------------------------


def rule_run_failure_rate(runs: list[dict], taken: set[str]) -> list[DiagnosticIssue]:
    """失败率异常。

    样本量不足 `MIN_RUNS_FOR_RATE` 不判：小样本的失败率波动极大
    （5 个里挂 1 个 = 20%，10 个里挂 1 个 = 10%），拿它下「异常」的结论
    会训练用户无视告警。判据只看 `status == "failed"` 的占比——
    running 态的 run 是「还没跑完」，不算失败也不算分母里的成功。
    """
    finished = [r for r in runs if r.get("status") in ("success", "failed")]
    if len(finished) < MIN_RUNS_FOR_RATE:
        return []
    failed = [r for r in finished if r.get("status") == "failed"]
    if not failed:
        return []
    rate = len(failed) / len(finished)
    if rate < FAILURE_RATE_WARN:
        return []
    severity = "error" if rate >= FAILURE_RATE_ERROR else "warn"
    evidence = tuple(_run_evidence(r, _run_label(r), 0.6) for r in failed[:5])
    # 失败原因**从轨迹里取**：ReAct 之后失败的 run，model 记的是真实模型名
    # （模型没报错，是答案无据或没收敛），标记不再写在 model 字段里。
    reasons = [
        f"run {r.get('id')}：{r.get('degraded_reason') or r.get('stop_reason') or '（原因未记录）'}"
        for r in failed[:6]
    ]
    return [
        DiagnosticIssue(
            id=make_issue_id("run_failure_rate_anomaly", f"{len(failed)}of{len(finished)}", taken),
            type="log_health",
            severity=severity,
            title=f"助手调用失败率 {rate:.0%}（{len(failed)}/{len(finished)}）",
            claim=(
                f"最近 {len(finished)} 次已收尾的助手调用中有 {len(failed)} 次失败"
                f"（失败率 {rate:.0%}，阈值 {FAILURE_RATE_WARN:.0%}）。"
                f"失败 run id：{[r.get('id') for r in failed[:10]]}。"
                + (f"逐条原因：{'；'.join(reasons)}。" if reasons else "")
            ),
            suggestion=(
                "按失败原因分三类看：**答案无据**（原因里带 unsupported_claims）是"
                "「模型说了没证据支撑的话」，该核内容不是查机器；**没收敛**"
                "（stagnated）是模型在同一处打转，换个问法或先看它卡在哪一步；"
                "**模型侧故障**（model_error / timeout / provider_stub）是机器问题，"
                "查 key、配额与网络。若是更早的记录、model 字段写着 (local_fallback)"
                "或 (blocked_by_registry)，那是旧口径：前者是没配可用模型，后者是"
                "越权被拦。"
            ),
            rule="run_failure_rate_anomaly",
            evidence=evidence,
        )
    ]


# ------------------------------------------------------------------
# 规则 ②：registry_blocked_attempts
# ------------------------------------------------------------------


def rule_registry_blocked_attempts(runs: list[dict], taken: set[str]) -> list[DiagnosticIssue]:
    """被工具注册表拦截的越权尝试。

    `AssistantToolRegistry.invoke` 对黑名单前缀工具抛 PermissionError，
    router 把它转成 403 并把 run 的 model 置为 `(blocked_by_registry)`。
    这个标记出现在历史里 = **真的发生过越权尝试**——不是「配置有问题」，
    是「有代码路径想让助手干写操作」。每一条都必须被人看见，故 severity
    恒为 error，且不设阈值（1 条就报）。
    """
    blocked = [r for r in runs if r.get("model") == MARKER_BLOCKED]
    if not blocked:
        return []
    evidence = tuple(_run_evidence(r, _run_label(r), 0.8) for r in blocked[:5])
    return [
        DiagnosticIssue(
            id=make_issue_id("registry_blocked_attempts", f"n{len(blocked)}", taken),
            type="security",
            severity="error",
            title=f"{len(blocked)} 次越权尝试被工具注册表拦截",
            claim=(
                f"历史里有 {len(blocked)} 次 run 以 (blocked_by_registry) 收场："
                f"Assistant 侧有代码路径试图调用 write_/commit_ 等黑名单工具，"
                f"被 AssistantToolRegistry 的双重门拒绝。run id："
                f"{[r.get('id') for r in blocked[:10]]}。"
            ),
            suggestion=(
                "核对这几次 run 的请求来源：正常业务不会触发黑名单工具。"
                "若来自新接入的工具/提示词，说明角色锁边界被试探，应回到"
                " app.agents.registry 的白名单收口，而不是放行单个工具名。"
            ),
            rule="registry_blocked_attempts",
            evidence=evidence,
        )
    ]


# ------------------------------------------------------------------
# 规则 ③：local_fallback_degradation
# ------------------------------------------------------------------


def rule_local_fallback_degradation(runs: list[dict], taken: set[str]) -> list[DiagnosticIssue]:
    """provider 降级为本地兜底的 run。

    `model == "(local_fallback)"` 意味着这次调用**没有真正过模型**：
    回答只是检索证据拼兜底文案。降级本身是设计之内的故障隔离（Assistant
    挂了不能拖垮 Writer），但「用户以为助手在思考、其实在念模板」必须
    可察觉——否则结论的可信度会被系统性高估。
    """
    degraded = [r for r in runs if r.get("model") == MARKER_LOCAL_FALLBACK]
    if not degraded:
        return []
    evidence = tuple(_run_evidence(r, _run_label(r), 0.5) for r in degraded[:5])
    return [
        DiagnosticIssue(
            id=make_issue_id("local_fallback_degradation", f"n{len(degraded)}", taken),
            type="provider_health",
            severity="warn",
            title=f"{len(degraded)} 次调用降级为本地兜底（未过模型）",
            claim=(
                f"历史里有 {len(degraded)} 次 run 以 (local_fallback) 收场："
                f"Assistant provider 构造或调用失败，回答仅为规则/检索结果。"
                f"run id：{[r.get('id') for r in degraded[:10]]}。"
            ),
            suggestion=(
                "检查对应时段的 assistant provider 配置（/api/settings/llm 的 "
                "assistant 对象）：api_key 是否失效、渠道是否限流、"
                "model 是否下架。持续降级时，诊断结论请只当规则结果用。"
            ),
            rule="local_fallback_degradation",
            evidence=evidence,
        )
    ]


# ------------------------------------------------------------------
# 规则 ④：empty_evidence_runs
# ------------------------------------------------------------------


def rule_empty_evidence_runs(runs: list[dict], taken: set[str]) -> list[DiagnosticIssue]:
    """空 evidence 的 run。

    evidence 表里没有行的 run，它的结论**不可复核**——diagnose 的设计
    前提就是「每个结论都能指回库内原文」。少量空 evidence run 可能是
    正常场景（如纯配置类提案创建），故 info 起评；数量达到
    `EMPTY_EVIDENCE_WARN_COUNT` 说明不是偶发，升 warn。
    """
    empty = [r for r in runs if (r.get("evidence_count") or 0) == 0]
    if not empty:
        return []
    severity = "warn" if len(empty) >= EMPTY_EVIDENCE_WARN_COUNT else "info"
    evidence = tuple(_run_evidence(r, _run_label(r), 0.4) for r in empty[:5])
    return [
        DiagnosticIssue(
            id=make_issue_id("empty_evidence_runs", f"n{len(empty)}", taken),
            type="log_health",
            severity=severity,
            title=f"{len(empty)} 次调用没有留下任何证据",
            claim=(
                f"{len(empty)} 次 run 的 evidence 条数为 0（共 {len(runs)} 条被分析）。"
                f"run id：{[r.get('id') for r in empty[:10]]}。"
            ),
            suggestion=(
                "空证据的结论无法复核。若这些 run 是提案创建/审批类操作属正常；"
                "若是诊断/问答类，说明检索与规则都没有产出——检查项目的语料、"
                "图谱与生成段是否为空。"
            ),
            rule="empty_evidence_runs",
            evidence=evidence,
        )
    ]


# ------------------------------------------------------------------
# 规则 ⑤：run_duration_outliers
# ------------------------------------------------------------------


def rule_run_duration_outliers(runs: list[dict], taken: set[str]) -> list[DiagnosticIssue]:
    """耗时离群。

    两个判据并列（满足其一即离群）：

    1. **统计判据**：耗时 > 均值 + 3σ（样本 ≥ `MIN_RUNS_FOR_STATS` 才启用）。
    2. **绝对判据**：耗时 > `DURATION_ABS_WARN_SECONDS`。Assistant provider
       的 timeout 默认 60s，一次「成功」的诊断跑掉 30s 以上，说明链路在
       拖（重试退避 / 网络抖动）——统计判据在「全体都慢」时会失效
       （均值被一起抬高），绝对上限补这个盲区。

    未收尾的 run（finished_at 为空）不参与：它的耗时还不存在。

    实现要点：两个判据是**并集**关系（满足其一即离群），绝不能取
    `max(绝对上限, 统计阈值)`——那会让绝对判据在「全体都慢」时失效
    （均值被一起抬高，统计阈值冲到 100s+，90s 的慢 run 反而漏报），
    而绝对上限补的正是这个盲区。
    """
    durations: list[tuple[dict, float]] = []
    for r in runs:
        ms = r.get("duration_ms")
        if ms is None:
            continue
        durations.append((r, ms / 1000.0))
    if not durations:
        return []

    # 判据一：绝对上限（不依赖样本量，最稳）
    abs_hits = [(r, d) for r, d in durations if d > DURATION_ABS_WARN_SECONDS]
    # 判据二：统计离群（样本足够才启用）
    stat_hits: list[tuple[dict, float]] = []
    stat_note = f"绝对上限 {DURATION_ABS_WARN_SECONDS:g}s"
    if len(durations) >= MIN_RUNS_FOR_STATS:
        values = [d for _, d in durations]
        mean = statistics.fmean(values)
        std = statistics.pstdev(values)
        stat_threshold = mean + DURATION_SIGMA * std
        stat_hits = [(r, d) for r, d in durations if d > stat_threshold]
        stat_note += (
            f" 或均值 {mean:.1f}s + {DURATION_SIGMA:g}σ（{stat_threshold:.1f}s）"
        )

    # 并集去重保序：绝对判据命中的排前（它最稳），同一条 run 不重复报
    merged: dict[Any, tuple[dict, float]] = {}
    for r, d in abs_hits + stat_hits:
        merged[r.get("id")] = (r, d)
    outliers = list(merged.values())
    if not outliers:
        return []
    ranked = sorted(outliers, key=lambda x: -x[1])
    evidence = tuple(_run_evidence(r, _run_label(r), 0.45) for r, _ in ranked[:5])
    return [
        DiagnosticIssue(
            id=make_issue_id("run_duration_outliers", f"n{len(outliers)}", taken),
            type="log_health",
            severity="warn",
            title=f"{len(outliers)} 次调用耗时离群",
            claim=(
                f"{len(outliers)}/{len(durations)} 次已收尾 run 的耗时超过判据"
                f"（{stat_note}）。最慢的几次："
                + "；".join(f"run {r.get('id')} {d:.1f}s" for r, d in ranked[:10])
                + "。"
            ),
            suggestion=(
                "耗时突增常见于 provider 重试退避（429/5xx）或上下文过长。"
                "对照同期 provider 状态与 generation_length 排查；"
                "若为个别 run 偶发无需处理，持续离群应调小 assistant timeout 或换渠道。"
            ),
            rule="run_duration_outliers",
            evidence=evidence,
        )
    ]


# ------------------------------------------------------------------
# 规则注册表与入口
# ------------------------------------------------------------------

#: rule 名 → 规则函数。顺序即 issue 的展示顺序（稳定确定性顺序）。
RULE_REGISTRY = {
    "run_failure_rate_anomaly": rule_run_failure_rate,
    "registry_blocked_attempts": rule_registry_blocked_attempts,
    "local_fallback_degradation": rule_local_fallback_degradation,
    "empty_evidence_runs": rule_empty_evidence_runs,
    "run_duration_outliers": rule_run_duration_outliers,
}

#: 对外暴露的规则顺序（`report.rules_evaluated` 与此一致）。
RULE_ORDER: tuple[str, ...] = tuple(RULE_REGISTRY.keys())


def run_log_audit_rules(
    db: Any,
    project_id: int,
    *,
    status: str | None = None,
    limit: int = 100,
    exclude_run_id: int | None = None,
) -> tuple[DiagnosticReport, list[dict]]:
    """跑全量日志分析规则，返回 `(报告, 被分析的 run 历史)`。

    返回历史而不是只在内部用：router 要把「分析了多少条」与概要
    放进响应（`runs_analyzed`），那份数据必须和规则看到的是同一批——
    分两次查会出现在「报告说 0 问题、响应说分析了 50 条」而对不上号
    的窗口期（期间又有新 run 落库）。

    `exclude_run_id` 透给 `fetch_run_history`：分析端点把自己（正在
    running 的那条）排除在外，理由见该参数的 docstring。

    单条规则抛错不拖垮整次分析（某条规则的数据缺失只该让那条哑火），
    出错规则记进 `tool_results["rule_errors"]`——与
    `run_diagnostic_rules` 的同款语义：**不静默跳过**。
    """
    runs = fetch_run_history(
        db, project_id, status=status, limit=limit, exclude_run_id=exclude_run_id
    )
    taken: set[str] = set()
    issues: list[DiagnosticIssue] = []
    rule_errors: dict[str, Any] = {}

    for name in RULE_ORDER:
        func = RULE_REGISTRY[name]
        try:
            issues.extend(func(runs, taken))
        except Exception as exc:  # noqa: BLE001 - 单规则失败不拖垮整次分析
            rule_errors[f"rule:{name}"] = {"error": str(exc)[:200]}

    stats = {
        "runs_analyzed": len(runs),
        "status_filter": status,
        "limit": limit,
        "by_status": {
            s: sum(1 for r in runs if r.get("status") == s)
            for s in ("success", "failed", "running")
        },
        "rule_errors": rule_errors,
    }
    report = DiagnosticReport(
        issues=issues,
        extra_evidence=[],
        tool_results=stats,
        rules_evaluated=list(RULE_ORDER),
    )
    return report, runs
