import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents.react_loop import build_tool_catalog, validate_arguments
from app.agents.registry import get_tool_registry


def test_diagnostic_discovery_exposes_contract_without_opt_in_execution():
    catalog = {t.name: t for t in build_tool_catalog()}
    assert 'list_diagnostic_skills' in catalog
    tool = get_tool_registry().resolve('list_diagnostic_skills')
    result = tool.callable(None, 1)
    planning = next(s for s in result['skills'] if s['id'] == 'planning_review')
    assert planning['status'] == 'experimental'
    assert planning['requires_experimental_opt_in'] is True
    assert planning['required_capabilities']
    assert result['business_read_only'] is True
    assert 'allow_experimental' in result['authorization_note']
    assert not validate_arguments(catalog['run_diagnostic_task'].parameters,
        {'novel_ids': [1], 'allow_experimental': True, 'cursor': 2})
    assert validate_arguments(catalog['run_diagnostic_task'].parameters, {'project_id': 2})
