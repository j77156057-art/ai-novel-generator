"""M4 提示词组装。

- build_system_prompt：固化人设/风格 + M3.3 约束（未填坑 + 角色约束），
  这是防漂移的核心——角色与悬念始终挂在系统层，不被前情淹没。
- build_user_prompt：前情提要（段级总结链）+ 检索上下文（RAG）+ 大纲 + 续写指令。

提示里的【前情提要】【检索上下文】【大纲】标记同时被 LocalFallbackLLMProvider
解析回显，保证离线可验证约束进入生成环。
"""

import re
from typing import Any

#: 提示词版本（与 `consistency.GEN_GATE_VERSION` 的闸门口径对齐，逐章记进
#: `quality_json.prompt_revision`）。存在意义是让「这一章是在哪套提示下生成的」
#: 可事后区分——闸门版本只管判了什么，提示版本管模型收到了什么指令。
#:
#: v10：句数配额 + 段数按 4,000 目标折算的 189 硬限 + 「极短纯对白段必须为 0」+
#:      「不得靠删内容抬段均」与闸门（非零章 p90=3.2%、段数 ≥70、汉字 ≤4800）
#:      并存但未对齐。
#: v11（2026-11-14）：提示与闸门口径对齐——可长到值与 4800 取小、段数上限只留
#:      「汉字数 ÷ 21」一条折算（不低于档案下限）、超长时压缩第一并明确允许删、
#:      极短纯对白段上限读闸门现推值、句数声明为中心值且不得突破硬上限。
#: v12（2026-11-14）：新增**对白配比参考线**（非硬判罚）——参考书章级
#:      `对白字占比` 的 p25/p50/p75 + 本书最近一卷实测值写进 user 尾部提示，
#:      让卷 2 已漂到 40.8%（参考 p50 26.9%）的对白占比有可见的收敛方向。
#:      判罚项与阈值不变；分位由 engine 从档案现推，无档案时退回配置/常量表。
#: v13（2026-11-14）：①删掉「段数上限 = max(档案下限, 汉字数÷21)」——该上限
#:      既不在闸门判、也没被执行（第 29 章按它该 ≤184 段，实际 324 段），
#:      提示不再说没人执行的话；`段长均值` 同步不再写「参考带」，只记录不设目标。
#:      ②段长缺口拆成 `段均句数 × 句长均值` 后，主因 `句长均值` 以参考线进提示
#:      （卷口径 p10/p50/p90 + 本书最近 14 章实测），非硬判罚。判罚阈值不变。
#: v14（2026-11-15）：三处写死值改档案现推，判罚项/阈值不动——
#:      ①句子分布（≤5 字短句至少 N 句、≥40 字长句至少 M 句）不再读常量表：
#:      总句数按「目标汉字数 × 参考书章级『句数 ÷ 汉字数』p50」折算（无任何
#:      手写换算，唯该项删档时退回字符/汉字 1.3 并留痕），N/M 取参考书
#:      **卷口径**短句/长句占比 p10；档案缺失退回配置并记 fallback_reason。
#:      ②「对白按每 10 句配比 5/2/2/2」改为参考书**章级**陈述/问句/感叹/叹词
#:      分位折算的句数，比例不再手写。
#:      ③`_VOICE_GENERIC_MARKERS` 降级为配置项 `voice_generic_markers`
#:      （无法由频次分位生成，原表作退回值），已登记写死值审计文档。
#: v15（2026-11-15）：删指令、换注入口径，判罚项/阈值不动——
#:      ①删掉「≤5 字短句至少 N 句 / ≥40 字长句至少 M 句」：第 31 章实到约 17 / 8 句，
#:      既没被执行、闸门也不判；短句/长句只保留 `_gate_block` 的 p10–p90 百分比区间。
#:      ②非时间线模式下不再注入整条大纲（全部章条目），改为只注入**本章大纲条目**：
#:      越章材料（大纲五拍提前写掉第 4/5 章）正是整条大纲入 prompt 造成的。
#: v16（2026-11-15）：初稿取到外部素材时，user prompt 末尾追加统一块头
#:      「【外部资料 · 仅作素材】」；**没取到素材时提示逐字不变**（不加任何字符）。
#:      素材轮本身走 tools（模板在 engine），不改变本章大纲与人物设定。
#: v17（2026-11-15）：`era_guard_enabled` 默认打开后，system 会按项目基线追加一段
#:      ≤300 字的时代约束（既有 `era_constraint` 机制，不是判罚）；基线判不出时不追加。
#:      生成指令、判罚阈值、素材块均未改。
#: v18（2026-11-15）：两批措辞改动合并登记——
#:      ①字数措辞：初稿下限从 3200 抬到合格带下沿（3400），并显式写出
#:        「初稿合格带 3400–4800」，避免模型写到 3200 后被补完判偏短；
#:        补完触发线仍是 3304，判罚上限仍是 4800，阈值未动。
#:      ②逐章推进三条要求：属性 / 阵容 / 时间锚点各一条，全部对应
#:        `progress_ledger` 的留痕，不新增判罚项。
#: v19（2026-11-22）：对白配比从参考线改为**硬约束**——
#:      ①`_gate_block` 的对白带改读 `active_gate_limits()`（下限=参考线 p25），
#:        不再印常量表里的 6%，并写明「低于下限判不合格」；
#:      ②`_dialogue_reference_block` 改成「硬约束」措辞，给出本章硬下限、
#:        最近若干章低于下限时明确要求把对白拉起来；
#:      ③判罚项由 engine 注入 `judge["对白字占比"]`，无参考书时不注入（旧行为）。
PROMPT_REVISION = "prompt-v19"

#: 物品持有块的标题 / 优先级说明。生成与重写共用同一份渲染，避免两条路径各拼一份、
#: 重写时漏块或块文本漂移。
ITEM_HOLDING_HEADER = "【物品持有事实（已写内容最新一段；续写须与此一致）】"
ITEM_HOLDING_PRIORITY_NOTE = (
    "（本块只补「物品在谁手上」，不改动也不覆盖【剧情圣经】与【世界观事实】"
    "的既有约束。三者冲突时按「作者设定 > 世界观事实 > 本块」取信，"
    "行文中回避正面矛盾，不要静默改写物品归属。）"
)


