"""规划调用的限次重试：模型偶发返回不合格式 JSON 时不该一次就判 needs_planning。"""
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

BIBLE = {'synopsis': '雨夜查旧档', 'direction': '揭穿旧案',
         # persona（说话方式等）是角色个性：有它，模型写对白才有依据。
         # 这里给齐，是为了让「重试次数」这类测试不被「补齐 persona」那次调用干扰。
         'characters': [{'name': '沈砚', 'role': '修复员', 'motivation': '澄清责任',
                         'persona': {'说话方式': '句子短，只陈述核对到的事实',
                                     '行为倾向': ['被追问时→先翻原件再回答'],
                                     '性格底线': ['不会替人担责'],
                                     '关键经历': ['三年前经手过同一批档案']}}],
         'relationships': [], 'conflicts': [], 'foreshadowing': []}
NODES = [{'title': f'核对{i}', 'premise': '记录矛盾', 'motivation': '澄清责任',
          'action': '核对原件', 'result': '确定差错', 'cost': 'NA', 'time': '03:00',
          'location': '档案室', 'participants': ['沈砚'],
          'state_before': '差错未明', 'state_after': '差错已定位'} for i in range(3)]


def _client(db, monkeypatch, writer):
    app = FastAPI()
    app.include_router(projects.router)
    app.include_router(wizard.router)
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(wizard, 'get_llm_provider', lambda: writer)
    return TestClient(app)


def test_bible_retries_when_the_model_returns_junk(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        calls = {'n': 0}

        class Writer:
            def chat(self, system, user, **kw):
                calls['n'] += 1
                if calls['n'] < 3:
                    return '这不是 JSON'
                return json.dumps(BIBLE, ensure_ascii=False)

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
        assert r.status_code == 200
        data = r.json()
        assert data['status'] == 'bible_draft', data
        assert calls['n'] == 3, '前两次不合格式应重试'


def test_bible_gives_up_after_bounded_attempts_and_reports_them(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        calls = {'n': 0}

        class Writer:
            def chat(self, system, user, **kw):
                calls['n'] += 1
                return '永远不是 JSON'

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
        assert r.status_code == 200
        data = r.json()
        assert data['status'] == 'needs_planning'
        assert data['error']['code'] == 'invalid_model_structure'
        assert data['error']['attempts'] == 3
        assert len(data['error']['failures']) == 3
        assert calls['n'] == 3, '重试必须有上限，不能无限调模型'


def test_retry_tells_the_model_what_was_wrong_instead_of_resending_the_same_prompt(monkeypatch):
    """原样重试只是赌模型下次不犯同样的错；第二次必须是**不同的**请求。"""
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        seen = []

        class Writer:
            def chat(self, system, user, **kw):
                seen.append(user)
                if len(seen) < 2:
                    return '{"synopsis": "坏的", "characters": '
                return json.dumps(BIBLE, ensure_ascii=False)

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
        assert r.status_code == 200
        assert r.json()['status'] == 'bible_draft'
        assert len(seen) == 2
        assert seen[0] != seen[1], '第二次不能与第一次完全一样'
        assert '纠正上一次不合格式的输出' in seen[1]
        assert 'JSONDecodeError' in seen[1], '要告诉模型错在哪（类型）'
        assert 'char' in seen[1], '要告诉模型错在哪（位置）'


def test_failure_record_never_echoes_the_raw_response(monkeypatch):
    """原始响应可能含异常串，不能进错误响应体。"""
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        secret = '异常串-SECRET-不要外泄'

        class Writer:
            def chat(self, system, user, **kw):
                return secret

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
        assert r.status_code == 200
        data = r.json()
        assert data['status'] == 'needs_planning'
        assert secret not in json.dumps(data, ensure_ascii=False)
        for f in data['error']['failures']:
            assert 'detail' in f and f['detail'].startswith('JSONDecodeError')


def test_retry_hint_names_the_missing_field_without_echoing_the_model_text(monkeypatch):
    """大纲侧的真实失败是「缺字段/数量不对」，不是 JSON 语法错 ——
    提示要说对，并且只带**字段名**，不带模型原文。"""
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True},
                    plot_bible={'synopsis': '雨夜查旧档', 'direction': '揭穿旧案'})
        db.add(p)
        db.commit()
        seen = []
        secret = '模型原文-SECRET-不该出现在提示词里'

        class Writer:
            def chat(self, system, user, **kw):
                seen.append(user)
                if len(seen) < 2:
                    return json.dumps({'nodes': [{'title': '缺字段', 'motive': secret}]},
                                      ensure_ascii=False)
                return json.dumps({'nodes': NODES}, ensure_ascii=False)

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/generate-outline',
                        json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                              'preview': True})
        assert r.status_code == 200
        assert r.json()['status'] in {'outline_draft', 'clarification_required'}
        assert len(seen) == 2
        assert secret not in seen[1], '提示词里不能回显模型原文'
        assert 'premise' in seen[1], '要告诉模型缺了哪个字段'


