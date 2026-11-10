# -*- coding: utf-8 -*-
"""合并后的读通道对照：三个名字查当前持有者 + 段清单 + 计数自检。

用法：python backend/migrations/2026-11-10-item-holding-v3/verify_item_merge.py
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings  # noqa: E402
from app.kg.item_holding_read import current_item_holdings, format_holding_facts  # noqa: E402
from app.models import KgEntity  # noqa: E402

APP = ROOT / "backend" / "novel_dev.db"
PROJECT_ID = 5
NAMES = ["地书", "玉石小镜", "三号碎片", "地书碎片·三号"]


def main() -> None:
    engine = create_engine(f"sqlite:///{APP.as_posix()}", future=True)
    db = sessionmaker(bind=engine, future=True)()

    print("== 三个名字分别查当前持有者 ==")
    facts_by_name = {}
    for name in NAMES:
        facts = current_item_holdings([name], db=db, project_id=PROJECT_ID)
        facts_by_name[name] = facts
        print(f"\n[{name}]")
        for f in facts:
            print(f"  item={f['item_name']} instance={f['instance_id']}"
                  f"({f['instance_label']}) status={f['status']}")
            for line in f["lines"]:
                print(f"    - {line['hold_type']}: {line['owner_name']} "
                      f"[{line['valid_from']}, {line['valid_to']})")
        print("  format:", format_holding_facts(facts))

    base = json.dumps(facts_by_name["地书"], sort_keys=True, ensure_ascii=False)
    same = all(json.dumps(facts_by_name[n], sort_keys=True, ensure_ascii=False) == base
               for n in NAMES)
    print(f"\n四名一致: {same}")
    if not same:
        raise SystemExit("读通道结果不一致")

    print("\n== kg_item_holding：地书 / 黑金长刀 段清单 ==")
    con = sqlite3.connect(f"file:{settings.kg_generated_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in con.execute(
        "SELECT id, item_name, hold_type, instance_id, owner_name, valid_from, valid_to "
        "FROM kg_item_holding WHERE item_name IN ('地书','黑金长刀','黑金刀','监正出品的长刀') "
        "ORDER BY item_name, hold_type, instance_id, valid_from"):
        print(f"  id={r['id']} {r['item_name']} / {r['hold_type']} / "
              f"inst={r['instance_id']} / {r['owner_name']} / "
              f"[{r['valid_from']}, {r['valid_to']})")

    total = con.execute("SELECT COUNT(*) FROM kg_item_holding").fetchone()[0]
    open_dups = list(con.execute(
        "SELECT item_name, hold_type, instance_id, COUNT(*) c FROM kg_item_holding "
        "WHERE valid_to IS NULL GROUP BY item_name, hold_type, instance_id HAVING c > 1"))
    print(f"  总行数: {total}（改前 63）")
    print(f"  并行开放段: {[tuple(r) for r in open_dups] or '无'}")
    if open_dups:
        raise SystemExit("仍有并行开放段")
    con.close()

    print("\n== kg_entities 收敛自检 ==")
    left = db.scalars(select(KgEntity).where(
        KgEntity.project_id == PROJECT_ID,
        KgEntity.canonical_name.in_(["玉石小镜", "暗金色长刀"]),
    )).all()
    print(f"  玉石小镜/暗金色长刀 仍是规范名的行数: {len(left)}")
    dishu = db.scalar(select(KgEntity).where(
        KgEntity.project_id == PROJECT_ID, KgEntity.canonical_name == "地书"))
    print(f"  地书 aliases: {dishu.aliases}")
    print(f"  item 实体总数: {len(db.scalars(select(KgEntity).where(KgEntity.project_id == PROJECT_ID, KgEntity.kind == 'item')).all())}")
    db.close()


if __name__ == "__main__":
    main()
