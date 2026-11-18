"""权威越界检查 K1–K5（B 子系统第三刀）。

设计口径（2026-11-18 与维护者确定）：

- **默认只报告**，写进 `quality_json["coherence"]["authority"]`，不改正文。
- 只有 K1（物件持有）/ K2（人物状态）的 definite 允许在标注精度 ≥90% 后
  进入重写；K3（未登记人物）/ K4（能力体系）/ K5（章纲剧透）永远只报告
  ——词表与名单覆盖不全，误报不可控。
- 每条判据都必须「证据闭合」：缺任一侧证据就不判。

数据源全部取项目自身：`kg_relations(rel_type="owns")`、`kg_character_state
(state_type="alive")`、`kg_entities(kind="character")`、`plot_bible`、`outline`。
参考书摄入的实体不参与（`novel_id` 非空的边/实体视为参考书事实，跳过）。
"""
from __future__ import annotations

import re

from sqlalchemy import select

from app.coherence import split_paragraphs
from app.models import KgCharacterState, KgEntity, KgRelation

CHECK_VERSION = 1

#: 明确的「持有/使用」动词：出现其一才认为正文在声称某人拿着某物。
_OWN_VERBS = (
    "拿出", "取出", "掏出", "握着", "挥动", "佩戴", "祭出", "捏着", "攥着",
    "手持", "提着", "亮出", "举起", "拔出", "摊开", "抛着", "把玩",
)

#: 转让/拾取动词：同句出现任一，持有关系可能正在变化，不判越界。
_TRANSFER_VERBS = (
    "给", "递给", "交给", "夺", "抢", "接过", "收下", "捡起", "拾起",
    "归还", "还回", "赠", "送出", "偷走", "抢走", "缴获", "掉落", "遗失", "丢掉",
)

#: 持有动词与物件名之间允许的最大字符距离。同句共现不等于同一动作
#: （「举起手机……附件页码一并拍下」里的「举起」作用在手机上，不是附件）。
_VERB_WINDOW = 12

#: 分句符：持有动词必须和物件在**同一分句**里才算同一动作。
_CLAUSE_SPLIT = re.compile(r"[，,、；;]")


def _clause_bounds(sent: str, pos: int) -> tuple[int, int]:
    """返回 `pos` 所在分句的 [start, end)。"""
    start = 0
    for m in _CLAUSE_SPLIT.finditer(sent):
        if m.start() < pos:
            start = m.end()
        else:
            return start, m.start()
    return start, len(sent)


#: 紧跟在这些字后面的物件名属于另一个词（「记录」→「记录单/记录本」）。
_COMPOUND_SUFFIX = frozenset("单本页表册簿")

#: 死亡语境豁免：回忆/梦境/遗物里的提及不算「出场」。
_MEMORY_MARKERS = (
    "回忆", "回想", "梦境", "梦见", "想起", "遗物", "替身", "复活",
    "尸体", "墓碑", "遗像", "幻觉", "闪回", "生前",
)

_DEAD_VALUES = ("false", "0", "否", "死", "死亡", "已死", "身亡", "殒命")

#: 说话人候选：`X道/说道/说/问/答/笑道`。只在 K3 用。
#: 后缀按「长的在前」排列 + 名字用惰性匹配，避免把「赵六说道」读成「赵六说」。
_SPEAKER_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,3}?)(?:说道|笑道|冷声道|开口|说|道|问|答)"
)

#: K3 的代词/泛称黑名单（不是人名）。
_NOT_NAMES = {
    "他们", "她们", "我们", "你们", "众人", "有人", "那人", "此人", "对方",
    "大家", "所有人", "自己", "两人", "三人", "一人", "旁人", "男人",
    "女人", "老者", "少年", "少女", "声音", "半晌", "片刻",
}

#: K3 的姓氏闸：说话人候选必须以常见姓氏开头，否则「我知/不知/没有回」这类
#: 短语会被当成名字（影子跑实测：不加闸时 52 条命中里几乎全是短语）。
_SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田"
    "董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白"
    "邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)

