"""AI 帮用户建**修炼体系**与**档位分层**（都是建议，人点采纳）。

## 为什么需要

用户诉求（2026-10-29）：体系不写死（武侠修仙都有），
且路人甲只记档位、不逐个建档。

⇒ 这两个都要**生成**：
1. 体系（阶梯 + 每境说明 + 小境界 + 档位）；
2. 档位分层（群像用）。

## 纪律（与角色卡一致）

- AI 只给建议，**不落库**；
- 生成的档位名必须来自**体系里真实存在的档位**（不能凭空造「宗师」这种）；
- 小境界数量按体系规模给（不是每个体系都该有 9 层）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.generation.cultivation import normalize_tiers
from app.generation.power_system import bands_of, normalize_power_system

#: 生成体系时的提示词。
_SYSTEM_PROMPT = """你在为一本中文小说设计**修炼体系**。

用户会说明这本书的调子与体系类型（武侠/修仙/异能/星际……）。
你要给出一套**自洽、可写**的境界阶梯。

## 先定「体系类型」（三选一）

| 类型 | 特征 | 例子 |
|---|---|---|
| **细分型** | 境界明确，且每境内再分小境界（初期/中期/大圆满、第一重…） | 炼气→筑基→金丹，每境九层 |
| **序列型** | 境界明确但**不细分** | 二十二条序列，同序列内不分小境界 |
| **粗放型** | 只有几个大阶段，**边界模糊**；阶段之间差距可能很大也可能很小 | 凡人→修行者→圣人→神明 |

⚠️ **类型决定后面所有细节**：细分型才给小境界；粗放型**不能假设等差**
（不能默认「圣人」就压过「修行者」很多）—— 要靠具体表现说明差距。

## 硬要求

1. **档位（band）分层**：整条阶梯要落在 **3~6 个档位**上。
   档位是「分层群像」的抓手 —— 写作时要知道「他大概什么档次」，
   不需要每个人精确境界。例：`入门/小成/登堂/一方/顶尖`。

   ⚠️ **档位 ≠ 江湖位置**（实测踩过：模型把「镖局趟子手」这种**人物描述**
   填进档位，于是每一境一个不同的长句，档位就不成「档」了）：
   - **档位**：少数几个可复用的**等级名**，会同时出现在好几境上
     （如「入门」覆盖立架，「小成」覆盖易筋，「登堂」覆盖行气…）；
   - **江湖位置**：这一境的人**在江湖里是什么身份**，写进「说明」里
     （如「镖局趟子手、各派入门弟子」）。

   ⇒ 若有 7 境却只放得下 4 个档位，就让**相邻的境界共用一个档位**。
2. **每一境都要有说明**：这一境意味着什么（能做什么、寿数、社会地位）。
   说明要**可写**，不是「很强」这种空话。
3. **小境界**：
   - **细分型**：每境都要给（如「炼气一~九层」或「初期/中期/大圆满」），
     小境界要**连续编号**，且同一体系内**命名规则一致**
     （都用「X层」或都用「X期」）；
   - **序列型 / 粗放型**：**不给**小境界（给了就说明类型选错了）。
4. **档位名不要和境界名重复**，也不要出现「宗师」「大佬」这种口语。
5. **不要照抄**常见修仙体系的名字 —— 用书里自己的叫法，除非用户明确要那种。

## 输出 JSON（只输出 JSON）

