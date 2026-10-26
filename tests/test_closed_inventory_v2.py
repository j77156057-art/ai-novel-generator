import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents import closed_inventory
from test_causal_timeline import fixture
from app.models import Generation
from app.agents.diagnostic import run_diagnostic_task


class ScopedReview:
    def __init__(self, omit=False):
        self.calls = []
        self.omit = omit

    def chat(self, system, user):
        d = json.loads(user)
        self.calls.append(d)
        kind = d['dimension']
        if d['stage'] == 'constraints':
            return json.dumps({'constraints': [] if self.omit else [{'kind': kind, 'mode': 'closed',
                'entities': [{'canonical': '扳手', 'aliases': []}],
                'source_id': next(iter(d['sources'])), 'quote': '室内仅有扳手。'}]})
        if d['stage'] == 'mentions':
            return json.dumps({'covered_source_ids': list(d['sources']), 'mentions': [
                {'source_id': sid, 'canonical': '焊枪', 'quote': '焊枪', 'state': 'actual'}
                for sid in d['sources']]})
        return json.dumps({'verdicts': [{'candidate_id': c['id'], 'status': 'violated', 'reason': '仅有扳手，新增焊枪'}
            for c in d['candidates']]})


def scoped_db():
    db, p = fixture()
    p.plot_timeline = {'planning': {'segment_contracts': {'1': {'authority': {'premise': '室内仅有扳手。'}}}}}
    db.add(Generation(project_id=p.id, segment_no=1, text='她拿起焊枪。\n\n她放下焊枪。'))
    db.commit()
    return db, p


def test_v2_object_scope_and_missing_constraint_fail_closed():
    db, p = scoped_db()
    provider = ScopedReview(omit=True)
    r = run_diagnostic_task(db, p.id, task_type='body_consistency', skill_id='closed_inventory',
        skill_version=2, inventory_kinds=['object'], allow_experimental=True, segment_nos=[1], provider=provider)
    assert r['status'] == 'failed'
    assert r['model_error_category'] == 'inventory_missing_dimension'
    assert len(provider.calls) == 1
    assert r['inventory']['coverage_complete'] is False


def test_v2_verified_batches_checkpoint_reuse_and_fingerprint_binding(tmp_path):
    db, p = scoped_db()
    provider = ScopedReview()
    kwargs = dict(task_type='body_consistency', skill_id='closed_inventory', skill_version=2,
        inventory_kinds=['object'], inventory_checkpoint_dir=str(tmp_path), allow_experimental=True, segment_nos=[1])
    r = run_diagnostic_task(db, p.id, provider=provider, **kwargs)
    assert r['status'] == 'completed', r
    assert r['inventory']['coverage_complete'] is True
    assert r['inventory']['unreviewed_dimensions'] == ['person', 'place']
    assert r['inventory']['dimensions']['object']['validated_source_ids'] == ['G1P001', 'G1P002']
    assert r['checks'][0]['status'] == 'contradicted'
    assert all(c['location_verified'] for c in r['checks'][0]['citations'])
    assert all(d['dimension'] == 'object' for d in provider.calls)
    provider2 = ScopedReview()
    r2 = run_diagnostic_task(db, p.id, provider=provider2, **kwargs)
    assert r2['status'] == 'completed'
    assert provider2.calls == []
    g = db.query(Generation).filter_by(project_id=p.id).one()
    g.text += '\n\n焊枪。'
    db.commit()
    r3 = run_diagnostic_task(db, p.id, provider=provider2, **kwargs)
    assert r3['status'] == 'completed'
    assert len(provider2.calls) == 3



def test_real_truncated_raw_stops_first_probe_and_recovers_only_failed_batch(tmp_path):
    db, p = scoped_db()
    raw = json.loads((Path(__file__).parent/'fixtures/closed_inventory_truncated_mentions.json').read_text(encoding='utf-8'))['raw']
    class Truncated(ScopedReview):
        def chat(self, system, user):
            if json.loads(user)['stage'] == 'mentions':
                self.calls.append(json.loads(user))
                return raw
            return super().chat(system,user)
    kwargs = dict(task_type='body_consistency', skill_id='closed_inventory', skill_version=2,
        inventory_kinds=['object'], inventory_checkpoint_dir=str(tmp_path), allow_experimental=True, segment_nos=[1])
    first = Truncated()
    r = run_diagnostic_task(db,p.id,provider=first,inventory_split_truncated=False,**kwargs)
    assert r['status']=='failed'
    assert r['inventory_failure']['exception_type']=='JSONDecodeError'
    assert len(first.calls)==2
    assert r['inventory']['dimensions']['object']['validated_source_ids']==[]
    assert len(list(tmp_path.glob('*.failed.json')))==1
    second = ScopedReview()
    r2 = run_diagnostic_task(db,p.id,provider=second,**kwargs)
    assert r2['status']=='completed'
    assert [d['stage'] for d in second.calls]==['mentions','verdicts']


