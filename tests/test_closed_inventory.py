import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from test_causal_timeline import fixture
from app.models import Generation
from app.agents.diagnostic import run_diagnostic_task


def test_closed_inventory_separate_extraction_and_verdict_with_authority_body_evidence():
    db, p = fixture()
    p.plot_timeline = {'planning': {'segment_contracts': {'1': {'authority': {'premise': '舰舱内仅有扳手。'}}}}}
    db.add(Generation(project_id=p.id, segment_no=1, text='她拿起焊枪。'))
    db.commit()
    class Review:
        calls = 0
        def chat(self, system, user):
            self.calls += 1
            data = json.loads(user)
            if data['stage'] == 'constraints':
                sid = next(iter(data['sources']))
                return json.dumps({'constraints':[{'kind':'object','mode':'closed','entities':[{'canonical':'扳手','aliases':[]}], 'source_id':sid,'quote':'仅有扳手'}]})
            if data['stage'] == 'mentions':
                return json.dumps({'covered_source_ids':list(data['sources']), 'mentions':[{'source_id':'G1P001','surface':'焊枪','canonical':'焊枪','kind':'object','state':'actual'}]})
            return json.dumps({'verdicts':[{'candidate_id':data['candidates'][0]['id'],'status':'violated','reason':'明确仅有扳手，实际拿起新工具'}]})
    provider = Review()
    r = run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',allow_experimental=True,segment_nos=[1],provider=provider)
    assert r['status'] == 'completed', r
    assert provider.calls == 3
    assert r['checks'][0]['status'] == 'contradicted'
    assert {c['body_source_id'].split(':')[0] for c in r['checks'][0]['citations']} == {'contract','generation'}
    assert r['inventory']['coverage_complete'] is True


import pytest
from app.agents.diagnostic import diagnostic_tool


@pytest.mark.parametrize('premise,text,canonical,state,aliases,closed', [
    ('江湖客栈有茶壶。', '他搬来木凳。', '木凳', 'actual', [], False),
    ('实验舱仅有扳手。', '她拿起活动扳手。', '扳手', 'actual', ['活动扳手'], True),
    ('古堡内仅有烛台。', '月光像银盘。', '银盘', 'metaphor', [], True),
    ('舰舱内仅有扳手。', '她没有焊枪。', '焊枪', 'negated', [], True),
    ('房内仅有书本。', '传闻房里藏着金印。', '金印', 'quoted', [], True),
    ('房内仅有书本。', '她想象桌上放着金印。', '金印', 'hypothetical', [], True),
    ('房内仅有书本。', '她忆起童年玩过风车。', '风车', 'memory', [], True),
])
def test_normal_fiction_and_nonactual_mentions_are_not_violations(monkeypatch, experimental_consent, premise,text,canonical,state,aliases,closed):
    db,p = fixture()
    p.plot_timeline = {'planning': {'segment_contracts': {'1': {'authority': {'premise': premise}}}}}
    db.add(Generation(project_id=p.id,segment_no=1,text=text)); db.commit()
    class Review:
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='constraints':
                return json.dumps({'constraints':[] if not closed else [{'kind':'object','mode':'closed',
                    'entities':[{'canonical':'扳手' if '扳手' in premise else '烛台' if '烛台' in premise else '书本','aliases':aliases}],
                    'source_id':next(iter(d['sources'])),'quote':premise}]})
            if d['stage']=='mentions':
                return json.dumps({'covered_source_ids':list(d['sources']), 'mentions':[{'source_id':next(iter(d['sources'])),
                    'surface':aliases[0] if aliases else canonical,'canonical':canonical,'kind':'object','state':state}]})
            return json.dumps({'verdicts':[{'candidate_id':c['id'],'status':'allowed','reason':'别名或范围外非现实提及'} for c in d['candidates']]})
    from app.routers import assistant_shared
    monkeypatch.setattr(assistant_shared,'_ensure_assistant_provider',lambda:Review())
    r=diagnostic_tool(db,p.id,task_type='body_consistency',skill_id='closed_inventory',allow_experimental=True,segment_nos=[1])
    assert r['status']=='completed',r
    assert all(c['status']=='supported' for c in r['checks'])
    assert not r['proposed_actions']


