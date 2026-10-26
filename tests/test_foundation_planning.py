import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from app.db import Base, get_db
from app.models import Project
from app.routers import projects, wizard

@pytest.fixture
def api():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        app=FastAPI()
        app.include_router(projects.router);app.include_router(wizard.router)
        app.dependency_overrides[get_db]=lambda:db
        p=Project(title='测试',premise='凌晨去核对记录',outline=['旧计划'],plot_timeline={'sentinel':True})
        db.add(p);db.commit()
        yield TestClient(app),db,p

def test_patch_outline_validation_null_and_readback(api):
    client,db,p=api
    for bad in ['text',[1],[' '],[{}]]:
        assert client.patch(f'/api/projects/{p.id}',json={'outline':bad}).status_code==422
    r=client.patch(f'/api/projects/{p.id}',json={'outline':['旧计划']})
    assert r.status_code==200
    db.refresh(p);assert p.plot_timeline=={'sentinel':True}
    r=client.patch(f'/api/projects/{p.id}',json={'outline':['核对原件']})
    assert r.status_code==200 and r.json()['outline']==['核对原件']
    db.refresh(p);assert p.plot_timeline is None and p.status=='needs_planning'
    assert client.get(f'/api/projects/{p.id}').json()['outline']==['核对原件']
    assert client.patch(f'/api/projects/{p.id}',json={'title':'保留'}).json()['outline']==['核对原件']
    assert client.patch(f'/api/projects/{p.id}',json={'outline':None}).json()['outline'] is None

def test_bible_writer_failure_and_preview_preserve_existing(api,monkeypatch):
    client,db,p=api
    p.plot_bible={'synopsis':'旧圣经'};db.commit()
    before=(p.plot_bible,p.plot_timeline,p.status)
    class Writer:
        def chat(self,*args):
            raise RuntimeError('secret-key-do-not-expose')
    monkeypatch.setattr(wizard,'get_llm_provider',lambda:Writer())
    r=client.post(f'/api/projects/{p.id}/build-bible',json={'preview':True})
    assert r.status_code==200
    data=r.json()
    assert data['status']=='needs_planning' and data['degraded'] and not data['model_participated']
    assert data['error']['code']=='model_request_failed'
    assert 'secret-key' not in r.text
    db.refresh(p);assert (p.plot_bible,p.plot_timeline,p.status)==before
    bible={'synopsis':'沈砚核对原件','direction':'回桥纠错','characters':[{'name':'沈砚','role':'修复员','motivation':'澄清错误'}], 'relationships':[], 'conflicts':[], 'foreshadowing':[]}
    Writer.chat=lambda self,*a:json.dumps(bible,ensure_ascii=False)
    data=client.post(f'/api/projects/{p.id}/build-bible',json={'preview':True}).json()
    assert data['model_participated'] and not data['degraded'] and data['status']=='bible_draft'
    db.refresh(p);assert (p.plot_bible,p.plot_timeline,p.status)==before
    data=client.post(f'/api/projects/{p.id}/build-bible',json={'preview':False}).json()
    readback=client.get(f'/api/projects/{p.id}').json()
    assert readback['plot_bible']==data and readback['status']=='bible_draft'
    assert readback['plot_timeline'] is None

def nodes():
    return [{'title':f'核对{i}', 'premise':'记录矛盾', 'motivation':'澄清责任', 'action':'核对原件', 'result':'确定差错', 'cost':'NA', 'time':'03:00', 'location':'档案室', 'participants':['沈砚'], 'state_before':'差错未明', 'state_after':'差错已定位'} for i in range(3)]

