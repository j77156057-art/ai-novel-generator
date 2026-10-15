"""v3.0 检索：按当前剧情点动态取 KG 切片（不 dump 全表）。

与 v2.6 get_reminders（kg_nodes top-K 全表扫描）的关系：
- get_reminders 保留（旧表兜底）
- slice_for_prompt（本模块）：基于 v3.0 表（kg_mentions 反向索引 + kg_events +
  kg_character_state + kg_foreshadows + kg_entity_knowledge），按**当前段号**取：
    1. 最近 N 段涉及的实体（mention 反查，带 context_window 原文片段）
    2. 当前角色的最新状态（location/injury/goal/alive）
    3. active 伏笔（planted/developing，未超期优先）
    4. 当前角色此刻知道什么（entity_knowledge）——防预知外挂的 prompt 侧
切片有界（budget），prompt 装得下。

8 个产品功能里，检索是喂 prompt 的地基；其余 API 是读侧展示（关系曲线/节奏等）。
"""
from __future__ import annotations

from sqlalchemy import select, func

from app.models import (
    KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow, KgMention,
)


def slice_for_prompt(
    db,
    project_id: int,
    current_segment_no: int,
    *,
    window: int = 5,
    max_entities: int = 20,
    active_characters: list[str] | None = None,
) -> dict:
    """按当前剧情点取 KG 切片喂 prompt。

    window: 回溯最近 N 段的 mention
    max_entities: 实体清单上限（prompt 预算）
    active_characters: 当前段涉及的角色名（用于取状态和情报）；None 则用本段全部

    返回：
    {
      "recent_entities": [{"name","kind","context","mention_count"}],
      "character_states": [{"character","state_type","new_value"}],
      "active_foreshadows": [{"name","status","planted_chapter","planned_payoff_chapter"}],
      "known_facts": [{"character","knows"}],   # 当前角色知道什么
      "recent_events": [{"summary","event_type","in_story_date"}],
    }
    """
    lo = max(1, current_segment_no - window + 1)

    # —— 1. 最近 N 段涉及的实体（mention 反向索引）——
    recent_mentions = db.scalars(
        select(KgMention).where(
            KgMention.project_id == project_id,
            KgMention.segment_no >= lo,
            KgMention.segment_no <= current_segment_no,
        ).order_by(KgMention.segment_no.desc())
    ).all()
    # 按实体聚合：取最近一次 mention 的 context + 累计 mention 数
    ent_agg: dict[int, dict] = {}
    for m in recent_mentions:
        agg = ent_agg.setdefault(m.entity_id, {"count": 0, "context": ""})
        agg["count"] += 1
        if not agg["context"] and m.context_window:
            agg["context"] = m.context_window  # 最近一次的原文片段
    # 按 mention 频次取 top，补 entity 信息
    top_ent_ids = sorted(ent_agg, key=lambda e: ent_agg[e]["count"], reverse=True)[:max_entities]
    recent_entities = []
    for eid in top_ent_ids:
        ent = db.get(KgEntity, eid)
        if ent is None:
            continue
        recent_entities.append({
            "name": ent.canonical_name,
            "kind": ent.kind,
            "context": ent_agg[eid]["context"],
            "mention_count": ent_agg[eid]["count"],
        })

    # —— 2. 当前角色最新状态（每角色每 state_type 取最新一条）——
    char_states: list[dict] = []
    if active_characters:
        for name in active_characters:
            ent = db.scalar(
                select(KgEntity).where(
                    KgEntity.project_id == project_id, KgEntity.canonical_name == name
                )
            )
            if ent is None:
                continue
            latest: dict[str, KgCharacterState] = {}
            for st in db.scalars(
                select(KgCharacterState).where(
                    KgCharacterState.project_id == project_id,
                    KgCharacterState.character_id == ent.id,
                ).order_by(KgCharacterState.segment_no)
            ):
                latest[st.state_type] = st
            for st_type, st in latest.items():
                char_states.append({
                    "character": ent.canonical_name,
                    "state_type": st_type,
                    "new_value": st.new_value,
                })

    # —— 3. active 伏笔（planted/developing）——
    active_foreshadows = []
    for fs in db.scalars(
        select(KgForeshadow).where(
            KgForeshadow.project_id == project_id,
            KgForeshadow.status.in_(["planted", "developing"]),
        ).order_by(KgForeshadow.planted_chapter)
    ):
        active_foreshadows.append({
            "name": fs.name,
            "status": fs.status,
            "planted_chapter": fs.planted_chapter,
            "planned_payoff_chapter": fs.planned_payoff_chapter,
        })

    # —— 4. 当前角色此刻知道什么（entity_knowledge，三类 known_kind 全消费）——
    # entity → 实体名；event → 「事件：简述」（有 id 用库内 summary，降级用 known_text）；
    # secret → 「秘密：文本」。kind 缺失的老行按 id 先实体后事件容错解析。
    known_facts: list[dict] = []
    if active_characters:
        for name in active_characters:
            ent = db.scalar(
                select(KgEntity).where(
                    KgEntity.project_id == project_id, KgEntity.canonical_name == name
                )
            )
            if ent is None:
                continue
            knows: list[str] = []
            for k in db.scalars(
                select(KgEntityKnowledge).where(
                    KgEntityKnowledge.project_id == project_id,
                    KgEntityKnowledge.entity_id == ent.id,
                )
            ):
                label = _knowledge_label(db, k)
                if label and label not in knows:
                    knows.append(label)
                if len(knows) >= 8:  # prompt 预算：单角色情报上限
                    break
            if knows:
                known_facts.append({"character": ent.canonical_name, "knows": knows})

    # —— 5. 最近 N 段的事件（in_story_date 喂物理时间校验 prompt）——
    recent_events = []
    for ev in db.scalars(
        select(KgEvent).where(
            KgEvent.project_id == project_id,
            KgEvent.segment_no >= lo,
            KgEvent.segment_no <= current_segment_no,
        ).order_by(KgEvent.segment_no.desc())
    ):
        recent_events.append({
            "summary": ev.summary,
            "event_type": ev.event_type,
            "in_story_date": ev.in_story_date,
        })

    return {
        "recent_entities": recent_entities,
        "character_states": char_states,
        "active_foreshadows": active_foreshadows,
        "known_facts": known_facts,
        "recent_events": recent_events,
    }


