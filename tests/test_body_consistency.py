import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from test_causal_timeline import fixture, Reply
from app.generation import engine
from app.generation.timeline import judge_node_completion


def test_short_ids_resolve_unicode_and_completion_audit():
    db, p = fixture()
    node = engine._timeline_state_for(p, 0).nodes['n1']
    class Select:
        def chat(self, system, user):
            assert 'P002' in user
            return json.dumps({'verdict':'supported','reason':'已找到','source_ids':['P002']})
    text = '😀门边。\n\n找到钥匙。'
    r = judge_node_completion(Select(), node, text, source_id='generation:1')
    assert r['verdict'] == 'supported'
    assert r['sources'][0]['start'] == text.index('找到')
    assert r['sources'][0]['quote'] == '找到钥匙。'
    assert r['audit']['source_id'] == 'generation:1'
    assert r['audit']['plan_hash'] and r['audit']['text_hash']
    assert json.loads(r['audit']['response_raw'])['source_ids'] == ['P002']


def test_experiment_success_requires_semantic_acceptance():
    from app.agents.body_consistency import experiment_outcome
    report = {'source_unchanged':True, 'original_body_unchanged':True, 'stages':{
        'diagnosis':{'status':'completed'}, 'writer':{'status':'pending','outside_range_unchanged':True},
        'verification':{'status':'completed','unresolved':[], 'checks':[{'status':'contradicted'}]}}}
    assert experiment_outcome(report)['success'] is False
    report['stages']['verification']['checks'] = [{'status':'supported'}]
    assert experiment_outcome(report)['success'] is True
    report['stages']['verification']['unresolved'] = ['missing_saved_contract']
    assert experiment_outcome(report)['success'] is False


def test_actual_response_replay_accepts_documented_bounded_references():
    from app.agents.body_consistency import validate_findings
    fixture = json.loads((Path(__file__).parent/'fixtures/body_consistency_actual_response.json').read_text(encoding='utf-8'))
    answer = json.loads(fixture['response_raw'])
    sources = {sid:{'source_id':'generation:10','quote':sid,'start':0,'end':len(sid)}
               for f in answer['findings'] for sid in f['source_ids']}
    checks = validate_findings(answer, sources)
    assert len(checks[-1]['citations']) == 7
    assert checks[-1]['status'] == 'supported'  # Known false negative retained, never "corrected" in replay.


def test_completion_invalid_response_audit_and_sanitation():
    db, p = fixture()
    node = engine._timeline_state_for(p, 0).nodes['n1']
    for data, category in (({'verdict':'supported','reason':'bad','source_ids':['P999']}, 'unknown_or_missing_source_id'),
                           ({'verdict':'supported','reason':'bad','sources':[{'source_id':'generation:1','quote':'找到钥匙。','start':1,'end':6}]}, 'source_location_mismatch')):
        r = judge_node_completion(Reply(data), node, '找到钥匙。', source_id='generation:1')
        assert r['verdict'] == 'unknown'
        assert r['audit']['error_category'] == category
        assert json.loads(r['audit']['response_raw']) == data
    class Raw:
        def chat(self, *args): return '{"verdict":'
    r = judge_node_completion(Raw(), node, '正文')
    assert r['audit']['error_category'] == 'invalid_json'
    assert r['audit']['response_raw'] == '{"verdict":'
    from app.agents.body_evidence import safe_response
    safe = safe_response('<think>hidden</think> {"api_key":"secret", "reasoning_content":"hidden2", "reason":"visible"} Bearer token123 sk-abc123')
    assert all(s not in safe for s in ['hidden','secret','token123','abc123'])
    assert 'visible' in safe
    legacy = judge_node_completion(Reply({'verdict':'unknown','reason':'未完成','sources':[]}), node, '正文')
    assert legacy['audit']['response_raw']
    assert legacy['audit']['error_category'] is None


def test_candidate_fixed_range_pending_proposal_and_read_only_overlay():
    from app.agents.body_consistency import build_body_candidate, candidate_observation
    from app.models import Generation
    from app.agents.diagnostic import read_capability
    from app.agents.proposals import build_proposal
    from app.models import AgentRun
    import pytest
    db, p = fixture()
    text = '😀他已写账册。\n账册未用。\n结束。'
    g = Generation(project_id=p.id, segment_no=1, text=text)
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='test')
    db.add_all([g, run]); db.commit()
    start = text.index('账册未用')
    candidate = build_body_candidate(text, start, start + len('账册未用。'), '账册已有记录。')
    assert candidate['after_text'] == text[:start] + '账册已有记录。' + text[start+len('账册未用。'):]
    observation = read_capability(db, p.id, 'read_body_consistency', {'segment_nos':[1]})
    overlay = candidate_observation(observation, g.id, candidate)
    assert '账册已有记录。' in str(overlay)
    assert db.get(Generation, g.id).text == text
    stale = dict(candidate, base_content_hash='wrong')
    with pytest.raises(ValueError, match='stale_candidate'):
        candidate_observation(observation, g.id, stale)
    proposal = build_proposal(db, run_id=run.id, project_id=p.id, proposal_type='text_edit',
        target_kind='generation_text', target_id=str(g.id), after_text=candidate['after_text'],
        reason='fixed scope', scope={'fixed_range':candidate['fixed_range']})
    db.commit(); db.refresh(proposal)
    assert proposal.status == 'pending'
    assert proposal.scope_json['base_content_hash'] == candidate['base_content_hash']
    assert db.get(Generation, g.id).text == text
    for a,b in ((-1,2),(True,2),(2,1),(0,len(text)+1)):
        with pytest.raises(ValueError): build_body_candidate(text,a,b,'new')


