"""KG v3.0 前端集成所需的读侧端点测试。

覆盖：
- judge findings 落库/读回（save_judge_findings / load_judge_findings）
  + 同段重跑先清后写 + 异段保留
- engine._merge_segment_into_graph 判定后落库（静态检查）
- 新端点：DELETE /exemptions（撤销豁免，Judge 恢复报告）
  GET /judge/findings（含 exempted 标记 + rules 清单）
  GET /characters（mention 降序 + has_secrets 标记）
  GET /relations（strength 降序 + 两端名解析 + 自环丢弃）

（这份原本是「脚本式断言」—— check() 只记数不抛，跑挂了也会显示通过。
 已改写成 pytest 用例：失败即抛，断言消息带标签订位。）
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
import app.models  # noqa: F401
from app.main import app
from app.kg.judge import save_judge_findings, load_judge_findings
from app.models import (
    Generation, KgEntity, KgEntityKnowledge, KgEvent, KgForeshadow,
    KgMention, KgRelation, Project,
)

FINDINGS_A = [
    {"rule": "teleportation", "severity": "err", "entity": "林悠然",
     "message": "瞬移了", "evidence": ["state@1: 办公室"]},
    {"rule": "secret_leak", "severity": "warn", "entity": "顾辰逸",
     "message": "说出秘密", "evidence": ["secret@2: 父亲并非因病去世"]},
]


@pytest.fixture()
def env():
    """一套干净的内存库 + TestClient（依赖覆盖指向该库）。"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    db = Session()
    db.add(Project(id=1, title="前端集成测试项目"))
    db.add(Generation(project_id=1, segment_no=1, text="测试段", summary=None))
    db.commit()
    try:
        yield db, client
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


# ====================================================================
# 1. judge findings 落库/读回
# ====================================================================
def test_findings_are_saved_and_read_back(env):
    db, _ = env
    n = save_judge_findings(db, 1, FINDINGS_A, segment_no=5)
    assert n == 2, f"落库 2 条: got={n}"
    items = load_judge_findings(db, 1)
    assert len(items) == 2, f"读回 2 条: got={len(items)}"
    assert items[0]["rule"] == "teleportation", "读回 rule"
    assert items[0]["severity"] == "err", "读回 severity"
    assert items[0]["segment_no"] == 5, "读回 segment"
    assert "state@1" in items[0]["evidence"][0], "读回 evidence"


def test_same_segment_rerun_replaces_instead_of_accumulating(env):
    db, _ = env
    save_judge_findings(db, 1, FINDINGS_A, segment_no=5)
    save_judge_findings(db, 1, [
        {"rule": "foreshadow_expiry", "severity": "warn", "entity": "伏笔A",
         "message": "超期", "evidence": []},
    ], segment_no=5)
    items = load_judge_findings(db, 1)
    assert len(items) == 1, f"同段重跑替换（不累积）: got={len(items)}"
    assert items[0]["rule"] == "foreshadow_expiry", "同段重跑后是新规则"


def test_different_segments_coexist(env):
    db, _ = env
    save_judge_findings(db, 1, FINDINGS_A, segment_no=5)
    save_judge_findings(db, 1, [
        {"rule": "foreshadow_expiry", "severity": "warn", "entity": "伏笔A",
         "message": "超期", "evidence": []},
    ], segment_no=5)
    save_judge_findings(db, 1, FINDINGS_A, segment_no=6)
    items = load_judge_findings(db, 1)
    assert len(items) == 3, f"异段保留（2 段共存）: got={len(items)}"
    assert items[0]["segment_no"] == 6, "按段号倒序（段6 在前）"


# ====================================================================
# 2. GET /judge/findings 端点（含 exempted 标记 + rules）
# ====================================================================
def test_findings_endpoint_shape(env):
    db, client = env
    # 段5 一条 + 段6 两条 = 共 3 条（异段共存）
    save_judge_findings(db, 1, [
        {"rule": "foreshadow_expiry", "severity": "warn", "entity": "伏笔A",
         "message": "超期", "evidence": []},
    ], segment_no=5)
    save_judge_findings(db, 1, FINDINGS_A, segment_no=6)
    r = client.get("/api/projects/1/kg-v3/judge/findings")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 3, f"findings count: got={data['count']}"
    assert len(data["rules"]) == 6, "findings rules 清单 6 条"
    assert data["by_severity"]["warn"] >= 1, "findings by_severity 有 warn"
    assert all(not i["exempted"] for i in data["items"]), "findings 初始未豁免"


def test_exemption_marks_finding_as_exempted(env):
    db, client = env
    save_judge_findings(db, 1, FINDINGS_A, segment_no=5)
    client.post("/api/projects/1/kg-v3/exemptions", json={
        "rule": "secret_leak", "entity": "顾辰逸", "note": "故意让他说漏嘴"})
    data = client.get("/api/projects/1/kg-v3/judge/findings").json()
    leak_items = [i for i in data["items"] if i["rule"] == "secret_leak"]
    assert leak_items and leak_items[0]["exempted"] is True, "豁免后 findings 带 exempted 标记"


