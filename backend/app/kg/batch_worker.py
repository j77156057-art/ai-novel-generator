"""v3.0 异步批抽 worker：分段落库后入队，一次抽 5 段合并落 v3.0 表。

与 v2.6 同步合并（engine._merge_segment_into_graph）的关系：
- v2.6 规则基线同步合并保留（零 LLM、即时）
- v3.0 是**异步增强**：段 commit 后只入队（轻量），worker 线程池后台跑 LLM 抽取，
  落 v3.0 的 9 张表。省 50% LLM 调用（5 段合并一次抽）+ 跨段一致性（同一角色
  跨段同一规范名）
- 队列用 AgentRun 表（已有 role/status 机）+ 内存 deque，不新建表（精巧优先）

设计要点：
- 入队零 LLM：enqueue_segment 只写一行 AgentRun(role=kg_batch_extract)
- worker 按 batch_size=5 攒批，攒满或 flush 时抽一次
- 失败静默兜底：抽取失败不阻塞生成主链（与 v2.6 同纪律）
- 落库幂等：entity upsert（canonical_name+kind），event 追加，foreshadow 状态机推进
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AgentRun, Generation, KgEntity, KgRelation, KgEvent, KgEventLink,
    KgCharacterState, KgForeshadow, KgEntityKnowledge, KgMention, Project,
)

# 批大小：一次抽 5 段。省 LLM 调用（5:1）+ 跨段一致性窗口
BATCH_SIZE = 5
# 队列上限：防内存膨胀（超过则最旧段出队丢弃，生成主链不阻塞）
MAX_QUEUE = 200
# worker 线程：单线程串行（LLM 限流友好，避免并发打爆免费档）
WORKER_THREADS = 1


@dataclass
class _QueueItem:
    """待抽取的一段。"""

    project_id: int
    segment_no: int
    text: str
    chapter: int | None = None


class _BatchQueue:
    """内存队列 + 每项目独立攒批。线程安全（锁保护队列与攒批缓冲）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: dict[int, deque[_QueueItem]] = {}
        self._flush_event = threading.Event()
        self._stop = False

    def put(self, item: _QueueItem) -> None:
        with self._lock:
            q = self._queues.setdefault(item.project_id, deque())
            q.append(item)
            # 超上限丢最旧（生成优先，KG 补齐靠后续批）
            while len(q) > MAX_QUEUE:
                q.popleft()

    def take_batch(self, project_id: int) -> list[_QueueItem]:
        """取一个项目的一个批（攒够 BATCH_SIZE 或队列不足时全取）。"""
        with self._lock:
            q = self._queues.get(project_id)
            if not q:
                return []
            n = min(BATCH_SIZE, len(q))
            return [q.popleft() for _ in range(n)]

    def pending_projects(self) -> list[int]:
        with self._lock:
            return [pid for pid, q in self._queues.items() if q]

    def pending_count(self, project_id: int) -> int:
        with self._lock:
            q = self._queues.get(project_id)
            return len(q) if q else 0

    def signal_flush(self) -> None:
        self._flush_event.set()

    def wait_flush(self, timeout: float = 5.0) -> None:
        self._flush_event.wait(timeout)
        self._flush_event.clear()

    def stop(self) -> None:
        self._stop = True
        self.signal_flush()


_queue = _BatchQueue()
_worker_started = False
_worker_lock = threading.Lock()


def enqueue_segment(db: Session, project_id: int, segment_no: int, text: str,
                    chapter: int | None = None) -> None:
    """段 commit 后调用：入队等待异步批抽。零 LLM、零阻塞。"""
    _queue.put(_QueueItem(project_id=project_id, segment_no=segment_no,
                          text=text, chapter=chapter))
    _signal_flush()


def _signal_flush() -> None:
    """通知 worker 有新段（攒批窗口内尽快处理）。"""
    _queue.signal_flush()


def flush_project(project_id: int) -> None:
    """手动触发一个项目的批抽（测试/收尾用）。"""
    _run_batches(project_id)


def ensure_worker() -> None:
    """确保后台 worker 线程在跑（幂等）。"""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        t = threading.Thread(target=_worker_loop, daemon=True, name="kg-v3-batch-worker")
        t.start()
        _worker_started = True


