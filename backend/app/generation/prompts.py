"""M4 提示词组装。

- build_system_prompt：固化人设/风格 + M3.3 约束（未填坑 + 角色约束），
  这是防漂移的核心——角色与悬念始终挂在系统层，不被前情淹没。
- build_user_prompt：前情提要（段级总结链）+ 检索上下文（RAG）+ 大纲 + 续写指令。

提示里的【前情提要】【检索上下文】【大纲】标记同时被 LocalFallbackLLMProvider
解析回显，保证离线可验证约束进入生成环。

## 运行时覆写（prompt_override）是**追加口**，不是改写口

`override_style` / `override_constraint` / `override_directive` 三个参数对应
`prompt_overrides` 表的三个键（见 `app.agents.prompt_overrides`）。它们的约束是：

- **只能加，不能减** —— 覆写文本被渲染成独立的一段，插在对应块**之后**；
  代码里的默认块（角色锁定 / 伏笔纪律 / 文风要求）一个字节都不动，
  覆写也**删不掉**它们（想改维度级行为要去 `profiles.PROFILE_RULES`，不是这里）。
- **不传就等于没接** —— 三个参数默认空串，此时输出与接入之前**逐字节相同**
  （这是本文件唯一的兼容底线，由 `tests/test_prompt_override_wiring.py` 钉住）。
"""

import logging
from typing import Any, Sequence

from app.style.attribution import attribution_directive


