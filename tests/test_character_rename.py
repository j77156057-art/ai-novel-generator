"""角色改名入口：影响面阈值 + 别名 / 实体联动（2026-11-11）。

## 被钉住的口径

用户定的规则：**影响前文的改名不允许，只在角色刚出场、影响不大时允许**。

- 判定源是 `generations.text`（段级正文，唯一全文事实源）；`kg_mentions`
  只是抽取索引，不能当正文用。
- 允许：从未在正文出现；或只出现在最后一段且次数 ≤ 3。
- 拒绝：跨 ≥ 2 段；或最后一段次数 > 3。
- 拒绝不许只说"不行"：回执必须带命中段号 / 次数 + 替代路径。

## 阈值边界（p25=2 / p50=4 的参考语料依据见实现模块 docstring）

- 恰好 3 次 → 允许（卡在阈值上）
- 4 次 → 拒绝（刚好多一次）
- 只跨两段、每段 1 次 → 拒绝（刚好多一段）

## 实体层

允许改名时同步 `kg_entities.canonical_name` 并把旧名写进 aliases；老段重抽
若再次产出旧名，`_upsert_entity` 的精确 alias 匹配必须并回同一实体，不能
和新名并存。
"""

from __future__ import annotations

import hashlib
import os
import sys

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db  # noqa: E402
from app.generation.prompts import build_system_prompt  # noqa: E402
from app.kg.batch_worker import _upsert_entity  # noqa: E402
from app.kg.character_rename import (  # noqa: E402
    MAX_LAST_SEGMENT_HITS,
    _count_name,
    scan_name_scope,
)
from app.main import app as fastapi_app  # noqa: E402
from app.models import Generation, KgBibleRevision, KgEntity, KgMention, Project  # noqa: E402

BIBLE = {
    "synopsis": "少年拾得黑铁钥匙。",
    "characters": [
        {"name": "林夜", "role": "主角", "motivation": "查清钥匙来历"},
        {"name": "老李", "role": "配角"},
    ],
    "relationships": [{"a": "林夜", "b": "老李", "relation": "师徒"}],
    "foreshadowing": ["黑铁钥匙的来历"],
}

REMINDERS = {
    "character_constraints": [{"name": "林夜", "mentions": 5}],
    "unresolved_plotholes": [{"question": "黑铁钥匙的来历"}],
}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _client_and_factory(bible: dict | None = None):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        db.add(Project(id=1, title="改名测试项目", plot_bible=bible or BIBLE))
        db.commit()

    def _override():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _override
    return TestClient(fastapi_app), factory


def _seed_generations(factory, texts: list[tuple[int, str]]):
    with factory() as db:
        for seg, text in texts:
            db.add(Generation(project_id=1, segment_no=seg, text=text,
                              summary={"text": text[:20]}))
        db.commit()


def _rename(client, old: str, new: str):
    return client.post("/api/projects/1/bible/characters/rename", json={
        "old_name": old, "new_name": new,
    })


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    fastapi_app.dependency_overrides.clear()


# ======================================================================
# 三种情形：刚出场 / 跨多段 / 从未出现
# ======================================================================
def test_never_in_text_allows_unconditionally():
    client, factory = _client_and_factory()
    _seed_generations(factory, [(1, "老李在渡口等了一夜，什么也没等到。")])
    r = _rename(client, "林夜", "林砚")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["scope"]["total"] == 0
    assert "没有在任何已生成正文里出现过" in body["receipt"]
    with factory() as db:
        proj = db.get(Project, 1)
        names = [c["name"] for c in proj.plot_bible["characters"]]
        assert names == ["林砚", "老李"]
        # 关系端点也同步，避免 bible 内部指向一个不存在的人
        assert proj.plot_bible["relationships"][0]["a"] == "林砚"
        assert db.scalar(select(func.count(KgBibleRevision.id))) == 1


