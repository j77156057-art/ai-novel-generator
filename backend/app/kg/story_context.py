"""KG v3 的三类消费视图：世界观清单 / 剧情时间线 / 按章截取。

## 为什么单独一层

`retrieval_v3.slice_for_prompt` 服务的是**续写时注入**（按段号取最近 N 段）；
本模块服务的是**人/外部工具消费**：把整本书的 KG 摊平成两个可直接读的清单
（世界观、时间线），以及一个「写到第 N 章时，此前发生了什么」的截取接口。

三个函数都是纯读、无 LLM：
- ``worldview``：按 kind 分组（势力/地点/体系/资源/规则），每条带证据章号；
- ``timeline``：按章排序的事件序列，每条给 who/where/what/result；
- ``story_context``：传入 ``up_to_chapter=N``，返回此前事件、未回收伏笔、
  关键人物当前状态。

时间线条目不发明字段：``who/where`` 来自实体表，``what`` 来自 verb+object，
``result`` 直接取抽取时的事件 summary（模型写的 SVO 描述，含结果）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    KgCharacterState, KgEntity, KgEvent, KgForeshadow,
)

#: 世界观视图要覆盖的 kind（info 不展示——它是解析失败的兜底类）
WORLDVIEW_KINDS = ("faction", "location", "power_system", "item", "worldrule")

#: 抽取时的占位名。模型在「不知道叫什么」时会写这些词，若不过滤会出现
#: 「未知」这种提及 74 次、证据跨 900 章的世界观条目，把真条目挤到后面。
_PLACEHOLDER_NAMES = {
    "未知", "未知地点", "未知人物", "未知势力", "未知物品", "不详", "不明",
    "无", "没有", "未提及", "未出现", "某人", "某地", "某物", "其他", "其它",
    "unknown", "none", "null", "n/a", "na", "-",
}


def _is_placeholder(name: str | None) -> bool:
    return not name or name.strip().lower() in _PLACEHOLDER_NAMES


def _clean_name(name: str | None) -> str:
    return "" if _is_placeholder(name) else (name or "").strip()


#: 地点/物品里混进大量一次性名词（「门板」「城门」「东校门」），按提及次数
#: 设门槛；势力/体系/规则是贯穿全书的概念，不设门槛。
_MIN_MENTION = {"faction": 1, "power_system": 1, "worldrule": 1,
                "location": 2, "item": 2}

#: 模型经常把「宗门/门派」判成地点（如 无极魔宗、仙农宗、空云派）。这些地名
#: 在有事件证据时同时列进势力清单，但保留 ``kind_origin`` 标记原判类，
#: 不掩盖「主抽把势力判成地点」这一质量问题。
_FACTION_SUFFIX = ("宗", "派", "教", "帮", "盟", "阁", "殿", "宫", "书院", "组织", "商行")
_GATE_WORDS = ("校门", "大门", "门口", "城门", "正门", "侧门", "后门", "前门", "山门", "石门")


def _faction_like(name: str) -> bool:
    if len(name) < 3 or name.endswith(_GATE_WORDS):
        return False
    return name.endswith(_FACTION_SUFFIX)


def _name_map(db: Session, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {i: n for i, n in db.execute(
        select(KgEntity.id, KgEntity.canonical_name).where(KgEntity.id.in_(ids))
    )}


def _event_dict(ev: KgEvent, names: dict[int, str]) -> dict:
    who = _clean_name(names.get(ev.subject_id or 0))
    where = _clean_name(names.get(ev.location_id or 0))
    obj = _clean_name(names.get(ev.object_id or 0))
    what = "".join(x for x in (ev.verb or "", obj) if x) or (ev.summary or "")
    return {
        "chapter": ev.chapter,
        "event_type": ev.event_type,
        "who": who,
        "where": where,
        "what": what,
        "result": ev.summary or "",
        "actors": [x for x in (who, obj) if x],
        "in_story_date": ev.in_story_date,
    }


def _events_for_entities(db: Session, novel_id: int, ids: list[int]) -> dict[int, list[dict]]:
    """每个实体最多 3 条证据事件（按章升序）。批量查，避免 N+1。"""
    out: dict[int, list[dict]] = {i: [] for i in ids}
    if not ids:
        return out
    # SQLite 变量上限：分批查
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        rows = db.scalars(
            select(KgEvent).where(
                KgEvent.novel_id == novel_id,
                KgEvent.is_current.is_(True),  # 同章旧稿事件只留档，不当证据
                (KgEvent.subject_id.in_(chunk))
                | (KgEvent.object_id.in_(chunk))
                | (KgEvent.location_id.in_(chunk)),
            ).order_by(KgEvent.chapter, KgEvent.sequence)
        ).all()
        names = _name_map(db, {r.subject_id for r in rows} | {r.object_id for r in rows}
                          | {r.location_id for r in rows})
        for ev in rows:
            for eid in {ev.subject_id, ev.object_id, ev.location_id}:
                if eid in out and len(out[eid]) < 3:
                    out[eid].append({
                        "chapter": ev.chapter,
                        "event_type": ev.event_type,
                        "summary": (ev.summary or "")[:160],
                    })
    return out


def worldview(db: Session, project_id: int, novel_id: int) -> dict:
    """世界观清单：势力/组织、地点、体系与境界、资源与规则，各带证据章号。"""
    ents = db.scalars(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.novel_id == novel_id,
            KgEntity.kind.in_(WORLDVIEW_KINDS),
        ).order_by(KgEntity.mention_count.desc(), KgEntity.first_chapter)
    ).all()
    ents = [e for e in ents if not _is_placeholder(e.canonical_name)]
    ents = [e for e in ents
            if (e.mention_count or 0) >= _MIN_MENTION.get(e.kind, 2)]
    evidence = _events_for_entities(db, novel_id, [e.id for e in ents])
    groups: dict[str, list[dict]] = {k: [] for k in WORLDVIEW_KINDS}
    for e in ents:
        ev = evidence.get(e.id, [])
        if not ev and e.first_chapter is not None:
            # 没有事件直接引用（如只出现在关系边上的组织）时，退回首现章：
            # 「带证据章号」不能因为事件稀疏就变成空数组。
            ev = [{"chapter": e.first_chapter, "event_type": "first_appearance",
                   "summary": f"首次出现于第 {e.first_chapter} 章"}]
        groups[e.kind].append({
            "name": e.canonical_name,
            "aliases": [a for a in (e.aliases or []) if not _is_placeholder(a)],
            "first_chapter": e.first_chapter,
            "last_chapter": e.last_chapter,
            "mention_count": e.mention_count,
            "kind_origin": e.kind,
            "attrs": dict(e.attrs or {}),
            "evidence": ev,
        })
    # 被判成地点、但名字是「XX宗/XX派」的实体，同时列进势力清单（带原判类）
    for loc in groups["location"]:
        if _faction_like(loc["name"]):
            groups["faction"].append(dict(loc))
    groups["faction"].sort(key=lambda x: (-(x["mention_count"] or 0), x["first_chapter"] or 0))
    return {
        "novel_id": novel_id,
        "factions": groups["faction"],
        "locations": groups["location"],
        "power_systems": groups["power_system"],
        "resources": groups["item"],
        "rules": groups["worldrule"],
        "counts": {k: len(v) for k, v in groups.items()},
    }


def _chapter_gaps(db: Session, novel_id: int, from_chapter: int, to_chapter: int) -> list[list[int]]:
    """事件覆盖不到、但语料里存在的章节的连续空段。"""
    from app.models import NovelChunk
    corpus = {c for (c,) in db.execute(
        select(func.distinct(NovelChunk.chapter_no)).where(
            NovelChunk.novel_id == novel_id,
            NovelChunk.chapter_no >= from_chapter,
            NovelChunk.chapter_no <= to_chapter,
        )
    )}
    covered = {c for (c,) in db.execute(
        select(func.distinct(KgEvent.chapter)).where(
            KgEvent.novel_id == novel_id,
            KgEvent.chapter >= from_chapter,
            KgEvent.chapter <= to_chapter,
        )
    )}
    gaps: list[list[int]] = []
    start = None
    prev = None
    for ch in sorted(corpus - covered):
        if start is None:
            start = prev = ch
        elif prev is not None and ch == prev + 1:
            prev = ch
        else:
            gaps.append([start, prev])
            start = prev = ch
    if start is not None:
        gaps.append([start, prev])
    return gaps


def timeline(db: Session, project_id: int, novel_id: int,
             from_chapter: int = 1, to_chapter: int | None = None,
             offset: int = 0, limit: int = 500,
             include_gaps: bool = False) -> dict:
    """按章排序的事件序列。``to_chapter=None`` 时取到本书最大事件章。"""
    cond = [KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None),
            KgEvent.chapter >= from_chapter, KgEvent.is_current.is_(True)]
    if to_chapter is not None:
        cond.append(KgEvent.chapter <= to_chapter)
    total = db.scalar(select(func.count(KgEvent.id)).where(*cond)) or 0
    rows = db.scalars(
        select(KgEvent).where(*cond)
        .order_by(KgEvent.chapter, KgEvent.sequence)
        .offset(offset).limit(limit)
    ).all()
    names = _name_map(db, {r.subject_id for r in rows} | {r.object_id for r in rows}
                      | {r.location_id for r in rows})
    out = {
        "novel_id": novel_id,
        "from_chapter": from_chapter,
        "to_chapter": to_chapter,
        "offset": offset,
        "limit": limit,
        "total": total,
        "events": [_event_dict(ev, names) for ev in rows],
    }
    if include_gaps:
        out["gaps"] = _chapter_gaps(db, novel_id, from_chapter,
                                    to_chapter or (db.scalar(
                                        select(func.max(KgEvent.chapter)).where(
                                            KgEvent.novel_id == novel_id)) or from_chapter))
    return out


def story_context(db: Session, project_id: int, novel_id: int, up_to_chapter: int,
                  recent_events: int = 30, top_characters: int = 20) -> dict:
    """「当前写到第 N 章」时的续写上下文。

    返回：
    - ``coverage``：此前事件数 / 覆盖章数 / 空段（用于判断 KG 是否够用）；
    - ``recent_events``：N 章前最近的一批事件（时间线尾部）；
    - ``open_foreshadows``：N 章前已埋、尚未回收的伏笔（按埋设章升序）；
    - ``characters``：关键人物在 N 章时的状态（存活/所在地/目标/情绪）与最近动向。
    """
    base = [KgEvent.novel_id == novel_id, KgEvent.chapter.isnot(None),
            KgEvent.chapter <= up_to_chapter, KgEvent.is_current.is_(True)]
    n_events = db.scalar(select(func.count(KgEvent.id)).where(*base)) or 0
    n_chapters = db.scalar(select(func.count(func.distinct(KgEvent.chapter))).where(*base)) or 0

    rows = db.scalars(
        select(KgEvent).where(*base)
        .order_by(KgEvent.chapter.desc(), KgEvent.sequence.desc())
        .limit(max(1, recent_events))
    ).all()
    names = _name_map(db, {r.subject_id for r in rows} | {r.object_id for r in rows}
                      | {r.location_id for r in rows})
    recent = [_event_dict(ev, names) for ev in reversed(rows)]

    fs_rows = db.scalars(
        select(KgForeshadow).where(
            KgForeshadow.project_id == project_id,
            KgForeshadow.status.in_(("planted", "developing", "expired")),
            KgForeshadow.planted_chapter.isnot(None),
            KgForeshadow.planted_chapter <= up_to_chapter,
        ).order_by(KgForeshadow.planted_chapter)
    ).all()
    open_fs = [{
        "name": f.name,
        "status": f.status,
        "planted_chapter": f.planted_chapter,
        "planned_payoff_chapter": f.planned_payoff_chapter,
        "age_chapters": up_to_chapter - (f.planted_chapter or up_to_chapter),
        "description": f.description,
    } for f in fs_rows]

    # 关键人物：本书 mention_count 最高的 N 个 character
    chars = db.scalars(
        select(KgEntity).where(
            KgEntity.project_id == project_id,
            KgEntity.novel_id == novel_id,
            KgEntity.kind == "character",
        ).order_by(KgEntity.mention_count.desc()).limit(max(1, top_characters))
    ).all()
    char_ids = [c.id for c in chars]
    states: dict[tuple[int, str], KgCharacterState] = {}
    if char_ids:
        for st in db.scalars(
            select(KgCharacterState).where(
                KgCharacterState.project_id == project_id,
                KgCharacterState.character_id.in_(char_ids),
                KgCharacterState.chapter.isnot(None),
                KgCharacterState.chapter <= up_to_chapter,
                KgCharacterState.is_current.is_(True),  # 旧版本状态不再渲染
            ).order_by(KgCharacterState.chapter)
        ):
            states[(st.character_id, st.state_type)] = st  # 后写覆盖 = 最新
    recent_by_char: dict[int, list[dict]] = {}
    if char_ids:
        ev_rows = db.scalars(
            select(KgEvent).where(
                KgEvent.novel_id == novel_id,
                KgEvent.chapter <= up_to_chapter,
                KgEvent.is_current.is_(True),  # 同章旧稿事件只留档
                (KgEvent.subject_id.in_(char_ids)) | (KgEvent.object_id.in_(char_ids)),
            ).order_by(KgEvent.chapter.desc()).limit(400)
        ).all()
        ev_names = _name_map(db, {r.subject_id for r in ev_rows} | {r.object_id for r in ev_rows}
                             | {r.location_id for r in ev_rows})
        for ev in ev_rows:
            for cid in {ev.subject_id, ev.object_id}:
                if cid and len(recent_by_char.setdefault(cid, [])) < 2:
                    recent_by_char[cid].append({
                        "chapter": ev.chapter,
                        "summary": (ev.summary or "")[:120],
                        "where": ev_names.get(ev.location_id or 0, ""),
                    })
    characters = []
    for c in chars:
        loc = states.get((c.id, "location"))
        alive = states.get((c.id, "alive"))
        goal = states.get((c.id, "goal"))
        mood = states.get((c.id, "mood"))
        char_recent = recent_by_char.get(c.id, [])
        loc_value = loc.new_value if loc else None
        loc_source = "state_change" if loc_value else None
        if not loc_value and char_recent and char_recent[0].get("where"):
            # state_changes 在本书很稀疏（多数章没有显式「谁在哪」），
            # 退而用该人物最后一条事件的 where，否则「现在在哪」永远为空。
            loc_value, loc_source = char_recent[0]["where"], "latest_event"
        characters.append({
            "name": c.canonical_name,
            "alive": (alive.new_value if alive else None),
            "location": loc_value,
            "location_source": loc_source,
            "goal": (goal.new_value if goal else None),
            "mood": (mood.new_value if mood else None),
            "last_seen_chapter": char_recent[0]["chapter"] if char_recent else None,
            "last_chapter": c.last_chapter,
            "mention_count": c.mention_count,
            "recent": char_recent,
        })

    return {
        "novel_id": novel_id,
        "up_to_chapter": up_to_chapter,
        "coverage": {
            "events_before": n_events,
            "chapters_with_events": n_chapters,
            "coverage_ratio": round(n_chapters / up_to_chapter, 4) if up_to_chapter else 0,
            "gaps": _chapter_gaps(db, novel_id, 1, up_to_chapter)[:50],
        },
        "recent_events": recent,
        "open_foreshadows": open_fs[:50],
        "characters": characters,
    }
