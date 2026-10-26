"""机制级校验：学到结构可以，照搬具体情节不行。"""
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

RULE = '贵客抵达→开中门并铺陈仪仗→以糖葫芦诱引小王爷，宾主在席间推手。'
RULES = {'高频场景模板': [RULE], '节拍序列': ['铺垫→升温；来客提出安排→主人预先铺垫→目标者拒绝或僵持']}


def test_verbatim_copy_is_flagged():
    from app.generation.plot_structure import copy_check
    node = {'action': '他学着话本里的排场：贵客抵达→开中门并铺陈仪仗→以糖葫芦诱引小王爷。'}
    report = copy_check(RULES, [node], window=12)
    assert report['verdict'] == 'possible_copy'
    assert report['match_count'] >= 1
    assert report['matches'][0]['dimension'] == '高频场景模板'


def test_structural_reuse_without_copying_is_clean():
    """按同样的结构排戏（自己的人物、自己的细节）不该被判成照搬。"""
    from app.generation.plot_structure import copy_check
    node = {'action': '他先在门口站定，等对方先开口，才把手里那卷东西推过去。'}
    report = copy_check(RULES, [node], window=12)
    assert report['verdict'] == 'clean'
    assert report['match_count'] == 0


def test_outline_response_reports_the_copy_check(tmp_path, monkeypatch):
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
        p = Project(title='测试', premise='雨夜查旧档', outline=[],
                    plot_timeline={'sentinel': True}, reference_novel_ids=[1],
                    style_narrative={'rules': RULES})
        p.plot_bible = {'synopsis': '雨夜查旧档', 'direction': '揭穿旧案'}
        db.add(p)
        db.commit()

        class Writer:
            def chat(self, system, user, **kw):
                return json.dumps({'nodes': [
                    {'title': f'核对{i}', 'premise': '记录矛盾', 'motivation': '澄清责任',
                     'action': '他先在门口站定，等对方先开口。', 'result': '确定差错', 'cost': 'NA',
                     'time': '03:00', 'location': '档案室', 'participants': ['沈砚'],
                     'state_before': '差错未明', 'state_after': '差错已定位'}
                    for i in range(3)]}, ensure_ascii=False)

        monkeypatch.setattr(wizard, 'get_llm_provider', lambda: Writer())
        client = TestClient(app)
        r = client.post(f'/api/projects/{p.id}/generate-outline',
                        json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                              'preview': True})
        assert r.status_code == 200, r.text
        data = r.json()
        check = data['structure_copy_check']
        assert check['verdict'] == 'clean', check
        assert check['window'] == 12
