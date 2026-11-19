"""plot_bible 级联协议：版本化 + 冲突检测 + KG 联动。

设计纪律（继承 batch_worker 的「可观测性先于正确性」）：
- 写不删：bible 改了什么留什么（kg_bible_revisions 表 + diff + 旧 facts 不删）
- 算不算冲突留给前端：返回 conflicts 列表即可，**不**自动改 KG
- 漏报优于误报：冲突判定只扫显式字段（characters/relations/locations），
  避免误把"角色升职了 vs KG 里没"也报成冲突

不做（收窄）：
- 不重抽旧段（事实不能改写）
- 不做 LLM 冲突判定（成本/准确率）
- 不存"修改前"双状态（设计文档决策 A：只存修改后 + diff）
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.kg.book_scope import book_for_project
from app.kg.entity_lookup import resolve_entity_by_name
from app.models import KgBibleRevision, KgEntity, KgForeshadow, Project


def _bible_dict(bible: Any) -> dict:
    """归一化：DB 列可能存 None / str（破损数据）/ 任意 dict——都按 dict 算。"""
    if not isinstance(bible, dict):
        return {}
    return bible


# ======================================================================
# bible 键契约（B2）：渲染器支持的键是一张**封闭集合**
# ======================================================================
#
# 之前没有这张表，键「恰好被某个模块读了」才算生效；没人读的键写进去
# 不报错、不告警、md5 一字节不变——用户以为改了设定，其实模型根本没看到。
# 现在：写入口（POST /bible/apply 与 PATCH plot_bible）都过本表，
# 支持之外一律给 warning，不再静默。
#
# 维护规则：**新增键必须同时改这里和真正读取它的模块**，否则测试
# `tests/test_bible_key_contract.py` 的一致性检查会红。

#: 生成 / 审计链路**确实读取**的 bible 顶层键。
SUPPORTED_BIBLE_KEYS: frozenset[str] = frozenset({
    # prompts.py（system 角色/伏笔 + user 动态指引）
    "characters", "relationships", "foreshadowing",
    "synopsis", "direction", "conflicts",
    # power_prompt.py（量级与体系块）
    "power_ladder", "量级阶梯",
    # power_system_type.py（体系类型）
    "system_type", "体系类型", "power_system_type",
    "境界体系类型", "realm_system_type", "systems",
    # sentinel.py（境界事件抽取，不进生成提示词但确实被使用）
    "realm_order",
    # era_check.py（时代基线，开关打开时进 system）
    "baseline", "world_baseline", "世界基线", "时代基线", "时代背景", "era",
    # era_check.py 的世界观容器（容器内再读 baseline 等同组键）
    "world", "world_setting", "worldview", "世界观", "设定", "setting", "settings", "背景",
})

#: 不进生成、但由向导写入的元数据（降级说明等），不算无效键。
BIBLE_META_KEYS: frozenset[str] = frozenset({"note"})

#: 顶层「已知没有承载概念」的键：写入仍然接受，但必须告警它不会被使用。
KNOWN_UNSUPPORTED_BIBLE_KEYS: dict[str, str] = {
    "功法": "v3 里功法只归 power_system（可修炼体系）或 item（秘籍实物），"
            "bible 没有承载它的键；写在这里不会被任何提示词读取。",
    "等级": "bible 没有角色等级字段。境界请写 characters[].境界 或 "
            "characters[].修炼.当前境界；强弱请写量级字段。",
    "品阶": "角色没有品阶字段（品阶只存在于 v3 item 的 attrs.grade），"
            "不会进入生成提示词。",
    "power_level": "未登记的量级写法。请改用 characters[].量级 / 量级细层 / 量级序号。",
    "level": "未登记的量级写法。请改用 characters[].量级 / 量级细层 / 量级序号。",
    "gender": "顶层 gender 没有读取者。角色性别请写 characters[].gender（或 性别）。",
    "first_chapter": "顶层 first_chapter 没有读取者。角色首次出场请写 "
                     "characters[].first_chapter（或 首次出场章节）。",
    "background": "顶层 background 没有读取者。角色背景请写 characters[].background"
                  "（或身世/来历）；世界基线请写 设定/背景 容器里的 baseline。",
    "身世": "顶层 身世 没有读取者。请写 characters[].身世。",
    "来历": "顶层 来历 没有读取者。请写 characters[].来历。",
}

#: 角色卡里**确实被渲染**的字段。
SUPPORTED_CHARACTER_KEYS: frozenset[str] = frozenset({
    # prompts.py：system 角色行
    "name", "名字", "role", "motivation",
    "background", "身世", "来历", "背景",
    # prompts.py：角色行口吻与台词示例
    "voice", "sample_lines",
    # prompts.py：角色行性别与代词（gender/性别）
    "gender", "性别",
    # prompts.py：按章过滤「本章可用角色」（first_chapter/首次出场章节）
    "first_chapter", "首次出场章节", "首次出场",
    # power_prompt.py：量级与体系块
    "境界", "修炼", "量级", "量级细层", "量级序号", "出手规模",
})

#: 角色卡里「已知没有渲染器」的字段：写入接受，但告警。
KNOWN_UNSUPPORTED_CHARACTER_KEYS: dict[str, str] = {
    "功法": "角色卡没有功法字段；功法只能作为 v3 的 power_system/item 存在。",
    "等级": "角色卡不渲染等级。境界请用 境界/修炼.当前境界；强弱请用量级字段。",
    "品阶": "角色卡不渲染品阶；品阶只用于 v3 item。",
    "power_level": "不渲染。请用 量级 / 量级细层 / 量级序号。",
    "level": "不渲染。请用 量级 / 量级细层 / 量级序号。",
    "goal": "只用于 apply 时的 KG 冲突扫描，不进入生成提示词；请用 motivation。",
    "current_goal": "只用于 apply 时的 KG 冲突扫描，不进入生成提示词；请用 motivation。",
}

#: 角色卡 `修炼` 子字段里被 power_prompt 读取的键。
SUPPORTED_CULTIVATION_KEYS: frozenset[str] = frozenset({"当前境界"})

#: relationships[] 元素里「关系类型」的键写法。
#:
#: 规范键是 `relation`（渲染 prompts.py、前端 ProjectWorkspace、向导落库都用它），
#: 但历史上 `type` / `rel_type` 也被人手写过，冲突扫描曾**只认后两个**，
#: 导致规范写法永远扫不到冲突（B4）。这里把仓库里实际出现的写法收成一张表，
#: 读侧一律走 `_first_text` 归一，写侧保持原键不改名。
RELATION_TYPE_KEYS: tuple[str, ...] = ("relation", "type", "rel_type", "关系", "关系类型")

#: relationships[] 元素里关系两端的键写法（a/from/source 与 b/to/target）。
RELATION_FROM_KEYS: tuple[str, ...] = ("a", "from", "source", "主体")
RELATION_TO_KEYS: tuple[str, ...] = ("b", "to", "target", "客体")


def _first_text(rel: dict, keys: tuple[str, ...]) -> str:
    """按写法顺序取第一个非空值（字符串化并去空白）。"""
    for key in keys:
        value = rel.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""

#: 世界设定容器内被 era_check 读取的基线键（与 era_check._BASELINE_KEYS 对齐）。
_WORLD_BASELINE_KEYS: tuple[str, ...] = (
    "baseline", "world_baseline", "世界基线", "时代基线", "时代背景", "era",
)


def _warning(path: str, disposition: str, message: str) -> dict:
    return {
        "path": path,
        "disposition": disposition,   # unsupported / unknown / shape
        "message": message,
    }


def _world_container_ok(value: Any) -> bool:
    """容器必须是 dict，且包含至少一个非空的基线键——否则 era_check 读不到。"""
    if not isinstance(value, dict):
        return False
    return any(str(value.get(k) or "").strip() for k in _WORLD_BASELINE_KEYS)


def _card_has_scale(card: dict) -> bool:
    """角色卡是否有显式量级证据（power_prompt._explicit_scale 同口径）。"""
    nested = card.get("量级")
    raw = {**card, **nested} if isinstance(nested, dict) else card
    return any(raw.get(k) not in (None, "") for k in ("量级", "量级细层", "量级序号"))


# ── 角色卡：性别 / 首次出场章节（生成侧按章过滤与代词渲染的读侧口径）──

_GENDER_ALIASES: dict[str, str] = {
    "女": "女", "女性": "女", "女生": "女", "female": "女", "f": "女", "woman": "女", "girl": "女",
    "男": "男", "男性": "男", "男生": "男", "male": "男", "m": "男", "man": "男", "boy": "男",
    "其他": "其他", "其它": "其他", "非二元": "其他", "other": "其他", "nonbinary": "其他",
}

_GENDER_KEYS: tuple[str, ...] = ("gender", "性别")
_FIRST_CHAPTER_KEYS: tuple[str, ...] = (
    "first_chapter", "首次出场章节", "首次出场", "first_appearance_chapter",
)

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(s: str) -> int | None:
    """中文数字 → int（0~999）。解析不了返回 None。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section = 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch == "十":
            total += (section or 1) * 10
            section = 0
        elif ch == "百":
            total += (section or 1) * 100
            section = 0
        else:
            return None
    return total + section


