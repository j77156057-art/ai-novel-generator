"""动作提案：只提不执行 + 前提漂移检测。

要验的不是「函数返回了个对象」，而是**前提变了能不能被发现**：
动作提案没有「目标内容」，它靠「前提」判断自己是否过期 ——
前提变了却照常执行，就是拿旧计划写新东西。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402
from test_causal_timeline import fixture  # noqa: E402


def _plan_outline_proposal(db, p, **kwargs):
    """建一条「规划大纲」提案（前提：项目还没有大纲）。"""
    from app.agents.action_proposals import build_action_proposal
    p.outline = None
    p.plot_timeline = {}
    db.commit()
    return build_action_proposal(
        db, p, kind='plan_outline', title='写一份剧情大纲',
        detail={'note': '按设定起草三个剧情点'},
        endpoint='/api/projects/{id}', method='PATCH',
        body={'outline': ['第一段：…']},
        risk_level='medium', **kwargs)


def test_building_a_proposal_only_creates_a_pending_row():
    """提提案**不动任何业务表** —— 它只落提案表，状态 pending 等人批。"""
    from app.agents.action_proposals import build_action_proposal
    from app.models import ActionProposal, Generation
    db, p = fixture()
    row = _plan_outline_proposal(db, p)
    assert row.status == 'pending'
    assert row.precondition_fingerprint
    assert row.precondition_note
    assert row.detail_json['precondition_params'] == {}
    assert db.query(ActionProposal).filter_by(project_id=p.id).count() == 1
    # 关键：没有写正文、没有改项目
    assert db.query(Generation).filter_by(project_id=p.id).count() == 0
    assert not p.outline


def test_unknown_kind_is_refused():
    from app.agents.action_proposals import build_action_proposal
    db, p = fixture()
    with pytest.raises(Exception) as exc:
        build_action_proposal(db, p, kind='do_something_weird', title='x',
                              endpoint='/api/x')
    assert '未知' in str(exc.value)


def test_empty_title_is_refused():
    """标题是空的 → 人没法判断要不要批，必须拒。"""
    from app.agents.action_proposals import build_action_proposal
    db, p = fixture()
    with pytest.raises(Exception) as exc:
        build_action_proposal(db, p, kind='plan_outline', title='  ',
                              endpoint='/api/x')
    assert '标题' in str(exc.value)


def test_state_machine_transitions():
    """pending → approved → applied；pending → rejected；终态不可再动。"""
    from app.agents.action_proposals import validate_action
    validate_action('pending', 'approve')
    validate_action('pending', 'reject')
    validate_action('approved', 'apply')
    validate_action('approved', 'stale')
    for terminal in ('rejected', 'applied', 'stale'):
        with pytest.raises(Exception):
            validate_action(terminal, 'approve')
        with pytest.raises(Exception):
            validate_action(terminal, 'apply')


def test_pending_cannot_jump_straight_to_applied():
    """没批就执行 → 必须拒（审批与落库分开是刻意设计）。"""
    from app.agents.action_proposals import validate_action
    with pytest.raises(Exception):
        validate_action('pending', 'apply')


def test_plan_outline_proposal_goes_stale_once_an_outline_appears():
    """**前提变了就要被发现**：有人先填了大纲 → 这条提案过期。"""
    from app.agents.action_proposals import check_precondition
    db, p = fixture()
    row = _plan_outline_proposal(db, p)
    assert check_precondition(db, p, row) is True
    # 期间有人把大纲填上了
    p.outline = ['第一段：少年在渡口摆渡。']
    db.commit()
    assert check_precondition(db, p, row) is False


def test_extend_plan_proposal_goes_stale_after_a_segment_is_written():
    """扩展规划的前提含「已写段数」：期间写了段 → 接不上 → 过期。"""
    from app.agents.action_proposals import (build_action_proposal,
                                             check_precondition)
    from app.models import Generation
    db, p = fixture()
    row = build_action_proposal(
        db, p, kind='extend_plan', title='追加一个剧情点',
        endpoint='/api/projects/{id}/plan/extend', method='POST',
        body={'project_id': p.id}, risk_level='medium')
    assert check_precondition(db, p, row) is True
    db.add(Generation(project_id=p.id, segment_no=1, text='少年把竹篙插进石缝。'))
    db.commit()
    assert check_precondition(db, p, row) is False


def test_the_precondition_is_recomputed_from_the_database():
    """前提只依赖库里的事实 + 存下来的参数，不依赖「当时的内存状态」。

    怎么验：把提案对象从库里**重新读一遍**再判定，结果必须一样
    （否则「同一份事实、两个进程、两个结论」，漂移检测就是随机的）。
    """
    from app.agents.action_proposals import (action_proposal_to_dict,
                                             build_action_proposal,
                                             check_precondition)
    from app.models import ActionProposal
    db, p = fixture()
    row = build_action_proposal(
        db, p, kind='plan_outline', title='写一份剧情大纲',
        endpoint='/api/projects/{id}', method='PATCH', body={'outline': []})
    db.expire_all()
    fresh = db.get(ActionProposal, row.id)
    assert check_precondition(db, p, fresh) is True
    payload = action_proposal_to_dict(fresh)
    assert payload['status'] == 'pending'
    assert payload['kind'] == 'plan_outline'
    assert payload['detail']['precondition_params'] == {}


def test_start_distill_needs_reference_corpus():
    """没有参考语料就起不了蒸馏 —— 前提算不出来，直接拒。"""
    from app.agents.action_proposals import build_action_proposal
    db, p = fixture()
    p.reference_novel_ids = []
    db.commit()
    with pytest.raises(Exception) as exc:
        build_action_proposal(db, p, kind='start_distill', title='启动蒸馏',
                              endpoint='/api/projects/{id}/distill')
    assert '语料' in str(exc.value)


# ----------------------------------------------------------------------
# API 级：审批与执行是**两个**端点
# ----------------------------------------------------------------------

@pytest.fixture
def api_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Project
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    project = Project(title='动作提案测试', outline=[], target_length=6000,
                      generation_length=3000)
    db.add(project)
    db.commit()
    yield db, project
    db.close()


@pytest.fixture
def client(api_db):
    from fastapi.testclient import TestClient
    from app.db import get_db
    from app.main import app as fastapi_app
    db, _project = api_db
    fastapi_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(fastapi_app)
    finally:
        fastapi_app.dependency_overrides.clear()


def _make_proposal(db, p, **kwargs):
    from app.agents.action_proposals import build_action_proposal
    p.outline = None
    p.plot_timeline = {}
    db.commit()
    return build_action_proposal(
        db, p, kind='plan_outline', title='写一份剧情大纲',
        detail={'note': '按设定起草三个剧情点'},
        endpoint='/api/projects/{id}', method='PATCH',
        body={'outline': ['第一段：少年在渡口摆渡。']},
        risk_level='medium', **kwargs)


def test_approval_and_execution_are_two_separate_steps(api_db, client):
    """批准**不执行**；执行是另一次点确认。"""
    db, p = api_db
    row = _make_proposal(db, p)
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/approve',
                       json={'action': 'approve'})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['proposal']['status'] == 'approved'
    assert data['next_action'] == 'apply'
    # 批准之后**还没写大纲**
    db.refresh(p)
    assert not p.outline

    resp = client.post(f'/api/assistant/action-proposals/{row.id}/apply',
                       json={'action': 'apply'})
    assert resp.status_code == 200, resp.text
    assert resp.json()['proposal']['status'] == 'applied'
    db.refresh(p)
    assert p.outline == ['第一段：少年在渡口摆渡。']


def test_apply_after_the_precondition_changed_is_409_and_marks_it_stale(api_db, client):
    """前提变了 → 409 + 提案置 stale（事实留痕），不静默执行。"""
    db, p = api_db
    row = _make_proposal(db, p)
    client.post(f'/api/assistant/action-proposals/{row.id}/approve', json={'action': 'approve'})
    # 期间有人先填了大纲
    p.outline = ['别人写的第一段。']
    db.commit()
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/apply',
                       json={'action': 'apply'})
    assert resp.status_code == 409, resp.text
    assert resp.json()['detail']['error'] == 'stale_precondition'
    db.refresh(row)
    assert row.status == 'stale'


def test_apply_without_approval_is_refused(api_db, client):
    """没批就执行 → 409（审批与执行分开是纪律）。"""
    db, p = api_db
    row = _make_proposal(db, p)
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/apply',
                       json={'action': 'apply'})
    assert resp.status_code == 409, resp.text


def test_reject_is_final(api_db, client):
    db, p = api_db
    row = _make_proposal(db, p)
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/reject',
                       json={'action': 'reject'})
    assert resp.status_code == 200, resp.text
    assert resp.json()['proposal']['status'] == 'rejected'
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/apply',
                       json={'action': 'apply'})
    assert resp.status_code == 409, resp.text


def test_the_request_body_must_match_the_path_action(api_db, client):
    """action 必填且必须与路径一致（少写就 422，不让服务端猜）。"""
    db, p = api_db
    row = _make_proposal(db, p)
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/approve',
                       json={'action': 'apply'})
    assert resp.status_code == 422, resp.text
    resp = client.post(f'/api/assistant/action-proposals/{row.id}/approve', json={})
    assert resp.status_code == 422, resp.text


def test_listing_proposals_by_project(api_db, client):
    db, p = api_db
    _make_proposal(db, p)
    resp = client.get(f'/api/assistant/action-proposals?project_id={p.id}')
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['count'] == 1
    assert data['proposals'][0]['kind'] == 'plan_outline'
    resp = client.get(f'/api/assistant/action-proposals?project_id={p.id}&status=nope')
    assert resp.status_code == 422, resp.text


# ----------------------------------------------------------------------
# agent 侧：只构造卡，不落库、不执行
# ----------------------------------------------------------------------

def _with_corpus(db, p):
    """给项目塞一份参考语料（让语料侧工序不是 pending）。"""
    from app.models import NovelChunk, RawNovel
    novel = RawNovel(title='语料', status='imported')
    db.add(novel)
    db.flush()
    db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=1, text='这一段有句号。'))
    p.reference_novel_ids = [novel.id]
    db.commit()


def test_agent_can_propose_an_action_at_the_right_step():
    """链走到「写正文」这一步时，agent 能提一个「规划大纲」的卡。"""
    from app.agents.registry import get_tool_registry
    from app.models import ActionProposal
    db, p = fixture()
    out = get_tool_registry().invoke(
        'propose_action', db, p.id, kind='plan_outline', title='写一份剧情大纲',
        outline=['第一段：少年在渡口摆渡。'])
    assert out['ok'] is True, out
    card = out['pending_action']
    assert card['kind'] == 'action_proposal'
    assert card['endpoint'] == '/api/assistant/action-proposals'
    assert card['method'] == 'POST'
    assert card['body']['kind'] == 'plan_outline'
    assert card['body']['action_endpoint'].endswith('/projects/%s' % p.id)
    # 只构造卡：不落库
    assert db.query(ActionProposal).count() == 0


def test_agent_cannot_propose_an_action_at_the_wrong_step():
    """这一步不允许的动作 → 拒（agent 不自己发明动作）。"""
    from app.agents.registry import get_tool_registry
    db, p = fixture()
    out = get_tool_registry().invoke(
        'propose_action', db, p.id, kind='start_distill', title='启动蒸馏',
        tier='fast')
    assert out['ok'] is False, out
    assert '不能提' in out['note']
    assert 'pending_action' not in out


def test_the_distill_card_carries_the_tier_options():
    """启动蒸馏的卡要**自带档位选项**：档位是用户的决断，不能替用户定。

    选项的补丁里放的是**完整**的动作体：前端做的是浅合并，
    只放增量会把动作体里其它字段一起换掉。
    """
    from app.agents.registry import get_tool_registry
    db, p = fixture()
    _with_corpus(db, p)
    # 档案不在位 → 链停在「文风蒸馏」那一步（那里才提得起启动蒸馏）
    p.style_narrative = None
    p.outline = None
    p.plot_timeline = {}
    db.commit()
    out = get_tool_registry().invoke(
        'propose_action', db, p.id, kind='start_distill', title='启动文风蒸馏')
    assert out['ok'] is True, out
    card = out['pending_action']
    options = card['detail']['options']
    assert [o['id'] for o in options] == ['full', 'balanced', 'fast']
    for option in options:
        patch = option['body_patch']['action_body']
        assert patch['tier'] == option['id']
        # 完整动作体：浅合并不能把其它字段换掉
        assert patch['force'] is False
    assert card['detail']['question']


def test_the_text_edit_card_carries_an_editable_draft():
    """改写卡要带**可编辑草稿**：草稿是助手的，落库的必须是用户确认的那一版。

    红线：提案的 `after_text` 必须由用户填写 —— 服务端替用户写正文就是越权。
    """
    from app.agents.registry import get_tool_registry
    from app.models import EditProposal
    db, p = fixture()
    out = get_tool_registry().invoke(
        'propose_text_edit', db, p.id, type='text_edit',
        target_kind='generation_text', target_id='1',
        draft='少年把竹篙插进石缝，船身稳住了。',
        reason='这段动作太笼统')
    assert out['ok'] is True, out
    card = out['pending_action']
    assert card['kind'] == 'proposal'
    assert card['endpoint'] == '/api/assistant/proposals'
    assert card['method'] == 'POST'
    # 草稿在请求体里，且**告诉前端哪个字段可编辑**
    assert card['body']['after_text'] == '少年把竹篙插进石缝，船身稳住了。'
    assert card['detail']['draft_field'] == 'after_text'
    assert card['detail']['draft_editable'] is True
    # 协议字段由服务端给，前端不参与构造
    assert card['body']['type'] == 'text_edit'
    assert card['body']['target_kind'] == 'generation_text'
    assert card['body']['project_id'] == p.id
    # 只构造卡：不落库
    assert db.query(EditProposal).count() == 0


def test_an_empty_draft_is_refused():
    """空草稿没有意义（「改成什么」没说）→ 拒。"""
    from app.agents.registry import get_tool_registry
    db, p = fixture()
    out = get_tool_registry().invoke(
        'propose_text_edit', db, p.id, type='text_edit',
        target_kind='generation_text', target_id='1', draft='   ')
    assert out['ok'] is False, out
    assert 'pending_action' not in out


def test_the_proposal_card_is_harvested_only_for_this_project():
    from app.agents.action_proposals import collect_action_proposal_cards
    card = {'kind': 'action_proposal', 'title': 'x',
            'endpoint': '/api/assistant/action-proposals', 'method': 'POST',
            'body': {'project_id': 7, 'kind': 'plan_outline'}}
    results = {'propose_action': {'ok': True, 'pending_action': card}}
    assert collect_action_proposal_cards(results, 7) == [card]
    assert collect_action_proposal_cards(results, 8) == []
    # 端点不对的卡也不收
    bad = {'kind': 'action_proposal', 'endpoint': '/api/assistant/proposals',
           'method': 'POST', 'body': {'project_id': 7}}
    assert collect_action_proposal_cards(
        {'propose_action': {'ok': True, 'pending_action': bad}}, 7) == []


def test_a_draft_card_reaches_the_user_from_any_propose_tool():
    """agent 换用另一个起草工具，卡**不能丢**。

    实测踩过：agent 调了 `propose_text_fix`（按规则修标点），卡生成了，
    但收割器只认 `propose_text_edit` → 界面上什么也没有，
    而 agent 在对话里说「请在待确认卡中确认」—— 承诺落空。
    """
    from app.agents.pending_actions import collect_text_edit_cards
    card = {'kind': 'proposal', 'title': '改写一段内容',
            'endpoint': '/api/assistant/proposals', 'method': 'POST',
            'body': {'project_id': 7, 'after_text': '改好的'}}
    for tool in ('propose_text_edit', 'propose_text_fix'):
        results = {tool: {'ok': True, 'pending_action': card}}
        assert collect_text_edit_cards(results, 7) == [card], tool
    # 跨项目 / 端点不对 → 不收
    assert collect_text_edit_cards(
        {'propose_text_fix': {'ok': True, 'pending_action': card}}, 8) == []
    bad = {'kind': 'proposal', 'endpoint': '/api/assistant/proposals/1/apply',
           'method': 'POST', 'body': {'project_id': 7}}
    assert collect_text_edit_cards(
        {'propose_text_fix': {'ok': True, 'pending_action': bad}}, 7) == []
