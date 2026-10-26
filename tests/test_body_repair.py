"""Local repair protocol regression; true previous Writer failure."""
import json
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents.body_consistency import build_body_candidate


def test_production_repair_is_pending_and_requires_independent_acceptance():
    from test_causal_timeline import fixture, Reply
    from app.models import AgentRun, Generation, EditProposal
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p = fixture()
    text = '他在账册写下页数。\n账册从未使用。\n灯灭了。'
    g = Generation(project_id=p.id, segment_no=1, text=text)
    db.add(g); db.commit()
    from app.agents.diagnostic import run_diagnostic_task
    answer = {'assessment':'observed','reason':'同一账册冲突','findings':[{'dimension':'object_state','assessment':'contradicted','reason':'已写却否认使用','source_ids':['G1P001','G1P002']}]}
    diag = run_diagnostic_task(db,p.id,task_type='body_consistency',skill_id='body_consistency',allow_experimental=True,segment_nos=[1],provider=Reply(answer))
    start=text.index('账册从未'); end=start+len('账册从未使用。')
    args = dict(diagnostic_run_id=diag['run_id'], generation_id=g.id, start=start, end=end,
        objective='保留账册已写入事实；消除全否定，不添加行动或结果', preserve_source_ids=['G1P001'], conflict_source_id='G1P002')
    from sqlalchemy import event
    def no_writes(conn,cursor,sql,*args):
        assert sql.lstrip().split()[0].upper() not in {'INSERT','UPDATE','DELETE','CREATE','ALTER'}
    event.listen(db.bind,'before_cursor_execute',no_writes)
    with db.no_autoflush:
        card = prepare_body_repair(db,p.id,**args)
    # preserve 集合不能空着走：服务端能从矛盾的另一端推出来就用推出来的
    #（显式给空 = 「没给」）；推不出来时仍然拒绝 —— 见下面那条测试。
    derived = prepare_body_repair(db,p.id,**dict(args,preserve_source_ids=[]))
    assert derived['pending_action']['body']['preserve_source_ids']==['G1P001']
    event.remove(db.bind,'before_cursor_execute',no_writes)
    assert card['pending_action']['endpoint'] == '/api/assistant/body-repairs'
    assert db.query(EditProposal).count() == 0
    class Writer:
        calls=0
        def chat(self,system,user):
            self.calls+=1
            data=json.loads(user)
            assert 'context' not in data and 'full_paragraph' not in data
            assert data['selected_text']=='账册从未使用。'
            assert data['objective']==args['objective']
            return json.dumps({'replacement':'账册尚未使用。'})
    writer=Writer()
    result=create_body_repair(db,p.id,**args,base_content_hash=card['pending_action']['body']['base_content_hash'],writer=writer,assistant=Reply(answer))
    assert writer.calls==1
    assert result['semantic_accepted'] is False
    assert result['verification_acceptance']['verification_scope']=='local_repair_verification'
    assert result['verification_acceptance']['local_repair_verification']=='unknown'
    assert result['proposal']['verification_acceptance']==result['verification_acceptance']
    assert result['proposal']['status']=='pending'
    assert db.get(Generation,g.id).text==text
    assert result['verification']['checks'][0]['status']=='contradicted'
    from app.agents.proposals import apply_proposal, InvalidTransition
    proposal=db.query(EditProposal).one();proposal.status='approved';db.commit()
    with pytest.raises(InvalidTransition,match='semantic'):
        apply_proposal(db,proposal=proposal)


@pytest.mark.parametrize('text,replacement',[
    ('星舰传来暗号。\n回声重复暗号。\n恒星仍在燃烧。','回声再次重复暗号。'),
    ('“门没有开。”他撒谎。\n门开着。\n她继续看书。','门仍开着。'),
    ('听说剑已折。\n她只听到传闻。\n远山仍在。','她尚未核实传闻。'),
])
def test_normal_repetition_denial_dialogue_and_rumour_are_not_deduplicated(text,replacement):
    start=text.index('\n')+1;end=text.index('\n',start)
    candidate=build_body_candidate(text,start,end,replacement)
    assert candidate['after_text']==text[:start]+replacement+text[end:]


