"""整段验收：覆盖证据不足就不发模型请求；通过才把全局义务关闭到这一版正文。"""
import json

import pytest
from test_causal_timeline import fixture, Reply

BODY_BEFORE = '她戴着手套。\n账册未动。'
BODY_AFTER = '她看着账册。\n账册未动。'


def _inventory_run(db, p, g, *, complete=True, body_text=None):
    """造一条 closed_inventory 审计（覆盖证据的来源）。"""
    from app.models import AgentRun
    from app.agents.diagnostic import read_capability, digest
    scope = {'character': '', 'cursor': 0, 'limit': 5, 'segment_nos': [1],
             'inventory_kinds': ['object']}
    obs = read_capability(db, p.id, 'read_closed_inventory', scope)
    report = {
        'project_id': p.id, 'status': 'completed', 'agent_role': 'diagnostic',
        'provider_role': 'assistant', 'model_participated': True,
        'skill': {'id': 'closed_inventory', 'version': 4, 'status': 'experimental'},
        'scope': scope, 'observations': [obs], 'input_fingerprint': digest([obs]),
        'acceptance': [{'id': 'explicit_closure', 'requires_evidence': True},
                       {'id': 'full_scope_coverage', 'requires_evidence': True}],
        'inventory': {'input_fingerprint': obs['input_fingerprint'],
                      'coverage_complete': complete,
                      'global_quality_verified': complete,
                      'verdict_scope': {'source_ids': ['G1P001'], 'complete': complete},
                      'unreviewed_dimensions': [] if complete else ['person', 'place'],
                      'mentions': [{'source_id': 'G1P001', 'quote': '她'}]},
        'checks': [] if complete else [{'id': 'object_N011', 'status': 'contradicted',
                                        'reason': '小凳子未授权', 'citations': []}],
        'unresolved': ['inventory_partial_coverage'],
    }
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='test')
    db.add(run)
    db.flush()
    report['run_id'] = run.id
    run.note = json.dumps(report, ensure_ascii=False)
    db.commit()
    return run, report


def _acceptance_fixture(*, current_inventory=True, complete=True):
    from app.models import Generation
    from app.agents.body_acceptance import body_fingerprint
    db, p = fixture()
    g = Generation(project_id=p.id, segment_no=1, text=BODY_BEFORE)
    db.add(g)
    db.commit()
    origin, _ = _inventory_run(db, p, g, complete=complete)
    # 模拟一次已落地的修复：正文变了，原审计的证据随之失效。
    g.text = BODY_AFTER
    db.commit()
    if current_inventory:
        _inventory_run(db, p, g, complete=complete)
    rows = list(db.query(Generation).filter_by(project_id=p.id).all())
    timeline = dict(p.plot_timeline or {})
    timeline['pending_acceptance'] = {
        'state': 'carried_forward',
        'unresolved': ['inventory_partial_coverage', 'original_check:object_N011'],
        'closed_check_ids': [],
        'acceptance': [{'id': 'explicit_closure', 'requires_evidence': True},
                       {'id': 'full_scope_coverage', 'requires_evidence': True}],
        'inventory_fingerprint': 'fp', 'source_run_id': origin.id,
        'diagnostic_run_id': origin.id,
        'bound_body_hash': body_fingerprint(rows),
    }
    p.plot_timeline = timeline
    db.commit()
    return db, p, g, body_fingerprint(rows)


def _good_answer():
    return {
        'assessment': 'observed', 'reason': '整段核对完成',
        'dimensions': [{'dimension': d, 'assessment': 'supported', 'reason': '一致',
                        'source_ids': ['G1P001']}
                       for d in ('time', 'place', 'participants', 'object_state')],
        'check_decisions': [{'check_id': 'object_N011', 'status': 'resolved',
                             'reason': '小凳子已不在正文中', 'source_ids': ['G1P001']}],
        'coverage': {'examined_source_ids': ['G1P001', 'G1P002'], 'complete': True,
                     'reason': '逐段核对'},
    }


def test_acceptance_failure_records_the_error_message():
    """验收失败时，报告要说得清**为什么**失败。

    只留异常类型名的话，「额度用尽」「限流」「输出被截断」「模型答得不合格」
    四种完全不同的失败都显示成同一个 `RuntimeError` —— 读报告的人没法处置：
    额度要等、限流要降并发、截断要调 max_tokens、答得不合格要回灌重问。
    「报告说不清为什么」和「报告说错了」一样坏。
    """
    from app.agents.body_acceptance import run_body_acceptance

    class Boom:
        """模拟通道侧失败（真实场景：429 额度用尽）。"""
        model = 'fake-minimax'

        def chat(self, *args, **kwargs):
            raise RuntimeError('MiniMax chat 调用失败: 429 已达到 Token Plan 用量上限')

    db, p, g, fingerprint = _acceptance_fixture()
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=Boom())
    assert report['status'] == 'blocked'
    assert report['global_semantic_accepted'] is False
    assert report['model_error_category'] == 'RuntimeError'
    assert '429' in report['model_error_message']
    assert 'Token Plan' in report['model_error_message']
    assert 'acceptance_failed' in report['unresolved']
    # 失败不得把全局义务当成已关闭
    db.refresh(p)
    assert p.plot_timeline['pending_acceptance']['state'] != 'accepted'


