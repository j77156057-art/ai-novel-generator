"""人物塑造模板：原创路径缺的那块「怎么塑造人物」。

要守住的：
1. **零专名** —— 模板会进原创路径的 system 提示，那里参考书的角色不可见；
   带一个专名就等于把参考书的人物漏进用户的新书。
2. **没有专名表就不产** —— 专名表是这条防线的唯一依据；表建不出来时
   宁可少一块方法论，也不要漏。
3. **只给原创路径** —— 续写与角色聊天有真实人物和角色声音词典，不需要它。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402


# ====================================================================
# 1) 抽取：提示词只要方法，解析要宽容
# ====================================================================

def test_the_prompt_asks_for_method_only():
    from app.style.character_template import build_character_template_prompt
    system, user = build_character_template_prompt("正文片段", "语料1 第1章")
    assert "不要写具体人名" in system
    assert "可迁移" in system
    assert "人物塑造模板" in system
    assert "正文片段" in user


def test_parsing_extracts_the_rules():
    from app.style.character_template import parse_character_template
    raw = json.dumps({"人物塑造模板": ["先给动作再给评价", "用旁人的反应侧写主角"]},
                     ensure_ascii=False)
    assert parse_character_template(raw) == ["先给动作再给评价", "用旁人的反应侧写主角"]


@pytest.mark.parametrize('raw', ['', None, '不是 JSON', '{"别的东西": ["x"]}',
                                  '{"人物塑造模板": "不是列表"}'])
def test_parsing_is_forgiving(raw):
    """坏响应 → 当作没有（不能把整次蒸馏带崩）。"""
    from app.style.character_template import parse_character_template
    assert parse_character_template(raw) == []


# ====================================================================
# 2) 过滤：专名一律丢
# ====================================================================

def test_a_rule_naming_a_reference_character_is_dropped():
    from app.style.character_template import entity_free_rules
    rules = ["先给动作再给评价", "徐凤年咳了两声掩饰不舍"]
    kept, dropped = entity_free_rules(rules, ["徐凤年", "北凉军"])
    assert kept == ["先给动作再给评价"]
    assert dropped == 1


def test_a_rule_naming_a_place_or_faction_is_dropped():
    from app.style.character_template import entity_free_rules
    kept, dropped = entity_free_rules(
        ["在太安城这样的地方铺陈", "用旁人的反应侧写主角"],
        ["太安城", "北凉王府"])
    assert kept == ["用旁人的反应侧写主角"]
    assert dropped == 1


def test_long_plot_reciting_and_verbatim_copies_are_dropped():
    from app.style.character_template import entity_free_rules
    long_rule = "用" + "很" * 90 + "的手法塑造人物"
    kept, dropped = entity_free_rules(
        [long_rule, "本章里作者写了很多细节", "医院里她抬起头，看着走廊尽头的光。"],
        ["徐凤年"], chunk_text="医院里她抬起头，看着走廊尽头的光。")
    assert kept == []
    assert dropped == 3


# ====================================================================
# 3) 渲染
# ====================================================================

def test_rendering_is_empty_when_there_is_nothing():
    from app.style.character_template import describe_character_template
    assert describe_character_template(None) == ""
    assert describe_character_template({}) == ""
    assert describe_character_template({"character_template": {"rules": []}}) == ""


def test_rendering_lists_the_rules():
    from app.style.character_template import describe_character_template
    text = describe_character_template(
        {"character_template": {"rules": ["先给动作再给评价", "用旁人的反应侧写主角"]}})
    assert "先给动作再给评价" in text
    assert "用旁人的反应侧写主角" in text


# ====================================================================
# 4) 注入：只给原创路径
# ====================================================================

def _system(profile: str, narrative: dict) -> str:
    from app.generation.prompts import build_system_prompt
    return build_system_prompt("测试", {}, None, None, None, narrative, profile=profile)


def test_the_original_path_gets_the_template_block():
    narrative = {"character_template": {"rules": ["用旁人的反应侧写主角"]}}
    assert "用旁人的反应侧写主角" in _system("original", narrative)


@pytest.mark.parametrize('profile', ['continuation', 'roleplay'])
def test_the_other_paths_do_not_get_it(profile):
    """续写与角色聊天有真实人物和角色声音词典，不需要这块。"""
    narrative = {"character_template": {"rules": ["用旁人的反应侧写主角"]}}
    assert "用旁人的反应侧写主角" not in _system(profile, narrative)


# ====================================================================
# 5) 端到端：蒸馏产出 → 专名过滤 → 进档案
# ====================================================================

class _TemplateProvider:
    """假 provider：对人物塑造模板的请求返回两条规则（一条带专名）。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system: str, user: str) -> str:
        self.calls += 1
        if "人物塑造模板" in system:
            return json.dumps({"人物塑造模板": [
                "先给动作再给评价，让读者自己得出结论",
                "徐凤年在江边咳了两声，掩饰不舍",
            ]}, ensure_ascii=False)
        return '{"占位": ["占位规则（证据：原文里根本没有这句话）"]}'

    async def async_chat(self, system: str, user: str, constraints=None) -> str:
        # 蒸馏走的是并发 gather（_gather_chat → provider.async_chat）
        return self.chat(system, user)


