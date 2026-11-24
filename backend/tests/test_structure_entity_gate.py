"""结构参考闸门、专名表精度与生成路径 profile 开关的回归测试。"""
from __future__ import annotations

import re
from types import SimpleNamespace

from app.generation.engine import _rag_enabled_for_project, _style_exemplars_for_profile
from app.generation.plot_structure import for_project, structure_rules
from app.style import entity_store
from app.style.entities import Redactor, drop_fragments

# ── 残片判据 ─────────────────────────────────────────────────────────────


def _repeat(word: str, times: int) -> str:
    return word * times


def test_drop_fragments_drops_same_layer_prefix_fragment():
    """「名字 + 一两个字」的同层残片要摘掉（宋书航笑）。"""
    text = _repeat("宋书航", 100) + _repeat("宋书航笑", 2)
    table = {"characters": ["宋书航", "宋书航笑"],
             "places": [], "factions": [], "items": []}
    kept, dropped = drop_fragments(table, text)
    assert "宋书航" in kept["characters"]
    assert "宋书航笑" not in kept["characters"]
    assert "宋书航笑" in dropped["characters"]


def test_drop_fragments_keeps_cross_layer_compound():
    """赤霄剑（物件）→ 赤霄剑道（门派）是不同实体，不能按前缀当残片。"""
    text = _repeat("赤霄剑", 100) + _repeat("赤霄剑道", 10)
    table = {"characters": [], "places": [],
             "factions": ["赤霄剑道"], "items": ["赤霄剑"]}
    kept, _dropped = drop_fragments(table, text)
    assert "赤霄剑道" in kept["factions"]


def test_drop_fragments_drops_particle_edge():
    """以虚词开头/结尾的候选不是专名（宋书航的 / 个道号）。"""
    text = _repeat("宋书航", 50) + "宋书航的" + "个道号"
    table = {"characters": ["宋书航", "宋书航的", "个道号"],
             "places": [], "factions": [], "items": []}
    kept, dropped = drop_fragments(table, text)
    assert kept["characters"] == ["宋书航"]
    assert set(dropped["characters"]) == {"宋书航的", "个道号"}


def test_drop_fragments_keeps_legit_compound():
    """出现率和短名相当的复合名要保留（九幽世界）。"""
    text = _repeat("九幽", 100) + _repeat("九幽世界", 30)
    table = {"characters": [], "places": ["九幽", "九幽世界"],
             "factions": [], "items": []}
    kept, _dropped = drop_fragments(table, text)
    assert "九幽" in kept["places"]
    assert "九幽世界" in kept["places"]


# ── 结构规则闸门 ─────────────────────────────────────────────────────────


def _profile(rules: list) -> dict:
    return {"method": {"剧情编排模式": rules}}


def test_structure_rules_gates_known_names_and_fragments():
    redactor = Redactor({"characters": ["宋书航"]})
    rules = [
        {"rule": "用宋书航的视角推进冲突", "novel_id": 7},   # 已知专名 → 占位符 → 丢
        {"rule": "用九幽龙魔线双线并行", "novel_id": 7},     # 残片名 → 丢
        {"rule": "先铺垫再反转", "novel_id": 7},             # 干净 → 留
        {"rule": "来自没有专名表的那本书", "novel_id": 6},   # 缺表 → 丢
        {"rule": "没有来源字段的规则"},                      # 无 novel_id → 丢
        "纯字符串规则",                                      # 非 dict → 丢
    ]
    out = structure_rules(
        _profile(rules),
        redactors={7: redactor},
        fragment_patterns={7: re.compile("九幽")},
    )
    assert out["剧情编排模式"] == ["先铺垫再反转"]


def test_structure_rules_without_redactors_does_not_gate():
    """不传 redactors 时保持旧语义（仅做提取，供 for_project 预检）。"""
    out = structure_rules(_profile([{"rule": "用宋书航的视角推进", "novel_id": 7}]))
    assert out["剧情编排模式"] == ["用宋书航的视角推进"]


def test_for_project_fails_closed_without_table(monkeypatch):
    monkeypatch.setattr(entity_store, "load_table", lambda _nid: {})
    project = SimpleNamespace(
        style_narrative=_profile([{"rule": "先铺垫再反转", "novel_id": 7}]),
        reference_novel_ids=[7],
    )
    picked, note = for_project(None, project, "主线")
    assert picked == {}
    assert "专名表" in note


def test_fragment_names_only_takes_judged_proper_nouns(monkeypatch):
    """残片集只收「判成专名」的词；泛称与「名字+虚词」片段都要排除。"""
    table = {"dropped_fragments": {
        "characters": ["九幽主", "师兄", "个道号"],
        "places": ["九幽"],
        "factions": [],
        "items": [],
    }}
    monkeypatch.setattr(
        entity_store, "load_verdicts",
        lambda _nid: {"九幽主": "character", "九幽": "place",
                      "师兄": None, "个道号": "character"},
    )
    assert entity_store.fragment_names(7, table) == {"九幽主", "九幽"}


# ── 生成路径的 profile 开关 ───────────────────────────────────────────────


def test_rag_disabled_on_original_path():
    project = SimpleNamespace(story_authority="generated_branch")
    assert _rag_enabled_for_project(project) is False


def test_rag_enabled_on_anchored_path():
    project = SimpleNamespace(story_authority="reference_anchor")
    assert _rag_enabled_for_project(project) is True


def test_exemplars_redacted_on_original_path(monkeypatch):
    monkeypatch.setattr(
        entity_store, "load_table",
        lambda _nid: {"characters": ["宋书航"], "places": [], "factions": [], "items": []},
    )
    exemplars = [{"novel_id": 7, "text": "宋书航笑了笑。"}]
    out = _style_exemplars_for_profile(
        SimpleNamespace(story_authority="generated_branch"), exemplars, [7]
    )
    assert len(out) == 1
    assert "宋书航" not in out[0]["text"]
    assert out[0]["redacted"] is True


def test_exemplars_raw_on_anchored_path():
    exemplars = [{"novel_id": 7, "text": "宋书航笑了笑。"}]
    out = _style_exemplars_for_profile(
        SimpleNamespace(story_authority="reference_anchor"), exemplars, [7]
    )
    assert out == exemplars


def test_exemplars_dropped_when_book_has_no_table(monkeypatch):
    monkeypatch.setattr(entity_store, "load_table", lambda _nid: {})
    exemplars = [{"novel_id": 7, "text": "宋书航笑了笑。"}]
    out = _style_exemplars_for_profile(
        SimpleNamespace(story_authority="generated_branch"), exemplars, [7]
    )
    assert out == []
