"""角色卡的**生成**：单角色完善 / **成批新建**（互相避让）。

## 核心问题：「生成出来的几个人说话方式差不多」

用户原话（2026-10-29）：

> 「生成的不要千篇一律，得有各自的性格，不能出现几个人说话方式差不多的情况」

**实测证明这不是幻觉，而且提示词说「要各不相同」根本不管用**：

| 项目 | 有没有 persona | 角色数 | 风格向量平均距离 | 最近一对 |
|---|---|---|---|---|
| 5 | **有**（用户手写 + 反推） | 5 | 0.230 | **0.037** |
| 1 | 无 | 8 | 0.399 | 0.138 |

五个角色的平均台词长度全挤在 7.0~8.6 字（顾星河 7.5 / 伊芙琳 7.0 /
陆沉舟 8.3 / 白烬 7.8 / 守烛人 8.6）。⇒ 模型默认就写「短句 + 我」，
**光在档案里写「沉稳寡言」只会让五个人更像**。

## 所以本模块做三件事

1. **只产出可执行锚点**，不产出形容词：自称 / 句长档位 / 句末习惯 / 口头禅 /
   禁用词 / 称呼方式。形容词没法比对，锚点能。
2. **成批生成时把已生成的声纹喂给模型**，明确要求「与下面每一个都至少差一项」。
   一次调用生成整批，比逐个生成更能拉开（模型在同一段上下文里能直接对比）。
3. **生成完做冲突检测**（`voice_collisions`），撞了就把冲突信息贴回去让它重写，
   最多重试 2 轮。这一层是**兜底**：模型没听招呼时，代码还能兜住。

## 不做什么

- 不替用户拍板：生成的卡**不落库**，由用户在界面上点采纳（与 persona 同一条纪律）。
- 不编正文里没有的事实：新建角色本来就无正文，允许想象；
  但从正文**反推**（`derive_card`）时，武器/能力必须能在正文里查到依据。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.generation.character_card import (
    ABILITY_FIELDS,
    ENDING_STYLES,
    GENERIC_SELF_REFERENCES,
    IDENTITY_FIELDS,
    LENGTH_BANDS,
    VOICE_FIELDS,
    normalize_card,
    strict_voice_collisions,
)
from app.generation.model_json import safe_json as _safe_json

#: 一批卡最多调用几次模型：1 次生成 + 1 次按反馈重写。
#: 实测再多轮也没用（模型对「自称重复/套话」不敏感），只会把建卡拖成 5 轮。
MAX_ATTEMPTS = 2

#: 模型仍会冒出来的万金油口头禅（提示词已禁，生成后兜底删掉）。
_CLICHE_PHRASES = (
    "加油", "一切都会好起来", "会好起来的", "没事的", "别担心",
    "你可以的", "相信自己", "相信你", "嗯", "呵呵",
)

#: 「禁用词」里不该出现的规则句特征（模型爱写「说自己是主角」这种）。
_RULE_LIKE = ("从不", "不喊", "不会说", "说自己是", "不说")

#: 只补禁用词时用的系统提示：聚焦任务比混在整卡里服从度高得多。
_FORBIDDEN_SYSTEM = """你在给一组中文小说角色各写 2 个「禁用词」。

禁用词 = 这个角色**永远不会**说的词或固定句式，会原样进写作提示词。

## 硬要求

