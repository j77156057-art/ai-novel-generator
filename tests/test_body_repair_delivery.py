"""Release closing regressions; controlled evidence is not real-model acceptance."""
import json
import pytest
from test_causal_timeline import fixture, Reply


def repair_fixture():
    from app.models import Generation
    from app.agents.diagnostic import run_diagnostic_task
    from app.agents.body_repair import prepare_body_repair
    db,p=fixture()
    text='已在账册写下页数。\n账册从未使用。\n灯仍亮着。'
    g=Generation(project_id=p.id,segment_no=1,text=text);db.add(g);db.commit()
    answer={'assessment':'observed','reason':'冲突','findings':[{'dimension':'object_state','assessment':'contradicted','reason':'已写却未用','source_ids':['G1P001','G1P002']}]}
    diag=run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='body_consistency',allow_experimental=True,segment_nos=[1],provider=Reply(answer))
    start=text.index('账册从未')
    card=prepare_body_repair(db,p.id,diagnostic_run_id=diag['run_id'],generation_id=g.id,start=start,end=start+len('账册从未使用。'),objective='保留已写入事实，消除全称否定，不增加事实',preserve_source_ids=['G1P001'],conflict_source_id='G1P002')
    return db,p,g,card['pending_action']['body']


def test_repeated_confirmation_reuses_pending_without_model_calls():
    from app.agents.body_repair import create_body_repair
    from app.models import EditProposal
    db,p,g,args=repair_fixture()
    writer=Reply({'replacement':'账册已经使用。'})
    assistant=Reply({'assessment':'observed','reason':'一致','findings':[{'dimension':'object_state','assessment':'supported','reason':'两端一致','source_ids':['G1P001','G1P002']}]})
    first=create_body_repair(db,**args,writer=writer,assistant=assistant)
    second=create_body_repair(db,**args,writer=writer,assistant=assistant)
    assert first['proposal']['id']==second['proposal']['id']
    assert len(writer.calls)==len(assistant.calls)==1
    assert db.query(EditProposal).count()==1
    assert second['semantic_accepted'] is False  # missing historical contract stays blocked
    assert second['verification']['run_id']==first['verification']['run_id']


def test_text_apply_invalidates_summary_and_project_memory():
    from app.models import Generation, AgentRun
    from app.agents.proposals import build_proposal,apply_proposal
    db,p=fixture()
    p.summary=['旧手机事实'];p.summary_tree={'l1':{'1':'旧手机事实'},'l2':['旧事实']}
    g=Generation(project_id=p.id,segment_no=1,text='旧正文。',summary={'text':'旧手机事实'})
    run=AgentRun(project_id=p.id,role='assistant',status='success',request_hash='controlled')
    db.add_all([g,run]);db.commit()
    prop=build_proposal(db,run_id=run.id,project_id=p.id,proposal_type='text_edit',target_kind='generation_text',target_id=str(g.id),after_text='新正文。',reason='controlled fixture')
    prop.status='approved';db.commit()
    result=apply_proposal(db,proposal=prop)
    db.refresh(g);db.refresh(p)
    assert g.summary is None
    assert not p.summary and not p.summary_tree
    from app.generation.engine import _prior_summaries
    assert _prior_summaries(p)==[]
    assert result['version_id']==prop.version_id


def test_unverified_repair_cannot_be_approved_via_api():
    """审批拒绝未验收修订；路由函数直调避免内存SQLite跨线程。"""
    from app.models import AgentRun, Generation
    from app.agents.proposals import build_proposal
    db,p=fixture()
    g=Generation(project_id=p.id,segment_no=1,text='旧正文。')
    run=AgentRun(project_id=p.id,role='assistant',status='success',request_hash='controlled')
    db.add_all([g,run]);db.commit()
    prop=build_proposal(db,run_id=run.id,project_id=p.id,proposal_type='text_edit',target_kind='generation_text',target_id=str(g.id),after_text='候选。',reason='test',scope={'independent_verification_required':True})
    db.commit()
    from app.routers.assistant_proposals import approve_proposal, ProposalActionRequest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        approve_proposal(prop.id,ProposalActionRequest(action='approve',actor='test'),db)
    assert error.value.status_code==409
    db.refresh(prop);assert prop.status=='pending'


def test_text_apply_invalidates_timeline_completion_but_preserves_contract():
    from app.models import Generation, AgentRun
    from app.agents.proposals import build_proposal,apply_proposal
    db,p=fixture()
    p.plot_timeline={'completion':{'n1':{'verdict':'supported','sources':['old text']}},'done':['n1'],'last_judged_segment':1,'planning':{'segment_contracts':{'1':{'authority':'frozen'}}}}
    g=Generation(project_id=p.id,segment_no=1,text='旧正文。')
    run=AgentRun(project_id=p.id,role='assistant',status='success',request_hash='controlled')
    db.add_all([g,run]);db.commit()
    prop=build_proposal(db,run_id=run.id,project_id=p.id,proposal_type='text_edit',target_kind='generation_text',target_id=str(g.id),after_text='候选。',reason='test')
    prop.status='approved';db.commit();apply_proposal(db,proposal=prop);db.refresh(p)
    assert not p.plot_timeline['completion'] and not p.plot_timeline['done']
    assert p.plot_timeline['last_judged_segment']==0
    assert p.plot_timeline['planning']['segment_contracts']['1']=={'authority':'frozen'}
    assert p.plot_timeline['body_revision_requires_review']['segment_no']==1
    from app.generation.engine import build_context
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        build_context(db,p,profile='original')
    assert error.value.status_code==409


@pytest.mark.parametrize('key',['semantic_accepted','verification_run_id','confirmation_hash','repair_result'])
def test_clients_cannot_forge_repair_server_metadata(key):
    from app.models import Generation, AgentRun
    from app.agents.proposals import build_proposal, PatchRejected
    db,p=fixture();g=Generation(project_id=p.id,segment_no=1,text='旧正文。')
    run=AgentRun(project_id=p.id,role='assistant',status='success',request_hash='controlled')
    db.add_all([g,run]);db.commit()
    with pytest.raises(PatchRejected):
        build_proposal(db,run_id=run.id,project_id=p.id,proposal_type='text_edit',target_kind='generation_text',target_id=str(g.id),after_text='候选。',reason='test',scope={key:True})