def render_item_holding_section(item_holding_block: str | None) -> str:
    """把物品持有事实渲染成完整段落（标题 + 明细 + 优先级说明）。

    生成（`build_user_prompt`）与 AI 味重写（`engine._rewrite_prompt`）共用这一个
    函数：重写必须携带与初稿**逐字相同**的块，不能另拼一份。
    """
    if not item_holding_block:
        return ""
    return "\n".join([
        ITEM_HOLDING_HEADER,
        item_holding_block,
        ITEM_HOLDING_PRIORITY_NOTE,
    ])


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

    **bible 删除权威**（kg-cascade-protocol 阶段 2-3）：bible.characters **显式列了**
    名字时，未列入的 KG 角色约束整条剔除——用户在 bible 里删掉「顾辰逸」后，
    旧抽取事实再把他说成锁定角色，会与新设定矛盾（旧段不可改，但未来段必须只认新设定）。

    **空列表 ≠ 未宣告**（B3，2026-11-11）：`"characters": []` 是作者显式声明
    「本项目现在没有任何角色」，必须把 KG 角色约束全部剔除；只有**没有**
    `characters` 键（老项目 bible 只有 relationships/foreshadowing）才不过滤。
    旧实现用 `if bible_chars` 把两者混为一谈，删光角色后旧 KG 角色会回流。
    """
    bible = plot_bible if isinstance(plot_bible, dict) else None
    has_roster = bible is not None and "characters" in bible
    raw_chars = bible.get("characters") if bible else []
    bible_chars = raw_chars if isinstance(raw_chars, list) else []
    kg_chars = reminders.get("character_constraints") or []
    # 作者侧台账优先：`reminders["foreshadow_ledger"]` 一旦由引擎写入（哪怕是空
    # 列表，表示「台账存在但都回收了」），就不再读旧字符串字段，避免双权威。
    if "foreshadow_ledger" in reminders:
        bible_fores = list(reminders.get("foreshadow_ledger") or [])
    else:
        bible_fores = (bible.get("foreshadowing") or []) if bible else []
    kg_fores = [h.get("question") for h in (reminders.get("unresolved_plotholes") or [])]
    if has_roster:
        declared: set[str] = {
            str((c.get("name") if isinstance(c, dict) else c) or "").strip()
            for c in bible_chars
        }
        declared.discard("")
        if declared:
            kg_chars = [
                c for c in kg_chars
                if not isinstance(c, dict)
                or not str(c.get("name") or "").strip()
                or str(c.get("name")).strip() in declared
            ]
        else:
            # 显式空清册：本项目现在没有任何角色 → 不再带任何 KG 角色约束。
            kg_chars = []
    return _merge_named(bible_chars, kg_chars), _merge_text(bible_fores, kg_fores)


def _narrative_block(style_narrative: dict | None,
                     allowed_novel_ids: set[int] | list[int] | None = None,
                     *, opening: bool = False,
                     mode: str = "original",
                     timeline: dict | None = None) -> str:
    """把叙事档案渲染成静态提示块，进 system prompt。

    与【文风档案】的分工：【文风档案】是统计数字（L1 表层），【叙事档案】是
    LLM 提炼的叙事手法规则（L2/L3 认知层）。两者互补——数字管「像不像」，
    规则管「怎么写」。

    `allowed_novel_ids` 用于来源隔离：续写项目只渲染续写书自己的规则，
    原创项目按当前参考书集合渲染；None 表示不过滤（兼容旧调用）。

    `opening`：首段（`is_opening`）时降权「越来越精简」的漂移趋势，并加一句
    篇幅优先——开篇需要铺陈，不能让参考语料的后期收缩趋势把开篇写成短摘要。
    档案本身不改，只在渲染层过滤。

    `mode`：渲染用途（original / continuation / dialogue），决定配额与声音排序。
    传错由 `describe_narrative` 抛 ValueError，不静默落回原创。
    `timeline`：KG 侧剧情时间线；续写必填，缺了由 `describe_narrative` 报错。
    """
    if not style_narrative:
        return ""
    from app.style.narrative import describe_narrative
    body = describe_narrative(
        style_narrative, allowed_novel_ids=allowed_novel_ids, opening=opening,
        mode=mode, timeline=timeline,
    )
    if not body:
        return ""
    return body + "\n"


def _catalog_skeleton_block(text: str | None) -> str:
    """目录骨架常驻块（设计稿 §5.12.1）：只给路径名 + 条数，不塞规则正文。

    固定落在【文风要求】/文风硬指标之后、【续写纪律】之前：这是原创档的常驻
    导航块，作者写正文时先看到验收口径，再看到「可以自己查什么」。骨架自身
    已按「通用→结构→技法」排序，这里只补一句用途说明，避免模型把条数当成
    正文素材。

    **路径键映射（2026-11-13）**：骨架渲染的是中文标签，而 `browse/search`
    的 `path/l1` 以英文键为主。工具侧已对中文标签做归一，照标签拼路径不会再
    空转；这里仍追加一张从 `catalog_paths` 常量生成的对照表，让模型优先用
    英文键做精确对照，不猜键名。
    """
    if not text:
        return ""
    return (
        "\n【检索目录（导航用）】下面只列出规则分布，供你决定查哪一类；"
        "不要把路径名或条数写进正文。\n"
        f"{text}\n"
        f"{_catalog_path_keys()}\n"
    )


def _catalog_path_keys() -> str:
    """五级目录的「英文键=中文标签」速查（优先用左值，中文标签也可直接使用）。"""
    try:
        from app.retrieval.catalog_paths import (
            L1_LABELS, L2_LABELS, L3_LABELS, L4_LABELS,
        )
    except Exception:  # noqa: BLE001 —— 映射表只是导航增强，缺了不影响正文
        return ""
    l1 = " / ".join(f"{k}={v}" for k, v in L1_LABELS.items())
    l2 = " / ".join(f"{k}={v}" for k, v in L2_LABELS.items())
    l3 = " / ".join(f"{k}={v}" for k, v in L3_LABELS.items())
    l4 = " / ".join(f"{k}={v}" for k, v in L4_LABELS.items())
    return (
        "【路径键（优先用英文键；中文标签也会被工具归一，可直接使用）】\n"
        f"l1：{l1}\n"
        f"L2 场景域：{l2}\n"
        f"L3 节拍：{l3}\n"
        f"L4 技法：{l4}\n"
        "层级拼法：结构卡 `style/<L2键>/<L3键>`，技法卡 `style/<L2键>/<L4键>`；"
        "不确定就先 `browse(level=1)` → `browse(level=2, path=\"style\")` 拿真实键。"
    )


#: 提示词覆写（runtime override）块标题。三个标题与
#: `app.agents.prompt_overrides.PromptKey` 的三个槽位一一对应，**落点语义沿用
#: 该文件 :40-66 的既有定义，不自创**：
#: - style_suffix     → 【风格】区（风格维度 / 文风档案 / 叙事档案）之后；
#: - constraint_note  → 【续写纪律】之后；
#: - directive_suffix → 【续写指令】之后。
#: 标题显式标注「作者助理微调 / 非参考语料规则」：覆写来自助理 agent 的自动
#: 微调，不是参考作者的行为规则——不能让模型把覆写误读成【文风档案】那种
#: 从参考语料实测出来的规则。
_OVERRIDE_STYLE_HEADER = "风格补充（作者助理微调，非参考语料规则）"
_OVERRIDE_CONSTRAINT_HEADER = "续写纪律补充（作者助理微调）"
_OVERRIDE_DIRECTIVE_HEADER = "续写指令补充（作者助理微调）"


def _override_section(header: str, text: str | None) -> str:
    """渲染一条提示词覆写块；空文本返回 `""`（拼接结果逐字节不变）。

    `resolve_prompt_block()` 未读到覆写时返回空串，此时本函数必须不产生
    任何字符——这是「老项目零影响」的唯一兼容底线。纯空白同样按未设置处理，
    避免库里误存空白行时拼出只有标题的空块。
    """
    body = (text or "").strip()
    if not body:
        return ""
    return f"【{header}】\n{body}\n"


def _inline_block(text: str | None) -> str:
    """渲染一段自带标题的约束块；空文本返回 `""`（逐字节兼容）。"""
    body = (text or "").strip()
    return f"{body}\n\n" if body else ""


def _gate_block() -> str:
    """生成侧硬指标块：数字必须与 `consistency.GEN_GATE_LIMITS` 同源。

    为什么写死在 system 里：三章实测的句长 12–14 / CV 0.50–0.55 / 段均 11 /
    问号 15–20，都发生在「档案数字已经出现在提示里」的情况下。说明光有数字
    不够，还得把它写成**可核对的验收项**并与 `style_check` 的闸门同阈值——
    否则模型会把“长短句交错”读成修辞建议，继续按短句惯性写。

    阈值从常量读，不在这里另抄一份：改闸门就同步改提示，杜绝两套口径。
    """
    from app.style.consistency import (
        ARCHIVE_CHAPTER_PERCENTILES, GEN_GATE_LIMITS, active_dialogue_mix_reference,
        active_full_limits, active_gate_limits, active_rhythm_reference,
    )

    cp = ARCHIVE_CHAPTER_PERCENTILES

    def _p(key: str, field: str = "p50") -> float:
        """对白档位：章级分位（与闸门新增项同一张表，不另抄数字）。"""
        return float(cp[key][field])

    def _target(key: str, unit: str = "") -> str:
        limit = GEN_GATE_LIMITS.get(key) or {}
        lo, hi = limit.get("min"), limit.get("max")
        if lo is not None and hi is not None:
            return f"{lo}–{hi}{unit}"
        if lo is not None:
            return f"≥{lo}{unit}"
        return f"≤{hi}{unit}"

    short = GEN_GATE_LIMITS.get("短句占比") or {}
    long40 = GEN_GATE_LIMITS.get("长句占比") or {}
    long_mid = GEN_GATE_LIMITS.get("长句31_39占比") or {}
    # v23：对白字占比进单章硬判，下限由 engine 按参考线 p25 注入；
    # 提示必须读**当前生效**的判罚带，否则会印出常量表里的旧下限（6%）。
    dlg = (active_gate_limits().get("对白字占比")
           or GEN_GATE_LIMITS.get("对白字占比") or {})
    # 极短纯对白段上限读**当前闸门**（v10 起为参考书非零章 p90），不写死 0：
    # 常量表里该项是退回值 0，直接读它会让提示要求「必须为 0」而闸门实际允许 3.2%。
    jit = active_gate_limits().get("极短纯对白段占比") or {}
    exc = GEN_GATE_LIMITS.get("对白感叹句占比") or {}
    tan = GEN_GATE_LIMITS.get("对白叹词起头占比") or {}
    nar = GEN_GATE_LIMITS.get("叙述单句段占比") or {}
    nar_band = f"{float(nar.get('min') or 0):.0%}–{float(nar.get('max') or 0):.0%}"
    # 段均句数与并段护栏取**同一份**参考带（`active_full_limits`）：档案可推时
    # 是章级 p10–p90，否则退回块级表。提示词里不再写「一段 2–3 句」这种手拍句数。
    spc = active_full_limits().get("段均句数") or {}
    spc_lo, spc_hi = spc.get("min"), spc.get("max")
    if spc_lo is not None and spc_hi is not None:
        spc_band = f"{float(spc_lo):g}–{float(spc_hi):g}"
    elif spc_lo is not None:
        spc_band = f"≥{float(spc_lo):g}"
    elif spc_hi is not None:
        spc_band = f"≤{float(spc_hi):g}"
    else:
        spc_band = "按档案"
    # v14：短句/长句的展示区间也跟参考档案走（卷口径 p10–p90），
    # 否则「短句 6%–25%」会和配额里的「至少 12.3%」自相矛盾。
    rhythm = active_rhythm_reference() or {}
    short_r = rhythm.get("短句占比") or {}
    long_r = rhythm.get("长句占比") or {}
    if short_r.get("p10") is not None and short_r.get("p90") is not None:
        short_band = f"{float(short_r['p10']):.0%}–{float(short_r['p90']):.0%}"
    else:
        short_band = f"{short.get('min', 0):.0%}–{short.get('max', 0):.0%}"
    if long_r.get("p10") is not None and long_r.get("p90") is not None:
        long_band = f"{float(long_r['p10']):.0%}–{float(long_r['p90']):.0%}"
    else:
        long_band = f"{long40.get('min', 0):.0%}–{long40.get('max', 0):.0%}"
    jit_max = float(jit.get("max") or 0.0)
    jit_rule = f"≤{jit_max:.1%}" if jit_max > 0 else "0 段（当前闸门上限为 0）"
    # 对白现在只判下限（≥22%），没有上限带；不能再拼成「22%–0%」。
    dlg_lo, dlg_hi = dlg.get("min"), dlg.get("max")
    if dlg_lo is not None and dlg_hi is not None:
        dlg_band = f"{dlg_lo:.0%}–{dlg_hi:.0%}"
    elif dlg_lo is not None:
        dlg_band = f"≥{dlg_lo:.0%}"
    else:
        dlg_band = f"≤{dlg_hi:.0%}"
    # v14：对白句类配比不再手写「5/2/2/2」，改为参考书**章级**分位折算到每 10 句。
    # 指标：对白陈述句占比 p50、对白问句占比 p25/p90、对白感叹句占比首个非零分位、
    # 对白叹词起头占比首个非零分位；无活动参考时退成不带数字的定性句（不写死）。
    mix = active_dialogue_mix_reference() or {}
    if mix:
        n_stmt = max(0, round(10 * float(mix.get("陈述p50") or 0)))
        n_q_lo = int(10 * float(mix.get("问句p25") or 0))
        n_q_hi = max(n_q_lo + 1, round(10 * float(mix.get("问句p90") or 0)))
        n_exc = max(1, int(-(-(10 * float(mix.get("感叹p25") or 0)) // 1)))
        n_tan = max(1, int(-(-(10 * float(mix.get("叹词p50") or 0)) // 1)))
        mix_line = (
            f"对白按每 10 句配比（参考书章级分位折算）：陈述约 {n_stmt} 句、"
            f"问句 {n_q_lo}–{n_q_hi} 句、感叹 ≥{n_exc} 句、叹词起头 ≥{n_tan} 句，"
            f"连续追问 ≤{n_q_hi} 轮；不要靠删对白来降问号。"
        )
    else:
        mix_line = (
            "对白按陈述为主、问句与感叹穿插来配比，连续追问不要成串；"
            "不要靠删对白来降问号。"
        )
    return (
        "【文风硬指标（与参考档案同一统计口径，写完必须自查）】\n"
        f"- 句长平均 {_target('句长均值', ' 字')}；句长 CV {_target('句长CV')}："
        "长短必须拉开，不能整段都是中短句。\n"
        f"- ≤4 字短句占 {short_band}（参考卷口径）："
        "**≤4 字的对白不得单独成段**：要么与说话人的"
        "神态/动作同段（“丫头，走吧。”老周把烟摁灭），要么补一句旁人反应；"
        f"叙述单句段是参考书的正常结构（{nar_band}），不要为凑段均把它并掉。\n"
        f"- 长句：≥40 字句 {long_band}（参考卷口径）；"
        f"31–39 字句占 {long_mid.get('min', 0):.0%}–{long_mid.get('max', 0):.0%}。"
        "长句要展开成分句递进，不是加逗号凑长度；长短必须在同一段内交替。\n"
        f"- 段均 {_target('段长均值', ' 字')}；段均句数 {spc_band} 句，"
        "不要把一句话拆成一段，也不要把多句硬并成一段。\n"
        f"- 对白字占比 {dlg_band}（**低于下限即判不合格，会触发重写**）；"
        f"问号 {_target('问号每千字', ' 个/千字')}。"
        f"{mix_line}\n"
        "【对白校准（2026-11-13 档案实测，与闸门同一把尺；碎与平都要治）】\n"
        f"- 不碎：极短纯对白段（整段只有引号、且 ≤4 字）{jit_rule}；"
        f"消息/对白段字数中位 ≥{_p('消息段字数中位', 'p10'):g} 字（目标 "
        f"{_p('消息段字数中位'):g} 字）——每条消息都要落出「谁说的、什么神态、"
        "旁人怎么反应」，禁止把 3–10 字的消息一条一段排成竖列。\n"
        "  纯对白段/纯消息段的总占比只做软档参考（一条 40 字的消息独立成段不算碎），"
        "不设硬上限；但任何短句独立成段都必须带动作、神态或旁人反应。\n"
        "  群聊/消息场景：两三条消息之间必须有一次群体反应、旁人动作或人物评价；"
        "消息本身要带称呼、反问、追问或语气词，不能只有干巴巴的事实。\n"
        "  电话/语音场景与群聊同一把尺：不得一条一句排成竖列，两三条消息之间要有"
        "停顿、动作、环境声或对方的反应；通话要写出一来一回的打断、听错、抢话，"
        "而不是把对白抄成记录稿。\n"
        f"- 不平：对白语气按档案章级配比——陈述约 {_p('对白陈述句占比'):.0%}、"
        f"问句 {_p('对白问句占比', 'p25'):.0%}–{_p('对白问句占比', 'p90'):.0%}"
        f"（中位 {_p('对白问句占比'):.0%}）、感叹句 ≥"
        f"{float(exc.get('min') or 0):.1%}（低于即判不合格）、叹词起头（哎/嘿/哟/"
        f"诶/嗯）≥{float(tan.get('min') or 0):.1%}（低于即判不合格）。"
        "同一场对话连续 3 条平铺陈述算不合格，改成反问、追问、感叹或叹词起头。\n"
        f"  语气词/句末助词（吧 吗 啊 呢 嘛 哦 呀 哈 啦 呗）密度 ≥"
        f"{_p('语气词密度千字', 'p25'):g}/千字（目标 {_p('语气词密度千字'):g}）；"
        f"含助词对白句 ≥{_p('含助词对白句占比', 'p25'):.1%}"
        f"（目标 {_p('含助词对白句占比'):.1%}）——助词是人物标签，不是点缀。\n"
        f"- 长对白：≥20 字对白句 ≥{_p('对白≥20占比', 'p10'):.1%}"
        f"（目标 {_p('对白≥20占比'):.1%}）；短问短答可以有，但不能整场只有短句。\n"
        "以上是可事后统计核对的数字，不是修辞建议；未达标会被判不合格并重写。\n\n"
    )


def _length_block(max_chars: int | None = None) -> str:
    """字数硬上限块（v5）：必须出现在首遍 system，不能只靠参数。

    口径与闸门同源：`consistency.char_count` 的字符数（含标点与换行），超过
    目标 ×1.2 判违规。数字从 `length_limits()` 取，不在这里另抄一份。
    """
    from app.style.consistency import LENGTH_LIMIT_RATIO, length_limits

    lim = length_limits(max_chars)
    return (
        "【字数硬上限（写完必须自查）】\n"
        f"- 正文**总字数**（含标点与换行）上限 {lim['max']} 字，"
        f"目标 {lim['target']} 字；超过 {lim['max']} 字直接判不合格并触发重写。\n"
        f"- 超出时只许压缩、合并、删冗句，不许靠加场景、加人物、加支线来凑长度。\n\n"
    )


def _chapter_quota_block(max_chars: int | None, min_chars: int | None = None) -> str:
    """user 侧可执行配额块：目标字数、极短纯对白段、消息段中位。

    为什么放在 user 而不是只放 system：system 的硬指标块离「请续写……」最近的一句
    仍然是风格口径，且与叙事档案同块竞争注意力。user 尾部是模型最后读到的位置，
    这里复述的必须是**可数的绝对量**，否则模型仍可以用「删字」而不是「并段」
    把段均比值做达标。

    **不再给段数上限**（v13）：旧口径的 `max(档案段数下限, 汉字数÷21)` 既不在闸门
    判、也没被执行（第 29 章按它该 ≤184 段，实际 324 段）。段长/段数一律只记录，
    由参考线（句长均值、对白配比）引导收敛，不设硬目标。

    **不再给绝对句数下限**（v15）：v14 的「≤5 字短句至少 N 句 / ≥40 字长句至少
    M 句」同样没被执行（第 31 章实到约 17 / 8 句，闸门也不判）。短句/长句只保留
    `_gate_block` 里参考书 p10–p90 的百分比区间，不再折算成绝对句数。

    所有数字来源：
    - 目标汉字数与上限：`length_limits(max_chars)`；
    - 极短纯对白段上限：`active_gate_limits()`（档案现推，与单章闸门同一份）；
    - 消息/对白段字数中位：`ARCHIVE_CHAPTER_PERCENTILES["消息段字数中位"]["p10"]`。
    """
    from app.style.consistency import (
        ARCHIVE_CHAPTER_PERCENTILES,
        active_gate_limits,
        length_limits,
    )

    lim = length_limits(max_chars)
    target = int(lim["target"])
    gate = active_gate_limits()
    # 极短纯对白段上限读当前闸门现推值（v10 起为参考书非零章 p90），不许写死 0。
    jit_max = float((gate.get("极短纯对白段占比") or {}).get("max") or 0.0)
    jit_rule = f"≤{jit_max:.1%}" if jit_max > 0 else "0 段（当前闸门上限为 0）"
    msg_med = float(
        ARCHIVE_CHAPTER_PERCENTILES["消息段字数中位"]["p10"]
    )
    lines = [
        "【本章配额（可执行；写完按段自查，超了就是不合格）】",
        (
            f"- 目标 {target} 个汉字；初稿合格带 {int(min_chars)}–{lim['max']} 个汉字"
            "（按正文汉字数统计，写完可自查）。"
            if isinstance(min_chars, int) and min_chars > 0
            else f"- 目标 {target} 个汉字。"
        ),
        f"- **极短纯对白段（整段只有引号、且 ≤4 字）{jit_rule}**："
        "这类短句一律并入相邻段，或补上说话人的神态/动作/旁人反应。",
        "- 短句**不限于对白**：动作、拟声、停顿、单字应答构成的叙述短句同样计入"
        "文风硬指标里的短句区间，不必靠单独成段的短对白凑数。",
        f"- 消息/对白段字数中位 ≥{msg_med:g} 字：每条消息都要写出"
        "「谁说的、什么神态、旁人怎么反应」，不得一条一句排成竖列。",
        f"- 若正文汉字数已超过 {lim['max']} 硬上限，则以压缩为第一目标，"
        "允许删短句、删对白、删可割舍情节（篇幅要求，与段均无关）。",
    ]
    # v15：**删掉「≤5 字短句至少 N 句 / ≥40 字长句至少 M 句」**（照删「段数上限
    # 189」的同一原则）。第 31 章实测短句约 17 句、长句约 8 句，离 v14 折算的
    # 30 / 20 差得远，说明这条绝对句数既没被执行、闸门也不判——写着不执行的指令
    # 只会制造「已经在管」的假象。短句/长句改为**只给 `_gate_block` 的 p10–p90
    # 参考区间**（同一次修订、同一份 `rhythm_reference`），不再折算绝对句数。
    return "\n".join(lines)


def _dialogue_reference_block() -> str:
    """对白配比参考 + **硬约束**（v23）。

    数字来源：`active_dialogue_reference()`——engine 从参考档案的章级分位现推
    p25/p50/p75，并附带本书最近一卷的实测值；未写入时退回
    `ARCHIVE_BLOCK_PERCENTILES["对白字占比"]`（此时不写本书实测）。

    v23 起不再是「只给方向」：**章级 p25**（`hard_floor`）同时是
    `generation_gate` 的单章下限，低于它即判不合格并触发重写。提示必须把
    这件事说透，否则模型仍会按「对白可写可不写」的惯性，把第 1 章写成
    7.8% 的叙述体。
    """
    from app.style.consistency import (
        ARCHIVE_BLOCK_PERCENTILES, active_dialogue_reference,
    )

    ref = active_dialogue_reference() or {}
    static = ARCHIVE_BLOCK_PERCENTILES["对白字占比"]
    p25 = float(ref.get("p25", static["p25"]))
    p50 = float(ref.get("p50", static["p50"]))
    p75 = float(ref.get("p75", static["p75"]))
    floor = float(ref.get("hard_floor", p25))
    window = int(ref.get("window") or 0)
    scale = f"卷口径（连续 {window} 章块）" if window else "章级"
    origin = (
        "由参考档案现推，换参考书自动重算"
        if ref.get("source") else "静态参考值（未取到当前参考书分位）"
    )
    lines = [
        "【对白配比硬约束（低于下限即判不合格）】",
        f"- 参考书{scale}「对白字占比」：p25 {p25:.1%} / p50 {p50:.1%} / "
        f"p75 {p75:.1%}（{origin}）。",
        f"- 本章**硬下限 {floor:.1%}**（参考书章级 p25）：低于它会被判不合格并重写。"
        "场景里该说话的地方必须让人物开口，用一来一回的对话推进；"
        "不要用「他想了想」「事情并不简单」这类叙述代替人物台词。",
    ]
    recent = ref.get("book_recent_ratio")
    n = int(ref.get("book_recent_chapters") or 0)
    if isinstance(recent, (int, float)) and n:
        recent = float(recent)
        diff = recent - p50
        gap = f"{abs(diff) * 100:.1f} 个百分点"
        if recent < floor:
            lines.append(
                f"- 本书最近 {n} 章实测 {recent:.1%}，**低于硬下限 {floor:.1%}**"
                f"（差 {(floor - recent) * 100:.1f} 个百分点）。本章必须把对白拉起来："
                "优先写人物之间的成段对话（每个场景至少一轮完整的你来我往），"
                "把纯叙述的信息改成由角色说出来。"
            )
        else:
            trend = "明显高于" if diff > 0 else ("低于" if diff < 0 else "接近")
            lines.append(
                f"- 本书最近 {n} 章实测 {recent:.1%}，{trend}参考 p50 "
                f"{p50:.1%}（{gap}）。"
            )
            if diff > 0:
                lines.append(
                    "- 处理方式：不要再抬高对白字占比。同样的信息量优先写成"
                    "「动作 + 神态 + 旁人反应」的叙述，或把对白压短后补叙述；"
                    f"但仍不得低于硬下限 {floor:.1%}。"
                )
    return "\n".join(lines)


def _sentence_reference_block() -> str:
    """句长参考线（v13，**非硬判罚**）。

    为什么是「句长均值」而不是「段长均值」：把段长缺口拆成
    `段长均值 = 段均句数 × 句长均值` 后，句长偏离参考带最远，且是最近一次
    下滑的主因（卷 2 → 第 29 章约 57%）。段长本身仍只记录、不设目标——
    给它定参考带等于重新引入段落结构目标。

    数字来源：`active_sentence_reference()`——engine 从参考档案的**卷口径**
    （连续 `volume_chapters` 章块）分位现推 p10/p50/p90，并附带本书最近
    14 章实测值；未写入时退回 `ARCHIVE_BLOCK_PERCENTILES["句长均值"]`
    （此时不写本书实测）。措辞里不出现「必须」「不合格」。
    """
    from app.style.consistency import (
        ARCHIVE_BLOCK_PERCENTILES, active_sentence_reference,
    )

    ref = active_sentence_reference() or {}
    static = ARCHIVE_BLOCK_PERCENTILES["句长均值"]
    p10 = float(ref.get("p10", static["p10"]))
    p50 = float(ref.get("p50", static["p50"]))
    p90 = float(ref.get("p90", static["p90"]))
    window = int(ref.get("window") or 0)
    scale = f"卷口径（连续 {window} 章块）" if window else "章级"
    origin = (
        "由参考档案现推，换参考书自动重算"
        if ref.get("source") else "静态参考值（未取到当前参考书分位）"
    )
    lines = [
        "【句长参考线（非硬判罚，只做校准）】",
        f"- 参考书{scale}「句长均值」：p10 {p10:.1f} / p50 {p50:.1f} / "
        f"p90 {p90:.1f} 字（{origin}）。",
    ]
    recent = ref.get("book_recent_mean")
    n = int(ref.get("book_recent_chapters") or 0)
    if isinstance(recent, (int, float)) and n:
        recent = float(recent)
        diff = recent - p50
        trend = "明显短于" if diff < 0 else ("长于" if diff > 0 else "接近")
        lines.append(
            f"- 本书最近 {n} 章实测 {recent:.1f} 字，{trend}参考 p50 "
            f"{p50:.1f} 字（差 {abs(diff):.1f} 字）。"
        )
        if diff < 0:
            lines.append(
                "- 处理方式：把同一场景里的连续动作、神态与叙述并成一句，"
                "不要一句一断；长句用逗号分层，而不是拆成多个短句。"
                "此项不判违规，只看是否向参考带收敛。"
            )
    return "\n".join(lines)


#: 首段角色卡遮罩：这些措辞一出现，等于把后续底牌（幕后主使 / 阴谋 / 身份反转）
#: 提前摊开。首段只保留姓名与不剧透的身份，命中即降级为「不展开」。
_OPENING_SPOILER_MARKERS = (
    "幕后", "主使", "主谋", "黑手", "真凶", "凶手", "内鬼", "卧底",
    "阴谋", "推动", "计划", "其实是", "原来是", "身份是", "底牌",
)
_OPENING_SPOILER_MASK = "（涉及后续揭底，本节不展开）"

#: premise 里一旦出现这些词，说明已经讲到结局 / 底牌；首段只保留它之前的高概念。
_OPENING_PREMISE_STOPWORDS = (
    "真相", "阴谋", "结局", "最终", "幕后", "计划", "反转", "复仇",
)


def _is_opening_spoiler(text: str | None) -> bool:
    value = str(text or "")
    return any(marker in value for marker in _OPENING_SPOILER_MARKERS)


def _opening_premise(premise: str) -> str:
    """首段只留高概念：截到第一个「讲结局 / 抖底牌」的句子之前。"""
    sentences = [s for s in re.split(r"(?<=[。！？!?])", premise) if s.strip()]
    kept: list[str] = []
    for sentence in sentences:
        if any(word in sentence for word in _OPENING_PREMISE_STOPWORDS):
            break
        kept.append(sentence)
    return "".join(kept).strip() or sentences[0].strip()


def _as_text_list(value) -> list[str]:
    """把 voice 里的字符串/数组统一成非空文本列表。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _voice_clauses(voice) -> list[str]:
    """把 characters[].voice 压成角色行里的几个短句。"""
    if not isinstance(voice, dict):
        return []
    bits: list[str] = []
    self_ref = str(voice.get("self_ref") or "").strip()
    if self_ref:
        bits.append("自称：" + self_ref)
    address = voice.get("address")
    if isinstance(address, dict):
        pairs = [
            f"{k}→{v}" for k, v in address.items()
            if str(k).strip() and str(v).strip()
        ][:4]
        if pairs:
            bits.append("称呼：" + "、".join(pairs))
    tics = _as_text_list(voice.get("tics"))[:3]
    if tics:
        bits.append("口头禅：" + "、".join(f"“{x}”" for x in tics))
    for key, label in (("sentence", "句长/节奏"), ("under_pressure", "急时")):
        value = str(voice.get(key) or "").strip()
        if value:
            bits.append(f"{label}：{value}")
    taboo = _as_text_list(voice.get("taboo"))[:3]
    if taboo:
        bits.append("禁说：" + "、".join(taboo))
    return bits


