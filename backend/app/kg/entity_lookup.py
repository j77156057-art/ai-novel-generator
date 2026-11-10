# -*- coding: utf-8 -*-
"""按规范名解析实体：**永不抛 MultipleResultsFound** 的确定性取一条。

``kg_entities`` 的唯一键是 ``(project_id, kind, canonical_name)``，所以同名
跨 kind 是合法的：物品里有「长刀」「黄金」「白棋」，人物里也可能有同名条目。
任何 ``select(KgEntity).where(project_id, canonical_name == name)`` 直接交给
``db.scalar`` 的写法，在两类数据并存后会抛 ``MultipleResultsFound``。

两种语义分开处理：

- **调用方知道要哪一类**（人物状态、人物情报）：传 ``kind="character"``，
  唯一键保证至多一行，语义不会串。
- **关系端点这类真的不限 kind**：不传 kind，按 ``KIND_ORDER`` 取一条。顺序是
  「人物 → 物品 → 势力 → 地点 → 体系 → 规则」，理由是关系（``kg_relations``）
  与 plot_bible 的 relationships 都以人物关系为主体；同 kind 再按 id 升序，
  保证同样的数据永远解析到同一个实体。
"""
from __future__ import annotations

from app.models import KgEntity

#: 不限 kind 时的确定性优先级（越靠前越优先）。
KIND_ORDER = ("character", "item", "faction", "location", "power_system", "worldrule")


def resolve_entity_by_name(db, project_id: int, name: str, *, kind: str | None = None):
    """按规范名取一个实体；没有返回 ``None``，同名多行时行为确定。

    Args:
        db: SQLAlchemy session。
        project_id: 项目隔离，绝不跨项目解析。
        name: 规范名（不是别名——别名归并属于写入口的职责）。
        kind: 指定类别时只在该类里查（唯一键保证至多一行）。

    Returns:
        ``KgEntity`` 或 ``None``。
    """
    name = (name or "").strip()
    if not name:
        return None

    from sqlalchemy import select

    stmt = select(KgEntity).where(
        KgEntity.project_id == project_id,
        KgEntity.canonical_name == name,
    )
    if kind is not None:
        stmt = stmt.where(KgEntity.kind == kind)
        return db.scalar(stmt)

    rows = db.scalars(stmt).all()
    if not rows:
        return None
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    return min(rows, key=lambda e: (rank.get(e.kind, len(rank)), e.id))
