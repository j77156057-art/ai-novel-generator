"""故事意向的「抽取 + 追问计划」层（纯逻辑，不碰 DB / FastAPI / 生成链路）。

为什么单独一层：原来的引导是一张固定步骤表——无论用户说「我要写小说」还是
「写个科幻悬疑」，都按 corpus → genre → style → length → chapters 的顺序念。
这层把「用户已经说了什么」和「这个题材还必须问什么」分开算：

1. `read_signals()` —— 从用户任意一句话里抽出可落库的信号：题材、视角/节奏/
   文风、主角姓名、核心冲突、全书篇幅、更新节奏、世界观约束、金手指边界。
2. `plan_steps()` —— 按**题材族**决定还要问哪几件、先问哪件。悬疑先问谜团，
   仙侠先问力量体系，历史先问时代落点；未知题材走通用集合。
3. `defaults()` / `question()` / `vague_message()` —— 模糊回答给 2–3 个具体
   方向，不直接套默认；用户明确说「按默认来 / 别问了」时才用默认值补齐。

边界：这里不写死任何一本书的书名、人名或情节；题材词只用于「族 → 该问哪几件」
的映射，换任何一本书都成立。
"""

from __future__ import annotations

import re
from typing import Any

#: 故事塑造阶段会问的步骤。genre 由原流程保留，其余由本模块按题材动态插入。
STORY_STEPS = ("genre", "pov", "premise", "protagonist", "world", "power", "scale")

#: 题材 → 题材族。族只决定「问什么、先问什么」，不决定具体剧情。
FAMILY_OF_GENRE = {
    "科幻": "speculative",
    "悬疑": "mystery",
    "奇幻": "fantasy",
    "仙侠": "fantasy",
    "言情": "romance",
    "历史": "history",
    "都市": "urban",
    "游戏": "game",
}

#: 每个族的追问顺序。`scale`（篇幅/更新）统一放最后。
#: 顺序即理由：先问这个题材里最容易写崩的那一项。
_FAMILY_ORDER = {
    "mystery": ["premise", "pov", "protagonist", "world", "scale"],
    "speculative": ["world", "premise", "protagonist", "power", "pov", "scale"],
    "fantasy": ["power", "world", "protagonist", "premise", "pov", "scale"],
    "game": ["power", "world", "protagonist", "premise", "pov", "scale"],
    "romance": ["protagonist", "premise", "pov", "world", "scale"],
    "history": ["world", "protagonist", "premise", "pov", "scale"],
    "urban": ["premise", "protagonist", "world", "pov", "scale"],
    "generic": ["pov", "premise", "protagonist", "world", "scale"],
}

#: 题材同义词。只在短句里生效，避免一整段剧情梗概里的「现实」被当成都市。
_GENRE_ALIASES = {
    "科幻": ("末世", "赛博", "机甲", "星际", "废土"),
    "悬疑": ("推理", "探案", "刑侦", "犯罪", "惊悚"),
    "奇幻": ("玄幻", "魔法", "异世界", "史诗幻想"),
    "仙侠": ("修真", "修仙", "武侠", "飞升", "门派"),
    "历史": ("古代", "权谋", "王朝", "架空历史"),
    "言情": ("爱情", "感情线", "恋爱", "甜宠", "虐恋"),
    "都市": ("职场", "都市情感", "现实题材"),
    "游戏": ("网游", "副本", "系统流", "升级流", "游戏流"),
}

_POV_PATTERNS = (
    ("第一人称", ("第一人称", "第一视角", "我视角")),
    ("第三人称", ("第三人称", "第三视角", "他视角")),
    ("双视角", ("双视角", "双线叙事", "男女主双视角")),
    ("多视角", ("多视角", "群像", "POV")),
)

_PACE_PATTERNS = (
    ("快节奏", ("快节奏", "节奏快", "爽文", "强节奏", "高强度")),
    ("慢热", ("慢热", "慢节奏", "日常流", "细腻铺陈")),
    ("强情节", ("强情节", "剧情流", "情节驱动")),
)