def _card_gender(card: dict) -> str:
    """角色卡性别（交给 kg.bible 的读侧口径，避免两套归一表漂移）。"""
    from app.kg.bible import character_gender

    return character_gender(card)


def _card_pronoun(card: dict) -> str:
    from app.kg.bible import character_pronoun

    return character_pronoun(card)


def _card_first_chapter(card: dict) -> int | None:
    from app.kg.bible import character_first_chapter

    return character_first_chapter(card)


def _character_available(
    card: dict,
    *,
    chapter: int | None,
    chapter_context: str | None,
    appeared_names: set[str] | None,
) -> bool:
    """判定「本章是否允许该角色出场」（修复项 4 的可执行口径）。

    优先级（前者覆盖后者）：
    1. 角色卡写了 first_chapter / 首次出场章节 → 章号 <= 当前章才可用；
    2. 第一章开篇 → 允许（没有前文可依据，不能把开篇角色全滤掉）；
    3. 当前章大纲 / 当前剧情点明确提到名字 → 允许（本章就要写他）；
    4. 名字已在前序已定稿章节出现 → 允许（连续性）；
    5. 其余 → 本章不启用。

    这样 system 层的「在册角色」与节点层的「不要引入未提及角色」不再打架：
    system 只列**本章可用**角色，未列出的角色本章本来就不该出场。
    """
    first = _card_first_chapter(card)
    if first is not None:
        return chapter is None or first <= chapter
    if chapter is None:
        return True
    if chapter <= 1:
        return True
    name = str(card.get("name") or "").strip()
    if not name:
        return False
    if chapter_context and name in chapter_context:
        return True
    if appeared_names and name in appeared_names:
        return True
    return False


