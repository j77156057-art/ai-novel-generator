"""对话式创作引导的状态机（纯逻辑，不依赖 DB / FastAPI）。

设计约束（来自产品需求）：

- **每一步只问一件事**，卡片类型固定为 single / multi / number / text / confirm；
- 每条问题都带一句 `why`，解释「为什么要问这个」，让不熟流程的人也能做决定；
- 分支三条：原创 / 续写 / 角色对话，后续步骤由已选项动态决定
  （例如没选参考语料就不问蒸馏档位）；
- 支持回退（history 栈）、跳过有默认值的步骤、最后汇总确认。

这里只做「下一步是哪题 / 这题长什么样 / 这个答案合不合法」，
不碰数据库、不调模型、不写项目字段——副作用全部在
`app.routers.assistant_wizard` 里，方便单独测状态迁移。
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# 分支
# ---------------------------------------------------------------------------

KIND_ORIGINAL = "original"
KIND_CONTINUATION = "continuation"
KIND_ROLE = "role_chat"

KINDS = (KIND_ORIGINAL, KIND_CONTINUATION, KIND_ROLE)

FIRST_STEP = "branch"

#: 各分支的展示名（汇总行 / 用户气泡回显用）。
KIND_LABELS = {
    KIND_ORIGINAL: "原创小说",
    KIND_CONTINUATION: "续写",
    KIND_ROLE: "角色对话",
}

#: 原创题材与一句调性说明。与 `routers/wizard.py::_GENRE_HINT` 同口径，
#: 但那边是提示词用的长句，这里只取给用户看的一句——不 import 它，
#: 避免纯逻辑模块反向依赖 FastAPI 路由。
GENRE_HINTS = {
    "科幻": "硬核设定、近未来或远未来、技术与人的张力",
    "都市": "现代城市、职场/悬疑/情感、接地气的日常与冲突",
    "悬疑": "谜团驱动、反转、不可靠叙事、真相延迟揭示",
    "言情": "情感关系主线、人物羁绊、细腻心理",
    "奇幻": "架空世界、魔法/种族/史诗、宏大设定",
    "仙侠": "修真飞升、门派恩怨、从凡到仙的成长",
    "历史": "真实或架空的过去、权谋、时代洪流下的个人",
    "游戏": "系统/副本/升级、爽文节奏、数值化成长",
}

STYLE_HINTS = {
    "番茄": "快节奏、强钩子、口语化、每章留悬念",
    "起点": "设定严谨、长线伏笔、世界观完整、热血或深沉",
    "晋江": "情感细腻、人物内心、CP 张力、文风清新或华丽",
    "豆瓣阅读": "文学质感、现实向、克制、留白与隐喻",
    "知乎盐选": "悬疑短钩子、第一人称代入、反转结尾",
}

#: 「不投喂」在多选里的哨兵值。0 不是合法的 novel_id（自增从 1 开始）。
CORPUS_NONE = 0

#: 沿用已有文风档案（不重跑蒸馏）的哨兵值。
DISTILL_REUSE = "reuse"

LENGTH_MIN = 500
LENGTH_MAX = 8000
CHAPTERS_MIN = 1
CHAPTERS_MAX = 10


# ---------------------------------------------------------------------------
# 步骤序列
# ---------------------------------------------------------------------------


def step_order(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    """按分支与已选项算出完整步骤序列。

    序列是**动态**的：选了语料才问蒸馏；项目本来就有参考语料或文风档案
    才问「是否沿用原风格」。回退时用同一条序列反查上一题，保证前后一致。
    """
    if kind == KIND_ORIGINAL:
        steps = [FIRST_STEP, "corpus"]
        if _has_corpus(answers, ctx):
            steps.append("distill")
        steps += ["genre", "style", "length", "chapters", "outline", "confirm"]
        return steps
    if kind == KIND_CONTINUATION:
        steps = [FIRST_STEP, "continue_from"]
        if ctx.get("needs_distill"):
            steps.append("distill")
        steps += ["length", "chapters", "confirm"]
        return steps
    if kind == KIND_ROLE:
        return [FIRST_STEP, "character", "scene", "confirm"]
    # 还没选方向：只有第一题
    return [FIRST_STEP]


def _has_corpus(answers: dict[str, Any], ctx: dict[str, Any]) -> bool:
    if "corpus" in answers:
        return bool(answers.get("corpus"))
    return bool(ctx.get("project_has_corpus"))


def next_step(
    kind: str | None,
    current: str,
    answers: dict[str, Any],
    ctx: dict[str, Any],
) -> str | None:
    """当前题的下一题；已到末尾返回 None。"""
    order = step_order(kind, answers, ctx)
    if current not in order:
        # 当前题因上游改动不再适用（例如把语料改成「不投喂」后蒸馏题消失）：
        # 从新序列里找第一个尚未回答的题，找不到就回到最后一题。
        for key in order:
            if key not in answers:
                return key
        return order[-1] if order else None
    idx = order.index(current)
    return order[idx + 1] if idx + 1 < len(order) else None


def prev_step(history: list[str], current: str) -> str | None:
    """回退一题：history 是「走到当前题之前走过的步骤」。"""
    if history:
        return history[-1]
    return None if current == FIRST_STEP else FIRST_STEP


# ---------------------------------------------------------------------------
# 问题渲染
# ---------------------------------------------------------------------------


def build_question(step: str, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """把步骤渲染成前端可直接画的中性结构。"""
    kind = answers.get("branch")
    order = step_order(kind, answers, ctx)
    index = order.index(step) + 1 if step in order else len(order)

    builders = {
        "branch": _q_branch,
        "corpus": _q_corpus,
        "distill": _q_distill,
        "genre": _q_genre,
        "style": _q_style,
        "length": _q_length,
        "chapters": _q_chapters,
        "outline": _q_outline,
        "continue_from": _q_continue_from,
        "character": _q_character,
        "scene": _q_scene,
        "confirm": _q_confirm,
    }
    builder = builders.get(step)
    if builder is None:
        raise ValueError(f"未知引导步骤：{step}")
    body = builder(kind, answers, ctx)
    card = body.get("card") or {}
    has_default = card.get("default") is not None and card.get("default") != ""
    body.update(
        {
            "step": step,
            "progress": {
                "index": index,
                # 还没选方向时整条序列只有第一题，用 0 表示「总数待定」，
                # 前端显示成「第 1 步」而不是别扭的「第 1/1 步」。
                "total": 0 if len(order) == 1 and step == FIRST_STEP else len(order),
            },
            "can_back": bool(prev_step(ctx.get("history") or [], step)),
            "can_skip": step in SKIPPABLE and step not in answers and has_default,
        }
    )
    return body


#: 允许「跳过 = 用默认值」的步骤。必答项（方向、语料、确认）不在其中。
SKIPPABLE = {"genre", "style", "length", "chapters", "outline", "scene"}


def _q_branch(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "这次想做哪一种？",
        "why": "原创、续写、角色对话三条路要填的参数完全不同。先定方向，后面就只问相关的。",
        "card": {
            "type": "single",
            "field": "branch",
            "default": answers.get("branch"),
            "options": [
                {
                    "value": KIND_ORIGINAL,
                    "label": "原创小说",
                    "desc": "从零起一本新书：语料、风格、题材、字数、章节数",
                },
                {
                    "value": KIND_CONTINUATION,
                    "label": "续写",
                    "desc": "接着已有正文往下写：起点、字数、是否沿用原风格",
                },
                {
                    "value": KIND_ROLE,
                    "label": "角色对话",
                    "desc": "和书里某个角色聊天：选角色、定情境",
                },
            ],
        },
    }


def _q_corpus(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    novels = ctx.get("novels") or []
    options = [
        {
            "value": int(n["id"]),
            "label": n.get("title") or f"语料 #{n['id']}",
            "desc": " · ".join(x for x in [n.get("genre") or "", n.get("author") or ""] if x)
            or "已上传语料",
        }
        for n in novels
    ]
    options.append(
        {
            "value": CORPUS_NONE,
            "label": "不投喂（自由生成）",
            "desc": "不参考任何现成作品，模型只按题材和风格自由写",
        }
    )
    return {
        "title": "有想参考的语料吗？",
        "why": "选中后模型会借鉴这些书的写法与情节密度；不选就是完全自由生成。可多选。",
        "card": {
            "type": "multi",
            "field": "corpus",
            "default": answers.get("corpus", []),
            "exclusive_value": CORPUS_NONE,
            "empty_hint": None
            if novels
            else "还没上传语料？先去「语料库」上传 txt，再回来选。",
            "options": options,
        },
    }


def _q_distill(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tiers = ctx.get("tiers") or []
    options = []
    cleaning = {
        "full": "全量伪角色清洗",
        "partial": "部分伪角色清洗",
        "none": "不清洗伪角色",
    }
    for t in tiers:
        if t.get("sampling_mode") == "percentage":
            sampling = f"按语料 {int(round(float(t.get('sample_ratio') or 0) * 100))}% 采样"
        else:
            sampling = f"固定 {t.get('fixed_chapters') or 30} 章"
        desc = f"{sampling}｜{t['dimension_count']} 维｜{t['cost_hint']}"
        if t.get("normalize_character_names"):
            desc += "｜角色名归一化"
        else:
            desc += "｜不做角色名归一化"
        desc += "｜" + cleaning.get(t.get("fake_character_cleaning"), "")
        if t["key"] == "full":
            desc += "｜参考：833 章语料约 42 分钟（docs 实测）"
        options.append(
            {
                "value": t["key"],
                "label": f"{t['name']}档",
                "desc": desc,
            }
        )
    if ctx.get("archive_exists"):
        options.append(
            {
                "value": DISTILL_REUSE,
                "label": "沿用现有档案（不重跑）",
                "desc": "项目里已有蒸馏结果，直接复用，秒级完成",
            }
        )
    return {
        "title": "文风蒸馏做到什么程度？",
        "why": (
            "蒸馏把参考语料提炼成可复用的文风档案，档位越高越像原著、也越慢越贵。"
            "选了语料时，生成前必须先有档案，所以我不会给你「跳过」这个选项。"
        ),
        "card": {
            "type": "single",
            "field": "distill",
            "default": answers.get("distill") or ctx.get("default_tier"),
            "options": options,
        },
    }


def _q_genre(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "想写什么题材？",
        "why": "题材决定世界观和读者预期，也影响模型对冲突、节奏的默认写法。",
        "card": {
            "type": "single",
            "field": "genre",
            "default": answers.get("genre"),
            "options": [
                {"value": g, "label": g, "desc": hint} for g, hint in GENRE_HINTS.items()
            ],
        },
    }


def _q_style(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "想要哪种平台调性？",
        "why": "同样是玄幻，番茄和豆瓣读起来是两本书。风格平台决定叙述节奏和爽点密度。",
        "card": {
            "type": "single",
            "field": "style",
            "default": answers.get("style"),
            "options": [
                {"value": s, "label": s, "desc": hint} for s, hint in STYLE_HINTS.items()
            ],
        },
    }


def _q_length(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    title = "这一段写多少字？" if kind == KIND_CONTINUATION else "每段写多少字？"
    return {
        "title": title,
        "why": "单段字数决定一次生成的篇幅。3000 是默认值：太长容易注水，太短情节展不开。",
        "card": {
            "type": "number",
            "field": "length",
            "default": answers.get("length", ctx.get("generation_length") or 3000),
            "min": LENGTH_MIN,
            "max": LENGTH_MAX,
            "step": 500,
            "unit": "字",
            "quick": [1500, 3000, 4000],
        },
    }


def _q_chapters(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "这次连着写几段？",
        "why": "串行生成、后段会读前段摘要。一次写太多容易前后矛盾，建议先写 1 段看手感。",
        "card": {
            "type": "number",
            "field": "chapters",
            "default": answers.get("chapters", 1),
            "min": CHAPTERS_MIN,
            "max": CHAPTERS_MAX,
            "step": 1,
            "unit": "段",
            "quick": [1, 2, 3],
        },
    }


def _q_outline(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "要不要先出大纲再动笔？",
        "why": "大纲（剧情圣经）会先列角色、关系、冲突和伏笔，你确认后我再写正文，能少走弯路。",
        "card": {
            "type": "confirm",
            "field": "outline",
            "default": answers.get("outline", True),
            "options": [
                {"value": True, "label": "先出大纲，我确认后再写", "desc": "更稳，多一轮确认"},
                {"value": False, "label": "直接开写", "desc": "最快拿到正文，风险自负"},
            ],
        },
    }


def _q_continue_from(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    segments = list(ctx.get("segments") or [])
    if segments:
        latest = max(segments)
        options = [
            {
                "value": int(latest),
                "label": f"接在第 {latest} 段之后",
                "desc": "现有链路只支持「接在最后一段之后」，这是唯一合法的续写起点",
            }
        ]
        default = latest
    else:
        options = [
            {
                "value": 0,
                "label": "从第 1 段开始写",
                "desc": "本项目还没有正文，续写会从第一段起步",
            }
        ]
        default = 0
    return {
        "title": "从哪里接着写？",
        "why": "续写会接着已有正文的结尾往下接。现有生成链路没有「跳到中间某段」的能力，所以只给最后一段。",
        "card": {
            "type": "single",
            "field": "continue_from",
            "default": answers.get("continue_from", default),
            "options": options,
        },
    }


def _q_character(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    chars = ctx.get("characters") or []
    if not chars:
        return {
            "title": "想和哪个角色对话？",
            "why": "项目里还没有角色卡。直接输入角色名，我会用最基础的设定陪你聊；想更准，先去 KG 工作台建图。",
            "card": {
                "type": "text",
                "field": "character",
                "default": answers.get("character", ""),
                "placeholder": "角色名，例如：许七安",
            },
        }
    legacy = ctx.get("character_source") == "legacy"
    why = (
        "角色卡来自旧版知识图谱（新图谱还没有角色），按提及次数排序，可能混进非人物名字。"
        "选谁决定我用谁的口吻和视角。"
        if legacy
        else "角色卡来自本项目的知识图谱，包含身份、关系和已知情报。选谁决定我用谁的口吻和视角。"
    )
    return {
        "title": "想和哪个角色对话？",
        "why": why,
        "card": {
            "type": "single",
            "field": "character",
            "default": answers.get("character"),
            "options": [
                {
                    "value": c["name"],
                    "label": c["name"],
                    "desc": (
                        f"旧图谱提及 {c.get('mention_count', 0)} 次"
                        if legacy
                        else f"图谱提及 {c.get('mention_count', 0)} 次"
                    )
                    + (" · 有秘密情报" if c.get("has_secrets") else ""),
                }
                for c in chars[:12]
            ],
        },
    }


def _q_scene(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "把对话放在什么情境里？",
        "why": "同一句台词在不同情境下意思不同。给个场景，我能用对语气、称呼和该知道的信息。",
        "card": {
            "type": "text",
            "field": "scene",
            "default": answers.get("scene", "当前剧情时间线"),
            "placeholder": "例如：大劫之后，他在城头等你来解释",
            "suggestions": ["当前剧情时间线", "故事开头，彼此还不熟", "一场对峙之后"],
        },
    }


def _q_confirm(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    rows = summary_rows(kind, answers, ctx)
    label = {
        KIND_ORIGINAL: "保存配置并开始",
        KIND_CONTINUATION: "保存配置并续写",
        KIND_ROLE: "进入角色对话",
    }.get(kind or "", "开始")
    return {
        "title": "确认一下，没问题就开始",
        "why": "下面是你选的参数。想改哪一项，点「上一步」回去改，或者直接点这一行跳回去。",
        "card": {
            "type": "confirm",
            "field": "confirm",
            "default": True,
            "action_label": label,
            "summary": rows,
        },
    }


# ---------------------------------------------------------------------------
# 答案校验 / 汇总
# ---------------------------------------------------------------------------


class AnswerError(ValueError):
    """答案不合法（路由层转 400，不静默改成默认值）。"""


def validate(
    step: str,
    raw: Any,
    kind: str | None,
    answers: dict[str, Any],
    ctx: dict[str, Any],
) -> Any:
    """校验并归一化用户提交的值。非法一律抛 AnswerError。"""
    if step == "branch":
        value = str(raw or "").strip()
        if value not in KINDS:
            raise AnswerError(f"方向只能是 {' / '.join(KINDS)}")
        return value

    if step == "corpus":
        if not isinstance(raw, list):
            raise AnswerError("语料必须是一个列表")
        values: list[int] = []
        for item in raw:
            try:
                values.append(int(item))
            except (TypeError, ValueError) as exc:
                raise AnswerError(f"非法语料 id：{item!r}") from exc
        values = list(dict.fromkeys(values))
        if CORPUS_NONE in values and len(values) > 1:
            raise AnswerError("「不投喂」不能和具体语料同时选")
        if CORPUS_NONE in values:
            return []
        known = {int(n["id"]) for n in (ctx.get("novels") or [])}
        unknown = [v for v in values if v not in known]
        if unknown:
            raise AnswerError(f"语料不存在：{unknown}")
        return values

    if step == "distill":
        value = str(raw or "").strip()
        valid = {t["key"] for t in (ctx.get("tiers") or [])}
        if value == DISTILL_REUSE:
            if not ctx.get("archive_exists"):
                raise AnswerError("当前项目还没有可沿用的文风档案")
            return value
        if value not in valid:
            raise AnswerError(f"蒸馏档位只能是 {' / '.join(sorted(valid))}")
        return value

    if step == "genre":
        value = str(raw or "").strip()
        if value not in GENRE_HINTS:
            raise AnswerError("未知题材")
        return value

    if step == "style":
        value = str(raw or "").strip()
        if value not in STYLE_HINTS:
            raise AnswerError("未知风格平台")
        return value

    if step == "length":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise AnswerError("字数必须是整数") from exc
        if not (LENGTH_MIN <= value <= LENGTH_MAX):
            raise AnswerError(f"字数需在 {LENGTH_MIN}–{LENGTH_MAX} 之间")
        return value

    if step == "chapters":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise AnswerError("段数必须是整数") from exc
        if not (CHAPTERS_MIN <= value <= CHAPTERS_MAX):
            raise AnswerError(f"段数需在 {CHAPTERS_MIN}–{CHAPTERS_MAX} 之间")
        return value

    if step in {"outline", "confirm"}:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        text = str(raw or "").strip().lower()
        if text in {"true", "yes", "1", "y"}:
            return True
        if text in {"false", "no", "0", "n"}:
            return False
        raise AnswerError("请选择「是」或「否」")

    if step == "continue_from":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise AnswerError("续写起点必须是段号") from exc
        valid = set(ctx.get("segments") or [])
        if value != 0 and value not in valid:
            raise AnswerError(f"第 {value} 段不存在")
        return value

    if step == "character":
        value = str(raw or "").strip()
        if not value:
            raise AnswerError("角色名不能为空")
        if len(value) > 40:
            raise AnswerError("角色名过长（最多 40 字）")
        return value

    if step == "scene":
        value = str(raw or "").strip()
        if len(value) > 200:
            raise AnswerError("情境描述最多 200 字")
        return value or "当前剧情时间线"

    raise AnswerError(f"未知步骤：{step}")


def default_for(step: str, answers: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """「跳过」时用的默认值：直接把卡片里的 default 交回去再走一遍校验。"""
    card = build_question(step, answers, ctx)["card"]
    if step == "outline":
        return bool(card.get("default", True))
    if step == "corpus":
        return list(card.get("default") or [])
    return card.get("default")


def echo_label(step: str, value: Any, kind: str | None, ctx: dict[str, Any]) -> str:
    """把提交的值翻成一句人话，作为用户气泡回显。"""
    if step == "branch":
        return KIND_LABELS.get(value, str(value))
    if step == "corpus":
        if not value:
            return "不投喂语料（自由生成）"
        titles = {int(n["id"]): n.get("title") or f"#{n['id']}" for n in (ctx.get("novels") or [])}
        return "参考语料：" + "、".join(titles.get(int(v), f"#{v}") for v in value)
    if step == "distill":
        if value == DISTILL_REUSE:
            return "蒸馏：沿用现有档案"
        for t in ctx.get("tiers") or []:
            if t["key"] == value:
                return f"蒸馏：{t['name']}档"
        return f"蒸馏：{value}"
    if step == "length":
        return f"{value} 字"
    if step == "chapters":
        return f"{value} 段"
    if step == "outline":
        return "先出大纲再写" if value else "直接开写"
    if step == "continue_from":
        return "从第 1 段开始" if not value else f"接在第 {value} 段之后"
    if step == "confirm":
        return "确认开始"
    return str(value)


def summary_rows(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:
    """汇总卡片逐行展示「这一步选了什么」。`step` 让前端能点回去改。"""
    rows: list[dict[str, str]] = []

    def add(step: str, label: str, value: Any) -> None:
        rows.append({"step": step, "label": label, "value": echo_label(step, value, kind, ctx)})

    add("branch", "方向", kind or KIND_LABELS.get(kind or "", "未选"))
    if kind == KIND_ORIGINAL:
        add("corpus", "参考语料", answers.get("corpus") or [])
        if _has_corpus(answers, ctx):
            add("distill", "蒸馏档位", answers.get("distill"))
        add("genre", "题材", answers.get("genre") or "未选")
        add("style", "风格", answers.get("style") or "未选")
        add("length", "单段字数", answers.get("length", ctx.get("generation_length") or 3000))
        add("chapters", "本次段数", answers.get("chapters", 1))
        add("outline", "先出大纲", answers.get("outline", True))
    elif kind == KIND_CONTINUATION:
        add("continue_from", "续写起点", answers.get("continue_from", 0))
        if ctx.get("needs_distill"):
            add("distill", "蒸馏档位", answers.get("distill"))
        add("length", "单段字数", answers.get("length", ctx.get("generation_length") or 3000))
        add("chapters", "本次段数", answers.get("chapters", 1))
    elif kind == KIND_ROLE:
        add("character", "角色", answers.get("character") or "未选")
        add("scene", "情境", answers.get("scene") or "当前剧情时间线")
    return rows
