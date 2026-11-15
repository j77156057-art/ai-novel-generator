"""Frozen-authority evidence adapter. Partial inventory never becomes global acceptance."""
import json
from datetime import datetime, timezone
from app.agents.diagnostic import digest, read_capability
from app.agents.body_evidence import safe_response
from app.agents.body_consistency import (candidate_observation, candidate_ranges, validate_findings,
                                         supported_citations, dimension_of, BODY_SYSTEM)
from app.agents.closed_inventory import explicit_closure
from app.models import AgentRun, Evidence


def validate_authority_request(db, project_id, run, g, report, start, end,
                               objective, preserve_source_ids, conflict_source_id, allow_delete,
                               *, ranges=None, dependency_note=''):
    if (report.get('run_id')!=run.id or report.get('project_id')!=project_id
            or report.get('agent_role')!='diagnostic' or report.get('provider_role')!='assistant'
            or not report.get('model_participated') or report.get('skill',{}).get('id')!='closed_inventory'
            or run.role!='assistant' or run.status!='success'):
        raise ValueError('invalid_inventory_repair_report')
    obs=next(o for o in report['observations'] if o['capability']=='read_closed_inventory')
    current=read_capability(db,project_id,'read_closed_inventory',report['scope'])
    if (obs['data']!=current['data'] or obs['input_fingerprint']!=digest(obs['data'])
            or report.get('input_fingerprint')!=digest(report['observations'])
            or report.get('inventory',{}).get('input_fingerprint')!=obs['input_fingerprint']):
        raise ValueError('stale_inventory_evidence')
    declared=list(ranges) if ranges else [{'start':start,'end':end,
                                           'conflict_source_id':conflict_source_id,'dependency':False}]
    primaries=[entry for entry in declared if not entry.get('dependency')]
    if not primaries:
        raise ValueError('repair_requires_clarification')
    if (not isinstance(objective,str) or not objective.strip() or len(objective)>1000
            or not isinstance(preserve_source_ids,list) or not 1<=len(preserve_source_ids)<=12
            or len(set(preserve_source_ids))!=len(preserve_source_ids)):
        raise ValueError('repair_requires_clarification')
    sources=obs['data']['sources']
    for entry in declared:
        if (type(entry.get('start')) is not int or type(entry.get('end')) is not int
                or not 0<=entry['start']<entry['end']<=len(g.text or '')):
            raise ValueError('repair_requires_clarification')
    ids=set(preserve_source_ids+[entry['conflict_source_id'] for entry in primaries])
    if not ids<=sources.keys() or any(entry['conflict_source_id'] in preserve_source_ids for entry in primaries):
        raise ValueError('repair_requires_clarification')
    segment=next(s for s in obs['data']['segments'] if s['generation_id']==g.id)
    for index in range(1,len(declared)):
        if declared[index]['start']<declared[index-1]['end']:
            raise ValueError('repair_scope_denied')
    body_paragraphs={sid:s for sid,s in sources.items() if s['source_id']==f'generation:{g.id}'}
    for entry in declared:
        owner=next((sid for sid,q in body_paragraphs.items()
                    if q['start']<=entry['start']<entry['end']<=q['end']),None)
        if owner is None:
            raise ValueError('repair_scope_denied')
        if entry.get('dependency') and owner in set(preserve_source_ids):
            raise ValueError('repair_scope_denied')
    for sid in preserve_source_ids:
        s=sources[sid]
        if s['source_id']!=f"contract:{segment['segment_no']}" or not s.get('path','').startswith('/authority/'):
            raise ValueError('repair_scope_denied')
    # 每个主区间都要对上自己那条 contradicted 检查；任何一条不过，整张卡都不出。
    range_targets=[]
    for entry in primaries:
        conflict=sources[entry['conflict_source_id']]
        if (conflict['source_id']!=f'generation:{g.id}'
                or not conflict['start']<=entry['start']<entry['end']<=conflict['end']):
            raise ValueError('repair_scope_denied')
        pair=set(preserve_source_ids)|{entry['conflict_source_id']}
        targets=[c for c in report['checks'] if c['status']=='contradicted'
                 and pair<={q['source_id'] for q in c['citations']}]
        if not targets:
            raise ValueError('repair_requires_clarification')
        target=targets[0]
        for q in target['citations']:
            s=sources.get(q['source_id'])
            if not s or q.get('body_source_id')!=s['source_id'] or q['quote']!=s['quote'][q['start']-s['start']:q['end']-s['start']]:
                raise ValueError('inventory_source_mismatch')
        candidates=report['inventory']['candidates']
        selected=next((c for c in candidates if c.get('id')==target['id'].split('_')[-1]),None)
        if not selected:raise ValueError('inventory_missing_verdict')
        c=selected['constraint'];v=selected.get('verdict',{})
        if (v.get('status')!='violated' or v.get('body_source_id')!=entry['conflict_source_id']
                or v.get('authority_source_id') not in preserve_source_ids
                or c.get('mode')!='closed' or c.get('source_id') not in preserve_source_ids
                or c.get('segment_no')!=g.segment_no
                or not explicit_closure(c['kind'],c['quote'])
                or c['quote'] not in sources[c['source_id']]['quote']):
            raise ValueError('inventory_requires_explicit_violation')
        range_targets.append({'start':entry['start'],'end':entry['end'],
                              'conflict_source_id':entry['conflict_source_id'],
                              'dependency':False,'target_check_id':target['id']})
    primary=primaries[0]
    payload={'objective':objective,'selected_text':g.text[primary['start']:primary['end']],
        'preserve_evidence':[{'source_id':s,'quote':sources[s]['quote']} for s in preserve_source_ids],
        'frozen_contract':segment['contract'],'contract_hash':digest(segment['contract']),
        'diagnostic_binding':{'run_id':run.id,'input_fingerprint':report['input_fingerprint'],
            'target_check_id':range_targets[0]['target_check_id']},
        'boundary_read_only':{'before':g.text[max(0,primary['start']-80):primary['start']],
                              'after':g.text[primary['end']:primary['end']+80]},
        'allow_delete':allow_delete,'uncertainty_policy':'不扩充权限，未知保持unknown'}
    if len(declared)>1:
        payload['diagnostic_binding']['target_check_ids']=[item['target_check_id'] for item in range_targets]
        payload['diagnostic_binding']['range_targets']=range_targets
        payload['selected_texts']=[g.text[entry['start']:entry['end']] for entry in declared]
        payload['declared_ranges']=[dict(entry) for entry in declared]
        payload['dependency_note']=dependency_note
    return payload


