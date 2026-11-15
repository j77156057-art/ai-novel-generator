"""目录检索的**调用轨迹**与监察官软档（设计稿 §5.8–§5.11）。

## 这一层解决什么

目录工具能让 agent 按级探索，但「探索得对不对」没人知道：可能同一件事查三次、
可能写群聊却一直拉战斗规则、也可能命中 10 条一条不用。轨迹表把这些行为落成数据，
监察官（助理 agent）据此告警——但**只告警、不改生成结果**。

## 本期边界（用户 2026-11-12 明确）

- 做：表结构、写入点、软档规则；
- 不做：硬档拦截。轮次/字数/越界都先只记录；阈值（>6 次、≥3 次、50%）是拍的，
  等小样跑出真实轨迹再校准。硬档一旦提前上线，误伤会被当成模型跑偏，反而更难查。

## 写入纪律

1. **每次工具调用都要落一行**，包括 `denied` / `budget_exhausted`。只记成功调用，
   事后就分不清「没查」和「查了被拒」。
2. **`adopted_rule_ids` 段末回填**（`fill_segment_adoption`）。没回填的段不参与
   「空转」「查了不用」统计——否则会把「还没到段末」误判成空转。
"""
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RetrievalLog

#: 允许的调用方。`system` = 程序自动检索（如兜底池补位），与两个 agent 分开统计。
AGENT_KINDS = ("author", "assistant", "system")

#: 状态口径（与设计稿 §5.8 一致）。
#: `blocked` 专指被外部站点反爬拦下（验证码/空结果页/跳转验证），与 `error` 分开统计。
TRACE_STATUSES = ("ok", "empty", "budget_exhausted", "denied", "error", "blocked")

#: 软档阈值。**全部是暂定值**，等真实轨迹校准；集中放这里，改动只有一处。
SOFT_ANCHOR_LIMIT = 6          # 单段 anchor 次数 > 6
SOFT_DUP_QUERY_LIMIT = 3       # 同 (tool, query, path) 重复 >= 3
SOFT_CROSS_SCENE_RATIO = 0.5   # 跨场景域占比 > 50%
SOFT_IDLE_CALLS = 5            # 同段 ok 调用 >= 5 且采用 <= 1
SOFT_IDLE_ADOPTED = 1


#: 预算池（设计稿 §5.12.2）：四类分开计，不合并成一个总池。
BUDGET_POOLS = ("catalog", "rule", "anchor", "writing")


def record_trace(
    db: Session,
    *,
    session_id: str,
    tool_name: str,
    agent_kind: str = "author",
    turn_no: int = 1,
    seq: int = 1,
    segment_no: int | None = None,
    project_id: int | None = None,
    novel_id: int | None = None,
    distill_id: int | None = None,
    path: str | None = None,
    rule_id: str | None = None,
    card_id: str | None = None,
    query: str | None = None,
    hit_ids: Iterable[str] | None = None,
    returned_chars: int = 0,
    budget_remaining: dict[str, Any] | None = None,
    latency_ms: int | None = None,
    status: str = "ok",
    width: int | None = None,
    is_delta: bool = False,
    budget_pool: str | None = None,
) -> RetrievalLog:
    """**唯一写入点**：一次工具调用写一行。

    `context_expansion_count` 在这里算，不让调用方自己传：口径是「同会话同 rule
    已成功拿到 delta 的次数」，调用方各算各的必然算法不一致。

    首次默认窗口写 0；`anchor_missing`（`status != 'ok'`）不计入。
    """
    if agent_kind not in AGENT_KINDS:
        raise ValueError(f"agent_kind 必须是 {AGENT_KINDS}，收到 {agent_kind!r}")
    if status not in TRACE_STATUSES:
        raise ValueError(f"status 必须是 {TRACE_STATUSES}，收到 {status!r}")
    if budget_pool not in (None, *BUDGET_POOLS):
        raise ValueError(
            f"budget_pool 只能是 {BUDGET_POOLS}/None，收到 {budget_pool!r}"
        )

    prior_delta = 0
    if rule_id:
        prior_delta = int(db.execute(
            select(func.count(RetrievalLog.id)).where(
                RetrievalLog.session_id == session_id,
                RetrievalLog.rule_id == rule_id,
                RetrievalLog.is_delta.is_(True),
                RetrievalLog.status == "ok",
            )
        ).scalar() or 0)
    expansion = prior_delta + (1 if (is_delta and status == "ok") else 0)

    row = RetrievalLog(
        project_id=project_id,
        novel_id=novel_id,
        distill_id=distill_id,
        agent_kind=agent_kind,
        session_id=session_id,
        turn_no=turn_no,
        seq=seq,
        segment_no=segment_no,
        tool_name=tool_name,
        path=path,
        rule_id=rule_id,
        card_id=card_id,
        query=query,
        hit_ids=list(hit_ids or []),
        width=width,
        is_delta=is_delta,
        returned_chars=returned_chars,
        budget_remaining=budget_remaining,
        budget_pool=budget_pool,
        context_expansion_count=expansion,
        status=status,
        latency_ms=latency_ms,
    )
    db.add(row)
    db.commit()
    return row


