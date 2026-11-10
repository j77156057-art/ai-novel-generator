"""生成层（generated）独立存储。

物理隔离原则：本库只存 AI 新写章节抽取出来的“已写情节”，与原著层
`kg_vol1_p1fix.db` 完全不共享表、不共享连接、不写回原库。

所有行都带 source='generated' 与 source_chapter；节点属性只接收非保护字段。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from app.kg.layers import GENERATED, strip_protected

SCHEMA = """
CREATE TABLE IF NOT EXISTS gen_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'generated',
    source_chapter INTEGER,
    first_chapter INTEGER,
    last_chapter INTEGER,
    mentions INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gen_node_mentions (
    name TEXT NOT NULL,
    chapter INTEGER NOT NULL,
    mentions INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, chapter)
);

CREATE TABLE IF NOT EXISTS gen_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_name TEXT NOT NULL,
    to_name TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    strength REAL NOT NULL DEFAULT 0.5,
    source TEXT NOT NULL DEFAULT 'generated',
    source_chapter INTEGER,
    chapters TEXT NOT NULL DEFAULT '[]',
    evidence TEXT,
    direction_basis TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5,
    established_chapter INTEGER,
    established_evidence TEXT,
    confirm_method TEXT NOT NULL DEFAULT '',
    valid_from INTEGER,
    valid_to INTEGER,
    status TEXT NOT NULL DEFAULT 'active',
    UNIQUE(from_name, to_name, rel_type)
);

CREATE TABLE IF NOT EXISTS kg_item_holding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    hold_type TEXT NOT NULL CHECK (hold_type IN ('own', 'custody')),
    owner_id INTEGER,
    owner_name TEXT,
    owner_kind TEXT,
    owner_state TEXT NOT NULL
        CHECK (owner_state IN ('held', 'unowned', 'unknown', 'sealed')),
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    transfer_event_id INTEGER,
    reason_code TEXT NOT NULL DEFAULT '',
    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    -- held 态：至少要有一个持有者标识（id 或未实体化的名字）；
    -- unowned / sealed / unknown：owner_name 必须为空，不许造「未知」「封印」这类假持有者。
    CHECK (
        (owner_state = 'held'
         AND (owner_id IS NOT NULL
              OR (owner_name IS NOT NULL AND length(trim(owner_name)) > 0)))
        OR
        (owner_state IN ('unowned', 'sealed', 'unknown')
         AND owner_name IS NULL)
    ),
    UNIQUE (item_id, hold_type, valid_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_item_holding_open
    ON kg_item_holding (COALESCE(item_id, -1), item_name, hold_type)
    WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_item_holding_item
    ON kg_item_holding (item_id, valid_from);
CREATE INDEX IF NOT EXISTS ix_item_holding_owner
    ON kg_item_holding (owner_id, valid_to, hold_type);

-- 第二条轴：故事先后。只做旁注，不改上面这张表。
-- holding_id 非空 = 主线段的旁注；holding_id 空 = 回溯/预叙分支（不进章节轴）。
-- order_key：同一物品内可比，越大越新；NULL = 正文看不出先后，读侧退到章节序。
CREATE TABLE IF NOT EXISTS kg_holding_story_segment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    holding_id INTEGER,
    item_id INTEGER,
    item_name TEXT NOT NULL CHECK (length(trim(item_name)) > 0),
    hold_type TEXT NOT NULL CHECK (hold_type IN ('own', 'custody')),
    owner_id INTEGER,
    owner_name TEXT,
    owner_kind TEXT,
    owner_state TEXT NOT NULL
        CHECK (owner_state IN ('held', 'unowned', 'unknown', 'sealed')),
    order_key INTEGER,
    order_basis TEXT NOT NULL DEFAULT 'chapter'
        CHECK (order_basis IN ('explicit', 'chapter')),
    anchor_label TEXT NOT NULL DEFAULT '',
    narration TEXT NOT NULL DEFAULT 'forward'
        CHECK (narration IN ('forward', 'retro', 'prefigure')),
    transfer_from_id INTEGER,
    transfer_from_name TEXT,
    evidence_chapter INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0 AND confidence <= 1),
    created_at TEXT NOT NULL,
    CHECK ((order_basis = 'explicit') = (order_key IS NOT NULL)),
    CHECK (order_key IS NULL OR order_key >= 0),
    CHECK (narration = 'forward' OR holding_id IS NULL),
    CHECK (
        (owner_state = 'held'
         AND (owner_id IS NOT NULL
              OR (owner_name IS NOT NULL AND length(trim(owner_name)) > 0)))
        OR
        (owner_state IN ('unowned', 'sealed', 'unknown')
         AND owner_name IS NULL)
    ),
    CHECK (transfer_from_name IS NULL OR length(trim(transfer_from_name)) > 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_holding
    ON kg_holding_story_segment (holding_id)
    WHERE holding_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_story_segment_branch
    ON kg_holding_story_segment
       (COALESCE(item_id, -1), item_name, hold_type, COALESCE(order_key, -1))
    WHERE holding_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_story_segment_item
    ON kg_holding_story_segment (item_id, order_key);
CREATE INDEX IF NOT EXISTS ix_story_segment_owner
    ON kg_holding_story_segment (owner_id, order_key);

CREATE TABLE IF NOT EXISTS gen_edge_invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_name TEXT NOT NULL,
    to_name TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    chapter INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    trigger TEXT NOT NULL DEFAULT '',
    evidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(from_name, to_name, rel_type, chapter, reason, trigger)
);

CREATE TABLE IF NOT EXISTS gen_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter INTEGER,
    event_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    subject TEXT,
    object TEXT,
    location TEXT,
    source TEXT NOT NULL DEFAULT 'generated',
    evidence TEXT,
    subject_status TEXT NOT NULL DEFAULT 'resolved',
    raw_subject TEXT
);