def filter_available_characters(
    chars: list,
    *,
    chapter: int | None,
    chapter_context: str | None,
    appeared_names: set[str] | None,
) -> tuple[list[dict], int]:
    """按 `_character_available` 过滤角色卡，返回 `(本章可用角色, 被挡下的人数)`。

    system 角色表与 user 尾部 constraints 名单都必须调用本函数——否则会出现
    「system 说未列出的不要出场，尾部锁定名单又把全量名字列了一遍」的自相矛盾。
    """
    if chapter is None:
        return [c for c in chars if isinstance(c, dict) and c.get("name")], 0
    eligible: list[dict] = []
    withheld = 0
    for card in chars:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        if _character_available(
            card,
            chapter=chapter,
            chapter_context=chapter_context,
            appeared_names=appeared_names,
        ):
            eligible.append(card)
        else:
            withheld += 1
    return eligible, withheld


def chapter_outline_entry(outline, chapter: int | None) -> str | None:
    """取「本章」在大纲里的那一条（1-based）；取不到返回 None。

    兼容三种 outline 形态：list/tuple、换行分隔字符串、其它（不解析）。
    时间线模式下用它把本章大纲补回 prompt——节点块不能替代本章大纲。
    """
    if not outline or not chapter:
        return None
    entries: list[str]
    if isinstance(outline, (list, tuple)):
        entries = [str(x).strip() for x in outline]
    elif isinstance(outline, str):
        entries = [line.strip() for line in outline.splitlines() if line.strip()]
    else:
        return None
    if not (1 <= chapter <= len(entries)):
        return None
    entry = entries[chapter - 1]
    return entry or None


