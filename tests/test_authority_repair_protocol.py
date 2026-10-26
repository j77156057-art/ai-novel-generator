"""The independent reviewer must see the same evidence contract as the validator."""
import json
from pathlib import Path
from test_authority_body_repair import authority_fixture
from test_causal_timeline import Reply
from app.agents.authority_body_repair import verify_authority_candidate
from app.agents.body_consistency import build_body_candidate


def setup_review():
    db,p,g,run,args=authority_fixture()
    a=args['preserve_source_ids'][0]
    payload={'preserve_evidence':[{'source_id':a}], 'contract_hash':'fixture',
             'diagnostic_binding':{'target_check_id':'object_N001'}}
    return db,p,g,json.loads(run.note),a,payload


def test_review_prompt_explicitly_discloses_required_endpoints_and_top_level_schema():
    db,p,g,previous,a,payload=setup_review()
    raw=json.loads((Path(__file__).parent/'fixtures/authority_body_repair_response.json').read_text(encoding='utf-8'))
    raw['findings'][0]['source_ids']=[a,'G1P001']
    class Capture(Reply):
        def chat(self,system,user,**kwargs):
            self.system=system
            return super().chat(system,user,**kwargs)
    provider=Capture(raw)
    candidate=build_body_candidate(g.text,0,6,'她看着账册。')
    result=verify_authority_candidate(db,p.id,previous,candidate,g.id,payload,provider)
    system=provider.system
    request=json.loads(provider.calls[0])
    assert request['verification_requirements']=={
        'required_authority_source_ids':[a],
        'changed_body_source_ids':['G1P001'],
        'target_check_id':'object_N001',
        'scope':'local_only',
    }
    assert '顶层assessment必须为observed' in system
    assert 'required_authority_source_ids' in system
    assert 'changed_body_source_ids' in system
    assert not result['local_semantic_accepted']
    assert 'invalid_top_level_assessment' in result['unresolved']


def test_unchanged_body_citation_cannot_certify_changed_range():
    db,p,g,previous,a,payload=setup_review()
    candidate=build_body_candidate(g.text,0,6,'她看着账册。')
    provider=Reply({'assessment':'observed','reason':'一致','findings':[
        {'dimension':'object_state','assessment':'supported','reason':'账册未动',
         'source_ids':[a,'G1P002']}]})
    result=verify_authority_candidate(db,p.id,previous,candidate,g.id,payload,provider)
    assert not result['local_semantic_accepted']
    assert 'missing_changed_body_evidence' in result['unresolved']


def test_exact_local_endpoints_do_not_clear_global_obligations():
    db,p,g,previous,a,payload=setup_review()
    candidate=build_body_candidate(g.text,0,6,'她看着账册。')
    provider=Reply({'assessment':'observed','reason':'局部一致','findings':[
        {'dimension':'object_state','assessment':'supported','reason':'移除手套依赖',
         'source_ids':[a,'G1P001']}]})
    result=verify_authority_candidate(db,p.id,previous,candidate,g.id,payload,provider)
    assert result['local_semantic_accepted']
    assert not result['global_quality_verified']
    assert 'inventory_partial_coverage' in result['unresolved']


def test_real_v2_response_is_locally_accepted_but_keeps_global_obligations():
    """局部检查只判定目标维度；时间/地点/参与者不在局部范围，unknown 不是失败。

    这正是上一轮卡住的地方：局部复验要求四维全 supported，而请求标的是 local_only，
    模型对未检查的三维如实返回 unknown —— 于是「正确的局部修复」永远无法被接受。
    现在：目标维度判定通过即构成局部接受；三维的 unknown 记入 deferred_dimensions，
    整段四维由独立���整段验收负责，全局义务一条都不许因此被清掉。
    """
    db,p,g,previous,a,payload=setup_review()
    answer=json.loads((Path(__file__).parent/'fixtures/authority_body_repair_review_v2.json').read_text(encoding='utf-8'))
    assert answer['assessment']=='observed'
    assert answer['findings'][0]['source_ids']==['C1S096','G1P006']
    # Map references only; preserve all original verdicts and reasons.
    for finding in answer['findings']:
        finding['source_ids']=[a,'G1P001']
    candidate=build_body_candidate(g.text,0,6,'她看着账册。')
    result=verify_authority_candidate(db,p.id,previous,candidate,g.id,payload,Reply(answer))
    assert [c['status'] for c in result['checks']]==['supported','unknown','unknown','unknown']
    assert result['local_semantic_accepted']
    assert not result['global_quality_verified']
    assert 'inventory_partial_coverage' in result['unresolved']
    assert 'local_semantic_review_required' not in result['unresolved']
    assert set(result.get('deferred_dimensions') or [])>={'time','place','participants'}
    # 协议项仍然成立：顶层是 observed，引用的权限端与改动段都对。
    assert 'invalid_top_level_assessment' not in result['unresolved']
    assert 'missing_required_authority_evidence' not in result['unresolved']
    assert 'missing_changed_body_evidence' not in result['unresolved']


def test_target_dimension_not_resolved_is_not_locally_accepted():
    """目标维度仍是 contradicted/unknown → 局部不放行（与三维是否 deferred 无关）。"""
    db,p,g,previous,a,payload=setup_review()
    candidate=build_body_candidate(g.text,0,6,'她看着账册。')
    provider=Reply({'assessment':'observed','reason':'仍有依赖','findings':[
        {'dimension':'object_state','assessment':'contradicted','reason':'手套依赖仍在','source_ids':[a,'G1P001']},
        {'dimension':'time','assessment':'unknown','reason':'未检查','source_ids':[a]},
        {'dimension':'place','assessment':'unknown','reason':'未检查','source_ids':[a]},
        {'dimension':'participants','assessment':'unknown','reason':'未检查','source_ids':[a]}]})
    result=verify_authority_candidate(db,p.id,previous,candidate,g.id,payload,provider)
    assert not result['local_semantic_accepted']
    assert 'local_semantic_review_required' in result['unresolved']