def test_bible_reports_request_failure_when_the_model_never_answered(monkeypatch):
    """请求类失败和格式类失败要分得开：模型没回 ≠ 模型回了但不合格式。"""
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        calls = {'n': 0}

        class Broken:
            def chat(self, system, user, **kw):
                calls['n'] += 1
                raise RuntimeError('connection reset')

        client = _client(db, monkeypatch, Broken())
        r = client.post(f'/api/projects/{p.id}/build-bible', json={'preview': True})
        assert r.status_code == 200
        data = r.json()
        assert data['status'] == 'needs_planning'
        assert data['error']['code'] == 'model_request_failed'
        assert data['model_participated'] is False, '模型压根没回，不能算参与'
        assert data['error']['attempts'] == 3
        assert data['error']['type'] == 'RuntimeError'
        assert calls['n'] == 3


def test_outline_also_retries(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[], plot_timeline={'sentinel': True},
                    plot_bible={'synopsis': '雨夜查旧档', 'direction': '揭穿旧案'})
        db.add(p)
        db.commit()
        calls = {'n': 0}

        class Writer:
            def chat(self, system, user, **kw):
                calls['n'] += 1
                if calls['n'] < 2:
                    return '{ 坏 JSON'
                return json.dumps({'nodes': NODES}, ensure_ascii=False)

        client = _client(db, monkeypatch, Writer())
        r = client.post(f'/api/projects/{p.id}/generate-outline',
                        json={'confirm_bible': True, 'goals': ['查清真相'], 'node_count': 3,
                              'preview': True})
        assert r.status_code == 200
        assert r.json()['status'] in {'outline_draft', 'clarification_required'}
        assert calls['n'] == 2


def test_bible_fills_missing_persona_in_one_extra_call(monkeypatch):
    """模型没给角色个性时补一轮（一次调用补所有人），补上才算完。

    为什么必须有：只有身份+动机的话，模型写对白没有「这个人怎么说话」的依据
    ⇒ 所有角色一个腔调（2026-10-28 真跑实测）。
    """
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        p = Project(title='测试', premise='雨夜查旧档', outline=[],
                    plot_timeline={'sentinel': True})
        db.add(p)
        db.commit()
        calls = {'persona_calls': 0}

        class Writer:
            def chat(self, system, user, **kw):
                if '写作档案' in system:
                    calls['persona_calls'] += 1
                    return json.dumps({'characters': [{
                        'name': '沈砚',
                        '说话方式': '句子短，只陈述核对到的事实',
                        '行为倾向': ['被追问时→先翻原件再回答'],
                    }]}, ensure_ascii=False)
                return json.dumps({
                    'synopsis': '雨夜查旧档', 'direction': '揭穿旧案',
                    'characters': [{'name': '沈砚', 'role': '修复员', 'motivation': '澄清责任'}],
                    'relationships': [], 'conflicts': [], 'foreshadowing': []},
                    ensure_ascii=False)

        client = _client(db, monkeypatch, Writer())
        r = client.post('/api/projects/%d/build-bible' % p.id, json={'preview': True})
        assert r.status_code == 200
        data = r.json()
        assert data['status'] == 'bible_draft', data
        assert calls['persona_calls'] == 1, '缺个性时应当补一轮（且只补一次）'
        persona = (data['characters'][0] or {}).get('persona') or {}
        assert persona.get('说话方式'), '补出来的个性要落进剧情圣经'