1. 直接写词本身（例：「亲爱的」「你说得对」，只是格式示例，不要照抄）。
2. **不要**写成规则说明（反例：「从不说爱字」「不喊口号」「说自己是主角」）。
3. 每个角色 2 个词，**不能与同批其他角色重复**。
4. 只输出 JSON，不要解释：
{"禁用词": {"角色名": ["词1", "词2"]}}
"""

#: 语料参考块的字数上限：只给「味道」，不给整份档案（档案动辄上万条规则）。
_STYLE_REF_CHARS = 2400

#: 语气底色的「贴标签」形容词：命中且没有行为线索 ⇒ 判定不合格、要求重写。
_TONE_LABELS = (
    "随和", "温和", "冷漠", "开朗", "温柔", "善良", "乐观", "悲观", "谨慎",
    "沉稳", "寡言", "活泼", "内向", "外向", "强势", "软弱", "天真", "腹黑",
    "热情", "冷酷", "高傲", "傲慢", "神秘", "古怪", "爱打听", "喜欢开玩笑",
    "有点迷糊", "暴躁", "急躁", "平和", "疑惑", "干练", "高效", "官僚",
    "威胁", "贪婪",
)

#: 行为线索：出现任意一个就不按「贴标签」处理。
_TONE_CUES = (
    "说", "问", "答", "先", "再", "不", "把", "越", "压", "盯", "停",
    "改口", "重复", "反问", "停顿", "含糊", "句", "字", "称呼", "叫",
    "抢", "低声", "高声", "确认", "语速", "动作", "眼神",
)


def tone_problem(tone: object) -> str:
    """语气底色是不是「贴标签」：命中形容词又没有行为线索就要求重写。"""
    t = str(tone or "").strip()
    if not t:
        return "语气底色为空"
    labels = [w for w in _TONE_LABELS if w in t]
    has_cue = any(w in t for w in _TONE_CUES)
    if labels and not has_cue:
        return f"只是形容词（{'、'.join(labels[:2])}），要写可观察的行为倾向"
    if len(t) <= 3 and not has_cue:
        return "太短，看不出行为习惯"
    return ""


def _fill_forbidden_words(provider: Any, cards: dict[str, dict],
                          story: dict) -> dict[str, list[str]]:
    """同批禁用词撞车被清空后，用一次**聚焦小调用**补回每人 2 个。

    混在整卡生成里模型只会把示例照抄六份；单独让它「只写禁用词」时
    服从度明显更高（实测）。失败不影响主流程，卡片保持「无禁用词」。
    """
    names = [n for n, c in cards.items()
             if isinstance(c, dict) and isinstance(c.get("声纹"), dict)]
    if not names:
        return {}
    lines = []
    for n in names:
        card = cards[n]
        voice = card.get("声纹") or {}
        ident = card.get("身份") or {}
        bits = [n, str(ident.get("身份职业") or ""), str(voice.get("语气底色") or "")]
        lines.append("- " + "｜".join(x for x in bits if x))
    user = (
        "## 项目\n"
        + json.dumps({"标题": story.get("title") or "",
                      "类型": story.get("genre") or ""}, ensure_ascii=False)
        + "\n\n## 角色（名字｜职业｜语气底色）\n" + "\n".join(lines)
        + f"\n\n给这 {len(names)} 个角色各写 2 个互不重复的禁用词，按上述 JSON 输出。"
    )
    try:
        raw = provider.chat(_FORBIDDEN_SYSTEM, user)
    except Exception:  # noqa: BLE001 —— 补写失败不影响主流程
        return {}
    table = (_safe_json(raw) or {}).get("禁用词")
    if not isinstance(table, dict):
        return {}

    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for n in names:
        kept: list[str] = []
        for w in table.get(n) or []:
            s = str(w or "").strip()
            if not s or s in seen or len(s) > 12:
                continue
            if any(k in s for k in _RULE_LIKE):
                continue
            seen.add(s)
            kept.append(s)
            if len(kept) >= 2:
                break
        if kept:
            out[n] = kept
    return out


#: 只有标点/占位符的口头禅（模型偷懒时写「…」），直接丢掉。
_PLACEHOLDER_TICS = frozenset({"…", "...", "......", "——", "—", "?", "？", "!", "！"})

#: 只重写语气底色时用的系统提示。
_TONE_SYSTEM = """你在给一组中文小说角色重写「语气底色」。

