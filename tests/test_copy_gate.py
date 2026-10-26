"""照搬检测接进门槛：命中 → 回灌片段让模型自改 → 改不掉就拒绝保存。

用户已确认的口径：
  - 照搬（与参考规则共享 ≥12 字逐字片段）**接进保存/应用门槛**；
  - 命中时把片段**回灌**给模型让它自己改，而不是直接报错；
  - 比对只针对**真正进了提示词**的那部分规则 —— 模型看不到的规则，
    与它产出相同只可能是巧合，不能算照搬。
"""
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
#: 一条**不会**被选中进提示词的规则（每维取 4 条，它排在第 5）。
UNPICKED = '甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳'


def _nodes(action: str):
    return [{'title': f'核对{i}', 'premise': '记录矛盾', 'motivation': '澄清责任',
             'action': action, 'result': '确定差错', 'cost': 'NA', 'time': '03:00',
             'location': '档案室', 'participants': ['沈砚'],
             'state_before': '差错未明', 'state_after': '差错已定位'} for i in range(3)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv('ENTITY_CACHE_DIR', str(tmp_path / 'entities'))
    from app.style.entity_store import save_table
    save_table(1, {'characters': ['徐凤年'], 'places': [], 'factions': [], 'items': [],
                   'version': 2})
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    p = Project(title='测试', premise='雨夜查旧档', outline=[],
                plot_timeline={'sentinel': True}, reference_novel_ids=[1],
                style_narrative={'rules': RULES})
    p.plot_bible = {'synopsis': '雨夜查旧档', 'direction': '揭穿旧案'}
    db.add(p)
    db.commit()
    yield db, p
    db.close()


def _client(db, monkeypatch, writer):
    app = FastAPI()
    app.include_router(projects.router)
    app.include_router(wizard.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(wizard, 'get_llm_provider', lambda: writer)
    return TestClient(app)


def test_copy_is_fed_back_and_the_model_rewrites_it(project, monkeypatch):
    """照搬 → 把命中的片段回灌 → 模型自己改对 → 正常产出。"""
    db, p = project
    seen = []

    class Writer:
        def chat(self, system, user, **kw):
            seen.append(user)
            if len(seen) == 1:
                return json.dumps({'nodes': _nodes('他照着话本里的排场：' + RULE)},
                                  ensure_ascii=False)
            return json.dumps({'nodes': _nodes('他先在门口站定，等对方先开口。')},
                              ensure_ascii=False)

    client = _client(db, monkeypatch, Writer())
    r = client.post(f'/api/projects/{p.id}/generate-outline',
                    json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                          'preview': True})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['status'] == 'outline_draft', data
    assert data['structure_copy_check']['verdict'] == 'clean'
    assert len(seen) == 2, '第一次照搬后必须重试'
    assert '纠正照搬问题' in seen[1], '重试要把「照搬」这件事告诉模型'
    assert '贵客抵达→开中门并铺陈仪仗' in seen[1], '重试要把命中的片段回灌给模型'


def test_persistent_copy_is_refused_and_nothing_is_saved(project, monkeypatch):
    """改不掉就拒绝保存：既不落库，也不产出照搬的稿子。"""
    db, p = project
    calls = {'n': 0}

    class Writer:
        def chat(self, system, user, **kw):
            calls['n'] += 1
            return json.dumps({'nodes': _nodes('他照着话本里的排场：' + RULE)},
                              ensure_ascii=False)

    client = _client(db, monkeypatch, Writer())
    r = client.post(f'/api/projects/{p.id}/generate-outline',
                    json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                          'preview': False})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data['status'] == 'needs_planning', data
    assert data['error']['code'] == 'possible_copy', data['error']
    assert data['error']['attempts'] == 3, '重试必须有上限'
    assert data['error']['failures'][0]['code'] == 'possible_copy'
    assert data['error']['failures'][0]['matches'], '要告诉用户命中了什么'
    assert calls['n'] == 3
    # 没保存：outline 还是原来的空
    db.expire_all()
    assert db.get(Project, p.id).outline == [], '照搬的稿子不能落库'


def test_a_phrase_the_model_never_saw_is_not_treated_as_copying(tmp_path, monkeypatch):
    """模型看不到的规则（每维只取 4 条，落选的第 5 条）与它产出相同只是巧合。"""
    monkeypatch.setenv('ENTITY_CACHE_DIR', str(tmp_path / 'entities'))
    from app.style.entity_store import save_table
    save_table(1, {'characters': ['徐凤年'], 'places': [], 'factions': [], 'items': [],
                   'version': 2})
    rules = {'高频场景模板': [RULE],
             '节拍序列': [f'规则{i}：铺垫→升温；目标者拒绝或僵持{i}' for i in range(4)] + [UNPICKED]}
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[],
                    plot_timeline={'sentinel': True}, reference_novel_ids=[1],
                    style_narrative={'rules': rules})
        p.plot_bible = {'synopsis': '雨夜查旧档', 'direction': '揭穿旧案'}
        db.add(p)
        db.commit()

        class Writer:
            def chat(self, system, user, **kw):
                return json.dumps({'nodes': _nodes('他嘴里念着一串怪话：' + UNPICKED)},
                                  ensure_ascii=False)

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/generate-outline',
                        json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                              'preview': True})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data['status'] == 'outline_draft', data
        assert data['structure_copy_check']['verdict'] == 'clean', data['structure_copy_check']