def test_missing_current_inventory_returns_coverage_incomplete_without_model_call():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture(current_inventory=False)
    provider = Reply(_good_answer())
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'coverage_incomplete'
    assert report['global_semantic_accepted'] is False
    assert provider.calls == [], '覆盖证据不足时不应发起模型调用'
    db.refresh(p)
    assert p.plot_timeline['pending_acceptance']['state'] == 'carried_forward'


def test_incomplete_inventory_coverage_returns_coverage_incomplete():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture(complete=False)
    provider = Reply(_good_answer())
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'coverage_incomplete'
    assert provider.calls == []


def test_unreviewed_dimension_keeps_coverage_open():
    """未选维度（person/place 从未审过）不能算覆盖完整。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    run, report = _inventory_run(db, p, g, complete=True)
    note = json.loads(run.note)
    note['inventory']['unreviewed_dimensions'] = ['place']
    run.note = json.dumps(note, ensure_ascii=False)
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert result['status'] == 'coverage_incomplete'
    assert 'place' in result['coverage']['unreviewed_dimensions']
    assert provider.calls == []


def test_audit_violation_blocks_the_acceptance():
    """完整审计若判出违例，整段验收必须挡住（不能只看四维就放行）。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    run, report = _inventory_run(db, p, g, complete=True)
    note = json.loads(run.note)
    note['checks'] = [{'id': 'object_N021', 'status': 'contradicted', 'reason': '仍有未授权物件',
                       'citations': []}]
    run.note = json.dumps(note, ensure_ascii=False)
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert result['status'] == 'blocked'
    assert 'audit_violation:object_N021' in result['unresolved']


def test_audit_unknown_verdict_is_kept_open_not_hidden():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    run, report = _inventory_run(db, p, g, complete=True)
    note = json.loads(run.note)
    note['checks'] = [{'id': 'object_N017', 'status': 'unknown', 'reason': '所指实体无法确认',
                       'citations': []}]
    run.note = json.dumps(note, ensure_ascii=False)
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert result['status'] == 'accepted'
    assert [item['id'] for item in result['open_items']] == ['object_N017']
    db.refresh(p)
    assert p.plot_timeline['pending_acceptance']['open_items']


def test_full_acceptance_closes_the_global_obligation():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    provider = Reply(_good_answer())
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'accepted'
    assert report['global_semantic_accepted'] is True
    assert report['unresolved'] == []
    assert len(provider.calls) == 1
    db.refresh(p)
    pending = p.plot_timeline['pending_acceptance']
    assert pending['state'] == 'accepted'
    assert pending['unresolved'] == []
    assert pending['bound_body_hash'] == fingerprint
    assert pending['acceptance_run_id'] == report['run_id']


def test_unresolved_check_keeps_the_obligation_open():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    answer = _good_answer()
    answer['check_decisions'][0]['status'] = 'still_violated'
    provider = Reply(answer)
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'blocked'
    assert report['global_semantic_accepted'] is False
    assert 'original_check:object_N011' in report['unresolved']
    db.refresh(p)
    assert p.plot_timeline['pending_acceptance']['state'] == 'carried_forward'


def test_incomplete_coverage_claim_does_not_pass():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    answer = _good_answer()
    answer['coverage']['examined_source_ids'] = ['G1P001']
    answer['coverage']['complete'] = True
    provider = Reply(answer)
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'blocked'
    assert 'coverage_incomplete' in report['unresolved']


def test_top_level_assessment_must_be_observed():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    answer = _good_answer()
    answer['assessment'] = 'supported'
    provider = Reply(answer)
    report = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert report['status'] == 'blocked'
    assert 'invalid_top_level_assessment' in report['unresolved']


def test_stale_body_fingerprint_is_rejected():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    provider = Reply(_good_answer())
    with pytest.raises(ValueError) as error:
        run_body_acceptance(db, p.id, base_content_hash='stale', provider=provider)
    assert str(error.value) == 'stale_body_fingerprint'
    assert provider.calls == []


def test_missing_pending_acceptance_is_rejected():
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    p.plot_timeline = {k: v for k, v in (p.plot_timeline or {}).items()
                       if k != 'pending_acceptance'}
    db.commit()
    provider = Reply(_good_answer())
    with pytest.raises(ValueError) as error:
        run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert str(error.value) == 'no_pending_acceptance'
    assert provider.calls == []
