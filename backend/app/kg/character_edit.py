"""角色编辑三入口：加角色 / 删角色 / 改境界。

与 `character_rename` 的分工：
- 改名要看正文影响面（历史事实不能随便改），归 `character_rename`；
- 这里三个动作都不改已有正文，只改 bible 清册，因此**不需要**正文覆盖判定。

共同约束（B2/B3 的延续）：
- 所有写入都走 `apply_bible_revision`（版本化 + diff + 冲突扫描 + 键告警）；
- 新增角色卡必须过 `validate_bible_keys` 的封闭表：键不在支持 / 已知不支持
  清单里就**拒绝**（这是新写入，不是老数据再保存，没有兼容包袱）；
- 删除到空列表必须保留空数组 `characters=[]` —— B3 把它定义成「本项目现在
  没有任何角色」，提示词会剔除所有旧 KG 角色；不能把键删掉（那是「未宣告」）。
"""
from __future__ import annotations

import copy

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.kg.bible import (
    RELATION_FROM_KEYS,
    RELATION_TO_KEYS,
    apply_bible_revision,
    validate_bible_keys,
)
from app.kg.sentinel import realm_order_from_project
from app.models import Project


def _card_name(card: dict) -> str:
    """角色卡名字（name 为规范键，历史卡可能只有 名字）。"""
    return str(card.get("name") or card.get("名字") or "").strip()


def _load(db: Session, project_id: int) -> tuple[Project, dict, list]:
    proj = db.get(Project, project_id)
    if proj is None:
        raise ValueError(f"project {project_id} 不存在")
    bible = proj.plot_bible if isinstance(proj.plot_bible, dict) else {}
    chars = bible.get("characters")
    if chars is None:
        chars = []
    if not isinstance(chars, list):
        raise ValueError("bible 的 characters 不是数组，先修好结构再编辑")
    return proj, bible, chars


def _locate(chars: list, name: str) -> int:
    idxs = [
        i for i, c in enumerate(chars)
        if isinstance(c, dict) and _card_name(c) == name
    ]
    if not idxs:
        raise ValueError(f"bible 的角色里没有「{name}」（名字需要完全一致）")
    if len(idxs) > 1:
        raise ValueError(f"bible 里有 {len(idxs)} 个「{name}」，先手动去重再编辑")
    return idxs[0]


def _realm_groups(proj: Project) -> list[list[str]]:
    """项目境界阶梯（一维 = 单套；二维 = 多套互不可比）。"""
    return [list(group) for group in realm_order_from_project(proj) if group]


def add_character(
    db: Session,
    project_id: int,
    card: dict,
    *,
    note: str | None = None,
) -> dict:
    """新增一条 `plot_bible.characters[]`。

    新角色没有任何正文出场，后续改名按既有规则 `total=0` 无条件允许。
    """
    if not isinstance(card, dict) or not card:
        raise ValueError("card 必须是一个非空对象")
    name = _card_name(card)
    if not name:
        raise ValueError("角色卡必须有 name（或 名字）")

    _, bible, chars = _load(db, project_id)
    if any(isinstance(c, dict) and _card_name(c) == name for c in chars):
        raise ValueError(f"bible 里已经有叫「{name}」的角色，不能重复添加")

    # 新写入必须过封闭表：未登记 / 已知不支持的键直接拒绝，而不是写进去静默失效。
    warnings = validate_bible_keys({"characters": [card]})
    if warnings:
        detail = "；".join(f"{w['path']}：{w['message']}" for w in warnings)
        raise ValueError(f"角色卡里有不会被使用的字段，已拒绝写入：{detail}")

    new_bible = copy.deepcopy(bible)
    roster = new_bible.get("characters")
    if not isinstance(roster, list):
        roster = []
        new_bible["characters"] = roster
    roster.append(copy.deepcopy(card))

    revision = apply_bible_revision(
        db, project_id, new_bible,
        note=(note or "").strip() or f"新增角色：{name}",
    )
    return {
        "ok": True,
        "action": "add",
        "character": copy.deepcopy(card),
        "name": name,
        "roster_size": len(roster),
        "receipt": f"已新增角色「{name}」。该角色还没有正文出场，后续改名不受影响。",
        "bible": revision,
    }


