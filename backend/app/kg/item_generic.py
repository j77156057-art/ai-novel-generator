# -*- coding: utf-8 -*-
"""泛称（类名）判据：只看数据形态，不依赖某本书的具体物品名。

为什么单独成模块：泛称混入不是「某几个名字写错了」，而是登记入口没有拦截。
这里给出一组可迁移的判据，迁移脚本、写法登记入口、扫描报告共用同一套实现，
避免再往 ``ITEM_ALIASES`` 或任何硬编码名单里补条目。

判据（每条都能从数据算出来）：

- ``R1_substring``：写法是另一个已登记写法/规范名的真子串，且不是自己规范名
  的缩写（``铜剑`` ⊂ ``黄铜剑``；``黑金刀`` ⊂ ``黑金长刀`` 属缩写，不算）。
- ``R2_cross_group``：同一写法挂在多个实体/组的 aliases 上。
- ``R3_coverage``：写法在正文出现的章数远高于所属组规范名（≥2 倍且 ≥5 章），
  需要正文统计，由调用方传入章数。
- ``R4_no_proper_noun``：整串能拆成「材质词 + 器型词 / 类别词 / 通用修饰词」，
  没有专名残留（``铜剑``、``神兵``、``长刀``）。
- ``R5_multi_holder``：同一写法在持有段里对应多个互无交接的持有者。
- ``R6_reference_phrase``：以指示代词/人称开头、带「的」、或含「数量 + 量词」的
  描述短语（``此兽``/``许七安的佩刀``/``腰间一把军弩``）。这类是正文里的指代，
  不是名字。**只用于写法层**，组规范名不走这条（``周旻的账簿`` 不受影响）。

置信分层：

- ``high``：纯类别词（``神兵``/``法宝``），或「单字裸材质 + 器型」（``铜剑``）；
  登记入口直接拒绝登成 approved 单件写法。
- ``medium``：多字材质/合金 + 器型（``黄铜剑``/``玉石小镜``）。形态上像类名，
  但正文可能被当作唯一指代使用，只报告、不自动拦截，交人工判断。

另有人工核定白名单 ``CANONICAL_WHITELIST``：只对**组规范名**生效，记录「形态像
类名、但正文只有类称且持有段全部指向同一实物」的已裁定项。白名单不是新判据，
也不允许凭形态扩充——每条都必须能复查「正文无专名」和「持有行对应唯一实物」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HIGH = "high"
MEDIUM = "medium"
NONE = "none"

R1 = "R1_substring"
R2 = "R2_cross_group"
R3 = "R3_coverage"
R4 = "R4_no_proper_noun"
R5 = "R5_multi_holder"
R6 = "R6_reference_phrase"

#: 材质词。多字在前，保证「青铜/黄铜/黑金」不会被拆成单字。
MATERIAL_WORDS = frozenset({
    "青铜", "黄铜", "黑金", "玄铁", "精铁", "生铁", "熟铁", "白银", "黄金",
    "紫金", "乌金", "赤金", "玉石", "翡翠", "珊瑚", "沉香", "檀木", "桃木",
    "铜", "铁", "钢", "金", "银", "玉", "石", "骨", "角", "木", "竹",
    "皮", "布", "纸", "陶", "瓷", "冰",
})

#: 器型词。长的在前，避免「长剑」被拆成「长」+「剑」。
TYPE_WORDS = frozenset({
    "长剑", "短剑", "宝剑", "神剑", "飞剑", "重剑", "软剑", "木剑",
    "长刀", "短刀", "宝刀", "大刀", "小刀", "飞刀", "戒刀",
    "长枪", "短枪", "银枪", "铁枪", "长棍", "齐眉棍",
    "鞭", "锤", "锏", "戟", "斧", "钺", "钩", "叉", "戈", "矛", "盾",
    "弓", "弩", "箭", "甲", "盔", "靴", "袍", "镜", "锣", "铃", "钟",
    "鼎", "炉", "塔", "幡", "旗", "令", "符", "印", "珠", "袋", "囊",
    "伞", "扇", "琴", "笛", "箫", "笔", "尺", "针", "线", "绳", "灯",
    "瓶", "碗", "钵", "葫芦", "剑", "刀", "枪", "棍",
})

#: 类别词：本身就是「一类东西」的说法，单独出现必定是泛称。
CATEGORY_WORDS = frozenset({
    "兵", "兵器", "神兵", "法宝", "法器", "神器", "宝物", "宝器", "器", "物",
})

#: 生物类称语素：只表示「一类活物」，落在词尾时没有专名。
#: 刻意不含「龙/马/骠」——``灵龙`` 是坐骑专名，不能因为带「灵」就被当类称。
CREATURE_CLASS_WORDS = frozenset({"兽", "禽", "虫", "鱼", "鸟", "坐骑", "妖兽", "灵兽"})

#: 通用修饰词：只起「大/小/长/短/颜色/神异」作用，不构成专名。
MODIFIER_WORDS = frozenset({
    "大", "小", "长", "短", "粗", "细", "古", "新", "旧", "破",
    "黑", "白", "青", "红", "黄", "紫", "玄", "乌", "赤", "丹", "碧", "墨",
    "金", "银", "神", "仙", "圣", "宝", "法", "血", "龙", "虎", "狼", "鹰",
    "蛇", "鬼", "魔", "妖", "佛", "道", "儒", "巫",
})

#: 「裸材质」：单字材质直接加器型（铜剑/铁剑/玉镜），没有专名部分。
BARE_MATERIAL_CHARS = frozenset("铜铁钢金银玉石骨木竹")

#: 匹配层判定「材质字紧邻」用；与上面的词表解耦，单独列出常见材质/颜色字。
MATERIAL_BOUNDARY_CHARS = frozenset(
    "青铜黄赤白黑金银铁玉石骨木竹皮布纸陶瓷冰玄乌紫丹碧墨"
)

_ALL_WORDS = sorted(MATERIAL_WORDS | TYPE_WORDS | MODIFIER_WORDS | CATEGORY_WORDS
                    | CREATURE_CLASS_WORDS,
                    key=len, reverse=True)

#: 不是「名字」的写法：带指示代词/助词/标点的描述性短语（「昨日新换的铜锣」）。
_PHRASE_MARKS = frozenset("的这那此其一二三四五六七八九十他她它你们我个枚柄口把件只条面块颗")


#: 人工核定的「描述性唯一指代」组规范名。
#:
#: 这些名字形态上命中 R4，但正文里没有专名可用，且 ``kg_item_holding`` 的段
#: 全部指向同一件实物；把它们当作类通称降级，会让唯一可读的持有事实消失。
#: 每条都写明「正文无专名」和「持有行对应唯一实物」两项证据，便于复查。
#:
#: 维护边界：只登记**组规范名**，且必须先确认该组 ``status='active'``、持有段
#: 指向同一实物。真正的类通称（``刀``/``法宝``/``神兵``…）继续走 ``generic``，
#: 不允许因为「读起来也像类名」就塞进这里。
CANONICAL_WHITELIST: dict[str, str] = {
    "大钢刀": (
        "正文 1 章 1 次，无专名；kg_item_holding 仅 hid=16"
        "（own 归「使大钢刀的汉子」193–∞），该称呼就是那汉子手中这一柄的唯一指代"
    ),
    "法器铜锣": (
        "正文 5 章 7 次，无专名；仅 hid=25（own 归许七安 227–∞），"
        "全书只以「法器铜锣」称呼这一件，没有更专的名字"
    ),
    "银枪": (
        "正文 18 章 34 次，无专名；hid=44/48/45 是同一杆枪的转手链"
        "（银甲女子 156–228 → 李妙真 227–228 → 杨砚 228–∞），"
        "写法「银枪/白马银枪」都指它；「那杆枪」唯一出现是比喻，2026-11-10 第四批已摘"
    ),
    "长剑": (
        "正文 27 章 40 次，无专名；仅 hid=47（own 归长公主 106–∞），"
        "这柄剑在正文里始终只叫「长剑」"
    ),
    "飞剑": (
        "正文 91 章 231 次，类称虽普遍，但唯一持有行 hid=52"
        "（own 归李妙真 227–∞）锁定的是李妙真那一柄，正文未给这柄剑专名"
    ),
    "黑金长刀": (
        "正文 47 章 88 次，无更专名；hid=23（93–102）与 hid=58（102–∞）"
        "均归许七安，组内 8 条 surface（黑金刀/暗金色长刀/修长战刀/刀胚…）"
        "都是这把刀的别名"
    ),
}


def whitelist_reason(name: str) -> str | None:
    """组规范名是否已在人工白名单里；返回登记理由（未登记返回 ``None``）。"""
    return CANONICAL_WHITELIST.get((name or "").strip())



def is_name_like(form: str) -> bool:
    """粗筛：像名字的写法才参与子串判据，避免「打更人的铜锣」反咬「铜锣」。"""
    form = (form or "").strip()
    if not (2 <= len(form) <= 10):
        return False
    if any(ch in _PHRASE_MARKS for ch in form):
        return False
    return not any(ch in "，。！？、；：“”「」『』（）()《》 " for ch in form)


#: 指示代词/人称开头：只能是正文指代，不可能是物品名。
_DEICTIC_PREFIXES = ("此", "这", "那", "该", "其", "我", "你", "他", "她", "它")

#: 「数量 + 量词」短语：腰间一把军弩、两柄短刀。
_QUANTITY_PHRASE = re.compile(r"[一二两三四五六七八九十几数][把杆柄枚口件条面块颗只匹张幅座支]")


def is_reference_phrase(form: str) -> bool:
    """写法是不是正文指代/描述短语，而不是名字（判据 R6）。

    命中任一条即算：指示代词或人称开头、带「的」、含「数量 + 量词」。
    只用于**写法层**：组规范名（如 ``周旻的账簿``）不进这条判据。
    """
    raw = (form or "").strip()
    if not raw:
        return False
    if raw.startswith(_DEICTIC_PREFIXES):
        return True
    if "的" in raw:
        return True
    return bool(_QUANTITY_PHRASE.search(raw))


@dataclass(frozen=True)
class NameShape:
    """一个写法的形态判定结果。"""

    level: str
    rule: str
    tokens: tuple[str, ...]
    detail: str

    @property
    def flagged(self) -> bool:
        return self.level != NONE


def _tokens(name: str) -> tuple[str, ...] | None:
    """把写法贪心切成已知词；切不干净（有专名残留）返回 ``None``。"""
    out: list[str] = []
    i = 0
    while i < len(name):
        for word in _ALL_WORDS:
            if name.startswith(word, i):
                out.append(word)
                i += len(word)
                break
        else:
            return None
    return tuple(out)


def classify_name(name: str) -> NameShape:
    """按「有没有专名残留」判断写法形态（判据 R4）。"""
    raw = (name or "").strip()
    if not raw:
        return NameShape(NONE, "", (), "空写法")
    tokens = _tokens(raw)
    if tokens is None:
        return NameShape(NONE, "", (), "含专名部分，不是类名形态")
    last = tokens[-1]
    if last in CATEGORY_WORDS:
        return NameShape(HIGH, R4, tokens,
                         f"纯类别词「{last}」，没有专名")
    if last in CREATURE_CLASS_WORDS:
        return NameShape(HIGH, R4, tokens,
                         f"生物类称语素「{last}」，只有类别没有专名")
    if len(tokens) == 1 and tokens[0] in TYPE_WORDS:
        return NameShape(HIGH, R4, tokens,
                         f"纯器型词「{last}」，没有专名")
    if len(raw) <= 2 and raw[0] in BARE_MATERIAL_CHARS:
        return NameShape(HIGH, R4, tokens,
                         f"单字裸材质「{raw[0]}」+ 器型「{last}」")
    return NameShape(MEDIUM, R4, tokens,
                     f"材质/修饰 + 器型「{last}」，形态像类名但可能是正文唯一指代")


def classify_canonical(name: str) -> NameShape:
    """组规范名专用的泛称判定：先查人工白名单，再走形态判据。

    与 :func:`classify_name` 的分工：后者只回答「形态像不像类名」，供写法登记
    入口使用；组规范名还要叠加人工裁定（白名单）。白名单命中时返回 ``NONE``，
    ``detail`` 原样带出登记理由，便于报告与复查。
    """
    raw = (name or "").strip()
    reason = whitelist_reason(raw)
    if reason:
        return NameShape(NONE, "", _tokens(raw) or (), f"已核定白名单：{reason}")
    return classify_name(raw)


def substring_owners(name: str, known: set[str]) -> list[str]:
    """``name`` 是哪些更长、像名字的写法的真子串（长的在前）。"""
    return sorted((k for k in known
                   if k != name and name in k and is_name_like(k)),
                  key=len, reverse=True)


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def is_own_abbreviation(name: str, canonical: str) -> bool:
    """短写法是自己规范名的缩写（黑金刀 ⊂ 黑金长刀），不算泛称。

    用「按序子序列」而不是连续子串：``黑金刀`` 里省掉了 ``长``，仍是同一件。
    """
    return bool(canonical) and name != canonical and _is_subsequence(name, canonical)


def cross_group_owners(name: str, alias_owners: dict[str, set[str]]) -> list[str]:
    """同一写法出现在哪些实体/组的 aliases 里。"""
    return sorted(alias_owners.get(name) or ())


def coverage_contrast(surface_chapters: int, group_chapters: int) -> bool:
    """判据 R3：写法覆盖章数 ≥ 所属组规范名的 2 倍，且至少 5 章。"""
    return surface_chapters >= 5 and surface_chapters >= 2 * max(group_chapters, 1)


def multi_holder(owners: set[str]) -> bool:
    """判据 R5：同一写法对应多个持有者。"""
    return len({o for o in owners if o}) >= 2


def alias_verdict(name: str, canonical: str, known: set[str],
                  alias_owners: dict[str, set[str]] | None = None) -> dict:
    """写法登记入口用：只拦 high 置信的泛称，medium 只报告。

    返回 ``{"blocked": bool, "rules": [...], "detail": str}``。
    """
    name = (name or "").strip()
    canonical = (canonical or "").strip()
    if not name:
        return {"blocked": False, "rules": [], "detail": "空写法"}
    if is_own_abbreviation(name, canonical):
        return {"blocked": False, "rules": [], "detail": "所属规范名的缩写"}
    if is_reference_phrase(name):
        return {"blocked": True, "rules": [R6],
                "detail": "正文指代/描述短语（指示代词、「的」或数量短语），不是名字"}

    rules: list[str] = []
    details: list[str] = []

    shape = classify_name(name)
    if shape.level == HIGH:
        rules.append(R4)
        details.append(shape.detail)

    others = substring_owners(name, known - {canonical})
    if others:
        rules.append(R1)
        details.append("是更长写法「{}」的子串".format("」「".join(others[:3])))

    owners = cross_group_owners(name, alias_owners or {})
    if len(owners) > 1:
        rules.append(R2)
        details.append("同时挂在 {} 个实体上：{}".format(len(owners), "、".join(owners[:4])))

    blocked = shape.level == HIGH or R2 in rules
    return {"blocked": blocked,
            "rules": sorted(set(rules)),
            "detail": "；".join(details) or "未命中泛称判据"}