def test_real_existing_83_paragraphs_budgeted_and_validated():
    from app.agents.body_evidence import paragraph_sources
    from app.agents.closed_inventory_v2 import inspect_inventory
    text=json.loads((Path(__file__).parent/'fixtures/closed_inventory_existing_body.json').read_text(encoding='utf-8'))['text']
    body=paragraph_sources(text,'generation:10',prefix='G1P')
    assert len(body)==83
    authority={'C1S096':{'source_id':'contract:1','path':'/authority/premise','quote':'室内仅有扳手。','start':0,'end':8}}
    class EmptyLedger(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions':
                self.calls.append(d)
                assert sum(len(v)+len(k)+8 for k,v in d['sources'].items())<=650
                return json.dumps({'covered_source_ids':list(d['sources']),'mentions':[]})
            return super().chat(system,user)
    report={'unresolved':[]}
    inspect_inventory(EmptyLedger(),{'data':{'segments':[{'segment_no':1,'generation_id':10}],
        'sources':dict(authority,**body)}},report,kinds=['object'])
    assert report['inventory']['dimensions']['object']['validated_source_ids']==list(body)
    assert report['inventory']['coverage_complete'] is True


def test_v2_public_tool_exposes_dimension_scope(experimental_consent):
    from app.agents.react_loop import build_tool_catalog, validate_arguments
    from app.agents.registry import invoke_tool
    tools={t.name:t for t in build_tool_catalog()}
    assert not validate_arguments(tools['run_diagnostic_task'].parameters, {'inventory_kinds':['object']})
    db,p=scoped_db()
    from app.routers import assistant_shared
    original=assistant_shared._ensure_assistant_provider
    try:
        assistant_shared._ensure_assistant_provider=lambda: ScopedReview()
        result=invoke_tool('run_diagnostic_task',db=db,project_id=p.id,task_type='body_consistency',
            skill_id='closed_inventory',skill_version=2,allow_experimental=True,segment_nos=[1],inventory_kinds=['object'])
        assert result['status']=='completed',result
        assert result['scope']['inventory_kinds']==['object']
    finally:
        assistant_shared._ensure_assistant_provider=original


def test_truncation_subdivision_is_bounded_to_one_level(tmp_path):
    db,p=scoped_db()
    raw=json.loads((Path(__file__).parent/'fixtures/closed_inventory_truncated_mentions.json').read_text(encoding='utf-8'))['raw']
    class Once(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions' and len(d['sources'])>1:
                self.calls.append(d)
                return raw
            return super().chat(system,user)
    provider=Once()
    r=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',skill_version=2,
        inventory_kinds=['object'],allow_experimental=True,segment_nos=[1],provider=provider)
    assert r['status']=='completed'
    assert [d['stage'] for d in provider.calls].count('mentions')==3
    assert r['inventory']['dimensions']['object']['validated_source_ids']==['G1P001','G1P002']


def test_real_v2_bad_quote_is_rejected_with_replayable_details():
    from app.agents.closed_inventory_v2 import inspect_inventory
    data=json.loads((Path(__file__).parent/'fixtures/closed_inventory_v2_source_mismatch.json').read_text(encoding='utf-8'))
    sources={sid:{'source_id':'generation:10','quote':text,'start':0,'end':len(text)} for sid,text in data['request']['sources'].items()}
    sources['C1S096']={'source_id':'contract:1','path':'/authority/premise','quote':'室内仅有扳手。','start':0,'end':8}
    class Replay(ScopedReview):
        def chat(self,system,user):
            d=json.loads(user)
            if d['stage']=='mentions': return json.dumps(data['response'],ensure_ascii=False)
            return super().chat(system,user)
    report={'unresolved':[]}
    with pytest.raises(ValueError,match='inventory_source_mismatch'):
        inspect_inventory(Replay(),{'data':{'segments':[{'segment_no':1,'generation_id':10}],'sources':sources}},report,kinds=['object'])
    assert report['inventory']['calls'][-1]['rejected_citation']=={
        'source_id':'G1P025','quote':'她手里的核对表','source_quote':data['request']['sources']['G1P025']}
    assert report['inventory']['dimensions']['object']['validated_source_ids']==[]


def test_default_v2_scopes_all_three_dimensions_independently():
    db,p=scoped_db()
    provider=ScopedReview()
    r=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='closed_inventory',skill_version=2,
        allow_experimental=True,segment_nos=[1],provider=provider)
    assert r['status']=='completed',r
    assert [d['dimension'] for d in provider.calls if d['stage']=='constraints']==['person','place','object']
    assert r['inventory']['unreviewed_dimensions']==[]
    assert all(c['estimated_input_tokens']>=len(json.dumps(d,ensure_ascii=False))//4
        for c,d in zip(r['inventory']['calls'],provider.calls))


def test_character_budget_covers_all_83_sources_without_loss():
    sources = {f'G1P{i:03}': {'quote': '正文物件。' * (i % 11 + 1)} for i in range(1, 84)}
    batches = closed_inventory.mention_batches(sources, char_budget=420)
    assert [sid for b in batches for sid in b] == list(sources)
    assert all(sum(len(v['quote']) + len(k) + 8 for k, v in b.items()) <= 420 for b in batches)
    assert len(batches) > 3
    with pytest.raises(ValueError, match='inventory_paragraph_too_large'):
        closed_inventory.mention_batches({'G1P001': {'quote': '长' * 421}}, char_budget=420)
