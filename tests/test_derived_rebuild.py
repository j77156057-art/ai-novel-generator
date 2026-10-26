import pytest
from test_causal_timeline import fixture, Reply
from app.models import Generation
from app.agents.proposals import write_target


def setup_revision():
    db,p=fixture()
    t=dict(p.plot_timeline);t['planning']={'segment_contracts':{'1':{'node_id':'n1','authority':{'causal_node':{'title':'寻找钥匙','entry_condition':'门锁着，钥匙遗失','exit_condition':'找到钥匙'}}}}};p.plot_timeline=t
    g=Generation(project_id=p.id,segment_no=1,text='旧正文。');db.add(g);db.commit()
    write_target(db,p.id,'generation_text',str(g.id),'找到钥匙。');db.commit()
    return db,p,g


def test_rebuild_records_current_hash_and_rebuilds_kg_and_facts():
    """重建成功后 KG/事实也必须按当前正文重建 —— 不再是「明确挂起」。

    挂起的是**生成守卫**（body_revision_requires_review 仍在），不是派生数据本身。
    """
    from app.agents.derived_rebuild import rebuild_derived, derived_fingerprint
    db,p,g=setup_revision();contracts=(p.plot_timeline.get('planning') or {}).get('segment_contracts')
    provider=Reply({'verdict':'supported','reason':'找到','sources':[{'source_id':f'generation:{g.id}','quote':g.text,'start':0,'end':len(g.text)}]})
    provider.summarize=lambda text:'找到钥匙，门尚未开启。'
    result=rebuild_derived(db,p.id,base_fingerprint=derived_fingerprint(db,p.id),provider=provider)
    db.refresh(g);db.refresh(p)
    assert g.summary['text']=='找到钥匙，门尚未开启。'
    assert p.summary_tree['l1']['1']==g.summary['text']
    assert result['status']=='needs_review' and result['kg_status']=='rebuilt'
    assert result['fact_status']=='rebuilt'
    assert p.plot_timeline['body_revision_requires_review'], '生成守卫必须保留'
    assert p.plot_timeline['derived_state']['kg']=='rebuilt'
    assert p.plot_timeline['derived_state']['facts']=='rebuilt'
    assert p.plot_timeline['derived_rebuild']['body_fingerprint']==derived_fingerprint(db,p.id)
    assert p.plot_timeline['completion']['n1']['verdict']=='supported'
    assert (p.plot_timeline.get('planning') or {}).get('segment_contracts')==contracts


def test_failed_rebuild_leaves_stale_summaries_and_guard():
    from app.agents.derived_rebuild import rebuild_derived, derived_fingerprint
    db,p,g=setup_revision();provider=Reply({})
    def fail(text):raise RuntimeError('model failed')
    provider.summarize=fail
    result=rebuild_derived(db,p.id,base_fingerprint=derived_fingerprint(db,p.id),provider=provider)
    db.refresh(p);db.refresh(g)
    assert result['status']=='stale' and g.summary is None
    assert p.plot_timeline['body_revision_requires_review']


def test_drifted_rebuild_never_calls_model():
    from app.agents.derived_rebuild import rebuild_derived
    db,p,g=setup_revision();provider=Reply({})
    with pytest.raises(ValueError,match='stale'):rebuild_derived(db,p.id,base_fingerprint='bad',provider=provider)
    assert not provider.calls
