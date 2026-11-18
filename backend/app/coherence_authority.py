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
    "大家", "所有人", "自己", "两人", "三人", "一人", "旁人", "旁人", "男人",
    "女人", "老者", "少年", "少女", "声音", "众人", "半晌", "片刻",
}

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


def _check_holdings(db, project_id: int, entities: dict[int, KgEntity],
                    text: str, limit: int = 5) -> list[dict]:
    """K1：段内有明确持有句，但 KG 当前持有者是别人（definite）。"""
    rels = db.scalars(
        select(KgRelation).where(
            KgRelation.project_id == project_id,
            KgRelation.rel_type == "owns",
            KgRelation.status != "candidate",
            KgRelation.novel_id.is_(None),
        )
    ).all()
    pairs: list[tuple[KgEntity, KgEntity]] = []
    for r in rels:
        item, holder = entities.get(r.to_id), entities.get(r.from_id)
        if item is not None and holder is not None and item.kind == "item":
            if holder.kind == "character":
                pairs.append((item, holder))
    if not pairs:
        return []

    chars = [e for e in entities.values() if e.kind == "character"]
    findings: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for pi, para in enumerate(split_paragraphs(text or ""), 1):
        if not para:
            continue
        for sent in _sentences(para):
            owners_here = [c for c in chars if any(a in sent for a in _aliases(c))]
            if not owners_here:
                continue
            for item, holder in pairs:
                if any(a in sent for a in _aliases(holder)):
                    continue  # 持有者本人就在句子里，不判
                if not any(a in sent for a in _aliases(item)):
                    continue
                verb = next((v for v in _OWN_VERBS if v in sent), None)
                if verb is None:
                    continue
                if any(v in sent for v in _TRANSFER_VERBS):
                    continue
                others = [c for c in owners_here if c.id != holder.id]
                if not others:
                    continue
                other = others[0]
                # 人物出现在持有动词之前，才更像「他在拿」
                if sent.find(other.canonical_name) > sent.find(verb):
                    continue
                key = (pi, item.canonical_name)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(_finding(
                    "authority_holding",
                    "definite",
                    pi,
                    f"「{other.canonical_name}」{verb}了「{item.canonical_name}」，"
                    f"但 KG 当前持有者是「{holder.canonical_name}」",
                    sent,
                    [{
                        "item": item.canonical_name,
                        "expected_holder": holder.canonical_name,
                        "mentioned_holder": other.canonical_name,
                        "verb": verb,
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


def _ngrams(text: str, sizes=(2, 3, 4)) -> set[str]:
    """按 2–4 字滑窗取候选词：outline 没有分词，整段匹配会漏。"""
    out: set[str] = set()
    for size in sizes:
        for i in range(0, max(len(text) - size + 1, 0)):
            token = text[i:i + size]
            if token.strip() and token not in _TOKEN_STOP:
                out.add(token)
    return out


def _check_outline_spoiler(outline: list | None, chapter: int | None, text: str,
                           limit: int = 2) -> list[dict]:
    """K5：本段出现下一章大纲的关键词（suspected）。"""
    entries = list(outline or [])
    if not chapter or chapter >= len(entries) or chapter < 1:
        return []
    nxt = str(entries[chapter])
    cur = str(entries[chapter - 1])
    tokens = {t for t in _ngrams(nxt) if t not in cur}
    hit = [t for t in tokens if t in (text or "")]
    # 去掉被更长命中词包含的短词，避免一条证据顶三条计数
    strong = [t for t in hit if not any(t != o and t in o for o in hit)]
    if len({t for t in hit if len(t) >= 2}) < 3 or not strong:
        return []
    return [_finding(
        "authority_outline_spoiler",
        "suspected",
        1,
        f"本段出现下一章章纲的关键词 {strong[:5]}，可能提前写了后续剧情",
        "",
        [{"chapter": chapter + 1, "tokens": strong[:5]}],
    )][:limit]


def run_authority_checks(db, project, text: str, chapter: int | None) -> dict:
    """跑 K1–K5，返回可直接并入 `quality_json["coherence"]` 的纯数据。"""
    findings: list[dict] = []
    notes: list[dict] = []
    try:
        entities = _entities(db, project.id)
        findings += _check_holdings(db, project.id, entities, text)
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
        findings += _check_outline_spoiler(project.outline, chapter, text)
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
