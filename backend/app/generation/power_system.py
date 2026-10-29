"""修炼体系：**可配的境界阶梯**（体系 → 境界 → 小境界）。

## 为什么要有这个模块

用户诉求（2026-10-29）：

> 「如果涉及到武侠的，或者是修仙的，还有其他乱七八糟的修炼体系的，
> 那么这个要不要做去角色卡，那么主角目前的境界，以后的境界，
> 还有中途的修炼等级，还有其他各式各样配角和路人甲的等级」

## 现有的东西能做什么、缺什么

项目里已有 `plot_bible["realm_order"]`（有序词表）+ sentinel 的**境界倒退**检测
（`app/kg/sentinel.py`）。量过之后（发布版冒烟库）：

**能做**：判断「写低了没有」（有跌落事件才判；不同量纲一律不判）。

**缺的**（= 用户要的）：

| 缺的 | 现在为什么不行 |
|---|---|
| 体系本身可配 | `REALM_SYSTEMS` 是**硬编码的 3 套**（修仙档位 / 品阶 / 星级），用户自己的体系只能塞进 `realm_order`，而那个结构只有「有序词表」，没有小境界、没有档位、没有含义 |
| 小境界 | `realm_order` 是**一维**的，`炼气三层` 只能塞成一个独立词条，和 `炼气` 平级 ⇒ 「炼气三层 → 炼气四层」不算升级 |
| 档位（分层群像） | 没有任何分层机制，路人只能一个个建档（实测 38 个 character 实体） |
| 修炼等级/天赋/瓶颈/掉境原因 | 完全没有 |
| 谁压谁（境界对比） | 没有 |

## 本模块的数据模型（三层）

```
体系（system）      「修仙」/「品阶」/「武道九品」…
  └─ 境界（realm）  「炼气」/「筑基」/「金丹」…
       └─ 小境界（stage）  「炼气一层」…「炼气九层」
```

每层都带**语义**，不只是名字：

- 境界带 `档位`（band）：用于**分层群像** —— 路人只记档位（「筑基期散修」），
  不逐个记境界；
- 境界带 `说明`：写作时知道这一境意味着什么（能做什么、寿命多长、社会地位）；
- 小境界带 `说明`：让「炼气三层到四层」有区分度。

## 与既有 `realm_order` 的关系：**叠加，不推翻**

- `realm_order`（一维/二维词表）继续是 **sentinel 的输入**（倒退检测），
  本模块**不改**它的语义；
- 本模块是**更结构化的一层**，存在 `plot_bible["power_system"]` 下，
  与 `realm_order` 平行；
- 两者都存在时：**sentinel 仍用 `realm_order`**（不动既有行为），
  写作侧与路线规划用本模块的结构。
  ⇒ 没有 `power_system` 的老项目，行为**逐字节不变**。
"""

from __future__ import annotations

from typing import Any

#: 体系层的键。
SYSTEM_KEYS = ("名称", "说明", "境界")

#: 境界层的键。
REALM_KEYS = ("名称", "说明", "档位", "小境界")

#: 小境界层的键。
STAGE_KEYS = ("名称", "说明")

#: 档位（分层群像用）—— 默认档位名。
#:
#: 为什么需要档位：实测 38 个 character 实体，若逐个记境界，维护成本高，
#: 而绝大多数角色的境界在正文里根本不会被提及。
#: ⇒ 只给「档位」（如「筑基期散修」「炼气期弟子」），
#: 主角与主要配角才逐个精确记境界。
DEFAULT_BANDS: tuple[str, ...] = ("凡境", "初入", "小成", "登堂", "一方", "顶尖")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "；".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip()


def _as_list(value: Any, limit: int = 30) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value] if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value if str(v).strip()]
    else:
        items = [str(value).strip()]
    return items[:limit]


def normalize_stage(raw: Any) -> dict:
    """规整一个小境界。"""
    if not isinstance(raw, dict):
        s = _as_text(raw)
        return {"名称": s, "说明": ""} if s else {}
    name = _as_text(raw.get("名称") or raw.get("name"))
    if not name:
        return {}
    return {"名称": name, "说明": _as_text(raw.get("说明") or raw.get("desc"))}


