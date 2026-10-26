import json
import pytest
from test_causal_timeline import fixture, Reply
from app.models import Generation, AgentRun
from app.agents.diagnostic import read_capability, digest
from app.agents.body_repair import prepare_body_repair, create_body_repair


def authority_fixture():
    db,p=fixture()
    t=dict(p.plot_timeline);t['planning']={'segment_contracts':{'1':{'authority':{'premise':'清单完整：一本账册。不得新增物品。'},'node_id':'n1'}}};p.plot_timeline=t
    g=Generation(project_id=p.id,segment_no=1,text='她戴上手套。\n账册未动。');db.add(g);db.commit()
    scope={'character':'','cursor':0,'limit':5,'segment_nos':[1],'inventory_kinds':['object']}
    obs=read_capability(db,p.id,'read_closed_inventory',scope)
    sources=obs['data']['sources'];a=next(k for k,v in sources.items() if v.get('path')=='/authority/premise');b='G1P001'
    def cite(s):return dict(sources[s],source_id=s,body_source_id=sources[s]['source_id'],location_verified=True)
    constraint={'kind':'object','mode':'closed','source_id':a,'quote':sources[a]['quote'],'segment_no':1}
    candidate={'id':'N001','constraint':constraint,'mention':{'source_id':b},'verdict':{'status':'violated','body_source_id':b,'authority_source_id':a}}
    report={'project_id':p.id,'status':'completed','agent_role':'diagnostic','provider_role':'assistant','model_participated':True,'skill':{'id':'closed_inventory','version':4,'status':'experimental'},'scope':scope,'observations':[obs],'input_fingerprint':digest([obs]),'acceptance':[{'id':'full_scope_coverage','requires_evidence':True}],'inventory':{'input_fingerprint':obs['input_fingerprint'],'candidates':[candidate]},'checks':[{'id':'object_N001','status':'contradicted','reason':'手套未授权','citations':[cite(a),cite(b)]}],'unresolved':['inventory_partial_coverage']}
    run=AgentRun(project_id=p.id,role='assistant',status='success',request_hash='test');db.add(run);db.flush();report['run_id']=run.id;run.note=json.dumps(report);db.commit()
    args=dict(project_id=p.id,diagnostic_run_id=run.id,generation_id=g.id,start=0,end=6,objective='消除手套，不添加事实',preserve_source_ids=[a],conflict_source_id=b)
    return db,p,g,run,args


def test_closed_authority_confirmation_is_read_only():
    db,p,g,run,args=authority_fixture()
    before=(g.text,run.note)
    card=prepare_body_repair(db,**args)
    assert card['pending_action']['body']['base_content_hash']
    assert before==(g.text,run.note) and not db.dirty


@pytest.mark.parametrize('mutation',['open','allowed','fingerprint','contract','citation','project'])
def test_untrusted_or_drifted_authority_rejected(mutation):
    db,p,g,run,args=authority_fixture();report=json.loads(run.note)
    if mutation=='open':report['inventory']['candidates'][0]['constraint']['mode']='open'
    if mutation=='allowed':report['inventory']['candidates'][0]['verdict']['status']='allowed'
    if mutation=='fingerprint':report['observations'][0]['input_fingerprint']='bad'
    if mutation=='citation':report['checks'][0]['citations'][0]['quote']='fabricated'
    if mutation=='project':report['project_id']=999
    if mutation=='contract':
        t=dict(p.plot_timeline);t['planning']={'segment_contracts':{'1':{'authority':{'premise':'其他'}}}};p.plot_timeline=t
    run.note=json.dumps(report);db.commit()
    with pytest.raises((ValueError,PermissionError)):prepare_body_repair(db,**args)


def test_one_independent_check_preserves_uncovered_acceptance():
    db,p,g,run,args=authority_fixture()
    card=prepare_body_repair(db,**args)['pending_action']['body']
    a=args['preserve_source_ids'][0]
    writer=Reply({'replacement':'她看着账册。'})
    assistant=Reply({'assessment':'observed','reason':'范围内一致','findings':[{'dimension':'object_state','assessment':'supported','reason':'无手套','source_ids':[a,'G1P001']}]})
    first=create_body_repair(db,**card,writer=writer,assistant=assistant)
    again=create_body_repair(db,**card,writer=writer,assistant=assistant)
    assert len(writer.calls)==len(assistant.calls)==1
    assert first['proposal']['id']==again['proposal']['id']
    assert not first['semantic_accepted']
    assert 'inventory_partial_coverage' in first['verification']['unresolved']
    assert g.text=='她戴上手套。\n账册未动。'
