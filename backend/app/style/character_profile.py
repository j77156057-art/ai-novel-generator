"""角色档案（characters 层）蒸馏。

## 为什么单独抽

`角色声音词典` 只记「这个人怎么说话」：

    隋珠公主：自称「本宫」，语气骄横尖刻，受辱时以反问、威胁反击

但角色聊天要防「说出不符合身份的话」，光有说话方式不够。还缺三样：

    行为倾向  他遇到 X 会怎么做（决定他做什么，不只是说什么）
    性格底线  什么话他不会说、什么事他不会做
    关系网    他对每个人的称呼与态度（决定他对着谁用什么语气）
    关键经历  他现在的状态从哪来（决定他的情绪底色）

续写也需要这些：写他出场时，得知道他会先看谁、会不会先动手。

## 数据来源

不依赖参考语料图谱 —— 实测它的角色名是截断的（`徐龙` 而非 `徐龙象`，
3150 个 character 节点从未做姓名归一化）。

改用 `_build_character_name_index`（角色声音提取已在用的同一套）：
它做了语料自证的姓名归一化（`徐凤` → `徐凤年`）和伪角色清洗。

## 按角色逐个抽，不批量

一本书几十个主要角色。把「抽 30 个角色」塞进一个 prompt 会让每个角色
只剩一句废话（已实测：14 维塞一个 prompt 会截断）。所以每个角色一次调用，
用该角色出现最多的若干 chunk 作输入。
"""

from __future__ import annotations

import re
from typing import Any


#: 一个角色档案要抽的字段。顺序即渲染顺序。
CHARACTER_FIELDS: tuple[str, ...] = (
    "身份",
    "说话方式",
    "行为倾向",
    "性格底线",
    "关系网",
    "关键经历",
)

#: 关系条目要抽的七个维度。
#:
#: ## 为什么不能只有「称呼 + 态度」
#:
#: 首版每条只有「称「世子殿下」，态度敌视压抑杀意」。实测（雪中 215 段
#: 姜泥×徐凤年共现）漏掉的信息量极大：
#:
#:   原文：她堆雪人然后一匕首劈掉脑袋，徐凤年「敢情这疯丫头是把雪人当作自己了」
#:         → 她私下的发泄方式。chatter 不知道，就不会让她做这类小动作。
#:   原文：她背地里咒骂徐凤年不得好死，当面从不说
#:         → 当面/背后两套话。不知道这点，聊天会让她当面就骂。
#:   原文：徐凤年拿剑鞘拍她脸，「力道刚好，不轻不重，不足以伤人」
#:         → 他的真实分寸：看似粗暴其实克制。
#:   原文：徐凤年蹲下给她垒雪人脑袋，她觉得「惺惺作态，面目可憎」
#:         → 同一件事两人读法完全相反。这是关系的核心张力，也是最好的
#:           对白素材——但只记「敌视」两个字，这层张力就没了。
#:
#: 七个维度各自回答一个聊天时必须知道的问题：
#:
#:   称呼      我该怎么叫对方（最基础，错一句就穿帮）
#:   态度      我对他什么感觉
#:   位阶      谁压谁、我能对他做什么、他不能对我做什么
#:   来历      我们之间为什么是这样（决定提到往事时的反应）
#:   互动史    我们之间发生过什么（决定哪些旧事会被提起）
#:   边界      什么话我不能对他说、什么事我不能对他做
#:   解读差    同一件事我怎么看、他怎么看（对白张力的来源）
RELATION_FIELDS: tuple[str, ...] = (
    "称呼",
    "态度",
    "位阶",
    "来历",
    "互动史",
    "边界",
    "解读差",
)


