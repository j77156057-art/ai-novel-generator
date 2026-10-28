"""v3.0 抽取器：LLM 版软 schema 输出 + schema 漂移防御。

与 v2.6 规则基线（extractor.py RuleBasedExtractor）的关系：
- 规则基线保留（确定性、可离线验证），作为 LLM 不可用时的兜底
- v3.0 的**高质量**抽取走 LLM：输出 EXTRACTION_SCHEMA 软 schema（含 SVO triplet、
  arc/secrets/state_changes/foreshadows），落 v3.0 的 9 张表
- schema 漂移防御（LLM 输出字段名不一致的兜底归一）在本模块 normalize_extraction

批抽策略见 batch.py：分段落库后入队，worker 一次抽 5 段合并（省 50% LLM 调用）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# —— 软 schema 的字段别名表（schema 漂移防御）——
# LLM 输出字段名不一致时（entities↔characters、from↔source、name↔canonical_name），
# 在归一阶段把别名映射回规范字段。这是**兜底**而非**依赖**：normalize 后仍缺键的
# 字段按类型默认值处理（list→[] / str→"" / int→None），不阻塞落库。
_FIELD_ALIASES = {
    "entities": ("entities", "characters", "人物", "实体"),
    "relations": ("relations", "relationships", "关系"),
    "events": ("events", "事件"),
    "event_links": ("event_links", "eventLinks", "causality", "因果链", "事件链"),
    "entity_knowledge": ("entity_knowledge", "entityKnowledge", "knowledge", "情报", "人物情报"),
    "state_changes": ("state_changes", "stateChanges", "character_states", "状态变化"),
    "foreshadows": ("foreshadows", "伏笔"),
}
# entity 字段别名
_ENTITY_ALIASES = {
    "kind": ("kind", "type", "类型"),
    "canonical_name": ("canonical_name", "name", "名字", "名称"),
    "aliases": ("aliases", "alias", "别名"),
    "attrs": ("attrs", "attributes", "properties", "属性"),
}
# relation 字段别名
_RELATION_ALIASES = {
    "from": ("from", "source", "a", "from_name", "主体"),
    "to": ("to", "target", "b", "to_name", "客体"),
    "rel_type": ("rel_type", "type", "relation", "关系类型"),
    "strength": ("strength", "weight", "强度"),
}
# event 字段别名
_EVENT_ALIASES = {
    "sequence": ("sequence", "seq", "序号"),
    "chapter": ("chapter", "chapter_no", "章"),
    "event_type": ("event_type", "type", "类型"),
    "summary": ("summary", "desc", "描述", "svo", "svo_triplet"),
    "subject": ("subject", "from", "主体"),
    "verb": ("verb", "action", "动作"),
    "object": ("object", "to", "客体"),
    "location": ("location", "place", "地点"),
    "in_story_date": ("in_story_date", "date", "story_date", "故事内时间"),
}
# state_change 字段别名
_STATE_ALIASES = {
    "character": ("character", "name", "角色"),
    "state_type": ("state_type", "type", "类型"),
    "new_value": ("new_value", "value", "新值"),
}
# foreshadow 字段别名
_FORESHADOW_ALIASES = {
    "name": ("name", "title", "名称"),
    "status": ("status", "state", "状态"),
    "planted_chapter": ("planted_chapter", "planted", "埋下章"),
    "planned_payoff_chapter": ("planned_payoff_chapter", "planned_payoff", "计划回收章"),
}
# event_link 字段别名
_EVENT_LINK_ALIASES = {
    "from_sequence": ("from_sequence", "from_seq", "from", "from_event", "源事件"),
    "to_sequence": ("to_sequence", "to_seq", "to", "to_event", "目标事件"),
    "link_type": ("link_type", "type", "关系", "link"),
}
# entity_knowledge 字段别名
_KNOWLEDGE_ALIASES = {
    "character": ("character", "entity", "角色", "name"),
    "knows": ("knows", "known", "fact", "known_entity", "知道"),
    "known_kind": ("known_kind", "kind", "类型"),
    "learned_at_sequence": ("learned_at_sequence", "learned_at", "learned_at_seq", "learned_at_event", "学到于"),
}

# 合法取值域（抽取结果超域值丢弃/归一，防 LLM 幻觉）
VALID_ENTITY_KINDS = {"character", "location", "faction", "item", "worldrule", "power_system"}
VALID_REL_TYPES = {
    "ally", "enemy", "mentor", "subordinate", "romantic", "belongs_to", "located_at", "owns",
}
VALID_EVENT_TYPES = {
    "incident", "revelation", "foreshadow_plant", "foreshadow_payoff", "death",
    "romance_milestone", "battle", "decision", "time_marker",
}
VALID_EVENT_LINK_TYPES = {"caused_by", "causes", "follows", "resolves", "reverses", "parallel"}
VALID_KNOWN_KINDS = {"entity", "event", "secret"}
VALID_STATE_TYPES = {"location", "injury", "mood", "goal", "inventory", "alive"}
VALID_FORESHADOW_STATUS = {"planted", "developing", "paid_off", "dropped", "expired"}


def _pick(d: dict, aliases: tuple) -> object | None:
    """按别名顺序取第一个存在的键值（字段漂移归一）。"""
    if not isinstance(d, dict):
        return None
    for a in aliases:
        if a in d:
            return d[a]
    return None


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v is None:
        return []
    return [v]


def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _as_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v, default: float) -> float:
    try:
        f = float(v)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return default


def normalize_extraction(raw: dict) -> dict:
    """schema 漂移防御：把 LLM 原始输出归一成规范软 schema。

    - 字段名别名映射（entities↔characters、from↔source…）
    - 非法枚举值丢弃（kind 不在 VALID_ENTITY_KINDS → 整条 entity 丢）
    - strength 截断到 [0,1]
    - 缺键字段按类型默认值兜底（不阻塞落库）
    """
    if not isinstance(raw, dict):
        return {"entities": [], "relations": [], "events": [], "event_links": [],
                "entity_knowledge": [], "state_changes": [], "foreshadows": []}

    out = {
        "entities": [],
        "relations": [],
        "events": [],
        "event_links": [],
        "entity_knowledge": [],
        "state_changes": [],
        "foreshadows": [],
    }

    # —— entities ——
    for e in _as_list(_pick(raw, _FIELD_ALIASES["entities"])):
        if not isinstance(e, dict):
            continue
        kind = _as_str(_pick(e, _ENTITY_ALIASES["kind"])) or "character"
        if kind not in VALID_ENTITY_KINDS:
            continue  # 非法 kind 整条丢（静默，不落库）
        name = _as_str(_pick(e, _ENTITY_ALIASES["canonical_name"]))
        if not name or len(name) > 128:
            continue
        aliases = [_as_str(a) for a in _as_list(_pick(e, _ENTITY_ALIASES["aliases"])) if _as_str(a)]
        attrs = _pick(e, _ENTITY_ALIASES["attrs"])
        out["entities"].append({
            "kind": kind,
            "canonical_name": name,
            "aliases": aliases[:10],
            "attrs": attrs if isinstance(attrs, dict) else {},
        })

    # —— relations ——
    for r in _as_list(_pick(raw, _FIELD_ALIASES["relations"])):
        if not isinstance(r, dict):
            continue
        a = _as_str(_pick(r, _RELATION_ALIASES["from"]))
        b = _as_str(_pick(r, _RELATION_ALIASES["to"]))
        rt = _as_str(_pick(r, _RELATION_ALIASES["rel_type"]))
        if not a or not b or a == b:
            continue
        if rt not in VALID_REL_TYPES:
            continue
        out["relations"].append({
            "from": a, "to": b, "rel_type": rt,
            "strength": _as_float(_pick(r, _RELATION_ALIASES["strength"]), 0.5),
        })

    # —— events ——
    for ev in _as_list(_pick(raw, _FIELD_ALIASES["events"])):
        if not isinstance(ev, dict):
            continue
        et = _as_str(_pick(ev, _EVENT_ALIASES["event_type"])) or "incident"
        if et not in VALID_EVENT_TYPES:
            et = "incident"
        summary = _as_str(_pick(ev, _EVENT_ALIASES["summary"]))
        out["events"].append({
            "sequence": _as_int(_pick(ev, _EVENT_ALIASES["sequence"])),  # 批内序号，供 links/knowledge 引用
            "chapter": _as_int(_pick(ev, _EVENT_ALIASES["chapter"])),
            "event_type": et,
            "summary": summary[:500],
            "subject": _as_str(_pick(ev, _EVENT_ALIASES["subject"])),
            "verb": _as_str(_pick(ev, _EVENT_ALIASES["verb"]))[:64],
            "object": _as_str(_pick(ev, _EVENT_ALIASES["object"])),
            "location": _as_str(_pick(ev, _EVENT_ALIASES["location"])),
            "in_story_date": _as_str(_pick(ev, _EVENT_ALIASES["in_story_date"]))[:64],
        })

    # —— state_changes ——
    for s in _as_list(_pick(raw, _FIELD_ALIASES["state_changes"])):
        if not isinstance(s, dict):
            continue
        char = _as_str(_pick(s, _STATE_ALIASES["character"]))
        st = _as_str(_pick(s, _STATE_ALIASES["state_type"]))
        if not char or st not in VALID_STATE_TYPES:
            continue
        out["state_changes"].append({
            "character": char,
            "state_type": st,
            "new_value": _as_str(_pick(s, _STATE_ALIASES["new_value"]))[:500],
        })

    # —— foreshadows ——
    for f in _as_list(_pick(raw, _FIELD_ALIASES["foreshadows"])):
        if not isinstance(f, dict):
            continue
        name = _as_str(_pick(f, _FORESHADOW_ALIASES["name"]))
        if not name or len(name) > 200:
            continue
        status = _as_str(_pick(f, _FORESHADOW_ALIASES["status"])) or "planted"
        if status not in VALID_FORESHADOW_STATUS:
            status = "planted"
        out["foreshadows"].append({
            "name": name,
            "status": status,
            "planted_chapter": _as_int(_pick(f, _FORESHADOW_ALIASES["planted_chapter"])),
            "planned_payoff_chapter": _as_int(_pick(f, _FORESHADOW_ALIASES["planned_payoff_chapter"])),
        })

    # —— event_links（事件因果链，用批内 sequence 引用）——
    for lk in _as_list(_pick(raw, _FIELD_ALIASES["event_links"])):
        if not isinstance(lk, dict):
            continue
        from_seq = _as_int(_pick(lk, _EVENT_LINK_ALIASES["from_sequence"]))
        to_seq = _as_int(_pick(lk, _EVENT_LINK_ALIASES["to_sequence"]))
        lt = _as_str(_pick(lk, _EVENT_LINK_ALIASES["link_type"]))
        if from_seq is None or to_seq is None or from_seq == to_seq:
            continue
        if lt not in VALID_EVENT_LINK_TYPES:
            continue
        out["event_links"].append({
            "from_sequence": from_seq,
            "to_sequence": to_seq,
            "link_type": lt,
        })

    # —— entity_knowledge（人物情报，learned_at_sequence 引用事件）——
    for k in _as_list(_pick(raw, _FIELD_ALIASES["entity_knowledge"])):
        if not isinstance(k, dict):
            continue
        char = _as_str(_pick(k, _KNOWLEDGE_ALIASES["character"]))
        knows = _as_str(_pick(k, _KNOWLEDGE_ALIASES["knows"]))
        if not char or not knows or char == knows:
            continue
        kk = _as_str(_pick(k, _KNOWLEDGE_ALIASES["known_kind"])) or "entity"
        if kk not in VALID_KNOWN_KINDS:
            kk = "entity"
        out["entity_knowledge"].append({
            "character": char,
            "knows": knows,
            "known_kind": kk,
            "learned_at_sequence": _as_int(_pick(k, _KNOWLEDGE_ALIASES["learned_at_sequence"])),
        })

    return out


# —— LLM 抽取 prompt ——
_EXTRACT_SYSTEM = (
    "你是小说知识图谱抽取器。从给定文本中抽取结构化信息，**只输出 JSON**，"
    "不要任何解释、不要 markdown 代码块标记。\n\n"
    "输出格式（字段名必须严格一致）：\n"
    "{\n"
    '  "entities": [{"kind": "character|location|faction|item|worldrule|power_system", '
    '"canonical_name": "规范名", "aliases": ["别名"], "attrs": {"arc": "角色弧", '
    '"current_goal": "当前目标", "secrets": ["秘密"]}}],\n'
    '  "relations": [{"from": "角色名", "to": "角色名", "rel_type": '
    '"ally|enemy|mentor|subordinate|romantic|belongs_to|located_at|owns", "strength": 0.5}],\n'
    '  "events": [{"sequence": 1, "chapter": 1, "event_type": "incident|revelation|foreshadow_plant|'
    'foreshadow_payoff|death|romance_milestone|battle|decision|time_marker", '
    '"summary": "SVO 事件描述", "subject": "主体名", "verb": "动作", "object": "客体名", '
    '"location": "地点名", "in_story_date": "故事内时间（如贞观三年秋）"}],\n'
    '  "event_links": [{"from_sequence": 1, "to_sequence": 2, "link_type": '
    '"caused_by|causes|follows|resolves|reverses|parallel"}],\n'
    '  "entity_knowledge": [{"character": "角色名", "knows": "另一角色/地点/事实名", '
    '"known_kind": "entity|event|secret", "learned_at_sequence": 1}],\n'
    '  "state_changes": [{"character": "角色名", "state_type": '
    '"location|injury|mood|goal|inventory|alive", "new_value": "变化后的值"}],\n'
    '  "foreshadows": [{"name": "伏笔名", "status": "planted|developing|paid_off|dropped", '
    '"planted_chapter": 1, "planned_payoff_chapter": 50}]\n'
    "}\n\n"
    "规则：\n"
    "- 只抽文本中**确实出现**的实体，不推测、不虚构\n"
    "- character 必须有名字；location/faction/item 用其规范称呼\n"
    "- 没有的类别输出空数组 []\n"
    "- in_story_date 只在文本有明确时间线索时填，否则留空字符串\n"
    "- events 的 sequence 从 1 开始连续编号（本批内），供 event_links/entity_knowledge 引用\n"
    "- event_links 用 sequence 引用事件（不要用事件描述），描述本批事件间因果/时序关系；\n"
    "  伏笔回收用 link_type=resolves\n"
    "- entity_knowledge：当文本明确表现某角色**得知**了另一角色/事实（被告知/看到/发现）时抽取，\n"
    "  learned_at_sequence 指向得知发生的事件 sequence；不臆测角色已知什么"
)

# 段落标注前缀：让 LLM 知道当前段落在故事中的位置（批抽时用）
_SEGMENT_TEMPLATE = (
    "【第{segment_no}段 · 当前章节 {chapter}】\n{text}\n\n"
    "请抽取本段的 KG 信息（只输出 JSON）。"
)

# 批抽模板：一次 5 段，段间用分隔线标注
_BATCH_TEMPLATE = (
    "以下是连续 {n} 段小说文本（跨段一致性已由你保证——同一角色跨段出现时用同一"
    "规范名）：\n\n{joined}\n\n请合并抽取这 {n} 段的 KG 信息（只输出 JSON）。"
)


def extract_segment(provider, text: str, segment_no: int, chapter: int | None = None) -> dict:
    """单段抽取（LLM）。失败返回空骨架（不阻塞主链）。

    返回 normalize_extraction 后的规范 dict。
    """
    user = _SEGMENT_TEMPLATE.format(segment_no=segment_no, chapter=chapter or "?", text=text)
    try:
        raw = provider.chat(_EXTRACT_SYSTEM, user)
    except Exception:  # noqa: BLE001 —— 抽取失败静默兜底
        return normalize_extraction({})
    return _parse_and_normalize(raw)


def extract_batch(provider, texts: list[tuple[int, str, int | None]]) -> dict:
    """批量抽取（一次抽 N 段合并——省 LLM 调用 + 跨段一致性）。

    texts: [(segment_no, text, chapter), ...]
    返回的 entities/events 不带 segment_no（落库时由调用方补：events.segment_no
    在批内无法精确归属，统一记批首段号）。
    """
    if not texts:
        return normalize_extraction({})
    if len(texts) == 1:
        seg_no, text, chapter = texts[0]
        return extract_segment(provider, text, seg_no, chapter)
    joined = "\n\n—— 段分隔 ——\n\n".join(
        f"【第{s} 段】\n{t}" for s, t, _c in texts
    )
    user = _BATCH_TEMPLATE.format(n=len(texts), joined=joined)
    try:
        raw = provider.chat(_EXTRACT_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return normalize_extraction({})
    return _parse_and_normalize(raw)


def _parse_and_normalize(raw: str) -> dict:
    """LLM 输出 → JSON → normalize_extraction。三级兜底。"""
    if not raw:
        return normalize_extraction({})
    data = None
    # 1) 整体就是 JSON
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        pass
    # 2) 剥 think 块 + 抽 {...}（复用 provider 层同款 strip_reasoning）
    if data is None:
        from app.llm.reasoning import strip_reasoning

        cleaned = strip_reasoning(raw)
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                data = None
    if data is None:
        return normalize_extraction({})
    return normalize_extraction(data)


@dataclass
class ExtractionStats:
    """批抽落库统计（worker 上报用）。"""

    segments: int = 0
    entities_upserted: int = 0
    entities_new: int = 0
    relations_upserted: int = 0
    events_added: int = 0
    event_links_added: int = 0
    entity_knowledge_added: int = 0
    state_changes: int = 0
    foreshadows: int = 0
    #: 抽出来**疑似对话碎片**的伏笔（只留痕，不删不改账本）。
    #: 实测（2026-10-28，验收库同一份数据）：KG 抽的 19 条伏笔里 **14 条**是对话碎片
    #: ⇒ 提示词侧会剔除它们，但账本原样保留（抽取层只做 warn，不做裁决）。
    foreshadows_suspect: int = 0
    errors: list = field(default_factory=list)