def _char_line(card: dict, *, opening: bool = False) -> str:
    """一个角色一行。只渲染 bible 键契约里**登记为支持**的字段（B2）。

    字段顺序固定：role → gender → motivation → background → voice → sample_lines。
    性别渲染成「性别：女（用「她」指代）」——只写「性别：女」模型仍可能用「他」，
    代词是正文层可直接执行的信号。没写性别时输出与旧版逐字节相同。

    `opening=True` 时遮蔽会提前揭底的角色卡：`role` 写成「幕后人物」、
    `motivation` 写「推动 X 计划」的，首段降级为「相关人物 / 不展开」，
    只保留姓名——否则开篇第一句就把全书底牌交给模型。
    """
    role = str(card.get("role") or "角色")
    motivation = card.get("motivation")
    background = (
        card.get("background") or card.get("身世")
        or card.get("来历") or card.get("背景")
    )
    if opening:
        if _is_opening_spoiler(role):
            role = "相关人物"
        if motivation and _is_opening_spoiler(motivation):
            motivation = _OPENING_SPOILER_MASK
        if background and _is_opening_spoiler(background):
            background = _OPENING_SPOILER_MASK
    bits = [role]
    gender = _card_gender(card)
    if gender:
        pronoun = _card_pronoun(card)
        bits.append(f"性别：{gender}" + (f"（用「{pronoun}」指代）" if pronoun else ""))
    if motivation:
        bits.append("动机：" + str(motivation))
    if background:
        bits.append("背景：" + str(background))
    bits.extend(_voice_clauses(card.get("voice")))
    samples = _as_text_list(card.get("sample_lines"))[:3]
    if samples:
        bits.append("台词参考：" + " / ".join(f"“{x}”" for x in samples))
    # 角色卡（身份/能力/声纹/关系 分组结构）是 persona 之外的结构化补充。
    # 老数据没有 card 或 card 是 persona 形状时 render_card 返回空串，
    # 输出与旧版逐字节相同；有卡时才追加一条压缩的人话。
    grouped = card.get("card") if isinstance(card.get("card"), dict) else card
    if isinstance(grouped, dict):
        from app.generation.character_card import render_card

        # 性别已在上面的「性别：女（用她指代）」里写过，卡里不再重复一遍。
        detail = render_card(grouped, skip_fields=("性别",) if gender else ())
        if detail:
            bits.append(detail)
    return f"  · {card.get('name')}（{'；'.join(bits)}）"