def test_a_repair_without_any_derivable_scope_is_still_refused():
    """既没给范围、也没给矛盾 → 推不出来 → 仍要人来澄清（不能空着走）。"""
    from test_causal_timeline import fixture, Reply
    from app.models import Generation
    from app.agents.body_repair import prepare_body_repair
    from app.agents.diagnostic import run_diagnostic_task
    db, p = fixture()
    text = '他在账册写下页数。\n账册从未使用。\n灯灭了。'
    g = Generation(project_id=p.id, segment_no=1, text=text)
    db.add(g); db.commit()
    answer = {'assessment': 'observed', 'reason': '同一账册冲突', 'findings': [
        {'dimension': 'object_state', 'assessment': 'contradicted',
         'reason': '已写却否认使用', 'source_ids': ['G1P001', 'G1P002']}]}
    diag = run_diagnostic_task(db, p.id, task_type='body_consistency',
                               skill_id='body_consistency', allow_experimental=True,
                               segment_nos=[1], provider=Reply(answer))
    with pytest.raises(ValueError, match='clarification'):
        prepare_body_repair(db, p.id, diagnostic_run_id=diag['run_id'],
                            objective='消除全否定')


def test_the_server_resolves_the_range_from_the_conflict_source():
    """agent 只要说清**哪个矛盾**，区间与要保留的事实由服务端推。

    字符偏移是机械活：source 自带行内跨度，引用里带着确切文本。实测（真书项目 8）
    agent 传了 `start=1, end=2`（猜的）→ 被 `repair_scope_denied` 拒掉。
    """
    from test_causal_timeline import fixture, Reply
    from app.models import EditProposal, Generation
    from app.agents.body_repair import prepare_body_repair
    from app.agents.diagnostic import run_diagnostic_task
    db, p = fixture()
    text = '他在账册写下页数。\n账册从未使用。\n灯灭了。'
    g = Generation(project_id=p.id, segment_no=1, text=text)
    db.add(g); db.commit()
    answer = {'assessment': 'observed', 'reason': '同一账册冲突', 'findings': [
        {'dimension': 'object_state', 'assessment': 'contradicted',
         'reason': '已写却否认使用', 'source_ids': ['G1P001', 'G1P002']}]}
    diag = run_diagnostic_task(db, p.id, task_type='body_consistency',
                               skill_id='body_consistency', allow_experimental=True,
                               segment_nos=[1], provider=Reply(answer))

    card = prepare_body_repair(
        db, p.id, diagnostic_run_id=diag['run_id'], conflict_source_id='G1P002',
        objective='保留账册已写入事实；消除全否定，不添加行动或结果')

    body = card['pending_action']['body']
    assert body['generation_id'] == g.id, '段落应从矛盾所在的 source 推出来'
    assert body['start'] == text.index('账册从未使用。')
    assert body['end'] == body['start'] + len('账册从未使用。')
    assert body['preserve_source_ids'] == ['G1P001'], '要保留的是矛盾的另一端'
    # 只构造卡：什么也不落库
    assert db.query(EditProposal).count() == 0


def test_the_derived_preserve_set_stays_inside_the_edited_segment():
    """跨段的矛盾：要保留的事实只能取**同一段**里的。

    局部修订只能保住「本段里已确认的事实」——把另一段的来源塞进保留集，
    会被下游按「范围不对」拒掉（实测真书项目 8：矛盾横跨第 1、2 段，
    保留集混进第 2 段 → repair_scope_denied）。
    """
    from test_causal_timeline import fixture, Reply
    from app.models import Generation
    from app.agents.body_repair import prepare_body_repair
    from app.agents.diagnostic import run_diagnostic_task
    db, p = fixture()
    t1 = '他在账册写下页数。\n账册从未使用。\n灯灭了。'
    t2 = '天亮了。\n风停了。'
    g1 = Generation(project_id=p.id, segment_no=1, text=t1)
    g2 = Generation(project_id=p.id, segment_no=2, text=t2)
    db.add_all([g1, g2]); db.commit()
    answer = {'assessment': 'observed', 'reason': '跨段矛盾', 'findings': [
        {'dimension': 'time', 'assessment': 'contradicted',
         'reason': '第1段与第2段对不上', 'source_ids': ['G1P001', 'G1P002', 'G2P001']}]}
    diag = run_diagnostic_task(db, p.id, task_type='body_consistency',
                               skill_id='body_consistency', allow_experimental=True,
                               segment_nos=[1, 2], provider=Reply(answer))
    card = prepare_body_repair(db, p.id, diagnostic_run_id=diag['run_id'],
                               conflict_source_id='G1P002', objective='消除矛盾')
    body = card['pending_action']['body']
    assert body['generation_id'] == g1.id
    assert body['preserve_source_ids'] == ['G1P001'], '另一段的来源不能进保留集'


