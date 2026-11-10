# -*- coding: utf-8 -*-
"""实例维度小样：在 generated.db 的临时副本上写两块真实碎片。

只验证，不推广：
  三号碎片（ch100 追述：「您将三号碎片赠予我时」）→ 许七安持，金莲师兄为赠予方
  六号碎片（ch153：「这面玉石小镜……是属于恒远的六号碎片」）→ 恒远持
  无片号（ch79「打更人掌握着一块地书碎片」）→ 不建实例、不建段

用法：backend\\venv\\Scripts\\python.exe backend\\migrations\\2026-11-10-item-holding-v3\\verify_instances.py
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


def snapshot(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM kg_item_holding ORDER BY id")]
    return (len(rows),
            hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                      default=str).encode()).hexdigest()[:16])


def main():
    real = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    real.row_factory = sqlite3.Row
    before = snapshot(real)
    real.close()

    tmp = pathlib.Path(tempfile.gettempdir()) / "kg_instance_copy.db"
    for suffix in ("", "-wal", "-shm"):
        p = pathlib.Path(str(tmp) + suffix)
        if p.exists():
            p.unlink()
    src = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    dst = sqlite3.connect(str(tmp))
    src.backup(dst)
    dst.close()
    src.close()

    con = store.connect(tmp)
    con.executescript((HERE / "002_story_time_axis.sql").read_text(encoding="utf-8"))
    con.executescript((HERE / "003_item_instance.sql").read_text(encoding="utf-8"))
    print("副本:", tmp)

    three = store.record_item_instance(
        con, "地书碎片", "三号", 100,
        evidence="金莲师兄，您将三号碎片赠予我时，说它是地书的一部分")
    six = store.record_item_instance(
        con, "地书碎片", "六号", 153,
        evidence="这面玉石小镜是从井底找到的，是属于恒远的六号碎片")
    print("实例:", {"三号": three, "六号": six})

    # 三号：赠予发生在 ch100 之前，只有追述，没有章号 → 回溯分支
    r3 = store.record_story_segment(
        con, "地书碎片", "own", "许七安", 100, instance_id=three,
        narration="retro", transfer_from_name="金莲师兄",
        anchor_label="追述于 ch100，赠予时点正文未给章号",
        evidence="您将三号碎片赠予我时")
    # 六号：ch153 当章陈述恒远持有 → 章节轴
    r6 = store.record_holding(con, "地书碎片", "own", "恒远", 153,
                              instance_id=six, reason_code="explicit_hold",
                              evidence="是属于恒远的六号碎片")
    n6 = store.record_story_segment(con, "地书碎片", "own", "恒远", 153,
                                    instance_id=six, holding_id=r6["id"],
                                    evidence="是属于恒远的六号碎片")
    print("三号:", r3)
    print("六号 章节轴:", r6, "旁注:", n6)

    print("\n实际行（kg_item_holding）:")
    for r in con.execute("SELECT id, item_name, instance_id, hold_type, owner_name, "
                         "valid_from, valid_to FROM kg_item_holding "
                         "WHERE item_name = '地书碎片' ORDER BY id"):
        print("  ", dict(r))
    print("实际行（kg_holding_story_segment）:")
    for r in con.execute("SELECT id, holding_id, instance_id, owner_name, order_key, "
                         "order_basis, anchor_label, narration, transfer_from_name, "
                         "evidence_chapter FROM kg_holding_story_segment WHERE "
                         "item_name = '地书碎片' ORDER BY id"):
        print("  ", dict(r))

    print("\n当前持有者（按实例收窄）:")
    for label, iid in (("三号", three), ("六号", six)):
        print(f"  {label} →", store.current_holder(con, "地书碎片", instance_id=iid))

    # 无片号：不建实例、不建段
    unnumbered = con.execute(
        "SELECT COUNT(*) FROM kg_item_instance WHERE item_name = '地书碎片' "
        "AND instance_label NOT IN ('三号', '六号')").fetchone()[0]
    print("\n无片号（打更人 ch79 一块）：实例", unnumbered,
          "行，持有段", con.execute(
              "SELECT COUNT(*) FROM kg_item_holding WHERE item_name = '地书碎片' "
              "AND instance_id IS NULL").fetchone()[0], "行 —— 按约定不建")

    con.close()
    real = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    real.row_factory = sqlite3.Row
    after = snapshot(real)
    real.close()
    print("\n真实库:", before, "→", after, "未变:", before == after)


if __name__ == "__main__":
    main()