def build_character_profile_prompt(
    name: str, excerpts: list[str], novel_title: str = "",
) -> tuple[str, str]:
    """构造单个角色的档案抽取 prompt。

    Args:
        name: 角色规范名（已归一化，如「徐凤年」）。
        excerpts: 该角色出现的原文片段（按重要性排序，总长控制在 6000 字内）。
        novel_title: 书名，仅用于 prompt 里的指代。

    Returns:
        (system, user)
    """
    body = "\n\n---\n\n".join(excerpts[:6])
    title_part = f"《{novel_title}》" if novel_title else "这本小说"

    system = (
        f"你是一位资深中文小说编辑。你要为{title_part}里的一个角色建立**写作档案**，"
        f"供另一个作者在续写这本书、或扮演这个角色对话时使用。\n\n"
        f"## 要抽的六个字段\n\n"
        f"1. **身份**：这个角色是谁（职业/地位/与主角的关系），一句话。\n"
        f"2. **说话方式**：自称（我/本座/老子/在下…）、口头禅、句长倾向"
        f"（短促/绵长/忽长忽短）、语气底色（冷淡/热络/嘲讽/恭谨）。\n"
        f"3. **行为倾向**：遇到具体情境会怎么做。**这是最重要的字段**——"
        f"要给「情境 → 反应」的对，不是形容词。\n"
        f"4. **性格底线**：什么事他不会做、什么话他不会说。"
        f"用来防止角色做出不符合身份的事。\n"
        f"5. **关系网**：他对每个重要人物的关系。**每条关系必填七个字段**：\n"
        f"   - 称呼：他怎么叫对方（原话里的称呼，有几个写几个）\n"
        f"   - 态度：敬/畏/亲/厌/恨，加程度（如「厌恶但不敢显露」）\n"
        f"   - 位阶：谁压谁。我能对他做什么、他不能对我做什么\n"
        f"   - 来历：这段关系从哪来的（恩情/血仇/旧识/主仆）\n"
        f"   - 互动史：他们之间发生过什么关键事（按时间，每条一句）\n"
        f"   - 边界：什么话他不能对对方说、什么事他不能对对方做\n"
        f"   - 解读差：同一件事我怎么看、对方怎么看（张力所在）\n"
        f"6. **关键经历**：决定他现在状态的几件事，按时间顺序，"
        f"每条一句话。\n\n"
        f"## 关键要求\n\n"
        f"- **只写这个角色**，不要写别人的档案。\n"
        f"- 行为倾向必须具体到可执行：「被晚辈顶撞时先冷笑再动手」而不是"
        f"「脾气不好」。\n"
        f"- 性格底线要给**否定式**：「不会当众示弱」「不会为钱出卖旧部」。\n"
        f"- 关系网里每个人都要写称呼（他怎么叫对方）——这是角色聊天时"
        f"最容易出错的地方。\n"
        f"- 有原文依据的才写，没依据的字段给空字符串，不要编。\n\n"
        f"## 输出格式（JSON）\n\n"
        '{\n'
        f'  "name": "{name}",\n'
        '  "身份": "...",\n'
        '  "说话方式": "自称「…」，口头禅「…」，句长…，语气…",\n'
        '  "行为倾向": ["被X时→…", "遇到Y时→…"],\n'
        '  "性格底线": ["不会…", "不会…"],\n'
        '  "关系网": {\n'
        '    "对方名": {"称呼": "…", "态度": "…", "位阶": "…",\n'
        '      "来历": "…", "互动史": ["…"], "边界": ["…"],\n'
        '      "解读差": "我看作…，对方看作…"}\n'
        '  },\n'
        '  "关键经历": ["第N章：…", "第N章：…"]\n'
        '}\n'
    )
    user = (
        f"以下是{title_part}中与「{name}」有关的片段：\n\n"
        f"{body}\n\n"
        f"请为「{name}」建立写作档案，按上述 JSON 格式输出。"
        f"**只写这一个角色。**"
    )
    system += (
        '\n证据约束优先于上面的填满要求：不得强迫否定底线或七字段齐全。'
        '每项事实附 claims 数组，每项 {subject,predicate,field,quote,source,status,kind}。'
        '每条 claim 必须包含 subject,predicate,field,quote,source；source 至少含 chapter 或可定位来源。'
        'status 只能 observed/inferred/unknown；kind 为 identity/speech/action/relationship/thought/hypothetical/metaphor。'
        'subject 必须是该 quote 中实际执行 predicate 的主体；仅被提及、被作用或主体不明时填 unknown，不得注入。'
        'value 使用 quote 中直接支持的连续原文摘录；quote 必须逐字来自对应章节输入。'
        '关系需另给 person 和 relation_field（七字段之一）。缺依据留空并标 unknown。'
        '内心设想不是说出口的话；一次台词不能推成稳定口头禅；比喻不是事实。'
        '育有孩子不能自动推夫妻；照顾女儿不能推亲生；同姓同官不能合并身份。'
        '推断保留为 inferred 提案，不写入 observed。每个 value 只含一项保守事实。'
    )
    return system, user