def test_missing_paragraph_coverage_fails_unknown():
    db,p=fixture()
    p.plot_timeline={'planning':{'segment_contracts':{'1':{'authority':{'premise':'室内仅有书本。'}}}}}
    db.add(Generation(project_id=p.id,segment_no=1,text='书本在桌上。\n\n她拿起金印。'));db.commit()
    class Review:
        def chat(self,system,user):
            d=json.loads(user)
            return json.dumps({'constraints':[]}) if d['stage']=='constraints' else json.dumps({'covered_source_ids':[], 'mentions':[]})
    r=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',allow_experimental=True,segment_nos=[1],provider=Review())
    assert r['status']=='failed'
    assert r['assessment']=='unknown'
    assert r['model_error_category']=='inventory_incomplete_coverage'
    assert r['inventory']['coverage_complete'] is False
    assert r['inventory_failure']=={'stage':'mentions','exception_type':'ValueError',
        'reason':'inventory_incomplete_coverage','raw_available':True}
    assert 'model_unavailable_or_invalid_output' not in r['unresolved']


def test_missing_authority_cannot_claim_complete_coverage():
    db,p=fixture()
    db.add(Generation(project_id=p.id,segment_no=1,text='她拿起金印。'));db.commit()
    r=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',allow_experimental=True,segment_nos=[1],provider=object())
    assert r['assessment']=='unknown'
    assert r['inventory']['coverage_complete'] is False
    assert r['checks'][0]['status']=='unknown'


def test_constraints_only_receive_user_authority_not_generated_node():
    db,p=fixture()
    p.plot_timeline={'planning':{'segment_contracts':{'1':{'authority':{
        'premise':'室内仅有书本。','causal_node':{'premise':'金印不得使用。'}}}}}}
    db.add(Generation(project_id=p.id,segment_no=1,text='她读书本。'));db.commit()
    class Review:
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='constraints':
                assert {s['path'] for s in d['sources'].values()}=={'/authority/premise'}
                return json.dumps({'constraints':[]})
            return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[]})
    r=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',allow_experimental=True,segment_nos=[1],provider=Review())
    assert r['status']=='completed',r


def test_real_invalid_constraint_response_is_rejected_without_repair():
    from app.agents.closed_inventory import inspect_inventory
    raw=json.loads((Path(__file__).parent/'fixtures/closed_inventory_invalid_constraint.json').read_text(encoding='utf-8'))['raw']
    quote=json.loads(raw)['constraints'][0]['quote']
    observation={'data':{'segments':[{'segment_no':1,'generation_id':1}], 'sources':{
        'C1S090':{'source_id':'contract:1','path':'/authority/causal_node/premise','quote':quote,'start':0,'end':len(quote)},
        'C1S100':{'source_id':'contract:1','path':'/authority/premise','quote':'室内仅有书本。','start':0,'end':8},
        'G1P001':{'source_id':'generation:1','quote':'她读书本。','start':0,'end':6}}}}
    class Replay:
        def chat(self,*args): return raw
    with pytest.raises(ValueError,match='invalid_inventory_constraints'):
        inspect_inventory(Replay(),observation,{'unresolved':[]})


def test_probe_outcome_requires_complete_coverage_and_known_violation():
    from probe_closed_inventory import probe_outcome
    assert probe_outcome({'status':'failed','inventory_coverage':False})=={'success':False,'probe_status':'blocked'}
    assert probe_outcome({'status':'completed','inventory_coverage':True,'source_unchanged':True,
        'input_unchanged':True,'original_body_unchanged':True,'violations':[]})['success'] is False


def test_real_retry_constraints_parse_with_explicit_fixed_location():
    from app.agents.closed_inventory import inspect_inventory
    raw=json.loads((Path(__file__).parent/'fixtures/closed_inventory_valid_constraints.json').read_text(encoding='utf-8'))['raw']
    constraints=json.loads(raw)['constraints']
    quote=''.join(c['quote'] for c in constraints)
    observation={'data':{'segments':[{'segment_no':1,'generation_id':1}], 'sources':{
        'C1S096':{'source_id':'contract:1','path':'/authority/premise','quote':quote,'start':0,'end':len(quote)},
        'G1P001':{'source_id':'generation:1','quote':'图册在原处。','start':0,'end':6}}}}
    class Replay:
        def chat(self,system,user):
            d=json.loads(user)
            return raw if d['stage']=='constraints' else json.dumps({'covered_source_ids':list(d['sources']),'mentions':[]})
    report={'unresolved':[]}
    inspect_inventory(Replay(),observation,report)
    assert len(report['inventory']['constraints'])==3


def test_empty_body_cannot_claim_complete_coverage():
    db,p=fixture()
    p.plot_timeline={'planning':{'segment_contracts':{'1':{'authority':{'premise':'室内仅有书本。'}}}}}
    db.add(Generation(project_id=p.id,segment_no=1,text=''));db.commit()
    class Review:
        def chat(self,system,user): return json.dumps({'constraints':[]})
    r=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',allow_experimental=True,segment_nos=[1],provider=Review())
    assert r['assessment']=='unknown'
    assert r['inventory']['coverage_complete'] is False