def character_gender(card: Any) -> str:
    """角色卡性别，归一为「男 / 女 / 其他」；没写或认不出返回空串（不猜）。

    兼容三种落点：
    - 平铺：`{"性别": "女"}` / `{"gender": "female"}`
    - 角色卡分组：`{"身份": {"性别": "女"}}`
    - 圣经角色条目：`{"card": {"身份": {"性别": "女"}}}`
    """
    if not isinstance(card, dict):
        return ""
    sources: list[dict] = [card]
    ident = card.get("身份")
    if isinstance(ident, dict):
        sources.append(ident)
    nested = card.get("card")
    if isinstance(nested, dict):
        sources.append(nested)
        nested_ident = nested.get("身份")
        if isinstance(nested_ident, dict):
            sources.append(nested_ident)
    for src in sources:
        for key in _GENDER_KEYS:
            raw = src.get(key)
            if raw in (None, ""):
                continue
            text = str(raw).strip().lower()
            if text in _GENDER_ALIASES:
                return _GENDER_ALIASES[text]
    return ""


def character_pronoun(card: Any) -> str:
    """按性别给出第三人称代词；未知返回空串。"""
    return {"女": "她", "男": "他"}.get(character_gender(card), "")


def character_first_chapter(card: Any) -> int | None:
    """角色首次出场章节（1-based）。支持 3 / "3" / "第3章" / "第三章"。"""
    if not isinstance(card, dict):
        return None
    for key in _FIRST_CHAPTER_KEYS:
        raw = card.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = int(raw)
            return value if value > 0 else None
        text = str(raw).strip()
        if not text:
            continue
        m = re.search(r"(?:第\s*)?([0-9]+|[零〇一二三四五六七八九十百]+)", text)
        if m:
            value = _cn_number(m.group(1))
            return value if value and value > 0 else None
    return None


