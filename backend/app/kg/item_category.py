# -*- coding: utf-8 -*-
"""非物品类名（功法 / 生物类称 / 关系概念）的人工标记表。

为什么不做形态判据
------------------
这三类词与物品写法在字面上重合，只靠形态无法可靠区分，硬写规则会误伤：

- **功法 vs 承载物**：``《天地一刀斩》`` 在本库里是「刀道绝学残篇」（物品，且已明确
  要求保留），``狮子吼`` 是佛门功法。两者都带招式后缀，形态完全一致，差别只在
  正文里指的是招式还是那本册子/那张图。
- **生物类称 vs 专名坐骑**：``灵龙`` 是元景帝水陆两栖的坐骑（保留 item），
  ``怪物``/``灵兽``/``此兽`` 是类称。构词都是「修饰 + 兽/龙」，没有可分离的标记。
- **关系概念 vs 实物**：``信物`` 可以指玉佩、地书、脚环、菩萨信物；它描述的是
  「关系中的凭证」这一角色，不对应某种材质或器型，没有形态特征。

因此这里只提供**封闭的手工标记表**。新增条目必须写清类别与证据（正文里指什么、
为什么不是单件实物），不允许按后缀推断，也不允许把专名塞进来。

另有一张同性质的封闭表 ``DESCRIPTIVE_SURFACES``：**仍然指一件真物品**、但只是
「修饰语 + 器型/类称」的临场描述（``水中坐骑``/``法器军弩``）。它不进
``NON_ITEM_NAMES``——那些写法指的就是同一件实物，只是不该当成稳定名字登记。
形态判据同样覆盖不了：同一形态在本库里就是真物品名（``半块玉佩``/``法器铜锣``/
``大钢刀``/``银枪``），按形态降级会连它们一起误伤。

``暗金色长刀`` 曾登记在这张表里，复核后确认它是那把刀在 ch843/919/926 的唯一写法
（全书 4 处只指监正 ch93 锻造、后命名「太平」的同一件），已从表中移出并恢复
``approved``——留着会让 ``--step 4`` 再把它降一次，见 reports 对应章节。
"""
from __future__ import annotations

TECHNIQUE = "technique"
CREATURE_CLASS = "creature_class"
RELATION_CONCEPT = "relation_concept"

CATEGORY_LABELS: dict[str, str] = {
    TECHNIQUE: "功法",
    CREATURE_CLASS: "生物类称",
    RELATION_CONCEPT: "关系概念",
}

#: 名字 -> (类别, 证据)。只登记「不是单件物理对象」的写法。
NON_ITEM_NAMES: dict[str, tuple[str, str]] = {
    "狮子吼": (
        TECHNIQUE,
        "佛门功法；26 章 37 次都指招式本身，不是 gid30 那张《金色雄狮咆哮图》",
    ),
    "怪物": (
        CREATURE_CLASS,
        "生物类称；82 章分别指蝾螈、水怪、神殊等不同对象，不是 gid20 的灵龙",
    ),
    "信物": (
        RELATION_CONCEPT,
        "关系概念；分别指玉佩、地书、脚环、法济菩萨信物等不同物件，不是 gid7 的半块玉佩",
    ),
}


def category_of(name: str) -> str | None:
    """返回非物品类别；未登记返回 ``None``。"""
    hit = NON_ITEM_NAMES.get((name or "").strip())
    return hit[0] if hit else None


def reason_of(name: str) -> str | None:
    hit = NON_ITEM_NAMES.get((name or "").strip())
    return hit[1] if hit else None


def is_non_item(name: str) -> bool:
    return category_of(name) is not None


#: 描述性写法 -> (所属组规范名, 证据)。这些写法**仍然指一件真物品**，只是
#: 「修饰语 + 器型/类称」的临场描述／绰号，不是这件物品的稳定名字。
#: 每条都要写清「它指什么」和「摘了会不会丢定位」；不允许凭「读起来像描述」扩充。
DESCRIPTIVE_SURFACES: dict[str, tuple[str, str]] = {
    "水中坐骑": (
        "灵龙",
        "功能描述（水中 + 坐骑）；ch121/128 都紧跟「灵龙」做同位语"
        "（「此兽唤做灵龙……人皇的水中坐骑」），2 章均与规范名同章，摘除不丢覆盖",
    ),
    "修长战刀": (
        "黑金长刀",
        "外观描述（修长 + 战刀）；ch199 唯一一次是不定指"
        "（「一口特殊的，与制式佩刀不同的修长战刀」），同章有「黑金长刀」，摘除不丢覆盖",
    ),
    "白马银枪": (
        "银枪",
        "绰号式描述（白马 + 银枪）；3 章都在写李妙真的形象（人是主语，枪只是伴随物），"
        "ch211/370/380 均与「银枪」同章，摘除不丢覆盖",
    ),
    "法器军弩": (
        "军弩",
        "来源描述（法器 + 军弩）；ch253 明说「制式武器，司天监的法器军弩」，"
        "是装备通称而非这把弩的名字；3 章均与「军弩」同章，摘除不丢覆盖",
    ),
}


def descriptive_group(name: str) -> str | None:
    """描述性写法所属的组规范名；未登记返回 ``None``。"""
    hit = DESCRIPTIVE_SURFACES.get((name or "").strip())
    return hit[0] if hit else None


def descriptive_reason(name: str) -> str | None:
    hit = DESCRIPTIVE_SURFACES.get((name or "").strip())
    return hit[1] if hit else None


def is_descriptive(name: str) -> bool:
    return descriptive_group(name) is not None