#: K5 的通用词（不能当剧透证据）。
_TOKEN_STOP = {
    "然后", "但是", "因为", "所以", "如果", "虽然", "只是", "已经", "还是",
    "这个", "那个", "一个", "自己", "他们", "我们", "你们", "什么", "怎么",
    "第一", "最终", "开始", "继续", "同时", "此时", "之后", "之前",
}

_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")


def _aliases(entity: KgEntity) -> list[str]:
    names = [entity.canonical_name] + list(entity.aliases or [])
    return [str(n).strip() for n in names if str(n).strip() and len(str(n).strip()) >= 2]


def _sentences(para: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(para) if s.strip()]


def _finding(check: str, level: str, paragraph: int, message: str,
             quote: str = "", evidence: list[dict] | None = None) -> dict:
    return {
        "category": "authority",
        "check": check,
        "level": level,
        "paragraph": paragraph,
        "message": message,
        "quote": quote[:120],
        "evidence": evidence or [],
    }


def _entities(db, project_id: int) -> dict[int, KgEntity]:
    rows = db.scalars(
        select(KgEntity).where(KgEntity.project_id == project_id)
    ).all()
    # 参考书摄入的实体（novel_id 非空）不属于本书设定
    return {e.id: e for e in rows if e.novel_id is None}


def _holder_as_of(edges: list[tuple[int, int, KgEntity]],
                  chapter: int | None) -> KgEntity | None:
    """按 `since_chapter` 取「截至本章」的持有者；并列或未知返回 None。

    影子跑实测：同一物件常有多个 active 持有边（先后属于不同角色），
    全部当成「当前持有者」会让每个后来的持有者都变成越界。只认
    `since_chapter` 最大且 ≤ 本章的那条；同一章有多个持有者说明 KG 自身冲突，
    不判（宁缺勿错）。
    """
    if chapter is None:
        # 章号未知时无法按时间线取持有者：多个持有者一律视为冲突，不判。
        owners = {holder.id for _since, _eid, holder in edges}
        return edges[0][2] if len(owners) == 1 else None
    usable = [
        (since or 0, eid, holder) for since, eid, holder in edges
        if (since or 0) <= chapter
    ]
    if not usable:
        return None
    top = max(since for since, _eid, _holder in usable)
    owners = {holder.id for since, _eid, holder in usable if since == top}
    if len(owners) > 1:
        return None
    return next(holder for since, _eid, holder in usable if since == top)


