"""Explicit bounded rebuild of summaries/completion/KG/facts; explicit generation unlock."""
from copy import deepcopy
from datetime import datetime, timezone
from sqlalchemy import select
from app.models import Generation, Project
from app.agents.diagnostic import digest
from app.agents.body_evidence import text_hash
from app.kg import build as kg_build
from app.kg import ledger as kg_ledger
from app.kg.build import rebuild_generated_graph
from app.kg.ledger import rebuild_facts

# 重建入口可能在本模块或来源模块上被替换（测试替身）：解析时取被替换的那个。
_ORIGINAL_REBUILDERS={'rebuild_generated_graph':rebuild_generated_graph,'rebuild_facts':rebuild_facts}


def _rows(db, project_id):
    return list(db.scalars(select(Generation).where(Generation.project_id==project_id).order_by(Generation.segment_no,Generation.id)))


def _first_frozen_text(frozen, *keys):
    """从冻结契约里按优先级取第一个非空文本字段。"""
    for key in keys:
        value=frozen.get(key)
        if isinstance(value,str) and value.strip():return value
    return ''


def _rebuilder(name, module):
    """取当前生效的重建入口：本模块的绑定被替换则用它，否则用来源模块的。"""
    local=globals().get(name)
    if local is not _ORIGINAL_REBUILDERS.get(name):
        return local
    return getattr(module,name)


def derived_fingerprint(db, project_id):
    p=db.get(Project,project_id)
    if p is None:raise PermissionError('project_not_found')
    return digest({'project_id':project_id,'bodies':[(g.id,g.segment_no,text_hash(g.text or '')) for g in _rows(db,project_id)],
        'contracts':((p.plot_timeline or {}).get('planning') or {}).get('segment_contracts')})


def _cached_result(cached):
    """幂等短路时补齐状态字段：旧版本缓存里可能没有 kg/fact 状态。"""
    out=dict(cached)
    out.setdefault('status','needs_review')
    out.setdefault('kg_status','pending_rebuild')
    out.setdefault('fact_status','pending_rebuild')
    out.setdefault('generation_unlocked',False)
    out.setdefault('completion_rejudged',False)
    return out


