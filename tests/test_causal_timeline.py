from pathlib import Path
import sys
import json
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Project
from app.generation import engine
from app.generation.timeline import plan_timeline, state_to_dict

class Reply:
    def __init__(self, data):
        self.data = data
        self.calls = []
    def chat(self, system, user, **kwargs):
        self.calls.append(user)
        return json.dumps(self.data, ensure_ascii=False)

def fixture():
    db = Session(create_engine('sqlite:///:memory:'))
    Base.metadata.create_all(db.bind)
    st = plan_timeline(3)
    for node in st.nodes.values():
        node.title = '寻找钥匙'
        node.entry_condition = '门锁着，钥匙遗失'
        node.exit_condition = '找到钥匙'
    p = Project(title='钥匙', outline=['寻找钥匙'], target_length=3000,
                generation_length=1000, plot_timeline=state_to_dict(st))
    db.add(p)
    db.commit()
    return db, p

def test_budget_last_segment_still_judged_and_not_done():
    db, p = fixture()
    provider = Reply({'verdict':'contradicted','reason':'明确未找到',
                      'sources':[{'source_id':'generation:1','quote':'钥匙未找到。','start':0,'end':6}]})
    out = engine._judge_and_advance(db,p,provider,'钥匙未找到。',0)
    assert len(provider.calls) == 1, 'budget last segment skipped semantic judge'
    assert p.plot_timeline['done'] == []
    assert p.plot_timeline['current_node'] == 'n1'
    assert p.plot_timeline['seg_counts'] == {'n1':1}
    assert out['status'] == 'needs_review'

def test_bare_boolean_cannot_prove_completion():
    from app.generation.timeline import judge_node_completion
    db, p = fixture()
    node = engine._timeline_state_for(p,0).nodes['n1']
    result = judge_node_completion(Reply({'done': True}),node,'钥匙未找到。',source_id='generation:1')
    assert result['verdict'] == 'unknown'
    assert result['sources'] == []

def test_supported_completion_is_persisted_with_source_and_exactly_one_count():
    db,p = fixture()
    evidence = {'verdict':'supported','reason':'正文明确找到',
                'sources':[{'source_id':'generation:1','quote':'找到钥匙。','start':0,'end':5}]}
    out = engine._judge_and_advance(db,p,Reply(evidence),'找到钥匙。',0)
    assert out['forced']
    assert p.plot_timeline['done'] == ['n1']
    saved = p.plot_timeline['completion']['n1']
    assert {k:saved[k] for k in evidence} == evidence
    assert saved['audit']['response_raw']
    assert p.plot_timeline['seg_counts'] == {'n1':1}

def test_empty_or_short_plan_is_rejected():
    from app.generation.timeline import plan_nodes_with_llm
    assert plan_nodes_with_llm(Reply({'nodes':[{'title':'寻找','entry_condition':'门锁着','exit_condition':''}]}),['找钥匙'],{},1) is None
    assert plan_nodes_with_llm(Reply({'nodes':[{'title':'寻找','entry_condition':'门锁着','exit_condition':'找到钥匙'}]}),['找钥匙'],{},3) is None

def test_preview_does_not_plan_and_generation_explains_missing_plan():
    import pytest
    from fastapi import HTTPException
    db,p=fixture()
    p.plot_timeline=None
    provider=Reply({'nodes':[]})
    engine.current_node_prompt(p,0,provider=provider,db=None)
    assert not provider.calls
    assert p.plot_timeline is None
    with pytest.raises(HTTPException) as exc:
        engine.current_node_prompt(p,0,provider=provider,db=db)
    assert exc.value.status_code == 409
    assert exc.value.detail['status'] == 'needs_planning'

def test_exhausted_budget_pauses_instead_of_writing_forever():
    import pytest
    from fastapi import HTTPException
    db,p=fixture()
    engine._judge_and_advance(db,p,Reply({'done':True}),'钥匙未找到。',0)
    with pytest.raises(HTTPException) as exc:
        engine.current_node_prompt(p,1,provider=Reply({}),db=db)
    assert exc.value.detail['status'] == 'needs_review'

def test_second_segment_not_double_counted_and_duplicate_judge_is_idempotent():
    db,p=fixture()
    st=p.plot_timeline
    st['nodes']['n1']['segment_budget']=4
    p.target_length=18000
    p.plot_timeline=st
    provider=Reply({'verdict':'unknown','reason':'仍在寻找','sources':[]})
    engine._judge_and_advance(db,p,provider,'还在寻找。',0)
    engine._judge_and_advance(db,p,provider,'仍在寻找。',1)
    assert p.plot_timeline['seg_counts'] == {'n1':2}
    engine._judge_and_advance(db,p,provider,'仍在寻找。',1)
    assert p.plot_timeline['seg_counts'] == {'n1':2}
    assert len(provider.calls)==2