# ====================================================================
# 3. DELETE /exemptions 撤销豁免
# ====================================================================
def test_exemption_can_be_revoked(env):
    db, client = env
    save_judge_findings(db, 1, FINDINGS_A, segment_no=5)
    client.post("/api/projects/1/kg-v3/exemptions", json={
        "rule": "secret_leak", "entity": "顾辰逸", "note": "故意让他说漏嘴"})
    r = client.delete("/api/projects/1/kg-v3/exemptions?rule=secret_leak&entity=顾辰逸")
    assert r.status_code == 200
    assert r.json()["revoked"] == 1, "撤销计数"
    ex = client.get("/api/projects/1/kg-v3/exemptions").json()["exemptions"]
    assert len(ex) == 0, "撤销后豁免清单空"
    data = client.get("/api/projects/1/kg-v3/judge/findings").json()
    assert all(not i["exempted"] for i in data["items"]), "撤销后 exempted 恢复 False"


def test_revoking_unknown_exemption_does_not_break(env):
    _, client = env
    r = client.delete("/api/projects/1/kg-v3/exemptions?rule=nothing&entity=")
    assert (r.status_code, r.json()["revoked"]) == (200, 0), "撤销不存在不炸"


# ====================================================================
# 4. GET /characters
# ====================================================================
def _seed_characters(db):
    ly = KgEntity(project_id=1, kind="character", canonical_name="林悠然",
                  attrs={"secrets": ["母亲之死"]}, mention_count=7)
    gy = KgEntity(project_id=1, kind="character", canonical_name="顾辰逸", mention_count=3)
    office = KgEntity(project_id=1, kind="location", canonical_name="办公室", mention_count=99)
    db.add_all([ly, gy, office])
    db.flush()
    from app.models import KgCharacterState  # noqa: E402

    db.add(KgCharacterState(project_id=1, character_id=ly.id, segment_no=1,
                            state_type="location", new_value="办公室"))
    db.add(KgEntityKnowledge(project_id=1, entity_id=ly.id, known_entity_id=office.id,
                             known_kind="entity", learned_at_chapter=1))
    db.commit()
    return ly, gy, office


def test_slice_prompt_includes_character_state_and_knowledge(env):
    db, client = env
    _seed_characters(db)
    # 回归：曾因端点不接 characters 参数，prompt 文本永远缺这两块（浏览器冒烟抓到）
    r = client.get("/api/projects/1/kg-v3/slice/prompt?segment_no=5&characters=林悠然")
    assert r.status_code == 200
    assert "角色当前状态" in r.json()["text"], "prompt 含角色当前状态块"
    assert "角色已知情报" in r.json()["text"], "prompt 含角色已知情报块"
    assert "办公室" in r.json()["text"], "prompt 含办公室情报"


def test_characters_endpoint_only_lists_characters(env):
    db, client = env
    _seed_characters(db)
    r = client.get("/api/projects/1/kg-v3/characters")
    assert r.status_code == 200
    chars = r.json()["characters"]
    assert [c["name"] for c in chars] == ["林悠然", "顾辰逸"], "characters 只含角色（无 location）"
    assert chars[0]["mention_count"] == 7, "characters mention 降序"
    assert chars[0]["has_secrets"] is True and chars[1]["has_secrets"] is False, "has_secrets 标记"


# ====================================================================
# 5. GET /relations
# ====================================================================
def test_relations_endpoint(env):
    db, client = env
    ly, gy, office = _seed_characters(db)
    db.add_all([
        KgRelation(project_id=1, from_id=ly.id, to_id=gy.id, rel_type="romantic", strength=0.9),
        KgRelation(project_id=1, from_id=gy.id, to_id=office.id, rel_type="located_at", strength=0.4),
        KgRelation(project_id=1, from_id=office.id, to_id=office.id, rel_type="located_at", strength=1.0),
    ])
    db.commit()
    r = client.get("/api/projects/1/kg-v3/relations")
    assert r.status_code == 200
    rels = r.json()["relations"]
    assert len(rels) == 2, f"relations 2 条（自环丢弃，location 边保留）: got={len(rels)}"
    assert rels[0]["rel_type"] == "romantic", "relations strength 降序首条"
    assert rels[1]["rel_type"] == "located_at", "relations strength 降序次条"
    assert rels[0]["from"] == "林悠然", "relations from 解析"
    assert rels[0]["to"] == "顾辰逸", "relations to 解析"
    assert rels[0]["strength"] == 0.9, "relations strength"


def test_missing_project_returns_404(env):
    _, client = env
    assert client.get("/api/projects/999/kg-v3/characters").status_code == 404
    assert client.get("/api/projects/999/kg-v3/relations").status_code == 404
    assert client.get("/api/projects/999/kg-v3/judge/findings").status_code == 404
    assert client.delete("/api/projects/999/kg-v3/exemptions?rule=x").status_code == 404


# ====================================================================
# 6. engine 落库接入（静态检查）
# ====================================================================
def test_engine_calls_save_judge_findings():
    eng = (BACKEND / "app" / "generation" / "engine.py").read_text(encoding="utf-8")
    assert "save_judge_findings" in eng, "engine 引 save_judge_findings"
