import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import json
from unittest.mock import patch


def test_diagnostic_tool_observation_keeps_checks_before_large_payload():
    from app.agents.diagnostic import diagnostic_tool
    report = {'task_id': 't', 'observations': [{'data': 'x' * 5000}],
        'run_id': 9, 'status': 'completed', 'assessment': 'unknown', 'model_participated': True,
        'checks': [{'status': 'unknown', 'reason': '接收权限缺失', 'citations': [
            {'source_id': 'S001', 'path': '/premise', 'quote': 'abc', 'location_verified': True}]}],
        'unresolved': ['semantic_review_required'], 'proposed_actions': [
            {'kind': 'planning_candidate_proposal', 'requires_confirmation': True, 'text': 'clarify'}]}
    with patch('app.agents.diagnostic.run_diagnostic_task', return_value=report):
        result = diagnostic_tool(None, 1)
    excerpt = json.dumps(result, ensure_ascii=False)[:2000]
    assert 'S001' in excerpt
    assert '接收权限缺失' in excerpt
    assert 'requires_confirmation' in excerpt
    assert result['run_id'] == 9
    assert result['full_report_ref'] == 'agent_runs:9:note'
