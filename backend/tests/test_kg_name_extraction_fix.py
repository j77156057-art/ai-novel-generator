"""v2.6 名字截断/误判修复的定向回归。

覆盖三类问题（2026-11-19）：
1. 3 字真名被 _NAME_CHARS 拒后回退成 2 字前缀（宋书航 → 宋书）；
2. 2 字常见词被当成角色（时间/空间/平台/费用/后台…）；
3. 作者在剧情圣经/设定卡里声明的角色（陈小满/顾青梧）不在人工名用字表内。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.kg.build import _known_character_names, merge_segment_into_kg
from app.kg.extractor import RuleBasedExtractor, _leading_name, _valid_name
from app.models import KgNode, Project, StoryCard


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


NOISE = [
    "时间", "空间", "平台", "费用", "后台", "毕竟", "边上", "上车",
    "后来", "索性", "东侧", "石碑", "长生", "诸天", "金丹", "黄山",
]


def test_common_words_are_not_names():
    for word in NOISE:
        assert not _valid_name(word), word


def test_author_declared_names_pass_even_with_unusual_tail():
    for name in ("陈小满", "顾青梧", "冯保国", "张一鸣", "李一凡"):
        assert not _valid_name(name), name  # 形态判据本身不认
        assert _valid_name(name, known_names=frozenset({name})), name


def test_leading_name_no_longer_truncates_three_char_names():
    cases = {
        "宋书航看了他一眼。": "宋书航",
        "白前辈说道：“走。”": "白前辈",
        "陈小满把那块铜牌放在柜台上。": "陈小满",
        "顾青梧没有接他的玩笑。": "顾青梧",
        "周叔知道这件事。": "周叔",
        "宋前辈说道：“走。”": "宋前辈",
        "时间不早了。": None,
        "万界修士来了。": None,
        "苏氏阿十六站在门口。": None,
    }
    for sentence, expected in cases.items():
        assert _leading_name(sentence) == expected, sentence


def test_object_position_does_not_keep_verb_tail():
    chunks = [
        SimpleNamespace(chapter_no=1, text="他看见李浩回头。"),
        SimpleNamespace(chapter_no=2, text="他看见李浩微笑。"),
        SimpleNamespace(chapter_no=3, text="李浩说道：“走。”"),
    ]
    chars = RuleBasedExtractor().extract(chunks).characters

    assert "李浩" in chars
    assert "李浩回" not in chars
    assert "李浩微" not in chars


def test_three_char_name_is_not_reduced_to_prefix():
    chunks = [
        SimpleNamespace(chapter_no=1, text="宋书航看了他一眼。"),
        SimpleNamespace(chapter_no=2, text="宋书航说道：“走。”"),
        SimpleNamespace(chapter_no=3, text="周叔知道这件事。"),
        SimpleNamespace(chapter_no=4, text="周叔说道：“知道了。”"),
    ]
    chars = RuleBasedExtractor().extract(chunks).characters

    assert "宋书航" in chars
    assert "宋书" not in chars
    assert "周叔" in chars
    assert "周叔知" not in chars


def test_called_name_keeps_verb_like_tail():
    """「叫X」是作者显式命名：末字像动词也不能截断。

    反例保底：「叫陆见说道」仍要按粘连剥成「陆见」——两件事不能互相破坏。
    """
    def extract(text: str) -> set[str]:
        chunks = [SimpleNamespace(chapter_no=n, text=text) for n in (1, 2)]
        return set(RuleBasedExtractor().extract(chunks).characters)

    real = extract("他叫王守道：“好。”")
    assert "王守道" in real
    assert "王守" not in real

    glued = extract("他叫陆见说道：“好。”")
    assert "陆见" in glued
    assert "陆见说" not in glued


def test_known_names_loader_reads_bible_and_story_cards(db):
    project = Project(
        title="白名单测试",
        plot_bible={"characters": [
            {"name": "陈小满", "aliases": ["小满"]},
            {"name": "顾青梧"},
        ]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    db.add(StoryCard(
        project_id=project.id, kind="character", title="冯保国",
        fields={"alias": "老冯、冯叔"},
    ))
    db.commit()

    known = _known_character_names(db, project.id)

    assert {"陈小满", "小满", "顾青梧", "冯保国", "老冯", "冯叔"} <= known


def test_merge_segment_uses_author_whitelist(db):
    project = Project(
        title="合并白名单测试",
        plot_bible={"characters": [{"name": "陈小满"}]},
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    merge_segment_into_kg(
        db, project.id, "陈小满说道：“先别急。”", 1, source="generated",
    )

    names = {
        (n.props or {}).get("name")
        for n in db.scalars(
            select(KgNode).where(
                KgNode.project_id == project.id,
                KgNode.type == "character",
                KgNode.source == "generated",
            )
        ).all()
    }
    assert "陈小满" in names
