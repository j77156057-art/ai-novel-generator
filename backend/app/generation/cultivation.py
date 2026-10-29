"""角色的**修炼档案**：当前境界、天赋、瓶颈、路线、代价。

## 解决用户提的四个具体问题

用户原话（2026-10-29）：

> 「主角目前的境界，以后的境界，还有中途的修炼等级，还有其他各式各样配角和路人甲的等级，
>  放进去以后**为了他们的境界会提示成什么样**，提升的慢还是快，
>  慢的话会不会被其他人嘲笑，快的话会不会被赞扬，
>  角色有什么**修炼天赋**，比如说什么体质，或者灵根，
>  或者是修炼**遇到了叉**，掉了境界，或者**为了活命，燃烧了寿命**，
>  或者是去了什么**洞府**得到了提升这种**动态的路线**，怎么规划」

拆成四件可实现的东西：

| 用户问的 | 本模块的字段 | 依据 |
|---|---|---|
| 目前的境界 | `当前境界` + `当前小境界` + `档位` | 正文里写出来的 |
| 以后的境界 / 修炼等级 | `路线`（有序）+ `目标境界` | **AI 建议，人点采纳** |
| 提升慢会怎样、快会怎样 | `处境`（由档位与路线的落差推导） | 见 `derive_situation` |
| 天赋 / 体质 / 灵根 | `天赋` | 正文里写出来的 |
| 遇到叉、掉境界、燃烧寿命、洞府机缘 | `代价` + `路线`里的里程碑 | 正文里写出来的 |

## 三条纪律

1. **正文写的**（当前境界/天赋/代价）⇒ 只能来自正文，不许编；
2. **AI 规划的**（路线/目标境界）⇒ 只作建议，人点采纳才写；
3. **推导的**（处境）⇒ 由已有数据按固定规则算，不问模型。
"""

from __future__ import annotations

import json
from typing import Any

from app.generation.power_system import (
    bands_of, find_realm, realm_index, realm_rank, systems_of,
)

