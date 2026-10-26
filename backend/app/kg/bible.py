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

from typing import Any

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import KgBibleRevision, KgEntity, KgForeshadow, Project


def _bible_dict(bible: Any) -> dict:
    """归一化：DB 列可能存 None / str（破损数据）/ 任意 dict——都按 dict 算。"""
    if not isinstance(bible, dict):
        return {}
    return bible


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


def detect_conflicts(db: Session, project_id: int, new_bible: Any) -> list[dict]:
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

    # 2) relationships 字段：bible 端 list[{a, b, type}]; KG 端 kg_relations
    new_rels = nb.get("relationships") or []
    if isinstance(new_rels, list):
        for r in new_rels:
            if not isinstance(r, dict):
                continue
            a = (r.get("a") or r.get("from") or "").strip()
            b = (r.get("b") or r.get("to") or "").strip()
            new_type = (r.get("type") or r.get("rel_type") or "").strip()
            if not (a and b and new_type):
                continue
            ea = db.scalar(select(KgEntity).where(
                KgEntity.project_id == project_id, KgEntity.canonical_name == a))
            eb = db.scalar(select(KgEntity).where(
                KgEntity.project_id == project_id, KgEntity.canonical_name == b))
            if ea is None or eb is None:
                continue
            from app.models import KgRelation
            # 双向查（KG 不区分 from/to）
            existing = db.scalars(select(KgRelation).where(
                KgRelation.project_id == project_id,
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

    返回 {revision_no, diff, conflicts, conflicts_count}。
    失败（项目不存在）抛 ValueError，由 router 转 404。
    """
    proj = db.get(Project, project_id)
    if proj is None:
        raise ValueError(f"project {project_id} 不存在")

    old_bible = proj.plot_bible
    diff = diff_bible(old_bible, new_bible)
    conflicts = detect_conflicts(db, project_id, new_bible)

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
