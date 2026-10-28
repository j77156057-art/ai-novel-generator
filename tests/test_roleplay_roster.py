"""角色对话的三处修法（2026-10-28 用户报的问题）。

1. **角色清单混进不是人的** —— 项目组改用 `kg_entities`（归一化实体表），
   不用 `kg_nodes`（抽取器的原始产物，模型把「水下」「边缘」也建成了角色节点）。
2. **切换角色还在同一个对话框** —— 前端按角色分组（`RoleplayPanel.vue`）。
3. **问「你的性格怎么样」答跑偏** —— 证据只给「他自己开过口的行」，
   且提示词禁掉第三人称人物小传。

## 为什么这些要有测试

问题①是**取错了数据源**（不是判断失误）—— 这种错没有测试就会再犯：
换个人照着旧代码读一遍，又会去拿 `kg_nodes`。
问题③的判据（哪一行算「他自己开过口」）是机械的、可复算的 ⇒ 必须钉住。
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "backend"))

from app.db import Base, get_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
import app.models  # noqa: E402,F401
from app.models import KgEntity, Project  # noqa: E402
from app.roleplay.evidence import (  # noqa: E402
    RoleplaySource,
    classify_origin,
    render_evidence_block,
)
from app.roleplay.roster import is_author_talk, project_characters  # noqa: E402


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

    fastapi_app.dependency_overrides[get_db] = override_get_db
    client = TestClient(fastapi_app)
    db = Session()
    db.add(Project(id=1, title="角色清单测试项目"))
    # 实体表里**个个是人**
    db.add(KgEntity(project_id=1, kind="character", canonical_name="沈砚",
                    aliases=["小子", "他爹"], mention_count=4))
    db.add(KgEntity(project_id=1, kind="character", canonical_name="周郎中",
                    aliases=["老头"], mention_count=3))
    db.add(KgEntity(project_id=1, kind="location", canonical_name="水门", mention_count=47))
    db.commit()
    try:
        yield db, client
    finally:
        fastapi_app.dependency_overrides.pop(get_db, None)
        db.close()


# ── 问题①：角色清单 ──────────────────────────────────────────────────────
def test_author_talk_markers_do_not_hit_real_characters():
    """强作者信号不能误伤真人。

    实测（docs/roleplay-character-list-and-answer-quality.md §1.3）：
    《北荒刀歌》「肖锵」是真角色，规则文本里也含「读者」——
    所以**不能用「读者」这种泛词**，只能用强信号（月票/请假/上架感言…）。
    """
    assert is_author_talk("肖锵：自称'肖某'，语气圆滑老练（证据：肖某就没那脸皮）") is False
    # 作者跟读者说话的那条 ⇒ 命中
    assert is_author_talk(
        "能力： 自称「我」 口头禅「嗯、当然」……向读者道歉请假，说本月身体不适，更新慢"
    ) is True
    # 强信号
    assert is_author_talk("白银：……向读者道歉请假，说本月身体不适") is True
    assert is_author_talk("危险：……上架感言") is True


def test_project_characters_uses_normalized_entities(env):
    """项目组角色**只**来自 kg_entities（canonical + aliases）。

    噪声的来源是 kg_nodes（抽取器的原始产物）。这条测试守住「别再回去拿 kg_nodes」。
    """
    db, _client = env
    names, aliases = project_characters(db, 1)
    assert names == ["沈砚", "周郎中"]
    # 「水门」是 location，不是角色 ⇒ 不该出现在清单里
    assert "水门" not in names


def test_project_characters_skips_pronominal_aliases(env):
    """「他爹」「那小子」是**指代**，不是别名 —— 不该显示出来。"""
    db, _client = env
    _names, aliases = project_characters(db, 1)
    assert aliases["沈砚"] == ["小子"]
    assert "他爹" not in aliases["沈砚"]


def test_character_list_endpoint_returns_aliases_and_no_junk(env):
    """端点：项目组全是人 + 带别名；location 类实体不进清单。"""
    _db, client = env
    resp = client.get("/api/projects/1/roleplay/characters")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["project"] == ["沈砚", "周郎中"], data["project"]
    assert data["aliases"]["沈砚"] == ["小子"]
    assert "水门" not in data["characters"]


# ── 问题③：证据只给他自己的行 ──────────────────────────────────────────
def test_firsthand_evidence_lists_his_own_words():
    """「他亲历」的证据要**列出他亲口说过的话**。

    实测（库里 roleplay_messages #8）：那段 952 字里老夫子只说了两三句，
    其余全是苏酥在说话、旁白在描述。整段喂给模型，模型就用第三人称点评他
    （"老夫子么，读书人的脾气…"）。

    ⇒ 证据块里除了原文，还要单独列出「你亲口说过的话」，
    模型据此知道自己的原话是什么（提示词规矩 4 也指向它）。
    """
    text = (
        "苏酥-到了狭小阴暗的灶房，将鲤鱼丢到砧板上。\n"
        "老夫子忍不住破戒说道：“卖技艺给贩夫走卒，成何体统！”\n"
        "苏酥斜眼看了木讷汉子和横眉竖眼的老夫子，无奈道：“贩夫走卒咋了，就不是人了？”\n"
        "老夫子一拍桌子，道：“荒诞！”\n"
    )
    src = RoleplaySource(
        chunk_id=1, chapter_no=290, idx=0, text=text,
        origin="亲历", basis="这段里他自己开过口", character="老夫子",
    )
    block = render_evidence_block([src])
    assert "你亲口说过的话" in block
    assert "卖技艺给贩夫走卒" in block      # 他自己的话被单独列出
    assert "荒诞" in block
    # 原文仍在（不裁剪 —— 裁剪会把别人的话混进来）
    assert "到了狭小阴暗的灶房" in block


def test_evidence_block_says_it_is_excerpt_not_biography():
    """证据块要说明它的性质：**书里的片段，不是你的传记**。

    实测：模型把「别人在谈论老夫子」的段落当成了他的素材 ⇒ 用第三人称写小传。
    """
    src = RoleplaySource(
        chunk_id=9, chapter_no=290, idx=0, text="老夫子站在门槛外头，眼神慈祥。",
        origin="亲历", basis="这段里他自己开过口", character="老夫子",
    )
    block = render_evidence_block([src])
    assert "不是你的传记" in block
    assert "别人在说话" in block


def test_heard_evidence_keeps_whole_text():
    """「在场听到」要保留整段 —— 那段的要点本来就是「他听见了别人说什么」。"""
    text = "陆长风苦笑道：“老周，你给我说说，这剑匣里有几把剑？”"
    src = RoleplaySource(
        chunk_id=2, chapter_no=20, idx=0, text=text,
        origin="在场听到", basis="台词里叫到了他", character="老周",
    )
    block = render_evidence_block([src])
    assert "老周，你给我说说" in block


def test_only_mentioned_tier_is_rendered_distinctly():
    """「只是被提到」要标出来 —— 它不代表他在场、也不代表他知道。"""
    src = RoleplaySource(
        chunk_id=3, chapter_no=981, idx=0,
        text="薛宋官缓缓抬起头，看着不知何时来到身边的年迈儒士。",
        origin="只是被提到", basis="文里提到了他，但这段里没认出他的对白",
        character="老夫子",
    )
    block = render_evidence_block([src])
    assert "只是被提到" in block


def test_origin_mentioned_is_not_the_same_as_present():
    """「文里提到他」不等于「他在场」—— 三档来源。"""
    origin, _ = classify_origin("程白霜缓缓抬起头，看着老夫子离去的背影。", "老夫子")
    assert origin == "只是被提到"
    origin, _ = classify_origin("苏酥道：“老夫子，你快些。”", "老夫子")
    assert origin == "在场听到"
    origin, _ = classify_origin("老夫子怒极，颤声道：“好一个丧家犬！”", "老夫子")
    assert origin == "亲历"


def test_prompt_forbids_third_person_character_profile():
    """提示词必须禁掉「人物小传」式的自我点评。

    实测（#8）：用户问「你的性格怎么样」，模型答成
    「老夫子么，读书人的脾气，未必好，却也不是一味蛮横」—— 第三人称 + 人物小传。
    """
    from app.roleplay.chat import SYSTEM_TEMPLATE

    assert "人物小传" in SYSTEM_TEMPLATE
    assert "第一人称" in SYSTEM_TEMPLATE
    # 旧的那条「不要总结自己的性格」会把「问性格」这个问题变成死路
    assert "不要总结自己的性格" not in SYSTEM_TEMPLATE