def rebuild_derived(db, project_id, *, base_fingerprint, provider=None):
    """重建派生状态：摘要 / 摘要树 / 完成度 / KG / 事实账本。

    **不解除生成守卫**：`body_revision_requires_review` 原样保留，
    解除是另一个显式动作 `unlock_generation`。
    """
    if db.new or db.dirty or db.deleted:raise ValueError('rebuild_requires_clean_session')
    p=db.get(Project,project_id)
    if p is None:raise PermissionError('project_not_found')
    if derived_fingerprint(db,project_id)!=base_fingerprint:raise ValueError('stale_body_fingerprint')
    timeline=deepcopy(p.plot_timeline or {})
    # 入口条件是「**派生数据待重建**」，不只是「有修订守卫」。
    # 以前只认 `body_revision_requires_review`（只在修订路径上产生）→
    # **重生成**过正文的项目，派生状态已经是 pending_rebuild，却没有任何重建入口，
    # 状态就永远停在「待重建」（实测：阶段三那三段重生成后，重建被
    # rebuild_requires_body_revision 拒掉，而 derived_state 一直说待重建）。
    derived_state=timeline.get('derived_state') or {}
    pending_derived=(derived_state.get('kg')=='pending_rebuild'
                     or derived_state.get('facts')=='pending_rebuild')
    if not (timeline.get('body_revision_requires_review') or pending_derived):
        raise ValueError('rebuild_requires_body_revision')
    cached=timeline.get('derived_rebuild') or {}
    # 只有**成功**的重建才可被复用；失败（stale）必须允许显式重试，
    # 否则一次失败会被缓存成永久短路，重试永远拿回同一个失败结果。
    if cached.get('body_fingerprint')==base_fingerprint and cached.get('status')!='stale':
        return _cached_result(cached)
    if provider is None:
        from app.llm.registry import get_provider_registry
        provider=get_provider_registry().get('writer')
    rows=_rows(db,project_id)
    if len(rows)>10:raise ValueError('rebuild_scope_too_large')
    from app.llm.provider import LocalFallbackLLMProvider
    from app.generation.summary_tree import empty_tree,record_l1,rollup_volumes
    from app.generation.timeline import state_from_dict,judge_node_completion
    result={'status':'stale','body_fingerprint':base_fingerprint,'kg_status':'pending_rebuild',
        'fact_status':'pending_rebuild','generation_unlocked':False,'completion_rejudged':False}
    try:
        if isinstance(provider,LocalFallbackLLMProvider):raise ValueError('local_fallback')
        summaries={};tree=empty_tree()
        for g in rows:
            summary=provider.summarize(g.text or '')
            if not isinstance(summary,str) or not summary.strip():raise ValueError('invalid_summary')
            summaries[g.id]=summary;record_l1(tree,g.segment_no,summary)
        rollup_volumes(tree,lambda texts,level:provider.summarize_rollup(texts,level))
        state=state_from_dict(timeline,total_segments=max(1,(p.target_length or 5000)//max(1,p.generation_length or 3000)))
        if state is None:raise ValueError('missing_timeline_state')
        contracts=(timeline.get('planning') or {}).get('segment_contracts',{})
        completion={};done=[]
        for g in rows:
            node_id=(contracts.get(str(g.segment_no)) or {}).get('node_id')
            if node_id not in state.nodes:raise ValueError('missing_frozen_node_binding')
            frozen=(contracts.get(str(g.segment_no)) or {}).get('authority',{}).get('causal_node')
            if not isinstance(frozen,dict):raise ValueError('missing_frozen_node_contract')
            node=deepcopy(state.nodes[node_id])
            # Never judge repaired historical text against today's changed plan.
            # 冻结契约里存的是**规划节点**的字段名（state_before/state_after/result），
            # 执行节点要的是 entry_condition/exit_condition —— 按**同一份冻结契约**映射，
            # 绝不退回今天的计划，也绝不凭空补。
            entry=_first_frozen_text(frozen,'entry_condition','state_before','premise')
            exit_=_first_frozen_text(frozen,'exit_condition','state_after','result')
            if not entry or not exit_:raise ValueError('missing_frozen_node_contract')
            node.entry_condition=entry
            node.exit_condition=exit_
            title=_first_frozen_text(frozen,'title')
            if title:node.title=title
            judge=judge_node_completion(provider,node,g.text or '',source_id=f'generation:{g.id}')
            if judge.get('audit',{}).get('error_category'):raise ValueError('completion_judge_failed')
            completion[node_id]=judge
            if judge['verdict']=='supported':done.append(node_id)
        # Reject concurrent changes before any derived business write.
        db.expire_all()
        if derived_fingerprint(db,project_id)!=base_fingerprint:raise ValueError('stale_body_fingerprint')
        p=db.get(Project,project_id)
        # KG 与事实账本按**当前正文**重建；与业务写同一个事务，失败整体回滚，
        # 不留「图谱已更新、账本还是旧版」的半重建状态。
        segments=[(g.segment_no,g.text or '') for g in _rows(db,project_id)]
        if not segments:raise ValueError('segments_required')
        kg_stats=_rebuilder('rebuild_generated_graph',kg_build)(db,project_id,segments,commit=False)
        fact_stats=_rebuilder('rebuild_facts',kg_ledger)(db,project_id,source='generated',segments=segments,commit=False)
        for g in _rows(db,project_id):g.summary={'text':summaries[g.id],'body_hash':text_hash(g.text or '')}
        p.summary=[summaries[g.id] for g in rows];p.summary_tree=tree
        timeline.update(completion=completion,done=list(dict.fromkeys(done)),last_judged_segment=max((g.segment_no for g in rows),default=0))
        revision=timeline.get('body_revision_requires_review') or {}
        changed_id=revision.get('generation_id')
        changed=db.get(Generation,int(changed_id)) if changed_id is not None else None
        result.update(status='needs_review',completion_rejudged=True,summaries_rebuilt=len(rows),
                      kg_status='rebuilt',fact_status='rebuilt',kg_stats=kg_stats,fact_stats=fact_stats,
                      changed_generation_id=changed_id,
                      body_hash=text_hash(changed.text or '') if changed is not None else None,
                      limitation='KG/Fact rebuilt from current body; generation guard retained')
    except Exception as exc:
        db.rollback();p=db.get(Project,project_id)
        result['error_category']=(str(exc) if isinstance(exc,ValueError)
                                  else f'{type(exc).__name__}: {exc}'[:200])
        # 任何一步失败都退回隔离态：KG/事实不算已重建，生成不解锁。
        result['status']='stale';result['kg_status']='pending_rebuild';result['fact_status']='pending_rebuild'
        result['generation_unlocked']=False
        timeline=deepcopy(p.plot_timeline or {})
        timeline.update(completion={},done=[],last_judged_segment=0)
    timeline['derived_rebuild']=result
    # 状态位按重建结果改写；apply 时记下的 changed_generation_id / body_hash 保留
    # （那是「哪一段被改过」的事实，与本模块的指纹不是同一个哈希空间）。
    state=dict(timeline.get('derived_state') or {})
    state.update(body_fingerprint=base_fingerprint,
                 kg='rebuilt' if result.get('kg_status')=='rebuilt' else 'pending_rebuild',
                 facts='rebuilt' if result.get('fact_status')=='rebuilt' else 'pending_rebuild')
    timeline['derived_state']=state
    p.plot_timeline=timeline;db.commit();db.refresh(p)
    return p.plot_timeline['derived_rebuild']


def unlock_generation(db, project_id, *, base_fingerprint):
    """显式解除生成守卫：只在派生状态已按当前正文重建、全局验收已关闭时放行。

    链序：局部修复落地（守卫仍拦） → 整段验收关闭全局义务 → 派生重建 → 解锁生成。
    **派生重建不是生成许可**：全局验收没关闭就不许放行。

    前置条件全部满足才改库；否则抛 ValueError（带具体错误码）且**什么都不改**：
      - project 存在 → 否则 `project_not_found`
      - `derived_fingerprint(db, project_id) == base_fingerprint` → 否则 `stale_body_fingerprint`
      - `timeline['body_revision_requires_review']` 存在 → 否则 `nothing_to_unlock`
      - 重建时记录的正文指纹与当前正文一致 → 否则 `body_changed_since_rebuild`
      - `timeline['pending_acceptance']` 存在且 `state == 'accepted'` → 否则 `acceptance_not_accepted`
      - `pending_acceptance['bound_body_hash']` 等于**当前正文级**指纹
        （`body_acceptance.body_fingerprint(rows)`，不含契约；与 `base_fingerprint` 不同哈希空间）
        → 否则 `acceptance_body_mismatch`
      - `timeline['derived_rebuild']` 存在且指纹一致 → 否则 `derived_rebuild_missing`
      - `status == 'needs_review'` → 否则 `derived_rebuild_incomplete`
      - `kg_status == 'rebuilt'` → 否则 `kg_not_rebuilt`
      - `fact_status == 'rebuilt'` → 否则 `facts_not_rebuilt`
      - `completion_rejudged is True` → 否则 `completion_not_rejudged`
    """
    p=db.get(Project,project_id)
    if p is None:raise ValueError('project_not_found')
    current=derived_fingerprint(db,project_id)
    if current!=base_fingerprint:raise ValueError('stale_body_fingerprint')
    timeline=deepcopy(p.plot_timeline or {})
    if not timeline.get('body_revision_requires_review'):raise ValueError('nothing_to_unlock')
    rebuild=timeline.get('derived_rebuild') or {}
    recorded=rebuild.get('body_fingerprint')
    if recorded and current!=recorded:raise ValueError('body_changed_since_rebuild')
    changed_id=rebuild.get('changed_generation_id') or (timeline.get('body_revision_requires_review') or {}).get('generation_id')
    if changed_id is not None and rebuild.get('body_hash'):
        changed=db.get(Generation,int(changed_id))
        if changed is None or text_hash(changed.text or '')!=rebuild['body_hash']:
            raise ValueError('body_changed_since_rebuild')
    pending=timeline.get('pending_acceptance') or {}
    if pending.get('state')!='accepted':raise ValueError('acceptance_not_accepted')
    # 验收义务绑在**正文级**指纹上（不含冻结契约），与 derived_fingerprint 不是同一个哈希空间。
    from app.agents.body_acceptance import body_fingerprint
    if pending.get('bound_body_hash')!=body_fingerprint(_rows(db,project_id)):
        raise ValueError('acceptance_body_mismatch')
    if not rebuild or rebuild.get('body_fingerprint')!=base_fingerprint:raise ValueError('derived_rebuild_missing')
    if rebuild.get('status')!='needs_review':raise ValueError('derived_rebuild_incomplete')
    if rebuild.get('kg_status')!='rebuilt':raise ValueError('kg_not_rebuilt')
    if rebuild.get('fact_status')!='rebuilt':raise ValueError('facts_not_rebuilt')
    if rebuild.get('completion_rejudged') is not True:raise ValueError('completion_not_rejudged')
    timeline.pop('body_revision_requires_review',None)
    timeline['derived_state']={'body_fingerprint':base_fingerprint,'kg':'rebuilt','facts':'rebuilt',
                               'unlocked_at':datetime.now(timezone.utc).isoformat()}
    rebuild['generation_unlocked']=True
    timeline['derived_rebuild']=rebuild
    p.plot_timeline=timeline;db.commit();db.refresh(p)
    return p.plot_timeline['derived_rebuild']
