"""角色编辑三入口 + B4 关系键归一 + B5 境界/量级双写。

被钉住的口径：
- 加角色：写 characters[]，新卡必须过封闭键表（未知 / 不支持键 400）；
  新角色没有正文出场，total=0，后续改名无条件允许。
- 删角色：删到空必须保留 `characters=[]`（B3 的空清册语义 = 本项目没有角色，
  提示词剔除所有旧 KG 角色），不能删掉整个键退回「未宣告、不过滤」。
- 改境界：写 `characters[].境界`（卡里已有 `修炼.当前境界` 时写那里），
  走 power 块；realm_order 配了就必须在阶梯里。
- B4：relationships 的 `relation` / `type` / `rel_type` / `关系` 四种写法命中
  同一处冲突；端点 a/b、from/to 也等价。
- B5：境界与量级同时存在时两者都要渲染，保质顺序「境界「A」（量级 B，seq n/15）」。
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.generation.power_prompt import build_power_constraint  # noqa: E402
from app.generation.prompts import build_system_prompt  # noqa: E402
from app.kg.bible import detect_conflicts  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import KgEntity, KgRelation, Project  # noqa: E402

BIBLE = {
    "synopsis": "少年拾得黑铁钥匙。",
    "characters": [
        {"name": "林夜", "role": "主角", "motivation": "查清钥匙来历"},
        {"name": "顾辰逸", "role": "配角"},
    ],
    "relationships": [{"a": "林夜", "b": "顾辰逸", "relation": "同门"}],
    "realm_order": ["炼气", "筑基", "金丹"],
}

REMINDERS = {"character_constraints": [{"name": "林夜", "mentions": 5}]}


def _client_and_factory(bible: dict | None = None):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        db.add(Project(id=1, title="角色编辑测试项目", plot_bible=bible or BIBLE))
        db.commit()

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _override
    return TestClient(fastapi_app), factory


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    fastapi_app.dependency_overrides.clear()


def _post(client, path: str, body: dict):
    return client.post(f"/api/projects/1/bible/characters/{path}", json=body)


def _bible(factory) -> dict:
    with factory() as db:
        return db.get(Project, 1).plot_bible


# ======================================================================
# 加角色
# ======================================================================
def test_add_character_writes_roster():
    client, factory = _client_and_factory()
    r = _post(client, "add", {
        "card": {"name": "沈砚", "role": "配角", "背景": "北境来的刀客"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["roster_size"] == 3
    assert body["bible"]["revision_no"] == 1
    bible = _bible(factory)
    assert [c["name"] for c in bible["characters"]] == ["林夜", "顾辰逸", "沈砚"]
    assert bible["characters"][2]["背景"] == "北境来的刀客"


def test_add_rejects_known_unsupported_key():
    client, _ = _client_and_factory()
    r = _post(client, "add", {"card": {"name": "沈砚", "功法": "太上忘情诀"}})
    assert r.status_code == 400
    assert "不会被使用" in r.json()["detail"]
    assert "功法" in r.json()["detail"]


def test_add_rejects_unregistered_key():
    client, _ = _client_and_factory()
    r = _post(client, "add", {"card": {"name": "沈砚", "favorite_food": "面"}})
    assert r.status_code == 400
    assert "favorite_food" in r.json()["detail"]


def test_add_duplicate_name_is_400():
    client, _ = _client_and_factory()
    r = _post(client, "add", {"card": {"name": "林夜", "role": "主角"}})
    assert r.status_code == 400
    assert "已经有叫「林夜」" in r.json()["detail"]


def test_new_character_rename_allowed_with_zero_coverage():
    client, factory = _client_and_factory()
    assert _post(client, "add", {"card": {"name": "沈砚", "role": "配角"}}).status_code == 200
    r = client.post("/api/projects/1/bible/characters/rename", json={
        "old_name": "沈砚", "new_name": "沈砚之",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scope"]["total"] == 0
    assert "沈砚之" in [c["name"] for c in _bible(factory)["characters"]]


# ======================================================================
# 删角色
# ======================================================================
def test_remove_character_and_its_relationships():
    client, factory = _client_and_factory()
    r = _post(client, "remove", {"name": "林夜"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roster_size"] == 1
    assert body["roster_empty"] is False
    assert body["relationships_removed"] == 1
    bible = _bible(factory)
    assert [c["name"] for c in bible["characters"]] == ["顾辰逸"]
    assert bible["relationships"] == []


def test_remove_last_character_keeps_empty_roster_and_filters_kg():
    client, factory = _client_and_factory()
    assert _post(client, "remove", {"name": "林夜"}).status_code == 200
    r = _post(client, "remove", {"name": "顾辰逸"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roster_empty"] is True
    bible = _bible(factory)
    # 键必须在、值是空数组：这是「本项目没有角色」，不是「未宣告」。
    assert bible["characters"] == []

    prompt = build_system_prompt("测试项目", REMINDERS, {"类型": "仙侠"}, bible)
    assert "林夜" not in prompt
    assert "暂无锁定角色" in prompt


def test_remove_missing_character_is_400():
    client, _ = _client_and_factory()
    r = _post(client, "remove", {"name": "不存在"})
    assert r.status_code == 400
    assert "没有「不存在」" in r.json()["detail"]


# ======================================================================
# 改境界
# ======================================================================
def test_set_realm_writes_bible_field_and_power_block():
    client, factory = _client_and_factory()
    r = _post(client, "realm", {"name": "林夜", "realm": "金丹"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["field"] == "境界"
    assert body["validated"] is True

    bible = _bible(factory)
    assert bible["characters"][0]["境界"] == "金丹"

    block = build_power_constraint(SimpleNamespace(plot_bible=bible))
    assert "境界「金丹」" in block

    system = build_system_prompt(
        "测试项目", {}, {"类型": "仙侠"}, bible, power_constraint=block,
    )
    assert "境界「金丹」" in system


def test_set_realm_rejects_value_outside_realm_order():
    client, factory = _client_and_factory()
    r = _post(client, "realm", {"name": "林夜", "realm": "元婴"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "不在本项目的 realm_order" in detail
    assert "炼气、筑基、金丹" in detail
    assert "元婴" not in [c.get("境界") for c in _bible(factory)["characters"]]


def test_set_realm_updates_cultivation_when_card_uses_it():
    bible = {
        **BIBLE,
        "characters": [
            {"name": "林夜", "修炼": {"当前境界": "炼气"}},
            {"name": "顾辰逸"},
        ],
    }
    client, factory = _client_and_factory(bible)
    r = _post(client, "realm", {"name": "林夜", "realm": "筑基"})
    assert r.status_code == 200, r.text
    assert r.json()["field"] == "修炼.当前境界"
    card = _bible(factory)["characters"][0]
    assert card["修炼"]["当前境界"] == "筑基"
    assert "境界" not in card


def test_set_realm_without_realm_order_is_accepted_but_not_validated():
    bible = {k: v for k, v in BIBLE.items() if k != "realm_order"}
    client, factory = _client_and_factory(bible)
    r = _post(client, "realm", {"name": "林夜", "realm": "自定义境界"})
    assert r.status_code == 200, r.text
    assert r.json()["validated"] is False
    assert "无法校验" in r.json()["receipt"]


# ======================================================================
# B5：境界与量级双写
# ======================================================================
def test_b5_realm_and_scale_both_rendered_with_realm_first():
    bible = {
        "characters": [{
            "name": "林砚", "境界": "金丹", "量级": "B", "量级序号": 3,
            "出手规模": "一城",
        }],
    }
    block = build_power_constraint(SimpleNamespace(plot_bible=bible))
    assert "境界「金丹」" in block
    assert "量级 B" in block
    assert block.index("境界「金丹」") < block.index("量级 B")
    assert "一城" in block


def test_b5_scale_only_unchanged_and_realm_only_marks_unknown():
    scale_only = build_power_constraint(SimpleNamespace(
        plot_bible={"characters": [{"name": "林砚", "量级": "B"}]},
    ))
    assert "· 林砚＝B" in scale_only
    assert "· 林砚＝境界" not in scale_only

    realm_only = build_power_constraint(SimpleNamespace(
        plot_bible={"characters": [{"name": "林砚", "境界": "金丹"}]},
    ))
    assert "境界「金丹」（量级 unknown" in realm_only


# ======================================================================
# B4：关系键归一（四种写法 + 两种端点写法）
# ======================================================================
def _seed_relation(factory):
    with factory() as db:
        a = KgEntity(project_id=1, kind="character", canonical_name="林夜")
        b = KgEntity(project_id=1, kind="character", canonical_name="顾辰逸")
        db.add_all([a, b])
        db.flush()
        db.add(KgRelation(
            project_id=1, from_id=a.id, to_id=b.id,
            rel_type="同门", status="active",
        ))
        db.commit()


@pytest.mark.parametrize("type_key", ["relation", "type", "rel_type", "关系"])
def test_b4_all_type_spellings_hit_same_conflict(type_key):
    _, factory = _client_and_factory()
    _seed_relation(factory)
    with factory() as db:
        conflicts = detect_conflicts(db, 1, {"relationships": [
            {"a": "林夜", "b": "顾辰逸", type_key: "仇敌"},
        ]})
    assert len(conflicts) == 1, conflicts
    assert conflicts[0]["kind"] == "relationship_type"
    assert conflicts[0]["bible_value"] == "仇敌"


@pytest.mark.parametrize("endpoints", [{"a": "林夜", "b": "顾辰逸"},
                                       {"from": "林夜", "to": "顾辰逸"}])
def test_b4_endpoint_spellings_are_equivalent(endpoints):
    _, factory = _client_and_factory()
    _seed_relation(factory)
    with factory() as db:
        conflicts = detect_conflicts(db, 1, {"relationships": [
            {**endpoints, "relation": "仇敌"},
        ]})
    assert len(conflicts) == 1, conflicts


def test_b4_matching_type_has_no_conflict():
    _, factory = _client_and_factory()
    _seed_relation(factory)
    with factory() as db:
        conflicts = detect_conflicts(db, 1, {"relationships": [
            {"a": "林夜", "b": "顾辰逸", "relation": "同门"},
        ]})
    assert conflicts == []
