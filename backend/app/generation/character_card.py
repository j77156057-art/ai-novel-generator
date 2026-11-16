"""角色卡（character card）：一个角色的**全部静态档案**。

## 为什么要有这张卡

用户原话（2026-10-29）：

> 「似乎没有记录角色使用的武器，使用的功法，有什么能力这一类型的有关角色的记录，
>  要不要做一个角色卡，专门记录有关与角色的东西」

实测确认：`kg_entities.attrs` 里 38 个 character **每个都有**
`arc / current_goal / secrets`，但 **没有一个**武器 / 功法 / 能力字段；
`kg_character_state` 只有 location / injury / goal / mood / inventory / alive 六类
（那是**会变的状态**，不是**不变的档案**）。

⇒ 缺的是一层「这个角色是谁」的静态档案。它和状态是两回事：

- **角色卡**：武器是断刀、会用「换气」、不识字 —— 几十段都不变；
- **角色状态**：现在在青石城、左肩中箭、目标是活下来 —— 每段都可能变。

## 字段设计（用户：「武器功法能力只是大概，背景性别等等你可以自己完善」）

分四组，每组各自回答一个写作时真正会用上的问题：

### 身份组（写描写时用）
- 性别 / 年龄 / 外貌 / 身份职业 / 出身背景
  —— 「他多大岁数、长什么样、干什么的、从哪来」

### 能力组（写打斗与解决问题时用，用户点名要的）
- **武器**：用什么，什么状态（`[{名称, 说明}]`）
- **功法/技艺**：会什么本事，达到什么程度（`[{名称, 说明}]`）
- **能力**：天赋、异能、特殊体质（`[{名称, 说明}]`）
- **能力上限**：明确**做不到**什么 —— 防止模型越写越强
  （这是「角色不崩」最有效的一条，比写「他很厉害」有用得多）

### 声纹组（写对话时用，**这一组决定「不千篇一律」**）
- 自称 / 对他人的称呼 / 句长 / 句末习惯 / 口头禅 / 禁用词 / 语气底色

**为什么这一组必须写成可执行锚点**：实测（project=5，5 个角色 129+76+89+43+62 条台词）
平均台词长度全挤在 7.0~8.6 字，风格向量最近的一对距离只有 0.037。
写「沉稳寡言」四个人都会写成一样；写「不用问句、句末不带语气词、叫人都用职衔」
才是**能比对、能检出冲突**的锚点。

### 关系组（写互动时用）
- 对每个重要角色的：称呼 / 态度 / 位置（谁压谁）/ 边界（什么不能说）
  —— 与 `app/style/character_profile.py` 的七维度同构（参考书角色用那套）。

## 与人写的 persona 的关系

`persona`（说话方式/行为倾向/性格底线/关键经历）是**用户手写优先**的权威层，
本模块的声纹组是它的**结构化补充** —— 两者都进提示词，不互相覆盖：

- `persona.来源 == user` ⇒ 用户写的，任何自动流程都不覆盖；
- 声纹组的 `来源` 独立记（model / derived / user），字段级保护。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

#: 身份组字段（写作时按这个顺序渲染）。
IDENTITY_FIELDS: tuple[str, ...] = (
    "性别", "年龄", "外貌", "身份职业", "出身背景",
)

#: 能力组字段。每一项是 `[{名称, 说明}]` 结构（能力上限是字符串列表）。
ABILITY_FIELDS: tuple[str, ...] = ("武器", "功法", "能力")

#: 声纹组字段 —— **可执行锚点**，不是形容词。
VOICE_FIELDS: tuple[str, ...] = (
    "自称", "称呼他人", "句长", "句末", "口头禅", "禁用词", "语气底色",
)

#: 来源标记。
SOURCE_MODEL = "model"
SOURCE_DERIVED = "derived"
SOURCE_USER = "user"

#: 句子长度档位 → 可判定的字数区间（供冲突检测与提示词共用）。
#:
#: 实测依据（project=5，五个角色各 43~129 条真实台词）：
#: 平均长度全挤在 7.0~8.6 字 —— 说明模型默认就写短句，
#: 档案里若只写「句子短」，五个角色会全部落在同一档，等于没写。
#: ⇒ 档位必须**分得开**：短 ≤8 字，中 9~16，长 ≥17。
LENGTH_BANDS: dict[str, tuple[int, int]] = {
    "极短": (1, 4),
    "短": (5, 8),
    "中": (9, 16),
    "长": (17, 999),
}

#: 句末习惯的可选值（同样是可判定的特征，不是形容词）。
ENDING_STYLES: tuple[str, ...] = (
    "直陈收尾（不用语气词）", "常用「呢」「吧」", "常用「啊」「嘛」",
    "常以反问收尾", "常以命令收尾", "常以省略号收尾",
)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _as_list(value: Any, limit: int = 8) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [str(value).strip()]
    return items[:limit]


def _as_items(value: Any, limit: int = 6) -> list[dict]:
    """把「武器/功法/能力」规整成 `[{名称, 说明}]`。

    模型常吐成几种形态，都收：
    - `["断刀", "短匕"]`                → `[{名称:"断刀", 说明:""}, ...]`
    - `[{"名称":"断刀","说明":"…"}]`     → 原样
    - `["断刀：锈刀，刀镡刻半个姓"]`      → 拆成名称 + 说明
    - `"断刀"`                          → 单条
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        value = [value]
    out: list[dict] = []
    for v in value:
        if isinstance(v, dict):
            name = _as_text(v.get("名称") or v.get("name") or "")
            desc = _as_text(v.get("说明") or v.get("desc") or v.get("description") or "")
            if name:
                out.append({"名称": name, "说明": desc})
            continue
        s = _as_text(v)
        if not s:
            continue
        # 「名称：说明」形态（中英文冒号都认）
        m = re.match(r"^(.{1,20}?)[：:]\s*(.+)$", s)
        if m:
            out.append({"名称": m.group(1).strip(), "说明": m.group(2).strip()})
        else:
            out.append({"名称": s, "说明": ""})
        if len(out) >= limit:
            break
    return out[:limit]