def test_last_segment_only_within_threshold_allows():
    """恰好卡在阈值上（3 次）：允许。"""
    client, factory = _client_and_factory()
    _seed_generations(factory, [
        (1, "老李在渡口等了一夜。"),
        (2, "林夜推门进来。林夜看了看四周。老李说：林夜，坐。"),
    ])
    r = _rename(client, "林夜", "林砚")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["scope"]["total"] == 3
    assert body["scope"]["only_last_segment"] is True
    assert body["scope"]["last_segment"] == 2

    # 名字真的换了：system 里出现新名，旧名不再出现（KG 旧角色被清册过滤）
    with factory() as db:
        bible = db.get(Project, 1).plot_bible
    before = build_system_prompt("改名测试项目", REMINDERS, {"类型": "仙侠"}, BIBLE)
    after = build_system_prompt("改名测试项目", REMINDERS, {"类型": "仙侠"}, bible)
    assert "林砚" in after and "林夜" not in after
    assert _md5(before) != _md5(after)


def test_name_forms_do_not_distort_count():
    """带称谓算本人；被更长的**另一个角色名**包住不算本人。"""
    assert _count_name("林夜兄来了。林夜兄走了。", "林夜", set()) == 2
    assert _count_name("李浩微进门。李浩看了他一眼。", "李浩", {"李浩微"}) == 1

    client, factory = _client_and_factory({
        "characters": [
            {"name": "李浩", "role": "主角"},
            {"name": "李浩微", "role": "配角"},
        ],
        "relationships": [],
    })
    # 最后一个（也是唯一）段：李浩微 3 次，李浩本人 1 次 → 短名只算 1 次，允许
    _seed_generations(factory, [
        (1, "李浩微进门。李浩微坐下。李浩微笑了。李浩看了他一眼。"),
    ])
    body = _rename(client, "李浩", "李浩然").json()
    assert body["ok"] is True, body
    assert body["scope"]["total"] == 1


def test_last_segment_over_threshold_is_denied():
    """刚好多一次（4 次）：拒绝，且不改库。"""
    client, factory = _client_and_factory()
    _seed_generations(factory, [
        (1, "老李在渡口等了一夜。"),
        (2, "林夜推门。林夜坐下。老李看着林夜。林夜没说话。"),
    ])
    r = _rename(client, "林夜", "林砚")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["decision"] == "denied"
    assert body["scope"]["total"] == 4
    assert "第 2 段" in body["receipt"] and "4 次" in body["receipt"]
    assert "上限" in body["receipt"]
    assert len(body["alternatives"]) == 3
    with factory() as db:
        proj = db.get(Project, 1)
        assert proj.plot_bible["characters"][0]["name"] == "林夜"
        assert db.scalar(select(func.count(KgBibleRevision.id))) == 0


def test_multi_segment_is_denied_with_readable_numbers():
    """跨 2 段：拒绝，回执要能让人看懂（段号区间 + 总次数）。"""
    client, factory = _client_and_factory()
    _seed_generations(factory, [
        (1, "林夜出门。" * 5),
        (2, "林夜回来。" * 12),
    ])
    r = _rename(client, "林夜", "林砚")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["scope"]["total"] == 17
    assert body["scope"]["segments"] == [1, 2]
    assert "第 1–2 段" in body["receipt"]
    assert "17 次" in body["receipt"]
    assert "前文对不上" in body["receipt"]
    rewrite = [a for a in body["alternatives"] if a.startswith("重写")]
    assert rewrite and "第 1–2 段" in rewrite[0]


def test_one_more_segment_rejected_even_with_one_hit_each():
    """刚好多一段：两段各 1 次也必须拒绝（段数优先于次数）。"""
    client, factory = _client_and_factory()
    _seed_generations(factory, [
        (1, "林夜推门进来。"),
        (2, "老李抬头看了林夜一眼。"),
    ])
    r = _rename(client, "林夜", "林砚")
    body = r.json()
    assert body["ok"] is False
    assert body["scope"]["total"] == 2
    assert len(body["scope"]["segments"]) == 2


