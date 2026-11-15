# -*- coding: utf-8 -*-
"""按 note 幂等回滚关系章归属回填（role=kg_relation_backfill）。

回填执行时把每条变更（id / old / new / basis / evidence）写进了
``agent_runs.note``，这里按它逐条把 ``since_chapter`` 改回 ``old``：

- 只在当前值仍等于 ``new`` 时回写（幂等；执行后模型重抽改过的边不会被误伤）
- ``old`` 为 NULL 的边回滚成 NULL
- 默认只打印计划，加 ``--yes`` 才写库；DB 备份仍在
  ``backend/novel_dev.db.bak-rel-backfill-<时间戳>``

用法（在 backend 目录）：
    python -m app.kg.relation_backfill_rollback            # 预览
    python -m app.kg.relation_backfill_rollback --yes      # 执行
    python -m app.kg.relation_backfill_rollback --run-id 189 --yes
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import SessionLocal

ROLE = "kg_relation_backfill"


def rollback(run_id: int | None = None, apply: bool = False) -> dict:
    db = SessionLocal()
    try:
        sql = ("select id, note from agent_runs where role=:r "
               + ("and id=:i " if run_id else "")
               + "order by id desc limit 1")
        params = {"r": ROLE}
        if run_id:
            params["i"] = run_id
        row = db.execute(text(sql), params).fetchone()
        if row is None:
            raise SystemExit("没有找到 kg_relation_backfill 留痕行")
        note = json.loads(row[1] or "{}")
        changes = note.get("changes") or []
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        done = skipped = 0
        for c in changes:
            if c.get("new") is None:
                where, extra = "since_chapter is null", {}
            else:
                where, extra = "since_chapter=:new", {"new": c["new"]}
            params = {"old": c.get("old"), "ts": ts, "id": c["id"], **extra}
            if not apply:
                continue
            r = db.execute(text(
                f"update kg_relations set since_chapter=:old, updated_at=:ts "
                f"where id=:id and {where}"), params)
            if r.rowcount == 1:
                done += 1
            else:
                skipped += 1
                print(f"跳过 #{c['id']}：当前值已不是 {c['new']}（可能被重抽改过）")
        if apply:
            db.commit()
        return {"run_id": row[0], "total": len(changes),
                "restored": done, "skipped": skipped, "applied": apply}
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", type=int, default=None)
    ap.add_argument("--yes", action="store_true", help="真正写库（默认只预览）")
    a = ap.parse_args()
    print(rollback(a.run_id, apply=a.yes))