def _override_block(text: str, label: str, *, leading_newline: bool = False) -> str:
    """把一段运行时覆写渲染成提示词里的追加块。

    空串/纯空白 ⇒ 返回空串 ⇒ 调用方拼出来的提示词**一个字节都不变**
    （这是「没设覆写就等于没接」的实现方式：不是加一个判断再跳过，
    而是渲染结果本身就是空的）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    return ("\n" if leading_newline else "") + f"【{label}】\n{t}\n"


def _style_block(style_dims: dict | None) -> str:
    if not style_dims:
        return "（未指定风格维度，默认自然叙事。)"
    items = []
    for k, v in style_dims.items():
        if isinstance(v, (list, tuple)):
            v = "、".join(str(x) for x in v)
        items.append(f"- {k}：{v}")
    return "风格维度预设：\n" + "\n".join(items)


def _profile_block(style_profile: dict | None) -> str:
    """把参考语料的实测档案渲染成静态提示块。

    **必须是静态的**：`describe()` 是纯确定性函数（同一档案两次调用逐字节相同，
    `verify_stylometry` D 段有闸门），档案本身又按指纹缓存，所以这段文本整轮生成
    保持不变，放在 system 里不会击穿提示词缓存。

    与【风格】的分工：【风格】是用户选的平台调性（番茄/晋江…），是**倾向**；
    这里是**参考语料实测出来的数字**，是可验收的。两者冲突时以数字为准 ——
    因为只有数字能事后核对。
    """
    if not style_profile:
        return ""
    from app.style.stylometry import describe

    body = describe(style_profile)
    health = style_profile.get("corpus_health") or {}
    cleaned = style_profile.get("clean")
    flagged = [
        f"《{k}》检出并已剔除注入串「{v['marks'][0]['text']}」{v['marks'][0]['count']} 处"
        for k, v in health.items()
        if v.get("marks")
    ]
    if flagged and cleaned:
        note = "（拟合前已做语料体检：" + "；".join(flagged) + "）"
    elif flagged and not cleaned:
        note = (
            "（注意：参考语料检出疑似注入串但**未剔除**，"
            "下述数字已被污染，仅作弱参考）"
        )
    else:
        note = ""
    return (
        "\n【文风档案：从参考语料实测，按下列数字贴近】"
        "以下是统计出来的客观区间，不是修辞建议；生成时优先满足这些数字特征：\n"
        f"{body}\n"
        + (f"{note}\n" if note else "")
    )


def _merge_named(bible_items: list, kg_items: list) -> list:
    """按 name 合并角色：bible（用户构思）优先，KG 里 bible 没有的补在后面。"""
    merged: list = []
    seen: set[str] = set()
    for c in list(bible_items) + list(kg_items):
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        key = name or str(c)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(c)
    return merged


def _merge_text(bible_items: list, kg_items: list) -> list:
    """按文本合并伏笔：去重保序，bible 在前。"""
    merged: list = []
    seen: set[str] = set()
    for f in list(bible_items) + list(kg_items):
        text = str(f).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged


def _character_constraint_line(c: dict) -> str:
    """角色约束块里的一行。

    带 `persona`（说话方式/行为倾向/性格底线/关键经历）时追加一句人话 ——
    没有它的话，模型只有「他是谁、他要干什么」，写对白没有依据 ⇒ 全员一个腔调。

    **没有 persona 时与改动前逐字节相同**（老项目的提示词不受影响）。
    """
    from app.generation.persona import character_line

    return character_line(c)


def merged_constraint_items(
    reminders: dict, plot_bible: dict | None
) -> tuple[list[dict], list[str]]:
    """「角色 / 伏笔」约束的**唯一合并口径**——system 提示块与 constraints 共用。

    权威顺序（评审 A 的 Q6）：plot_bible（用户构思 / bible）在前，图谱抽取在后。
    两处必须共用本函数，否则会出现「system 里列了、user 尾部那份自称完整的锁定名单里没列」
    这种自相矛盾的提示词——最高权威层被漏掉，模型会以为游离在约束之外。

    **bible 删除权威**（kg-cascade-protocol 阶段 2-3）：bible.characters **显式列了**
    名字时，未列入的 KG 角色约束整条剔除——用户在 bible 里删掉「顾辰逸」后，
    旧抽取事实再把他说成锁定角色，会与新设定矛盾（旧段不可改，但未来段必须只认新设定）。
    保守边界：bible 无 characters 键或空列表 = 未显式宣告清册，不过滤
    （老项目 bible 只有 relationships/foreshadowing 时不误伤全表）。
    """
    bible_chars = (plot_bible.get("characters") or []) if plot_bible else []
    kg_chars = reminders.get("character_constraints") or []
    bible_fores = (plot_bible.get("foreshadowing") or []) if plot_bible else []
    kg_fores = [h.get("question") for h in (reminders.get("unresolved_plotholes") or [])]
    # —— 只过滤 **KG 抽来的**伏笔里的对话碎片（2026-10-28）——
    # 实测（验收库同一份数据）：KG 抽的 19 条伏笔里 **14 条是对话碎片**
    # （“你把我当什么？”…）⇒ 它们照原样进了 system 提示的约束块：
    # 白占位置，还可能让模型误以为那些是「要在后续慢慢推进的悬念」。
    # **用户自己在剧情圣经里写的伏笔一个字都不动**（那是权威）。
    if kg_fores:
        from app.kg.foreshadow_quality import split_quality

        _keep, _junk = split_quality([str(f) for f in kg_fores if f])
        if _junk:
            logging.getLogger(__name__).info(
                "伏笔约束：剔除 %d 条疑似对话碎片的 KG 伏笔（保留 %d 条）",
                len(_junk), len(_keep))
        kg_fores = _keep
    if bible_chars and isinstance(bible_chars, list):
        declared: set[str] = set()
        for c in bible_chars:
            declared.add(str((c.get("name") if isinstance(c, dict) else c) or "").strip())
        if declared:
            kg_chars = [
                c for c in kg_chars
                if not isinstance(c, dict)
                or not str(c.get("name") or "").strip()
                or str(c.get("name")).strip() in declared
            ]
    return _merge_named(bible_chars, kg_chars), _merge_text(bible_fores, kg_fores)


def _description_guides() -> str:
    """心理描写 + 场景描写的**可执行**指引（2026-10-29）。

    ## 为什么需要（实测，不是猜测）

    发布版冒烟库三个项目的**真实提示词**里，角色约束块
    写满了「失落神女」「窃取太阳权柄的旧神」这类身份描述，
    却没有一个字说明「他内心怎么想」「这个地方长什么样」。

    实测生成文本 vs 参考书（每千字）：

        直陈心理   生成 1.03 ／ 参考 3.06   ← 缺 2/3
        身体反应   生成 3.10 ／ 参考 0.46   ← 多 6.7 倍
        语言提示语  生成 11.9 ／ 参考 18.5   ← 缺 35%

    ⇒ 生成文本把「心理」全部替换成了「身体反应」。
    根因之一是**蒸馏教了「不写心理」**（已在新维度里修），
    但提示词侧也要给出**可执行的目标**，否则模型没有依据。

    ## 与「禁止情绪直陈」的边界（关键，别打架）

    现有规则禁止「眼神里露出悲凉」「瞳孔骤缩」这类**贴标签式的情绪形容**。
    那条规则本身没错，但模型把它理解成了「不许写内心」⇒ 心理绝迹。

    ⇒ 这里显式划清：禁止的是**形容词贴标签**，鼓励的是**具体的心理活动**
    （他在盘算什么、他以为是什么、他想起了谁）—— 那是内容，不是形容。

    ## 频次对齐参考书（实测）

    参考书心理密度 3.06 处/千字。A/B 实测「每段至少一处」会写到 9.73
    （参考书的 3 倍，写滥）⇒ 改成「每 2~3 段一处」。
    """
    try:
        from app.generation.description_elements import (
            render_psych_guide, render_scene_guide,
        )

        return "\n" + render_psych_guide() + "\n" + render_scene_guide() + "\n"
    except Exception:  # noqa: BLE001 —— 指引渲染失败不该让整段提示词丢
        return ""


def _psych_scene_boundary() -> str:
    """「禁止情绪直陈」与「要写心理」的边界说明（必须紧跟在那条禁令后面）。"""
    return (
        "（注意：上面禁的是「眼神里露出悲凉」这种**贴标签式的情绪形容**；"
        "**不禁止**写具体的心理活动 —— 他在盘算什么、他以为是什么、他想起了谁，"
        "这些是**内容**，该写就写。参考作者的内心描写约占三十分之一段，"
        "每 2~3 段出现一处即可，不要一段里连写好几处。）\n"
    )


def _scale_contrast_block(plot_bible: dict | None,
                          character_names: list[str] | None) -> str:
    """**量级对照块**（2026-10-29）。

    ## 为什么要有这个

    实测（发布版冒烟库三个项目的**真实提示词**）：角色约束块里
    「这一击能有多大」类信号词 **0 次** —— 1930 字里写满了
    「失落神女」「窃取太阳权柄的旧神阵营首领」这些**身份描述**，
    却没有一个字说明**他出手是什么规模**。

    ⇒ 模型只能按它最熟悉的物理尺度（刀/门板/石阶）去写，
    于是「旧神的一击」写成了「黑刃骤然穿过木门」。

    实测 A/B（同一世界观、同一场景、同一模型，只差这个块）：
    **高量级动作 A 组 0 次 / B 组 7 次**
    （沉进/化成/倒卷/退开/往上提…），
    且 B 组自发写出「她能烧我一里，我就退一里。可她站的地方，只有一里」
    ——**量级差进了对话**。

    ## 纪律

    - **只给本段在场的人**（其余角色的量级对这场戏没用，只占地方）；
    - **成对比较**（谁压谁）—— 模型最需要的不是「每个人多强」，
      而是「这两个打起来会怎样」；
    - 没有量级数据 ⇒ **整块不出现**（老项目提示词逐字节不变）。
    """
    try:
        from app.generation.power_scale import render_scale_contrast
        from app.generation.power_system import systems_of
    except Exception:  # noqa: BLE001 —— 渲染失败不该让整段提示词丢
        return ""
    if not plot_bible or not character_names:
        return ""
    chars = plot_bible.get("characters")
    if not isinstance(chars, list) or not chars:
        return ""
    wanted = {str(n).strip() for n in character_names if str(n).strip()}
    casts = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if not name or (wanted and name not in wanted):
            continue
        casts.append({"名字": name, "身份": c.get("role") or "",
                      "card": c.get("card") or {}})
    if not casts:
        return ""
    try:
        block = render_scale_contrast(casts, systems=systems_of(plot_bible))
    except Exception:  # noqa: BLE001
        return ""
    return f"{block}\n\n" if block else ""


def _character_profile_block(
    style_narrative: dict | None,
    *,
    profile: str | None,
    character_names: list[str] | None,
) -> str:
    """按生成模式和当前角色清单注入 L2 角色档案。"""
    from app.generation.profiles import normalize_profile
    mode = normalize_profile(profile)
    if mode == "original" or not style_narrative:
        return ""
    wanted = {str(n).strip() for n in (character_names or []) if str(n).strip()}
    if not wanted:
        return ""
    entries = style_narrative.get("characters") or []
    if not isinstance(entries, list):
        return ""
    from app.style.character_profile import render_character_profile
    depth = "brief" if mode == "roleplay" else "full"
    blocks = []
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("name") or "").strip() not in wanted:
            continue
        import os
        item = dict(entry)
        relations = item.get("关系网") or {}
        experimental = os.environ.get("SCENE_CONTRACT_ENABLED") == "1"
        if experimental:
            item["关系网"] = {k: v for k, v in relations.items() if k in wanted}
        blocks.append(render_character_profile(item, relation_depth=depth))
        if experimental and len(blocks) >= (1 if mode == "roleplay" else 3):
            break
    blocks = [block for block in blocks if block]
    if not blocks:
        return ""
    return "【L2角色档案】\\n" + "\\n".join(f"- {block}" for block in blocks) + "\\n"


def _voice_character_for(profile: str | None, character_names: list[str] | None) -> str | None:
    """角色聊天时，被扮演的是哪个角色 —— 只有它的话音词典该进 prompt。

    其它路径返回 None（全部角色的声音都给）。
    """
    if str(profile or "") != "roleplay":
        return None
    names = [str(n).strip() for n in (character_names or []) if str(n or "").strip()]
    return names[0] if names else None


def _narrative_block(
    style_narrative: dict | None,
    include_voice: bool = True,
    *,
    dims: Sequence[str] | None = None,
    voice_character: str | None = None,
    entity_table: dict | None = None,
) -> str:
    """把叙事档案渲染成静态提示块，进 system prompt。

    与【文风档案】的分工：【文风档案】是统计数字（L1 表层），【叙事档案】是
    LLM 提炼的叙事手法规则（L2/L3 认知层）。两者互补——数字管「像不像」，
    规则管「怎么写」。

    `include_voice=False` 时剔除【角色声音词典】——原创模式必须这样，
    否则模型会照着参考书的「自称『本世子』」造出一个参考书角色。

    `dims` 是创作路径给的维度白名单（角色聊天 = 一个手法维度都不给）。
    `voice_character` 只留这一个角色的说话方式（角色聊天必须这样 ——
    几个角色的腔调混在一起，模型会演串味）。
    """
    if not style_narrative:
        return ""
    from app.style.narrative import describe_narrative

    # 例句要脱敏才敢进提示词 ⇒ 需要专名表；没有表就不给例句。
    redactor = None
    guard: list[str] = []
    if entity_table:
        try:
            from app.style.entities import Redactor

            redactor = Redactor(entity_table)
        except Exception:  # noqa: BLE001 —— 建不了就不给例句
            redactor = None
        # 兜底闸：判定**之前**的 PMI 候选（专名表保证精度，不保证覆盖）
        raw_cands = entity_table.get("candidates") or []
        guard = [c for c in raw_cands if isinstance(c, str) and c]
    body = describe_narrative(
        style_narrative,
        include_voice=include_voice,
        dims=dims,
        voice_character=voice_character,
        redactor=redactor,
        example_guard=guard,
    )
    if not body:
        return ""
    return body + "\n"


def _character_template_block(style_narrative: dict | None, *, enabled: bool) -> str:
    """「人物塑造方法」块 —— **只给原创路径**。

    原创模式下参考书的角色是不可见的（characters 层不给读），所以「这个人
    怎么说话」这条线索是断的；这块补的是「**怎么塑造一个人物**」——
    从参考语料归纳出来的、零专名的做法，续写与角色聊天不需要（它们有真实
    人物与角色声音词典）。

    开关来自路径规则（`profiles.PROFILE_RULES[...]['include_character_template']`），
    不是这里自己判断的。
    """
    if not enabled:
        return ""
    from app.style.character_template import describe_character_template
    return describe_character_template(style_narrative)


def build_system_prompt(
    project_title: str,
    reminders: dict,
    style_dims: dict | None = None,
    plot_bible: dict | None = None,
    style_profile: dict | None = None,
    style_narrative: dict | None = None,
    profile: str | None = None,
    character_names: list[str] | None = None,
    entity_table: dict | None = None,
    override_style: str = "",
    override_constraint: str = "",
) -> str:
    """静态系统提示——整轮生成保持不变，置于最前，保证提示词缓存命中率。

    角色与伏笔 = plot_bible（用户构思）+ reminders（生成后图谱抽取）**合并**，
    不再二选一——见下方「合并而非遮蔽」注释。合并口径见 merged_constraint_items。

    `style_profile` 由 `app.style.pipeline.project_profile` 产出（按指纹缓存），
    渲染成静态块；自由生成（未选参考语料）时为 None，该块整段不出现。
    `style_narrative` 由 `app.style.narrative.project_narrative` 产出（按指纹缓存），
    是 LLM 提炼的 L2/L3 叙事规则，同样为 None 时不出现。

    `override_style` / `override_constraint` 是**运行时覆写**（`prompt_overrides` 表）：
    渲染成独立的一段，插在【风格】之后 / 【续写纪律】之后；默认空串 ⇒ 输出不变。
    """
    # —— 合并而非遮蔽 ——
    # 旧实现「有 plot_bible 就只取 bible」会让生成后抽取的事实（KG reminders）被静默丢弃：
    # 用户构思完圣经后，后续段落里新出现的角色 / 新埋的伏笔永远进不了约束，等于构思完就失忆。
    # 现改为：bible（用户构思，权威在前）+ reminders（生成后抽取，补充新增）合并去重。
    from app.generation.profiles import normalize_profile, rules_for

    _profile_rules = rules_for(normalize_profile(profile))
    chars, fores = merged_constraint_items(reminders, plot_bible)

    char_lines = (
        "\n".join(
            _character_constraint_line(c)
            for c in chars
            if isinstance(c, dict) and c.get("name")
        )
        or "  · 暂无锁定角色（自由发挥，但保持已写人物一致）"
    )

    # —— 量级对照（2026-10-29）——
    # 「这一击能有多大」不进提示词 ⇒ 模型把神明之战写成贴身短刃尺度
    # （实测高量级动作 A 组 0 次 vs B 组 7 次）。没有量级数据就整块不出现。
    _scale_block = _scale_contrast_block(plot_bible, character_names)

    hole_lines = (
        "\n".join(f"  · {f}" for f in fores if f)
        or "  · 暂无伏笔"
    )

    # —— 运行时覆写（只加不改；空串 ⇒ 渲染结果为空 ⇒ 输出逐字节不变）——
    _style_override = _override_block(override_style, "附加文风说明")
    _constraint_override = _override_block(
        override_constraint, "附加硬约束", leading_newline=True
    )

    return (
        f"你是一位长篇小说续写助手。当前创作项目：《{project_title}》。\n\n"
        "【角色人设约束】续写时必须保持以下角色一致，不得凭空新增主要人物，"
        "不得扭曲已确立的关系：\n"
        f"{char_lines}\n\n"
        "【未填伏笔（供你知情，不必本轮回收）】以下是已埋下、暂不打烊的悬念，"
        "你可以在后续段落里慢慢推进、隐晦呼应，或暂时不提；"
        "**不要在本次续写中集中兑现多个伏笔**，也不要用台词直接说出伏笔本身：\n"
        f"{hole_lines}\n\n"
        f"【风格】{_style_block(style_dims)}\n"
        "（以上维度是倾向参考，不要直接翻译成类型套路或标签化文风。）\n"
        f"{_style_override}"
        f"{_profile_block(style_profile)}\n"
        f"{_narrative_block(style_narrative, include_voice=_profile_rules.get('include_voice', True), dims=_profile_rules.get('narrative_dims'), voice_character=_voice_character_for(profile, character_names), entity_table=entity_table)}"
        f"{_character_template_block(style_narrative, enabled=bool(_profile_rules.get('include_character_template')))}"
        f"{_character_profile_block(style_narrative, profile=profile, character_names=character_names)}"
        f"{_scale_block}"
        "【文风要求：真人小说感，忌 AI 腔】"
        "- 禁止金句化台词：人物不许说出格言式总结或口号（如「不被遗忘本身就是最贵的代价」）；"
        "台词要像真人说话，可有停顿、改口、答非所问、言外之意。\n"
        "- 禁止段尾升华：不要在段落结尾习惯性地总结、拔高、抒情。\n"
        "- 禁止情绪直陈：不用「眼神里露出悲凉」「瞳孔骤缩」这类贴标签式的情绪描写，"
        "改用动作、姿态、语气、沉默来承载情绪。\n"
        f"{_psych_scene_boundary()}"
        "- 对话不承担设定讲解：人物不互相解释世界观或概念，读者靠情节自行理解。\n"
        "- 具体优于形容：少堆形容词，多写可感知的具体物件、动作、气味声响。\n"
        "- 允许留白：信息不必解释完，场景不必交代清，段落可以以一个动作或物件收住。\n"
        "- 省略号节制：避免在叙事中习惯性地用「……」「...」拖腔；\n"
        "用「，」「。」「——」承担语气停顿，不要让省略号成为情感拐杖。\n"
        "- 避免堆叠四字格：连续使用四字成语/对仗句会显得机械、像 AI 生成的「金句段」；\n"
        "人物对话和叙事以自然口语/长短句为主，四字格用一处即止。\n"
        "- 贴近真实节奏：长短句交错，不要每段都同样长度、不要句句工整。\n"
        f"{_description_guides()}\n"
        "【续写纪律】只输出正文；保持与上一段的连贯；不要重复已写过的内容；"
        "每一轮都是「连载的一节」，不是一篇独立完整的短篇小说——"
        "结尾必须停在某个仍未解决的冲突、悬念或动作中途（为下一节留钩子），"
        "不得在本次续写里收束主线、给出结局，也不要写「从此……／自此……」式的收尾；"
        "每轮控制在指定字数内。"
        f"{_constraint_override}"
    )


def build_user_prompt(
    prior_summaries: list[str],
    rag_context: list[str],
    outline: Any,
    max_chars: int,
    plot_bible: dict | None = None,
    premise: str | None = None,
    prior_blocks: list[str] | None = None,
    style_exemplars: list[dict] | None = None,
    rag_has_forbidden: bool = False,
    node_prompt: str | None = None,
    kg_slice: str | None = None,
    segment_no: int | None = None,
    prev_scene: str | None = None,
    rag_blocks: list[tuple[str, str]] | None = None,
    scene_contract: str | None = None,
    override_directive: str = "",
) -> str:
    """动态用户提示——置于系统提示之后，随前情/走向/冲突变化；频繁变更不影响系统层缓存。

    前情有两种口径，**必须分清**（步骤 2 的核心改动）：

    - `prior_blocks`（**新，生产走这条**）：`summary_tree.compose_prior` 产出的分层块
      （[主线] / [卷 k｜段 a-b] / [近情·段 n]），已按预算裁剪过、且有覆盖证明。
      它是自解释的文本，**不能再套「1. 2. 3.」编号**——编号会把卷摘要读成「第 N 段」，
      层级信息全丢。
    - `prior_summaries`（旧，兼容路径）：扁平段级链，仍按 1..N 编号渲染。

    两者都给时以 `prior_blocks` 为准；都不给即首段。

    注意：续写文风由 system 提示的【风格】（类型 + 风格平台调性）统一控制，
    这里不再携带 mode——mode 只表示「构思方式」（是否走 wayfinder 追问），属构思阶段语义，
    若在续写指令里再表达一次文风，会与风格平台冲突（如「番茄快节奏」vs「expert 细节丰盈」）。

    `segment_no` / `prev_scene`（v2.290 场景推进）：

    旧版把整条大纲（如 7 章）一次性给模型，只说「不要重复前情提要写过的场景」。
    实测五章里第 3/4/5 章全部困在「船上」同一个场景，因为 prompt 里没有任何
    东西告诉模型「本节该写第 3 章客栈」。

    现在传入当前段号后：

    1. 大纲列表里给当前项打 `→ 【本节】` 标记，并在前面插入【本节大纲目标】块；
    2. 续写指令追加场景推进硬约束，`prev_scene` 是上一节尾部的场景要素词
       （由 `narrative.scene_signature` 算出，零 LLM）。

    两个参数都可缺省——四个调用点全部向后兼容，行为与旧版逐字节一致。

    `node_prompt`（时间线，见 timeline.py）：传了它就走「按剧情点分段投喂」——
    **不注入整条【大纲】，也不注入 plot_bible 的 synopsis/direction**（那会把结局走向
    一次给全，导致第一章就讲完）。改注入当前剧情点的写作目标。
    没传则走原逻辑（向后兼容，旧项目/无时间线项目不受影响）。
    """
    if prior_blocks:
        prior = "\n".join(prior_blocks)
    elif prior_summaries:
        prior = "\n".join(f"{i+1}. {s}" for i, s in enumerate(prior_summaries))
    else:
        prior = "（首段，无前情）"

    # v2.7：智谱弱模型「写约 N 字」是软性，常 1300-1500 字就停
    # （v2.7 smoke 实测段 1 wc=1359 / 目标 3000 = 45%）。
    # directive 模板里写「写不少于 min_chars」是硬性下限，让模型倾向写够才出 stop marker。
    min_chars = int(max_chars * 0.8)

    from app.ingestion.clean import clean_consumption_text
    rag = "\n---\n".join(
        clean_consumption_text("\n".join(x) if isinstance(x, tuple) else str(x))[0]
        for x in rag_context
    ) if rag_context else "（无检索上下文，凭风格自由发挥）"

    # —— 大纲渲染（v2.290：给当前节打标记，解决「章节跑偏」）——
    # 旧版把 7 章全给，不标当前项。实测模型写完「上船」后顺势继续写船，
    # 第 3/4/5 章都在船舱，而大纲要求的是客栈/药铺/追兵。
    outline_items: list[str] = []
    cur_idx: int | None = None          # 0-based
    if outline and isinstance(outline, (list, tuple)):
        outline_items = [str(o) for o in outline]
        if segment_no is not None and 1 <= segment_no <= len(outline_items):
            cur_idx = segment_no - 1
    outline_text = ""
    if outline_items:
        lines = []
        for i, o in enumerate(outline_items):
            if cur_idx is not None and i == cur_idx:
                lines.append(f"- {o}   ← 【本节】")
            else:
                lines.append(f"- {o}")
        outline_text = "\n".join(lines)
    elif outline:
        outline_text = str(outline)

    # —— 本节大纲目标（v2.290）——
    # 单独成块放在大纲之前，用祈使句把「本节只写这一项」说死。
    # 只在大纲是列表、且段号落在范围内时输出。
    outline_target = ""
    if cur_idx is not None:
        cur = outline_items[cur_idx]
        prev = outline_items[cur_idx - 1] if cur_idx > 0 else None
        tlines = [
            "【本节大纲目标】",
            f"本节要写第 {cur_idx + 1} 项：「{cur}」",
            "必须在本节内完成这一项的事件；标题里的地点、人物、事件不能替换成别处，"
            "也不能只写它的前奏就停。",
        ]
        if prev:
            tlines.append(f"上一项（已写完，不要重写）：第 {cur_idx} 项：{prev}")
        nxt = outline_items[cur_idx + 1] if cur_idx + 1 < len(outline_items) else None
        if nxt:
            tlines.append(f"后续项（严禁提前写进本节）：第 {cur_idx + 2} 项：{nxt}")
        outline_target = "\n".join(tlines)

    # —— 世界背景（作者手写：这个世界是什么样）——
    # **时间线模式也注入**（与下面 plot_bible 的处理相反）。
    #
    # 为什么单独拎出来：plot_bible 里 synopsis/direction 讲的是「故事往哪走」，
    # 走向必须剔（否则第一段就奔结局）；而「这个世界是什么样」**不含走向**，
    # 剔掉它只会让模型每段自己现编一套（城名、规则、势力各写各的）。
    # 两者混在同一个 plot_bible 里一起剔，是「世界观前后不一致」的根因。
    #
    # 背景组里没有走向 ⇒ 注入它不会破坏节点粒度。
    # 没写背景 ⇒ **整块不输出**（空块是负面暗示：等于告诉模型「这个世界没有设定」）。
    from app.generation.background import render_background

    background_text = render_background(plot_bible.get("background") if isinstance(plot_bible, dict) else None)

    # —— 动态剧情指引（来自剧情圣经，随构思更新；不污染静态系统层）——
    # 时间线模式（传了 node_prompt）下，**剔除整个 plot_bible**——它里边的
    # synopsis/direction/relationships/foreshadowing/conflicts 任意一项都可能
    # 携带整条故事走向（"与顾辰逸相知相守""父亲是董事长""感情升温"等），模型
    # 拿到就会把后续剧情提前写进第一段，正是节点粒度问题的根因。
    # 当前剧情点的具体指引已经在 node_prompt 里（entry/exit/emotion/beats），
    # plot_bible 在时间线模式下的价值由 §7.3 节点规划阶段吸收——不需要再
    # 在生成 prompt 里二次注入。
    # 保留理由（向后兼容）：非时间线模式仍按原逻辑注所有 plot_bible 字段。
    #
    # 注意：**世界背景不在此列**（上面已单独渲染）——它是「世界长什么样」，
    # 不是「故事往哪走」。剔除规则因此只针对走向，不针对背景。
    bible_lines: list[str] = []
    if plot_bible and not node_prompt:
        synopsis = plot_bible.get("synopsis")
        direction = plot_bible.get("direction")
        conflicts = plot_bible.get("conflicts") or []
        relationships = plot_bible.get("relationships") or []
        fores = plot_bible.get("foreshadowing") or []
        if synopsis:
            bible_lines.append(f"整体梗概：{synopsis}")
        if direction:
            bible_lines.append(f"后续走向：{direction}")
        if conflicts:
            bible_lines.append("剧情冲突：" + "；".join(str(c) for c in conflicts))
        if relationships:
            # —— 空关系不渲染（2026-10-28）——
            # 原实现 `f"{a}与{b}——{relation}"` 在 relation 为空时把字面量
            # **None** 写进了提示词（实测：「人物关系：沈砚与沈铁山——None」）
            # ⇒ 模型看到的是「没有关系」还是「没有关系」？—— 两种解读都可能，
            # 而这条关系本来就没有信息量。空的就**整条不列**。
            rel_lines = [
                f"{r.get('a')}与{r.get('b')}——{str(r.get('relation')).strip()}"
                for r in relationships
                if isinstance(r, dict) and r.get("a")
                and r.get("relation") and str(r.get("relation")).strip()
                and str(r.get("relation")).strip().lower() != "none"
            ]
            if rel_lines:
                bible_lines.append("人物关系：" + "；".join(rel_lines))
        if fores:
            bible_lines.append("持续伏笔（继续埋或隐晦呼应）：" + "；".join(str(f) for f in fores))
    bible_block = "\n".join(bible_lines) if bible_lines else "（暂无剧情圣经，凭前情与风格续写）"

    # —— 首段 vs 续写，指令不同 ——
    # 首段：模型没有被「前情」约束，极易把整篇弧光一次性写完（用户实测第一段就完结）。
    # 必须正向要求「只铺陈设悬、不许收尾」，而不是只说「不要完结」。
    # 续写段：明确要求结尾停在未解决处，为下节留钩子，避免每段都自洽收束。
    is_opening = not prior_blocks and not prior_summaries
    # v2.290：开头也要对齐大纲第一项，否则容易从别的场景起笔。
    # 用前置前缀而不是在拼接链中间插 `+`——隐式拼接的括号表达式后面
    # 不能再直接跟字符串字面量（`(expr) "str"` 是语法错误，实际踩过一次）。
    _open_target_prefix = ""
    if is_opening and outline_target and outline_items:
        _open_target_prefix = (
            f"**本节必须写到大纲第 1 项的事件**：「{outline_items[0]}」，"
            f"不要从别处起笔。\n"
        )
    if is_opening:
        directive = _open_target_prefix + (
            f"这是整篇的**开头**。请从建立人物与情境写起，抛出一个推动故事的冲突或事件。"
            # v2.274：字数要求必须是**带后果的硬约束**。
            # 旧措辞「不少于 {min_chars} 字（建议 {max_chars} 字）」实测只写 851 字（下限的 35%）——
            # 「建议」二字让模型把 3000 当成「酌情」，讲完一个场景就自己收尾了。
            # A/B 实测同一 prompt 只改这一句：1285 字 → 5917 字。
            f"**长度是硬要求：正文必须写满 {min_chars} 字以上，少于 {min_chars} 字视为不合格、需要重写。**"
            f"（这里的「字」按**汉字**计，标点、空格与换行不算。）"
            f"请在这个长度内充分展开场景、动作、对白与人物反应，不要用概述跳过时间（"
            f"例如「接下来的几天里他们遇到了各种困难」这种一句话带过一周的写法是禁止的）。"
            f"**本节只负责铺陈与设悬，不要收尾、不要给出结局、不要写『从此……』式的收束**，"
            f"把更大的冲突留到后面展开。"
            f"风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——告诉模型在节末写 stop marker，
            # provider 会传 stop=["【本节完】", ...] 给 LLM，模型写到此 marker 时停
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口（后端会切分正文与这个 marker）。"
        )
    else:
        directive = (
            # v2.274：同上，硬约束替代「建议」措辞。
            f"**长度是硬要求：正文必须写满 {min_chars} 字以上，少于 {min_chars} 字视为不合格、需要重写。**"
            f"（这里的「字」按**汉字**计，标点、空格与换行不算。）"
            f"请在这个长度内充分展开本节的场景、动作、对白与人物反应，"
            f"不要用概述跳过时间（例如「接下来的几天里他们遇到了各种困难」这种一句话带过一周的写法是禁止的），"
            f"也不要重复前情提要里已经写过的场景。"
            # v2.290：场景推进硬约束。
            # 旧版只有上面那句软的「不要重复」，模型没有可对照的锚点。
            # 现在把上节尾部的**具体要素词**摆出来，它知道「避开哪些就等于换场景」。
            # v2.291：场景推进**以大纲为准**，不强制换场景。
            #
            # 首版写的是「本节必须发生场景转移，请从这五类里另选一类」。
            # 这是把症状当病因：真正的问题不是「三章同一场景」，而是
            # **没按大纲走**（大纲要求客栈/药铺/追兵，实际三章停在船上）。
            #
            # 而「一个场景连续写几章」在长篇里完全正常——大战在一个区域
            # 打三章、对峙拉锯两章，都是作者的正常写法。硬性强制换场景
            # 会破坏这种节奏，逼模型跳过情节。
            #
            # 现在的写法：上一节的场景只作为**判断依据**告知模型，
            # 换不换由本节大纲决定。
            + (
                f"上一节写的是「{prev_scene}」场景。本节是延续它还是转到别处，"
                f"以【本节大纲目标】为准：大纲要求换地点/换事件就转移，"
                f"要求接着写（如一场战斗的后续）就延续，不要为了换场景跳过情节。"
                if prev_scene
                else ""
            )
            + f"结尾停在某个仍未解决的冲突 / 悬念 / 动作中途，为下一节留钩子——不要在此收束主线或给出结局。"
            f"风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——同上
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口。"
        )

    # —— 运行时覆写（directive_suffix）：并进【续写指令】这一段，**不另起一块**——
    # 另起一块会被后面的【句法样板】挤到注意力更弱的位置，而样板刻意放最末；
    # 并进指令段则落在「模型正在读指令」的位置上。
    _directive_override = (override_directive or "").strip()
    if _directive_override:
        directive = directive + "\n" + _directive_override

    blocks = [
        "【前情提要】",
        prior,
        "",
    ]
    # —— 世界背景放在「剧情圣经·动态指引」**之前** ——
    # 顺序有理由：背景是**恒定设定**（这一段和后面每一段都一样），
    # 动态指引是**会变的**（伏笔继续埋、冲突升级）。
    # 先恒定后变化，模型读到最后时记忆里最靠前的是背景 ⇒ 不容易随剧情漂移。
    # 没有背景组 ⇒ 整块不出现（不留空标题）。
    if background_text:
        blocks += [
            "【世界背景（作者设定，全书一致，不得与之矛盾）】",
            background_text,
            "",
        ]
    blocks += [
        "【剧情圣经·动态指引】",
        bible_block,
    ]
    if scene_contract and scene_contract.strip():
        blocks += ["", scene_contract.strip()]
    # premise：用户给的故事大概，**时间线模式下剔除**（同 plot_bible 一样
    # 携带整条故事走向，与 node_prompt 职责重复）。非时间线模式保留作
    # "基调锚点"。
    #
    # —— 剔除规则的精确口径（2026-10-30）——
    # 剔的是 premise 里的**走向部分**，不是它的全部内容。
    # premise 是用户写的自由文本，世界设定与剧情走向**混在一起**
    # （"末法时代灵气枯竭，主角在矿盟废墟上拜师" —— 前半句是背景，
    # 后半句是走向）。整块剔掉 ⇒ 连背景一起丢，模型只好自己现编世界观。
    #
    # **不在提示词层做句子级切分**（脆弱、静默丢内容）：
    # 拆分改在**合成剧情圣经**时做一次——模型额外产出 `background`
    # （世界设定，走 background 通道），premise 留在走向通道。
    # 于是这里剔 premise = 剔它的走向部分，而世界设定从 background 通道进。
    # 老项目（没 background）可用「AI 帮我补全」从 premise 抽一份补上。
    if premise and not node_prompt:
        blocks += ["", "【小说大概内容（用户给定，作为基调锚点）】", premise]
    # v2.276：空检索上下文时**整块不输出**。
    # 旧代码无条件输出块头 + 「（无检索上下文，凭风格自由发挥）」占位——那是在
    # 告诉模型「本次没有风格素材，你自己看着办」，属于负面暗示。
    # 原创模式（profile=original）下 rag_context 恒为空，这一块必须整个消失。
    if rag_blocks:
        # v2.293：分块渲染，每块自带语义标签。
        #
        # 旧实现把位置检索和语义检索的结果拼成一段纯文本，模型分不清
        # 哪块是「紧接着的前文」、哪块是「参考语料的气氛样例」，
        # 结果把参考语料的剧情当成已发生的事实写进本书。
        #
        # 现在每块一个标题，直接说明这块是什么、该怎么用：
        #   【前文衔接…必须与之自然衔接】
        #   【参考语料同类场景…严禁复用其中情节与人名】
        #   【角色原文片段…用于把握该角色的言行】
        for title, body in rag_blocks:
            if not body.strip():
                continue
            if rag_has_forbidden:
                title = title + "（注意：以下原文可能含【反向禁忌】中的表达，严禁使用）"
            blocks += ["", title, clean_consumption_text(body)[0]]
    elif rag_context:
        rag_header = "【检索上下文（参考风格素材，可化用不可照抄）】"
        if rag_has_forbidden:
            rag_header += "（注意：以下原文中可能含有系统提示【反向禁忌】列出的表达，仅供了解剧情，续写时严禁使用这些表达）"
        blocks += [
            "",
            rag_header,
            rag,
        ]
    if node_prompt:
        # 时间线模式：只投喂当前剧情点，不投喂整条【大纲】
        blocks += ["", "【当前剧情进度（按点推进，不要越过本点）】", node_prompt]
    elif outline_text:
        if outline_target:
            blocks += ["", outline_target]
        blocks += ["", "【大纲（标出「本节」的那一项是本节的写作目标，其余是后续，不要提前写）】",
                   outline_text]
    if kg_slice:
        # v3.0 KG 切片：按当前剧情点动态取的近期实体/状态/伏笔/情报/事件。
        # 只标红不强制——是给模型的「世界观锚点」，让它别写飞。与 plot_bible 区别：
        # plot_bible 是用户构思（含走向，时间线模式剔除），kg_slice 是已生成事实。
        blocks += ["", "【世界观事实（已发生，续写须保持一致，不得矛盾）】", kg_slice]
    blocks += ["", "【续写指令】", directive]
    if style_exemplars:
        # 风格样例：few-shot 原文，教模型「怎么写」而非「写什么」。
        #
        # ## 为什么放在提示的**最末**
        #
        # 原文样例是唯一能传递「句子怎么断」的信号 —— 数字指令实测无效：
        # 提示里明明写着「句长 56±18.8 字」，不给样例时模型写出的是 10.9 字/句
        # （参考书 43~56 字）。放末尾 = 落在模型注意力最强的那一段。
        #
        # ## 为什么口径要写死「照它的句法写」
        #
        # 只说「学习语气/节奏/段落编排」太弱，模型会当成「内容参考」。
        # 实测（真书 + 真模型，各 3 次）：
        #   弱口径 + 600 字样例 + 埋在中间 → 句长 11.1（几乎没动）
        #   强口径 + 1200 字样例 + 放末尾 → 句长 12.8，句号密度 z 从 +11.8 降到 +8.3
        #
        # 「不要复用其内容」这句必须留着：样例是写法参照，不是剧情素材。
        #
        # ## 口径里为什么不能写「只有对白用短句」
        #
        # 那是当初为了治「叙述句太碎」写的，**但它授权了另一种碎法**：
        # 真书实测，参考书的含引号行**中位 47 字**（叙述与台词同段），
        # 裸台词行只占含引号行的 **23%**；而模型写的是 **68%** 裸台词行、
        # 含引号行中位只有 **12 字** —— 一行一句「“别动。”」「“客人？”」。
        # 那句口径正是这么写出来的，所以要改成「对白也不单独成行」。
        exemplar_lines = []
        exemplar_texts = []
        for i, ex in enumerate(style_exemplars, 1):
            title = ex.get("novel_title") or f"语料{ex.get('novel_id')}"
            safe_text = clean_consumption_text(ex.get("text", ""))[0]
            exemplar_texts.append(safe_text)
            exemplar_lines.append(f"—— 样板 {i}（来自《{title}》）——\n{safe_text}")
        # —— 对话归属位置的口径（2026-10-31 新加）——
        #
        # 为什么要有这一句：盲评第一次真跑判 FAIL，逐对排查发现
        # **只用一条写法规则就能定位哪边是人类侧**（精确率 100%）：
        # 参考书写「X道：“…”」（引语在说话人后面），我们写「“…。”某某说」
        # 或「某某说：“…”」（说字派）。八道闸门查的是内容，这一维是写法。
        #
        # 为什么**光给样例不够**：实测那 3 条样例里已经有 29 次「X道：“…”」，
        # 模型照样只写出 0.06 次/百字（差 16 倍）⇒ 形态要**样例 + 显式口径**一起给。
        #
        # 为什么口径是**量出来的**而不是写死的：不同参考书差别极大
        # （《魔道祖师》0.99 次/百字、《校花的透视高手》0.00），
        # 写死「一律用『道』」会在别的书上教错 ⇒ 按这本书自己的样例量。
        attr_directive = attribution_directive(exemplar_texts)
        style_rules = [
            "请用同样的句法写本次要写的这一段：叙述句要写到该写长的时候再断，"
            "一句里带上动作、环境、神态，不要写成每句十来个字的短句流水账；"
            "对白也不要单独成行——说话人的动作、神态、语气要和台词写进同一段，照样板里的样子。",
        ]
        if attr_directive:
            style_rules.append(attr_directive)
        style_rules.append("样板只用来学写法，**不要复用其中的情节与人物**。")
        blocks += [
            "",
            "【句法样板（照着写）】",
            "下面是参考书原文的几个片段。它们示范了这个作者的**句子长度、断句位置、标点习惯**。",
            *style_rules,
            "\n\n".join(exemplar_lines),
        ]
    return "\n".join(blocks)
