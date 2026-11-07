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
    - 非法枚举值**不再静默丢**：保留到 `rejected`（含 stage/reason/原始值），
      entities 里非法 kind 的条目也保留一份原始内容；events 里非法 event_type
      归一为 incident，但保留 raw_event_type
    - strength 截断到 [0,1]
    - 缺键字段按类型默认值兜底（不阻塞落库）
    """
    if not isinstance(raw, dict):
        return {"entities": [], "relations": [], "events": [], "event_links": [],
                "entity_knowledge": [], "state_changes": [], "foreshadows": [],
                "rejected": []}

    out = {
        "entities": [],
        "relations": [],
        "events": [],
        "event_links": [],
        "entity_knowledge": [],
        "state_changes": [],
        "foreshadows": [],
        #: 非法枚举值的落盘出口。调用方写 JSONL 即可查询（见 experiments 的
        #: kg_build.py：每批把 rejected 追加到 rejects.jsonl，字段 chapter/reason）。
        "rejected": [],
    }

    # —— entities ——
    for e in _as_list(_pick(raw, _FIELD_ALIASES["entities"])):
        if not isinstance(e, dict):
            continue
        kind = _as_str(_pick(e, _ENTITY_ALIASES["kind"])) or "character"
        name = _as_str(_pick(e, _ENTITY_ALIASES["canonical_name"]))
        if kind not in VALID_ENTITY_KINDS:
            # 保留 + 记录：不再静默丢。原始 kind 与名字都进 rejected，便于回捞。
            out["rejected"].append({"stage": "entity", "reason": "invalid_kind",
                                    "raw_kind": kind, "name": name,
                                    "raw": e})
            continue
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
        if not a or not b:
            # 主体/客体缺失不是枚举问题，属结构性无效，仍按原样丢弃。
            continue
        if rt not in VALID_REL_TYPES:
            # 非法关系类型：保留 + 记录（含两端），便于事后回捞与统计漂移分布。
            out["rejected"].append({"stage": "relation", "reason": "invalid_rel_type",
                                    "raw_rel_type": rt, "from": a, "to": b, "raw": r})
            continue
        if a == b:
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
        raw_et = None
        if et not in VALID_EVENT_TYPES:
            # 非法 event_type：归一为 incident 以便落库，但保留原始值并记录，
            # 避免「模型总写 battle_scene 而系统永远只看到 incident」这种静默漂移。
            raw_et = et
            out["rejected"].append({"stage": "event", "reason": "invalid_event_type",
                                    "raw_event_type": et,
                                    "summary": _as_str(_pick(ev, _EVENT_ALIASES["summary"]))[:200],
                                    "raw": ev})
            et = "incident"
        summary = _as_str(_pick(ev, _EVENT_ALIASES["summary"]))
        item = {
            "sequence": _as_int(_pick(ev, _EVENT_ALIASES["sequence"])),  # 批内序号，供 links/knowledge 引用
            "chapter": _as_int(_pick(ev, _EVENT_ALIASES["chapter"])),
            "event_type": et,
            "summary": summary[:500],
            "subject": _as_str(_pick(ev, _EVENT_ALIASES["subject"])),
            "verb": _as_str(_pick(ev, _EVENT_ALIASES["verb"]))[:64],
            "object": _as_str(_pick(ev, _EVENT_ALIASES["object"])),
            "location": _as_str(_pick(ev, _EVENT_ALIASES["location"])),
            "in_story_date": _as_str(_pick(ev, _EVENT_ALIASES["in_story_date"]))[:64],
        }
        if raw_et is not None:
            item["raw_event_type"] = raw_et
        out["events"].append(item)

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
    "【kind 边界（六类固定，不得新增类别）】\n"
    "判定顺序：具体的人 → 具体的物件 → 有成员能整体行动的组织 → 可修炼/可晋级的体系 →"
    "约束所有人的成文规则 → 可出入的具体场所。命中即停，不要六类都给一遍。\n"
    "- character = 具体的个人：有名有姓，或有唯一指代。\n"
    "  是：许七安、魏渊、陈府尹（ch2 姓+官职，指一个人）、临安公主（ch2 唯一指代）。\n"
    "  不是：刑部郎中（ch23 无姓官衔，指谁不唯一）、老爷（ch18 泛称）、我（代词）。\n"
    "  官职与身份只写进 attrs：{\"office\": \"刑部郎中\", \"office_type\": \"职官|散衔|泛称\"}；\n"
    "  不要因为带官职就判成 faction，也不要把纯官衔单独建节点（本 schema 没有官职档）。\n"
    "- item = 具体的物件/实物：刀、丹药、法器、银钱、凭证、秘籍实物。\n"
    "  是：镇北王刀、炼金术秘籍、税银。\n"
    "  不是：炼金术（术法体系）、望气术（能力）、大奉律法（成文规则）。\n"
    "- faction = 有成员、有立场、能作为整体发出动作的组织/机构/门派/教派/朝廷部门/军队编制。\n"
    "  判据：原文里有「说/查/缉拿/下令/表态/派人」这类整体动作，或有编制（长官+属员）。\n"
    "  是：司天监（ch6「我们司天监不干涉朝政」）、"
    "御刀卫（ch11「御刀卫说当晚附近没有可疑人物出没」）、\n"
    "      云鹿书院（ch17「儒家圣人的大弟子创立」）、道门（ch10「道门看不起佛门」）、\n"
    "      京兆府衙门（ch3「这只是京兆府衙门的猜测」）、"
    "府衙（ch10「上次在府衙…炼成假银」）。\n"
    "  不是：桂月楼（ch8 只是出入场所）、京城（地理范围）。\n"
    "  attrs 至少给 {\"type\": \"衙门|门派|教派|军队|书院|家族|国家\", "
    "\"members\": [\"已知成员名\"]}。\n"
    "- power_system = 可修炼、可施展、可晋级的体系，含境界/术法/职业体系。\n"
    "  是：炼金术（ch25「言传身教…一年半载就能入门」）、"
    "望气术（ch30「术士最基础的能力」）、\n"
    "      武夫体系的炼精境（ch17）、练气境（ch18）。\n"
    "  不是：道门/佛门（是教派，有成员有立场 → faction；它们修的法门才是 power_system）、\n"
    "      京察（ch11「大奉京官考核制度，三年一查」是制度 → worldrule）、\n"
    "      九品到一品的官阶（是官制 → worldrule）。\n"
    "  attrs 至少给 {\"system\": \"体系名\", \"tier\": \"文本提到的品阶/境界\", "
    "\"belongs_to\": \"所属势力或职业\"}。\n"
    "- worldrule = 约束所有人的成文规则/制度/律法/普遍规律；不修炼、无成员、非物件。\n"
    "  是：大奉律法（ch6「大奉律法规定…」）、京察（ch11）、三百年定律（ch9）。\n"
    "  不是：炼金术（术法体系）、某家的家规（属该家族的内部设定）。\n"
    "- location = 具体可出入的场所/地理范围。\n"
    "  是：京城、长乐县、桂月楼（ch8 酒楼）。\n"
    "  不是：司天监（有监正与弟子，能整体表态 → faction）、"
    "府衙（有吏员、能办案 → faction）。\n"
    "【三条硬规则】\n"
    "1. 不要只按尾字判 kind。「府/院/司/卫/楼/县/州」这类尾字既可能是场所也可能是机构，"
    "看它有没有成员和整体动作；只有「XX术/XX法/XX功/XX诀/XX境」可作 power_system 的强提示。\n"
    "2. 一个词只取一个 kind。同一 canonical_name 不要同时输出 faction 和 location"
    "（如「司天监」不要既 faction 又 location）；拿不准时取判定顺序更靠前的一类。\n"
    "3. 取主指称：同一实体多个称呼时，canonical_name 用全书最常用的规范称呼，其余放 aliases。\n"
    "【event_type 触发条件（按实际情节选，禁止一律 incident）】\n"
    "- battle：武力冲突/斗法/交手，有一方动手\n"
    "- death：角色死亡或确认死亡\n"
    "- revelation：真相/身份/阴谋被揭开，认知发生反转\n"
    "- decision：角色做出明确抉择并产生后续影响\n"
    "- foreshadow_plant：埋下未解释的线索/物件/预言\n"
    "- foreshadow_payoff：先前线索在本段被回收或点明\n"
    "- romance_milestone：感情关系出现阶段性推进（表白/定情/婚约）\n"
    "- time_marker：明确的时间跳转或时间点交代（如「三日后」「贞观三年秋」）\n"
    "- incident：以上都不符合的普通情节推进（兜底，占比应最低）\n"
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


# ── 输出上限说明（2026-11-07）──────────────────────────────────────────────
# 本模块三个抽取入口（extract_segment / extract_batch / extract_targeted）都只调
# provider.chat(system, user)，**不传 max_tokens**：上限统一由 provider 侧决定，
# 即 backend/config/providers.yaml 的 `distiller.max_tokens`（settings.distiller_max_tokens，
# 兜底值在 app/config.py）。改抽取上限请改那两处，不要在这里加参数。
# 该值 2026-11-07 由 8192 抬到 32768：大奉 30 章三路对照中 deepseek / qwen 都出现
# 30/30 章在 8192 触顶（finish=length）后重试，单章输出文本最长 13,782 字、
# completion 峰值 32,805 token（reasoning 占 71%）。32768 ≈ 4× 旧上限、约 2× 最长
# 单章输出，给 reasoning 留余量；glm-4-flash / deepseek-v4.1-flash 实测均接受。
# 原则：遇到截断先抬上限取全结果，不要靠缩短输入或反复重试去将就。


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


# 定向二遍抽取：主抽里 faction / power_system 为空时的补齐通道。
# 只跑这两类，prompt 更窄、约束更硬，避免和人物抽取抢注意力。
_TARGETED_SYSTEM = (
    "你是小说知识图谱的**势力与体系**专项抽取器。只抽两类实体，**只输出 JSON**，"
    "不要解释、不要 markdown 代码块。\n\n"
    "输出格式：\n"
    "{\n"
    '  "entities": [\n'
    '    {"kind": "faction", "canonical_name": "势力名", '
    '"aliases": ["别名"], "attrs": {"type": "衙门|门派|教派|军队|书院|国家", '
    '"members": ["成员名"], "stance": "立场或目标"}},\n'
    '    {"kind": "power_system", "canonical_name": "体系名", '
    '"aliases": ["别名"], "attrs": {"system": "体系名", "tier": "品阶/境界", '
    '"belongs_to": "所属势力或职业"}}\n'
    "  ],\n"
    '  "relations": [{"from": "成员名", "to": "势力名", "rel_type": "belongs_to", '
    '"strength": 0.8}]\n'
    "}\n\n"
    "规则：\n"
    "- faction 必须是**有成员、能作为整体发出动作的组织/机构/门派/教派/朝廷部门/军队编制**"
    "（司天监、御刀卫、打更人、户部、云鹿书院、道门、佛门、京兆府衙门、府衙、万妖国）。"
    "单个散人不是 faction。\n"
    "- faction 判据：原文里有「说/查/缉拿/下令/表态/派人」这类整体动作，"
    "或有编制（长官+属员）。只有场所、没有成员与整体动作的（桂月楼、京城）不是 faction。\n"
    "- power_system 必须是**可修炼/可施展/可晋级的体系或法门**"
    "（术士体系、武夫体系、炼金术、望气术、练气境、炼精境）。"
    "具体的刀剑丹药法器**不是** power_system。\n"
    "- 「XX术/XX法/XX功/XX诀/XX境」可作 power_system 的强提示；"
    "但「府/院/司/卫/楼/县/州」这类尾字**不能**据此判类，要看有没有成员和整体动作。\n"
    "- **教派/宗门是 faction，不是 power_system**（道门、佛门是 faction；"
    "它们修的法门才是 power_system）。\n"
    "- **制度/律法/官制不是 power_system**（京察、大奉律法、九品到一品的官阶都不是 "
    "power_system；本轮不抽 worldrule，遇到直接跳过）。\n"
    "- 同一 canonical_name 只出一类，不要同时给 faction 和 power_system。\n"
    "- 只抽文本中**确实出现**的，文本没有就返回空数组 []，禁止编造。\n"
    "- relations 只允许 belongs_to（成员→势力、体系→上级势力/职业）。\n"
)

_TARGETED_USER = (
    "【第{segment_no}段 · 当前章节 {chapter}】\n{text}\n\n"
    "请只抽取本段的 faction（势力）与 power_system（体系），只输出 JSON。"
)


def extract_targeted(provider, text: str, segment_no: int, chapter: int | None = None,
                     kinds: tuple[str, ...] = ("faction", "power_system")) -> dict:
    """定向二遍抽取（只抽 faction / power_system）。

    触发条件由调用方控制：主抽结果里这两类为空时才调用，避免全量翻倍成本。
    返回 normalize_extraction 后的规范 dict（entities 已按 kinds 过滤）。
    """
    user = _TARGETED_USER.format(segment_no=segment_no, chapter=chapter or "?", text=text)
    try:
        raw = provider.chat(_TARGETED_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return normalize_extraction({})
    out = _parse_and_normalize(raw)
    keep = set(kinds)
    out["entities"] = [e for e in out["entities"] if e["kind"] in keep]
    # 关系只保留两端都在保留实体里的
    names = {e["canonical_name"] for e in out["entities"]}
    out["relations"] = [r for r in out["relations"] if r["from"] in names or r["to"] in names]
    # 定向通道不产出事件/伏笔，清空避免污染主抽统计
    for k in ("events", "event_links", "entity_knowledge", "state_changes", "foreshadows"):
        out[k] = []
    return out


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
    errors: list = field(default_factory=list)
