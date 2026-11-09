"""canon 源开关（legacy|cleaned）与实体层/称呼层的回归测试。

重点不是「能读出来」，而是三条纪律：
1. 默认 legacy，行为不变；
2. cleaned 读不出来时降级 legacy 并 WARN，**绝不返回空表**；
3. 称呼层只收带专名的写法，纯称谓不进层；带约束的写法（白莲/平远伯）不误命中。
"""
from __future__ import annotations

import json
import logging
import pathlib
import sqlite3

import pytest

from app.config import settings
from app.kg import canon_source, layer_context

LAYERS = pathlib.Path(canon_source.LAYERS_PATH)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(settings, "kg_canon_source", "legacy")
    monkeypatch.delenv("KG_CANON_LAYERS", raising=False)
    canon_source.reset_cache()
    yield
    canon_source.reset_cache()


def _legacy_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """最小原著层库：new_nodes / new_edges 各两行，够验证「非空」与过滤。"""
    path = tmp_path / "canon.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE new_nodes (
            canonical_name TEXT, kind TEXT, aliases TEXT, attrs TEXT,
            mentions INTEGER, last_chapter INTEGER, review_status TEXT
        );
        CREATE TABLE new_edges (
            from_name TEXT, to_name TEXT, rel_type TEXT, strength REAL,
            chapters TEXT, status TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO new_nodes VALUES (?,?,?,?,?,?,?)",
        [
            ("许七安", "character", "宁宴|许郎", '{"identity": "打更人"}', 900, 12, "active"),
            ("白帝", "location", "", "{}", 50, 3, "active"),
            ("魏渊", "character", "", "{}", 400, 9, "active"),
        ],
    )
    con.executemany(
        "INSERT INTO new_edges VALUES (?,?,?,?,?,?)",
        [
            ("许七安", "魏渊", "mentor", 0.8, "1-30", "resolved"),
            ("许七安", "白帝", "located_at", 0.9, "1-5", "resolved"),
        ],
    )
    con.commit()
    con.close()
    return path


def _mixed_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """混合源用库：legacy 侧混入势力/地点/法术/物品与两类噪声。"""
    path = tmp_path / "mixed_canon.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE new_nodes (
            canonical_name TEXT, kind TEXT, aliases TEXT, attrs TEXT,
            mentions INTEGER, last_chapter INTEGER, review_status TEXT
        );
        CREATE TABLE new_edges (
            from_name TEXT, to_name TEXT, rel_type TEXT, strength REAL,
            chapters TEXT, status TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO new_nodes VALUES (?,?,?,?,?,?,?)",
        [
            ("许七安", "character", "", "{}", 900, 12, "active"),
            ("司天监", "faction", "", "{}", 291, 30, "active"),
            ("云州", "faction", "", "{}", 60, 5, "active"),
            ("打更人", "faction", "", "{}", 190, 30, "active"),
            ("秘术", "power_system", "", "{}", 100, 40, "active"),
            ("残卷", "item", "", "{}", 100, 40, "active"),
            ("府尹", "faction", "", "{}", 10, 3, "active"),      # 无后缀且低频 → 丢
            ("云州官场", "faction", "", "{}", 9, 3, "active"),    # 抽象词 → 丢
        ],
    )
    con.executemany(
        "INSERT INTO new_edges VALUES (?,?,?,?,?,?)",
        [
            ("许七安", "司天监", "member_of", 0.8, "1-30", "resolved"),
            ("许七安", "云州", "located_at", 0.7, "1-5", "resolved"),
            ("司天监", "云州", "belongs_to", 0.6, "1-5", "resolved"),
            ("许七安", "秘术", "belongs_to", 0.9, "1-5", "resolved"),  # 非法术域 → 丢
        ],
    )
    con.commit()
    con.close()
    return path


def _set_cleaned(monkeypatch, layers_path: pathlib.Path | None = None) -> None:
    monkeypatch.setattr(settings, "kg_canon_source", "cleaned")
    if layers_path is not None:
        monkeypatch.setenv("KG_CANON_LAYERS", str(layers_path))
    canon_source.reset_cache()


def _set_mixed(monkeypatch, layers_path: pathlib.Path | None = None) -> None:
    monkeypatch.setattr(settings, "kg_canon_source", "mixed")
    if layers_path is not None:
        monkeypatch.setenv("KG_CANON_LAYERS", str(layers_path))
    canon_source.reset_cache()


