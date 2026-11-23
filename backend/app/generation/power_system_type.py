"""**体系类型**：这套修炼体系的**精细度**是哪一档。

## 为什么要有这个维度

用户（2026-10-29）：

> 「顺便给境界卡加一个档位吧，境界分明清晰，类似修仙的那种什么什么境界，
> 第几重，什么初期，中期，大圆满，**这种算第一类**，
> 然后是相对没这么清晰的，类似《长夜司》那种没有细分那么清楚的，**算第二档**，
> 再然后是那种境界基本没划定的，类似从凡人再到什么修行者，再到什么圣人，
> 再到什么神明这一类的，境界划分模糊，**可能每一个阶段差距很大，
> 也可能每一个档位差距很小**」

## 三类的差别不在「有几个境界」，在「边界有多硬」

| 类型 | 边界 | 典型 | 写作时怎么用 |
|---|---|---|---|
| **细分型** | **硬**：明确列出，且常有「初期/中期/大圆满」 | 炼气→筑基→金丹；每境分九层 | 境界差**一阶**就可能生死之别 ⇒ 差距要写实 |
| **序列型** | **较硬**：明确列出，但**不细分**（同一序列内不分小境界） | 二十来条「途径」并列，各管一摊；同序列内强弱也有别 | 跨序列差距大；同序列内差距**按个人**（序列内的「扮演」程度） |
| **粗放型** | **软**：只有大阶段，阶段之间差距**可能很大也可能很小** | 凡人→修行者→圣人→神明 | **不能假设等差**！要靠**具体表现**说明强弱 |

⚠️ 第三类是最容易写坏的：模型看到「圣人」会默认比「修行者」强很多，
但设定里可能「修行者」这一档就压过「圣人」。
⇒ 第三类**必须**靠「出手规模」（量级）来定强弱，而不是靠境界名。

## 与「档位（band）」的关系：正交，不是同一个东西

- **体系类型**：纵向 —— 这套体系**怎么分**（硬/较硬/软）；
- **档位（band）**：横向 —— 群像分层用（「入门/小成/登堂」，路人按层归类）。

「第一类/第二类/第三类」说的是**前者**。

## 三类各自怎么影响「谁压谁」的判断

- 细分型 / 序列型：境界序数**可以直接比**（同体系内）；
- 粗放型：境界序数**不可比** ⇒ 只能用量级（`app.generation.power_scale`）比。
"""

from __future__ import annotations

from typing import Any

from app.generation.coerce import as_text as _as_text

#: 体系类型（纵向精细度）。
#:
#: - `细分型`：有境界 + 小境界（初期/中期/大圆满、第几重）
#: - `序列型`：有境界、不细分（如「二十来条途径」那种）
#: - `粗放型`：只有大阶段、边界模糊（凡人→修行者→圣人→神明）
SYSTEM_TYPES: tuple[str, ...] = ("细分型", "序列型", "粗放型")

#: 各类型的默认小境界数量（0 = 不细分）。
#:
#: 细分型才有小境界；序列型/粗放型默认没有。
DEFAULT_STAGE_COUNT: dict[str, int] = {
    "细分型": 3,     # 初期 / 中期 / 大圆满（或第1重/第2重…）
    "序列型": 0,
    "粗放型": 0,
}

#: 各类型在提示词里的**一句话说明**（给模型看，影响它怎么写差距）。
SYSTEM_TYPE_HINTS: dict[str, str] = {
    "细分型": "境界分明，每境之间可能再分初期/中期/大圆满；"
              "差一个小境界就可能是强弱之别，差距要写实。",
    "序列型": "境界（序列）明确但**不细分**，同序列内强弱因人而异；"
              "跨序列的差距才写得出来。",
    "粗放型": "只有几个大阶段，**边界模糊**：每阶段之间的差距可能很大也可能很小，"
              "不能假设等差；强弱要靠具体表现（出手规模）来说明。",
}

#: 粗放型：境界序数**不可信**（不能假设「后一个必然强很多」）。
#:
#: ⇒ 遇到这类体系，比较强弱时**只能用量级**（power_scale），
#: 不能用境界序数 —— 否则会把「修行者」写成比「圣人」强。
SOFT_TYPES = ("粗放型",)



def normalize_system_type(raw: Any) -> str:
    """规整体系类型；认不出来默认「序列型」（最中性的一种）。

    默认选「序列型」而不是「细分型」：细分型会**假设**有初期/中期，
    序列型只假设「有明确境界但没细分」—— 假设更少 ⇒ 更安全。
    """
    s = _as_text(raw)
    if not s:
        return "序列型"
    for t in SYSTEM_TYPES:
        if t in s:
            return t
    # 也认「细分/序列/粗放」这种不带「型」的写法
    for key, t in (("细分", "细分型"), ("序列", "序列型"),
                   ("粗放", "粗放型"), ("模糊", "粗放型")):
        if key in s:
            return t
    return "序列型"


def is_soft(system: Any) -> bool:
    """这套体系的境界序数**不可信**吗？（粗放型 ⇒ 不可信）"""
    if not isinstance(system, dict):
        return False
    return normalize_system_type(system.get("类型")) in SOFT_TYPES


def type_hint(system: Any) -> str:
    """体系类型的一句话说明（进提示词）。"""
    if not isinstance(system, dict):
        return ""
    t = normalize_system_type(system.get("类型"))
    return SYSTEM_TYPE_HINTS.get(t, "")


def realm_order_trusted(system: Any) -> bool:
    """境界序数能不能直接比高低？

    - 细分型 / 序列型 ⇒ 能（同体系内序数即高低）；
    - 粗放型 ⇒ **不能**（边界模糊，可能后一档差距更小）。
    """
    return not is_soft(system)


#: 项目设定里可能承载体系类型的键（按优先级）。
_SYSTEM_TYPE_KEYS: tuple[str, ...] = (
    "system_type", "体系类型", "power_system_type",
    "境界体系类型", "realm_system_type",
)


def system_type_from_bible(bible: dict | None) -> tuple[str, str]:
    """从 `plot_bible` 读体系类型，返回 `(类型, 来源)`。

    来源只有三种：
    - `plot_bible.<键>`：项目显式声明；
    - `plot_bible.systems`：从体系列表里读，全部一致才算；
    - `default`：**没有证据**，按最中性的「序列型」处理（只做同体系相对高低，
      不假设细分、更不假设粗放）。这不是给数值补值，而是把校验边界收到最保守。

    刻意不读正文、不猜类型：类型决定「能不能比、比到什么程度」，
    猜错会把粗放型的「圣人」误判成比「修行者」强。
    """
    if not isinstance(bible, dict):
        return "序列型", "default"
    for key in _SYSTEM_TYPE_KEYS:
        raw = bible.get(key)
        if raw:
            return normalize_system_type(raw), f"plot_bible.{key}"
    systems = bible.get("systems")
    if isinstance(systems, list):
        declared = {
            normalize_system_type(s.get("类型") or s.get("system_type"))
            for s in systems
            if isinstance(s, dict) and (s.get("类型") or s.get("system_type"))
        }
        if len(declared) == 1:
            return declared.pop(), "plot_bible.systems"
        if len(declared) > 1:
            # 一个项目里混着多种体系类型：没有单一可信口径 ⇒ 收到最保守的一档。
            return "序列型", "plot_bible.systems.mixed"
    return "序列型", "default"

