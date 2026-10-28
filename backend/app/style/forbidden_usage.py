"""档案禁用词在规则正文里的**语法角色**判定，以及「修档案」的方案生成。

## 为什么要分角色

体检的 `forbidden_conflict` 判据原先是**纯子串命中**：
禁用清单里的词只要在规则正文里出现，就判「档案自相矛盾」
（`error` ⇒ 挡住生成）。

实测（2026-10-28 真机验收）：这条判据把

    场景常先以人物当下一刻的细微反应或即时判断切入

里的「下一刻」判成了转折连词冲突 —— 但这里它是**名词性状语**
（「下一刻的细微反应」＝下一个时刻的反应），跟清单要禁的
「下一刻，X 发生了」这种**转折连词**用法不是一回事。
量化：蒸馏库 4 份档案 2 份命中、验收库 2/2 项目命中 ⇒
真档案几乎必然被挡住生成。

## 角色怎么定（判据：紧跟的助词）

- 紧跟「的 / 地 / 得」⇒ **名词性用法**（词被当名词/状语修饰语用）
  ⇒ 不是清单要禁的那种用法 ⇒ 不判冲突；
- 其它一切用法 ⇒ **连词/短语用法** ⇒ 就是清单要禁的那种用法 ⇒ 判冲突；
- **例外**：禁用项本身含标点、或长度 ≥ 7 字（禁用段尾拔高、解释性对白那类
  **整句模板**）⇒ 一律按字面判。这类东西没有「用法差别」这回事。

为什么判据落在助词上而不是词表：`然而/但是/不过` 在任何位置都只能作连词，
`下一刻/紧接着` 才会出现名词性用法；落在助词上就与具体哪个词无关 ——
换一本书、换一批禁用词、换一个模型蒸馏出来的档案，同样成立。

设计文档见 `docs/style-prompt-audit-repair.md`。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

#: 角色：连词/短语用法（清单要禁的就是这个）⇒ 判冲突。
ROLE_CONNECTIVE = "connective"

#: 角色：名词性用法（被当名词修饰语用）⇒ 不是清单要禁的那种用法。
ROLE_ATTRIBUTIVE = "attributive"

#: 「名词性用法」判据：词后面紧跟的助词。
_ATTRIBUTIVE_MARKERS = ("的", "地", "得")

#: 禁用项里出现这些标点 ⇒ 它是**整句/整段模板**，按字面判，不分角色。
_PUNCT = set("，。！？；：、,.!?;:（）()「」『』《》〈〉【】…—－-～~\"'“”‘’")

#: 整句模板的最小长度（含标点的不看长度）：≥ 这个字数的禁用项按字面判。
_PHRASE_TEMPLATE_MIN_CHARS = 7


def is_phrase_template(word: str) -> bool:
    """这个禁用项是不是「整句/整段模板」。

    模板类（禁用段尾拔高、解释性对白）出现即冲突，没有「用法差别」——
    分角色对它们没有意义，只会漏判。
    """
    w = str(word or "").strip()
    if not w:
        return False
    if any(ch in _PUNCT for ch in w):
        return True
    return len(w) >= _PHRASE_TEMPLATE_MIN_CHARS


#: 角色：判不出来（词出现在文本末尾，后面没字符可看）⇒ 回退到结构化口径。
ROLE_UNKNOWN = ""


def classify_occurrence(text: str, index: int, word: str) -> str:
    """判定 `text[index:index+len(word)]` 这一次出现的角色。

    `index` 必须是该词在 `text` 里的真实起点。
    词后面没字符时返回 `ROLE_UNKNOWN`（判不出来，不猜）。
    """
    if is_phrase_template(word):
        return ROLE_CONNECTIVE
    tail = text[index + len(word):]
    stripped = tail.lstrip()
    if not stripped:
        return ROLE_UNKNOWN
    if stripped[:1] in _ATTRIBUTIVE_MARKERS:
        return ROLE_ATTRIBUTIVE
    return ROLE_CONNECTIVE


@dataclass(frozen=True)
class ForbiddenUsage:
    """一次「禁用词出现在规则正文里」的记录。"""

    #: 命中的禁用词。
    word: str
    #: 命中所在的**禁用分类**（如「禁用转折词」）。
    dimension: str = ""
    #: 命中所在规则的**维度**（如「语序编排」）；定位不到时是空串。
    rule_dimension: str = ""
    #: 角色（见 ROLE_*）。
    role: str = ROLE_CONNECTIVE
    #: 上下文片段（前后各若干字），给人看的。
    context: str = ""
    #: 命中的那条规则原文（定位不到时是空串）。
    rule_text: str = ""


@dataclass
class ForbiddenAnalysis:
    """一份档案的禁用词用法分析结果。"""

    #: 冲突（连词/短语用法）⇒ error，挡住生成。
    conflicts: list[ForbiddenUsage] = field(default_factory=list)
    #: 名词性用法 ⇒ warn，不挡生成，但可见、可修。
    attributive: list[ForbiddenUsage] = field(default_factory=list)

    @property
    def conflict_words(self) -> list[str]:
        out: list[str] = []
        for u in self.conflicts:
            if u.word not in out:
                out.append(u.word)
        return out

    @property
    def attributive_words(self) -> list[str]:
        out: list[str] = []
        for u in self.attributive:
            if u.word not in out:
                out.append(u.word)
        return out


def _iter_rule_items(narrative: dict[str, Any]) -> Iterable[tuple[str, int, str]]:
    """遍历档案里的结构化规则：`(维度, 序号, 规则原文)`。

    规则项有两种形态：蒸馏产出的是 `{"rule": "...", ...}`，
    少数老档案直接存字符串 —— 两种都认。
    """
    rules = (narrative or {}).get("rules")
    if not isinstance(rules, dict):
        return
    for dim, items in rules.items():
        if not isinstance(items, (list, tuple)):
            continue
        for idx, item in enumerate(items):
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                text = str(item.get("rule") or "")
            else:
                text = ""
            if text.strip():
                yield str(dim), idx, text


def _context(text: str, index: int, width: int = 28) -> str:
    start = max(0, index - width)
    end = min(len(text), index + width)
    return text[start:end]


def scan_text(text: str, forbidden: dict[str, Any] | None,
              *, dimension: str = "") -> list[ForbiddenUsage]:
    """扫一段文本，返回其中所有「禁用词出现」的记录（含角色）。

    这是**渲染块**口径：模型看到什么，就判什么。
    """
    out: list[ForbiddenUsage] = []
    if not text or not isinstance(forbidden, dict):
        return out
    for dim, words in forbidden.items():
        if not isinstance(words, (list, tuple)):
            continue
        for w in words:
            word = str(w or "").strip()
            if not word:
                continue
            start = 0
            while True:
                i = text.find(word, start)
                if i < 0:
                    break
                out.append(ForbiddenUsage(
                    word=word,
                    dimension=str(dim),
                    role=classify_occurrence(text, i, word),
                    context=_context(text, i),
                ))
                start = i + len(word)
    return out


def scan_narrative_rules(narrative: dict[str, Any]) -> list[ForbiddenUsage]:
    """扫档案的**结构化规则**，返回带维度与规则原文的记录。

    用途：① 给冲突定位到「哪一维的哪一条」；② 修复动作要作用在结构化数据上。
    """
    out: list[ForbiddenUsage] = []
    forbidden = (narrative or {}).get("forbidden")
    if not isinstance(forbidden, dict):
        return out
    for rule_dim, _idx, text in _iter_rule_items(narrative):
        for u in scan_text(text, forbidden):
            out.append(ForbiddenUsage(
                word=u.word, dimension=u.dimension, rule_dimension=rule_dim,
                role=u.role, context=u.context, rule_text=text,
            ))
    return out


def locate_rule(narrative: dict[str, Any], word: str,
                role: str | None = None) -> tuple[str, str]:
    """在档案里找到含该词的那条规则：返回 `(规则维度, 规则原文)`。

    `role` 给了就只在**该角色**的那次出现上匹配（同一个词在一条规则里
    可能有多种用法）。找不到时返回 `("", "")`。
    """
    for rule_dim, _idx, text in _iter_rule_items(narrative):
        start = 0
        while True:
            i = text.find(word, start)
            if i < 0:
                break
            if role is None or classify_occurrence(text, i, word) == role:
                return rule_dim, text
            start = i + len(word)
    return "", ""


def analyze(narrative: dict[str, Any] | None,
            rendered_rules_text: str | None) -> ForbiddenAnalysis:
    """分析一份档案的禁用词用法。

    `rendered_rules_text` = 渲染块里**禁用清单之前**的那段（模型真正读到的规则正文）。
    传 `None` ⇒ 只按结构化规则判（不做可见性过滤）。

    ## 可见性过滤为什么必须有

    `_clip` 会把过长的规则按标点边界截断 ⇒ 有些规则里的禁用词**根本没进提示词**。
    模型看不见的字串，不该替它挡生成。

    ## 判定口径

    **以渲染块为准**（模型看到什么就判什么）：
    词在结构化规则里是连词用法、但渲染块里那次出现是名词性用法 ⇒ 不判冲突
    （模型看到的是名词性用法那份）。
    唯一回退：渲染块里那次出现**紧贴块尾**（后面没字符了，角色判不出来）
    ⇒ 回退到结构化口径取严（挡住）。
    """
    result = ForbiddenAnalysis()
    if not isinstance(narrative, dict) or not narrative:
        return result

    if rendered_rules_text is None:
        # 没有渲染文本 ⇒ 直接用结构化口径。
        structured = scan_narrative_rules(narrative)
        for u in structured:
            if u.role == ROLE_CONNECTIVE:
                result.conflicts.append(u)
            else:
                result.attributive.append(u)
        return result

    structured = scan_narrative_rules(narrative)
    rendered = scan_text(rendered_rules_text, narrative.get("forbidden"))

    # 角色判不出来时（词贴着块尾）⇒ 回退结构化口径：那个词在规则里
    # 有连词用法就按连词算（取严），否则按名词性用法算。
    structured_roles: dict[str, set[str]] = {}
    for u in structured:
        structured_roles.setdefault(u.word, set()).add(u.role)

    seen: set[tuple[str, str]] = set()
    for u in rendered:
        role = u.role
        if role == ROLE_UNKNOWN:
            roles = structured_roles.get(u.word) or set()
            role = (ROLE_CONNECTIVE if ROLE_CONNECTIVE in roles
                    else (ROLE_ATTRIBUTIVE if ROLE_ATTRIBUTIVE in roles
                          else ROLE_CONNECTIVE))
        key = (u.word, role)
        if key in seen:
            continue
        seen.add(key)
        rule_dim, rule_text = locate_rule(narrative, u.word, role)
        usage = ForbiddenUsage(
            word=u.word, dimension=u.dimension, rule_dimension=rule_dim,
            role=role, context=u.context, rule_text=rule_text,
        )
        if role == ROLE_CONNECTIVE:
            result.conflicts.append(usage)
        else:
            result.attributive.append(usage)
    return result


# ====================================================================
# 修档案：把冲突改掉
# ====================================================================

#: 修法：删掉词（含紧跟的「的」）。
MODE_REMOVE_WORD = "remove_word"

#: 修法：删掉整条规则。
MODE_DROP_RULE = "drop_rule"

#: 修法：从禁用清单里去掉这个词。
MODE_UNBAN = "unban"

REPAIR_MODES = (MODE_REMOVE_WORD, MODE_DROP_RULE, MODE_UNBAN)


@dataclass
class RepairPlan:
    """一次修复的方案（还没落库）。"""

    mode: str
    word: str
    #: 逐条改动：`{"dimension", "before", "after"}`。
    changes: list[dict[str, str]] = field(default_factory=list)
    #: 改完之后的新档案（深拷贝，未落库）。
    narrative: dict[str, Any] = field(default_factory=dict)

    @property
    def affected(self) -> int:
        return len(self.changes)


#: 规则尾部的证据引文（与 `narrative._EVIDENCE_TAIL_RE` 同形）。
#:
#: 为什么要单独认它：证据引文是**参考语料的原句**，渲染进提示词前会被
#: `narrative._strip_evidence` 剥掉 ⇒ 只出现在证据里的禁用词**模型根本看不见**。
#: 修档案时如果连证据一起改，等于把语料原句改坏了（还可能留下一个孤零零的逗号），
#: 而对提示词毫无影响 —— 所以修复只动**规则正文**。
_EVIDENCE_TAIL_RE = re.compile(r"[（(]\s*证据\s*[:：].*?[）)]\s*", re.S)


def _split_evidence(text: str) -> tuple[str, str]:
    """把一条规则拆成 `(进提示词的部分, 证据引文部分)`。"""
    m = _EVIDENCE_TAIL_RE.search(text or "")
    if not m:
        return text or "", ""
    return text[:m.start()], text[m.start():]


def _rule_body(text: str) -> str:
    """这条规则里**会进提示词**的那部分（不含证据引文）。"""
    return _split_evidence(text)[0]


def _remove_word_from_text(text: str, word: str) -> str:
    """从一条规则的**正文**里删掉这个词（连同紧跟的「的」），证据引文一个字不动。

    「下一刻的细微反应」⇒ 「细微反应」—— 规则还在，只是不再含禁用词。
    """
    head, tail = _split_evidence(text)
    pattern = re.compile(re.escape(word) + "的?")
    new_head = pattern.sub("", head)
    new_head = re.sub(r"[ \t]{2,}", " ", new_head)
    return new_head + tail


def plan_repair(narrative: dict[str, Any], word: str, mode: str = MODE_REMOVE_WORD
                ) -> RepairPlan | None:
    """算出一份修复方案（**不落库**）。

    返回 `None` 表示这个词在档案里找不到（没什么可修）。
    """
    if not isinstance(narrative, dict) or not narrative:
        return None
    word = str(word or "").strip()
    mode = str(mode or MODE_REMOVE_WORD)
    if not word or mode not in REPAIR_MODES:
        return None

    new = copy.deepcopy(narrative)
    changes: list[dict[str, str]] = []

    if mode == MODE_UNBAN:
        forbidden = new.get("forbidden")
        if not isinstance(forbidden, dict):
            return None
        for dim, words in list(forbidden.items()):
            if not isinstance(words, (list, tuple)) or word not in words:
                continue
            before = "、".join(str(w) for w in words)
            forbidden[dim] = [w for w in words if str(w) != word]
            after = "、".join(str(w) for w in forbidden[dim])
            changes.append({"dimension": str(dim), "before": before, "after": after})
        if not changes:
            return None
        return RepairPlan(mode=mode, word=word, changes=changes, narrative=new)

    if mode == MODE_DROP_RULE:
        rules = new.get("rules")
        if not isinstance(rules, dict):
            return None
        for dim, items in list(rules.items()):
            if not isinstance(items, (list, tuple)):
                continue
            keep = []
            for item in items:
                text = item if isinstance(item, str) else (
                    str(item.get("rule") or "") if isinstance(item, dict) else "")
                # 只在**规则正文**里出现才算数：只在证据引文里的词模型看不见，
                # 为它删掉整条规则是白删（还会把一条好规则弄丢）。
                if word in _rule_body(text):
                    changes.append({"dimension": str(dim), "before": text, "after": ""})
                    continue
                keep.append(item)
            rules[dim] = keep
        if not changes:
            return None
        return RepairPlan(mode=mode, word=word, changes=changes, narrative=new)

    # MODE_REMOVE_WORD
    for dim, _idx, text in _iter_rule_items(narrative):
        if word not in _rule_body(text):
            continue
        after = _remove_word_from_text(text, word)
        if after == text:
            continue
        changes.append({"dimension": str(dim), "before": text, "after": after})
    if not changes:
        return None
    rules = new.get("rules")
    if isinstance(rules, dict):
        for dim, items in list(rules.items()):
            if not isinstance(items, (list, tuple)):
                continue
            fixed = []
            for item in items:
                if isinstance(item, str):
                    fixed.append(_remove_word_from_text(item, word))
                elif isinstance(item, dict):
                    item = dict(item)
                    if item.get("rule"):
                        item["rule"] = _remove_word_from_text(str(item["rule"]), word)
                    fixed.append(item)
                else:
                    fixed.append(item)
            rules[dim] = fixed
    return RepairPlan(mode=mode, word=word, changes=changes, narrative=new)
