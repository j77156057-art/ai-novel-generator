"""规划层必须真的拿到「作者怎么搭故事」的知识 —— 且不夹带参考书专名。"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from app.db import Base, get_db
from app.models import Project
from app.routers import projects, wizard

RULES = {
    '剧情编排模式': [
        {'rule': '以悬念开场，先给结果再倒推起因（证据：某人夜里翻墙）'},
        {'rule': '用误会串起三场戏，让误会自己解开（证据：另一段）'},
    ],
    '节拍序列': [
        {'rule': '铺垫→升温；来客提出安排→主人预先铺垫→目标者拒绝或僵持→僵持被第三方打断（证据：一句）'},
        {'rule': '静→动→静；先立规矩再破规矩→代价显现→默认回到规矩但已不同（证据：另一句）'},
    ],
    '高频场景模板': [
        {'rule': '贵客抵达→开中门并铺陈仪仗→以小利诱引对方（证据：徐凤年进了府）'},
    ],
    '伏笔与悬念设计': [
        {'rule': '把答案放在场景的边缘物件上，读者看见但主角没注意（证据：一句）'},
    ],
    '语序编排': [{'rule': '先环境后人物（证据：另一句）'}],
    '角色声音词典': [{'rule': '徐凤年：自称「本世子」（证据：一句）'}],
}


def _project(db, **kw):
    p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True},
                **kw)
    db.add(p)
    db.commit()
    return p


def test_only_structure_dims_and_evidence_stripped():
    from app.generation.plot_structure import STRUCTURE_DIMS, structure_rules
    out = structure_rules({'rules': RULES})
    assert set(out) <= set(STRUCTURE_DIMS)
    assert '语序编排' not in out and '角色声音词典' not in out
    for items in out.values():
        for text in items:
            assert '（证据' not in text


def test_selection_prefers_lexically_related_rules_and_is_deterministic():
    from app.generation.plot_structure import select, structure_rules
    rules = structure_rules({'rules': RULES})
    picked = select(rules, '雨夜有人来敲门，主人不肯开门', per_dim=1)
    # 与上下文重合最高的那条应被选中（第二条提到「拒绝」「僵持」）
    assert '僵持' in picked['节拍序列'][0]
    again = select(rules, '雨夜有人来敲门，主人不肯开门', per_dim=1)
    assert again == picked


def test_fails_closed_without_a_usable_entity_table(tmp_path, monkeypatch):
    """没有专名表 → 结构规则可能含参考书专名 → 一律不给。"""
    monkeypatch.setenv('ENTITY_CACHE_DIR', str(tmp_path / 'entities'))
    from app.generation.plot_structure import for_project
    db = Session(create_engine('sqlite://', poolclass=StaticPool))
    Base.metadata.create_all(db.bind)
    p = _project(db, style_narrative={'rules': RULES}, reference_novel_ids=[1])
    picked, note = for_project(db, p, '随便什么上下文')
    assert picked == {}
    assert 'fail closed' in note or '专名表' in note


def test_redacts_reference_names_when_a_table_exists(tmp_path, monkeypatch):
    monkeypatch.setenv('ENTITY_CACHE_DIR', str(tmp_path / 'entities'))
    from app.style.entity_store import save_table
    save_table(1, {'characters': ['徐凤年'], 'places': ['横水城'], 'factions': [],
                   'items': [], 'version': 2})
    from app.generation.plot_structure import for_project
    db = Session(create_engine('sqlite://', poolclass=StaticPool))
    Base.metadata.create_all(db.bind)
    p = _project(db, style_narrative={'rules': RULES}, reference_novel_ids=[1])
    picked, note = for_project(db, p, '雨夜有人来敲门，主人不肯开门')
    assert picked, note
    blob = json.dumps(picked, ensure_ascii=False)
    assert '徐凤年' not in blob, '参考书专名不能进规划提示'
    assert '角色声音词典' not in blob


def test_outline_prompt_carries_the_structure_section(tmp_path, monkeypatch):
    monkeypatch.setenv('ENTITY_CACHE_DIR', str(tmp_path / 'entities'))
    from app.style.entity_store import save_table
    save_table(1, {'characters': ['徐凤年'], 'places': [], 'factions': [], 'items': [],
                   'version': 2})
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        app = FastAPI()
        app.include_router(projects.router)
        app.include_router(wizard.router)
        app.dependency_overrides[get_db] = lambda: db
        p = _project(db, style_narrative={'rules': RULES}, reference_novel_ids=[1])
        p.plot_bible = {'synopsis': '雨夜查旧档', 'direction': '揭穿旧案'}
        db.commit()
        seen = []

        class Writer:
            def chat(self, system, user, **kw):
                seen.append(user)
                return json.dumps({'nodes': [
                    {'title': f'核对{i}', 'premise': '记录矛盾', 'motivation': '澄清责任',
                     'action': '核对原件', 'result': '确定差错', 'cost': 'NA', 'time': '03:00',
                     'location': '档案室', 'participants': ['沈砚'],
                     'state_before': '差错未明', 'state_after': '差错已定位'}
                    for i in range(3)]}, ensure_ascii=False)

        monkeypatch.setattr(wizard, 'get_llm_provider', lambda: Writer())
        client = TestClient(app)
        r = client.post(f'/api/projects/{p.id}/generate-outline',
                        json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                              'preview': True})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data['status'] in {'outline_draft', 'clarification_required'}
        assert data['plot_structure_note'], data
        payload = json.loads(seen[0])
        assert payload['plot_structure'], '规划提示里必须带剧情结构参考'
        assert '徐凤年' not in seen[0]
