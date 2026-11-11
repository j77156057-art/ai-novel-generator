# -*- coding: utf-8 -*-
"""第三批历史数据修复：清非物品类目 + 修宽写法多算（两层同改）。

两步都落在「写法层 + 实体别名层」，每步一个独立快照、独立回滚点：

**Step 1 清类目**（``app/kg/item_category.py`` 的封闭手工标记表）
    ``狮子吼``（功法）、``怪物``（生物类称）、``信物``（关系概念）不是
    「能拿在手上、能丢失、能转交的物理对象」。它们在本库都没有独立的
    ``kg_entities`` 行，只以 alias 挂在物品实体上，所以「清类目」只能是：

    - ``kg_item_surface``：``approved`` → ``rejected``（保留行与证据，语义干净，
      读通道不会多加「待审映射」提示）；
    - ``kg_entities.aliases``：删掉该写法，避免别名把写法又解析回原组。

**Step 2 修多算**（按独有章数从大到小）
    ``佩刀``(85 章独有)、``弩``(78)、``玉佩``(29)、``长枪``(17)、``爱驹``(1)。
    ``长枪`` 不在任何实体的 aliases 里，只改 surface 这一层即生效。

不动：``劝学诗``、``天地一刀斩``（书名号伪差）、``账簿``（同物异写）。

**Step 3 摘残留**（§8 那批：正文指代 / 生物类称 / 纯器型泛称）
    ``此兽``、``灵兽``、``水陆两栖的异兽``、``宝刀``、``许七安的佩刀``、
    ``腰间一把军弩``、``那杆枪``。判据走 ``item_generic`` 的可迁移规则：
    ``R6_reference_phrase``（指示代词/「的」/数量短语）、``R4`` 的生物类称语素、
    ``R4`` 纯器型。待审档（``水中坐骑``/``修长战刀``/``暗金色长刀``/``刀胚``/
    ``法器军弩``/``白马银枪``）本步不动，见 Step 4。

**Step 4 摘描述性写法**（上一批的待审档，人工裁定后执行）
    ``水中坐骑``、``修长战刀``、``白马银枪``、``法器军弩``。
    这 4 条都是「修饰语 + 器型/类称」，形态判据覆盖不了（同形态在本库里就是真物品名
    ``半块玉佩``/``法器铜锣``/``大钢刀``/``银枪``，硬编规则会误伤），因此落到
    ``item_category.DESCRIPTIVE_SURFACES`` 这张封闭手工表，脚本只执行表里登记过的写法。

    ``暗金色长刀`` 原在本步降级，复核后**已恢复**（``--restore``）：全书 4 处只指同一把
    刀，其中 ch843/919/926 是那三章的唯一写法，摘掉会丢 3 章真实覆盖。它已从
    ``STEPS[4]`` 与手工表里移出，避免本步重复降级。

    ``刀胚`` 不降：见该表外的说明——ch93 三次都是同一次锻造场景，有动作句
    （「监正握住刀胚」「屈指一弹」）和明确持有者（监正 → 许七安），是成形前的同一件实物。

写库前的守卫
------------
受影响组里的每条 ``kg_item_holding.item_name`` 在改动后仍必须能解析回该组
（规范名仍 ``approved``，或该名字仍是同组 approved 写法，或规范名实体还在）。
任何一条解析不到 → 整步中止、不写库、打印是哪条持有行，交人工判断。

用法::

    python backend/migrations/2026-11-13-item-cleanup-v1/apply_cleanup.py --step 1 --dry-run
    python backend/migrations/2026-11-13-item-cleanup-v1/apply_cleanup.py --step 1 --apply
    python backend/migrations/2026-11-13-item-cleanup-v1/apply_cleanup.py --step 2 --apply
    python ... --rollback backend/migrations/2026-11-13-item-cleanup-v1/snapshots/cleanup_step1_xxx.json

    # 单条恢复（复核后确认写法专指本组这一件，摘掉会丢真实覆盖）
    python ... --restore 暗金色长刀 --note "复核理由" --apply

边界：不动 ``kg_item_group`` / ``kg_item_holding`` / ``kg_holding_story_segment``，
不重抽，不改 ``kg_entities.kind``（这五项没有独立实体行可改）。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
if str(PROJ / "backend") not in sys.path:
    sys.path.insert(0, str(PROJ / "backend"))

from app.kg import item_category as icat  # noqa: E402

GEN_DB = PROJ / "backend" / "data" / "generated.db"
APP_DB = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"
SNAP_DIR = HERE / "snapshots"
MIGRATION = "2026-11-13-item-cleanup-v1"


@dataclass(frozen=True)
class Target:
    surface: str
    group_id: int
    canonical: str
    reason: str = ""


STEPS: dict[int, tuple[Target, ...]] = {
    1: (
        Target("狮子吼", 30, "金色雄狮咆哮图"),
        Target("怪物", 20, "灵龙"),
        Target("信物", 7, "半块玉佩"),
    ),
    2: (
        Target("佩刀", 45, "黑金长刀"),
        Target("弩", 5, "军弩"),
        Target("玉佩", 7, "半块玉佩"),
        Target("长枪", 33, "银枪"),
        Target("爱驹", 38, "雪蹄乌龙骠"),
    ),
    3: (
        Target("此兽", 20, "灵龙",
               "正文指代（6 章分别指灵龙、靖国杂交马种等不同对象），不是名字"),
        Target("灵兽", 20, "灵龙",
               "生物类称（11 章里赤尾烈鹰等也自称灵兽），不专指这只坐骑"),
        Target("水陆两栖的异兽", 20, "灵龙",
               "描述短语（含「的」），1 章且与灵龙同章，摘除不丢定位"),
        Target("宝刀", 45, "黑金长刀",
               "纯器型泛称（3 章里 2 章指别人的刀），比专名宽"),
        Target("许七安的佩刀", 45, "黑金长刀",
               "人称 + 的 + 器物：携带会过期的持有者信息，属正文指代，不是稳定别名"),
        Target("腰间一把军弩", 5, "军弩",
               "数量短语描述，1 章且与军弩同章"),
        Target("那杆枪", 33, "银枪",
               "指示代词短语；唯一出现是比喻（「不愿意当那杆枪」），不是这支枪"),
    ),
    4: (
        Target("水中坐骑", 20, "灵龙"),
        Target("修长战刀", 45, "黑金长刀"),
        Target("白马银枪", 33, "银枪"),
        Target("法器军弩", 5, "军弩"),
    ),
}


# ---------------------------------------------------------------------------
# 连接与快照
# ---------------------------------------------------------------------------
def _connect(path: pathlib.Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _checkpoint(con: sqlite3.Connection) -> None:
    """WAL 库备份前把 -wal 落盘，保证拷贝出来的是完整库。"""
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.DatabaseError:
        pass


def _backup(path: pathlib.Path, label: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / f"{path.stem}-pre-cleanup-{label}-{stamp}{path.suffix}"
    shutil.copy2(path, target)
    return target


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 读现状
# ---------------------------------------------------------------------------
def _surface(gen: sqlite3.Connection, project_id: int, surface: str):
    return gen.execute(
        "SELECT * FROM kg_item_surface WHERE project_id=? AND surface=?",
        (project_id, surface)).fetchone()


def _group(gen: sqlite3.Connection, gid: int):
    return gen.execute("SELECT * FROM kg_item_group WHERE id=?", (gid,)).fetchone()


def _entity(app: sqlite3.Connection, project_id: int, canonical: str):
    return app.execute(
        "SELECT id, canonical_name, aliases, updated_at FROM kg_entities"
        " WHERE project_id=? AND kind='item' AND canonical_name=?",
        (project_id, canonical)).fetchone()


def _holdings(gen: sqlite3.Connection, gid: int) -> list[sqlite3.Row]:
    return gen.execute(
        "SELECT id, group_id, item_id, item_name, instance_id, hold_type"
        " FROM kg_item_holding WHERE group_id=?", (gid,)).fetchall()


def _approved_surfaces(gen: sqlite3.Connection, gid: int) -> set[str]:
    return {r["surface"] for r in gen.execute(
        "SELECT surface FROM kg_item_surface WHERE group_id=? AND status='approved'",
        (gid,)).fetchall()}


def _aliases(raw) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [a for a in value if isinstance(a, str)]


def _text_evidence(app: sqlite3.Connection, novel_id: int,
                   surface: str) -> tuple[int, int | None, str]:
    """返回（出现章数、首章、首处上下文），只读。"""
    rows = app.execute("SELECT chapter_no, text FROM novel_chunks WHERE novel_id=?"
                       " ORDER BY chapter_no, idx", (novel_id,)).fetchall()
    chapters: list[int] = []
    first_chapter, snippet = None, ""
    for row in rows:
        text = row["text"] or ""
        if surface not in text:
            continue
        if row["chapter_no"] not in chapters:
            chapters.append(row["chapter_no"])
        if first_chapter is None:
            pos = text.find(surface)
            first_chapter = row["chapter_no"]
            snippet = text[max(0, pos - 30):pos + 80].replace("\n", " ")
    return len(chapters), first_chapter, snippet


# ---------------------------------------------------------------------------
# 守卫：真持有行必须仍然读得到
# ---------------------------------------------------------------------------
def _guard_holdings(gen: sqlite3.Connection, app: sqlite3.Connection,
                    project_id: int, target: Target,
                    entity_row) -> tuple[list[dict], list[dict]]:
    """返回（守卫明细, 冲突）。冲突非空 → 整步中止。"""
    remaining = _approved_surfaces(gen, target.group_id) - {target.surface}
    canonical_entity = bool(entity_row is not None)
    detail: list[dict] = []
    conflicts: list[dict] = []
    for h in _holdings(gen, target.group_id):
        name = (h["item_name"] or "").strip()
        resolvable = (name in remaining
                      or (name == target.canonical and canonical_entity))
        row = {"holding_id": h["id"], "item_name": name,
               "hold_type": h["hold_type"], "instance_id": h["instance_id"],
               "after_resolvable": resolvable}
        detail.append(row)
        if not resolvable:
            conflicts.append(row)
    return detail, conflicts


# ---------------------------------------------------------------------------
# 变更计划（不改库）
# ---------------------------------------------------------------------------
def plan_step(gen: sqlite3.Connection, app: sqlite3.Connection, project_id: int,
              step: int) -> dict:
    surfaces: list[dict] = []
    entities: list[dict] = []
    conflicts: list[dict] = []
    skipped: list[dict] = []

    for target in STEPS[step]:
        if step == 4 and not icat.is_descriptive(target.surface):
            skipped.append({"surface": target.surface,
                            "why": "未登记在 item_category.DESCRIPTIVE_SURFACES，拒绝执行"})
            continue
        if step == 4 and icat.descriptive_group(target.surface) != target.canonical:
            skipped.append({"surface": target.surface,
                            "why": f"手工表登记组 {icat.descriptive_group(target.surface)}"
                                   f" 与计划组 {target.canonical} 不符"})
            continue
        surface_row = _surface(gen, project_id, target.surface)
        if surface_row is None:
            skipped.append({"surface": target.surface, "why": "写法未登记"})
            continue
        group = _group(gen, surface_row["group_id"])
        if group is None:
            skipped.append({"surface": target.surface,
                            "why": f"组 {surface_row['group_id']} 不存在"})
            continue
        if surface_row["status"] != "approved":
            skipped.append({"surface": target.surface,
                            "why": f"当前状态 {surface_row['status']}，不是 approved"})
            continue
        if int(group["id"]) != target.group_id or group["canonical_name"] != target.canonical:
            skipped.append({"surface": target.surface,
                            "why": f"组不符：实际 {group['id']}/{group['canonical_name']}"})
            continue

        entity_row = _entity(app, project_id, target.canonical)
        old_aliases = _aliases(entity_row["aliases"]) if entity_row else []
        removable = target.surface in old_aliases
        new_aliases = [a for a in old_aliases if a != target.surface]

        guard_rows, guard_conflicts = _guard_holdings(
            gen, app, project_id, target, entity_row)
        conflicts.extend(guard_conflicts)

        surfaces.append({
            "surface": target.surface,
            "surface_id": surface_row["id"],
            "group_id": target.group_id,
            "group_name": target.canonical,
            "old_status": surface_row["status"],
            "new_status": "rejected",
            "old_note": surface_row["note"],
            "old_confidence": surface_row["confidence"],
            "old_reviewer": surface_row["reviewer"],
            "old_reviewed_at": surface_row["reviewed_at"],
            "new_note": _new_note(target, step),
            "evidence_chapter": surface_row["evidence_chapter"],
            "evidence": surface_row["evidence"],
            "holdings": guard_rows,
        })
        if entity_row is not None:
            entities.append({
                "entity_id": entity_row["id"],
                "canonical_name": target.canonical,
                "old_aliases": old_aliases,
                "new_aliases": new_aliases,
                "removed": [target.surface] if removable else [],
                "old_updated_at": entity_row["updated_at"],
            })

    return {"step": step, "project_id": project_id,
            "surfaces": surfaces, "entities": entities,
            "conflicts": conflicts, "skipped": skipped}


def plan_add(gen: sqlite3.Connection, app: sqlite3.Connection, project_id: int,
             surface: str, group_id: int | None, note: str = "") -> dict:
    """给已有组新增一条写法（新建 surface 行 + 把写法加进该组实体的别名）。

    只用于「正文后期的正式名，和组里现有规范名是同一件」的补充登记；
    不建组、不改规范名、不动持有行。
    """
    empty = {"label": "add", "project_id": project_id, "surfaces": [],
             "entities": [], "conflicts": [], "skipped": []}
    if _surface(gen, project_id, surface) is not None:
        return {**empty, "skipped": [{"surface": surface, "why": "写法已被占用"}]}
    if group_id is None:
        return {**empty, "skipped": [{"surface": surface, "why": "未指定组 id"}]}
    group = _group(gen, int(group_id))
    if group is None or int(group["project_id"]) != project_id:
        return {**empty, "skipped": [{"surface": surface,
                                      "why": f"组 {group_id} 不存在或不属于项目 {project_id}"}]}

    chapters, first_chapter, snippet = _text_evidence(app, project_id, surface)
    if not chapters:
        return {**empty, "skipped": [{"surface": surface, "why": "正文里找不到该写法"}]}

    entity_row = _entity(app, project_id, group["canonical_name"])
    old_aliases = _aliases(entity_row["aliases"]) if entity_row else []
    new_note = note or (f"补充登记：正文后期正式名，与组名「{group['canonical_name']}」"
                        f"是同一件（{chapters} 章出现）；不新建物品，只加写法")
    plan = {
        "label": "add", "project_id": project_id,
        "surfaces": [{
            "surface": surface,
            "surface_id": None,
            "group_id": group["id"],
            "group_name": group["canonical_name"],
            "old_status": None,
            "new_status": "approved",
            "old_note": None,
            "old_confidence": None,
            "old_reviewer": None,
            "old_reviewed_at": None,
            "new_note": new_note,
            "evidence_chapter": first_chapter,
            "evidence": snippet,
            "chapters": chapters,
            "holdings": [dict(h) for h in _holdings(gen, group["id"])],
        }],
        "entities": [], "conflicts": [], "skipped": [],
    }
    if entity_row is None:
        plan["skipped"].append({"surface": surface,
                                "why": "组规范名没有对应实体行，只加 surface"})
    else:
        plan["entities"].append({
            "entity_id": entity_row["id"],
            "canonical_name": group["canonical_name"],
            "old_aliases": old_aliases,
            "new_aliases": old_aliases + ([surface] if surface not in old_aliases else []),
            "removed": [],
            "added": [] if surface in old_aliases else [surface],
            "old_updated_at": entity_row["updated_at"],
        })
    return plan


def plan_restore(gen: sqlite3.Connection, app: sqlite3.Connection, project_id: int,
                 surface: str, note: str = "") -> dict:
    """把一条已 rejected 的写法恢复成 approved，并把写法加回实体别名。

    只用于「复核后确认该写法专指本组这一件、摘掉会丢真实覆盖」的情形；
    快照格式与 ``plan_step`` 一致，因此 ``--rollback`` 能把它退回 rejected。
    """
    surfaces: list[dict] = []
    entities: list[dict] = []
    skipped: list[dict] = []
    row = _surface(gen, project_id, surface)
    if row is None:
        return {"label": "restore", "project_id": project_id, "surfaces": [],
                "entities": [], "conflicts": [],
                "skipped": [{"surface": surface, "why": "写法未登记"}]}
    group = _group(gen, row["group_id"])
    if group is None:
        return {"label": "restore", "project_id": project_id, "surfaces": [],
                "entities": [], "conflicts": [],
                "skipped": [{"surface": surface, "why": "组不存在"}]}
    if row["status"] == "approved":
        return {"label": "restore", "project_id": project_id, "surfaces": [],
                "entities": [], "conflicts": [],
                "skipped": [{"surface": surface, "why": "当前已是 approved"}]}
    if row["status"] != "rejected":
        return {"label": "restore", "project_id": project_id, "surfaces": [],
                "entities": [], "conflicts": [],
                "skipped": [{"surface": surface,
                             "why": f"当前状态 {row['status']}，只恢复 rejected"}]}

    entity_row = _entity(app, project_id, group["canonical_name"])
    old_aliases = _aliases(entity_row["aliases"]) if entity_row else []
    new_note = note or ("恢复写法的专指性：复核确认本库正文里该写法只指本组这一件，"
                        "摘除会丢真实覆盖（详见 reports 对应章节）")
    surfaces.append({
        "surface": surface,
        "surface_id": row["id"],
        "group_id": group["id"],
        "group_name": group["canonical_name"],
        "old_status": row["status"],
        "new_status": "approved",
        "old_note": row["note"],
        "old_confidence": row["confidence"],
        "old_reviewer": row["reviewer"],
        "old_reviewed_at": row["reviewed_at"],
        "new_note": new_note,
        "evidence_chapter": row["evidence_chapter"],
        "evidence": row["evidence"],
        "holdings": [dict(h) for h in _holdings(gen, group["id"])],
    })
    if entity_row is not None:
        entities.append({
            "entity_id": entity_row["id"],
            "canonical_name": group["canonical_name"],
            "old_aliases": old_aliases,
            "new_aliases": old_aliases + ([surface] if surface not in old_aliases else []),
            "removed": [],
            "added": [] if surface in old_aliases else [surface],
            "old_updated_at": entity_row["updated_at"],
        })
    else:
        skipped.append({"surface": surface,
                        "why": "组规范名没有对应实体行，只恢复 surface"})
    return {"label": "restore", "project_id": project_id, "surfaces": surfaces,
            "entities": entities, "conflicts": [], "skipped": skipped}


def _new_note(target: Target, step: int) -> str:
    surface, canonical = target.surface, target.canonical
    if step == 1:
        category = icat.category_of(surface)
        label = icat.CATEGORY_LABELS.get(category or "", "非物品")
        reason = icat.reason_of(surface) or ""
        return (f"清类目：{label}，不是可持有/可转交的物理对象。{reason}"
                f"（原登记在组「{canonical}」，已摘除写法与实体别名）")
    if step == 3:
        return (f"摘残留用法：{target.reason}"
                f"（原登记在组「{canonical}」，已摘除写法与实体别名）")
    if step == 4:
        return (f"描述性写法：{icat.descriptive_reason(surface) or ''}"
                f"（原登记在组「{canonical}」，已摘除写法与实体别名）")
    return (f"修多算：宽写法，覆盖章远多于组名「{canonical}」，多指别处的同类物件"
            f"（已摘除写法与实体别名）")


# ---------------------------------------------------------------------------
# 执行 / 回滚
# ---------------------------------------------------------------------------
def apply_plan(gen: sqlite3.Connection, app: sqlite3.Connection,
               plan: dict) -> dict:
    stamp = _now()
    for item in plan["surfaces"]:
        if item.get("surface_id") is None:
            cur = gen.execute(
                "INSERT INTO kg_item_surface (project_id, surface, group_id, status,"
                " source, confidence, evidence_chapter, evidence, note, reviewer,"
                " reviewed_at, created_at)"
                " VALUES (?, ?, ?, 'approved', 'migration', 0.95, ?, ?, ?, ?, ?, ?)",
                (plan["project_id"], item["surface"], item["group_id"],
                 item["evidence_chapter"], item["evidence"], item["new_note"],
                 f"migration:{MIGRATION}", stamp, stamp))
            item["surface_id"] = int(cur.lastrowid)
            continue
        gen.execute(
            "UPDATE kg_item_surface SET status=?, note=?, reviewer=?, reviewed_at=?"
            " WHERE id=?",
            (item["new_status"], item["new_note"],
             f"migration:{MIGRATION}", stamp, item["surface_id"]))
    # 同一个实体可能一次摘多个写法（如 eid21 摘 3 条）：合并成一条 UPDATE，
    # 否则逐条覆盖会只剩最后一条生效。
    by_entity: dict[int, dict] = {}
    for item in plan["entities"]:
        if not item.get("removed") and not item.get("added"):
            continue
        slot = by_entity.setdefault(item["entity_id"],
                                    {"keep": list(item["old_aliases"]),
                                     "drop": set(), "add": []})
        slot["drop"].update(item.get("removed") or [])
        slot["add"].extend(item.get("added") or [])
    for entity_id, slot in by_entity.items():
        aliases = [a for a in slot["keep"] if a not in slot["drop"]]
        for name in slot["add"]:
            if name not in aliases:
                aliases.append(name)
        app.execute(
            "UPDATE kg_entities SET aliases=?, updated_at=? WHERE id=?",
            (json.dumps(aliases, ensure_ascii=False), stamp, entity_id))
    return {"applied_at": stamp, "entities": len(by_entity)}


def write_snapshot(plan: dict, backups: dict) -> pathlib.Path:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if plan.get("label") == "restore":
        name = f"cleanup_restore_{plan['surfaces'][0]['surface']}_{stamp}.json"
    elif plan.get("label") == "add":
        name = f"cleanup_add_{plan['surfaces'][0]['surface']}_{stamp}.json"
    else:
        name = f"cleanup_step{plan['step']}_{stamp}.json"
    path = SNAP_DIR / name
    payload = {"migration": MIGRATION, "created_at": _now(),
               "backups": backups, **plan}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def rollback(snapshot: pathlib.Path, gen: sqlite3.Connection,
             app: sqlite3.Connection) -> dict:
    snap = json.loads(snapshot.read_text(encoding="utf-8"))
    for item in snap["surfaces"]:
        if item.get("old_status") is None and item.get("surface_id"):
            # 本步新插入的写法：回滚即删除整行
            gen.execute("DELETE FROM kg_item_surface WHERE id=?", (item["surface_id"],))
            continue
        gen.execute(
            "UPDATE kg_item_surface SET status=?, note=?, confidence=?,"
            " reviewer=?, reviewed_at=? WHERE id=?",
            (item["old_status"], item["old_note"], item["old_confidence"],
             item["old_reviewer"], item["old_reviewed_at"], item["surface_id"]))
    for item in snap["entities"]:
        app.execute(
            "UPDATE kg_entities SET aliases=?, updated_at=? WHERE id=?",
            (json.dumps(item["old_aliases"], ensure_ascii=False),
             item["old_updated_at"], item["entity_id"]))
    gen.commit()
    app.commit()

    mismatched: list[dict] = []
    for item in snap["surfaces"]:
        row = gen.execute("SELECT status FROM kg_item_surface WHERE id=?",
                          (item["surface_id"],)).fetchone()
        if item.get("old_status") is None:
            if row is not None:
                mismatched.append({"surface": item["surface"],
                                   "got": "行仍在（应已删除）"})
            continue
        if row is None or row["status"] != item["old_status"]:
            mismatched.append({"surface": item["surface"], "got": row and row["status"]})
    for item in snap["entities"]:
        row = app.execute("SELECT aliases FROM kg_entities WHERE id=?",
                          (item["entity_id"],)).fetchone()
        got = _aliases(row["aliases"]) if row else None
        if got != item["old_aliases"]:
            mismatched.append({"entity": item["canonical_name"], "got": got})
    return {"snapshot": str(snapshot), "restored_surfaces": len(snap["surfaces"]),
            "restored_entities": len(snap["entities"]), "mismatched": mismatched}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, choices=sorted(STEPS), default=1)
    ap.add_argument("--apply", action="store_true", help="真正写库；默认 dry-run")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划（默认行为）")
    ap.add_argument("--rollback", default=None, help="快照 JSON 路径，按快照回滚")
    ap.add_argument("--restore", default=None,
                    help="把一条已 rejected 的写法恢复成 approved（写回实体别名）")
    ap.add_argument("--add-surface", default=None,
                    help="给已有组新增一条写法（新建 surface + 加进实体别名）")
    ap.add_argument("--group", type=int, default=None, help="--add-surface 的目标组 id")
    ap.add_argument("--note", default="", help="--restore/--add-surface 写进 note 的理由")
    ap.add_argument("--db", default=str(GEN_DB))
    ap.add_argument("--app-db", default=str(APP_DB))
    ap.add_argument("--project-id", type=int, default=5)
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    gen_path, app_path = pathlib.Path(args.db), pathlib.Path(args.app_db)
    gen, app = _connect(gen_path), _connect(app_path)
    try:
        if args.rollback:
            result = rollback(pathlib.Path(args.rollback), gen, app)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        if args.restore:
            plan = plan_restore(gen, app, args.project_id, args.restore, args.note)
            if not plan["surfaces"]:
                print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
                raise SystemExit(1)
            label = "restore"
        elif args.add_surface:
            plan = plan_add(gen, app, args.project_id, args.add_surface,
                            args.group, args.note)
            if not plan["surfaces"]:
                print(json.dumps({"dry_run": True, **plan}, ensure_ascii=False, indent=2))
                raise SystemExit(1)
            label = "add"
        else:
            plan = plan_step(gen, app, args.project_id, args.step)
            label = f"step{args.step}"
        if plan["conflicts"]:
            print(json.dumps({"aborted": True,
                              "why": "摘除后真持有行会读不到，先报不动",
                              "step": args.step,
                              "conflicts": plan["conflicts"],
                              "plan": plan}, ensure_ascii=False, indent=2))
            raise SystemExit(2)

        dry_run = not args.apply
        backups: dict = {}
        if dry_run:
            gen.rollback()
            app.rollback()
        else:
            if not args.no_backup:
                _checkpoint(gen)
                backups["gen"] = str(_backup(gen_path, label))
                backups["app"] = str(_backup(app_path, label))
            result = apply_plan(gen, app, plan)
            gen.commit()
            app.commit()
            backups["snapshot"] = str(write_snapshot(plan, backups))
            plan["applied"] = result

        print(json.dumps({"dry_run": dry_run, **plan}, ensure_ascii=False, indent=2))
    finally:
        gen.close()
        app.close()


if __name__ == "__main__":
    main()
