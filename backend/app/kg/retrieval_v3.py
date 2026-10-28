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

import re

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
    max_foreshadows: int = 60,
    active_characters: list[str] | None = None,
) -> dict:
    """按当前剧情点取 KG 切片喂 prompt。

    window: 回溯最近 N 段的 mention
    max_entities: 实体清单上限（prompt 预算）
    max_foreshadows: 未回收伏笔上限（安全阀；超了留最早埋下的那批，并把省略条数
        放进 `foreshadows_omitted` 让渲染层如实说出）
    active_characters: 当前段涉及的角色名（用于取状态和情报）；None 则用本段全部

    返回：
    {
      "recent_entities": [{"name","kind","context","mention_count"}],
      "character_states": [{"character","state_type","new_value"}],
      "active_foreshadows": [{"name","status","planted_chapter","planned_payoff_chapter"}],
      "foreshadows_omitted": int,    # 因上限被省略的条数（0 = 没省略）
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
    #
    # ## 为什么要**有界**（2026-10-28 加）
    #
    # 这一块从前是**没有上限**的：实测一个只写了 9 段的项目攒出 **117 条**伏笔
    # （单段抽取只有 3~5 条，但后台对同一批段落**反复抽了很多轮**，
    # 每轮都攒一批同义新名字）⇒ 切片把全部 97 条 active 一条不落地渲染出来
    # ⇒ **2,639 字，占 user 提示词两成**。而本模块的文档一直写着「切片有界（budget）」——
    # 那时只有实体有上限，伏笔没有。
    #
    # 上限只当**安全阀**：真实线索数不该有一百条，上限宽（默认 60）到通常不触发；
    # **真触发了要如实说出省略了几条**（渲染层会写「另有 N 条未列出」），不静默隐藏。
    #
    # ## 超限时留哪一批
    #
    # 排序口径：**未超期优先**（本模块文档一直写着的口径：active 伏笔
    # 「未超期优先」）—— 有**说得通**的回收章的先列（快该收的那条最该被看见），
    # 按回收章升序；其余的按**埋下章升序**（越早埋的越容易被长提示词淹没，
    # 越该被提醒；最近埋下的那些前情提要里刚写过，模型本来就看得见）。
    #
    # 实测两种固定顺序都有坏处：「最近优先」会把第 1~3 章的**全部**铺垫丢掉
    # （对这个要写第 5 段的模型恰恰是最坏的丢法）；
    # 所以上限只当**安全阀**（默认 60，真实线索数不该有一百条），
    # **真触发了要如实说出省略了几条**，不静默隐藏。
    active_all = list(db.scalars(
        select(KgForeshadow).where(
            KgForeshadow.project_id == project_id,
            KgForeshadow.status.in_(["planted", "developing"]),
        )
    ))
    from app.kg.foreshadow_quality import plausible_payoff_chapter

    def _order(fs):
        payoff = fs.planned_payoff_chapter
        has_payoff = plausible_payoff_chapter(payoff, fs.planted_chapter)
        return (
            0 if has_payoff else 1,          # 有可信回收章的先列
            payoff if has_payoff else 0,     # 回收章升序（先收的先记着）
            fs.planted_chapter is None,      # 埋下章未知的排后
            fs.planted_chapter or 0,         # 埋下章升序
            fs.id,                           # 同分按 id（截断结果可测）
        )

    active_all.sort(key=_order)
    active_foreshadows = [
        {
            "name": fs.name,
            "status": fs.status,
            "planted_chapter": fs.planted_chapter,
            "planned_payoff_chapter": fs.planned_payoff_chapter,
        }
        for fs in active_all[:max_foreshadows]
    ]
    foreshadows_omitted = max(0, len(active_all) - len(active_foreshadows))

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
        "foreshadows_omitted": foreshadows_omitted,
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


#: 一个「完整句」：任意字符 + 句末标点 + 可选的收尾引号。
#:
#: 为什么单独写正则：中文的收尾引号在句号**之后**（`。”`）⇒ 按标点裸切会把
#: 那个引号甩到下一段开头，切出一堆「”沈」这种碎片（第一版就是这么错的）。
_SENT_RE = re.compile(r"[^。！？…]*[。！？…](?:[”’」』）】》\"])?")
_CJK_RE = re.compile(r"[一-鿿]")

#: **续句开头**：以这些字/符号起头的句子必然是上一句被切断的后半截
#: （「了一眼街口，才把药箱放到柜上。」—— 句首的「看」被切掉了）。
#: 只收**无歧义**的那些：了/着/的/和/与/及/跟 + 标点 + 收尾引号。
#: 不用「被/就/都/也/还/又/才」——它们在口语里能正常起句。
_CONTINUATION_HEADS = "了着的和与及跟，、；：！？…）】》”’」』"

#: 从句边界（半句停在这里）：，、；：
_CLAUSE_BREAKS = "，、；："


def _squash(s: str) -> str:
    """去掉空白与换行 —— 提示词里一行就是一行，别让原文的段落空行插进来。"""
    return re.sub(r"\s+", "", s or "")


def _trim_leading_clause(sent: str) -> str:
    """砍掉句首那段**不完整的从句**。

    窗口起点落在句中时，正则凑出来的「完整句」是上一句的后半截
    （「看了一眼街口，才把药箱放到柜上。」⇒ 句首的「看」被切掉了）。
    整句作废太狠（后半截「才把药箱放到柜上。」本身是能读的），
    ⇒ 只砍掉第一个从句边界之前的那截残句。

    没有从句边界可砍 ⇒ 这句没救了（返回空串）。
    """
    if not sent or sent[0] not in _CONTINUATION_HEADS:
        return sent
    idx = -1
    for i, ch in enumerate(sent):
        if ch in _CLAUSE_BREAKS:
            idx = i
            break
    if idx < 0:
        return ""
    rest = sent[idx + 1:].strip()
    return rest if len(rest) > 1 else ""


def _compact_context(text: str, limit: int = 40) -> str:
    """把原文碎片压成**一句完整的话**（≤ limit 字）。

    ## 为什么

    `context_window` 是从正文里**随机关**截出来的窗口 ⇒ 边界落在半句上。
    实测（2026-10-28，验收库同一份数据）：**20 条里只有 1 条**以句末标点收尾，
    15 条还带着原文的段落空行（「像有人在匣中敲了一）」「周郎中坐在柜台后，
    正用麻布擦刀镡…」）。

    半句既费 token、又**不可扫读**，还可能让模型照抄半截话
    —— 它本来只是用来「说明这个名字指什么」的。

    ⇒ 取窗口里**最后一个完整句**（以 。！？ 收尾，可带收尾引号），
    **尾部那段残句直接丢掉**；窗口起点落在句中就砍掉开头那个残从句；
    整窗都在一句长句里才退到「按从句边界截断 + 标…」。

    ## 口径（宁可少给一句，不给半句）
    - 完整句候选：含汉字、长度 > 1（滤掉纯标点与孤立引号）
    - 候选以「续句开头」起头 ⇒ 砍掉开头残从句；砍完为空 ⇒ 整句作废
    - 从**最近**的候选往前找第一条装得下 limit 的
    """
    s = (text or "").strip()
    if not s:
        return ""
    # 完整句候选：必须含汉字、长度 > 1（滤掉纯标点与孤立引号）
    cands = [m.group(0).strip() for m in _SENT_RE.finditer(s)]
    cands = [c for c in cands if len(c) > 1 and _CJK_RE.search(c)]
    # 起点落在句中 ⇒ 砍掉开头那段残从句（不是整句作废）
    cands = [_trim_leading_clause(c) for c in cands]
    cands = [c for c in cands if c]
    if not cands:
        # 整个窗口都在一句长句里 ⇒ 没有完整句可用
        s2 = _trim_leading_clause(s) or s
        cut = s2[:limit]
        # 尽量断在从句边界上，别断在半截词中间
        brk = max((cut.rfind(ch) for ch in _CLAUSE_BREAKS), default=-1)
        if brk > 0:
            cut = cut[:brk]
        return _squash(cut.rstrip("，。！？、；：") + "…")
    # 从最近的句子往前找第一条装得下的
    for sent in reversed(cands):
        if len(sent) <= limit:
            return _squash(sent)
    head = cands[0]
    return _squash(head[:limit].rstrip("，。！？、；：") + "…")


def format_slice_for_prompt(sl: dict) -> str:
    """把 slice 渲染成一段 prompt 文本（有血有肉的事实，带原文片段）。"""
    lines: list[str] = []
    if sl.get("recent_entities"):
        lines.append("【近期出场实体】")
        lines.append("（名字清单：括号里是这个名字最近一次出现的**完整一句**，"
                     "只作指代参考，不是要照抄的原文）")
        # 同一句原文常被多个实体各带一遍（实测 20 个实体里有 **7 对**共享同一段原文）
        # ⇒ 同一块里**只留第一处**，其余实体只留名字与类型。
        seen_ctx: set[str] = set()
        for e in sl["recent_entities"]:
            ctx = _compact_context(e.get("context") or "")
            if ctx and ctx in seen_ctx:
                ctx = ""
            elif ctx:
                seen_ctx.add(ctx)
            suffix = f"（{ctx}）" if ctx else ""
            lines.append(f"- {e['name']}（{e['kind']}）{suffix}")
    if sl.get("character_states"):
        lines.append("【角色当前状态】")
        for s in sl["character_states"]:
            lines.append(f"- {s['character']}·{s['state_type']}: {s['new_value']}")
    if sl.get("active_foreshadows"):
        lines.append("【未回收伏笔】")
        # ## 为什么这里**只列名字**（2026-10-28 改）
        #
        # 原来是 `- {名字}（{状态}{，计划第 N 章回收}）`，97 行 **2,639 字**。
        # 压成只列名字后 **1,457 字（省 44%）**，而且**一条线索都不丢**
        # —— 只是不再陪标状态与回收章。
        #
        # 丢的这两样都经不起查：
        # - **状态**（planted/developing）：这一块**只会**列这两种状态
        #   （查询就按 `status IN ('planted','developing')` 过滤），
        #   所以「二者之一」对读者没有任何信息量。
        # - **计划回收章**：实测 97 条里 **72 条是 0**（第 0 章不存在）、
        #   **18 条是 50**（＝抽取提示 JSON 示例里的字面值，模型照抄的）
        #   —— 只有 2 条说得通。**编出来的约束比没有约束更糟**：
        #   把「计划第 50 章回收」喂给模型，就是给它一条假约束。
        #
        # ⇒ 回收章**不进提示词**（账本里还在，界面与诊断仍看得到）。
        for f in sl["active_foreshadows"]:
            lines.append(f"- {f['name']}")
        omitted = int(sl.get("foreshadows_omitted") or 0)
        if omitted:
            # 上限是安全阀 —— 触发了就**如实说出**省略了几条，不静默隐藏。
            # 留的是哪一批见 slice_for_prompt 里的排序理由。
            lines.append(f"（另有 {omitted} 条未列出）")
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