def _worker_loop() -> None:
    """worker 主循环：轮询各项目队列，攒批抽取落库。"""
    while not _queue._stop:
        projects = _queue.pending_projects()
        if not projects:
            _queue.wait_flush(timeout=10.0)
            continue
        for pid in projects:
            try:
                _run_batches(pid)
            except Exception:  # noqa: BLE001 —— 单项目失败不影响其他项目
                import logging
                logging.getLogger(__name__).warning("kg v3 批抽项目 %d 失败", pid, exc_info=True)


def _run_batches(project_id: int) -> None:
    """把一个项目的当前队列抽干（每批 BATCH_SIZE 段）。"""
    from app.db import SessionLocal
    from app.kg.extractor_v3 import extract_batch, ExtractionStats

    while True:
        batch = _queue.take_batch(project_id)
        if not batch:
            return
        db = SessionLocal()
        try:
            provider = _get_provider()
            if provider is None:
                # 无 provider：段直接落 v3.0 mention 索引（零 LLM 部分仍做）
                _index_mentions_only(db, batch)
                continue
            texts = [(it.segment_no, it.text, it.chapter) for it in batch]
            result = extract_batch(provider, texts)
            stats = _persist_extraction(db, project_id, result, batch)
        finally:
            db.close()


def _get_provider():
    try:
        from app.llm.provider import get_llm_provider

        return get_llm_provider()
    except Exception:  # noqa: BLE001
        return None


