"""与角色对话：提问 → 用他的身份回答。

## 这是什么

「第三板块」的第二条路（`roleplay`）。用户选了一个书里的角色，问他问题，
他**以自己的身份**回答 —— 依据是他真出场/真说过的原文，外加他明确知道的事。

产物是**对话记录**，不进 `generations`（那两条路才是写正文）。

## 为什么它以前答不上来

真跑（MiniMax-M3.1-Flash + 雪中悍刀行）里，老黄被问
「你是看着世子长大的，他小时候什么样子？谁带他骑马的？」，答得像模像样：

    回世子，小时候他是胖墩儿一个，白白胖净，嘴边常年挂着糖渣……
    骑马是我带的。先让他趴在马背上，后来牵着他走……

但**检索到的 6 段原文里没有任何一条是这个内容** —— 他在编。

四个根因（都已在别处修掉）：

1. **没人能指定扮演谁** —— `_roleplay_character_name` 只查 plot_bible，
   而界面上的角色来自**图谱抽取** ⇒ 取不到人 ⇒ 检索压根没跑（`rag_items` 0 条）。
2. **写法样例恒空** —— `ctx_profile` 按 `character_filtered` 过滤，
   而那个标记**上游从没设过**。
3. **叙事档案没裁剪** —— `PROFILE_RULES["roleplay"]["narrative_dims"] = ()`
   是一条**死规则**（全仓没人读它）⇒ 13 维手法全量注入。
4. **声音词典没按角色筛** —— 几个角色的腔调一起给 ⇒ 演串味。

## 这里的关键设计：来源标注

检索到的是「**他出现过**的片段」，不是「他**知道**的事」。
别人当着他面说破的秘密，他照样会被喂到。

本模块给每条证据标一个来源：

- **他亲历** —— 他自己开过口（说话人是他），或这件事被明确记在他名下（KG 已知情报）；
- **他在场听到** —— 别人在说，他在旁边。

标注之后由**模型自己判断**能不能当作记忆说出口（用户在两条路里选了这一条，
而不是「直接过滤掉在场听到的」）—— 这样能演出「这事我是听说的」这种层次感。

## 模型走哪条通道

用 **Assistant** 的 provider，不是 Writer 的 —— 这是对话不是正文生成，
而且两者的 provider 单例必须互不干扰（`get_assistant_provider`）。
"""
from __future__ import annotations

import time

#: 角色人设约束（来自图谱抽取）里的角色条目 → 渲染成「你是谁」。
def _character_profile_block(style_narrative: dict | None, character: str) -> str:
    """这个角色的档案（关系网/性格），取自叙事档案的 characters 层。"""
    entries = ((style_narrative or {}).get("characters") or [])
    if not entries or not character:
        return ""
    try:
        from app.generation.scene_contract import select_l2_profiles
        from app.style.character_profile import render_character_profile
    except Exception:  # noqa: BLE001 —— 拿不到档案不该阻断对话
        return ""
    try:
        sel = select_l2_profiles(
            entries, active_names=[], roleplay_name=character, max_profiles=1,
        )
        if not sel:
            return ""
        return render_character_profile(sel[0], relation_depth="brief")
    except Exception:  # noqa: BLE001
        return ""


def _voice_block(style_narrative: dict | None, character: str) -> str:
    """这个角色**自己**的说话方式（声音词典里只取他这一条）。"""
    if not style_narrative:
        return ""
    try:
        from app.style.narrative import describe_narrative
        body = describe_narrative(
            style_narrative, include_voice=True, dims=(), voice_character=character,
        )
    except Exception:  # noqa: BLE001
        return ""
    if not body:
        return ""
    # 只留声音那一段（手法维度这里本来就被 dims=() 关掉了）
    for line in body.splitlines():
        if "人物说话方式" in line:
            return line.strip()
    return ""


SYSTEM_TEMPLATE = """你现在扮演《{book_title}》里的{character}。

{profile_block}
{voice_block}
【你说话的方式】
{voice}
【你知道的和你只是听到的】
下面每一段都标了来源：
- 标「他亲历」的，可以当成你自己的经历说；
- 标「他在场听到」的，只能说成「我听人提起过」，不能当成自己的经历。

【规矩】
1. **只说你知道的事。** 你不知道的，直说不知道 —— 不要编。
   编出来的细节比「不知道」更像谎话。
2. 你是一个**在说话的人**，不是一个在转述的旁白：不要写「他说」「她」，
   不要描写场景和动作，不要第三人称转述。
3. 回答要短，像真的在回话：可以有停顿、改口、答非所问、言外之意。
4. 不要解释这本书的设定，不要总结自己的性格，不要复述问题。
5. 你是{character}，不是别的角色 —— 别人说的话不是你的话。"""


