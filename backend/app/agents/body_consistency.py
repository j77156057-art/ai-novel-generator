"""Bounded body reads; no generation, retrieval or business mutations."""
from sqlalchemy import select
from app.models import Generation
from app.agents.body_evidence import paragraph_sources, text_hash


BODY_SYSTEM = '''正文一致性诊断只检查限定段号和保存契约。时间、地点、参与者、物件状态四维分别列出finding。
区分叙述事实、回忆、谎言、设想和有意重复；这些本身不是矛盾。没有证据允许unknown，不添加事实。
输出assessment/ reason以及findings数组，每项严格含dimension:time|place|participants|object_state、
assessment:supported|contradicted|unknown、reason、source_ids:["G1P001"]。
矛盾必须引用两端至少两个不同source ID；无法定位双方时为unknown。source_ids只能选本次sources键，禁止quote和坐标。
每项最多12个source_ids。逐个核对物件，不能把某一件物品的「未使用」推广为另一件物品的「未使用」。
比较后文全称否定与前文实际动作时，说明是否同一物件、同一时段、同一使用含义；发现冲突须分别引用两端。
同一段落内双方冲突无法分开定位时保留unknown。整体observed不表示各项通过。'''


def validate_findings(answer, sources):
    findings = answer.get('findings')
    if not isinstance(findings, list) or not 1 <= len(findings) <= 12:
        raise ValueError('invalid_body_evidence')
    checks = []
    for i, finding in enumerate(findings):
        if (not isinstance(finding, dict) or set(finding) != {'dimension','assessment','reason','source_ids'}
                or finding['dimension'] not in {'time','place','participants','object_state'}
                or finding['assessment'] not in {'supported','contradicted','unknown'}
                or not isinstance(finding['reason'], str) or not finding['reason'].strip()
                or not isinstance(finding['source_ids'], list)):
            raise ValueError('invalid_body_evidence')
        ids = finding['source_ids']
        if (len(ids) > 12 or any(not isinstance(s, str) or s not in sources for s in ids)
                or len(set(ids)) != len(ids)
                or (finding['assessment'] == 'contradicted' and len(ids) < 2)
                or (finding['assessment'] == 'supported' and not ids)):
            raise ValueError('invalid_body_evidence')
        checks.append({'id':f"{finding['dimension']}_{i+1}", 'status':finding['assessment'],
                       'reason':finding['reason'], 'citations':[dict(sources[s], source_id=s,
                       body_source_id=sources[s]['source_id'], location_verified=True) for s in ids]})
    return checks


def experiment_outcome(report):
    stages = report.get('stages', {})
    verify = stages.get('verification', {})
    pipeline = (not report.get('error') and stages.get('diagnosis', {}).get('status') == 'completed'
                and stages.get('writer', {}).get('status') == 'pending' and verify.get('status') == 'completed'
                and report.get('source_unchanged') is True and report.get('original_body_unchanged') is True
                and stages.get('writer', {}).get('outside_range_unchanged') is True)
    semantic = (verify.get('status') == 'completed' and bool(verify.get('checks'))
                and all(c.get('status') == 'supported' for c in verify['checks'])
                and not verify.get('unresolved'))
    return {'pipeline_completed':bool(pipeline), 'semantic_accepted':bool(semantic),
            'success':bool(pipeline and semantic)}


#: 封闭清单的库存种类 → 正文维度（'object_N001' 与 'object_state_1' 同指 object_state）。
KIND_DIMENSION = {'object': 'object_state', 'person': 'participants', 'place': 'place'}


def dimension_of(check_id):
    """检查 id → 正文维度。"""
    prefix = str(check_id or '').rsplit('_', 1)[0]
    return KIND_DIMENSION.get(prefix, prefix)


#: 一次修复请求允许声明的区间上限。依赖链（手机链）在实测案例里跨 5 段，
#: 多个独立违例同时修需要 7 段 —— 一次申请、一次落地：apply 会重排后续段落的
#: 字符坐标，第二次 apply 会让剩下的区间再也无法对齐原诊断报告的证据。
MAX_REPAIR_RANGES = 8


