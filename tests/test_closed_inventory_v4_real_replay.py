import json
from pathlib import Path
from app.agents.closed_inventory_v2 import inspect_inventory


def test_real_v4_raw_replay_rechecks_desk_alias_and_negative_controls():
    fixture=json.loads((Path(__file__).parent/'fixtures/closed_inventory_v4_real_verdict.json').read_text(encoding='utf-8'))
    request=fixture['request']['payload']; cs=request['candidates']
    sources={c['mention']['source_id']:dict(source_id='generation:10',quote=c['mention']['citation']['quote'],
        start=c['mention']['citation']['start'],end=c['mention']['citation']['end']) for c in cs}
    authority=cs[0]['constraint']['authority_context']
    sources['C1S096']=dict(source_id='contract:1',path='/authority/premise',quote=authority['quote'],start=0,end=len(authority['quote']))
    previous=dict(run_id=78,inventory=dict(input_fingerprint=None,reviewed_dimensions=['object'],
        constraints=[cs[0]['constraint']],mentions=[c['mention'] for c in cs],
        dimensions=dict(object=dict(total_source_ids=list(sources)[:-1],sent_source_ids=list(sources)[:-1],
            validated_source_ids=list(sources)[:-1],unvalidated_source_ids=[],unsent_source_ids=[],complete=True)),
        rejected_mentions=[],candidates=[]))
    from app.agents.diagnostic import digest
    observation=dict(data=dict(segments=[dict(segment_no=1,generation_id=10)],sources=sources))
    previous['inventory']['input_fingerprint']=digest(observation['data'])
    # IDs regenerated in the same stable order; preserve raw statuses/reasons via source/canonical mapping.
    original=json.loads(fixture['response']['raw'])['verdicts']
    mapping={(c['mention']['source_id'],c['mention']['canonical']):v for c,v in zip(cs,original)}
    class Replay:
        def chat(self,system,user):
            payload=json.loads(user)
            assert payload['stage']=='verdicts'
            assert '类别、位置、时点' in system
            return json.dumps(dict(verdicts=[dict(mapping[(c['mention']['source_id'],c['mention']['canonical'])],candidate_id=c['id']) for c in payload['candidates']]))
    report=dict(unresolved=[])
    inspect_inventory(Replay(),observation,report,protocol=4,kinds=['object'],previous_report=previous)
    results={(c['mention']['source_id'],c['mention']['canonical']):c['verdict']['status'] for c in report['inventory']['candidates']}
    assert results[('G1P001','修复桌')]=='allowed'
    assert results[('G1P009','修复桌')]=='allowed'
    assert results[('G1P001','铅笔')]=='allowed'
    assert results[('G1P006','棉质手套')]=='violated'
    assert results[('G1P009','手机')]=='violated'
    assert results[('G1P006','加固纸')]=='unknown'
    assert report['inventory']['summary']['unknown_entities']==1
