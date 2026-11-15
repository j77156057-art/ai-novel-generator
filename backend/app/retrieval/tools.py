"""第三步：五个只读目录工具（作者 agent / 助理 agent 共用同一份实现）。

工具名与设计稿 §5.4 对齐：`browse / search / rule / anchor / similar`。

## 共用而不是两套

- 实现只有本模块一份；两个 agent 通过同一个 `ToolContext` 调用。
- 只读：不改档案、不改卡表、不改生成结果；唯一写入是 `retrieval_logs`。
- 权限一致：不按 `agent_kind` 分叉逻辑，只把 `agent_kind` 写进轨迹用于对账。

## 预算边界（本轮）

用户 2026-11-13 明确：**不设硬性轮次上限**。预算池（catalog/rule/anchor/writing）
的硬闸是 R27 的事；本轮只做工具本身。`anchor` 单次 4,000 字是**单次返回**上限，
不是轮次预算；模型可以自己决定继续 `search` 或继续 `anchor` 扩上下文。

## 轨迹

每次调用（含 empty / error）都写一行 `retrieval_logs`。`anchor` 额外写
`width / is_delta`，`context_expansion_count` 由 `trace.record_trace` 统一计算。
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Project, RetrievalLog, StyleDistill
from app.retrieval.anchor import _chapter_text, locate_rule_anchor
from app.retrieval.bm25 import bm25_scores
from app.retrieval.budget import (
    ANCHOR_MAX_EXPANSIONS,
    PHASE_EXHAUSTED,
    PHASE_NORMAL,
    budget_state,
    narrowed_children,
    narrowed_k,
    narrowed_width,
    note_for,
    width_for_expansion,
)
from app.retrieval.catalog_config import (
    SCENE_MATCHER,
    SCENE_PRIOR_ENABLED,
    SCENE_PRIOR_WEIGHT,
)
from app.retrieval.catalog_paths import (
    L1_LABELS,
    L1_ORDER,
    L2_LABELS,
    L3_LABELS,
    L4_LABELS,
    label_for,
    unassigned_label,
    vocab_for,
)
from app.retrieval.catalog_store import (
    Catalog,
    CatalogNotBuilt,
    load_catalog,
)
from app.retrieval.trace import record_trace

ANCHOR_DEFAULT_WIDTH = 300
ANCHOR_MAX_WIDTH = 2000
ANCHOR_MAX_CHARS = 4000
#: 单次返回的 JSON 字符预算。设得太小会在 14 个场景域 / k 条命中时截尾，
#: 模型看到的是「目录只有 7 个场景」，比多花一点上下文字节更糟。
BROWSE_CHAR_BUDGET = 1600
SEARCH_CHAR_BUDGET = 1400
SIMILAR_CHAR_BUDGET = 1200
SEARCH_DEFAULT_K = 5
SEARCH_MAX_K = 10
MMR_LAMBDA = 0.72
FALLBACK_MIN_HITS = 3


@dataclass
class ToolContext:
    """两个 agent 共用的调用上下文。`session_id` 是轨迹的必填键。"""

    session_id: str
    agent_kind: str = "author"
    turn_no: int = 1
    seq: int = 1
    segment_no: int | None = None
    project_id: int | None = None
    novel_id: int | None = None
    distill_id: int | None = None
    budget_pool: str | None = None
    #: 走 registry 调用时置 False：轨迹由 registry 的 `_record_invoke_trace`
    #: 统一写一行，工具内部再写会变成双行。直接调用工具时保持 True。
    trace_enabled: bool = True
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 解析 / 轨迹
# --------------------------------------------------------------------------- #

def _resolve_novel_id(db: Session, ctx: ToolContext) -> int | None:
    if ctx.novel_id is not None:
        return int(ctx.novel_id)
    if ctx.project_id is None:
        return None
    project = db.get(Project, int(ctx.project_id))
    if project is None:
        return None
    if project.active_novel_id:
        return int(project.active_novel_id)
    refs = project.reference_novel_ids or []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except Exception:  # noqa: BLE001
            refs = []
    return int(refs[0]) if refs else None


def _resolve_distill_id(db: Session, ctx: ToolContext) -> int:
    if ctx.distill_id is not None:
        return int(ctx.distill_id)
    novel_id = _resolve_novel_id(db, ctx)
    if novel_id is None:
        raise ValueError("需要 distill_id / novel_id / project_id 之一")
    rows = db.execute(
        select(StyleDistill.id, StyleDistill.source_ids)
        .where(StyleDistill.status == "active")
        .order_by(StyleDistill.id.desc())
    ).all()
    for distill_id, source_ids in rows:
        ids = source_ids or []
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except Exception:  # noqa: BLE001
                ids = []
        if int(novel_id) in [int(x) for x in ids]:
            return int(distill_id)
    raise ValueError(f"novel_id={novel_id} 没有 active 蒸馏档案")


def _catalog(db: Session, ctx: ToolContext) -> Catalog:
    catalog = load_catalog(_resolve_distill_id(db, ctx))
    if ctx.distill_id is None:
        # 解析出来后回填，保证同一轮后续轨迹都能带上 distill_id。
        ctx.distill_id = catalog.distill_id
    return catalog


def _trace(db: Session, ctx: ToolContext, *, tool_name: str,
           started: float, status: str, path: str | None = None,
           rule_id: str | None = None, card_id: str | None = None,
           query: str | None = None, hit_ids: Iterable[str] | None = None,
           returned_chars: int = 0, width: int | None = None,
           is_delta: bool = False, budget_pool: str | None = None,
           budget_remaining: dict | None = None,
           error: str | None = None) -> dict:
    if not ctx.trace_enabled:
        return {"trace_recorded": False, "trace_skipped": True}
    latency = int((time.perf_counter() - started) * 1000)
    try:
        row = record_trace(
            db,
            session_id=ctx.session_id,
            tool_name=tool_name,
            agent_kind=ctx.agent_kind,
            turn_no=ctx.turn_no,
            seq=ctx.seq,
            segment_no=ctx.segment_no,
            project_id=ctx.project_id,
            novel_id=ctx.novel_id or _resolve_novel_id(db, ctx),
            distill_id=ctx.distill_id,
            path=path,
            rule_id=rule_id,
            card_id=card_id,
            query=query,
            hit_ids=hit_ids,
            returned_chars=returned_chars,
            latency_ms=latency,
            status=status,
            width=width,
            is_delta=is_delta,
            budget_pool=budget_pool or ctx.budget_pool or _DEFAULT_POOL.get(tool_name),
            budget_remaining=budget_remaining,
        )
    except Exception as exc:  # noqa: BLE001 - 轨迹失败不能吞掉工具结果
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"trace_recorded": False, "trace_error": str(exc)}
    return {
        "trace_recorded": True,
        "trace_id": row.id,
        "context_expansion_count": int(row.context_expansion_count or 0),
    }


def _error(ctx: ToolContext, message: str, **extra: Any) -> dict:
    return {"status": "error", "error": message, **extra}


def _budget(db: Session, ctx: ToolContext, tool_name: str) -> dict:
    return budget_state(
        db, ctx.session_id,
        ctx.budget_pool or _DEFAULT_POOL.get(tool_name),
    )


def _fail(db: Session, ctx: ToolContext, *, tool_name: str, started: float,
          message: str, path: str | None = None, query: str | None = None,
          rule_id: str | None = None, width: int | None = None,
          budget: dict | None = None) -> dict:
    """参数非法也落轨迹。

    实测里 `rule {}` 直接 return 造成「32 次调用只有 31 行」，`seq` 出现缺口，
    对账时只能靠人工数。所有提前返回都走这里，保证一次调用一行。
    """
    payload = _error(ctx, message)
    payload.update(_trace(
        db, ctx, tool_name=tool_name, started=started, status="error",
        path=path, query=query, rule_id=rule_id, width=width,
        budget_remaining=budget, error=message,
    ))
    return payload


def _budget_fields(state: dict) -> dict:
    return {
        "budget_pool": state.get("pool"),
        "budget_used": state.get("used"),
        "budget_soft": state.get("soft"),
        "budget_hard": state.get("hard"),
    }


def _mark_budget(payload: dict, state: dict) -> dict:
    """把预算状态写进返回，并在收窄/耗尽时给一句可执行提示。"""
    payload.update(_budget_fields(state))
    note = note_for(state)
    if note:
        payload["budget_note"] = note
    return payload


def _trim_json(payload: dict, budget: int, key: str = "children") -> dict:
    """按 JSON 字符数裁掉尾部，避免 browse/search 一次吐爆上下文。"""
    items = payload.get(key)
    if not isinstance(items, list):
        return payload
    omitted = 0
    while items and len(json.dumps(payload, ensure_ascii=False)) > budget:
        items.pop()
        omitted += 1
    if omitted:
        payload["truncated"] = True
        payload["omitted_count"] = omitted
    return payload


def _child_path(parent: str, key: str) -> str:
    return f"{parent}/{key}" if parent else key


def _level_value(card: dict, level: int) -> str | None:
    return card.get(("l1", "l2", "l3", "l4")[level - 1])


def _applicable(card: dict, level: int) -> bool:
    """该形状是否真的拥有这一级。

    - L2 只有 structural / technique 挂场景；global 是跨场景基线，不算「未挂场景」；
    - L3 只有 structural 挂节拍；technique 的下一条边是 L4，不是 L3；
    - L4 三种 style 形状都可挂；world/voice/metric 没有技法级。
    """
    shape = card.get("shape")
    if level == 1:
        return True
    if level == 2:
        return shape in ("structural", "technique")
    if level == 3:
        return shape == "structural"
    return shape in ("structural", "technique", "global")


def _children_at(cards: list[dict], parent: str, level: int) -> tuple[list[dict], int, int]:
    counts: dict[str, int] = {}
    applicable = 0
    for card in cards:
        if not _applicable(card, level):
            continue
        applicable += 1
        key = _level_value(card, level)
        if key:
            counts[str(key)] = counts.get(str(key), 0) + 1
    unassigned = applicable - sum(counts.values())
    children = [
        {
            "key": key,
            "label": label_for(level, key),
            "path": _child_path(parent, key),
            "count": n,
        }
        for key, n in counts.items()
    ]
    children.sort(key=lambda x: (-x["count"], x["key"]))
    return children, unassigned, len(cards) - applicable


def browse(db: Session, ctx: ToolContext, *, level: int = 1, path: str = "",
           depth: int = 1) -> dict:
    """按级下钻：`level` 是想要的子级（1–4），`path` 是父路径。

    - `browse(level=1)` → L1 资源族；
    - `browse(level=2, path="style")` → 14 个场景域；
    - `browse(level=3, path="style/combat")` → 6 个节拍；
    - `browse(level=4, path="style/combat/climax")` → 8 个技法。

    `path` 既接受英文 key，也接受骨架里展示的中文标签（`战斗对峙`、`技法/战斗对峙`），
    由卡表数据构建的 `PathVocab` 归一；认不出的段会回显在 `path_unknown` 里。
    """
    started = time.perf_counter()
    bs = _budget(db, ctx, "browse")
    if level not in (1, 2, 3, 4):
        return _fail(db, ctx, tool_name="browse", started=started,
                     message="level 只能是 1–4", path=(path or None), budget=bs)
    depth = max(1, min(2, int(depth)))
    if bs["phase"] != PHASE_NORMAL:
        depth = 1
    try:
        catalog = _catalog(db, ctx)
    except (CatalogNotBuilt, ValueError) as exc:
        _trace(db, ctx, tool_name="browse", started=started, status="error",
               path=(path or None), budget_remaining=bs, error=str(exc))
        return _error(ctx, str(exc), **_budget_fields(bs))

    vocab = vocab_for(catalog)
    resolution = vocab.resolve(path)
    parent = vocab.path_for_level(resolution.path, level) if level > 1 else ""

    if level > 1 and not parent:
        # 不再直接报错：把该层合法的 key/标签给回去，模型一次就能改对。
        payload = {
            "status": "empty",
            "distill_id": catalog.distill_id,
            "catalog_version": catalog.catalog_version,
            "level": level,
            "path": "",
            "path_input": resolution.input,
            "path_resolved": "",
            "path_unknown": list(resolution.unknown),
            "hint": "path 不是卡表里的键；用 valid_keys 里的 key 或它对应的 label 重试",
            "valid_keys": vocab.keys_at(level),
            "children": [],
        }
        _mark_budget(payload, bs)
        payload.update(_trace(
            db, ctx, tool_name="browse", started=started, status="empty",
            path=resolution.input or None, budget_remaining=bs,
        ))
        return payload

    cards = catalog.filter_cards(path=parent)
    payload = {
        "status": "ok",
        "distill_id": catalog.distill_id,
        "catalog_version": catalog.catalog_version,
        "level": level,
        "path": parent,
        "path_input": resolution.input,
        "path_resolved": parent,
        "parent_count": len(cards),
        "children": [],
        "unassigned": None,
    }
    if resolution.changed:
        payload["path_alias"] = {
            "from": resolution.input,
            "to": parent,
            "unknown": list(resolution.unknown),
        }
    _mark_budget(payload, bs)
    if not cards:
        payload["status"] = "empty"
        payload.update(_trace(db, ctx, tool_name="browse", started=started,
                              status="empty", path=parent or None,
                              budget_remaining=bs))
        return payload

    children, unassigned, not_applicable = _children_at(cards, parent, level)
    for child in children:
        child["label"] = vocab.label(level, child["key"])
    payload["children"] = children
    payload["not_applicable"] = not_applicable
    if unassigned:
        payload["unassigned"] = {
            "key": "_unassigned",
            "label": unassigned_label(level),
            "count": unassigned,
        }
    if level == 1:
        payload["children"].sort(
            key=lambda x: (-x["count"], L1_ORDER.index(x["key"])
                           if x["key"] in L1_ORDER else 99)
        )

    # L4 的下一层是 L5 原子规则，不是可浏览的路径；再往下钻只会把同一个
    # L4 key 复制成子节点（action/action），对模型是误导。
    if depth == 2 and level < 4:
        for child in payload["children"]:
            sub_cards = catalog.filter_cards(path=child["path"])
            if not sub_cards:
                continue
            sub_children, sub_unassigned, sub_na = _children_at(
                sub_cards, child["path"], min(4, level + 1)
            )
            child["children"] = sub_children[:6]
            child["not_applicable"] = sub_na
            if sub_unassigned:
                child["unassigned"] = {
                    "label": unassigned_label(min(4, level + 1)),
                    "count": sub_unassigned,
                }

    limit = narrowed_children(len(payload["children"]), bs)
    if limit < len(payload["children"]):
        payload["omitted_count"] = len(payload["children"]) - limit
        payload["truncated"] = True
        payload["children"] = payload["children"][:limit]

    char_budget = BROWSE_CHAR_BUDGET if bs["phase"] == PHASE_NORMAL else BROWSE_CHAR_BUDGET // 2
    payload = _trim_json(payload, char_budget)
    status = "budget_exhausted" if bs["phase"] == PHASE_EXHAUSTED else "ok"
    payload["status"] = status
    payload.update(_trace(db, ctx, tool_name="browse", started=started,
                          status=status, path=parent or None,
                          returned_chars=len(json.dumps(payload, ensure_ascii=False)),
                          budget_remaining=bs))
    return payload


# --------------------------------------------------------------------------- #
# 2) search
# --------------------------------------------------------------------------- #

def _jaccard(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _mmr(candidates: list[tuple[float, dict]], k: int) -> list[dict]:
    selected: list[dict] = []
    pool = list(candidates)
    while pool and len(selected) < k:
        best_idx, best_val = 0, -1e9
        for i, (score, card) in enumerate(pool):
            sim = max((_jaccard(card.get("text", ""), s.get("text", ""))
                       for s in selected), default=0.0)
            val = MMR_LAMBDA * score - (1 - MMR_LAMBDA) * sim
            if val > best_val:
                best_idx, best_val = i, val
        selected.append(pool.pop(best_idx)[1])
    return selected


def _parse_l1(value: str | Iterable[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return [str(x) for x in value if str(x)]


def _resolve_l1(vocab, value: str | Iterable[str] | None) -> tuple[list[str], str]:
    """`l1` 参数也接受中文标签。

    实测里模型把 `l1` 填成了场景域（如 `战斗对峙`），旧实现把它当资源族 key
    直接过滤，必然 0 命中。这里把非 L1 的标签转成 path 约束，L1 的才留在
    `l1` 过滤里；认不出的原样保留，让空结果能暴露真实拼写问题。
    """
    keys: list[str] = []
    path = ""
    for token in _parse_l1(value):
        hit = vocab.alias.get(token) or vocab.alias.get(token.lower())
        if hit is None:
            keys.append(token)
        elif hit[0] == 1:
            keys.append(hit[1])
        else:
            path = vocab.node_path(*hit)
    return keys, path


def search(db: Session, ctx: ToolContext, *, query: str, path: str = "",
           l1: str | Iterable[str] | None = None, k: int = SEARCH_DEFAULT_K) -> dict:
    """关键词 + 四维总分 + 场景先验 + MMR；命中不足时回落到 global 卡。"""
    started = time.perf_counter()
    bs = _budget(db, ctx, "search")
    query = (query or "").strip()
    if not query:
        return _fail(db, ctx, tool_name="search", started=started,
                     message="query 不能为空", path=(path or None), budget=bs)
    k = max(1, min(SEARCH_MAX_K, int(k)))
    k = narrowed_k(k, bs)
    try:
        catalog = _catalog(db, ctx)
    except (CatalogNotBuilt, ValueError) as exc:
        _trace(db, ctx, tool_name="search", started=started, status="error",
               path=(path or None), query=query, budget_remaining=bs,
               error=str(exc))
        return _error(ctx, str(exc), **_budget_fields(bs))

    vocab = vocab_for(catalog)
    resolution = vocab.resolve(path)
    parent = resolution.path
    l1_keys, l1_path = _resolve_l1(vocab, l1)
    if not parent and l1_path:
        parent = l1_path

    candidates = catalog.filter_cards(path=parent, l1=l1_keys)
    if not candidates:
        payload = {
            "status": "empty", "query": query, "path": parent,
            "path_input": resolution.input,
            "path_resolved": parent,
            "path_unknown": list(resolution.unknown),
            "l1": l1_keys, "hits": [],
        }
        _mark_budget(payload, bs)
        payload.update(_trace(db, ctx, tool_name="search", started=started,
                              status="empty", path=parent or None, query=query,
                              budget_remaining=bs))
        return payload

    raw = bm25_scores(query, [f"{c.get('text','')} {c.get('summary','')}" for c in candidates])
    max_raw = max(raw) or 1.0
    prior_scenes = set(SCENE_MATCHER.query_scenes(query))
    ranked: list[tuple[float, dict]] = []
    for card, bm in zip(candidates, raw):
        prior = SCENE_PRIOR_WEIGHT if (
            SCENE_PRIOR_ENABLED and card.get("l2") in prior_scenes
        ) else 0.0
        score = 0.65 * (bm / max_raw) + 0.35 * float(
            (card.get("scores") or {}).get("total") or 0.0
        ) + prior
        ranked.append((score, card))
    ranked.sort(key=lambda x: -x[0])

    picked = _mmr(ranked[: max(30, k * 6)], k)
    hits = []
    for card in picked:
        score = next(s for s, c in ranked if c is card)
        hits.append({
            "card_id": card["id"],
            "rule_id": card["id"],
            "summary": card.get("summary") or "",
            "path": card.get("path"),
            "l2": card.get("l2"),
            "l3": card.get("l3"),
            "l4": card.get("l4"),
            "score": round(float(score), 3),
            "fallback": False,
        })

    fallback_count = 0
    if len(hits) < FALLBACK_MIN_HITS:
        seen = {h["card_id"] for h in hits}
        global_cards = [
            c for c in catalog.active_cards
            if c.get("shape") == "global" and c["id"] not in seen
        ]
        global_cards.sort(key=lambda c: -float((c.get("scores") or {}).get("total") or 0))
        for card in global_cards[: FALLBACK_MIN_HITS - len(hits)]:
            hits.append({
                "card_id": card["id"],
                "rule_id": card["id"],
                "summary": card.get("summary") or "",
                "path": card.get("path"),
                "l2": card.get("l2"),
                "l3": card.get("l3"),
                "l4": card.get("l4"),
                "score": 0.0,
                "fallback": True,
            })
            fallback_count += 1

    payload = {
        "status": "ok" if hits else "empty",
        "distill_id": catalog.distill_id,
        "catalog_version": catalog.catalog_version,
        "query": query,
        "path": parent,
        "path_input": resolution.input,
        "path_resolved": parent,
        "l1": l1_keys,
        "total_candidates": len(candidates),
        "fallback_count": fallback_count,
        "fallback_source": "global_cards" if fallback_count else None,
        "hits": hits,
    }
    if resolution.changed:
        payload["path_alias"] = {
            "from": resolution.input, "to": parent,
            "unknown": list(resolution.unknown),
        }
    _mark_budget(payload, bs)
    char_budget = SEARCH_CHAR_BUDGET if bs["phase"] == PHASE_NORMAL else SEARCH_CHAR_BUDGET // 2
    payload = _trim_json(payload, char_budget, key="hits")
    status = payload["status"]
    if bs["phase"] == PHASE_EXHAUSTED:
        status = "budget_exhausted"
    payload["status"] = status
    payload.update(_trace(db, ctx, tool_name="search", started=started,
                          status=status, path=parent or None,
                          query=query, hit_ids=[h["card_id"] for h in payload["hits"]],
                          returned_chars=len(json.dumps(payload, ensure_ascii=False)),
                          budget_remaining=bs))
    return payload


# --------------------------------------------------------------------------- #
# 3) rule
# --------------------------------------------------------------------------- #

def rule(db: Session, ctx: ToolContext, *, card_id: str | None = None,
         rule_id: str | None = None) -> dict:
    """读一条卡的完整规则；被硬过滤的旧 id 也能回查原文。"""
    started = time.perf_counter()
    bs = _budget(db, ctx, "rule")
    key = str(card_id or rule_id or "").strip()
    if not key:
        # 实测 `rule {}` 曾直接 return 不写轨迹，导致 32 次调用只有 31 行。
        return _fail(db, ctx, tool_name="rule", started=started,
                     message="需要 card_id 或 rule_id", budget=bs)
    try:
        catalog = _catalog(db, ctx)
    except (CatalogNotBuilt, ValueError) as exc:
        _trace(db, ctx, tool_name="rule", started=started, status="error",
               rule_id=key, budget_remaining=bs, error=str(exc))
        return _error(ctx, str(exc), **_budget_fields(bs))

    status = "budget_exhausted" if bs["phase"] == PHASE_EXHAUSTED else "ok"
    card = catalog.find(key)
    if card:
        payload = {
            "status": status,
            "card_missing": False,
            "card_id": card["id"],
            "rule_id": card["id"],
            "path": card.get("path"),
            "dim": card.get("dim"),
            "shape": card.get("shape"),
            "summary": card.get("summary"),
            "text": card.get("text"),
            "text_truncated": False,
            "evidence": card.get("evidence"),
            "chapter_no": card.get("chapter_no"),
            "evidence_chapter_no": card.get("evidence_chapter_no"),
            "timeline_desc": card.get("timeline_desc"),
            "timeline_src": card.get("timeline_src"),
            "scores": card.get("scores"),
            "flags": card.get("flags"),
            "flag_reasons": card.get("flag_reasons"),
            "anchor": card.get("anchor"),
            # deprecated 卡不参与默认检索，但按 id 回查仍返回，并显式告知调用方。
            "deprecated": bool(card.get("deprecated")),
            "deprecated_reason": card.get("deprecated_reason"),
        }
        _mark_budget(payload, bs)
        payload.update(_trace(db, ctx, tool_name="rule", started=started,
                              status=status, path=card.get("path"),
                              rule_id=card["id"], card_id=card["id"],
                              hit_ids=[card["id"]],
                              returned_chars=len(str(card.get("text") or "")),
                              budget_remaining=bs))
        return payload

    discarded = catalog.discarded.get(key)
    if discarded:
        payload = {
            "status": status,
            "card_missing": True,
            "card_id": key,
            "rule_id": key,
            "path": None,
            "dim": discarded.get("dim"),
            "summary": None,
            "text": discarded.get("text"),
            "text_truncated": False,
            "evidence": discarded.get("evidence"),
            "chapter_no": discarded.get("chapter_no"),
            "evidence_chapter_no": discarded.get("evidence_chapter_no"),
            "flags": discarded.get("flags"),
            "discard_reasons": discarded.get("reasons"),
        }
        _mark_budget(payload, bs)
        payload.update(_trace(db, ctx, tool_name="rule", started=started,
                              status=status, rule_id=key,
                              returned_chars=len(str(discarded.get("text") or "")),
                              budget_remaining=bs))
        return payload

    payload = {"status": "empty", "card_missing": True, "card_id": key,
               "rule_id": key, "text": None}
    _mark_budget(payload, bs)
    payload.update(_trace(db, ctx, tool_name="rule", started=started,
                          status="empty", rule_id=key, budget_remaining=bs))
    return payload


# --------------------------------------------------------------------------- #
# 4) anchor
# --------------------------------------------------------------------------- #

def _align_left(text: str, pos: int, limit: int = 80) -> int:
    pos = max(0, min(len(text), pos))
    floor = max(0, pos - limit)
    for i in range(pos - 1, floor - 1, -1):
        if text[i] in "\n。！？；":
            return i + 1
    return pos


def _align_right(text: str, pos: int, limit: int = 80) -> int:
    pos = max(0, min(len(text), pos))
    ceil = min(len(text), pos + limit)
    for i in range(pos, ceil):
        if text[i] in "\n。！？；":
            return i + 1
    return pos


def _encode_cursor(card_id: str, start: int, end: int) -> str:
    raw = json.dumps({"c": card_id, "f": start, "t": end},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> dict:
    raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or "f" not in data or "t" not in data:
        raise ValueError("cursor 格式不对")
    return data


def _anchor_item(card: dict) -> dict:
    return {
        "rule": card.get("text"),
        "evidence": card.get("evidence"),
        "chunk_id": card.get("chunk_id"),
        "evidence_chapter_no": card.get("evidence_chapter_no"),
        "novel_id": card.get("novel_id"),
    }


def anchor(db: Session, ctx: ToolContext, *, card_id: str | None = None,
           rule_id: str | None = None, width: int = ANCHOR_DEFAULT_WIDTH,
           cursor: str | None = None) -> dict:
    """定位证据 + 扩上下文；`cursor` 只回新增区间，不回已给过的正文。

    单次最多返回 4,000 字（width 夹到 50–2,000）。没有轮次上限：模型可以带着
    上一次返回的 cursor 继续扩，`context_expansion_count` 会逐次累加。
    """
    started = time.perf_counter()
    bs = _budget(db, ctx, "anchor")
    key = str(card_id or rule_id or "").strip()
    if not key:
        return _fail(db, ctx, tool_name="anchor", started=started,
                     message="需要 card_id 或 rule_id",
                     width=max(50, min(ANCHOR_MAX_WIDTH, int(width))), budget=bs)
    width = max(50, min(ANCHOR_MAX_WIDTH, int(width)))
    try:
        catalog = _catalog(db, ctx)
    except (CatalogNotBuilt, ValueError) as exc:
        _trace(db, ctx, tool_name="anchor", started=started, status="error",
               rule_id=key, width=width, budget_remaining=bs, error=str(exc))
        return _error(ctx, str(exc), **_budget_fields(bs))

    # 扩窗边际递减 + 每规则封顶：先按历史 delta 次数收窄窗口。
    expansions = 0
    if ctx.session_id:
        expansions = int(db.execute(
            select(func.count(RetrievalLog.id)).where(
                RetrievalLog.session_id == ctx.session_id,
                RetrievalLog.rule_id == key,
                RetrievalLog.is_delta.is_(True),
                RetrievalLog.status == "ok",
            )
        ).scalar() or 0)
    capped = expansions >= ANCHOR_MAX_EXPANSIONS
    if capped:
        width = 60
    else:
        width = width_for_expansion(width, expansions)
        width = narrowed_width(width, bs)

    card = catalog.find(key) or catalog.discarded.get(key)
    if not card:
        payload = {"status": "empty", "card_id": key, "rule_id": key}
        _mark_budget(payload, bs)
        payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                              status="empty", rule_id=key, width=width,
                              budget_remaining=bs))
        return payload

    try:
        located = locate_rule_anchor(db, _anchor_item(card), span=0)
    except Exception as exc:  # noqa: BLE001
        payload = _error(ctx, f"锚点定位失败：{exc}", card_id=key)
        payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                              status="error", rule_id=key, width=width,
                              budget_remaining=bs))
        return payload

    if located.anchor_missing:
        text = (located.context or "")[:ANCHOR_MAX_CHARS]
        anchor_info = located.to_dict()
        # `context` 与下面的 `text` 是同一段正文，重复返回只会多占预算。
        anchor_info.pop("context", None)
        payload = {
            "status": "empty",
            "anchor_missing": True,
            "reason": located.reason or "证据句无法在章内定位",
            "card_id": key,
            "rule_id": key,
            "chapter_no": located.evidence_chapter_no or located.chapter_no,
            "anchor": anchor_info,
            "text": text,
            "segments": [],
            "is_delta": False,
            "width": width,
            "expansions": expansions,
            "capped": capped,
        }
        _mark_budget(payload, bs)
        payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                              status="empty", rule_id=key, width=width,
                              returned_chars=len(text), budget_remaining=bs))
        return payload

    chapter_no = located.evidence_chapter_no or located.chapter_no
    if chapter_no is None or located.char_start is None or located.char_end is None:
        payload = _error(ctx, "锚点缺少章号或字符区间", card_id=key)
        payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                              status="error", rule_id=key, width=width,
                              budget_remaining=bs))
        return payload
    text = _chapter_text(db, int(card.get("novel_id") or 0), int(chapter_no))
    if not text:
        payload = _error(ctx, "章正文为空", card_id=key)
        payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                              status="error", rule_id=key, width=width,
                              budget_remaining=bs))
        return payload

    start = int(located.char_start)
    end = int(located.char_end)
    is_delta = bool(cursor)
    segments: list[dict] = []
    if cursor:
        try:
            state = _decode_cursor(cursor)
            if str(state.get("c")) not in ("", key):
                raise ValueError("cursor 属于另一张卡")
            old_from, old_to = int(state["f"]), int(state["t"])
        except Exception as exc:  # noqa: BLE001
            payload = _error(ctx, f"cursor 无效：{exc}", card_id=key)
            payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                                  status="error", rule_id=key, width=width,
                                  budget_remaining=bs))
            return payload
        new_from = _align_left(text, max(0, old_from - width))
        new_to = _align_right(text, min(len(text), old_to + width))
        if new_from < old_from:
            segments.append({
                "from": new_from, "to": old_from,
                "text": text[new_from:old_from],
            })
        if new_to > old_to:
            segments.append({
                "from": old_to, "to": new_to,
                "text": text[old_to:new_to],
            })
        out_from, out_to = min(new_from, old_from), max(new_to, old_to)
        if not segments:
            anchor_info = located.to_dict()
            anchor_info.pop("context", None)
            payload = {
                "status": "empty", "card_id": key, "rule_id": key,
                "chapter_no": chapter_no, "anchor": anchor_info,
                "segments": [], "text": "", "is_delta": True, "width": width,
                "cursor": _encode_cursor(key, old_from, old_to),
                "expansions": expansions, "capped": capped,
            }
            _mark_budget(payload, bs)
            payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                                  status="empty", rule_id=key, width=width,
                                  is_delta=True, returned_chars=0,
                                  budget_remaining=bs))
            return payload
    else:
        out_from = _align_left(text, max(0, start - width))
        out_to = _align_right(text, min(len(text), end + width))
        segments.append({
            "from": out_from, "to": out_to, "text": text[out_from:out_to],
        })
        is_delta = False

    out_text = "\n…\n".join(s["text"] for s in segments if s["text"])
    if len(out_text) > ANCHOR_MAX_CHARS:
        out_text = out_text[:ANCHOR_MAX_CHARS]
    anchor_info = located.to_dict()
    anchor_info.pop("context", None)
    status = "ok" if out_text else "empty"
    if bs["phase"] == PHASE_EXHAUSTED:
        status = "budget_exhausted"
    payload = {
        "status": status,
        "anchor_missing": False,
        "card_id": key,
        "rule_id": key,
        "chapter_no": chapter_no,
        "anchor": anchor_info,
        "span": {"from": out_from, "to": out_to},
        "segments": segments,
        "text": out_text,
        "returned_chars": len(out_text),
        "is_delta": is_delta,
        "width": width,
        "cursor": _encode_cursor(key, out_from, out_to),
        "expansions": expansions,
        "capped": capped,
        "budget_note": (
            f"该规则已扩窗 {expansions} 次，到上限 {ANCHOR_MAX_EXPANSIONS}；"
            "不再加宽，请改用 rule 摘要或换一条规则"
            if capped else
            "扩窗次数越多窗口越窄；继续扩用返回的 cursor，单次最多 4,000 字"
        ),
    }
    _mark_budget(payload, bs)
    payload.update(_trace(db, ctx, tool_name="anchor", started=started,
                          status=status, path=card.get("path"),
                          rule_id=key, width=width, is_delta=is_delta,
                          returned_chars=len(out_text), budget_remaining=bs))
    return payload


# --------------------------------------------------------------------------- #
# 5) similar
# --------------------------------------------------------------------------- #

def similar(db: Session, ctx: ToolContext, *, card_id: str | None = None,
            rule_id: str | None = None, k: int = 5) -> dict:
    """同 L4 → 同 L3 → 同 L2 的同类规则；不跨 L1。"""
    started = time.perf_counter()
    bs = _budget(db, ctx, "similar")
    key = str(card_id or rule_id or "").strip()
    if not key:
        return _fail(db, ctx, tool_name="similar", started=started,
                     message="需要 card_id 或 rule_id", budget=bs)
    k = max(1, min(SEARCH_MAX_K, int(k)))
    k = narrowed_k(k, bs)
    try:
        catalog = _catalog(db, ctx)
    except (CatalogNotBuilt, ValueError) as exc:
        _trace(db, ctx, tool_name="similar", started=started, status="error",
               rule_id=key, budget_remaining=bs, error=str(exc))
        return _error(ctx, str(exc), **_budget_fields(bs))

    seed = catalog.find(key)
    if not seed:
        payload = {"status": "empty", "card_id": key, "hits": []}
        _mark_budget(payload, bs)
        payload.update(_trace(db, ctx, tool_name="similar", started=started,
                              status="empty", rule_id=key, budget_remaining=bs))
        return payload

    pools: list[tuple[str, float, list[dict]]] = []
    if seed.get("l4"):
        pools.append(("same_l4", 0.30,
                      [catalog.cards[i] for i in catalog.by_l4().get(seed["l4"], [])]))
    if seed.get("l3"):
        pools.append(("same_l3", 0.20,
                      [catalog.cards[i] for i in catalog.by_l3().get(seed["l3"], [])]))
    if seed.get("l2"):
        pools.append(("same_l2", 0.10,
                      [catalog.cards[i] for i in catalog.by_l2().get(seed["l2"], [])]))

    scored: dict[str, tuple[float, str, dict]] = {}
    for relation, bonus, pool in pools:
        for card in pool:
            if card["id"] == seed["id"] or card.get("deprecated"):
                continue
            if card.get("l1") != seed.get("l1"):
                continue
            score = float((card.get("scores") or {}).get("total") or 0.0) + bonus
            old = scored.get(card["id"])
            if old is None or score > old[0]:
                scored[card["id"]] = (score, relation, card)
    ranked = sorted(scored.values(), key=lambda x: -x[0])[:k]
    hits = [{
        "card_id": card["id"],
        "rule_id": card["id"],
        "summary": card.get("summary") or "",
        "path": card.get("path"),
        "score": round(float(score), 3),
        "relation": relation,
    } for score, relation, card in ranked]
    payload = {
        "status": "ok" if hits else "empty",
        "distill_id": catalog.distill_id,
        "catalog_version": catalog.catalog_version,
        "card_id": seed["id"],
        "seed_path": seed.get("path"),
        "hits": hits,
    }
    _mark_budget(payload, bs)
    char_budget = SIMILAR_CHAR_BUDGET if bs["phase"] == PHASE_NORMAL else SIMILAR_CHAR_BUDGET // 2
    payload = _trim_json(payload, char_budget, key="hits")
    status = payload["status"]
    if bs["phase"] == PHASE_EXHAUSTED:
        status = "budget_exhausted"
    payload["status"] = status
    # 不传 rule_id：similar 不是「读某一条规则的锚点」，传了会让
    # `context_expansion_count` 把 seed 的历史扩窗算到这次查询头上。
    payload.update(_trace(db, ctx, tool_name="similar", started=started,
                          status=status, path=seed.get("path"),
                          card_id=seed["id"],
                          hit_ids=[h["card_id"] for h in payload["hits"]],
                          returned_chars=len(json.dumps(payload, ensure_ascii=False)),
                          budget_remaining=bs))
    return payload


CATALOG_TOOL_NAMES = ("browse", "search", "rule", "anchor", "similar")

_DEFAULT_POOL = {
    "browse": "catalog",
    "search": "catalog",
    "similar": "catalog",
    "rule": "rule",
    "anchor": "anchor",
}


# --------------------------------------------------------------------------- #
# registry 适配层（作者 agent / 助理 agent 共用）
# --------------------------------------------------------------------------- #

def _registry_context(project_id: int, kwargs: dict) -> ToolContext:
    """registry 调用没有会话上下文：轨迹由 registry 侧统一写。

    `distill_id` / `novel_id` 允许作为工具参数显式给，其余关键字段由
    `project_id` 解析；`trace_enabled=False` 避免工具内部和 registry 各写一行。
    `_trace_session_id` 由 registry 注入，仅用于**预算池计数**（不写轨迹）。
    """
    return ToolContext(
        session_id=str(kwargs.pop("_trace_session_id", "") or ""),
        project_id=int(project_id),
        novel_id=kwargs.pop("novel_id", None),
        distill_id=kwargs.pop("distill_id", None),
        trace_enabled=False,
    )


def tool_browse(*, db: Session, project_id: int, **kwargs: Any) -> dict:
    return browse(db, _registry_context(project_id, kwargs), **kwargs)


def tool_search(*, db: Session, project_id: int, **kwargs: Any) -> dict:
    return search(db, _registry_context(project_id, kwargs), **kwargs)


def tool_rule(*, db: Session, project_id: int, **kwargs: Any) -> dict:
    return rule(db, _registry_context(project_id, kwargs), **kwargs)


def tool_anchor(*, db: Session, project_id: int, **kwargs: Any) -> dict:
    return anchor(db, _registry_context(project_id, kwargs), **kwargs)


def tool_similar(*, db: Session, project_id: int, **kwargs: Any) -> dict:
    return similar(db, _registry_context(project_id, kwargs), **kwargs)


TOOL_CALLABLES = {
    "browse": tool_browse,
    "search": tool_search,
    "rule": tool_rule,
    "anchor": tool_anchor,
    "similar": tool_similar,
}
