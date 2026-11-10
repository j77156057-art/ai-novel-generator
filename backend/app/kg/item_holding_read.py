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
``COALESCE(order_key, valid_from * 100)`` 最大的那一段；**旁注表里
``holding_id IS NULL`` 的回溯/预叙分支不参与判定**（它们挂在 LEFT JOIN 的
``holding_id`` 上，天然不会成为候选）。不按 confidence 挑。

四种状态必须分开渲染，另加一个库里的值不存在的「无记录」：

- ``held``：有主，给出持有者 + ``own``（权属）/ ``custody``（实物在谁手上）；
- ``unowned``：确实无主，不能写成「某人持有」；
- ``unknown``：不知道在谁手上；
- ``sealed``：被封印，不是被某人持有；
- 查不到任何段：``no_record``。这是「无记录」，不是 ``unowned``，
  生成侧**不下发持有约束**，更不能写成「无人持有 / 已确认无主」。

多实例物品按 ``instance_id`` 分别返回，不合成一条。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
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


def _resolve_entities(db, project_id: int | None, items: list[dict]) -> list[dict]:
    """第一次查询：从 ``kg_entities`` 批量解析规范名（按 id / 名 / 别名）。

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

        stmt = select(KgEntity.id, KgEntity.canonical_name, KgEntity.aliases).where(
            KgEntity.kind == "item"
        )
        if project_id is not None:
            stmt = stmt.where(KgEntity.project_id == project_id)
        rows = db.execute(stmt.where(or_(*conds))).all()
    except Exception:  # noqa: BLE001 - 实体索引读不到时按原名继续，不阻断生成
        return [_with_candidates(it) for it in items]

    by_id: dict[int, tuple[int, str]] = {}
    by_name: dict[str, set[int]] = {}
    id_to_name: dict[int, str] = {}
    for row in rows:
        entity_id, canonical = row[0], row[1]
        if not canonical:
            continue
        by_id[entity_id] = (entity_id, canonical)
        id_to_name[entity_id] = canonical
        by_name.setdefault(canonical, set()).add(entity_id)
        for alias in (row[2] or []):
            if isinstance(alias, str) and alias.strip():
                by_name.setdefault(alias.strip(), set()).add(entity_id)

    resolved: list[dict] = []
    seen: set[tuple[int | None, str | None]] = set()
    for it in items:
        candidates: set[int] = set()
        if it["item_id"] is not None:
            # 调用方给了 id 就按 id 为准，不再按名扩大候选。
            candidates = {it["item_id"]} if it["item_id"] in by_id else set()
        elif it.get("name"):
            candidates = by_name.get(it["name"], set())
        if len(candidates) == 1:
            entity_id = next(iter(candidates))
            name = id_to_name.get(entity_id) or it["name"]
            item_id = entity_id
        else:
            name, item_id = it["name"], it["item_id"]
        key = (item_id, name, it.get("name"))
        if key in seen:
            continue
        seen.add(key)
        resolved.append({
            "name": name,
            "item_id": item_id,
            # 调用方原话要留着：别名被解析成规范名之后，写法→组的查找仍按原话走
            # （如「三号碎片」要按片号收窄，「黑金刀」要落到同一组）。
            "input_name": it.get("name"),
            # 名字同时命中多个实体（规范名 + 别人的别名）时原样带出，交给上层报歧义。
            "entity_ids": sorted(candidates),
            "entity_names": [id_to_name[i] for i in sorted(candidates) if i in id_to_name],
        })
    return resolved


def _connect_generated(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _load_registry(path: pathlib.Path, project_id: int | None) -> dict | None:
    """读「同物组 + 写法」登记表（一次 JOIN 批量取回，不按名字循环）。

    表不存在（旧库）返回 ``None``，调用方走原来的按名解析，行为不变。
    """
    con = _connect_generated(path)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"kg_item_group", "kg_item_surface"} <= tables:
            return None
        sql = (
            "SELECT g.id AS gid, g.canonical_name, g.status AS g_status,"
            "       g.generic_members, g.note AS g_note,"
            "       s.surface, s.status AS s_status, s.instance_label"
            "  FROM kg_item_group g"
            "  LEFT JOIN kg_item_surface s ON s.group_id = g.id"
        )
        params: tuple = ()
        if project_id is not None:
            sql += " WHERE g.project_id = ?"
            params = (project_id,)
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    groups: dict[int, dict] = {}
    by_surface: dict[str, dict] = {}
    surfaces_by_group: dict[int, list[dict]] = {}
    name_to_group: dict[str, int] = {}
    raw_members: dict[int, str] = {}
    for row in rows:
        gid = row["gid"]
        if gid not in groups:
            groups[gid] = {"id": gid, "canonical_name": row["canonical_name"],
                           "status": row["g_status"], "members": [],
                           "note": row["g_note"] or ""}
            raw_members[gid] = row["generic_members"] or "[]"
        if row["surface"]:
            rec = {"surface": row["surface"], "status": row["s_status"],
                   "group_id": gid, "instance_label": row["instance_label"]}
            surfaces_by_group.setdefault(gid, []).append(rec)
            by_surface.setdefault(row["surface"], rec)
            if rec["status"] == "approved":
                name_to_group.setdefault(row["surface"], gid)

    for gid, raw in raw_members.items():
        try:
            groups[gid]["members"] = [int(x) for x in json.loads(raw)]
        except (TypeError, ValueError):
            groups[gid]["members"] = []
        # 规范名本身也是这个组的写法（老库里不一定有对应 surface 行）
        name_to_group.setdefault(groups[gid]["canonical_name"], gid)

    return {"groups": groups, "by_surface": by_surface,
            "surfaces_by_group": surfaces_by_group,
            "name_to_group": name_to_group,
            "group_by_canonical": {g["canonical_name"]: gid for gid, g in groups.items()}}


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

        if surf_group and surf_group["status"] == "generic":
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
    """一个单位的事实：按实例拆、每个 hold_type 只留顺序键最大的一段。"""
    if unit["instance_label"]:
        # 专指某一号碎片时，组级事实（instance_id 为空，如 own）仍保留。
        rows = [r for r in rows
                if r["instance_label"] == unit["instance_label"] or r["instance_id"] is None]

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
                      "status": _status_of(picked), "lines": picked})
    return facts


def _fetch_holding_rows(path: pathlib.Path, items: list[dict]) -> list[sqlite3.Row]:
    """第二次查询：一条 IN 语句取回所有候选段 + 旁注顺序键 + 实例片号。"""
    names = sorted({it["name"] for it in items if it.get("name")})
    ids = sorted({it["item_id"] for it in items if it.get("item_id") is not None})
    if not names and not ids:
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
        if names:
            conds.append(f"h.item_name IN ({','.join('?' * len(names))})")
            params.extend(names)
        if ids:
            conds.append(f"h.item_id IN ({','.join('?' * len(ids))})")
            params.extend(ids)

        sql = f"""SELECT h.id, h.item_id, h.instance_id, h.item_name,
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
) -> list[dict]:
    """批量取「这些物品现在在谁手上」。

    Args:
        items: 物品名（str）或 ``{"name": ..., "item_id": ...}``；name 可空，
            只要有 item_id 也能解析出规范名。
        db: ``novel_dev.db`` 的 SQLAlchemy session。为 None 时跳过实体解析
            （只按调用方给的名字/ id 查），不推荐在生产路径省掉。
        project_id: 实体解析时限定项目，避免跨项目同名串味。
        generated_db: 持有库路径，缺省读 ``settings.kg_generated_db``。

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

    path = pathlib.Path(generated_db) if generated_db else _default_generated_db()
    if not path.exists():
        return []

    resolved = _resolve_entities(db, project_id, normalized)
    try:
        registry = _load_registry(path, project_id)
    except sqlite3.Error:
        registry = None
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

    fetch_items: list[dict] = []
    for unit in units + extras:
        if unit["ambiguous"]:
            continue
        if unit["group_id"] is not None and registry:
            for rec in registry["surfaces_by_group"].get(unit["group_id"], []):
                if rec["status"] == "approved":
                    fetch_items.append({"name": rec["surface"], "item_id": None})
        elif unit["name"]:
            fetch_items.append({"name": unit["name"], "item_id": unit["item_id"]})

    try:
        rows = _fetch_holding_rows(path, fetch_items) if fetch_items else []
    except sqlite3.Error:
        return []

    # 行 → 单位：优先按「这个写法属于哪个组」，再退回 item_id / 原名。
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
        if row["item_id"] is not None and row["item_id"] in uid_by_id:
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


def format_holding_facts(facts: Iterable[dict]) -> str:
    """把事实渲染成 prompt 用的自然语言行（每条物品一行）。"""
    lines: list[str] = []
    for fact in facts:
        name = fact["item_name"] or "（未命名物品）"
        label = fact.get("instance_label")
        if label and label not in name:
            name = f"{name}·{label}"
        if fact["status"] == GENERIC:
            lines.append(f"- {name}：{_render_generic(fact)}")
            continue
        if fact["status"] == AMBIGUOUS:
            lines.append(f"- {name}：{_render_ambiguous(fact)}")
            continue
        if fact["status"] == NO_RECORD or not fact["lines"]:
            body = "无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）"
            if fact.get("note"):
                body = f"{body}；{fact['note']}"
        else:
            body = "；".join(_render_line(line) for line in fact["lines"])
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
            body = "；".join(_render_line(line) for line in cand["lines"])
        else:
            body = "无持有记录"
        parts.append(f"{cand['item_name']}（{body}）")
    return f"{note}；候选：" + " / ".join(parts)


def _render_ambiguous(fact: dict) -> str:
    names = [c.get("item_name") or f"实体#{c.get('entity_id')}"
             for c in (fact.get("candidates") or [])]
    detail = " / ".join(names) if names else "多个候选"
    return f"名字有歧义，可能在指：{detail}；不猜，需指定具体是哪一件"
