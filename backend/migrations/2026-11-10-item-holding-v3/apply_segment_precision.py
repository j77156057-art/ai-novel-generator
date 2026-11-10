# -*- coding: utf-8 -*-
"""段精度批次：黑金刀并行段 / 地书 G2 魏渊 / G3 起点 107→108。

一次性数据修正，只动这三项涉及的行。改前自动备份 generated.db 与 novel_dev.db。

1. 黑金刀（id=57, [94,∞)）与黑金长刀（id=58, [102,∞)）并行：
   同一把刀同一主人，[94,∞) 完全被 [93,102)+[102,∞) 覆盖，删 id=57 即合并；
   kg_entities 里「黑金刀」(id=47) 收作「黑金长刀」(id=48) 的别名后删除。
2. G2：id=32 由 [51,105) 闭合到 74，拆出 魏渊 [74,76) 与 许七安 [76,105)。
   依据：ch74「他没带走镜子，径直离开茶室」；ch75 杨砚夺回；
   ch76「杨砚挥了挥手，镜子隔空飞到许七安面前……许七安接过镜子，收入怀中」。
3. G3：id=33 由 [105,107) 延到 108，id=34 起点 107→108。
   依据：ch107 是「被告知玉石小镜被堂弟取走」（不持有的证据），
   ch108 才「在书房里找到了自己的玉石小镜」。接成 [105,108) → [108,∞)，不留缝。

用法：
    python backend/migrations/2026-11-10-item-holding-v3/apply_segment_precision.py --dry-run
    python backend/migrations/2026-11-10-item-holding-v3/apply_segment_precision.py

一次性脚本：重复跑会在 EXPECT 处失败，那是「已改过」的信号。
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

#: 改前必须逐项核对：(item_name, hold_type, owner_name, valid_from, valid_to, instance_id)
EXPECT = {
    32: ("地书", "custody", "许七安", 51, 105, 1),
    33: ("地书", "custody", "许新年", 105, 107, 1),
    34: ("地书", "custody", "许七安", 107, None, 1),
    57: ("黑金刀", "own", "许七安", 94, None, None),
    23: ("黑金长刀", "own", "许七安", 93, 102, None),
    58: ("黑金长刀", "own", "许七安", 102, None, None),
}

WEI_EVIDENCE = "他没带走镜子，径直离开茶室。"
XU_BACK_EVIDENCE = ("杨砚挥了挥手，镜子隔空飞到许七安面前，悬停不动。"
                    "许七安接过镜子，收入怀中")
XU_108_EVIDENCE = "在书房里找到了自己的玉石小镜，许七安收入怀中"

#: 黑金长刀最终别名（原 5 条 + 黑金刀 + 描述式指代；不搬「口刀」这条脏别名）
HEIJIN_ALIASES = ["佩刀", "修长战刀", "宝刀", "暗金色长刀", "刀胚",
                  "黑金刀", "许七安的佩刀"]


# ---------------------------------------------------------------------------
def backup(source: pathlib.Path, tag: str) -> pathlib.Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"{tag}-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    out = sqlite3.connect(str(dst))
    src.backup(out)
    out.execute("PRAGMA journal_mode=DELETE")
    out.close()
    src.close()
    chk = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    ok = chk.execute("PRAGMA integrity_check").fetchone()[0]
    n = (chk.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0]
         if tag.startswith("generated") else -1)
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
            r = before[rid]
            print(f"  - 删除 id={rid}: {r['item_name']} / {r['hold_type']} / "
                  f"{r['owner_name']} / [{r['valid_from']},{r['valid_to']})")
            continue
        if rid not in before:
            r = after[rid]
            print(f"  + 新增 id={rid}: {r['item_name']} / {r['hold_type']} / "
                  f"{r['owner_name']} / [{r['valid_from']},{r['valid_to']})"
                  f" / instance={r['instance_id']}")
            continue
        changed = {k: (before[rid].get(k), after[rid].get(k))
                   for k in after[rid] if before[rid].get(k) != after[rid].get(k)}
        if changed:
            body = ", ".join(f"{k}: {a!r} → {b!r}" for k, (a, b) in changed.items())
            print(f"  ~ 修改 id={rid}: {body}")
    print(f"  行数: {len(before)} → {len(after)}")


def group_segments(con: sqlite3.Connection) -> dict[tuple, list[dict]]:
    con.row_factory = sqlite3.Row
    groups: dict[tuple, list[dict]] = {}
    for r in con.execute(
            "SELECT id, item_name, hold_type, instance_id, owner_name, valid_from, valid_to "
            "FROM kg_item_holding "
            "ORDER BY item_name, hold_type, COALESCE(instance_id,-1), valid_from"):
        key = (r["item_name"], r["hold_type"], r["instance_id"])
        groups.setdefault(key, []).append(dict(r))
    return groups


def check_all(con: sqlite3.Connection, *, verbose: bool = False) -> list[str]:
    """全表结构自检：只报不改。返回问题清单。"""
    problems: list[str] = []
    groups = group_segments(con)
    for key, segs in groups.items():
        opens = [s for s in segs if s["valid_to"] is None]
        if len(opens) > 1:
            problems.append(f"{key} 并行开放段 {[s['id'] for s in opens]}")
        for i, s in enumerate(segs[:-1]):
            nxt = segs[i + 1]
            if s["valid_to"] != nxt["valid_from"]:
                problems.append(
                    f"{key} 不连续/重叠: id={s['id']}.valid_to={s['valid_to']} "
                    f"≠ id={nxt['id']}.valid_from={nxt['valid_from']}")
    if verbose:
        print("\n== 全表分组 ==")
        for key, segs in groups.items():
            chain = " → ".join(
                f"[{s['valid_from']},{s['valid_to']}) {s['owner_name']}(id={s['id']})"
                for s in segs)
            print(f"  {key}: {chain}")
    return problems


def check_target(con: sqlite3.Connection) -> None:
    """三项涉及的组，必须严格首尾相接。"""
    print("\n== 目标组段清单 ==")
    want = {
        ("地书", "custody", 1):
            [(0, 51, "金莲师兄"), (51, 74, "许七安"), (74, 76, "魏渊"),
             (76, 105, "许七安"), (105, 108, "许新年"), (108, None, "许七安")],
        ("黑金长刀", "own", None): [(93, 102, "许七安"), (102, None, "许七安")],
    }
    groups = group_segments(con)
    for key, exp in want.items():
        segs = groups.get(key, [])
        got = [(s["valid_from"], s["valid_to"], s["owner_name"]) for s in segs]
        for s in segs:
            print(f"  {key}: id={s['id']} [{s['valid_from']},{s['valid_to']}) "
                  f"{s['owner_name']}")
        if got != exp:
            raise SystemExit(f"段校验失败 {key}\n  期望 {exp}\n  实际 {got}")
    if ("黑金刀", "own", None) in groups:
        raise SystemExit("黑金刀 仍有持有段，未合并干净")
    print("  目标组：全部首尾相接、无并行开放段")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
def apply_generated(con: sqlite3.Connection, *, apply: bool) -> None:
    before = rows_of(con)
    for rid, exp in EXPECT.items():
        r = before.get(rid)
        got = ((r["item_name"], r["hold_type"], r["owner_name"],
                r["valid_from"], r["valid_to"], r["instance_id"]) if r else None)
        if got != exp:
            raise SystemExit(f"前置校验失败 id={rid}: 期望 {exp}，实际 {got}")

    # 1. 黑金刀并行段：删除被完全覆盖的 [94,∞)
    con.execute("DELETE FROM kg_item_holding WHERE id=57")

    # 2. G2：拆 id=32
    con.execute("UPDATE kg_item_holding SET valid_to=74 WHERE id=32")
    con.execute(
        "INSERT INTO kg_item_holding (item_id, item_name, hold_type, owner_id, owner_name, "
        "owner_kind, owner_state, valid_from, valid_to, transfer_event_id, reason_code, "
        "evidence_chapter, evidence, confidence, created_at, instance_id) "
        "VALUES (NULL, '地书', 'custody', NULL, '魏渊', 'character', 'held', 74, 76, "
        "NULL, 'transfer_event', 74, ?, 0.6, ?, 1)", (WEI_EVIDENCE, now_iso()))
    con.execute(
        "INSERT INTO kg_item_holding (item_id, item_name, hold_type, owner_id, owner_name, "
        "owner_kind, owner_state, valid_from, valid_to, transfer_event_id, reason_code, "
        "evidence_chapter, evidence, confidence, created_at, instance_id) "
        "VALUES (NULL, '地书', 'custody', NULL, '许七安', 'character', 'held', 76, 105, "
        "NULL, 'transfer_event', 76, ?, 0.6, ?, 1)", (XU_BACK_EVIDENCE, now_iso()))

    # 3. G3：105-108 不留缝
    con.execute("UPDATE kg_item_holding SET valid_to=108 WHERE id=33")
    con.execute(
        "UPDATE kg_item_holding SET valid_from=108, evidence_chapter=108, evidence=? "
        "WHERE id=34", (XU_108_EVIDENCE,))

    check_target(con)
    after = rows_of(con)
    diff_rows(before, after)

    problems = check_all(con, verbose=True)
    print("\n== 全表自检 ==")
    for p in problems:
        print("  !!", p)
    if not problems:
        print("  无重叠、无并行开放段")

    if not apply:
        con.rollback()
        print("\n[dry-run] generated.db 已回滚")
    else:
        con.commit()


# ---------------------------------------------------------------------------
def apply_entities(con: sqlite3.Connection, *, apply: bool) -> None:
    con.row_factory = sqlite3.Row
    ents = {r["canonical_name"]: dict(r) for r in con.execute(
        "SELECT id, kind, canonical_name, aliases FROM kg_entities "
        "WHERE kind='item' AND canonical_name IN ('黑金刀','黑金长刀')")}
    for need in ("黑金刀", "黑金长刀"):
        if need not in ents:
            raise SystemExit(f"前置校验失败：kg_entities 缺 item「{need}」")

    print("\n== kg_entities 黑金 ==")
    for name in ("黑金刀", "黑金长刀"):
        print(f"  改前 id={ents[name]['id']} {name}: "
              f"{json.loads(ents[name]['aliases'] or '[]')}")

    con.execute("UPDATE kg_entities SET aliases=? WHERE id=?",
                (json.dumps(HEIJIN_ALIASES, ensure_ascii=False), ents["黑金长刀"]["id"]))
    con.execute("DELETE FROM kg_entities WHERE id=?", (ents["黑金刀"]["id"],))

    left = list(con.execute(
        "SELECT id FROM kg_entities WHERE project_id=5 AND kind='item' "
        "AND canonical_name='黑金刀'"))
    if left:
        raise SystemExit(f"收敛失败，仍存在规范名 黑金刀: {[r[0] for r in left]}")
    aliases = json.loads(con.execute(
        "SELECT aliases FROM kg_entities WHERE id=?",
        (ents["黑金长刀"]["id"],)).fetchone()[0])
    if "黑金刀" not in aliases:
        raise SystemExit("黑金长刀 别名里没有 黑金刀")
    print(f"  改后 黑金长刀(id={ents['黑金长刀']['id']}): {aliases}")
    print(f"  删除规范名: 黑金刀(id={ents['黑金刀']['id']})")

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
        backup(GEN, "generated-segment")
        path = GEN
    else:
        tmp = pathlib.Path(tempfile.gettempdir()) / "generated_segment_dry.db"
        if tmp.exists():
            tmp.unlink()
        src = sqlite3.connect(f"file:{GEN}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp))
        src.backup(dst)
        dst.close()
        src.close()
        print("[dry-run] 副本:", tmp)
        path = tmp
    con = sqlite3.connect(str(path))
    apply_generated(con, apply=apply)
    con.close()

    print("\n" + "=" * 74)
    print("novel_dev.db:", APP)
    if apply:
        backup(APP, "novel_dev-segment")
        path2 = APP
    else:
        tmp2 = pathlib.Path(tempfile.gettempdir()) / "novel_dev_segment_dry.db"
        if tmp2.exists():
            tmp2.unlink()
        src = sqlite3.connect(f"file:{APP}?mode=ro", uri=True)
        dst = sqlite3.connect(str(tmp2))
        src.backup(dst)
        dst.close()
        src.close()
        print("[dry-run] 副本:", tmp2)
        path2 = tmp2
    con2 = sqlite3.connect(str(path2))
    apply_entities(con2, apply=apply)
    con2.close()

    print("\n完成:", "已写入真实库" if apply else "dry-run，未写真实库")


if __name__ == "__main__":
    sys.exit(main())
