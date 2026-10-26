import json
from app.agents.registry import invoke_tool
from app.agents.diagnostic import run_diagnostic_task
from test_closed_inventory_v3 import LiteralReview
from test_closed_inventory_v2 import scoped_db


def test_v4_public_tool_keeps_partial_summary(monkeypatch, experimental_consent):
    from app.routers import assistant_shared
    db,p=scoped_db()
    monkeypatch.setattr(assistant_shared,'_ensure_assistant_provider',lambda:LiteralReview())
    base=run_diagnostic_task(db,p.id,provider=LiteralReview(),skill_version=3,task_type='body_consistency',
        skill_id='closed_inventory',inventory_kinds=['object'],allow_experimental=True,segment_nos=[1])
    r=invoke_tool('run_diagnostic_task',db=db,project_id=p.id,task_type='body_consistency',
        skill_id='closed_inventory',skill_version=4,allow_experimental=True,segment_nos=[1],inventory_kinds=['object'],
        previous_run_id=base['run_id'],inventory_verdict_source_ids=['G1P001'])
    assert r['inventory_summary']['partial'] is True
    assert r['inventory_summary']['success'] is False
    assert r['inventory_summary']['unreviewed_dimensions']==['person','place']
