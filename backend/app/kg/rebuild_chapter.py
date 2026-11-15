"""按章重建 v3 KG 的 CLI 入口（修数据用；走版本化，不物理删行）。

语义见 ``batch_worker.rebuild_chapter``：同章旧版本的事件/提及/关系/角色状态
保留在库里但置 ``is_current=0``，新结果写为当前版本；检索、切片、状态渲染、
时间线只认当前版本。

用法（在 ``backend/`` 目录下）::

    # 先拿主库副本演练（只影响副本）
    python -m app.kg.rebuild_chapter --project-id 6 --chapter 5 --db sqlite:///./copy.db

    # 只入队，不同步等待批抽跑完（交给常驻后端 worker / 周期扫描）
    python -m app.kg.rebuild_chapter --project-id 6 --chapter 5 --no-flush

缺省 ``--db`` 用项目配置（``DATABASE_URL`` / settings.database_url）。入队后
默认同步跑完该项目队列，当场能看到事件/状态/关系落到哪个版本。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按章重建 v3 KG（版本化，不删行）")
    ap.add_argument("--project-id", type=int, required=True)
    ap.add_argument("--chapter", type=int, required=True,
                    help="故事章号（从章节标签派生，不是段号）")
    ap.add_argument("--db", default="",
                    help="SQLAlchemy URL；缺省用 settings.database_url。"
                         "建议先指向主库副本演练")
    ap.add_argument("--no-flush", action="store_true",
                    help="只入队，不同步跑批抽（交给常驻 worker / 周期扫描）")
    args = ap.parse_args(argv)

    # DATABASE_URL 必须在 import app.config / app.db 之前生效
    if args.db:
        os.environ["DATABASE_URL"] = args.db
    # 允许 `python path/to/rebuild_chapter.py` 直接跑（不依赖 cwd）
    backend_root = pathlib.Path(__file__).resolve().parents[2]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))

    from app.db import SessionLocal
    from app.kg import batch_worker as bw

    with SessionLocal() as db:
        result = bw.rebuild_chapter(db, args.project_id, args.chapter)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("run_id") is None:
            return 1
        if not args.no_flush:
            bw.flush_project(args.project_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