{"体系": [{"名称": "凝气", "说明": "本书的修行体系", "类型": "细分型|序列型|粗放型", "境界": [
  {"名称": "引气", "说明": "能引外气入体，延寿十年；此境之人多为外门弟子",
   "档位": "初入",
   "小境界": [{"名称": "引气一层", "说明": "刚能引气入体"}]},
  {"名称": "筑基", "说明": "气凝成基，可御物；此境可任一方长老", "档位": "初入"},
  {"名称": "金丹", "说明": "结丹驻颜，寿五百；可开宗立派", "档位": "小成"}
]}]}
"""


def build_system_prompt(genre: str, tone: str, note: str = "") -> str:
    """构造体系生成提示词。"""
    extra = ""
    if note.strip():
        extra = f"\n\n## 作者的要求\n{note.strip()[:600]}"
    return (
        f"## 这本书的设定\n\n"
        f"- 类型：{genre or '（未指定）'}\n"
        f"- 调子：{tone or '（未指定）'}"
        f"{extra}\n\n"
        f"按上述要求给出这一套体系。"
    )


def parse_systems(raw: Any) -> list[dict]:
    """解析体系生成结果。

    **兼容 Markdown 表格**（实测真实模型常这么回，见 `_parse_systems_from_table`）。
    """
    data = _safe_json(raw)
    if isinstance(data, dict):
        raw_systems = data.get("体系") or data.get("systems") or []
        if isinstance(raw_systems, dict):
            raw_systems = [raw_systems]
        systems = normalize_power_system(raw_systems)["体系"]
        if systems:
            return _consolidate_all(systems)
    # JSON 拿不到 ⇒ 试 Markdown 表格
    systems = _parse_systems_from_table(str(raw or ""))
    # 表格解析没有「类型」字段 ⇒ 从内容推断（粗放型的标志）
    _text = str(raw or "")
    for s in systems:
        if not s.get("类型"):
            s["类型"] = _infer_system_type(s, _text)
    return _consolidate_all(systems)


#: 粗放型的典型大阶段名（模型不给「类型」时用它推断）。
#:
#: 实测：模型回 Markdown 表格时**常常不带「类型」字段**，
#: 但「凡人 / 修行者 / 圣人 / 神明」这种命名就是粗放型的标志。
_SOFT_REALM_HINTS = (
    "凡人", "修行者", "圣人", "神明", "普通人", "超凡者", "半神", "神祇",
    "初民", "异能者", "修炼者", "修行士",
)


def _infer_system_type(system: dict, source_text: str = "") -> str:
    """模型没给「类型」时，从内容推断。"""
    from app.generation.power_system_type import normalize_system_type

    given = normalize_system_type((system or {}).get("类型"))
    if (system or {}).get("类型"):
        return given
    # 有「内部跨度」列 ⇒ 模型在说「同阶段内差距可能很大」⇒ 粗放型
    for realm in (system or {}).get("境界") or []:
        if realm.get("内部跨度"):
            return "粗放型"
    # 境界名全是「凡人/修行者/…」这类大阶段 ⇒ 粗放型
    names = [str(r.get("名称") or "") for r in (system or {}).get("境界") or []]
    if names and all(any(h in n for h in _SOFT_REALM_HINTS) for n in names):
        return "粗放型"
    # 正文里明说「不细分 / 边界模糊 / 不一定均匀」⇒ 粗放型
    if source_text and any(k in source_text for k in
                            ("不细分", "边界模糊", "不一定均匀", "不能假设等差",
                             "跨度", "阶段只决定")):
        return "粗放型"
    return given


def _consolidate_all(systems: list[dict]) -> list[dict]:
    """解析完立刻整理（按类型清理 + 归纳档位）。"""
    if not systems:
        return systems
    from app.generation.power_system import consolidate_bands
    from app.generation.power_system_type import normalize_system_type

    out = []
    for s in systems:
        t = normalize_system_type(s.get("类型"))
        s["类型"] = t
        # 非细分型不该有小境界（模型给了说明类型选错了 ⇒ 去掉，别让它进账）
        if t != "细分型":
            for realm in s.get("境界") or []:
                if realm.get("小境界"):
                    realm["小境界"] = []
        out.append(consolidate_bands(s))
    return out


#: 表格列名 → 我们要的字段（含多个别名，因为模型自己起名字）。
_COL_ALIASES: dict[str, tuple[str, ...]] = {
    # ⚠️ 别名必须**穷举模型真会用过的词**（实测表头是「境/江湖切口/本质/常人耗时/
    # 江湖位置」——「境」不在第一版别名里 ⇒ 整张表被丢掉，解析出 0 个体系）。
    "名称": ("关名", "境界", "境界名", "名称", "层级", "品阶", "阶", "段位", "级别",
             "境", "关口", "层", "品", "期", "重", "阶位"),
    "档位": ("档位", "档次", "层级", "分档", "阶段", "江湖位置", "位置", "地位",
             "定位", "分量"),
    "说明": ("过关标志", "练的是什么", "说明", "描述", "特征", "备注", "作用",
             "能力", "含义", "标志", "本质", "内容", "要旨", "要点",
             # —— 语义列：模型常用这些列名描述一个境界（实测）——
             "契约对象", "力量来源", "强项", "死穴", "内部跨度", "手段",
             "代价", "门槛", "象征", "修行内容", "能做什么", "限制"),
    "江湖叫法": ("江湖叫法", "江湖切口", "俗称", "又称", "别名", "切口", "叫法"),
    "内部跨度": ("内部跨度", "跨度", "内部差距"),
}


def _strip_md(s: str) -> str:
    """去掉 Markdown 强调与首尾空白。"""
    t = str(s or "").strip()
    t = t.replace("**", "").replace("`", "")
    return t.strip()


def _table_rows(text: str) -> list[list[str]]:
    """从文本里抽出 Markdown 表格的数据行（跳过表头与分隔行）。"""
    rows: list[list[str]] = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [_strip_md(c) for c in line.strip("|").split("|")]
        # 分隔行（全是 --- 或 :--:）
        if all(set(c) <= set("-: ") and c for c in cells):
            continue
        rows.append(cells)
    return rows


#: 像「序号」的列（第一列常是「第N境/第N关/一/壹/1」）。
_ORDINAL_RE = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千万零两]+\s*"
    r"[境界关口层阶段品重程番关劫途道品阶品级等级序列段]"
    r"|[0-9]{1,4}|[一二三四五六七八九十]{1,3}|[壹贰叁肆伍陆柒捌玖拾]+)$"
)

#: 名称列里可能带的序号前缀（`一境·立架` / `第一关 立架` / `1. 立架`）。
_NAME_PREFIX_RE = re.compile(
    r"^(?:第\s*[0-9一二三四五六七八九十百千万零两]+\s*"
    r"[境界关口层阶段品重程番关劫途道品阶品级等级序列段]?\s*[·・.、:：\s]*)"
    r"|^[0-9]{1,4}\s*[.、:：·\s]\s*"
    # 「一程」「二关」这种**没有分隔符**的序号写法
    r"|^[一二三四五六七八九十壹贰叁肆伍陆柒捌玖拾]{1,3}"
    r"[境界关口层阶段品重程番关劫途道序列段](?=\S|$)"
)


def _strip_ordinal_prefix(name: str) -> str:
    """去掉名称里的序号前缀（`一境·立架` → `立架`）。

    ⚠️ 剥完**为空**时不要回退原值 —— 那说明**整列就是序号**
    （实测「一程」「二程」这种写法：剥掉「一程」后没剩下名字）。
    回退的原意只是「正则没匹配上时保留原名」。
    """
    original = str(name or "").strip()
    if not original:
        return ""
    if not _NAME_PREFIX_RE.match(original):
        return original
    out = _NAME_PREFIX_RE.sub("", original, count=1).strip(" ·・.、:：")
    return out


#: 列名里含这些字 ⇒ 这列是「序号列」（程次/关次/序号/次第/序位…）。
_ORDINAL_HEADER_HINTS = ("序", "次", "第", "位次", "阶次", "层级序")


def _is_ordinal_header(text: str) -> bool:
    """这一列是序号列吗？

    两个判据：
    1. **列名**含「序/次/第」（实测表头「程次」「关次」「次第」）；
    2. **列值**像序号（第N/一/壹/1…）。
    """
    h = str(text or "").strip()
    if not h:
        return False
    if any(k in h for k in _ORDINAL_HEADER_HINTS):
        return True
    return bool(_ORDINAL_RE.match(h))


def _guess_name_column(header: list[str]) -> int | None:
    """表头认不出「名称」列时，按位置推断。

    规则：跳过**序号列**（第一列常是「第N境/程次/关次」），
    取它右边第一个**非序号**的列当名称列。
    """
    if not header:
        return None
    for i, h in enumerate(header):
        if _is_ordinal_header(h):
            for j in range(i + 1, len(header)):
                hj = (header[j] or "").strip()
                if hj and not _is_ordinal_header(hj):
                    return j
            continue
        # 第一个不像序号的列就是名称列
        return i
    return None


def _parse_systems_from_table(text: str) -> list[dict]:
    """从 Markdown 表格还原体系（真实模型常这么回）。

    认的表头列：名称/关名/境界…、档位、说明/练的是什么/过关标志…。

    形如：

        | 关 | 关名 | 江湖叫法 | 练的是什么 | 过关标志 |
        |---|---|---|---|---|
        | 一 | **立桩** | 入门把式 | 马步、三体式 | 一炷香桩架不散 |

    ⇒ `{"名称": "立桩", "说明": "马步、三体式；一炷香桩架不散"}`
    """
    rows = _table_rows(text)
    if len(rows) < 2:
        return []
    header = rows[0]
    body = rows[1:]
    # 表头行必须是「有内容的列名」，且至少认出一列名称
    idx: dict[str, int] = {}
    for field, aliases in _COL_ALIASES.items():
        for i, h in enumerate(header):
            if h and h in aliases and field not in idx:
                idx[field] = i
    if "名称" not in idx:
        # —— 兜底 1：按位置推断（别名表追不上模型的自创列名）——
        name_col = _guess_name_column(header)
        if name_col is None:
            return []
        idx["名称"] = name_col
    else:
        name_col = idx["名称"]
    realms: list[dict] = []
    for row in body:
        ni = idx.get("名称")
        if ni is None or ni >= len(row):
            continue
        name = _strip_md(row[ni])
        name = _strip_ordinal_prefix(name)
        if not name or name in ("-", "—"):
            continue
        realm: dict[str, Any] = {"名称": name}
        # 说明：**拼多个语义列**（模型常用好几列描述一个境界，
        # 实测表头是「阶段/契约对象/力量来源/强项/死穴/内部跨度」，
        # 任何单一「说明」别名都对不上）。
        desc_parts: list[str] = []
        di = idx.get("说明")
        if di is not None and di < len(row):
            d = _strip_md(row[di])
            if d:
                desc_parts.append(d)
        # 「内部跨度」单独留（粗放型的关键证据：阶段差距不一定均匀）
        for key in ("内部跨度", "门槛"):
            ki = idx.get(key)
            if ki is not None and ki < len(row):
                k = _strip_md(row[ki])
                if k:
                    realm[key] = k
                    desc_parts.append(f"{key}：{k}")
        if desc_parts:
            realm["说明"] = "；".join(desc_parts)
        bi = idx.get("档位")
        if bi is not None and bi < len(row):
            band = _strip_md(row[bi])
            # ⚠️ 档位取值 == 境界名 ⇒ 它其实是「阶段名」（实测粗放型：
            # 阶段=凡人/修行者/…）⇒ 不当档位用（档位由 consolidate 归纳）。
            if band and band != name:
                realm["档位"] = band
        realms.append(realm)
    if not realms:
        return []
    # 体系名：正文里第一行提到的「XX」当作体系名（取表格前的标题行）
    name = _extract_system_name(text)
    return [{"名称": name or "本书体系", "说明": "", "境界": realms}]


def _extract_system_name(text: str) -> str:
    """从文本里找体系名。

    认得这些形态（实测）：
      `## 体系名：四契体系` / `体系名：**九关**` / `《九程武谱》` / `九关体系`
    """
    for line in (text or "").split("\n"):
        s = line.strip()
        if not s or s.startswith("|"):
            continue
        # 「体系名：X」/「体系名：X 体系」
        m = re.search(r"体系名?\s*[：:]\s*\**\s*([^\n*。]{1,20})", s)
        if m:
            cand = m.group(1).strip(" *：:，。、")
            if cand:
                return cand
        # 「《X》」
        m = re.search(r"[「『\"《]([^」』\"》]{2,12})[」』\"》]", s)
        if m:
            return m.group(1).strip()
        # 短行且以「体系/阶/境/道/谱/诀」结尾
        plain = _strip_md(s).rstrip("。．.")
        if len(plain) <= 20 and plain.endswith(("体系", "阶", "境", "道", "谱", "诀", "录")):
            return plain
    return ""


#: 档位分层的生成提示词。
_TIER_PROMPT = """你在为一本中文小说的**群像**设计**档位分层**。

