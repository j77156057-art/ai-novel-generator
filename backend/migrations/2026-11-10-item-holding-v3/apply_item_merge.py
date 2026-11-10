# -*- coding: utf-8 -*-
"""地书 / 玉石小镜 合并 + 黑金长刀折叠：一次性数据修正（真实库）。

核证结论（详见 reports/kg-item-holding.md）：许七安在 ch51 摊位「二选一」拿到
玉石小镜，ch73 才滴血认主；金莲师兄持有到 ch51 为止，不是 ch73。四项改动：

1. hold_type：玉石小镜三段 own → custody；own 只留「地书 → 天地会」一条。
2. 合并：规范名取「地书」，玉石小镜落别名；单块用 instance_id 区分（三号）。
3. 段冲突：删掉并行的 [74,∞)，金莲段改 [0,51)，其余首尾相接、不重叠。
4. 别名收敛：kg_entities 里玉石小镜只作地书别名；剔泛称；暗金色长刀→黑金长刀。

用法：
    python backend/migrations/2026-11-10-item-holding-v3/apply_item_merge.py --dry-run
    python backend/migrations/2026-11-10-item-holding-v3/apply_item_merge.py

一次性脚本：重复跑会在前置校验（EXPECT）处失败，那是「已改过」的信号，
不是可以忽略的报错。回滚见 reports/kg-item-holding.md 第 10.4 节。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
PROJ = HERE.parents[2]
GEN = PROJ / "backend" / "data" / "generated.db"
APP = PROJ / "backend" / "novel_dev.db"
BACKUP_DIR = PROJ / "backend" / "backups"

#: 改前必须逐项核对的 (item_name, hold_type, owner_name, valid_from, valid_to)
EXPECT = {
    12: ("地书", "custody", "金莲师兄", 73, 74, None),
    13: ("地书", "custody", "许七安", 74, None, None),
    14: ("地书", "own", "地宗", 73, 74, None),
    15: ("地书", "own", "天地会", 74, None, None),
    32: ("玉石小镜", "own", "许七安", 51, 105, None),
    33: ("玉石小镜", "own", "许新年", 105, 107, None),
    34: ("玉石小镜", "own", "许七安", 107, None, None),
    23: ("暗金色长刀", "own", "许七安", 93, None, None),
    58: ("黑金长刀", "own", "许七安", 102, None, None),
}

#: 地书最终别名（只留专属写法；泛称 镜子/镜面/这面镜子/破镜子/小镜 全剔）
DISHU_ALIASES = [
    "地书碎片", "地书聊天群", "法宝", "玉石小镜", "玉石镜",
    "三号碎片", "地书碎片·三号", "叁号镜子", "地宗法宝", "玉石碎片",
    "枕头底下的玉石小镜", "那面玉石小镜",
]
#: 黑金长刀最终别名（收 暗金色长刀/刀胚，剔 刀/长刀/刀子）
HEIJIN_ALIASES = ["佩刀", "修长战刀", "宝刀", "暗金色长刀", "刀胚"]

INSTANCE_LABEL = "三号"
INSTANCE_EVIDENCE_CHAPTER = 72
INSTANCE_EVIDENCE = ("叁代表的是我这面镜子的序号吗....对方是玖....他也有一面镜子？")

GENERIC_ALIASES = frozenset({"镜子", "镜面", "这面镜子", "破镜子", "小镜",
                             "刀", "长刀", "刀子"})


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def backup(source: pathlib.Path, tag: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"{tag}-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    src.backup(out)
    # 备份文件不留在 WAL 模式：否则容易只拷走 .db 而漏掉 -wal/-shm。
    out.execute("PRAGMA journal_mode=DELETE")
    out.close()
    src.close()
    chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    n = chk.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0] \
        if tag.startswith("generated") else -1
    chk.close()
    print(f"[备份] {dst}  integrity={ok}" + (f"  holding_rows={n}" if n >= 0 else ""))
    return dst


def rows_of(con: sqlite3.Connection) -> dict[int, dict]:
    con.row_factory = sqlite3.Row
    return {r["id"]: dict(r) for r in con.execute("SELECT * FROM kg_item_holding")}


def diff_rows(before: dict[int, dict], after: dict[int, dict]) -> None:
    print("\n== kg_item_holding 逐行 diff ==")
    for rid in sorted(set(before) | set(after)):
        if rid not in after:
            print(f"  - 删除 id={rid}: {before[rid]['item_name']} / "
                  f"{before[rid]['hold_type']} / {before[rid]['owner_name']} / "
                  f"[{before[rid]['valid_from']},{before[rid]['valid_to']})")
            continue
        if rid not in before:
            print(f"  + 新增 id={rid}: {after[rid]['item_name']} / "
                  f"{after[rid]['hold_type']} / {after[rid]['owner_name']} / "
                  f"[{after[rid]['valid_from']},{after[rid]['valid_to']})")
            continue
        changed = {k: (before[rid].get(k), after[rid].get(k))
                   for k in after[rid] if before[rid].get(k) != after[rid].get(k)}
        if changed:
            body = ", ".join(f"{k}: {a!r} → {b!r}" for k, (a, b) in changed.items())
            print(f"  ~ 修改 id={rid}: {body}")
    print(f"  行数: {len(before)} → {len(after)}")


def check_segments(con: sqlite3.Connection) -> None:
    """同一 (item_name, hold_type, instance_id) 必须首尾相接、无重叠、至多一条开放段。"""
    print("\n== 段清单（按 valid_from） ==")
    con.row_factory = sqlite3.Row
    q = ("SELECT id, item_name, hold_type, instance_id, owner_name, "
         "valid_from, valid_to FROM kg_item_holding "
         "WHERE item_name IN ('地书','黑金长刀','黑金刀','监正出品的长刀') "
         "ORDER BY item_name, hold_type, instance_id, valid_from")
    groups: dict[tuple, list[dict]] = {}
    for r in con.execute(q):
        groups.setdefault((r["item_name"], r["hold_type"], r["instance_id"]), []).append(dict(r))

    bad = 0
    for key, segs in groups.items():
        print(f"  {key}:")
        open_rows = [s for s in segs if s["valid_to"] is None]
        if len(open_rows) > 1:
            print(f"    !! 并行开放段 {len(open_rows)} 条: {[s['id'] for s in open_rows]}")
            bad += 1
        for i, s in enumerate(segs):
            print(f"    id={s['id']} [{s['valid_from']}, {s['valid_to']}) "
                  f"{s['owner_name']}")
            if i + 1 < len(segs):
                nxt = segs[i + 1]
                if s["valid_to"] != nxt["valid_from"]:
                    print(f"    !! 不连续/重叠: id={s['id']}.valid_to={s['valid_to']} "
                          f"≠ id={nxt['id']}.valid_from={nxt['valid_from']}")
                    bad += 1
    if bad:
        raise SystemExit(f"段校验失败：{bad} 处")
    print("  段校验：全部首尾相接，无并行开放段")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# generated.db
# ---------------------------------------------------------------------------
def apply_generated(con: sqlite3.Connection, *, apply: bool) -> None:
    before = rows_of(con)
    for rid, exp in EXPECT.items():
        r = before.get(rid)
        got = (r["item_name"], r["hold_type"], r["owner_name"],
               r["valid_from"], r["valid_to"], r["instance_id"]) if r else None
        if got != exp:
            raise SystemExit(f"前置校验失败 id={rid}: 期望 {exp}，实际 {got}")

    con.execute(
        "INSERT OR IGNORE INTO kg_item_instance"
        "(item_name, item_id, instance_label, evidence_chapter, evidence, created_at) "
        "VALUES (?,?,?,?,?,?)",
        ("地书", None, INSTANCE_LABEL, INSTANCE_EVIDENCE_CHAPTER,
         INSTANCE_EVIDENCE, now_iso()))
    inst = con.execute(
        "SELECT id FROM kg_item_instance WHERE item_name='地书' AND instance_label=?",
        (INSTANCE_LABEL,)).fetchone()[0]

    # 1+3：金莲段改 [0,51)；删并行的 [74,∞) 与地宗 own；三段改 custody/实例
    con.execute(
        "UPDATE kg_item_holding SET instance_id=?, valid_from=0, valid_to=51, "
        "evidence_chapter=51, evidence=? WHERE id=12",
        (inst, "许七安接过钱囊，又从老道那里取走玉石镜，目送着马车离开。"))
    con.execute("DELETE FROM kg_item_holding WHERE id IN (13, 14)")
    for rid in (32, 33, 34):
        con.execute(
            "UPDATE kg_item_holding SET item_name='地书', hold_type='custody', "
            "instance_id=? WHERE id=?", (inst, rid))

    # 4：暗金色长刀 → 黑金长刀（[93,102) 闭合，[102,∞) 由 id=58 接上）
    con.execute("UPDATE kg_item_holding SET item_name='黑金长刀', valid_to=102 "
                "WHERE id=23")

    check_segments(con)
    after = rows_of(con)
    diff_rows(before, after)
    if not apply:
        con.rollback()
        print("\n[dry-run] generated.db 已回滚")
    else:
        con.commit()


# ---------------------------------------------------------------------------
# novel_dev.db
# ---------------------------------------------------------------------------
def apply_entities(con: sqlite3.Connection, *, apply: bool) -> None:
    con.row_factory = sqlite3.Row
    ents = {r["id"]: dict(r) for r in
            con.execute("SELECT id, kind, canonical_name, aliases FROM kg_entities "
                        "WHERE kind='item' AND canonical_name IN "
                        "('地书','玉石小镜','暗金色长刀','黑金长刀')")}
    by_name = {v["canonical_name"]: k for k, v in ents.items()}
    for need in ("地书", "玉石小镜", "暗金色长刀", "黑金长刀"):
        if need not in by_name:
            raise SystemExit(f"前置校验失败：kg_entities 缺 item「{need}」")

    # 同名不得跨 kind 重复（否则别名收敛后仍会 MultipleResultsFound）
    dup = list(con.execute(
        "SELECT kind, COUNT(*) FROM kg_entities WHERE project_id=5 AND canonical_name IN "
        "('地书','玉石小镜','暗金色长刀') GROUP BY kind"))
    print("\n== kg_entities 同名前检 ==", [(r[0], r[1]) for r in dup])

    for name, rid in by_name.items():
        print(f"  改前 id={rid} {name}: {json.loads(ents[rid]['aliases'] or '[]')}")

    con.execute("UPDATE kg_entities SET aliases=? WHERE id=?",
                (json.dumps(DISHU_ALIASES, ensure_ascii=False), by_name["地书"]))
    con.execute("UPDATE kg_entities SET aliases=? WHERE id=?",
                (json.dumps(HEIJIN_ALIASES, ensure_ascii=False), by_name["黑金长刀"]))
    con.execute("DELETE FROM kg_entities WHERE id=?", (by_name["玉石小镜"],))
    con.execute("DELETE FROM kg_entities WHERE id=?", (by_name["暗金色长刀"],))

    # 收敛后自检：玉石小镜/暗金色长刀 不再是规范名；地书别名里没有泛称
    left = list(con.execute(
        "SELECT canonical_name FROM kg_entities WHERE project_id=5 AND canonical_name IN "
        "('玉石小镜','暗金色长刀')"))
    if left:
        raise SystemExit(f"收敛失败，仍存在规范名: {[r[0] for r in left]}")
    aliases = json.loads(con.execute(
        "SELECT aliases FROM kg_entities WHERE id=?", (by_name["地书"],)).fetchone()[0])
    bad = sorted(set(aliases) & GENERIC_ALIASES)
    if bad:
        raise SystemExit(f"地书别名仍含泛称: {bad}")
    print(f"  改后 地书: {aliases}")
    print(f"  改后 黑金长刀: {HEIJIN_ALIASES}")
    print(f"  删除规范名: 玉石小镜(id={by_name['玉石小镜']}), "
          f"暗金色长刀(id={by_name['暗金色长刀']})")

    if apply:
        con.commit()
    else:
        con.rollback()
        print("[dry-run] novel_dev.db 已回滚")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply = not args.dry_run

    print("=" * 74)
    print("generated.db:", GEN)
    if apply:
        backup(GEN, "generated-merge")
    else:
        tmp = pathlib.Path(tempfile.gettempdir()) / "generated_merge_dry.db"
        if tmp.exists():
            tmp.unlink()
        src = sqlite3.connect(f"file:{GEN}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp))
        src.backup(dst)
        dst.close()
        src.close()
        print("[dry-run] 副本:", tmp)
    path = GEN if apply else tmp
    con = sqlite3.connect(str(path))
    apply_generated(con, apply=apply)
    con.close()

    print("\n" + "=" * 74)
    print("novel_dev.db:", APP)
    if apply:
        backup(APP, "novel_dev-merge")
    else:
        tmp2 = pathlib.Path(tempfile.gettempdir()) / "novel_dev_merge_dry.db"
        if tmp2.exists():
            tmp2.unlink()
        src = sqlite3.connect(f"file:{APP}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp2))
        src.backup(dst)
        dst.close()
        src.close()
        print("[dry-run] 副本:", tmp2)
    con2 = sqlite3.connect(str(APP if apply else tmp2))
    apply_entities(con2, apply=apply)
    con2.close()

    print("\n完成:", "已写入真实库" if apply else "dry-run，未写真实库")


if __name__ == "__main__":
    sys.exit(main())
