# -*- coding: utf-8 -*-
"""端到端验证：在 novel_dev.db 的**副本**上 seed 一段 kg_mentions，
跑真实 build_context，打印【物品持有事实】块原文。

真实库一行不写：mentions 只存在于副本。脚本结束会复查真实库 kg_mentions=0。

跑法：python backend/migrations/2026-11-10-item-holding-v3/verify_copy_e2e.py
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import create_engine, event, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.kg.item_holding_read as ihr  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.generation.engine import build_context  # noqa: E402
from app.generation.prompts import build_user_prompt  # noqa: E402
from app.models import Generation, KgEntity, KgMention, Project  # noqa: E402

REAL = ROOT / "backend" / "novel_dev.db"
COPY = pathlib.Path(tempfile.gettempdir()) / "novel_dev_e2e.db"
PROJECT_ID = 5
SEGMENT = 2  # build_context 用 _next_segment_no - 1 = 2


def main() -> None:
    COPY.parent.mkdir(parents=True, exist_ok=True)
    if COPY.exists():
        COPY.unlink()
    src = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    dst = sqlite3.connect(str(COPY))
    src.backup(dst)
    dst.close()
    src.close()
    print(f"副本：{COPY}")

    engine = create_engine(f"sqlite:///{COPY.as_posix()}", future=True)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    project = db.get(Project, PROJECT_ID)
    gen = db.scalar(select(Generation).where(
        Generation.project_id == PROJECT_ID,
        Generation.segment_no == SEGMENT,
    ))

    # —— 挑「持有表里真的有记录」的物品，覆盖：own / own+custody 多段 / sealed ——
    PREFER = ["神剑", "地书", "封印物", "军弩", "半块玉佩"]
    gen_con = sqlite3.connect(f"file:{settings.kg_generated_db}?mode=ro", uri=True)
    gen_names = [r[0] for r in gen_con.execute(
        "SELECT item_name FROM kg_item_holding "
        "WHERE item_name IS NOT NULL GROUP BY item_name "
        "ORDER BY COUNT(*) DESC, item_name")]
    gen_con.close()

    entities = db.scalars(select(KgEntity).where(
        KgEntity.project_id == PROJECT_ID,
        KgEntity.kind == "item",
        KgEntity.canonical_name.in_(gen_names),
    )).all()
    by_name = {e.canonical_name: e for e in entities}
    picked = [by_name[n] for n in PREFER if n in by_name]
    picked += [e for e in entities if e.canonical_name not in PREFER][:6 - len(picked)]
    print(f"seed 物品：{[e.canonical_name for e in picked]}")

    for ent in picked:
        db.add(KgMention(
            project_id=PROJECT_ID, entity_id=ent.id,
            generation_id=gen.id if gen else None, segment_no=SEGMENT,
            context_window=f"（副本验证用的人造提及）{ent.canonical_name}",
            mention_kind="direct",
        ))
    db.commit()
    seeded = db.scalar(select(KgMention).where(
        KgMention.project_id == PROJECT_ID, KgMention.segment_no == SEGMENT))
    print(f"副本 kg_mentions(project=5, seg=2) 已 seed，第一条 id={seeded.id}")

    # —— 真实 build_context（不调 LLM） ——
    ctx = build_context(db, project)
    block = ctx["item_holding_block"] or ""
    print("\n===== build_context['item_holding_block'] 原文 =====")
    print(block if block else "（空）")

    max_chars = project.generation_length or 3000
    user = build_user_prompt(
        ctx["prior_summaries"], ctx["rag_context"], project.outline, max_chars,
        project.plot_bible, project.premise,
        prior_blocks=ctx["prior_blocks"],
        style_exemplars=ctx.get("style_exemplars"),
        rag_has_forbidden=ctx.get("rag_has_forbidden", False),
        kg_slice=ctx.get("kg_slice") or None,
        item_holding_block=ctx.get("item_holding_block") or None,
    )
    start = user.find("【物品持有事实")
    end = user.find("【续写指令】")
    print("\n===== 真实 user prompt 中的该段（含标题）=====")
    print(user[start:end].rstrip() if start != -1 else "（prompt 里没有该块）")

    # —— 查询次数：应用库用 SQLAlchemy 事件，持有库用 trace 回调 ——
    app_hits: list[str] = []
    gen_hits: list[str] = []
    event.listen(
        engine, "before_cursor_execute",
        lambda conn, cur, stmt, params, ctx_, many: app_hits.append(stmt),
    )
    orig_connect = ihr._connect_generated

    def counting_connect(path):
        con = orig_connect(path)
        con.set_trace_callback(lambda s: gen_hits.append(s))
        return con

    ihr._connect_generated = counting_connect
    try:
        facts = ihr.current_item_holdings(
            [{"name": e.canonical_name, "item_id": e.id} for e in picked],
            db=db, project_id=PROJECT_ID,
            generated_db=settings.kg_generated_db,
        )
    finally:
        ihr._connect_generated = orig_connect

    print("\n===== current_item_holdings 查询次数 =====")
    print(f"novel_dev（应用库）SQL 数：{len(app_hits)}")
    for s in app_hits:
        print("  ·", " ".join(s.split())[:100])
    print(f"generated（持有库）SQL 数：{len(gen_hits)}")
    for s in gen_hits:
        print("  ·", " ".join(s.split())[:100])
    print(f"事实条数：{len(facts)}  状态：{[f['status'] for f in facts]}")

    db.close()
    engine.dispose()

    # —— 复查真实库没被写 ——
    real = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    n = real.execute("SELECT COUNT(*) FROM kg_mentions").fetchone()[0]
    items = real.execute(
        "SELECT COUNT(*) FROM kg_entities WHERE kind='item'").fetchone()[0]
    real.close()
    print(f"\n真实库复查：kg_mentions={n} 行（应为 0），kg_entities item={items} 行")


if __name__ == "__main__":
    main()