def normalize_realm(raw: Any, default_band: str = "") -> dict:
    """规整一个境界（可含小境界与档位）。"""
    if not isinstance(raw, dict):
        s = _as_text(raw)
        return {"名称": s, "说明": "", "档位": default_band, "小境界": []} if s else {}
    name = _as_text(raw.get("名称") or raw.get("name"))
    if not name:
        return {}
    out: dict[str, Any] = {
        "名称": name,
        "说明": _as_text(raw.get("说明") or raw.get("desc")),
        "档位": _as_text(raw.get("档位") or raw.get("band")) or default_band,
    }
    stages = [normalize_stage(s) for s in (raw.get("小境界") or raw.get("stages") or [])]
    out["小境界"] = [s for s in stages if s]
    return out


def normalize_system(raw: Any) -> dict:
    """规整一个体系（名称 + 说明 + 境界列表）。"""
    if not isinstance(raw, dict):
        s = _as_text(raw)
        return {"名称": s, "说明": "", "境界": []} if s else {}
    name = _as_text(raw.get("名称") or raw.get("name"))
    if not name:
        return {}
    realms = [normalize_realm(r) for r in (raw.get("境界") or raw.get("realms") or [])]
    from app.generation.power_system_type import normalize_system_type

    return {
        "名称": name,
        "说明": _as_text(raw.get("说明") or raw.get("desc")),
        # 体系类型（纵向精细度）：细分型 / 序列型 / 粗放型。
        # ⚠️ 与「档位（band）」正交：类型说「这套体系怎么分」，
        # 档位说「群像怎么分层」。
        "类型": normalize_system_type(raw.get("类型") or raw.get("type")),
        "境界": [r for r in realms if r],
    }


def normalize_power_system(raw: Any) -> dict:
    """把用户填的体系规整成规范结构。

    输入可以是单个体系（dict）或多个体系（list）⇒ 统一成
    `{"体系": [体系...]}`。
    """
    # ⚠️ 两种输入形态都要认（实测踩到契约不一致）：
    #   裸 list            → 顶层就是体系数组
    #   {"体系": [...]}     → 顶层包了一层
    #   {"名称": ..., ...}  → 单个体系
    if isinstance(raw, dict) and "体系" in raw:
        raw = raw.get("体系")
    if isinstance(raw, list):
        systems = [normalize_system(s) for s in raw]
        return {"体系": [s for s in systems if s]}
    one = normalize_system(raw)
    return {"体系": [one] if one else []}


def systems_of(bible: Any) -> list[dict]:
    """从剧情圣经里取体系列表（没有就空）。"""
    if not isinstance(bible, dict):
        return []
    raw = bible.get("power_system")
    if not raw:
        return []
    return normalize_power_system(raw)["体系"]


def realm_index(system: dict) -> dict[str, dict]:
    """体系内「境界名 → 境界」的索引（含小境界名，便于查「炼气三层」）。

    大小境界名都会登记 ⇒ 「炼气三层」能查到它所属的境界与档位。
    """
    out: dict[str, dict] = {}
    for realm in (system or {}).get("境界") or []:
        rname = realm.get("名称")
        if not rname:
            continue
        out[rname] = realm
        for stage in realm.get("小境界") or []:
            sname = stage.get("名称")
            if sname:
                out[sname] = {**realm, "所属境界": rname, "小境界名": sname}
    return out


def find_realm(systems: list[dict], word: str) -> dict | None:
    """在所有体系里找一个境界/小境界名。找不到返回 None。"""
    w = _as_text(word)
    if not w:
        return None
    for sysx in systems or []:
        idx = realm_index(sysx)
        if w in idx:
            hit = dict(idx[w])
            hit["体系"] = sysx.get("名称") or ""
            return hit
    return None


def realm_rank(system: dict, word: str) -> int | None:
    """境界在体系里的序数（从 0 起）；找不到返回 None。

    小境界算「它所属境界的序数」—— 跨小境界的精细比较交给 `stage_rank`。
    """
    realms = (system or {}).get("境界") or []
    w = _as_text(word)
    for i, realm in enumerate(realms):
        if realm.get("名称") == w:
            return i
        for stage in realm.get("小境界") or []:
            if stage.get("名称") == w:
                return i
    return None


def stage_rank(system: dict, word: str) -> int | None:
    """小境界的精细序数（`炼气三层` → 2）；不是小境界返回 None。"""
    for realm in (system or {}).get("境界") or []:
        for j, stage in enumerate(realm.get("小境界") or []):
            if stage.get("名称") == word:
                return j
    return None


