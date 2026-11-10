"""物品持有史（kg_item_holding）：建表幂等、写入三条硬规则、两条读接口。

用户口径：
1. 先关后插：段首尾相接，无重叠无空洞；
2. 同章竞争不写史：有转交事件按事件 object 胜出，否则全部进候选；
3. 拒绝零长度段与倒序段；
4. 第三态（owner_id 空、owner_name 非空）不能被当成无主。
"""
from __future__ import annotations

import pathlib
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.kg import generated_store as store  # noqa: E402


def _db(tmp_path):
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    return con


def _rows(con, item_name):
    return store.item_holding_timeline(con, item_name)


def _assert_contiguous(rows):
    """每种 hold_type 各自一条线：按起点排序后首尾相接，最后一段开放。"""
    by_type: dict[str, list[dict]] = {}
    for row in rows:
        by_type.setdefault(row["hold_type"], []).append(row)
    assert by_type, "没有任何段"
    for hold_type, segs in by_type.items():
        segs.sort(key=lambda r: r["valid_from"])
        for seg in segs:
            assert seg["valid_to"] is None or seg["valid_to"] > seg["valid_from"], \
                f"{hold_type} 出现零长度/倒序段：{seg}"
        for prev, nxt in zip(segs, segs[1:]):
            assert prev["valid_to"] == nxt["valid_from"], \
                f"{hold_type} 段不连续：{prev['valid_to']} != {nxt['valid_from']}"
        assert segs[-1]["valid_to"] is None, f"{hold_type} 最后一段不是开放段"