# ---------------------------------------------------------------------------
# 1. 默认值
# ---------------------------------------------------------------------------
def test_default_source_is_legacy(tmp_path):
    db = _legacy_db(tmp_path)
    assert canon_source.source() == "legacy"

    nodes, edges = canon_source.load(db)
    # legacy 口径 = 原著层全量（含 location 白帝），不做任何称呼层归一
    assert {n["name"] for n in nodes} == {"许七安", "白帝", "魏渊"}
    assert len(edges) == 2
    assert canon_source.normalize("许郎") == "许郎"
    assert canon_source.describe()["effective"] == "legacy"


def test_invalid_source_value_falls_back_to_legacy(monkeypatch, caplog):
    monkeypatch.setattr(settings, "kg_canon_source", "v2")
    assert canon_source.source() == "legacy"
    assert "不是合法取值" in caplog.text


# ---------------------------------------------------------------------------
# 2. cleaned 词表结构
# ---------------------------------------------------------------------------
def test_cleaned_layers_shape():
    layers = canon_source.load_layers()
    assert layers is not None

    names = [e["name"] for e in layers["entities"]]
    assert len(names) == 51
    assert len(names) == len(set(names)), "实体层规范名必须唯一"
    assert {e["kind"] for e in layers["entities"]} == {"character"}

    # 每条称呼层写法都必须指回实体层里存在的规范名
    assert all(a["entity"] in names for a in layers["aliases"])

    # 63 条四类统计（人物专名 / 称谓式别名 / 非人物残留 / 歧义待定）
    assert layers["classification"] == {
        "人物专名": 49, "称谓式别名": 13, "非人物残留": 1, "歧义待定": 0,
    }
    assert [e["name"] for e in layers["excluded"]] == ["白帝"]

    # 每条都要能回答「能不能唯一指向、指向哪个」
    items = layers["source_items"]
    assert len(items) == 63 == len({i["name"] for i in items})
    assert all(i["entity"] in names for i in items if i["entity"])
    assert {i["name"]: i["category"] for i in items}["许郎"] == "称谓式别名"
    assert {i["name"]: i["entity"] for i in items}["许郎"] == "许七安"
    assert {i["name"]: i["unique"] for i in items}["白帝"] is False


def test_pure_titles_never_enter_alias_layer():
    layers = canon_source.load_layers()
    forms = {a["form"] for a in layers["aliases"]}
    # 纯称谓只能走共指消解：大哥/老爷子/师父/夫人/皇帝/银锣/道长
    for title in ("大哥", "老爷", "师父", "师傅", "夫人", "皇帝", "银锣", "道长"):
        assert title not in forms


def test_aliases_point_to_single_entity():
    layers = canon_source.load_layers()
    seen: dict[str, str] = {}
    for row in layers["aliases"]:
        if row.get("in_layer") is False:
            continue
        assert row["form"] not in seen, f"称呼写法重复指向：{row['form']}"
        seen[row["form"]] = row["entity"]
    assert seen["许郎"] == "许七安"
    assert seen["许二叔"] == "许平志"
    assert seen["魏公"] == "魏渊"
    assert seen["临安公主"] == "临安"
    assert seen["白莲道长"] == "白莲"
    # 长公主是唯一破例：满足「唯一原文直证 + 全库唯一归属」，启用且带判据
    chang = next(a for a in layers["aliases"] if a["form"] == "长公主")
    assert chang["in_layer"] is True and chang["entity"] == "怀庆"
    assert chang["title_exception"]["rule"] == "唯一原文直证 + 全库唯一归属"
    assert chang["title_exception"]["unique_owner"] is True
    assert chang["title_exception"]["owner_count"] == 1


def test_no_attrs_marks_entities_without_canon_node():
    layers = canon_source.load_layers()
    no_attrs = {e["name"] for e in layers["entities"] if e.get("no_attrs")}
    assert len(no_attrs) == 20
    assert {"白莲", "孙玄机", "尤尸"} <= no_attrs
    assert "许七安" not in no_attrs

    nodes = canon_source.nodes_from_layers(layers)
    marked = {n["name"] for n in nodes if n.get("no_attrs")}
    assert marked == no_attrs
    assert all(n.get("no_attrs_reason") == "not_in_canon_layer"
               for n in nodes if n.get("no_attrs"))


def test_supplement_entities_have_checkable_evidence():
    layers = canon_source.load_layers()
    sup = {e["name"]: e["supplement"] for e in layers["entities"] if e.get("supplement")}
    assert set(sup) == {"孙玄机", "杨千幻"}
    for name, spec in sup.items():
        assert spec["rule"] == "direct_evidence_and_in_batch_chapter"
        assert spec["evidence"] and name in spec["evidence"]
        assert isinstance(spec["chapter"], int)


