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


def resolve_entity_by_name(db, project_id: int, name: str, *,
                           kind: str | None = None,
                           novel_id: int | None = None,
                           own_only: bool = False):
    """按规范名取一个实体；没有返回 ``None``，同名多行时行为确定。

    Args:
        db: SQLAlchemy session。
        project_id: 项目隔离，绝不跨项目解析。
        name: 规范名（不是别名——别名归并属于写入口的职责）。
        kind: 指定类别时只在该类里查。
        novel_id: 传了才按书收窄（**这本书 + NULL 通配**），且同名时真书行优先；
            不传 = 只按项目隔离，与加书维度之前逐字一致。
        own_only: 只认 ``novel_id IS NULL``（本书行）。原创项目没有独立书 id，
            ``book=None`` 就是本书值而不是「不过滤」——用这个开关把「本书」
            和「不按书过滤」区分开，避免本书抽取解析到参考书的同名实体。

    Returns:
        ``KgEntity`` 或 ``None``。
    """
    name = (name or "").strip()
    if not name:
        return None

    from sqlalchemy import or_, select

    stmt = select(KgEntity).where(
        KgEntity.project_id == project_id,
        KgEntity.canonical_name == name,
    )
    if kind is not None:
        stmt = stmt.where(KgEntity.kind == kind)
    if own_only:
        stmt = stmt.where(KgEntity.novel_id.is_(None))
    elif novel_id is not None:
        stmt = stmt.where(or_(KgEntity.novel_id.is_(None),
                              KgEntity.novel_id == novel_id))

    rows = db.scalars(stmt).all()
    if not rows:
        return None
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    # 真书行优先于 NULL 通配行：同一个名字在书里与项目级都有时，按书取书里的。
    return min(rows, key=lambda e: (
        e.novel_id is None,
        rank.get(e.kind, len(rank)),
        e.id,
    ))