def fill_segment_adoption(
    db: Session, session_id: str, segment_no: int, rule_ids: Iterable[str]
) -> int:
    """段末回填「本段实际采用的规则」。返回被更新的行数。

    更新该段**所有**行（含 `denied`），这样「查了被拒但最终没用」也能对账。
    """
    ids = [str(r) for r in rule_ids if r]
    rows = db.execute(
        select(RetrievalLog).where(
            RetrievalLog.session_id == session_id,
            RetrievalLog.segment_no == segment_no,
        )
    ).scalars().all()
    for row in rows:
        row.adopted_rule_ids = ids
    db.commit()
    return len(rows)


def adopted_by_segment(db: Session, session_id: str) -> dict[int, list[str]]:
    """读回「段 → 采用规则」，供监察官与反哺统计复用。未回填的段不出现。"""
    out: dict[int, list[str]] = {}
    rows = db.execute(
        select(RetrievalLog.segment_no, RetrievalLog.adopted_rule_ids).where(
            RetrievalLog.session_id == session_id,
            RetrievalLog.segment_no.is_not(None),
            RetrievalLog.adopted_rule_ids.is_not(None),
        )
    ).all()
    for seg, ids in rows:
        if seg is not None:
            out[int(seg)] = list(ids or [])
    return out


# --------------------------------------------------------------------------- #
# 软档：只记录、告警、给建议
# --------------------------------------------------------------------------- #

