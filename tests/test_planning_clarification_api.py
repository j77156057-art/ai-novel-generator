from test_foundation_planning import api, nodes
from app.generation.planning import build_clarification_report, source_fingerprint


def test_complete_draft_is_never_semantically_verified():
    report = build_clarification_report(nodes(), 'fp')
    assert report['status'] == 'outline_draft'
    assert report['structurally_complete'] is True
    assert report['verified'] is False


def test_unknown_optional_cost_does_not_block():
    ns = nodes(); ns[0]['cost'] = 'unknown'
    report = build_clarification_report(ns, 'fp')
    assert report['unknown_fields'] == []
    assert report['verified'] is False


def test_clarification_api_readonly_stale_and_apply(api):
    client, db, p = api
    ns = nodes(); ns[0]['time'] = 'unknown'
    candidate = {'nodes': ns, 'source_fingerprint': source_fingerprint(p),
                 'status': 'clarification_required', 'degraded': False}
    before = client.get(f'/api/projects/{p.id}').json()
    url = f'/api/projects/{p.id}/outline/clarify'
    response = client.post(url, json={'candidate': candidate, 'answers': {'n1.time': '03:00'}})
    assert response.status_code == 200
    clarified = response.json()
    assert clarified['status'] == 'outline_draft'
    assert clarified['verified'] is False
    assert clarified['nodes'][0]['time'] == '03:00'
    assert client.get(f'/api/projects/{p.id}').json() == before
    assert client.post(url, json={'candidate': candidate, 'answers': {'n1.result': 'invented'}}).status_code == 422
    candidate['source_fingerprint'] = 'stale'
    assert client.post(url, json={'candidate': candidate, 'answers': {}}).status_code == 409
    response = client.post(f'/api/projects/{p.id}/outline/apply', json={'candidate': clarified})
    assert response.status_code == 200
    saved = client.get(f'/api/projects/{p.id}').json()
    assert saved['plot_timeline']['planning']['verified'] is False
    assert saved['plot_timeline']['planning']['nodes'][0]['time'] == '03:00'