# ---------------------------------------------------------------------------
# 3. 降级纪律：cleaned 不可用 → legacy 且非空
# ---------------------------------------------------------------------------
def test_missing_cleaned_file_degrades_to_non_empty_legacy(monkeypatch, caplog, tmp_path):
    db = _legacy_db(tmp_path)
    _set_cleaned(monkeypatch, tmp_path / "does_not_exist.json")

    nodes, edges = canon_source.load(db)
    assert nodes, "cleaned 读不出来时绝不能当空表"
    assert {n["name"] for n in nodes} == {"许七安", "白帝", "魏渊"}
    assert len(edges) == 2
    assert "降级 legacy" in caplog.text
    assert canon_source.describe()["degraded"] is True
    assert canon_source.describe()["effective"] == "legacy"


def test_empty_cleaned_entities_degrades(monkeypatch, caplog, tmp_path):
    bad = tmp_path / "empty.json"
    bad.write_text(json.dumps({"entities": [], "aliases": []}), encoding="utf-8")
    db = _legacy_db(tmp_path)
    _set_cleaned(monkeypatch, bad)

    nodes, _ = canon_source.load(db)
    assert nodes, "entities 为空同样必须降级，不能返回空表"
    assert "降级 legacy" in caplog.text


def test_corrupt_cleaned_json_degrades(monkeypatch, caplog, tmp_path):
    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    db = _legacy_db(tmp_path)
    _set_cleaned(monkeypatch, bad)

    nodes, _ = canon_source.load(db)
    assert nodes
    assert "降级 legacy" in caplog.text


# ---------------------------------------------------------------------------
# 4. cleaned 生效：实体层 + 称呼层 + 边过滤
# ---------------------------------------------------------------------------
def test_cleaned_switches_nodes_and_filters_edges(monkeypatch, tmp_path):
    db = _legacy_db(tmp_path)
    _set_cleaned(monkeypatch)

    nodes, edges = canon_source.load(db)
    names = {n["name"] for n in nodes}
    assert len(nodes) == 51
    assert "白帝" not in names, "非人物残留不得进实体层"
    assert names == {n["name"] for n in canon_source.load_layers()["entities"]}

    # 边只保留两端都在实体层的：许七安—魏渊 留下，许七安—白帝 掉
    assert [(e["from"], e["to"]) for e in edges] == [("许七安", "魏渊")]

    # 称呼层写法进了 aliases，供生成层匹配
    aliases = next(n["aliases"] for n in nodes if n["name"] == "许七安")
    assert "许郎" in aliases and "许银锣" in aliases


def test_layer_context_read_canon_uses_switch(monkeypatch, tmp_path):
    db = _legacy_db(tmp_path)
    _set_cleaned(monkeypatch)

    nodes, _ = layer_context.read_canon(db)
    assert len(nodes) == 51
    assert all(n["kind"] == "character" for n in nodes)


# ---------------------------------------------------------------------------
# 4b. 混合源：人物域 cleaned + 势力/地点域 legacy（各自过滤，不合名单）
# ---------------------------------------------------------------------------
def test_mixed_keeps_characters_and_suffix_filtered_noncharacters(monkeypatch, tmp_path):
    db = _mixed_db(tmp_path)
    _set_mixed(monkeypatch)

    nodes, edges = canon_source.load(db)
    names = {n["name"] for n in nodes}
    # 人物域来自 cleaned 实体层，不是 legacy 的 character 节点
    assert len([n for n in nodes if n["domain"] == "character"]) == 51
    # 势力/地点域必须过结构后缀门；法术/物品/抽象词整体退出
    assert {"司天监", "打更人", "云州"} <= names
    assert not ({"秘术", "残卷", "府尹", "云州官场"} & names)
    assert {n["name"]: n["domain"] for n in nodes
            if n["name"] in {"司天监", "打更人", "云州"}} == {
        "司天监": "faction", "打更人": "faction", "云州": "location",
    }
    # 边要求两端都在并集里：人物—势力、人物—地点、势力—地点都保留
    pairs = {(e["from"], e["to"]) for e in edges}
    assert ("许七安", "司天监") in pairs
    assert ("许七安", "云州") in pairs
    assert ("司天监", "云州") in pairs
    assert all("秘术" not in p and "残卷" not in p for p in pairs)


def test_mixed_records_domain_stats(monkeypatch, caplog, tmp_path):
    db = _mixed_db(tmp_path)
    _set_mixed(monkeypatch)
    caplog.set_level(logging.INFO, logger="app.kg.canon_source")

    info = canon_source.log_effective(path=db, prefix="测试 ")
    assert info["effective"] == "mixed"
    assert info["domains"] == {
        "character": 51, "faction": 2, "location": 1, "filtered_out": 5,
    }
    assert "+" in info["sha256"]
    assert "domains=character:51,faction:2,location:1,filtered:5" in caplog.text


