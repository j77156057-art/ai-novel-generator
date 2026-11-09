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
    UNIQUE(from_name, to_name, rel_type)
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
                trigger: str = "", confidence: float = 0.5) -> None:
    """写入/更新一条生成层关系；章节列表累加，不丢历史。"""
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
                chapters, evidence, direction_basis, trigger, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (from_name, to_name, rel_type, float(strength), GENERATED, chapter,
             json.dumps([chapter] if chapter is not None else [], ensure_ascii=False),
             evidence[:400], direction_basis, trigger, float(confidence)),
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
                  confidence = MAX(confidence, ?)
            WHERE id = ?""",
        (max(float(row["strength"] or 0.5), float(strength)),
         json.dumps(chapters, ensure_ascii=False), evidence[:400],
         direction_basis, trigger, float(confidence), row["id"]),
    )


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
