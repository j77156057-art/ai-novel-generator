"""正文修订后的派生状态恢复：KG/事实账本重建 + 显式生成解锁。

覆盖 KG/Fact 重建原语、守卫保留、解锁前置条件与失败隔离。
"""
import pytest
from test_causal_timeline import fixture, Reply
from fastapi import HTTPException
from app.models import Fact, Generation, KgNode
from app.agents.proposals import write_target


def setup_revision(text='林夏说：门锁着。'):
    """带规划契约的正文修订现场（apply 后派生状态失效、生成被拦截）。"""
    db, p = fixture()
    t = dict(p.plot_timeline)
    t['planning'] = {'segment_contracts': {'1': {'node_id': 'n1', 'authority': {'causal_node': {
        'title': '寻找钥匙', 'entry_condition': '门锁着，钥匙遗失', 'exit_condition': '找到钥匙'}}}}}
    p.plot_timeline = t
    g = Generation(project_id=p.id, segment_no=1, text='旧正文。')
    db.add(g)
    db.commit()
    write_target(db, p.id, 'generation_text', str(g.id), text)
    db.commit()
    return db, p, g


def provider_for(g):
    """固定回答的假 provider：摘要 + 完成度判定都不需要真实模型。"""
    provider = Reply({'verdict': 'supported', 'reason': '找到',
                       'sources': [{'source_id': f'generation:{g.id}', 'quote': g.text,
                                    'start': 0, 'end': len(g.text)}]})
    provider.summarize = lambda text: '找到钥匙，门尚未开启。'
    return provider


def body_fp(db, project_id):
    """当前正文级指纹（验收义务绑在这个哈希空间上，不含冻结契约）。"""
    from app.agents.body_acceptance import body_fingerprint
    from app.agents.derived_rebuild import _rows
    return body_fingerprint(_rows(db, project_id))


def close_acceptance(db, p, bound_hash, state='accepted'):
    """把全局验收义务关闭（或保持在未关闭状态）并绑定到某一版正文上。"""
    timeline = dict(p.plot_timeline or {})
    timeline['pending_acceptance'] = {'state': state, 'bound_body_hash': bound_hash}
    p.plot_timeline = timeline
    db.commit()
    return timeline


def generated_nodes(db):
    return db.query(KgNode).filter(KgNode.source == 'generated').all()


def generated_facts(db):
    return db.query(Fact).filter(Fact.source == 'generated').all()


# ---------------------------------------------------------------------------
# 1) 事实账本重建：旧正文的行必须消失，新正文的行必须写入
# ---------------------------------------------------------------------------

def test_rebuild_facts_replaces_rows_derived_from_the_old_body():
    from app.kg.build import extract_from_text
    from app.kg.ledger import facts_from_extraction, rebuild_facts, upsert_facts
    db, p = fixture()
    old_result, _ = extract_from_text('周明说：灯还亮着。', 1)
    upsert_facts(db, p.id, facts_from_extraction(old_result, 1), source='generated')
    assert [f.key for f in generated_facts(db)] == ['周明']

    stats = rebuild_facts(db, p.id, source='generated', segments=[(1, '林夏说：门锁着。')])
    keys = [f.key for f in generated_facts(db)]
    assert '周明' not in keys
    assert '林夏' in keys
    assert stats['deleted'] == 1
    assert stats['created'] == 1
    assert stats['upserted'] == 1


def test_rebuild_facts_keeps_author_exemptions():
    from app.kg.ledger import rebuild_facts
    db, p = fixture()
    db.add(Fact(project_id=p.id, type='exemption', key='exempt:rule:林夏',
                props={'rule': 'rule', 'entity': '林夏', 'note': '作者认定'},
                source='author', confidence='high'))
    db.add(Fact(project_id=p.id, type='exemption', key='exempt:rule:周明',
                props={'rule': 'rule', 'entity': '周明', 'note': '作者认定'},
                source='generated', confidence='high'))
    db.commit()
    stats = rebuild_facts(db, p.id, source='generated', segments=[(1, '林夏说：门锁着。')])
    kept = {f.key for f in db.query(Fact).filter(Fact.type == 'exemption').all()}
    assert kept == {'exempt:rule:林夏', 'exempt:rule:周明'}
    assert stats['exemptions_kept'] == 2


