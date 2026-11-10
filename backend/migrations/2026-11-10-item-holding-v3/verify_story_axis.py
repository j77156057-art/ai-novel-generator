# -*- coding: utf-8 -*-
"""故事先后轴小样验证：只在 generated.db 的临时副本上写。

真实库只读；跑完对比前后行数与内容哈希，证明 58 行没动。
用法：backend\\venv\\Scripts\\python.exe backend\\migrations\\2026-11-10-item-holding-v3\\verify_story_axis.py
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
sys.path.insert(0, str(PROJ / "backend"))

from app.kg import generated_store as store  # noqa: E402

REAL = PROJ / "backend" / "data" / "generated.db"
SQL = HERE / "002_story_time_axis.sql"


def dump(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def content_hash(con):
    rows = dump(con, "SELECT * FROM kg_item_holding ORDER BY id")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def real_snapshot():
    con = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {
            "rows": con.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0],
            "hash": content_hash(con),
            "tables": [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kg_%'"
                " ORDER BY name")],
            "ddl": con.execute(
                "SELECT sql FROM sqlite_master WHERE name='kg_item_holding'"
            ).fetchone()[0],
        }
    finally:
        con.close()


def copy_db() -> pathlib.Path:
    tmp = pathlib.Path(tempfile.gettempdir()) / "kg_story_axis_copy.db"
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(tmp) + suffix)
        if p.exists():
            p.unlink()
    src = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    dst.close()
    src.close()
    return tmp


def main():
    before = real_snapshot()
    print("== 真实库（写入前） ==")
    print("kg_item_holding 行数:", before["rows"])
    print("内容哈希:", before["hash"][:16])
    print("kg_* 表:", before["tables"])

    tmp = copy_db()
    print("\n== 临时副本 ==")
    print(tmp)
    con = store.connect(tmp)
    con.executescript(SQL.read_text(encoding="utf-8"))
    con.executescript(SQL.read_text(encoding="utf-8"))  # 幂等

    print("\n== 副本 sqlite_master ==")
    for r in con.execute(
        """SELECT type, name, sql FROM sqlite_master
            WHERE name = 'kg_item_holding' OR name LIKE '%story_segment%'
            ORDER BY type, name"""):
        print(f"-- [{r['type']}] {r['name']}")
        print(r["sql"])

    print("\n老表 DDL 与真实库逐字一致:", 
          con.execute("SELECT sql FROM sqlite_master WHERE name='kg_item_holding'"
                      ).fetchone()[0] == before["ddl"])
    print("老表列:", [r[1] for r in con.execute("PRAGMA table_info(kg_item_holding)")])

    shu_before = dump(con, "SELECT * FROM kg_item_holding WHERE item_name LIKE '%地书%'"
                           " ORDER BY hold_type, valid_from")
    print("\n== 地书（写入前） ==")
    for r in shu_before:
        print(r)

    print("\n== 神剑（写入前） ==")
    for r in dump(con, "SELECT * FROM kg_item_holding WHERE item_name='神剑'"):
        print(r)
    print("— 空表示 58 行里没有神剑主线段 —")

    # --- 小样写入：只落在副本 ---
    print("\n== 小样写入 ==")
    # ch95 追述：开国皇帝得剑，早于故事开端 → 先后键 0
    r1 = store.record_story_segment(
        con, "神剑", "own", "大奉开国皇帝", 95,
        narration="retro", order_key=0, anchor_label="开国之初",
        evidence="玄武奉上神剑……大奉开国皇帝得了神剑")
    print("ch95 retro:", r1)

    # ch99 赠予：章节轴取受赠方，赠予方进 transfer_from_name
    r2 = store.record_holding(con, "神剑", "own", "镇北王", 99,
                              reason_code="gift", evidence="元景帝请出神剑，赠予镇北王")
    print("ch99 章节轴:", r2)
    r3 = store.record_story_segment(
        con, "神剑", "own", "镇北王", 99, holding_id=r2["id"],
        narration="forward", transfer_from_name="元景帝",
        evidence="元景帝请出神剑，赠予镇北王")
    print("ch99 旁注:", r3)

    # ch97 庙供奉：located_at 不许变持有 —— 这一条不写任何行，只做断言
    print("ch97 不写行（located_at ≠ 持有），当前神剑行数:",
          con.execute("SELECT COUNT(*) FROM kg_item_holding WHERE item_name='神剑'"
                      ).fetchone()[0])

    print("\n== 神剑（写入后） ==")
    print("-- 章节轴 --")
    for r in dump(con, "SELECT id, item_name, hold_type, owner_name, owner_state, "
                       "valid_from, valid_to, transfer_event_id FROM kg_item_holding "
                       "WHERE item_name='神剑' ORDER BY valid_from"):
        print(r)
    print("-- 先后轴 --")
    for r in store.item_story_timeline(con, "神剑"):
        print({k: r[k] for k in ("id", "holding_id", "owner_name", "order_key",
                                 "order_basis", "anchor_label", "narration",
                                 "transfer_from_name", "evidence_chapter")})

    print("\n== 当前持有者 ==")
    for item in ("神剑", "地书"):
        print(item, "→", store.current_holder(con, item))

    print("\n== 地书回归 ==")
    shu_after = dump(con, "SELECT * FROM kg_item_holding WHERE item_name LIKE '%地书%'"
                          " ORDER BY hold_type, valid_from")
    ok = shu_before == shu_after
    print("逐字段一致:", ok)
    if not ok:
        for a, b in zip(shu_before, shu_after):
            for k in a:
                if a[k] != b[k]:
                    print("  差异:", a.get("id"), k, a[k], "→", b[k])
    for r in shu_after:
        print(r)

    print("\n== 真实库（写入后） ==")
    after = real_snapshot()
    print("kg_item_holding 行数:", after["rows"])
    print("内容哈希:", after["hash"][:16])
    print("kg_* 表:", after["tables"])
    print("行数未变:", before["rows"] == after["rows"] == 58)
    print("内容未变:", before["hash"] == after["hash"])
    print("真实库仍无旁注表:", "kg_holding_story_segment" not in after["tables"])
    con.close()


if __name__ == "__main__":
    main()