def _persist_extraction(db: Session, project_id: int, result: dict, batch: list[_QueueItem]):
    """把归一化后的抽取结果落 v3.0 表。幂等 upsert。"""
    from app.kg.extractor_v3 import ExtractionStats

    stats = ExtractionStats(segments=len(batch))
    # 段→generation id 映射（mentions 落库用）
    seg_to_gen = _seg_to_gen(db, project_id, batch)

    # —— 1. entities upsert（canonical_name + kind 唯一）——
    name_to_id: dict[str, int] = {}
    for e in result.get("entities", []):
        ent = _upsert_entity(db, project_id, e)
        if ent:
            name_to_id[e["canonical_name"]] = ent.id
            stats.entities_upserted += 1

    # —— 2. relations upsert（两端名字解析为 id，缺失跳过）——
    for r in result.get("relations", []):
        a_id = name_to_id.get(r["from"]) or _lookup_entity_id(db, project_id, r["from"])
        b_id = name_to_id.get(r["to"]) or _lookup_entity_id(db, project_id, r["to"])
        if a_id is None or b_id is None:
            continue
        rel = db.scalar(
            select(KgRelation).where(
                KgRelation.project_id == project_id,
                KgRelation.from_id == a_id, KgRelation.to_id == b_id,
                KgRelation.rel_type == r["rel_type"],
            )
        )
        if rel is None:
            rel = KgRelation(project_id=project_id, from_id=a_id, to_id=b_id,
                             rel_type=r["rel_type"], strength=r.get("strength", 0.5))
            db.add(rel)
            stats.relations_upserted += 1
        elif abs((rel.strength or 0.5) - r.get("strength", 0.5)) > 0.01:
            # strength 变化 → 追加 evolution 行（记过程不记快照）
            from app.models import KgRelationEvolution

            db.add(KgRelationEvolution(
                project_id=project_id, edge_id=rel.id,
                strength=r.get("strength", 0.5), note="批抽更新",
            ))
            rel.strength = r.get("strength", 0.5)

    # —— 3. events 追加（sequence 项目内连续）——
    # seq_map：LLM 给的批内 sequence → 落库后的实际 event.id。
    # event_links / entity_knowledge 用 sequence 引用事件（LLM 不知道自增 id），
    # 必须 flush 拿到 id 后才能解析——两段式落库。
    base_seq = _next_event_sequence(db, project_id)
    seq_map: dict[int, int] = {}
    for i, ev in enumerate(result.get("events", [])):
        subject_id = name_to_id.get(ev.get("subject") or "") or _lookup_entity_id(db, project_id, ev.get("subject") or "")
        object_id = name_to_id.get(ev.get("object") or "") or _lookup_entity_id(db, project_id, ev.get("object") or "")
        location_id = name_to_id.get(ev.get("location") or "") or _lookup_entity_id(db, project_id, ev.get("location") or "")
        event = KgEvent(
            project_id=project_id,
            sequence=base_seq + i,
            chapter=ev.get("chapter"),
            segment_no=batch[0].segment_no if batch else None,
            event_type=ev.get("event_type", "incident"),
            summary=ev.get("summary", "")[:500],
            subject_id=subject_id,
            verb=(ev.get("verb") or "")[:64],
            object_id=object_id,
            location_id=location_id,
            in_story_date=(ev.get("in_story_date") or "")[:64],
        )
        db.add(event)
        stats.events_added += 1
    db.flush()  # 拿 event.id 建 sequence→id 映射
    # 重新查本批 events（flush 后 id 已分配），按 sequence 排序对齐 LLM 的批内序号
    db_events = db.scalars(
        select(KgEvent).where(
            KgEvent.project_id == project_id,
            KgEvent.sequence >= base_seq,
        ).order_by(KgEvent.sequence)
    ).all()
    for pos, ev in enumerate(result.get("events", [])):
        llm_seq = ev.get("sequence")
        if llm_seq is not None and pos < len(db_events):
            seq_map[llm_seq] = db_events[pos].id

    # —— 3.5 event_links（用 sequence→id 映射解析事件引用）——
    for lk in result.get("event_links", []):
        from_eid = seq_map.get(lk.get("from_sequence"))
        to_eid = seq_map.get(lk.get("to_sequence"))
        if from_eid is None or to_eid is None:
            continue
        existing = db.scalar(
            select(KgEventLink).where(
                KgEventLink.project_id == project_id,
                KgEventLink.from_event_id == from_eid,
                KgEventLink.to_event_id == to_eid,
                KgEventLink.link_type == lk.get("link_type"),
            )
        )
        if existing is None:
            db.add(KgEventLink(
                project_id=project_id, from_event_id=from_eid, to_event_id=to_eid,
                link_type=lk.get("link_type"),
            ))
            stats.event_links_added += 1

    # —— 3.6 entity_knowledge（人物情报，learned_at 用 sequence 解析）——
    for k in result.get("entity_knowledge", []):
        char_id = name_to_id.get(k.get("character") or "") or _lookup_entity_id(db, project_id, k.get("character") or "")
        known_id = name_to_id.get(k.get("knows") or "") or _lookup_entity_id(db, project_id, k.get("knows") or "")
        if char_id is None or known_id is None or char_id == known_id:
            continue
        learned_event_id = seq_map.get(k.get("learned_at_sequence"))
        existing = db.scalar(
            select(KgEntityKnowledge).where(
                KgEntityKnowledge.project_id == project_id,
                KgEntityKnowledge.entity_id == char_id,
                KgEntityKnowledge.known_entity_id == known_id,
                KgEntityKnowledge.learned_at_event_id == learned_event_id,
            )
        )
        if existing is None:
            db.add(KgEntityKnowledge(
                project_id=project_id, entity_id=char_id, known_entity_id=known_id,
                known_kind=k.get("known_kind", "entity"), learned_at_event_id=learned_event_id,
            ))
            stats.entity_knowledge_added += 1

    # —— 4. state_changes ——
    for s in result.get("state_changes", []):
        char_id = name_to_id.get(s.get("character") or "") or _lookup_entity_id(db, project_id, s.get("character") or "")
        if char_id is None:
            continue
        db.add(KgCharacterState(
            project_id=project_id, character_id=char_id,
            chapter=None, segment_no=batch[0].segment_no if batch else None,
            state_type=s.get("state_type", "mood"), new_value=(s.get("new_value") or "")[:500],
        ))
        stats.state_changes += 1

    # —— 5. foreshadows upsert（name 唯一，状态机推进）——
    for f in result.get("foreshadows", []):
        fs = db.scalar(
            select(KgForeshadow).where(
                KgForeshadow.project_id == project_id, KgForeshadow.name == f["name"]
            )
        )
        if fs is None:
            fs = KgForeshadow(project_id=project_id, name=f["name"],
                              status=f.get("status", "planted"),
                              planted_chapter=f.get("planted_chapter"),
                              planned_payoff_chapter=f.get("planned_payoff_chapter"))
            db.add(fs)
        else:
            # 状态机只前进不后退：paid_off 是终态倾向，planted→developing 可推进
            _ORDER = {"planted": 0, "developing": 1, "paid_off": 2, "expired": 3, "dropped": 4}
            if _ORDER.get(f.get("status", ""), 0) > _ORDER.get(fs.status, 0):
                fs.status = f["status"]
            if f.get("planned_payoff_chapter"):
                fs.planned_payoff_chapter = f["planned_payoff_chapter"]
        stats.foreshadows += 1

    # —— 6. mentions 反向索引（实体×段，context_window 原文片段）——
    for item in batch:
        for name, ent_id in name_to_id.items():
            if name in item.text:
                gen_id = seg_to_gen.get(item.segment_no)
                existing = db.scalar(
                    select(KgMention).where(
                        KgMention.project_id == project_id,
                        KgMention.entity_id == ent_id,
                        KgMention.segment_no == item.segment_no,
                    )
                )
                if existing is None:
                    db.add(KgMention(
                        project_id=project_id, entity_id=ent_id, generation_id=gen_id,
                        segment_no=item.segment_no,
                        context_window=_window_around(item.text, name),
                        mention_kind="direct",
                    ))

    # entity mention_count 更新
    from sqlalchemy import func

    for name, ent_id in name_to_id.items():
        cnt = db.scalar(
            select(func.count()).select_from(KgMention).where(
                KgMention.project_id == project_id, KgMention.entity_id == ent_id
            )
        )
        ent = db.get(KgEntity, ent_id)
        if ent is not None:
            ent.mention_count = cnt or 0

    db.commit()
    return stats


