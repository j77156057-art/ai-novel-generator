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

from app.kg import canon_source
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

#: 主宾门的四类硬拒。命中即不产正式边，也不落 candidate
#: （「拒绝」不等于「降级为候选」，候选只留给端点可疑、需人工复核的行）。
HARD_REJECT_REASONS = frozenset({"negated", "question", "hypothetical", "reported"})
#: 主宾门给出的全部拒绝原因（含端点不符 / 主宾解不出）。
ATTRIBUTION_REASONS = HARD_REJECT_REASONS | {"endpoint_mismatch", "weak_subject_object"}

#: 小句边界。刻意不含「、」：并列名单要留在同一小句里才能判。
_CLAUSE_DELIMS = "，。！？；：\n“”「」『』（）()"
_QUESTION = re.compile(
    r"[？?]|难道|岂(?:能|敢|会|有)|何曾|怎能|怎么可能|是否|能不能|会不会"
)
_HYPOTHETICAL = re.compile(r"如果|若是|要是|万一|假如|倘若|假设|除非")
_REPORTED_CN = re.compile(
    r"据说|听说|传言|据传|声称|扬言|谎称|都说|传说|外头说|有人说|"
    r"闻说|听闻|传闻|相传"
)
_NEGATED = re.compile(
    r"没有|没(?!走|过|几|想)|未(?:曾|能|免)|并非|不是|不再|不曾|"
    r"别(?!人|处)|莫(?:非)?|勿|岂|无法|不能|不会|不肯|不予"
)

#: 相互性动词：双方必须同小句并列，否则触发词修饰的是别人。
_MUTUAL_TRIGGERS = (
    "并肩", "联手", "结盟", "联盟", "同盟", "合作", "交好", "相善", "亲近",
    "搭手", "接应", "分头", "敌对", "为敌", "结仇", "反目",
)
#: 关系名词：Y 是关系名，双方要按「A 和 B 是 Y」「Y 的指代」解。
_REL_NOUN_TRIGGERS = (
    "同僚", "同袍", "旧交", "旧识", "故交", "好友", "盟友", "同党",
    "一伙", "同谋", "共谋", "仇敌", "死敌", "宿敌",
)
#: 后置角色词：`X 在 Y 麾下` → Y 是持有者、X 是下属。
_POSTPOSITION_TRIGGERS = (
    "麾下", "手下", "部下", "下属", "属下", "部属", "旧部", "旧属",
    "老部下", "门下",
)
#: 及物角色动词：主语→宾语单向绑定。
_ROLE_VERB_TRIGGERS = (
    "教导", "传授", "拜师", "授业", "指点", "启蒙",
    "投靠", "投奔", "效忠", "听命", "隶属", "受命",
)
#: 「两位同僚」「他们二人」这类同位语，只有前一小句的并列名单能解。
_APPELLATION = re.compile(r"^(?:这)?(?:两位|二位|两人|二人|俩|他们|她们)")


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


def _count_forms(text: str, forms: list[str], chapter: int | None = None) -> int:
    """按最长优先计数，避免「许七安」被「七安」重复计。

    交给 canon_source 统一做：cleaned 源下带上下文约束的写法（「白莲」）在
    「白莲花」里不会被计数，``except_chapters`` 里另有所指的写法也不计；
    legacy 源下索引为空，结果与纯正则计数一致。
    """
    cleaned = sorted({f for f in forms if _valid_form(f)}, key=len, reverse=True)
    if not cleaned:
        return 0
    return canon_source.count_forms(text or "", cleaned, chapter)


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


def _collect_mentions(text: str, canon_nodes: list[dict],
                      chapter: int | None = None) -> dict[str, dict]:
    by_name, _, kinds = _canon_index(canon_nodes)
    mentioned: dict[str, dict] = {}
    for name in by_name:
        if name in SUBJECT_ALIASES or name in GENERIC_SUBJECTS:
            continue
        node = by_name[name]
        kind = _preferred_kind(name, kinds.get(name) or {node.get("kind")})
        if kind not in GENERATED_NODE_KINDS:
            continue
        count = _count_forms(text, _forms_for(name, canon_nodes), chapter)
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
                  window: int = 24) -> re.Match | None:
    """a 在前、b 在后，触发词可落在 a 与 b 之间或 b 之后 12 字内。

    返回触发词的 match（带绝对位置），供主宾门判断它修饰的是谁。
    """
    for ma in re.finditer(re.escape(a), sent):
        for mb in re.finditer(re.escape(b), sent):
            if ma.end() > mb.start():
                continue
            if mb.start() - ma.end() > window:
                continue
            span_end = min(len(sent), mb.end() + 12)
            match = pattern.search(sent, ma.end(), span_end)
            if match:
                return match
    return None


