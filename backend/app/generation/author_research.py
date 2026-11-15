"""作者 agent 写后自查回路（2026-11-13 v2：缺口驱动）。

## 与 v1 的区别

v1 是「写前盲搜」：正文还没写，先让模型自己翻目录，实测 33 轮、151k prompt
tokens，闸门却没有超过段 7。v2 改成**写后定向查**：

1. 先按现有管线出初稿并跑 `generation_gate`；
2. **只有不过的项**才触发自查，查询词由缺口反推（短句不足→搜短句/节奏，
   叙述单句段过多→搜段落编排，句长均值不足→搜长句/复合句）；
3. 每段 3–8 次工具调用：先按缺口严重度做 1–4 次确定性 `search`，再由程序
   确定性读**最高缺口**命中的第一条规则（防止模型把额度全押在句长类缺口），
   最后让模型按需补挑 `rule`；预算接近上限时工具会自动收窄返回，笔记按缺口归组；
4. 笔记只用于**重写轮**，不进入初稿。

## 轨迹

每次调用都写 `retrieval_logs`（工具内部 `_trace`，带预算池）。`rule {}` 这类
缺 id 的非法调用也在工具内补了 error 轨迹（2026-11-13 修复）。

## 采纳判定

判定与回填已委托 `app.retrieval.adoption`，本模块只负责把调用轨迹整理成
它要的字段（`rule_id` / `path` / `raw_text`）。五档口径：

- `adopted_literal`：规则里的数字+单位 / 引号短语字面出现在正文；
- `adopted_rewrite`：规则里的高区分度词在正文成簇出现（权重来自卡表 IDF），
  或规则某分句与正文有足够词面重合；
- `not_detected`：有可核对标记，但既没字面也没改写命中；
- `unjudgeable`：判不了（纯抽象规则），不是未采用；
- `nav`：browse/search/similar 这类导航调用，不计入采纳统计。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.models import Project
from app.retrieval.adoption import (
    TermWeights,
    backfill_adoption as retrieval_backfill_adoption,
    judge_call as retrieval_judge_call,
    judge_calls as retrieval_judge_calls,
)
from app.retrieval.tools import (
    ToolContext,
    anchor,
    browse,
    rule,
    search,
    similar,
)

TOOL_NAMES = ("browse", "search", "rule", "anchor", "similar")

#: 缺口 → 检索词。值里第一条用于确定性 search，第二条留给模型自己补搜。
#:
#: 2026-11-13 实测订正（段 11）：`叙述单句段占比` / `段长均值` 原来按「段落编排 /
#: 单句成段」检索，但参考语料本身就是极短段风格，命中的全是**鼓励单句成段**的规则
#: （style/pacing「大量使用单句成段的短段落」等）。把这类规则写进重写提示等于反向
#: 加力——段 11 重写后该指标反而从 0.403 升到 0.462。改查「长段 展开」后命中的是
#: style/psych「心理活动用较长段落展开」这类同向规则（离线探针验证，未重跑生成）。
_GAP_QUERIES: dict[str, list[str]] = {
    "短句占比": ["短句 节奏 打断", "短句 对白 独立成句"],
    "叙述单句段占比": ["长段 展开", "叙述 段落 密度"],
    "句长均值": ["长句 复合句 描写", "句长 长短交错 节奏"],
    "句长CV": ["句长变化 长短句 交错", "节奏 句长 波动"],
    "长句占比": ["长句 复合句 描写", "长句 递进 铺陈"],
    "长句31_39占比": ["中长句 复合句 描写", "长句 层次 铺陈"],
    "段长均值": ["长段 展开", "段落 展开 密度"],
    "问号每千字": ["对白 问句 频率", "疑问句 节奏 克制"],
    "对白字占比": ["对白 配比 功能", "对白 推进 信息"],
}

#: 确定性 search 的总上限（缺口再多也不超过 4 次，保证总轮次 3–8）。
_MAX_SEARCHES = 4
#: 模型挑规则的轮次上限（每轮一次调用）。
_MAX_MODEL_ROUNDS = 3
#: 模型自己最多再读几条规则（最高缺口那条由程序确定性读，不占这个额度）。
_MAX_MODEL_RULES = 2
#: 单章工具调用总上限（4 search + 1 确定性 rule + 模型 3 轮 = 8）。
_MAX_ROUNDS_SAFETY = 8
#: 回灌给模型的工具内容预算（字符）。
_RESEARCH_CHAR_BUDGET = 12000
#: 写进重写提示的笔记上限（字符）。
_NOTES_MAX_CHARS = 4200
#: 单次 echo 给模型的工具结果上限。
_ECHO_MAX_CHARS = 2200

#: 卡表 IDF 权重按 distill_id 缓存：卡表 54MB，逐段重建 `TermWeights` 不划算。
_WEIGHTS_CACHE: dict[int, TermWeights] = {}


# --------------------------------------------------------------------------- #
# 缺口 → 查询
# --------------------------------------------------------------------------- #

def plan_gap_queries(gate: dict) -> list[dict]:
    """把闸门违规项翻译成定向查询，并按「离阈值多远」从重到轻排序。"""
    out: list[dict] = []
    for v in (gate or {}).get("violations") or []:
        metric = v.get("metric") or ""
        queries = _GAP_QUERIES.get(metric)
        if not queries:
            continue
        limit = v.get("limit") or {}
        value = v.get("value")
        severity = 0.0
        try:
            lo, hi = limit.get("min"), limit.get("max")
            if lo is not None and value < lo:
                severity = (lo - value) / lo if lo else 0.0
            elif hi is not None and value > hi:
                severity = (value - hi) / hi if hi else 0.0
        except Exception:  # noqa: BLE001
            severity = 0.0
        out.append({
            "metric": metric,
            "value": value,
            "limit": limit,
            "reason": v.get("reason"),
            "queries": list(queries),
            "severity": round(float(severity), 3),
        })
    out.sort(key=lambda g: -g["severity"])
    return out


# --------------------------------------------------------------------------- #
# 判定工具
# --------------------------------------------------------------------------- #

def _adoption_weights(distill_id: Any) -> TermWeights | None:
    """从卡表构建二元组 IDF；换一本书重建卡表，权重自动跟着换。"""
    try:
        key = int(distill_id)
    except (TypeError, ValueError):
        return None
    if key in _WEIGHTS_CACHE:
        return _WEIGHTS_CACHE[key]
    try:
        from app.retrieval.catalog_store import load_catalog
        weights = TermWeights.from_cards(load_catalog(key).cards)
    except Exception:  # noqa: BLE001 —— 卡表缺失时退化为无权重判定，不阻断生成
        return None
    _WEIGHTS_CACHE[key] = weights
    return weights


def _judge_call(call: dict, text: str, weights: TermWeights | None = None) -> dict:
    """对一次工具调用的返回内容判「最终正文有没有用上」（委托检索侧）。"""
    return retrieval_judge_call(
        str(call.get("raw_text") or ""),
        text,
        tool=str(call.get("tool") or "rule"),
        weights=weights,
    )


def check_adoption(
    calls: list[dict], text: str, weights: TermWeights | None = None
) -> dict:
    """逐调用判定并汇总占比（委托检索侧，返回 counts/shares/per_call）。"""
    result = retrieval_judge_calls(calls, text, weights=weights)
    # 兼容旧 quality_json：把原调用上的 args / gap_metric 带回逐条明细。
    by_seq = {int(c.get("seq") or i): c for i, c in enumerate(calls, 1)}
    for item in result.get("per_call") or []:
        src = by_seq.get(int(item.get("seq") or 0))
        if not src:
            continue
        item.setdefault("args", src.get("args") or {})
        if src.get("gap_metric"):
            item.setdefault("gap_metric", src["gap_metric"])
    return result


def backfill_adoption(
    db, research: dict, text: str, weights: TermWeights | None = None
) -> dict:
    """段末回填：委托检索侧判定并写回该段所有行（含被拒）。"""
    calls = research.get("calls") or []
    session_id = research.get("session_id")
    segment_no = research.get("segment_no")
    if weights is None:
        weights = _adoption_weights(research.get("distill_id"))
    if not session_id or segment_no is None:
        return check_adoption(calls, text, weights=weights)
    try:
        return retrieval_backfill_adoption(
            db, session_id, int(segment_no), text,
            calls=calls, weights=weights,
        )
    except Exception:  # noqa: BLE001 —— 回填失败不能弄挂生成
        db.rollback()
        return check_adoption(calls, text, weights=weights)


# --------------------------------------------------------------------------- #
# 工具分发
# --------------------------------------------------------------------------- #

def _dispatch(db, ctx: ToolContext, name: str, args: dict) -> dict:
    if name == "browse":
        return browse(db, ctx,
                      level=int(args.get("level") or 1),
                      path=str(args.get("path") or ""),
                      depth=int(args.get("depth") or 1))
    if name == "search":
        return search(db, ctx,
                      query=str(args.get("query") or ""),
                      path=str(args.get("path") or ""),
                      l1=args.get("l1"),
                      k=int(args.get("k") or 5))
    if name == "rule":
        return rule(db, ctx,
                    card_id=args.get("card_id"),
                    rule_id=args.get("rule_id"))
    if name == "anchor":
        return anchor(db, ctx,
                      card_id=args.get("card_id"),
                      rule_id=args.get("rule_id"),
                      width=int(args.get("width") or 300),
                      cursor=args.get("cursor"))
    if name == "similar":
        return similar(db, ctx,
                       card_id=args.get("card_id"),
                       rule_id=args.get("rule_id"),
                       k=int(args.get("k") or 5))
    raise ValueError(f"未知工具 {name}")


def _raw_text(name: str, payload: dict) -> str:
    if name == "search":
        hits = payload.get("hits") or []
        return "\n".join(
            f"{h.get('summary') or ''}" for h in hits if h.get("summary")
        )
    if name == "rule":
        parts = [payload.get("text") or "", payload.get("evidence") or ""]
        return "\n".join(p for p in parts if p)
    if name == "anchor":
        return payload.get("text") or ""
    return JSON_SAFE(payload)


def JSON_SAFE(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(payload)


def _ids(name: str, payload: dict) -> list[str]:
    if name == "search":
        ids: list[str] = []
        for h in payload.get("hits") or []:
            if h.get("card_id"):
                ids.append(str(h["card_id"]))
        return ids
    out: list[str] = []
    if payload.get("rule_id"):
        out.append(str(payload["rule_id"]))
    if payload.get("card_id"):
        out.append(str(payload["card_id"]))
    return out


def _parse_tool_request(reply: str) -> dict | None:
    text = (reply or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    tool = str(data.get("tool") or "").strip()
    if tool == "done":
        return {"tool": "done", "args": {}}
    if tool not in TOOL_NAMES:
        return None
    args = data.get("args")
    if not isinstance(args, dict):
        args = {}
    return {"tool": tool, "args": args}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def _system_prompt(
    gaps: list[dict], candidates: list[dict], already: list[str]
) -> str:
    lines = [
        "你是同一部小说的作者。初稿已经写完，但没过以下文风硬指标。",
        "请从候选规则里挑最相关的一条，用 rule 读全文，供接下来的定向重写参考。",
        "",
        "【未过项（按严重程度排序）】",
    ]
    for gap in gaps:
        lines.append(f"- {gap.get('reason')}（实测 {gap.get('value')}）")
    lines += ["", "【候选规则（search 命中，按相关度排序）】"]
    if not candidates:
        lines.append('- 无命中：直接回 {"tool":"done"}')
    for idx, c in enumerate(candidates, 1):
        lines.append(
            f"[{idx}] card_id={c.get('card_id')} | path={c.get('path')} | "
            f"{c.get('summary')}"
        )
    if already:
        lines += ["", f"【已读过，不要重复】{', '.join(already)}"]
    lines += [
        "",
        "只输出一个 JSON，不要解释：",
        '{"tool":"rule","args":{"card_id":"<候选里的 id>"}}',
        '或 {"tool":"search","args":{"query":"更精确的词"}}',
        '内容够了就 {"tool":"done"}。预算接近上限时工具会自动收窄返回'
        '（条数变少、窗口变窄），按需要继续读即可；不要重复读同一条。',
    ]
    return "\n".join(lines)


def run_author_research(
    db,
    project: Project,
    provider,
    ctx: dict,
    *,
    segment_no: int,
    gate: dict,
    max_chars: int = 3000,
    node_prompt: str | None = None,
    skeleton: str | None = None,
) -> dict:
    """写后定向自查：只有闸门不过的项才查，3–8 次调用，失败不阻断改写。"""
    started = time.time()
    session_id = f"author-p{project.id}-s{segment_no}-{int(started) % 1000000:06d}"
    out: dict[str, Any] = {
        "enabled": False,
        "status": "skipped",
        "session_id": session_id,
        "gaps": [],
        "rounds": 0,
        "stopped": "gate_passed",
        "calls": [],
        "by_tool": {},
        "returned_chars": 0,
        "anchor_chars": 0,
        "usage": {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0},
        "notes": "",
        "notes_block": "",
    }
    if (gate or {}).get("passed"):
        out["status"] = "gate_passed"
        out["elapsed"] = round(time.time() - started, 2)
        return out

    gaps = plan_gap_queries(gate)
    out["gaps"] = [{"metric": g["metric"], "reason": g["reason"]} for g in gaps]
    if not gaps:
        out["status"] = "no_known_gap"
        out["stopped"] = "no_known_gap"
        out["elapsed"] = round(time.time() - started, 2)
        return out

    tool_ctx = ToolContext(
        session_id=session_id,
        agent_kind="author",
        turn_no=1,
        seq=1,
        segment_no=segment_no,
        project_id=project.id,
        novel_id=(project.active_novel_id or None),
        trace_enabled=True,
    )

    calls: list[dict] = []
    candidates: list[dict] = []
    seen_cards: set[str] = set()
    echoed = 0

    def _dispatch_traced(name: str, args: dict) -> dict:
        """每次调用前把 seq 对齐到「已完成调用数 +1」。

        `ToolContext` 是可变对象、工具只读它来写 `retrieval_logs.seq`，不主动递增；
        不在这里对齐，整段轨迹会全部落在 seq=1，段末按 seq 回填采纳结果时错位。
        """
        tool_ctx.seq = len(calls) + 1
        return _dispatch(db, tool_ctx, name, args)

    def _record(name: str, args: dict, payload: dict,
                gap_metric: str | None = None) -> dict:
        nonlocal echoed
        raw = _raw_text(name, payload)
        ids = _ids(name, payload)
        call = {
            "seq": len(calls) + 1,
            "tool": name,
            "path": str(args.get("path") or ""),
            "rule_id": ids[0] if ids else None,
            "args": args,
            "status": payload.get("status") or "ok",
            "returned_chars": len(raw),
            "rule_ids": ids,
            "gap_metric": gap_metric,
            "raw_text": raw,
        }
        calls.append(call)
        echoed += min(len(raw), _ECHO_MAX_CHARS)
        out["by_tool"][name] = out["by_tool"].get(name, 0) + 1
        return call

    try:
        # ① 缺口驱动的确定性 search（已按严重度排序，最多 4 次）
        used_queries: set[str] = set()
        for gap in gaps[:_MAX_SEARCHES]:
            # 两个缺口可能共用同一条检索词（如「叙述单句段」与「段长均值」都查长段），
            # 重复搜索只会白烧一次调用，换该缺口的备用词。
            query = next(
                (q for q in gap["queries"] if q not in used_queries),
                None,
            )
            if not query:
                continue
            used_queries.add(query)
            payload = _dispatch_traced("search", {"query": query})
            _record("search", {"query": query}, payload, gap["metric"])
            for hit in payload.get("hits") or []:
                cid = str(hit.get("card_id") or "")
                if cid and cid not in seen_cards and hit.get("summary"):
                    seen_cards.add(cid)
                    candidates.append({
                        "card_id": cid,
                        "path": hit.get("path") or "",
                        "summary": hit.get("summary"),
                        "gap_metric": gap["metric"],
                    })

        # ② 最高缺口必读一条：不交给模型挑，否则它容易全押在句长类缺口上，
        #    把最严重的缺口（如叙述单句段占比）漏掉。
        card_gap = {c["card_id"]: c.get("gap_metric") for c in candidates}
        read_cards: list[str] = []
        if candidates:
            top_gap = gaps[0]
            top = next(
                (c for c in candidates if c.get("gap_metric") == top_gap["metric"]),
                candidates[0],
            )
            payload = _dispatch_traced("rule", {"card_id": top["card_id"]})
            _record("rule", {"card_id": top["card_id"]}, payload,
                    top.get("gap_metric"))
            read_cards.append(str(top["card_id"]))

        # ③ 模型从命中里补挑（最多 3 轮，其中 rule 最多 2 条）
        system = _system_prompt(gaps, candidates[:8], read_cards)
        transcript = "请开始。"
        seen_calls: set[str] = set()
        model_rounds = 0
        model_rules = 0
        stopped = "model_done"
        while model_rounds < _MAX_MODEL_ROUNDS:
            reply = provider.chat(system, transcript, constraints=[], stop_sequences=None)
            usage = getattr(provider, "last_usage", None) or {}
            out["usage"]["llm_calls"] += 1
            out["usage"]["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
            out["usage"]["completion_tokens"] += int(usage.get("completion_tokens") or 0)
            req = _parse_tool_request(reply)
            if req is None:
                stopped = "unparsable"
                break
            if req["tool"] == "done":
                stopped = "model_done"
                break
            name, args = req["tool"], req["args"]
            sig = name + json.dumps(args, ensure_ascii=False, sort_keys=True)
            if sig in seen_calls:
                stopped = "duplicate_call"
                break
            seen_calls.add(sig)
            if name == "rule":
                if model_rules >= _MAX_MODEL_RULES:
                    stopped = "model_rule_quota"
                    break
                model_rules += 1
            payload = _dispatch_traced(name, args)
            call = _record(
                name, args, payload,
                card_gap.get(str(args.get("card_id") or "")),
            )
            model_rounds += 1
            if name == "rule" and args.get("card_id"):
                read_cards.append(str(args["card_id"]))
            transcript = (
                f"上一轮 {name} 已返回：\n{call['raw_text'][:_ECHO_MAX_CHARS]}\n"
                "继续挑下一条，或回 {\"tool\":\"done\"}。"
            )
            if echoed >= _RESEARCH_CHAR_BUDGET:
                stopped = "char_budget"
                break
            if len(calls) >= _MAX_ROUNDS_SAFETY:
                stopped = "max_rounds_safety"
                break
        out["stopped"] = stopped

        # ④ 生成笔记（按缺口归组；无缺口对应时放「其他命中的规则」）
        notes = _build_notes(gaps, calls)
        out["notes"] = notes
        if notes:
            out["notes_block"] = (
                "\n【作者自查笔记（初稿未过项反查所得，只学写法，不要照抄路径/条数）】\n"
                f"{notes}"
            )
        out["anchor_chars"] = sum(
            int(c.get("returned_chars") or 0) for c in calls if c.get("tool") == "anchor"
        )
        out["returned_chars"] = sum(int(c.get("returned_chars") or 0) for c in calls)
        out["enabled"] = True
        out["status"] = "done"
    except Exception as exc:  # noqa: BLE001 —— 自查失败退化为「没有笔记」
        out["stopped"] = f"error: {type(exc).__name__}"
        out["status"] = "error"
        out["enabled"] = False
    finally:
        # 必须回填：`check_adoption` / `public_summary` 都从这里读调用轨迹。
        # 漏了这一步，段末采纳判定会得到空列表（段 10 实测 0 条）。
        out["calls"] = calls
        out["rounds"] = len(calls)
        # 段号与卡表 id 供段末回填委托检索侧使用（`engine` 调用签名不变）。
        out["segment_no"] = segment_no
        out["distill_id"] = tool_ctx.distill_id
        out["elapsed"] = round(time.time() - started, 2)
    return out


def _build_notes(gaps: list[dict], calls: list[dict]) -> str:
    """把 rule 原文按缺口归组；该缺口没有规则时用它的 search 摘要兜底。"""
    blocks: list[str] = []
    rule_calls = [c for c in calls if c.get("tool") == "rule" and c.get("raw_text")]
    search_calls = [c for c in calls if c.get("tool") == "search" and c.get("raw_text")]
    used: set[int] = set()
    for idx, gap in enumerate(gaps, 1):
        metric = gap.get("metric")
        blocks.append(f"■ 缺口 {idx}：{gap.get('reason')}")
        own = [c for c in rule_calls
               if c.get("gap_metric") == metric and id(c) not in used]
        if own:
            for call in own[:2]:
                used.add(id(call))
                blocks.append("  · 规则：" + " ".join(call["raw_text"].split())[:300])
            continue
        hit = next((c for c in search_calls
                    if c.get("gap_metric") == metric and id(c) not in used), None)
        if hit is not None:
            used.add(id(hit))
            blocks.append("  · 检索摘要：" + " ".join(hit["raw_text"].split())[:220])
    rest = [c for c in rule_calls if id(c) not in used]
    if rest:
        blocks.append("■ 其他命中的规则")
        for call in rest[:2]:
            blocks.append("  · " + " ".join(call["raw_text"].split())[:220])
    return "\n".join(blocks)[:_NOTES_MAX_CHARS]


def public_summary(research: dict, adoption: dict | None = None) -> dict:
    """剥掉 `raw_text` 的可落库摘要（进 `quality_json` 与生成返回体）。"""
    calls = research.get("calls") or []
    out = {
        "enabled": bool(research.get("enabled")),
        "status": research.get("status")
        or ("done" if research.get("enabled") else "disabled"),
        "session_id": research.get("session_id"),
        "gaps": research.get("gaps") or [],
        "rounds": research.get("rounds"),
        "stopped": research.get("stopped"),
        "call_count": len(calls),
        "by_tool": research.get("by_tool") or {},
        "returned_chars": research.get("returned_chars"),
        "anchor_chars": research.get("anchor_chars"),
        "usage": research.get("usage") or {},
        "elapsed": research.get("elapsed"),
        "notes": research.get("notes") or "",
        "calls": [
            {k: v for k, v in call.items() if k != "raw_text"} for call in calls
        ],
    }
    if adoption is not None:
        out["adoption"] = adoption
    return out
