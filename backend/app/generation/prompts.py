"""M4 提示词组装。

- build_system_prompt：固化人设/风格 + M3.3 约束（未填坑 + 角色约束），
  这是防漂移的核心——角色与悬念始终挂在系统层，不被前情淹没。
- build_user_prompt：前情提要（段级总结链）+ 检索上下文（RAG）+ 大纲 + 续写指令。

提示里的【前情提要】【检索上下文】【大纲】标记同时被 LocalFallbackLLMProvider
解析回显，保证离线可验证约束进入生成环。
"""

from typing import Any


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


def merged_constraint_items(
    reminders: dict, plot_bible: dict | None
) -> tuple[list[dict], list[str]]:
    """「角色 / 伏笔」约束的**唯一合并口径**——system 提示块与 constraints 共用。

    权威顺序（评审 A 的 Q6）：plot_bible（用户构思 / bible）在前，图谱抽取在后。
    两处必须共用本函数，否则会出现「system 里列了、user 尾部那份自称完整的锁定名单里没列」
    这种自相矛盾的提示词——最高权威层被漏掉，模型会以为游离在约束之外。
    """
    bible_chars = (plot_bible.get("characters") or []) if plot_bible else []
    kg_chars = reminders.get("character_constraints") or []
    bible_fores = (plot_bible.get("foreshadowing") or []) if plot_bible else []
    kg_fores = [h.get("question") for h in (reminders.get("unresolved_plotholes") or [])]
    return _merge_named(bible_chars, kg_chars), _merge_text(bible_fores, kg_fores)


def _narrative_block(style_narrative: dict | None) -> str:
    """把叙事档案渲染成静态提示块，进 system prompt。

    与【文风档案】的分工：【文风档案】是统计数字（L1 表层），【叙事档案】是
    LLM 提炼的叙事手法规则（L2/L3 认知层）。两者互补——数字管「像不像」，
    规则管「怎么写」。
    """
    if not style_narrative:
        return ""
    from app.style.narrative import describe_narrative
    body = describe_narrative(style_narrative)
    if not body:
        return ""
    return body + "\n"