def _knowledge_label(db, k) -> str | None:
    """一条 entity_knowledge 行 → 给 prompt 的事实标签（三类 known_kind）。

    返回 None 的行不渲染（三类都解析失败的脏行）。
    """
    kind = (k.known_kind or "").strip()
    if not kind:
        # 老行容错：kind 缺失时按 id 先实体后事件解析（老库行为兼容）
        known_ent = db.get(KgEntity, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ent is not None:
            return known_ent.canonical_name
        known_ev = db.get(KgEvent, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ev is not None:
            return f"事件：{(known_ev.summary or '')[:80]}"
        return (k.known_text or "").strip() or None
    if kind == "entity":
        known_ent = db.get(KgEntity, k.known_entity_id) if k.known_entity_id is not None else None
        return known_ent.canonical_name if known_ent else None
    if kind == "event":
        known_ev = db.get(KgEvent, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ev is not None:
            return f"事件：{(known_ev.summary or '')[:80]}"
        return f"事件：{(k.known_text or '').strip()[:80]}" if k.known_text else None
    if kind == "secret":
        return f"秘密：{(k.known_text or '').strip()[:80]}" if k.known_text else None
    return None


def format_slice_for_prompt(sl: dict) -> str:
    """把 slice 渲染成一段 prompt 文本（有血有肉的事实，带原文片段）。"""
    lines: list[str] = []
    if sl.get("recent_entities"):
        lines.append("【近期出场实体】")
        for e in sl["recent_entities"]:
            ctx = f"（{e['context']}）" if e.get("context") else ""
            lines.append(f"- {e['name']}（{e['kind']}）{ctx}")
    if sl.get("character_states"):
        lines.append("【角色当前状态】")
        for s in sl["character_states"]:
            lines.append(f"- {s['character']}·{s['state_type']}: {s['new_value']}")
    if sl.get("active_foreshadows"):
        lines.append("【未回收伏笔】")
        for f in sl["active_foreshadows"]:
            due = f"，计划第{f['planned_payoff_chapter']}章回收" if f.get("planned_payoff_chapter") else ""
            lines.append(f"- {f['name']}（{f['status']}{due}）")
    if sl.get("known_facts"):
        lines.append("【角色已知情报】")
        for k in sl["known_facts"]:
            lines.append(f"- {k['character']}知道: {', '.join(k['knows'])}")
    if sl.get("recent_events"):
        lines.append("【近期事件】")
        for ev in sl["recent_events"][:10]:
            date = f"[{ev['in_story_date']}]" if ev.get("in_story_date") else ""
            lines.append(f"- {date}{ev['summary']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 8 个产品功能的读侧 API（关系演变曲线 / 节奏分析 / 人物情报追踪等）
# --------------------------------------------------------------------------- #

def relation_curve(db, project_id: int, from_name: str, to_name: str) -> list[dict]:
    """关系演变曲线：两角色 rel 的 strength 历史（kg_relation_evolution）。"""
    from app.models import KgRelation, KgRelationEvolution

    a = db.scalar(select(KgEntity).where(
        KgEntity.project_id == project_id, KgEntity.canonical_name == from_name))
    b = db.scalar(select(KgEntity).where(
        KgEntity.project_id == project_id, KgEntity.canonical_name == to_name))
    if a is None or b is None:
        return []
    rels = db.scalars(select(KgRelation).where(
        KgRelation.project_id == project_id,
        KgRelation.from_id.in_([a.id, b.id]),
        KgRelation.to_id.in_([a.id, b.id]),
    )).all()
    curve: list[dict] = []
    for rel in rels:
        for ev in db.scalars(
            select(KgRelationEvolution).where(
                KgRelationEvolution.edge_id == rel.id
            ).order_by(KgRelationEvolution.chapter)
        ):
            curve.append({
                "rel_type": rel.rel_type,
                "chapter": ev.chapter,
                "strength": ev.strength,
                "note": ev.note,
            })
    curve.sort(key=lambda x: (x["chapter"] or 0))
    return curve


def can_character_know(db, project_id: int, character_name: str, fact_name: str) -> bool:
    """人物情报追踪：character 此刻是否知道 fact（已知 → True）。

    fact 三种形态（known_kind 三类全消费）：
    - 实体名（"天津港"）→ 匹配 known_kind=entity 行
    - "事件：xxx" / 事件描述 → 匹配 event 行（id→summary 或 known_text）
    - "秘密：xxx" / 秘密文本 → 匹配 secret 行的 known_text（规范化包含）
    """
    from app.kg.knowledge_match import normalize_fact, event_summary_match

    ent = db.scalar(select(KgEntity).where(
        KgEntity.project_id == project_id, KgEntity.canonical_name == character_name))
    if ent is None:
        return False
    fact = (fact_name or "").strip()
    if not fact:
        return False
    # 前缀归一：调用方可以直接传 slice 渲染出的 "事件：xxx" / "秘密：xxx" 标签
    kind_hint = ""
    body = fact
    if fact.startswith("事件："):
        kind_hint, body = "event", fact[3:].strip()
    elif fact.startswith("秘密："):
        kind_hint, body = "secret", fact[3:].strip()

    q = select(KgEntityKnowledge).where(
        KgEntityKnowledge.project_id == project_id,
        KgEntityKnowledge.entity_id == ent.id,
    )
    if kind_hint:
        q = q.where(KgEntityKnowledge.known_kind == kind_hint)
    for k in db.scalars(q):
        label = _knowledge_label(db, k)
        if label is not None and label == fact:
            return True
        # 去掉 "事件：" / "秘密：" 前缀后的文本比对
        core = label
        if label and "：" in label and label.split("：", 1)[0] in ("事件", "秘密"):
            core = label.split("：", 1)[1]
        if core is not None and core == body:
            return True
        # secret 行：规范化文本包含比对（标点/空白变体）
        if k.known_kind == "secret" and k.known_text:
            ntext = normalize_fact(k.known_text)
            nbody = normalize_fact(body)
            if nbody and ntext and (nbody in ntext or ntext in nbody):
                return True
        # event 行：事件描述 vs 库内 summary 的保守匹配
        if k.known_kind == "event":
            summary = ""
            if k.known_entity_id is not None:
                ev = db.get(KgEvent, k.known_entity_id)
                summary = (ev.summary or "") if ev else ""
            target = summary or (k.known_text or "")
            if target and event_summary_match(body, target):
                return True
    return False


def rhythm_analysis(db, project_id: int, window: int = 10) -> dict:
    """节奏分析：每实体在最近 N 段的 mention 分布（谁在唱主角）。"""
    segs = db.scalars(
        select(KgMention.segment_no).where(
            KgMention.project_id == project_id
        ).distinct().order_by(KgMention.segment_no.desc()).limit(window)
    ).all()
    if not segs:
        return {"segments": [], "top_entities": []}
    lo = min(segs)
    rows = db.execute(
        select(KgEntity.canonical_name, KgEntity.kind, func.count(KgMention.id).label("n"))
        .join(KgMention, KgMention.entity_id == KgEntity.id)
        .where(KgMention.project_id == project_id, KgMention.segment_no >= lo)
        .group_by(KgEntity.id)
        .order_by(func.count(KgMention.id).desc())
        .limit(20)
    ).all()
    return {
        "segments": sorted(segs),
        "top_entities": [{"name": r[0], "kind": r[1], "mentions": r[2]} for r in rows],
    }