def _check_holdings(db, project_id: int, entities: dict[int, KgEntity],
                    text: str, chapter: int | None = None,
                    limit: int = 5) -> list[dict]:
    """K1：段内有明确持有句，但 KG 当前持有者是别人（definite）。"""
    rels = db.scalars(
        select(KgRelation).where(
            KgRelation.project_id == project_id,
            KgRelation.rel_type == "owns",
            KgRelation.status != "candidate",
            KgRelation.novel_id.is_(None),
        )
    ).all()
    by_item: dict[int, list[tuple[int, int, KgEntity]]] = {}
    item_entities: dict[int, KgEntity] = {}
    for r in rels:
        item, holder = entities.get(r.to_id), entities.get(r.from_id)
        if item is None or holder is None or item.kind != "item":
            continue
        if holder.kind != "character":
            continue
        by_item.setdefault(item.id, []).append(
            (int(r.since_chapter or 0), int(r.id), holder)
        )
        item_entities[item.id] = item
    pairs: list[tuple[KgEntity, KgEntity]] = []
    for item_id, edges in by_item.items():
        holder = _holder_as_of(edges, chapter)
        if holder is not None:
            pairs.append((item_entities[item_id], holder))
    if not pairs:
        return []

    chars = [e for e in entities.values() if e.kind == "character"]
    char_names = {a for c in chars for a in _aliases(c)}
    # 别名冲突：同一个别名被多个物件实体共用（如「手机」同时属于旧手机/录像手机）
    # 时，它无法定位具体物件——整条别名剔除，否则一句话会产出多条互相矛盾的命中。
    # 必须按**全部物件实体**统计，而不是只看有持有者的那些：某个物件因为持有者
    # 冲突被剔除后，它的别名不能改由另一个物件独占。
    alias_owner: dict[str, set[int]] = {}
    for item in entities.values():
        if item.kind != "item":
            continue
        for alias in _aliases(item):
            alias_owner.setdefault(alias, set()).add(item.id)
    shared = {a for a, owners in alias_owner.items() if len(owners) > 1}

    findings: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for pi, para in enumerate(split_paragraphs(text or ""), 1):
        if not para:
            continue
        for sent in _sentences(para):
            owners_here = [c for c in chars if any(a in sent for a in _aliases(c))]
            if not owners_here:
                continue
            # 同一句里多个物件命中时只取「最长别名」那条，避免别名包含关系重复报
            best: tuple[int, KgEntity, KgEntity, str, str, KgEntity] | None = None
            for item, holder in pairs:
                if any(a in sent for a in _aliases(holder)):
                    continue  # 持有者本人就在句子里，不判
                matched = [a for a in _aliases(item) if a in sent and a not in shared]
                if not matched:
                    continue
                alias = max(matched, key=len)
                # 「某某的旧手机」这类别名自带物主，按它判持有者只会自相矛盾
                if any(n in alias for n in char_names):
                    continue
                alias_pos = sent.find(alias)
                # 「拿出自己的旧手机」：正文已声明归属，不算越界
                if "自己的" in sent[max(0, alias_pos - 3):alias_pos]:
                    continue
                # 「记录」后面紧跟「单/本/页」时是另一个词（记录单/记录本），不是该物件
                tail_pos = alias_pos + len(alias)
                if sent[tail_pos:tail_pos + 1] in _COMPOUND_SUFFIX:
                    continue
                c_start, c_end = _clause_bounds(sent, alias_pos)
                verbs = [
                    (p, v) for v in _OWN_VERBS
                    if (p := sent.find(v)) != -1 and c_start <= p <= c_end
                ]
                if not verbs:
                    continue
                if any(v in sent for v in _TRANSFER_VERBS):
                    continue
                verb_pos, verb = min(verbs, key=lambda pv: abs(pv[0] - alias_pos))
                # 同句共现 ≠ 同一动作：动词必须作用在这个物件附近
                if abs(verb_pos - alias_pos) > _VERB_WINDOW:
                    continue
                others = [c for c in owners_here if c.id != holder.id]
                if not others:
                    continue
                other = others[0]
                # 人物出现在持有动词之前，才更像「他在拿」
                if sent.find(other.canonical_name) > verb_pos:
                    continue
                if best is None or len(alias) > len(best[3]):
                    best = (pi, item, holder, alias, sent, other)
            if best is None:
                continue
            pi2, item, holder, alias, sent2, other = best
            key = (pi2, item.canonical_name)
            if key in seen:
                continue
            seen.add(key)
            findings.append(_finding(
                "authority_holding",
                "definite",
                pi2,
                f"「{other.canonical_name}」使用了「{alias}」，"
                f"但 KG 里「{item.canonical_name}」的持有者是「{holder.canonical_name}」",
                sent2,
                [{
                    "item": item.canonical_name,
                    "matched_alias": alias,
                    "expected_holder": holder.canonical_name,
                    "mentioned_holder": other.canonical_name,
                }],
            ))
            if len(findings) >= limit:
                return findings
    return findings


def _latest_dead(db, project_id: int, entities: dict[int, KgEntity]) -> set[int]:
    rows = db.scalars(
        select(KgCharacterState)
        .where(
            KgCharacterState.project_id == project_id,
            KgCharacterState.state_type == "alive",
            KgCharacterState.is_current.is_(True),
        )
        .order_by(KgCharacterState.segment_no.asc(), KgCharacterState.id.asc())
    ).all()
    latest: dict[int, KgCharacterState] = {}
    for row in rows:
        latest[row.character_id] = row
    dead = {
        cid for cid, row in latest.items()
        if str(row.new_value or "").strip().lower() in _DEAD_VALUES
        or "死" in str(row.new_value or "")
    }
    # attrs 里的显式 alive=false 同样算死亡证据
    for cid, ent in entities.items():
        attrs = ent.attrs if isinstance(ent.attrs, dict) else {}
        if attrs.get("alive") is False:
            dead.add(cid)
    return dead