def normalize_character_profile(raw: dict[str, Any], name: str, *, sources: list[tuple[int, str]] | None = None) -> dict[str, Any]:
    """把模型输出规范成固定字段的字典。

    ## 为什么要规范化

    M3 偶尔会：
    - 把 `行为倾向` 写成字符串而不是列表
    - 把 `关系网` 写成列表而不是字典
    - 漏字段 / 多字段

    下游（角色聊天、续写）按固定形状读，形状不稳会让它们静默取到空值。
    这里统一成：

        {name, 身份: str, 说话方式: str, 行为倾向: list[str],
         性格底线: list[str], 关系网: dict[str,str], 关键经历: list[str]}
    """
    def _as_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            # 有的模型用「；」或换行分隔
            parts = re.split(r"[；\n]+", v)
            return [p.strip() for p in parts if p.strip()]
        return []

    def _one_relation(v: Any) -> dict[str, Any]:
        """把一条关系规范成七字段形状。

        兼容四种实测形态：
          1. 七字段 dict（新格式）
          2. 「称呼「…」，态度：…」字符串（旧格式）
          3. {"称呼": ..., "态度": ...} 部分字段 dict
          4. dict 里混着未知名（保留到 `其他` 字段，不丢信息）

        缺失字段给空值而不是省略——下游按固定键读，缺键会 KeyError
        或被静默当成空。给空字符串让渲染层自然跳过。
        """
        empty = {f: ([] if f in ("互动史", "边界") else "") for f in RELATION_FIELDS}
        if isinstance(v, dict):
            out = dict(empty)
            for f in RELATION_FIELDS:
                if f in v:
                    val = v[f]
                    if f in ("互动史", "边界"):
                        if isinstance(val, list):
                            out[f] = [str(x).strip() for x in val if str(x).strip()]
                        elif isinstance(val, str) and val.strip():
                            out[f] = [p.strip() for p in re.split(r"[；\n]+", val) if p.strip()]
                    else:
                        out[f] = str(val).strip()
            # 未知名保留，避免模型给的额外信息被丢弃
            extra = {k: str(vv).strip() for k, vv in v.items()
                     if k not in RELATION_FIELDS and str(vv).strip()}
            if extra:
                out["其他"] = "；".join(f"{k}：{x}" for k, x in extra.items())
            return out
        if isinstance(v, str):
            # 旧格式：「称呼「X」，态度：Y」或「X，Y」
            text = v.strip()
            out = dict(empty)
            m = re.search(r"称(?:\(「([^」]+)」\)|「([^」]+)」)", text)
            if m:
                out["称呼"] = (m.group(1) or m.group(2) or "").strip()
            m2 = re.search(r"态度[：:]\s*([^，。；]+)", text)
            if m2:
                out["态度"] = m2.group(1).strip()
            if not out["称呼"] and not out["态度"]:
                out["态度"] = text  # 完全无法解析时整句塞进态度，不丢
            return out
        return dict(empty)

    def _as_dict(v: Any) -> dict[str, Any]:
        """关系网 → {对方名: 七字段 dict}。"""
        if isinstance(v, dict):
            return {str(k).strip(): _one_relation(x)
                    for k, x in v.items() if str(k).strip()}
        if isinstance(v, list):
            out: dict[str, Any] = {}
            for item in v:
                if isinstance(item, dict):
                    k = item.get("对方") or item.get("name") or item.get("人物")
                    if k:
                        rest = {kk: vv for kk, vv in item.items()
                                if kk not in ("对方", "name", "人物")}
                        out[str(k).strip()] = _one_relation(rest)
                elif isinstance(item, str) and ("：" in item or ":" in item):
                    k, _, val = re.split(r"([：:])", item, maxsplit=1)
                    out[k.strip()] = _one_relation(val)
            return out
        return {}

    if sources is not None:
        # Strict production path: retain proposals, inject only located extracts.
        safe: dict[str, Any] = {"身份": "", "说话方式": "", "行为倾向": [],
                                "性格底线": [], "关系网": {}, "关键经历": []}
        evidence = []
        claims = raw.get('claims', raw.get('evidence', []))
        for item in claims if isinstance(claims, list) else []:
            if not isinstance(item, dict):
                continue
            claim = dict(item)
            quote, value = claim.get("quote"), claim.get("value")
            # Format compatibility is not semantic repair: only unwrap outer quotes.
            if isinstance(value, str) and len(value) >= 2 and (value[0], value[-1]) in (('「', '」'), ('“', '”'), ('"', '"')):
                value = value[1:-1]
                claim['normalized_value'] = value
            chapter_token = str(claim.get('chapter', ''))
            chapter_match = re.fullmatch(r'(?:第)?([0-9]+)(?:章)?', chapter_token)
            chapter = int(chapter_match.group(1)) if chapter_match else None
            source_kind = claim.get('kind')
            valid_kind = source_kind in ('identity', 'speech', 'action', 'relationship', 'thought', 'hypothetical', 'metaphor')
            matches = [(ch, text.find(quote)) for ch, text in sources
                       if isinstance(quote, str) and quote and ch == chapter and quote in text]
            field = str(claim.get("field") or "")
            quote_text = quote if isinstance(quote, str) else ''
            # A name occurring in a quote is not enough to establish that the
            # character performed the predicate.  These constructions place
            # the target in an object/recipient slot (姜泥 is being flown to,
            # rather than doing the pestering), so fail closed and retain the
            # claim for review.
            subject_not_actor = bool(
                field in ('行为倾向', '关键经历')
                and re.search(
                    rf'(?:缠着|跟着|追着|围着|对着|给|替|为|向|对)'
                    rf'\s*{re.escape(name)}', quote_text)
            )
            # `kind=speech` is model metadata, not proof.  Require a speech
            # act or quoted utterance; visual reactions such as 看/瞥/眼神
            # must never become a voice profile.
            speech_context = bool(
                re.search(r'[“"「].+?[”"」]', quote_text)
                or re.search(r'(?:说道|说着|道|问道|答道|喊道|喝道|低声道|开口)', quote_text)
            )
            non_speech_context = field == '说话方式' and not speech_context
            if not matches:
                reason = 'source_mismatch'
            elif claim.get('status') != 'observed':
                reason = 'not_observed'
            elif not valid_kind:
                reason = 'invalid_kind'
            elif subject_not_actor:
                reason = 'subject_not_actor'
            elif non_speech_context:
                reason = 'non_speech_context'
            elif name not in quote_text:
                reason = 'target_not_explicit'
            elif not isinstance(value, str) or not value or value not in quote_text:
                reason = 'value_not_extract'
            else:
                reason = 'field_support_requires_review'
            # Exact extract is deliberately narrower than semantic entailment.
            accepted = (bool(matches) and valid_kind and claim.get("status") == "observed"
                        and name in quote_text
                        and isinstance(value, str) and bool(value.strip()) and value in quote_text
                        and field in CHARACTER_FIELDS
                        and claim.get("kind") not in ("thought", "hypothetical", "metaphor")
                        and not subject_not_actor
                        and not non_speech_context
                        and not re.search(r'[？！!?]|你竟敢|指责|怒喝|传言|据说', quote_text)
                        and not re.search(r'心想|心中|想象|设想|假如|仿佛|犹如|好似|宛如', quote_text)
                        and not (field in ('行为倾向', '关键经历') and re.search(r'[“”「」？！!?]', quote_text))
                        and not (field == '身份' and re.search(r'或者|可能|也许|说', value or ''))
                        and not (field == "说话方式" and claim.get("kind") != "speech"))
            if accepted:
                if field == "关系网":
                    person, subfield = claim.get("person"), claim.get("relation_field")
                    accepted = isinstance(person, str) and person in quote_text and subfield in RELATION_FIELDS
                    if accepted:
                        rel = safe[field].setdefault(person, {})
                        if subfield in ("互动史", "边界"):
                            rel.setdefault(subfield, []).append(value)
                        else:
                            rel[subfield] = value
                elif isinstance(safe[field], list):
                    safe[field].append(value)
                else:
                    safe[field] = "；".join(filter(None, [safe[field], value]))
            claim["verification"] = "located_extract" if accepted else reason
            if matches:
                claim["source_start"] = matches[0][1]
                claim["source_end"] = matches[0][1] + len(quote_text)
            evidence.append(claim)
        out = normalize_character_profile(safe, name)
        out["evidence"] = evidence
        out["evidence_status"] = "located_extracts_not_semantic_proof"
        out["evidence_summary"] = {
            "accepted": sum(c["verification"] == "located_extract" for c in evidence),
            "source_mismatch": sum(c["verification"] == "source_mismatch" for c in evidence),
            "unverified": sum(c["verification"] != "located_extract" for c in evidence),
            "legacy_without_claims": not bool(evidence),
        }
        return out

    return {
        "evidence_status": "unverified_legacy",
        "name": str(raw.get("name") or name).strip(),
        "身份": str(raw.get("身份") or "").strip(),
        "说话方式": str(raw.get("说话方式") or "").strip(),
        "行为倾向": _as_list(raw.get("行为倾向")),
        "性格底线": _as_list(raw.get("性格底线")),
        "关系网": _as_dict(raw.get("关系网")),
        "关键经历": _as_list(raw.get("关键经历")),
    }


