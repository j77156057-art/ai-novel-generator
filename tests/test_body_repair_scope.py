"""Bounded acceptance never upgrades historical proposals or whole-body quality."""
import copy
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


def evidence():
    return json.loads((Path(__file__).parent/'fixtures/body_repair_run79.json').read_text(encoding='utf-8'))


def assess(data):
    from app.agents.body_repair import assess_local_repair
    return assess_local_repair(**data)


def test_real_run79_local_acceptance_preserves_unknown_plan_and_ambiguity():
    data=evidence()
    result=assess(data)
    assert result['local_repair_verification']=='accepted'
    assert result['segment_plan_consistency']=='unknown'
    assert result['overall']=='unknown'
    assert result['verification_scope']=='local_repair_verification'
    assert result['covered']['diagnostic_check_ids']==['object_state_4']
    assert result['binding']['after_hash']==data['candidate']['after_hash']
    assert 'missing_saved_contract' in result['uncovered']
    assert 'other_body_semantics' in result['uncovered']
    assert result['apply_allowed'] is False


@pytest.mark.parametrize('mutation', ['scope','hash','preserved','no_endpoint','unknown','conflict','unresolved','association'])
def test_local_acceptance_rejects_unbound_or_unresolved_evidence(mutation):
    d=evidence()
    if mutation=='scope': d['verification']['scope']['segment_nos']=[2]
    elif mutation=='hash': d['candidate']['after_hash']='bad'
    elif mutation=='preserved': d['preserve_source_ids']=['absent']
    elif mutation=='no_endpoint':
        for c in d['verification']['checks']: c['citations']=[q for q in c['citations'] if q['source_id']!='G1P154']
    elif mutation=='unknown': d['verification']['checks'][-1]['status']='unknown'
    elif mutation=='conflict': d['verification']['checks'][-1]['status']='contradicted'
    elif mutation=='unresolved': d['verification']['unresolved'].append('partial_coverage')
    elif mutation=='association': d['conflict_source_id']='G1P001'
    assert assess(d)['local_repair_verification']=='unknown'


def test_real_evidence_replay_through_production_service_stays_blocked():
    from test_causal_timeline import fixture, Reply
    from app.models import AgentRun, Generation, EditProposal
    from app.agents.body_repair import create_body_repair
    from app.agents.proposals import apply_proposal, InvalidTransition
    d=evidence(); db,p=fixture()
    g=Generation(id=10,project_id=p.id,segment_no=1,
        text=d['previous']['observations'][0]['data']['body_texts']['10'])
    previous=copy.deepcopy(d['previous'])
    previous.update(status='completed',agent_role='diagnostic',
        scope={'character':'','cursor':0,'limit':5,'segment_nos':[1]},
        skill={'id':'body_repair','version':3,'status':'experimental'},acceptance=[])
    run=AgentRun(id=77,project_id=p.id,role='assistant',status='success',request_hash='replay',note=json.dumps(previous))
    db.add_all([g,run]);db.commit()
    class Replay:
        def __init__(self, raw): self.raw=raw
        def chat(self,*a,**kw): return self.raw
    r=d['candidate']['fixed_range']
    result=create_body_repair(db,p.id,diagnostic_run_id=77,generation_id=10,
        start=r['start'],end=r['end'],objective=d['objective'],preserve_source_ids=d['preserve_source_ids'],
        conflict_source_id=d['conflict_source_id'],base_content_hash=d['candidate']['base_content_hash'],
        writer=Replay(json.dumps({'replacement':r['replacement']})),
        assistant=Replay(d['verification']['model_response_raw']))
    assert result['verification_acceptance']['local_repair_verification']=='accepted'
    assert result['semantic_accepted'] is False
    proposal=db.query(EditProposal).one()
    assert proposal.scope_json['verification_acceptance']==result['proposal']['verification_acceptance']
    proposal.status='approved'; db.commit()
    with pytest.raises(InvalidTransition,match='semantic'):
        apply_proposal(db,proposal=proposal)
    db.refresh(g); assert g.text==d['previous']['observations'][0]['data']['body_texts']['10']


def test_client_cannot_supply_local_verification_scope():
    from app.routers.assistant_proposals import BodyRepairRequest
    from pydantic import ValidationError
    d=evidence()
    with pytest.raises(ValidationError):
        BodyRepairRequest(project_id=5,diagnostic_run_id=77,generation_id=10,start=5517,end=5535,
            objective=d['objective'],preserve_source_ids=d['preserve_source_ids'],
            conflict_source_id=d['conflict_source_id'],base_content_hash=d['candidate']['base_content_hash'],
            verification_scope='local_repair_verification')
    from app.agents.proposals import SERVER_COMPUTED_SCOPE_KEYS
    assert 'verification_acceptance' in SERVER_COMPUTED_SCOPE_KEYS


def test_real_run79_does_not_mutate_audit_or_candidate():
    d=evidence(); before=copy.deepcopy(d)
    assess(d)
    assert d==before