## 已有体系

{system}

## 要做的事

给这套体系配 **3~6 个「档位层」**，每一层是**一类人**（不是一个人）。

例：
- 名称：筑基期散修
- 档位：小成
- 人数：数十
- 特征：独来独往，忌惮同门，见了宗门弟子先避

规则：
1. **档位必须取自体系里真实存在的档位**（{bands}），不能新造；
2. 每层的「人数」写量级（数人/数十/数百/上千），不要精确数字；
3. 「特征」写**一眼能认出来的行为特征**，不要写「实力强劲」这种空话；
4. 层与层之间要有**递进关系**（人多但弱 → 人少但强）。

## 输出 JSON

{"档位层": [{"名称": "...", "档位": "...", "人数": "...", "特征": "..."}]}
"""


def build_tier_prompt(system: dict) -> str:
    """构造档位分层提示词。"""
    bands = bands_of(system)
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
        ladder.append(seg)
    return _TIER_PROMPT.format(
        system="\n".join(f"- {x}" for x in ladder) or "（未提供）",
        bands="、".join(bands) or "（体系里没给档位，先用境界名）",
    )


def parse_tiers(raw: Any) -> list[dict]:
    """解析档位分层结果，并**校验档位确实存在于体系里**。

    校验不过的层**丢掉**（AI 凭空造的档位名不能进账 —— 否则分层群像
    会出现「体系里没有的档次」，写作时就没法对照）。
    """
    data = _safe_json(raw)
    if not isinstance(data, dict):
        return []
    raw_tiers = data.get("档位层") or data.get("tiers") or []
    if isinstance(raw_tiers, dict):
        raw_tiers = [raw_tiers]
    out = normalize_tiers(raw_tiers)
    return out


def filter_tiers_by_bands(tiers: list[dict], system: dict) -> list[dict]:
    """只保留档位确实存在于体系里的层。"""
    bands = set(bands_of(system))
    if not bands:
        return tiers
    return [t for t in tiers if not t.get("档位") or t["档位"] in bands]


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    try:
        return json.loads(cleaned)
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:  # noqa: BLE001
            return {}
    return {}