#: 档位个数超过这个数 ⇒ 判定「模型把位置描述当档位了」，按顺序归纳。
#:
#: 实测（2026-10-29，真实模型）：模型把「江湖位置」填进档位列，
#: 于是 5 个境界有 5 个**各不相同的长句**当档位（「镖局趟子手」…），
#: 档位就不成「档」了 —— 分层群像需要的是少数几档。
MAX_BANDS = 6

#: 归纳出的档位名（**中性等级词**，不写死武侠/修仙）。
#:
#: ⚠️ 不能用「第1档」这种机器名 —— 它会被原样渲染进提示词，
#: 读者/模型看到的是「第1档」而不是「入门」。
#: 这些词是**跨体系通用**的（不预设修仙还是武侠）。
_FALLBACK_BAND_NAMES: tuple[str, ...] = (
    "入门", "小成", "登堂", "一方", "顶尖", "超凡",
)


def _derive_bands(system: dict) -> dict:
    """体系里**一个档位都没有**时，按境界顺序派生出少数几档。

    为什么要有（2026-10-29 真机录制取证）：真实模型常回 Markdown 表格，
    而表格里往往没有档位列（实测表头是「阶段 / 契约对象 / 力量来源 /
    强项 / 死穴 / 内部跨度」）⇒ `bands_of` 为空 ⇒「档位分层」这个
    群像抓手落空：路人甲没有档次可归，只能一个个建档。

    纪律：
    - 派生只做**分层**，不做**强弱比较** —— 档位名取中性闭集，
      粗放型照样不参与序数比较（`realm_rank` 仍返 None）；
    - 只在**完全没有**档位时动手；模型给了档位就一个字都不改。
    """
    realms = system.get("境界") or []
    n = len(realms)
    if n < 2:
        return system
    groups = min(MAX_BANDS, n)
    for i, realm in enumerate(realms):
        idx = i * groups // n
        realm["档位"] = _FALLBACK_BAND_NAMES[
            min(idx, len(_FALLBACK_BAND_NAMES) - 1)]
    system["境界"] = realms
    return system


def consolidate_bands(system: dict) -> dict:
    """档位过多时，按境界顺序**归纳**成少数几档。

    做法：把 N 个境界均分成 `min(MAX_BANDS, N)` 段，
    同一段的境界共用一个档位名（`第1档`…），并把原来的位置描述并进「说明」。

    ⚠️ 只在**档位明显不是档位**时动手：判据是「档位数 > MAX_BANDS」
    或「档位值过长/像句子」（实测的「镖局趟子手、各派入门弟子」）。
    """
    if not isinstance(system, dict):
        return system
    realms = system.get("境界") or []
    if not realms:
        return system
    bands = bands_of(system)
    if not bands:
        # 一个档位都没有（表格形态常见）⇒ 派生，别让分层抓手落空
        return _derive_bands(system)
    if len(bands) <= MAX_BANDS and all(
            len(str(r.get("档位") or "")) <= 8 for r in realms):
        return system

    n = len(realms)
    groups = min(MAX_BANDS, n)
    # 均分：第 i 境落在第 i*groups//n 档
    for i, realm in enumerate(realms):
        idx = i * groups // n
        old = str(realm.get("档位") or "").strip()
        if old:
            # 原来的描述并进说明（它多半是「这一境的人在江湖里是什么身份」）
            desc = str(realm.get("说明") or "").strip()
            if old not in desc:
                realm["说明"] = (desc + "；" + old) if desc else old
        realm["档位"] = _FALLBACK_BAND_NAMES[
            min(idx, len(_FALLBACK_BAND_NAMES) - 1)]
    system["境界"] = realms
    return system


def bands_of(system: dict) -> list[str]:
    """体系里用到的档位（去重保序）。

    档位是**分层群像**的抓手：写作时只需要知道「他大概什么档次」，
    不需要每个人精确境界。
    """
    out: list[str] = []
    for realm in (system or {}).get("境界") or []:
        b = _as_text(realm.get("档位"))
        if b and b not in out:
            out.append(b)
    return out


def normalize_type_name(system: Any) -> str:
    """体系类型的名字（细分型 / 序列型 / 粗放型）。"""
    from app.generation.power_system_type import normalize_system_type

    return normalize_system_type((system or {}).get("类型")
                                 if isinstance(system, dict) else None)