def test_body_diagnostic_two_endpoints_unknown_and_invalid_ids():
    from app.models import Generation
    from app.agents.diagnostic import run_diagnostic_task
    from sqlalchemy import event
    db, p = fixture()
    body = '他在账册写下页数。\n账册从未使用。\n他想起昨天的账册，重复说了一遍。'
    db.add(Generation(project_id=p.id, segment_no=1, text=body)); db.commit()
    def guard(conn, cursor, sql, *args):
        if sql.lstrip().split()[0].upper() in {'INSERT','UPDATE','DELETE'}:
            assert 'agent_runs' in sql.lower() or 'evidence' in sql.lower()
    event.listen(db.bind, 'before_cursor_execute', guard)
    class Review:
        def __init__(self, ids): self.ids = ids
        def chat(self, system, user):
            assert '回忆' in system and '谎言' in system and '有意重复' in system
            return json.dumps({'assessment':'observed','reason':'正文物件状态矛盾','findings':[
                {'dimension':'object_state','assessment':'contradicted','reason':'先用后未用','source_ids':self.ids},
                {'dimension':'time','assessment':'unknown','reason':'回忆不等于时间冲突','source_ids':[]}]})
    kw = dict(task_type='body_consistency', skill_id='body_consistency', allow_experimental=True, segment_nos=[1])
    good = run_diagnostic_task(db, p.id, provider=Review(['G1P001','G1P002']), **kw)
    assert good['status'] == 'completed'
    assert len(good['checks'][0]['citations']) == 2
    assert good['checks'][1]['status'] == 'unknown'
    assert good['unresolved']
    for ids in (['G1P001'], ['G1P001','INVENTED'], ['G1P001','G1P001']):
        bad = run_diagnostic_task(db, p.id, provider=Review(ids), **kw)
        assert bad['status'] == 'failed'
        assert bad['model_error_category'] == 'invalid_body_evidence'
    assert db.query(Generation).filter_by(project_id=p.id).one().text == body
    from app.agents.body_consistency import build_body_candidate
    candidate = build_body_candidate(body, body.index('账册从未'), body.index('账册从未')+len('账册从未使用。'), '账册已用。')
    verified = run_diagnostic_task(db, p.id, task_type='repair_verification', skill_id='body_consistency',
        allow_experimental=True, segment_nos=[1], previous_run_id=good['run_id'],
        candidate_overlay={'generation_id':db.query(Generation).filter_by(project_id=p.id).one().id, 'candidate':candidate},
        provider=Review(['G1P001','G1P002']))
    assert verified['status'] == 'completed'
    assert verified['observations'][0]['candidate_overlay']['business_written'] is False
    assert verified['input_changed']
    assert db.query(Generation).filter_by(project_id=p.id).one().text == body


def test_body_read_explicit_segments_saved_contract_and_no_writes():
    from app.models import Generation
    from app.agents.diagnostic import read_capability
    from sqlalchemy import event
    import pytest
    db, p = fixture()
    timeline = dict(p.plot_timeline)
    timeline['planning'] = {'segment_contracts': {'1': {'node_id':'n1', 'prompt':'同一房间，钥匙遗失'}}}
    p.plot_timeline = timeline
    db.add_all([Generation(project_id=p.id, segment_no=1, text='找到钥匙。\n钥匙一直未找到。'),
                Generation(project_id=p.id, segment_no=2, text='范围外正文')])
    db.commit()
    def guard(conn, cursor, sql, *args):
        assert sql.lstrip().split()[0].upper() not in {'INSERT','UPDATE','DELETE','CREATE','ALTER'}
    event.listen(db.bind, 'before_cursor_execute', guard)
    with pytest.raises(ValueError, match='segment_scope_required'):
        read_capability(db, p.id, 'read_body_consistency', {})
    a = read_capability(db, p.id, 'read_body_consistency', {'segment_nos':[1]})
    assert a['data']['segments'][0]['contract']['prompt'] == '同一房间，钥匙遗失'
    assert '范围外正文' not in json.dumps(a, ensure_ascii=False)
    assert len(a['data']['sources']) == 2
    b = read_capability(db, p.id, 'read_body_consistency', {'segment_nos':[2]})
    assert b['data']['segments'][0]['contract'] is None
    assert b['data']['segments'][0]['unknown'] == 'missing_saved_contract'
    with pytest.raises(PermissionError, match='segment_not_found'):
        read_capability(db, p.id, 'read_body_consistency', {'segment_nos':[3]})