要求：写**可观察的行为倾向**（例：压着说话 / 先反问再回答 / 越急越短），
**不要**写形容词（开朗、冷漠、温柔、干练、热情、神秘…）。
只输出 JSON，不要解释：
{"语气底色": {"角色名": "..."}}
"""


def _is_placeholder_tic(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if s in _PLACEHOLDER_TICS:
        return True
    return not re.sub(r"[\s\W_]+", "", s)  # 去掉标点/空白后为空 ⇒ 占位符


def _fill_tones(provider: Any, cards: dict[str, dict], names: list[str],
                story: dict) -> dict[str, str]:
    """聚焦小调用：只重写「贴标签」的语气底色。失败不影响主流程。"""
    lines = []
    for n in names:
        card = cards[n]
        voice = card.get("声纹") or {}
        ident = card.get("身份") or {}
        bits = [n, str(ident.get("身份职业") or ""),
                str(voice.get("口头禅") or "")]
        lines.append("- " + "｜".join(x for x in bits if x))
    user = (
        "## 项目\n"
        + json.dumps({"标题": story.get("title") or "",
                      "类型": story.get("genre") or ""}, ensure_ascii=False)
        + "\n\n## 角色（名字｜职业｜口头禅）\n" + "\n".join(lines)
        + f"\n\n给这 {len(names)} 个角色各写一句行为化的语气底色，按上述 JSON 输出。"
    )
    try:
        raw = provider.chat(_TONE_SYSTEM, user)
    except Exception:  # noqa: BLE001
        return {}
    table = (_safe_json(raw) or {}).get("语气底色")
    if not isinstance(table, dict):
        return {}
    out: dict[str, str] = {}
    for n in names:
        tone = str(table.get(n) or "").strip()
        if tone and len(tone) <= 60 and not tone_problem(tone):
            out[n] = tone
    return out


def _only_forbidden(entry: dict) -> bool:
    """冲突条目是不是只由「禁用词整套一样」构成（这类不触发重写）。"""
    return bool(entry.get("相同")) and all(
        "禁用词" in str(x) for x in entry["相同"])


def finalize_cards(cards: dict[str, dict], *, provider: Any = None,
                   story: dict | None = None) -> dict:
    """角色卡收尾处理（批量生成与单卡补全共用）。

    1. 过滤不合法禁用词：规则句（「不说爱字」）和过长整句；
    2. 同批「禁用词」整套雷同 → 清空；有 provider 时用聚焦小调用补回；
    3. 套话口头禅、跨卡重复口头禅、非通用自称重复。

    返回清理明细（`cards` 原地修改），供调用方拼回执。
    """
    filtered: list[dict] = []
    groups: dict[tuple[str, ...], list[str]] = {}
    for n, c in cards.items():
        voice = c.get("声纹") if isinstance(c.get("声纹"), dict) else None
        if voice is None:
            continue
        words: list[str] = []
        tics = [str(t) for t in (voice.get("口头禅") or []) if str(t).strip()]
        for w in voice.get("禁用词") or []:
            s = str(w or "").strip()
            if not s:
                continue
            if len(s) > 12 or any(k in s for k in _RULE_LIKE):
                filtered.append({"人物": n, "禁用词": s})
                continue
            # 口头禅里已经会说的话不可能是禁用词（如「还钱」两边都写）。
            if any(s in t for t in tics):
                filtered.append({"人物": n, "禁用词": s,
                                 "原因": "与口头禅冲突"})
                continue
            words.append(s)
        if words:
            voice["禁用词"] = sorted(set(words))
        else:
            voice.pop("禁用词", None)
        if words:
            groups.setdefault(tuple(sorted(set(words))), []).append(n)

    cleaned: list[str] = []
    for words, names in groups.items():
        if len(names) > 1:
            for n in names:
                cards[n]["声纹"].pop("禁用词", None)
            cleaned.extend(names)

    removed_tics, deduped_tics, self_fallback = _clean_voice(cards)

    filled: dict[str, list[str]] = {}
    if cleaned and provider is not None and story is not None:
        filled = _fill_forbidden_words(provider, cards, story)
        for n, words in filled.items():
            voice = cards.get(n, {}).get("声纹")
            if isinstance(voice, dict):
                voice["禁用词"] = words

    # 语气底色仍贴标签的，再用一次聚焦小调用重写（失败就保留原值）。
    bad_tones = [n for n, c in cards.items()
                 if tone_problem((c.get("声纹") or {}).get("语气底色"))]
    tone_rewrites: dict[str, str] = {}
    if bad_tones and provider is not None and story is not None:
        tone_rewrites = _fill_tones(provider, cards, bad_tones, story)
        for n, tone in tone_rewrites.items():
            voice = cards.get(n, {}).get("声纹")
            if isinstance(voice, dict):
                voice["语气底色"] = tone

    return {
        "cards": cards,
        "已清理禁用词": sorted(set(cleaned)),
        "已过滤禁用词": filtered,
        "补写禁用词": filled,
        "语气重写": tone_rewrites,
        "已清理口头禅": removed_tics,
        "已去重口头禅": deduped_tics,
        "自称回退": self_fallback,
    }


def _clean_voice(cards: dict[str, dict]) -> tuple[list[dict], list[dict], list[str]]:
    """收尾清理：套话口头禅 / 跨卡重复口头禅 / 非通用自称重复。

    这三件事模型重写也改不掉（实测 4 轮不变），只能在产出后确定性收口：
    - 套话直接删（「加油」「一切都会好起来的」这类）；
    - 同一句口头禅只保留先出现的那条；
    - 自称撞车的后来者回退成「我」。

    返回 `(删掉的套话, 跨卡重复删掉的, 自称回退的角色)`，便于回执/排查。
    """
    removed: list[dict] = []
    deduped: list[dict] = []
    fallback: list[str] = []
    seen_self: set[str] = set()
    seen_tics: set[str] = set()

    for name, card in cards.items():
        voice = card.get("声纹")
        if not isinstance(voice, dict):
            continue
        self_ref = str(voice.get("自称") or "").strip()
        if self_ref and self_ref not in GENERIC_SELF_REFERENCES:
            if self_ref in seen_self:
                voice["自称"] = "我"
                fallback.append(name)
            else:
                seen_self.add(self_ref)

        tics = voice.get("口头禅")
        if isinstance(tics, list):
            kept: list[str] = []
            for tic in tics:
                s = str(tic or "").strip()
                if _is_placeholder_tic(s):
                    removed.append({"人物": name, "口头禅": s or "空"})
                    continue
                if any(c in s for c in _CLICHE_PHRASES):
                    removed.append({"人物": name, "口头禅": s})
                    continue
                if s in seen_tics:
                    deduped.append({"人物": name, "口头禅": s})
                    continue
                seen_tics.add(s)
                kept.append(s)
            if kept:
                voice["口头禅"] = kept
            else:
                voice.pop("口头禅", None)
    return removed, deduped, fallback


def _rule_texts(items: Any, limit: int, width: int = 220) -> list[str]:
    """从档案条目里取 `rule` 文本，去重后截断。"""
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        raw = item.get("rule") if isinstance(item, dict) else item
        text = str(raw or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        if len(text) > width:
            text = text[:width].rstrip() + "…"
        out.append(text)
        if len(out) >= limit:
            break
    return out


def style_reference(profile: dict | None, *, max_chars: int = _STYLE_REF_CHARS) -> str:
    """把蒸馏档案里「人怎么说话」的部分压成一段参考语料。

    只取与角色卡直接相关的三样：

    - **对白风格**：语料里对白的节奏、称谓、停顿习惯（带原句证据）；
    - **角色声音词典**：语料里真实角色的自称/称谓/句长；
    - **禁用表达**：作者从不用的 AI 腔词，正好当「禁用词」的反面清单。

    用户反馈（2026-11-19）：「AI 自己造的卡太 AI 感了，性格、口头禅都一股 AI 味」。
    根因是生成时只看得见项目简介，看不见参考书里人**实际怎么说话**。
    档案为空（没选参考语料 / 没蒸馏）时返回空串，调用方按原逻辑生成。
    """
    if not isinstance(profile, dict) or not profile:
        return ""
    try:
        from app.style.layers import method_rules, normalize_layers
        from app.style.narrative import CHARACTER_VOICE_DIMENSION
    except Exception:  # noqa: BLE001 —— 拿不到档案不该阻断建卡
        return ""

    try:
        method = method_rules(profile)
    except Exception:  # noqa: BLE001
        return ""

    dialogue = _rule_texts(method.get("对白风格"), 3)
    voice = _rule_texts(method.get(CHARACTER_VOICE_DIMENSION), 4)

    forbidden_map = {}
    try:
        forbidden_map = normalize_layers(profile).get("forbidden") or {}
    except Exception:  # noqa: BLE001
        forbidden_map = profile.get("forbidden") or {}
    forbidden_lines: list[str] = []
    for group, items in list(forbidden_map.items())[:3]:
        # 禁用词多是「他知道，」这种带尾标点的片段，拼成清单前先收干净。
        words = [w.rstrip("，,、。 ") for w in _rule_texts(items, 4, width=24)]
        words = [w for w in words if w]
        if words:
            forbidden_lines.append(f"- {group}：{'、'.join(words)}")

    parts: list[str] = []
    if dialogue:
        parts.append("【对白风格】\n" + "\n".join(f"- {x}" for x in dialogue))
    if voice:
        parts.append(
            "【语料里的人物声音（借自称/称谓/句长的习惯，不要照搬人名与门派）】\n"
            + "\n".join(f"- {x}" for x in voice))
    if forbidden_lines:
        parts.append("【语料禁用表达（写卡时不要用这些词/句式）】\n"
                     + "\n".join(forbidden_lines))
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text



def _scale_menu() -> str:
    """量级档位清单（给模型闭集选）。"""
    from app.generation.power_scale import SCALE_HINTS, SCALE_LEVELS

    return "\n".join(f"- {name}：{SCALE_HINTS.get(name, '')}" for name in SCALE_LEVELS)


def _anchor_menu() -> str:
    """把可选项列给模型 —— 让它在**有限档位**里选，而不是自由发挥。

    自由发挥的后果实测过：五个角色的句长全落在同一档。
    给了闭集，模型才可能真的分散开。
    """
    bands = "、".join(f"「{k}」（{b[0]}~{b[1] if b[1] < 999 else '多'} 字）"
                      for k, b in LENGTH_BANDS.items())
    ends = "、".join(f"「{x}」" for x in ENDING_STYLES)
    return f"句长档位（必选其一）：{bands}\n句末习惯（必选其一）：{ends}"


def _voice_block(already: dict[str, Any]) -> str:
    """把「已经定了的声纹」渲染成避让清单。"""
    if not already:
        return "（这批是第一批，没有需要避开的已有角色）"
    lines = []
    for name, sig in already.items():
        bits = []
        for k in ("自称", "句长", "句末", "语气底色"):
            v = (sig or {}).get(k)
            if v:
                bits.append(f"{k}={v}")
        tong = (sig or {}).get("口头禅") or []
        if tong:
            bits.append("口头禅=" + "/".join(tong))
        if bits:
            lines.append(f"- {name}：{'，'.join(bits)}")
    return "\n".join(lines) or "（无）"


_SYSTEM = """你在为一个中文小说项目设计**角色档案**。作者要新建几个角色，\
你要给每个角色一份能直接拿去写作的档案。

