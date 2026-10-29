"""**量级标尺**：让模型知道「这一击该有多大」。

## 问题（2026-10-29 实测，用户反馈）

> 「世界观里有神明……这种高纬度生物，举手投足可以破坏一个城市甚至一个星球那种角色的感觉，
>  但生成出来的就和路边混混斗殴一样，角色统一干扁的发言，千篇一律的短字，
>  打架毫无张力，boss拿着个刀打，男女主能被个门挡住」

## 根因不是「少一个境界字段」，而是这个

量了发布版冒烟库三个项目的**真实提示词**（角色约束块 = 模型真正看到的东西）：

| 项目 | 角色约束块长度 | 「这一击能有多大」类信号词 |
|---|---|---|
| 1 | 81 字 | **0 次** |
| 2 | 35 字 | **0 次** |
| 5 | 1930 字 | **0 次** |

1930 字里写满了「失落神女」「窃取太阳权柄的旧神阵营首领」「弑神之战」——
**全是身份描述词，没有一个字说明「他出手是什么规模」**。

⇒ 模型只能按它**最熟悉的物理尺度**去写：刀、门板、石阶。
于是「旧神」的一击写成了「黑刃骤然穿过木门」——神明之战变成门板之争。

**「身份描述」不等于「量级」**：模型读到「神女」不知道该写多大，
读到「他能一击毁掉半座城」才知道。

## 本模块做什么

给每个角色/体系一个**可操作的量级**，并在同场戏里**横向对照**：

```
量级对照（本场）：
  · 沉日者（窃取太阳权柄的旧神）＝灭城级：出手能抹平半座城 / 能让整片海倒卷
  · 顾星河（最后的点灯人）＝毁物级：能压住一盏不灭的火，但伤不了人
  · 白烬（无面亡者统领）＝凡人级：被黑刃擦到就会散
  ⇒ 沉日者若要出手，顾星河挡不住；白烬在两人对峙里只能当人质。
```

## 三条设计纪律

1. **量级是相对标尺，不是绝对数值**：用「能造成什么后果」描述
   （毁城 / 毁屋 / 杀人 / 杀人），**不写 DPS**；
2. **同场才对照**：不在提示词里塞全部角色的量级（那会占地方），
   只给**本段在场**的，且**成对比较**（谁压谁）——
   模型最需要的不是「每个人多强」，而是「这两个打起来谁赢」；
3. **量级不越权**：模型给的量级必须与角色身份一致，
   不允许「凡人级角色一击毁城」这种自相矛盾（由 judge 检查）。
"""

from __future__ import annotations

from typing import Any

#: 量级档（从低到高）。**相对标尺**，跨体系通用。
#:
#: 名字用「能造成什么后果」而不是数值 —— 因为叙事需要的是**画面**，
#: 不是算式。且这些词本身就在提示词里给模型一个物理锚点。
SCALE_LEVELS: tuple[str, ...] = (
    "凡人级",      # 只能伤到一个人
    "伤人级",      # 一击重伤/杀人
    "毁物级",      # 毁掉一间屋/一段城墙
    "毁城级",      # 抹平一片城区
    "灭世级",      # 改变一地的地貌或天象
)

#: 量级 → 典型的一句话尺度描述（写进提示词给模型当锚点）。
SCALE_HINTS: dict[str, str] = {
    "凡人级": "出手只能伤到眼前的人",
    "伤人级": "一击能重伤或杀死一个人",
    "毁物级": "一击能毁掉一间屋子、一段城墙",
    "毁城级": "一击能抹平一片城区",
    "灭世级": "出手能改变天象、地貌或一地的存亡",
}

#: 角色档案里量级相关的字段。
SCALE_FIELDS: tuple[str, ...] = ("量级", "出手规模", "压制")

#: 量级在体系里的对应（境界名 → 量级档）。
#:
#: 这是「境界阶梯」与「量级标尺」的桥：**境界高 ⇒ 量级高**，
#: 但不是一一对应（同一个境界里可以有量级差很远的两种人），
#: 所以是「默认映射 + 可覆盖」。


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def normalize_scale(raw: Any) -> dict:
    """规整一个角色的量级信息。

    形态：`{"量级": "毁城级", "出手规模": "抹平半座城", "压制": "能压住灯塔的火"}`
    """
    if not isinstance(raw, dict):
        s = _as_text(raw)
        return {"量级": s} if s else {}
    out: dict[str, Any] = {}
    for f in SCALE_FIELDS:
        v = _as_text(raw.get(f))
        if v:
            out[f] = v
    return out


def scale_level(raw: Any) -> int | None:
    """量级档的序号（越高越强）；认不出来返回 None。"""
    s = _as_text(raw)
    if not s:
        return None
    for i, name in enumerate(SCALE_LEVELS):
        if name in s:
            return i
    # 也认「毁城」「灭世」这类不带「级」的写法
    for i, name in enumerate(SCALE_LEVELS):
        core = name.replace("级", "")
        if core and core in s:
            return i
    return None


def scale_of(card: Any) -> dict:
    """从角色卡里取量级信息。"""
    if not isinstance(card, dict):
        return {}
    return normalize_scale(card.get("量级"))


