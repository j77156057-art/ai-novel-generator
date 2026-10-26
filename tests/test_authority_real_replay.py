"""Offline replay of the single saved real response; no model/API calls."""
import json
from pathlib import Path
from test_authority_body_repair import authority_fixture
from app.agents.authority_body_repair import verify_authority_candidate
from app.agents.body_consistency import build_body_candidate
from test_causal_timeline import Reply


def test_real_response_cannot_claim_local_or_global_acceptance_without_required_endpoint():
    saved=json.loads((Path(__file__).resolve().parents[1]/'tests/fixtures/authority_body_repair_response.json').read_text(encoding='utf-8'))
    assert saved['assessment']=='supported'
    assert saved['findings'][0]['source_ids']==['C1S084','G1P006']
    db,p,g,run,args=authority_fixture()
    previous=json.loads(run.note)
    # Replay original structure with source IDs mapped to fixture authority.
    # It still violates the explicit observed/required-preservation protocol.
    saved['findings'][0]['source_ids']=[args['preserve_source_ids'][0],'G1P001']
    candidate=build_body_candidate(g.text,0,6,'她看着账册。')
    result=verify_authority_candidate(db,p.id,previous,candidate,g.id,
        {'preserve_evidence':[{'source_id':args['preserve_source_ids'][0]}],
         'contract_hash':'fixture','diagnostic_binding':{'target_check_id':'object_N001'}},Reply(saved))
    assert not result['local_semantic_accepted']
    assert result['unresolved']
