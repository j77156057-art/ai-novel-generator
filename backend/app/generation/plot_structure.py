"""规划层的「剧情结构」参考：作者**怎么搭故事**，而不只是怎么写句子。

## 为什么单独一层

蒸馏产物里有一千多条讲**故事怎么排**的规则（剧情编排模式 / 节拍序列 / 高频场景模板 /
伏笔与悬念设计，合计 1280 条）。它们以前只进**正文层**（`describe_narrative`），
当"怎么写"用；**规划层**（剧情圣经 + 因果节点）一份也看不到。

结果就是：模型写大纲时手里只有 premise + 类型套话提示 + 平台调性提示，
于是产出的是**它默认的网文套路**——句子可能像作者，骨架却是通用的。

## 三条纪律

1. **只取结构四维**：句子怎么写（语序/段落/对白/战斗）由正文层负责，这里不重复。
2. **专名安全**：结构规则里可能有参考书的人名地名。**没有可用专名表就不给**
   （fail closed）——给了等于把参考书世界观灌进用户的剧情规划。
3. **按上下文挑，不全塞**：一次规划提示词不能塞 1280 条。按与剧情上下文的字面重合度
   每维取几条（确定性，不需要检索服务）。
"""
from __future__ import annotations

import re
from typing import Any

#: 结构四维：只讲「故事怎么排」，不讲「句子怎么写」。
STRUCTURE_DIMS: tuple[str, ...] = (
    "剧情编排模式",     # 情节怎么组织
    "节拍序列",         # 一场冲突的 5~7 步推进
    "高频场景模板",     # 某类场景通常怎么展开
    "伏笔与悬念设计",   # 钩子 / 伏笔 / 回收
)

#: 每维给规划层的条数上限。
DEFAULT_PER_DIM = 4

_EVIDENCE = re.compile(r'（证据[:：]\s*([\s\S]+?)\s*[)）]\s*$')


def _rule_text(item: Any) -> str:
    """规则正文：剥掉内嵌引文（视图里不该带参考书原句）。"""
    raw = item.get("rule") if isinstance(item, dict) else str(item)
    raw = (raw or "").strip()
    match = _EVIDENCE.search(raw)
    if match:
        raw = raw[:match.start()].strip()
    return raw


def structure_rules(profile: dict | None, *, redactor=None) -> dict[str, list[str]]:
    """从档案里取结构四维的规则正文（剥引文；给了 redactor 就再脱敏一次）。"""
    from app.style.layers import method_rules

    method = method_rules(profile)
    out: dict[str, list[str]] = {}
    for dim in STRUCTURE_DIMS:
        items: list[str] = []
        for item in (method.get(dim) or []):
            text = _rule_text(item)
            if not text:
                continue
            if redactor is not None:
                try:
                    text = redactor.redact(text)
                except Exception:  # noqa: BLE001 - 脱敏失败就不给这条
                    continue
            if text and text not in items:
                items.append(text)
        if items:
            out[dim] = items
    return out


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text or "")
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def select(rules: dict[str, list[str]], context: str, *,
           per_dim: int = DEFAULT_PER_DIM) -> dict[str, list[str]]:
    """按与上下文的字面重合度挑每维最相关的几条（确定性，不依赖检索服务）。"""
    grams = _bigrams(context)
    out: dict[str, list[str]] = {}
    for dim, items in (rules or {}).items():
        scored = []
        for index, text in enumerate(items):
            overlap = len(grams & _bigrams(text))
            scored.append((-overlap, index, text))
        scored.sort()
        out[dim] = [text for _s, _i, text in scored[:max(1, per_dim)]]
    return out


def copy_check(rules: dict[str, list[str]], nodes: list[dict], *,
              window: int = 12) -> dict[str, Any]:
    """检测生成节点是否**照搬**了参考规则里的具体情节。

    ## 为什么要有机制校验

    提示里写了"不得照搬其中的人物、地名与具体情节"，但提示层约束没有强制力：
    模型完全可能把某条规则的**具体动作链**（"贵客抵达→开中门并铺陈仪仗→以糖葫芦诱引小王爷"）
    原样搬进自己的节点。学到结构（节拍顺序、钩子位置）是我们要的，搬走具体情节是我们不要的。

    ## 判据

    规则文本与节点文本若共享一段**连续 ≥window 字的逐字片段**，就是照搬
    （结构复用不会产生逐字长片段；逐字长片段只可能来自抄）。
    """
    fields = ("title", "premise", "motivation", "action", "result", "cost", "state_after")
    matches: list[dict[str, Any]] = []
    for index, node in enumerate(nodes or []):
        if isinstance(node, str):
            text = node
        elif isinstance(node, dict):
            text = " ".join(str(node.get(f) or "") for f in fields)
        else:
            continue
        grams = {text[i:i + window] for i in range(max(0, len(text) - window + 1))}
        if not grams:
            continue
        for dim, items in (rules or {}).items():
            for rule in items:
                for start in range(0, max(0, len(rule) - window + 1)):
                    piece = rule[start:start + window]
                    if piece in grams:
                        matches.append({'node_index': index, 'dimension': dim,
                                        'shared_text': piece,
                                        'rule_excerpt': rule[max(0, start - 20):start + window + 20]})
                        break
    return {
        'verdict': 'possible_copy' if matches else 'clean',
        'window': window,
        'matches': matches[:20],
        'match_count': len(matches),
    }