def _changed_sources(sources, generation_id, ranges):
    """声明集合真正触及的正文段落（候选正文坐标；删除段按落点计）。"""
    changed=set(); offset=0
    for entry in ranges:
        start=entry['start']+offset; end=start+len(entry['replacement'])
        for sid,s in sources.items():
            if s['source_id']!=f'generation:{generation_id}': continue
            if end>start:
                if s['start']<end and s['end']>start: changed.add(sid)
            elif s['start']<=start<=s['end']:
                changed.add(sid)
        offset+=len(entry['replacement'])-(entry['end']-entry['start'])
    return changed


def verify_authority_candidate(db, project_id, previous, candidate, generation_id, payload, provider=None):
    """One Assistant call; preserve all unresolved original acceptance obligations."""
    if provider is None:
        from app.routers.assistant_shared import _ensure_assistant_provider
        provider=_ensure_assistant_provider()
    from app.llm.provider import LocalFallbackLLMProvider
    obs=candidate_observation(previous['observations'][0],generation_id,candidate)
    needed=set(q['source_id'] for q in payload['preserve_evidence'])
    ranges=candidate_ranges(candidate)
    changed=_changed_sources(obs['data']['sources'],generation_id,ranges)
    binding=payload.get('diagnostic_binding',{}) or {}
    target_ids=list(binding.get('target_check_ids') or [])
    if not target_ids and binding.get('target_check_id'):
        target_ids=[binding['target_check_id']]
    target_dimensions=sorted({dimension_of(cid) for cid in target_ids}) or ['object_state']
    requirements={'required_authority_source_ids':sorted(needed),
        'changed_body_source_ids':sorted(changed),
        'target_check_id':binding.get('target_check_id') or (target_ids[0] if target_ids else None),
        'scope':'local_only'}
    report={'project_id':project_id,'agent_role':'diagnostic','provider_role':'assistant',
        'task_type':'repair_verification','previous_run_id':previous['run_id'],'skill':previous['skill'],
        'scope':previous['scope'],'acceptance':previous['acceptance'],'observations':[obs],
        'input_fingerprint':digest([obs]),'status':'failed','checks':[],
        'unresolved':list(previous.get('unresolved',[])),'model_participated':False,
        'verification_scope':'authority_to_body_local','global_quality_verified':False,
        'local_semantic_accepted':False,'deferred_dimensions':[],
        'contract_hash':payload['contract_hash'],'model':getattr(provider,'model',type(provider).__name__)}
    try:
        if isinstance(provider,LocalFallbackLLMProvider):raise ValueError('local_fallback')
        raw=provider.chat('你是独立Assistant。'+BODY_SYSTEM+
            '本次局部修复须核对完整冻结权威、原诊断及候选正文。不能因清单缺项判错，必须明确closed。'
            '核对目标违例是否消除、有无同义改写残留、有无新增动作或结果、保留事实及四维契约。'
            '顶层assessment必须为observed，表示已观察，不表示语义通过；supported/contradicted/unknown仅用于findings内。'
            'object_state supported必须在同一finding引用verification_requirements.required_authority_source_ids全部权限端，'
            '以及changed_body_source_ids中至少一个改动段；其他相关权威不能替代指定封闭权限来源。'
            '须解释目标依赖是否消除及既有事实是否保留。未检查内容保持unknown，不宣称全段通过。'
            '本次只判定目标维度（'+'、'.join(target_dimensions)+'）在本次改动范围内是否消除并保留既有事实；'
            '时间/地点/参与者三个维度不属于本次局部检查范围，未检查即unknown属正常，不构成失败，'
            '但不得因此宣称整段通过（整段验收另有独立复验）。'
            'changed_body_source_ids中的每个改动段都必须被某个supported finding引用；未看过的依赖段不得算作通过。',
            json.dumps({'request':payload,'candidate':candidate,'sources':obs['data']['sources'],
                        'verification_requirements':requirements,
                        'original_checks':previous['checks'],'acceptance':previous['acceptance']},ensure_ascii=False))
        report['model_participated']=True;report['model_response_raw']=safe_response(raw)
        answer=json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
        report['checks']=validate_findings(answer,obs['data']['sources'])
        report['status']='completed'
        dimensions={c['id'].rsplit('_',1)[0] for c in report['checks']}
        supported_dimensions={c['id'].rsplit('_',1)[0] for c in report['checks'] if c['status']=='supported'}
        report['deferred_dimensions']=sorted(dimensions-supported_dimensions)
        # 局部判定只看目标维度：其它维度的 unknown 是「未检查」，不是「失败」。
        local=all(any(c['id'].rsplit('_',1)[0]==dimension and c['status']=='supported'
                and needed<={q['source_id'] for q in c['citations']}
                and changed.intersection(q['source_id'] for q in c['citations'])
                for c in report['checks']) for dimension in target_dimensions)
        covered=changed<=supported_citations(report['checks'])
        if answer.get('assessment')!='observed':report['unresolved'].append('invalid_top_level_assessment')
        if not any(needed<={q['source_id'] for q in c['citations']} for c in report['checks']):
            report['unresolved'].append('missing_required_authority_evidence')
        if not any(changed.intersection(q['source_id'] for q in c['citations']) for c in report['checks']):
            report['unresolved'].append('missing_changed_body_evidence')
        if not covered:report['unresolved'].append('missing_declared_dependency_evidence')
        report['local_semantic_accepted']=bool(local and covered and answer.get('assessment')=='observed')
        if not report['local_semantic_accepted']:report['unresolved'].append('local_semantic_review_required')
        # Existing other violations and missing coverage cannot be certified by a local check.
        # 只搬**违例**（contradicted）：审计里的 unknown 检查项是**开放项**
        # （定位失败的提及、说不清的提及）——按审计自己的口径它们「不当违例」，
        # 搬进阻塞清单会让整段验收永远关不掉（实测：一次修复把 24 条开放项
        # 搬成阻塞项，验收直接 no-op，链路死锁）。
        other=[c['id'] for c in previous['checks']
               if c['id'] not in set(target_ids) and c['status']=='contradicted']
        report['unresolved']+=['original_check:'+c for c in other]
        if not previous['inventory'].get('coverage_complete'):report['unresolved'].append('inventory_partial_coverage')
        if not previous['inventory'].get('global_quality_verified'):report['unresolved'].append('full_scope_acceptance_unverified')
    except Exception as exc:
        report['model_error_category']=type(exc).__name__
        report['unresolved'].append('independent_verification_failed')
    report['unresolved']=list(dict.fromkeys(report['unresolved']))
    row=AgentRun(project_id=project_id,role='assistant',status='success' if report['status']=='completed' else 'failed',
        request_hash=digest([previous['run_id'],candidate['after_hash']]),model=report['model'],finished_at=datetime.now(timezone.utc))
    db.add(row);db.flush();report['run_id']=row.id;row.note=json.dumps(report,ensure_ascii=False)
    db.add(Evidence(run_id=row.id,kind='run',source='diagnostic',ref_id=str(row.id),score=1.0,
        snippet=json.dumps({'input_fingerprint':report['input_fingerprint'],'contract_hash':report['contract_hash']})))
    db.commit();db.refresh(row)
    return json.loads(row.note)
