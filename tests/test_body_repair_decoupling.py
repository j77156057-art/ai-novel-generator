"""局部修复准入与全局验收解耦：apply 闸门 + 全局验收义务的重新绑定。

背景（离线审计结论，见 docs/authority-body-repair-scope-audit.md）：
原实现的 apply 闸门只认 `semantic_accepted`，而它依赖 `unresolved` 为空；`unresolved` 里的
`inventory_partial_coverage` 与 `full_scope_acceptance_unverified` 恒定读自**原诊断**的
inventory（正文修订不改变它），于是任何修复都永远无法落地 —— 修不好就无法验收，不修就无法验收。
"""
import json

import pytest
from test_causal_timeline import fixture, Reply


def _proposal(db, p, g, scope, after='新正文。'):
    from app.models import AgentRun
    from app.agents.proposals import build_proposal
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='controlled')
    db.add(run)
    db.commit()
    prop = build_proposal(db, run_id=run.id, project_id=p.id, proposal_type='text_edit',
                          target_kind='generation_text', target_id=str(g.id),
                          after_text=after, reason='controlled', scope=scope)
    db.commit()
    return prop


def _with_server_scope(prop, **extra):
    """模拟 create_body_repair 的服务端写入（请求体无法写这些键）。"""
    scope = dict(prop.scope_json or {})
    scope.update(extra)
    prop.scope_json = scope
    return scope


def test_repair_write_allowed_modes():
    from app.agents.proposals import repair_write_allowed
    assert repair_write_allowed({}) is None
    assert repair_write_allowed({'semantic_accepted': False}) is None
    assert repair_write_allowed({'semantic_accepted': True}) == 'global'
    assert repair_write_allowed({'local_apply_authorized': True}) == 'local'
    assert repair_write_allowed({'local_apply_authorized': False}) is None
    assert repair_write_allowed({'semantic_accepted': True,
                                  'local_apply_authorized': True}) == 'global'


def test_local_authorized_repair_can_be_approved_and_applied():
    """局部修复被独立复验授权后可以落地，且全局义务被重新绑定而不是删除。"""
    from app.models import Generation
    from app.routers.assistant_proposals import approve_proposal, apply_proposal, ProposalActionRequest
    db, p = fixture()
    g = Generation(project_id=p.id, segment_no=1, text='旧正文。')
    db.add(g)
    db.commit()
    prop = _proposal(db, p, g, {'independent_verification_required': True})
    _with_server_scope(prop, local_apply_authorized=True, semantic_accepted=False,
                       global_acceptance_state={
                           'state': 'carried_forward',
                           'unresolved': ['inventory_partial_coverage',
                                          'full_scope_acceptance_unverified',
                                          'original_check:object_N011'],
                           'closed_check_ids': ['object_N013'],
                           'source_run_id': 1,
                           'inventory_fingerprint': 'fp',
                           'acceptance': [{'id': 'full_scope_coverage', 'requires_evidence': True}],
                           'bound_body_hash': 'after-hash'})

    approve_proposal(prop.id, ProposalActionRequest(action='approve', actor='test'), db)
    db.refresh(prop)
    assert prop.status == 'approved'
    result = apply_proposal(prop.id, ProposalActionRequest(action='apply', actor='test'), db)
    db.refresh(g)
    db.refresh(p)
    assert g.text == '新正文。'
    assert result['version_id']

    pending = p.plot_timeline['pending_acceptance']
    assert pending['state'] == 'carried_forward'
    # 全局义务没有被删除、也没有被缩小。
    assert 'inventory_partial_coverage' in pending['unresolved']
    assert 'full_scope_acceptance_unverified' in pending['unresolved']
    assert 'original_check:object_N011' in pending['unresolved']
    # 已关闭的检查不会被重新挂回去。
    assert 'object_N013' in pending['closed_check_ids']
    assert not any('object_N013' in item for item in pending['unresolved'])
    # 义务绑定在这一版正文上（正文指纹，与整段验收同一把尺）。
    from app.agents.body_acceptance import body_fingerprint
    from app.models import Generation
    rows = list(db.query(Generation).filter_by(project_id=p.id).all())
    assert pending['bound_body_hash'] == body_fingerprint(rows)
    # 生成守卫仍然拦截：局部落地不等于验收通过。
    assert p.plot_timeline['body_revision_requires_review']['segment_no'] == 1
    from app.generation.engine import build_context
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        build_context(db, p, profile='original')
    assert error.value.status_code == 409


def test_unverified_repair_is_still_rejected():
    from app.models import Generation
    from app.routers.assistant_proposals import approve_proposal, ProposalActionRequest
    from fastapi import HTTPException
    db, p = fixture()
    g = Generation(project_id=p.id, segment_no=1, text='旧正文。')
    db.add(g)
    db.commit()
    prop = _proposal(db, p, g, {'independent_verification_required': True})
    _with_server_scope(prop, semantic_accepted=False, local_apply_authorized=False)
    with pytest.raises(HTTPException) as error:
        approve_proposal(prop.id, ProposalActionRequest(action='approve', actor='test'), db)
    assert error.value.status_code == 409
    db.refresh(prop)
    assert prop.status == 'pending'


def test_global_acceptance_path_still_opens_the_gate():
    from app.models import Generation
    from app.routers.assistant_proposals import approve_proposal, ProposalActionRequest
    db, p = fixture()
    g = Generation(project_id=p.id, segment_no=1, text='旧正文。')
    db.add(g)
    db.commit()
    prop = _proposal(db, p, g, {'independent_verification_required': True})
    _with_server_scope(prop, semantic_accepted=True)
    approve_proposal(prop.id, ProposalActionRequest(action='approve', actor='test'), db)
    db.refresh(prop)
    assert prop.status == 'approved'


@pytest.mark.parametrize('key', ['local_apply_authorized', 'global_acceptance_state'])
def test_clients_cannot_forge_the_new_server_computed_keys(key):
    from app.models import Generation, AgentRun
    from app.agents.proposals import build_proposal, PatchRejected
    db, p = fixture()
    g = Generation(project_id=p.id, segment_no=1, text='旧正文。')
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='controlled')
    db.add_all([g, run])
    db.commit()
    with pytest.raises(PatchRejected):
        build_proposal(db, run_id=run.id, project_id=p.id, proposal_type='text_edit',
                       target_kind='generation_text', target_id=str(g.id), after_text='候选。',
                       reason='test', scope={key: True})


def test_later_repair_does_not_reopen_a_closed_obligation():
    """后一次修复每次都从原诊断报告重算未解决列表 —— 不能把已关闭的义务重新挂回去。"""
    from app.agents.proposals import merge_pending_acceptance
    first = merge_pending_acceptance(
        None,
        {'unresolved': ['inventory_partial_coverage', 'original_check:object_N011',
                        'original_check:object_N017'],
         'closed_check_ids': ['object_N011'], 'acceptance': [], 'source_run_id': 1},
        bound_body_hash='h1')
    assert 'original_check:object_N011' not in first['unresolved']
    assert 'original_check:object_N017' in first['unresolved']

    second = merge_pending_acceptance(
        first,
        {'unresolved': ['inventory_partial_coverage', 'original_check:object_N011'],
         'closed_check_ids': ['object_N021'], 'acceptance': [], 'source_run_id': 2},
        bound_body_hash='h2')
    assert 'original_check:object_N011' not in second['unresolved']
    assert 'original_check:object_N017' in second['unresolved']
    assert 'inventory_partial_coverage' in second['unresolved']
    assert second['closed_check_ids'] == ['object_N011', 'object_N021']
    assert second['bound_body_hash'] == 'h2'