def set_character_gender(card: dict, gender: str) -> None:
    """回写性别（保留用户原有键名：有 gender 写 gender，否则写 性别）。"""
    key = "gender" if "gender" in card else "性别"
    card[key] = gender


def set_character_first_chapter(card: dict, chapter: int) -> None:
    """回写首次出场章节（保留用户原有键名：有 first_chapter 写它，否则写 首次出场章节）。"""
    key = "first_chapter" if "first_chapter" in card else "首次出场章节"
    card[key] = int(chapter)


def validate_bible_keys(bible: Any) -> list[dict]:
    """校验 bible 的键是否真的会被使用；返回告警列表（空 = 全部有效）。

    三层：
    - 顶层键：支持集合 / 元数据 / 已知不支持（告警）/ 未登记（告警）；
    - 世界容器（world/世界观/设定/背景…）必须是含基线键的 dict，否则读取者
      拿不到任何东西（静默无效的另一种形态）；
    - characters[] 卡内字段：同上（支持 / 已知不支持 / 未登记）。

    只告警、不改写、不拒绝写入——拒绝会让老项目的历史 bible 无法再次保存；
    用户需要的是「知道这个键没被使用」，而不是保存失败。
    """
    if not isinstance(bible, dict):
        return []
    out: list[dict] = []
    supported_top = ", ".join(sorted(SUPPORTED_BIBLE_KEYS))
    for key, value in bible.items():
        if key in BIBLE_META_KEYS:
            continue
        if key in SUPPORTED_BIBLE_KEYS:
            if key == "characters" and not isinstance(value, list):
                out.append(_warning(
                    key, "shape",
                    "characters 必须是数组；非数组时渲染器读不到任何角色。",
                ))
            if key in _WORLD_BASELINE_KEYS:
                if isinstance(value, (dict, list)) or not str(value or "").strip():
                    out.append(_warning(
                        key, "shape",
                        f"「{key}」需要是一个非空的基线文本（如「王朝」「现代」），"
                        "当前形态不会被时代约束读取。",
                    ))
            elif key in {
                "world", "world_setting", "worldview", "世界观",
                "设定", "setting", "settings", "背景",
            }:
                if not _world_container_ok(value):
                    out.append(_warning(
                        key, "shape",
                        f"「{key}」只有作为包含 baseline/世界基线/时代背景… 的对象时"
                        "才会被时代约束读取；当前形态不会被使用。",
                    ))
            continue
        reason = KNOWN_UNSUPPORTED_BIBLE_KEYS.get(key)
        out.append(_warning(
            key,
            "unsupported" if reason else "unknown",
            reason or f"未登记的 bible 键「{key}」：生成链路不会读取它。"
                       f"已登记的键：{supported_top}",
        ))

    chars = bible.get("characters")
    if isinstance(chars, list):
        for i, card in enumerate(chars):
            if not isinstance(card, dict):
                continue
            for key, value in card.items():
                path = f"characters[{i}].{key}"
                if key in SUPPORTED_CHARACTER_KEYS:
                    if key == "出手规模" and not _card_has_scale(card):
                        out.append(_warning(
                            path, "shape",
                            "出手规模只在同时写了 量级/量级细层/量级序号 时才会"
                            "进入量级块；只有出手规模不会被使用。",
                        ))
                    if key == "修炼" and isinstance(value, dict):
                        for sub in value:
                            if sub not in SUPPORTED_CULTIVATION_KEYS:
                                out.append(_warning(
                                    f"{path}.{sub}", "unsupported",
                                    "修炼 里只有 当前境界 会被量级块读取；"
                                    "其余子字段不会进入生成提示词。",
                                ))
                    continue
                reason = KNOWN_UNSUPPORTED_CHARACTER_KEYS.get(key)
                out.append(_warning(
                    path,
                    "unsupported" if reason else "unknown",
                    reason or f"未登记的角色字段「{key}」：生成链路不会读取它。",
                ))
    return out


