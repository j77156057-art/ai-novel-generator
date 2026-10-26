import copy
import pytest
from test_foundation_planning import api, nodes
from app.generation import engine
from app.generation.planning import timeline_for
from app.models import Generation

class Captured(Exception):
    pass

class Writer:
    def __init__(self): self.user = ''
    def chat(self, system, user, **kwargs):
        self.user = user
        raise Captured()
    def stream_chat(self, system, user, **kwargs):
        self.user = user
        raise Captured()
        yield ''


def test_production_sync_stream_preview_consume_same_structured_state(api):
    client, db, p = api
    p.plot_timeline = timeline_for(nodes(), {'verified': False}, 3)
    p.target_length=9000; p.generation_length=3000
    db.commit()
    prompt = engine.current_node_prompt(p, 0)
    before = copy.deepcopy(p.plot_timeline)
    for run in (lambda w: engine.generate_next_segment(db,p,provider=w,profile='original'),
                lambda w: list(engine.generate_next_segment_stream(db,p,provider=w,profile='original'))):
        w=Writer()
        with pytest.raises(Captured): run(w)
        for value in ('03:00','档案室','沈砚','差错未明','差错已定位'):
            assert value in w.user and value in prompt
    from app.routers import generation
    client.app.include_router(generation.router)
    result=client.post('/api/generate-plan',json={'project_id':p.id,'profile':'original'})
    assert result.status_code==200
    for value in ('03:00','档案室','沈砚','差错未明','差错已定位'):
        assert value in result.json()['user_prompt']
    assert p.plot_timeline == before


@pytest.mark.parametrize('stream', [False, True])
def test_successful_generation_saves_original_contract_in_both_paths(api, monkeypatch, stream):
    client, db, p = api
    p.plot_timeline=timeline_for(nodes(), {'verified':False},3)
    p.target_length=9000; p.generation_length=3000
    p.premise='完整物品清单：记录册与铅笔；禁止添加道具。'
    db.commit()
    class CompletedWriter:
        def chat(self, system, user, **kwargs):
            if 'verdict' in system:
                return '{"verdict":"unknown","reason":"尚未完成","sources":[]}'
            return '沈砚在档案室核对记录，差错未明。'
        def stream_chat(self, *args, **kwargs):
            yield '沈砚在档案室核对记录，差错未明。'
        def summarize(self, text): return '核对记录。'
    monkeypatch.setattr(engine,'_audit_and_repair_scene',lambda text,*a,**kw:(text,{}))
    monkeypatch.setattr(engine,'_merge_segment_into_graph',lambda *a,**kw:{})
    monkeypatch.setattr(engine,'_enqueue_v3_extraction',lambda *a,**kw:{})
    from app.generation import branch
    monkeypatch.setattr(branch,'roll_branch',lambda *a,**kw:(None,{}))
    if stream:
        list(engine.generate_next_segment_stream(db,p,provider=CompletedWriter(),profile='original'))
    else:
        engine.generate_next_segment(db,p,provider=CompletedWriter(),profile='original')
    db.refresh(p)
    assert db.query(Generation).filter_by(project_id=p.id,segment_no=1).one().text
    contract=p.plot_timeline['planning']['segment_contracts']['1']
    assert contract['node_id']=='n1'
    assert '03:00' in contract['prompt'] and '档案室' in contract['prompt']
    assert contract['authority']['premise']==p.premise
    assert contract['authority']['causal_node']==nodes()[0]
    original_premise=p.premise
    p.premise='此后修订设定，不得倒填历史契约。'
    db.commit()
    from app.agents.body_consistency import read_body
    observed, _, _=read_body(db,p,{'segment_nos':[1]})
    assert observed['segments'][0]['contract']['authority']['premise']==original_premise


def test_regeneration_uses_original_segment_contract_after_node_advanced(api):
    client, db, p = api
    ns=nodes(); ns[1]['time']='04:00'; ns[1]['location']='新库房'
    p.plot_timeline=timeline_for(ns, {'verified':False},3)
    p.target_length=9000; p.generation_length=3000
    db.add(Generation(project_id=p.id,segment_no=1,text='差错已定位。'))
    db.commit()
    class Judge:
        def chat(self,*args,**kwargs):
            return '{"verdict":"supported","reason":"明确定位","sources":[{"source_id":"generation:1","quote":"差错已定位。"}]}'
    engine._judge_and_advance(db,p,Judge(),'差错已定位。',0)
    assert p.plot_timeline['current_node']=='n2'
    # Snapshot survives timeline restoration even for older non-causal plans.
    from app.generation.timeline import state_from_dict
    legacy = copy.deepcopy(p.plot_timeline)
    legacy['planning'].pop('nodes')
    restored = state_from_dict(legacy, total_segments=3)
    assert restored.planning['segment_contracts']['1']['node_id'] == 'n1'
    w=Writer()
    with pytest.raises(Captured):
        list(engine.regenerate_segment_stream(db,p,1,provider=w,profile='original'))
    assert '03:00' in w.user and '档案室' in w.user
    assert '04:00' not in w.user and '新库房' not in w.user
    assert p.plot_timeline['current_node']=='n2'
