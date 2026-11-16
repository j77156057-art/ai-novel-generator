"""派生档位：表格里没有真档位列时，按境界派生出少数几档（2026-10-29）。

## 取证（真机录制的模型原文，不随仓库分发）

模型回 Markdown 表格，表头是：

    阶段 | 契约对象 | 力量来源 | 强项 | 死穴 | 内部跨度

`_COL_ALIASES["档位"]` 里收了「阶段」⇒ 第一列既被当境界名、又被当档位；
随后「档位值 == 境界名 ⇒ 不当档位」的守卫把它丢掉，`bands_of()` 返回**空**。

后果：档位是分层群像的抓手（路人甲按档归类、不逐个建档），空 ⇒ 抓手落空；
`filter_tiers_by_bands` 也退化成「什么都放行」，AI 凭空造的档次能进账。

## 修法（两条都通用，不为某一篇打补丁）

1. 表格解析**先定名称列，再挑其它列**，并**排除名称列** —— 同一列不该身兼两职；
2. 一个档位都没有时，按境界顺序**派生** `min(MAX_BANDS, N)` 档，档位名取中性闭集。

   纪律：派生只做**分层**，不做**强弱比较** —— 粗放型照样不参与序数比较
   （`realm_rank` 仍返 None）；模型给了档位就一个字都不改。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.generation.power_system import (  # noqa: E402
    MAX_BANDS, _FALLBACK_BAND_NAMES, bands_of, realm_rank,
)
from app.generation.power_system_gen import parse_systems  # noqa: E402

#: 真机录下来的那种表头（第一列是「阶段」，值是境界名）
SOFT_TABLE = """## 体系名：四契体系

四阶不是四层楼，而是四种“与世界立约”的方式。**阶段只决定权限类型，不决定战力倍数。**

| 阶段 | 契约对象 | 力量来源 | 强项 | 死穴 | 内部跨度 |
|---|---|---|---|---|---|
| 凡人 | 血肉、技艺、组织 | 体魄、工具、阵法 | 数量、组织、地理 | 寿命短 | 农夫到破城武宗，差距可极大 |
| 修行者 | 法门、灵机 | 术法、法器、丹药 | 术法 | 寿元有耗 | 初入与顶尖差距可极大 |
| 圣人 | 天地、律令 | 借天地之势 | 律令 | 受约束 | 同阶可分高下 |
| 神明 | 因果、权柄 | 直接改规则 | 权柄 | 被更大权柄压 | 差距极大 |
"""


def test_表格的档位列不和境界名抢同一列():
    """「阶段」列是境界名，不能同时被当成档位。"""
    systems = parse_systems(SOFT_TABLE)
    assert len(systems) == 1, systems
    realms = systems[0]["境界"]
    assert [r["名称"] for r in realms] == ["凡人", "修行者", "圣人", "神明"]
    for r in realms:
        assert r.get("档位") != r["名称"], r


def test_没有档位时按境界派生():
    """表格里没有真档位列 ⇒ 派生，别让分层抓手落空。"""
    systems = parse_systems(SOFT_TABLE)
    bands = bands_of(systems[0])
    assert bands, "派生后档位不能为空"
    assert 2 <= len(bands) <= MAX_BANDS, bands
    assert all(r.get("档位") for r in systems[0]["境界"]), systems[0]["境界"]


def test_派生档位名来自中性闭集():
    """派生的是**等级词**，不是机器名（「第1档」会被原样渲染进提示词）。"""
    bands = bands_of(parse_systems(SOFT_TABLE)[0])
    assert set(bands) <= set(_FALLBACK_BAND_NAMES), bands


def test_派生档位不改粗放型的序数不可信():
    """派生只做分层，不做强弱比较 —— 粗放型仍不参与序数比较。

    注意用的是 `compare_realms`（带类型守卫的那个），不是 `realm_rank`
    —— 后者是**原始下标**，本来就该照实返回位置。
    """
    from app.generation.power_system import compare_realms
    from app.generation.power_system_type import realm_order_trusted

    s = parse_systems(SOFT_TABLE)[0]
    assert s["类型"] == "粗放型"
    assert realm_order_trusted(s) is False
    assert compare_realms({"当前境界": "圣人"}, {"当前境界": "凡人"}, [s]) is None


def test_模型给了档位就一个字都不改():
    """有档位时不许覆盖 —— 派生只是兜底。"""
    raw = """{"体系": [{"名称": "九境", "说明": "修仙", "类型": "细分型", "境界": [
      {"名称": "炼气", "说明": "引气入体", "档位": "初入"},
      {"名称": "筑基", "说明": "气凝成基", "档位": "小成"},
      {"名称": "金丹", "说明": "结丹驻颜", "档位": "一方"}]}]}"""
    s = parse_systems(raw)[0]
    assert [r["档位"] for r in s["境界"]] == ["初入", "小成", "一方"]