## 最重要的一条：几个人不能听起来像同一个人

作者的原话是「不能出现几个人说话方式差不多的情况」。
**实测教训**：只写「沉稳寡言」「泼辣直爽」这类形容词没用——\
实测五个角色的平均台词长度全挤在 7~8 字，读者分不出谁在说话。

所以「声纹」必须是**可执行的锚点**，不是形容词：
- 自称：他管自己叫什么（我 / 在下 / 老夫 / 小的 / 本座…）。
  **只写称谓本身，不要带角色名**：写「在下」，不要写「在下林汐」。
- 称呼他人：他怎么叫别人（用职衔 / 用名字 / 用外号 / 不叫名字只喂一声…）。
  **只写称呼方式，不要写整句台词**：写「小满」「林主管」，不要写「林满，快过来」。
- 句长：**必须**从下面档位里选一个
- 句末：**必须**从下面档位里选一个
- 口头禅：一句他**在特定处境里**会反复说的话（例：先反问再回答；
  用「这个我说不好」挡回去）。**不要**写「嗯」「呵呵」「加油」
  「一切都会好起来的」这类万金油 —— 这是 AI 腔最明显的地方。
- 禁用词：这个角色**永远不会**说的词或固定句式，**直接写词本身**
  （例：「亲爱的」「宝贝」「你说得对」——只是格式示例，**不要照抄**）。
  **不要**写成规则说明（反例：「从不说爱字」「不喊口号」「说自己是主角」）——
  这一栏会原样进写作提示词，规则句没法当禁用词用。
  每个角色至少 2 个，且**不许与同批其他角色重复**。
  给了参考语料时，优先从语料的「禁用表达」里挑。