def _normalize_ranges(text, ranges, allow_delete):
    """校验并归一化声明的区间集合：有序、不重叠、逐个合法。"""
    if (not isinstance(text, str) or not isinstance(ranges, list)
            or not 1 <= len(ranges) <= MAX_REPAIR_RANGES):
        raise ValueError('invalid_candidate_range')
    normalized = []
    for entry in ranges:
        if not isinstance(entry, dict):
            raise ValueError('invalid_candidate_range')
        start, end, replacement = entry.get('start'), entry.get('end'), entry.get('replacement')
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
                or not isinstance(replacement, str) or len(replacement) > 2000
                or (not replacement.strip() and not (allow_delete and replacement == ''))):
            raise ValueError('invalid_candidate_range')
        normalized.append((start, end, replacement))
    for index in range(1, len(normalized)):
        if normalized[index][0] < normalized[index-1][1]:
            raise ValueError('invalid_candidate_range')
    return normalized


def _check_boundary_echo(text, normalized, index):
    """边界回显检查：替换文本不得复制紧邻区间的上下文。

    命中的上下文若落在同一次候选的**另一个**被选区间内，那是合法的
    （那段本来就在替换集合里），不判错。区间之间的换行不属于任何区间，
    因此判定覆盖时按去掉首尾空白后的实际文字范围算。
    """
    start, end, replacement = normalized[index]

    def covered(low, high):
        return any(other <= low and high <= stop
                   for position, (other, stop, _) in enumerate(normalized) if position != index)

    def span_of(run, origin):
        """run 在正文里的坐标区间（去掉首尾空白）。"""
        stripped = run.strip()
        if not stripped:
            return None
        lead = len(run) - len(run.lstrip())
        trail = len(run) - len(run.rstrip())
        return (origin + lead, origin + len(run) - trail)

    suffix, prefix = text[end:end+2000], text[max(0,start-2000):start]
    for n in range(8, len(suffix)+1):
        run = suffix[:n]
        if len(run.strip()) >= 8 and run in replacement:
            span = span_of(run, end)
            if span is None or not covered(*span):
                raise ValueError('candidate_boundary_echo')
    for n in range(8, len(prefix)+1):
        run = prefix[-n:]
        if len(run.strip()) >= 8 and run in replacement:
            span = span_of(run, start - len(run))
            if span is None or not covered(*span):
                raise ValueError('candidate_boundary_echo')


def build_body_candidate_multi(text, ranges, *, allow_delete=False):
    """按声明的区间集合构造候选正文；区间外逐字不变。

    `ranges` 为 [{'start','end','replacement'}, ...]，有序且互不重叠。
    """
    normalized = _normalize_ranges(text, ranges, allow_delete)
    for index in range(len(normalized)):
        _check_boundary_echo(text, normalized, index)
    after = text
    for start, end, replacement in reversed(normalized):
        after = after[:start] + replacement + after[end:]
    fixed = [{'start':start, 'end':end, 'range_hash':text_hash(text[start:end]), 'replacement':replacement}
             for start, end, replacement in normalized]
    return {'base_content_hash':text_hash(text), 'after_text':after,
            'after_hash':text_hash(after), 'verified':False, 'apply_state':'candidate_only',
            'fixed_ranges':fixed, 'fixed_range':fixed[0] if len(fixed) == 1 else None}


def build_body_candidate(text, start, end, replacement, *, allow_delete=False):
    """单区间候选；与多区间路径共用同一实现。"""
    return build_body_candidate_multi(text, [{'start':start, 'end':end, 'replacement':replacement}],
                                      allow_delete=allow_delete)


def candidate_ranges(candidate):
    """候选声明的区间集合（兼容只带 fixed_range 的旧候选）。"""
    ranges = candidate.get('fixed_ranges')
    if ranges is None:
        single = candidate['fixed_range']
        ranges = [{'start':single['start'], 'end':single['end'], 'replacement':single['replacement']}]
    return [{'start':r['start'], 'end':r['end'], 'replacement':r['replacement']} for r in ranges]