def diff_bible(old: Any, new: Any) -> dict:
    """顶层 key 维度的 diff。

    返回：{
      "added":   {key: new_val for key in new - old},
      "removed": {key: old_val for key in old - new},
      "changed": {key: {"old": old_val, "new": new_val} for key 在两侧但值不同},
    }

    不做深度递归——bible 是手编 dict，key 改个字母也算"删除+新增"两层冲突，
    而人物/关系顶层结构稳定。这一层的 diff 对前端"高亮改动"已够用。
    """
    o = _bible_dict(old)
    n = _bible_dict(new)
    keys_o, keys_n = set(o), set(n)
    return {
        "added": {k: n[k] for k in sorted(keys_n - keys_o)},
        "removed": {k: o[k] for k in sorted(keys_o - keys_n)},
        "changed": {
            k: {"old": o[k], "new": n[k]}
            for k in sorted(keys_o & keys_n)
            if o[k] != n[k]
        },
    }


def _book_filter(model, novel_id: int | None) -> list:
    """书维度过滤：指定书 + 项目级 NULL。``None`` = 不限制（老行为）。"""
    return [] if novel_id is None else [model.novel_id.in_((novel_id, None))]


def detect_conflicts(db: Session, project_id: int, new_bible: Any,
                     novel_id: int | None = None) -> list[dict]:
    """扫 KG：哪些 fact 与新 bible 字面矛盾？

    字段映射（保守，只扫显式三块）：
    - bible["characters"][i].name  ↔  kg_entities.canonical_name（kind=character）的
      attrs.role / attrs.current_goal 等
    - bible["relationships"]     ↔  kg_relations.rel_type + 两端名
    - bible["foreshadowing"]      ↔  kg_foreshadows.name（同名 → 状态/描述冲突）

    不扫：bible 里加的新角色/关系（KG 还没建）——那不是冲突，是"未来要建"。

    返回 list[dict] 给前端，每条含：kind / kg_key / kg_value / bible_field / bible_value
    """
    nb = _bible_dict(new_bible)
    if not nb:
        return []

    out: list[dict] = []

    # 1) characters 字段：bible 端 list[{name, role, goal}]；KG 端 entities
    new_chars = nb.get("characters") or []
    if isinstance(new_chars, list):
        for c in new_chars:
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if not name:
                continue
            ent = db.scalar(select(KgEntity).where(
                KgEntity.project_id == project_id,
                KgEntity.kind == "character",
                KgEntity.canonical_name == name,
                *_book_filter(KgEntity, novel_id),
            ))
            if ent is None:
                continue
            ent_attrs = ent.attrs or {}
            # role 字段冲突（bible.role vs ent.attrs.role）
            new_role = (c.get("role") or "").strip()
            old_role = (ent_attrs.get("role") or "").strip()
            if new_role and old_role and new_role != old_role:
                out.append({
                    "kind": "character_role",
                    "kg_key": name,
                    "kg_value": old_role,
                    "bible_field": f"characters.{name}.role",
                    "bible_value": new_role,
                    "severity": "warn",
                })
            # goal 字段冲突
            new_goal = (c.get("goal") or c.get("current_goal") or "").strip()
            old_goal = (ent_attrs.get("current_goal") or ent_attrs.get("goal") or "").strip()
            if new_goal and old_goal and new_goal != old_goal:
                out.append({
                    "kind": "character_goal",
                    "kg_key": name,
                    "kg_value": old_goal,
                    "bible_field": f"characters.{name}.goal",
                    "bible_value": new_goal,
                    "severity": "warn",
                })

    # 2) relationships 字段：bible 端 list[{a, b, relation}]（a/b/from/to、
    #    relation/type/rel_type/关系 都是被接受的写法，统一走键表归一）;
    #    KG 端 kg_relations。规范键 relation 曾经扫不到冲突（B4），现在三条路径
    #    都落到同一个 new_type 上。
    new_rels = nb.get("relationships") or []
    if isinstance(new_rels, list):
        for r in new_rels:
            if not isinstance(r, dict):
                continue
            a = _first_text(r, RELATION_FROM_KEYS)
            b = _first_text(r, RELATION_TO_KEYS)
            new_type = _first_text(r, RELATION_TYPE_KEYS)
            if not (a and b and new_type):
                continue
            # 关系端点**不限 kind**：owner→item（owns）与人物关系都合法。
            # 但同名跨 kind 时不能交给 db.scalar 抛 MultipleResultsFound，
            # 统一走确定性的解析（人物优先，其次物品，再按 KIND_ORDER/id）。
            ea = resolve_entity_by_name(db, project_id, a, novel_id=novel_id)
            eb = resolve_entity_by_name(db, project_id, b, novel_id=novel_id)
            if ea is None or eb is None:
                continue
            from app.models import KgRelation
            # 双向查（KG 不区分 from/to）
            existing = db.scalars(select(KgRelation).where(
                KgRelation.project_id == project_id,
                KgRelation.status == "active",  # 候选边不参与冲突扫描
                KgRelation.is_current.is_(True),  # 已被重写章否定的旧边不参与
                ((KgRelation.from_id == ea.id) & (KgRelation.to_id == eb.id))
                | ((KgRelation.from_id == eb.id) & (KgRelation.to_id == ea.id))
            )).all()
            for rel in existing:
                if rel.rel_type != new_type:
                    out.append({
                        "kind": "relationship_type",
                        "kg_key": f"{a}↔{b}",
                        "kg_value": rel.rel_type,
                        "bible_field": "relationships",
                        "bible_value": new_type,
                        "severity": "warn",
                    })
                    break  # 同对多边只报一次

    # 3) foreshadowing 字段：bible 端 list[str 或 {name, status}]；KG 端 kg_foreshadows
    new_fores = nb.get("foreshadowing") or []
    if isinstance(new_fores, list):
        for f_item in new_fores:
            if isinstance(f_item, str):
                name = f_item.strip()
                new_status = None
            elif isinstance(f_item, dict):
                name = (f_item.get("name") or "").strip()
                new_status = (f_item.get("status") or "").strip() or None
            else:
                continue
            if not name:
                continue
            fs = db.scalar(select(KgForeshadow).where(
                KgForeshadow.project_id == project_id,
                KgForeshadow.name == name,
                *_book_filter(KgForeshadow, novel_id),
            ))
            if fs is None:
                continue
            if new_status and fs.status != new_status:
                out.append({
                    "kind": "foreshadow_status",
                    "kg_key": name,
                    "kg_value": fs.status,
                    "bible_field": "foreshadowing",
                    "bible_value": new_status,
                    "severity": "warn",
                })

    return out


