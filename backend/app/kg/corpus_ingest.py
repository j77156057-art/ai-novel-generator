"""参考语料 → KG v3 补建器（corpus_ingest）。

## 为什么需要它

KG v3 的常规入口（``batch_worker.enqueue_segment`` / ``recover_running``）只认
``generations`` 表：生成主链每写一段，才把**生成正文**喂给抽取器。参考语料
（``novel_chunks``）没有任何进图路径——所以《修真聊天群》3173 章虽然蒸馏全进了
（``distill_chunks`` 635 块铺满 1–3173、``style_distills`` 记录 3173 章），
v3 的 entities/relations/events/mentions 却一行都没有：**蒸馏只产文风，不产 KG**。

本模块补的就是这一环：把参考语料按「章窗口」喂给**同一个** v3 抽取器
（``extract_v3.extract_batch``，不另造口径），落库到同一组 v3 表，只是带上
``novel_id``、并用章号当 ``segment_no``。

## 断点续跑

每个窗口先在 ``kg_corpus_windows`` 登记一行；抽取在线程池里跑，**落库只在主线程**
按窗口一个事务提交。中断后重跑时 done 的窗口直接跳过，failed/pending 才重试——
全本 3173 章是一次几十分钟到数小时的长任务，不能靠「一口气跑完」。

## 只写一本书

所有查询/写入都带 ``novel_id`` 与 ``project_id`` 双条件，不碰别的书（尤其项目 5）。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.kg.extractor_v3 import ERROR_FIELD, extract_batch, extract_targeted
from app.models import (
    KgCharacterState, KgCorpusWindow, KgEntity, KgEntityKnowledge, KgEvent,
    KgEventLink, KgForeshadow, KgIngestQuarantine, KgMention, KgRelation, NovelChunk,
)

logger = logging.getLogger(__name__)

#: 一批喂几章。3 章是吞吐与截断的折中：窗口越大单次越划算（prompt 摊销），
#: 但 5 章窗口的 JSON 会顶到模型 4096 输出上限被截断（finish_reason=length）→
#: parse_failed。3 章窗口约 2–3.5k 输出，截断率可接受，失败窗口可单独重跑。
DEFAULT_CHAPTERS_PER_WINDOW = 3

#: 单窗口字符上限：超了就提前封口，避免超长章节把 prompt 顶爆。
DEFAULT_MAX_WINDOW_CHARS = 16_000

#: 抽取并发。urllib 每次新建连接，provider 无共享状态，可安全多线程。
DEFAULT_CONCURRENCY = 8

#: 事件 sequence 的章内步长：sequence = chapter * 1000 + 章内序号。
_SEQ_STRIDE = 1000

#: 单窗抽取的失败重试（只针对**可重试**的失败，见 ``_RETRY_HINTS``）。
#: 智谱 glm-4-flash 免费档的并发上限低于 24：突发并发会成批 429，而 provider
#: 层对 429 的处理是「换模型」，候选链全被限流时整窗直接失败。这里补一层
#: 指数退避，把突发摊平——实测并发 24 时 6 次退避（最长 25s）能把 429 吃掉。
_RETRY_ATTEMPTS = 6
_RETRY_HINTS = ("429", "Too Many Requests", "timed out", "timeout",
                "502", "503", "connection reset")


def _should_retry(err: str | None) -> bool:
    msg = str(err or "")
    return any(h in msg for h in _RETRY_HINTS)


def _retry_pause(attempt: int) -> None:
    time.sleep(min(25.0, 4.0 * (attempt + 1)) + (time.time() % 1.0))


#: 语料补建的输出预算约束（追加在批抽 prompt 末尾，只走 corpus 路径）。
#: 2026-11-12 实测：按「每章 8 条事件」说话时，3 章窗口的模型仍会写到 24 条
#: → completion 顶到 4095 触发 finish_reason=length，半数窗口 parse_failed。
#: 改成**整窗合计**预算后输出稳定落在 ~1500 token，截断率归零、单窗耗时减半。
_OUTPUT_NOTE = (
    "【输出预算】整窗（下面这几章合计）最多输出 12 条 events，挑对主线影响最大的；"
    "summary 不超过 30 字。整窗 entities 不超过 15 个、relations 不超过 10 条。"
    "宁可少列，也必须保证 JSON 完整闭合——被截断的输出视为无效。"
)


#: glm-4-flash 的单次输出硬上限。distiller 全局配置是 32768（给长篇小说正文用），
#: 但 glm-4-flash 实际只有 4096：请求里写 32768 并不会报错，只会让服务端按
#: 32768 预留输出配额 → 并发 6 以上成批 429（实测 60% 窗口失败，且每次 429
#: 都要走一遍候选链切换 + 退避，wall time 全浪费在等待上）。
#: 2026-11-12 实测对照（5 窗并发）：max_tokens=32768 → 成功率 42%；
#: max_tokens=4096 → 成功率 80%、无 429。故补建链路统一压到 4096。
_FLASH_OUTPUT_CAP = 4096

#: 补洞通道的追加约束：这些章在批量抽取里被模型整章跳过，单章重跑时要求它
#: 至少落一条事件（日常/铺垫也算），否则时间线仍会留空章。真的没有任何情节
#: 的章节（作者感言、说明）才允许返回空数组。
_FILL_NOTE = (
    "【补洞·禁止空数组】这一章**必须**输出至少 1 条 events。哪怕整章只是日常闲聊、"
    "吃饭、赶路、布置任务，也要写一条（event_type 用 daily），格式照样是"
    "「谁 + 在哪 + 做了什么 + 结果」，summary 不超过 30 字。"
    "只有整章是作者感言/更新说明、完全没有人物动作时，才允许 events 为空。"
    "整窗 entities 不超过 12 个、relations 不超过 8 条。"
)


#: 作者公告章（non-story）：有正文、但整章没有可抽情节，永久排除在「空章」口径之外。
#: 2026-11-13 复核：两章都是作者单章，相邻章（1715/1717、2123/2125）衔接正常，
#: 不是解析截断、也不是串章；补抽只会得到空 events，因此不补。
#:   - 1716（364 字）：换封面 + 求月票 + 公众号
#:   - 2124（384 字）：求月票 + 客户端活动 + 推《神话版三国》
NON_STORY_CHAPTERS: dict[int, str] = {
    1716: "作者公告：换封面/求月票/公众号",
    2124: "作者公告：求月票/客户端活动/推书",
}


def _refresh_window_counts(db: Session, novel_id: int, chapter: int) -> None:
    """按**实际落库数据**重算该章所属窗口的 n_events / n_entities。

    `fill_gaps` 走单章补洞，不经过整窗主循环，此前只写事件、不回写
    `kg_corpus_windows` 的计数，于是窗口账本长期停在历史快照（2026-11-13
    实测 40 个 done 但计数为 0 的窗口里 39 个其实有事件）。这里统一按
    窗口章区间重算：事件数 = 区间内 kg_events 行数；实体数 = 区间内事件的
    subject / object / location 去重数（与整窗 stats 口径略有差异，但单调且
    可复现，不再依赖某一次抽取的 payload）。
    """
    row = db.scalar(select(KgCorpusWindow).where(
        KgCorpusWindow.novel_id == novel_id,
        KgCorpusWindow.chapter_start <= chapter,
        KgCorpusWindow.chapter_end >= chapter,
    ))
    if row is None:
        return
    triples = db.execute(select(
        KgEvent.subject_id, KgEvent.object_id, KgEvent.location_id,
    ).where(
        KgEvent.novel_id == novel_id,
        KgEvent.chapter >= row.chapter_start,
        KgEvent.chapter <= row.chapter_end,
    )).all()
    row.n_events = len(triples)
    row.n_entities = len({i for t in triples for i in t if i})


#: 非 glm 补洞 provider 的默认输出/超时预算（2026-11-13）：
#:   - max_tokens=8192 让 9 章被 finish_reason=length 拦腰截断
#:     （1101/1114/1476/1577/1629/1745/2907/2985/3067），JSON 不闭合即判失败；
#:   - 3067 单章实测 410s、reasoning 11,369 tokens，300s 超时同样会误杀。
#: 故补建/补洞链路对非 glm 模型统一抬到 32768 / 600s；glm-4-flash 仍压回 4096。
_FILL_MAX_TOKENS = 32768
_FILL_TIMEOUT = 600


def _fit_provider(provider):
    """按模型给补建链路设输出/超时预算（就地改，本进程专用）。

    glm-4-flash 的服务端硬上限是 4096，写大了只会在并发时成批 429，故保持压回；
    其他模型（Command Code / MiniMax / DashScope）统一用补洞预算，避免截断与超时。
    """
    model = str(getattr(provider, "model", "") or "")
    if "glm-4-flash" in model:
        if (getattr(provider, "max_tokens", None) or 0) > _FLASH_OUTPUT_CAP:
            provider.max_tokens = _FLASH_OUTPUT_CAP
        return provider
    if (getattr(provider, "max_tokens", None) or 0) < _FILL_MAX_TOKENS:
        provider.max_tokens = _FILL_MAX_TOKENS
    if (getattr(provider, "timeout", None) or 0) < _FILL_TIMEOUT:
        provider.timeout = _FILL_TIMEOUT
    return provider


@dataclass
class WindowResult:
    window_id: int
    window_key: str
    chapter_start: int
    chapter_end: int
    payload: dict | None
    error: str | None = None


# --------------------------------------------------------------------------- #
# 语料切窗
# --------------------------------------------------------------------------- #

def load_chapters(db: Session, novel_id: int) -> list[tuple[int, str]]:
    """按章号升序返回 ``(chapter_no, 正文)``；同章多块按 idx 拼接。"""
    rows = db.execute(
        select(NovelChunk.chapter_no, NovelChunk.text)
        .where(NovelChunk.novel_id == novel_id)
        .order_by(NovelChunk.chapter_no, NovelChunk.idx)
    ).all()
    chapters: list[tuple[int, str]] = []
    cur_no: int | None = None
    buf: list[str] = []
    for no, text in rows:
        if no is None:
            continue
        if no != cur_no:
            if cur_no is not None and buf:
                chapters.append((cur_no, "\n".join(buf)))
            cur_no = no
            buf = []
        buf.append(text or "")
    if cur_no is not None and buf:
        chapters.append((cur_no, "\n".join(buf)))
    return chapters


def build_windows(chapters: list[tuple[int, str]],
                  per_window: int = DEFAULT_CHAPTERS_PER_WINDOW,
                  max_chars: int = DEFAULT_MAX_WINDOW_CHARS) -> list[list[tuple[int, str]]]:
    """把章节按「最多 per_window 章 / 最多 max_chars 字」切成窗口。"""
    windows: list[list[tuple[int, str]]] = []
    cur: list[tuple[int, str]] = []
    cur_chars = 0
    for no, text in chapters:
        if cur and (len(cur) >= per_window or cur_chars + len(text) > max_chars):
            windows.append(cur)
            cur, cur_chars = [], 0
        cur.append((no, text))
        cur_chars += len(text)
    if cur:
        windows.append(cur)
    return windows


def window_key(novel_id: int, win: list[tuple[int, str]]) -> str:
    return f"{novel_id}:{win[0][0]}-{win[-1][0]}"


def register_windows(db: Session, project_id: int, novel_id: int,
                     windows: list[list[tuple[int, str]]]) -> int:
    """把还没登记的窗口写进进度表（幂等）。返回新登记数。"""
    existing = {
        k for (k,) in db.execute(
            select(KgCorpusWindow.window_key).where(
                KgCorpusWindow.novel_id == novel_id,
                KgCorpusWindow.project_id == project_id,
            )
        )
    }
    added = 0
    for win in windows:
        key = window_key(novel_id, win)
        if key in existing:
            continue
        db.add(KgCorpusWindow(
            project_id=project_id, novel_id=novel_id, window_key=key,
            chapter_start=win[0][0], chapter_end=win[-1][0], status="pending",
        ))
        added += 1
    db.commit()
    return added


def pending_windows(db: Session, novel_id: int, project_id: int,
                    retry_failed: bool = True) -> list[KgCorpusWindow]:
    statuses = ["pending"]
    if retry_failed:
        statuses.append("failed")
    return list(db.scalars(
        select(KgCorpusWindow).where(
            KgCorpusWindow.novel_id == novel_id,
            KgCorpusWindow.project_id == project_id,
            KgCorpusWindow.status.in_(statuses),
        ).order_by(KgCorpusWindow.chapter_start)
    ))


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #

def _coalesce_novel(col):
    return func.coalesce(col, -1)


#: 定向补抽允许**改写**的旧 kind：主抽把组织判成 `info`（事件宾语兜底）时，
#: faction/power_system 的定向结论更可信。
#: 只允许从「兜底类 info」改判成 faction / power_system。
#: 不要把 character 也放进来：2026-11-12 全量实测，定向通道会把主角团
#: （宋书航 1980 次提及、羽柔子、白尊者…）整批判成 faction，因为 prompt 问的是
#: 「这里有没有组织」，模型就把出场人物都说成组织成员。character 在主抽里是
#: 判对的，不能再被覆盖。
_UPGRADABLE_KINDS = {"info"}


def _resolve_entity(db: Session, project_id: int, novel_id: int,
                    name: str, kind: str, chapter: int,
                    aliases: list[str] | None = None,
                    attrs: dict | None = None,
                    cache: dict[str, KgEntity] | None = None,
                    upgrade_kind: bool = False) -> KgEntity | None:
    """按规范名找实体；找不到且给了 kind 就新建。名字为空返回 None。

    ``upgrade_kind=True`` 时允许把已有的 info 改判成 faction / power_system
    （定向补抽专用）；其余情况一律保留首次判定的 kind。
    """
    name = (name or "").strip()
    if not name:
        return None
    if cache is not None and name in cache:
        return cache[name]
    ent = db.scalar(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.canonical_name == name,
            _coalesce_novel(KgEntity.novel_id) == novel_id,
        ).order_by(KgEntity.id)
    )
    if ent is None:
        if not kind:
            # 只解析不新建（state_changes / entity_knowledge 里的名字若从未
            # 作为实体出现，宁可不落这条状态，也不要凭空造一个无 kind 的节点）
            return None
        ent = KgEntity(
            project_id=project_id, novel_id=novel_id, kind=kind,
            canonical_name=name, aliases=list(aliases or []),
            attrs=dict(attrs or {}), first_chapter=chapter, last_chapter=chapter,
            mention_count=1, confidence=0.5,
        )
        db.add(ent)
        db.flush()
    else:
        merged = list(ent.aliases or [])
        for a in aliases or []:
            if a and a != name and a not in merged:
                merged.append(a)
        ent.aliases = merged
        if (upgrade_kind and kind in ("faction", "power_system")
                and ent.kind in _UPGRADABLE_KINDS):
            ent.kind = kind
        if attrs:
            cur = dict(ent.attrs or {})
            for k, v in attrs.items():
                if v not in (None, "", "unknown", "未知", "无", "未提及"):
                    cur.setdefault(k, v)
            ent.attrs = cur
        if ent.first_chapter is None or chapter < ent.first_chapter:
            ent.first_chapter = chapter
        if ent.last_chapter is None or chapter > ent.last_chapter:
            ent.last_chapter = chapter
        ent.mention_count = (ent.mention_count or 0) + 1
    if cache is not None:
        cache[name] = ent
    return ent


def persist_window(db: Session, *, project_id: int, novel_id: int,
                   win: list[tuple[int, str]], payload: dict) -> dict:
    """把一个窗口的抽取结果写进 v3 表。整窗一个事务（调用方 commit/rollback）。

    幂等口径：同一 ``(novel_id, chapter)`` 的旧事件先删后写；实体/关系按
    唯一键合并（不删历史章节区间）。
    """
    chapters = [no for no, _ in win]
    name_to_ent: dict[str, KgEntity] = {}
    stats = {"entities": 0, "relations": 0, "events": 0, "foreshadows": 0,
             "foreshadow_cross_book": 0}

    # —— 1. 实体 ——
    for e in payload.get("entities") or []:
        name = (e.get("canonical_name") or "").strip()
        kind = e.get("kind") or "info"
        ent = _resolve_entity(db, project_id, novel_id, name, kind, chapters[0],
                              aliases=e.get("aliases"), attrs=e.get("attrs"),
                              cache=name_to_ent)
        if ent is not None:
            stats["entities"] += 1

    # —— 2. 清掉本窗旧事件（重跑幂等），再按章写入 ——
    old_ids = list(db.scalars(select(KgEvent.id).where(
        KgEvent.project_id == project_id,
        KgEvent.novel_id == novel_id,
        KgEvent.chapter.in_(chapters),
    )))
    if old_ids:
        db.execute(delete(KgEventLink).where(
            KgEventLink.project_id == project_id,
            (KgEventLink.from_event_id.in_(old_ids))
            | (KgEventLink.to_event_id.in_(old_ids)),
        ))
    db.execute(delete(KgEvent).where(
        KgEvent.project_id == project_id,
        KgEvent.novel_id == novel_id,
        KgEvent.chapter.in_(chapters),
    ))
    # 提及是窗口粒度：同窗重跑先清掉本窗的语料提及（generation_id IS NULL）
    db.execute(delete(KgMention).where(
        KgMention.project_id == project_id,
        KgMention.novel_id == novel_id,
        KgMention.segment_no.in_(chapters),
        KgMention.generation_id.is_(None),
    ))
    db.flush()

    #: 批内 sequence → 落库 event.id（event_links / knowledge 要用）
    seq_to_event: dict[int, KgEvent] = {}
    per_chapter_idx: dict[int, int] = {}
    for ev in payload.get("events") or []:
        seg = ev.get("segment")
        chapter = int(seg) if isinstance(seg, int) and seg in chapters else chapters[0]
        idx = per_chapter_idx.get(chapter, 0)
        per_chapter_idx[chapter] = idx + 1
        subj = _resolve_entity(db, project_id, novel_id, ev.get("subject"), "character",
                              chapter, cache=name_to_ent)
        obj = _resolve_entity(db, project_id, novel_id, ev.get("object"), "info",
                              chapter, cache=name_to_ent)
        loc = _resolve_entity(db, project_id, novel_id, ev.get("location"), "location",
                              chapter, cache=name_to_ent)
        row = KgEvent(
            project_id=project_id, novel_id=novel_id,
            sequence=chapter * _SEQ_STRIDE + idx,
            chapter=chapter, segment_no=chapter,
            event_type=ev.get("event_type") or "incident",
            summary=(ev.get("summary") or "")[:500],
            subject_id=(subj.id if subj else None),
            verb=(ev.get("verb") or None),
            object_id=(obj.id if obj else None),
            location_id=(loc.id if loc else None),
            in_story_date=(ev.get("in_story_date") or None),
            confidence=0.7,
        )
        db.add(row)
        db.flush()
        seq_to_event[int(ev.get("sequence") or 0)] = row
        stats["events"] += 1

    # —— 3. 关系 ——
    seen_rel: set[tuple[int, int, str]] = set()
    for r in payload.get("relations") or []:
        f = _resolve_entity(db, project_id, novel_id, r.get("from"), "character",
                            chapters[0], cache=name_to_ent)
        t = _resolve_entity(db, project_id, novel_id, r.get("to"), "info",
                            chapters[0], cache=name_to_ent)
        rel_type = (r.get("rel_type") or "").strip()
        if not f or not t or not rel_type or f.id == t.id:
            continue
        triple = (f.id, t.id, rel_type)
        # 同窗内先去重：两遍 add 之间若未 flush，唯一约束会直接炸整窗
        if triple in seen_rel:
            continue
        seen_rel.add(triple)
        exist = db.scalar(select(KgRelation).where(
            KgRelation.project_id == project_id,
            KgRelation.from_id == f.id, KgRelation.to_id == t.id,
            KgRelation.rel_type == rel_type,
        ))
        strength = float(r.get("strength") or 0.5)
        if exist is None:
            db.add(KgRelation(
                project_id=project_id, novel_id=novel_id,
                from_id=f.id, to_id=t.id, rel_type=rel_type,
                strength=strength, since_chapter=chapters[0], confidence=0.7,
            ))
            stats["relations"] += 1
        elif exist.since_chapter is None or chapters[0] < exist.since_chapter:
            exist.since_chapter = chapters[0]

    # —— 4. 伏笔（按 name 合并，回收就翻状态）——
    for fo in payload.get("foreshadows") or []:
        name = (fo.get("name") or "").strip()[:200]
        if not name:
            continue
        st = fo.get("status") or "planted"
        planted = fo.get("planted_chapter") or chapters[0]
        planted = planted if planted in chapters else chapters[0]
        row = db.scalar(select(KgForeshadow).where(
            KgForeshadow.project_id == project_id, KgForeshadow.name == name))
        if row is not None and row.novel_id is not None and row.novel_id != novel_id:
            # 同名跨书撞 uq_kg_foreshadow(project_id, name)：**不合并、不改写
            # 别书行**。落隔离区留痕，book-scope 的
            # foreshadows.cross_book_name_collision 可查——静默吞掉等于丢掉
            # 「这本书有同名伏笔」这个事实。
            db.add(KgIngestQuarantine(
                project_id=project_id, run_id=None, segment_no=chapters[0],
                kind="foreshadow", reason="cross_book_name_conflict",
                detail={"name": name, "existing_novel_id": row.novel_id,
                        "incoming_novel_id": novel_id, "status": st,
                        "planted_chapter": planted},
            ))
            stats["foreshadow_cross_book"] += 1
            continue
        if row is None:
            db.add(KgForeshadow(
                project_id=project_id, novel_id=novel_id, name=name, status=st,
                planted_chapter=planted,
                planned_payoff_chapter=fo.get("planned_payoff_chapter"),
                actual_payoff_chapter=(chapters[0] if st == "paid_off" else None),
                description=name, confidence=0.6,
            ))
            stats["foreshadows"] += 1
        elif st == "paid_off" and row.status in ("planted", "developing", "expired"):
            row.status = "paid_off"
            row.actual_payoff_chapter = chapters[0]
        elif st == "developing" and row.status == "planted":
            row.status = "developing"

    # —— 5. 人物状态 / 情报 / 因果链 ——
    for s in payload.get("state_changes") or []:
        ent = _resolve_entity(db, project_id, novel_id, s.get("character"), None,
                              chapters[0], cache=name_to_ent) if s.get("character") else None
        if ent is None:
            continue
        db.add(KgCharacterState(
            project_id=project_id, character_id=ent.id, chapter=chapters[0],
            segment_no=chapters[0], state_type=s.get("state_type") or "mood",
            new_value=(s.get("new_value") or "")[:500],
        ))

    seen_knowledge: set[tuple] = set()
    for k in payload.get("entity_knowledge") or []:
        ent = _resolve_entity(db, project_id, novel_id, k.get("character"), None,
                              chapters[0], cache=name_to_ent)
        if ent is None:
            continue
        known = _resolve_entity(db, project_id, novel_id, k.get("knows"), None,
                                chapters[0], cache=name_to_ent)
        ev = seq_to_event.get(int(k.get("learned_at_sequence") or 0))
        dedupe_key = (ent.id, known.id if known else (k.get("knows") or ""),
                      ev.id if ev else None)
        if dedupe_key in seen_knowledge:
            continue
        seen_knowledge.add(dedupe_key)
        db.add(KgEntityKnowledge(
            project_id=project_id, entity_id=ent.id,
            known_entity_id=(known.id if known else None),
            known_kind=(k.get("known_kind") or "entity"),
            known_text=(None if known else (k.get("knows") or "")[:200]),
            learned_at_event_id=(ev.id if ev else None),
            learned_at_chapter=(ev.chapter if ev else chapters[0]),
            confidence=0.6,
        ))

    links = 0
    for lk in payload.get("event_links") or []:
        a = seq_to_event.get(int(lk.get("from_sequence") or 0))
        b = seq_to_event.get(int(lk.get("to_sequence") or 0))
        if not a or not b or a.id == b.id:
            continue
        db.add(KgEventLink(
            project_id=project_id, from_event_id=a.id, to_event_id=b.id,
            link_type=lk.get("link_type") or "follows", confidence=0.6,
        ))
        links += 1
    stats["links"] = links

    # —— 6. 提及反向索引（窗口粒度一条，供检索/证据回查）——
    for ent in name_to_ent.values():
        db.add(KgMention(
            project_id=project_id, novel_id=novel_id, entity_id=ent.id,
            generation_id=None, segment_no=chapters[0],
            context_window=ent.canonical_name, mention_kind="llm",
        ))

    return stats


def persist_entities_only(db: Session, *, project_id: int, novel_id: int,
                          chapter: int, payload: dict) -> dict:
    """只写实体与关系（定向补抽 faction / power_system 用）。

    **不能复用 ``persist_window``**：它会先删掉本窗事件与提及再重写，而定向
    通道只产出实体、不产出事件——复用等于把主抽写好的剧情时间线清空。
    """
    stats = {"entities": 0, "relations": 0}
    name_to_ent: dict[str, KgEntity] = {}
    for e in payload.get("entities") or []:
        ent = _resolve_entity(db, project_id, novel_id,
                              e.get("canonical_name"), e.get("kind") or "", chapter,
                              aliases=e.get("aliases"), attrs=e.get("attrs"),
                              cache=name_to_ent, upgrade_kind=True)
        if ent is not None:
            stats["entities"] += 1

    seen_rel: set[tuple[int, int, str]] = set()
    for r in payload.get("relations") or []:
        f = _resolve_entity(db, project_id, novel_id, r.get("from"), "", chapter,
                            cache=name_to_ent)
        t = _resolve_entity(db, project_id, novel_id, r.get("to"), "", chapter,
                            cache=name_to_ent)
        rel_type = (r.get("rel_type") or "").strip()
        if not f or not t or not rel_type or f.id == t.id:
            continue
        triple = (f.id, t.id, rel_type)
        if triple in seen_rel:
            continue
        seen_rel.add(triple)
        exist = db.scalar(select(KgRelation).where(
            KgRelation.project_id == project_id,
            KgRelation.from_id == f.id, KgRelation.to_id == t.id,
            KgRelation.rel_type == rel_type,
        ))
        if exist is None:
            db.add(KgRelation(
                project_id=project_id, from_id=f.id, to_id=t.id, rel_type=rel_type,
                strength=float(r.get("strength") or 0.5), since_chapter=chapter,
                confidence=0.7,
            ))
            stats["relations"] += 1
        elif exist.since_chapter is None or chapter < exist.since_chapter:
            exist.since_chapter = chapter
    return stats


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def ingest_novel(*, project_id: int, novel_id: int,
                 chapters_per_window: int = DEFAULT_CHAPTERS_PER_WINDOW,
                 max_chars: int = DEFAULT_MAX_WINDOW_CHARS,
                 concurrency: int = DEFAULT_CONCURRENCY,
                 limit: int | None = None,
                 retry_failed: bool = True,
                 provider=None) -> dict:
    """把一本书的参考语料补建进 KG v3。返回汇总数字。"""
    if provider is None:
        from app.llm.provider import get_distiller_provider
        provider = get_distiller_provider()

    provider = _fit_provider(provider)

    with SessionLocal() as db:
        chapters = load_chapters(db, novel_id)
        if not chapters:
            raise RuntimeError(f"novel {novel_id} 没有 novel_chunks 正文")
        windows = build_windows(chapters, chapters_per_window, max_chars)
        added = register_windows(db, project_id, novel_id, windows)
        todo = pending_windows(db, novel_id, project_id, retry_failed)
        if limit:
            todo = todo[:limit]
        # 出 session 前转成纯数据：后台线程/后续事务不持有 ORM 实例
        todo_rows = [(r.id, r.window_key, r.chapter_start, r.chapter_end) for r in todo]
        by_key = {window_key(novel_id, w): w for w in windows}
        logger.info("novel=%s 章=%s 窗口=%s 新登记=%s 本轮待跑=%s",
                    novel_id, len(chapters), len(windows), added, len(todo_rows))

    total = {"windows": len(todo_rows), "done": 0, "failed": 0,
             "entities": 0, "relations": 0, "events": 0, "foreshadows": 0}
    started = time.time()

    def _extract(row: tuple) -> WindowResult:
        wid, key, start, end = row
        win = by_key.get(key) or []
        texts = [(no, text, no) for no, text in win]
        err: str | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                payload = extract_batch(provider, texts, note=_OUTPUT_NOTE)
                if payload.get(ERROR_FIELD):
                    err = str(payload.get(ERROR_FIELD))[:500]
                    if _should_retry(err) and attempt < _RETRY_ATTEMPTS - 1:
                        _retry_pause(attempt)
                        continue
                    return WindowResult(wid, key, start, end, None, err)
                return WindowResult(wid, key, start, end, payload)
            except Exception as exc:  # noqa: BLE001 —— 单窗失败不拖垮整本
                err = str(exc)[:500]
                if not _should_retry(err) or attempt == _RETRY_ATTEMPTS - 1:
                    return WindowResult(wid, key, start, end, None, err)
                _retry_pause(attempt)
        return WindowResult(wid, key, start, end, None, err)

    # 抽取并发；落库单线程（SQLite 写串行，避免锁竞争与 sequence 冲突）
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(_extract, row): row for row in todo_rows}
        for done_no, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            win = by_key.get(res.window_key) or []
            with SessionLocal() as db:
                row = db.get(KgCorpusWindow, res.window_id)
                if row is None:
                    continue
                row.attempts = (row.attempts or 0) + 1
                if res.error or res.payload is None:
                    row.status = "failed"
                    row.error = res.error or "empty payload"
                    total["failed"] += 1
                else:
                    try:
                        stats = persist_window(db, project_id=project_id,
                                               novel_id=novel_id, win=win,
                                               payload=res.payload)
                        row.status = "done"
                        row.error = None
                        row.n_events = stats["events"]
                        row.n_entities = stats["entities"]
                        for k in ("entities", "relations", "events", "foreshadows"):
                            total[k] += stats.get(k, 0)
                        total["done"] += 1
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        row = db.get(KgCorpusWindow, res.window_id)
                        if row is not None:
                            row.status = "failed"
                            row.error = str(exc)[:500]
                        total["failed"] += 1
                db.commit()
            if done_no % 10 == 0 or done_no == len(todo):
                elapsed = time.time() - started
                rate = done_no / elapsed if elapsed else 0
                logger.info("novel=%s 进度 %s/%s done=%s failed=%s 事件=%s 实体=%s %.1f 窗/分钟",
                            novel_id, done_no, len(todo), total["done"],
                            total["failed"], total["events"], total["entities"],
                            rate * 60)
    total["elapsed_sec"] = round(time.time() - started, 1)
    return total


def retry_failed_split(*, project_id: int, novel_id: int,
                       concurrency: int = DEFAULT_CONCURRENCY,
                       provider=None) -> dict:
    """把 failed 窗口拆成**单章**重跑。

    失败主因是窗口输出顶到模型 4096 token 上限被截断（parse_failed）或瞬时
    429。单章正文约 3–4k 字，输出能稳定落在上限内，所以这是最后一公里的
    兜底通道：只碰 failed 窗口，已 done 的章节不重复花钱。
    """
    if provider is None:
        from app.llm.provider import get_distiller_provider
        provider = get_distiller_provider()
    provider = _fit_provider(provider)

    with SessionLocal() as db:
        chapters = dict(load_chapters(db, novel_id))
        failed = list(db.scalars(
            select(KgCorpusWindow).where(
                KgCorpusWindow.novel_id == novel_id,
                KgCorpusWindow.project_id == project_id,
                KgCorpusWindow.status == "failed",
            ).order_by(KgCorpusWindow.chapter_start)
        ))
        windows = [(r.id, r.window_key, r.chapter_start, r.chapter_end) for r in failed]

    tasks = [(wid, key, ch) for wid, key, a, b in windows
             for ch in range(a, b + 1) if ch in chapters]
    logger.info("拆章重跑：failed 窗口 %s 个 → 单章任务 %s 个", len(windows), len(tasks))
    ok: set[int] = set()
    failed_ch: set[int] = set()
    started = time.time()

    def _one(task: tuple) -> tuple[int, int, dict | None, str | None]:
        wid, key, ch = task
        try:
            payload = extract_batch(provider, [(ch, chapters[ch], ch)])
            if payload.get(ERROR_FIELD):
                return wid, ch, None, str(payload.get(ERROR_FIELD))[:300]
            return wid, ch, payload, None
        except Exception as exc:  # noqa: BLE001
            return wid, ch, None, str(exc)[:300]

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_one, t) for t in tasks]
        for n, fut in enumerate(as_completed(futures), 1):
            wid, ch, payload, err = fut.result()
            if err or payload is None:
                failed_ch.add(ch)
            else:
                with SessionLocal() as db:
                    try:
                        persist_window(db, project_id=project_id, novel_id=novel_id,
                                       win=[(ch, chapters[ch])], payload=payload)
                        db.commit()
                        ok.add(ch)
                    except Exception:  # noqa: BLE001
                        db.rollback()
                        failed_ch.add(ch)
            if n % 20 == 0 or n == len(tasks):
                logger.info("拆章重跑 %s/%s 成功=%s 失败=%s %.1f 章/分钟",
                            n, len(tasks), len(ok), len(failed_ch),
                            n / max(1e-6, time.time() - started) * 60)

    # 窗口状态：整窗的每一章都成功才置 done
    with SessionLocal() as db:
        for wid, key, a, b in windows:
            row = db.get(KgCorpusWindow, wid)
            if row is None:
                continue
            missing = [c for c in range(a, b + 1) if c in chapters and c not in ok]
            row.attempts = (row.attempts or 0) + 1
            if missing:
                row.status = "failed"
                row.error = f"拆章后仍缺章：{missing[:10]}"
            else:
                row.status = "done"
                row.error = None
        db.commit()
    return {"windows": len(windows), "chapters": len(tasks),
            "ok": len(ok), "failed": len(failed_ch),
            "elapsed_sec": round(time.time() - started, 1)}


def fill_gaps(*, project_id: int, novel_id: int,
              concurrency: int = DEFAULT_CONCURRENCY,
              limit: int | None = None,
              only_chapters: list[int] | None = None,
              max_rounds: int = 2,
              provider=None) -> dict:
    """给「有正文、无事件」的章节单章补抽，直到空段收敛。

    为什么需要：3 章窗口里模型常把 3 章的事件全写在 ``segment``=首章上，
    末两章因此一条事件都没有——窗口状态是 done，但时间线出现空章。
    这里按章挑出这些洞，单章重跑；单章输出短、截断率低，是补洞最省的方式。

    ``only_chapters``：只补这些章（跳过其余空章）。给「剔除残缺章、只补点名章」
    这类定向补洞用；不传时行为与加这个参数之前逐字节一致。
    """
    from app.models import NovelChunk

    if provider is None:
        from app.llm.provider import get_distiller_provider
        provider = get_distiller_provider()
    provider = _fit_provider(provider)

    with SessionLocal() as db:
        chapters = dict(load_chapters(db, novel_id))
        corpus = {c for (c,) in db.execute(
            select(func.distinct(NovelChunk.chapter_no)).where(
                NovelChunk.novel_id == novel_id))}
        covered = {c for (c,) in db.execute(
            select(func.distinct(KgEvent.chapter)).where(
                KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None)))}
    gaps = sorted(corpus - covered)
    # non-story 章（作者公告）永远不算空章：口径上直接剔除，补抽也不排队。
    gaps = [c for c in gaps if c not in NON_STORY_CHAPTERS]
    if only_chapters:
        allowed = set(only_chapters)
        gaps = [c for c in gaps if c in allowed]
    if limit:
        gaps = gaps[:limit]
    logger.info("补洞 novel=%s 空章 %s 个", novel_id, len(gaps))

    def _one(ch: int) -> tuple[int, dict | None, str | None]:
        try:
            payload = extract_batch(provider, [(ch, chapters[ch], ch)], note=_FILL_NOTE)
            if payload.get(ERROR_FIELD):
                return ch, None, str(payload.get(ERROR_FIELD))[:300]
            return ch, payload, None
        except Exception as exc:  # noqa: BLE001
            return ch, None, str(exc)[:300]

    total = {"chapters": len(gaps), "ok": 0, "failed": 0, "still_empty": 0,
             "events": 0, "rounds": 0}
    pending = list(gaps)
    empty_set: set[int] = set()
    for rnd in range(1, max_rounds + 1):
        if not pending:
            break
        total["rounds"] = rnd
        logger.info("补洞第 %s 轮：%s 章", rnd, len(pending))
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = [pool.submit(_one, ch) for ch in pending]
            for n, fut in enumerate(as_completed(futures), 1):
                ch, payload, err = fut.result()
                if err or payload is None:
                    total["failed"] += 1
                elif not payload.get("events"):
                    # 模型读完仍认为没有可抽事件：记一笔，下一轮不再重跑
                    total["still_empty"] += 1
                    empty_set.add(ch)
                else:
                    with SessionLocal() as db:
                        try:
                            stats = persist_window(db, project_id=project_id,
                                                   novel_id=novel_id,
                                                   win=[(ch, chapters[ch])],
                                                   payload=payload)
                            # 单章补洞不经过整窗主循环，必须自己回写窗口计数，
                            # 否则 kg_corpus_windows 会一直停在历史快照。
                            _refresh_window_counts(db, novel_id, ch)
                            db.commit()
                            total["ok"] += 1
                            total["events"] += stats["events"]
                        except Exception as exc:  # noqa: BLE001
                            db.rollback()
                            total["failed"] += 1
                            logger.warning("补洞落库失败 %s: %s", ch, exc)
                if n % 20 == 0 or n == len(pending):
                    logger.info("补洞 %s/%s 成功=%s 失败=%s 空=%s 事件=%s",
                                n, len(pending), total["ok"], total["failed"],
                                total["still_empty"], total["events"])
        with SessionLocal() as db:
            covered = {c for (c,) in db.execute(
                select(func.distinct(KgEvent.chapter)).where(
                    KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None)))}
        pending = [c for c in pending if c not in covered and c not in empty_set]
    with SessionLocal() as db:
        covered = {c for (c,) in db.execute(
            select(func.distinct(KgEvent.chapter)).where(
                KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None)))}
        corpus = {c for (c,) in db.execute(
            select(func.distinct(NovelChunk.chapter_no)).where(
                NovelChunk.novel_id == novel_id))}
    remaining = sorted((corpus - covered) - set(NON_STORY_CHAPTERS))
    total["remaining_gaps"] = len(remaining)
    total["remaining_chapters"] = remaining[:200]
    total["non_story_excluded"] = sorted(NON_STORY_CHAPTERS)
    return total


def backfill_targeted(*, project_id: int, novel_id: int,
                      chapters_per_window: int = DEFAULT_CHAPTERS_PER_WINDOW,
                      max_chars: int = DEFAULT_MAX_WINDOW_CHARS,
                      concurrency: int = 4,
                      limit: int | None = None,
                      from_chapter: int = 1,
                      provider=None) -> dict:
    """定向补抽 faction / power_system（主抽这两类几乎为空时用）。

    为什么必须单独跑：主抽的 ``_EXTRACT_SYSTEM`` 把注意力放在人物与事件上，
    书目实测 faction / power_system 恒为 0（门派被判成 character、群被判成
    item/info），世界观清单的「势力」「体系」两栏会整片空白。``extract_targeted``
    是窄 prompt 的第二遍通道，只出这两类，输出短、调用便宜。

    幂等：实体按 ``(canonical_name, novel_id)`` 合并、关系按唯一键去重，重跑只会
    累加 mention_count，不会翻倍。中断后可用 ``from_chapter`` 续跑。
    """
    if provider is None:
        from app.llm.provider import get_distiller_provider
        provider = get_distiller_provider()

    provider = _fit_provider(provider)

    with SessionLocal() as db:
        chapters = load_chapters(db, novel_id)
        if not chapters:
            raise RuntimeError(f"novel {novel_id} 没有 novel_chunks 正文")
        windows = build_windows(chapters, chapters_per_window, max_chars)
    targets = [w for w in windows if w[-1][0] >= from_chapter]
    if limit:
        targets = targets[:limit]
    logger.info("定向补抽 novel=%s 窗口=%s（自第 %s 章起）", novel_id, len(targets), from_chapter)

    total = {"windows": len(targets), "ok": 0, "failed": 0,
             "entities": 0, "relations": 0}
    started = time.time()

    def _one(win: list[tuple[int, str]]) -> tuple[list, dict | None, str | None]:
        text = "\n\n—— 章分隔 ——\n\n".join(f"【第{no}章】\n{t}" for no, t in win)
        err: str | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                payload = extract_targeted(provider, text, win[0][0], win[0][0])
                if payload.get(ERROR_FIELD):
                    err = str(payload.get(ERROR_FIELD))[:300]
                    if _should_retry(err) and attempt < _RETRY_ATTEMPTS - 1:
                        _retry_pause(attempt)
                        continue
                    return win, None, err
                return win, payload, None
            except Exception as exc:  # noqa: BLE001
                err = str(exc)[:300]
                if not _should_retry(err) or attempt == _RETRY_ATTEMPTS - 1:
                    return win, None, err
                _retry_pause(attempt)
        return win, None, err

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(_one, w) for w in targets]
        for n, fut in enumerate(as_completed(futures), 1):
            win, payload, err = fut.result()
            if err or payload is None:
                total["failed"] += 1
            else:
                with SessionLocal() as db:
                    try:
                        stats = persist_entities_only(
                            db, project_id=project_id, novel_id=novel_id,
                            chapter=win[0][0], payload=payload)
                        db.commit()
                        total["entities"] += stats["entities"]
                        total["relations"] += stats["relations"]
                        total["ok"] += 1
                    except Exception as exc:  # noqa: BLE001
                        db.rollback()
                        total["failed"] += 1
                        logger.warning("定向补抽落库失败 %s: %s", win[0][0], exc)
            if n % 20 == 0 or n == len(targets):
                logger.info("定向补抽进度 %s/%s ok=%s failed=%s 实体=%s 关系=%s",
                            n, len(targets), total["ok"], total["failed"],
                            total["entities"], total["relations"])
    total["elapsed_sec"] = round(time.time() - started, 1)
    return total


def coverage(db: Session, novel_id: int, project_id: int | None = None) -> dict:
    """按章统计 v3 覆盖（给验收用）：总章数、有事件的章数、最大事件章、空窗数。"""
    from app.models import NovelChunk
    total_chapters = db.scalar(
        select(func.count(func.distinct(NovelChunk.chapter_no)))
        .where(NovelChunk.novel_id == novel_id)) or 0
    cond = [KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None)]
    if project_id is not None:
        cond.append(KgEvent.project_id == project_id)
    with_events = db.scalar(
        select(func.count(func.distinct(KgEvent.chapter))).where(*cond)) or 0
    max_ch = db.scalar(select(func.max(KgEvent.chapter)).where(*cond))
    n_events = db.scalar(select(func.count(KgEvent.id)).where(*cond))
    return {"total_chapters": total_chapters, "chapters_with_events": with_events,
            "max_event_chapter": max_ch, "events": n_events}


def main() -> None:
    ap = argparse.ArgumentParser(description="参考语料 → KG v3 补建")
    ap.add_argument("--novel-id", type=int, required=True)
    ap.add_argument("--project-id", type=int, required=True)
    ap.add_argument("--chapters-per-window", type=int, default=DEFAULT_CHAPTERS_PER_WINDOW)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_WINDOW_CHARS)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个待办窗口（试跑）")
    ap.add_argument("--no-retry-failed", action="store_true")
    ap.add_argument("--coverage-only", action="store_true")
    ap.add_argument("--retry-failed-split", action="store_true",
                    help="把 failed 窗口拆成单章重跑（最后一公里兜底）")
    ap.add_argument("--fill-gaps", action="store_true",
                    help="给有正文但无事件的章节单章补抽（时间线不留空章）")
    ap.add_argument("--targeted", action="store_true",
                    help="定向补抽 faction / power_system（主抽这两类为空时跑）")
    ap.add_argument("--targeted-from", type=int, default=1,
                    help="定向补抽从第几章开始（断点续跑用）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    from app.db import ensure_schema
    ensure_schema()
    if args.coverage_only:
        with SessionLocal() as db:
            print(json.dumps(coverage(db, args.novel_id, args.project_id),
                             ensure_ascii=False))
        return
    if args.targeted:
        print(json.dumps(backfill_targeted(
            project_id=args.project_id, novel_id=args.novel_id,
            chapters_per_window=args.chapters_per_window,
            max_chars=args.max_chars, concurrency=args.concurrency,
            limit=args.limit, from_chapter=args.targeted_from,
        ), ensure_ascii=False))
        return
    if args.retry_failed_split:
        print(json.dumps(retry_failed_split(
            project_id=args.project_id, novel_id=args.novel_id,
            concurrency=args.concurrency), ensure_ascii=False))
        return
    if args.fill_gaps:
        print(json.dumps(fill_gaps(
            project_id=args.project_id, novel_id=args.novel_id,
            concurrency=args.concurrency, limit=args.limit), ensure_ascii=False))
        return
    print(json.dumps(ingest_novel(
        project_id=args.project_id, novel_id=args.novel_id,
        chapters_per_window=args.chapters_per_window,
        max_chars=args.max_chars, concurrency=args.concurrency,
        limit=args.limit, retry_failed=not args.no_retry_failed,
    ), ensure_ascii=False))


if __name__ == "__main__":
    main()
