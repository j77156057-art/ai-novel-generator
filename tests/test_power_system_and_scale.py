"""修炼体系 / 量级标尺（2026-10-29）。

## 为什么要有这两个模块

用户诉求：

> 「如果涉及到武侠的，或者是修仙的……那么这个要不要做去角色卡，
>  那么主角目前的境界，以后的境界，还有中途的修炼等级，
>  还有其他各式各样配角和路人甲的等级……
>  角色有什么修炼天赋……修炼遇到了叉，掉了境界，或者为了活命燃烧了寿命，
>  或者是去了什么洞府得到了提升这种动态的路线，怎么规划」

> 「世界观里有神明……举手投足可以破坏一个城市甚至一个星球那种角色的感觉，
>  但生成出来的就和路边混混斗殴一样」

## 实测（发布版冒烟库真实提示词）

角色约束块里「这一击能有多大」类信号词 **0 次**（三个项目都是）——
写满了「失落神女」「窃取太阳权柄的旧神阵营首领」这些**身份描述**，
却没有一个字说明**他出手是什么规模**。
⇒ 模型只能按它最熟悉的物理尺度（刀/门板/石阶）去写。

**身份描述 ≠ 量级**：模型读到「神女」不知道该写多大，
读到「抬手能让半座雾港沉进海里」才知道。

## A/B 对照（真实模型，同世界观同场景，只差一个量级块）

| | 高量级动作 |
|---|---|
| A（现状） | **0 次** |
| B（带量级对照） | **7 次**（沉进/化成/倒卷/退开/往上提…） |

B 组自发写出「她能烧我一里，我就退一里。可她站的地方，只有一里」
——**量级差进了对话**。

## 本文件钉住的口径

- **体系可配**：体系 → 境界 → 小境界，每层带说明与档位；
- **分层群像**：路人只记档位（「筑基期散修」），主角与主要配角才逐个精确记；
- **路线只作建议**：AI 规划，人点采纳；
- **量级是相对标尺**：五档（凡人/伤人/毁物/毁城/灭世）+ 具体的「出手规模」；
- **只给同场的人**，并**成对比较**（谁压谁）；
- **没有量级数据 ⇒ 提示词逐字节不变**。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.generation.character_card import normalize_card  # noqa: E402
from app.generation.cultivation import (  # noqa: E402
    derive_situation, normalize_profile, normalize_tier, parse_route,
)
from app.generation.power_scale import (  # noqa: E402
    SCALE_LEVELS, compare_scales, default_scale_for_realm, render_scale_contrast,
    resolve_scale, scale_level,
)
from app.generation.power_scale_gen import parse_scale  # noqa: E402
from app.generation.power_system import (  # noqa: E402
    bands_of, consolidate_bands, find_realm, normalize_power_system,
    realm_index, realm_rank, render_character_realm, render_system, stage_rank,
)
from app.generation.power_system_gen import (  # noqa: E402
    filter_tiers_by_bands, parse_systems, parse_tiers,
)
from app.generation.power_system_type import (  # noqa: E402
    SYSTEM_TYPES, is_soft, normalize_system_type, realm_order_trusted,
)


# ======================================================================
# 1. 体系：可配 + 小境界 + 档位
# ======================================================================

def test_体系三层结构():
    ps = normalize_power_system({"体系": [{
        "名称": "凝气",
        "境界": [
            {"名称": "引气", "说明": "能引外气入体", "档位": "初入",
             "小境界": [{"名称": "引气一层", "说明": "刚能引气"}]},
            {"名称": "筑基", "说明": "气凝成基", "档位": "小成"},
        ]}]})
    sysx = ps["体系"][0]
    assert sysx["名称"] == "凝气"
    realms = sysx["境界"]
    assert len(realms) == 2
    assert realms[0]["档位"] == "初入"
    assert realms[0]["小境界"][0]["名称"] == "引气一层"


def test_小境界能查到所属境界():
    ps = normalize_power_system({"体系": [{
        "名称": "凝气",
        "境界": [{"名称": "引气", "档位": "初入",
                  "小境界": [{"名称": "引气三层", "说明": ""}]}]}]})
    sysx = ps["体系"][0]
    hit = find_realm([sysx], "引气三层")
    assert hit is not None, hit
    assert hit["所属境界"] == "引气"
    assert realm_rank(sysx, "引气三层") == 0
    assert stage_rank(sysx, "引气三层") == 0


def test_档位只取体系里真有的():
    sysx = {"境界": [
        {"名称": "引气", "档位": "初入"}, {"名称": "筑基", "档位": "小成"}]}
    tiers = [{"名称": "散修", "档位": "初入"}, {"名称": "不存在", "档位": "宗师"}]
    kept = filter_tiers_by_bands(tiers, sysx)
    assert [t["名称"] for t in kept] == ["散修"], kept


def test_档位过多时归纳成少数几档():
    """实测：模型把「江湖位置」填进档位列 ⇒ 每境一个不同的长句。"""
    sysx = {"境界": [
        {"名称": f"第{i}境", "档位": f"某某身份{i}", "说明": "很强"} for i in range(1, 8)]}
    out = consolidate_bands(sysx)
    bands = bands_of(out)
    assert len(bands) <= 6, bands
    # 位置描述并进说明，没有丢
    assert "某某身份1" in out["境界"][0]["说明"]
    # 档位名是人话，不是「第1档」
    assert all(not b.startswith("第") or "档" not in b for b in bands), bands


def test_体系渲染():
    sysx = normalize_power_system({"体系": [{
        "名称": "凝气", "说明": "本书体系",
        "境界": [{"名称": "引气", "说明": "延寿十年", "档位": "初入"}]}]})["体系"][0]
    text = render_system(sysx)
    assert "凝气" in text and "引气" in text and "延寿十年" in text


# ======================================================================
# 2. 修炼档案：路线 / 代价 / 处境
# ======================================================================

def test_修炼档案规整():
    p = normalize_profile({
        "当前境界": "引气", "天赋": "筋骨好", "瓶颈": "经脉旧伤",
        "路线": ["炼气三层 → 炼气四层：苦修三年"], "代价": "服过压境界的丹药",
    })
    assert p["当前境界"] == "引气"
    assert p["路线"] == ["炼气三层 → 炼气四层：苦修三年"]
    assert p["代价"].startswith("服过")


def test_路线解析():
    r = parse_route({"路线": ["a", "b"], "目标境界": "筑基", "瓶颈": "经脉",
                      "代价": "燃烧寿命", "无关": "x"})
    assert r["目标境界"] == "筑基"
    assert r["路线"] == ["a", "b"]
    assert "无关" not in r


def test_处境由规则推导_不问模型():
    """用户问的：「提升慢会怎样、快会怎样」⇒ 只给**倾向**，不给台词。"""
    systems = normalize_power_system({"体系": [{
        "名称": "X", "境界": [
            {"名称": "低阶", "档位": "初入"},
            {"名称": "高阶", "档位": "小成"}]}]})["体系"]
    prof = {"当前境界": "高阶", "瓶颈": "经脉承受不住",
            "代价": "服过压境界的丹药", "目标境界": "更高"}
    audience = [{"名字": "路人", "当前境界": "低阶"}]
    s = derive_situation(prof, systems, audience=audience)
    assert "强于路人" in s, s
    assert "卡在瓶颈" in s, s
    assert "身体会先垮" in s, s


def test_处境_同体系内不同档位可比():
    """⚠️ 同体系内不同档位（初入 < 小成）**是**可比的高下。

    之前判成「不可比」是实现 bug：那会让「谁压谁」永远不出现，
    处境推导就只剩与强弱无关的信息。
    """
    systems = normalize_power_system({"体系": [{
        "名称": "X", "境界": [
            {"名称": "甲阶", "档位": "初入"},
            {"名称": "乙阶", "档位": "超凡"}]}]})["体系"]
    prof = {"当前境界": "乙阶"}
    audience = [{"名字": "路人", "当前境界": "甲阶"}]
    s = derive_situation(prof, systems, audience=audience)
    assert "强于路人" in s, s


def test_处境_找不到境界就不比较():
    """境界名不在体系里 ⇒ 不可比 ⇒ 不下结论（宁可少判不可误报）。"""
    systems = normalize_power_system({"体系": [{
        "名称": "X", "境界": [{"名称": "甲阶", "档位": "初入"}]}]})["体系"]
    prof = {"当前境界": "不在体系里的境界"}
    audience = [{"名字": "路人", "当前境界": "甲阶"}]
    s = derive_situation(prof, systems, audience=audience)
    assert "强于" not in s and "弱于" not in s, s


def test_档位分层规整():
    t = normalize_tier({"名称": "筑基期散修", "档位": "小成", "人数": "数十",
                         "特征": "独来独往"})
    assert t["名称"] == "筑基期散修"
    assert t["人数"] == "数十"


# ======================================================================
# 3. 量级标尺（解决「神明之战像街头斗殴」）
# ======================================================================

def test_量级档位固定五档():
    assert len(SCALE_LEVELS) == 5
    assert "灭世级" in SCALE_LEVELS and "凡人级" in SCALE_LEVELS


def test_量级解析_认不出档位就丢掉():
    sc = parse_scale('{"量级": "毁城级", "出手规模": "抬手能让半座城沉进海里"}')
    assert sc["量级"] == "毁城级"
    assert sc["出手规模"]
    # 模型自造的档位不能进账（横向比较要用同一把尺）
    sc2 = parse_scale('{"量级": "神级", "出手规模": "很厉害"}')
    assert "量级" not in sc2, sc2


def test_量级比较():
    assert compare_scales({"量级": "毁城级"}, {"量级": "凡人级"}) == 1
    assert compare_scales({"量级": "凡人级"}, {"量级": "毁城级"}) == -1
    assert compare_scales({"量级": "凡人级"}, {"量级": "凡人级"}) == 0
    assert compare_scales({"量级": "未知"}, {"量级": "凡人级"}) is None


def test_按境界推定量级():
    systems = normalize_power_system({"体系": [{
        "名称": "X", "境界": [
            {"名称": "凡", "档位": "初入"},
            {"名称": "中", "档位": "小成"},
            {"名称": "高", "档位": "登堂"}]}]})["体系"]
    assert default_scale_for_realm("凡", systems) == SCALE_LEVELS[0]
    assert default_scale_for_realm("高", systems) is not None


def test_卡里的量级优先于按境界推定():
    systems = normalize_power_system({"体系": [{
        "名称": "X", "境界": [{"名称": "高", "档位": "登堂"}]}]})["体系"]
    card = {"修炼": {"当前境界": "高"}, "量级": {"量级": "凡人级"}}
    got = resolve_scale(card, systems)
    assert got["量级"] == "凡人级"


def test_量级对照块_只给在场的人且带对比():
    casts = [
        {"名字": "沉日者", "身份": "旧神",
         "card": {"量级": {"量级": "灭城级", "出手规模": "抬手能让半座城沉进海里"}}},
        {"名字": "顾星河", "身份": "点灯人",
         "card": {"量级": {"量级": "毁物级", "出手规模": "能压住一盏不灭的火"}}},
        {"名字": "白烬", "身份": "无面亡者",
         "card": {"量级": {"量级": "凡人级", "出手规模": "被黑刃擦到就会散"}}},
        {"名字": "路人", "身份": "船工", "card": {}},
    ]
    text = render_scale_contrast(casts)
    assert "沉日者" in text and "白烬" in text
    assert "路人" not in text, text        # 没有量级的路人不进块
    assert "远强于" in text, text
    assert "挡不住" in text, text


def test_没有量级就整块不出现():
    casts = [{"名字": "甲", "身份": "船工", "card": {}}]
    assert render_scale_contrast(casts) == ""


def test_量级组不会被规整丢掉():
    """实测踩过两次：量级是纯分组，摊平后组装不出来。"""
    card = normalize_card({"name": "甲", "量级": {
        "量级": "毁城级", "出手规模": "抬手能让半座城沉进海里", "压制": "压得住灯火"}})
    assert card.get("量级", {}).get("量级") == "毁城级"
    assert card["量级"]["出手规模"] == "抬手能让半座城沉进海里"


def test_量级字符串形态也认():
    card = normalize_card({"name": "甲", "量级": "毁城级"})
    assert card.get("量级", {}).get("量级") == "毁城级"


# ======================================================================
# 4. 解析：真实模型会回 Markdown 表格（实测）
# ======================================================================

def test_体系能从markdown表格解析():
    raw = """体系名：**九关**。九关是火候。