def test_full_text_and_invalid_evidence_are_checked():
    from app.generation.timeline import judge_node_completion
    db,p=fixture()
    node=engine._timeline_state_for(p,0).nodes['n1']
    text='找到钥匙。'+'院里很静。'*400
    item={'source_id':'generation:8','quote':'找到钥匙。','start':0,'end':5}
    provider=Reply({'verdict':'supported','reason':'原文明示','sources':[item]})
    assert judge_node_completion(provider,node,text,source_id='generation:8')['verdict']=='supported'
    assert text in provider.calls[0]
    for bad in ({'start':1},{'end':50000},{'source_id':'generation:9'},{'quote':'没有钥匙。'},{'start':True}):
        baditem=dict(item,**bad)
        result=judge_node_completion(Reply({'verdict':'supported','reason':'假证据','sources':[baditem]}),node,text,source_id='generation:8')
        assert result['verdict']=='unknown'
    for raw in ('bad json','{"verdict":"supported","reason":"缺证据","sources":[]}'):
        class Raw:
            def chat(self,*args): return raw
        assert judge_node_completion(Raw(),node,text)['verdict']=='unknown'
    class Boom:
        def chat(self,*args): raise RuntimeError('network')
    assert judge_node_completion(Boom(),node,text)['reason']=='judge_failed'

def test_legacy_done_preserved_without_inventing_evidence():
    from app.generation.timeline import state_from_dict
    db,p=fixture()
    saved=dict(p.plot_timeline,version=1,done=['n1'],current_node='n2')
    saved.pop('completion',None)
    state=state_from_dict(saved,total_segments=3)
    assert state.done=={'n1'}
    assert state.completion['n1']=={'verdict':'unknown','reason':'legacy_unverified','sources':[]}
    restored=state_from_dict(state_to_dict(state),total_segments=3)
    assert restored.completion==state.completion

def test_completion_helper_requires_supported_evidence():
    from app.generation.timeline import force_complete_current
    state=plan_timeline(3)
    assert force_complete_current(state).done==set()

def test_failed_placeholder_retries_explicit_generation_planning():
    db,p=fixture()
    p.plot_timeline=state_to_dict(plan_timeline(3))
    provider=Reply({'nodes':[{'title':'找钥匙','entry_condition':'门锁着','exit_condition':'找到钥匙'}]*3})
    prompt=engine.current_node_prompt(p,0,provider=provider,db=db)
    assert len(provider.calls)==1
    assert '找到钥匙' in prompt

def test_unique_quote_can_be_located_programmatically_without_model_counting():
    from app.generation.timeline import judge_node_completion
    db,p=fixture()
    node=engine._timeline_state_for(p,0).nodes['n1']
    provider=Reply({'verdict':'supported','reason':'明确找到','sources':[{'source_id':'generation:3','quote':'找到钥匙。'}]})
    result=judge_node_completion(provider,node,'门边。找到钥匙。',source_id='generation:3')
    assert result['verdict']=='supported'
    assert result['sources'][0]['start']==3
    assert result['sources'][0]['end']==8
    assert judge_node_completion(provider,node,'找到钥匙。找到钥匙。',source_id='generation:3')['verdict']=='unknown'

def test_readonly_preview_explains_pause_without_mutation():
    db,p=fixture()
    engine._judge_and_advance(db,p,Reply({'done':True}),'钥匙未找到。',0)
    before=json.dumps(p.plot_timeline,sort_keys=True)
    provider=Reply({})
    prompt=engine.current_node_prompt(p,1,provider=provider,db=None)
    assert 'needs_review' in prompt
    assert json.dumps(p.plot_timeline,sort_keys=True)==before
    assert not provider.calls

def test_sync_generation_budget_end_persists_unknown_without_done(monkeypatch):
    db,p=fixture()
    class Writer(Reply):
        def chat(self, system, user, **kwargs):
            if '【source_id】' in user:
                self.calls.append(user)
                return '{"done":true}'
            return '钥匙未找到。'
        def summarize(self,text): return '钥匙未找到'
        def summarize_rollup(self,*args,**kwargs): return '钥匙未找到'
    provider=Writer({})
    monkeypatch.setattr(engine,'_enqueue_v3_extraction',lambda *a,**kw:{})
    monkeypatch.setattr(engine,'_schedule_async_audit_kg',lambda *a,**kw:None)
    engine.generate_next_segment(db,p,provider=provider)
    assert len(provider.calls)==1
    assert p.plot_timeline['done']==[]
    assert p.plot_timeline['completion']['n1']['verdict']=='unknown'
    assert p.plot_timeline['seg_counts']=={'n1':1}
