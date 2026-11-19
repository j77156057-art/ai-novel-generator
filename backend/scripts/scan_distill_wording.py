"""只读扫描：找出 assistant_messages / wizard_notices 里仍带「蒸馏」的展示文案。

用途：确认历史气泡回写的命中范围（不动数据）。实际回写见
`backfill_distill_wording.py`。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "novel_dev.db"
LIKE = "%蒸馏%"


def main() -> int:
    if not DB.exists():
        print(f"数据库不存在：{DB}")
        return 1
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, project_id, role, kind, text, payload "
            "FROM assistant_messages "
            "WHERE text LIKE ? OR payload LIKE ? ORDER BY id",
            (LIKE, LIKE),
        ).fetchall()
        print(f"assistant_messages 命中 {len(rows)} 条")
        for row in rows[:20]:
            _id, pid, role, kind, text, payload = row
            print(f"  #{_id} p={pid} {role}/{kind} text={text[:80]!r}")
            if payload and "蒸馏" in payload:
                print(f"    payload={payload[:160]}")
        notices = con.execute(
            "SELECT id, project_id, kind, payload FROM wizard_notices "
            "WHERE payload LIKE ? ORDER BY id",
            (LIKE,),
        ).fetchall()
        print(f"wizard_notices 命中 {len(notices)} 条")
        for row in notices[:10]:
            print(f"  #{row[0]} p={row[1]} {row[2]} payload={row[3][:160]}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