def test_rebuild_facts_keeps_sentinel_state_facts():
    """哨兵写下的角色状态不由正文决定 —— 重建不得抹掉（否则「已死」的角色会复活）。"""
    from app.kg.ledger import rebuild_facts
    db, p = fixture()
    db.add(Fact(project_id=p.id, type='state', key='life:周明',
                props={'value': 'dead', 'since_segment': 3},
                source='generated', confidence='high'))
    db.add(Fact(project_id=p.id, type='character', key='周明',
                props={'name': '周明'}, source='generated', confidence='high'))
    db.commit()
    stats = rebuild_facts(db, p.id, source='generated', segments=[(1, '林夏说：门锁着。')])
    rows = {f.type: f for f in db.query(Fact).filter(Fact.source == 'generated').all()}
    assert 'state' in rows, '角色生死状态不能被正文重建抹掉'
    assert rows['state'].props['value'] == 'dead'
    assert stats['deleted'] == 1, '只有正文抽取得出的事实才被重建'


def test_rebuild_facts_rejects_invalid_segments():
    from app.kg.ledger import rebuild_facts
    db, p = fixture()
    with pytest.raises(ValueError):
        rebuild_facts(db, p.id, source='generated', segments=[])
    with pytest.raises(ValueError):
        rebuild_facts(db, p.id, source='generated', segments=[('1', '不是整数段号')])


# ---------------------------------------------------------------------------
# 2) 图谱重建：旧节点必须消失，章节号必须等于段号
# ---------------------------------------------------------------------------

def test_rebuild_generated_graph_rebuilds_nodes_with_segment_chapters():
    from app.kg.build import build_graph_from_text, rebuild_generated_graph
    db, p = fixture()
    build_graph_from_text(db, p.id, '周明说：灯还亮着。', source='generated')
    db.add(KgNode(project_id=p.id, type='character', props={'name': '旧角色'},
                  first_chapter=1, last_chapter=1, source='generated'))
    db.commit()
    assert {'周明', '旧角色'} == {n.props.get('name') for n in generated_nodes(db)}

    stats = rebuild_generated_graph(db, p.id, [(1, '林夏说：门锁着。'), (2, '周明说：灯还亮着。')])
    by_name = {n.props.get('name'): n for n in generated_nodes(db)}
    assert set(by_name) == {'林夏', '周明'}
    assert (by_name['林夏'].first_chapter, by_name['林夏'].last_chapter) == (1, 1)
    assert (by_name['周明'].first_chapter, by_name['周明'].last_chapter) == (2, 2)
    assert stats['characters'] == 2
    assert stats['segments'] == 2


# ---------------------------------------------------------------------------
# 3) rebuild_derived：派生状态重建，但生成守卫保留
# ---------------------------------------------------------------------------

def test_rebuild_derived_rebuilds_kg_and_facts_but_keeps_generation_blocked():
    from app.agents.derived_rebuild import derived_fingerprint, rebuild_derived
    from app.generation.engine import build_context
    db, p, g = setup_revision()
    result = rebuild_derived(db, p.id, base_fingerprint=derived_fingerprint(db, p.id),
                              provider=provider_for(g))
    db.refresh(p)
    assert result['status'] == 'needs_review'
    assert result['kg_status'] == 'rebuilt'
    assert result['fact_status'] == 'rebuilt'
    assert result['generation_unlocked'] is False
    assert result['kg_stats']['segments'] == 1
    assert p.plot_timeline['derived_state']['kg'] == 'rebuilt'
    assert p.plot_timeline['derived_state']['facts'] == 'rebuilt'
    assert p.plot_timeline.get('body_revision_requires_review')
    assert '林夏' in {n.props.get('name') for n in generated_nodes(db)}
    assert '林夏' in [f.key for f in generated_facts(db)]
    with pytest.raises(HTTPException) as error:
        build_context(db, p, profile='original')
    assert error.value.status_code == 409


