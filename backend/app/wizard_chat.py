"""引导对话里的「自由输入」解析：直接打字也能答，中途追问就地解释。

三件事，全部是纯逻辑（不碰 DB、不依赖 FastAPI）：

1. `parse()` —— 把用户这句话分成三种：**作答** / **追问** / **没听懂**。
   追问永远不推进流程（路由层原样停在当前题）。
2. `explain()` —— 追问就地展开：先查该主题的固定知识（提炼档位、续写起点、
   字数区间这些数字都来自仓库里已核实的事实），查不到再让助手模型基于
   当前这一步的原文回答，模型失败则退回一段兜底话。
3. 数字/选项的模糊匹配（「三千字」「随便你定」「就完整档吧」）。

为什么不直接用模型解析：解析错一次就会把用户的答案写进项目参数，
代价比多问一句大得多。所以这里只认确定性规则，认不出来就请用户再说一次。
"""

from __future__ import annotations

import re
from typing import Any

from app import wizard_flow as flow
from app import wizard_market as market
from app import wizard_story as story

#: 「你定就行」：直接取该题默认值，并在回执里说明取了什么、为什么。
DEFAULT_WORDS = (
    "随便",
    "你定",
    "你看着办",
    "听你的",
    "都行",
    "都可以",
    "默认",
    "不知道",
    "无所谓",
    "你决定",
    "都随你",
)

#: 明确的「我在做选择」信号：带着这些词时，即使句尾有「？」也按作答处理。
_COMMIT_VERBS = (
    "就用",
    "就选",
    "那就",
    "好的",
    "可以吧",
    "就这样",
    "按这个",
    "选它",
    "定了",
    "要这个",
    "用默认",
)

#: 追问信号。带这些词的问题按「解释」处理，不写任何参数。
_Q_MARKS = (
    "为什么",
    "怎么",
    "如何",
    "什么",
    "啥",
    "吗",
    "呢",
    "多少",
    "多久",
    "多长",
    "区别",
    "差别",
    "差在哪",
    "能不能",
    "可不可以",
    "是不是",
    "有没有",
    "到底",
    "哪个好",
    "哪个",
    "哪种",
    "推荐哪",
)

_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}

#: 去掉空白后超过这个字数就算「长文本」：不再当成一个选项吞掉。
LONG_TEXT_CHARS = 40

#: 每一步自己的兜底话术。原则：只举这一步真正能说的例子；
#: 多选步骤绝不出现「3000 字」这种别的步骤的答案。
FALLBACKS: dict[str, str] = {
    "branch": (
        "方向还没定：想写原创、接着已有正文续写，还是和角色对话？"
        "说「原创」「续写」「角色对话」，或者直接点上面的卡片。"
    ),
    "corpus": "可以说书名（如「参考某某书」），或者「不投喂语料」；也可以直接点卡片选。",
    "distill": "可以说「完整档」「平衡档」「快速档」；已有档案时也可以说「沿用现有档案」。",
    "book_scope": "说按哪本书读，比如「按某某书读」，或者说「不指定」；也可以直接点卡片。",
    "genre": "说一个题材，比如「悬疑」「科幻」「历史」；也可以直接点卡片。",
    "pov": "说写法，比如「第一人称」「第三人称快节奏」「多视角」；也可以点卡片。",
    "premise": "用一句话说「主角想要什么、最大的阻力是什么」；也可以从卡片里的方向挑一个。",
    "protagonist": "说主角的名字和一句设定，比如「名字 + 身份/处境」；也可以让大纲先起一个。",
    "world": "说这个世界必须守住的约束，比如科技上限、时代落点、力量来源与禁忌。",
    "power": "说金手指/力量体系能做什么、代价是什么、什么不能做；没有就说「不设金手指」。",
    "scale": "说全书篇幅和更新节奏，比如「30 万字，日更，每段 4000 字」。",
    "style": "说一个平台调性，比如「番茄」「起点」「晋江」「豆瓣阅读」「知乎盐选」。",
    "length": "说单段字数，比如「3000 字」（范围 500–8000）；这是每段字数，不是全书长度。",
    "chapters": "说这次写几段，比如「1 段」「3 段」；也可以直接点卡片。",
    "outline": "说「先出大纲」或「直接开写」；也可以直接点卡片。",
    "cards": "看一遍上面的角色卡，满意就说「继续」「没问题」；要改就直接点卡上的编辑/重生成。",
    "continue_from": "说「接在最后一段之后」，或者给一个段号；也可以直接点卡片。",
    "character": "直接打一个角色名（用上面的角色卡），或者点卡片选。",
    "scene": "描述一下情境，比如「初次见面」「一场对峙之后」；也可以点卡片选。",
    "confirm": "确认无误就说「开始」；想改就说「上一步」，或点要改的那一行。",
}

#: 越界要求：明确拒绝，并给出替代路径。顺序在动作/作答之前。
_REFUSALS = (
    (
        re.compile(
            r"(抄|照抄|复制|粘贴|搬运|贴).{0,12}(原文|全文|整段|正文|章节)"
            r"|(原文|全文|整段).{0,10}(抄|复制|粘贴|搬)"
        ),
        "这件事我不做：不会把参考语料整段抄进你的正文。要参考原文，可以把它作为参考语料做提炼/检索，"
        "只取检索到的片段或摘要来学写法。",
    ),
    (
        re.compile(r"(删掉|删除|删了|清空|毁掉|干掉).{0,8}(项目|这本|当前|小说|书)"),
        "这件事我不做：引导流程里不能删项目。要删除项目，请到项目工作台的项目设置里操作（那里有二次确认）。",
    ),
    (
        re.compile(
            r"(帮我|替我|给我|代我).{0,8}(写|代写|做).{0,8}(论文|作业|毕设|毕业设计|开题报告)"
            r"|(别人的|他人的|别人).{0,6}(论文|作业|毕设)"
            r"|(代写|枪手).{0,6}(论文|作业)"
        ),
        "这件事我不做：不代写他人的论文、作业或毕设。这个助手只用于小说创作；"
        "如果你在写自己的论文，请用专门的写作工具。",
    ),
)

