# -*- coding: utf-8 -*-
"""验证物品持有读通道（只读真实库 + D:\\Temp 副本）。

真实库：backend/data/generated.db（63 行）、backend/novel_dev.db（kg_entities 为空，
所以实体解析按原名继续）。副本只用来放真实库没有的演示行：

- 地书碎片·三号 / 六号（实例维度，数据与此前 verify_instances 一致）；
- 试炼石（unowned）、无名匣（unknown）——真实库只有 held / sealed，
  这两行仅用于四态渲染样例，不写回真实库。

跑法（仓库根）：
    python backend/migrations/2026-11-10-item-holding-v3/verify_read_channel.py
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.generation.prompts import build_user_prompt  # noqa: E402
from app.kg import generated_store as store  # noqa: E402
from app.kg import item_holding_read as read  # noqa: E402

REAL_DB = BACKEND / "data" / "generated.db"
APP_DB = BACKEND / "novel_dev.db"
COPY_DB = pathlib.Path("D:/Temp/kg_read_copy.db")


def _app_session():
    return Session(create_engine(f"sqlite:///{APP_DB}"))


def _backup_copy() -> pathlib.Path:
    COPY_DB.parent.mkdir(parents=True, exist_ok=True)
    if COPY_DB.exists():
        COPY_DB.unlink()
    src = sqlite3.connect(f"file:{REAL_DB}?mode=ro", uri=True)
    dst = sqlite3.connect(COPY_DB)
    with dst:
        src.backup(dst)
    dst.execute("PRAGMA integrity_check").fetchone()
    src.close()
    dst.close()
    return COPY_DB


def _seed_copy(path: pathlib.Path) -> None:
    """只在副本上加实例与 unowned/unknown 演示行。"""
    con = store.connect(path)
    store.ensure_schema(con)
    three = store.record_item_instance(
        con, "地书碎片", "三号", 100,
        evidence="三号碎片在许七安手中（副本演示，取自 verify_instances）")
    six = store.record_item_instance(
        con, "地书碎片", "六号", 153,
        evidence="六号碎片由恒远持有（副本演示，取自 verify_instances）")
    store.record_holding(con, "地书碎片", "own", "许七安", 100, instance_id=three)
    store.record_holding(con, "地书碎片", "own", "恒远", 153, instance_id=six)
    store.record_holding(con, "试炼石", "own", None, 30, owner_state="unowned",
                         reason_code="demo_unowned", evidence="演示行：确实无主")
    store.record_holding(con, "无名匣", "own", None, 40, owner_state="unknown",
                         reason_code="demo_unknown", evidence="演示行：不知在谁手上")
    con.commit()
    con.close()


def _show(title: str, facts: list[dict]) -> str:
    print(f"\n===== {title} =====")
    for fact in facts:
        print(json.dumps(fact, ensure_ascii=False))
    text = read.format_holding_facts(facts)
    print("--- 渲染 ---")
    print(text)
    return text


def _count_queries(items: list[dict], db) -> tuple[int, int]:
    app_queries: list[str] = []
    engine = db.get_bind()
    listener = lambda conn, cur, stmt, *a: app_queries.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)

    holding_queries: list[str] = []
    original = read._connect_generated

    def traced(path):
        con = original(path)
        con.set_trace_callback(
            lambda sql: holding_queries.append(sql)
            if "FROM kg_item_holding" in sql else None)
        return con

    read._connect_generated = traced
    try:
        read.current_item_holdings(items, db=db, project_id=1, generated_db=REAL_DB)
    finally:
        read._connect_generated = original
        event.remove(engine, "before_cursor_execute", listener)
    return len(app_queries), len(holding_queries)


def main() -> None:
    print(f"real db: {REAL_DB}")
    print(f"app db : {APP_DB}")
    copy = _backup_copy()
    _seed_copy(copy)
    print(f"copy   : {copy}（只放实例与 unowned/unknown 演示行）")

    with _app_session() as app:
        real = read.current_item_holdings(
            ["地书", "神剑", "封印物", "诗碑"], db=app, project_id=1,
            generated_db=REAL_DB)
        _show("真实库：地书 / 神剑 / 封印物 / 诗碑", real)

        inst = read.current_item_holdings(
            [{"name": "地书碎片"}], db=app, project_id=1, generated_db=copy)
        _show("副本：地书碎片（实例）", inst)

        states = read.current_item_holdings(
            ["试炼石", "无名匣"], db=app, project_id=1, generated_db=copy)
        _show("副本演示：unowned / unknown", states)

        all_facts = real + inst + states
        block = read.format_holding_facts(all_facts)
        print("\n===== 进 prompt 的位置（build_user_prompt 实参） =====")
        prompt = build_user_prompt([], [], "", 3000, item_holding_block=block)
        start = prompt.index("【物品持有事实")
        print(prompt[start:])

        items20 = [{"name": "地书", "item_id": None}] + [
            {"name": f"查无此物{i}", "item_id": None} for i in range(19)]
        app_n, holding_n = _count_queries(items20, app)
        print("\n===== 批量查询次数（20 个物品） =====")
        print(f"novel_dev.db 查询: {app_n} 次")
        print(f"generated.db 查询: {holding_n} 次")
        print(f"合计: {app_n + holding_n} 次")

    print("\n===== 开关 =====")
    print("settings.kg_item_holding_inject = True（默认）")
    print("关闭：环境变量 KG_ITEM_HOLDING_INJECT=false，或运行时改 settings 后重建 context")
    print("真实库未被写入；副本路径:", copy)


if __name__ == "__main__":
    main()
