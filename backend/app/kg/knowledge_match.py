"""secret/event 类情报的匹配与判别（known_kind != entity 的消费路径工具）。

背景：extractor 产出三类 known_kind，但 entity 之外的两类长期空跑——
写侧把 knows 当实体名解析（secret 文本解析不到实体 → 整条丢弃），
读侧把 known_entity_id 一律 join kg_entities（secret 行 known_entity_id=NULL → 不渲染）。

本模块两个独立判断，都守「漏报优于误报」纪律：

event_summary_match(query, summary)
  known_kind=event 的解析：LLM 的事件简述 vs 库内 kg_events.summary。
  1. 去空白后全等
  2. 原文双向包含（LLM 简述是库内 summary 的缩写/超集）
  3. 剥掉标点/空白后的双向包含（措辞只差标点）
  全不中 → False，调用方降级 known_text 存文本（情报不丢）。
  **刻意不做**词频/bigram 模糊相似：两个不同事件常只差一个动词
  （分手/和好、背叛/和解），模糊匹配必错链——错情报比缺情报糟。

is_incident_reference(text)
  knows 文本是在「指称一个事件」还是在「命名一个秘密」：
  - "X的Y" 领属结构且 Y 非时间词（"父亲的死因"→死因、"身世之谜"类）→ secret
  - 领属尾巴是时间词（"烧毁账本的今晚"→今晚）或整句以时间词开头
    （"当晚，账本被烧毁"）或整句就是时间词（"今晚"）→ 事件引用
  - 整体命中某实体 canonical_name → 不是事件（调用方升级为 entity）
  都不命中 → 默认 secret（保守：secret 只影响 prompt 标注与 warn 级
  检查，误判为 secret 的代价远小于误判为 event 的错链代价）。
"""
from __future__ import annotations

import re

#: 剥标点/空白（保留中英文与数字——事件简述可能含"2018年"）
_STRIP_RE = re.compile(r"[^一-鿿A-Za-z0-9]+")

#: 领属结构（"X的Y"）尾巴上的时间词 → 整体是事件引用而非秘密
_TIME_TAILS = {
    "今晚", "今夜", "今日", "今天", "昨晚", "昨夜", "昨日",
    "那晚", "那夜", "那年", "当晚", "当夜", "当年", "此刻", "此时",
}

#: 整句开头的时间词（"当晚，账本被烧毁"型无领属标记的事件引用）
_TIME_LEADS = _TIME_TAILS | {
    "清晨", "黎明", "清晨", "中午", "黄昏", "傍晚", "午夜", "深夜",
    "上月", "本月", "下月", "今年", "去年", "明年", "方才", "刚才", "此刻",
}


def normalize_fact(s: str) -> str:
    """事实文本规范化：剥标点/空白，只留中英文与数字（秘密文本匹配用）。"""
    return _STRIP_RE.sub("", s or "")


def event_summary_match(query: str, summary: str) -> bool:
    """LLM 事件简述是否指向库内某条 event.summary（保守三层）。"""
    q = (query or "").strip()
    s = (summary or "").strip()
    if not q or not s:
        return False
    if q == s:
        return True
    if q in s or s in q:
        return True
    nq, ns = normalize_fact(q), normalize_fact(s)
    if len(nq) >= 4 and nq in ns:
        return True
    if len(ns) >= 4 and ns in nq:
        return True
    return False


def is_incident_reference(text: str, entity_names: set[str] | None = None) -> bool:
    """knows 文本是否在指称一个事件（True=event / False=secret）。

    entity_names：项目实体 canonical_name 集合——text 完全等于某实体名时
    返回 False（调用方应把它当 entity 处理，而不是 secret）。
    """
    text = (text or "").strip()
    if not text:
        return False
    if entity_names and text in entity_names:
        return False
    # 1) 领属结构：最后一个 的/之 的尾巴——抽象名词 → 秘密；时间词 → 事件
    for sep in ("的", "之"):
        if sep in text:
            tail = text.rsplit(sep, 1)[1]
            return tail in _TIME_TAILS  # "父亲的死因"→False；"烧毁账本的今晚"→True
    # 2) 整句以时间词开头（"当晚，账本被烧毁"）或整句是时间词（"今晚"）
    if any(text.startswith(t) for t in _TIME_LEADS):
        return True
    # 3) 默认：秘密（保守）
    return False
