"""作者 agent 轨迹的**可读监察**（助理 agent 的只读工具）。

作者 agent 会留下 `retrieval_logs`，但原始行数是给程序看的：助理要回答
「这一段检索是不是跑偏了」，需要的是结论 + 证据 + 建议。本模块只读轨迹，
不改任何生成结果，也不写轨迹。

检测五类跑偏（与设计稿 §5.9 对应，但口径以实测为准）：

1. **参数空转**：path 还是中文标签、或 `status=empty` 占比过高；
2. **原地打转**：同一 `(tool, path)` / 同一 query 反复查；
3. **层级偏食**：只在 L1 打转，从不下钻到节拍/技法；
4. **轨迹对不上**：`seq` 有缺口或重复，说明有调用没落行；
5. **预算/扩窗失控**：池接近或超过 hard、同一条规则扩窗到上限；
6. **采纳回填状态**：区分「段末根本没回填」与「回填了但采纳为 0」——
   后者是判定结论（未采用/判不了），不是漏了回填。

输出 `verdict ∈ healthy / watch / drifting`，并给一句可直接读给用户听的
`summary`。阈值集中在文件顶部，实测后只改一处。
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RetrievalLog
from app.retrieval.budget import POOL_HARD, POOL_SOFT

#: 空转判定：一段里 empty+error 占比。
EMPTY_RATE_WATCH = 0.25
EMPTY_RATE_DRIFT = 0.40
#: 同 (tool, path) 重复。
DUP_PATH_WATCH = 3
DUP_PATH_DRIFT = 5
#: 同 query 重复。
DUP_QUERY_WATCH = 3
#: 只看 L1 的最小调用数。
L1_ONLY_MIN_CALLS = 4
#: 规则/锚点调用达到这个数却没有任何采纳回填 → 提醒。
ADOPTION_MIN_RULE_CALLS = 3

_CJK = re.compile(r"[\u4e00-\u9fff]")
_NAV_TOOLS = ("browse", "search")
_PATH_TOOLS = ("browse", "search", "rule")


def _depth(path: str | None) -> int:
    return len([p for p in (path or "").split("/") if p])


def _latest_author_session(db: Session, project_id: int | None) -> str | None:
    stmt = select(RetrievalLog.session_id).where(
        RetrievalLog.agent_kind == "author",
        RetrievalLog.session_id.is_not(None),
    )
    if project_id is not None:
        stmt = stmt.where(RetrievalLog.project_id == project_id)
    return db.execute(stmt.order_by(RetrievalLog.id.desc()).limit(1)).scalar()


def inspect_session(
    db: Session,
    *,
    session_id: str | None = None,
    project_id: int | None = None,
    segment_no: int | None = None,
) -> dict[str, Any]:
    """读一段作者轨迹，返回 `{session_id, verdict, summary, metrics, findings}`。"""
    sid = session_id or _latest_author_session(db, project_id)
    if not sid:
        return {
            "session_id": None, "verdict": "healthy",
            "summary": "没有找到作者 agent 的检索轨迹，无法监察。",
            "metrics": {"calls": 0}, "findings": [],
        }

    stmt = select(RetrievalLog).where(RetrievalLog.session_id == sid)
    if segment_no is not None:
        stmt = stmt.where(RetrievalLog.segment_no == segment_no)
    rows = db.execute(stmt.order_by(RetrievalLog.seq)).scalars().all()
    if not rows:
        return {
            "session_id": sid, "verdict": "healthy",
            "summary": f"会话 {sid} 没有轨迹行（可能还没开始检索）。",
            "metrics": {"calls": 0}, "findings": [],
        }

    calls = len(rows)
    status_counts = Counter(r.status for r in rows)
    empty = status_counts.get("empty", 0)
    error = status_counts.get("error", 0)
    spin_rate = round((empty + error) / calls, 3)
    seqs = [int(r.seq) for r in rows if r.seq is not None]
    missing_seq: list[int] = []
    if seqs:
        seen = set(seqs)
        missing_seq = [i for i in range(1, max(seqs) + 1) if i not in seen]
        # 起始 seq 不一定从 1 开始（作者段内续号），只报区间内缺口。
        if seen and min(seen) > 1:
            missing_seq = [i for i in missing_seq if i >= min(seen)]
    dup_seq = sorted(k for k, n in Counter(seqs).items() if n > 1)

    path_counts: Counter = Counter()
    query_counts: Counter = Counter()
    path_rows = 0
    label_paths = 0
    for r in rows:
        if r.path:
            path_rows += 1
            if _CJK.search(r.path):
                label_paths += 1
        if r.status == "ok":
            if r.path and r.tool_name in _NAV_TOOLS:
                path_counts[(r.tool_name, r.path)] += 1
            if r.query:
                query_counts[(r.tool_name, r.query, r.path or "")] += 1
    dup_paths = [
        {"tool": t, "path": p, "count": n}
        for (t, p), n in path_counts.most_common(5)
        if n >= DUP_PATH_WATCH
    ]
    dup_queries = [
        {"tool": t, "query": q, "path": p, "count": n}
        for (t, q, p), n in query_counts.most_common(5)
        if n >= DUP_QUERY_WATCH
    ]
    depths = [_depth(r.path) for r in rows
              if r.path and r.tool_name in _NAV_TOOLS]
    l1_only = bool(
        len(depths) >= L1_ONLY_MIN_CALLS and max(depths) <= 1
    )

    by_pool = Counter(r.budget_pool for r in rows if r.budget_pool)
    budget_findings: list[dict[str, Any]] = []
    for pool, used in by_pool.items():
        if used >= POOL_HARD.get(pool, 10 ** 9):
            budget_findings.append({
                "pool": pool, "used": used, "finite": "hard",
                "soft": POOL_SOFT.get(pool), "hard": POOL_HARD.get(pool),
            })
        elif used >= POOL_SOFT.get(pool, 10 ** 9):
            budget_findings.append({
                "pool": pool, "used": used, "finite": "soft",
                "soft": POOL_SOFT.get(pool), "hard": POOL_HARD.get(pool),
            })

    max_expansion = max((int(r.context_expansion_count or 0) for r in rows), default=0)
    # `fill_segment_adoption` 会把该段所有行都写一遍：采纳为 0 时写 `[]`，
    # 没回填才是 NULL。两者必须分开，否则「回填 7 行、采纳 0」会被误报成漏回填。
    backfill_rows = [r for r in rows if r.adopted_rule_ids is not None]
    adopted_rows = [r for r in backfill_rows if r.adopted_rule_ids]
    adopted_ids = list(dict.fromkeys(
        str(x) for r in adopted_rows for x in (r.adopted_rule_ids or [])
    ))
    if not backfill_rows:
        backfill_state = "not_backfilled"
    elif adopted_ids:
        backfill_state = "adopted"
    else:
        backfill_state = "backfilled_zero"
    rule_calls = sum(1 for r in rows if r.tool_name in ("rule", "anchor"))

    findings: list[dict[str, Any]] = []

    def add(fid: str, level: str, message: str, evidence: dict,
            advice: str) -> None:
        findings.append({
            "id": fid, "level": level, "message": message,
            "evidence": evidence, "advice": advice,
        })

    if missing_seq or dup_seq:
        add(
            "trace_misaligned", "high",
            f"轨迹 seq 有缺口/重复（缺 {missing_seq[:6]}，重 {dup_seq[:6]}），"
            "说明有工具调用没落行或同一 seq 写了多行，调用数与日志对不上。",
            {"calls": calls, "missing_seq": missing_seq, "duplicate_seq": dup_seq},
            "检查工具的所有提前 return 是否都走统一出口；用本工具复核。",
        )
    if spin_rate >= EMPTY_RATE_WATCH:
        add(
            "empty_spin", "high" if spin_rate >= EMPTY_RATE_DRIFT else "medium",
            f"空转占比 {spin_rate:.0%}（empty {empty} / error {error} / 共 {calls} 次），"
            "检索没有产出可用规则。",
            {"empty": empty, "error": error, "spin_rate": spin_rate,
             "status": dict(status_counts)},
            "先看 path 拼写与场景域，再决定是否换 query；不要把空转重试当探索。",
        )
    if label_paths:
        add(
            "label_path", "medium",
            f"{label_paths} 次调用的 path 含中文标签，说明参数还在照抄骨架展示名。",
            {"label_paths": label_paths, "path_rows": path_rows},
            "path 用 browse 返回的 key；中文标签只有在路径归一化命中时才等价。",
        )
    if dup_paths:
        add(
            "duplicate_path",
            "high" if any(x["count"] >= DUP_PATH_DRIFT for x in dup_paths) else "medium",
            f"有 {len(dup_paths)} 个路径被反复查（最多 {dup_paths[0]['count']} 次），"
            "说明在同一层原地打转。",
            {"duplicates": dup_paths},
            "换层级：L2 场景域查到 0 条就下钻 L3/L4，或换相邻场景。",
        )
    if dup_queries:
        add(
            "duplicate_query", "medium",
            f"有 {len(dup_queries)} 个 query 重复检索（最多 {dup_queries[0]['count']} 次）。",
            {"duplicates": dup_queries},
            "拆关键词或改用 rule/anchor 读已命中的卡。",
        )
    if l1_only:
        add(
            "l1_only", "medium",
            "所有带路径的检索都停在 L1，没有下钻到场景/节拍/技法，命中会很泛。",
            {"path_depth_max": max(depths), "path_rows": len(depths)},
            "browse(level=2, path=<L1>) 选场景域后再 search。",
        )
    for item in budget_findings:
        add(
            "budget_pressure",
            "high" if item["finite"] == "hard" else "medium",
            f"{item['pool']} 池已用 {item['used']} 次"
            f"（soft {item['soft']} / hard {item['hard']}）。",
            item,
            "池接近上限时工具会收窄返回；优先消费已命中的规则，不要继续扩。",
        )
    if max_expansion >= 6:
        add(
            "anchor_overuse", "medium",
            f"单条规则累计扩窗 {max_expansion} 次，已到边际收益很低的位置。",
            {"max_expansion": max_expansion},
            "改读相邻规则或直接进入写作，不要继续扩同一锚点。",
        )
    if rule_calls >= ADOPTION_MIN_RULE_CALLS and backfill_state == "not_backfilled":
        add(
            "adoption_not_backfilled", "medium",
            f"读了 {rule_calls} 次规则/锚点，但段末没有任何采纳回填"
            "（adopted_rule_ids 全为 NULL）。",
            {"rule_calls": rule_calls, "backfill_state": backfill_state},
            "段末调用采纳判定（retrieval.adoption.backfill_adoption）；"
            "判不了的应为 unjudgeable，不要当成未采用。",
        )
    elif rule_calls >= ADOPTION_MIN_RULE_CALLS and backfill_state == "backfilled_zero":
        add(
            "adoption_zero", "low",
            f"段末已回填 {len(backfill_rows)} 行，但没有一条判为采纳"
            "（结论是未采用或判不了，不是漏回填）。",
            {"rule_calls": rule_calls, "backfilled_rows": len(backfill_rows),
             "backfill_state": backfill_state},
            "看该段 adoption.distribution：unjudgeable 占多说明规则偏抽象，"
            "属正常；not_detected 占多才需要检查判定输入是否混了参考书证据。",
        )

    high = sum(1 for f in findings if f["level"] == "high")
    medium = sum(1 for f in findings if f["level"] == "medium")
    verdict = "drifting" if high else ("watch" if medium else "healthy")

    metrics = {
        "calls": calls,
        "status": dict(status_counts),
        "spin_rate": spin_rate,
        "missing_seq": missing_seq,
        "duplicate_seq": dup_seq,
        "duplicate_paths": dup_paths,
        "duplicate_queries": dup_queries,
        "max_path_depth": max(depths) if depths else 0,
        "l1_only": l1_only,
        "budget": {k: v for k, v in by_pool.items()},
        "max_expansion": max_expansion,
        "adopted_rule_ids": adopted_ids,
        "adopted_rows": len(adopted_rows),
        "backfilled_rows": len(backfill_rows),
        "backfill_state": backfill_state,
        "label_paths": label_paths,
    }

    if verdict == "healthy":
        summary = (
            f"这段检索基本正常：{calls} 次调用，空转 {spin_rate:.0%}，"
            f"最深下钻到 L{max(depths) if depths else 0}，没有发现重复路径或预算压力。"
        )
        if backfill_state == "backfilled_zero":
            summary += (
                f" 采纳回填 {len(backfill_rows)} 行、判为采纳 0 条"
                "（已回填，不是漏回填）。"
            )
    else:
        reasons = "；".join(f["message"] for f in findings[:3])
        summary = (
            f"这段检索{'明显跑偏' if verdict == 'drifting' else '需要留意'}："
            f"{calls} 次调用，空转 {spin_rate:.0%}。{reasons}"
        )

    return {
        "session_id": sid,
        "segment_no": segment_no,
        "verdict": verdict,
        "summary": summary,
        "metrics": metrics,
        "findings": findings,
    }
