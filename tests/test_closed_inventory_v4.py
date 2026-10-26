import copy
import json
from test_closed_inventory_v3 import LiteralReview
from test_closed_inventory_v2 import scoped_db
from app.agents.closed_inventory_v2 import inspect_inventory


def test_v4_converges_source_entities_without_losing_provenance():
    from app.agents.closed_inventory_v2 import converge_candidates
    c = dict(kind='object', mode='closed', entities=[dict(canonical='铅笔', aliases=['笔'])], source_id='C1', segment_no=1)
    def mention(name, state='actual', sid='G1', relation='held'):
        return dict(canonical=name, state=state, source_id=sid, kind='object', segment_no=1,
                    surface=name, relation=relation, locations=[dict(start=0,end=len(name))])
    ms=[mention('笔'),mention('剑'),mention('剑',relation='seen'),mention('剑','metaphor'),mention('剑',sid='G2')]
    result=converge_candidates(ms,[c])
    assert result['counts']==dict(mentions=5, independent_entities=4, authorized_entities=1, duplicate_mentions_merged=1, pending_entities=3)
    assert len(result['candidates'][0]['provenance'])==2
    assert len(result['authorized'][0]['provenance'])==1
    assert {m['state'] for c in result['candidates'] for m in c['provenance']}=={'actual','metaphor'}


def test_v4_saved_mentions_reuse_full_authority_with_new_verdict_version(tmp_path):
    from app.agents.closed_inventory_v2 import converge_candidates
    db,p=scoped_db()
    from app.agents.diagnostic import run_diagnostic_task
    base=run_diagnostic_task(db,p.id,provider=LiteralReview(),skill_version=3,task_type='body_consistency',
        skill_id='closed_inventory',inventory_kinds=['object'],allow_experimental=True,segment_nos=[1])
    provider=LiteralReview()
    result=run_diagnostic_task(db,p.id,provider=provider,skill_version=4,task_type='body_consistency',
        skill_id='closed_inventory',inventory_kinds=['object'],allow_experimental=True,segment_nos=[1],
        previous_run_id=base['run_id'],inventory_verdict_source_ids=['G1P001'])
    assert result['status']=='completed',result
    assert [d['stage'] for d in provider.calls]==['verdicts']
    inv=result['inventory']
    assert inv['history']['previous_run_id']==base['run_id']
    assert inv['history']['verdicts_invalidated_reason']=='authority_protocol_candidate_version_changed'
    assert len(inv['history']['verdicts'])==2
    assert inv['candidate_version']==1
    assert inv['summary']['partial'] is True
    assert inv['summary']['success'] is False
    assert inv['summary']['unknown_entities']==0
    assert inv['summary']['unreviewed_dimensions']==['person','place']
    # Body changes cannot reuse prior mentions or verdicts.
    from app.models import Generation
    g=db.query(Generation).filter_by(project_id=p.id).one()
    g.text+='新增文本。'; db.commit()
    refused=run_diagnostic_task(db,p.id,provider=LiteralReview(),skill_version=4,task_type='body_consistency',
        skill_id='closed_inventory',inventory_kinds=['object'],allow_experimental=True,segment_nos=[1],previous_run_id=base['run_id'])
    assert refused['status']=='failed'
    assert refused['model_error_category']=='inventory_checkpoint_mismatch'
    payload=provider.calls[0]
    assert payload['protocol_version']==4 and payload['candidate_version']==1
    assert payload['candidates'][0]['constraint']['authority_context']['quote']=='室内仅有扳手。'


def test_verdict_payload_has_exactly_one_body_source_id_meaning():
    """候选里不能同时存在两种含义的 body_source_id。

    citation 自带的 body_source_id 是正文端点（generation:10），而校验要求模型回
    段落ID（G1P001）。同名不同义时模型会照着眼前的字段回 → 整批作废
    （实测 20/20 全错）。发出去的候选必须只剩一种含义。
    """
    from app.agents.closed_inventory_v2 import _verdict_view
    candidate = {
        'id': 'N001',
        'mention': {'source_id': 'G1P001', 'surface': '焊枪', 'state': 'actual',
                    'citation': {'source_id': 'G1P001', 'body_source_id': 'generation:10'}},
        'constraint': {'source_id': 'C1S096', 'mode': 'closed',
                       'citation': {'source_id': 'C1S096', 'body_source_id': 'generation:10'}},
    }
    view = _verdict_view(candidate, {})
    assert view['body_source_id'] == 'G1P001'
    assert view['authority_source_id'] == 'C1S096'
    found = []

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'body_source_id':
                    found.append(value)
                else:
                    collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(view)
    assert found == ['G1P001']


def test_verdict_group_size_is_bounded():
    """全范围审计候选可达上百：单次裁决必须封顶，否则响应被截断整批判废。"""
    from app.agents.closed_inventory_v2 import MAX_VERDICT_GROUP
    assert 0 < MAX_VERDICT_GROUP <= 24