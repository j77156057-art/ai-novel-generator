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

import re
from typing import Any

from app import wizard_market as market
from app import wizard_story as story

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

#: 书级事实「不指定」的哨兵值。0 不是合法的 novel_id。
#: 语义：不按书过滤 —— 多本语料的物品组/实体/持有事实合并读。
BOOK_SCOPE_NONE = 0

#: 沿用已有文风档案（不重跑蒸馏）的哨兵值。
DISTILL_REUSE = "reuse"

LENGTH_MIN = 500
LENGTH_MAX = 8000
CHAPTERS_MIN = 1
CHAPTERS_MAX = 10

#: 自定义问题的 key 前缀。自定义步骤只能来自 `overrides.custom`，
#: 不能让模型凭空发明一个不在白名单里的步骤（否则 next_step 直接断）。
CUSTOM_PREFIX = "custom:"

#: 受保护的步骤：不允许跳过、不允许被排序挪走。
#: 方向是分支入口，语料决定后面问什么，确认是提交前的最后一道闸。
PROTECTED_STEPS = {"branch", "corpus", "confirm"}

#: 自定义问题只允许落到这些真实字段（当前只有「这本书大概讲什么」）。
#: 想加新字段必须先改这里 + 提交流程，模型不能自己填。
CUSTOM_FIELDS = {"premise"}


# ---------------------------------------------------------------------------
# 步骤序列
# ---------------------------------------------------------------------------


