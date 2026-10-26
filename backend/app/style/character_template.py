"""人物塑造模板：原创路径缺的那一块「怎么塑造人物」。

## 它是什么

一份从参考语料归纳出来的、**可迁移到任何新书**的人物塑造方法。

不是参考书的人物。参考书的人物在原创模式下**不可见**（`roles=characters` 层
不给读，见 `layers.py`），所以「这个人怎么说话」这条线索在原创路径上是断的 ——
模板补的正是这一块：**怎么塑造一个人物**（而不是**这个人**怎么说话）。

## 为什么不能只靠提示词说「别写人名」

分层结构的教训（`layers.py`）：原创模式早年是**逐条剥离实体**的，
实测 791 条规则剥完仍带完整剧情（「贵客抵达→王府开中门并铺陈仪仗→
以糖葫芦诱引小王爷」）。剥离必然漏。

所以这块**两条腿走路**：
1. 抽取时只收「方法」不收「内容」；
2. 收完再过一遍**专名表** —— 命中任何专名（人名/地名/门派/物件）就丢。

## 没有专名表就不产

专名表是这条防线的**唯一依据**。表建不出来（LLM 判定失败、被清空）时，
这块**整块不产**：宁可原创路径少一块方法论，也不要把参考书的人物漏进提示词。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

#: 档案里的键（与 `rules` / `forbidden` 平级）。
CHARACTER_TEMPLATE_KEY = "character_template"

#: 维度名（解析时用；不进 `rules`，它是独立的一块）。
CHARACTER_TEMPLATE_DIMENSION = "人物塑造模板"

#: 单条规则的长度上限（方法论短句；超了就不是"方法"而是复述）。
MAX_RULE_CHARS = 80

#: 规则条数上限（每块 chunk 收 0~3 条，够密也不撑爆档案）。
MAX_RULES_PER_CHUNK = 3

#: 一份模板产物最多留多少条（不同块常给出同一条做法，去重后仍要封顶：
#: 档案不该无限膨胀，提示词也要有上限）。
MAX_TEMPLATE_RULES = 12

#: 明显是"这段里发生了什么"而不是"怎么塑造"的收尾语 —— 出现即丢。
_PLOT_MARKERS = (
    "本章", "这一章", "第章", "上一章", "下一章", "前文", "后文",
    "原文", "文中", "作者写", "作者用", "小说里", "本书",
)


def build_character_template_prompt(chunk_text: str, chunk_label: str) -> tuple[str, str]:
    """构造「人物塑造模板」的抽取 prompt。

    要点是**只写方法不写内容**：模型被明确告知，输出会被用来指导另一本
    人物完全不同的书，所以任何具体人名/地名/门派名/物件名、任何"这段里
    发生了什么"都是错的。
    """
    system = (
        "你是一位资深中文网文编辑。你的任务是从给定文本中提炼"
        "**作者是怎么塑造人物的**。\n\n"
        "不是总结剧情，也不是描述某个角色；提炼的是**可迁移的手法**。\n"
        "例如：\n"
        "- 先给一个动作再给一句评价，让读者自己下结论\n"
        "- 用旁人的反应侧写主角，不直接写他的内心\n"
        "- 让人物在小事上露出性格，比让他自述更有效\n"
        "- 给每个人物一个想要的东西和一个拦着他的东西\n"
        "- 重要的话不让他说出口，用对方的反应带出来\n\n"
        "## 关键约束：只写方法，不写内容\n\n"
        "你的输出会被用来指导**另一本小说**，那本书的人物和事件完全不同。所以：\n"
        "- 不要写具体人名、地名、门派名、物件名\n"
        "- 不要写这段里发生了什么（谁做了什么、谁说了哪句话）\n"
        "- 不要引用原文句子\n"
        "- 每条必须**可迁移**：换一组人物、换一个场景依然成立\n"
        "- 每条是一句**做法**，不是一句评价（「用侧写代替自述」而不是"
        "「侧写写得很好」）\n\n"
        "**正确的写法**：\n"
        "  先给动作再给评价，让读者自己得出结论\n"
        "  用旁人的反应侧写主角，主角自己不自述\n"
        "**错误的写法**（含具体内容，严禁）：\n"
        "  老黄在江边咳了两声，掩饰自己的不舍\n\n"
        "输出格式（JSON，0~3 条）：\n"
        '{\n'
        '  "人物塑造模板": [\n'
        '    "一句可迁移的做法"\n'
        '  ]\n'
        '}\n'
    )
    user = (
        f"语料片段（{chunk_label}）：\n\n"
        f"{chunk_text}\n\n"
        "请只提炼**塑造人物的手法**，不要写这段里发生了什么。"
    )
    return system, user


def parse_character_template(raw: Any) -> list[str]:
    """从模型响应里取出规则列表（宽容解析：坏了就当没有）。"""
    if not isinstance(raw, str) or not raw.strip():
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    items = parsed.get(CHARACTER_TEMPLATE_DIMENSION)
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= MAX_RULES_PER_CHUNK:
            break
    return out


def _mentions_entity(rule: str, entity_names: Sequence[str]) -> bool:
    """规则里是否出现了参考语料的专名。"""
    for name in entity_names:
        if name and len(name) >= 2 and name in rule:
            return True
    return False


def entity_free_rules(
    rules: Iterable[str],
    entity_names: Sequence[str],
    *,
    chunk_text: str = "",
) -> tuple[list[str], int]:
    """筛出**零专名**的方法论规则。

    三道关：
    1. 长度上限（超了就不是"方法"）；
    2. 不含任何专名（人名/地名/门派/物件）；
    3. 不含"这段里发生了什么"的收尾语，也不照抄原文。

    返回 `(留下的规则, 被丢掉的条数)`。
    """
    kept: list[str] = []
    dropped = 0
    names = [n for n in entity_names if isinstance(n, str) and len(n.strip()) >= 2]
    for rule in rules:
        if not isinstance(rule, str):
            dropped += 1
            continue
        text = rule.strip()
        if not text or len(text) > MAX_RULE_CHARS:
            dropped += 1
            continue
        if _mentions_entity(text, names):
            dropped += 1
            continue
        if any(marker in text for marker in _PLOT_MARKERS):
            dropped += 1
            continue
        if chunk_text and text in chunk_text:
            # 照抄原文 = 没有抽象
            dropped += 1
            continue
        kept.append(text)
    return kept, dropped


def describe_character_template(
    profile: dict | None,
    max_rules: int = 6,
) -> str:
    """把人物塑造模板渲染成一段可注入的文字（没有就返回空串）。

    确定性：同一份档案永远产出同一段文本（模板进的是**静态** system 块，
    变一次就击穿提示词缓存）。
    """
    if not isinstance(profile, dict):
        return ""
    section = profile.get(CHARACTER_TEMPLATE_KEY)
    if not isinstance(section, dict):
        return ""
    rules = section.get("rules")
    if not isinstance(rules, list):
        return ""
    picked = [str(r).strip() for r in rules[:max_rules] if isinstance(r, str) and r.strip()]
    if not picked:
        return ""
    lines = "\n".join(f"- {r}" for r in picked)
    return (
        "【人物塑造方法】（从参考语料归纳的做法，不含任何具体人物，可直接套用）\n"
        f"{lines}\n"
    )


# ====================================================================
# 存放：按档位分开，id 自带档位
# ====================================================================

#: 产物 id 的三段：档位 : 档位配置哈希 : 语料正文哈希前 8 位。
#:
#: 一眼就能看出「哪一档、哪次配置、哪一版语料」，不用再翻表。
def template_id(tier: str, tier_signature: str, content_hash: str) -> str:
    return f"{tier}:{tier_signature}:{(content_hash or '')[:8]}"


def save_template(
    db: Any,
    project: Any,
    *,
    tier: str,
    tier_signature: str,
    content_hash: str,
    source_ids: list[int],
    source_ids_hash: str,
    rules: list[str],
    n_chunks: int | None = None,
    rejected: int | None = None,
) -> Any:
    """把模板产物写进库（**按 id 幂等 upsert**）。

    同一个 (语料, 档位, 档位配置, 正文) 永远只有一行；重抽同一条会更新它，
    不会堆出第二行同义的产物。
    """
    from app.models import CharacterTemplate
    row_id = template_id(tier, tier_signature, content_hash)
    row = db.get(CharacterTemplate, row_id)
    if row is None:
        row = CharacterTemplate(id=row_id)
        db.add(row)
    row.tier = tier
    row.tier_signature = tier_signature
    row.content_hash = content_hash
    row.source_ids = sorted(int(i) for i in source_ids)
    row.source_ids_hash = source_ids_hash
    row.rules = list(rules)
    row.n_chunks = n_chunks
    row.rejected = rejected
    row.created_by_project_id = int(project.id)
    db.commit()
    db.refresh(row)
    return row


def find_template(
    db: Any,
    *,
    tier: str,
    tier_signature: str,
    content_hash: str,
) -> Any | None:
    """按 (档位, 档位配置, 正文) 找一条模板产物。"""
    from app.models import CharacterTemplate
    return db.get(CharacterTemplate, template_id(tier, tier_signature, content_hash))


def apply_template(db: Any, project: Any, row: Any) -> Any:
    """把一条模板产物应用到项目（项目记住它的 id）。"""
    from datetime import datetime, timezone
    project.character_template_id = row.id
    row.applied_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(project)
    return project


def applied_template_rules(db: Any, project: Any, tier: str) -> list[str]:
    """项目当前档位下、已应用的模板 → 规则列表（没有就空列表）。

    **档位不一致就不给**：项目切了档，模板是按旧档采样的 —— 拿来注入就是
    「拿 A 档的方法论配 B 档的档案」，静默的不一致比没有更糟。
    """
    from app.models import CharacterTemplate
    row_id = getattr(project, "character_template_id", None)
    if not row_id or not tier:
        return []
    row = db.get(CharacterTemplate, row_id)
    if row is None or row.tier != tier:
        return []
    return [str(r) for r in (row.rules or []) if isinstance(r, str) and r.strip()]


def applied_template_block(db: Any, project: Any, tier: str) -> str:
    """项目当前档位下、已应用的模板 → 可注入的文字（没有就空串）。"""
    return describe_character_template(
        {CHARACTER_TEMPLATE_KEY: {"rules": applied_template_rules(db, project, tier)}})


def list_templates(db: Any, *, limit: int = 50) -> list[Any]:
    """库里的模板产物（新的在前）。**id 自带档位**，列表里一眼能分档。"""
    from sqlalchemy import select

    from app.models import CharacterTemplate
    return list(db.scalars(
        select(CharacterTemplate).order_by(CharacterTemplate.created_at.desc())
        .limit(limit)).all())