def normalize_card(raw: Any) -> dict:
    """把模型吐的角色卡规整成固定结构（缺的留空，多的丢掉）。

    只做**结构**整理，不做内容判断。
    """
    if not isinstance(raw, dict):
        return {}
    card: dict[str, Any] = {}

    # —— 先把「已分组」的写法摊平（2026-10-29 修）——
    # 模型会按我们给的格式返回 {"身份": {...}, "声纹": {...}}，
    # 但下面的循环读的是平铺的 raw["性别"] / raw["自称"]
    # ⇒ 分组形态全丢，整张卡变成 {}（实测：假 provider 返回合法 JSON，
    # 解析出来仍然是 0 张卡）。这里统一摊平后再走同一套逻辑。
    flat: dict[str, Any] = {}
    # ⚠️「量级」**既不摊平、也不进 flat**：它是纯分组（量级/出手规模/压制）。
    # `raw = flat` 之后 raw 里就没有它了 ⇒ 量级组永远是 None（实测踩过两次）。
    # ⇒ 提前单独取出，后面直接用。
    _raw_scale = raw.get("量级")
    for grp in ("身份", "能力", "声纹"):
        g = raw.get(grp)
        if isinstance(g, dict):
            for k, v in g.items():
                if k not in flat or flat[k] in (None, "", [], {}):
                    flat[k] = v
    for k, v in raw.items():
        if k in ("身份", "声纹", "关系", "来源"):
            continue
        if k == "量级":
            # 量级要么是分组 dict，要么是单个档位字符串（都保留原样）
            continue
        # ⚠️「能力」既是**分组名**又是**字段名**（能力/异能）：
        # 传进来的是 dict ⇒ 它是分组，已经展开过，跳过；
        # 传进来的是 list/str ⇒ 它是平铺字段，必须**按原名保留**
        # （否则 {"能力": ["听声辨位"]} 会被整块丢掉 —— 实测踩过）。
        if k == "能力" and isinstance(v, dict):
            continue
        # 平铺字段优先（它就是权威，分组里的同名字段不覆盖）
        flat[k] = v
    raw = flat

    ident = {}
    for f in IDENTITY_FIELDS:
        v = _as_text(raw.get(f))
        if v:
            ident[f] = v
    # 性别单独收（模型有时写进「身份职业」里）
    if ident:
        card["身份"] = ident

    ability = {}
    for f in ABILITY_FIELDS:
        items = _as_items(raw.get(f))
        if items:
            ability[f] = items
    cap = _as_list(raw.get("能力上限"), limit=6)
    if cap:
        ability["能力上限"] = cap
    if ability:
        card["能力"] = ability

    voice = {}
    for f in VOICE_FIELDS:
        if f in ("称呼他人", "口头禅", "禁忌", "禁用词"):
            v = _as_list(raw.get(f), limit=6) if not isinstance(raw.get(f), str) \
                else _as_list(raw.get(f), limit=1)
            if v:
                voice[f] = v
        else:
            v = _as_text(raw.get(f))
            if v:
                voice[f] = v
    if voice:
        card["声纹"] = voice

    scale = _raw_scale
    if isinstance(scale, dict) and scale:
        sc = {}
        for f in ("量级", "出手规模", "压制"):
            v = _as_text(scale.get(f))
            if v:
                sc[f] = v
        if sc:
            card["量级"] = sc
    elif isinstance(scale, str) and scale.strip():
        card["量级"] = {"量级": scale.strip()}

    rel = raw.get("关系")
    if isinstance(rel, dict) and rel:
        card["关系"] = {str(k): _as_text(v) for k, v in rel.items() if _as_text(v)}
    elif isinstance(rel, list):
        keep = {}
        for r in rel:
            if not isinstance(r, dict):
                continue
            who = _as_text(r.get("对象") or r.get("name") or "")
            if not who:
                continue
            keep[who] = _as_text({k: v for k, v in r.items()
                                  if k not in ("对象", "name")})
        if keep:
            card["关系"] = keep

    src = _as_text(raw.get("来源"))
    if src:
        card["来源"] = src
    return card