| 境 | 江湖切口 | 本质 | 常人耗时 | 江湖位置 |
|---|---|---|---|---|
| 立架 | 有根了 | 把身子练成架子 | 三年打底 | 镖局趟子手 |
| 易筋 | 换过筋了 | 换掉拙力 | 五年到二十年 | 香主、教头 |
"""
    systems = parse_systems(raw)
    assert len(systems) == 1, systems
    realms = systems[0]["境界"]
    assert len(realms) == 2, realms
    assert realms[0]["名称"] == "立架"
    assert "架子" in realms[0]["说明"]


def test_表格列名别名要全():
    """实测表头是「境/江湖切口/本质…」⇒ 别名少一个就整张表被丢。"""
    raw = """| 程次 | 名目 | 核心火候 | 验法 |
|---|---|---|---|
| 一程 | 开架 | 桩稳 | 站一炷香 |
| 二程 | 换力 | 力从地起 | 推手不顶不丢 |
"""
    systems = parse_systems(raw)
    assert len(systems) == 1, systems
    assert [r["名称"] for r in systems[0]["境界"]] == ["开架", "换力"]


def test_档位分层解析():
    tiers = parse_tiers('{"档位层": [{"名称":"散修","档位":"初入","人数":"数十","特征":"独行"}]}')
    assert tiers and tiers[0]["名称"] == "散修"


# ======================================================================
# 5. 体系类型：细分型 / 序列型 / 粗放型（用户 2026-10-29 追加）
# ======================================================================
#
# 用户的三类：
#   第一类「境界分明清晰」：什么境界、第几重、初期/中期/大圆满
#   第二类「相对没这么清晰」：诡秘式，有境界但不细分
#   第三类「基本没划定」：凡人→修行者→圣人→神明，阶段差距可能很大也可能很小


def test_三类体系():
    assert SYSTEM_TYPES == ("细分型", "序列型", "粗放型")


def test_类型规整():
    assert normalize_system_type("细分型") == "细分型"
    assert normalize_system_type("序列") == "序列型"
    assert normalize_system_type("粗放") == "粗放型"
    assert normalize_system_type(None) == "序列型"     # 默认最中性的一种
    assert normalize_system_type("胡说八道") == "序列型"


def test_默认选序列型而不是细分型():
    """细分型会**假设**有初期/中期；序列型只假设「有境界但没细分」
    ⇒ 假设更少 ⇒ 更安全。"""
    assert normalize_system_type("") == "序列型"


def test_粗放型_序数不可信():
    """凡人→修行者→圣人→神明：后一档可能差距**更小**
    ⇒ 不能按序数判强弱。"""
    soft = {"名称": "X", "类型": "粗放型",
            "境界": [{"名称": "修行者", "档位": "初入"},
                     {"名称": "圣人", "档位": "登堂"}]}
    assert is_soft(soft) is True
    assert realm_order_trusted(soft) is False


def test_细分型与序列型_序数可信():
    for t in ("细分型", "序列型"):
        s = {"名称": "X", "类型": t,
             "境界": [{"名称": "低", "档位": "初入"},
                     {"名称": "高", "档位": "登堂"}]}
        assert is_soft(s) is False
        assert realm_order_trusted(s) is True


def test_粗放型_不按序数比较高低():
    """粗放型比较返回 None（不可比）⇒ 只能用量级比。"""
    from app.generation.power_system import compare_realms

    systems = normalize_power_system({"体系": [{
        "名称": "X", "类型": "粗放型",
        "境界": [{"名称": "修行者", "档位": "初入"},
                 {"名称": "圣人", "档位": "登堂"}]}]})["体系"]
    assert compare_realms({"当前境界": "圣人"},
                          {"当前境界": "修行者"}, systems) is None


def test_细分型与序列型_按序数比高低():
    from app.generation.power_system import compare_realms

    for t in ("细分型", "序列型"):
        systems = normalize_power_system({"体系": [{
            "名称": "X", "类型": t,
            "境界": [{"名称": "低", "档位": "初入"},
                     {"名称": "高", "档位": "登堂"}]}]})["体系"]
        assert compare_realms({"当前境界": "高"},
                              {"当前境界": "低"}, systems) == 1


def test_量纲看体系不看档位():
    """⚠️ 概念混淆修过：档位是**横向分层**，同一体系里多境可同档
    （炼气与筑基可能都算「初入」）⇒ 用它当量纲会把正常比较全判成不可比。"""
    from app.generation.power_system import compare_realms

    systems = normalize_power_system({"体系": [{
        "名称": "X", "类型": "细分型",
        "境界": [{"名称": "炼气", "档位": "初入"},
                 {"名称": "筑基", "档位": "初入"},   # 同档！
                 {"名称": "金丹", "档位": "登堂"}]}]})["体系"]
    assert compare_realms({"当前境界": "金丹"},
                          {"当前境界": "炼气"}, systems) == 1


def test_非细分型不给小境界():
    """模型给粗放型加了小境界 ⇒ 去掉（说明类型选错了，别让它进账）。"""
    systems = parse_systems(
        '{"体系": [{"名称": "X", "类型": "粗放型", "境界": '
        '[{"名称": "修行者", "小境界": [{"名称": "初"}]}]}]}')
    assert systems
    assert systems[0]["类型"] == "粗放型"
    assert systems[0]["境界"][0].get("小境界") == []


def test_细分型保留小境界():
    systems = parse_systems(
        '{"体系": [{"名称": "X", "类型": "细分型", "境界": '
        '[{"名称": "炼气", "小境界": [{"名称": "炼气一层"}]}]}]}')
    assert systems
    assert systems[0]["类型"] == "细分型"
    assert systems[0]["境界"][0]["小境界"][0]["名称"] == "炼气一层"


def test_体系渲染带类型说明():
    from app.generation.power_system import render_system

    sysx = normalize_power_system({"体系": [{
        "名称": "X", "类型": "粗放型",
        "境界": [{"名称": "修行者", "档位": "初入"}]}]})["体系"][0]
    text = render_system(sysx)
    assert "粗放型" in text
    assert "边界模糊" in text