_TONE_PATTERNS = (
    ("轻松", ("轻松", "搞笑", "沙雕", "轻喜剧")),
    ("黑暗", ("黑暗", "压抑", "严肃向", "灰暗")),
    ("治愈", ("治愈", "温暖", "温馨")),
    ("虐", ("虐心", "虐恋", "刀子")),
    ("爽", ("爽点", "爽文", "打脸")),
)

_WORLD_WORDS = ("世界观", "世界设定", "世界规则", "背景设定", "时代背景", "舞台设定")
_POWER_WORDS = ("金手指", "能力体系", "力量体系", "异能", "功法", "系统流", "外挂")

_STYLE_KEYS: tuple[str, ...] = ()

#: 模糊回答：在故事类问题上不套默认，改为给方向。
VAGUE_WORDS = (
    "随便",
    "都行",
    "都可以",
    "你定",
    "你看着办",
    "听你的",
    "不知道",
    "不清楚",
    "没想好",
    "没概念",
    "无所谓",
    "看你",
)

#: 明确要求「别问了 / 按默认来」：直接补齐并推进。
BYPASS_WORDS = (
    "按默认来",
    "按默认",
    "用默认",
    "都按默认",
    "全都默认",
    "别问了",
    "不要问了",
    "不用问了",
    "别再问",
    "直接开写",
    "直接生成",
    "你全定了",
    "剩下的你定",
)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _cn_int(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    table = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if raw in table:
        return table[raw]
    if raw == "十":
        return 10
    return None


def family_of_genre(genre: str | None) -> str:
    return FAMILY_OF_GENRE.get(str(genre or ""), "generic")


def family_of(answers: dict[str, Any], genre_keys: tuple[str, ...] = ()) -> str:
    tags = answers.get("genre_tags") or []
    if not tags and answers.get("genre"):
        tags = [answers.get("genre")]
    for tag in tags:
        fam = FAMILY_OF_GENRE.get(str(tag))
        if fam:
            return fam
    return "generic"


# ---------------------------------------------------------------------------
# 信号抽取
# ---------------------------------------------------------------------------


def detect_genres(text: str, genre_keys: tuple[str, ...]) -> list[str]:
    """按出现位置返回题材（可多选）。短句里才认同义词，避免长梗概误判。"""
    t = text or ""
    hits: list[tuple[int, str]] = []
    for key in genre_keys:
        idx = t.find(key)
        if idx >= 0:
            hits.append((idx, key))
    if len(_norm(t)) <= 40:
        for key, aliases in _GENRE_ALIASES.items():
            if key not in genre_keys:
                continue
            for alias in aliases:
                idx = t.find(alias)
                if idx >= 0:
                    hits.append((idx, key))
                    break
    hits.sort()
    out: list[str] = []
    for _, key in hits:
        if key not in out:
            out.append(key)
    return out


def _match_first(text: str, patterns: tuple[tuple[str, tuple[str, ...]], ...]) -> str | None:
    for value, words in patterns:
        if any(w in text for w in words):
            return value
    return None


def _extract_protagonist(text: str) -> str | None:
    """「主角叫林深，28 岁，前刑警」→「林深：28 岁，前刑警」。

    只认明确句式，避免把「主角是一个被灭门的少年」里的描述当成名字。
    """
    m = re.search(
        r"(?:主角|主人公|男主|女主|男一号|女一号)\s*(?:叫|名叫|名字叫)\s*"
        r"([\u4e00-\u9fa5A-Za-z·]{2,8})",
        text,
    )
    if m is None:
        m = re.search(
            r"(?:主角|主人公|男主|女主)\s*是\s*([\u4e00-\u9fa5A-Za-z·]{2,4})",
            text,
        )
    if m is None:
        return None
    name = m.group(1)
    # 「一个/一名/位/个」开头的是描述，不是名字。
    if re.match(r"^(一个|一名|一位|这个|那个|个|位)", name):
        return None
    if "的" in name:
        return None
    # 名字后面紧跟的短句才算设定；一旦碰到别的维度（写法/篇幅/世界观…）
    # 就停下，避免把「第一人称，30 万字」也塞进主角设定。
    stop = re.compile(
        r"^(第一人称|第三人称|双视角|多视角|快节奏|慢节奏|慢热|强情节|日更|周更|"
        r"一次写完|世界观|世界设定|世界规则|金手指|能力体系|力量体系|核心冲突|"
        r"题材|每段|单段|每章|全书|篇幅|主角|主人公|风格|"
        r"\d+(?:\.\d+)?\s*(?:万|千)?\s*字|\d+\s*(?:段|章))"
    )
    setup: list[str] = []
    for part in re.split(r"[，,]", text[m.end():])[:3]:
        part = part.strip(" 。；;")
        if not part:
            continue
        if stop.match(part):
            break
        setup.append(part)
    tail = "，".join(setup)
    if tail and len(tail) <= 80:
        return f"{name}：{tail}"
    return name


def _extract_premise(text: str) -> str | None:
    """够长、且像在讲剧情/人物处境，才当成核心冲突。"""
    t = (text or "").strip()
    norm = _norm(t)
    if len(norm) < 30:
        return None
    if t.endswith("？") or t.endswith("?"):
        return None
    if re.search(r"(改成|改为|换成|跳过|别问|按默认)", t):
        return None
    # 明确写了「核心冲突是 …」：只取这一句，别把整段复合信息都塞进去。
    m = re.search(
        r"(?:核心冲突|剧情方向|故事主线|主线是|讲的是|剧情是)\s*(?:是|为|：|:)?\s*([^。；;\n]{6,160})",
        t,
    )
    if m:
        return m.group(1).strip(" ，,")
    story_words = (
        "主角", "故事", "剧情", "世界", "阴谋", "复仇", "失踪", "调查",
        "相爱", "对抗", "背叛", "为了", "却被", "发现", "秘密", "真相",
    )
    if len(norm) >= 50 or any(w in t for w in story_words):
        return t[:500]
    return None


def _extract_book_length(text: str) -> int | None:
    """全书字数（不是每段字数）。返回整数，认不出返回 None。"""
    t = text or ""
    m = re.search(
        r"(?:全书|总字数|总篇幅|篇幅|目标字数|预计)\D{0,8}(\d+(?:\.\d+)?)\s*(万|千)\s*字?",
        t,
    )
    unit = None
    if m is not None:
        value = float(m.group(1))
        unit = m.group(2)
    else:
        m = re.search(
            r"(?:全书|总字数|总篇幅|篇幅|目标字数|预计)\D{0,8}(\d+(?:\.\d+)?)\s*字",
            t,
        )
        if m is not None:
            value = float(m.group(1))
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(万|千)\s*字?", t)
            if m is None:
                # 先剔除「每段 4000 字」这类容量描述，再看裸的「300000 字」。
                cleaned = re.sub(r"(?:每段|单段|每章|一章|一段)\D{0,6}\d{2,5}\s*字", "", t)
                m = re.search(r"(\d{4,})\s*字", cleaned)
                return int(m.group(1)) if m else None
            value = float(m.group(1))
            unit = m.group(2)
    if unit == "万":
        value *= 10000
    elif unit == "千":
        value *= 1000
    elif value < 1000:
        # 「篇幅 30」这种没单位的，按万字理解。
        value *= 10000
    return int(round(value))


def _extract_cadence(text: str) -> str | None:
    if any(w in text for w in ("日更", "每日更新", "每天更新")):
        return "日更"
    if any(w in text for w in ("周更", "每周更新")):
        return "周更"
    if any(w in text for w in ("月更", "每月更新")):
        return "月更"
    if any(w in text for w in ("一次写完", "一口气写完", "写完再发", "存稿")):
        return "一次写完"
    if any(w in text for w in ("随缘更新", "不定期更新")):
        return "随缘更新"
    return None


_ATTRIBUTE_STOPS = (
    "世界观", "世界设定", "世界规则", "金手指", "能力体系", "力量体系",
    "核心冲突", "剧情方向", "故事主线", "主角", "主人公",
    "第一人称", "第三人称", "双视角", "多视角", "快节奏", "慢热",
    "篇幅", "全书", "总字数", "每段", "日更", "周更", "一次写完",
)


def _clause_after(text: str, words: tuple[str, ...]) -> str | None:
    """取某个属性后面的短句，遇到下一个属性/句号就停。

    一句话里常同时给多个维度（「世界观是…，金手指是…，核心冲突是…」），
    不在这里截断的话，世界观点会一路吃到核心冲突。
    """
    for word in words:
        idx = text.find(word)
        if idx < 0:
            continue
        rest = text[idx:]
        end = len(rest)
        for punc in "。；;\n":
            pos = rest.find(punc)
            if 0 <= pos < end:
                end = pos
        for stop in _ATTRIBUTE_STOPS:
            if stop in word or word in stop:
                continue
            pos = rest.find(stop, len(word))
            if 0 <= pos < end:
                end = pos
        clause = rest[:end].strip(" ，,")
        if 2 <= len(clause) <= 120:
            return clause
    return None


def _strip_attribute_prefix(clause: str, extra: tuple[str, ...] = ()) -> str:
    """「世界观约束是…」→「…」；保留内容，去掉重复的标签。"""
    for label in extra + ("世界观约束", "世界观", "世界设定", "世界规则",
                          "金手指边界", "金手指", "能力体系", "力量体系"):
        if clause.startswith(label):
            return clause[len(label):].lstrip("是：:，, ")
    return clause


def _extract_length(text: str) -> int | None:
    m = re.search(r"(?:每段|单段|每章|一章|一段)\D{0,4}(\d{2,5})\s*字", text)
    if m is None:
        m = re.search(r"(\d{2,5})\s*字\s*(?:每段|一段|每章)", text)
    if m:
        n = int(m.group(1))
        return n if 500 <= n <= 8000 else None
    return None


def _extract_chapters(text: str) -> int | None:
    m = re.search(r"(?:这次|本轮)?\s*(?:写|生成)\s*([一二两三四五六七八九十\d]{1,3})\s*(?:段|章)", text)
    if m is None:
        return None
    n = _cn_int(m.group(1))
    if n is not None and 1 <= n <= 10:
        return n
    return None


def read_signals(
    text: str,
    genre_keys: tuple[str, ...] = (),
    style_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    """把一句话拆成可落库的故事信号；没有的键不返回。"""
    t = (text or "").strip()
    sig: dict[str, Any] = {}
    if not t:
        return sig

    genres = detect_genres(t, genre_keys)
    if genres:
        sig["genres"] = genres

    pov = _match_first(t, _POV_PATTERNS)
    pace = _match_first(t, _PACE_PATTERNS)
    tone = _match_first(t, _TONE_PATTERNS)
    parts = [p for p in (pov, pace, tone) if p]
    if parts:
        # 去重但保留顺序（「快节奏」和「爽」可以同时存在）。
        sig["pov"] = "、".join(dict.fromkeys(parts))

    protagonist = _extract_protagonist(t)
    if protagonist:
        sig["protagonist"] = protagonist

    premise = _extract_premise(t)
    if premise:
        sig["premise"] = premise

    book_length = _extract_book_length(t)
    cadence = _extract_cadence(t)
    if book_length or cadence:
        if book_length:
            sig["book_length"] = book_length
        if cadence:
            sig["cadence"] = cadence
        bits = []
        if book_length:
            bits.append(f"{book_length / 10000:g} 万字" if book_length >= 10000 else f"{book_length} 字")
        if cadence:
            bits.append(cadence)
        sig["scale"] = "，".join(bits)

    length = _extract_length(t)
    if length:
        sig["length"] = length
    chapters = _extract_chapters(t)
    if chapters:
        sig["chapters"] = chapters

    world = _clause_after(t, _WORLD_WORDS)
    if world:
        sig["world"] = _strip_attribute_prefix(world)
    power = _clause_after(t, _POWER_WORDS)
    if power:
        sig["power"] = _strip_attribute_prefix(power)

    for key in style_keys:
        if key and key in t:
            sig["style"] = key
            break
    return sig


def prefill(
    answers: dict[str, Any],
    signals: dict[str, Any],
) -> dict[str, Any]:
    """把信号转成「还没答过的步骤」的预填值（不改 answers 本身）。"""
    out: dict[str, Any] = {}
    genres = signals.get("genres") or []
    if genres:
        out["genre_tags"] = list(genres)
        if not answers.get("genre"):
            out["genre"] = genres[0]
    for step in ("pov", "premise", "protagonist", "world", "power", "scale",
                 "length", "chapters", "style"):
        if step in answers:
            continue
        value = signals.get(step)
        if value not in (None, "", []):
            out[step] = value
    return out


# ---------------------------------------------------------------------------
# 追问计划
# ---------------------------------------------------------------------------


def plan_steps(
    answers: dict[str, Any],
    genre_keys: tuple[str, ...] = (),
) -> list[str]:
    """按题材族算出故事阶段的追问顺序（含已答步骤，便于回退/汇总）。"""
    tags = answers.get("genre_tags") or []
    if not tags and answers.get("genre"):
        tags = [answers.get("genre")]
    families: list[str] = []
    for tag in tags:
        fam = FAMILY_OF_GENRE.get(str(tag), "generic")
        if fam not in families:
            families.append(fam)
    if not families:
        families = ["generic"]

    plan = list(_FAMILY_ORDER.get(families[0], _FAMILY_ORDER["generic"]))
    for fam in families[1:]:
        for step in _FAMILY_ORDER.get(fam, []):
            if step not in plan:
                plan.append(step)
    plan = [s for s in plan if s != "scale"] + ["scale"]
    return plan


def missing_steps(answers: dict[str, Any], genre_keys: tuple[str, ...] = ()) -> list[str]:
    """还缺哪些故事问题（模糊回答不算已答，调用方不要先写进 answers）。"""
    out: list[str] = []
    for step in plan_steps(answers, genre_keys):
        value = answers.get(step)
        if value is None or (isinstance(value, str) and not value.strip()):
            out.append(step)
    if not answers.get("genre"):
        out.insert(0, "genre")
    return list(dict.fromkeys(out))


def is_sufficient(answers: dict[str, Any], genre_keys: tuple[str, ...] = ()) -> bool:
    """信息够不够：题材 + 该题材族要求的全部故事项都答了。"""
    if not answers.get("genre"):
        return False
    return not missing_steps(answers, genre_keys)


# ---------------------------------------------------------------------------
# 默认值（只在用户明确说「按默认来 / 别问了」时使用）
# ---------------------------------------------------------------------------


def _default_genre(genre_keys: tuple[str, ...], project_genre: str | None) -> str:
    if project_genre and project_genre in genre_keys:
        return project_genre
    if "都市" in genre_keys:
        return "都市"
    return genre_keys[0] if genre_keys else ""


def _default_world(family: str) -> str:
    return {
        "speculative": "科技水平贴近现实可验证，超出现实的部分必须有代价；不引入无法解释的万能科技。",
        "mystery": "信息按时间线发放，主角只能依据已发生的线索推理；不靠超自然力量破案。",
        "fantasy": "力量有来源、有上限、有禁忌；世界规则一旦定下就不临时改。",
        "game": "系统/副本规则明确，数值成长有上限，不临时发新外挂。",
        "romance": "关系推进遵守人物处境与时代边界，不靠误会硬拖。",
        "history": "时代落点明确，重大史实不篡改，虚构人物不改变历史结论。",
        "urban": "职业、法律与现实常识成立，冲突来自人物处境而不是降智。",
        "generic": "世界规则前后一致，超出常识的设定要给出代价。",
    }.get(family, "世界规则前后一致，超出常识的设定要给出代价。")


def _default_power(family: str) -> str:
    if family in {"mystery", "urban", "history", "romance"}:
        return "不设金手指；主角靠能力、信息与选择推进。"
    return "力量/金手指有明确上限与代价，不能无成本解决冲突。"


def defaults(
    answers: dict[str, Any],
    genre_keys: tuple[str, ...] = (),
    project_genre: str | None = None,
) -> dict[str, Any]:
    family = family_of(answers, genre_keys)
    genre = answers.get("genre") or _default_genre(genre_keys, project_genre)
    return {
        "genre": genre,
        "pov": "第三人称、常规节奏、强情节",
        "premise": "由模型按题材与主角设定自行构思核心冲突，开篇即抛出。",
        "protagonist": "由模型在设定与大纲里给出主角姓名与基础设定。",
        "world": _default_world(family),
        "power": _default_power(family),
        "scale": "30 万字，一次写完",
    }


def default_for(
    step: str,
    answers: dict[str, Any],
    genre_keys: tuple[str, ...] = (),
    project_genre: str | None = None,
) -> Any:
    if step in ("length", "chapters"):
        return None
    return defaults(answers, genre_keys, project_genre).get(step)


# ---------------------------------------------------------------------------
# 问题卡与模糊回答的方向建议
# ---------------------------------------------------------------------------


def _pov_options(family: str) -> list[dict[str, str]]:
    options = [
        {
            "value": "第一人称 / 沉浸 / 心理流",
            "label": "第一人称 · 沉浸",
            "desc": "读者跟着主角的感官走，信息差天然成立；适合悬疑、情感和强代入。",
        },
        {
            "value": "第三人称 / 快节奏 / 强钩子",
            "label": "第三人称 · 快节奏",
            "desc": "镜头在人物之间切换，情节推进快；网文主流，适合大多数题材。",
        },
        {
            "value": "多视角 / 群像 / 慢热",
            "label": "多视角 · 群像",
            "desc": "多条人物线并行，世界更立体；对结构控制要求高，适合史诗与群像。",
        },
    ]
    if family == "mystery":
        return options
    if family in {"speculative", "fantasy", "game"}:
        return [options[1], options[2], options[0]]
    if family == "romance":
        return [options[0], options[1], options[2]]
    return options


def _premise_directions(family: str) -> list[str]:
    common = [
        "围绕一个未解之谜层层反转，主角每进一步就发现代价更大。",
        "围绕一次关键选择展开，主角的选择不断把身边人卷进来。",
        "围绕两方势力的对抗与背叛，主角在夹缝里找自己的位置。",
    ]
    extra = {
        "mystery": "围绕一桩被所有人默认已经结案的旧事，主角发现它还在继续。",
        "speculative": "围绕一条被普遍接受的技术规则，主角发现它的代价被隐瞒了。",
        "fantasy": "围绕一条所有人都遵守的禁忌，主角被迫去验证它是否真实。",
        "romance": "围绕两个立场相反的人被迫合作，关系在一次次选择里改变。",
        "history": "围绕一个身处时代转折点的小人物，他的选择改变不了大势却改变了身边人。",
        "urban": "围绕一件看似普通的职场/家庭事件，主角发现背后有一套没人明说的规则。",
        "game": "围绕一个所有人都在用的系统规则，主角发现规则本身有漏洞。",
    }
    return [extra.get(family, common[0])] + common[:2]


def _world_options(family: str) -> list[str]:
    return {
        "speculative": ["近未来：技术只比现实多一小步", "远未来：科技重塑社会结构", "架空科技：规则自洽即可"],
        "mystery": ["现实都市：没有超自然力量", "封闭空间：有限嫌疑人", "时间线诡计：信息按时间发放"],
        "fantasy": ["单一力量体系：来源、上限、禁忌明确", "多种族/多势力：规则互相制约", "低魔世界：力量稀少且危险"],
        "game": ["系统面板：数值与冷却明确", "副本规则：每关规则独立", "无面板：能力靠设定自洽"],
        "romance": ["现实向：职业与家庭约束成立", "架空：只保留情感逻辑", "时代背景：关系受时代限制"],
        "history": ["真实朝代：重大史实不篡改", "架空古代：制度自洽", "近现代：职业与器物考据"],
        "urban": ["现实职业：行业规则可查", "都市悬疑：日常里藏异常", "家庭/情感：关系驱动"],
        "generic": ["世界规则前后一致", "超常设定必须有代价", "不临时改设定"],
    }.get(family, ["世界规则前后一致", "超常设定必须有代价", "不临时改设定"])


def question(
    step: str,
    answers: dict[str, Any],
    genre_keys: tuple[str, ...] = (),
    ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """渲染一个故事步骤的问题卡（标题 / 为什么问 / 选项或输入框）。"""
    ctx = ctx or {}
    family = family_of(answers, genre_keys)
    genre = answers.get("genre") or ""
    hint = (ctx.get("genre_hints") or {}).get(genre, "")
    suffix = f"（你选的是{genre}：{hint}）" if hint else ""

    if step == "genre":
        hints = ctx.get("genre_hints") or {}
        options = [
            {
                "value": key,
                "label": key,
                "desc": str(hints.get(key) or ""),
            }
            for key in genre_keys[:8]
        ]
        return {
            "title": "想写什么题材？",
            "why": "题材不同，我接下来要问的东西也不同：悬疑先问谜团，仙侠先问"
            "力量体系，历史先问时代落点。先定题材，再决定追问清单。",
            "card": {
                "type": "single",
                "field": "genre",
                "default": answers.get("genre") or "",
                "options": options,
            },
        }

    if step == "pov":
        return {
            "title": "想怎么写：视角、节奏、文风倾向？",
            "why": "同一段剧情，第一人称和第三人称读起来是两本书；快节奏和慢热也决定"
            "冲突多久抛一次。先定写法，后面的大纲和正文都按它落。",
            "card": {
                "type": "single",
                "field": "pov",
                "default": answers.get("pov") or _pov_options(family)[0]["value"],
                "options": _pov_options(family),
            },
        }

    if step == "premise":
        return {
            "title": "核心冲突或剧情方向是什么？",
            "why": "只需要一句「谁想要什么、被什么挡住」。它决定大纲的骨架；"
            "不想现在细想，可以从下面三个结构里挑一个，我按你的题材展开。"
            + suffix,
            "card": {
                "type": "text",
                "field": "premise",
                "default": answers.get("premise") or defaults(answers, genre_keys)[ "premise"],
                "placeholder": "一句话：主角想要什么、最大的阻力是什么",
                "suggestions": _premise_directions(family),
            },
        }

    if step == "protagonist":
        return {
            "title": "主角叫什么？一句话设定是什么？",
            "why": "姓名和身份会直接进入设定与大纲。你只给名字也行；"
            "如果还没想好，可以让我在大纲里先起一个，之后再改。",
            "card": {
                "type": "text",
                "field": "protagonist",
                "default": answers.get("protagonist") or defaults(answers, genre_keys)["protagonist"],
                "placeholder": "例如：名字 + 身份 / 处境 / 他最在意的东西",
                "suggestions": [
                    "主角叫…，职业/身份是…，最在意…",
                    "先不定名，让大纲给一个",
                    "我有几个候选名字，帮我挑",
                ],
            },
        }

    if step == "world":
        return {
            "title": "世界观有什么必须守住的约束？",
            "why": "这一项用户往往不会主动提，但它决定后面会不会写崩：力量从哪来、"
            "信息怎么流动、哪些事在这个世界里做不到。"
            + suffix,
            "card": {
                "type": "text",
                "field": "world",
                "default": answers.get("world") or _default_world(family),
                "placeholder": "例如：科技上限、时代落点、力量来源与禁忌",
                "suggestions": _world_options(family),
            },
        }

    if step == "power":
        return {
            "title": "金手指 / 力量体系的边界是什么？",
            "why": "能做什么、代价是什么、什么不能做。边界不清，后面很容易变成"
            "「主角一遇到问题就开新能力」，冲突会失效。"
            + suffix,
            "card": {
                "type": "text",
                "field": "power",
                "default": answers.get("power") or _default_power(family),
                "placeholder": "例如：能力上限、使用代价、不能解决的问题",
                "suggestions": [
                    "有金手指：说清能做什么、代价是什么",
                    "有力量体系：说清上限与禁忌",
                    "不设金手指，靠人物与信息推进",
                ],
            },
        }

    if step == "scale":
        return {
            "title": "全书想要多长？更新节奏怎么走？",
            "why": "篇幅决定大纲铺几卷、每段容量怎么切；更新节奏决定每段要不要留钩子。"
            "这一项也会写进项目参数，后面生成按它算。",
            "card": {
                "type": "text",
                "field": "scale",
                "default": answers.get("scale") or defaults(answers, genre_keys)["scale"],
                "placeholder": "例如：30 万字，日更，每段 4000 字",
                "suggestions": ["10 万字，一次写完", "30 万字，日更", "80 万字，周更"],
            },
        }

    raise ValueError(f"未知故事步骤：{step}")


def vague_message(
    step: str,
    answers: dict[str, Any],
    genre_keys: tuple[str, ...] = (),
    ctx: dict[str, Any] | None = None,
) -> str:
    """模糊回答时给 2–3 个具体方向，而不是套默认。"""
    body = question(step, answers, genre_keys, ctx)
    card = body.get("card") or {}
    options = card.get("options") or []
    suggestions = card.get("suggestions") or []
    lines: list[str] = []
    if options:
        for i, opt in enumerate(options[:3], 1):
            label = str(opt.get("label") or opt.get("value") or "")
            desc = str(opt.get("desc") or "")
            lines.append(f"{i}. {label}" + (f"——{desc}" if desc else ""))
    else:
        for i, text in enumerate(suggestions[:3], 1):
            lines.append(f"{i}. {text}")
    if not lines:
        return (
            "这一项我还没法替你定。直接说一个你的想法就行；"
            "或者说「按默认来」，我按题材常规补齐，之后还能改。"
        )
    return (
        "你说的这个我没法当成答案——它会直接决定后面怎么写。"
        "可以从下面挑一个，或者直接给我一句你的想法：\n"
        + "\n".join(lines)
        + "\n\n选好之后我接着问下一项；想让我全部按默认补齐，就说「按默认来」。"
    )


# ---------------------------------------------------------------------------
# 回执文案
# ---------------------------------------------------------------------------


_LABELS = {
    "genre": "题材",
    "pov": "写法",
    "premise": "核心冲突",
    "protagonist": "主角",
    "world": "世界观约束",
    "power": "金手指边界",
    "scale": "篇幅与更新",
    "length": "每段字数",
    "chapters": "本次段数",
    "style": "风格",
    "genre_tags": "题材标签",
}

#: 供 flow / 前端复用的中文步骤名。
STORY_LABELS = _LABELS


def _short(value: Any, limit: int = 40) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def captured_summary(prefilled: dict[str, Any]) -> str:
    """「已记下：题材 科幻（+悬疑）、写法 第一人称 …」。"""
    bits: list[str] = []
    tags = prefilled.get("genre_tags")
    for key, value in prefilled.items():
        if key in ("genre_tags",) or value in (None, "", []):
            continue
        label = _LABELS.get(key, key)
        extra = ""
        if key == "genre" and tags and len(tags) > 1:
            extra = "（+" + "、".join(str(t) for t in tags[1:]) + "）"
        bits.append(f"{label} {_short(value)}{extra}")
    if not bits:
        return ""
    return "已记下：" + "、".join(bits) + "。"


def remaining_summary(
    answers: dict[str, Any],
    genre_keys: tuple[str, ...] = (),
) -> str:
    missing = missing_steps(answers, genre_keys)
    if not missing:
        return "信息够了，接下来落到设定与大纲。"
    labels = "、".join(_LABELS.get(s, s) for s in missing)
    return f"还差：{labels}。"