CREATE TABLE IF NOT EXISTS gen_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_name TEXT NOT NULL,
    to_name TEXT NOT NULL,
    rel_type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'generated',
    source_chapter INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    evidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_chapter, from_name, to_name, rel_type, reason)
);

CREATE TABLE IF NOT EXISTS gen_rejected_subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter INTEGER,
    raw_subject TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    evidence TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chapter, raw_subject, evidence)
);

CREATE TABLE IF NOT EXISTS gen_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter INTEGER,
    conflict_type TEXT NOT NULL,
    subject TEXT,
    canon_value TEXT,
    generated_value TEXT,
    evidence TEXT,
    decision TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(chapter, conflict_type, subject, generated_value)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | pathlib.Path, readonly: bool = False) -> sqlite3.Connection:
    """打开生成层库。readonly=True 时不会创建文件。"""
    path = pathlib.Path(path)
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    return con


#: 对称关系在落库前统一方向，避免正反两条重复。
SYMMETRIC_RELS = frozenset({"ally", "enemy", "family", "sibling"})

_MIGRATIONS = (
    ("gen_edges", "direction_basis", "TEXT NOT NULL DEFAULT ''"),
    ("gen_edges", "trigger", "TEXT NOT NULL DEFAULT ''"),
    ("gen_edges", "confidence", "REAL NOT NULL DEFAULT 0.5"),
    ("gen_edges", "established_chapter", "INTEGER"),
    ("gen_edges", "established_evidence", "TEXT"),
    ("gen_edges", "confirm_method", "TEXT NOT NULL DEFAULT ''"),
    ("gen_edges", "valid_from", "INTEGER"),
    ("gen_edges", "valid_to", "INTEGER"),
    ("gen_edges", "status", "TEXT NOT NULL DEFAULT 'active'"),
    ("gen_events", "subject_status", "TEXT NOT NULL DEFAULT 'resolved'"),
    ("gen_events", "raw_subject", "TEXT"),
)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    for table, column, decl in _MIGRATIONS:
        if column not in _columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.commit()