# ---------------------------------------------------------------------------
# 1. 迁移幂等
# ---------------------------------------------------------------------------
def _snapshot(con):
    cols = [tuple(r) for r in con.execute("PRAGMA table_info(kg_item_holding)")]
    idx = [tuple(r) for r in con.execute("PRAGMA index_list(kg_item_holding)")]
    ddl = [tuple(r) for r in con.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name LIKE '%item_holding%' ORDER BY name")]
    return cols, idx, ddl


def test_schema_is_idempotent(tmp_path):
    con = store.connect(tmp_path / "gen.db")
    store.ensure_schema(con)
    first = _snapshot(con)
    store.ensure_schema(con)  # 第二次：不报错、不重建
    second = _snapshot(con)
    con.close()
    assert first == second
    assert first[0], "建表后没有列"
    names = {row[1] for row in first[2]}
    assert {"kg_item_holding", "ux_item_holding_open",
            "ix_item_holding_item", "ix_item_holding_owner"} <= names


# ---------------------------------------------------------------------------
# 2. 地书：own / custody 两条线并行
# ---------------------------------------------------------------------------
def test_own_and_custody_run_in_parallel(tmp_path):
    con = _db(tmp_path)
    try:
        r1 = store.record_holding(con, "地书", "own", "地宗", 20,
                                  owner_state="held", evidence="地书归地宗所有")
        r2 = store.record_holding(con, "地书", "custody", "许七安", 30,
                                  owner_state="held", evidence="许七安随身保管地书")
        assert r1["action"] == "inserted" and r2["action"] == "inserted"

        rows = _rows(con, "地书")
        assert len(rows) == 2
        assert {r["hold_type"] for r in rows} == {"own", "custody"}
        assert all(r["valid_to"] is None for r in rows)
        _assert_contiguous(rows)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 3. 神剑：sealed -> held -> held（同主同态复述不重开段）
# ---------------------------------------------------------------------------
def test_sealed_then_held_then_held_is_contiguous(tmp_path):
    con = _db(tmp_path)
    try:
        a = store.record_holding(con, "镇国神剑", "own", None, 10,
                                 owner_state="sealed", reason_code="seal",
                                 evidence="神剑被封在剑冢")
        b = store.record_holding(con, "镇国神剑", "own", "许七安", 20,
                                 owner_state="held", reason_code="take",
                                 evidence="许七安拔出了神剑")
        c = store.record_holding(con, "镇国神剑", "own", "许七安", 30,
                                 owner_state="held", evidence="许七安仍佩神剑")
        assert (a["action"], b["action"], c["action"]) == ("inserted", "inserted", "confirmed")
        assert b["closed"] == 1

        rows = _rows(con, "镇国神剑")
        assert [(r["owner_name"], r["owner_state"], r["valid_from"], r["valid_to"])
                for r in rows] == [(None, "sealed", 10, 20),
                                   ("许七安", "held", 20, None)]
        _assert_contiguous(rows)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 4. 封印物：sealed -> unknown（三态不写哨兵名）
# ---------------------------------------------------------------------------
def test_sealed_to_unknown(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "封印物", "own", None, 15,
                             owner_state="sealed", reason_code="seal")
        r = store.record_holding(con, "封印物", "own", None, 60,
                                 owner_state="unknown", reason_code="lost_track")
        assert r["action"] == "inserted" and r["closed"] == 1

        rows = _rows(con, "封印物")
        assert [r["owner_state"] for r in rows] == ["sealed", "unknown"]
        assert all(r["owner_name"] is None for r in rows)
        assert rows[0]["valid_to"] == 60
        _assert_contiguous(rows)

        # 「未知」不能变成可按名字查到的持有者
        for sentinel in ("未知", "桑泊封印"):
            assert store.holder_current_holdings(con, sentinel) == []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 4b. 三态的 owner_name 口径：非 held 不许带名字，held 至少一个标识
# ---------------------------------------------------------------------------
def test_owner_name_rules_per_state(tmp_path):
    con = _db(tmp_path)
    try:
        for state in ("unowned", "sealed", "unknown"):
            try:
                store.record_holding(con, "某物", "own", "未知", 10,
                                     owner_state=state)
            except ValueError as exc:
                assert "owner_name" in str(exc)
            else:  # pragma: no cover - 触发即说明约束没生效
                raise AssertionError(f"{state} 态居然接受了 owner_name")
        try:
            store.record_holding(con, "某物", "own", None, 10, owner_state="held")
        except ValueError as exc:
            assert "owner_id" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("held 态居然接受了空 id + 空 name")
        assert _rows(con, "某物") == []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 5. 玉石小镜：同章买卖（无转交事件）-> 全部候选，不写史
# ---------------------------------------------------------------------------
def test_same_chapter_clash_without_event_goes_to_candidates(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "玉石小镜", "own", "许七安", 105,
                             evidence="许七安把镜子卖给堂弟")
        r = store.record_holding(con, "玉石小镜", "own", "许新年", 105,
                                 evidence="许新年买下玉石小镜")
        assert r["action"] == "candidate"
        assert r["candidates"] == 2

        assert _rows(con, "玉石小镜") == []
        cands = [dict(row) for row in con.execute(
            "SELECT * FROM gen_candidates WHERE from_name = ? ORDER BY to_name",
            ("玉石小镜",))]
        assert [(c["to_name"], c["rel_type"], c["reason"]) for c in cands] == [
            ("许七安", "own", store.SAME_CHAPTER_CLASH),
            ("许新年", "own", store.SAME_CHAPTER_CLASH),
        ]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 6. 同章竞争：有转交事件，事件 object 胜出（两条路径）
# ---------------------------------------------------------------------------
def test_same_chapter_event_object_wins(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "镇国神剑", "own", "许七安", 110,
                             evidence="许七安手持神剑")
        r = store.record_holding(con, "镇国神剑", "own", "宋廷风", 110,
                                 transfer_event_id=7, event_object="宋廷风",
                                 evidence="许七安把神剑交给宋廷风")
        assert r["action"] == "replaced"

        rows = _rows(con, "镇国神剑")
        assert [(r["owner_name"], r["valid_from"], r["valid_to"])
                for r in rows] == [("宋廷风", 110, None)]
        _assert_contiguous(rows)
    finally:
        con.close()


def test_same_chapter_event_object_keeps_previous(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "镇国神剑", "own", "宋廷风", 110,
                             transfer_event_id=7, event_object="宋廷风",
                             evidence="宋廷风接剑")
        r = store.record_holding(con, "镇国神剑", "own", "许七安", 110,
                                 transfer_event_id=7, event_object="宋廷风",
                                 evidence="误判：许七安仍握着剑")
        assert r["action"] == "candidate"

        rows = _rows(con, "镇国神剑")
        assert [r["owner_name"] for r in rows] == ["宋廷风"]
        cands = [dict(row) for row in con.execute(
            "SELECT * FROM gen_candidates WHERE from_name = ?", ("镇国神剑",))]
        assert [c["to_name"] for c in cands] == ["许七安"]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 7. 拒绝零长度段与倒序段
# ---------------------------------------------------------------------------
def test_reject_zero_length_and_backward(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "封印物", "own", None, 50,
                             owner_state="sealed", evidence="封在禁地")
        zero = store.record_holding(con, "封印物", "own", None, 50,
                                    owner_state="unknown", evidence="同章说失联")
        back = store.record_holding(con, "封印物", "own", None, 40,
                                    owner_state="held", owner_id=7,
                                    evidence="倒序主张")
        assert zero["action"] == "rejected" and zero["reason"] == "zero_length"
        assert back["action"] == "rejected" and back["reason"] == "non_forward"

        rows = _rows(con, "封印物")
        assert len(rows) == 1
        assert rows[0]["owner_state"] == "sealed" and rows[0]["valid_to"] is None
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 8. 第三态：owner_id 空、owner_name 非空
# ---------------------------------------------------------------------------
def test_unnamed_owner_is_readable_and_not_unowned(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "佩刀", "custody", "狱头", 102,
                             owner_id=None, owner_kind="character",
                             owner_state="held", reason_code="confiscated",
                             evidence="收押期间由狱头保管")
        rows = store.holder_current_holdings(con, "狱头")
        assert len(rows) == 1
        assert rows[0]["owner_id"] is None
        assert rows[0]["owner_name"] == "狱头"
        # 第三态不是「无主」：owner_state 仍是 held，且没有落成 unowned
        assert rows[0]["owner_state"] == "held"
        unowned = con.execute(
            "SELECT COUNT(*) FROM kg_item_holding WHERE owner_state = 'unowned'"
        ).fetchone()[0]
        assert unowned == 0
        # 只按 id 查不到它（不能拿「id 为空」当无主）
        assert store.holder_current_holdings(con, owner_id=99) == []
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 9. 读接口：按章覆盖 + 已关闭的段不再算当前
# ---------------------------------------------------------------------------
def test_current_holdings_at_chapter(tmp_path):
    con = _db(tmp_path)
    try:
        store.record_holding(con, "佩刀", "custody", "狱头", 102,
                             owner_state="held", evidence="狱头保管")
        store.record_holding(con, "佩刀", "custody", "许七安", 107,
                             owner_state="held", evidence="许七安取回佩刀")
        at_103 = store.holder_current_holdings(con, "狱头", chapter=103)
        at_107 = store.holder_current_holdings(con, "狱头", chapter=107)
        assert len(at_103) == 1 and at_103[0]["item_name"] == "佩刀"
        assert at_107 == []  # [102, 107) 半开区间，107 章已不归狱头

        open_now = store.holder_current_holdings(con, "许七安")
        assert [r["item_name"] for r in open_now] == ["佩刀"]
        _assert_contiguous(_rows(con, "佩刀"))
    finally:
        con.close()