def build_system_prompt(
    project_title: str,
    reminders: dict,
    style_dims: dict | None = None,
    plot_bible: dict | None = None,
    style_profile: dict | None = None,
    style_narrative: dict | None = None,
    *,
    style_suffix: str | None = None,
    constraint_note: str | None = None,
    era_constraint: str | None = None,
    power_constraint: str | None = None,
    style_source_ids: set[int] | list[int] | None = None,
    is_opening: bool = False,
    narrative_mode: str = "original",
    narrative_timeline: dict | None = None,
    catalog_skeleton: str | None = None,
    max_chars: int | None = None,
    chapter: int | None = None,
    chapter_context: str | None = None,
    appeared_names: set[str] | None = None,
) -> str:
    """静态系统提示——整轮生成保持不变，置于最前，保证提示词缓存命中率。

    角色与伏笔 = plot_bible（用户构思）+ reminders（生成后图谱抽取）**合并**，
    不再二选一——见下方「合并而非遮蔽」注释。合并口径见 merged_constraint_items。

    `style_profile` 由 `app.style.pipeline.project_profile` 产出（按指纹缓存），
    渲染成静态块；自由生成（未选参考语料）时为 None，该块整段不出现。
    `style_narrative` 由 `app.style.narrative.project_narrative` 产出（按指纹缓存），
    是 LLM 提炼的 L2/L3 叙事规则，同样为 None 时不出现。

    `style_suffix` / `constraint_note` 是提示词运行时覆写（见
    `app.agents.prompt_overrides.resolve_prompt_block`），由 engine 侧读库后
    传入——本函数保持纯函数、不接 db。为空（未设置覆写）时输出与旧版**逐字节
    相同**；非空时分别拼在【风格】区之后与【续写纪律】之后，块标题标明来源
    是「作者助理微调」，防止模型误以为是参考作者的规则。

    `era_constraint` / `power_constraint` 由 engine 从项目设定算好后传入
    （时代越界约束 / 量级与体系约束）。两者都为空时输出与旧版**逐字节相同**。

    `is_opening`（首段）：角色卡遮蔽提前揭底的底牌，「叙事档案」降权压缩趋势。
    非首段时逐字节回到旧行为。

    `chapter` / `chapter_context` / `appeared_names`（修复项 4）：按章过滤角色表。
    传了 `chapter` 时，system 只列**本章可用角色**（first_chapter <= 当前章、
    本章大纲/剧情点提到、或前序正文已出现），未列出的在册角色本章不启用；
    这三个参数都为 None 时输出与旧版逐字节相同。
    """
    # —— 合并而非遮蔽 ——
    # 旧实现「有 plot_bible 就只取 bible」会让生成后抽取的事实（KG reminders）被静默丢弃：
    # 用户构思完圣经后，后续段落里新出现的角色 / 新埋的伏笔永远进不了约束，等于构思完就失忆。
    # 现改为：bible（用户构思，权威在前）+ reminders（生成后抽取，补充新增）合并去重。
    chars, fores = merged_constraint_items(reminders, plot_bible)

    roster_header = (
        "【角色人设约束】续写时必须保持以下角色一致，不得凭空新增主要人物，"
        "不得扭曲已确立的关系：\n"
    )
    withheld_note = ""
    if chapter is None:
        char_lines = (
            "\n".join(
                _char_line(c, opening=is_opening)
                for c in chars
                if isinstance(c, dict) and c.get("name")
            )
            or "  · 暂无锁定角色（自由发挥，但保持已写人物一致）"
        )
    else:
        # 本章可用角色：把「不要引入未提及角色」的上游判据前移到 system，
        # 这样 system 不再要求模型对全量在册角色负责，节点层也就没有冲突。
        eligible, withheld = filter_available_characters(
            chars,
            chapter=chapter,
            chapter_context=chapter_context,
            appeared_names=appeared_names,
        )
        char_lines = (
            "\n".join(_char_line(c, opening=is_opening) for c in eligible)
            or "  · 本章暂无确定出场角色（按本章大纲推进，不要从在册名单外抓人）"
        )
        roster_header = (
            "【角色人设约束（本章可用角色）】续写时必须保持以下角色一致，"
            "不得凭空新增主要人物，不得扭曲已确立的关系；"
            "**未列出的在册角色本章不要出场**——本章需要的新角色必须由本章大纲或"
            "当前剧情点明确写出，不能以「他也在册」为理由提前带入：\n"
        )
        if withheld:
            withheld_note = (
                f"\n  · 另有 {withheld} 名在册角色尚未到首次出场章节，本章不要启用；"
                "他们会在后续章节按大纲出场。"
            )
    if withheld_note:
        char_lines = char_lines + withheld_note

    if is_opening:
        # 首段不列伏笔清单：三条伏笔就是全书后手，首段给全等于把底牌摊开。
        hole_lines = "  · （首段不列伏笔清单；伏笔从第 2 节起注入，本节不要自行揭底）"
    else:
        hole_lines = (
            "\n".join(f"  · {f}" for f in fores if f)
            or "  · 暂无伏笔"
        )

    gate_block = (
        _gate_block()
        if style_narrative and narrative_mode == "original"
        else ""
    )

    return (
        f"你是一位长篇小说续写助手。当前创作项目：《{project_title}》。\n\n"
        f"{roster_header}"
        f"{char_lines}\n\n"
        "【未填伏笔（供你知情，不必本轮回收）】以下是已埋下、暂不打烊的悬念，"
        "**本节最多推进一条，其余留待后续**；不要在本节集中兑现多个伏笔，"
        "也不要用台词直接说出伏笔本身：\n"
        f"{hole_lines}\n\n"
        f"{_inline_block(era_constraint)}"
        f"{_inline_block(power_constraint)}"
        f"【风格】{_style_block(style_dims)}\n"
        "（以上维度是倾向参考，不要直接翻译成类型套路或标签化文风。）\n"
        f"{_profile_block(style_profile)}\n"
        f"{_narrative_block(style_narrative, style_source_ids, opening=is_opening, mode=narrative_mode, timeline=narrative_timeline)}"
        f"{_character_profile_block(style_narrative, profile=narrative_mode, character_names=sorted(appeared_names) if appeared_names else None)}"
        f"{_override_section(_OVERRIDE_STYLE_HEADER, style_suffix)}"
        "【文风要求：真人小说感，忌 AI 腔】\n"
        "- 禁止金句化台词：人物不许说出格言式总结或口号（如「不被遗忘本身就是最贵的代价」）；"
        "台词要像真人说话，可有停顿、改口、答非所问、言外之意。\n"
        "- 禁止段尾升华：不要在段落结尾习惯性地总结、拔高、抒情。\n"
        "- 禁止情绪直陈：不用「眼神里露出悲凉」「瞳孔骤缩」这类贴标签式的情绪描写，"
        "改用动作、姿态、语气、沉默来承载情绪。\n"
        "- 对话不承担设定讲解：人物不互相解释世界观或概念，读者靠情节自行理解。\n"
        "- 具体优于形容：少堆形容词，多写可感知的具体物件、动作、气味声响。\n"
        "- 允许留白：信息不必解释完，场景不必交代清，段落可以以一个动作或物件收住。\n"
        "- 省略号节制：避免在叙事中习惯性地用「……」「...」拖腔；\n"
        "用「，」「。」「——」承担语气停顿，不要让省略号成为情感拐杖。\n"
        "- 避免堆叠四字格：连续使用四字成语/对仗句会显得机械、像 AI 生成的「金句段」；\n"
        "人物对话和叙事以自然口语/长短句为主，四字格用一处即止。\n"
        "- 贴近真实节奏：长短句交错，不要每段都同样长度、不要句句工整。\n\n"
        f"{_length_block(max_chars)}"
        f"{gate_block}"
        f"{_catalog_skeleton_block(catalog_skeleton) if narrative_mode == 'original' else ''}"
        "【续写纪律】只输出正文；保持与上一段的连贯；不要重复已写过的内容；"
        "每一轮都是「连载的一节」，不是一篇独立完整的短篇小说——"
        "结尾必须停在某个仍未解决的冲突、悬念或动作中途（为下一节留钩子），"
        "不得在本次续写里收束主线、给出结局，也不要写「从此……／自此……」式的收尾；"
        "每轮**不少于**本节要求的字数：未写够不要写【本节完】，"
        "也不要用总结句、抒情句或「从此……」式收尾提前结束。"
        f"{_override_section(_OVERRIDE_CONSTRAINT_HEADER, constraint_note)}"
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
    *,
    directive_suffix: str | None = None,
    instruction_suffix: str | None = None,
    node_prompt: str | None = None,
    kg_slice: str | None = None,
    item_holding_block: str | None = None,
    rewrite_before_chars: int | None = None,
    chapter_outline: str | None = None,
    min_chars: int | None = None,
    advance_block: str | None = None,
    story_cards_block: str | None = None,
    foreshadow_block: str | None = None,
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

    `directive_suffix` 是【续写指令】之后的运行时覆写（见
    `app.agents.prompt_overrides`）；为空（未设置覆写）时输出与旧版逐字节相同。

    `node_prompt`（时间线，见 timeline.py）：传了它就走「按剧情点分段投喂」——
    **不注入整条【大纲】，也不注入 plot_bible 的 synopsis/direction**（那会把结局走向
    一次给全，导致第一章就讲完）。改注入当前剧情点的写作目标。
    没传则走原逻辑（向后兼容，旧项目/无时间线项目不受影响）。

    `rewrite_before_chars`（2026-11-12）：重写已有段落时传**原文长度**。此时
    【续写指令】整段替换为重写专用篇幅要求，不再输出「请续写不少于 N 字」——
    后者是续写预算，语义是「写够一整段」，与重写「在原文篇幅内改」冲突。

    篇幅要求写成**祈使句**而非区间（2026-11-12 二次修正）：实测区间版
    「80%–120%，即 A–B 字」把续写下限从 2400 拉低到 1818，模型两次都取
    区间外的低值（34.4% / 26.8%）——给出区间等于给了「选低的」余地。
    现在只给一条单向硬要求「不得短于原文 N 字」，上限用一句话兜住，
    不再出现任何数字区间。None 时逐字节回到原行为。

    `min_chars`（2026-11-15）：初稿字数**下限**，由 engine 按
    `_archive_rhythm()["completion_min_ratio"]`（与补完触发线同源）算出后传入。
    不传（None）时退回旧的 `max_chars × 0.8`。只用于初稿/开篇的续写指令；
    传了 `rewrite_before_chars` 的重写路径不使用它。
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
    #
    # 2026-11-15：下限改由 engine 传（与补完触发线同源），不再在本文件写死 0.8。
    # 旧口径 0.8×4000=3200 低于触发线 3304，模型按提示写到 3200 仍被判偏短——
    # 提示允许的字数必须落在「补完触发线 ~ 硬上限」这条合格带内。
    floor_chars = (
        int(min_chars)
        if isinstance(min_chars, int) and min_chars > 0
        else int(max_chars * 0.8)
    )
    from app.style.consistency import length_limits as _length_limits
    hard_max = int(_length_limits(max_chars)["max"])

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
    # 保留理由（向后兼容）：非时间线模式仍注入 plot_bible，但**首段只给
    # conflicts**，synopsis/direction/relationships/foreshadowing 从第 2 节起
    # 才注入（见下方 is_opening 分支）。
    # —— 首段 vs 续写，指令不同 ——
    # 首段：模型没有被「前情」约束，极易把整篇弧光一次性写完（用户实测第一段就完结）。
    # 必须正向要求「只铺陈设悬、不许收尾」，而不是只说「不要完结」。
    # 续写段：明确要求结尾停在未解决处，为下节留钩子，避免每段都自洽收束。
    is_opening = not prior_blocks and not prior_summaries
    bible_lines: list[str] = []
    if plot_bible and not node_prompt:
        if is_opening:
            # 首段裁剪（2026-11-12）：synopsis / direction / relationships 全表 /
            # foreshadowing 三条都在描述「整条弧光」，一次给全，模型会在第一节
            # 把故事演完再收口（p5 实测段 1 只有 750 字）。首段只给本节能切入的
            # 冲突——它们是并列张力，不含结局；其余一律延后到第 2 节。
            conflicts = plot_bible.get("conflicts") or []
            if conflicts:
                bible_lines.append(
                    "本节可切入的冲突（只展开其一，不要在本节解决）："
                    + "；".join(str(c) for c in conflicts)
                )
            bible_lines.append(
                "（整篇梗概、后续走向、人物关系全表与伏笔清单从第 2 节起再给；"
                "本节不要自行推进到结局。）"
            )
        else:
            synopsis = plot_bible.get("synopsis")
            direction = plot_bible.get("direction")
            conflicts = plot_bible.get("conflicts") or []
            relationships = plot_bible.get("relationships") or []
            fores = [] if foreshadow_block else (plot_bible.get("foreshadowing") or [])
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
                bible_lines.append(
                    "持续伏笔（本节最多推进一条，其余留待后续）："
                    + "；".join(str(f) for f in fores)
                )
    # 作者侧台账（只含未回收项）：与旧 plot_bible.foreshadowing 二选一。
    # 首段与时间线节点模式同样不给，避免模型把整条线一次演完。
    if foreshadow_block and not is_opening and not node_prompt:
        bible_lines.append(foreshadow_block)
    if bible_lines:
        bible_block = "\n".join(bible_lines)
    elif is_opening and plot_bible and not node_prompt:
        bible_block = "（首段：只写本节目标，整篇走向留待后续）"
    else:
        bible_block = "（暂无剧情圣经，凭前情与风格续写）"

    # —— 首段 vs 续写，指令不同 ——
    # 首段：模型没有被「前情」约束，极易把整篇弧光一次性写完（用户实测第一段就完结）。
    # 必须正向要求「只铺陈设悬、不许收尾」，而不是只说「不要完结」。
    # 续写段：明确要求结尾停在未解决处，为下节留钩子，避免每段都自洽收束。
    if rewrite_before_chars:
        # 重写（替换已有段落）≠ 续写新段落。这里**替换**而不是追加字数要求：
        # 提示词原本只有续写预算「不少于 max_chars*0.8（建议 max_chars）」，
        # 对重写是错的——续写语义是「写够一整段」，且默认 max_chars=3000 的
        # 建议值 3000 已超过原文 2273 的 120%（2727）。两条预算同时存在必然
        # 互相打架，故只保留这一条。
        #
        # 放在【续写指令】——现有字数预算本来就在这里，同区替换才只有一个
        # 权威来源；硬约束（物品归属/已发生事实）在 system 提示的纪律区，
        # 篇幅约束属于「这一节写多长」，与它们不冲突，也不该挤进事实区。
        #
        # 用单向祈使句、**不写区间**：区间版实测被模型取下界外侧（34.4%/26.8%），
        # 「A–B 字」这种可选空间反而成了压缩的许可。上限只留一句话，不给数字。
        directive = (
            f"这是**重写已有段落**，不是续写新段落：原文共 {rewrite_before_chars} 字。"
            f"重写后的正文**不得短于原文 {rewrite_before_chars} 字**；如需增删，"
            "只允许在这一段内小幅调整，不得把整段压缩成梗概或摘要，"
            "也不得明显超出原文篇幅。"
            "除非【本次重写要求】明确要求精简 / 压缩 / 扩写，否则不得大幅增删内容。"
            "只重写这一段并保持与前后文衔接；不要输出解释、标题或分点。"
            "风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——同上
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口。"
        )
    elif is_opening:
        directive = (
            f"这是整篇的**开头**。请从建立人物与情境写起，抛出一个推动故事的冲突或事件，"
            f"节奏舒展地写不少于 {floor_chars} 字（目标 {max_chars} 字）；正文汉字数超过 {hard_max} 字会按不合格处理；"
            f"**本节只负责铺陈与设悬，不要收尾、不要给出结局、"
            f"不要写『从此……』式的收束**，把更大的冲突留到后面展开。"
            f"风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——告诉模型在节末写 stop marker，
            # provider 会传 stop=["【本节完】", ...] 给 LLM，模型写到此 marker 时停
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口（后端会切分正文与这个 marker）。"
        )
    else:
        directive = (
            f"请续写不少于 {floor_chars} 字（目标 {max_chars} 字）；正文汉字数超过 {hard_max} 字会按不合格处理。"
            f"并在本节结尾停在某个仍未解决的冲突 / 悬念 / 动作中途，"
            f"为下一节留钩子——不要在此收束主线或给出结局。"
            f"风格与节奏以系统提示中的【风格】与【文风要求】为准。"
            # v2.6：节点粒度根治——同上
            "**节末信号**：写完本节主要内容后，请在文末单独一行写【本节完】三个字，"
            "表示本节已完整收口。"
        )

    # —— 世界背景（作者手写：这个世界是什么样）——
    # **时间线模式也注入**（与 plot_bible 的处理相反）：背景组不含走向，
    # 剔掉只会让模型每段自己现编一套（城名、规则、势力各写各的）。
    # 没写背景 ⇒ **整块不输出**（空块是负面暗示）。
    from app.generation.background import render_background

    background_text = render_background(
        plot_bible.get("background") if isinstance(plot_bible, dict) else None
    )

    blocks = [
        "【前情提要】",
        prior,
        "",
    ]
    # —— 世界背景放在「剧情圣经·动态指引」**之前** ——
    # 背景是恒定设定（每段都一样），动态指引是会变的；先恒定后变化，
    # 模型读到最后时记忆里最靠前的是背景 ⇒ 不容易随剧情漂移。
    if background_text:
        blocks += [
            "【世界背景（作者设定，全书一致，不得与之矛盾）】",
            background_text,
            "",
        ]
    # 设定卡（人物 / 场景 / 道具）：作者亲手写下的意图，与【世界背景】同区——
    # 先恒定设定、后剧情走向。空块（项目没建卡）不追加任何字符，
    # 保证与旧版提示逐字节一致。
    if story_cards_block:
        blocks += [
            "【设定卡（作者亲手维护；本段出现的人物 / 势力 / 场景 / 道具 / 规则须与之一致）】",
            story_cards_block,
            "（人物卡里的「性格」「动机」「禁忌」「说话方式」是**行为硬约束**："
            "本段人物的关键选择、动作与对白都要能追溯到这些设定，"
            "不要只推进情节、把人物写成没有性格的功能性角色。"
            "与【世界观事实】【物品持有事实】冲突时，以本块与【剧情圣经】为准；"
            "不得静默改动卡片里的设定。）",
            "",
        ]
    blocks += [
        "【剧情圣经·动态指引】",
        bible_block,
    ]
    # premise：用户给的故事大概，**时间线模式下剔除**（同 plot_bible 一样
    # 携带整条故事走向，与 node_prompt 职责重复）。非时间线模式保留作
    # "基调锚点"；首段只留高概念，讲到「真相 / 阴谋 / 结局」的句子截掉。
    if premise and not node_prompt:
        premise_text = _opening_premise(premise) if is_opening else premise
        blocks += ["", "【小说大概内容（用户给定，作为基调锚点）】", premise_text]
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
        # 批 6：说话人归属的口径（样例 + 一句显式口径要一起给）。
        # 实测光给样例教不会：3 条样例里已有 29 次「X 道：“…”」，模型照样
        # 写成「说」字派 ⇒ 口径必须按**这本书自己的样例**量出来，不能写死。
        from app.style.attribution import attribution_directive

        attr_directive = attribution_directive(
            [str(ex.get("text") or "") for ex in style_exemplars])
        if attr_directive:
            blocks += [
                "",
                "【句法样板（照着写）】",
                "下面是参考书原文的几个片段。它们示范了这个作者的**句子长度、"
                "断句位置、标点习惯**。",
                attr_directive,
                "样板只用来学写法，**不要复用其中的情节与人物**。",
            ]
    if node_prompt:
        # 时间线模式：节点块约束「写到哪」，但**不能替代本章大纲**——
        # 节点是章节的压缩包（一个点可能覆盖多章），本章的具体事件只在
        # project.outline 的那一条里。这里把本章条目单独补回来，两者同时生效：
        # 先写本章大纲列的事件，再用节点块约束推进节奏与停笔位置。
        if chapter_outline:
            blocks += [
                "",
                "【本章大纲（本章必须写到的事件；与下方当前剧情进度同时生效，"
                "本章大纲的具体事件优先，剧情进度只约束推进节奏与停笔点）】",
                chapter_outline,
            ]
        blocks += ["", "【当前剧情进度（按点推进，不要越过本点）】", node_prompt]
    elif chapter_outline:
        # v15：非时间线模式此前把**整条大纲**（全部章条目）塞进提示，等于把后续
        # 章节的标题与事件一并交给模型，越章材料正是这样来的；同时「本章」没有
        # 独立锚点，五拍写了没有无法判断。这里改为只注入本章条目。
        blocks += [
            "",
            "【本章大纲（本章要写到的事件；后续章节的条目不在本次上下文里）】",
            chapter_outline,
        ]
    elif outline_text:
        blocks += ["", "【大纲】", outline_text]
    if kg_slice:
        # v3.0 KG 切片：按当前剧情点动态取的近期实体/状态/伏笔/情报/事件。
        # 只标红不强制——是给模型的「世界观锚点」，让它别写飞。与 plot_bible 区别：
        # plot_bible 是用户构思（含走向，时间线模式剔除），kg_slice 是已生成事实。
        blocks += ["", "【世界观事实（已发生，续写须保持一致，不得矛盾）】", kg_slice]
    if item_holding_block:
        # 物品持有事实：从 kg_item_holding 读出的「现在在谁手上」。
        # 与 kg_slice 同为已发生事实，但**优先级更低**：只补物品归属，
        # 不许反过来覆盖剧情圣经 / 世界观事实里的既有 canon 约束。
        # 标题/说明/明细统一走 render_item_holding_section——重写路径复用同一份。
        blocks += ["", render_item_holding_section(item_holding_block)]
    # 逐章推进要求（v1）：属性 / 阵容 / 时间锚点，每条都对应台账里的一个
    # 可核字段（见 `progress_ledger.advance_requirements`）。放在【续写指令】
    # 之前：先约束「这一章要推进什么」，再给字数与停笔位置。空串时逐字节不变。
    if advance_block:
        blocks += ["", advance_block]
    blocks += ["", "【续写指令】", directive]
    # 可执行配额：目标字数 / 极短纯对白段上限 / 消息段中位 / 句子分布。
    # 放在 user 尾部（模型最后读到的位置），数字全部从档案分位与闸门阈值取，
    # 与 system 的硬指标块同源——system 那份是「区间」，这份是「可数的绝对量」。
    quota_block = _chapter_quota_block(
        max_chars, None if rewrite_before_chars else min_chars
    )
    if quota_block:
        blocks += ["", quota_block]
    # 对白配比参考线（v12，非判罚）：参考书章级 p25/p50/p75 + 本书最近一卷实测。
    # 单独成块，与上面的可执行配额分开——配额是判罚项，这里只给收敛方向。
    dialogue_ref = _dialogue_reference_block()
    if dialogue_ref:
        blocks += ["", dialogue_ref]
    # 句长参考线（v13，非判罚）：段长缺口拆成 `段均句数 × 句长均值` 后的主因指标，
    # 卷口径 p10/p50/p90 + 本书最近 14 章实测。段长均值本身只记录、不设目标。
    sentence_ref = _sentence_reference_block()
    if sentence_ref:
        blocks += ["", sentence_ref]
    # 用户在对话里明确给出的重写要求。放在【续写指令】之后的指令区，
    # 不追加到待改正文/物品块/constraints 里；空文本时逐字节不变。
    instruction_block = _override_section(
        "本次重写要求（用户指定；与已发生事实、物品归属冲突时以事实为准）",
        instruction_suffix,
    )
    if instruction_block:
        blocks += ["", instruction_block.rstrip("\n")]
    directive_extra = _override_section(_OVERRIDE_DIRECTIVE_HEADER, directive_suffix)
    if directive_extra:
        blocks += ["", directive_extra.rstrip("\n")]
    return "\n".join(blocks)


def _character_profile_block(
    style_narrative: dict | None,
    *,
    profile: str | None,
    character_names: list[str] | None,
) -> str:
    """按生成模式和当前角色清单注入 L2 角色档案。

    批 5 前置：`render_character_profile` 此前零调用（只有定义），这一层把它
    接到 writer 提示词上。original（原创）模式恒空——档案抽取自参考书角色，
    塞进原创提示词等于让模型照抄参考书人设。
    """
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
    return "【L2角色档案】\n" + "\n".join(f"- {block}" for block in blocks) + "\n"
