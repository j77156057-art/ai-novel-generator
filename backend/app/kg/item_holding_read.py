# -*- coding: utf-8 -*-
"""物品持有事实的读通道（生成侧注入用）。

物理布局决定了这里**不能 JOIN**：

- 物品实体索引在 ``novel_dev.db``（SQLAlchemy session，``kg_entities``）；
- 持有段在 ``backend/data/generated.db``（裸 sqlite3，``kg_item_holding``）。

两条连接、两个事务、没有 ORM 映射，也不上 ``ATTACH``。所以固定两次批量查询，
在应用层按 ``item_id`` / ``item_name`` 拼接：

1. ``_resolve_entities``：一条 ``IN`` 查询解析物品 id ↔ 规范名（别名也参与匹配）；
2. ``_fetch_holding_rows``：一条 ``LEFT JOIN`` 查询取全部候选持有段，
   连同旁注顺序键与实例片号一起取回。

20 个物品也只打这两次库，不按物品循环查询（无 N+1）。

「当前」的口径与写入口一致：同一 ``(物品, 实例, hold_type)`` 取
``COALESCE(order_key, valid_from * 100)`` 最大的那一段做 ``status`` 聚合；
**旁注表里 ``holding_id IS NULL`` 的回溯/预叙分支不参与判定**（它们挂在
LEFT JOIN 的 ``holding_id`` 上，天然不会成为候选）。不按 confidence 挑。

``lines`` 返回该层**全部**段（按顺序键升序）：历史段可见，但不覆盖
``status``——否则「镇北王 own 99–106 / 元景帝 own 106–∞」只剩后者。

已登记组的单位做组级/碎片级分层：无 ``instance_label`` 只返回组级段
（``instance_id IS NULL``），有 ``instance_label`` 只返回该碎片的段。

四种状态必须分开渲染，另加一个库里的值不存在的「无记录」：

- ``held``：有主，给出持有者 + ``own``（权属）/ ``custody``（实物在谁手上）；
- ``unowned``：确实无主，不能写成「某人持有」；
- ``unknown``：不知道在谁手上；
- ``sealed``：被封印，不是被某人持有；
- 查不到任何段：``no_record``。这是「无记录」，不是 ``unowned``，
  生成侧**不下发持有约束**，更不能写成「无人持有 / 已确认无主」。

多实例物品按 ``instance_id`` 分别返回，不合成一条。

读作用域（2026-11-10 定：a 的修订版，不加 novel 归属列）
--------------------------------------------------------

- **正主**：谁拥有 ``kg_item_group`` 行，组就是谁的——组的 ``project_id`` 即归属
  项目。不用 ``reference_novel_ids`` 的交集结果直接当归属。
- ``reference_novel_ids`` 只用来**枚举候选项目**（与本项目共用参考语料的其他项目），
  再与「真正拥有组行的项目」取交集，避免读到完全无关项目的组。
- **实体解析锁在组的 ``project_id`` 里**：名字先经写法落到组，再只在组的归属项目
  里找实体。若按 ``project_id IN (family)`` 解析，项目 5 与项目 7 的两个同名实体
  会再次撞成 ``ambiguous``。
- 查不到组的物品仍按**本项目**实体解析，不跨项目合并候选。
- 产线路径禁止 ``project_id=None``：抛 ``ValueError``，不再静默跨项目合并后判歧义。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from collections import Counter
from collections.abc import Iterable

from app.config import REPO_ROOT, settings

#: 库里的四种 owner_state。
HELD = "held"
UNOWNED = "unowned"
UNKNOWN = "unknown"
SEALED = "sealed"
#: 读通道独有的第五种：该物品在持有表里一条段都没有。
NO_RECORD = "no_record"

#: 聚合状态时谁先露出。held 优先（`own` 有主、`custody` 另说），
#: 其余按「更接近无主」排序；明细始终在 ``lines`` 里，不丢信息。
_STATUS_ORDER = (HELD, SEALED, UNOWNED, UNKNOWN)


#: 读通道独有的第六种：名字是类通称（如「玉石小镜」是九块碎片共用的写法）。
GENERIC = "generic"
#: 读通道独有的第七种：同一个名字命中多个组/实体，不猜。
AMBIGUOUS = "ambiguous"


def _default_generated_db() -> pathlib.Path:
    path = pathlib.Path(settings.kg_generated_db)
    return path if path.is_absolute() else REPO_ROOT / path


def _normalize_items(items: Iterable[str | dict]) -> list[dict]:
    """把 ``["神剑", {"name": "地书", "item_id": 883}]`` 统一成 name/item_id。"""
    out: list[dict] = []
    for raw in items:
        if isinstance(raw, str):
            name, item_id = raw.strip(), None
        elif isinstance(raw, dict):
            name = (raw.get("name") or raw.get("item_name") or "").strip() or None
            item_id = raw.get("item_id")
        else:  # noqa: TRY003 - 调用方契约错误，直接暴露
            raise TypeError(f"物品项只接受 str 或 dict，收到 {type(raw)!r}")
        if not name and item_id is None:
            continue
        out.append({"name": name,
                    "item_id": int(item_id) if item_id is not None else None})
    return out


def _with_candidates(item: dict) -> dict:
    """补上候选字段：没有实体索引时也不让上层少键。"""
    out = dict(item)
    out.setdefault("entity_ids", [])
    out.setdefault("entity_names", [])
    out.setdefault("input_name", item.get("name"))
    return out


def _reference_ids(db, project_id: int) -> set[int] | None:
    """项目的参考语料 id 集合；读不到（无 projects 表 / 无此行 / 非列表）返回 None。"""
    if db is None:
        return None
    try:
        from sqlalchemy import select

        from app.models import Project

        row = db.execute(
            select(Project.reference_novel_ids).where(Project.id == project_id)
        ).first()
    except Exception:  # noqa: BLE001 - 测试库可能只有 kg_entities，按「无参考语料」处理
        return None
    raw = row[0] if row is not None else None
    if not isinstance(raw, (list, tuple)):
        return None
    out: set[int] = set()
    for x in raw:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _family_project_ids(db, project_id: int) -> set[int]:
    """与本项目共用参考语料的其他项目（仅用于**枚举候选**，不决定归属）。

    归属由「谁拥有 ``kg_item_group`` 行」决定：加载候选项目的组时，只有真正有
    组行的项目才会出现在结果里，组的 ``project_id`` 就是它的正主。
    """
    if db is None:
        return {project_id}
    mine = _reference_ids(db, project_id)
    if not mine:
        return {project_id}
    out: set[int] = {project_id}
    try:
        from sqlalchemy import select

        from app.models import Project

        for pid, refs in db.execute(
                select(Project.id, Project.reference_novel_ids)
                .where(Project.id != project_id)).all():
            if not isinstance(refs, (list, tuple)):
                continue
            other: set[int] = set()
            for x in refs:
                try:
                    other.add(int(x))
                except (TypeError, ValueError):
                    continue
            if mine & other:
                out.add(int(pid))
    except Exception:  # noqa: BLE001 - 枚举失败时退回本项目作用域
        return {project_id}
    return out


def _resolve_entities(db, primary_project_id: int, scope_projects: set[int],
                      items: list[dict], registry: dict | None = None) -> list[dict]:
    """第一次查询：从 ``kg_entities`` 批量解析规范名（按 id / 名 / 别名）。

    每个物品的实体解析范围由**组的归属项目**决定：名字先经 approved 写法落到组，
    就只在那个组的 ``project_id`` 里找实体；查不到组才退回本项目。不跨项目合并
    候选——那是 ``ambiguous`` 的来源（项目 5 的实体 26 与项目 7 的实体 68 同名）。

    解析不到就保留调用方给的原名/原 id——读通道不替调用方猜实体。
    """
    if db is None or not items:
        return [_with_candidates(it) for it in items]
    names = {it["name"] for it in items if it.get("name")}
    ids = {it["item_id"] for it in items if it.get("item_id") is not None}
    if not names and not ids:
        return [_with_candidates(it) for it in items]

    try:
        from sqlalchemy import or_, select

        from app.models import KgEntity

        conds = []
        if names:
            conds.append(KgEntity.canonical_name.in_(sorted(names)))
            # 别名不在 SQL 条件里时，「玉石小镜 → 地书」这种只给别名的调用会漏。
            # aliases 是 JSON 列，先用带引号的子串把候选行捞进来，再交给下面的
            # Python 索引做精确匹配（预筛不当判据）。仍然只打一次库。
            for name in sorted(names):
                esc = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                conds.append(KgEntity.aliases.like(f'%"{esc}"%', escape="\\"))
        if ids:
            conds.append(KgEntity.id.in_(sorted(ids)))

        stmt = select(KgEntity.id, KgEntity.project_id, KgEntity.canonical_name,
                      KgEntity.aliases).where(KgEntity.kind == "item")
        # 作用域 = 组的正主 + 本项目；不再允许「不加项目过滤」的全局读。
        stmt = stmt.where(
            KgEntity.project_id.in_(sorted(scope_projects | {primary_project_id})))
        rows = db.execute(stmt.where(or_(*conds))).all()
    except Exception:  # noqa: BLE001 - 实体索引读不到时按原名继续，不阻断生成
        return [_with_candidates(it) for it in items]

    by_id: dict[int, tuple[int, str]] = {}
    by_project_name: dict[tuple[int, str], set[int]] = {}
    id_to_name: dict[int, str] = {}
    id_to_project: dict[int, int] = {}
    for row in rows:
        entity_id, entity_project, canonical = row[0], row[1], row[2]
        if not canonical:
            continue
        by_id[entity_id] = (entity_id, canonical)
        id_to_name[entity_id] = canonical
        id_to_project[entity_id] = entity_project
        by_project_name.setdefault((entity_project, canonical), set()).add(entity_id)
        for alias in (row[3] or []):
            if isinstance(alias, str) and alias.strip():
                by_project_name.setdefault(
                    (entity_project, alias.strip()), set()).add(entity_id)

    def _target_projects(name: str | None) -> set[int]:
        """这个名字该在哪个项目里找实体：写法落组 → 组的 project_id；否则本项目。"""
        if registry and name:
            surf = registry["by_surface"].get(name)
            if surf and surf.get("status") == "approved":
                group = registry["groups"].get(surf["group_id"])
                if group is not None:
                    return {group["project_id"]}
        return {primary_project_id}

    resolved: list[dict] = []
    seen: set[tuple[int | None, str | None]] = set()
    for it in items:
        name = it.get("name")
        targets = _target_projects(name)
        candidates: set[int] = set()
        if it["item_id"] is not None:
            # 调用方给了 id 就按 id 为准，不再按名扩大候选。
            if it["item_id"] in by_id and id_to_project.get(it["item_id"]) in targets:
                candidates = {it["item_id"]}
        elif name:
            for pid in targets:
                candidates |= by_project_name.get((pid, name), set())
        if len(candidates) == 1:
            entity_id = next(iter(candidates))
            resolved_name = id_to_name.get(entity_id) or name
            item_id = entity_id
        else:
            resolved_name, item_id = name, it["item_id"]
        key = (item_id, resolved_name, name)
        if key in seen:
            continue
        seen.add(key)
        resolved.append({
            "name": resolved_name,
            "item_id": item_id,
            # 调用方原话要留着：别名被解析成规范名之后，写法→组的查找仍按原话走
            # （如「三号碎片」要按片号收窄，「黑金刀」要落到同一组）。
            "input_name": name,
            # 名字同时命中多个实体（规范名 + 别人的别名）时原样带出，交给上层报歧义。
            "entity_ids": sorted(candidates),
            "entity_names": [id_to_name[i] for i in sorted(candidates) if i in id_to_name],
        })
    return resolved


def _connect_generated(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load_registry(path: pathlib.Path, project_ids: int | Iterable[int] | None,
                   novel_id: int | None = None) -> dict | None:
    """读「同物组 + 写法」登记表（一次 JOIN 批量取回，不按名字循环）。

    ``project_ids``：只加载这些项目的组；``None`` 表示不过滤（仅诊断用，产线不走）。
    组字典带上 ``project_id``——它既是正主，也是实体解析的作用域。

    ``novel_id``（2026-11-11 切读侧时加）：**显式传才按书收窄**，
    ``None`` = 不按书过滤（与加参数前逐字一致）。

    - 传了书：加载「这本书的组」+「``novel_id IS NULL`` 的项目级通配组」；
    - 名字解析：同名时**真书组优先于通配组**（通配组不参与跨书冲突）。
      没有真书组时通配组单独命中；候选横跨两本以上的书则**不挑**，报歧义，
      绝不 `ORDER BY ... LIMIT 1` 抽到另一本。

    表不存在（旧库）返回 ``None``，调用方走原来的按名解析，行为不变。
    """
    if project_ids is None:
        ids: list[int] | None = None
    elif isinstance(project_ids, int):
        ids = [project_ids]
    else:
        ids = sorted({int(x) for x in project_ids})
        if not ids:
            ids = None
    con = _connect_generated(path)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"kg_item_group", "kg_item_surface"} <= tables:
            return None
        has_novel = any(r[1] == "novel_id" for r in con.execute(
            "PRAGMA table_info(kg_item_group)"))
        if novel_id is not None and not has_novel:
            # 旧库没有书维度：宁可不给组（退回按名解析），也不做假过滤。
            return None
        novel_col = "g.novel_id" if has_novel else "NULL"
        sql = (
            "SELECT g.id AS gid, g.project_id, g.canonical_name, g.status AS g_status,"
            "       g.generic_members, g.note AS g_note,"
            f"      {novel_col} AS g_novel_id,"
            "       s.surface, s.status AS s_status, s.instance_label"
            "  FROM kg_item_group g"
            "  LEFT JOIN kg_item_surface s ON s.group_id = g.id"
        )
        conds: list[str] = []
        params: list = []
        if ids is not None:
            conds.append(f"g.project_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)
        if novel_id is not None:
            conds.append("(g.novel_id IS NULL OR g.novel_id = ?)")
            params.append(int(novel_id))
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = con.execute(sql, tuple(params)).fetchall()
    finally:
        con.close()

    groups: dict[int, dict] = {}
    surfaces_by_group: dict[int, list[dict]] = {}
    raw_members: dict[int, str] = {}
    surface_candidates: dict[str, list[dict]] = {}
    #: 名字 → 候选组 id（approved 写法 + 规范名），按书解析用。
    name_groups: dict[str, list[int]] = {}
    for row in rows:
        gid = row["gid"]
        if gid not in groups:
            groups[gid] = {"id": gid, "project_id": row["project_id"],
                           "canonical_name": row["canonical_name"],
                           "status": row["g_status"], "members": [],
                           "note": row["g_note"] or "",
                           "novel_id": row["g_novel_id"]}
            raw_members[gid] = row["generic_members"] or "[]"
        if row["surface"]:
            rec = {"surface": row["surface"], "status": row["s_status"],
                   "group_id": gid, "instance_label": row["instance_label"]}
            surfaces_by_group.setdefault(gid, []).append(rec)
            surface_candidates.setdefault(row["surface"], []).append(rec)
            if rec["status"] == "approved":
                name_groups.setdefault(row["surface"], []).append(gid)

    for gid, raw in raw_members.items():
        try:
            groups[gid]["members"] = [int(x) for x in json.loads(raw)]
        except (TypeError, ValueError):
            groups[gid]["members"] = []
        # 规范名本身也是这个组的写法（老库里不一定有对应 surface 行）
        name_groups.setdefault(groups[gid]["canonical_name"], []).append(gid)

    def _cand_novel(cand) -> int | None:
        """候选（组 id 或写法 rec）所属的书；``None`` = 项目级通配。"""
        gid = cand["group_id"] if isinstance(cand, dict) else cand
        group = groups.get(gid)
        return None if group is None else group.get("novel_id")

    def _pick(cands: list) -> tuple[object, bool]:
        """同名的多个候选按「真书组 > 通配组」收敛；横跨多本书就报歧义。

        通配组（``novel_id IS NULL``）不参与跨书冲突：同名时它只是真书组的兜底，
        真书组存在就让位；只有它自己命中时才算数。
        先按组去重：同一个组的「写法 + 规范名」会在候选里出现两次，不是歧义。
        """
        uniq: list = []
        for c in cands:
            gid = c["group_id"] if isinstance(c, dict) else c
            if all(gid != (x["group_id"] if isinstance(x, dict) else x) for x in uniq):
                uniq.append(c)
        books = [c for c in uniq if _cand_novel(c) is not None]
        wild = [c for c in uniq if _cand_novel(c) is None]
        if len(books) == 1:
            return books[0], True
        if not books and len(wild) == 1:
            return wild[0], True
        return (books[0] if books else wild[0] if wild else None), False

    by_surface: dict[str, dict] = {}
    name_to_group: dict[str, int] = {}
    conflicts: dict[str, list[str]] = {}
    for name, cands in surface_candidates.items():
        picked, ok = _pick(cands)
        if ok:
            by_surface[name] = picked  # type: ignore[assignment]
    for name, gids in name_groups.items():
        picked, ok = _pick(gids)
        if ok:
            name_to_group[name] = int(picked)  # type: ignore[arg-type]
        elif len({g for g in gids}) > 1:
            conflicts[name] = [groups[g]["canonical_name"]
                               for g in sorted(set(gids)) if g in groups]

    canon_groups: dict[str, list[int]] = {}
    for gid, group in groups.items():
        canon_groups.setdefault(group["canonical_name"], []).append(gid)
    group_by_canonical: dict[str, int] = {}
    for name, gids in canon_groups.items():
        # 规范名同名跨书时与写法走同一套裁决，不做 `first wins`。
        picked, ok = _pick(gids)
        if ok:
            group_by_canonical[name] = int(picked)  # type: ignore[arg-type]

    return {"groups": groups, "by_surface": by_surface,
            "surfaces_by_group": surfaces_by_group,
            "name_to_group": name_to_group,
            "conflicts": conflicts,
            "group_by_canonical": group_by_canonical}


def _group_name(registry: dict | None, gid: int | None) -> str | None:
    if not registry or gid is None:
        return None
    group = registry["groups"].get(gid)
    return group["canonical_name"] if group else None


def _build_units(resolved: list[dict], registry: dict | None) -> list[dict]:
    """把实体解析结果落成「组单位」：哪一组、怎么解析到的、要不要报歧义。"""
    units: list[dict] = []
    for it in resolved:
        unit = {
            "uid": len(units),
            "name": it["name"], "item_id": it["item_id"], "group_id": None,
            "group_name": None, "via": "name", "instance_label": None,
            "generic": False, "ambiguous": [], "members": [], "note": "",
        }
        ent_ids = it.get("entity_ids") or []
        input_name = it.get("input_name") or it["name"]
        surf = registry["by_surface"].get(input_name) if registry else None
        surf_group = (registry["groups"].get(surf["group_id"])
                      if surf and surf["status"] == "approved" else None)

        conflict = registry["conflicts"].get(input_name) if registry else None
        if conflict:
            # 同一个名字在多个组里登记（跨项目也会）：不挑、不猜，如实报歧义。
            unit["ambiguous"] = list(conflict)
            unit["item_id"] = None
            unit["note"] = "名字在多个组里都有登记，未做任何猜测"
        elif surf_group and surf_group["status"] == "generic":
            # 明确登记成通称的写法：通称优先，不进任何单件。
            unit["name"] = input_name
            unit["item_id"] = None
            unit["group_id"] = surf_group["id"]
            unit["group_name"] = surf_group["canonical_name"]
            unit["generic"] = True
            unit["via"] = "surface"
            unit["members"] = list(surf_group["members"])
            unit["note"] = surf_group.get("note") or "类是通称，不对应单件"
        elif len(ent_ids) > 1:
            # 同一个名字命中多个实体（规范名 + 别人的别名）：如实报歧义，不猜。
            unit["ambiguous"] = list(it.get("entity_names") or [])
            unit["item_id"] = None
            unit["note"] = "名字同时命中多个实体，未做任何猜测"
        else:
            if surf_group:
                unit["group_id"] = surf_group["id"]
                unit["group_name"] = surf_group["canonical_name"]
                unit["via"] = "surface"
                unit["instance_label"] = surf["instance_label"]
            elif ent_ids and registry:
                gid = registry["group_by_canonical"].get(it["name"])
                if gid is not None:
                    unit["group_id"] = gid
                    unit["group_name"] = _group_name(registry, gid)
                    unit["via"] = "entity"
            if surf and surf["status"] == "pending" and unit["group_id"] is None:
                unit["note"] = (f"「{it['name']}」→{_group_name(registry, surf['group_id'])}"
                                " 是待审映射，本轮不作数")
            # 写法指向的组和实体指向的组不是同一个：也是歧义。
            if surf_group and ent_ids and registry:
                egid = registry["group_by_canonical"].get(it["name"])
                if egid is not None and egid != surf_group["id"]:
                    unit["ambiguous"] = [surf_group["canonical_name"],
                                         _group_name(registry, egid) or it["name"]]
                    unit["group_id"] = None
                    unit["note"] = "写法与实体索引指向不同的组，未做任何猜测"
        units.append(unit)
    return units


def _facts_from_rows(unit: dict, rows: list[sqlite3.Row]) -> list[dict]:
    """一个单位的事实：按实例拆；组级与碎片级分层，明细给完整时间线。

    分层只对「已登记组」的单位生效（``group_id`` 非空；旧库没有登记表时行为不变）：

    - 有 ``instance_label``：只返回该碎片自己的段，不再夹带组级 ``own``；
    - 无 ``instance_label``：只返回组级段（``instance_id IS NULL``），
      查「地书」不会再吐出「三号在许七安手上」。

    ``lines`` 是该层全部段（按顺序键升序），历史段可见；``status`` 仍按每个
    ``hold_type`` 的最新一段聚合——历史不覆盖当前。
    """
    layered = unit.get("group_id") is not None
    if unit["instance_label"]:
        rows = [r for r in rows if r["instance_label"] == unit["instance_label"]]
    elif layered:
        rows = [r for r in rows if r["instance_id"] is None]

    base = {
        "item_name": unit["group_name"] or unit["name"],
        "item_id": unit["item_id"],
        "group_id": unit["group_id"],
        "group_name": unit["group_name"],
        "via": unit["via"],
        "note": unit["note"],
    }
    if not rows:
        return [{**base, "instance_id": None, "instance_label": None,
                 "status": NO_RECORD, "lines": []}]

    by_instance: dict[int | None, list[dict]] = {}
    for row in rows:
        by_instance.setdefault(row["instance_id"], []).append(_row_to_line(row))

    facts: list[dict] = []
    for instance_id, lines in sorted(
            by_instance.items(), key=lambda x: (x[0] is not None, x[0] or 0)):
        lines.sort(key=lambda x: (x["order_key"] or 0, x["chapter"] or 0))
        latest: dict[str, dict] = {}
        for line in lines:
            old = latest.get(line["hold_type"])
            if old is None or (line["order_key"], line["chapter"], line["confidence"]) > (
                    old["order_key"], old["chapter"], old["confidence"]):
                latest[line["hold_type"]] = line
        picked = sorted(latest.values(),
                        key=lambda x: 0 if x["hold_type"] == "own" else 1)
        label = next((r["instance_label"] for r in rows
                      if r["instance_id"] == instance_id and r["instance_label"]), None) \
            if instance_id is not None else None
        facts.append({**base, "instance_id": instance_id, "instance_label": label,
                      "status": _status_of(picked), "lines": lines})
    return facts


def _fetch_holding_rows(path: pathlib.Path, group_ids: list[int],
                        items: list[dict], novel_id: int | None = None) -> list[sqlite3.Row]:
    """第二次查询：一条 IN 语句取回所有候选段 + 旁注顺序键 + 实例片号。

    主路径按 ``group_id`` 取段；``group_id IS NULL`` 的旧行/未登记物品才按
    名字或 ``item_id`` 兜底，所以新物品不会因为没建组而查不到。

    传了 ``novel_id`` 时**不做名字兜底**：持有行没有书维度，未挂组的老行无法
    证明属于哪本书，按书查询时宁可少给，也不跨书返回。
    """
    names = sorted({it["name"] for it in items if it.get("name")})
    ids = sorted({it["item_id"] for it in items if it.get("item_id") is not None})
    groups = sorted({int(g) for g in group_ids})
    if not names and not ids and not groups:
        return []

    con = _connect_generated(path)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "kg_item_holding" not in tables:
            return []
        has_story = "kg_holding_story_segment" in tables
        has_instance = "kg_item_instance" in tables

        # 旁注 only-forward 行才有 holding_id；holding_id IS NULL 的回溯分支
        # 因此不会出现在候选里，天然不参与「当前」判定。
        story_join = ("LEFT JOIN kg_holding_story_segment s "
                      "ON s.holding_id = h.id") if has_story else ""
        order_expr = ("COALESCE(s.order_key, h.valid_from * 100)"
                      if has_story else "h.valid_from * 100")
        instance_join = ("LEFT JOIN kg_item_instance i "
                         "ON i.id = h.instance_id") if has_instance else ""
        label_expr = "i.instance_label" if has_instance else "NULL"

        conds, params = [], []
        has_group = "group_id" in {r[1] for r in con.execute(
            "PRAGMA table_info(kg_item_holding)")}
        if has_group and groups:
            conds.append(f"h.group_id IN ({','.join('?' * len(groups))})")
            params.extend(groups)
        fallback = novel_id is None
        if names and fallback:
            name_cond = f"h.item_name IN ({','.join('?' * len(names))})"
            conds.append(f"(h.group_id IS NULL AND {name_cond})" if has_group else name_cond)
            params.extend(names)
        if ids and fallback:
            id_cond = f"h.item_id IN ({','.join('?' * len(ids))})"
            conds.append(f"(h.group_id IS NULL AND {id_cond})" if has_group else id_cond)
            params.extend(ids)
        if not conds:
            return []
        group_expr = "h.group_id" if has_group else "NULL AS group_id"
        sql = f"""SELECT h.id, h.item_id, h.instance_id, h.item_name,
                         {group_expr},
                         h.hold_type, h.owner_id, h.owner_name, h.owner_kind,
                         h.owner_state, h.valid_from, h.valid_to,
                         h.reason_code, h.evidence, h.evidence_chapter,
                         h.confidence,
                         {order_expr} AS order_key,
                         {label_expr} AS instance_label
                    FROM kg_item_holding h
                    {story_join}
                    {instance_join}
                   WHERE {' OR '.join(conds)}"""
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _row_to_line(row: sqlite3.Row) -> dict:
    return {
        "hold_type": row["hold_type"],
        "owner_state": row["owner_state"],
        "owner_name": row["owner_name"],
        "owner_id": row["owner_id"],
        "owner_kind": row["owner_kind"],
        "chapter": row["valid_from"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "order_key": row["order_key"],
        "reason_code": row["reason_code"],
        "evidence": row["evidence"],
        "confidence": row["confidence"],
    }


def _status_of(lines: list[dict]) -> str:
    present = {line["owner_state"] for line in lines}
    for state in _STATUS_ORDER:
        if state in present:
            return state
    return NO_RECORD


def current_item_holdings(
    items: Iterable[str | dict],
    *,
    db=None,
    project_id: int | None = None,
    generated_db: str | pathlib.Path | None = None,
    novel_id: int | None = None,
) -> list[dict]:
    """批量取「这些物品现在在谁手上」。

    Args:
        items: 物品名（str）或 ``{"name": ..., "item_id": ...}``；name 可空，
            只要有 item_id 也能解析出规范名。
        db: ``novel_dev.db`` 的 SQLAlchemy session。为 None 时跳过实体解析
            （只按调用方给的名字/ id 查），不推荐在生产路径省掉。
        project_id: **必填**。读作用域的正主是「拥有 ``kg_item_group`` 行的项目」；
            本参数同时是查不到组时的实体回退范围。传 ``None`` 会抛 ``ValueError``
            ——不再静默跨项目合并同名候选后判 ``ambiguous``。
        generated_db: 持有库路径，缺省读 ``settings.kg_generated_db``。
        novel_id: 读哪本书的组。``None``（默认）= 不按书过滤，与加参数前逐字一致；
            传了就只加载「这本书的组 + ``novel_id IS NULL`` 的项目级通配组」。
            **注意**：目前没有调用方传它（见报告 §E.7），缺口是「当前书」还没接上。

    Returns:
        每个物品（多实例按实例拆分）一条事实，顺序与输入一致：

        ``{"item_name", "item_id", "instance_id", "instance_label",
        "status", "lines": [...], "group_id", "group_name", "via"}``

        ``status`` 是聚合值（held > sealed > unowned > unknown > no_record），
        另有两种不猜的状态：``generic``（名字是类通称，见 ``candidates``）与
        ``ambiguous``（名字命中多个组/实体，见 ``candidates``）。
        明细以 ``lines`` 为准。库文件不存在 / 没有持有表时返回空列表
        （= 本层不提供任何约束，而不是逐条报无记录）。
    """
    normalized = _normalize_items(items)
    if not normalized:
        return []
    if project_id is None:
        raise ValueError(
            "current_item_holdings 必须传 project_id：读通道不允许跨项目合并同名候选"
            "（项目 5 与项目 7 都有「镇国剑」时会从 no_record 变成 ambiguous）。"
            "全库诊断请直接查库，不要走生成侧读通道。"
        )

    path = pathlib.Path(generated_db) if generated_db else _default_generated_db()
    if not path.exists():
        return []

    scope: set[int] = {project_id}
    try:
        registry = _load_registry(path, project_id, novel_id)
    except sqlite3.Error:
        registry = None
    if registry is not None and not registry["groups"]:
        # 本项目没有组行：按参考语料枚举候选项目，再加载候选的组。
        # 只有真正拥有组行的项目才会被加载进来（正主 = 组的 project_id）。
        family = _family_project_ids(db, project_id)
        if len(family) > 1:
            try:
                candidate = _load_registry(path, family, novel_id)
            except sqlite3.Error:
                candidate = None
            if candidate and candidate["groups"]:
                registry, scope = candidate, family
    resolved = _resolve_entities(db, project_id, scope, normalized, registry)
    units = _build_units(resolved, registry)

    # 通称的候选组也一起取（一次批量查询，不按名字循环）。
    # 已经作为独立物品被查的组不再重复建「候选单位」，否则事实会被覆盖。
    extras: list[dict] = []
    seen_gids: set[int] = {u["group_id"] for u in units if u["group_id"] is not None}
    for unit in units:
        if not unit["generic"] or not registry:
            continue
        for gid in unit["members"]:
            if gid in seen_gids or gid not in registry["groups"]:
                continue
            seen_gids.add(gid)
            extras.append({
                "uid": len(units) + len(extras),
                "name": registry["groups"][gid]["canonical_name"], "item_id": None,
                "group_id": gid, "group_name": registry["groups"][gid]["canonical_name"],
                "via": "group_member", "instance_label": None, "generic": False,
                "ambiguous": [], "members": [], "note": "",
            })

    fetch_gids: list[int] = []
    fetch_items: list[dict] = []
    for unit in units + extras:
        if unit["ambiguous"]:
            continue
        if unit["group_id"] is not None:
            # 主路径：组已登记，直接按组取段，不再经「组名 → 写法」桥。
            fetch_gids.append(unit["group_id"])
            # 兜底名单：group_id 仍为 NULL 的旧行只能按写法匹配，所以把组内
            # 已审核写法一并带上；它只作用于 NULL 行，不参与主路径。
            if registry:
                for rec in registry["surfaces_by_group"].get(unit["group_id"], []):
                    if rec["status"] == "approved":
                        fetch_items.append({"name": rec["surface"], "item_id": None})
        elif unit["name"]:
            fetch_items.append({"name": unit["name"], "item_id": unit["item_id"]})

    try:
        rows = _fetch_holding_rows(path, fetch_gids, fetch_items, novel_id) \
            if (fetch_gids or fetch_items) else []
    except sqlite3.Error:
        return []

    # 行 → 单位：优先按行上的 group_id；旧行（NULL）再退回写法/ item_id / 原名。
    # 同一个组可能被查多次（如「地书」和「三号碎片」），所以一个组要挂多个单位。
    uids_by_gid: dict[int, list[int]] = {}
    uids_by_name: dict[str, list[int]] = {}
    uid_by_id: dict[int, int] = {}
    for u in units + extras:
        if u["group_id"] is not None:
            uids_by_gid.setdefault(u["group_id"], []).append(u["uid"])
        if u["name"]:
            uids_by_name.setdefault(u["name"], []).append(u["uid"])
        if u.get("item_id") is not None:
            uid_by_id[u["item_id"]] = u["uid"]

    rows_by_uid: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        uids: list[int] = []
        row_gid = row["group_id"] if "group_id" in row.keys() else None
        if row_gid is not None:
            uids = uids_by_gid.get(row_gid, [])
        elif row["item_id"] is not None and row["item_id"] in uid_by_id:
            uids = [uid_by_id[row["item_id"]]]
        elif registry:
            gid = registry["name_to_group"].get(row["item_name"])
            if gid is not None:
                # 候选组也参与匹配：通称的 candidates 要能拿到成员组的段。
                uids = uids_by_gid.get(gid, [])
        if not uids:
            uids = uids_by_name.get(row["item_name"], [])
        for uid in uids:
            rows_by_uid.setdefault(uid, []).append(row)

    facts_by_uid: dict[int, list[dict]] = {}
    for unit in units + extras:
        facts_by_uid[unit["uid"]] = _facts_from_rows(unit, rows_by_uid.get(unit["uid"], []))

    facts: list[dict] = []
    for unit in units:
        base = {"item_name": unit["group_name"] or unit["name"],
                "item_id": unit["item_id"], "group_id": unit["group_id"],
                "group_name": unit["group_name"], "via": unit["via"],
                "note": unit["note"]}
        if unit["ambiguous"]:
            facts.append({**base, "status": AMBIGUOUS, "lines": [],
                          "instance_id": None, "instance_label": None,
                          "candidates": [{"item_name": n} for n in unit["ambiguous"]]})
            continue
        if unit["generic"]:
            candidates = []
            for gid in unit["members"]:
                lines: list[dict] = []
                seen_lines: set[tuple] = set()
                name = _group_name(registry, gid) or f"组#{gid}"
                for uid in uids_by_gid.get(gid, []):
                    for cand in facts_by_uid.get(uid) or []:
                        if not cand.get("lines"):
                            continue
                        name = cand["item_name"] or name
                        for line in cand["lines"]:
                            key = (cand["instance_id"], line["hold_type"],
                                   line["owner_name"], line["valid_from"])
                            if key in seen_lines:
                                continue
                            seen_lines.add(key)
                            lines.append(line)
                candidates.append({"group_id": gid, "item_name": name,
                                   "status": _status_of(lines) if lines else NO_RECORD,
                                   "lines": lines})
            facts.append({**base, "status": GENERIC, "lines": [],
                          "instance_id": None, "instance_label": None,
                          "candidates": candidates})
            continue
        facts.extend(facts_by_uid.get(unit["uid"], []))
    return facts


def _owner_text(line: dict) -> str:
    if line["owner_name"]:
        return line["owner_name"]
    if line["owner_id"] is not None:
        return f"实体#{line['owner_id']}"
    return "（未具名）"


def _render_line(line: dict) -> str:
    state = line["owner_state"]
    hold_type = line["hold_type"]
    if state == HELD:
        if hold_type == "own":
            return f"权属（own）归{_owner_text(line)}"
        return f"实物（custody）在{_owner_text(line)}手上"
    if state == UNOWNED:
        return "确认为无主（unowned）"
    if state == UNKNOWN:
        return "不知在谁手上（unknown）"
    if state == SEALED:
        return "已被封印（sealed），不属于任何人持有"
    return f"状态未知（{state}）"


def _range_text(line: dict) -> str:
    """章号区间后缀；开区间写「起」。"""
    start, end = line.get("valid_from"), line.get("valid_to")
    if start is None and end is None:
        return ""
    if end is None:
        return f"（{start}–∞）"
    return f"（{start}–{end}）"


def _render_lines(lines: list[dict]) -> str:
    """按时间线渲染；同一 ``hold_type`` 有多段时才补章号区间，单行输出保持原样。"""
    counts = Counter(line["hold_type"] for line in lines)
    parts = []
    for line in lines:
        text = _render_line(line)
        if counts[line["hold_type"]] > 1:
            text += _range_text(line)
        parts.append(text)
    return "；".join(parts)


#: 无记录行的固定措辞。单条与压缩行共用，保证语义一字不差。
_NO_RECORD_BODY = "无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）"


def _fact_name(fact: dict) -> str:
    name = fact["item_name"] or "（未命名物品）"
    label = fact.get("instance_label")
    if label and label not in name:
        name = f"{name}·{label}"
    return name


def _is_blank_record(fact: dict) -> bool:
    """这一条是否只会渲染成 no_record（通称/歧义另有分支，不参与压缩）。"""
    return (fact["status"] not in (GENERIC, AMBIGUOUS)
            and (fact["status"] == NO_RECORD or not fact["lines"]))


def format_holding_facts(facts: Iterable[dict]) -> str:
    """把事实渲染成 prompt 用的自然语言行（每条物品一行）。

    全部都是 ``no_record`` 时压成**一行**：逐条列 7 遍同样的「无持有记录」只是
    白占上下文（2026-11-10 实测 p1：427 字符的块占 user prompt 22.86%），
    而这一层本来就不提供任何持有约束。只要有任何一条是
    held / sealed / unowned / unknown（或带 note），就退回逐条明细，一条不省。
    """
    facts = list(facts)
    if facts and all(_is_blank_record(f) and not f.get("note") for f in facts):
        names = "、".join(_fact_name(f) for f in facts)
        all_word = "均" if len(facts) > 1 else ""
        return f"- {names}：{all_word}{_NO_RECORD_BODY}"

    lines: list[str] = []
    for fact in facts:
        name = _fact_name(fact)
        if fact["status"] == GENERIC:
            lines.append(f"- {name}：{_render_generic(fact)}")
            continue
        if fact["status"] == AMBIGUOUS:
            lines.append(f"- {name}：{_render_ambiguous(fact)}")
            continue
        if fact["status"] == NO_RECORD or not fact["lines"]:
            body = _NO_RECORD_BODY
            if fact.get("note"):
                body = f"{body}；{fact['note']}"
        else:
            body = _render_lines(fact["lines"])
        lines.append(f"- {name}：{body}")
    return "\n".join(lines)


def _render_generic(fact: dict) -> str:
    """通称：不谎称是某一件的别名，只给候选与候选的当前状态。"""
    note = fact.get("note") or "类的通称，不对应单件"
    candidates = fact.get("candidates") or []
    if not candidates:
        return f"{note}；当前没有登记候选"
    parts = []
    for cand in candidates:
        if cand.get("lines"):
            body = _render_lines(cand["lines"])
        else:
            body = "无持有记录"
        parts.append(f"{cand['item_name']}（{body}）")
    return f"{note}；候选：" + " / ".join(parts)


def _render_ambiguous(fact: dict) -> str:
    names = [c.get("item_name") or f"实体#{c.get('entity_id')}"
             for c in (fact.get("candidates") or [])]
    detail = " / ".join(names) if names else "多个候选"
    return f"名字有歧义，可能在指：{detail}；不猜，需指定具体是哪一件"