#: 「第 N 段」= 指代某一已有/指定段落，不是「这次写几段」的数量。
_SEG_REF = re.compile(r"第\s*(\d+|[零〇一二两三四五六七八九十百]+)\s*段")

#: 带这些词时，「第 N 段」是改写/调整意图，而不是选段号（continue_from 要放行纯段号）。
_EDIT_WORDS = re.compile(
    r"改写|重写|重新写|修改|改一下|改得|改紧|改成|改为|写紧|写松|写短|写长"
    r"|润色|扩写|缩写|压缩|删掉|删除|替换|换成|调整"
)

_SEG_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}

#: 控制意图的封闭清单：只在 character / scene 这类自由文本步骤前置识别，
#: 识别到就不再当角色名/情境。清单外的新说法见 control_intent 末尾的兜底。
_CONTROL_PATTERNS = (
    (
        "skip",
        re.compile(r"^(跳过|略过|不要问|别问|不用问)(掉)?(这|这一|当前|本)?(一)?(步|题|项|步骤)?$"),
    ),
    ("skip", re.compile(r"^(跳过|略过)(提炼|蒸馏|语料|风格|题材|字数|段数|大纲|情境|角色)$")),
    (
        "process",
        re.compile(
            r"^(直接|马上|立刻)?(生成|开写|开始写|写正文|出正文)$"
            r"|^(先)?(别|不要|不用|暂停|停掉)(再)?(提炼|蒸馏)$"
        ),
    ),
    ("set_length", re.compile(r"字数|篇幅")),
    ("set_genre", re.compile(r"题材|类型")),
    ("set_style", re.compile(r"风格|平台调性")),
    (
        "edit_character",
        re.compile(r"(主角|人物|角色|设定|背景|朝代|世界观).{0,8}(改成|改为|换成|变成|设为|调整)"),
    ),
    ("edit_character", re.compile(r"^(换|改)(个|一个|一下)?(朝代|背景|设定|人物|主角)")),
)

_BRANCH_EXACT = {
    "原创": flow.KIND_ORIGINAL,
    "原创小说": flow.KIND_ORIGINAL,
    "原创故事": flow.KIND_ORIGINAL,
    "写原创": flow.KIND_ORIGINAL,
    "自己写": flow.KIND_ORIGINAL,
    "从零开始": flow.KIND_ORIGINAL,
    "从头写": flow.KIND_ORIGINAL,
    "新书": flow.KIND_ORIGINAL,
    "写小说": flow.KIND_ORIGINAL,
    "我要写小说": flow.KIND_ORIGINAL,
    "我想写小说": flow.KIND_ORIGINAL,
    "写一本书": flow.KIND_ORIGINAL,
    "写个故事": flow.KIND_ORIGINAL,
    "我要写一篇小说": flow.KIND_ORIGINAL,
    "续写": flow.KIND_CONTINUATION,
    "接着写": flow.KIND_CONTINUATION,
    "往下写": flow.KIND_CONTINUATION,
    "继续写": flow.KIND_CONTINUATION,
    "接着写下去": flow.KIND_CONTINUATION,
    "角色对话": flow.KIND_ROLE,
    "角色扮演": flow.KIND_ROLE,
    "和角色聊天": flow.KIND_ROLE,
    "跟角色聊天": flow.KIND_ROLE,
    "聊天": flow.KIND_ROLE,
    "对话": flow.KIND_ROLE,
    "角色": flow.KIND_ROLE,
}

_BRANCH_PATTERNS = (
    (
        flow.KIND_ORIGINAL,
        r"(我)?(想|要|准备|打算)?(写|做|开)(一本|一个|一部|个)?(小说|故事|书)"
        r"|(我)?(想|要|准备|打算)?(写|做|开)(一本|一个|一部|个)?"
        r"(原创|新书|全新的书|从零|从头写)(小说|故事|书)?",
    ),
    (
        flow.KIND_CONTINUATION,
        r"(我)?(想|要|准备|打算)?(续写|接着写|往下写|继续写|接着写下去)(下去)?",
    ),
    (
        flow.KIND_ROLE,
        r"(我)?(想|要|准备|打算)?(和|跟)?(角色|AI|角色卡)?(聊天|对话|扮演|角色扮演)(一下|一会儿)?",
    ),
)


# ---------------------------------------------------------------------------
# 基础判断
# ---------------------------------------------------------------------------


def _is_default_word(text: str) -> bool:
    return any(w in text for w in DEFAULT_WORDS)


#: 「别问了 / 按默认来」：明确要求停止追问。只在非方向、非确认步骤生效。
_BYPASS_ALL = (
    "按默认来",
    "按默认",
    "用默认",
    "都按默认",
    "全都默认",
    "别问了",
    "不要问了",
    "不用问了",
    "别再问",
    "你全定了",
    "剩下的你定",
)


def is_bypass(text: str, include_process: bool = False) -> bool:
    words = story.BYPASS_WORDS if include_process else _BYPASS_ALL
    return any(w in (text or "") for w in words)


def _is_vague(text: str) -> bool:
    """短句里只有「随便/都行/不知道」这类词，才算模糊回答。

    带具体信息的句子（「随便写个科幻」）不算——那种应该直接抽出题材。
    """
    t = _norm(text).strip("。！？!?～~")
    if not any(w in t for w in story.VAGUE_WORDS):
        return False
    residual = t
    for word in story.VAGUE_WORDS:
        residual = residual.replace(word, "")
    return len(residual) <= 4