def test_mixed_normalizes_character_aliases(monkeypatch):
    _set_mixed(monkeypatch)
    assert canon_source.normalize("长公主") == "怀庆"
    assert canon_source.normalize("许郎") == "许七安"


def test_mixed_rejects_upper_level_and_low_evidence_venues(monkeypatch):
    _set_mixed(monkeypatch)
    # 集合/上位类：关系会退化成集合关系，直接不要
    for name in ("魏渊阵营", "西方佛门", "西域佛门", "道门天地人三宗", "道门地宗"):
        assert canon_source._nonchar_domain(
            {"name": name, "kind": "faction", "mentions": 100}) is None
    # 具体场所不按类型剔，只按 mentions 阈值（<3 丢，>=3 留）
    assert canon_source._nonchar_domain(
        {"name": "山货铺", "kind": "faction", "mentions": 1}) is None
    assert canon_source._nonchar_domain(
        {"name": "驿站", "kind": "faction", "mentions": 7}) == "location"
    assert canon_source._nonchar_domain(
        {"name": "司天监", "kind": "faction", "mentions": 291}) == "faction"


def test_mixed_drops_collective_races_and_sects(monkeypatch):
    _set_mixed(monkeypatch)
    # 种族/教派集合：关系挂到集合上无法使用，直接剔（与政权/党派分开口径）
    for name in ("妖族", "蛊族", "南疆蛊族", "万妖国", "佛门", "道门", "阉党"):
        assert canon_source._nonchar_domain(
            {"name": name, "kind": "faction", "mentions": 200}) is None
    # 政权/党派照旧保留
    for name in ("大奉王朝", "大奉朝", "齐党", "王党", "梁党"):
        assert canon_source._nonchar_domain(
            {"name": name, "kind": "faction", "mentions": 200}) == "faction"


