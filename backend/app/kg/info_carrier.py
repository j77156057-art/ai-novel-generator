# -*- coding: utf-8 -*-
"""信息载体判据：只看名字形态就能算出来，不依赖某本书的具体名字。

为什么单独成模块：``kind='item'`` 里混进「文件/邮件/策划书」不是某几个名字写错了，
而是抽取侧只有「具体物件」一个筐，信息载体无处可去。这里给出一组可迁移的判据，
批抽落库（判定侧）与历史数据迁移共用同一套实现，避免再往硬编码名单里补条目。

判据（每一条都能从名字算出来，语境只用来拆「文件」这类两可词的平局）：

- ``R1_electronic``：电子/数字载体（邮件、短信、私信……），没有可拿在手上的实体。
- ``R2_collective``：集合/不可数名词（资料、档案、数据……），指「一堆信息」，
  不是一个可转交的物件。
- ``R3_container``：明确是**装东西的实物**（文件夹、档案袋、信封、公文包），
  即使名字里带「文件/档案」，它仍然是 item。
- ``R4_paper_individual``：纸质实物单件（信件、书信、便条、卷轴……），
  能拿、能丢、能转交 → item。这条专门挡住「信件」被当成「邮件」一起划掉。
- ``R5_document_product``：成篇的信息产品（策划书、报告、合同、名单……），
  内容就是它的全部，脱离内容没有独立物理形态 → info。
- ``R0_default``：形态无信息载体特征 → item（笔、咖啡、手机、电脑走这条）。

结论三档：

- ``info``：信息载体，落库时 kind 改 ``info``，不再进物品持有链。
- ``review``：形态两可（「文件」可能是信息载体，也可能是实物档案夹），
  单列待审，**不自动改库**；语境里能确认是纸堆/信息时才升为 info。
- ``item``：物理物件，保持 ``kind='item'``。

注意：本模块**不碰** ``kg_item_group`` / ``kg_item_holding`` /
``kg_holding_story_segment``。调用方（迁移脚本）必须在改 kind 前自行挡掉
已挂组的实体——那会直接打断读通道。
"""
from __future__ import annotations

from dataclasses import dataclass

INFO = "info"
REVIEW = "review"
ITEM = "item"

R0 = "R0_default"
R1 = "R1_electronic"
R2 = "R2_collective"
R3 = "R3_container"
R4 = "R4_paper_individual"
R5 = "R5_document_product"

#: 电子/数字载体：没有可拿在手上的实体。
ELECTRONIC_WORDS = frozenset({
    "邮件", "电邮", "电子邮件", "短信", "私信", "微信", "电子文档",
    "扫描件", "传真", "聊天记录", "通话记录",
})

#: 集合/不可数名词：指「一堆信息」，不是一个可转交的物件。
COLLECTIVE_WORDS = frozenset({
    "资料", "档案", "数据", "信息", "情报", "文案", "素材", "文档",
})

#: 成篇的信息产品：内容就是它的全部。
DOCUMENT_WORDS = frozenset({
    "策划书", "计划书", "报告", "方案", "提案", "合同", "协议",
    "报表", "说明书", "申请书", "判决书", "名单", "表格", "清单", "简历",
})

#: 形态两可：可能是信息载体，也可能是实物档案夹/纸质原件 → 待审。
REVIEW_WORDS = frozenset({"文件", "卷宗", "材料", "记录"})

#: 明确是「装东西的实物」：即使名字里带文件/档案，也仍是 item。
CONTAINER_WORDS = frozenset({
    "文件夹", "文件盒", "文件柜", "文件包", "档案袋", "档案夹", "档案盒",
    "资料袋", "资料盒", "公文包", "信封", "卷宗夹",
})

#: 纸质实物单件：能拿、能丢、能转交 → item。
#: 「信」单列一条：它既是通用单字，也确实是可转交的纸。
PAPER_ITEM_WORDS = frozenset({
    "信件", "书信", "信", "便条", "纸条", "字条", "卷轴", "画轴",
    "册子", "本子", "日记本", "笔记本", "信纸", "信笺",
})

