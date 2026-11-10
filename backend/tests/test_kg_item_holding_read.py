# -*- coding: utf-8 -*-
"""物品持有读通道（item_holding_read）：四态 + 无记录、实例拆分、批量两次查询。

口径：
- 「当前」取顺序键最大的段，回溯分支（holding_id IS NULL）不参与；
- held / unowned / unknown / sealed 分开渲染，查不到段是 no_record，不是 unowned；
- 多实例按 instance_id 分别返回；
- 20 个物品只打两次库（实体一次、持有一次）。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.kg import generated_store as store  # noqa: E402
from app.kg import item_holding_read as read  # noqa: E402

ITEMS = [
    (883, 1, "item", "地书", '["玉石小镜", "镜子"]'),
    (1178, 1, "item", "神剑", '["镇国神剑"]'),
    (884, 1, "item", "地书碎片", "[]"),
    (932, 1, "item", "封印物", "[]"),
]


def _app_session(tmp_path):
    path = tmp_path / "app.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE kg_entities (id INTEGER PRIMARY KEY, project_id INTEGER, "
        "kind TEXT, canonical_name TEXT, aliases TEXT)"
    )
    con.executemany(
        "INSERT INTO kg_entities (id, project_id, kind, canonical_name, aliases) "
        "VALUES (?, ?, ?, ?, ?)", ITEMS,
    )
    con.commit()
    con.close()
    return Session(create_engine(f"sqlite:///{path}"))


def _gen_db(tmp_path):
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    store.record_holding(con, "地书", "own", "天地会", 74, owner_kind="faction")
    store.record_holding(con, "地书", "custody", "许七安", 74, owner_kind="character")
    store.record_holding(con, "神剑", "own", "元景帝", 106)
    store.record_holding(con, "封印物", "own", None, 115, owner_state="sealed")
    store.record_holding(con, "无主剑", "own", None, 10, owner_state="unowned")
    store.record_holding(con, "迷雾珠", "own", None, 20, owner_state="unknown")

    three = store.record_item_instance(con, "地书碎片", "三号", 100)
    six = store.record_item_instance(con, "地书碎片", "六号", 153)
    store.record_holding(con, "地书碎片", "own", "许七安", 100, instance_id=three)
    store.record_holding(con, "地书碎片", "own", "恒远", 153, instance_id=six)

    # 回溯分支：顺序键极小，但不参与「当前」判定（holding_id 为空）。
    store.record_story_segment(
        con, "神剑", "own", "大奉开国皇帝", 95,
        holding_id=None, narration="retro", order_key=0, order_basis="explicit",
        anchor_label="开国之初",
    )
    con.commit()
    return con


def _read(tmp_path, items, db=None):
    app = _app_session(tmp_path)
    con = _gen_db(tmp_path)
    path = pathlib.Path(con.execute("PRAGMA database_list").fetchone()[2])
    con.close()
    with app:
        return read.current_item_holdings(items, db=db if db is not None else app,
                                          project_id=1, generated_db=path)


# ---------------------------------------------------------------------------
# 四态 + 无记录
# ---------------------------------------------------------------------------
def test_four_states_and_no_record(tmp_path):
    facts = _read(tmp_path, ["地书", "神剑", "封印物", "无主剑", "迷雾珠", "诗碑"])
    by_name = {f["item_name"]: f for f in facts}

    assert by_name["地书"]["status"] == "held"
    own = next(l for l in by_name["地书"]["lines"] if l["hold_type"] == "own")
    custody = next(l for l in by_name["地书"]["lines"] if l["hold_type"] == "custody")
    assert (own["owner_name"], custody["owner_name"]) == ("天地会", "许七安")

    assert by_name["神剑"]["status"] == "held"
    assert by_name["神剑"]["lines"][0]["owner_name"] == "元景帝"
    assert by_name["封印物"]["status"] == "sealed"
    assert by_name["无主剑"]["status"] == "unowned"
    assert by_name["迷雾珠"]["status"] == "unknown"
    assert by_name["诗碑"]["status"] == read.NO_RECORD

    text = read.format_holding_facts(facts)
    assert "权属（own）归天地会" in text and "实物（custody）在许七安手上" in text
    assert "权属（own）归元景帝" in text
    assert "已被封印（sealed），不属于任何人持有" in text
    assert "确认为无主（unowned）" in text
    assert "不知在谁手上（unknown）" in text
    # 无记录不能渲染成「无人持有 / 无主」
    no_record_line = next(l for l in text.splitlines() if l.startswith("- 诗碑"))
    assert no_record_line.startswith("- 诗碑：无持有记录")
    assert "不提供持有约束" in no_record_line
    assert "无人持有。" not in no_record_line


# ---------------------------------------------------------------------------
# 回溯分支不参与当前判定
# ---------------------------------------------------------------------------
def test_retro_branch_does_not_win(tmp_path):
    facts = _read(tmp_path, ["神剑"])
    assert facts[0]["lines"][0]["owner_name"] == "元景帝"
    assert facts[0]["lines"][0]["order_key"] == 10600


# ---------------------------------------------------------------------------
# 实例拆分
# ---------------------------------------------------------------------------
def test_instances_are_separate(tmp_path):
    facts = _read(tmp_path, [{"name": None, "item_id": 884}])
    assert [f["instance_label"] for f in facts] == ["三号", "六号"]
    assert [f["lines"][0]["owner_name"] for f in facts] == ["许七安", "恒远"]
    text = read.format_holding_facts(facts)
    assert "地书碎片·三号" in text and "地书碎片·六号" in text


def test_id_only_input_resolves_name_via_entity_query(tmp_path):
    facts = _read(tmp_path, [{"name": None, "item_id": 1178}])
    assert facts[0]["item_name"] == "神剑"
    assert facts[0]["lines"][0]["owner_name"] == "元景帝"


def test_alias_input_resolves_to_canonical(tmp_path):
    """只给别名（玉石小镜）也要落到规范名「地书」，否则合并后生成层查不到持有者。"""
    facts = _read(tmp_path, ["玉石小镜"])
    assert len(facts) == 1
    assert facts[0]["item_name"] == "地书"
    assert facts[0]["status"] == "held"


# ---------------------------------------------------------------------------
# 批量查询次数：20 个物品 → 2 次（实体 1 次 + 持有 1 次）
# ---------------------------------------------------------------------------
def test_batch_hits_each_db_once(tmp_path, monkeypatch):
    items = [{"name": f"物品{i}", "item_id": 1000 + i} for i in range(20)]
    app_queries = []
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}")
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cur, stmt, *a: app_queries.append(stmt))

    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    store.record_holding(con, "物品0", "own", "甲", 10)
    con.commit()
    path = pathlib.Path(con.execute("PRAGMA database_list").fetchone()[2])
    con.close()

    holding_queries = []
    original = read._connect_generated

    def traced(p):
        c = original(p)
        c.set_trace_callback(
            lambda sql: holding_queries.append(sql)
            if "FROM kg_item_holding" in sql else None
        )
        return c

    monkeypatch.setattr(read, "_connect_generated", traced)
    with Session(engine) as db:
        facts = read.current_item_holdings(
            items, db=db, project_id=1, generated_db=path
        )

    assert len(facts) == 20
    assert len(app_queries) == 1, app_queries
    assert len(holding_queries) == 1, holding_queries
    assert facts[0]["status"] == "held"
    assert all(f["status"] == read.NO_RECORD for f in facts[1:])


# ---------------------------------------------------------------------------
# prompt 注入块：有/无开关
# ---------------------------------------------------------------------------
def test_prompt_block_is_optional():
    from app.generation.prompts import build_user_prompt

    plain = build_user_prompt([], [], "", 1000)
    injected = build_user_prompt([], [], "", 1000,
                                 item_holding_block="- 神剑：权属（own）归元景帝")
    assert "物品持有事实" not in plain
    assert "【物品持有事实" in injected
    assert "- 神剑：权属（own）归元景帝" in injected
    # 优先级声明必须写明：持有事实不得压过作者设定 / 世界观事实
    assert "作者设定 > 世界观事实 > 本块" in injected


def test_engine_injection_switch(monkeypatch):
    from app.generation import engine

    fact = {
        "item_name": "神剑", "item_id": None, "instance_id": None,
        "instance_label": None, "status": "held",
        "lines": [{"hold_type": "own", "owner_state": "held",
                   "owner_name": "元景帝", "owner_id": None,
                   "chapter": 106, "order_key": 10600}],
    }
    monkeypatch.setattr(read, "current_item_holdings", lambda *a, **k: [fact])
    monkeypatch.setattr(engine, "_active_item_entries",
                        lambda *a, **k: [{"name": "神剑", "item_id": None}])
    monkeypatch.setattr(engine.settings, "kg_item_holding_inject", False)

    class _Project:
        id = 1

    assert engine._item_holding_text(object(), _Project(), 5) == ""

    monkeypatch.setattr(engine.settings, "kg_item_holding_inject", True)
    text = engine._item_holding_text(object(), _Project(), 5)
    assert "神剑：权属（own）归元景帝" in text