def _trigger_kind(trigger: str) -> str:
    """触发词按句法行为分型；分不出型的（亲属词等）不介入主宾门。"""
    if "当差" in trigger or trigger.endswith("的人"):
        return "copula"
    for word in _MUTUAL_TRIGGERS:
        if word in trigger:
            return "mutual"
    for word in _REL_NOUN_TRIGGERS:
        if word in trigger:
            return "noun"
    for word in _POSTPOSITION_TRIGGERS:
        if word in trigger:
            return "postposition"
    for word in _ROLE_VERB_TRIGGERS:
        if word in trigger:
            return "verb"
    return "other"


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    start = 0
    for match in re.finditer(r"[。！？!?]", text):
        if match.end() <= pos:
            start = match.end()
        else:
            return start, match.end()
    return start, len(text)


def _clause_span(text: str, pos: int) -> tuple[int, int]:
    start = pos
    while start > 0 and text[start - 1] not in _CLAUSE_DELIMS:
        start -= 1
    end = pos
    while end < len(text) and text[end] not in _CLAUSE_DELIMS:
        end += 1
    return start, end


def _prev_clause_span(text: str, start: int) -> tuple[int, int] | None:
    """紧邻的前一小句。只允许跨逗号承前省略，跨句号不回溯。"""
    cursor = start
    while cursor > 0 and text[cursor - 1] in "、；：":
        cursor -= 1
    if cursor == 0 or text[cursor - 1] != "，":
        return None
    end = cursor - 1
    begin = end
    while begin > 0 and text[begin - 1] not in _CLAUSE_DELIMS:
        begin -= 1
    return begin, end


def _mentions_in(text: str, span: tuple[int, int] | None, form_re: re.Pattern,
                 forms: dict[str, str]) -> list[tuple[int, int, str]]:
    """span 内的 canon 专名，按出现位置排序，返回规范名。"""
    if span is None or form_re is None:
        return []
    start, end = span
    if end <= start:
        return []
    return [(m.start(), m.end(), forms.get(m.group(0), m.group(0)))
            for m in form_re.finditer(text, start, end)]


def _coordinated(text: str, a: str, b: str) -> bool:
    """a、b 是否以「和/与/跟/同/、」直接并列。"""
    ea, eb = re.escape(a), re.escape(b)
    gap = r"[^，。！？；：\n]{0,4}?"
    return bool(re.search(rf"(?:{ea}[和与跟同、]{gap}{eb}|{eb}[和与跟同、]{gap}{ea})",
                          text))


def _coordination_pair(text: str, span: tuple[int, int] | None,
                       form_re: re.Pattern,
                       forms: dict[str, str]) -> set[str]:
    """span 内 `X 和 Y` 形式的并列名单（只取第一组两个专名）。"""
    items = _mentions_in(text, span, form_re, forms)
    for idx, (_, end, first) in enumerate(items):
        if end >= len(text) or text[end] not in "和与跟、":
            continue
        for start, _, second in items[idx + 1:]:
            if 0 <= start - end <= 2 and second != first:
                return {first, second}
    return set()


def _quote_speaker(text: str, clause_start: int, form_re: re.Pattern,
                   forms: dict[str, str]) -> str:
    """「X 道：『…』」里的 X；引语内 `我` 只有这一种稳定解法。"""
    quote = max(text.rfind("“", 0, clause_start + 1),
                text.rfind("「", 0, clause_start + 1))
    if quote < 0:
        return ""
    hits = _mentions_in(text, (max(0, quote - 24), quote), form_re, forms)
    return hits[-1][2] if hits else ""


def _left_actor(text: str, clause_start: int, tpos: int, form_re: re.Pattern,
                forms: dict[str, str]) -> str:
    """触发词左侧的施事：代词优先，其次小句内最近专名，最后前一小句。"""
    tail = text[max(clause_start, tpos - 2):tpos]
    if "我" in tail or "俺" in tail:
        return _quote_speaker(text, clause_start, form_re, forms) or "我"
    hits = _mentions_in(text, (clause_start, tpos), form_re, forms)
    if hits:
        return hits[-1][2]
    hits = _mentions_in(text, _prev_clause_span(text, clause_start), form_re, forms)
    return hits[-1][2] if hits else ""