def voice_signature(card: Any) -> dict:
    """取出声纹的**可比较**特征（冲突检测用）。"""
    voice = (card or {}).get("声纹") if isinstance(card, dict) else {}
    if not isinstance(voice, dict):
        return {}
    sig = {
        "自称": _as_text(voice.get("自称")),
        "句长": _as_text(voice.get("句长")),
        "句末": _as_text(voice.get("句末")),
        "语气底色": _as_text(voice.get("语气底色")),
        "口头禅数": len(_as_list(voice.get("口头禅"))),
    }
    return {k: v for k, v in sig.items() if v}


def voice_collisions(cards: dict[str, Any], *, min_shared: int = 3) -> list[dict]:
    """检查一批角色卡里，哪些人的**声纹撞了**。

    判据（`min_shared` 个锚点相同就算撞）：
    - 自称相同（都自称「我」**不算**撞 —— 中文里绝大多数人自称「我」，
      那没有区分度；只有非「我」的自称相同才算，如两个人都自称「在下」）；
    - 句长同档 + 句末习惯相同 + 语气底色相同。

    返回 `[{人物:[甲,乙], 相同:[字段], 建议:"…"}]` —— 给模型看，让它改一个。

    ## 为什么需要这个

    用户原话：「生成的不要千篇一律，得有各自的性格，
    不能出现几个人说话方式差不多的情况」。

    **靠提示词说「要各不相同」不管用** —— 实测 project=5 有 persona，
    五个角色平均台词长度仍全挤在 7.0~8.6 字（最近一对距离 0.037）。
    ⇒ 必须先**生成**，再**检测**，撞了就带着冲突信息让它重写。
    """
    names = [n for n in (cards or {}) if n]
    sigs = {n: voice_signature(cards[n]) for n in names}
    out: list[dict] = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            sa, sb = sigs[a], sigs[b]
            shared: list[str] = []
            if sa.get("自称") and sa.get("自称") == sb.get("自称") \
                    and sa.get("自称") not in ("我", "俺", "我自己"):
                shared.append(f"自称都是「{sa['自称']}」")
            if sa.get("句长") and sa.get("句长") == sb.get("句长"):
                shared.append(f"句长都是「{sa['句长']}」")
            if sa.get("句末") and sa.get("句末") == sb.get("句末"):
                shared.append(f"句末习惯都是「{sa['句末']}」")
            if sa.get("语气底色") and sa.get("语气底色") == sb.get("语气底色"):
                shared.append(f"语气底色都是「{sa['语气底色']}」")
            if len(shared) >= min_shared:
                out.append({
                    "人物": [a, b],
                    "相同": shared,
                    "建议": f"「{b}」要换个说法：至少改掉句长档位或句末习惯，"
                            f"让两个人开口就能分出来",
                })
    return out


