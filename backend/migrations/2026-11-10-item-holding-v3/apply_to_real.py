# -*- coding: utf-8 -*-
"""把 DDL 与本轮补的段落到真实 generated.db。

顺序：先备份库文件 → 校验备份 → 落 DDL → 走两个唯一入口写段 → 前后对照。
真实库只在这一步被写；副本与备份都不入库。

用法：backend\\venv\\Scripts\\python.exe backend\\migrations\\2026-11-10-item-holding-v3\\apply_to_real.py
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sqlite3
import sys
from datetime import datetime

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
sys.path.insert(0, str(PROJ / "backend"))

from app.kg import generated_store as store  # noqa: E402

REAL = PROJ / "backend" / "data" / "generated.db"
BACKUP_DIR = pathlib.Path("D:/Temp")

#: 落库前那 58 行的原始列（不含本轮新增的 instance_id）
ORIG_COLS = ("id", "item_id", "item_name", "hold_type", "owner_id", "owner_name",
             "owner_kind", "owner_state", "valid_from", "valid_to",
             "transfer_event_id", "reason_code", "evidence_chapter", "evidence",
             "confidence", "created_at")


def rows(con, cols, where="", params=()):
    return [dict(r) for r in con.execute(
        f"SELECT {', '.join(cols)} FROM kg_item_holding {where} ORDER BY id", params)]


def digest(rows_) -> str:
    return hashlib.sha256(json.dumps(rows_, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()[:16]


def tables(con):
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'kg_%' "
        "ORDER BY name")]


def backup(source: pathlib.Path) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"generated-backup-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    src.backup(out)
    out.close()
    src.close()
    chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    cnt = chk.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0]
    chk.close()
    print(f"备份: {dst}  integrity={ok}  rows={cnt}")
    return dst


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REAL), help="目标库；先用副本试跑再动真库")
    args = ap.parse_args()
    con = store.connect(pathlib.Path(args.db))
    before = rows(con, ORIG_COLS)
    print("落库前:", len(before), "行, hash", digest(before), "表", tables(con))

    bak = backup(pathlib.Path(args.db))

    con.executescript((HERE / "002_story_time_axis.sql").read_text(encoding="utf-8"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(kg_item_holding)")}
    if "instance_id" not in cols:
        con.executescript((HERE / "003_item_instance.sql").read_text(encoding="utf-8"))
    store.ensure_schema(con)
    print("DDL 落完, 表", tables(con))

    # --- 神剑：追述 → 赠予 → 转手 ---
    s95 = store.record_story_segment(
        con, "神剑", "own", "大奉开国皇帝", 95, narration="retro",
        order_key=0, anchor_label="开国之初",
        evidence="湖心高台那座庙里，供奉着开国皇帝当年使用过的佩剑")
    s99 = store.record_holding(con, "神剑", "own", "镇北王", 99,
                               reason_code="gift",
                               evidence="当年元景帝请出神剑，赠予镇北王的往事")
    n99 = store.record_story_segment(
        con, "神剑", "own", "镇北王", 99, holding_id=s99["id"],
        transfer_from_name="元景帝",
        evidence="当年元景帝请出神剑，赠予镇北王的往事")
    s106 = store.record_holding(
        con, "神剑", "own", "元景帝", 106, reason_code="transfer",
        evidence="一柄三尺长的铜剑破水而出，飞入元景帝手中")
    n106 = store.record_story_segment(
        con, "神剑", "own", "元景帝", 106, holding_id=s106["id"],
        evidence="确认神剑完好无损的元景帝松了口气")
    print("神剑:", s95, s99, n99, s106, n106)

    # --- 其余可补 ---
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
    # --- 金牌：同主更早，向前补起点 ---
    b5 = store.record_holding(con, "金牌", "own", "许七安", 108,
                              reason_code="gift",
                              evidence="陛下赐下了一面金牌，可在皇城行走")
    for label, r in (("朴刀 ch10", b1), ("朴刀 ch80 追述", b2),
                     ("鹿皮小包 ch2", b3), ("酒壶 ch113", b4), ("金牌 ch108", b5)):
        print(f"{label}:", r)
    con.commit()

    after = rows(con, ORIG_COLS, "WHERE id <= 58")
    print("\n落库后（原 58 行，不含新列）:", len(after), "行, hash", digest(after))
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    print("原 58 行改动条数:", len(changed))
    for b, a in changed:
        diff = {k: (b[k], a[k]) for k in b if b[k] != a[k]}
        print("  改动:", b["id"], b["item_name"], diff)

    print("\nkg_* 表:", tables(con))
    print("开放段索引:",
          con.execute("SELECT sql FROM sqlite_master WHERE name='ux_item_holding_open'")
          .fetchone()[0].replace("\n", " "))
    print("分支索引:",
          con.execute("SELECT sql FROM sqlite_master WHERE name='ux_story_rule' "
                      "OR name='ux_story_segment_branch'").fetchone()[0].replace("\n", " "))

    print("\n地书 4 行:")
    shu_after = rows(con, ORIG_COLS, "WHERE item_name LIKE '%地书%'")
    for r in shu_after:
        print("  ", r)
    shu_before = [r for r in before if "地书" in r["item_name"]]
    print("地书与落库前逐字段一致:", shu_before == shu_after)

    print("\n神剑当前持有者:", store.current_holder(con, "神剑"))
    print("神剑章节轴:", rows(con, ("id", "item_name", "hold_type", "owner_name",
                                    "valid_from", "valid_to"),
                              "WHERE item_name='神剑'"))
    print("碎片实例行（应为 0）:",
          con.execute("SELECT COUNT(*) FROM kg_item_instance").fetchone()[0])

    total = con.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0]
    print("\n总行数:", len(before), "→", total)
    con.close()

    print(f"\n回滚：停服后把 {bak} 复制回 {REAL}，"
          f"或 sqlite3 执行 .restore '{bak}'。回滚前请先删掉 -wal / -shm。")


if __name__ == "__main__":
    main()
