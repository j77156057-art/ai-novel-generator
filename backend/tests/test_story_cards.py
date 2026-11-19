"""设定卡：纯函数校验/渲染 + 路由直调 + 提示词注入。

跑法（backend 目录）：`python -m pytest tests/test_story_cards.py -q`
只用内存 SQLite，不写生产库、不调模型。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.generation.prompts import build_user_prompt
from app.generation.story_cards import (
    FIELD_SCHEMA,
    MAX_BLOCK_CHARS,
    _bible_character_fields,
    normalize_card_input,
    render_block,
    retire_story_card,
    sync_from_project,
)
from app.models import KgEntity, Project, StoryCard
from app.routers.story_cards import (
    StoryCardCreate,
    StoryCardPatch,
    create_story_card,
    delete_story_card,
    list_story_cards,
    sync_story_cards,
    update_story_card,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _card(kind: str, title: str, fields: dict | None = None,
          enabled: bool = True, sort_order: int = 0, id: int = 0):
    return SimpleNamespace(
        id=id, kind=kind, title=title, fields=fields or {},
        enabled=enabled, sort_order=sort_order,
    )


# ── render_block ──────────────────────────────────────────────────────


def test_render_block_empty_and_disabled_only_return_empty():
    assert render_block([]) == ""
    assert render_block([_card("character", "林晚", enabled=False)]) == ""


def test_render_block_groups_by_kind_and_skips_disabled():
    text = render_block([
        _card("item", "铜钥匙", {"function": "开启西门"}, sort_order=0, id=3),
        _card("character", "林晚", {"identity": "急诊科医生"}, sort_order=0, id=1),
        _card("character", "顾辰逸", {"alias": "顾医生"}, enabled=False, id=2),
        _card("scene", "老城墙", {"location": "城西"}, sort_order=0, id=4),
    ])
    assert "◆ 人物卡" in text
    assert "◆ 场景卡" in text
    assert "◆ 道具卡" in text
    assert text.index("◆ 人物卡") < text.index("◆ 场景卡") < text.index("◆ 道具卡")
    assert "1. 林晚" in text and "- 身份 / 定位：急诊科医生" in text
    assert "顾辰逸" not in text  # 停用卡不出现
    assert "1. 铜钥匙" in text and "- 功能：开启西门" in text


def test_render_block_sorts_by_sort_order_and_skips_empty_fields():
    text = render_block([
        _card("character", "乙", {"notes": ""}, sort_order=2, id=2),
        _card("character", "甲", {"notes": "先出场"}, sort_order=1, id=1),
    ])
    assert text.index("1. 甲") < text.index("2. 乙")
    assert text.count("备注：") == 1  # 乙的空备注不占位


def test_render_block_truncates_with_note():
    huge = "字" * 1000
    cards = [
        _card("character", f"角色{i}", {key: huge for key in
              (s["key"] for s in FIELD_SCHEMA["character"])}, sort_order=i, id=i)
        for i in range(3)
    ]
    text = render_block(cards)
    assert len(text) <= MAX_BLOCK_CHARS + 120
    assert "超出长度上限" in text


def test_render_block_includes_new_kinds_and_custom_fields():
    text = render_block([
        _card("faction", "青云宗", {"leader": "掌门坐镇"}, id=1),
        _card("rule", "御剑术", {"cost": "每息耗气"}, id=2),
        _card("custom", "种族·鲛人", {"栖息地": "深海", "弱点": ""}, id=3),
    ])
    assert "◆ 势力卡" in text and "◆ 规则卡" in text and "◆ 自定义卡" in text
    assert "- 首领 / 核心人物：掌门坐镇" in text
    assert "- 代价 / 限制：每息耗气" in text
    assert "- 栖息地：深海" in text
    assert "弱点" not in text  # 自定义卡的空值不渲染


# ── normalize_card_input ──────────────────────────────────────────────


def test_normalize_rejects_unknown_kind_title_and_field():
    with pytest.raises(ValueError):
        normalize_card_input("monster", "某物", {})
    with pytest.raises(ValueError):
        normalize_card_input("character", "   ", {})
    with pytest.raises(ValueError):
        normalize_card_input("character", "林晚", {"not_a_field": "x"})


def test_normalize_trims_and_drops_empty_fields():
    kind, title, fields = normalize_card_input(
        " character ", " 林晚 ", {"identity": " 医生 ", "notes": "   "},
    )
    assert (kind, title) == ("character", "林晚")
    assert fields == {"identity": "医生"}


def test_normalize_rejects_overlong_field():
    with pytest.raises(ValueError):
        normalize_card_input("character", "林晚", {"notes": "字" * 1001})


def test_normalize_custom_accepts_arbitrary_fields_and_keeps_empty():
    kind, title, fields = normalize_card_input(
        "custom", " 鲛人 ", {"栖息地": "深海", "弱点": ""},
    )
    assert (kind, title) == ("custom", "鲛人")
    assert fields == {"栖息地": "深海", "弱点": ""}


def test_normalize_custom_enforces_limits():
    with pytest.raises(ValueError):
        normalize_card_input("custom", "物种", {f"k{i}": "" for i in range(21)})
    with pytest.raises(ValueError):
        normalize_card_input("custom", "物种", {"k" * 31: ""})
    with pytest.raises(ValueError):
        normalize_card_input("custom", "物种", {"  ": "值"})


# ── 路由 CRUD ─────────────────────────────────────────────────────────


def _seed_project(db) -> Project:
    project = Project(title="设定卡测试", premise="测试")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def test_router_crud_affects_rendered_block(db):
    project = _seed_project(db)

    created = create_story_card(
        project.id,
        StoryCardCreate(kind="character", title="林晚",
                        fields={"identity": "急诊科医生"}),
        db,
    )
    assert created["title"] == "林晚" and created["enabled"] is True

    listed = list_story_cards(project.id, db)
    assert listed["project_id"] == project.id
    assert {item["kind"] for item in listed["kinds"]} == {
        "character", "faction", "scene", "item", "rule", "custom",
    }
    custom_spec = next(
        item for item in listed["kinds"] if item["kind"] == "custom"
    )
    assert custom_spec["freeform"] is True
    assert len(listed["cards"]) == 1

    disabled = update_story_card(
        project.id, created["id"], StoryCardPatch(enabled=False), db,
    )
    assert disabled["enabled"] is False

    from app.generation.story_cards import render_for_project

    assert render_for_project(db, project.id) == ""  # 停用后不进提示

    update_story_card(
        project.id, created["id"],
        StoryCardPatch(enabled=True, fields={"identity": "外科主任"}), db,
    )
    text = render_for_project(db, project.id)
    assert "林晚" in text and "外科主任" in text

    delete_story_card(project.id, created["id"], db)
    assert list_story_cards(project.id, db)["cards"] == []


def test_router_rejects_unknown_field_and_cross_project_card(db):
    project = _seed_project(db)
    other = Project(title="别的项目")
    db.add(other)
    db.commit()

    with pytest.raises(HTTPException) as invalid:
        create_story_card(
            project.id,
            StoryCardCreate(kind="item", title="钥匙", fields={"unknown": "x"}),
            db,
        )
    assert invalid.value.status_code == 422

    card = create_story_card(
        project.id, StoryCardCreate(kind="item", title="钥匙"), db,
    )
    with pytest.raises(HTTPException) as missing:
        update_story_card(other.id, card["id"], StoryCardPatch(title="改名"), db)
    assert missing.value.status_code == 404


# ── 从剧情圣经 / 知识图谱同步 ────────────────────────────────────────


def test_sync_from_project_builds_cards_and_dedupes(db):
    project = Project(
        title="同步测试",
        plot_bible={
            "characters": [{
                "name": "陈小满",
                "role": "跑腿站站长",
                "motivation": "盘下自己的站点",
                "voice": {
                    "sentence": "短句，先报价再给方案",
                    "tics": ["行，我先看看"],
                    "taboo": ["不说包在我身上"],
                    "address": {"周德贵": "周叔"},
                },
                "首次出场章节": 1,
            }],
            "derived_item_names": ["地书碎片"],
        },
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add_all([
        KgEntity(project_id=project.id, kind="character", canonical_name="宋书航",
                 attrs={"current_goal": "参加婚礼", "arc": "炼制本命法器"},
                 mention_count=50),
        KgEntity(project_id=project.id, kind="location", canonical_name="核心世界",
                 attrs={"type": "空间", "description": "主角的修炼场所"},
                 mention_count=30),
        KgEntity(project_id=project.id, kind="item", canonical_name="三十三兽组合法器",
                 attrs={"type": "法器", "held_by": "宋书航"}, mention_count=20),
        KgEntity(project_id=project.id, kind="faction", canonical_name="九洲一号群",
                 attrs={"type": "组织", "members": ["宋书航", "羽柔子"]},
                 mention_count=10),
        KgEntity(project_id=project.id, kind="worldrule", canonical_name="天庭之道",
                 attrs={"system": "天庭之道", "tier": "长生之道"}, mention_count=5),
        # 占位名不该被导成卡片
        KgEntity(project_id=project.id, kind="location", canonical_name="未知",
                 attrs={}, mention_count=999),
    ])
    db.commit()

    first = sync_from_project(db, project)
    db.commit()
    assert first["created"] == 7

    cards = {
        card.title: card
        for card in db.query(StoryCard).filter_by(project_id=project.id).all()
    }
    assert "未知" not in cards
    assert cards["陈小满"].kind == "character"
    assert cards["陈小满"].fields["identity"] == "跑腿站站长"
    assert "短句" in cards["陈小满"].fields["speech"]
    # 剧情圣经没有「性格」字段，但压力反应/行事描述要整理成性格草稿
    assert cards["陈小满"].fields["personality"].startswith("（据已有人设整理）")
    assert "处事与说话" in cards["陈小满"].fields["personality"]
    assert cards["地书碎片"].kind == "item"
    assert cards["核心世界"].kind == "scene"
    assert cards["核心世界"].fields["scene_type"] == "空间"
    assert cards["三十三兽组合法器"].fields["owner"] == "宋书航"
    assert cards["九洲一号群"].kind == "faction"
    assert cards["天庭之道"].kind == "rule"
    assert cards["天庭之道"].fields["tiers"] == "长生之道"

    second = sync_from_project(db, project)
    db.commit()
    assert second["created"] == 0


def test_sync_route_marks_seeded_and_get_reflects_it(db):
    project = Project(
        title="同步路由测试",
        plot_bible={"characters": [{"name": "林晚", "role": "医生"}]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    assert list_story_cards(project.id, db)["seeded"] is False

    result = sync_story_cards(project.id, db)
    assert result["created"] == 1
    assert result["seeded"] is True
    assert list_story_cards(project.id, db)["seeded"] is True
    assert len(list_story_cards(project.id, db)["cards"]) == 1


def test_manual_create_marks_seeded_to_avoid_reimport(db):
    """手动建过卡后，即使全部删空，也不该在下次加载被自动导入填回来。"""
    project = _seed_project(db)
    assert list_story_cards(project.id, db)["seeded"] is False

    create_story_card(
        project.id, StoryCardCreate(kind="item", title="钥匙"), db,
    )
    assert list_story_cards(project.id, db)["seeded"] is True


def test_sync_cap_counts_existing_cards(db):
    """上限按现有卡片总数算：已满的类别不能再被反复同步塞新卡。"""
    project = _seed_project(db)
    db.add_all([
        StoryCard(
            project_id=project.id, kind="character", title=f"角色{i}",
            fields={}, enabled=True, sort_order=i,
        )
        for i in range(22)
    ])
    project.plot_bible = {"characters": [{"name": "新角色", "role": "路人"}]}
    db.commit()

    result = sync_from_project(db, project)
    db.commit()
    assert result["created"] == 0


def test_sync_fills_empty_fields_without_overwriting_edits(db):
    """只补空字段：作者手写的内容不能被同步覆盖。"""
    project = Project(
        title="补齐测试",
        plot_bible={"characters": [{
            "name": "林晚",
            "role": "急诊科医生",
            "voice": {"under_pressure": "越急越冷静"},
        }]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    card = StoryCard(
        project_id=project.id,
        kind="character",
        title="林晚",
        fields={"notes": "作者手写备注"},
        enabled=True,
        sort_order=0,
    )
    db.add(card)
    db.commit()

    result = sync_from_project(db, project)
    db.commit()
    assert result["created"] == 0
    assert result["filled"] == 1

    db.refresh(card)
    assert card.fields["notes"] == "作者手写备注"  # 已有内容保持原样
    assert card.fields["identity"] == "急诊科医生"
    assert "越急越冷静" in card.fields["personality"]

    again = sync_from_project(db, project)
    db.commit()
    assert again["filled"] == 0  # 再同步不会重复"补齐"


# ── 角色卡 → 设定卡智能同步 ──────────────────────────────────────────


def _character(identity: str, self_call: str) -> dict:
    return {
        "name": "林晚",
        "card": {
            "身份": {"身份职业": identity},
            "声纹": {"自称": self_call},
        },
    }


def test_sync_updates_synced_field_but_keeps_hand_edit(db):
    project = Project(
        title="智能合并", plot_bible={"characters": [_character("医生", "我")]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    sync_from_project(db, project)
    db.commit()
    card = db.query(StoryCard).filter_by(project_id=project.id).one()
    assert "自称：我" in card.fields["speech"]

    # 作者在设定卡里手改身份
    card.fields = {**card.fields, "identity": "作者手写身份"}
    # 角色卡重生成：自称与身份都变了
    project.plot_bible = {"characters": [_character("外科主任", "在下")]}
    db.commit()

    result = sync_from_project(db, project)
    db.commit()
    db.refresh(card)
    assert "自称：在下" in card.fields["speech"]      # 未被手改 → 更新
    assert card.fields["identity"] == "作者手写身份"   # 手改 → 保留
    assert result["updated"] == 1 and result["kept"] == 1


def test_sync_with_previous_fields_updates_old_synced_values(db):
    project = Project(
        title="旧基线合并", plot_bible={"characters": [_character("外科主任", "在下")]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    card = StoryCard(
        project_id=project.id, kind="character", title="林晚",
        fields={"speech": "自称：我", "identity": "作者手写身份"},
        sync_snapshot=None, enabled=True, sort_order=0,
    )
    db.add(card)
    db.commit()

    sync_from_project(
        db,
        project,
        previous_fields={"林晚": {"speech": "自称：我", "identity": "医生"}},
    )
    db.commit()
    db.refresh(card)
    assert "自称：在下" in card.fields["speech"]
    assert card.fields["identity"] == "作者手写身份"


def test_sync_new_bible_card_gets_snapshot_kg_card_does_not(db):
    project = Project(
        title="快照范围", plot_bible={"characters": [_character("医生", "我")]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add(KgEntity(project_id=project.id, kind="location", canonical_name="核心世界",
                    attrs={"type": "空间"}, mention_count=10))
    db.commit()

    sync_from_project(db, project)
    db.commit()
    cards = {card.title: card for card in db.query(StoryCard).all()}
    assert cards["林晚"].sync_snapshot
    assert cards["核心世界"].sync_snapshot is None


def test_retire_story_card_delete_disable_and_missing_snapshot(db):
    project = Project(title="移除角色", plot_bible={})
    db.add(project)
    db.commit()
    db.refresh(project)

    untouched = StoryCard(
        project_id=project.id, kind="character", title="未改角色",
        fields={"identity": "医生"}, sync_snapshot={"identity": "医生"},
        enabled=True, sort_order=0,
    )
    edited = StoryCard(
        project_id=project.id, kind="character", title="手改角色",
        fields={"identity": "作者手写"}, sync_snapshot={"identity": "医生"},
        enabled=True, sort_order=1,
    )
    legacy = StoryCard(
        project_id=project.id, kind="character", title="无快照角色",
        fields={"identity": "旧卡"}, sync_snapshot=None,
        enabled=True, sort_order=2,
    )
    db.add_all([untouched, edited, legacy])
    db.commit()

    assert retire_story_card(db, project, "未改角色") == {"deleted": True, "disabled": False}
    assert retire_story_card(db, project, "手改角色") == {"deleted": False, "disabled": True}
    assert retire_story_card(db, project, "无快照角色") == {"deleted": False, "disabled": True}
    db.commit()

    assert db.get(StoryCard, untouched.id) is None
    assert db.get(StoryCard, edited.id).enabled is False
    assert "角色已从剧情圣经移除" in db.get(StoryCard, edited.id).fields["notes"]
    assert db.get(StoryCard, legacy.id).enabled is False


def test_save_character_card_triggers_story_card_sync(db):
    from app.routers.assistant_wizard import CharacterCardIn, update_character_card

    project = Project(
        title="保存即同步", plot_bible={"characters": [_character("医生", "我")]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    sync_from_project(db, project)
    db.commit()
    card = db.query(StoryCard).filter_by(project_id=project.id).one()
    card.fields = {**card.fields, "identity": "作者手写身份"}
    db.commit()

    update_character_card(
        project.id, "林晚",
        CharacterCardIn(card={
            "身份": {"身份职业": "外科主任"},
            "声纹": {"自称": "在下"},
        }),
        db,
    )
    db.refresh(card)
    assert "自称：在下" in card.fields["speech"]
    assert card.fields["identity"] == "作者手写身份"


def test_remove_character_retires_story_card(db):
    from app.routers.assistant_wizard import remove_character

    project = Project(
        title="删除角色同步", plot_bible={"characters": [_character("医生", "我")]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    sync_from_project(db, project)
    db.commit()
    card = db.query(StoryCard).filter_by(project_id=project.id).one()

    out = remove_character(project.id, "林晚", db)
    assert out["story_card"] == {"deleted": True, "disabled": False}
    assert db.get(StoryCard, card.id) is None


# ── 提示词注入 ────────────────────────────────────────────────────────


def test_build_user_prompt_unchanged_without_cards_and_contains_block_with_cards():
    baseline = build_user_prompt([], [], None, 3000)
    assert "【设定卡" not in baseline

    with_cards = build_user_prompt(
        [], [], None, 3000, story_cards_block="◆ 人物卡\n1. 林晚",
    )
    assert "【设定卡" in with_cards
    assert "1. 林晚" in with_cards
    # 卡片是上游：提示必须要求人物言行体现卡里的性格/禁忌
    assert "行为硬约束" in with_cards


def test_bible_character_fields_reads_new_card_groups():
    """角色卡（身份/能力/声纹/量级）要能映射进设定卡字段。"""
    char = {
        "name": "林汐",
        "role": "夜班观测员",
        "motivation": "查清姐姐死因",
        "card": {
            "身份": {"性别": "女", "年龄": "27", "外貌": "旧防风外套",
                     "身份职业": "潮汐观测员", "出身背景": "雾港本地"},
            "能力": {"能力上限": ["不会游泳", "看不懂旧式代码"]},
            "声纹": {"自称": "我", "称呼他人": ["老周"], "句长": "短",
                     "句末": "直陈收尾", "口头禅": ["把原始波形再调出来"],
                     "禁用词": ["确实", "没关系"], "语气底色": "压着声音说"},
            "量级": {"量级": "凡人级", "出手规模": "扳动一台旧阀门"},
            "关系": {"林满": "妹妹；护着她"},
        },
    }
    fields = _bible_character_fields(char)

    assert "潮汐观测员" in fields["identity"]
    assert fields["appearance"] == "旧防风外套"
    assert "自称：我" in fields["speech"]
    assert "口头禅：把原始波形再调出来" in fields["speech"]
    assert "不说：确实、没关系" in fields["taboo"]
    assert "做不到：不会游泳、看不懂旧式代码" in fields["taboo"]
    assert "林满：妹妹；护着她" in fields["relations"]
    assert "压着声音说" in fields["personality"]
    assert "量级：凡人级" in fields["notes"]