def remove_character(
    db: Session,
    project_id: int,
    name: str,
    *,
    note: str | None = None,
) -> dict:
    """从 `plot_bible.characters[]` 移除一个角色。

    - 同时删掉以该角色为端点的 relationships（否则关系行会把旧名字重新带进
      user 提示词，等于没删干净）；
    - 删到空列表时保留 `characters=[]`：B3 语义是「本项目现在没有任何角色」，
      提示词会剔除所有旧 KG 角色；删掉整个键会退回「未宣告、不过滤」。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name 不能为空")

    _, bible, chars = _load(db, project_id)
    idx = _locate(chars, name)

    new_bible = copy.deepcopy(bible)
    removed = new_bible["characters"].pop(idx)

    rel_removed = 0
    kept_rels = []
    for rel in new_bible.get("relationships") or []:
        if not isinstance(rel, dict):
            kept_rels.append(rel)
            continue
        endpoints = {
            str(rel.get(k) or "").strip()
            for k in RELATION_FROM_KEYS + RELATION_TO_KEYS
        }
        if name in endpoints:
            rel_removed += 1
            continue
        kept_rels.append(rel)
    if "relationships" in new_bible:
        new_bible["relationships"] = kept_rels

    revision = apply_bible_revision(
        db, project_id, new_bible,
        note=(note or "").strip() or f"删除角色：{name}",
    )
    roster_size = len(new_bible["characters"])
    parts = [f"已删除角色「{name}」。"]
    if rel_removed:
        parts.append(f"同时移除了 {rel_removed} 条以该角色为端点的关系。")
    if roster_size == 0:
        parts.append(
            "角色清册现在是空数组（本项目不再有任何角色）："
            "生成时会剔除所有旧图谱角色，不会再有已删角色回流。"
        )
    return {
        "ok": True,
        "action": "remove",
        "removed": copy.deepcopy(removed),
        "name": name,
        "roster_size": roster_size,
        "roster_empty": roster_size == 0,
        "relationships_removed": rel_removed,
        "receipt": "".join(parts),
        "bible": revision,
    }


def set_character_realm(
    db: Session,
    project_id: int,
    name: str,
    realm: str,
    *,
    note: str | None = None,
) -> dict:
    """改一个角色的境界：写 `characters[].境界`（或已有的 `修炼.当前境界`）。

    只走 power 块（`build_power_constraint` 读这两个字段），不写 bible 顶层键。
    项目配了 `realm_order` 时，境界名必须在阶梯里；没配则无法校验，放行并注明。
    """
    name = (name or "").strip()
    realm = (realm or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    if not realm:
        raise ValueError("境界不能为空")

    proj, bible, chars = _load(db, project_id)
    idx = _locate(chars, name)

    groups = _realm_groups(proj)
    configured = isinstance(bible.get("realm_order"), (list, tuple)) and bool(bible.get("realm_order"))
    if configured:
        known = [r for group in groups for r in group]
        if realm not in known:
            raise ValueError(
                f"「{realm}」不在本项目的 realm_order 里；可选境界："
                + "、".join(known)
            )

    new_bible = copy.deepcopy(bible)
    card = new_bible["characters"][idx]
    cultivation = card.get("修炼")
    if isinstance(cultivation, dict) and "当前境界" in cultivation:
        # 卡里已经在用 修炼.当前境界（渲染优先级更高），改这一处，
        # 避免同时留下两个互相矛盾的境界值。
        cultivation["当前境界"] = realm
        field = "修炼.当前境界"
    else:
        card["境界"] = realm
        field = "境界"

    revision = apply_bible_revision(
        db, project_id, new_bible,
        note=(note or "").strip() or f"改境界：{name} → {realm}",
    )
    receipt = f"已把「{name}」的境界改为「{realm}」（写入 {field}，量级块会立即使用）。"
    if not configured:
        receipt += " 项目没有配置 realm_order，无法校验这个境界名是否在阶梯里。"
    return {
        "ok": True,
        "action": "set_realm",
        "name": name,
        "realm": realm,
        "field": field,
        "validated": configured,
        "receipt": receipt,
        "bible": revision,
    }