class CopyViolation(Exception):
    """生成内容把参考规则里的原文**逐字搬了过来**。

    判据是「与参考共享一段连续 ≥window 字的逐字片段」：结构复用不会产生
    逐字长片段，逐字长片段只可能来自抄。

    它是**可重试**的失败：把命中的片段回灌给模型，让它用自己的措辞重写，
    比直接报错更可能一次改对。`retry_hint` 就是回灌用的提示词。
    """

    #: 失败分类 —— 调用方据此决定错误码（不再归到 invalid_model_structure）。
    code = 'possible_copy'

    def __init__(self, report: dict):
        self.report = report
        self.matches = list(report.get('matches') or [])
        self.detail = f"possible_copy: {len(self.matches)} 处与参考逐字相同"
        super().__init__(self.detail)
        self.retry_hint = self._build_hint(self.matches)

    @staticmethod
    def _build_hint(matches: list[dict]) -> str:
        lines = [f"  - 「{m.get('shared_text') or ''}」（维度：{m.get('dimension')}）"
                 for m in matches[:5]]
        return (
            "\n\n—— 纠正照搬问题 ——\n"
            "你上一次的输出被丢弃了：它把参考规则里的原句**逐字搬了过来**。\n"
            "以下片段与参考逐字相同：\n" + "\n".join(lines) + "\n"
            "请重新输出，并做到：\n"
            "  1. 不出现上面这些片段，也不要出现与参考规则任何 12 字以上的逐字相同片段；\n"
            "  2. **可以学结构**（节拍顺序、冲突类型、钩子位置、场景模板的骨架），\n"
            "     但情节、动作链、措辞必须是你自己的表达；\n"
            "  3. 只输出 JSON 对象本身，不要解释。"
        )


def assert_no_copy(rules: dict[str, list[str]], texts, *, window: int = 12) -> dict:
    """照搬校验：**命中就抛** `CopyViolation`（可重试），否则返回 clean 报告。

    传入的 `rules` 必须是**真正进了提示词的那部分**参考 —— 模型看不到的规则，
    与它产出相同只可能是巧合，不能算照搬。
    """
    report = copy_check(rules, texts, window=window)
    if report['verdict'] == 'possible_copy':
        raise CopyViolation(report)
    return report


def for_project(db, project, context: str, *,
                per_dim: int = DEFAULT_PER_DIM) -> tuple[dict[str, list[str]], str]:
    """给规划层取结构参考。返回 (选择结果, 说明)。

    没有已落库档案 / 没有结构四维 / 没有可用专名表 → 一律不给（fail closed），
    并在说明里写清原因，让调用方能把它显示出来而不是静默降级。
    """
    profile = getattr(project, "style_narrative", None)
    if not profile:
        return {}, "无已落库的叙事档案 → 不给剧情结构参考"
    rules = structure_rules(profile)
    if not rules:
        return {}, "档案里没有结构四维 → 不给剧情结构参考"
    novel_ids = list(getattr(project, "reference_novel_ids", None) or [])
    redactor = None
    if novel_ids:
        from app.style.entities import Redactor
        from app.style.entity_store import entity_entries, load_table
        table = load_table(int(novel_ids[0]))
        if entity_entries(table):
            redactor = Redactor(table)
    if redactor is None:
        return {}, "无可用专名表 → 结构规则可能含参考书专名，不给（fail closed）"
    rules = structure_rules(profile, redactor=redactor)
    picked = select(rules, context, per_dim=per_dim)
    total = sum(len(v) for v in rules.values())
    given = sum(len(v) for v in picked.values())
    return picked, f"剧情结构参考：{total} 条候选 → 每维取 {per_dim} 条（实际给出 {given} 条）"