def _check_dead_characters(db, project_id: int, entities: dict[int, KgEntity],
                           text: str, limit: int = 5) -> list[dict]:
    """K2：已判死亡的角色以行动/对白出场，且无回忆/遗物语境（definite）。"""
    dead = _latest_dead(db, project_id, entities)
    if not dead:
        return []
    findings: list[dict] = []
    for cid in dead:
        ent = entities.get(cid)
        if ent is None:
            continue
        names = _aliases(ent)
        for pi, para in enumerate(split_paragraphs(text or ""), 1):
            if not any(n in para for n in names):
                continue
            if any(m in para for m in _MEMORY_MARKERS):
                continue
            quote = next((s for s in _sentences(para) if any(n in s for n in names)), para)
            findings.append(_finding(
                "authority_dead",
                "definite",
                pi,
                f"「{ent.canonical_name}」已被判定死亡，但本段仍以在场状态出现",
                quote,
                [{"character": ent.canonical_name, "state": "alive=false"}],
            ))
            break  # 每个角色只报首个段落
        if len(findings) >= limit:
            break
    return findings


def _known_names(entities: dict[int, KgEntity], plot_bible: dict | None,
                 outline: list | None, chapter: int | None) -> set[str]:
    known = {e.canonical_name for e in entities.values() if e.kind == "character"}
    for e in entities.values():
        if e.kind == "character":
            known.update(_aliases(e))
    bible = plot_bible if isinstance(plot_bible, dict) else {}
    for card in bible.get("characters") or []:
        if isinstance(card, dict) and card.get("name"):
            known.add(str(card["name"]).strip())
    entries = list(outline or [])
    if chapter and 0 < chapter <= len(entries):
        # 本章与前一章的大纲都算「已登记」
        for idx in (chapter - 2, chapter - 1):
            if 0 <= idx < len(entries):
                known.update(re.findall(r"[\u4e00-\u9fa5]{2,4}", str(entries[idx])))
    return {n for n in known if len(n) >= 2}


def _check_unknown_speakers(entities: dict[int, KgEntity], plot_bible: dict | None,
                            text: str, chapter: int | None, outline: list | None,
                            limit: int = 2) -> list[dict]:
    """K3：首次出现、KG 无记录、章纲也没有的说话人（suspected）。"""
    known = _known_names(entities, plot_bible, outline, chapter)
    counts: dict[str, int] = {}
    for match in _SPEAKER_RE.finditer(text or ""):
        name = match.group(1)
        if name in _NOT_NAMES or any(name in k for k in known):
            continue
        # 姓氏闸：不姓「我/不/没/通/电」的候选一律不是人名
        if len(name) < 2 or name[0] not in _SURNAMES:
            continue
        counts[name] = counts.get(name, 0) + 1
    findings: list[dict] = []
    for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        if count < 2:
            continue
        findings.append(_finding(
            "authority_unknown_person",
            "suspected",
            1,
            f"「{name}」以说话人身份出现 {count} 次，但 KG 与本章章纲都没有登记",
            "",
            [{"name": name, "mentions": count}],
        ))
        if len(findings) >= limit:
            break
    return findings


def _ability_schema(plot_bible: dict | None) -> dict[str, set[str]]:
    """从角色卡提取「角色 → 已授予能力」。没有该结构返回空 dict。"""
    bible = plot_bible if isinstance(plot_bible, dict) else {}
    out: dict[str, set[str]] = {}
    for card in bible.get("characters") or []:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        name = str(card["name"]).strip()
        raw = (
            card.get("skills") or card.get("abilities")
            or card.get("功法") or card.get("技能") or card.get("能力")
        )
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[、,，;；/\s]+", raw) if x]
        if isinstance(raw, list):
            granted = {str(x).strip() for x in raw if str(x).strip()}
            if granted:
                out[name] = granted
    return out


