"""引导对话里的「自由输入」解析：直接打字也能答，中途追问就地解释。

三件事，全部是纯逻辑（不碰 DB、不依赖 FastAPI）：

1. `parse()` —— 把用户这句话分成三种：**作答** / **追问** / **没听懂**。
   追问永远不推进流程（路由层原样停在当前题）。
2. `explain()` —— 追问就地展开：先查该主题的固定知识（蒸馏档位、续写起点、
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

_BACK_WORDS = ("上一步", "返回上一步", "退回上一步", "退回去", "回去改", "改上一步")

_NUM = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


# ---------------------------------------------------------------------------
# 基础判断
# ---------------------------------------------------------------------------


def _is_default_word(text: str) -> bool:
    return any(w in text for w in DEFAULT_WORDS)


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
        rules = (
            (("原创", "新书", "自己写", "从零", "从头写"), flow.KIND_ORIGINAL),
            (("续写", "接着写", "往下写", "继续写", "接着"), flow.KIND_CONTINUATION),
            (("角色对话", "角色", "扮演", "聊天", "对话"), flow.KIND_ROLE),
        )
        for keys, value in rules:
            if any(k in t for k in keys):
                return value, flow.KIND_LABELS[value]
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
        return {"kind": "clarify", "message": "没看到内容，你直接说选哪个就行。"}

    if t in _BACK_WORDS or any(t == w for w in _BACK_WORDS):
        return {"kind": "back"}

    if _is_default_word(t):
        value = flow.default_for(step, answers, ctx)
        label = flow.echo_label(step, value, answers.get("branch"), ctx)
        return {
            "kind": "answer",
            "value": value,
            "label": label,
            "note": "你没特别指定，我就按默认走了——这一步用默认最不容易翻车，之后想改随时说。",
        }

    if looks_like_question(t) and not _has_commit_verb(t) and not _has_number_with_unit(t):
        return {"kind": "question"}

    match = _match(step, t, answers, ctx, question)
    if match is not None:
        value, label = match
        return {"kind": "answer", "value": value, "label": label, "note": None}

    if looks_like_question(t):
        return {"kind": "question"}

    return {
        "kind": "clarify",
        "message": (
            "我没听懂这句算选哪个。可以直接点上面的卡片，或者换个说法——"
            "比如「完整档」「3000 字」「先出大纲」。"
        ),
    }


# ---------------------------------------------------------------------------
# 追问：就地解释（不推进流程）
# ---------------------------------------------------------------------------

_FAQ_DISTILL = (
    "全量蒸馏就是完整档，也是我这的默认：按语料 30% 的篇幅抽样（撒开抽，不是只取开头），"
    "跑 14 个维度——句长节奏、对话密度、场景模板、潜台词、章末钩子这些——"
    "再拿语料本身校验角色名，把「许七」「安小」这种别名碎片和明显不是人的名字清掉。\n\n"
    "833 章的实测大约 42 分钟。平衡档固定取 30 章、维度一样但不做角色名归一化，快不少；"
    "快速档只跑 11 个维度、不清洗，只够验证管线。差的不是「能不能写」，是「像不像」。"
)

_FAQ_CONTINUE = (
    "不能。生成链路是「读前面已写内容的摘要，然后往后追加」，它没有在前面某段插入、"
    "再重写后续的能力，所以我只把最后一段作为起点。\n\n"
    "想从中间开始，得先在项目工作台把后面的段删掉，或者新建一个项目只导入到你要的位置，"
    "再回来续写。这样不会破坏已有正文。"
)

_FAQ_COST = (
    "耗时主要在两处：蒸馏和生成。蒸馏完整档 833 章约 42 分钟；平衡/快速固定 30 章，分钟级。"
    "生成按段算，一段 3000 字通常一两分钟，具体看模型和限流；段数是串行的，时间基本按倍数走。\n\n"
    "钱按 token 算，语料越长蒸馏越贵——这也是快速档存在的意义：先确认管线通不通，"
    "再决定要不要花完整档的时间。"
)

_FAQ_CORPUS = (
    "语料就是你想模仿的书，在「语料库」里上传 txt 导入后，这一步才选得到。"
    "选中的书会在蒸馏时被压成文风档案（句式、节奏、场景习惯），生成时注入到提示词里。\n\n"
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


def _faq(text: str) -> str | None:
    t = text.strip()
    if re.search(r"(全量|完整).*(蒸馏|档)|蒸馏.*(区别|差别|档位|是什么|啥)|42\s*分钟", t):
        return _FAQ_DISTILL
    if re.search(r"(续写|接着写|往下写).*(中间|指定|某段|跳|第几段)|能.*从.*段.*开始", t):
        return _FAQ_CONTINUE
    if re.search(r"(耗时|多久|多长时间|成本|多少钱|贵|慢|快)\s*(呢|吗|？|\?)?$", t) or "要多久" in t:
        return _FAQ_COST
    if re.search(r"语料.*(什么|哪|怎么|上传|来源|干|用)|语料库", t):
        return _FAQ_CORPUS
    if re.search(r"大纲|剧情圣经|bible", t):
        return _FAQ_OUTLINE
    if re.search(r"(字数|多少字|篇幅|多长)", t):
        return _FAQ_LENGTH
    if re.search(r"(上一步|回退|返回|改前面|跳回)", t):
        return _FAQ_BACK
    return None


def explain(
    step: str,
    text: str,
    answers: dict[str, Any],
    ctx: dict[str, Any],
    question: dict[str, Any],
) -> str:
    """就地回答追问；无论答成什么，调用方都不会推进流程。"""
    fixed = _faq(text)
    if fixed:
        return fixed

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
            + "\n\n要求：只回答用户的问题，不要替用户做选择，不要催促，"
            "不要编造上面没有的数字或承诺。150 字以内，直接说结论。"
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