def evaluate_soft(
    db: Session,
    session_id: str,
    *,
    target_scene: str | None = None,
    segment_no: int | None = None,
) -> list[dict[str, Any]]:
    """跑一遍软档规则，把标记写回 `soft_flags`，并返回 findings。

    Args:
        target_scene: 本段的**目标场景域**（如 `groupchat`）。为 None 时
            **不判越界**——抽不出目标场景就判越界一定是误伤（设计稿 §5.9 第 3 条）。

    每条 finding：`{"rule", "level", "message", "segment_no", "evidence"}`。
    `level` 只有两种：`hard` 仅限 `HARD_RULES`（越界检索 / 超护栏），
    其余全部 `soft`——包括「命中 0 条也采用」，它只告警、不中断（R25）。
    """
    findings: list[dict[str, Any]] = []
    rows = db.execute(
        select(RetrievalLog).where(RetrievalLog.session_id == session_id)
        .order_by(RetrievalLog.seq)
    ).scalars().all()
    if not rows:
        return findings

    if segment_no is not None:
        rows = [r for r in rows if r.segment_no == segment_no]

    # 0) 超护栏（硬档之二）：预算池耗尽
    over = [r for r in rows if r.status == "budget_exhausted"]
    if over:
        findings.append(_finding(
            "over_guardrail", over[0].segment_no,
            f"预算池已耗尽 {len(over)} 次，停止本轮检索",
            {"count": len(over), "pool": over[0].budget_pool},
            level="hard",
        ))

    # 1) 单段 anchor 过多
    anchors: dict[int, int] = {}
    for r in rows:
        if r.tool_name == "anchor" and r.segment_no is not None:
            anchors[r.segment_no] = anchors.get(r.segment_no, 0) + 1
    for seg, n in anchors.items():
        if n > SOFT_ANCHOR_LIMIT:
            findings.append(_finding(
                "anchor_overuse", seg,
                f"本段扩窗已 {n} 次，先读 rule(id) 原文再决定是否继续",
                {"count": n},
            ))

    # 2) 同 query 重复。只统计 ok；error 后重试不算重复。
    dup: dict[tuple, int] = {}
    for r in rows:
        if r.status != "ok" or not r.query:
            continue
        key = (r.tool_name, r.query, r.path or "")
        dup[key] = dup.get(key, 0) + 1
    for (tool, query, path), n in dup.items():
        if n >= SOFT_DUP_QUERY_LIMIT:
            findings.append(_finding(
                "duplicate_query", None,
                f"同一 query 查了 {n} 次，换关键词或换层级",
                {"tool": tool, "query": query, "path": path, "count": n},
            ))

    # 3) 命中 0 采用。只对已回填的段判，未回填 = 未统计。
    adopted = adopted_by_segment(db, session_id)
    safe_segs = {s for s in adopted if segment_no is None or s == segment_no}
    hits_by_seg: dict[int, set[str]] = {}
    calls_by_seg: dict[int, int] = {}
    for r in rows:
        if r.segment_no is None or r.status != "ok":
            continue
        calls_by_seg[r.segment_no] = calls_by_seg.get(r.segment_no, 0) + 1
        if r.hit_ids:
            hits_by_seg.setdefault(r.segment_no, set()).update(r.hit_ids)
    for seg in safe_segs:
        got = hits_by_seg.get(seg, set())
        if got and not adopted.get(seg):
            findings.append(_finding(
                "hit_not_adopted", seg,
                f"命中 {len(got)} 条但一条没用，说明检索词与场景不匹配",
                {"hit_count": len(got)},
            ))

    # 4) 空转：查了 N 次只采用 <= 1
    for seg in safe_segs:
        n = calls_by_seg.get(seg, 0)
        used = len(set(adopted.get(seg) or []))
        if n >= SOFT_IDLE_CALLS and used <= SOFT_IDLE_ADOPTED:
            findings.append(_finding(
                "idle_retrieval", seg,
                f"本段查了 {n} 次、只采用 {used} 条，先确认场景域再检索",
                {"calls": n, "adopted": used},
            ))

    # 5) 越界：path 的 L2 与目标场景域不一致
    if target_scene:
        l2_calls = [r for r in rows if r.status == "ok" and _l2(r.path)]
        off = [r for r in l2_calls if _l2(r.path) != target_scene]
        ratio = len(off) / len(l2_calls) if l2_calls else 0.0
        if l2_calls and ratio > SOFT_CROSS_SCENE_RATIO:
            findings.append(_finding(
                "cross_scene", None,
                f"本段 {ratio:.0%} 的检索跨了场景域（目标 {target_scene}），检查 path",
                {"ratio": round(ratio, 3), "target_scene": target_scene},
                level="hard",   # R25：越界检索是两种硬档之一
            ))

    _write_flags(rows, findings)
    db.commit()
    return findings