def _has_commit_verb(text: str) -> bool:
    return any(w in text for w in _COMMIT_VERBS)


def _has_number_with_unit(text: str) -> bool:
    """「3000 字行吗」这类：带量纲的数字本身就是明确选择。"""
    if re.search(r"\d+\s*(字|段|章|k|K|千)", text):
        return True
    return bool(re.search(r"[一二两三四五六七八九十百千]+\s*(字|段|章)", text))


def looks_like_question(text: str) -> bool:
    t = text.strip()
    if t.endswith("？") or t.endswith("?"):
        return True
    return any(m in t for m in _Q_MARKS)


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def is_long_text(text: str) -> bool:
    """去空白后 ≥40 字算长文本：不能再被当成一个选项吞掉。"""
    return len(_norm(text)) >= LONG_TEXT_CHARS


def _first_clause(text: str) -> str:
    """取第一句/第一个分句：方向意图只在开头这一小段里认。"""
    return re.split(r"[，,。；;：:\n]", (text or "").strip(), maxsplit=1)[0].strip()


def _branch_intent(text: str) -> str | None:
    """方向判定：明确选项、或第一分句是成句意图才算；整段梗概不算。

    这样「原创」「我想写一本原创小说」照常接住，而「……对话里的潜台词」
    这种长文本不会再被子串命中。
    """
    t = (text or "").strip()
    if not t:
        return None
    has_punct = bool(re.search(r"[，,。；;：:\n]", t))
    # 没有标点又是一整段：多半是梗概/设定，不在这里替用户定方向。
    if not has_punct and len(_norm(t)) > 24:
        return None
    clause = _first_clause(t)
    key = _norm(clause).strip("。！？!?～~")
    if key in _BRANCH_EXACT:
        return _BRANCH_EXACT[key]
    if not key or len(key) > 24:
        return None
    for value, pattern in _BRANCH_PATTERNS:
        if re.fullmatch(pattern, key):
            return value
    return None


def is_back(text: str) -> bool:
    """回退意图：整句匹配，避免「回到上一步」比「上一步」多两个字就不认。"""
    t = _norm(text).strip("。！？!?～~")
    if t in {"退回去", "回去改", "改上一步", "重来一次", "重新来"}:
        return True
    return bool(re.fullmatch(r"(回到|返回|退回|退到)?到?(上一步|前面|上一题|上题|上一步骤)", t))


def control_intent(step: str, text: str) -> str | None:
    """character/scene 的前置控制意图识别（封闭清单）。

    清单外但看起来像指令的短句，返回 `unknown_control`，交给上层问一句，
    而不是当成角色名/情境。真正的自由文本（角色名、情境描述）才放行。
    """
    if step not in {"character", "scene"}:
        return None
    t = _norm(text).strip("。！？!?～~")
    for key, pattern in _CONTROL_PATTERNS:
        if pattern.search(t):
            return key
    if len(t) <= 20 and re.search(
        r"(跳过|略过|上一步|返回|回退|改成|改为|换成|设为|调整|生成|提炼|蒸馏)", t
    ):
        return "unknown_control"
    return None


def control_message(step: str, intent: str) -> str:
    where = "角色" if step == "character" else "情境"
    if intent == "skip":
        if step == "scene":
            return "这一步是情境；不想写可以点卡片上的跳过按钮，或者说「跳过情境」。"
        return "这一步要先选一个角色，不能跳过。想换方向可以说「上一步」。"
    if intent == "process":
        return (
            f"这是流程操作，不是{where}。要直接开写，请先答完引导；"
            "提炼相关操作请到项目工作台处理。"
        )
    if intent == "set_length":
        return (
            f"这一步在选{where}。要改单段字数，直接说「把字数改成 3000」；"
            "全书长度不在引导里设置。"
        )
    if intent == "set_genre":
        return f"这一步在选{where}。要改题材，直接说「把题材改成悬疑」这类带字段名的说法。"
    if intent == "set_style":
        return f"这一步在选{where}。要改风格，直接说「把风格改成番茄」这类带字段名的说法。"
    if intent == "edit_character":
        return (
            f"人物/世界观设定不在这条引导里改：引导只选{where}。"
            "要改角色设定，请到「项目工作台 → 剧情圣经 → 角色」："
            "那里可以加角色、改境界、改名、删除（改名只允许影响不大的情况）。"
        )
    return f"这句像操作指令，但我不确定指哪一步。可以说「跳过风格」「上一步」，或直接点卡片。"


def _segment_no(raw: str) -> int | None:
    """「3」「三」「十二」→ 段号；认不出返回 None。"""
    raw = (raw or "").strip()
    if raw.isdigit():
        return int(raw)
    n = _cn_to_int(raw)
    if n is not None:
        return n
    if len(raw) == 1:
        return _SEG_DIGITS.get(raw)
    return None


def segment_edit_hint(step: str, text: str, ctx: dict[str, Any]) -> str | None:
    """「第 N 段」指代已有段落时给出诚实回应，绝不当成数量/字数答案。

    只在两种情况下拦截（其余步骤保持原行为）：
    - 当前步就是在问数量/字数（chapters / length）——「第 N 段」不可能是答案；
    - 这句话带改写动词（重写/写紧/改成…）——即使在 continue_from 也不能当段号。

    返回 `None` 表示放行；返回字符串表示直接以 clarify 回给用户，不推进流程。
    """
    t = text or ""
    m = _SEG_REF.search(t)
    if not m:
        return None
    if step not in {"chapters", "length"} and not _EDIT_WORDS.search(t):
        return None

    prefix = {
        "chapters": "这一步是「这次写几段」，不是改第几段。",
        "length": "这一步是「每段多少字」，不是改某一段。",
    }.get(step, "")

    n = _segment_no(m.group(1))
    segments: list[int] = []
    for x in ctx.get("segments") or []:
        try:
            segments.append(int(x))
        except (TypeError, ValueError):
            continue
    segments = sorted(set(segments))

    if not segments:
        return (
            f"{prefix}现在还没有可改写的正文——等正文生成后，"
            "在工作台助手说「把第 N 段重写，<你的要求>」，那里会先弹确认卡再覆盖。"
        )
    if n is not None and n in segments:
        return (
            f"{prefix}第 {n} 段已经有正文了。要重写它，请到工作台助手说"
            f"「把第 {n} 段重写，<你的要求>」，确认后会覆盖该段；"
            "这条引导不会直接改已写正文。"
        )
    have = "、".join(f"第 {x} 段" for x in segments[:5])
    target = f"第 {n} 段" if n is not None else "这一段"
    return (
        f"{prefix}目前已有正文的是 {have}；{target}还没有正文，没法改写。"
        "要重写已有段，请到工作台助手说「把第 N 段重写」。"
    )