def _corpus_and_project(db, chapters: int = 6):
    from app.models import NovelChunk, Project, RawNovel
    novel = RawNovel(title="模板测试语料", status="imported")
    db.add(novel)
    db.commit()
    db.refresh(novel)
    for i in range(chapters):
        db.add(NovelChunk(
            novel_id=novel.id, chapter_no=i + 1, idx=i,
            text="医院里她抬起头，看着走廊尽头的光。风从门缝里挤进来。"))
    project = Project(title="模板测试项目", target_length=3000, generation_length=1000,
                      mode="quick", reference_novel_ids=[novel.id])
    db.add(project)
    db.commit()
    db.refresh(project)
    return novel.id, project


def test_the_extraction_filters_entity_names_out(monkeypatch):
    from app.style import entity_store
    from app.style.narrative import extract_character_template
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db import Base
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        nid, project = _corpus_and_project(db)
        # 专名表：这一本里有「徐凤年」和「太安城」
        monkeypatch.setattr(entity_store, 'ensure_table',
                            lambda db_, novel_id, **kw: {
                                "characters": ["徐凤年"], "places": ["太安城"],
                                "factions": [], "items": [],
                            })
        row = extract_character_template(db, project, [nid], provider=_TemplateProvider())
        rules = row.rules
        assert "先给动作再给评价，让读者自己得出结论" in rules
        assert not any("徐凤年" in r for r in rules), "带专名的规则不能进产物"
    finally:
        db.close()


def test_without_an_entity_table_no_template_is_produced(monkeypatch):
    """专名表建不出来 → 整块不产（宁可没有，不要泄漏）。"""
    from app.style import entity_store
    from app.style.narrative import extract_character_template
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db import Base
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        nid, project = _corpus_and_project(db)
        monkeypatch.setattr(entity_store, 'ensure_table', lambda *a, **k: {})
        row = extract_character_template(db, project, [nid], provider=_TemplateProvider())
        assert row.rules == []
    finally:
        db.close()


# ====================================================================
# 6) 按档位分开存放：id 自带档位；各档位互不覆盖
# ====================================================================

def test_the_product_id_says_which_tier_it_is():
    from app.style.character_template import template_id
    assert template_id('full', '9a3f1166f4e6', '1f2a3b4c5d') == 'full:9a3f1166f4e6:1f2a3b4c'
    assert template_id('fast', '9a3f1166f4e6', '1f2a3b4c5d').startswith('fast:')


def test_different_tiers_get_different_rows():
    """不同档位 → 不同 id → 不同行。跑一档**不能**把另一档的产物顶掉。"""
    from app.style.character_template import save_template, find_template
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db import Base
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        nid, project = _corpus_and_project(db)
        full = save_template(db, project, tier='full', tier_signature='sig1',
                             content_hash='c1', source_ids=[nid], source_ids_hash='h1',
                             rules=['完整档的做法'])
        fast = save_template(db, project, tier='fast', tier_signature='sig2',
                             content_hash='c2', source_ids=[nid], source_ids_hash='h1',
                             rules=['快速档的做法'])
        assert full.id != fast.id
        assert full.tier == 'full' and fast.tier == 'fast'
        assert find_template(db, tier='full', tier_signature='sig1',
                             content_hash='c1').id == full.id
        assert find_template(db, tier='fast', tier_signature='sig2',
                             content_hash='c2').id == fast.id
        # 重抽同一档 → 原地更新，不长出第二行
        again = save_template(db, project, tier='full', tier_signature='sig1',
                              content_hash='c1', source_ids=[nid], source_ids_hash='h1',
                              rules=['完整档的做法（改了）'])
        assert again.id == full.id
        from app.models import CharacterTemplate
        assert db.query(CharacterTemplate).count() == 2
    finally:
        db.close()


def test_extracting_the_template_does_not_invalidate_the_archive():
    """**抽模板不必重蒸**：主档案的档位签名不含模板 → 旧档案照旧命中。"""
    from app.style.narrative import _tier_signature, resolve_tier
    payload = resolve_tier('full').fingerprint_payload()
    assert 'character_template' not in payload, "模板开关进了指纹就会作废旧档案"
    # 模板是**另一张表**里的另一份产物，与档案指纹无关
    from app.style.character_template import template_id
    assert template_id('full', _tier_signature(resolve_tier('full')), 'abc12345') \
        .startswith('full:')


def test_a_template_of_another_tier_is_not_injected():
    """档位不一致就不注入：拿 A 档的方法论配 B 档的档案是静默的不一致。"""
    from app.style.character_template import applied_template_rules, save_template
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.db import Base
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        nid, project = _corpus_and_project(db)
        row = save_template(db, project, tier='fast', tier_signature='s', content_hash='c',
                            source_ids=[nid], source_ids_hash='h', rules=['快速档的做法'])
        project.character_template_id = row.id
        db.commit()
        assert applied_template_rules(db, project, 'fast') == ['快速档的做法']
        assert applied_template_rules(db, project, 'full') == [], '档位不符就不给'
    finally:
        db.close()