def _next_revision_no(db: Session, project_id: int) -> int:
    """项目内下一个 revision_no（严格 +1，连号不跳号）。"""
    mx = db.scalar(select(func.max(KgBibleRevision.revision_no)).where(
        KgBibleRevision.project_id == project_id
    ))
    return (mx or 0) + 1


def apply_bible_revision(
    db: Session,
    project_id: int,
    new_bible: Any,
    *,
    note: str | None = None,
    source: str = "user",
) -> dict:
    """改 plot_bible：写 revision + 算 diff + 扫冲突 + 覆盖 project.plot_bible。

    返回 `{revision_no, diff, conflicts, conflicts_count, warnings, warnings_count}`。
    `warnings` 来自 `validate_bible_keys`：写入的键里哪些不会被生成链路读取
    （B2）——键仍然原样保存，但用户能在回执里看到「这个键不会被使用」。
    失败（项目不存在）抛 ValueError，由 router 转 404。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise ValueError(f"project {project_id} 不存在")

    old_bible = proj.plot_bible
    diff = diff_bible(old_bible, new_bible)
    conflicts = detect_conflicts(db, project_id, new_bible,
                                 novel_id=book_for_project(proj))
    warnings = validate_bible_keys(new_bible)

    rev_no = _next_revision_no(db, project_id)
    rev = KgBibleRevision(
        project_id=project_id,
        revision_no=rev_no,
        bible_json=dict(new_bible) if isinstance(new_bible, dict) else {},
        diff_json=diff,
        conflicts_json=conflicts,
        source=source,
        note=note,
    )
    db.add(rev)
    proj.plot_bible = dict(new_bible) if isinstance(new_bible, dict) else None
    db.commit()
    db.refresh(rev)
    return {
        "revision_no": rev.revision_no,
        "revision_id": rev.id,
        "diff": diff,
        "conflicts": conflicts,
        "conflicts_count": len(conflicts),
        "warnings": warnings,
        "warnings_count": len(warnings),
    }


def list_bible_revisions(db: Session, project_id: int, limit: int = 20) -> list[dict]:
    """历史版本清单（按 revision_no 倒序，新的在前）。"""
    rows = db.scalars(select(KgBibleRevision).where(
        KgBibleRevision.project_id == project_id,
    ).order_by(KgBibleRevision.revision_no.desc()).limit(limit)).all()
    return [{
        "id": r.id,
        "revision_no": r.revision_no,
        "diff": r.diff_json,
        "conflicts": r.conflicts_json or [],
        "conflicts_count": len(r.conflicts_json or []),
        "source": r.source,
        "note": r.note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


#: 删 facts 时需要排除的 key 集合（与 retrieval_v3 联动用）。
#: 在 bible 改后，下游 prompt 注入（merged_constraint_items）应跳过这些
#: 字段对应的旧 fact。新表是「事实账本层」的；这条是「注入层」的 hint。
#: 暴露出去给 retrieval_v3 用，但不直接做注入剪裁（避免本模块依赖注入逻辑）。
#: ——**设计上留给后端检索路径单独调**。本模块只管 revision 与冲突。
DELETED_BIBLE_FIELDS_HINT = "caller should re-read bible after patch and rebuild hints"