def test_outline_candidate_apply_readback_and_failure_preserves(api,monkeypatch):
    client,db,p=api
    p.plot_bible={'synopsis':'核对差错','direction':'纠错'};db.commit()
    before=(p.outline,p.plot_timeline,p.status)
    seen=[]
    class Writer:
        def chat(self,system,user):
            seen.append(user)
            return json.dumps({'nodes':nodes()},ensure_ascii=False)
    monkeypatch.setattr(wizard,'get_llm_provider',lambda:Writer())
    url=f'/api/projects/{p.id}/generate-outline'
    assert client.post(url,json={'confirm_bible':False,'goals':['定位差错']}).status_code==422
    r=client.post(url,json={'confirm_bible':True,'goals':['定位差错'],'node_count':3})
    assert r.status_code==200
    data=r.json();assert data['status']=='outline_draft' and data['model_participated']
    assert '凌晨去核对记录' in seen[0] and '核对差错' in seen[0] and '定位差错' in seen[0]
    db.refresh(p);assert (p.outline,p.plot_timeline,p.status)==before
    applied=client.post(f'/api/projects/{p.id}/outline/apply',json={'candidate':data}).json()
    assert applied['status']=='outline_draft'
    got=client.get(f'/api/projects/{p.id}').json()
    assert got['outline']==[n['title'] for n in nodes()]
    assert got['plot_timeline']['planning']['nodes']==nodes()
    from app.generation.timeline import state_from_dict,state_to_dict
    st=state_from_dict(got['plot_timeline'],total_segments=20)
    assert st and st.nodes['n1'].entry_condition and st.nodes['n1'].exit_condition
    assert state_to_dict(st)['planning']==got['plot_timeline']['planning']
    before=(got['outline'],got['plot_timeline'],got['status'])
    Writer.chat=lambda self,*a:'{"nodes":[{"title":"缺失结构"}]}'
    failed=client.post(url,json={'confirm_bible':True,'goals':['定位差错'],'node_count':3}).json()
    assert failed['status']=='needs_planning' and failed['error']['code']=='invalid_model_structure'
    got=client.get(f'/api/projects/{p.id}').json()
    assert (got['outline'],got['plot_timeline'],got['status'])==before

def test_outline_stale_and_existing_generation_rejected(api,monkeypatch):
    client,db,p=api
    from app.models import Generation
    from app.generation.planning import source_fingerprint
    candidate={'status':'outline_draft','degraded':False,'model_participated':True,'nodes':nodes(),'source_fingerprint':source_fingerprint(p)}
    p.premise='已变化';db.commit()
    assert client.post(f'/api/projects/{p.id}/outline/apply',json={'candidate':candidate}).status_code==409
    candidate['source_fingerprint']=source_fingerprint(p)
    db.add(Generation(project_id=p.id,segment_no=1,text='已有正文'));db.commit()
    assert client.post(f'/api/projects/{p.id}/outline/apply',json={'candidate':candidate}).status_code==409
    assert client.post(f'/api/projects/{p.id}/build-bible',json={'preview':False}).status_code==409

def test_outline_unknown_and_failure_categories(api,monkeypatch):
    client,db,p=api
    p.plot_bible={'synopsis':'测试','direction':'核对'};db.commit()
    from app.llm.provider import LocalFallbackLLMProvider
    monkeypatch.setattr(wizard,'get_llm_provider',lambda:LocalFallbackLLMProvider())
    data=client.post(f'/api/projects/{p.id}/generate-outline',json={'confirm_bible':True,'goals':['核对']}).json()
    assert data['error']['code']=='writer_unavailable' and not data['model_participated']
    before=(p.outline,p.plot_timeline,p.status)
    class Writer:
        def chat(self,*a):
            raise TimeoutError('secret')
    monkeypatch.setattr(wizard,'get_llm_provider',lambda:Writer())
    data=client.post(f'/api/projects/{p.id}/generate-outline',json={'confirm_bible':True,'goals':['核对']}).json()
    assert data['error']['code']=='model_request_failed'
    assert data['error']['type']=='TimeoutError'
    # 限次重试：请求类失败要重试满上限，并把每次失败分类记下来。
    assert data['error']['attempts']==3
    assert [f['code'] for f in data['error']['failures']]==['model_request_failed']*3
    db.refresh(p);assert (p.outline,p.plot_timeline,p.status)==before
    ns=nodes();ns[0]['motivation']='unknown';ns[0]['cost']='NA'
    Writer.chat=lambda self,*a:json.dumps({'nodes':ns})
    response=client.post(f'/api/projects/{p.id}/generate-outline',json={'confirm_bible':True,'goals':['核对'],'preview':False})
    assert response.status_code == 409
    data=response.json()['detail']
    assert data['code']=='clarification_required'
    assert data['verified'] is False
    assert data['unknown_fields']

def test_planning_metadata_survives_execution_transitions():
    from app.generation.timeline import plan_timeline,advance,force_complete_current,PlotNode
    from app.generation.branch import attach_branch_to_state
    st=plan_timeline(10);st.planning={'status':'outline_draft','nodes':nodes()}
    assert advance(st).planning==st.planning
    st.completion={'n1':{'verdict':'supported','sources':[{'quote':'已完成'}]}}
    assert force_complete_current(st).planning==st.planning
    branch=PlotNode('b1','branch','复核','','起点','结果')
    assert attach_branch_to_state(st,branch).planning==st.planning