- 语气底色：写**可观察的行为倾向**（例：压着说话 / 先反问再回答 / 越急越短），
  不要写「开朗」「温柔」「腹黑」这种贴标签的形容词。

{menu}

## 硬约束（会检查）

1. 同一批里任意两个人，**句长档位 + 句末习惯 + 语气底色**这三项
   **至少有两项不同**；自称不许两个人一样（除非都是「我」）。
2. 口头禅不许重复；两个人不许有同一句口头禅。
3. 句长档位在这一批里要**尽量铺开**（不要五个人全是「短」）。

## 参考语料（有就给）

如果用户消息里给了「参考语料」，那是作者选的参考书蒸馏出来的**真人说话记录**：
- 自称/称呼可以借语料里的称谓体系（如「贫道」「小友」「本座」），
  但**不要照搬角色名、门派、专有名词**；
- 句长、句末语气词按语料里人说话的习惯来，别退回默认的「短句 + 我」；
- 口头禅要仿照语料里那种处境化的口头习惯，不要换成通用安慰话；
- 禁用词优先从语料的「禁用表达」里选——那正是 AI 腔的高发词。

## 其余字段

- 身份：性别 / 年龄 / 外貌 / 身份职业 / 出身背景（每个角色都要有）
- 能力：武器 / 功法 / 能力 / 能力上限
- **量级**：他出手能造成什么后果（见下面的档位）

  量级档（**必选其一**，跨体系通用，不要预设是武侠还是修仙）：
{scales}

  - `量级`：五档里选一个
  - `出手规模`：**具体的一句**，写他能造成什么后果
    （例「抬手能让半座雾港沉进海里」）。**不要写**「威力极大」这种空话 ——
    这句会原样进提示词，模型照着它写场面。
  - `压制`：他能压住什么、压不住什么（一句话）
  - 武器、功法、能力写成 `{{"名称": "...", "说明": "..."}}`
  - **能力上限**写他**做不到**的事（2~4 条）——这是防止角色越写越强的关键
- 关系：`{{"对象名": "称呼；态度"}}`，只写与本次新建角色或已知角色的关系

## 输出

