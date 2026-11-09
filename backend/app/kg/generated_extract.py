"""生成层机械抽取：只记录“这一章写了什么”，不发现新设定。

设计取舍：零 LLM、可复算、结果稳定。只认 canon 里已有的实体与关系，
新专名不自动赋身份，避免 AI 生成的错误设定被抽成事实后固化。

关系判定不再用“同段共现即关系”，而是要求句内出现明确的
称谓、归属、持有/修习或方位触发词；没有触发依据的共现降级为
`candidates`（单独表），不进入正式边。事件主语必须是 canon 实体，
通称/碎片走别名表合并；无法确定主语时保留事件并标
`subject=unresolved`，供冲突扫描显式报出。

输出统一带 source='generated'、source_chapter 与证据句，供
`generated_store` 落库、`conflict_scan` 做冲突判定。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from app.kg.layers import GENERATED

_CN_RANK = "九八七六五四三二一"
_REALM_NAMES = "炼精|炼气|炼神|铜皮|银骨|铁血|武夫|律者"

#: 生成层只记录有连续剧情意义的实体，避免「面/铁/钱」这类单字或泛化物品噪声。
GENERATED_NODE_KINDS = frozenset({"character", "faction", "power_system", "location"})

#: 机械路只处理这些关系；其余 rel 不进入正式边。
EDGE_RELS = frozenset({
    "ally", "enemy", "subordinate", "superior", "mentor", "student",
    "family", "sibling", "parent", "child", "located_at", "member_of",
    "belongs_to",
})
SYMMETRIC_RELS = frozenset({"ally", "enemy", "family", "sibling"})

#: 通称、泛称、碎片：即使 canon 里被误建成节点，也不许当事件主语或正式节点。
GENERIC_SUBJECTS = frozenset({
    "外头", "里头", "大人", "孩子", "孩子们", "小孩", "衙役", "差役",
    "众人", "有人", "那人", "此人", "对方", "所有人", "大家", "他们",
    "她们", "我们", "你们", "自己", "小的", "老的", "男子", "女子",
    "少年", "老者", "妇人", "汉子", "姑娘", "丫鬟", "小厮", "掌柜",
    "伙计", "百姓", "路人", "旁人", "外人", "来人", "几人", "一人",
})

#: 碎片名归并表。canon 里这些名字被单独建成了节点，机械路必须先归并。
SUBJECT_ALIASES = {
    "七安": "许七安",
    "铃音": "许铃音",
    "魏公": "魏渊",
    "老姜": "姜律中",
    "巡抚": "张巡抚",
    "许白嫖": "许七安",
    "许宁宴": "许七安",
    "宁宴": "许七安",
}

#: 地点名后缀。canon 里同一名字常有 faction/location 两条，按后缀优先取 location。
LOCATION_SUFFIXES = (
    "府", "楼", "州", "城", "门", "院", "仓", "巷", "街", "山", "河",
    "关", "驿", "县", "镇", "村", "寺", "观", "堂", "宫", "殿", "阁",
    "斋", "居", "庄", "园", "场", "房", "屋", "店", "铺", "坊", "市",
    "渡", "桥", "塔", "陵", "墓", "湖", "江", "岭", "谷", "洞",
)

#: 关系触发词。只有句内出现这些词，关系才成立。
REL_TRIGGERS = {
    "ally": re.compile(
        r"结盟|联盟|盟友|同盟|联手|合作|同党|一伙|同谋|共谋|交好|亲近|相善|好友|"
        r"同僚|同袍|旧交|旧识|故交|并肩|分头|接应|搭手"
    ),
    "enemy": re.compile(r"敌对|仇敌|为敌|结仇|追杀|通缉|反目|死敌|宿敌|不共戴天"),
    "subordinate": re.compile(
        r"下属|部下|属下|部属|麾下|手下|听命|受命|隶属|效忠|投靠|投奔|拜入|"
        r"旧部|旧属|老部下|听命于|受命于|奉.{0,6}之命|"
        r"在.{0,4}当差|是.{0,4}的人"
    ),
    "superior": re.compile(r"上司|上级|统领|管辖|掌管|座下|主上|旧上司|老上司"),
    "mentor": re.compile(r"师父|师傅|老师|教导|传授|拜师|授业|指点|恩师|启蒙"),
    "student": re.compile(r"弟子|学生|门生|徒弟|门下"),
    "family": re.compile(
        r"父亲|母亲|爹|娘|兄长|哥哥|弟弟|妹妹|姐姐|儿子|女儿|"
        r"妻子|丈夫|叔|伯|舅|姑|姨|堂|表|族|亲人"
    ),
    "sibling": re.compile(r"兄长|哥哥|弟弟|妹妹|姐姐|兄弟|姐妹|堂妹|堂兄|表妹|表哥"),
    "parent": re.compile(r"父亲|母亲|爹|娘|养父|养母"),
    "child": re.compile(r"儿子|女儿|养子|养女"),
    "member_of": re.compile(r"加入|隶属|效忠|投靠|归于|门下|麾下|成员|门人|弟子|当差|供职"),
    "belongs_to": re.compile(r"属于|归.{0,4}所有|是.{0,6}的人|供职于|在.{0,4}当差"),
}

#: 方位/移动触发词；located_at 必须靠它把主语和地点连起来。
LOCATIVE = r"(?:在|到|回|进|抵|往|从|至|落脚|住进|赶到|送入|送到|带到|赶往|前往)"

DEATH_MARKERS = r"(?:已死|死了|身亡|断气|咽气|身死|毙命|被杀|阵亡)"

#: 非当章时态/历史关系标记：保留边，但置信度下调，供上层决定是否当“当前关系”用。
PAST_MARKERS = re.compile(r"曾是|旧部|旧属|旧上司|老部下|老下属|当年|原先|原为|旧日")

REPORTED_HINTS = re.compile(
    r"据说|听说|传言|据传|声称|扬言|谎称|都说|传说|外头说|有人说|"
    r"闻说|听闻|传闻|如果|若是|要是|万一|假如"
)
QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”", '"': '"'}


def parse_attrs(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"value": value}
    except Exception:  # noqa: BLE001
        return {"value": str(raw)}


def parse_aliases(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    if not raw:
        return []
    text = str(raw).replace("|", ",")
    return [x.strip() for x in text.split(",") if x.strip()]


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?])", text or "")
    return [p.strip() for p in parts if p.strip()]


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def _evidence(text: str, needle: str, limit: int = 160) -> str:
    for sent in _sentences(text):
        if needle in sent:
            return sent[:limit]
    return ""


def _valid_form(form: str) -> bool:
    form = str(form or "").strip()
    if not 2 <= len(form) <= 10:
        return False
    if form in ("未提及", "不详", "未知", "无"):
        return False
    return not bool(re.search(r"[，。！？、；：“”「」『』（）()\s]", form))


def _count_forms(text: str, forms: list[str]) -> int:
    """按最长优先计数，避免「许七安」被「七安」重复计。"""
    cleaned = sorted({f for f in forms if _valid_form(f)}, key=len, reverse=True)
    if not cleaned:
        return 0
    pattern = re.compile("|".join(re.escape(f) for f in cleaned))
    return len(pattern.findall(text or ""))


def _preferred_kind(name: str, kinds: set[str]) -> str:
    if "character" in kinds:
        return "character"
    if "location" in kinds and any(name.endswith(x) for x in LOCATION_SUFFIXES):
        return "location"
    if "faction" in kinds:
        return "faction"
    if "location" in kinds:
        return "location"
    if "power_system" in kinds:
        return "power_system"
    return next(iter(kinds), "unknown")


def _looks_like_location(name: str) -> bool:
    return any(name.endswith(x) for x in LOCATION_SUFFIXES)


def _canon_index(canon_nodes: list[dict]) -> tuple[dict, dict, dict]:
    """canonical_name -> node；alias -> canonical_name；canonical -> kinds。"""
    by_name: dict[str, dict] = {}
    alias: dict[str, str] = {}
    kinds: dict[str, set[str]] = defaultdict(set)
    for node in canon_nodes:
        name = node["name"]
        kind = node.get("kind") or "unknown"
        kinds[name].add(kind)
        # 同名跨 kind 时优先保留 character 的身份信息。
        if name not in by_name or (kind == "character"
                                   and by_name[name].get("kind") != "character"):
            by_name[name] = node
        for form in [name] + parse_aliases(node.get("aliases")):
            if _valid_form(form):
                alias.setdefault(form, name)
    # 人工归并表优先级最高：碎片名必须先折到正式实体。
    for src, dst in SUBJECT_ALIASES.items():
        alias[src] = dst
    for name, node_kinds in list(kinds.items()):
        preferred = _preferred_kind(name, node_kinds)
        if preferred not in node_kinds:
            for node in canon_nodes:
                if node["name"] == name and node.get("kind") == preferred:
                    by_name[name] = node
                    break
    return by_name, alias, kinds


def _forms_for(name: str, canon_nodes: list[dict]) -> list[str]:
    forms = {name}
    for node in canon_nodes:
        if node["name"] == name:
            forms.update(parse_aliases(node.get("aliases")))
    for src, dst in SUBJECT_ALIASES.items():
        if dst == name:
            forms.add(src)
    return [f for f in forms if _valid_form(f) and f not in GENERIC_SUBJECTS]


def _collect_mentions(text: str, canon_nodes: list[dict]) -> dict[str, dict]:
    by_name, _, kinds = _canon_index(canon_nodes)
    mentioned: dict[str, dict] = {}
    for name in by_name:
        if name in SUBJECT_ALIASES or name in GENERIC_SUBJECTS:
            continue
        node = by_name[name]
        kind = _preferred_kind(name, kinds.get(name) or {node.get("kind")})
        if kind not in GENERATED_NODE_KINDS:
            continue
        count = _count_forms(text, _forms_for(name, canon_nodes))
        if count <= 0:
            continue
        mentioned[name] = {
            "name": name,
            "kind": kind,
            "attrs": parse_attrs(node.get("attrs")),
            "mentions": count,
            "evidence": _evidence(text, name),
        }
    return mentioned


def _pair_trigger(sent: str, a: str, b: str, pattern: re.Pattern,
                  window: int = 24) -> str:
    """a 在前、b 在后，触发词可落在 a 与 b 之间或 b 之后 12 字内。"""
    for ma in re.finditer(re.escape(a), sent):
        for mb in re.finditer(re.escape(b), sent):
            if ma.end() > mb.start():
                continue
            if mb.start() - ma.end() > window:
                continue
            span = sent[ma.end():min(len(sent), mb.end() + 12)]
            match = pattern.search(span)
            if match:
                return match.group(0)
    return ""


def _edge_conf(sent: str, base: float, trigger: str = "") -> float:
    """历史/过去时态的关系保留，但下调置信度。

    只看触发词所在小句，避免同段其它句子的「曾是」误伤。
    """
    scope = sent
    if trigger:
        for part in re.split(r"(?<=[。！？!?])", sent):
            if trigger in part:
                scope = part
                break
    return round(base - 0.1, 2) if PAST_MARKERS.search(scope) else base


def _edge_trigger(sent: str, a: str, b: str, rel: str,
                  kinds: dict[str, set[str]]) -> tuple[bool, str, str, float]:
    """返回 (是否成立, 触发词, 原因, 置信度)。"""
    ka = kinds.get(a) or set()
    kb = kinds.get(b) or set()

    if rel == "located_at":
        if "location" not in kb and not _looks_like_location(b):
            return False, "", "target_not_location", 0.0
        pat1 = re.compile(
            re.escape(a) + r"([^！？\n]{0,60})" + LOCATIVE
            + r"[^。！？\n]{0,6}" + re.escape(b)
        )
        pat2 = re.compile(
            re.escape(b) + r"[^。！？\n]{0,6}(?:在|有|住|等|停|是)[^。！？\n]{0,6}" + re.escape(a)
        )
        chars = {name for name, ks in kinds.items() if "character" in ks}
        for match in pat1.finditer(sent):
            # 只看定位动词所在的小句：若小句里另有 canon 人物，
            # 说明这个地点属于那个人（如「张巡抚在云州」）。
            tail = re.split(r"。", match.group(1))[-1]
            if any(other not in (a, b) and other in tail for other in chars):
                continue
            return True, match.group(0), "locative", _edge_conf(sent, 0.7, match.group(0))
        match = pat2.search(sent)
        if not match:
            return False, "", "no_locative_trigger", 0.0
        return True, match.group(0), "locative", _edge_conf(sent, 0.7, match.group(0))

    if rel in ("belongs_to", "member_of"):
        if "location" in kb or _looks_like_location(b):
            return False, "", "target_is_location", 0.0
        if rel == "member_of" and not ({"faction", "power_system"} & kb):
            return False, "", "target_not_faction", 0.0
        if rel == "belongs_to" and "character" in kb:
            return False, "", "belongs_to_character", 0.0
        trigger = _pair_trigger(sent, a, b, REL_TRIGGERS[rel])
        if not trigger:
            return False, "", "no_membership_trigger", 0.0
        return True, trigger, "membership", _edge_conf(sent, 0.75, trigger)

    if rel in ("subordinate", "superior", "mentor", "student",
               "parent", "child", "family", "sibling"):
        if "character" not in ka or "character" not in kb:
            return False, "", "non_character_endpoint", 0.0
        trigger = _pair_trigger(sent, a, b, REL_TRIGGERS[rel], window=20)
        if not trigger:
            return False, "", "no_role_trigger", 0.0
        return True, trigger, "role", _edge_conf(sent, 0.7, trigger)

    if rel in ("ally", "enemy"):
        if "character" not in ka or "character" not in kb:
            return False, "", "non_character_endpoint", 0.0
        trigger = _pair_trigger(sent, a, b, REL_TRIGGERS[rel], window=24)
        if not trigger:
            return False, "", "no_conflict_trigger", 0.0
        return True, trigger, "relation", _edge_conf(sent, 0.65, trigger)

    return False, "", "unsupported_rel", 0.0


def _collect_edges(text: str, canon_edges: list[dict], mentioned: dict[str, dict],
                   canon_nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    _, alias, kinds = _canon_index(canon_nodes)
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return [], []
    names = set(mentioned)
    out: list[dict] = []
    candidates: list[dict] = []
    seen: set[tuple] = set()
    seen_cand: set[tuple] = set()

    def _push_candidate(row: dict) -> None:
        """候选也做正反向归一并去重，避免同一对实体刷两条。"""
        if row["rel"] in SYMMETRIC_RELS:
            pair = tuple(sorted((row["from"], row["to"])))
            row["from"], row["to"] = pair
        key = (row["from"], row["to"], row["rel"], row.get("reason"))
        if key in seen_cand:
            return
        seen_cand.add(key)
        candidates.append(row)

    for edge in canon_edges:
        if (edge.get("status") or "resolved") != "resolved":
            continue
        a_raw = edge.get("from_name") or edge.get("from")
        b_raw = edge.get("to_name") or edge.get("to")
        rel = edge.get("rel_type") or edge.get("rel")
        a = alias.get(a_raw, a_raw)
        b = alias.get(b_raw, b_raw)
        if not a or not b or a == b or rel not in EDGE_RELS:
            continue
        if a not in names or b not in names:
            continue
        hits = [para[:240] for para in paragraphs if a in para and b in para]
        if not hits:
            continue

        chosen: tuple[str, str, float] | None = None
        first_fail: tuple[str, str] | None = None
        for evidence in hits:
            ok, trigger, reason, score = _edge_trigger(evidence, a, b, rel, kinds)
            if ok:
                chosen = (evidence, trigger, score)
                break
            if first_fail is None:
                first_fail = (evidence, reason)

        if chosen is None:
            evidence, reason = first_fail
            _push_candidate({
                "from": a,
                "to": b,
                "rel": rel,
                "strength": float(edge.get("strength") or 0.5),
                "confidence": 0.0,
                "trigger": "",
                "evidence": evidence,
                "reason": reason,
            })
            continue

        evidence, trigger, score = chosen
        row = {
            "from": a,
            "to": b,
            "rel": rel,
            "strength": float(edge.get("strength") or 0.5),
            "confidence": score,
            "trigger": trigger,
            "evidence": evidence,
        }

        if rel in SYMMETRIC_RELS:
            pair = tuple(sorted((a, b)))
            key = (pair, rel)
            if key in seen:
                continue
            seen.add(key)
            row["from"], row["to"] = pair
            row["direction_basis"] = "symmetric"
        else:
            key = (a, b, rel)
            if key in seen:
                continue
            seen.add(key)
            row["direction_basis"] = f"triggered:{a}->{b}"
        out.append(row)

    # 同一对实体同章最多留两条不同关系；多出来的降级为候选。
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in out:
        grouped[tuple(sorted((row["from"], row["to"])))].append(row)
    final: list[dict] = []
    for rows in grouped.values():
        rows.sort(key=lambda x: -x["confidence"])
        final.extend(rows[:2])
        for extra in rows[2:]:
            extra["reason"] = "too_many_relations_same_pair"
            extra.pop("direction_basis", None)
            _push_candidate(extra)

    final.sort(key=lambda x: -x["confidence"])
    return final[:40], candidates


def _is_reported(sent: str, start: int, end: int) -> bool:
    """断言是否处在引述/传言/假设语境。"""
    if REPORTED_HINTS.search(sent):
        return True
    for i, ch in enumerate(sent):
        if ch not in QUOTE_PAIRS:
            continue
        j = sent.find(QUOTE_PAIRS[ch], i + 1)
        seg_end = len(sent) if j < 0 else j
        if i < start < seg_end:
            return True
    return False


def _claim_patterns(alt: str) -> list[tuple[str, re.Pattern]]:
    return [
        ("death", re.compile(
            rf"(?P<subject>{alt})[^。！？\n]{{0,24}}{DEATH_MARKERS}")),
        ("faction", re.compile(
            rf"(?P<subject>{alt})[^。！？\n]{{0,12}}"
            rf"(?:加入|投靠|隶属(?:于)?|归于)(?P<object>[^，。！？\s]{{2,12}}?)"
            rf"(?:的人|门下|麾下|衙门|司|监|卫|会)?(?:[，。！？]|$)")),
        ("faction", re.compile(
            rf"(?P<subject>{alt})[^。！？\n]{{0,12}}是"
            rf"(?P<object>[^，。！？\s]{{2,12}}?)(?:的人|门下|麾下)(?:[，。！？]|$)")),
        ("realm", re.compile(
            rf"(?P<subject>{alt})[^。！？\n]{{0,16}}"
            rf"(?P<rank>[{_CN_RANK}])品\s*(?P<realm>{_REALM_NAMES})?(?:境)?")),
    ]


def extract_claims(text: str, subject_names: list[str],
                   with_rejected: bool = False):
    """机械识别三类高风险断言：死亡、势力归属、境界。

    主语只接受 canon 实体；通称/碎片不生成正式断言，改为
    subject='unresolved' + raw_subject，并进入 rejected 列表。
    """
    valid = [
        name for name in dict.fromkeys(subject_names or [])
        if name and len(name) >= 2 and name not in GENERIC_SUBJECTS
    ]
    valid.sort(key=len, reverse=True)
    generic = sorted(GENERIC_SUBJECTS, key=len, reverse=True)
    valid_patterns = _claim_patterns("|".join(re.escape(n) for n in valid)) if valid else []
    generic_patterns = _claim_patterns("|".join(re.escape(n) for n in generic))

    claims: list[dict] = []
    rejected: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()

    for sent in _sentences(text):
        covered: list[int] = []
        for kind, pattern in valid_patterns:
            for match in pattern.finditer(sent):
                subject = match.group("subject")
                if kind == "death":
                    value = "已死"
                    summary = f"{subject}被写成已死/死亡"
                elif kind == "faction":
                    value = match.group("object")
                    summary = f"{subject}被写成{value}的人"
                else:
                    realm = match.groupdict().get("realm") or ""
                    value = f"{match.group('rank')}品{realm}"
                    summary = f"{subject}被写成{value}"
                key = (kind, subject, value, sent)
                if key in seen:
                    continue
                seen.add(key)
                covered.append(match.start())
                claims.append({
                    "type": kind,
                    "subject": subject,
                    "raw_subject": subject,
                    "subject_status": "resolved",
                    "value": value,
                    "summary": summary,
                    "reported": _is_reported(sent, match.start(), match.end()),
                    "evidence": sent[:200],
                    "source": GENERATED,
                })

        for kind, pattern in generic_patterns:
            for match in pattern.finditer(sent):
                # 同一句已有合法主语的同类断言时，不再重复记 unresolved。
                if any(abs(match.start() - pos) < 8 for pos in covered):
                    continue
                raw = match.group("subject")
                if kind == "death":
                    value = "已死"
                    summary = f"未确定主语的死亡断言（原文主语：{raw}）"
                elif kind == "faction":
                    value = match.group("object")
                    summary = f"未确定主语的势力归属断言（原文主语：{raw}）"
                else:
                    realm = match.groupdict().get("realm") or ""
                    value = f"{match.group('rank')}品{realm}"
                    summary = f"未确定主语的境界断言（原文主语：{raw}）"
                key = (kind, "unresolved", value, sent)
                if key in seen:
                    continue
                seen.add(key)
                rejected.append({
                    "raw_subject": raw,
                    "reason": "generic_or_unresolved",
                    "evidence": sent[:200],
                })
                claims.append({
                    "type": kind,
                    "subject": "unresolved",
                    "raw_subject": raw,
                    "subject_status": "unresolved",
                    "value": value,
                    "summary": summary,
                    "reported": _is_reported(sent, match.start(), match.end()),
                    "evidence": sent[:200],
                    "source": GENERATED,
                })

    return (claims, rejected) if with_rejected else claims


def extract_from_text(text: str, chapter: int, canon_nodes: list[dict],
                      canon_edges: list[dict]) -> dict:
    """抽取一章生成文本，返回可直接落生成层的结构。"""
    mentioned = _collect_mentions(text, canon_nodes)
    edges, candidates = _collect_edges(text, canon_edges, mentioned, canon_nodes)
    subject_names = [name for name, node in mentioned.items()
                     if node["kind"] == "character"]
    claims, rejected = extract_claims(text, subject_names, with_rejected=True)

    nodes = [{**node, "source": GENERATED, "source_chapter": chapter}
             for node in mentioned.values()]
    events = []
    for claim in claims:
        event_type = f"{claim['type']}_claim"
        if claim.get("reported"):
            event_type += "_reported"
        events.append({
            "chapter": chapter,
            "event_type": event_type,
            "summary": claim["summary"],
            "subject": claim["subject"],
            "raw_subject": claim.get("raw_subject"),
            "subject_status": claim.get("subject_status") or "resolved",
            "object": claim["value"] if claim["type"] != "realm" else None,
            "evidence": claim["evidence"],
            "source": GENERATED,
        })
    return {
        "chapter": chapter,
        "nodes": nodes,
        "edges": edges,
        "events": events,
        "claims": claims,
        "candidates": candidates,
        "rejected_subjects": rejected,
    }