def _check_abilities(plot_bible: dict | None, text: str,
                     limit: int = 2) -> tuple[list[dict], list[str]]:
    """K4：使用未授予的能力（suspected）。能力表缺失时显式跳过，不猜。"""
    schema = _ability_schema(plot_bible)
    if not schema:
        return [], ["capability_schema_missing"]
    all_abilities = {a for granted in schema.values() for a in granted}
    if not all_abilities:
        return [], ["capability_schema_missing"]

    findings: list[dict] = []
    for pi, para in enumerate(split_paragraphs(text or ""), 1):
        for sent in _sentences(para):
            present = [name for name in schema if name in sent]
            if not present:
                continue
            for ability in all_abilities:
                if ability not in sent:
                    continue
                allowed = any(ability in schema.get(name, set()) for name in present)
                if allowed:
                    continue
                findings.append(_finding(
                    "authority_ability",
                    "suspected",
                    pi,
                    f"「{present[0]}」使用了未在设定中授予的能力「{ability}」",
                    sent,
                    [{"character": present[0], "ability": ability}],
                ))
                if len(findings) >= limit:
                    return findings, []
    return findings, []


def _check_outline_spoiler(entities: dict[int, KgEntity], outline: list | None,
                           chapter: int | None, text: str,
                           limit: int = 2) -> list[dict]:
    """K5：下一章才登场的人物，本章已出场（suspected）。

    影子跑两轮修正后的判据（60 段样本）：

    - 不按字面滑窗取词：标点/虚词会被当成关键词，大面积误报；
    - 不认物件/地点：药盒、楼道、手机这类场景名词提前出现是正常铺垫，
      30 段样本里几乎全是误报；
    - 必须**首次登场**：名字在更早任何一章的章纲里出现过，就不算「提前」，
      否则主角名会每章都命中。
    """
    entries = list(outline or [])
    if not chapter or chapter >= len(entries) or chapter < 1:
        return []
    prior = " ".join(str(e) for e in entries[:chapter])
    nxt = str(entries[chapter])
    hit: list[str] = []
    for ent in entities.values():
        if ent.kind != "character":
            continue
        matched = [a for a in _aliases(ent) if a in nxt and a not in prior]
        if not matched:
            continue
        alias = max(matched, key=len)
        if alias in (text or ""):
            hit.append(alias)
    if len(hit) < 2:
        return []
    hit = sorted(set(hit))
    return [_finding(
        "authority_outline_spoiler",
        "suspected",
        1,
        f"本段出现下一章才登场的人物 {hit[:5]}，可能提前写了后续剧情",
        "",
        [{"chapter": chapter + 1, "tokens": hit[:5]}],
    )][:limit]


def run_authority_checks(db, project, text: str, chapter: int | None) -> dict:
    """跑 K1–K5，返回可直接并入 `quality_json["coherence"]` 的纯数据。"""
    findings: list[dict] = []
    notes: list[dict] = []
    try:
        entities = _entities(db, project.id)
        findings += _check_holdings(db, project.id, entities, text, chapter)
        findings += _check_dead_characters(db, project.id, entities, text)
        findings += _check_unknown_speakers(
            entities, project.plot_bible, text, chapter, project.outline
        )
        ability_findings, ability_notes = _check_abilities(project.plot_bible, text)
        findings += ability_findings
        notes += [
            {"check": "authority_ability", "reason": reason}
            for reason in ability_notes
        ]
        findings += _check_outline_spoiler(entities, project.outline, chapter, text)
    except Exception as exc:  # noqa: BLE001 —— 检测器故障不能拖垮生成
        return {
            "version": CHECK_VERSION,
            "findings": [],
            "counts": {"definite": 0, "suspected": 0},
            "notes": [{"check": "authority", "reason": f"error: {exc}"}],
        }

    return {
        "version": CHECK_VERSION,
        "findings": findings,
        "counts": {
            "definite": sum(1 for f in findings if f["level"] == "definite"),
            "suspected": sum(1 for f in findings if f["level"] == "suspected"),
        },
        "notes": notes,
    }