只输出 JSON，不要解释：
{{"characters": [{{"name": "角色名", "身份": {{"性别": "...", "年龄": "...", \
"外貌": "...", "身份职业": "...", "出身背景": "..."}}, \
"能力": {{"武器": [{{"名称": "...", "说明": "..."}}], "功法": [], "能力": [], \
"能力上限": ["..."]}}, "声纹": {{"自称": "...", "称呼他人": ["..."], "句长": "短", \
"句末": "直陈收尾（不用语气词）", "口头禅": ["..."], "禁用词": ["亲爱的"], \
"语气底色": "..."}}, "量级": {{"量级": "毁城级", "出手规模": "...", "压制": "..."}}, \
"关系": {{"某人": "称呼；态度"}}}}]}}"""


def _build_user(story: dict, wants: list[str], already: dict[str, Any],
                note: str = "", style_ref: str = "") -> str:
    parts = [
        "## 项目设定",
        json.dumps({
            "标题": story.get("title") or "",
            "类型": story.get("genre") or "",
            "风格": style_or(story),
            "故事简介": (story.get("premise") or "")[:800],
        }, ensure_ascii=False, indent=1),
        "",
        f"## 本次要新建的 {len(wants)} 个角色",
    ]
    for w in wants:
        parts.append(f"- {w}")
    parts += [
        "",
        "## 已有角色的声纹（**必须与下面每一个都拉开距离**）",
        _voice_block(already),
    ]
    if style_ref:
        parts += [
            "",
            "## 参考语料：这本参考书里的人是怎么说话的（声纹照这个味道写）",
            style_ref,
            "",
            "只借说话方式；语料里的角色名、门派、专有名词不要照搬。",
        ]
    if note:
        parts += ["", "## 作者额外要求", note[:600]]
    parts += ["", f"按上述要求输出这 {len(wants)} 个角色的 JSON。"]
    return "\n".join(parts)


def style_or(story: dict) -> str:
    return str(story.get("style") or "")


# ------------------------------------------------------------------ #
# 单角色（完善用户已有的卡）
# ------------------------------------------------------------------ #

def complete_card(provider: Any, character: dict, user_card: Any = None,
                  note: str = "", raise_errors: bool = False,
                  style_ref: str = "") -> dict:
    """给**一个已存在的角色**补全档案。只返回建议，不落库。

    用户已经写的内容**一个字都不改**（与 `persona.complete_persona` 同一条纪律）。

    Args:
        raise_errors: 渠道异常要不要往上抛。默认 False（吞掉、返回空）——
            「完善失败不该让用户写的东西丢掉」；用户**亲手点了按钮**的端点
            传 True，好把「渠道 429」和「模型没给出可用结果」区分开。
    """
    if not provider:
        return {}
    name = str((character or {}).get("name") or "")
    if not name:
        return {}
    existing = normalize_card(user_card) if user_card else {}
    try:
        sys_p = _SYSTEM.format(menu=_anchor_menu(), scales=_scale_menu())
        user = (
            "## 项目设定\n"
            + json.dumps({
                "标题": character.get("_title") or "",
                "类型": character.get("_genre") or "",
                "故事简介": (character.get("_premise") or "")[:600],
            }, ensure_ascii=False)
            + f"\n\n## 要完善的角色\n- {name}"
            + (f"（{character.get('role')}）" if character.get("role") else "")
            + (f"，动机：{character.get('motivation')}" if character.get("motivation") else "")
            + "\n\n## 作者已经写好的部分（**原样保留，只补空缺**）\n"
            + (json.dumps(existing, ensure_ascii=False) if existing else "（空）")
            + (("\n\n## 参考语料：参考书里的人是怎么说话的（声纹照这个味道补）\n"
                + style_ref
                + "\n\n只借说话方式；语料里的角色名、门派、专有名词不要照搬。")
               if style_ref else "")
            + (("\n\n## 作者的要求\n" + note[:400]) if note else "")
            + "\n\n只输出这一个角色（characters 数组里 1 条）。"
        )
        raw = provider.chat(sys_p, user)
        parsed = _safe_json(raw)
        cards = (parsed or {}).get("characters") or []
        if not cards or not isinstance(cards[0], dict):
            return {}
        card = normalize_card(cards[0])
        if not card:
            return {}
        # 与批量生成共用收尾：过滤规则句禁用词、清套话口头禅、重写贴标签的底色。
        # 只处理 AI 产出的卡，用户已写的字段仍由 merge_card 原样保留。
        card = finalize_cards(
            {name: card}, provider=provider,
            story={"title": character.get("_title") or "",
                   "genre": character.get("_genre") or ""},
        )["cards"][name]
        merged = merge_card(existing, card)
        return {"补充": merged, "说明": "已按作者写的内容补充空缺字段"}
    except Exception:  # noqa: BLE001 —— 完善失败不该让用户写的东西丢掉
        if raise_errors:
            raise
        return {}


#: 规范卡的顶层键 —— 有这些键就说明**已经是** normalize_card 的产物，
#: 不能再 normalize 一次（否则「身份」「能力」「声纹」这些分组会被当未知字段丢掉）。
_CARD_TOP_KEYS = frozenset({"身份", "能力", "声纹", "关系", "来源"})


def _as_card(value: Any) -> dict:
    """已经是规范结构就原样用；否则规整一次。"""
    if not isinstance(value, dict):
        return {}
    if _CARD_TOP_KEYS & set(value.keys()):
        return dict(value)
    return normalize_card(value)


def merge_card(user_card: Any, ai_card: Any) -> dict:
    """合并：**用户原文在前，AI 只补空**（同字段用户有值就不动）。"""
    u = _as_card(user_card)
    a = _as_card(ai_card)
    out: dict[str, Any] = {}

    for group in ("身份", "能力", "声纹"):
        uv = u.get(group) if isinstance(u.get(group), dict) else {}
        av = a.get(group) if isinstance(a.get(group), dict) else {}
        merged = dict(av)
        for k, v in uv.items():
            if v not in (None, "", [], {}):
                merged[k] = v
            elif k in merged:
                pass
        if merged:
            out[group] = merged

    ur = u.get("关系") if isinstance(u.get("关系"), dict) else {}
    ar = a.get("关系") if isinstance(a.get("关系"), dict) else {}
    if ur or ar:
        r = dict(ar)
        r.update(ur)
        out["关系"] = r

    if u.get("来源"):
        out["来源"] = u["来源"]
    return out


# ------------------------------------------------------------------ #
# 成批新建（互相避让 + 冲突重写）
# ------------------------------------------------------------------ #

def generate_cards(provider: Any, story: dict, wants: list[str],
                   already: dict[str, Any] | None = None,
                   note: str = "", raise_errors: bool = False,
                   style_ref: str = "") -> dict:
    """一次生成**一批**新角色的卡，彼此拉开距离。

    Args:
        provider: LLM provider（None ⇒ 空结果）。
        story: `{title, genre, style, premise}` —— 项目设定。
        wants: 要新建的角色名（或「名字：一句话设定」）。
        already: 已有角色的声纹签名 `{名: {自称/句长/句末/语气底色/口头禅}}`，
            成批生成时用来避让（也包括本批已经出来的）。
        note: 作者额外要求。
        style_ref: `style_reference()` 压出来的语料参考块（对白风格/人物声音/禁用表达）。
            **有就一定要传**：这是角色卡不「AI 腔」的主要依据。
        raise_errors: 渠道异常要不要往上抛（默认吞掉、返回空）。端点亲手触发时
            传 True —— 否则「渠道 429」会被说成「模型没给出可用结果」。

    Returns:
        `{"角色": {名: 卡}, "冲突": [...], "语气问题": [...], "轮次": n}`；
        冲突/贴标签没消掉也照样返回（标出来给人看，不静默）。
    """
    wants = [str(w).strip() for w in (wants or []) if str(w).strip()]
    if not provider or not wants:
        return {"角色": {}, "冲突": [], "语气问题": [], "轮次": 0}

    fixed = dict(already or {})
    cards: dict[str, dict] = {}
    collisions: list[dict] = []
    tone_issues: list[dict] = []
    rounds = 0

    for attempt in range(MAX_ATTEMPTS):
        rounds = attempt + 1
        try:
            raw = provider.chat(
                _SYSTEM.format(menu=_anchor_menu(), scales=_scale_menu()),
                _build_user(story, wants, {**fixed, **{
                    n: (c.get("声纹") or {}) for n, c in cards.items()}}, note,
                    style_ref),
            )
        except Exception:  # noqa: BLE001 —— 生成失败不阻断
            if raise_errors:
                raise
            break
        parsed = _safe_json(raw)
        got = (parsed or {}).get("characters") or []
        fresh: dict[str, dict] = {}
        for item in got:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            card = normalize_card(item)
            if card:
                fresh[name] = card
        if not fresh:
            # 这一轮没吐出可解析的卡 ⇒ 再试（不是最终结论）
            continue
        cards = fresh
        # 冲突检测：本批内部 + 与已有角色（已有角色只**看**不判撞，
        # 因为已有的是用户既定事实，改不了，只能让新角色让路）
        inside = strict_voice_collisions(cards, min_shared=3)
        with_fixed: list[dict] = []
        for n, c in cards.items():
            sig = c.get("声纹") or {}
            for fname, fsig in fixed.items():
                hit = _collide(sig, fsig)
                # 自称重复是单项即撞；其余锚点仍按「≥3 项相同」判。
                if len(hit) >= 3 or any("自称" in x for x in hit):
                    with_fixed.append({
                        "人物": [fname, n],
                        "相同": hit,
                        "建议": f"「{n}」要和既有的「{fname}」拉开："
                                f"换掉句长档位或句末习惯",
                    })
        collisions = inside + with_fixed

        # 「禁用词整套一样」重写没用（实测 4 轮都不改）：这类是模型偷懒的
        # 全局写作禁忌，不是角色特征。留到收尾统一清掉，不拿它触发重写。
        retry_collisions = [c for c in collisions if not _only_forbidden(c)]

        # 语气底色贴标签也当成需要重写的问题（不混进「声纹撞车」的口径）。
        tone_issues = []
        for n, c in cards.items():
            why = tone_problem((c.get("声纹") or {}).get("语气底色"))
            if why:
                tone_issues.append({
                    "人物": [n],
                    "问题": [why],
                    "建议": f"「{n}」重写语气底色：写他在什么情况下怎么说话",
                })

        if not retry_collisions and not tone_issues:
            break

        # 把冲突/不合格项贴回去让它改
        feedback: list[str] = []
        if retry_collisions:
            feedback.append(
                "上一轮有几个人说话方式撞了，必须改掉其中一个：\n"
                + "\n".join(f"- {' vs '.join(c['人物'])}：{'；'.join(c['相同'])}"
                            for c in retry_collisions[:8]))
        if tone_issues:
            feedback.append(
                "以下角色的语气底色只是形容词，必须改成可观察的行为：\n"
                + "\n".join(f"- {c['人物'][0]}：{'；'.join(c['问题'])}"
                            for c in tone_issues[:8]))
        note = (note + "\n\n" if note else "") + "\n\n".join(feedback)

    # 收尾统一处理（与单卡补全共用同一个函数）。
    fin = finalize_cards(cards, provider=provider, story=story)

    # 收尾（含聚焦重写）后按最终卡面重算，避免回执里挂着已经改好的问题。
    tone_issues = [
        {"人物": [n], "问题": [why],
         "建议": f"「{n}」重写语气底色：写他在什么情况下怎么说话"}
        for n, c in cards.items()
        if (why := tone_problem((c.get("声纹") or {}).get("语气底色")))
    ]
    collisions = strict_voice_collisions(cards) if cards else []

    return {
        "角色": cards,
        "冲突": collisions,
        "语气问题": tone_issues,
        "已清理禁用词": fin["已清理禁用词"],
        "已过滤禁用词": fin["已过滤禁用词"],
        "补写禁用词": fin["补写禁用词"],
        "语气重写": fin["语气重写"],
        "已清理口头禅": fin["已清理口头禅"],
        "已去重口头禅": fin["已去重口头禅"],
        "自称回退": fin["自称回退"],
        "轮次": rounds,
    }


def _collide(a: dict, b: dict) -> list[str]:
    """两份声纹之间相同的锚点（与 `voice_collisions` 同口径）。"""
    shared: list[str] = []
    a = a or {}
    b = b or {}
    if a.get("自称") and a.get("自称") == b.get("自称") \
            and a.get("自称") not in GENERIC_SELF_REFERENCES:
        shared.append(f"自称都是「{a['自称']}」")
    for key, label in (("句长", "句长"), ("句末", "句末习惯"), ("语气底色", "语气底色")):
        if a.get(key) and a.get(key) == b.get(key):
            shared.append(f"{label}都是「{a[key]}」")
    return shared


# ------------------------------------------------------------------ #
# 从正文抽出角色卡（原创项目用；也用于参考书语料的蒸馏侧）
# ------------------------------------------------------------------ #
#
# 从正文抽卡时，武器/功法/能力的名称**必须**在正文里出现过。
# 理由：蒸馏侧那条线（`style/character_profile.py`）的闸门是「逐字摘录」，
# 这里是同一套哲学的简化版 —— 宁可这条能力没有，也不能凭空造一个。

_CARD_EXTRACT_SYSTEM = """你从**已经写出来的正文**里，为指定角色整理一份「角色卡」。\
只写正文里有依据的内容。