#: 语境证据：这些说法出现时，「文件/材料/记录」指的是成叠的载体内容，
#: 不是档案夹本身。
CARRIER_CONTEXT_MARKS = (
    "一叠", "一摞", "一堆", "一沓", "厚厚", "几张", "几页",
    "堆满", "摊开", "翻开", "翻阅", "阅读", "打印", "复印",
    "签字", "盖章", "递交", "交到", "提交", "汇报", "归档",
    "邮件里", "文件里", "报告里", "资料里",
)

#: 语境反证：出现这些词时，命中的「文件」很可能只是「文件夹」的一部分，
#: 是真实物，不能升成 info。
CONTAINER_CONTEXT_MARKS = (
    "档案夹", "文件夹", "档案袋", "文件袋", "文件盒", "公文包", "柜子", "抽屉",
)


@dataclass(frozen=True)
class CarrierVerdict:
    """一个名字的形态判定结果。"""

    label: str
    rule: str
    detail: str

    @property
    def is_info(self) -> bool:
        return self.label == INFO


def _match_end(name: str, words: frozenset[str]) -> str | None:
    """名字等于或以后缀形式包含词表里的词；长词优先，避免「文件夹」被拆成「文件」。"""
    if name in words:
        return name
    for word in sorted(words, key=len, reverse=True):
        if len(name) > len(word) and name.endswith(word):
            return word
    return None


def _context_has(context: str, marks: tuple[str, ...]) -> str | None:
    for mark in marks:
        if mark in context:
            return mark
    return None


def classify_name(name: str, context: str | None = None) -> CarrierVerdict:
    """判断一个名字是不是信息载体。

    Args:
        name: 实体规范名。
        context: 可选，mention 语境（或整批正文）。只用于拆 ``REVIEW_WORDS``
            的平局：有「一叠/堆满/递交」等载体证据才升 info，有「文件夹/柜子」
            等实物反证则保持 item。形态明确的词不看语境。

    Returns:
        ``CarrierVerdict``，``label`` 为 ``info`` / ``review`` / ``item``。
    """
    raw = (name or "").strip()
    if not raw:
        return CarrierVerdict(REVIEW, R0, "空写法，无法判定")

    # R3 先判：容器词里的「文件/档案」是真实物，不能被后面的词表截走。
    hit = _match_end(raw, CONTAINER_WORDS)
    if hit:
        return CarrierVerdict(ITEM, R3, f"「{hit}」是装东西的实物，不是载体内容")

    hit = _match_end(raw, ELECTRONIC_WORDS)
    if hit:
        return CarrierVerdict(INFO, R1, f"「{hit}」是电子/数字载体，不是实物")

    hit = _match_end(raw, COLLECTIVE_WORDS)
    if hit:
        return CarrierVerdict(INFO, R2, f"「{hit}」是集合名词，指一堆信息而非一个物件")

    hit = _match_end(raw, DOCUMENT_WORDS)
    if hit:
        return CarrierVerdict(INFO, R5, f"「{hit}」是成篇的信息产品，内容即其全部")

    hit = _match_end(raw, PAPER_ITEM_WORDS)
    if hit:
        return CarrierVerdict(ITEM, R4, f"「{hit}」是可拿、可丢、可转交的纸质实物")

    hit = _match_end(raw, REVIEW_WORDS)
    if hit:
        text = (context or "").strip()
        if text:
            container = _context_has(text, CONTAINER_CONTEXT_MARKS)
            if container:
                return CarrierVerdict(
                    ITEM, R3, f"语境出现「{container}」，「{hit}」是实物的一部分")
            evidence = _context_has(text, CARRIER_CONTEXT_MARKS)
            if evidence:
                return CarrierVerdict(
                    INFO, R2, f"语境出现「{evidence}」，「{hit}」指成叠的载体内容")
        return CarrierVerdict(
            REVIEW, R2, f"「{hit}」两可（信息载体 / 实物档案夹），单列待审")

    return CarrierVerdict(ITEM, R0, "形态无信息载体特征，按物理物件处理")


def gate_kind(name: str, kind: str, context: str | None = None) -> str:
    """判定侧闸门：``kind='item'`` 的信息载体改为 ``info``。

    只降不升：非 item 一律原样返回；``review`` 也原样返回（不硬改）。
    """
    if kind != "item":
        return kind
    return INFO if classify_name(name, context=context).is_info else kind