def upsert_node(con: sqlite3.Connection, name: str, kind: str,
                attrs: dict | None, chapter: int | None,
                mentions: int = 1) -> None:
    """写入/累加一个生成层节点。attrs 先剥离受保护字段。"""
    now = _now()
    safe = strip_protected(attrs)
    row = con.execute("SELECT id, attrs, first_chapter FROM gen_nodes WHERE name = ?",
                      (name,)).fetchone()
    if row is None:
        con.execute(
            """INSERT INTO gen_nodes
               (name, kind, attrs, source, source_chapter, first_chapter,
                last_chapter, mentions, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, kind, json.dumps(safe, ensure_ascii=False), GENERATED,
             chapter, chapter, chapter, mentions, now, now),
        )
    else:
        old = json.loads(row["attrs"] or "{}")
        merged = {**old, **safe}
        con.execute(
            """UPDATE gen_nodes
                  SET kind = ?, attrs = ?, last_chapter = ?,
                      updated_at = ?
                WHERE name = ?""",
            (kind or "unknown", json.dumps(merged, ensure_ascii=False),
             chapter, now, name),
        )
    if chapter is not None:
        # 按章记一份提及数，mentions 由它汇总；这样重跑同一章可精确回退。
        con.execute(
            """INSERT INTO gen_node_mentions (name, chapter, mentions)
               VALUES (?, ?, ?)
               ON CONFLICT(name, chapter) DO UPDATE SET mentions = excluded.mentions""",
            (name, chapter, mentions),
        )
        con.execute(
            """UPDATE gen_nodes
                  SET mentions = (SELECT COALESCE(SUM(mentions), 0)
                                    FROM gen_node_mentions WHERE name = ?)
                WHERE name = ?""",
            (name, name),
        )


def upsert_edge(con: sqlite3.Connection, from_name: str, to_name: str,
                rel_type: str, strength: float, chapter: int | None,
                evidence: str = "", direction_basis: str = "",
                trigger: str = "", confidence: float = 0.5,
                confirm_method: str = "") -> None:
    """写入/更新一条生成层关系；章节列表累加，不丢历史。

    ``established_*`` 只在首次建边时写：后续复述、失效都不覆盖建边原句
    （失效证据另记在 ``gen_edge_invalidations``）。
    """
    if rel_type in SYMMETRIC_RELS:
        from_name, to_name = sorted((from_name, to_name))
    row = con.execute(
        """SELECT id, chapters, strength, confidence, evidence FROM gen_edges
            WHERE from_name = ? AND to_name = ? AND rel_type = ?""",
        (from_name, to_name, rel_type),
    ).fetchone()
    if row is None:
        con.execute(
            """INSERT INTO gen_edges
               (from_name, to_name, rel_type, strength, source, source_chapter,
                chapters, evidence, direction_basis, trigger, confidence,
                established_chapter, established_evidence, confirm_method,
                valid_from, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
            (from_name, to_name, rel_type, float(strength), GENERATED, chapter,
             json.dumps([chapter] if chapter is not None else [], ensure_ascii=False),
             evidence[:400], direction_basis, trigger, float(confidence),
             chapter, evidence[:400], confirm_method, chapter),
        )
        return
    chapters = json.loads(row["chapters"] or "[]")
    if chapter is not None and chapter not in chapters:
        chapters.append(chapter)
    con.execute(
        """UPDATE gen_edges
              SET strength = ?, chapters = ?, evidence = COALESCE(NULLIF(?, ''), evidence),
                  direction_basis = COALESCE(NULLIF(?, ''), direction_basis),
                  trigger = COALESCE(NULLIF(?, ''), trigger),
                  confidence = MAX(confidence, ?),
                  confirm_method = COALESCE(NULLIF(?, ''), confirm_method),
                  established_evidence = COALESCE(established_evidence, NULLIF(?, '')),
                  established_chapter = COALESCE(established_chapter, ?),
                  valid_from = COALESCE(valid_from, ?)
            WHERE id = ?""",
        (max(float(row["strength"] or 0.5), float(strength)),
         json.dumps(chapters, ensure_ascii=False), evidence[:400],
         direction_basis, trigger, float(confidence), confirm_method,
         evidence[:400], chapter, chapter, row["id"]),
    )


def _close_open_segment(con: sqlite3.Connection, table: str,
                        key: dict, chapter: int) -> int:
    """把 ``table`` 里 ``key`` 指定的开放段关闭在 ``chapter``（半开区间右端点）。

    有效段统一按 ``[valid_from, valid_to)`` 解释：只更新 ``valid_to IS NULL``
    的行，所以重复调用不会重复关闭，也不会改写已经关闭的历史段。
    ``key`` 的比较用 ``IS``，这样值为 ``NULL`` 的键（如未实体化的 item_id）
    也能正确匹配。表名与列名只来自本模块内部常量，不接收外部字符串。

    返回实际关闭的段数。
    """
    if not key:
        raise ValueError("_close_open_segment 需要非空 key")
    where = " AND ".join(f"{col} IS ?" for col in key)
    cur = con.execute(
        f"UPDATE {table} SET valid_to = ? WHERE {where} AND valid_to IS NULL",
        (chapter, *key.values()),
    )
    return cur.rowcount


def invalidate_edge(con: sqlite3.Connection, from_name: str, to_name: str,
                    rel_type: str, chapter: int, *, reason: str,
                    trigger: str = "", evidence: str = "",
                    status: str = "ended") -> bool:
    """关闭一条边并追加失效记录；建边证据保持不动。

    返回是否真的关掉了（边不存在或失效章早于建边章时不动）。
    """
    if rel_type in SYMMETRIC_RELS:
        from_name, to_name = sorted((from_name, to_name))
    row = con.execute(
        """SELECT id, valid_from, status FROM gen_edges
            WHERE from_name = ? AND to_name = ? AND rel_type = ?""",
        (from_name, to_name, rel_type),
    ).fetchone()
    if row is None:
        return False
    if chapter is not None and row["valid_from"] is not None and chapter < row["valid_from"]:
        return False
    con.execute(
        """INSERT OR IGNORE INTO gen_edge_invalidations
           (from_name, to_name, rel_type, chapter, reason, trigger, evidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (from_name, to_name, rel_type, chapter, reason, trigger,
         evidence[:400], _now()),
    )
    if row["status"] == "active":
        _close_open_segment(con, "gen_edges", {"id": row["id"]}, chapter)
        con.execute("UPDATE gen_edges SET status = ? WHERE id = ?",
                    (status, row["id"]))
    return True


def close_edges_by_endpoint(con: sqlite3.Connection, name: str, chapter: int,
                            *, reason: str = "death", trigger: str = "",
                            evidence: str = "") -> int:
    """角色死亡/退场：把它名下仍有效的边全部切成 historical。"""
    rows = _rows(con.execute(
        """SELECT from_name, to_name, rel_type FROM gen_edges
            WHERE (from_name = ? OR to_name = ?) AND status = 'active'""",
        (name, name),
    ))
    closed = 0
    for row in rows:
        if invalidate_edge(con, row["from_name"], row["to_name"], row["rel_type"],
                           chapter, reason=reason, trigger=trigger,
                           evidence=evidence, status="historical"):
            closed += 1
    return closed


#: 持有类型：own = 权属（谁的），custody = 保管（谁拿着）。两者可并行成两条时间线。
HOLD_TYPES = frozenset({"own", "custody"})
#: 持有者状态：held 有主；unowned 无主；unknown 未知；sealed 封印中。
OWNER_STATES = frozenset({"held", "unowned", "unknown", "sealed"})
#: 同章出现多个归属主张且无法用转交事件排序时的候选原因。
SAME_CHAPTER_CLASH = "same_chapter_clash"
EXTEND_BACK_OVERLAP = "extend_back_overlap"
#: 回溯段的坐标与主线旁注矛盾时的候选原因（只留证据，不裁决）。
STORY_ORDER_CONFLICT = "story_order_conflict"

#: 叙述关系：forward 顺序讲述 / retro 追述 / prefigure 预叙。
NARRATIONS = frozenset({"forward", "retro", "prefigure"})
#: 先后键的来源：explicit 正文看得出先后 / chapter 看不出，退到章节序。
ORDER_BASES = frozenset({"explicit", "chapter"})
#: 先后键的刻度：章节序 = chapter * 100，显式相对序用同一刻度插空。
ORDER_SCALE = 100


def _same_owner(row, owner_id: int | None, owner_name: str | None) -> bool:
    """同一持有者判定：有 id 比 id，没有 id 比规范名（都为空视为同一状态）。"""
    if owner_id is not None and row["owner_id"] is not None:
        return int(row["owner_id"]) == int(owner_id)
    return (row["owner_name"] or "").strip() == (owner_name or "").strip()


def _insert_holding(con: sqlite3.Connection, *, item_id: int | None, item_name: str,
                    hold_type: str, owner_id: int | None, owner_name: str | None,
                    owner_kind: str | None, owner_state: str, chapter: int,
                    transfer_event_id: int | None, reason_code: str,
                    evidence: str, confidence: float) -> int:
    cur = con.execute(
        """INSERT INTO kg_item_holding
           (item_id, item_name, hold_type, owner_id, owner_name, owner_kind,
            owner_state, valid_from, valid_to, transfer_event_id, reason_code,
            evidence_chapter, evidence, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
        (item_id, item_name, hold_type, owner_id, owner_name, owner_kind,
         owner_state, chapter, transfer_event_id, reason_code, chapter,
         (evidence or "")[:400], float(confidence), _now()),
    )
    return int(cur.lastrowid)


def record_holding(con: sqlite3.Connection, item_name: str, hold_type: str,
                   owner_name: str | None, chapter: int, *,
                   item_id: int | None = None,
                   owner_id: int | None = None, owner_kind: str | None = None,
                   owner_state: str = "held",
                   transfer_event_id: int | None = None,
                   event_object: str | None = None, reason_code: str = "",
                   evidence: str = "", confidence: float = 0.5) -> dict:
    """写入一段持有史（唯一写入口）。

    ``owner_state`` 决定 ``owner_name`` 的口径：
    - ``held``：``owner_id`` 与 ``owner_name`` 至少一个非空；「有主但主人未实体化」
      就是 ``owner_id`` 空 + ``owner_name`` 非空；
    - ``unowned`` / ``sealed`` / ``unknown``：``owner_name`` 必须为空，不写哨兵名。

    三条硬规则：
    1. **先关后插**：同一 (item, hold_type) 已有开放段时，先在 ``chapter``
       关闭它（``valid_to = chapter``，半开区间），再插入新的开放段；
       同一持有者、同一状态的复述不重开段，返回 ``confirmed``。
       但候选章**早于**开放段起点时不是复述，而是**向前补起点**
       （``extended_back``）；若前移后会盖住更早的已关段，则不写、
       落 ``extend_back_overlap`` 候选，并在 ``blocked_by`` 里说明撞的段。
    2. **同章竞争不写史**：同一章内对同一 (item, hold_type) 出现不同持有者时，
       有 ``transfer_event_id`` 且能对上事件对象（``event_object``）的按事件
       对象胜出；否则两条都只进 ``gen_candidates``，持有表不留任何零长度段。
    3. **拒绝零长度段与倒序段**：``chapter <=`` 当前开放段的 ``valid_from``
       时，除同章竞争外一律拒绝（``rejected``），不写库。

    返回 ``{"action": ..., "id": ..., "closed": ..., "reason": ...}``；
    ``action`` ∈ inserted / confirmed / replaced / candidate / rejected。
    """
    item_name = (item_name or "").strip()
    owner_name = (owner_name or "").strip() or None
    if not item_name:
        raise ValueError("item_name 不能为空")
    if hold_type not in HOLD_TYPES:
        raise ValueError(f"hold_type 只能是 {sorted(HOLD_TYPES)}")
    if owner_state not in OWNER_STATES:
        raise ValueError(f"owner_state 只能是 {sorted(OWNER_STATES)}")
    if owner_state == "held":
        if owner_id is None and owner_name is None:
            raise ValueError("held 态要求 owner_id 与 owner_name 至少一个非空")
    elif owner_name is not None:
        raise ValueError(f"{owner_state} 态不允许 owner_name（不许造假持有者）")
    if chapter is None:
        raise ValueError("chapter 不能为空")

    key = {"item_id": item_id, "item_name": item_name, "hold_type": hold_type}
    open_row = con.execute(
        """SELECT * FROM kg_item_holding
            WHERE item_id IS ? AND item_name = ? AND hold_type = ?
              AND valid_to IS NULL
            ORDER BY id DESC LIMIT 1""",
        (item_id, item_name, hold_type),
    ).fetchone()

    insert_kwargs = dict(
        item_id=item_id, item_name=item_name, hold_type=hold_type,
        owner_id=owner_id, owner_name=owner_name, owner_kind=owner_kind,
        owner_state=owner_state, chapter=chapter,
        transfer_event_id=transfer_event_id, reason_code=reason_code,
        evidence=evidence, confidence=confidence,
    )

    if open_row is None:
        rid = _insert_holding(con, **insert_kwargs)
        return {"action": "inserted", "id": rid, "closed": 0,
                "reason": "first_segment"}

    if _same_owner(open_row, owner_id, owner_name) \
            and open_row["owner_state"] == owner_state:
        if chapter < open_row["valid_from"]:
            return _extend_back(con, open_row, item_name, hold_type, chapter,
                                item_id=item_id, evidence=evidence,
                                reason_code=reason_code, confidence=confidence)
        return {"action": "confirmed", "id": open_row["id"], "closed": 0,
                "reason": "same_owner_same_state"}

    if chapter < open_row["valid_from"]:
        return {"action": "rejected", "id": open_row["id"], "closed": 0,
                "reason": "non_forward"}
    if chapter == open_row["valid_from"]:
        if _same_owner(open_row, owner_id, owner_name):
            # 同一持有者换状态压在同一章 = 零长度段，拒绝
            return {"action": "rejected", "id": open_row["id"], "closed": 0,
                    "reason": "zero_length"}
        return _resolve_same_chapter(
            con, open_row, item_name, hold_type, chapter,
            event_object=event_object, insert_kwargs=insert_kwargs)

    closed = _close_open_segment(con, "kg_item_holding", key, chapter)
    rid = _insert_holding(con, **insert_kwargs)
    return {"action": "inserted", "id": rid, "closed": closed,
            "reason": "transfer"}


def _extend_back(con: sqlite3.Connection, open_row, item_name: str,
                 hold_type: str, chapter: int, *, item_id: int | None,
                 evidence: str, reason_code: str, confidence: float) -> dict:
    """同主同态、候选章更早：把开放段起点前移，不新增段。

    前移前先查同 (item, hold_type) 更早的已关段：它的 ``valid_to`` 若大于
    新起点，说明前移会盖住它，此时不写库，只落 ``extend_back_overlap`` 候选。
    """
    prev = con.execute(
        """SELECT id, owner_name, valid_from, valid_to FROM kg_item_holding
            WHERE item_id IS ? AND item_name = ? AND hold_type = ?
              AND valid_to IS NOT NULL
              AND valid_to > ? AND valid_from < ?
            ORDER BY valid_to DESC, id DESC LIMIT 1""",
        (item_id, item_name, hold_type, chapter, open_row["valid_from"]),
    ).fetchone()
    if prev is not None:
        add_candidate(
            con, item_name, (open_row["owner_name"] or ""), hold_type, chapter,
            EXTEND_BACK_OVERLAP,
            evidence or (f"向前补起点 {chapter} 会盖住已关段 "
                         f"[{prev['valid_from']},{prev['valid_to']})"))
        return {"action": "conflict", "id": open_row["id"], "closed": 0,
                "reason": "extend_back_overlap",
                "blocked_by": {"id": prev["id"],
                               "owner_name": prev["owner_name"],
                               "valid_from": prev["valid_from"],
                               "valid_to": prev["valid_to"]}}

    old = int(open_row["valid_from"])
    con.execute(
        """UPDATE kg_item_holding
              SET valid_from = ?, evidence_chapter = ?, evidence = ?,
                  reason_code = CASE WHEN ? = '' THEN reason_code ELSE ? END,
                  confidence = ?
            WHERE id = ?""",
        (chapter, chapter, (evidence or open_row["evidence"])[:400],
         reason_code, reason_code, float(confidence), open_row["id"]),
    )
    return {"action": "extended_back", "id": open_row["id"], "closed": 0,
            "reason": "backfill_start", "from": old, "to": chapter}


def _resolve_same_chapter(con: sqlite3.Connection, open_row, item_name: str,
                          hold_type: str, chapter: int, *,
                          event_object: str | None,
                          insert_kwargs: dict) -> dict:
    """同章竞争：事件对象胜出，否则全部进候选（持有表不留零长度段）。"""
    prev_name = (open_row["owner_name"] or "").strip()
    prev_id = open_row["id"]
    candidate_owner = (insert_kwargs["owner_name"] or "").strip()
    evidence = (insert_kwargs.get("evidence") or "")[:400]

    if event_object and event_object.strip():
        winner = event_object.strip()
        if candidate_owner == winner:
            con.execute("DELETE FROM kg_item_holding WHERE id = ?", (prev_id,))
            rid = _insert_holding(con, **insert_kwargs)
            return {"action": "replaced", "id": rid, "closed": 0,
                    "reason": "event_object_wins"}
        if prev_name == winner:
            add_candidate(con, item_name, candidate_owner, hold_type, chapter,
                          SAME_CHAPTER_CLASH, evidence)
            return {"action": "candidate", "id": prev_id, "closed": 0,
                    "reason": "event_object_keeps_previous"}

    # 没有可用的转交事件：先写的零长度段撤掉，两条主张都只留候选
    con.execute("DELETE FROM kg_item_holding WHERE id = ?", (prev_id,))
    add_candidate(con, item_name, prev_name, hold_type, chapter,
                  SAME_CHAPTER_CLASH, open_row["evidence"] or "")
    add_candidate(con, item_name, candidate_owner, hold_type, chapter,
                  SAME_CHAPTER_CLASH, evidence)
    return {"action": "candidate", "id": None, "closed": 0, "candidates": 2,
            "reason": SAME_CHAPTER_CLASH}


def record_story_segment(con: sqlite3.Connection, item_name: str, hold_type: str,
                         owner_name: str | None, chapter: int, *,
                         item_id: int | None = None,
                         owner_id: int | None = None,
                         owner_kind: str | None = None,
                         owner_state: str = "held",
                         holding_id: int | None = None,
                         narration: str = "forward",
                         order_key: int | None = None,
                         order_basis: str | None = None,
                         anchor_label: str = "",
                         transfer_from_id: int | None = None,
                         transfer_from_name: str | None = None,
                         evidence: str = "",
                         confidence: float = 0.5) -> dict:
    """写一条故事先后轴旁注（``kg_holding_story_segment`` 的唯一写入口）。

    只排先后，不记绝对时间：

    - ``order_key`` 是同一物品内可比的整数，**越大越新**；``NULL`` 表示正文看不出先后，
      读侧退到章节序（``evidence_chapter``）。刻度约定 ``chapter * 100``，
      显式相对序用同一刻度插空（如「开国之初」= 0，介于 50 章与 51 章之间 = 5050）。
    - ``order_basis``：``explicit``（正文有先后）/ ``chapter``（没有，用章节序）。
    - 年号只作可选证据写在 ``anchor_label``，**缺了不补，不推断年份**。

    两种行，不允许别的方式写入：

    - **主线旁注**：``holding_id`` 指向 ``kg_item_holding.id``，``narration='forward'``；
      持有者、物品必须与对应持有行一致，否则拒绝（防两轴漂移）。
    - **回溯/预叙分支**：``holding_id`` 必须为空，不进章节轴，**不调用**
      ``_close_open_segment``，因此不可能覆盖主线段的 ``valid_to``。

    与主线旁注的先后矛盾时**只记** ``gen_candidates(reason='story_order_conflict')``；
    不拒写、不裁决——当前持有者由「最新先后键」决定，候选不作为裁决依据。

    返回 ``{"action": inserted|confirmed, "id": ..., "axis": "story",
    "holding_id": ..., "conflict": bool}``。
    """
    item_name = (item_name or "").strip()
    owner_name = (owner_name or "").strip() or None
    transfer_from_name = (transfer_from_name or "").strip() or None
    if not item_name:
        raise ValueError("item_name 不能为空")
    if hold_type not in HOLD_TYPES:
        raise ValueError(f"hold_type 只能是 {sorted(HOLD_TYPES)}")
    if owner_state not in OWNER_STATES:
        raise ValueError(f"owner_state 只能是 {sorted(OWNER_STATES)}")
    if narration not in NARRATIONS:
        raise ValueError(f"narration 只能是 {sorted(NARRATIONS)}")
    if owner_state == "held":
        if owner_id is None and owner_name is None:
            raise ValueError("held 态要求 owner_id 与 owner_name 至少一个非空")
    elif owner_name is not None:
        raise ValueError(f"{owner_state} 态不允许 owner_name（不许造假持有者）")
    if chapter is None:
        raise ValueError("chapter 不能为空")

    # 两档：有先后键（explicit）/ 只有叙述序（chapter，键为空）
    if order_basis is None:
        order_basis = "explicit" if order_key is not None else "chapter"
    if order_basis not in ORDER_BASES:
        raise ValueError(f"order_basis 只能是 {sorted(ORDER_BASES)}")
    if order_basis == "explicit" and order_key is None:
        raise ValueError("order_basis=explicit 必须给 order_key")
    if order_basis == "chapter" and order_key is not None:
        raise ValueError("order_basis=chapter 时 order_key 必须为空（退到章节序）")
    if order_key is not None and int(order_key) < 0:
        raise ValueError("order_key 不能为负")

    # 主线旁注必须真有主线行，且两轴口径一致
    if narration == "forward" and holding_id is None:
        raise ValueError("forward 旁注必须给 holding_id；回溯/预叙才写分支行")
    if holding_id is not None:
        row = con.execute("SELECT * FROM kg_item_holding WHERE id = ?",
                          (holding_id,)).fetchone()
        if row is None:
            raise ValueError(f"holding_id={holding_id} 不存在")
        if row["item_name"] != item_name or row["hold_type"] != hold_type:
            raise ValueError("旁注的 item_name/hold_type 与持有行不一致")
        if not _same_owner(row, owner_id, owner_name) or row["owner_state"] != owner_state:
            raise ValueError("旁注的持有者/状态与持有行不一致")

    conflict = False
    if narration != "forward" and order_key is not None:
        prev = con.execute(
            """SELECT MAX(COALESCE(s.order_key, h.valid_from * ?)) AS ord
                 FROM kg_item_holding h
                 LEFT JOIN kg_holding_story_segment s ON s.holding_id = h.id
                WHERE h.item_name = ? AND h.hold_type = ?""",
            (ORDER_SCALE, item_name, hold_type),
        ).fetchone()
        if prev is not None and prev["ord"] is not None and int(order_key) >= int(prev["ord"]):
            conflict = True
            add_candidate(con, item_name, owner_name or "", hold_type, chapter,
                          STORY_ORDER_CONFLICT,
                          evidence or f"回溯先后键 {order_key} 不早于主线最新 {prev['ord']}")

    try:
        cur = con.execute(
            """INSERT INTO kg_holding_story_segment
               (holding_id, item_id, item_name, hold_type, owner_id, owner_name,
                owner_kind, owner_state, order_key, order_basis, anchor_label,
                narration, transfer_from_id, transfer_from_name,
                evidence_chapter, evidence, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (holding_id, item_id, item_name, hold_type, owner_id, owner_name,
             owner_kind, owner_state, order_key, order_basis, anchor_label,
             narration, transfer_from_id, transfer_from_name,
             chapter, (evidence or "")[:400], float(confidence), _now()),
        )
        return {"action": "inserted", "id": int(cur.lastrowid), "axis": "story",
                "holding_id": holding_id, "conflict": conflict}
    except sqlite3.IntegrityError:
        # 同一主线行已有旁注 / 同一分支坐标已写过：幂等复述，不重复插
        if holding_id is not None:
            row = con.execute(
                "SELECT id FROM kg_holding_story_segment WHERE holding_id = ?",
                (holding_id,)).fetchone()
        else:
            row = con.execute(
                """SELECT id FROM kg_holding_story_segment
                    WHERE holding_id IS NULL AND item_name = ? AND hold_type = ?
                      AND COALESCE(item_id, -1) = COALESCE(?, -1)
                      AND COALESCE(order_key, -1) = COALESCE(?, -1)""",
                (item_name, hold_type, item_id, order_key),
            ).fetchone()
        return {"action": "confirmed", "id": int(row["id"]) if row else None,
                "axis": "story", "holding_id": holding_id, "conflict": conflict}


def item_story_timeline(con: sqlite3.Connection, item_name: str, *,
                        item_id: int | None = None) -> list[dict]:
    """某物品的故事先后轴：主线旁注 + 回溯/预叙分支，按先后键从早到晚。

    先后键 ``COALESCE(order_key, evidence_chapter * 100)``：有明确先后的按键排，
    看不出的退到章节序。这个键**只表示先后，不表示距离**。
    """
    sql = "SELECT * FROM kg_holding_story_segment WHERE item_name = ?"
    params: list = [item_name]
    if item_id is not None:
        sql += " AND item_id IS ?"
        params.append(item_id)
    rows = _rows(con.execute(sql, params))
    for row in rows:
        row["order_effective"] = _effective_order(row)
    rows.sort(key=lambda r: (r["order_effective"], r["evidence_chapter"], r["id"]))
    return rows


def _effective_order(row) -> int:
    """先后键的读取口径：显式键优先，缺了退到章节序（chapter*100）。"""
    if row["order_key"] is not None:
        return int(row["order_key"])
    return int(row["evidence_chapter"]) * ORDER_SCALE


def current_holder(con: sqlite3.Connection, item_name: str,
                   hold_type: str | None = None) -> list[dict]:
    """当前持有者：每一类持有线取**最新先后键**的那一段，不按置信度挑。

    只看先后，不看距离；段落已结束也照样参与（「最新一段」就是当前口径）。
    主线旁注（``holding_id`` 非空）不重复计入，它的先后键通过 JOIN 覆盖主线行。

    返回 ``[{"item_name", "hold_type", "owner_name", "owner_id", "owner_state",
    "chapter", "order_key", "narration"}]``，每个 ``hold_type`` 一条。
    """
    sql = """
        SELECT h.item_name AS item_name, h.hold_type AS hold_type,
               h.owner_name AS owner_name, h.owner_id AS owner_id,
               h.owner_state AS owner_state, h.valid_from AS chapter,
               COALESCE(s.order_key, h.valid_from * ?) AS ord,
               'forward' AS narration, h.id AS row_id
          FROM kg_item_holding h
          LEFT JOIN kg_holding_story_segment s ON s.holding_id = h.id
         WHERE h.item_name = ?
        UNION ALL
        SELECT s.item_name, s.hold_type, s.owner_name, s.owner_id,
               s.owner_state, s.evidence_chapter,
               COALESCE(s.order_key, s.evidence_chapter * ?),
               s.narration, s.id
          FROM kg_holding_story_segment s
         WHERE s.holding_id IS NULL AND s.item_name = ?
    """
    params: list = [ORDER_SCALE, item_name, ORDER_SCALE, item_name]
    if hold_type is not None:
        sql = f"SELECT * FROM ({sql}) WHERE hold_type = ?"
        params.append(hold_type)
    rows = _rows(con.execute(sql, params))
    best: dict[str, dict] = {}
    for row in rows:
        key = row["hold_type"]
        cur = best.get(key)
        if cur is None or (row["ord"], row["chapter"], row["row_id"]) > \
                (cur["ord"], cur["chapter"], cur["row_id"]):
            best[key] = row
    out = []
    for row in best.values():
        out.append({"item_name": row["item_name"], "hold_type": row["hold_type"],
                    "owner_name": row["owner_name"], "owner_id": row["owner_id"],
                    "owner_state": row["owner_state"], "chapter": row["chapter"],
                    "order_key": row["ord"], "narration": row["narration"]})
    out.sort(key=lambda r: r["hold_type"])
    return out


def item_holding_timeline(con: sqlite3.Connection, item_name: str, *,
                          item_id: int | None = None) -> list[dict]:
    """某物品的持有时间线（own / custody 各自成线，按段起点排序）。"""
    if item_id is None:
        rows = con.execute(
            """SELECT * FROM kg_item_holding WHERE item_name = ?
                ORDER BY hold_type, valid_from, id""",
            (item_name,),
        )
    else:
        rows = con.execute(
            """SELECT * FROM kg_item_holding WHERE item_name = ? AND item_id IS ?
                ORDER BY hold_type, valid_from, id""",
            (item_name, item_id),
        )
    return _rows(rows)


def holder_current_holdings(con: sqlite3.Connection, owner_name: str | None = None,
                            *, owner_id: int | None = None,
                            chapter: int | None = None) -> list[dict]:
    """某持有者当前持有什么。

    ``chapter=None``：只取开放段（``valid_to IS NULL``）；
    ``chapter=N``：取覆盖 N 的段（``valid_from <= N < valid_to`` 或 ``valid_to`` 为空）。

    只按 id / name 命中，**不加** ``owner_id IS NULL`` 过滤——第三态
    （``owner_id`` 空、``owner_name`` 非空）必须能被读到，不能被当成无主。
    """
    conds, params = [], []
    if owner_name is not None:
        conds.append("owner_name = ?")
        params.append(owner_name)
    if owner_id is not None:
        conds.append("owner_id = ?")
        params.append(owner_id)
    if not conds:
        raise ValueError("owner_name 与 owner_id 至少给一个")
    owner_sql = f"({' OR '.join(conds)})"
    if chapter is None:
        sql = (f"SELECT * FROM kg_item_holding WHERE {owner_sql} "
               "AND valid_to IS NULL")
    else:
        sql = (f"SELECT * FROM kg_item_holding WHERE {owner_sql} "
               "AND valid_from <= ? AND (valid_to IS NULL OR ? < valid_to)")
        params.extend([chapter, chapter])
    return _rows(con.execute(sql + " ORDER BY item_name, hold_type", params))


def add_event(con: sqlite3.Connection, chapter: int | None, event_type: str,
              summary: str, subject: str | None = None, object_: str | None = None,
              location: str | None = None, evidence: str = "",
              subject_status: str = "resolved",
              raw_subject: str | None = None) -> None:
    """追加一条生成层事件；同章同类型同摘要去重。"""
    dup = con.execute(
        """SELECT id FROM gen_events
            WHERE chapter = ? AND event_type = ? AND summary = ?
              AND COALESCE(subject, '') = COALESCE(?, '')
              AND COALESCE(raw_subject, '') = COALESCE(?, '')""",
        (chapter, event_type, summary, subject, raw_subject),
    ).fetchone()
    if dup is not None:
        return
    con.execute(
        """INSERT INTO gen_events
           (chapter, event_type, summary, subject, object, location, source, evidence,
            subject_status, raw_subject)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chapter, event_type, summary[:500], subject, object_, location,
         GENERATED, evidence[:400], subject_status, raw_subject),
    )


def add_candidate(con: sqlite3.Connection, from_name: str, to_name: str,
                  rel_type: str, chapter: int | None, reason: str,
                  evidence: str = "") -> None:
    """记录一条“共现但无触发依据”的候选边，不进入正式边。"""
    con.execute(
        """INSERT OR IGNORE INTO gen_candidates
           (from_name, to_name, rel_type, source, source_chapter, reason,
            evidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (from_name, to_name, rel_type, GENERATED, chapter, reason,
         evidence[:400], _now()),
    )


def add_rejected_subject(con: sqlite3.Connection, chapter: int | None,
                         raw_subject: str, reason: str,
                         evidence: str = "") -> None:
    """记录被挡掉的事件主语（通称/碎片），便于回看漏了什么。"""
    con.execute(
        """INSERT OR IGNORE INTO gen_rejected_subjects
           (chapter, raw_subject, reason, evidence, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (chapter, raw_subject, reason, evidence[:400], _now()),
    )


def reset_chapter(con: sqlite3.Connection, chapter: int) -> None:
    """重跑同一章前清掉该章的机械产物，保证计数可复算。

    只动 source='generated' 的 gen_* 表；模型路 gen_llm_* 不受影响。
    """
    con.execute("DELETE FROM gen_edges WHERE source_chapter = ? AND source = ?",
                (chapter, GENERATED))
    con.execute("DELETE FROM gen_events WHERE chapter = ? AND source = ?",
                (chapter, GENERATED))
    con.execute("DELETE FROM gen_candidates WHERE source_chapter = ?", (chapter,))
    con.execute("DELETE FROM gen_rejected_subjects WHERE chapter = ?", (chapter,))
    # 失效记录随章重算：本章推翻过的边要回到 active，否则重跑会把边永久关掉。
    con.execute("DELETE FROM gen_edge_invalidations WHERE chapter = ?", (chapter,))
    con.execute(
        "UPDATE gen_edges SET status = 'active', valid_to = NULL WHERE valid_to = ?",
        (chapter,),
    )
    # 仅清掉“首次出现就在本章”的节点；更早的节点保留历史。
    survivors = [
        r["name"] for r in con.execute(
            "SELECT name FROM gen_node_mentions WHERE chapter = ?", (chapter,))
    ]
    con.execute("DELETE FROM gen_node_mentions WHERE chapter = ?", (chapter,))
    for name in survivors:
        con.execute(
            """UPDATE gen_nodes
                  SET mentions = (SELECT COALESCE(SUM(mentions), 0)
                                    FROM gen_node_mentions WHERE name = ?)
                WHERE name = ?""",
            (name, name),
        )
    con.execute(
        "DELETE FROM gen_nodes WHERE source_chapter = ? AND first_chapter = ?",
        (chapter, chapter),
    )


def add_conflict(con: sqlite3.Connection, chapter: int | None, conflict_type: str,
                 subject: str, canon_value: str, generated_value: str,
                 evidence: str, decision: str, note: str = "") -> None:
    """记录一条冲突；同一章/类型/主体/生成值只记一次。"""
    con.execute(
        """INSERT OR IGNORE INTO gen_conflicts
           (chapter, conflict_type, subject, canon_value, generated_value,
            evidence, decision, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (chapter, conflict_type, subject, canon_value[:400],
         generated_value[:400], evidence[:400], decision, note[:400], _now()),
    )


def _rows(rows) -> list[dict]:
    return [dict(row) for row in rows]


def load_nodes(con: sqlite3.Connection) -> list[dict]:
    return _rows(con.execute("SELECT * FROM gen_nodes ORDER BY mentions DESC"))


def load_edges(con: sqlite3.Connection) -> list[dict]:
    return _rows(con.execute("SELECT * FROM gen_edges ORDER BY strength DESC"))


def load_invalidations(con: sqlite3.Connection) -> list[dict]:
    """失效记录：每条都是「哪一章、因为什么触发词、哪句原文」推翻了旧边。"""
    return _rows(con.execute("SELECT * FROM gen_edge_invalidations ORDER BY chapter, id"))


def load_candidates(con: sqlite3.Connection,
                    chapter: int | None = None) -> list[dict]:
    if chapter is None:
        rows = con.execute("SELECT * FROM gen_candidates ORDER BY id")
    else:
        rows = con.execute(
            "SELECT * FROM gen_candidates WHERE source_chapter = ? ORDER BY id",
            (chapter,),
        )
    return _rows(rows)


def load_rejected_subjects(con: sqlite3.Connection,
                           chapter: int | None = None) -> list[dict]:
    if chapter is None:
        rows = con.execute("SELECT * FROM gen_rejected_subjects ORDER BY id")
    else:
        rows = con.execute(
            "SELECT * FROM gen_rejected_subjects WHERE chapter = ? ORDER BY id",
            (chapter,),
        )
    return _rows(rows)


def load_events(con: sqlite3.Connection, before_chapter: int | None = None,
                limit: int = 8) -> list[dict]:
    if before_chapter is None:
        rows = con.execute(
            "SELECT * FROM gen_events ORDER BY chapter DESC, id DESC LIMIT ?",
            (limit,),
        )
    else:
        rows = con.execute(
            """SELECT * FROM gen_events
                WHERE chapter IS NULL OR chapter < ?
                ORDER BY chapter DESC, id DESC LIMIT ?""",
            (before_chapter, limit),
        )
    return _rows(rows)


def seeds_for_chapter(con: sqlite3.Connection, chapter: int,
                      limit: int = 12) -> list[str]:
    """上一章生成层里出现过的名字，用于下一章注入时保持连续。"""
    rows = con.execute(
        """SELECT name FROM gen_nodes
            WHERE last_chapter = ?
            ORDER BY mentions DESC, name LIMIT ?""",
        (chapter, limit),
    )
    return [r["name"] for r in rows]


def conflicts(con: sqlite3.Connection, chapter: int | None = None) -> list[dict]:
    if chapter is None:
        rows = con.execute("SELECT * FROM gen_conflicts ORDER BY id")
    else:
        rows = con.execute(
            "SELECT * FROM gen_conflicts WHERE chapter = ? ORDER BY id", (chapter,)
        )
    return _rows(rows)
