from test_foundation_planning import api, nodes
from app.generation.planning import timeline_for, source_fingerprint


def test_explicit_budget_revision_preserves_completion_and_history(api):
    client, db, p = api
    p.plot_timeline = timeline_for(nodes(), {'verified': False}, 3)
    p.plot_timeline = {**p.plot_timeline, 'seg_counts': {'n1': 1},
                       'completion': {'n1': {'verdict': 'unknown', 'sources': []}}}
    db.commit()
    before = p.plot_timeline
    url = f'/api/projects/{p.id}/outline/revise-budget'
    body = {'source_fingerprint': source_fingerprint(p), 'node_id': 'n1',
            'segment_budget': 2, 'reason': '作者明确增加一段核对过程', 'confirm': True}
    response = client.post(url, json=body)
    assert response.status_code == 200
    got = client.get(f'/api/projects/{p.id}').json()['plot_timeline']
    assert got['nodes']['n1']['segment_budget'] == 2
    assert got['completion'] == before['completion']
    assert got['done'] == [] and got['seg_counts'] == {'n1': 1}
    assert got['planning']['budget_revisions'][-1]['reason'] == body['reason']
    assert client.post(url, json=body).status_code == 409
    from app.generation.engine import current_node_prompt
    db.refresh(p)
    assert 'needs_review' not in current_node_prompt(p, 1)