## 要抽的字段

- **身份**：性别 / 年龄 / 外貌 / 身份职业 / 出身背景（只写正文能支持的）
- **能力**：
  - 武器：他用什么兵器/法器，写成 {"名称": "...", "说明": "..."}
  - 功法/技艺：他会什么本事
  - 能力：天赋、异能、特殊体质
  - **能力上限**：正文里明确表现出的「他做不到什么」（这一项最容易被忽略，
    但它决定角色会不会越写越强，请务必找）
- **声纹**：自称 / 称呼他人 / 句长（短/中/长）/ 句末习惯 / 口头禅 / 禁用词 / 语气底色
  - 口头禅必须是**正文里他真说过的原话**，逐字摘；只出现过一次的不算口头禅

## 硬要求

- `武器` `功法` `能力` 的**名称**必须逐字出现在给定正文里；查不到就不要写这一条。
- 没有依据的字段给空数组/空字符串，不要推测。
- 不要写别人的内容。

## 输出

只输出 JSON：
{"身份": {"性别": "...", "年龄": "...", "外貌": "...", "身份职业": "...", \
"出身背景": "..."}, "能力": {"武器": [{"名称": "...", "说明": "..."}], "功法": [], \
"能力": [], "能力上限": ["..."]}, "声纹": {"自称": "...", "称呼他人": [], \
"句长": "短", "句末": "...", "口头禅": [], "禁用词": [], "语气底色": "..."}}
"""


def extract_card(provider: Any, name: str, excerpts: list[str],
                 *, story: dict | None = None) -> dict:
    """从正文片段抽一个角色的卡（**名称必须能在正文里查到**）。

    Returns:
        规整后的卡（可能只有部分字段）；失败返回 `{}`。

    为什么不复用 `style/character_profile.py`：那条流水线有 6 字段 + claims
    证据门（只认 `observed` 且要求逐字摘录），是给参考书角色用的、
    改它的闸门会牵动大量既有验收。这里只补「武器/功法/能力」这一层，
    用同一套哲学（证据优先）但独立实现，互不影响。
    """
    if not provider or not name:
        return {}
    body = "\n\n---\n\n".join([str(x) for x in (excerpts or []) if x][:6])
    if not body:
        return {}
    try:
        user = (f"角色：{name}\n"
                + (f"项目设定：{json.dumps(story, ensure_ascii=False)}\n" if story else "")
                + f"\n正文片段：\n\n{body}\n\n按上述 JSON 输出这一个角色的卡。")
        raw = provider.chat(_CARD_EXTRACT_SYSTEM, user)
        card = normalize_card(_safe_json(raw))
    except Exception:  # noqa: BLE001 —— 抽卡失败不该阻断任何主链
        return {}
    return _ground_card(card, body)


def _ground_card(card: dict, source_text: str) -> dict:
    """把卡里**正文查不到**的武器/功法/能力删掉（证据优先）。

    只校验能力组的「名称」与口头禅 —— 身份类字段（性别/年龄）常以
    「她」「十九岁」这类改写出现，逐字匹配会把对的也删光。
    """
    import re

    if not isinstance(card, dict) or not card:
        return card
    text = source_text or ""

    def _appears(item: str) -> bool:
        s = (item or "").strip()
        if not s:
            return False
        if s in text:
            return True
        # 「断刀」在正文里可能写成「那把断刀」—— 去掉常见量词前缀再试
        core = re.sub(r"^(那把|这把|一柄|一把|那柄|这柄)", "", s)
        return bool(core) and core in text

    ability = card.get("能力")
    if isinstance(ability, dict):
        for f in ABILITY_FIELDS:
            items = ability.get(f) or []
            kept = [it for it in items
                    if isinstance(it, dict) and _appears(it.get("名称"))]
            if kept:
                ability[f] = kept
            else:
                ability.pop(f, None)
        if not ability:
            card.pop("能力", None)

    voice = card.get("声纹")
    if isinstance(voice, dict):
        tong = [x for x in (voice.get("口头禅") or []) if _appears(str(x))]
        if tong:
            voice["口头禅"] = tong
        else:
            voice.pop("口头禅", None)
        if not voice:
            card.pop("声纹", None)
    return card
