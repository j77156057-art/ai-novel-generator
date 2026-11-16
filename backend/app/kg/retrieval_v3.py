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

from app.kg.entity_chapters import format_chapter_range
from app.kg.book_scope import book_for_project, prompt_scope_book_ids, scope_sql_values
from app.kg.entity_lookup import resolve_entity_by_name
from app.models import (
    KgCharacterState, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow, KgMention,
    KgRelation, Project,
)

#: 双向关系类型；其余按 from→to 单向渲染（亲属类方向靠 rel_type 本身表达）。
_SYMMETRIC_REL_TYPES = frozenset({"ally", "enemy", "sibling", "romantic", "friend", "rival"})


def slice_for_prompt(
    db,
    project_id: int,
    current_segment_no: int,
    *,
    window: int = 5,
    max_entities: int = 20,
    max_relations: int = 10,
    active_characters: list[str] | None = None,
) -> dict:
    """按当前剧情点取 KG 切片喂 prompt。

    window: 回溯最近 N 段的 mention
    max_entities: 实体清单上限（prompt 预算）
    max_relations: 关系清单上限（关系只列当前段实体之间的边，不写人物小传）
    active_characters: 当前段涉及的角色名（用于取状态和情报）；None 则用本段全部

    书维度：只取「本书 + 项目级 NULL」（见 `book_scope.prompt_scope_book_ids`）。
    参考书（`project.reference_novel_ids`）抽出来的实体/事件/关系/情报不进
    prompt —— 否则同号段的别书内容会被当成本书「已发生的事实」。

    返回：
    {
      "recent_entities": [{"name","kind","context","mention_count"}],
      "character_states": [{"character","state_type","new_value"}],
      "active_foreshadows": [{"name","status","planted_chapter","planned_payoff_chapter"}],
      "known_facts": [{"character","knows"}],   # 当前角色知道什么
      "recent_events": [{"summary","event_type","in_story_date"}],
      "relationships": [{"from","to","rel_type"}],  # 当前段实体内已发生的关系
    }
    """
    lo = max(1, current_segment_no - window + 1)
    # 书维度白名单：本书 + 项目级 NULL。参考书（reference_novel_ids）抽出来的
    # 实体/事件/情报一律不进 prompt —— 那是给检索线用的语料，不是本书已发生的事。
    # 项目对象取不到时按「只认项目级」处理：宁少给，不串书。
    project = db.get(Project, project_id)
    own_book = book_for_project(project) if project is not None else None
    scope = prompt_scope_book_ids(project) if project is not None else (None,)
    scope_sql = scope_sql_values(scope)

    def _in_scope(novel_id) -> bool:
        """该行的事实是否属于本书可见范围（NULL = 项目级通配，始终可见）。"""
        return novel_id is None or novel_id in scope

    # —— 1. 最近 N 段涉及的实体（mention 反向索引）——
    # 书维度必须在**排序前**就卡掉，不能等取完 top-K 再过滤：参考书的 mention
    # 频次远高于本书（同号段撞号），先取 top-K 会把预算占满，本书实体一个都进不来。
    # 两个维度都要卡：mention 行的 novel_id（这条提及来自哪本书）+ 实体自身的
    # novel_id（这个实体属于哪本书）。老数据前者可能是 NULL，后者才是准的。
    recent_mentions = db.scalars(
        select(KgMention).join(
            KgEntity, KgEntity.id == KgMention.entity_id,
        ).where(
            KgMention.project_id == project_id,
            KgMention.segment_no >= lo,
            KgMention.segment_no <= current_segment_no,
            func.coalesce(KgMention.novel_id, -1).in_(scope_sql),
            func.coalesce(KgEntity.novel_id, -1).in_(scope_sql),
            # 同章旧版本提及只留档，不进切片（否则重写章会新旧两版同时出现）
            KgMention.is_current.is_(True),
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
    #: 关系取边的锚点：只认本段实体，顺带记名字/书，供书维度过滤用。
    anchor_ids: set[int] = set(top_ent_ids)
    ent_names: dict[int, str] = {}
    ent_books: dict[int, int | None] = {}
    for eid in top_ent_ids:
        ent = db.get(KgEntity, eid)
        # 第二道门：提及行的书维度可能缺失，实体本身的书才是准的。
        if ent is None or not _in_scope(ent.novel_id):
            continue
        ent_names[eid] = ent.canonical_name
        ent_books[eid] = ent.novel_id
        recent_entities.append({
            "name": ent.canonical_name,
            "kind": ent.kind,
            "context": ent_agg[eid]["context"],
            "mention_count": ent_agg[eid]["count"],
            # 章号只给「确定区间」或「章号未知」两种字符串，不给 null：
            # 下游拼 prompt / 前端渲染都不需要再各自决定「空值算什么」。
            "chapters": format_chapter_range(ent.first_chapter, ent.last_chapter),
        })

    # —— 2. 当前角色最新状态（每角色每 state_type 取最新一条）——
    char_states: list[dict] = []
    if active_characters:
        for name in active_characters:
            # 必须限定 kind：同一 project 里 item 与 character 可以同名
            # （「长刀」「黄金」这类），不带 kind 时 db.scalar 在两者并存后
            # 会直接抛 MultipleResultsFound；本块语义是「角色的最新状态」，
            # 只认 character。
            ent = resolve_entity_by_name(db, project_id, name, kind="character",
                                         novel_id=own_book)
            if ent is None or not _in_scope(ent.novel_id):
                continue
            anchor_ids.add(ent.id)
            ent_names[ent.id] = ent.canonical_name
            ent_books[ent.id] = ent.novel_id
            latest: dict[str, KgCharacterState] = {}
            for st in db.scalars(
                select(KgCharacterState).where(
                    KgCharacterState.project_id == project_id,
                    KgCharacterState.character_id == ent.id,
                    # 只取当前版本状态：旧版本保留可回溯，但不再参与渲染
                    KgCharacterState.is_current.is_(True),
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
    # kg_foreshadows 现在有书维度：按「本书 + 项目级 NULL」过滤，跨书伏笔
    # （同名跨书撞键的那条）不会因为同名被当成本书伏笔注入。
    # 另加一道进度门：原创项目（无续写/来源书）写到第 N 段时，不可能在自己的
    # 正文里埋下第 N+ 章才出现的伏笔；planted_chapter 晚于当前进度的行，只可能
    # 是参考书语料抽出来的，注入就是把别书的后续剧透塞进本章。有续写书时不加
    # 这道门：那本书的旧章节伏笔对续写是有效上下文。
    fs_q = select(KgForeshadow).where(
        KgForeshadow.project_id == project_id,
        KgForeshadow.status.in_(["planted", "developing"]),
        func.coalesce(KgForeshadow.novel_id, -1).in_(scope_sql),
    )
    if own_book is None:
        fs_q = fs_q.where(
            (KgForeshadow.planted_chapter.is_(None))
            | (KgForeshadow.planted_chapter <= current_segment_no)
        )
    active_foreshadows = []
    for fs in db.scalars(fs_q.order_by(KgForeshadow.planted_chapter)):
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
            # 同上：情报的归属者是角色，kind 必须限定，否则同名 item 会串味/多结果。
            ent = resolve_entity_by_name(db, project_id, name, kind="character",
                                         novel_id=own_book)
            if ent is None or not _in_scope(ent.novel_id):
                continue
            knows: list[str] = []
            for k in db.scalars(
                select(KgEntityKnowledge).where(
                    KgEntityKnowledge.project_id == project_id,
                    KgEntityKnowledge.entity_id == ent.id,
                )
            ):
                label = _knowledge_label(db, k, scope)
                if label and label not in knows:
                    knows.append(label)
                if len(knows) >= 8:  # prompt 预算：单角色情报上限
                    break
            if knows:
                known_facts.append({"character": ent.canonical_name, "knows": knows})

    # —— 5. 最近 N 段的事件（in_story_date 喂物理时间校验 prompt）——
    # 书维度：只取本书 + 项目级 NULL。参考书的事件**绝不参与** —— 段号撞号时
    # 它们会以「近期事件」的身份被当成本书已发生的事，正是串书的入口。
    # SQLite 里 NULL 互不相等，折成 -1 再比（与 COALESCE(novel_id,-1) 同口径）。
    event_q = select(KgEvent).where(
        KgEvent.project_id == project_id,
        KgEvent.segment_no >= lo,
        KgEvent.segment_no <= current_segment_no,
        func.coalesce(KgEvent.novel_id, -1).in_(scope_sql),
        # 只取当前版本事件：同章旧稿的事件留档但不注入
        KgEvent.is_current.is_(True),
    )
    event_order = [KgEvent.segment_no.desc(), func.coalesce(KgEvent.novel_id, -1).desc()]
    recent_events = []
    for ev in db.scalars(event_q.order_by(*event_order)):
        recent_events.append({
            "summary": ev.summary,
            "event_type": ev.event_type,
            "in_story_date": ev.in_story_date,
        })

    # —— 6. 当前段实体之间已发生的关系（kg_relations）——
    # 两道门同时卡：边自身的 novel_id（现在有了这一列）+ 两端实体都在可见范围。
    # 只靠端点推导会让「一端项目级 NULL、另一端本书」的边钻进来；只靠边自身
    # 则挡不住老数据（novel_id 全 NULL）。两道一起才既不注别书的边，也不把整张
    # 关系图铺成人物小传。
    relationships: list[dict] = []
    visible = {
        eid for eid in anchor_ids
        if eid in ent_names and _in_scope(ent_books.get(eid))
    }
    if visible:
        seen: set[tuple[frozenset[int], str]] = set()
        for rel in db.scalars(
            select(KgRelation).where(
                KgRelation.project_id == project_id,
                KgRelation.status == "active",  # 候选边（集合端点门降级）不注入
                KgRelation.is_current.is_(True),  # 旧版本被重写章否定的边不注入
                func.coalesce(KgRelation.novel_id, -1).in_(scope_sql),
                KgRelation.from_id.in_(visible),
                KgRelation.to_id.in_(visible),
            ).order_by(KgRelation.strength.desc(), KgRelation.id)
        ):
            if rel.from_id == rel.to_id:
                continue
            key = (frozenset((rel.from_id, rel.to_id)), rel.rel_type)
            if key in seen:  # 反向重复边（A ally B / B ally A）只留一条
                continue
            seen.add(key)
            relationships.append({
                "from": ent_names[rel.from_id],
                "to": ent_names[rel.to_id],
                "rel_type": rel.rel_type,
            })
            if len(relationships) >= max_relations:
                break

    return {
        "recent_entities": recent_entities,
        "character_states": char_states,
        "active_foreshadows": active_foreshadows,
        "known_facts": known_facts,
        "recent_events": recent_events,
        "relationships": relationships,
    }


def _label_in_scope(obj, scope: tuple[int | None, ...]) -> bool:
    """标签行指向的实体/事件是否属于本书可见范围（NULL = 项目级通配）。"""
    if obj is None:
        return False
    nid = getattr(obj, "novel_id", None)
    return nid is None or nid in scope


def _knowledge_label(db, k, scope: tuple[int | None, ...] = ()) -> str | None:
    """一条 entity_knowledge 行 → 给 prompt 的事实标签（三类 known_kind）。

    返回 None 的行不渲染：三类都解析失败的脏行，或指向参考书实体/事件的
    情报（那是别书的事实，注入本书 prompt 等于把别人的剧情当已知情报）。
    """
    kind = (k.known_kind or "").strip()
    if not kind:
        # 老行容错：kind 缺失时按 id 先实体后事件解析（老库行为兼容）
        known_ent = db.get(KgEntity, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ent is not None:
            return known_ent.canonical_name if _label_in_scope(known_ent, scope) else None
        known_ev = db.get(KgEvent, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ev is not None:
            if not _label_in_scope(known_ev, scope):
                return None
            return f"事件：{(known_ev.summary or '')[:80]}"
        return (k.known_text or "").strip() or None
    if kind == "entity":
        known_ent = db.get(KgEntity, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ent is None or not _label_in_scope(known_ent, scope):
            return None
        return known_ent.canonical_name
    if kind == "event":
        known_ev = db.get(KgEvent, k.known_entity_id) if k.known_entity_id is not None else None
        if known_ev is not None:
            if not _label_in_scope(known_ev, scope):
                return None
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
    if sl.get("relationships"):
        # 只列当前段实体之间的边；明确标成「已发生的事实」，避免模型当成可改写设定。
        lines.append("【既有人物关系（已发生的事实，不得改写）】")
        for r in sl["relationships"]:
            mark = "↔" if r.get("rel_type") in _SYMMETRIC_REL_TYPES else "→"
            lines.append(f"- {r['from']}{mark}{r['to']}：{r['rel_type']}")
    if sl.get("recent_events"):
        lines.append("【近期事件】")
        for ev in sl["recent_events"][:10]:
            date = f"[{ev['in_story_date']}]" if ev.get("in_story_date") else ""
            lines.append(f"- {date}{ev['summary']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 8 个产品功能的读侧 API（关系演变曲线 / 节奏分析 / 人物情报追踪等）
# --------------------------------------------------------------------------- #

def relation_curve(db, project_id: int, from_name: str, to_name: str,
                   project=None) -> list[dict]:
    """关系演变曲线：两角色 rel 的 strength 历史（kg_relation_evolution）。

    按书过滤两层：实体解析只认本书（+ NULL），边自身也必须落在本书可见范围，
    否则同名角色在别书的演变曲线会混进本条曲线。
    """
    from app.models import KgRelation, KgRelationEvolution

    if project is None:
        project = db.get(Project, project_id)
    own_book = book_for_project(project) if project is not None else None
    scope_sql = (
        scope_sql_values(prompt_scope_book_ids(project)) if project is not None else (-1,)
    )
    a = resolve_entity_by_name(db, project_id, from_name, novel_id=own_book)
    b = resolve_entity_by_name(db, project_id, to_name, novel_id=own_book)
    if a is None or b is None:
        return []
    rels = db.scalars(select(KgRelation).where(
        KgRelation.project_id == project_id,
        KgRelation.status == "active",  # 候选边（集合端点门降级）不进演变曲线
        KgRelation.is_current.is_(True),  # 同章旧稿关系只留档，不进曲线
        func.coalesce(KgRelation.novel_id, -1).in_(scope_sql),
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


def can_character_know(db, project_id: int, character_name: str, fact_name: str,
                       project=None) -> bool:
    """人物情报追踪：character 此刻是否知道 fact（已知 → True）。

    fact 三种形态（known_kind 三类全消费）：
    - 实体名（"天津港"）→ 匹配 known_kind=entity 行
    - "事件：xxx" / 事件描述 → 匹配 event 行（id→summary 或 known_text）
    - "秘密：xxx" / 秘密文本 → 匹配 secret 行的 known_text（规范化包含）
    """
    from app.kg.knowledge_match import normalize_fact, event_summary_match

    if project is None:
        project = db.get(Project, project_id)
    own_book = book_for_project(project) if project is not None else None
    scope = prompt_scope_book_ids(project) if project is not None else (None,)

    # 人物情报的主体是角色，kind 必须限定（同名 item 不能当角色用）。
    # 同时只认本书的角色：别书的同名角色知道什么，与本书无关。
    ent = resolve_entity_by_name(
        db, project_id, character_name, kind="character", novel_id=own_book
    )
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
        label = _knowledge_label(db, k, scope)
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
                if ev is not None and _label_in_scope(ev, scope):
                    summary = ev.summary or ""
            target = summary or (k.known_text or "")
            if target and event_summary_match(body, target):
                return True
    return False


def rhythm_analysis(db, project_id: int, window: int = 10, project=None) -> dict:
    """节奏分析：每实体在最近 N 段的 mention 分布（谁在唱主角）。

    按书过滤：段号与实体都必须落在本书可见范围，否则两本书的段号会被当成
    同一条时间轴，主角榜会把别书的高频角色排到前面。
    """
    if project is None:
        project = db.get(Project, project_id)
    scope_sql = (
        scope_sql_values(prompt_scope_book_ids(project)) if project is not None else (-1,)
    )
    segs = db.scalars(
        select(KgMention.segment_no).where(
            KgMention.project_id == project_id,
            KgMention.is_current.is_(True),
            func.coalesce(KgMention.novel_id, -1).in_(scope_sql),
        ).distinct().order_by(KgMention.segment_no.desc()).limit(window)
    ).all()
    if not segs:
        return {"segments": [], "top_entities": []}
    lo = min(segs)
    rows = db.execute(
        select(KgEntity.canonical_name, KgEntity.kind, func.count(KgMention.id).label("n"))
        .join(KgMention, KgMention.entity_id == KgEntity.id)
        .where(KgMention.project_id == project_id, KgMention.segment_no >= lo,
               KgMention.is_current.is_(True),
               func.coalesce(KgMention.novel_id, -1).in_(scope_sql),
               func.coalesce(KgEntity.novel_id, -1).in_(scope_sql))
        .group_by(KgEntity.id)
        .order_by(func.count(KgMention.id).desc())
        .limit(20)
    ).all()
    return {
        "segments": sorted(segs),
        "top_entities": [{"name": r[0], "kind": r[1], "mentions": r[2]} for r in rows],
    }