def _right_actor(text: str, clause_start: int, tpos: int, tend: int,
                 form_re: re.Pattern, forms: dict[str, str]) -> str:
    """触发词右侧的受事：代词回指同小句左侧专名，否则取 8 字内最近专名。"""
    tail = text[tend:min(len(text), tend + 2)]
    if "她" in tail or "他" in tail or "你" in tail:
        hits = _mentions_in(text, (clause_start, tpos), form_re, forms)
        return hits[-1][2] if hits else ""
    hits = _mentions_in(text, (tend, min(len(text), tend + 8)), form_re, forms)
    return hits[0][2] if hits else ""


def _attribution_gate(text: str, a: str, b: str, rel: str, trigger: str,
                      tpos: int, tend: int,
                      ctx: tuple[re.Pattern, dict[str, str]]) -> tuple[bool, str]:
    """减法式主宾门：只回答「该不该拒」，从不新增配对。

    先做四类语境硬拒（否定/疑问/假设/转述），再按触发词类型解出
    (主语, 宾语) 并要求它与 canon 边相等；不相等只拒，不改边。
    """
    form_re, forms = ctx
    s_start, s_end = _sentence_span(text, tpos)
    sentence = text[s_start:s_end]
    c_start, c_end = _clause_span(text, tpos)
    clause = text[c_start:c_end]
    before = clause[:max(0, tpos - c_start)]

    if _QUESTION.search(sentence):
        return False, "question"
    if _HYPOTHETICAL.search(sentence):
        return False, "hypothetical"
    if _REPORTED_CN.search(sentence):
        return False, "reported"
    if _NEGATED.search(before):
        return False, "negated"

    kind = _trigger_kind(trigger)
    if kind == "other":
        return True, ""

    if kind == "mutual":
        # 相互性动词只有「A 和 B」同小句并列时才断言 a、b 这一对。
        return (True, "") if _coordinated(clause, a, b) else (False, "endpoint_mismatch")

    if kind == "noun":
        if _APPELLATION.match(clause):
            pair = _coordination_pair(text, _prev_clause_span(text, c_start),
                                      form_re, forms)
            return (True, "") if pair and {a, b} == pair else (False, "endpoint_mismatch")
        # A 和 B 是 Y：Y 作谓语，a、b 就是 Y 的双方。
        if _coordinated(clause, a, b) and re.search(r"是[^，。！？；：\n]{0,4}$", before):
            return True, ""
        # X 和诸位 Y：Y 只是并列项之一，名单外的人无从确认。
        if re.search(r"(?:和|与|跟|及)(?:诸位|各位|众|一众)?$", before):
            return False, "endpoint_mismatch"
        # 裸名词：持有者取小句内左侧专名，指代取全句唯一其它专名。
        holders = {name for _s, _e, name in
                   _mentions_in(text, (c_start, tpos), form_re, forms)}
        others = ({name for _s, _e, name in
                   _mentions_in(text, (s_start, s_end), form_re, forms)} - holders)
        if len(others) == 1:
            ref = next(iter(others))
            if (a in holders and b == ref) or (b in holders and a == ref):
                return True, ""
        return False, "endpoint_mismatch"

    if kind == "postposition":
        holders = _mentions_in(text, (c_start, tpos), form_re, forms)
        if not holders:
            return False, "endpoint_mismatch"
        owner = holders[-1][2]
        subs = _mentions_in(text, (c_start, holders[-1][0]), form_re, forms)
        if not subs:
            subs = _mentions_in(text, _prev_clause_span(text, c_start), form_re, forms)
        if not subs:
            return False, "weak_subject_object"
        return (True, "") if {subs[-1][2], owner} == {a, b} else (False, "endpoint_mismatch")

    if kind == "copula":
        if "当差" in trigger:
            # 「当差的」是职位/仆从，人和机构都不在触发词里，不可绑定。
            owner = _mentions_in(text, (tend, min(len(text), tend + 6)), form_re, forms)
            subject = _left_actor(text, c_start, tpos, form_re, forms)
            if owner and subject and (subject, owner[0][2]) == (a, b):
                return True, ""
            return False, "endpoint_mismatch"
        inner = [forms.get(m.group(0), m.group(0)) for m in form_re.finditer(trigger)]
        hits = _mentions_in(text, (c_start, tpos), form_re, forms)
        subject = hits[-1][2] if hits and (tpos - hits[-1][1]) <= 2 else ""
        if subject and inner and (subject, inner[0]) == (a, b):
            return True, ""
        return False, "endpoint_mismatch"

    # kind == "verb"：及物角色动词要求主宾严格对上 canon 边方向。
    subject = _left_actor(text, c_start, tpos, form_re, forms)
    obj = _right_actor(text, c_start, tpos, tend, form_re, forms)
    if not subject or not obj:
        return False, "weak_subject_object"
    return (True, "") if (subject, obj) == (a, b) else (False, "endpoint_mismatch")


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
                  kinds: dict[str, set[str]],
                  ctx: tuple[re.Pattern, dict[str, str]] | None = None,
                  ) -> tuple[bool, str, str, float]:
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
        match = _pair_trigger(sent, a, b, REL_TRIGGERS[rel])
        if not match:
            return False, "", "no_membership_trigger", 0.0
        return True, match.group(0), "membership", _edge_conf(sent, 0.75, match.group(0))

    if rel in ("subordinate", "superior", "mentor", "student",
               "parent", "child", "family", "sibling"):
        if "character" not in ka or "character" not in kb:
            return False, "", "non_character_endpoint", 0.0
        match = _pair_trigger(sent, a, b, REL_TRIGGERS[rel], window=20)
        if not match:
            return False, "", "no_role_trigger", 0.0
        trigger = match.group(0)
        if ctx is not None:
            allowed, reason = _attribution_gate(
                sent, a, b, rel, trigger, match.start(), match.end(), ctx)
            if not allowed:
                return False, trigger, reason, 0.0
        return True, trigger, "role", _edge_conf(sent, 0.7, trigger)

    if rel in ("ally", "enemy"):
        if "character" not in ka or "character" not in kb:
            return False, "", "non_character_endpoint", 0.0
        match = _pair_trigger(sent, a, b, REL_TRIGGERS[rel], window=24)
        if not match:
            return False, "", "no_conflict_trigger", 0.0
        trigger = match.group(0)
        if ctx is not None:
            allowed, reason = _attribution_gate(
                sent, a, b, rel, trigger, match.start(), match.end(), ctx)
            if not allowed:
                return False, trigger, reason, 0.0
        return True, trigger, "relation", _edge_conf(sent, 0.65, trigger)

    return False, "", "unsupported_rel", 0.0