#: 各 depth 档渲染哪些关系字段。顺序即渲染顺序。
_RELATION_DEPTH_FIELDS: dict[str, tuple[str, ...]] = {
    "full": ("称呼", "态度", "位阶", "边界", "解读差", "来历", "互动史"),
    "brief": ("称呼", "态度", "位阶", "边界"),
    "minimal": ("称呼", "态度"),
}


def _render_relations(
    relations: dict[str, Any], *, max_people: int = 8, depth: str = "full",
) -> str:
    """把关系网渲染成提示词文本。

    ## 为什么不能压成「对X称「Y」，态度Z」一行

    首版就是这样，实测丢掉了太多东西（雪中 215 段姜泥×徐凤年共现）：

        原文：她堆雪人然后一匕首劈掉脑袋，徐凤年「敢情这疯丫头是把雪人当作自己了」
        原文：她背地里咒骂他不得好死，当面从不说
        原文：他拿剑鞘拍她脸，「力道刚好，不轻不重，不足以伤人」
        原文：他蹲下给她垒雪人脑袋，她觉得「惺惺作态，面目可憎」

    这四件事分别对应「私下的发泄方式」「当面背后两套话」「他的真实分寸」
    「同一件事两人读法相反」——都是角色聊天时最有用的东西，但一行
    「敌视压抑杀意」全装不下。

    ## 渲染顺序

    称呼 → 态度 → 位阶 → 边界 → 解读差 → 来历 → 互动史

    先给「怎么叫、什么感觉、谁能压谁、什么不能说」（聊天当场要用的），
    再给「为什么、发生过什么」（背景，长但可省）。

    没有值的字段整段跳过，不留空标签——空标签会让模型以为「这里本来就没有」。

    ## depth 三档

    七字段全展开在实测里让单角色档案从 1000 字涨到 4067 字（关系占 3296），
    比整份叙事档案（1100 字）还大三倍。**必须按用途裁**：

        "full"   七字段全展开。续写用——写这段时可能涉及任何一个角色。
        "brief"  称呼 + 态度 + 位阶 + 边界。角色聊天用——只跟一个人对话，
                 需要「怎么叫、什么感觉、谁压谁、什么不能说」，不需要来历和旧事。
        "minimal" 称呼 + 态度。给对话对象做背景参考时用（不是被扮演者本人）。

    猜错不会报错，只会让 prompt 变长或信息不足，所以调用方必须显式传。
    """
    blocks: list[str] = []
    for person, rel in list(relations.items())[:max_people]:
        if not isinstance(rel, dict):
            rel = {"态度": str(rel)}
        want = _RELATION_DEPTH_FIELDS.get(depth) or _RELATION_DEPTH_FIELDS["full"]
        seg: list[str] = []
        if "称呼" in want and rel.get("称呼"):
            # 模型给的称呼字段常自带引号（「你」「世子殿下」），
            # 再套一层就成「「你」」，实测出现过。
            c = str(rel["称呼"]).strip().strip("「」“”\"'")
            if c:
                seg.append(f"称「{c}」")
        if "态度" in want and rel.get("态度"):
            seg.append(str(rel["态度"]).strip().rstrip("。．.；;，,"))
        if "位阶" in want and rel.get("位阶"):
            # 模型给的位阶常是两三句（谁压谁 + 各自能做什么）。brief 档
            # 只要第一句——第二句往往是「我不能做什么」，与边界重复。
            p_text = str(rel["位阶"])
            if depth != "full":
                p_text = p_text.split("。")[0]
            # 去掉尾标点：段内用「，」连接，自带句号会拼成「敌仇。，不会」
            seg.append(f"位阶：{p_text.strip().rstrip('。．.；;，,')}")
        if "边界" in want and rel.get("边界"):
            b = rel["边界"]
            # 边界最多 3 条：实测模型会给 5-6 条，全给会让关系段喧宾夺主
            b = ("；".join(b[:3]) if isinstance(b, list) else str(b))
            # 模型给的边界都是「不会…」句式，前面再加「不做：」会读成
            # 「不做：不会向X倾诉」——语义重复。去掉标签，直接列。
            seg.append(b)
        if "解读差" in want and rel.get("解读差"):
            seg.append(f"分歧：{rel['解读差']}")
        if "来历" in want and rel.get("来历"):
            seg.append(f"来历：{rel['来历']}")
        if "互动史" in want and rel.get("互动史"):
            h = rel["互动史"]
            h = "；".join(h[:2]) if isinstance(h, list) else str(h)
            seg.append(f"旧事：{h}")
        if seg:
            blocks.append(f"{person}——" + "，".join(seg))
    return "／".join(blocks) if blocks else ""


