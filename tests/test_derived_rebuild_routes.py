import pytest
from test_derived_rebuild import setup_revision


def test_apply_explicitly_invalidates_kg_before_any_rebuild():
    db,p,g=setup_revision()
    assert p.plot_timeline['derived_state']['kg']=='pending_rebuild'
    assert p.plot_timeline['derived_state']['facts']=='pending_rebuild'


def test_rebuild_route_rejects_wrong_project_and_stale_hash():
    from app.routers.assistant_proposals import rebuild_project_derived, DerivedRebuildRequest
    from fastapi import HTTPException
    db,p,g=setup_revision()
    with pytest.raises(HTTPException) as error:
        rebuild_project_derived(DerivedRebuildRequest(project_id=999,base_fingerprint='bad',action='rebuild'),db)
    assert error.value.status_code==403
    with pytest.raises(HTTPException) as error:
        rebuild_project_derived(DerivedRebuildRequest(project_id=p.id,base_fingerprint='bad',action='rebuild'),db)
    assert error.value.status_code==409