def test_mixed_keeps_institutions_with_low_evidence_flag(monkeypatch, caplog, tmp_path):
    db = tmp_path / "institution_canon.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE new_nodes (
            canonical_name TEXT, kind TEXT, aliases TEXT, attrs TEXT,
            mentions INTEGER, last_chapter INTEGER, review_status TEXT
        );
        CREATE TABLE new_edges (
            from_name TEXT, to_name TEXT, rel_type TEXT, strength REAL,
            chapters TEXT, status TEXT
        );
        """
    )
    con.executemany(
        "INSERT INTO new_nodes VALUES (?,?,?,?,?,?,?)",
        [
            ("许七安", "character", "", "{}", 900, 12, "active"),
            # 机构类：提及数低也保留，但必须打 low_evidence
            ("京城五卫", "faction", "", "{}", 1, 5, "active"),
            ("禁军五卫", "faction", "", "{}", 2, 5, "active"),
            ("天门", "faction", "", "{}", 1, 5, "active"),
            ("六扇门", "faction", "", "{}", 1, 5, "active"),
            ("卫指挥使司", "faction", "", "{}", 2, 5, "active"),
            # 高证据节点不该被误标
            ("司天监", "faction", "", "{}", 291, 30, "active"),
        ],
    )
    con.commit()
    con.close()
    _set_mixed(monkeypatch)

    nodes, _ = canon_source.load(db)
    by_name = {n["name"]: n for n in nodes}
    institutions = ("京城五卫", "禁军五卫", "天门", "六扇门", "卫指挥使司")
    assert set(institutions) <= set(by_name), "低证据机构只打标，不加 mentions 地板"
    assert all(by_name[n].get("low_evidence") is True for n in institutions)
    assert "low_evidence" not in by_name["司天监"]

    caplog.set_level(logging.INFO, logger="app.kg.canon_source")
    info = canon_source.log_effective(path=db, prefix="测试 ")
    assert info["low_evidence"] == 5
    assert "low_evidence:5" in caplog.text


def test_mixed_degrades_to_legacy_when_layers_missing(monkeypatch, caplog, tmp_path):
    db = _mixed_db(tmp_path)
    _set_mixed(monkeypatch, tmp_path / "missing.json")

    nodes, _ = canon_source.load(db)
    assert nodes, "混合源读不到 cleaned 时也不能当空表"
    assert {"秘术", "残卷"} <= {n["name"] for n in nodes}
    assert "降级 legacy" in caplog.text
    assert canon_source.describe(db)["effective"] == "legacy"


def test_mixed_degrades_to_cleaned_without_canon_db(monkeypatch, caplog):
    _set_mixed(monkeypatch)

    nodes, edges = canon_source.load(None)
    assert len(nodes) == 51, "原著层不可用时保人物域，不能返回空表"
    assert edges == []
    assert "降级 cleaned" in caplog.text
    assert canon_source.describe(None)["effective"] == "cleaned"


# ---------------------------------------------------------------------------
# 5. 称呼层归一与约束
# ---------------------------------------------------------------------------
def test_normalize_maps_alias_to_entity(monkeypatch):
    _set_cleaned(monkeypatch)
    assert canon_source.normalize("许郎") == "许七安"
    assert canon_source.normalize("许公子") == "许七安"
    assert canon_source.normalize("魏公") == "魏渊"
    assert canon_source.normalize("慕白") == "李慕白"
    # 纯称谓、未登记写法原样返回
    assert canon_source.normalize("大哥") == "大哥"
    assert canon_source.normalize("皇帝") == "皇帝"
    # 长公主是带条件破例：唯一原文直证 + 全库唯一归属 → 归一为怀庆
    assert canon_source.normalize("长公主") == "怀庆"


def test_constraint_keeps_whitelotus_out(monkeypatch):
    _set_cleaned(monkeypatch)
    assert canon_source.count_forms("池边开了一朵白莲花", ["白莲"]) == 0
    assert canon_source.count_forms("白莲女道长拂尘一摆", ["白莲"]) == 1
    assert canon_source.count_forms("道号白莲的少妇", ["白莲"]) == 1
    assert canon_source.normalize("白莲") == "白莲"  # 无上下文不硬猜


def test_constraint_keeps_mansion_out(monkeypatch):
    _set_cleaned(monkeypatch)
    assert canon_source.count_forms("平远伯府的门槛", ["平远伯"]) == 0
    assert canon_source.count_forms("平远伯被杀了", ["平远伯"]) == 1


def test_longest_match_first(monkeypatch):
    _set_cleaned(monkeypatch)
    # 许宁宴 不能被拆成 宁宴 + 许
    assert canon_source.count_forms("许宁宴笑了笑", ["许七安", "许宁宴", "宁宴"]) == 1


def test_normalize_extraction_keeps_original_form(monkeypatch):
    _set_cleaned(monkeypatch)
    result = {
        "entities": [{"canonical_name": "许郎", "kind": "character", "aliases": []}],
        "relations": [{"from": "许郎", "to": "许七安", "rel_type": "family"}],
        "events": [{"subject": "许郎", "object": "浮香", "summary": "赎身"}],
    }
    canon_source.normalize_extraction(result)

    ent = result["entities"][0]
    assert ent["canonical_name"] == "许七安"
    assert "许郎" in ent["aliases"], "原文写法必须保留"
    # 关系两端归一后同一人，不会拆成两个节点
    assert result["relations"][0]["from"] == result["relations"][0]["to"] == "许七安"
    assert result["events"][0]["subject"] == "许七安"
    assert result["events"][0]["object"] == "浮香"


def test_legacy_mode_does_not_normalize(monkeypatch):
    result = {
        "entities": [{"canonical_name": "许郎", "kind": "character", "aliases": []}],
        "relations": [{"from": "许郎", "to": "许七安", "rel_type": "family"}],
    }
    canon_source.normalize_extraction(result)
    assert result["entities"][0]["canonical_name"] == "许郎"
    assert result["relations"][0]["from"] == "许郎"


# ---------------------------------------------------------------------------
# 6. 记账：hash 跟内容走，不跟文件名走
# ---------------------------------------------------------------------------
def test_hash_follows_content(monkeypatch, tmp_path):
    original = LAYERS.read_text(encoding="utf-8")
    copy = tmp_path / "canon_layers.json"
    copy.write_text(original, encoding="utf-8")

    first = canon_source.content_hash(copy)
    assert first and first == canon_source.content_hash(LAYERS)

    data = json.loads(original)
    data["entities"][0]["mentions"] += 1
    copy.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert canon_source.content_hash(copy) != first


def test_log_effective_reports_source_and_hash(monkeypatch, caplog):
    _set_cleaned(monkeypatch)
    caplog.set_level(logging.INFO, logger="app.kg.canon_source")
    info = canon_source.log_effective(prefix="测试 ")
    assert info["effective"] == "cleaned"
    assert info["entities"] == 51
    assert info["sha256"] == canon_source.content_hash()
    assert "canon source=cleaned" in caplog.text
    assert "sha256=" in caplog.text