def render_system(system: dict, *, max_realms: int = 12) -> str:
    """把体系渲染成一段人话（进提示词）。

    形态：`修仙｜炼气（凡人之上，寿三百）→ 筑基（…）→ …`
    小境界只在**当前在场角色用到**时单独渲染（见 `render_character_realm`），
    不在这里铺开 —— 十几档全写进提示词太占地方。
    """
    if not isinstance(system, dict):
        return ""
    name = _as_text(system.get("名称"))
    if not name:
        return ""
    parts: list[str] = [name]
    desc = _as_text(system.get("说明"))
    if desc:
        parts.append(desc)
    # 体系类型说明（告诉模型「差距该写多大」）
    try:
        from app.generation.power_system_type import type_hint

        hint = type_hint(system)
        if hint:
            parts.append(f"【{normalize_type_name(system)}】{hint}")
    except Exception:  # noqa: BLE001 —— 渲染失败不该丢整段
        pass
    ladder = []
    for realm in (system.get("境界") or [])[:max_realms]:
        rname = _as_text(realm.get("名称"))
        if not rname:
            continue
        rdesc = _as_text(realm.get("说明"))
        band = _as_text(realm.get("档位"))
        seg = rname
        if band:
            seg += f"（{band}）"
        if rdesc:
            seg += f"：{rdesc}"
        ladder.append(seg)
    if ladder:
        parts.append(" → ".join(ladder))
    return " ｜ ".join(parts)


def render_character_realm(realm: dict, *, system_name: str = "") -> str:
    """渲染**单个角色**的境界信息（一行）。

    形态：`境界：炼气三层（炼气 · 初入）｜ 天赋：单灵根 ｜ 卡在：炼气四层`
    """
    if not isinstance(realm, dict) or not realm:
        return ""
    parts: list[str] = []
    cur = _as_text(realm.get("当前境界"))
    stage = _as_text(realm.get("当前小境界"))
    band = _as_text(realm.get("档位"))
    if cur or stage:
        seg = f"{cur}{stage}"
        if band:
            seg += f"（{band}）"
        parts.append(f"境界：{seg}")
    talent = _as_text(realm.get("天赋"))
    if talent:
        parts.append(f"天赋：{talent}")
    stuck = _as_text(realm.get("瓶颈"))
    if stuck:
        parts.append(f"卡在：{stuck}")
    route = _as_list(realm.get("路线"), limit=6)
    if route:
        parts.append("路线：" + " → ".join(route))
    cost = _as_text(realm.get("代价"))
    if cost:
        parts.append(f"代价：{cost}")
    if system_name:
        parts.insert(0, f"体系：{system_name}")
    return " ｜ ".join(parts) if parts else ""


def compare_realms(a: dict, b: dict, systems: list[dict]) -> int | None:
    """比较两个角色的境界高低：返回 1/0/-1（a 高/平/低）。

    **不同体系或不同量纲 ⇒ 返回 None**（不可比就不比）。
    与 sentinel 的量纲纪律一致：宁可少判，不可误报。
    """
    if not a or not b:
        return None
    aw = _as_text(a.get("当前境界"))
    bw = _as_text(b.get("当前境界"))
    if not aw or not bw:
        return None
    for sysx in systems or []:
        ra = realm_rank(sysx, aw)
        rb = realm_rank(sysx, bw)
        if ra is None or rb is None:
            continue
        # ⚠️ **量纲 = 体系**，不是档位。
        # 档位是**横向分层**（入门/小成），同一体系里多个境界可以落在同一档
        # （炼气与筑基可能都算「初入」）⇒ 用它当量纲会把正常比较全判成不可比
        # （实测：三种体系类型比较全返回 None，「谁压谁」永远出不来）。
        # 跨体系才不可比（同名不同义）—— 而这里本来就在**同一个** sysx 里比。
        # ⚠️ **粗放型**（凡人→修行者→圣人→神明）边界模糊 ⇒
        # 序数不能当强弱（后一档可能差距更小）⇒ 不比，只说「不可比」。
        from app.generation.power_system_type import realm_order_trusted

        if not realm_order_trusted(sysx):
            return None
        return 1 if ra > rb else (0 if ra == rb else -1)
    return None