def build_system_prompt(
    project_title: str,
    reminders: dict,
    style_dims: dict | None = None,
    plot_bible: dict | None = None,
    style_profile: dict | None = None,
    style_narrative: dict | None = None,
) -> str:
    """静态系统提示——整轮生成保持不变，置于最前，保证提示词缓存命中率。

    角色与伏笔 = plot_bible（用户构思）+ reminders（生成后图谱抽取）**合并**，
    不再二选一——见下方「合并而非遮蔽」注释。合并口径见 merged_constraint_items。

    `style_profile` 由 `app.style.pipeline.project_profile` 产出（按指纹缓存），
    渲染成静态块；自由生成（未选参考语料）时为 None，该块整段不出现。
    `style_narrative` 由 `app.style.narrative.project_narrative` 产出（按指纹缓存），
    是 LLM 提炼的 L2/L3 叙事规则，同样为 None 时不出现。
    """
    # —— 合并而非遮蔽 ——
    # 旧实现「有 plot_bible 就只取 bible」会让生成后抽取的事实（KG reminders）被静默丢弃：
    # 用户构思完圣经后，后续段落里新出现的角色 / 新埋的伏笔永远进不了约束，等于构思完就失忆。
    # 现改为：bible（用户构思，权威在前）+ reminders（生成后抽取，补充新增）合并去重。
    chars, fores = merged_constraint_items(reminders, plot_bible)

    char_lines = (
        "\n".join(
            f"  · {c.get('name')}（{c.get('role') or '角色'}"
            f"{('；动机：' + c.get('motivation') + '）') if c.get('motivation') else '）'}"
            for c in chars
            if isinstance(c, dict) and c.get("name")
        )
        or "  · 暂无锁定角色（自由发挥，但保持已写人物一致）"
    )

    hole_lines = (
        "\n".join(f"  · {f}" for f in fores if f)
        or "  · 暂无伏笔"
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
        f"{_profile_block(style_profile)}\n"
        f"{_narrative_block(style_narrative)}"
        "【文风要求：真人小说感，忌 AI 腔】\n"
        "- 禁止金句化台词：人物不许说出格言式总结或口号（如「不被遗忘本身就是最贵的代价」）；"
        "台词要像真人说话，可有停顿、改口、答非所问、言外之意。\n"
        "- 禁止段尾升华：不要在段落结尾习惯性地总结、拔高、抒情。\n"
        "- 禁止情绪直陈：不用「眼神里露出悲凉」「瞳孔骤缩」这类贴标签式的情绪描写，"
        "改用动作、姿态、语气、沉默来承载情绪。\n"
        "- 对话不承担设定讲解：人物不互相解释世界观或概念，读者靠情节自行理解。\n"
        "- 具体优于形容：少堆形容词，多写可感知的具体物件、动作、气味声响。\n"
        "- 允许留白：信息不必解释完，场景不必交代清，段落可以以一个动作或物件收住。\n"
        "- 贴近真实节奏：长短句交错，不要每段都同样长度、不要句句工整。\n\n"
        "【续写纪律】只输出正文；保持与上一段的连贯；不要重复已写过的内容；"
        "每一轮都是「连载的一节」，不是一篇独立完整的短篇小说——"
        "结尾必须停在某个仍未解决的冲突、悬念或动作中途（为下一节留钩子），"
        "不得在本次续写里收束主线、给出结局，也不要写「从此……／自此……」式的收尾；"
        "每轮控制在指定字数内。"
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

    rag = "\n---\n".join(rag_context) if rag_context else "（无检索上下文，凭风格自由发挥）"

    outline_text = ""
    if outline:
        if isinstance(outline, (list, tuple)):
            outline_text = "\n".join(f"- {o}" for o in outline)
        else:
            outline_text = str(outline)

    # —— 动态剧情指引（来自剧情圣经，随构思更新；不污染静态系统层）——
    # 时间线模式（传了 node_prompt）下，**剔除整个 plot_bible**——它里边的
    # synopsis/direction/relationships/foreshadowing/conflicts 任意一项都可能
    # 携带整条故事走向（"与顾辰逸相知相守""父亲是董事长""感情升温"等），模型
    # 拿到就会把后续剧情提前写进第一段，正是节点粒度问题的根因。
    # 当前剧情点的具体指引已经在 node_prompt 里（entry/exit/emotion/beats），
    # plot_bible 在时间线模式下的价值由 §7.3 节点规划阶段吸收——不需要再
    # 在生成 prompt 里二次注入。
    # 保留理由（向后兼容）：非时间线模式仍按原逻辑注所有 plot_bible 字段。
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
            bible_lines.append(
                "人物关系："
                + "；".join(
                    f"{r.get('a')}与{r.get('b')}——{r.get('relation')}"
                    for r in relationships
                    if isinstance(r, dict) and r.get("a")
                )
            )
        if fores:
            bible_lines.append("持续伏笔（继续埋或隐晦呼应）：" + "；".join(str(f) for f in fores))
    bible_block = "\n".join(bible_lines) if bible_lines else "（暂无剧情圣经，凭前情与风格续写）"

    # —— 首段 vs 续写，指令不同 ——
    # 首段：模型没有被「前情」约束，极易把整篇弧光一次性写完（用户实测第一段就完结）。
    # 必须正向要求「只铺陈设悬、不许收尾」，而不是只说「不要完结」。
    # 续写段：明确要求结尾停在未解决处，为下节留钩子，避免每段都自洽收束。
    is_opening = not prior_blocks and not prior_summaries
    if is_opening:
        directive = (
            f"这是整篇的**开头**。请从建立人物与情境写起，抛出一个推动故事的冲突或事件，"
            f"节奏舒展地写约 {max_chars} 字；**本节只负责铺陈与设悬，不要收尾、不要给出结局、"
            f"不要写『从此……』式的收束**，把更大的冲突留到后面展开。"
            f"风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——告诉模型在节末写 stop marker，
            # provider 会传 stop=["【本节完】", ...] 给 LLM，模型写到此 marker 时停
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口（后端会切分正文与这个 marker）。"
        )
    else:
        directive = (
            f"请续写约 {max_chars} 字，并在本节结尾停在某个仍未解决的冲突 / 悬念 / 动作中途，"
            f"为下一节留钩子——不要在此收束主线或给出结局。"
            f"风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——同上
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口。"
        )

    blocks = [
        "【前情提要】",
        prior,
        "",
        "【剧情圣经·动态指引】",
        bible_block,
    ]
    # premise：用户给的故事大概，**时间线模式下剔除**（同 plot_bible 一样
    # 携带整条故事走向，与 node_prompt 职责重复）。非时间线模式保留作
    # "基调锚点"。
    if premise and not node_prompt:
        blocks += ["", "【小说大概内容（用户给定，作为基调锚点）】", premise]
    rag_header = "【检索上下文（参考风格素材，可化用不可照抄）】"
    if rag_has_forbidden:
        rag_header += "（注意：以下原文中可能含有系统提示【反向禁忌】列出的表达，仅供了解剧情，续写时严禁使用这些表达）"
    blocks += [
        "",
        rag_header,
        rag,
    ]
    if style_exemplars:
        # 风格样例：few-shot 原文，教模型「怎么写」而非「写什么」。
        # 明确标注「不要复用其内容」——样例是风格参照，不是剧情素材。
        exemplar_lines = []
        for i, ex in enumerate(style_exemplars, 1):
            title = ex.get("novel_title") or f"语料{ex.get('novel_id')}"
            exemplar_lines.append(f"—— 样例 {i}（来自《{title}》）——\n{ex.get('text', '')}")
        blocks += [
            "",
            "【风格样例（参考语料原文，学习其语气/节奏/段落编排/人物口吻，不要复用其内容）】",
            "\n\n".join(exemplar_lines),
        ]
    if node_prompt:
        # 时间线模式：只投喂当前剧情点，不投喂整条【大纲】
        blocks += ["", "【当前剧情进度（按点推进，不要越过本点）】", node_prompt]
    elif outline_text:
        blocks += ["", "【大纲】", outline_text]
    blocks += ["", "【续写指令】", directive]
    return "\n".join(blocks)
