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


def _resolve_entities(db, project_id: int | None, items: list[dict]) -> list[dict]:
    """第一次查询：从 ``kg_entities`` 批量解析规范名（按 id / 名 / 别名）。

    解析不到就保留调用方给的原名/原 id——读通道不替调用方猜实体。
    """
    if db is None or not items:
        return [dict(it) for it in items]
    names = {it["name"] for it in items if it.get("name")}
    ids = {it["item_id"] for it in items if it.get("item_id") is not None}
    if not names and not ids:
        return [dict(it) for it in items]

    try:
        from sqlalchemy import or_, select

        from app.models import KgEntity

        stmt = select(KgEntity.id, KgEntity.canonical_name, KgEntity.aliases).where(
            KgEntity.kind == "item"
        )
        if project_id is not None:
            stmt = stmt.where(KgEntity.project_id == project_id)
        conds = []
        if names:
            conds.append(KgEntity.canonical_name.in_(sorted(names)))
        if ids:
            conds.append(KgEntity.id.in_(sorted(ids)))
        rows = db.execute(stmt.where(or_(*conds))).all()
    except Exception:  # noqa: BLE001 - 实体索引读不到时按原名继续，不阻断生成
        return [dict(it) for it in items]

    by_id: dict[int, tuple[int, str]] = {}
    by_name: dict[str, tuple[int, str]] = {}
    for row in rows:
        entity_id, canonical = row[0], row[1]
        if not canonical:
            continue
        by_id[entity_id] = (entity_id, canonical)
        by_name.setdefault(canonical, (entity_id, canonical))
        for alias in (row[2] or []):
            if isinstance(alias, str) and alias.strip():
                by_name.setdefault(alias.strip(), (entity_id, canonical))

    resolved: list[dict] = []
    seen: set[tuple[int | None, str | None]] = set()
    for it in items:
        hit = by_id.get(it["item_id"]) if it["item_id"] is not None else None
        if hit is None and it.get("name"):
            hit = by_name.get(it["name"])
        name, item_id = (hit[1], hit[0]) if hit else (it["name"], it["item_id"])
        key = (item_id, name)
        if key in seen:
            continue
        seen.add(key)
        resolved.append({"name": name, "item_id": item_id})
    return resolved


def _connect_generated(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


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
        "status", "lines": [...]}``

        ``status`` 是聚合值（held > sealed > unowned > unknown > no_record），
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
        rows = _fetch_holding_rows(path, resolved)
    except sqlite3.Error:
        return []

    id_to_idx = {it["item_id"]: i for i, it in enumerate(resolved)
                 if it["item_id"] is not None}
    name_to_idx: dict[str, int] = {}
    for i, it in enumerate(resolved):
        if it.get("name"):
            name_to_idx.setdefault(it["name"], i)

    groups: dict[tuple[int, int | None], list[dict]] = {}
    for row in rows:
        idx = None
        if row["item_id"] is not None:
            idx = id_to_idx.get(row["item_id"])
        if idx is None:
            idx = name_to_idx.get(row["item_name"])
        if idx is None:  # 查询条件保证不会出现，保险起见不张冠李戴
            continue
        groups.setdefault((idx, row["instance_id"]), []).append(_row_to_line(row))

    facts: list[dict] = []
    for idx, item in enumerate(resolved):
        item_groups = [(inst, lines) for (i, inst), lines in groups.items() if i == idx]
        if not item_groups:
            facts.append({
                "item_name": item["name"],
                "item_id": item["item_id"],
                "instance_id": None,
                "instance_label": None,
                "status": NO_RECORD,
                "lines": [],
            })
            continue
        for instance_id, lines in sorted(
                item_groups, key=lambda x: (x[0] is not None, x[0] or 0)):
            # 同一 hold_type 只留顺序键最大的一段；own / custody 各自独立。
            latest: dict[str, dict] = {}
            for line in lines:
                old = latest.get(line["hold_type"])
                if old is None or (line["order_key"], line["chapter"], line["confidence"]) > (
                        old["order_key"], old["chapter"], old["confidence"]):
                    latest[line["hold_type"]] = line
            picked = sorted(latest.values(),
                            key=lambda x: 0 if x["hold_type"] == "own" else 1)
            label = next((r["instance_label"] for r in rows
                          if r["instance_id"] == instance_id and r["instance_label"]),
                         None) if instance_id is not None else None
            facts.append({
                "item_name": item["name"],
                "item_id": item["item_id"],
                "instance_id": instance_id,
                "instance_label": label,
                "status": _status_of(picked),
                "lines": picked,
            })
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
        if fact["status"] == NO_RECORD or not fact["lines"]:
            body = "无持有记录（no_record；本层不提供持有约束，不要写成「已确认无主」）"
        else:
            body = "；".join(_render_line(line) for line in fact["lines"])
        lines.append(f"- {name}：{body}")
    return "\n".join(lines)