#: 一个角色档案进提示词的长度上限（字）。
#:
#: 为什么要有上限：实测雪中悍刀行的角色档案渲染出来 **1117~2005 字/角色** ——
#: 8 个角色就是上万字，会把 system 提示词挤爆，而且**模型对每条要求的权重会被摊平**
#: （重点 = 没重点）。
#:
#: 上限怎么定的：单角色（角色聊天）给 400 字；写正文路径最多 3 个角色 ⇒ 1200 字。
PROFILE_MAX_CHARS = 400

#: 单条字段（行为倾向/底线/经历）的内容上限（字）。
ITEM_MAX_CHARS = 46


def render_character_profile(
    entry: dict[str, Any], *, include_relations: bool = True,
    relation_depth: str = "full", max_chars: int = PROFILE_MAX_CHARS,
    with_evidence: bool = True,
) -> str:
    """把一条角色档案渲染成提示词文本。

    用于续写（全字段）和角色聊天（可能只要部分）。

    刻意用**短标签而不是长句**：实测长句式的角色描述会被模型当成
    「角色设定文档」照抄进正文，短标签式更容易被当成「背景知识」。
    """
    def _trim(t: str) -> str:
        """去掉字段自带的句末标点，避免与 join 的分隔符叠成「。。」。

        实测（M3 输出）：身份字段常自带句号「…后才归府。」，
        拼成「…归府。）。说话：…」时出现「。）。」这种怪形状。
        """
        return str(t).strip().rstrip("。．.；;，,")

    # Stored legacy fields remain available to reviewers, never to generation.
    # A warning next to an unverified claim still supplies that claim to the model.
    if entry.get("evidence_status", "unverified_legacy") != "located_extracts_not_semantic_proof":
        return ""
    if not any(entry.get(field) for field in CHARACTER_FIELDS):
        return ""

    def _clip_item(x: Any) -> str:
        s = _trim(str(x))
        return s if len(s) <= ITEM_MAX_CHARS else s[:ITEM_MAX_CHARS] + "…"

    parts: list[str] = []
    name = entry.get("name") or "未知角色"
    ident = _trim(entry.get("身份") or "")
    parts.append(f"{name}{'（' + ident + '）' if ident else ''}")
    parts.append('以下为局部原文摘录，非完整人设；单句台词不代表稳定口头禅，局部行为不代表永久底线')

    if entry.get("说话方式"):
        parts.append(f"说话：{_clip_item(entry['说话方式'])}")
    # 证据原文插在「说话」之后、「行为」之前 ——
    # 原文是「句子怎么写」的样本（模仿一个「说话」块里的人会抄话），
    # 放在后面会被长度上限截掉（实测只剩 401 字，原文根本轮不到）。
    if with_evidence:
        quotes = [
            q for q in (entry.get("evidence_quotes") or [])
            if isinstance(q, dict) and str(q.get("quote") or "").strip()
        ]
        for q in quotes[:2]:
            txt = _trim(str(q.get("quote") or ""))
            if len(txt) > 80:
                txt = txt[:80] + "…"
            ch = q.get("chapter_no")
            parts.append(f"原文（第{ch}章）：{txt}" if ch else f"原文：{txt}")
    if entry.get("行为倾向"):
        parts.append("行为：" + "；".join(_clip_item(x) for x in entry["行为倾向"][:5]))
    if entry.get("性格底线"):
        parts.append("底线：" + "；".join(_clip_item(x) for x in entry["性格底线"][:4]))
    if include_relations and entry.get("关系网"):
        parts.append("关系：" + _render_relations(
            entry["关系网"], depth=relation_depth))
    if entry.get("关键经历"):
        parts.append("经历：" + "；".join(_clip_item(x) for x in entry["关键经历"][:5]))

    out = "。".join(parts)
    if max_chars and len(out) > max_chars:
        out = out[:max_chars].rstrip("；;，, ") + "…"
    return out