#: 修炼档案的字段（存在角色的 `card.修炼` 下）。
REALM_PROFILE_FIELDS: tuple[str, ...] = (
    "当前境界", "当前小境界", "档位", "天赋", "瓶颈", "目标境界", "路线", "代价", "来历",
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


def normalize_profile(raw: Any) -> dict:
    """规整一个修炼档案。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for f in ("当前境界", "当前小境界", "档位", "天赋", "瓶颈", "目标境界", "代价", "来历"):
        v = _as_text(raw.get(f))
        if v:
            out[f] = v
    route = _as_list(raw.get("路线"), limit=8)
    if route:
        out["路线"] = route
    return out


def attach_profile(card: Any, profile: Any) -> dict:
    """把修炼档案挂到角色卡上（`card.修炼`）。"""
    if not isinstance(card, dict):
        return {}
    p = normalize_profile(profile)
    if not p:
        return dict(card)
    out = dict(card)
    out["修炼"] = p
    return out


def profile_of(card: Any) -> dict:
    """从角色卡里取修炼档案。"""
    if not isinstance(card, dict):
        return {}
    p = card.get("修炼")
    return normalize_profile(p)


# ==================================================================== #
# 分层群像：路人只记档位
# ==================================================================== #

def normalize_tier(raw: Any) -> dict:
    """规整一个「档位层」（分层群像的一层）。

    形态：`{"名称": "筑基期散修", "档位": "小成", "人数": "数十",
             "特征": "独来独往，忌惮同门"}`
    """
    if not isinstance(raw, dict):
        s = _as_text(raw)
        return {"名称": s, "档位": "", "人数": "", "特征": ""} if s else {}
    out = {
        "名称": _as_text(raw.get("名称") or raw.get("name")),
        "档位": _as_text(raw.get("档位") or raw.get("band")),
        "人数": _as_text(raw.get("人数") or raw.get("count")),
        "特征": _as_text(raw.get("特征") or raw.get("trait")),
    }
    return out if out["名称"] else {}


def normalize_tiers(raw: Any) -> list[dict]:
    out = [normalize_tier(t) for t in (raw or [])]
    return [t for t in out if t]


# ==================================================================== #
# 「处境」推导：境界差距会引发什么
# ==================================================================== #

def derive_situation(profile: dict, systems: list[dict],
                     *, audience: list[dict] | None = None) -> str:
    """由**已有的数据**推出「他现在的处境会引发什么」——不问模型。

    用户问的：「提升的慢还是快，慢的话会不会被其他人嘲笑，
    快的话会不会被赞扬」。

    规则（全部是**通用不变式**，不猜内容）：

    1. **比在场的人强** ⇒ 可能被畏惧/被拉拢；
    2. **比在场的人弱** ⇒ 可能被轻视/被欺；
    3. **路线卡在瓶颈** ⇒ 会被议论/催逼；
    4. **燃烧过寿命** ⇒ 身体会先垮（无论他多强）；
    5. **路线快到顶** ⇒ 可能被针对。

    ⚠️ 只给**倾向**，不给具体台词 —— 具体怎么被嘲笑/赞扬是写作时的事，
    这里只提供「谁压谁」这个事实依据。
    """
    if not isinstance(profile, dict) or not profile:
        return ""
    bits: list[str] = []

    cur = _as_text(profile.get("当前境界"))
    if cur and audience:
        for other in audience:
            if not isinstance(other, dict):
                continue
            oc = _as_text(other.get("当前境界"))
            if not oc:
                continue
            cmp = _compare(cur, oc, systems)
            if cmp is None:
                continue
            who = _as_text(other.get("名字")) or "对方"
            if cmp > 0:
                bits.append(f"强于{who}（{oc}）")
            elif cmp < 0:
                bits.append(f"弱于{who}（{oc}）")
            else:
                bits.append(f"与{who}（{oc}）相当")

    if _as_text(profile.get("瓶颈")):
        bits.append("卡在瓶颈，旁人容易议论或催逼")
    cost = _as_text(profile.get("代价"))
    if cost:
        bits.append(f"已经付出过代价（{cost}），身体会先垮")
    route = _as_list(profile.get("路线"), limit=8)
    if len(route) >= 2 and _as_text(profile.get("目标境界")):
        bits.append("路线已铺到目标境界前夜，可能被人盯上")

    if not bits:
        return ""
    return "；".join(bits)


def _compare(a: str, b: str, systems: list[dict]) -> int | None:
    """两个境界名比较（1/0/-1）；不可比返回 None。"""
    for sysx in systems or []:
        ra = realm_rank(sysx, a)
        rb = realm_rank(sysx, b)
        if ra is None or rb is None:
            continue
        ia = realm_index(sysx)
        ja, jb = ia.get(a), ia.get(b)
        if not ja or not jb:
            continue
        # ⚠️ **同体系内不同档位是可比的高下**（初入 < 小成 < 登堂）——
        # 之前判成「不可比」是错的：那会让「谁压谁」永远不出现，
        # 处境推导就只剩「卡在瓶颈」这类与强弱无关的信息。
        # 真正不可比的是**跨体系**（另一个体系的同名档位），
        # 而这里只在**同一个体系**内比较（同一个 sysx）⇒ 必然可比。
        return 1 if ra > rb else (0 if ra == rb else -1)
    return None


# ==================================================================== #
# AI 规划路线（只作建议）
# ==================================================================== #

_ROUTE_SYSTEM = """你在为一个中文小说的角色规划**修炼路线**。

## 体系（本书的境界阶梯）

{ladder}

## 这个角色的档案

{profile}

## 要规划的路线

给出他**从现在到目标境界**的路线，要点：

1. **路线是有代价的**：每一步写清「达到什么境界」+「要付出什么」
   （资源、时间、伤、代价、机缘在哪儿）。
2. **中间要有卡点**：修炼不可能一路顺，标出最容易卡住的一处（瓶颈）。
3. **要有意外**：允许有「掉境界」「燃烧寿命换短期战力」「进洞府/得传承」
   这类转折 —— 但每一条都要有**理由**，不能是作者安排。
4. **速度要合理**：给的步数与代价要匹配他现在的处境
   （天赋好的人快，天赋差的慢；被打压过的人可能有隐患）。
5. 目标境界要与体系里的**真实档位名**一致。

## 输出 JSON（只输出 JSON）

{{"路线": ["炼气三层 → 炼气四层：靠苦修，耗三年", "炼气四层 → 炼气五层：卡在经脉，需一枚洗髓丹"],
 "目标境界": "筑基",
 "瓶颈": "炼气七层，经脉承受不住雷劫",
 "代价": "为压制伤势服过一枚压境界的丹药，短期爆发后跌回炼气二层"}}
"""


def build_route_prompt(profile: dict, system: dict) -> str:
    """构造路线规划提示词。"""
    ladder = []
    for realm in (system or {}).get("境界") or []:
        rname = realm.get("名称")
        if not rname:
            continue
        seg = rname
        if realm.get("档位"):
            seg += f"（{realm['档位']}）"
        if realm.get("说明"):
            seg += f"：{realm['说明']}"
        stages = [s.get("名称") for s in (realm.get("小境界") or []) if s.get("名称")]
        if stages:
            seg += "　小境界：" + "、".join(stages)
        ladder.append(seg)
    return _ROUTE_SYSTEM.format(
        ladder="\n".join(f"- {x}" for x in ladder) or "（未提供）",
        profile=json.dumps(profile or {}, ensure_ascii=False, indent=1),
    )


def parse_route(raw: Any) -> dict:
    """解析路线规划结果（只取能用的字段）。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for f in ("目标境界", "瓶颈", "代价"):
        v = _as_text(raw.get(f))
        if v:
            out[f] = v
    route = _as_list(raw.get("路线"), limit=8)
    if route:
        out["路线"] = route
    return out


def plan_route(provider: Any, profile: dict, system: dict) -> dict:
    """让模型规划路线。**只返回建议，不落库。**

    失败返回 `{}`。
    """
    if not provider or not isinstance(profile, dict):
        return {}
    try:
        raw = provider.chat(build_route_prompt(profile, system), "按上述 JSON 输出。")
    except Exception:  # noqa: BLE001 —— 规划失败不该阻断任何主链
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            import re
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                return {}
            try:
                raw = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return {}
    return parse_route(raw)