def test_failed_kg_rebuild_keeps_quarantine_and_leaves_no_half_rebuild(monkeypatch):
    import app.agents.derived_rebuild as dr
    from app.kg.build import _clear_graph_by_source
    from app.generation.engine import build_context
    db, p, g = setup_revision()
    db.add(KgNode(project_id=p.id, type='character', props={'name': '旧角色'},
                  first_chapter=1, last_chapter=1, source='generated'))
    db.commit()
    before = sorted(n.id for n in generated_nodes(db))

    def boom(db_, project_id, segments, **kwargs):
        # 清图之后立刻失败：若事务边界不对，这里就会留下半重建的图谱。
        _clear_graph_by_source(db_, project_id, 'generated')
        raise RuntimeError('kg down')

    monkeypatch.setattr(dr, 'rebuild_generated_graph', boom)
    result = dr.rebuild_derived(db, p.id, base_fingerprint=dr.derived_fingerprint(db, p.id),
                                provider=provider_for(g))
    db.refresh(p)
    assert result['status'] == 'stale'
    assert result['kg_status'] == 'pending_rebuild'
    assert result['fact_status'] == 'pending_rebuild'
    assert result['generation_unlocked'] is False
    assert p.plot_timeline.get('body_revision_requires_review')
    assert p.plot_timeline['derived_state']['kg'] == 'pending_rebuild'
    assert sorted(n.id for n in generated_nodes(db)) == before
    with pytest.raises(HTTPException) as error:
        build_context(db, p, profile='original')
    assert error.value.status_code == 409


# ---------------------------------------------------------------------------
# 4) unlock_generation 的前置条件
# ---------------------------------------------------------------------------

def test_unlock_requires_a_completed_derived_rebuild():
    from app.agents.derived_rebuild import derived_fingerprint, unlock_generation
    db, p, g = setup_revision()
    fingerprint = derived_fingerprint(db, p.id)
    close_acceptance(db, p, body_fp(db, p.id))
    with pytest.raises(ValueError, match='derived_rebuild_missing'):
        unlock_generation(db, p.id, base_fingerprint=fingerprint)


def test_unlock_rejects_incomplete_derived_rebuild():
    from app.agents.derived_rebuild import derived_fingerprint, rebuild_derived, unlock_generation
    db, p, g = setup_revision()
    fingerprint = derived_fingerprint(db, p.id)
    close_acceptance(db, p, body_fp(db, p.id))
    provider = Reply({})
    provider.summarize = lambda text: (_ for _ in ()).throw(RuntimeError('model failed'))
    rebuild_derived(db, p.id, base_fingerprint=fingerprint, provider=provider)
    with pytest.raises(ValueError, match='derived_rebuild_incomplete'):
        unlock_generation(db, p.id, base_fingerprint=fingerprint)


def test_unlock_rejects_unrebuilt_kg():
    from app.agents.derived_rebuild import derived_fingerprint, unlock_generation
    db, p, g = setup_revision()
    fingerprint = derived_fingerprint(db, p.id)
    close_acceptance(db, p, body_fp(db, p.id))
    timeline = dict(p.plot_timeline)
    timeline['derived_rebuild'] = {'status': 'needs_review', 'body_fingerprint': fingerprint,
                                   'kg_status': 'pending_rebuild', 'fact_status': 'rebuilt',
                                   'completion_rejudged': True, 'generation_unlocked': False}
    timeline['derived_state'] = {'body_fingerprint': fingerprint, 'kg': 'pending_rebuild',
                                 'facts': 'rebuilt'}
    p.plot_timeline = timeline
    db.commit()
    with pytest.raises(ValueError, match='kg_not_rebuilt'):
        unlock_generation(db, p.id, base_fingerprint=fingerprint)


def test_unlock_rejects_drifted_body_fingerprint():
    from app.agents.derived_rebuild import unlock_generation
    db, p, g = setup_revision()
    with pytest.raises(ValueError, match='stale_body_fingerprint'):
        unlock_generation(db, p.id, base_fingerprint='bad')