def render_card(card: Any, *, max_items: int = 3,
                include_scale: bool = False) -> str:
    """把角色卡渲染成一行人话（进提示词）。

    形态：`性别…｜职业…｜武器：断刀（锈）｜功法：换气｜上限：不会水｜声纹：…`

    ⚠️ **量级默认不渲染**：提示词里量级由「量级对照块」统一渲染（带本场对照），
    这里再写一遍是重复。界面要用时传 `include_scale=True`。

    为什么压成一行：35 个角色的卡各占五行就是 175 行，
    会把提示词挤爆（实测伏笔块 97 行 2,639 字就占了两成）。
    只有**本段出场**的角色才渲染，见 `render_cast`。
    """
    if not isinstance(card, dict) or not card:
        return ""
    parts: list[str] = []

    ident = card.get("身份") or {}
    if isinstance(ident, dict):
        for f in IDENTITY_FIELDS:
            v = _as_text(ident.get(f))
            if v:
                parts.append(f"{f}{v}" if f in ("性别", "年龄") else f"{f}：{v}")

    ability = card.get("能力") or {}
    if isinstance(ability, dict):
        for f in ABILITY_FIELDS:
            items = ability.get(f) or []
            if not items:
                continue
            names = []
            for it in items[:max_items]:
                nm = _as_text(it.get("名称")) if isinstance(it, dict) else _as_text(it)
                ds = _as_text(it.get("说明")) if isinstance(it, dict) else ""
                names.append(f"{nm}（{ds}）" if ds else nm)
            if names:
                parts.append(f"{f}：" + "、".join(names))
        cap = _as_list(ability.get("能力上限"), limit=3)
        if cap:
            parts.append("做不到：" + "；".join(cap))

    # —— 量级（默认不渲染：提示词里由「量级对照块」统一渲染）——
    if include_scale:
        scale = card.get("量级")
        if isinstance(scale, dict):
            lv = _as_text(scale.get("量级"))
            scope = _as_text(scale.get("出手规模"))
            if lv:
                parts.append(f"量级{lv}" + (f"：{scope}" if scope else ""))

    voice = card.get("声纹") or {}
    if isinstance(voice, dict):
        vparts = []
        if voice.get("自称"):
            vparts.append(f"自称「{_as_text(voice['自称'])}」")
        if voice.get("称呼他人"):
            vparts.append("称呼：" + "、".join(_as_list(voice["称呼他人"], limit=3)))
        if voice.get("句长"):
            vparts.append(f"句长{_as_text(voice['句长'])}")
        if voice.get("句末"):
            vparts.append(_as_text(voice["句末"]))
        if voice.get("口头禅"):
            vparts.append("口头禅：" + "、".join(
                f"「{x}」" for x in _as_list(voice["口头禅"], limit=3)))
        if voice.get("禁用词"):
            vparts.append("不说：" + "、".join(_as_list(voice["禁用词"], limit=3)))
        if voice.get("语气底色"):
            vparts.append(_as_text(voice["语气底色"]))
        if vparts:
            parts.append("说话：" + "，".join(vparts))

    return " ｜ ".join(parts)


def render_cast(cards: dict[str, Any], names: Iterable[str]) -> str:
    """渲染**本段出场**角色的卡（多角色时额外加「拉开距离」的提醒）。"""
    lines: list[str] = []
    used = []
    for n in names:
        card = (cards or {}).get(n)
        if not card:
            continue
        text = render_card(card)
        if text:
            lines.append(f"- {n}：{text}")
            used.append(n)
    if len(used) < 2:
        return "\n".join(lines)
    # 多角色同场：明确要求互相拉开（这是「不千篇一律」的提示词侧手段）
    return ("\n".join(lines)
            + "\n（以上几人同场说话时，句长、句末与称呼必须**彼此不同**；"
              "不要把同一个人的腔调套到别人身上。）")
