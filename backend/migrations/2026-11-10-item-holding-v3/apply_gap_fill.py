# -*- coding: utf-8 -*-
"""补已知缺口：只在 generated.db 的临时副本上写，真实库只读。

分级依据见报告；这里只落「可补」的段，走 record_holding / record_story_segment
两个唯一入口，不直接 INSERT。

用法：backend\\venv\\Scripts\\python.exe backend\\migrations\\2026-11-10-item-holding-v3\\apply_gap_fill.py
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


def content_hash(con, table="kg_item_holding"):
    rows = dump(con, f"SELECT * FROM {table} ORDER BY id")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def snapshot():
    con = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return {"rows": con.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0],
                "hash": content_hash(con),
                "tables": [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kg_%'"
                    " ORDER BY name")]}
    finally:
        con.close()


def copy_db() -> pathlib.Path:
    tmp = pathlib.Path(tempfile.gettempdir()) / "kg_gap_fill_copy.db"
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


def show(con, item):
    print(f"  {item} current_holder →", store.current_holder(con, item))


def main():
    before = snapshot()
    print("== 真实库（写入前）行数/哈希/表 ==")
    print(before["rows"], before["hash"][:16], before["tables"])

    tmp = copy_db()
    print("\n== 临时副本 ==", tmp)
    con = store.connect(tmp)
    con.executescript(SQL.read_text(encoding="utf-8"))
    con.executescript(SQL.read_text(encoding="utf-8"))

    print("\n== 神剑：补 ch106 前 ==")
    show(con, "神剑")

    # ch95 追述：开国皇帝得剑，早于故事开端
    r95 = store.record_story_segment(
        con, "神剑", "own", "大奉开国皇帝", 95, narration="retro",
        order_key=0, anchor_label="开国之初",
        evidence="湖心高台那座庙里，供奉着开国皇帝当年使用过的佩剑")
    # ch99 赠予：持有段取受赠方，赠予方进 transfer_from_name
    r99 = store.record_holding(con, "神剑", "own", "镇北王", 99,
                               reason_code="gift",
                               evidence="当年元景帝请出神剑，赠予镇北王的往事")
    n99 = store.record_story_segment(
        con, "神剑", "own", "镇北王", 99, holding_id=r99["id"],
        transfer_from_name="元景帝",
        evidence="当年元景帝请出神剑，赠予镇北王的往事")
    # ch97 只有供奉，located_at 不转持有 —— 不写
    print("  ch95:", r95)
    print("  ch99 章节轴:", r99)
    print("  ch99 旁注:", n99)
    print("  ch97 供奉：不写行（located_at ≠ 持有）")

    print("\n== 补 ch106 前（已写 ch95/ch99） ==")
    show(con, "神剑")

    # ch106：神剑破水飞入元景帝手中 —— 晚于 ch99，关掉镇北王段
    r106 = store.record_holding(
        con, "神剑", "own", "元景帝", 106, reason_code="transfer",
        evidence="一柄三尺长的铜剑破水而出，飞入元景帝手中")
    n106 = store.record_story_segment(
        con, "神剑", "own", "元景帝", 106, holding_id=r106["id"],
        evidence="确认神剑完好无损的元景帝松了口气")
    print("  ch106 章节轴:", r106)
    print("  ch106 旁注:", n106)

    print("\n== 补 ch106 后 ==")
    show(con, "神剑")
    print("  神剑章节轴:")
    for r in dump(con, "SELECT id, item_name, hold_type, owner_name, owner_state, "
                       "valid_from, valid_to FROM kg_item_holding WHERE item_name='神剑' "
                       "ORDER BY valid_from"):
        print("   ", r)
    print("  神剑先后轴:")
    for r in store.item_story_timeline(con, "神剑"):
        print("   ", {k: r[k] for k in ("id", "holding_id", "owner_name", "order_key",
                                       "order_basis", "anchor_label", "narration",
                                       "transfer_from_name", "evidence_chapter")})

    # --- 其余可补 ---
    print("\n== 其余可补 ==")
    b1 = store.record_holding(con, "朴刀", "custody", "许七安", 10,
                              reason_code="explicit_hold",
                              evidence="系好腰带，束好长发，再把朴刀挂在腰间")
    b2 = store.record_story_segment(
        con, "朴刀", "custody", "许七安", 80, narration="retro", order_key=0,
        anchor_label="胥吏时期",
        evidence="他还是胥吏的时候，就佩着朴刀，随身携带了多年")
    b3 = store.record_holding(con, "鹿皮小包", "custody", "褚采薇", 2,
                              reason_code="explicit_hold",
                              evidence="她……腰间挂着鹿皮小包以及一块八卦风水盘")
    b4 = store.record_holding(con, "酒壶", "custody", "许七安", 113,
                              reason_code="explicit_hold",
                              evidence="许七安低声骂了一句，从厨娘那里接过酒壶")
    for label, r in (("朴刀 ch10", b1), ("朴刀 ch80 追述", b2),
                     ("鹿皮小包 ch2", b3), ("酒壶 ch113", b4)):
        print(f"  {label}:", r)

    print("\n== 金牌：可补但被倒序保护挡住（只演示，不写） ==")
    blocked = store.record_holding(
        con, "金牌", "own", "许七安", 108, reason_code="gift",
        evidence="陛下赐下了一面金牌，可在皇城行走")
    print("  ch108 赐金牌 →", blocked,
          "；现有段:", dump(con, "SELECT id, owner_name, valid_from, valid_to "
                                 "FROM kg_item_holding WHERE item_name='金牌'"))

    print("\n== 地书回归 ==")
    shu = dump(con, "SELECT * FROM kg_item_holding WHERE item_name LIKE '%地书%' ORDER BY id")
    real = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    real.row_factory = sqlite3.Row
    shu_real = [dict(r) for r in real.execute(
        "SELECT * FROM kg_item_holding WHERE item_name LIKE '%地书%' ORDER BY id")]
    real.close()
    print("  逐字段一致:", shu == shu_real)

    print("\n== 新增行汇总（副本） ==")
    for r in dump(con, "SELECT id, item_name, hold_type, owner_name, valid_from, valid_to "
                       "FROM kg_item_holding WHERE item_name IN ('神剑','朴刀','鹿皮小包','酒壶') "
                       "ORDER BY item_name, hold_type, valid_from"):
        print("  ", r)
    for r in dump(con, "SELECT id, holding_id, item_name, hold_type, owner_name, order_key, "
                       "order_basis, anchor_label, narration, transfer_from_name, evidence_chapter "
                       "FROM kg_holding_story_segment ORDER BY id"):
        print("  ", r)

    print("\n== 真实库（写入后） ==")
    after = snapshot()
    print(after["rows"], after["hash"][:16], after["tables"])
    print("行数未变:", before["rows"] == after["rows"] == 58)
    print("内容未变:", before["hash"] == after["hash"])
    print("仍无旁注表:", "kg_holding_story_segment" not in after["tables"])
    con.close()


if __name__ == "__main__":
    main()