def default_scale_for_realm(realm_name: str, systems: list[dict]) -> str | None:
    """按境界高低给一个**默认量级**（可被角色自己的量级覆盖）。

    规则（通用不变式，不猜内容）：
    - 凡人 ⇒ 凡人级；
    - 体系里越靠后的境界 ⇒ 量级越高（线性映射到 `SCALE_LEVELS`）。

    ⚠️ 这是**兜底**，不是判决：角色卡里写了量级就以角色卡的为准。
    """
    if not realm_name:
        return None
    for sysx in systems or []:
        realms = sysx.get("境界") or []
        for i, realm in enumerate(realms):
            names = [realm.get("名称")] + [
                s.get("名称") for s in (realm.get("小境界") or [])]
            if realm_name not in names:
                continue
            if i == 0:
                return SCALE_LEVELS[0]
            # 线性映射：第 i 境 → 第 round(i * (len-1) / (len-1)) 档
            span = len(realms) - 1
            if span <= 0:
                return SCALE_LEVELS[0]
            idx = round(i * (len(SCALE_LEVELS) - 1) / span)
            return SCALE_LEVELS[max(0, min(idx, len(SCALE_LEVELS) - 1))]
    return None


def resolve_scale(card: Any, systems: list[dict]) -> dict:
    """取角色最终生效的量级：卡里写的优先，否则按境界推。"""
    s = scale_of(card)
    if s.get("量级"):
        return s
    realm = ""
    if isinstance(card, dict):
        p = card.get("修炼") or {}
        if isinstance(p, dict):
            realm = _as_text(p.get("当前境界"))
    guess = default_scale_for_realm(realm, systems)
    if not guess:
        return {}
    return {"量级": guess, "来源": "按境界推定"}


def compare_scales(a: dict, b: dict) -> int | None:
    """比较两个量级：1/0/-1；任一认不出来返回 None（不可比就不比）。"""
    ia = scale_level(a)
    ib = scale_level(b)
    if ia is None or ib is None:
        return None
    return 1 if ia > ib else (0 if ia == ib else -1)


def render_scale_contrast(casts: list[dict], *, systems: list[dict] | None = None
                          ) -> str:
    """渲染**同场戏**的量级对照块。

    Args:
        casts: 在场角色 `[{"名字","card"}]`。
        systems: 体系（用于按境界推定缺失的量级）。

    形态：

        【量级对照（本场）】
        · 沉日者（窃取太阳权柄的旧神）＝灭城级：出手能抹平半座城
        · 顾星河（最后的点灯人）＝毁物级：能压住一盏不灭的火
        ⇒ 沉日者若全力出手，顾星河挡不住；白烬在这两人之间只能当人质。

    ⚠️ **只给同场的人**（其余角色的量级对这场戏没用，只会占地方）。
    ⚠️ 结尾那句「谁压谁」是模型最需要的 —— 它要的不是「每个人多强」，
    而是「这两个打起来会怎样」。
    """
    if not casts:
        return ""
    rows: list[tuple[str, str, str]] = []
    for entry in casts:
        if not isinstance(entry, dict):
            continue
        name = _as_text(entry.get("名字") or entry.get("name"))
        if not name:
            continue
        card = entry.get("card") if isinstance(entry.get("card"), dict) else {}
        sc = resolve_scale(card, systems or [])
        level = _as_text(sc.get("量级"))
        if not level:
            continue
        hint = _as_text(sc.get("出手规模")) or SCALE_HINTS.get(level, "")
        role = _as_text(entry.get("身份") or (card.get("身份") or {}).get("身份职业")
                        if isinstance(card.get("身份"), dict) else "")
        head = f"{name}（{role}）＝{level}" if role else f"{name}＝{level}"
        if hint:
            head += f"：{hint}"
        rows.append((name, head, level))
    if not rows:
        return ""

    lines = ["【量级对照（本场）】"]
    for _n, head, _lv in rows:
        lines.append(f"· {head}")

    # —— 谁压谁（两两比较，取最悬殊的一对）——
    best: tuple[int, str] | None = None
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            cmp = compare_scales(
                {"量级": rows[i][2]}, {"量级": rows[j][2]})
            if cmp is None:
                continue
            gap = abs((scale_level({"量级": rows[i][2]}) or 0)
                      - (scale_level({"量级": rows[j][2]}) or 0))
            if best is None or gap > best[0]:
                if cmp > 0:
                    best = (gap, f"{rows[i][0]}远强于{rows[j][0]}；"
                                 f"若{rows[i][0]}出手，{rows[j][0]}挡不住")
                else:
                    best = (gap, f"{rows[j][0]}远强于{rows[i][0]}；"
                                 f"若{rows[j][0]}出手，{rows[i][0]}挡不住")
    if best and best[0] >= 2:
        lines.append(f"⇒ {best[1]}")
    lines.append("（写打斗时，出手的规模按各自量级来：不要让低量级的角色挡下高量级的一击，"
                 "也不要让高量级的交锋写成贴身短刃。）")
    return "\n".join(lines)