def _collect_edges(text: str, canon_edges: list[dict], mentioned: dict[str, dict],
                   canon_nodes: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """返回 (正式边, 候选, 硬拒)。硬拒行只描述、不落边也不进候选。"""
    _, alias, kinds = _canon_index(canon_nodes)
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return [], [], []
    names = set(mentioned)
    forms = {name: name for name in names}
    forms.update({form: canon for form, canon in alias.items() if canon in names})
    form_re = re.compile("|".join(
        re.escape(form) for form in sorted(forms, key=len, reverse=True)))
    ctx = (form_re, forms)
    out: list[dict] = []
    candidates: list[dict] = []
    rejected: list[dict] = []
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
        first_fail: tuple[str, str, str] | None = None
        gate_fail: tuple[str, str, str] | None = None
        for evidence in hits:
            ok, trigger, reason, score = _edge_trigger(evidence, a, b, rel, kinds, ctx)
            if ok:
                chosen = (evidence, trigger, score)
                break
            if reason in ATTRIBUTION_REASONS and gate_fail is None:
                gate_fail = (evidence, trigger, reason)
            if first_fail is None:
                first_fail = (evidence, trigger, reason)

        if chosen is None:
            # 有触发词、但被主宾门拒掉的段落，比「这段没触发词」更能说明原因。
            evidence, trigger, reason = gate_fail or first_fail
            row = {
                "from": a,
                "to": b,
                "rel": rel,
                "strength": float(edge.get("strength") or 0.5),
                "confidence": 0.0,
                "trigger": trigger,
                "evidence": evidence,
                "reason": reason,
            }
            if reason in HARD_REJECT_REASONS:
                rejected.append(row)
            else:
                _push_candidate(row)
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
    return final[:40], candidates, rejected


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
    mentioned = _collect_mentions(text, canon_nodes, chapter)
    edges, candidates, rejected_edges = _collect_edges(
        text, canon_edges, mentioned, canon_nodes)
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
        "rejected_edges": rejected_edges,
    }