def changed_body_sources(sources, generation_id, ranges):
    """被声明区间真正触及的正文段落 source id（原文字符坐标）。"""
    changed = set()
    for sid, source in sources.items():
        if source.get('source_id') != f'generation:{generation_id}':
            continue
        for entry in ranges:
            if source['start'] < entry['end'] and source['end'] > entry['start']:
                changed.add(sid)
                break
    return changed


def supported_citations(checks):
    """所有 supported finding 引用过的 source id。"""
    cited = set()
    for check in checks or []:
        if check.get('status') != 'supported':
            continue
        cited |= {q['source_id'] for q in check.get('citations', [])}
    return cited


def candidate_observation(observation, generation_id, candidate):
    import copy
    from app.agents.diagnostic import digest
    result = copy.deepcopy(observation)
    segments = result['data']['segments']
    segment = next((s for s in segments if s['generation_id'] == generation_id), None)
    if not segment or segment['text_hash'] != candidate['base_content_hash']:
        raise ValueError('stale_candidate')
    sources = result['data']['sources']
    text = result['data'].get('body_texts', {}).get(str(generation_id))
    if text is None or text_hash(text) != candidate['base_content_hash']:
        raise ValueError('stale_candidate')
    ranges = candidate_ranges(candidate)
    rebuilt = build_body_candidate_multi(text, ranges,
                                         allow_delete=any(r['replacement'] == '' for r in ranges))
    normalized = dict(candidate)
    normalized.setdefault('fixed_ranges', rebuilt['fixed_ranges'])
    if rebuilt != normalized: raise ValueError('stale_candidate')
    result['data']['sources'] = {k:v for k,v in sources.items() if v['source_id'] != f'generation:{generation_id}'}
    result['data']['sources'].update(paragraph_sources(candidate['after_text'], f'generation:{generation_id}', prefix=f"G{segment['segment_no']}P"))
    result['data']['body_texts'][str(generation_id)] = candidate['after_text']
    segment['text_hash'] = candidate['after_hash']
    result['candidate_overlay'] = {'generation_id':generation_id, 'base_content_hash':candidate['base_content_hash'],
                                   'fixed_range':rebuilt['fixed_range'], 'fixed_ranges':rebuilt['fixed_ranges'],
                                   'business_written':False}
    result['input_fingerprint'] = digest(result['data'])
    return result


def read_body(db, project, scope):
    numbers = scope.get('segment_nos')
    if (not isinstance(numbers, list) or not 1 <= len(numbers) <= 10
            or any(type(n) is not int or n < 1 for n in numbers) or len(set(numbers)) != len(numbers)):
        raise ValueError('segment_scope_required')
    rows = db.scalars(select(Generation).where(Generation.project_id == project.id,
        Generation.segment_no.in_(numbers)).order_by(Generation.segment_no, Generation.id)).all()
    if len(rows) != len(numbers) or {r.segment_no for r in rows} != set(numbers):
        raise PermissionError('segment_not_found_or_ambiguous')
    contracts = ((project.plot_timeline or {}).get('planning') or {}).get('segment_contracts') or {}
    sources, segments = {}, []
    for row in rows:
        text = row.text or ''
        if len(text) > 40000: raise ValueError('body_scope_too_large')
        sources.update(paragraph_sources(text, f'generation:{row.id}', prefix=f'G{row.segment_no}P'))
        contract = contracts.get(str(row.segment_no))
        segments.append({'generation_id':row.id, 'segment_no':row.segment_no, 'text_hash':text_hash(text),
                         'contract':contract, 'unknown':None if contract else 'missing_saved_contract'})
    from app.agents.diagnostic import digest
    data = {'sources':sources, 'segments':segments, 'body_texts':{str(r.id):r.text or '' for r in rows},
            'warning':'quote_location_is_not_semantic_proof'}
    return data, digest(data), {'segment_nos':sorted(numbers), 'source':'Generation.text+saved_segment_contract',
                              'complete':True, 'contract_complete':all(s['contract'] for s in segments)}