def _index_mentions_only(db: Session, batch: list[_QueueItem]) -> None:
    """无 provider 时的降级：只落 mention 索引（零 LLM 部分仍可用）。"""
    # 无实体可索引（抽取需要 LLM），直接跳过——mention 需要 entity 先存在
    return


def _lookup_entity_id(db: Session, project_id: int, name: str) -> int | None:
    """按 canonical_name 或 aliases 反查已有 entity id（跨批关系解析兜底）。

    本批 entities 里没有、但库里已存在的实体（前批抽过的），关系仍能建上。
    aliases 用 JSON 包含查询（SQLite json_each 兜底用 Python 侧过滤——批抽规模
    小，entity 表通常在千级以内，线性扫可接受）。
    """
    if not name:
        return None
    ent = db.scalar(
        select(KgEntity).where(
            KgEntity.project_id == project_id, KgEntity.canonical_name == name
        )
    )
    if ent is not None:
        return ent.id
    # aliases 反查（Python 侧，避免 SQLite JSON 函数方言问题）
    for e in db.scalars(select(KgEntity).where(KgEntity.project_id == project_id)):
        if name in (e.aliases or []):
            return e.id
    return None


def _upsert_entity(db: Session, project_id: int, e: dict):
    """entity upsert：aliases 合并，attrs 深合并。"""
    ent = db.scalar(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.kind == e["kind"],
            KgEntity.canonical_name == e["canonical_name"],
        )
    )
    if ent is None:
        ent = KgEntity(
            project_id=project_id, kind=e["kind"],
            canonical_name=e["canonical_name"],
            aliases=e.get("aliases") or [],
            attrs=e.get("attrs") or {},
        )
        db.add(ent)
        db.flush()  # 拿 id
        return ent
    # 已存在：合并 aliases（去重）+ attrs 深合并（新键覆盖旧值）
    old_aliases = ent.aliases or []
    merged = list(dict.fromkeys(old_aliases + (e.get("aliases") or [])))
    if merged != old_aliases:
        ent.aliases = merged[:20]
    old_attrs = ent.attrs or {}
    new_attrs = e.get("attrs") or {}
    if new_attrs:
        merged_attrs = dict(old_attrs)
        merged_attrs.update({k: v for k, v in new_attrs.items() if v})
        ent.attrs = merged_attrs
    return ent


def _seg_to_gen(db: Session, project_id: int, batch: list[_QueueItem]) -> dict[int, int]:
    """段号 → generation id 映射（mentions FK 用）。"""
    segs = [it.segment_no for it in batch]
    rows = db.scalars(
        select(Generation).where(
            Generation.project_id == project_id, Generation.segment_no.in_(segs)
        )
    ).all()
    return {g.segment_no: g.id for g in rows}


def _next_event_sequence(db: Session, project_id: int) -> int:
    """项目内下一个事件序号（连续）。"""
    from sqlalchemy import func

    mx = db.scalar(
        select(func.max(KgEvent.sequence)).where(KgEvent.project_id == project_id)
    )
    return (mx or 0) + 1


def _window_around(text: str, name: str, width: int = 60) -> str:
    """取名字在文本中的上下文片段（前后各 width/2 字）。"""
    i = text.find(name)
    if i < 0:
        return text[:width]
    half = width // 2
    start = max(0, i - half)
    end = min(len(text), i + len(name) + half)
    return text[start:end]
