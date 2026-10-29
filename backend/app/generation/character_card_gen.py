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
from typing import Any

from app.generation.character_card import (
    ABILITY_FIELDS,
    ENDING_STYLES,
    IDENTITY_FIELDS,
    LENGTH_BANDS,
    VOICE_FIELDS,
    normalize_card,
    voice_collisions,
)

#: 冲突重写的最大轮数（一次重写就够用；两轮是防模型第一次没听清）。
MAX_RETRY = 2


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
- 自称：他管自己叫什么（我 / 在下 / 老夫 / 小的 / 本座…）
- 称呼他人：他怎么叫别人（用职衔 / 用名字 / 用外号 / 不叫名字只喂一声…）
- 句长：**必须**从下面档位里选一个
- 句末：**必须**从下面档位里选一个
- 口头禅：一句他反复会说的话（要具体，不要「嗯」「啊」这种）
- 禁用词：这个角色**永远不会**说的词或句式（例：从不用感叹句 / 不说「爱」字）
- 语气底色：一句话概括腔调（例：压着说话 / 居高临下 / 怯生生）

{menu}

## 硬约束（会检查）

1. 同一批里任意两个人，**句长档位 + 句末习惯 + 语气底色**这三项
   **至少有两项不同**；自称不许两个人一样（除非都是「我」）。
2. 口头禅不许重复；两个人不许有同一句口头禅。
3. 句长档位在这一批里要**尽量铺开**（不要五个人全是「短」）。

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
"句末": "直陈收尾（不用语气词）", "口头禅": ["..."], "禁用词": ["..."], \
"语气底色": "..."}}, "量级": {{"量级": "毁城级", "出手规模": "...", "压制": "..."}}, \
"关系": {{"某人": "称呼；态度"}}}}]}}"""


def _build_user(story: dict, wants: list[str], already: dict[str, Any],
                note: str = "") -> str:
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
                  note: str = "", raise_errors: bool = False) -> dict:
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
                   note: str = "", raise_errors: bool = False) -> dict:
    """一次生成**一批**新角色的卡，彼此拉开距离。

    Args:
        provider: LLM provider（None ⇒ 空结果）。
        story: `{title, genre, style, premise}` —— 项目设定。
        wants: 要新建的角色名（或「名字：一句话设定」）。
        already: 已有角色的声纹签名 `{名: {自称/句长/句末/语气底色/口头禅}}`，
            成批生成时用来避让（也包括本批已经出来的）。
        note: 作者额外要求。
        raise_errors: 渠道异常要不要往上抛（默认吞掉、返回空）。端点亲手触发时
            传 True —— 否则「渠道 429」会被说成「模型没给出可用结果」。

    Returns:
        `{"角色": {名: 卡}, "冲突": [...], "轮次": n}`；
        冲突没消掉也照样返回（标出来给人看，不静默）。
    """
    wants = [str(w).strip() for w in (wants or []) if str(w).strip()]
    if not provider or not wants:
        return {"角色": {}, "冲突": [], "轮次": 0}

    fixed = dict(already or {})
    cards: dict[str, dict] = {}
    collisions: list[dict] = []
    rounds = 0

    # 解析不出角色时的最大尝试次数。
    #
    # 实测（opencode Go 通道，deepseek-v4.1-flash）：同一个 prompt，
    # 这一次返回 4 个角色的合法 JSON，下一次却返回一句寒暄
    # 「Hi! It looks like your message came through empty」。
    # 思考型模型在推理被吃光时会这样退化 ⇒ **必须重试**，
    # 不能把「这一次没吐 JSON」当成「这个功能不可用」。
    PARSE_RETRY = 2

    for attempt in range(MAX_RETRY + 1 + PARSE_RETRY):
        rounds = attempt + 1
        try:
            raw = provider.chat(
                _SYSTEM.format(menu=_anchor_menu(), scales=_scale_menu()),
                _build_user(story, wants, {**fixed, **{
                    n: (c.get("声纹") or {}) for n, c in cards.items()}}, note),
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
        inside = voice_collisions(
            {n: c for n, c in cards.items()}, min_shared=3)
        with_fixed: list[dict] = []
        for n, c in cards.items():
            sig = c.get("声纹") or {}
            for fname, fsig in fixed.items():
                hit = _collide(sig, fsig)
                if len(hit) >= 3:
                    with_fixed.append({
                        "人物": [fname, n],
                        "相同": hit,
                        "建议": f"「{n}」要和既有的「{fname}」拉开："
                                f"换掉句长档位或句末习惯",
                    })
        collisions = inside + with_fixed
        if not collisions:
            break
        # 把冲突贴回去让它改
        note = (note + "\n\n" if note else "") + (
            "上一轮有几个人说话方式撞了，必须改掉其中一个：\n"
            + "\n".join(f"- {' vs '.join(c['人物'])}：{'；'.join(c['相同'])}"
                        for c in collisions[:8]))

    return {"角色": cards, "冲突": collisions, "轮次": rounds}


def _collide(a: dict, b: dict) -> list[str]:
    """两份声纹之间相同的锚点（与 `voice_collisions` 同口径）。"""
    shared: list[str] = []
    a = a or {}
    b = b or {}
    if a.get("自称") and a.get("自称") == b.get("自称") \
            and a.get("自称") not in ("我", "俺", "我自己"):
        shared.append(f"自称都是「{a['自称']}」")
    for key, label in (("句长", "句长"), ("句末", "句末习惯"), ("语气底色", "语气底色")):
        if a.get(key) and a.get(key) == b.get(key):
            shared.append(f"{label}都是「{a[key]}」")
    return shared


def _safe_json(raw: Any) -> Any:
    """模型输出 → dict（三级兜底：整体 JSON / 剥 think / 抓第一个 {...}）。"""
    import re

    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}
    return {}

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