def test_unlock_rejects_body_changed_after_rebuild():
    from app.agents.derived_rebuild import derived_fingerprint, rebuild_derived, unlock_generation
    db, p, g = setup_revision()
    provider = provider_for(g)
    rebuild_derived(db, p.id, base_fingerprint=derived_fingerprint(db, p.id), provider=provider)
    g.text = '林夏说：门开了。'
    db.commit()
    current = derived_fingerprint(db, p.id)
    close_acceptance(db, p, body_fp(db, p.id))
    with pytest.raises(ValueError, match='body_changed_since_rebuild'):
        unlock_generation(db, p.id, base_fingerprint=current)


# ---------------------------------------------------------------------------
# 4b) 全局验收没关闭就不许解锁（派生重建不是生成许可）
# ---------------------------------------------------------------------------

def test_unlock_requires_closed_global_acceptance():
    from app.agents.derived_rebuild import derived_fingerprint, rebuild_derived, unlock_generation
    db, p, g = setup_revision()
    fingerprint = derived_fingerprint(db, p.id)
    rebuild_derived(db, p.id, base_fingerprint=fingerprint, provider=provider_for(g))
    db.refresh(p)
    before = dict(p.plot_timeline)
    with pytest.raises(ValueError, match='acceptance_not_accepted'):
        unlock_generation(db, p.id, base_fingerprint=fingerprint)
    db.refresh(p)
    assert p.plot_timeline.get('body_revision_requires_review')
    assert p.plot_timeline['derived_rebuild']['generation_unlocked'] is False
    assert p.plot_timeline == before

    # 义务仍然挂着（carried_forward）同样不许放行。
    close_acceptance(db, p, body_fp(db, p.id), state='carried_forward')
    db.refresh(p)
    with pytest.raises(ValueError, match='acceptance_not_accepted'):
        unlock_generation(db, p.id, base_fingerprint=fingerprint)
    db.refresh(p)
    assert p.plot_timeline.get('body_revision_requires_review')
    assert p.plot_timeline['derived_rebuild']['generation_unlocked'] is False


def test_unlock_requires_acceptance_bound_to_this_body():
    """验收关闭在**另一版正文**上时必须拒绝（正文级指纹对不上）。"""
    from app.agents.body_acceptance import body_fingerprint
    from app.agents.derived_rebuild import derived_fingerprint, rebuild_derived, unlock_generation
    db, p, g = setup_revision()
    fingerprint = derived_fingerprint(db, p.id)
    rebuild_derived(db, p.id, base_fingerprint=fingerprint, provider=provider_for(g))

    class _OldBody:  # 修订前的旧正文：验收就关闭在它上面
        id = 1
        segment_no = 1
        text = '旧正文。'

    stale = body_fingerprint([_OldBody()])
    assert stale != body_fp(db, p.id)
    close_acceptance(db, p, stale)
    db.refresh(p)
    with pytest.raises(ValueError, match='acceptance_body_mismatch'):
        unlock_generation(db, p.id, base_fingerprint=fingerprint)
    db.refresh(p)
    assert p.plot_timeline.get('body_revision_requires_review')
    assert p.plot_timeline['derived_rebuild']['generation_unlocked'] is False


# ---------------------------------------------------------------------------
# 5) unlock_generation 成功路径
# ---------------------------------------------------------------------------

def test_unlock_generation_releases_the_guard_after_verified_rebuild():
    from app.agents.derived_rebuild import derived_fingerprint, rebuild_derived, unlock_generation
    from app.generation.engine import build_context
    db, p, g = setup_revision()
    fingerprint = derived_fingerprint(db, p.id)
    rebuild_derived(db, p.id, base_fingerprint=fingerprint, provider=provider_for(g))
    close_acceptance(db, p, body_fp(db, p.id))
    db.refresh(p)
    out = unlock_generation(db, p.id, base_fingerprint=fingerprint)
    db.refresh(p)
    assert 'body_revision_requires_review' not in p.plot_timeline
    assert p.plot_timeline['derived_state']['kg'] == 'rebuilt'
    assert p.plot_timeline['derived_state']['facts'] == 'rebuilt'
    assert p.plot_timeline['derived_state']['unlocked_at']
    assert out['generation_unlocked'] is True
    assert p.plot_timeline['derived_rebuild']['generation_unlocked'] is True
    build_context(db, p, profile='original')
