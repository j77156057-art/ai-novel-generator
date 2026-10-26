"""规则哨兵：零 LLM 的高置信硬冲突检测。

## 它在整条链里干什么

账本（`ledger.py`）回答「这个项目当前认定的事实是什么」；哨兵回答**另一个问题**：
「新写的这一段，跟已认定的事实**硬冲突**了吗」。前者是状态，后者是状态的一致性检查。

设计纪律（外部评审 Q4 的结论）：
**只标红，绝不自动改写。** 哨兵的产出是一条条带证据的告警，不是对正文的修改。
理由是判据本身可能出错，而改错正文是不可见的损失；标红把判断权留给人。

## 实现状态：五个候选规则，逐条交代

外部评审提了五条规则。逐条说明落到什么程度 —— **没实现的必须说清为什么**，
不能留一个空壳让人以为它在工作。

| 规则 | 状态 | 说明 |
|---|---|---|
| 已死角色又说话 | **已实现** `dead_speaks` | 死亡事件 + 后续说话证据 |
| 已死角色又出场 | **已实现** `dead_acts` | 死亡事件 + 后续动作证据。只认动作谓词，单纯提及死者不算出场 |
| 物品归属冲突 | **已实现但默认沉默** `item_owner_conflict` | 需要「身份唯一」前提（见下）。账本目前不产物品事实，所以实际不响 —— **这是刻意的，不是漏做** |
| 境界倒退 | **已实现** `realm_regression` | 需要**有序**词表 + **同量纲**才判；取不到就不判（宁可漏报不误报） |
| 别名分裂 | **复用账本，未接入** `alias_split` | 转调 `ledger.plan_alias_merges` 的待裁决提案，不另写一套字符串规则。当前只在 `RULES` 里登记了说明，判定仍由账本出 |

## 精度从哪里来（每条规则都要说清楚，否则就是碰运气）

统一手法：**判别性的动词/位置谓词 + 必须锚定在已知名字上**。六处具体的收紧：

1. **死亡必须紧跟句读**：`X死了` / `X，身亡`。`X死前`、`死气沉沉` 一律不匹配 ——
   中文里「死」的构词能力太强，不加位置约束必然误报。
2. **复活要分两档**：`X没死` / `X死而复生` 是无歧义的复活事件；
   `X醒了过来` 只在**已有死亡记录**时才算复活。否则日常的「醒来」会把死亡状态冲掉。
3. **物品归属需要「身份唯一」这个前提**。这条是**实测逼出来的**：116 万字真实网文跑下来，
   不加前提会打出一条确凿误报 —— 《长夜司》第 41 段「许新年举起茶杯」，
   而该书「茶杯」出现 12 次、被 6 个不同的人端着，各是各的杯子。
   更麻烦的是 `hold` 的宾语槽还会把方位词切成物件（`桌上`/`腰间`/`手中`/`所有`），
   它本身就不是个可靠的信号源。所以现在只有**登记过**（`seed_tracked`）
   或**被交接证伪过唯一性**的物品才产生归属主张。
4. **境界必须有序，而且必须同量纲**。两条独立的闸门：
   `REALM_ORDER` 外的写法不参与比较；再加一条 —— `order` 只是整数，
   序号写法（`三阶`→3）和词表位次（`金丹`→4）落在同一个整数空间里但**是两把尺子**，
   不比量纲就会把「金丹之后写三阶」判成倒退（实测确实如此，4 组用例全是误报）。
   量纲共三种：`ord:<单位>`、`ord:段@<子阶梯>`（`第七段斗之气`）、`ladder:<体系号>`。
5. **`realm_order` 必须真的接进正则**。曾经的实现只在排名时用它，正则按默认表编译死，
   于是「传了本书的阶梯却一个字都抽不出来，而默认表里的词照样抽得出来」——
   抽取结果取决于一张调用方没见过的表。**已修**，且 `verify_sentinel.py` F3 两个方向都验。
6. **成语与远主语不认**。`死了这条心` / `死不瞑目` 是构词成分；主语被整句隔开的
   （`陆见微身受重伤，最终身亡。`）也不认 —— 认了会把 `陆见微，他不想死。` 一起认进来。

## 时间序

所有判定都按**段号升序**推进状态。而且**段内也按 span 顺序推进**：一次生成是 3000 字，
`X 死了……X 说道` 完全可能落在同一段里，只跟「本段之前」的状态比就会漏掉这种自相矛盾。

## 复活/转移事件是**豁免**

`X` 死了，后来 `X复活` 了 —— 这不是 bug，是剧情。所以复活事件会把死亡状态解除，
之后不再报 `dead_*`。物品同理：`X把剑交给Y` 之后，`Y的剑` 不再与 `X的剑` 冲突。
没有这层豁免，哨兵会在正常的剧情推进上疯狂误报。

## 已验证到哪一步（**区分实测与假设**，不许混）

实测（`verify_sentinel.py` G/H 段，可复现、离线）：

- **116 万字真实网文（三本，387 段）上零误报**，抽取到 416 条事件 —— 名字表还用的是
  「逐段累积 + ≥3 段才留」的**脏表**（含 `冷笑`/`连忙` 这类被误当人名的动词碎片），
  所以这个 0 是**上界**。
- 空名字表下一事件不产：哨兵不会凭空造主体。
- 跨量纲、复合档位、真实句式补主语后的抽取，全部有正反例对拍。

**未验证的假设（明确标注，别当成结论）：**

- 真实人类网文里，境界构造总共出现 25 次，**主语在构造内的 0 次** —— 中文网文极爱省略主语。
  因此本模块对**人类写的**境界描写召回接近零。硬要认就得跨句找远主语，
  而 `沈砚和赵磐石交谈，然后突破到化神` 会把功劳记到赵磐石头上 —— **记错人比漏报更糟**，
  所以放弃。对**生成正文**（本模块真正的目标）的召回**尚未验证**：
  现有 demo 项目的正文是离线兜底占位，没有境界描写，没有可测的样本。
- 因此「哨兵能在生成正文上抓到硬冲突」这句话，目前只有合成正例支撑，
  **没有真实生成样本支撑**。要给这个结论补证据，得先有真实模型生成的正文。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from sqlalchemy import select

from app.models import Fact

# --------------------------------------------------------------------------- #
# 词表
# --------------------------------------------------------------------------- #

#: 死亡谓词。要求后面紧跟句读或「了」——`死前`/`死敌`/`死气` 这类构词一律不匹配。
DEATH_VERBS = (
    "死|身亡|毙命|殒命|断气|咽气|气绝|没了气息|不治身亡|阵亡|陨落|命丧|身死|丧命"
)
#: 死亡动词后的合法后继（位置约束，防构词误报）。
#:
#: 刻意**不含「的」**：`X的死` 虽然多半意味着 X 死了，但 `X的死党`/`X的死对头`
#: 会把「死」当成构词成分。为了一条精确规则去枚举「的」后面所有构词尾巴不值得，
#: 所以这里选择漏报 —— 本模块的统一取舍就是「漏报优于误报」。
_AFTER_DEATH = r"(?:了|，|,|。|！|!|？|\?|、|；|;|”|」|’|'|$)"
#: 主语与死亡动词之间**只允许真实副词**，不允许任意字符。
#:
#: 这条是踩过坑的：写成 `[一-鿿]{0,2}?` 时，`陆见微不想死。` 会匹配成
#: 「陆见微 + 不想 + 死 + 。」→ 把一个大活人记成死亡状态，之后他每次说话都报错。
_DEATH_ADV = (
    r"(?:已经|已|终于|早已|早|竟|竟然|果然|也|就|便|还是|顿时|随即|当场|后来|最后"
    r"|最终|终究|如今|这下|真的|确实|的确|先|再|都|全|忽然|突然|彻底)*(?:[，,]?)"
)
#: 说话动词
_SPEAK_VERBS = "说道|问道|答道|笑道|喝道|低声道|轻声道|沉声道|道|说|问|答|喊|骂"
#: 主语与说话动词之间的状语。三段相加，各有各的职责：
#:
#:   1) **光杆副词**（`沉声道` / `忽然道`）—— 没有 的/地 标记，只能列出来；
#:   2) **带标记的方式状语**（`淡淡的说道` / `咬牙切齿的说道`）—— 这是**开放类**，
#:      枚举不出来（实测形态有 笑吟吟 / 咬牙切齿 / 没好气 / 漫不经心 / 似笑非笑 …），
#:      所以改用结构特征认它：1~4 个非标点字符 + 的/地。
#:      **实测依据**：116 万字真实语料里 `的`+说话动词出现 **1159** 次、
#:      `地`+说话动词只有 **21** 次 —— 55:1。只认「地」等于系统性漏掉主流写法。
#:   3) 收尾的 `[地的]?`：给第 1 段留出 `忽然地道` 这种「光杆副词 + 地」的组合。
#:
#: **为什么 的/地 必须和第 2 段绑成一组**（`{1,4}[地的]` 而不是 `{0,4}(?:[地的])?`）：
#: 后者会让 `X的死对头说道` 匹配成「X 在说话」—— 说话的是死对头。
#: 把 的/地 设成**必需标记**后，那个句子里没有任何「N 字的」紧邻 `说道`，
#: 于是整条不匹配。**记错人比漏报更糟**，这个代价必须付。
#:
#: 死亡谓词那边刻意**不放开**：`不想死` / `死了这条心` 正是被宽状语槽坑出来的，
#: 说话动词是明确的，死亡谓词在中文里构词能力太强，不能同法处理。
_SPEAK_ADV = (
    r"(?:淡淡地|冷冷地|沉声|轻声|低声|笑着|忽然|又|才|这才|便|就|也|则|却|终于|随即)*"
    r"(?:[^，。！？；：、\s“”「」]{1,4}[地的])?"
    r"(?:[地的])?"
    r"(?:[，,]?)"
)

#: 无歧义的复活谓词
REVIVE_VERBS = "复活|死而复生|起死回生|活了过来|又活了|没有死|没死|未曾死|诈死|假死"
#: 有歧义的「苏醒」类谓词：只在**当前状态为死**时才算复活事件
REVIVE_WEAK = "醒了过来|醒转|睁开了眼|睁开眼"
#: 复活谓词前的副词槽。同样只允许真实副词，不允许任意字符。
_REVIVE_ADV = r"(?:其实|原来|竟然|居然|根本|并没有|并|也|竟|却是|是|只是|不过)*"

#: 「死」的成语/惯用尾巴。命中了就说明这个「死」是构词成分，不是状态。
#: 实测踩到的是 `陆见微死了这条心。` —— 不加这道过滤会把大活人记成死亡状态。
_DEATH_IDIOM_TAIL = re.compile(r"^(?:这|那)?(?:一)?(?:条|份|颗)?心|^不瞑目|^有余辜")

#: 交接动词：`X 把 <物> 交给 Y`
_GIVE_VERBS = "交给|递给|送给|拿给|还给|交付|托付给|转交给|传给|抛给"
#: 持握动词：`X 握住 <物>` —— 这些位置的宾语天然是具体物件
_HOLD_VERBS = ("握住|攥住|捏住|抓起|拿过|拔出|抽出|取出|掏出|收起|收好|祭出|挥动"
               "|举起|揣进|别在|挂在|系在|套上|戴上|佩着")

#: 境界推进谓词
_REALM_UP = "突破|晋升|进阶|踏入|达到|进入|迈入|跨入|凝结|结成|修成|臻至"
#: 境界跌落谓词
_REALM_DOWN = "跌落|跌至|跌到|退到|退回|退至|散尽|尽失|被废|崩碎"

#: 境界**体系**（每个内层 tuple 是一套自洽的阶梯）。
#:
#: 为什么必须是二维的：境界表里混着好几套互不相干的刻度 —— 修仙档位（炼气→大罗）、
#: 器物档位（黄阶→神阶）、星阶（一星→九星）。它们各自有序，但**彼此不可比**。
#: 拍平成一维以后，`一星`（第 21 位）后面写 `黄阶`（第 15 位）就会被判成「境界倒退」，
#: 而这两件事根本不在一个刻度上 —— 这是纯粹的误报源。
#: 实测（见 `verify_sentinel.py` F2）：拍平时 4 组跨刻度用例全是误报。
#:
#: **可配置**：`extract_events(realm_order=...)` 可传本结构，也可传一维 tuple
#: （表示「本书只有一套阶梯」——多数书确实如此）。
#: 不在表里的写法一律不参与大小比较 —— 少报优于误报。
REALM_SYSTEMS: tuple[tuple[str, ...], ...] = (
    ("凡人", "炼气", "筑基", "金丹", "元婴", "化神", "炼虚", "合体", "大乘", "渡劫",
     "真仙", "金仙", "太乙", "大罗"),
    ("黄阶", "玄阶", "地阶", "天阶", "圣阶", "神阶"),
    ("一星", "二星", "三星", "四星", "五星", "六星", "七星", "八星", "九星"),
)
#: 拍平后的默认表（保持一维形态，供 `_REALM_ALT` 与外部只读消费）
REALM_ORDER: tuple[str, ...] = tuple(r for sys_ in REALM_SYSTEMS for r in sys_)
_REALM_ALT = "|".join(re.escape(r) for r in sorted(REALM_ORDER, key=len, reverse=True))

#: 序号类境界的**单位**（量纲的一部分）。正则与解析共用一处，避免两边漂移。
_ORDINAL_UNITS = "阶|层|品|重|级"
#: 序号类境界（`三阶`/`七层`/`九品`）——顺序由序号本身给出，不依赖词表。
#: **单位也是量纲的一部分**：`三品` 与 `七重` 不是同一套刻度。
_ORDINAL_RE = re.compile(rf"(?P<num>[一二三四五六七八九十百]+)\s*(?P<unit>{_ORDINAL_UNITS})")

#: 复合档位（`第七段斗之气`）：段位挂在某套子阶梯下，两段各自成刻度。
#: `段` **刻意不进 `_ORDINAL_UNITS`** —— 它不是通用序号单位，
#: `突破了一段时间` 里的「一段」不是段位。
_COMPOSITE_RE = re.compile(r"(?:第)?(?P<num>[一二三四五六七八九十百]+)段")

#: 物品名的字符类：汉字但**不含「的」**。
#: 含「的」会把 `握住验骨碑的手` 的宾语吞成 `验骨碑的手`，归属就归错人了。
_ITEM_CH = r"(?:(?!的)[一-鿿])"

_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}


def cn_num(s: str) -> int | None:
    """中文数字 → 整数。只支持 1..999 的常见写法，其余返回 None（不猜）。"""
    if not s:
        return None
    total, section, number = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch == "十":
            section += (number or 1) * 10
            number = 0
        elif ch == "百":
            section += (number or 1) * 100
            number = 0
        else:
            return None
    total = section + number
    return total or None


# --------------------------------------------------------------------------- #
# 事件
# --------------------------------------------------------------------------- #

#: 事件种类
E_DEATH = "death"
E_REVIVE = "revive"
E_SPEAK = "speak"
E_ACT = "act"
E_GIVE = "give"
E_HOLD = "hold"
E_REALM_UP = "realm_up"
E_REALM_DOWN = "realm_down"

#: 状态种类 → 取值来源
STATE_LIFE = "life"          # alive | dead
STATE_OWNER = "owner"        # 拥有者名字
STATE_REALM = "realm"        # 境界序数或名字
#: **内部记账，不是剧情事实**：该物品是否已被认定为「身份唯一」。
#: 不参与 `state_facts_from_events` 的落库，也不该出现在告警文案里。
STATE_TRACKED = "tracked"


@dataclass
class Event:
    """一条**带出处**的状态证据。

    刻意保留 `span`：告警必须能指回原文的确切位置。没有出处的告警无法复核，
    而无法复核的告警在实操中等于噪音 —— 用户看两次就会开始无视它。
    """

    kind: str
    entity: str
    segment_no: int
    span: tuple[int, int]
    text: str
    value: str | None = None
    #: 排序用：境界序数
    order: int | None = None
    #: **`order` 属于哪套刻度**（`ord:阶` / `ladder:0` …）。
    #: `order` 只是整数，跨刻度比大小必然误报，所以两者必须一起传、一起比。
    scale: str | None = None
    #: 弱证据标记（`醒了过来` 这种要看状态的）
    weak: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["span"] = list(self.span)
        return d


@dataclass
class Finding:
    """一条告警。**只报告，不改正文。**"""

    rule: str
    severity: str          # err（硬冲突）| warn（可疑，需人看）
    entity: str
    message: str
    #: 当前段的证据
    spans: list[dict] = field(default_factory=list)
    #: 更早段的反证（告警的说服力全在这里：不给原文，用户没法判断是不是误报）
    evidence: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "entity": self.entity,
            "message": self.message,
            "spans": self.spans,
            "evidence": self.evidence,
        }

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #

def _name_alt(names: list[str]) -> str:
    """名字交替式：长的在前，避免 `宁折` 抢先匹配掉 `宁折枝`。"""
    return "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))


@dataclass
class Extractors:
    """编译好的模式集。名字表变了就重建 —— 模式里嵌了名字交替式。"""

    names: list[str]
    death: re.Pattern | None
    revive: re.Pattern | None
    speak: re.Pattern | None
    give: re.Pattern | None
    hold: re.Pattern | None
    realm: re.Pattern | None
    realm_down: re.Pattern | None


def build_extractors(names: list[str], realm_order=REALM_SYSTEMS) -> Extractors:
    """编译模式集。**名字表或境界阶梯变了都要重建。**

    境界阶梯必须进正则 —— 曾经的实现只把 `realm_order` 用在排名上，正则却在
    导入时按默认表编译好。后果是「可配置」配了没用：传一本书自己的阶梯
    （`斗之气/斗者/…/斗帝`），`沈砚突破到化神。` 一个字都抽不出来，
    而 `沈砚突破到金丹。`（默认表里有）却能抽 —— 抽取结果取决于一张**调用方
    根本没见过的表**。这个 bug 是靠「真实语料上 0 条境界事件」暴露出来的。
    """
    names = [n for n in names if n]
    if not names:
        return Extractors([], None, None, None, None, None, None, None)
    alt = _name_alt(names)
    nm = f"(?P<name>{alt})"
    # 本调用方自己的阶梯 → 本调用方自己的交替式
    realm_alt = "|".join(
        re.escape(r) for r in sorted((r for g in _as_systems(realm_order) for r in g),
                                     key=len, reverse=True)
    )

    # 死亡，三个语序都要覆盖（缺一个就是漏报一种写法）：
    #   1) 主动：`陆见微已经死了。`
    #   2) 被动：`陆见微被杀死。`  —— 只写 `杀死X` 会漏掉这一种
    #   3) 施动在前：`沈砚杀死了陆见微。`
    #
    # 注意三个分支必须用**不同的组名**：Python 的 `re` 不允许同一个名字在
    # 交替分支里重复定义（会报 `redefinition of group name`）。
    death = re.compile(
        rf"{nm}{_DEATH_ADV}(?P<verb>{DEATH_VERBS}){_AFTER_DEATH}"
        rf"|(?P<name_p>{alt})(?:被|遭)(?:人)?"
        rf"(?P<verb3>杀死|杀了|刺死|砍死|毒死|击毙|害死|除掉){_AFTER_DEATH}"
        rf"|(?P<verb2>杀死|杀了|刺死|砍死|毒死|击毙|害死)了?(?P<name_k>{alt})"
    )
    revive = re.compile(rf"{nm}{_REVIVE_ADV}(?P<verb>{REVIVE_VERBS}|{REVIVE_WEAK})")
    speak = re.compile(
        rf"{nm}{_SPEAK_ADV}(?P<verb>{_SPEAK_VERBS})"
        rf"(?P<colon>[：:]|，|。|,|\s|“|「)"
    )
    give = re.compile(
        rf"(?:把|将)(?P<item>{_ITEM_CH}{{2,6}}?)(?P<verb>{_GIVE_VERBS})(?P<to>{alt})"
    )
    hold = re.compile(
        rf"{nm}(?:[一-鿿]{{0,4}})?(?P<verb>{_HOLD_VERBS})(?:了)?(?:自己的)?"
        rf"(?P<item>{_ITEM_CH}{{2,4}})"
    )
    realm = re.compile(
        rf"{nm}(?:的)?(?:修为|境界)?(?:已经|已)?(?:{_REALM_UP})(?:到|至|了)?"
        rf"(?P<realm>"
        # 复合档位：`第七段斗之气` —— 后面**必须**紧跟阶梯词才认。
        # 不设这道后视守卫，`突破了一段时间` 会被切成 `一段` 判成突破（实测会踩）。
        rf"(?:第)?(?P<seg>[一二三四五六七八九十百]+)段(?=(?P<sub>{realm_alt}))"
        rf"|(?:第)?(?P<ord>[一二三四五六七八九十百]+(?:{_ORDINAL_UNITS}))"
        rf"|{realm_alt}"
        rf")"
    )
    realm_down = re.compile(
        rf"{nm}(?:的)?(?:修为|境界)?(?:{_REALM_DOWN})(?:到|至|了)?"
        rf"(?P<realm>"
        rf"(?:第)?(?P<seg>[一二三四五六七八九十百]+)段(?=(?P<sub>{realm_alt}))"
        rf"|(?:第)?(?P<ord>[一二三四五六七八九十百]+(?:{_ORDINAL_UNITS}))"
        rf"|{realm_alt}"
        rf")?"
    )
    return Extractors(names, death, revive, speak, give, hold, realm, realm_down)


def _as_systems(order) -> tuple[tuple[str, ...], ...]:
    """把 `realm_order` 归一成「体系列表」。

    传一维 tuple 表示**本书只有一套阶梯**（多数书确实如此）；
    传二维 tuple 则是显式声明多套互不可比的体系。
    """
    if order and isinstance(order[0], (tuple, list)):
        return tuple(tuple(g) for g in order)
    return (tuple(order),)


def _realm_key(word: str | None, realm_order, sub: str | None = None) -> tuple[int, str] | None:
    """境界 → `(序数, 量纲标签)`。

    **量纲标签是必需的，不是装饰。** `order` 是个整数，但它来自两套互不相干的刻度：
    序号写法（`三阶` → 3）和词表位次（`金丹` → 4）。只比整数大小，`金丹`(4) 后面写
    `三阶`(3) 就会被判成「境界倒退」—— 实测就是这样，纯粹误报。
    所以比较必须在同一量纲内进行，跨量纲一律不判（少报优于误报）。

    量纲一共三种来源，全都不可互比：
      - `ord:<单位>`（`三阶` → `ord:阶`）——单位不同就不是一把尺子；
      - `ord:段@<子阶梯>`（`第七段斗之气`）——同是「段」，挂在不同的子阶梯下也不可比；
      - `ladder:<体系号>`（`金丹`）——按词表体系编号。

    取不到序数时返回 `None`：词表外写法、或序号解析不出来。
    """
    if not word:
        return None
    if sub:  # 复合档位：段位 + 子阶梯名，两者一起才构成刻度
        m = _COMPOSITE_RE.fullmatch(word)
        n = cn_num(m.group("num")) if m else None
        return (n, f"ord:段@{sub}") if n is not None else None
    m = _ORDINAL_RE.fullmatch(word)
    if m:
        n = cn_num(m.group("num"))
        # 单位进量纲：`三品` 与 `七重` 不是同一套刻度
        return (n, f"ord:{m.group('unit')}") if n is not None else None
    systems = _as_systems(realm_order)
    for gi, group in enumerate(systems):
        if word in group:
            return (group.index(word) + 1, f"ladder:{gi}")
    return None


def extract_events(
    text: str,
    *,
    segment_no: int,
    names: list[str],
    realm_order=REALM_SYSTEMS,
    alive_entities: set[str] | None = None,
) -> list[Event]:
    """从一段文本里抽出状态事件。

    `alive_entities` 是**已知状态**，用来解歧义：`X醒了过来` 只有 X 当前是死的，
    才算复活事件，否则日常醒来会冲掉死亡状态。

    物品登记不走这里 —— 那是**状态**，不是抽取时的歧义消解，
    用 `seed_tracked(state, [...])` 在开跑前一次性登记。
    （此前这里收了一个 `tracked_items` 参数却从不读取，是「接口承诺了但没实现」，
    已删除；交接事件仍会自动把物品标记为身份唯一。）
    """
    ex = build_extractors(names, realm_order)
    alive_entities = alive_entities or set()
    out: list[Event] = []

    if ex.death:
        for m in ex.death.finditer(text):
            # 三个语序分支各用各的组名，这里按顺序取第一个命中的
            name = m.group("name") or m.group("name_p") or m.group("name_k")
            if not name:
                continue
            # 成语尾巴过滤：`死了这条心` / `死不瞑目` 里的「死」是构词成分
            if _DEATH_IDIOM_TAIL.match(text[m.end():]):
                continue
            out.append(Event(E_DEATH, name, segment_no, m.span(), m.group(0)))

    if ex.revive:
        for m in ex.revive.finditer(text):
            verb = m.group("verb")
            weak = verb in REVIVE_WEAK
            # 弱证据只在「此前已判死」时才成立 —— 否则日常醒来会冲掉死亡状态
            if weak and m.group("name") not in alive_entities:
                continue
            out.append(Event(E_REVIVE, m.group("name"), segment_no, m.span(),
                             m.group(0), weak=weak))

    if ex.speak:
        for m in ex.speak.finditer(text):
            out.append(Event(E_SPEAK, m.group("name"), segment_no, m.span(), m.group(0)))

    if ex.give:
        for m in ex.give.finditer(text):
            item, to = m.group("item"), m.group("to")
            if item == to or len(item) < 2:
                continue
            out.append(Event(E_GIVE, item, segment_no, m.span(), m.group(0), value=to))

    if ex.hold:
        for m in ex.hold.finditer(text):
            item = m.group("item")
            if len(item) < 2 or item in ex.names:
                continue
            # 归属主张：持握动词的主语就是当前持有者
            out.append(Event(E_HOLD, item, segment_no, m.span(), m.group(0),
                             value=m.group("name")))

    if ex.realm:
        for m in ex.realm.finditer(text):
            key = _realm_key(m.group("realm"), realm_order, m.group("sub"))
            if key is None:
                continue
            rank, scale = key
            out.append(Event(E_REALM_UP, m.group("name"), segment_no, m.span(),
                             m.group(0), value=m.group("realm"), order=rank, scale=scale))

    if ex.realm_down:
        for m in ex.realm_down.finditer(text):
            key = _realm_key(m.group("realm"), realm_order, m.group("sub"))
            rank, scale = key if key else (None, None)
            out.append(Event(E_REALM_DOWN, m.group("name"), segment_no, m.span(),
                             m.group(0), value=m.group("realm"), order=rank, scale=scale))

    out.sort(key=lambda e: e.span[0])
    return out


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #

def initial_state() -> dict:
    """状态表：`{(kind, entity): {value, order, segment_no, span, text}}`。"""
    return {}


def seed_tracked(state: dict, items) -> None:
    """把「身份唯一」的物品登记进状态表。

    只有登记过的物品才会产生归属冲突 —— 原因见 `rule_item_owner_conflict` 的说明。
    实测（116 万字真实网文）：不设这道门，`《长夜司》` 第 41 段就会因为
    `许新年举起茶杯` 报一次冲突，而那个「茶杯」在全书出现 12 次、被 6 个不同的人
    端着 —— 各人手里各有一只，这是确凿的误报。
    """
    for it in items or ():
        if it:
            state[(STATE_TRACKED, it)] = {"value": True, "segment_no": 0,
                                          "span": [0, 0], "text": "", "seeded": True}


def apply_event(state: dict, ev: Event) -> None:
    """把事件写进状态表。复活/交接会**解除**先前状态（这是豁免，不是覆盖）。"""
    if ev.kind == E_DEATH:
        state[(STATE_LIFE, ev.entity)] = {
            "value": "dead", "segment_no": ev.segment_no,
            "span": list(ev.span), "text": ev.text,
        }
    elif ev.kind == E_REVIVE:
        state[(STATE_LIFE, ev.entity)] = {
            "value": "alive", "segment_no": ev.segment_no,
            "span": list(ev.span), "text": ev.text, "revived": True,
        }
    elif ev.kind in (E_GIVE, E_HOLD) and ev.value:
        prev = state.get((STATE_OWNER, ev.entity))
        state[(STATE_OWNER, ev.entity)] = {
            "value": ev.value, "segment_no": ev.segment_no,
            "span": list(ev.span), "text": ev.text,
            # 转移来源留痕：`X把剑交给Y` 之后 `Y的剑` 不该再跟 `X的剑` 冲突
            "from": (prev or {}).get("value"),
        }
        # **只有交接能证明「这物件是唯一的那一件」。**
        # `X把剑交给Y` 是明确的、不可替代的一次转手；而 `举起茶杯` 只是拿个东西做动作，
        # 桌上那只和隔壁桌上那只本来就不是同一只。归属冲突只在有唯一性证据时才有意义。
        if ev.kind == E_GIVE:
            state[(STATE_TRACKED, ev.entity)] = {
                "value": True, "segment_no": ev.segment_no,
                "span": list(ev.span), "text": ev.text,
            }
    elif ev.kind == E_REALM_UP and ev.order is not None:
        prev = state.get((STATE_REALM, ev.entity))
        # 换刻度时不是「倒退」，是换了把尺子 —— 不能拿旧刻度的读数去比新刻度
        if (prev and prev.get("order") is not None
                and prev.get("scale") == ev.scale and prev["order"] > ev.order):
            return  # 倒退不当成「更高」写进状态；由规则去报
        state[(STATE_REALM, ev.entity)] = {
            "value": ev.value, "order": ev.order, "scale": ev.scale,
            "segment_no": ev.segment_no,
            "span": list(ev.span), "text": ev.text,
        }
    elif ev.kind == E_REALM_DOWN and ev.order is not None:
        state[(STATE_REALM, ev.entity)] = {
            "value": ev.value, "order": ev.order, "scale": ev.scale,
            "segment_no": ev.segment_no,
            "span": list(ev.span), "text": ev.text, "regressed": True,
        }


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #
#: 规则注册表：`rule 名 → (severity, 人话说明)`。
#: 每条规则都要在 `verify_sentinel.py` 里有**正例和反例**，否则不许进这张表。
RULES: dict[str, tuple[str, str]] = {
    "dead_speaks": ("err", "已记录死亡的角色，在后续段落里说话了（且中间没有复活事件）"),
    "dead_acts": ("err", "已记录死亡的角色，在后续段落里又有明确动作出场（且无复活事件）"),
    "item_owner_conflict": ("err", "同一件物品在后续段落被归给另一个角色，中间没有交接事件"),
    "realm_regression": ("err", "角色境界低于已记录的最高境界，中间没有跌落事件"),
    "alias_split": ("warn", "出现疑似同一人的新名字（转自账本的待裁决提案）"),
}


def _f(rule: str, entity: str, message: str, *, spans: list[dict],
       evidence: list[dict]) -> Finding:
    return Finding(rule, RULES[rule][0], entity, message, spans, evidence)


def _ev_span(ev: Event) -> dict:
    return {"segment_no": ev.segment_no, "span": list(ev.span), "text": ev.text}


# 下面四个规则的签名统一为 `(state, ev, events)`：
# `state` 是**该事件之前**的累计状态（由 check_segment 按 span 顺序推进），
# `events` 是本段的全部事件（供「本段内豁免」用，例如本段已有交接/跌落描写）。

def rule_dead_speaks(state: dict, ev: Event, events: list[Event]) -> list[Finding]:
    if ev.kind != E_SPEAK:
        return []
    st = state.get((STATE_LIFE, ev.entity))
    if not st or st.get("value") != "dead":
        return []
    return [_f(
        "dead_speaks", ev.entity,
        f"「{ev.entity}」已记于第 {st['segment_no']} 段死亡，本段（第 {ev.segment_no} 段）"
        f"却有台词：「{ev.text}」。若剧情上确实复活了，请补一个明确的复活描写。",
        spans=[_ev_span(ev)],
        evidence=[{"segment_no": st["segment_no"], "span": st["span"],
                   "text": st["text"], "kind": "death"}],
    )]


def rule_dead_acts(state: dict, ev: Event, events: list[Event]) -> list[Finding]:
    """已死角色又「做事」了。

    只认**动作事件**（持握），不认单纯的名字出现 —— 名字出现在回忆、提及、
    灵位、遗物里都是正常的，那不算出场。这条边界是刻意划的：
    宁可漏报，也不能把「他想起死去的师父」判成 bug。
    """
    if ev.kind != E_HOLD or not ev.value:
        return []
    st = state.get((STATE_LIFE, ev.value))
    if not st or st.get("value") != "dead":
        return []
    return [_f(
        "dead_acts", ev.value,
        f"「{ev.value}」已记于第 {st['segment_no']} 段死亡，本段却有动作：「{ev.text}」。",
        spans=[_ev_span(ev)],
        evidence=[{"segment_no": st["segment_no"], "span": st["span"],
                   "text": st["text"], "kind": "death"}],
    )]


def rule_item_owner_conflict(state: dict, ev: Event, events: list[Event]) -> list[Finding]:
    """物品归属冲突。

    判据有三层，每一层都是被实测逼出来的：

    1. 本段有 `X握住<物>`（持握即主张）；
    2. 状态表里该物的持有者是别人；
    3. 本段没有该物的交接事件来解释这个变化。
       （`X把剑交给Y。然后Y握住剑。` 完全正常，没这层豁免每次正当转手都会报错。）

    外加一道**前提闸门**：该物必须已被认定为「身份唯一」（`seed_tracked` 登记过，
    或者本身上下文里发生过交接）。为什么必须有这道门：`hold` 的宾语槽区分不了
    「铜钥匙」和「茶杯」——两者都是 2~3 字的普通名词，而现实里茶杯人人各有一只。
    实测 116 万字真实网文：不设这道门会打出一次确凿误报 ——
    `《长夜司》` 第 41 段「许新年举起茶杯」，而该书「茶杯」出现 12 次、6 个不同的人
    端着，各是各的杯子。更麻烦的是这个宾语槽还会把方位词切成物件
    （`桌上`/`腰间`/`手中`/`所有` 都被切出来过），所以它不是个可靠的信号源。
    结论：**宁可少报**。这条规则在调用方声明物品之前保持沉默，
    等账本开始产出物品事实（当前只有 characters/plotholes/relationships）再打开。
    """
    if ev.kind != E_HOLD:
        return []
    if not state.get((STATE_TRACKED, ev.entity)):
        return []
    if any(e.kind == E_GIVE and e.entity == ev.entity for e in events):
        return []
    st = state.get((STATE_OWNER, ev.entity))
    if not st or st.get("value") == ev.value:
        return []
    return [_f(
        "item_owner_conflict", ev.entity,
        f"「{ev.entity}」在第 {st['segment_no']} 段归「{st['value']}」，"
        f"本段却由「{ev.value}」持有：「{ev.text}」。中间没有交接描写。",
        spans=[_ev_span(ev)],
        evidence=[{"segment_no": st["segment_no"], "span": st["span"],
                   "text": st["text"], "kind": "owner"}],
    )]


def rule_realm_regression(state: dict, ev: Event, events: list[Event]) -> list[Finding]:
    """境界倒退：事件记录了更低的境界，而此前已记录更高境界。

    两道「不判」的闸门，都是精度的来源：

    1. `order is None`（词表里没有、也不是序号写法）——不判。
    2. **`scale` 不一致 ——不判。** `order` 只是个整数，序号写法（`三阶`→3）和
       词表位次（`金丹`→4）落在同一个整数空间里，但它们是两把不同的尺子。
       只比整数大小，`金丹`(4) 后面写 `三阶`(3) 就会报「境界倒退」—— 实测如此。
       同一刻度内比才是有效比较，跨刻度一律让位给「少报优于误报」。
    """
    if ev.kind not in (E_REALM_UP, E_REALM_DOWN) or ev.order is None:
        return []
    # 本段已有明确跌落描写 → 正当降级，不报
    if any(e.kind == E_REALM_DOWN and e.entity == ev.entity for e in events):
        return []
    st = state.get((STATE_REALM, ev.entity))
    if not st or st.get("order") is None:
        return []
    if st.get("scale") != ev.scale:
        return []
    if ev.order >= st["order"]:
        return []
    return [_f(
        "realm_regression", ev.entity,
        f"「{ev.entity}」的境界在第 {st['segment_no']} 段记为「{st['value']}」，"
        f"本段写的是「{ev.value}」（更低）：「{ev.text}」。中间没有跌落描写。",
        spans=[_ev_span(ev)],
        evidence=[{"segment_no": st["segment_no"], "span": st["span"],
                   "text": st["text"], "kind": "realm"}],
    )]


#: 事件种类 → 规则函数。多对一（`realm_up` 与 `realm_down` 共用同一条规则）。
_RULES_BY_EVENT: dict[str, tuple] = {
    E_SPEAK: (rule_dead_speaks,),
    E_HOLD: (rule_dead_acts, rule_item_owner_conflict),
    E_REALM_UP: (rule_realm_regression,),
    E_REALM_DOWN: (rule_realm_regression,),
}

#: 规则注册顺序（也是告警的展示顺序）
RULE_ORDER = (
    "dead_speaks",
    "dead_acts",
    "item_owner_conflict",
    "realm_regression",
)


def check_segment(state_before: dict, events: list[Event]) -> list[Finding]:
    """纯函数：按**时间序**推进状态，产出冲突告警。

    两个关键语义：

    1. **段内也按 span 顺序推进。** 一次生成是 3000 字，`X 死了……X 说道` 完全可能
       落在同一段里。若只跟「本段之前」的状态比，这种同段内的自相矛盾就漏了。
       做法是逐事件推进：每条事件先对**它之前**的累计状态判定，再写进状态。
    2. **不修改 `state_before`，不碰正文。** 推进在副本上做；
       `verify_sentinel.py` 有闸门直接验这一点。
    """
    work = {k: dict(v) for k, v in state_before.items()}
    out: list[Finding] = []
    for ev in sorted(events, key=lambda e: e.span[0]):
        for fn in _RULES_BY_EVENT.get(ev.kind, ()):
            out.extend(fn(work, ev, events))
        apply_event(work, ev)
    order = {name: i for i, name in enumerate(RULE_ORDER)}
    out.sort(key=lambda f: (order.get(f.rule, 99), f.spans[0]["segment_no"] if f.spans else 0,
                            f.spans[0]["span"][0] if f.spans else 0))
    return out


# --------------------------------------------------------------------------- #
# 落库（状态 + 审计结果）
# --------------------------------------------------------------------------- #

T_STATE = "state"
#: 告警事实的类型。**必须落库** —— 只放在生成响应里的告警等于没告警：
#: 用户当时没盯着响应，事后就再也看不到它，也就无从复核。
#: 所有事实消费方都按显式类型过滤（`character`/`plothole`/`relationship`/`state`），
#: 所以这个新类型不会漏进提示词或提醒。
T_ALERT = "alert"


def save_findings(db, project_id: int, findings: list[Finding], *,
                  segment_no: int, source: str = "generated") -> int:
    """把本段告警写进事实表。同一段重跑（重新生成）时**先清后写**，不累积旧告警。

    键 = `规则:实体:段号:偏移`：同一段同一规则对同一实体**在不同位置**各留一条
    （同一个角色在一段里说两次话就是两条，不能互相覆盖），
    重跑时旧的那条被替换掉，不会出现「段落改好了、旧告警还挂着」。
    """
    from app.models import Fact

    def _key_of(fd: Finding) -> str:
        off = (fd.spans[0]["span"][0] if fd.spans and fd.spans[0].get("span") else 0)
        return f"{fd.rule}:{fd.entity}:{segment_no}:{off}"

    old = db.scalars(
        select(Fact).where(Fact.project_id == project_id, Fact.type == T_ALERT,
                           Fact.source == source)
    ).all()
    keep = {_key_of(fd) for fd in findings}
    for row in old:
        # 只清本段的：其它段的告警是历史记录，不该被本段重跑带走
        if row.last_segment == segment_no and row.key not in keep:
            db.delete(row)

    index = {(r.type, r.key): r for r in old}
    n = 0
    for fd in findings:
        key = _key_of(fd)
        props = {
            # 实体必须一起存：键里虽然含实体，但那是**寻址**用的，不是数据。
            # 只把实体编码进键、读回时不带出来，就等于让「谁」这件事只存在于
            # 一个需要被解析的字符串里 —— 见 load_findings 的同款注释。
            "entity": fd.entity,
            "message": fd.message,
            "severity": fd.severity,
            "spans": fd.spans,
            "evidence": fd.evidence,
        }
        cur = index.get((T_ALERT, key))
        if cur is None:
            db.add(Fact(project_id=project_id, type=T_ALERT, key=key, props=props,
                        aliases=[], source=source, confidence="high",
                        first_segment=segment_no, last_segment=segment_no))
            n += 1
        else:
            cur.props = props
            cur.last_segment = segment_no
            n += 1
    db.flush()
    return n


def load_findings(db, project_id: int, source: str = "generated") -> list[dict]:
    """读回告警（按段号倒序 —— 最近的先看）。"""
    from app.models import Fact

    rows = db.scalars(
        select(Fact).where(Fact.project_id == project_id, Fact.type == T_ALERT,
                           Fact.source == source)
    ).all()
    def _entity_of(row) -> str:
        """实体：优先取 props（新行都写了）。

        老行只有键里那份，退化为按 `规则:实体:段号:偏移` 切一刀 ——
        实体本身不含 `:`，切出来是准的。写成函数而不是内联三元，
        是因为「条件 or 取值 if 条件 else 兜底」这种写法一旦内联，
        运算符优先级会把 props 命中值也在分支外丢掉（先写成内联版踩过一次）。
        """
        p = row.props or {}
        if p.get("entity"):
            return p["entity"]
        parts = (row.key or "").split(":", 3)
        return parts[1] if len(parts) > 1 else ""

    out = [
        {
            "rule": (r.key or "").split(":", 1)[0],
            "severity": (r.props or {}).get("severity", "err"),
            "segment_no": r.last_segment,
            "entity": _entity_of(r),
            "message": (r.props or {}).get("message", ""),
            "spans": (r.props or {}).get("spans") or [],
            "evidence": (r.props or {}).get("evidence") or [],
        }
        for r in rows
    ]
    out.sort(key=lambda d: (d["segment_no"] or 0), reverse=True)
    return out


def state_to_rows(state: dict) -> list[dict]:
    """状态表 → `Fact(type="state")` 行（纯转换，不碰库）。

    `key` 形如 `life:陆见微` / `owner:验骨碑`；`since_segment` 记下这个值
    是哪一段写下的 —— 冲突诊断全靠它，只留最新值就答不了「它什么时候变的」。

    转换与写入分开，是为了让「键卫生」这类性质能单独测（见 verify_sentinel I6），
    也让写入端可以按**替换**语义实现（见 `replace_state_facts`）。
    """
    rows = []
    for (kind, entity), st in state.items():
        # `tracked:*` 是内部记账（物品是否已认定为身份唯一），**必须一起落库**：
        # 唯一性是由「本上下文里发生过交接」推出来的，而交接和后续持握往往隔很多段，
        # 不持久化就会在段落边界上丢掉前提，归属冲突随之静默失效。
        # 它不会污染剧情事实面 —— `state` 类事实只有本模块读（`load_state`）。
        props = {
            "value": st.get("value"),
            "order": st.get("order"),
            # **量纲必须落库。** 判定要求量纲一致，而这一项此前没写进 props
            # —— 于是「第 1 段记金丹、第 9 段写炼气」这种跨段真倒退永远报不出来，
            # 因为还原出来的 state 里 scale 是 None，跟事件的 scale 永远不等。
            # 这是「新增字段必须一起落库」的又一个实例（同 PIPELINE_SCHEMA 那次的教训）。
            "scale": st.get("scale"),
            "since_segment": st.get("segment_no"),
            "span": st.get("span"),
            "text": st.get("text"),
        }
        if st.get("from"):
            props["from"] = st["from"]
        rows.append({
            "type": T_STATE,
            "key": f"{kind}:{entity}",
            "props": props,
            "confidence": "high",
        })
    return rows


def state_facts_from_events(
    events: list[Event], *, project_id: int = 0, existing: dict | None = None
) -> list[dict]:
    """把事件推进到 `existing` 状态之上，再转成事实行。

    只读地对待 `existing`（复制一份再推进），所以反复调用是幂等的 ——
    这一点由 I7 守着：同一状态重复落库不得让段号累加。
    """
    state = {k: dict(v) for k, v in (existing or {}).items()}
    for ev in events:
        apply_event(state, ev)
    return state_to_rows(state)


def load_state(db, project_id: int, source: str = "generated") -> dict:
    """从库里的 `state` 事实还原状态表。"""
    state: dict = {}
    rows = db.scalars(
        select(Fact).where(
            Fact.project_id == project_id, Fact.type == T_STATE, Fact.source == source
        )
    ).all()
    for r in rows:
        if ":" not in (r.key or ""):
            continue
        kind, entity = r.key.split(":", 1)
        p = r.props or {}
        state[(kind, entity)] = {
            "value": p.get("value"),
            "order": p.get("order"),
            # 量纲跟着 order 一起还原 —— 少这一项，跨段的境界判定会静默失效
            "scale": p.get("scale"),
            "segment_no": p.get("since_segment"),
            "span": p.get("span"),
            "text": p.get("text"),
            "from": p.get("from"),
        }
    return state


def known_names(db, project_id: int, source: str = "generated") -> list[str]:
    """名字表（规范名 + 别名），从账本取 —— 哨兵不自己维护名字。"""
    rows = db.scalars(
        select(Fact).where(
            Fact.project_id == project_id, Fact.type == "character",
            Fact.source == source, Fact.confidence == "high",
        )
    ).all()
    names: list[str] = []
    for r in rows:
        names.append(r.key)
        names.extend(a for a in (r.aliases or []) if isinstance(a, str))
    return [n for n in dict.fromkeys(names) if n]


def replace_state_facts(db, project_id: int, rows: list[dict],
                        source: str = "generated") -> dict:
    """把状态事实按**快照**写库：props 整体替换，不合并、不累加。

    为什么不走通用的 `upsert_facts`：那个函数的语义是「知识累加」——
    int 型 props 会**相加**（为「提及次数」这类计数器设计的），
    `first_segment`/`last_segment` 取 min/max。而状态是**点值快照**：
    `since_segment` 是「这个值是哪一段写下的」，不是次数。
    实测后果非常实在：同一状态被重复落库 5 次，`since_segment` 就累加到 **164**，
    于是告警里那句「境界在第 N 段记为…」的 N 是累加出来的假值 ——
    **证据失真的告警等于没有告警**。

    字符串 props（如 `owner` 的 `from`）在通用路径上是「键已存在就不覆盖」，
    同样会永远停在旧值；这里一并按替换处理。

    `rows` 就是 `state_facts_from_events(...)` 的产出；本函数只管写，
    转换与写入分开是为了让「键卫生」这类性质能单独测（见 verify_sentinel I6）。
    """
    from app.models import Fact

    live = {r["key"]: r for r in rows}
    existing = db.scalars(
        select(Fact).where(Fact.project_id == project_id, Fact.type == T_STATE,
                           Fact.source == source)
    ).all()
    index = {(f.type, f.key): f for f in existing}

    replaced = created = dropped = 0
    for key, item in live.items():
        # 段号只从 props 取 —— 它就是「当前值来自哪一段」，不另存一份免得不一致
        seg = (item.get("props") or {}).get("since_segment")
        cur = index.get((T_STATE, key))
        if cur is None:
            db.add(Fact(project_id=project_id, type=T_STATE, key=key,
                        props=dict(item.get("props") or {}), aliases=[],
                        source=source, confidence=item.get("confidence", "high"),
                        first_segment=seg, last_segment=seg))
            created += 1
            continue
        cur.props = dict(item.get("props") or {})
        if seg is not None:
            # 首次出现的时间不变，但「当前值来自哪一段」跟着 props 走
            cur.first_segment = seg if cur.first_segment is None else min(cur.first_segment, seg)
            cur.last_segment = seg
        replaced += 1

    for f in existing:
        # 快照语义：state 里没有的键就不该留在库里，否则残值会参与后续判定
        if f.key not in live:
            db.delete(f)
            dropped += 1

    db.flush()
    return {"created": created, "replaced": replaced, "dropped": dropped}


def realm_order_from_project(project) -> tuple:
    """项目自己的境界阶梯，没配就用默认表。

    阶梯挂在 `plot_bible["realm_order"]` 下 —— 不新增数据库字段就能配：
    一维列表是「本书只有一套阶梯」，二维列表是「多套互不可比」（见 `REALM_SYSTEMS`）。
    配错了不致命：取不到序数就不判，不会乱报。
    """
    bible = getattr(project, "plot_bible", None) or {}
    raw = bible.get("realm_order") if isinstance(bible, dict) else None
    if not raw or not isinstance(raw, (list, tuple)):
        return REALM_SYSTEMS
    groups: list[tuple[str, ...]] = []
    for g in raw:
        if isinstance(g, (list, tuple)):
            groups.append(tuple(str(w) for w in g if w))
        elif isinstance(g, str) and g:
            groups.append((g,))
    return tuple(g for g in groups if g) or REALM_SYSTEMS


def audit_segment(
    db, project, text: str, segment_no: int, *, source: str = "generated",
) -> dict:
    """审计一段：抽事件 → 判冲突 → 把状态推进落库。**只报告，不改正文。**

    这是把本模块接进生成环的入口。三件事按顺序做，任何一步失败都不该炸掉生成 ——
    调用方负责兜异常并降级（哨兵是增强，不是生成的前提）。

    返回 `{findings, events, names, state_size, rules}`。`findings` 已序列化，
    可直接进 API 响应。
    """
    names = known_names(db, project.id, source)
    state = load_state(db, project.id, source)
    ladder = realm_order_from_project(project)
    # 已死但后来又复活的，`alive_entities` 用来解 `醒来` 的歧义
    alive = {e for (kind, e), st in state.items()
             if kind == STATE_LIFE and st.get("value") != "dead"}
    events = extract_events(text, segment_no=segment_no, names=names,
                            realm_order=ladder, alive_entities=alive)
    findings = check_segment(state, events)
    for ev in events:
        apply_event(state, ev)
    # 状态推进过了：生成的正文改变了后续段落要遵守的事实。
    # 必须用**替换**语义写（`replace_state_facts`），不能用累加语义的通用 upsert ——
    # 见那个函数的 docstring：累加会把 `since_segment` 加成垃圾值，告警证据随之失真。
    replace_state_facts(db, project.id, state_to_rows(state), source=source)
    # 告警必须落库：只放响应里的告警，用户没盯着那一刻就永远看不到了
    save_findings(db, project.id, findings, segment_no=segment_no, source=source)
    db.commit()
    return {
        "findings": [f.to_dict() for f in findings],
        "events": len(events),
        "names": len(names),
        "state_size": len(state),
        "rules": _active_rules(),
        "realm_order": len(ladder),
    }


def _active_rules() -> dict:
    """哪几条规则当前真的会响 —— 下发给前端，免得用户以为五条都在跑。

    两条**不响**的必须显式说出来，否则界面看上去像全都在工作，
    而用户会据此读成「没报错 = 没问题」，实际是那条规则根本没参与：

    - `item_owner_conflict`：需要「身份唯一」前提，而账本目前不产物品事实 → 默认沉默；
    - `alias_split`：判定仍在账本（`plan_alias_merges` 的待裁决提案），
      本模块只登记了规则名与说明，**没有自己的判定实现**。
    """
    notes = {
        "item_owner_conflict": "需要先声明物品身份唯一（账本暂不产出物品事实，故默认沉默）",
        "alias_split": "判定由账本出（plan_alias_merges 的待裁决提案），本模块不重复实现",
    }
    return {
        name: {"severity": sev, "desc": desc,
               "active": name not in notes,
               "note": notes.get(name, "")}
        for name, (sev, desc) in RULES.items()
    }