def analyze(db: Session, session_id: str) -> dict[str, Any]:
    """五类问题的只读盘点（给前端/调试用），不下 verdict。

    第 5 类「凭空编」**只做提示**：它需要卡片索引与措辞特征比对，P3 接工具层后才成立。
    本期如实返回 `unavailable`，不拿命中 id 假装成结论。
    """
    rows = db.execute(
        select(RetrievalLog).where(RetrievalLog.session_id == session_id)
        .order_by(RetrievalLog.seq)
    ).scalars().all()
    adopted = adopted_by_segment(db, session_id)

    idle = []
    for seg, n in _count_by(rows, lambda r: r.segment_no if r.status == "ok" else None).items():
        used = len(set(adopted.get(seg) or []))
        if n >= SOFT_IDLE_CALLS and used <= SOFT_IDLE_ADOPTED:
            idle.append({"segment_no": seg, "calls": n, "adopted": used})

    dup = []
    seen: dict[tuple, int] = {}
    for r in rows:
        if r.status == "ok" and r.query:
            key = (r.tool_name, r.query, r.path or "")
            seen[key] = seen.get(key, 0) + 1
    for (tool, query, path), n in seen.items():
        if n >= SOFT_DUP_QUERY_LIMIT:
            dup.append({"tool": tool, "query": query, "path": path, "count": n})

    hit_not_adopted = []
    hits_by_seg: dict[int, set[str]] = {}
    for r in rows:
        if r.segment_no is not None and r.hit_ids:
            hits_by_seg.setdefault(r.segment_no, set()).update(r.hit_ids)
    for seg in adopted:
        if hits_by_seg.get(seg) and not adopted.get(seg):
            hit_not_adopted.append({
                "segment_no": seg,
                "hit_count": len(hits_by_seg[seg]),
            })

    return {
        "session_id": session_id,
        "calls": len(rows),
        "idle": idle,
        "duplicate": dup,
        "hit_not_adopted": hit_not_adopted,
        "possibly_invented": {
            "status": "unavailable",
            "note": "需要卡片索引与 style_check 措辞特征比对；P3 接工具层后实现，只提示不下结论",
        },
    }


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #

#: 硬档只挡两种（R25，2026-11-12 用户定）：越界检索、超护栏。
#: 其余一律只是告警——尤其 **`hit_not_adopted`（命中 0 条也采用）降为告警**，
#: 记 `trace` 但不中断生成。硬档判定只读这里，不要在别处再写 if。
HARD_RULES = ("cross_scene", "over_guardrail")


def hard_stop_reason(findings: list[dict[str, Any]]) -> str | None:
    """返回第一条硬档原因；没有硬档就返回 None（调用方据此决定是否中断）。"""
    for f in findings:
        if f.get("level") == "hard" and f.get("rule") in HARD_RULES:
            return f.get("message")
    return None


def _finding(rule: str, seg: int | None, message: str, evidence: dict,
             level: str = "soft") -> dict[str, Any]:
    return {
        "rule": rule,
        "level": level,
        "segment_no": seg,
        "message": message,
        "evidence": evidence,
    }


def _l2(path: str | None) -> str | None:
    """从路径里取 L2 场景域。

    不能按第 2 段硬取：`style/dialogue` 的第 2 段是 L4 技法，global 卡也只有
    两段。按 L1/L2/L3/L4 词表逐段认领，认不出的段不参与层级判断。
    """
    from app.retrieval.catalog_paths import classify_path

    return classify_path(path or "").get("l2")


def _count_by(rows: list[RetrievalLog], key) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in rows:
        k = key(r)
        if k is not None:
            out[int(k)] = out.get(int(k), 0) + 1
    return out


def _write_flags(rows: list[RetrievalLog], findings: list[dict[str, Any]]) -> None:
    """把 findings 写到对应行的 `soft_flags`（只加不改生成结果）。

    段级问题落到该段每一行；会话级问题落到该会话第一行。
    """
    per_seg: dict[int, list[str]] = {}
    global_flags: list[str] = []
    for f in findings:
        seg = f.get("segment_no")
        if seg is None:
            global_flags.append(f["rule"])
        else:
            per_seg.setdefault(int(seg), []).append(f["rule"])
    for row in rows:
        flags = list(row.soft_flags or [])
        if row.segment_no is not None:
            for name in per_seg.get(row.segment_no, []):
                if name not in flags:
                    flags.append(name)
        if row is rows[0]:
            for name in global_flags:
                if name not in flags:
                    flags.append(name)
        if flags:
            row.soft_flags = flags
    # 不 commit：由调用方决定事务边界（分析可以只读）。