# ======================================================================
# 实体层：改名 + 别名 + 旧名回流
# ======================================================================
def test_entity_renamed_and_old_name_kept_as_alias():
    client, factory = _client_and_factory()
    _seed_generations(factory, [(1, "老李在渡口等了一夜。")])
    with factory() as db:
        ent = KgEntity(project_id=1, kind="character", canonical_name="林夜",
                       aliases=["小林"], attrs={"role": "主角"})
        db.add(ent)
        db.flush()
        db.add(KgMention(project_id=1, entity_id=ent.id, segment_no=9,
                         context_window="林夜按住钥匙", mention_kind="llm"))
        db.commit()
        ent_id = ent.id

    r = _rename(client, "林夜", "林砚")
    assert r.json()["ok"] is True
    with factory() as db:
        ent = db.get(KgEntity, ent_id)
        assert ent.canonical_name == "林砚"
        assert "林夜" in ent.aliases
        assert "小林" in ent.aliases
        # mention 挂的是实体 id，改名不丢历史提及
        assert db.scalar(select(func.count(KgMention.id))) == 1

    # 老段重抽只会产出旧名：必须并回同一实体，不新建
    with factory() as db:
        got = _upsert_entity(db, 1, {"kind": "character", "canonical_name": "林夜"})
        db.commit()
        assert got.id == ent_id
        assert db.scalar(select(func.count(KgEntity.id))) == 1


def test_rename_does_not_touch_other_entity():
    client, factory = _client_and_factory()
    _seed_generations(factory, [(1, "老李在渡口等了一夜。")])
    with factory() as db:
        db.add_all([
            KgEntity(project_id=1, kind="character", canonical_name="林夜"),
            KgEntity(project_id=1, kind="character", canonical_name="老李",
                     aliases=["李伯"]),
        ])
        db.commit()

    assert _rename(client, "林夜", "林砚").json()["ok"] is True
    with factory() as db:
        laoli = db.scalar(select(KgEntity).where(KgEntity.canonical_name == "老李"))
        assert laoli is not None and laoli.aliases == ["李伯"]


# ======================================================================
# 参数 / 冲突
# ======================================================================
def test_missing_bible_character_is_400():
    client, _ = _client_and_factory()
    r = _rename(client, "不存在", "某人")
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]


def test_duplicate_new_name_in_bible_is_400():
    client, _ = _client_and_factory()
    r = _rename(client, "林夜", "老李")
    assert r.status_code == 400
    assert "已经有叫「老李」" in r.json()["detail"]


def test_kg_name_collision_is_400_and_keeps_bible():
    client, factory = _client_and_factory()
    _seed_generations(factory, [(1, "老李在渡口等了一夜。")])
    with factory() as db:
        db.add(KgEntity(project_id=1, kind="character", canonical_name="林砚"))
        db.commit()

    r = _rename(client, "林夜", "林砚")
    assert r.status_code == 400
    assert "已经有叫「林砚」" in r.json()["detail"]
    with factory() as db:
        assert db.get(Project, 1).plot_bible["characters"][0]["name"] == "林夜"
        assert db.scalar(select(func.count(KgBibleRevision.id))) == 0


def test_scope_counts_summary_but_does_not_gate():
    """摘要命中只报告：正文没出现 → 允许，但回执提醒摘要里还有旧名。"""
    client, factory = _client_and_factory()
    with factory() as db:
        db.add(Generation(project_id=1, segment_no=1, text="老李在渡口等了一夜。",
                          summary={"text": "林夜去了渡口。"}))
        db.commit()
    with factory() as db:
        scope = scan_name_scope(db, 1, "林夜")
        assert scope["total"] == 0
        assert scope["summary_occurrences"] == 1
        assert scope["threshold"] == MAX_LAST_SEGMENT_HITS
    body = _rename(client, "林夜", "林砚").json()
    assert body["ok"] is True
    assert "还有 1 处旧名" in body["receipt"]