def refusal(text: str) -> str | None:
    """越界要求：命中就返回一句明确拒绝；不命中返回 None。"""
    t = text or ""
    for pattern, message in _REFUSALS:
        if pattern.search(t):
            return message
    return None


def fallback_for(step: str) -> str:
    return FALLBACKS.get(
        step,
        "这一步需要你从上面的卡片里选一个；也可以说「上一步」或「重新开始」。",
    )


def _cn_to_int(text: str) -> int | None:
    """把「三千」「两千五」这类写法转成整数；认不出返回 None。

    只支持到万位、且「三千五」这种口语缩写按 3500 处理（末位数字跟在
    千/百后时按下一档计），够覆盖字数和段数的说法。
    """
    total = 0
    section = 0
    num = 0
    seen_unit = False
    last_unit = 0
    for ch in text:
        if ch in _NUM:
            num = _NUM[ch]
        elif ch in _UNIT:
            unit = _UNIT[ch]
            if unit == 10000:
                section = (section + (num or 1)) * 10000
                total += section
                section = 0
            else:
                section += (num or 1) * unit
            num = 0
            seen_unit = True
            last_unit = unit
    if not seen_unit:
        return None
    if num and last_unit >= 100:
        # 「三千五」= 3500：末位数字按上一单位的下一位计
        section += num * (last_unit // 10)
        num = 0
    return total + section + num


def first_number(text: str) -> int | None:
    """从一句话里取第一个数字：3000 / 3,000 / 3k / 3千 / 三千。"""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(万|千|k|K)?", text)
    if m:
        raw, suffix = m.group(1), m.group(2)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            value = 0.0
        if suffix in {"千", "k", "K"}:
            value *= 1000
        elif suffix == "万":
            value *= 10000
        if value > 0:
            return int(round(value))
    cn = "".join(ch for ch in text if ch in _NUM or ch in _UNIT)
    if cn:
        return _cn_to_int(cn)
    return None


# ---------------------------------------------------------------------------
# 选项匹配
# ---------------------------------------------------------------------------


def _label_of(question: dict[str, Any], value: Any) -> str:
    for opt in (question.get("card") or {}).get("options") or []:
        if opt.get("value") == value:
            return str(opt.get("label") or value)
    return str(value)


def _match(step: str, text: str, answers: dict[str, Any], ctx: dict[str, Any],
           question: dict[str, Any]) -> tuple[Any, str] | None:
    """把一句话匹配成该题的一个合法答案；匹配不上返回 None。"""
    t = text.strip()
    low = t.lower()

    if step == "branch":
        value = _branch_intent(t)
        if value:
            return value, flow.KIND_LABELS[value]
        if any(k in t for k in ("续写", "接着写", "往下写", "继续写")):
            return flow.KIND_CONTINUATION, flow.KIND_LABELS[flow.KIND_CONTINUATION]
        if re.search(r"(角色|人物).{0,4}(对话|聊天|扮演)", t):
            return flow.KIND_ROLE, flow.KIND_LABELS[flow.KIND_ROLE]
        # 「写个科幻悬疑」这种只给题材、没说「小说」的，也按原创接住。
        sig = story.read_signals(t, tuple(flow.GENRE_HINTS))
        if sig.get("genres"):
            return flow.KIND_ORIGINAL, flow.KIND_LABELS[flow.KIND_ORIGINAL]
        return None

    if step == "corpus":
        novels = ctx.get("novels") or []
        if any(k in t for k in ("不投喂", "不参考", "不要语料", "不用语料", "自由写", "自由生成", "没有语料")):
            return [], "不投喂语料（自由生成）"
        if any(k in t for k in ("全部", "都选", "全用")) and novels:
            ids = [int(n["id"]) for n in novels]
            return ids, "参考语料：" + "、".join(str(n.get("title") or n["id"]) for n in novels)
        picked = []
        labels = []
        for n in novels:
            title = str(n.get("title") or "")
            base = re.sub(r"\.(txt|md|epub)$", "", title, flags=re.I)
            if title and (title in t or (base and base in t)) and int(n["id"]) not in picked:
                picked.append(int(n["id"]))
                labels.append(base or title)
        if picked:
            return picked, "参考语料：" + "、".join(labels)
        # 用户在「选语料」这题直接开始说题材/写法：视为「不投喂」，
        # 同一句里的题材信息交给 `_story_prefill` 吸收，不要把话打回去。
        if story.read_signals(t, tuple(flow.GENRE_HINTS), tuple(flow.STYLE_HINTS)):
            return [], "不投喂语料（自由生成）"
        return None

    if step == "distill":
        keys = {t2["key"] for t2 in (ctx.get("tiers") or [])}
        rules = (
            (("沿用", "复用", "现有档案", "不重跑", "上次的"), flow.DISTILL_REUSE),
            (("完整", "全量", "最准", "质量", "精度", "最高"), "full"),
            (("平衡", "中等", "中档", "试跑"), "balanced"),
            (("快速", "最快", "冒烟", "验证", "赶时间", "先跑通"), "fast"),
        )
        for words, value in rules:
            if not any(k in t for k in words):
                continue
            if value == flow.DISTILL_REUSE and not ctx.get("archive_exists"):
                continue
            if value != flow.DISTILL_REUSE and value not in keys:
                continue
            return value, _label_of(question, value)
        return None

    if step == "genre":
        for value in flow.GENRE_HINTS:
            if value in t:
                return value, value
        synonyms = {
            "玄幻": "奇幻",
            "仙侠": "仙侠",
            "修真": "仙侠",
            "修仙": "仙侠",
            "爱情": "言情",
            "感情": "言情",
            "探案": "悬疑",
            "推理": "悬疑",
            "末世": "科幻",
            "机甲": "科幻",
            "武侠": "仙侠",
            "古代": "历史",
            "权谋": "历史",
        }
        for word, value in synonyms.items():
            if word in t:
                return value, value
        return None

    if step in {"pov", "premise", "protagonist", "world", "power", "scale"}:
        # 这几步收自由文本：写法、冲突、主角、约束、篇幅都可以直接用一句话说。
        return t, t

    if step == "style":
        for value in flow.STYLE_HINTS:
            if value in t:
                return value, value
        synonyms = {
            "番茄": "番茄",
            "免费": "番茄",
            "起点": "起点",
            "晋江": "晋江",
            "言情站": "晋江",
            "豆瓣": "豆瓣阅读",
            "文学": "豆瓣阅读",
            "知乎": "知乎盐选",
            "盐选": "知乎盐选",
        }
        for word, value in synonyms.items():
            if word in t:
                return value, value
        return None

    if step == "length":
        n = first_number(t)
        if n is not None:
            return n, f"{n} 字"
        if any(k in t for k in ("短一点", "短些", "少一点", "简短", "短")):
            return 1500, "1500 字"
        if any(k in t for k in ("长一点", "多写", "详细", "长")):
            return 4000, "4000 字"
        return None

    if step == "chapters":
        n = first_number(t)
        if n is not None:
            return n, f"{n} 段"
        if any(k in t for k in ("先一段", "就一段", "写一段", "一段")):
            return 1, "1 段"
        return None

    if step == "outline":
        if any(k in t for k in ("不用", "不要", "不需要", "直接写", "跳过", "省掉", "别出")):
            return False, "直接开写"
        if any(k in t for k in ("要", "需要", "先出", "先看", "大纲", "确认")):
            return True, "先出大纲再写"
        return None

    if step == "cards":
        if any(k in t for k in ("确认", "继续", "没问题", "可以", "好的", "行", "下一步")):
            return True, "角色卡已确认"
        return None

    if step == "continue_from":
        segments = list(ctx.get("segments") or [])
        latest = max(segments) if segments else 0
        if any(k in t for k in ("最后", "末尾", "结尾", "接着", "后面")):
            return latest, "从第 1 段开始" if not latest else f"接在第 {latest} 段之后"
        n = first_number(t)
        if n is not None:
            return n, "从第 1 段开始" if n == 0 else f"接在第 {n} 段之后"
        return None

    if step == "character":
        options = (question.get("card") or {}).get("options") or []
        for opt in options:
            name = str(opt.get("value") or "")
            if name and name in t:
                return name, name
        # 列表里没有时允许直接给名字（提示词里会如实说明只有基础设定）。
        if options:
            return None
        return t, t

    if step == "scene":
        return t, t

    if step == "confirm":
        if any(k in t for k in ("确认", "开始", "开写", "没问题", "可以", "好的", "行")):
            return True, "确认开始"
        return None

    return None


# ---------------------------------------------------------------------------
# 解析入口
# ---------------------------------------------------------------------------


def parse(
    step: str,
    text: str,
    answers: dict[str, Any],
    ctx: dict[str, Any],
    question: dict[str, Any],
) -> dict[str, Any]:
    """把一句话分成 作答 / 追问 / 没听懂 / 回退。

    返回：
      {"kind": "answer", "value": ..., "label": ..., "note": str|None}
      {"kind": "question"}
      {"kind": "clarify", "message": str}
      {"kind": "back"}
    """
    t = (text or "").strip()
    if not t:
        return {"kind": "clarify", "message": "没看到内容。" + fallback_for(step)}

    refused = refusal(t)
    if refused:
        return {"kind": "clarify", "message": refused}

    if is_back(t):
        return {"kind": "back"}

    # 明确要求「别问了 / 按默认来」：不再逐题追问，按默认值补齐后直达确认页。
    if step not in {"branch", "confirm"} and is_bypass(t):
        return {"kind": "bypass"}

    signals = (
        story.read_signals(t, tuple(flow.GENRE_HINTS), tuple(flow.STYLE_HINTS))
        if step in story.STORY_STEPS
        else {}
    )

    # 故事类问题收到模糊回答（「随便」「都行」）：给 2–3 个具体方向，
    # 而不是直接套默认配置开写。参数类问题（字数/段数/风格）保持原默认行为。
    if step in story.STORY_STEPS and _is_vague(t) and not signals:
        return {
            "kind": "clarify",
            "message": story.vague_message(
                step,
                answers,
                tuple(flow.GENRE_HINTS),
                {**ctx, "genre_hints": flow.GENRE_HINTS},
            ),
        }

    # 这句话里已经带了这一项的明确信息（「随便写个科幻」「第一人称吧」）：
    # 直接当成答案，不让「随便」把它拖回默认。
    if signals:
        value = signals.get(step)
        if step == "genre" and signals.get("genres"):
            value = signals["genres"][0]
        if value not in (None, "", []):
            return {
                "kind": "answer",
                "value": value,
                "label": flow.echo_label(step, value, answers.get("branch"), ctx),
                "note": None,
            }

    # 长文本不套默认值：避免整段话里出现「不知道」就被当成「你定」。
    if not is_long_text(t) and _is_default_word(t) and not signals:
        value = flow.default_for(step, answers, ctx)
        if value in (None, "", []):
            # 故事类问题没有默认值（如题材）：仍然给方向，不静默失败。
            return {
                "kind": "clarify",
                "message": story.vague_message(
                    step,
                    answers,
                    tuple(flow.GENRE_HINTS),
                    {**ctx, "genre_hints": flow.GENRE_HINTS},
                ),
            }
        label = flow.echo_label(step, value, answers.get("branch"), ctx)
        return {
            "kind": "answer",
            "value": value,
            "label": label,
            "note": "你没特别指定，我就按默认走了——这一步用默认最不容易翻车，之后想改随时说。",
        }

    if looks_like_question(t) and not _has_commit_verb(t) and not _has_number_with_unit(t):
        return {"kind": "question"}

    # 「第 N 段」指代已有正文：不能把它当成「写几段 / 每段多少字」的数字。
    seg_hint = segment_edit_hint(step, t, ctx)
    if seg_hint:
        return {"kind": "clarify", "message": seg_hint}

    # character/scene 是自由文本：先认控制指令和改设定，剩下的才当角色名/情境。
    ctrl = control_intent(step, t)
    if ctrl:
        return {"kind": "clarify", "message": control_message(step, ctrl)}

    match = _match(step, t, answers, ctx, question)
    if match is not None:
        value, label = match
        return {"kind": "answer", "value": value, "label": label, "note": None}

    if looks_like_question(t):
        return {"kind": "question"}

    # 方向步骤收到整段梗概：不替用户定方向，先问一句。
    if step == "branch" and is_long_text(t):
        return {
            "kind": "clarify",
            "message": (
                "这段像完整的想法，但方向还没定：你想写原创、续写，还是和角色对话？"
                "先定方向，再往下走。"
            ),
        }

    return {"kind": "clarify", "message": fallback_for(step)}


# ---------------------------------------------------------------------------
# 追问：就地解释（不推进流程）
# ---------------------------------------------------------------------------

def _corpus_phrase(ctx: dict[str, Any]) -> str:
    books = ctx.get("corpus_books") or []
    if not books:
        return "当前项目还没投喂语料"
    names = "、".join(
        f"《{re.sub(r'\.(txt|md|epub)$', '', str(b.get('title') or b.get('id')), flags=re.I)}》"
        f"（{b.get('chapters', 0)} 章）"
        for b in books
    )
    return f"当前项目投喂的是 {names}"


def _tier_phrase(ctx: dict[str, Any], key: str) -> str:
    for t in ctx.get("tiers") or []:
        if t["key"] == key:
            return (
                f"约 {t['sampled_chapters']} 章、{t['dimension_count']} 个维度、"
                f"预计约 {t['estimated_minutes']} 分钟"
            )
    return ""


def _faq_distill(ctx: dict[str, Any]) -> str:
    full = _tier_phrase(ctx, "full")
    balanced = _tier_phrase(ctx, "balanced")
    fast = _tier_phrase(ctx, "fast")
    dim = ctx.get("dimension_inventory") or {}
    measured = ctx.get("measured") or {}
    return (
        f"{_corpus_phrase(ctx)}。三个档位不是「准/中/快」三个形容词，差别落在四个"
        "可实现的轴上：采样多少章、跑哪些维度、要不要做角色名归一化、伪角色清洗多狠。\n\n"
        f"完整档：{full}。{dim.get('extract_count', 13)} 个提炼维度全开，角色名会做归一化（小落→安小落），"
        "伪角色也按词频自证全量清洗。\n"
        f"平衡档：{balanced}。维度一样，但不归一化角色名——简称会各算一个角色。\n"
        f"快速档：{fast}。砍掉节拍序列、潜台词模式两维（13→11），"
        "角色名完全不清洗，只适合试跑。\n\n"
        f"耗时口径要说明白：仓库里唯一一次带计时的实测是《{measured.get('novel', '').strip('《》')}》"
        f"语料 {measured.get('corpus_chapters')} 章、n_ch={measured.get('sampled_chapters')}、"
        f"{measured.get('minutes')} 分钟（{measured.get('model')}）；上面每档的分钟数是按这一刻"
        "线性外推的估算，不是该档实测值。"
    )


def _faq_dimensions(ctx: dict[str, Any]) -> str:
    dim = ctx.get("dimension_inventory") or {}
    extract = "、".join(dim.get("extract") or [])
    forbidden = "、".join(dim.get("forbidden") or [])
    return (
        f"维度不是一个孤零零的「{dim.get('extract_count', 13)}」。代码里的口径是：\n"
        f"- {dim.get('extract_count', 13)} 个提炼维度（NARRATIVE_DIMENSIONS）：{extract}\n"
        f"- 运行时额外算 1 个「文风漂移分析」（开头与结尾的文风差多少），"
        f"落库合计 {dim.get('stored_count', 14)} 个维度\n"
        f"- 另有 {dim.get('forbidden_count', 4)} 个反向禁忌维度：{forbidden}\n\n"
        f"所以全部维度键是 {dim.get('total_keys', 18)} 个。快速档砍的是节拍序列、"
        "潜台词模式这两个提炼维度，落库维度随之从 14 降到 12。"
    )


def _faq_normalize(ctx: dict[str, Any]) -> str:
    measured = ctx.get("measured") or {}
    return (
        "归一化干的事是「把同一个人的简称并回全名」。不做的直接后果不是文风差一点，"
        "而是角色声音词典会把一个人拆成好几个人：\n\n"
        "图谱是按提及次数抽角色的。书里「小落」出现得比「安小落」多，两个名字就各占一个名额，"
        "各自学一套口头禅和句长。生成时你写「安小落」，档案里可能只匹配到「小落」那一份，"
        "或者两份打架——同一个人一会儿冷嘲一会儿软语。\n\n"
        "完整档的做法是拿语料词频做包含关系自证：短名如果高频出现在长名里，就并进长名。"
        f"实测里真截断的占比 ≤1.1%、真名 ≥67%，分得很开；像那种 "
        f"{measured.get('corpus_chapters', 833)} 章的语料，513 个角色节点最后收敛成 19 个规范名"
        "+12 条别名映射，还把「相信/冷声」这种伪角色清掉了。平衡档不做这一步，所以快，"
        "但简称噪声会留在档案里。"
    )


def _faq_full(ctx: dict[str, Any]) -> str:
    total = int(ctx.get("corpus_total_chapters") or 0)
    full = next((t for t in (ctx.get("tiers") or []) if t["key"] == "full"), {})
    ratio = int((full.get("sample_ratio") or 0) * 100)
    # 全本耗时不能取档位的 estimated_minutes：采样被覆盖成 200 章后，
    # 那个数会变成 200 章的估算，和「全本 1528 章」对不上。
    full_minutes = ctx.get("full_corpus_minutes") or full.get("estimated_minutes") or "?"
    hours = ctx.get("full_corpus_hours")
    duration = f"{full_minutes} 分钟" + (f"（约 {hours} 小时）" if hours else "")
    return (
        f"先说现状：代码里没有第四个「全本」档位，只有完整/平衡/快速三档。"
        f"完整档本身是按语料 {ratio}% 采样，不是整本灌进去。\n\n"
        f"你要的「全本完整提炼」可以做到：我可以把采样章数覆盖成这批语料共 {total} 章"
        "（0 在我这里表示全本），维度和清洗仍按完整档走。这个覆盖会写进提炼指纹，"
        "所以改完必须重跑，不会命中旧档案。\n\n"
        f"代价是时间随字数线性涨：全本 {total} 章按唯一实测点外推大约 "
        f"{duration}；"
        "如果只是想验证效果，先压到 200 章更划算，确认方向对了再跑全本。"
    )


def _faq_cost(ctx: dict[str, Any]) -> str:
    full = _tier_phrase(ctx, "full")
    balanced = _tier_phrase(ctx, "balanced")
    return (
        f"耗时分两段。提炼：{_corpus_phrase(ctx)}，完整档 {full}；平衡档 {balanced}；"
        "快速档分钟级。生成：一段 3000 字通常一两分钟，段数是串行的，2 段基本就是 2 倍。\n\n"
        "钱按 token 算，语料越长提炼越贵——这也是快速档存在的意义：先确认管线通不通，"
        "再决定要不要花完整档的时间。所有分钟数都是按文档里唯一一次实测外推的估算，"
        "换模型或限流会变。"
    )


_FAQ_CONTINUE = (
    "不能。生成链路是「读前面已写内容的摘要，然后往后追加」，它没有在前面某段插入、"
    "再重写后续的能力，所以我只把最后一段作为起点。\n\n"
    "想从中间开始，得先在项目工作台把后面的段删掉，或者新建一个项目只导入到你要的位置，"
    "再回来续写。这样不会破坏已有正文。"
)

_FAQ_CORPUS = (
    "语料就是你想模仿的书，在「语料库」里上传 txt 导入后，这一步才选得到。"
    "选中的书会在提炼时被压成文风档案（句式、节奏、场景习惯），生成时注入到提示词里。\n\n"
    "它只借用写法，不会把原文抄进你的正文，也不会改写语料本身。"
)

_FAQ_OUTLINE = (
    "大纲是一份剧情圣经：角色、关系、核心冲突、伏笔，还有后续走向，先进项目，"
    "你确认后我才写正文。写歪了可以回头改大纲再重生成，比改正文便宜。\n\n"
    "不想要这一轮就选「直接开写」，正文照样会参考已写内容的摘要。"
)

_FAQ_LENGTH = (
    "3000 字是默认值，也是比较稳的落点：够展开一个完整场景加一次转折。"
    "低于 1500 基本只剩一个场景；超过 5000 模型容易注水、重复前面的描写，单次耗时也明显变长。\n\n"
    "先按 3000 试一段，觉得拖就降到 1500–2000，觉得没展开就提到 4000。"
)

_FAQ_BACK = (
    "可以。点卡片上的「上一步」我就退回上一题，之前选过的不丢，卡片会带回原来的值；"
    "在最后的汇总页，点任意一行也能直接跳回那一项改。刷新页面也不会丢——进度存在服务端。"
)

_FAQ_ARCHIVE = (
    "「沿用现有档案」不是重新提炼，而是直接拿项目里已经算好的那份文风档案来写："
    "秒级完成、不花模型钱，适合你只是想接着上次的文风写。\n\n"
    "它的风险是可能和现在的语料/档位不一致（比如你后来加了参考书），"
    "想确认一致就说「重新提炼」，我会按当前参数重跑。"
)


def _faq(text: str, ctx: dict[str, Any]) -> str | None:
    t = text.strip()
    if re.search(r"(归一化|别名|简称|小落|安小落|角色名).*(不准|为什么|影响|怎么|啥|什么)|"
                 r"为什么.*(不|没).*归一化", t):
        return _faq_normalize(ctx)
    if re.search(r"(维度|多少维|14\s*维|15\s*维|19\s*维|维数)", t):
        return _faq_dimensions(ctx)
    if re.search(r"(全本|全量|整本|全部.*灌|完整语料)", t):
        return _faq_full(ctx)
    if re.search(r"(全量|完整).*(提炼|蒸馏|档)|(提炼|蒸馏).*(区别|差别|档位|是什么|啥)|42\s*分钟|"
                 r"三个档|三档", t):
        return _faq_distill(ctx)
    if re.search(r"(续写|接着写|往下写).*(中间|指定|某段|跳|第几段)|能.*从.*段.*开始", t):
        return _FAQ_CONTINUE
    if re.search(r"(耗时|多久|多长时间|成本|多少钱|贵|慢|快)\s*(呢|吗|？|\?)?$", t) or "要多久" in t:
        return _faq_cost(ctx)
    if re.search(r"(沿用|现有档案|复用|不重跑)", t):
        return _FAQ_ARCHIVE
    if re.search(r"语料.*(什么|哪|怎么|上传|来源|干|用)|语料库", t):
        return _FAQ_CORPUS
    if re.search(r"大纲|剧情圣经|bible", t):
        return _FAQ_OUTLINE
    if re.search(r"(字数|多少字|篇幅|多长)", t):
        return _FAQ_LENGTH
    if re.search(r"(上一步|回退|返回|改前面|跳回)", t):
        return _FAQ_BACK
    return None


#: 「最近什么题材好卖」：这类问题必须用带来源的行情卡回答，不能让模型凭记忆编。
_MARKET_Q = re.compile(
    r"(题材|类型).{0,8}(好卖|火|热门|趋势|行情|吃香|市场)"
    r"|(什么|哪个|哪种|哪类).{0,6}(题材|类型).{0,6}(好|火|热|流行)"
    r"|最近的?风向|市场行情|什么好卖|网文趋势"
)

#: 「什么主角讨喜」：同样必须带来源与样本口径，不能当事实下判断。
_POPULAR_Q = re.compile(
    r"(人设|主角|性格).{0,8}(讨喜|受欢迎|吃香|好卖)"
    r"|什么.{0,6}(人设|性格|主角).{0,6}(好|讨喜|受欢迎)"
)


def _market_answer(
    step: str,
    text: str,
    answers: dict[str, Any],
    ctx: dict[str, Any],
) -> str | None:
    """用独立行情卡回答「最近什么题材好卖 / 什么主角讨喜」。

    卡过期或没有匹配题材时返回 None，继续走原来的解释路径；
    绝不用模型记忆补一条「行情」。
    """
    today = ctx.get("today")
    if _MARKET_Q.search(text):
        block = market.genre_block(today=today)
        if block:
            return block
    if step == "protagonist" and _POPULAR_Q.search(text):
        directions = market.protagonist_directions(answers.get("genre"), today=today)
        if directions:
            lines = "\n".join(f"{i}. {d['text']}" for i, d in enumerate(directions, 1))
            return (
                "近期平台编辑口径里，这类人设出现得多：\n"
                + lines
                + "\n\n注意：这是编辑解读与投放视角，样本口径未公开，也存在幸存者偏差；"
                "「讨喜」不能当事实，只能当参考方向。"
            )
    return None


def explain(
    step: str,
    text: str,
    answers: dict[str, Any],
    ctx: dict[str, Any],
    question: dict[str, Any],
) -> str:
    """就地回答追问；无论答成什么，调用方都不会推进流程。"""
    fixed = _faq(text, ctx)
    if fixed:
        return fixed

    market_reply = _market_answer(step, text, answers, ctx)
    if market_reply:
        return market_reply

    chars = ctx.get("characters") or []
    if re.search(r"(角色|人物).*(哪些|谁|列表|几个|有什么)", text) and chars:
        names = "、".join(str(c["name"]) for c in chars[:12])
        source = "旧版知识图谱" if ctx.get("character_source") == "legacy" else "本项目知识图谱"
        return (
            f"这一步能选的角色来自{source}，按提及次数排前 12 个：{names}。\n\n"
            "列表里没有的人也可以直接打名字；如果名字明显不对（像「许七」这种碎片），"
            "那是图谱里的别名没合并，不影响你选完整人名。"
        )

    if re.search(r"(默认|推荐|建议)", text):
        card = question.get("card") or {}
        default = card.get("default")
        title = question.get("title") or "这一步"
        if default is None or default == "":
            return (
                f"「{title}」这项我没有设默认，因为选错会连带影响后面的问题。"
                "你按自己的偏好选就行，选完还能回退。"
            )
        return (
            f"这一步的默认是「{flow.echo_label(step, default, answers.get('branch'), ctx)}」。"
            "我建议先用默认走完一轮，看到正文效果再回来微调——比一开始纠结参数划算。"
        )

    # 固定知识里没有：让助手模型基于这一步的原文回答（不推进、不替用户决定）。
    try:
        from app.llm.provider import get_assistant_provider

        system = (
            "你是小说创作助手的引导员。用户正在选择「"
            + str(question.get("title") or "")
            + "」这一步。\n\n这一步的说明是：\n"
            + str(question.get("message") or "")
            + "\n\n【可用事实清单（唯一允许引用的数字）】\n"
            + str(ctx.get("fact_sheet") or "（无事实清单，不要给任何具体数字）")
            + "\n\n要求：1) 只回答用户的问题，不替用户做选择、不催促；"
            "2) 可以往下追一层——先给结论，再解释机制或后果，但不要写成条款列表；"
            "3) 只能使用上面事实清单里的数字/章数/维度数/耗时，清单没有就说不知道，"
            "严禁自己编；4) 200 字以内，直接说。"
        )
        reply = get_assistant_provider().chat(system, text)
        reply = (reply or "").strip()
        if reply:
            return reply
    except Exception:  # noqa: BLE001 —— 模型失败不能挡住引导
        pass

    head = str(question.get("message") or "").split("\n\n")[0]
    return (
        "这个问题我这边没法展开（模型调用失败），简单说："
        + head
        + "\n\n你可以先按默认走，之后随时能回退改。"
    )
