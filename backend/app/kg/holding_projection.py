"""v3.0 抽取 → ``generated.db`` 的持有投影（运行时唯一写入口）。

## 为什么要有这个模块

v3.0 批抽（``batch_worker._persist_extraction``）只写 ``novel_dev.db`` 的九张表。
``generated.db`` 的组 / 写法 / 持有段在此之前**只有迁移脚本写过**：库里 60 条持有行
全部产自 2026-11-10 的回填，运行时零写入（见报告 §J）。于是「正文写出来 → 持有事实
能按书读出来」这条链在中间断掉：抽取记 success，持有层却永远不增长。

本模块把「本批抽取里**带持有证据**的物品」投影成 组 → 写法 → 持有段：

1. 物品：``entities`` 里 ``kind='item'`` 且有 ``attrs.held_by``；或
2. ``relations`` 里 ``rel_type='owns'`` 的物品端（另一端起持有者）；或
3. ``events`` 里客体是本批物品、动词属于「取得/持有」白名单的事件。

没看到持有者就**不写**（漏报优于误报，不猜「无人持有」）。

## 幂等键（同一份抽取重跑必须零新增）

- 组：``(project_id, kind, COALESCE(novel_id,-1), canonical_name)`` 唯一索引；
- 写法：``(project_id, COALESCE(novel_id,-1), surface)`` 唯一索引；
- 持有段：``(COALESCE(item_id,-1), item_name, hold_type, COALESCE(instance_id,-1))
  WHERE valid_to IS NULL`` —— 同主同章复述由 ``record_holding`` 返回 ``confirmed``，
  重跑不重开段。

## 失败口径

- 连接/建表失败 → 直接抛，由 ````batch_worker`` 把批标 failed（不能 success 但没写）；
- 单条物品/持有者投影失败 → 收进 ``stats["errors"]``，同样让批 failed，但其它条继续写。
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Callable, Iterable

from app.kg import generated_store

_log = logging.getLogger(__name__)

#: 视为「取得/持有」的动词（子串匹配）。故意窄：宁可漏一条持有，也不要
#: 把「交给他」「递过去」这类**给出**动作误判成施主动持有。
OWN_VERBS = (
    "收进", "收起", "收回", "夺得", "夺过", "抢过", "抢走", "握在", "握住",
    "持有", "佩戴", "佩上", "携带", "拿起", "拾起", "捡起", "买下", "购得",
    "获得", "得到", "盗走", "偷走", "揣进", "塞进", "别在",
)
#: held_by 里表示「无主」的写法：不写持有行。
_NO_OWNER = frozenset({"", "无", "暂无", "无主", "无人", "未知", "unknown", "none", "null"})
#: 投影只写这一种持有态（其余态做不出可信判定）。
HOLD_TYPE = "own"


def _norm(value) -> str:
    return str(value or "").strip()


def _chapter_of(item) -> int:
    """段所属的故事章号：优先队列项带的 chapter，缺了用段号（与一天一章的现状一致）。"""
    return int(item.chapter if item.chapter is not None else item.segment_no)


def _window(text: str, name: str, radius: int = 40) -> str:
    """取命中词前后的原文片段（落 evidence，最长 200 字）。"""
    if not text or not name:
        return ""
    idx = text.find(name)
    if idx < 0:
        return ""
    return text[max(0, idx - radius): idx + len(name) + radius][:200]


def _locate(batch: list, names: Iterable[str]) -> tuple[int, int]:
    """把一条关系/事件定位到最可能的段：返回 ``(segment_no, chapter)``。

    打分 = 各端名字在该段正文里的出现次数；都没命中退到第一条提到物品的段，
    再退到批首段。批内 5 段以内，这比「统一记批首段」（旧行为）精确得多。
    """
    names = [n for n in names if n and len(n) >= 2]
    best, best_score = None, 0
    for item in batch:
        text = item.text or ""
        score = sum(text.count(n) for n in names)
        if score > best_score:
            best, best_score = item, score
    if best is None:
        if not batch:  # 空批：防御性返回
            return 0, 0
        best = next(
            (it for it in batch
             if names and any(n in (it.text or "") for n in names)),
            batch[0])
    return int(best.segment_no), _chapter_of(best)


def collect_pairs(result: dict, batch: list, *,
                  kind_of: Callable[[str], str | None]) -> list[dict]:
    """从抽取结果里收集 ``(物品, 持有者)`` 主张，按章排序后返回。

    同一条 (物品, 持有者) 被多种来源同时支持时只留置信度最高的一条，
    ``sources`` 记全部来源（报告/排查用）。
    """
    pairs: dict[tuple[str, str], dict] = {}

    def _add(item_name: str, owner_name: str, *, source: str,
             confidence: float, segment_no: int, chapter: int,
             evidence: str) -> None:
        item_name, owner_name = _norm(item_name), _norm(owner_name)
        if not item_name or not owner_name or item_name == owner_name:
            return
        key = (item_name, owner_name)
        prev = pairs.get(key)
        cand = {
            "item": item_name, "owner": owner_name, "source": source,
            "confidence": float(confidence or 0.5),
            "segment_no": segment_no, "chapter": chapter,
            "evidence": evidence[:200], "sources": [source],
        }
        if prev is None:
            pairs[key] = cand
        else:
            if source not in prev["sources"]:
                prev["sources"].append(source)
            if cand["confidence"] > prev["confidence"]:
                prev.update({k: v for k, v in cand.items() if k != "sources"})

    # 1) item 实体的 attrs.held_by（抽取 prompt 约定「持有者写这里，不镜像 owns」）
    for e in result.get("entities") or []:
        if _norm(e.get("kind")) != "item":
            continue
        name = _norm(e.get("canonical_name"))
        owner = _norm((e.get("attrs") or {}).get("held_by"))
        if not name or owner.lower() in _NO_OWNER:
            continue
        seg, chapter = _locate(batch, [name, owner])
        item = next((b for b in batch if int(b.segment_no) == seg), batch[0])
        _add(name, owner, source="attr_held_by", confidence=0.6,
             segment_no=seg, chapter=chapter,
             evidence=_window(item.text, name))

    # 2) owns 关系（物品端可能是 from 也可能是 to）
    for r in result.get("relations") or []:
        if _norm(r.get("rel_type")) != "owns":
            continue
        a, b = _norm(r.get("from")), _norm(r.get("to"))
        a_kind = kind_of(a) if a else None
        b_kind = kind_of(b) if b else None
        if b_kind == "item" and a_kind != "item":
            item_name, owner = b, a
        elif a_kind == "item" and b_kind != "item":
            item_name, owner = a, b
        else:
            continue
        seg, chapter = _locate(batch, [item_name, owner])
        item = next((x for x in batch if int(x.segment_no) == seg), batch[0])
        _add(item_name, owner, source="rel_owns",
             confidence=float(r.get("strength") or 0.6),
             segment_no=seg, chapter=chapter,
             evidence=_window(item.text, item_name))

    # 3) 事件：客体是本批物品 + 动词命中「取得/持有」白名单
    for idx, ev in enumerate(result.get("events") or []):
        item_name = _norm(ev.get("object"))
        owner = _norm(ev.get("subject"))
        verb = _norm(ev.get("verb")) or _norm(ev.get("summary"))
        if not item_name or kind_of(item_name) != "item":
            continue
        if not any(v in verb for v in OWN_VERBS):
            continue
        seg_no = ev.get("segment")
        seg, chapter = _locate(batch, [item_name, owner])
        if isinstance(seg_no, int) and any(int(b.segment_no) == seg_no for b in batch):
            seg = seg_no
            chapter = _chapter_of(
                next(b for b in batch if int(b.segment_no) == seg_no))
        item = next((x for x in batch if int(x.segment_no) == seg), batch[0])
        _add(item_name, owner, source="event_verb", confidence=0.5,
             segment_no=seg, chapter=chapter,
             evidence=_norm(ev.get("summary"))[:200]
             or _window(item.text, item_name))

    return sorted(pairs.values(), key=lambda p: (p["chapter"], p["segment_no"], p["item"]))


def project_holdings(*, project_id: int, book: int | None, result: dict,
                     batch: list, kind_of: Callable[[str], str | None],
                     owner_lookup: Callable[[str], tuple[int | None, str | None]],
                     event_lookup: Callable[[str, str, int | None], int | None] | None = None,
                     generated_db: str | None = None) -> dict:
    """把本批抽取的持有主张投影进 ``generated.db``，返回可观测统计。

    ``owner_lookup(name) -> (entity_id, kind)``：解析持有者实体（本批优先，
    跨批只认同书或通配）。投影拿不到 entity_id 时仍写 ``owner_name``
    （``record_holding`` 允许「有主但主人未实体化」）。
    """
    pairs = collect_pairs(result, batch, kind_of=kind_of)
    stats = {
        "pairs": len(pairs),
        "groups_created": 0, "surfaces_created": 0,
        "holdings": Counter(), "story_segments": 0,
        "surface_conflicts": [], "errors": [],
    }
    if not pairs:
        return {**stats, "holdings": {}}
    path = generated_db or _default_generated_db()
    con = generated_store.connect(path)
    try:
        for p in pairs:
            try:
                reg = generated_store.ensure_item_group(
                    con, p["item"], project_id=project_id, novel_id=book,
                    evidence=p["evidence"], chapter=p["chapter"])
                stats["groups_created"] += int(reg["group_created"])
                stats["surfaces_created"] += int(reg["surface_created"])
                if reg.get("surface_conflict"):
                    stats["surface_conflicts"].append(
                        {"item": p["item"], **reg["surface_conflict"]})
                owner_id, owner_kind = owner_lookup(p["owner"])
                # transfer_event_id：这条持有是「哪件事」造成的。按
                # (主体=持有者, 客体=物品) 反查本批已落库事件；查不到留空，
                # 不拿别的角色的事件硬凑。
                transfer_id = None
                if event_lookup is not None:
                    try:
                        transfer_id = event_lookup(
                            p["item"], p["owner"], p.get("segment_no"))
                    except Exception:  # noqa: BLE001 —— 查事件失败不阻断持有投影
                        transfer_id = None
                res = generated_store.record_holding(
                    con, p["item"], HOLD_TYPE, p["owner"], p["chapter"],
                    owner_id=owner_id, owner_kind=owner_kind or "character",
                    transfer_event_id=transfer_id,
                    event_object=(p["item"] if transfer_id is not None else None),
                    reason_code=p["source"], evidence=p["evidence"],
                    confidence=p["confidence"], novel_id=book,
                    project_id=project_id)
                action = res.get("action") or "unknown"
                stats["holdings"][action] += 1
                if action in ("inserted", "replaced", "extended_back") and res.get("id"):
                    seg = generated_store.record_story_segment(
                        con, p["item"], HOLD_TYPE, p["owner"], p["chapter"],
                        owner_id=owner_id, owner_kind=owner_kind or "character",
                        owner_state="held", holding_id=res["id"],
                        narration="forward", evidence=p["evidence"],
                        confidence=p["confidence"], novel_id=book,
                        project_id=project_id)
                    if seg.get("action") in ("inserted", "confirmed"):
                        stats["story_segments"] += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败要可见，但不连坐
                stats["errors"].append(
                    f"{p['item']}←{p['owner']}: {type(exc).__name__}: {exc}"[:300])
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {**stats, "holdings": dict(stats["holdings"])}


def _default_generated_db() -> str:
    from app.config import settings

    return settings.kg_generated_db
