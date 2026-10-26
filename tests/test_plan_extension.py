"""扩展规划（plan_extension）的服务端纪律。

只测**服务端推导**那部分：余量怎么算、越界怎么拒、契约怎么写。
封闭清单协议那部分由正文审计自己的测试覆盖（这里是同一套复用）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Generation, Project
from app.agents import plan_extension as pe


AUTHORITY = (
    '现实生活短篇。2026年10月12日上午09:00至15:00，地点始终是社区图书室修复桌旁。'
    '仅两位人物：管理员林夏、志愿者陈舟。起始物品清单完整：旧植物图册、登记本、一支铅笔。'
    '不增添工具。场景依次分三节点：09:00至10:00只检查封面；10:00至11:00加固封面；'
    '11:00至12:00复核登记。'
)

#: 权威只写到 12:00（三个节点刚好写满）——用来测「没有余量就显式失败」。
AUTHORITY_FULL = AUTHORITY.replace('上午09:00至15:00', '上午09:00至12:00')


def causal(title, time):
    return {'title': title, 'premise': '前提', 'motivation': '动机', 'action': '行动',
            'result': '结果', 'cost': 'NA', 'time': time, 'location': '社区图书室修复桌旁',
            'participants': ['林夏', '陈舟'], 'state_before': '起始状态',
            'state_after': '结束状态'}


def make_project(db, *, authority_span=('09:00', '12:00'), covered=('09:00至10:00',
                                                                  '10:00至11:00',
                                                                  '11:00至12:00'),
                 done=None, contracts=None, authority=AUTHORITY):
    """造一个「规划已用尽」的项目状态。"""
    nodes = {}
    planning_nodes = []
    contracts = contracts if contracts is not None else {}
    for index, window in enumerate(covered):
        nid = f'n{index + 1}'
        nodes[nid] = {'id': nid, 'kind': 'main', 'title': f'节点{index + 1}',
                      'emotion': '', 'entry_condition': f'时间：2026年10月12日{window}；地点：X；',
                      'exit_condition': '行动：A；结果：B；', 'beats': [], 'opens': [],
                      'closes': [], 'segment_budget': 1, 'parent': None, 'children': []}
        planning_nodes.append(causal(f'节点{index + 1}', f'2026年10月12日{window}'))
        contracts[str(index + 1)] = {'node_id': nid, 'prompt': '旧提示',
                                     'authority': {'premise': authority,
                                                   'causal_node': planning_nodes[-1]}}
    timeline = {
        'version': 2, 'nodes': nodes, 'done': done if done is not None else list(nodes),
        'current_node': None, 'current_beat': 0, 'branch_used': 0,
        'seg_counts': {nid: 1 for nid in nodes}, 'completion': {}, 'last_judged_segment': 3,
        'planning': {'nodes': planning_nodes, 'segment_contracts': contracts},
    }
    p = Project(title='扩展规划测试', outline=['a', 'b', 'c'], target_length=27000,
                generation_length=3000, premise=authority, plot_timeline=timeline)
    db.add(p)
    db.commit()
    for index in range(len(covered)):
        db.add(Generation(project_id=p.id, segment_no=index + 1, text='旧正文' * 10))
    db.commit()
    return db, p


class Reply:
    """只会回固定 JSON 的假 provider。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, system, user, **kwargs):
        self.calls.append((system, user))
        if isinstance(self.payload, list):
            return self.payload[min(len(self.calls) - 1, len(self.payload) - 1)]
        return json.dumps(self.payload, ensure_ascii=False)


def proposal(time='2026年10月12日13:00至14:00'):
    return {'nodes': [causal('午后复核', time)]}


def test_no_headroom_reports_the_boundary_and_calls_no_model(monkeypatch):
    session = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(session.bind)
    db, p = make_project(session, authority=AUTHORITY_FULL)
    provider = Reply(proposal())
    result = pe.extend_plan(db, p, provider, count=1)
    assert result['status'] == 'no_authority_headroom'
    assert '09:00' in result['headroom']['authority_span']
    assert provider.calls == [], '没有余量就不该调模型'


def test_plan_not_exhausted_refuses_to_extend(monkeypatch):
    session = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(session.bind)
    db, p = make_project(session, done=['n1'])
    provider = Reply(proposal())
    result = pe.extend_plan(db, p, provider, count=1)
    assert result['status'] == 'plan_not_exhausted'
    assert provider.calls == []


def test_extension_writes_new_node_and_contract_only_appends(monkeypatch):
    session = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(session.bind)
    db, p = make_project(session)
    monkeypatch.setattr(pe, '_out_of_authority', lambda *a, **k: [])
    before_contracts = dict(p.plot_timeline['planning']['segment_contracts'])
    provider = Reply(proposal())
    result = pe.extend_plan(db, p, provider, count=1)
    assert result['status'] == 'extended', result
    timeline = p.plot_timeline
    assert timeline['current_node'] == 'n4'
    contracts = timeline['planning']['segment_contracts']
    assert set(contracts) == {'1', '2', '3', '4'}
    assert contracts['1'] == before_contracts['1'], '历史契约不能被改'
    assert contracts['4']['node_id'] == 'n4'
    assert contracts['4']['authority']['premise'] == AUTHORITY
    assert '【权威约束（不得违反）】' in contracts['4']['prompt'], '新契约的提示必须带权威'
    assert p.premise == AUTHORITY, '权威不能被改'
    assert len(timeline['nodes']) == 4


def test_out_of_authority_items_are_pushed_back_then_rejected(monkeypatch):
    session = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(session.bind)
    db, p = make_project(session)
    monkeypatch.setattr(pe, '_out_of_authority',
                        lambda *a, **k: [{'entity': '镊子', 'kind': 'object', 'reason': '清单外'}])
    provider = Reply(proposal())
    result = pe.extend_plan(db, p, provider, count=1)
    assert result['status'] == 'out_of_authority_items'
    assert '镊子' in result['message']
    assert len(provider.calls) == 2, '应该回灌一次让模型自己改'
    assert '镊子' in provider.calls[1][1], '回灌必须说清命中了什么'
    assert 'n4' not in (p.plot_timeline.get('nodes') or {}), '不合格就不许写进状态'


def test_free_spans_leave_room_only_where_nothing_is_written():
    covered = [(540, 600), (600, 660)]
    assert pe._free_spans((540, 720), covered) == [(660, 720)]
    assert pe._free_spans((540, 720), [(540, 720)]) == []
    assert pe._free_spans((540, 720), []) == [(540, 720)]


def test_time_ranges_ignore_cross_day_and_non_time_text():
    assert pe._spans('第1至48页') == []
    assert pe._spans('23:00至次日02:00') == []
    assert pe._spans('09:00至10:00') == [(540, 600)]