def step_order(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    """按分支与已选项算出完整步骤序列。

    序列是**动态**的：选了语料才问蒸馏；项目本来就有参考语料或文风档案
    才问「是否沿用原风格」。回退时用同一条序列反查上一题，保证前后一致。

    本轮引导的流程改写（`ctx["overrides"]`）在这里叠加，但**只做白名单
    内的三种操作**：跳过可跳步骤、在可动步骤间换序、插入字段已登记的自定义
    问题。branch / corpus / confirm 永远在首位/末位，answers/history 的语义
    完全不变。
    """
    base = _base_step_order(kind, answers, ctx)
    return _apply_overrides(base, ctx)


def _base_step_order(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    if kind == KIND_ORIGINAL:
        # 先问题材与写法，再问参考语料：用户说「我要写小说」时，先问清要写
        # 什么，而不是先照着生产路径问「投喂哪本」。语料/蒸馏仍保留，只是
        # 排到故事塑造之后，按题材族动态决定中间问哪几件。
        steps = [FIRST_STEP, "genre"]
        steps += story.plan_steps(answers, tuple(GENRE_HINTS))
        steps.append("corpus")
        corpus_ids = _corpus_ids(answers, ctx)
        if corpus_ids:
            steps.append("distill")
        if len(corpus_ids) > 1:
            # 多本参考语料才问：书级事实（物品/实体）按哪本读。
            # 单本不存在跨书歧义，问它只是多一步。
            steps.insert(steps.index("corpus") + 1, "book_scope")
        steps += ["style", "length", "chapters", "outline", "confirm"]
        return steps
    if kind == KIND_CONTINUATION:
        steps = [FIRST_STEP, "continue_from"]
        if len(_corpus_ids(answers, ctx)) > 1:
            # 续写分支的「书级事实」问的是续写书：决定往后写时按哪本读持有。
            steps.append("book_scope")
        if ctx.get("needs_distill"):
            steps.append("distill")
        steps += ["length", "chapters", "confirm"]
        return steps
    if kind == KIND_ROLE:
        return [FIRST_STEP, "character", "scene", "confirm"]
    # 还没选方向：只有第一题
    return [FIRST_STEP]


def _apply_overrides(steps: list[str], ctx: dict[str, Any]) -> list[str]:
    """把白名单内的流程改写叠到基础序列上（保证每一步仍可渲染/可校验）。"""
    ov = ctx.get("overrides") or {}
    skip = {
        s
        for s in (ov.get("skip") or [])
        if s in SKIPPABLE and s not in PROTECTED_STEPS
    }
    base = [s for s in steps if s not in skip]
    confirm = "confirm" if "confirm" in base else None
    core = [s for s in base if s != confirm]

    # 换序：只动可动步骤，受保护步骤保持原相对位置（branch/corpus 在头部）。
    seq = [s for s in (ov.get("order") or []) if s in core and s not in PROTECTED_STEPS]
    if seq:
        head = [s for s in core if s in PROTECTED_STEPS]
        movable = [s for s in core if s not in PROTECTED_STEPS]
        rest = [s for s in movable if s not in seq]
        core = head + seq + rest

    # 插入自定义问题：只能插在已有步骤之后，key 必须来自 overrides.custom。
    valid_keys = {
        str(item.get("key"))
        for item in (ov.get("custom") or [])
        if item.get("key") and item.get("field") in CUSTOM_FIELDS
    }
    for item in ov.get("custom") or []:
        key = str(item.get("key") or "")
        if key not in valid_keys or key in core:
            continue
        after = item.get("after")
        idx = core.index(after) + 1 if after in core else len(core)
        core.insert(idx, key)

    return core + ([confirm] if confirm else [])


def _has_corpus(answers: dict[str, Any], ctx: dict[str, Any]) -> bool:
    if "corpus" in answers:
        return bool(answers.get("corpus"))
    return bool(ctx.get("project_has_corpus"))


def _corpus_ids(answers: dict[str, Any], ctx: dict[str, Any]) -> list[int]:
    """本轮引导**实际在场**的语料 id（书级事实步骤的选项来源）。

    原创分支看这轮选了什么（提交时会整体覆盖 `reference_novel_ids`）；
    续写分支看项目已有语料（续写不再选书）。两类都只取合法正整数。
    """
    raw: Any
    if "corpus" in answers:
        raw = answers.get("corpus") or []
    elif answers.get("branch") == KIND_CONTINUATION:
        raw = ctx.get("project_corpus_ids") or []
    else:
        raw = []
    ids: list[int] = []
    for item in raw:
        try:
            nid = int(item)
        except (TypeError, ValueError):
            continue
        if nid > 0 and nid not in ids:
            ids.append(nid)
    return ids


def next_step(
    kind: str | None,
    current: str,
    answers: dict[str, Any],
    ctx: dict[str, Any],
) -> str | None:
    """当前题的下一题；已到末尾返回 None。

    `answers["_auto"]` 里的步骤是「用户在一句话里已经给过信息、由 agent
    预填」的项，往前推进时直接跳过，避免把已经说过的话再问一遍。
    """
    order = step_order(kind, answers, ctx)
    auto = set(answers.get("_auto") or [])
    if current not in order:
        # 当前题因上游改动不再适用（例如把语料改成「不投喂」后蒸馏题消失）：
        # 从新序列里找第一个尚未回答的题，找不到就回到最后一题。
        for key in order:
            if key not in answers and key not in auto:
                return key
        return order[-1] if order else None
    idx = order.index(current)
    for key in order[idx + 1:]:
        if key not in auto:
            return key
    return None


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
        "book_scope": _q_book_scope,
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
    if step in story.STORY_STEPS and step != "genre":
        body = _q_story(step, answers, ctx)
    elif builder is None:
        if not step.startswith(CUSTOM_PREFIX):
            raise ValueError(f"未知引导步骤：{step}")
        body = _q_custom(step, answers, ctx)
    else:
        body = builder(kind, answers, ctx)
    card = body.get("card") or {}
    has_default = card.get("default") is not None and card.get("default") != ""
    body.update(
        {
            # 对话正文：这一步在问什么、各选项代价差在哪、默认值与推荐。
            # 卡片只是这段话的快捷入口，不能替代解释。
            "message": _message(step, kind, answers, ctx),
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
#: 书级事实默认「不指定（合并读）」，跳过等于保持合并读，因此可跳。
SKIPPABLE = {
    "genre",
    "pov",
    "premise",
    "protagonist",
    "world",
    "power",
    "scale",
    "style",
    "length",
    "chapters",
    "outline",
    "scene",
    "book_scope",
}


# ---------------------------------------------------------------------------
# 对话正文（每条回复的第一部分）
# ---------------------------------------------------------------------------
#
# 文案口径：先说「这一步在决定什么」，再说各选项的代价（时间/成本/质量/
# 可控性）与默认值，最后给推荐和「什么情况下该选别的」。数字一律来自
# `wizard_facts`（档位定义 + 唯一实测点 + 当前项目语料），这里不再硬编码。


def _message(step: str, kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    """把步骤渲染成两段对话文字（不含卡片）。"""
    if step.startswith(CUSTOM_PREFIX):
        return _msg_custom(step, ctx)
    if step in story.STORY_STEPS and step != "genre":
        return _msg_story(step, kind, answers, ctx)
    builder = _MESSAGE_BUILDERS.get(step)
    if builder is None:
        raise ValueError(f"未知引导步骤：{step}")
    return builder(kind, answers, ctx)


def _msg_story(step: str, kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    """故事问题只给「为什么问」，选项/输入框由卡片承接。"""
    body = _q_story(step, answers, ctx)
    why = str(body.get("why") or "")
    if step == "protagonist":
        note = market.protagonist_note(answers.get("genre"), today=ctx.get("today"))
        if note:
            why = f"{why}\n\n{note}"
    return why


def _msg_branch(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    return (
        "先定方向，因为三条路要填的参数完全不同：原创是从零起一本新书，后面要定语料、"
        "蒸馏档位、题材、风格、字数和这次写几段；续写是接着你已有正文的结尾往下写，"
        "只问起点和篇幅；角色对话不出正文，是我用书里某个角色的口吻跟你聊，只问是谁、"
        "在什么情境下。\n\n"
        "如果你手上还没正文、也没想好要不要仿写某本书，选原创最稳——它会把该定的都定下来，"
        "之后想接着写，直接从已有正文续就行。"
    )


def _msg_corpus(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    novels = ctx.get("novels") or []
    if novels:
        sizes = "、".join(
            f"《{_display_title(n)}》{n['chapters']} 章"
            + (f"（完整档约 {n['full_minutes']} 分钟）" if n.get("full_minutes") else "")
            for n in novels[:4]
        )
        opening = (
            f"这里有 {len(novels)} 本已上传的语料可选（可多选）：{sizes}。"
            "选了会怎样：蒸馏把它的句长、对话密度、场景切换节奏和角色说话方式提炼成文风档案，"
            "写出来更贴那本书；代价是蒸馏要跑模型，选中几本就按合计章数叠时间。"
        )
    else:
        opening = (
            "语料库里还没有书。不投喂也能写，只是模型完全按题材和风格自由发挥，"
            "像不像你说的那本就看运气了。想投喂的话，先去「语料库」上传 txt 再回来。"
        )
    return (
        opening
        + "\n\n"
        "不投喂的好处是快、可控：生成时不用等蒸馏，风格靠题材和平台调性兜。"
        "如果你想「像某本书那样写」，就选那本书；只是想要个能读的故事，可以不投喂。"
    )


def _msg_book_scope(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    ids = _corpus_ids(answers, ctx)
    titles = {int(n["id"]): _display_title(n) for n in (ctx.get("novels") or [])}
    names = "、".join(f"《{titles.get(i) or f'#{i}'}》" for i in ids)
    if kind == KIND_CONTINUATION:
        return (
            f"你手上有 {len(ids)} 本可以接着写的语料：{names}。续写书决定我按哪本读"
            "物品「现在在谁手上」、实体和写法；不指定就是把两本的同名物品合并读，"
            "相同时会标成「名字有歧义」——写正文时那条持有约束等于没有。\n\n"
            "如果你要接着其中一本的剧情写，选那本；只是想把两本当参考一起用，可以不指定。"
        )
    return (
        f"这次参考了 {len(ids)} 本语料：{names}。生成时物品「现在在谁手上」按「事实来源书」读，"
        "两本里出现同名物品时，不指定会同时命中两边、读通道按「名字有歧义」处理（不猜、"
        "也不给持有约束）。\n\n"
        "挑一本主参考书，物品持有的约束才落得下去；只想借文风、不关心物品事实，可以不指定。"
        "这一项之后也可以在项目工作台「参考语料」里改。"
    )


def _msg_distill(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    books = ctx.get("corpus_books") or []
    total = int(ctx.get("corpus_total_chapters") or 0)
    tiers = {t["key"]: t for t in (ctx.get("tiers") or [])}

    if books:
        names = "、".join(f"《{_display_title(b)}》" for b in books)
        opening = (
            f"你这个项目投喂的是{names}，合计 {total} 章 / "
            f"{(int(ctx.get('corpus_total_chars') or 0)) / 10000:.1f} 万字。"
            "蒸馏会把它的句长、对白密度、场景切换节奏、角色说话方式压成一份档案，"
            "写正文时注入提示词。"
        )
    else:
        opening = (
            "这个项目还没投喂语料，没有东西可蒸。想仿某本书，先去「语料库」上传 txt，"
            "回来再选这一档。"
        )

    full = tiers.get("full")
    balanced = tiers.get("balanced")
    fast = tiers.get("fast")

    def line(t: dict[str, Any] | None, what: str) -> str:
        if not t:
            return ""
        return (
            f"{t['name']}：约 {t['sampled_chapters']} 章、{t['dimension_count']} 个维度，"
            f"预计约 {t['estimated_minutes']} 分钟。{what}"
        )

    parts = [
        opening,
        "三档在这批语料上的实际差别（耗时按仓库里唯一一次实测外推，不是每档实测）：",
        line(
            full,
            "14 个提炼维度全开，带角色名归一化和全量伪角色清洗——"
            "「小落→安小落」「宫爵→南宫爵」会并回同一个角色，声音词典才不至于张冠李戴。",
        ),
        line(
            balanced,
            "维度一样，但关掉角色名归一化：简称会各算一个角色，配角台词可能串；"
            "清洗也只做词表那层。",
        ),
        line(
            fast,
            "砍掉节拍序列、潜台词模式两个维度（13→11），角色名完全不清洗；"
            "只适合先验证管线通不通，不建议拿它出成品。",
        ),
        "选哪一档，接下来都是同一条链：起后台蒸馏任务 → 项目工作台显示逐块进度 → "
        "跑完才允许写正文（没跑完去生成会直接 409）。",
    ]

    if full:
        parts.append(
            "另外，你之前提过「全本完整蒸馏」：代码里并没有第四个档位，完整档本身就是"
            f"按语料 {int((full.get('sample_ratio') or 0) * 100)}% 采样。"
            f"但我可以直接把采样章数覆盖成这批语料共 {total} 章（说「采样改成全本」），"
            "或者压到 200 章（说「采样改成 200 章」）。这个覆盖会进蒸馏指纹，"
            "改完必须重跑才会生效——不会偷偷复用旧档案。"
        )

    archive = ctx.get("archive")
    if archive:
        parts.append(
            "这个项目已经有档案了："
            f"{archive.get('tier_name') or archive.get('tier')}档，"
            f"{archive.get('n_chapters')} 章，{archive.get('rule_count')} 条规则 / "
            f"{archive.get('dimension_count')} 个维度。"
            "也可以选「沿用现有档案」——秒级复用、不花模型钱，适合接着上次的文风写。"
        )

    last = ctx.get("last_tier")
    override = ctx.get("sampling_override")
    if last or override is not None:
        bits = []
        if last:
            bits.append(f"上次选的是 {last} 档")
        if override is not None:
            cap = int(override)
            bits.append("采样覆盖是全本" if cap <= 0 else f"采样覆盖是 {cap} 章")
        parts.append("；".join(bits) + "。要改直接说，不用重新答一遍。")

    parts.append(
        "我的建议：既然都投喂了，默认走完整档——文风不准等于白投。"
        "只想快点看到正文效果选平衡；快速档只用来试跑。"
    )
    return "\n\n".join(p for p in parts if p)


def _msg_genre(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    base = (
        "题材决定模型对世界观、冲突类型和节奏的默认假设：科幻会先铺设定，悬疑先埋谜团，"
        "言情把重心放在关系推进，仙侠则默认从凡到仙的成长线。它不影响耗时，只影响"
        "「写出来的东西像不像你要的那类」。\n\n"
        "这一项我没有给默认值，因为选错会让后面的风格和字数一起偏。"
        "拿不准就选你最近读得最多的那类——你对那个题材的「不对劲」最敏感，改起来也最快。"
    )
    block = market.genre_block(today=ctx.get("today"))
    return f"{base}\n\n{block}" if block else base


def _msg_style(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    return (
        "平台调性决定叙述节奏，不是题材：番茄是短句、强钩子、每章留悬念；起点重设定和"
        "长线伏笔；晋江偏情感和内心戏；豆瓣阅读更文学、更克制；知乎盐选是短钩子加反转。"
        "同一本玄幻，番茄和豆瓣读起来是两本书。\n\n"
        "没主意就选起点——它对设定完整度要求最高，写出来最不容易散。"
        "如果你要的是「一口气读完」的爽感，选番茄。"
    )


def _msg_length(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    default = ctx.get("generation_length") or 3000
    return (
        f"单段字数决定一次生成能铺开多少情节。默认 {default} 字：低于 1500 基本只剩一个场景，"
        "对话和动作刚展开就得收；超过 5000 模型容易开始注水、重复前面的描写，"
        "而且单次耗时也会明显变长。\n\n"
        f"先按 {default} 试一段，觉得节奏拖就降到 1500–2000，觉得没展开就提到 4000。"
        "这个值之后在项目设置里也能改。"
    )


def _msg_chapters(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    return (
        "这次连着写几段，是串行跑的：每写完一段会把它压成摘要喂给下一段，"
        "所以段数越多越容易前后走样，耗时也基本线性增加（2 段大约就是 2 倍）。\n\n"
        "默认 1 段，建议先写一段看手感，满意再回来加。"
        "如果你已经想好了完整的几段结构，直接填 2–3 段也行。"
    )


def _msg_outline(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    return (
        "大纲（剧情圣经）会先列出角色、关系、核心冲突和几条伏笔，你确认后我再写正文。"
        "多这一轮大概几分钟，但能明显减少写偏和人物走形，改起来也比改正文便宜。\n\n"
        "默认是先出大纲。如果你已经有很明确的开头想法、只想快点看到正文，"
        "选「直接开写」也可以——写歪了还能用编辑提案改。"
    )


def _msg_continue_from(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    segments = list(ctx.get("segments") or [])
    if segments:
        latest = max(segments)
        opening = (
            f"这个项目已经有 {len(segments)} 段正文，最后一段是第 {latest} 段。"
            "续写只能接在最后一段之后——生成链路本身就是「读前面摘要、往后追加」，"
            "跳到中间插入会和后面已写的内容对不上，所以我没给这个选项。"
        )
    else:
        opening = (
            "这个项目还没有正文，续写会从第 1 段开始写，等于先起个头。"
            "之后每追加一段，都会自动接在最后一段后面。"
        )
    return (
        opening
        + "\n\n"
        "想从别的地方开始，可以先在项目工作台删掉后面的段，或者新建一个项目只导入到你要的位置。"
    )


def _msg_character(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    chars = ctx.get("characters") or []
    if not chars:
        return (
            "项目里还没有角色卡，所以这一项得你直接打名字。我会用最基础的设定陪你聊，"
            "细节靠常识补，可能不够贴原著。\n\n"
            "想让角色更准，先去「项目工作台 → 剧情圣经 → 角色」加角色，"
            "再回来这里选。"
        )
    legacy = ctx.get("character_source") == "legacy"
    source = (
        "角色卡来自旧版知识图谱（新图谱里还没有角色），按提及次数排的，"
        "可能混进「许七」「安小」这类别名碎片，挑完整的人名就行。"
        if legacy
        else "角色卡来自本项目的知识图谱，带身份、关系和最近状态。"
    )
    return (
        f"这里按提及次数列了 {min(len(chars), 12)} 个角色。{source}"
        "选谁决定我用谁的口吻、视角和他已知的信息——比如选一个不知情的角色，"
        "我就不会让他说出只有主角才知道的事。\n\n"
        "想聊的人不在列表里也可以直接打名字；某个角色怎么看都不对，多半是图谱里数据有问题，"
        "可以先去人物情报页核对。"
    )


def _msg_scene(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    return (
        "情境决定语气、称呼和他该知道多少：同一句台词，在「初次见面」和「一场对峙之后」"
        "说出来意思完全不同。默认是当前剧情时间线，也就是按你已经写到的地方接着聊。\n\n"
        "随便给一句就行，比如「大劫之后，他在城头等你来解释」。"
        "不想定就直接用默认，我会按最近一段的处境来演。"
    )


def _msg_confirm(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    return (
        "下面是这次的全部参数，确认后我会写回项目：该蒸馏的先起蒸馏任务、"
        "该出大纲的先出大纲，之后才轮到写正文。\n\n"
        "想改哪一项，直接点那一行跳回去，或者告诉我「上一步」。"
    )


def _custom_item(step: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """从 overrides 里取自定义问题的定义；拿不到就报错，不静默编一个。"""
    for item in (ctx.get("overrides") or {}).get("custom") or []:
        if str(item.get("key")) == step:
            return item
    raise ValueError(f"找不到自定义问题：{step}")


def _msg_custom(step: str, ctx: dict[str, Any]) -> str:
    item = _custom_item(step, ctx)
    question = str(item.get("question") or item.get("title") or "这个问题是你自己加的")
    return (
        f"{question}\n\n"
        "这是你在本轮引导里自己加的问题，答案会写进项目简介（premise），"
        "后面的写作和对话都会参考它。不想答可以说「跳过这步」；"
        "想删掉这个问题，说「撤销这次流程修改」。"
    )


_MESSAGE_BUILDERS = {
    "branch": _msg_branch,
    "corpus": _msg_corpus,
    "book_scope": _msg_book_scope,
    "distill": _msg_distill,
    "genre": _msg_genre,
    "style": _msg_style,
    "length": _msg_length,
    "chapters": _msg_chapters,
    "outline": _msg_outline,
    "continue_from": _msg_continue_from,
    "character": _msg_character,
    "scene": _msg_scene,
    "confirm": _msg_confirm,
}


# ---------------------------------------------------------------------------
# 回执（答完之后先确认一句，再进下一题）
# ---------------------------------------------------------------------------


def _display_title(novel: dict[str, Any]) -> str:
    """语料标题去掉 .txt 后缀：对话里说书名比说后缀自然。"""
    title = str(novel.get("title") or novel.get("id") or "")
    return re.sub(r"\.(txt|md|epub)$", "", title, flags=re.I)


def _short(value: Any, limit: int = 40) -> str:
    text = str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def ack_for(step: str, value: Any, answers: dict[str, Any], ctx: dict[str, Any]) -> str:
    """把「刚答的这一题」说成一句人话，作为下一条回复的开头。"""
    if step == "branch":
        return f"好，{KIND_LABELS.get(value, value)}。"
    if step == "corpus":
        if not value:
            return "好，不投喂语料，接下来的文风全靠题材和平台调性。"
        titles = {int(n["id"]): _display_title(n) for n in (ctx.get("novels") or [])}
        names = [titles.get(int(v)) or f"#{v}" for v in value]
        return "好，参考" + "、".join("《" + n + "》" for n in names) + "。"
    if step == "book_scope":
        if not value:
            return "好，不指定——按所有在场语料合并读，同名物品会标成「无法确定」。"
        titles = {int(n["id"]): _display_title(n) for n in (ctx.get("novels") or [])}
        name = titles.get(int(value)) or f"#{value}"
        if answers.get("branch") == KIND_CONTINUATION:
            return f"好，续写书定为《{name}》，物品和实体事实按它读。"
        return f"好，事实来源书定为《{name}》，物品持有按它读。"
    if step == "distill":
        if value == DISTILL_REUSE:
            return "好，沿用现有档案，不用重跑蒸馏，秒级完成。"
        for t in ctx.get("tiers") or []:
            if t["key"] == value:
                est = t.get("estimated_minutes")
                sampled = t.get("sampled_chapters")
                detail = (
                    f"{sampled} 章、预计约 {est} 分钟"
                    if est is not None
                    else "耗时按档位默认口径"
                )
                if t["key"] == "full":
                    return f"好，完整档，{detail}，跑完才开始写。"
                if t["key"] == "balanced":
                    return f"好，平衡档，{detail}（维度齐全，但不做角色名归一化）。"
                return f"好，快速档，{detail}，只够验证管线，出成品前建议换档重跑。"
        return f"好，蒸馏档位记为 {value}。"
    if step.startswith(CUSTOM_PREFIX):
        return "好，记下了，会写进项目简介。" if value else "好，这条先空着。"
    if step == "genre":
        return f"好，{value}。"
    if step == "pov":
        return f"好，写法按「{value}」。"
    if step == "premise":
        return f"好，核心冲突记下了：{_short(value, 60)}"
    if step == "protagonist":
        return f"好，主角按「{_short(value, 60)}」来。"
    if step == "world":
        return f"好，世界观约束记下了：{_short(value, 60)}"
    if step == "power":
        return f"好，力量/金手指边界记下了：{_short(value, 60)}"
    if step == "scale":
        return f"好，篇幅与更新按「{value}」。"
    if step == "style":
        return f"好，{value}调性。"
    if step == "length":
        return f"好，每段 {value} 字。"
    if step == "chapters":
        return f"好，这次连着写 {value} 段。" + ("（串行，时间也按倍数走）" if int(value) > 1 else "")
    if step == "outline":
        return "好，先出大纲，你确认后我再动笔。" if value else "好，直接开写，跳过大纲确认。"
    if step == "continue_from":
        return "好，从第 1 段开始。" if not value else f"好，接在第 {value} 段之后。"
    if step == "character":
        return f"好，就和{value}聊。"
    if step == "scene":
        return f"好，情境按「{value or '当前剧情时间线'}」。"
    if step == "confirm":
        return "好，开始。"
    return "好。"


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
            "label": _display_title(n) or f"语料 #{n['id']}",
            "desc": " · ".join(
                x
                for x in [
                    f"{n['chapters']} 章 / {n['chars'] / 10000:.1f} 万字"
                    if n.get("chapters")
                    else "",
                    n.get("genre") or "",
                    n.get("author") or "",
                ]
                if x
            )
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


def _q_book_scope(kind: str | None, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    ids = _corpus_ids(answers, ctx)
    titles = {int(n["id"]): _display_title(n) for n in (ctx.get("novels") or [])}
    # 默认值优先取「已经设好的那本书」（且仍在本次语料里）——否则用户跳过这一步
    # 会把工作台里手工设过的书清成「不指定」。
    if "book_scope" in answers:
        default = answers.get("book_scope")
    else:
        existing = (ctx.get("continuation_novel_id")
                    if kind == KIND_CONTINUATION else ctx.get("active_novel_id"))
        default = int(existing) if existing in ids else BOOK_SCOPE_NONE
    options = [
        {
            "value": nid,
            "label": f"《{titles.get(nid) or f'#{nid}'}》",
            "desc": "物品组 / 持有 / 实体事实只按这本读，不和另一本混",
        }
        for nid in ids
    ]
    options.append(
        {
            "value": BOOK_SCOPE_NONE,
            "label": "不指定（合并读）",
            "desc": "两本书合并读：同名物品命中多个组时会标成「名字有歧义」，不猜",
        }
    )
    if kind == KIND_CONTINUATION:
        return {
            "title": "接着哪本书往下写？",
            "why": "你手上有多本参考语料。续写书决定物品「现在在谁手上」、实体和写法按哪本读；"
            "选错会把另一本的同名物品当成这本书的事实。",
            "card": {
                "type": "single",
                "field": "continuation_novel_id",
                "default": default,
                "options": options,
            },
        }
    return {
        "title": "物品和事实按哪本书读？",
        "why": "你选了多本参考语料。两本里可能有同名物品/同名角色，不指定就会把它们合并读，"
        "命中多个组时读通道按「名字有歧义」处理（不猜、也不给持有约束）；"
        "挑一本作为事实来源书，物品持有的约束才落得下去。",
        "card": {
            "type": "single",
            "field": "active_novel_id",
            "default": default,
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
        if t.get("sample_cap") is not None:
            cap = int(t["sample_cap"])
            sampling = "全本（不抽样）" if cap <= 0 else f"最多 {cap} 章"
        elif t.get("sampling_mode") == "percentage":
            sampling = f"按语料 {int(round(float(t.get('sample_ratio') or 0) * 100))}% 采样"
        else:
            sampling = f"固定 {t.get('fixed_chapters') or 30} 章"
        desc = f"{sampling}｜{t['dimension_count']} 维｜{t.get('cost_hint') or ''}"
        if t.get("normalize_character_names"):
            desc += "｜角色名归一化"
        else:
            desc += "｜不做角色名归一化"
        desc += "｜" + cleaning.get(t.get("fake_character_cleaning"), "")
        if t.get("estimated_minutes") is not None:
            desc += (
                f"｜本语料约 {t.get('sampled_chapters')} 章、"
                f"预计约 {t['estimated_minutes']} 分钟"
            )
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
            "why": "项目里还没有角色卡。直接输入角色名，我会用最基础的设定陪你聊；想更准，先去「项目工作台 → 剧情圣经 → 角色」加角色。",
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


def _q_story(step: str, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """故事塑造步骤的问题卡：由 wizard_story 按题材族渲染。"""
    body = story.question(
        step,
        answers,
        tuple(GENRE_HINTS),
        {**ctx, "genre_hints": GENRE_HINTS},
    )
    if step == "protagonist":
        # 有有效行情卡时，用「带来源的人设方向」替换静态示例；
        # 卡过期/题材不匹配时 playbook 保持原样，不硬凑。
        directions = market.protagonist_directions(
            answers.get("genre"), today=ctx.get("today")
        )
        if directions:
            card = dict(body.get("card") or {})
            card["suggestions"] = [d["text"] for d in directions] + ["按自己的设定来"]
            body["card"] = card
    return body


def _q_custom(step: str, answers: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """渲染用户自己插入的问题（字段已登记，答案落到 premise）。"""
    item = _custom_item(step, ctx)
    return {
        "title": str(item.get("title") or "补充一个问题"),
        "why": str(item.get("question") or "这个问题是你自己加的。"),
        "card": {
            "type": "text",
            "field": "premise",
            "default": answers.get(step, ""),
            "placeholder": "直接打字回答；不想答就说「跳过这步」",
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
    if step.startswith(CUSTOM_PREFIX):
        value = str(raw or "").strip()
        if len(value) > 500:
            raise AnswerError("自定义问题的回答最多 500 字")
        return value

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

    if step == "book_scope":
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise AnswerError("书级事实只能选一本书，或选「不指定」") from exc
        ids = _corpus_ids(answers, ctx)
        if value != BOOK_SCOPE_NONE and value not in ids:
            raise AnswerError("只能从本次在场的参考语料里挑一本")
        return value

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

    if step in {"pov", "premise", "protagonist", "world", "power", "scale"}:
        value = str(raw or "").strip()
        limits = {
            "pov": 200,
            "premise": 1000,
            "protagonist": 300,
            "world": 500,
            "power": 500,
            "scale": 200,
        }
        if not value:
            raise AnswerError("这一项不能为空；不想现在定可以说「按默认来」")
        if len(value) > limits[step]:
            raise AnswerError(f"这一项最多 {limits[step]} 字")
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
    if step.startswith(CUSTOM_PREFIX):
        return str(value) if value else "未填"
    if step == "branch":
        return KIND_LABELS.get(value, str(value))
    if step == "corpus":
        if not value:
            return "不投喂语料（自由生成）"
        titles = {int(n["id"]): n.get("title") or f"#{n['id']}" for n in (ctx.get("novels") or [])}
        return "参考语料：" + "、".join(titles.get(int(v), f"#{v}") for v in value)
    if step == "book_scope":
        if not value:
            return "书级事实：不指定（合并读）"
        titles = {int(n["id"]): n.get("title") or f"#{n['id']}" for n in (ctx.get("novels") or [])}
        label = "续写书" if kind == KIND_CONTINUATION else "事实来源书"
        return f"{label}：{titles.get(int(value), f'#{value}')}"
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
    skipped = set((ctx.get("overrides") or {}).get("skip") or [])

    def add(step: str, label: str, value: Any) -> None:
        if step in skipped:
            return
        rows.append({"step": step, "label": label, "value": echo_label(step, value, kind, ctx)})

    add("branch", "方向", kind or KIND_LABELS.get(kind or "", "未选"))
    if kind == KIND_ORIGINAL:
        add("corpus", "参考语料", answers.get("corpus") or [])
        if len(_corpus_ids(answers, ctx)) > 1:
            add("book_scope", "事实来源书", answers.get("book_scope", BOOK_SCOPE_NONE))
        if _has_corpus(answers, ctx):
            add("distill", "蒸馏档位", answers.get("distill"))
        add("genre", "题材", answers.get("genre") or "未选")
        # 故事塑造项：只展示真正答过/预填过的，顺序跟本轮题材计划一致。
        for step in story.plan_steps(answers, tuple(GENRE_HINTS)):
            value = answers.get(step)
            if value in (None, ""):
                continue
            add(step, story.STORY_LABELS.get(step, step), value)
        add("style", "风格", answers.get("style") or "未选")
        add("length", "单段字数", answers.get("length", ctx.get("generation_length") or 3000))
        add("chapters", "本次段数", answers.get("chapters", 1))
        add("outline", "先出大纲", answers.get("outline", True))
    elif kind == KIND_CONTINUATION:
        add("continue_from", "续写起点", answers.get("continue_from", 0))
        if len(_corpus_ids(answers, ctx)) > 1:
            add("book_scope", "续写书", answers.get("book_scope", BOOK_SCOPE_NONE))
        if ctx.get("needs_distill"):
            add("distill", "蒸馏档位", answers.get("distill"))
        add("length", "单段字数", answers.get("length", ctx.get("generation_length") or 3000))
        add("chapters", "本次段数", answers.get("chapters", 1))
    elif kind == KIND_ROLE:
        add("character", "角色", answers.get("character") or "未选")
        add("scene", "情境", answers.get("scene") or "当前剧情时间线")

    # 自定义问题：只展示本轮真正插入过的（白名单字段在 step_order 里已过滤）。
    for item in (ctx.get("overrides") or {}).get("custom") or []:
        key = str(item.get("key") or "")
        if key and item.get("field") in CUSTOM_FIELDS:
            add(key, str(item.get("label") or item.get("title") or "补充"), answers.get(key, ""))

    # 按本轮真实顺序排，流程改写后汇总行也跟着动。
    order = step_order(kind, answers, ctx)
    rows.sort(key=lambda r: order.index(r["step"]) if r["step"] in order else 999)
    return rows