def build_system(
    project,
    character: str,
    *,
    book_title: str = "",
    style_narrative: dict | None = None,
    timeline_block: str = "",
) -> str:
    """角色扮演的 system 提示（稳定部分 ⇒ 进 system，不击穿缓存）。"""
    profile = _character_profile_block(style_narrative, character)
    voice = _voice_block(style_narrative, character)
    title = book_title or getattr(project, "title", "") or "参考书"
    return SYSTEM_TEMPLATE.format(
        book_title=title,
        character=character,
        profile_block=("【你是谁】\n" + profile) if profile else "",
        voice_block=("\n" + timeline_block) if timeline_block else "",
        voice=voice or "（没有这个角色的说话方式参考 —— 用你自己的日常口吻说话。）",
    )


def build_user(question: str, evidence_block: str) -> str:
    """证据 + 问题（每轮都变 ⇒ 进 user 的近因位置）。"""
    parts = []
    if evidence_block:
        parts.append(evidence_block)
    parts.append("【对方问你】\n" + (question or "").strip())
    return "\n\n".join(parts)


#: 没配模型时给用户看的话（不是角色的回答，是环境状态）。
OFFLINE_NOTICE = (
    "（本机还没有配置模型，角色暂时开不了口。\n"
    "在 `backend/.env` 里填好 `LLM_PROVIDER` 与对应的 key 之后再来问 —— "
    "他会按你给的原文回答，并标出每段依据的来源。）"
)


def finalize_answer(provider, raw: str) -> str:
    """把 provider 的原始输出变成「角色的一句回答」。

    没配模型时 provider 是 `LocalFallbackLLMProvider`，它回的是一句
    「【离线兜底·确定性续写】……续写正文占位」—— 那是对**正文生成**说的话，
    放进角色对话等于让角色念了一段旁白。空白机（没填 key）上第一眼看到的就是它，
    所以这里换成一句实话。

    判据用 provider 的**类型**，不靠匹配输出文本 —— 换模型/换兜底实现都不会失配。
    """
    from app.generation.engine import _strip_thinking

    answer = _strip_thinking(raw or "").strip()
    from app.llm.provider import LocalFallbackLLMProvider

    if isinstance(provider, LocalFallbackLLMProvider):
        return OFFLINE_NOTICE
    return answer


def run_chat(
    db,
    project,
    character: str,
    question: str,
    *,
    novel_ids: list[int] | None = None,
    provider=None,
    k: int = 6,
) -> dict:
    """问一句，答一句。返回 {answer, sources[], character}。

    不写 `generations` —— 产物是对话记录，由调用方（路由）落 `roleplay_messages`。
    """
    from app.roleplay.evidence import gather_evidence, render_evidence_block

    name = (character or "").strip()
    if not name:
        raise ValueError("必须指定扮演哪个角色")
    if not (question or "").strip():
        raise ValueError("问题不能为空")

    if novel_ids is None:
        novel_ids = list(getattr(project, "reference_novel_ids", None) or [])
    novel_ids = list(novel_ids)

    sources = gather_evidence(db, project, novel_ids, name, k=k)
    evidence_block = render_evidence_block(sources)

    # —— 他经历过的事（机械层，零模型调用）——
    # 档案只覆盖前 N 个主要角色 ⇒ 用户选的角��常常没有 L2 档案 ⇒ 这里兜底。
    timeline_block = ""
    try:
        from app.roleplay.bio import character_timeline, render_timeline

        _nids = list(novel_ids or [])
        _tl: list[dict] = []
        for _nid in _nids:
            _tl = character_timeline(db, _nid, name)
            if _tl:
                break
        timeline_block = render_timeline(_tl)
    except Exception:  # noqa: BLE001 —— 取不到时间线不该阻断对话
        timeline_block = ""

    style_narrative = getattr(project, "style_narrative", None) or {}
    # 角色档案的证据回填：老档案没有 claim 账本 ⇒ 整个档案渲染成空串 ⇒
    # 模型拿不到「他经历过什么」。回填走字段级事实核验（零模型调用）。
    if style_narrative.get("characters"):
        try:
            from app.generation.engine import _backfill_character_evidence

            style_narrative = _backfill_character_evidence(db, project, style_narrative)
        except Exception:  # noqa: BLE001 —— 回填失败不该阻断对话
            pass
    system = build_system(
        project, name, style_narrative=style_narrative,
        timeline_block=timeline_block,
    )
    user = build_user(question, evidence_block)

    if provider is None:
        # 用**写手**通道,不用助理通道 ——
        # 理由:角色对话是**小说对白**,要的是这本书的文风与腔调,
        # 写手模型才是为这件事配的(且它带候选模型回退);
        # 助理模型是给「问答/诊断/提卡」用的工具调用模型。
        from app.llm.provider import get_llm_provider
        provider = get_llm_provider()

    started = time.time()
    raw = provider.chat(system, user)
    answer = finalize_answer(provider, raw)

    return {
        "character": name,
        "question": question,
        "answer": answer,
        "sources": [s.to_dict() for s in sources],
        "elapsed": round(time.time() - started, 1),
        "has_evidence": bool(sources),
    }