def test_an_explicit_range_still_wins_over_the_resolved_one():
    """人自己改过的范围优先 —— 服务端解析只是兜底。"""
    from test_causal_timeline import fixture, Reply
    from app.models import Generation
    from app.agents.body_repair import prepare_body_repair
    from app.agents.diagnostic import run_diagnostic_task
    db, p = fixture()
    text = '他在账册写下页数。\n账册从未使用。\n灯灭了。'
    g = Generation(project_id=p.id, segment_no=1, text=text)
    db.add(g); db.commit()
    answer = {'assessment': 'observed', 'reason': '同一账册冲突', 'findings': [
        {'dimension': 'object_state', 'assessment': 'contradicted',
         'reason': '已写却否认使用', 'source_ids': ['G1P001', 'G1P002']}]}
    diag = run_diagnostic_task(db, p.id, task_type='body_consistency',
                               skill_id='body_consistency', allow_experimental=True,
                               segment_nos=[1], provider=Reply(answer))
    start = text.index('账册从未'); end = start + len('账册从未使用。')
    card = prepare_body_repair(
        db, p.id, diagnostic_run_id=diag['run_id'], conflict_source_id='G1P002',
        start=start, end=end, objective='消除全否定',
        preserve_source_ids=['G1P001'])
    body = card['pending_action']['body']
    assert (body['start'], body['end']) == (start, end)


def test_proposal_rejects_outside_fixed_range_changes():
    from test_causal_timeline import fixture
    from app.models import Generation, AgentRun
    from app.agents.proposals import build_proposal, PatchRejected
    db,p=fixture();g=Generation(project_id=p.id,segment_no=1,text='前文。错误。后文。')
    run=AgentRun(project_id=p.id,role='assistant',status='success',request_hash='x');db.add_all([g,run]);db.commit()
    with pytest.raises(PatchRejected,match='fixed_range'):
        build_proposal(db,run_id=run.id,project_id=p.id,proposal_type='text_edit',target_kind='generation_text',target_id=str(g.id),
            after_text='改前文。正确。后文。',reason='定点修订',scope={'fixed_range':{'start':3,'end':6,'replacement':'正确。'}})


def test_read_only_cards_are_collected_without_execution():
    from app.agents.body_repair import collect_body_repair_cards
    action={'kind':'body_repair','endpoint':'/api/assistant/body-repairs','method':'POST','body':{'project_id':4}}
    assert collect_body_repair_cards({'prepare_body_repair':{'pending_action':action}},4)==[action]
    assert collect_body_repair_cards({'other':{'pending_action':action}},4)==[]
    assert collect_body_repair_cards({'prepare_body_repair':{'pending_action':action}},5)==[]


def test_assistant_repair_discovery_and_write_endpoint():
    from app.agents.registry import get_tool_registry
    reg=get_tool_registry()
    assert reg.resolve('prepare_body_repair').read_only is True
    assert reg.resolve('prepare_body_repair').parameters['properties']['preserve_source_ids']['type']=='array'
    from app.routers.assistant_proposals import router
    assert any(r.path=='/api/assistant/body-repairs' and 'POST' in r.methods for r in router.routes)
    from app.agents.diagnostic_skills import load_skill
    skill=load_skill('body_consistency',2,allow_experimental=True)
    assert skill['status']=='experimental' and skill['supersedes']==1


def test_deletion_requires_explicit_noncritical_authorization():
    text='已登记。\n从未登记。\n继续。'
    start=text.index('从未');end=start+len('从未登记。')
    with pytest.raises(ValueError): build_body_candidate(text,start,end,'')
    assert build_body_candidate(text,start,end,'',allow_delete=True)['after_text']==text[:start]+text[end:]


def test_actual_writer_suffix_echo_is_rejected():
    text = '陈舟写下第一行。\n桌面上的刷、压板和登记本都还没有使用，旧图册保持摊开的状态，等着下一步。'
    start = text.index('桌面'); end = start + len('桌面上的刷、压板和登记本都还没有使用')
    with pytest.raises(ValueError, match='candidate_boundary_echo'):
        build_body_candidate(text, start, end, '桌面上的刷、压板和登记本都尚未使用，旧图册保持摊开的状态，等着下一步。')
