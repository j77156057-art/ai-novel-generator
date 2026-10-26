"""依赖链式多区间修订：声明的修复集合共享同一违例根因，范围外逐字不变。"""
import json

import pytest
from test_causal_timeline import fixture, Reply


BODY = ('她掏出手机拍了两张照片。\n林夏俯身看了那两张照片。\n灯灭了。\n桌上的手机计时器响了。')


def authority_fixture():
    """closed_inventory 诊断报告：只有 G1P001 挂了 contradicted 检查。"""
    db, p = fixture()
    from app.models import Generation, AgentRun
    from app.agents.diagnostic import read_capability, digest
    t = dict(p.plot_timeline)
    t['planning'] = {'segment_contracts': {'1': {'authority': {'premise': '清单完整：一本登记本。不得新增物品。'},
                                                      'node_id': 'n1'}}}
    p.plot_timeline = t
    g = Generation(project_id=p.id, segment_no=1, text=BODY); db.add(g); db.commit()
    scope = {'character': '', 'cursor': 0, 'limit': 5, 'segment_nos': [1], 'inventory_kinds': ['object']}
    obs = read_capability(db, p.id, 'read_closed_inventory', scope)
    sources = obs['data']['sources']
    a = next(k for k, v in sources.items() if v.get('path') == '/authority/premise')
    b = 'G1P001'

    def cite(s):
        return dict(sources[s], source_id=s, body_source_id=sources[s]['source_id'], location_verified=True)

    constraint = {'kind': 'object', 'mode': 'closed', 'source_id': a, 'quote': sources[a]['quote'], 'segment_no': 1}
    candidate = {'id': 'N001', 'constraint': constraint, 'mention': {'source_id': b},
                 'verdict': {'status': 'violated', 'body_source_id': b, 'authority_source_id': a}}
    report = {'project_id': p.id, 'status': 'completed', 'agent_role': 'diagnostic', 'provider_role': 'assistant',
              'model_participated': True, 'skill': {'id': 'closed_inventory', 'version': 4, 'status': 'experimental'},
              'scope': scope, 'observations': [obs], 'input_fingerprint': digest([obs]),
              'acceptance': [{'id': 'full_scope_coverage', 'requires_evidence': True}],
              'inventory': {'input_fingerprint': obs['input_fingerprint'], 'candidates': [candidate]},
              'checks': [{'id': 'object_N001', 'status': 'contradicted', 'reason': '手机不在清单内',
                          'citations': [cite(a), cite(b)]}],
              'unresolved': ['inventory_partial_coverage']}
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='test')
    db.add(run); db.flush(); report['run_id'] = run.id; run.note = json.dumps(report); db.commit()
    p1 = (BODY.index('她掏出手机'), BODY.index('她掏出手机') + len('她掏出手机拍了两张照片。'))
    p2 = (BODY.index('林夏俯身'), BODY.index('林夏俯身') + len('林夏俯身看了那两张照片。'))
    ranges = [{'start': p1[0], 'end': p1[1], 'conflict_source_id': 'G1P001'},
              {'start': p2[0], 'end': p2[1], 'dependency': True}]
    args = dict(project_id=p.id, diagnostic_run_id=run.id, generation_id=g.id, objective='消除手机依赖，不引入清单外物件',
                preserve_source_ids=[a], conflict_source_id='G1P001', ranges=ranges,
                dependency_note='手机的照片在后文被再次引用，两段必须一起改。')
    return db, p, g, run, args, a


def test_multi_candidate_only_touches_declared_ranges():
    from app.agents.body_consistency import build_body_candidate_multi, text_hash
    first = (BODY.index('她掏出手机'), BODY.index('她掏出手机') + len('她掏出手机拍了两张照片。'))
    second = (BODY.index('林夏俯身'), BODY.index('林夏俯身') + len('林夏俯身看了那两张照片。'))
    candidate = build_body_candidate_multi(BODY, [
        {'start': first[0], 'end': first[1], 'replacement': '她举起登记本记下两项。'},
        {'start': second[0], 'end': second[1], 'replacement': '林夏俯身核对了那两行字。'}])
    after = candidate['after_text']
    assert after.startswith('她举起登记本记下两项。')
    assert '林夏俯身核对了那两行字。' in after
    # 范围外逐字不变：第三个、第四个段落和所有换行位置原样保留。
    assert after.endswith('灯灭了。\n桌上的手机计时器响了。')
    assert BODY[BODY.index('灯灭了。'):] == after[after.index('灯灭了。'):]
    assert len(candidate['fixed_ranges']) == 2
    assert candidate['fixed_range'] is None
    assert candidate['fixed_ranges'][0]['start'] == first[0]
    assert candidate['fixed_ranges'][0]['replacement'] == '她举起登记本记下两项。'
    assert candidate['fixed_ranges'][0]['range_hash'] == text_hash(BODY[first[0]:first[1]])
    assert candidate['after_hash'] == text_hash(after)
    assert candidate['base_content_hash'] == text_hash(BODY)
    assert candidate['verified'] is False and candidate['apply_state'] == 'candidate_only'


def test_single_range_candidate_keeps_legacy_shape():
    from app.agents.body_consistency import build_body_candidate
    start = BODY.index('她掏出手机')
    end = start + len('她掏出手机拍了两张照片。')
    candidate = build_body_candidate(BODY, start, end, '她举起登记本记下两项。')
    assert candidate['fixed_range']['start'] == start
    assert candidate['fixed_ranges'] == [candidate['fixed_range']]
    assert candidate['after_text'] == BODY[:start] + '她举起登记本记下两项。' + BODY[end:]


@pytest.mark.parametrize('ranges',[
    [{'start': 10, 'end': 20, 'replacement': 'x'}, {'start': 15, 'end': 25, 'replacement': 'y'}],
    [{'start': 20, 'end': 30, 'replacement': 'x'}, {'start': 0, 'end': 10, 'replacement': 'y'}],
    [{'start': 0, 'end': len(BODY) + 5, 'replacement': 'x'}],
    [{'start': -1, 'end': 5, 'replacement': 'x'}],
    [{'start': 5, 'end': 5, 'replacement': 'x'}],
    [],
    [{'start': 0, 'end': 4, 'replacement': ''}],
])
def test_invalid_range_sets_are_rejected(ranges):
    from app.agents.body_consistency import build_body_candidate_multi
    with pytest.raises(ValueError, match='invalid_candidate_range'):
        build_body_candidate_multi(BODY, ranges)


def test_too_many_ranges_are_rejected():
    from app.agents.body_consistency import build_body_candidate_multi, MAX_REPAIR_RANGES
    assert MAX_REPAIR_RANGES == 8
    ranges = [{'start': i, 'end': i + 1, 'replacement': 'x'} for i in range(MAX_REPAIR_RANGES + 1)]
    with pytest.raises(ValueError, match='invalid_candidate_range'):
        build_body_candidate_multi(BODY, ranges)


def test_boundary_echo_rejects_context_copy_but_allows_other_selected_range():
    from app.agents.body_consistency import build_body_candidate_multi
    text = '她拿起登记本翻到第一页。她把纸压在桌角。她把登记本放回架子上。'
    first = (0, len('她拿起登记本翻到第一页。'))
    second = (first[1], first[1] + len('她把纸压在桌角。'))
    # 单区间：复制紧随其后的上下文 —— 协议错误。
    with pytest.raises(ValueError, match='candidate_boundary_echo'):
        build_body_candidate_multi(text, [{'start': first[0], 'end': first[1],
                                           'replacement': '她把纸压在桌角。'}])
    # 多区间：同样的文字落在另一个被选区间里 —— 合法，不判错。
    candidate = build_body_candidate_multi(text, [
        {'start': first[0], 'end': first[1], 'replacement': '她把纸压在桌角。'},
        {'start': second[0], 'end': second[1], 'replacement': '她把纸压平。'}])
    assert candidate['after_text'] == '她把纸压在桌角。她把纸压平。她把登记本放回架子上。'
    # 复制不属于任何区间的上下文仍然是错误。
    with pytest.raises(ValueError, match='candidate_boundary_echo'):
        build_body_candidate_multi(text, [
            {'start': first[0], 'end': first[1], 'replacement': '她把纸压在桌角。'},
            {'start': second[0], 'end': second[1], 'replacement': '她把登记本放回架子上。'}])


def test_declared_dependency_without_check_is_accepted_by_server_validation():
    from app.agents.body_repair import prepare_body_repair
    db, p, g, run, args, a = authority_fixture()
    card = prepare_body_repair(db, **args)
    body = card['pending_action']['body']
    assert body['ranges'][0]['conflict_source_id'] == 'G1P001'
    assert body['ranges'][0]['dependency'] is False
    assert body['ranges'][1]['dependency'] is True
    assert body['ranges'][1]['conflict_source_id'] is None
    assert body['dependency_note'] == args['dependency_note']
    assert body['conflict_source_id'] == 'G1P001'
    assert body['base_content_hash']


def test_declared_card_is_a_pure_read():
    from app.agents.body_repair import prepare_body_repair
    from app.models import AgentRun
    db, p, g, run, args, a = authority_fixture()
    before = (g.text, run.note)
    runs = db.query(AgentRun).count()
    card = prepare_body_repair(db, **args)
    assert card['pending_action']['kind'] == 'body_repair'
    assert card['pending_action']['endpoint'] == '/api/assistant/body-repairs'
    assert card['pending_action']['body']['ranges'] == [
        {'start': args['ranges'][0]['start'], 'end': args['ranges'][0]['end'],
         'conflict_source_id': 'G1P001', 'dependency': False},
        {'start': args['ranges'][1]['start'], 'end': args['ranges'][1]['end'],
         'conflict_source_id': None, 'dependency': True}]
    assert before == (g.text, run.note) and not db.dirty
    assert db.query(AgentRun).count() == runs


def test_dependency_outside_target_generation_is_rejected():
    from app.agents.body_repair import prepare_body_repair
    db, p, g, run, args, a = authority_fixture()
    spanning = {'start': BODY.index('灯灭了。'), 'end': len(BODY), 'dependency': True}
    with pytest.raises(ValueError, match='repair_scope_denied'):
        prepare_body_repair(db, **dict(args, ranges=[args['ranges'][0], spanning]))


def test_dependency_overlapping_primary_range_is_rejected():
    from app.agents.body_repair import prepare_body_repair
    db, p, g, run, args, a = authority_fixture()
    primary = args['ranges'][0]
    overlapping = {'start': primary['start'] + 1, 'end': primary['end'], 'dependency': True}
    with pytest.raises(ValueError, match='repair_scope_denied'):
        prepare_body_repair(db, **dict(args, ranges=[primary, overlapping]))


def test_dependency_without_note_is_rejected():
    from app.agents.body_repair import prepare_body_repair
    db, p, g, run, args, a = authority_fixture()
    with pytest.raises(ValueError, match='repair_requires_clarification'):
        prepare_body_repair(db, **dict(args, dependency_note=''))


def body_path_fixture():
    """普通正文诊断：preserve_source_ids 是正文段落。"""
    from app.models import Generation
    from app.agents.diagnostic import run_diagnostic_task
    db, p = fixture()
    text = '已在账册写下页数。\n账册从未使用。\n灯仍亮着。'
    g = Generation(project_id=p.id, segment_no=1, text=text); db.add(g); db.commit()
    answer = {'assessment': 'observed', 'reason': '冲突',
              'findings': [{'dimension': 'object_state', 'assessment': 'contradicted', 'reason': '已写却未用',
                            'source_ids': ['G1P001', 'G1P002']}]}
    diag = run_diagnostic_task(db, p.id, task_type='body_consistency', skill_id='body_consistency',
                               allow_experimental=True, segment_nos=[1], provider=Reply(answer))
    start = text.index('账册从未'); end = start + len('账册从未使用。')
    args = dict(project_id=p.id, diagnostic_run_id=diag['run_id'], generation_id=g.id,
                objective='保留已写入事实，消除全称否定', preserve_source_ids=['G1P001'],
                conflict_source_id='G1P002')
    return db, p, g, text, args


def test_dependency_overlapping_preserve_paragraph_is_rejected():
    from app.agents.body_repair import prepare_body_repair
    db, p, g, text, args = body_path_fixture()
    dependency = {'start': 0, 'end': len('已在账册写下页数。'), 'dependency': True}
    ranges = [{'start': text.index('账册从未'), 'end': text.index('账册从未') + len('账册从未使用。'),
               'conflict_source_id': 'G1P002'}, dependency]
    with pytest.raises(ValueError, match='repair_scope_denied'):
        prepare_body_repair(db, **dict(args, ranges=ranges, dependency_note='依赖说明'))


def multi_repair(assistant_answer=None):
    """跑一次多区间修复：一次 Writer、一次独立 Assistant。"""
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p, g, run, args, a = authority_fixture()
    card = prepare_body_repair(db, **args)['pending_action']['body']
    writer = Reply({'replacements': {'G1P001': '她举起登记本记下两项。',
                                     'G1P002': '林夏俯身核对了那两行字。'}})
    answer = assistant_answer(a) if callable(assistant_answer) else (assistant_answer or {
        'assessment': 'observed', 'reason': '范围内一致',
        'findings': [{'dimension': 'object_state', 'assessment': 'supported', 'reason': '两段都不再依赖手机',
                      'source_ids': [a, 'G1P001', 'G1P002']}]})
    assistant = Reply(answer)
    result = create_body_repair(db, **card, writer=writer, assistant=assistant)
    return db, p, g, run, card, writer, assistant, result, a


def test_multi_range_repair_is_one_writer_one_assistant_and_stays_pending():
    from app.models import EditProposal
    db, p, g, run, card, writer, assistant, result, a = multi_repair()
    assert len(writer.calls) == 1 and len(assistant.calls) == 1
    assert result['proposal']['status'] == 'pending'
    assert db.query(EditProposal).count() == 1
    assert g.text == BODY
    candidate = result['candidate']
    assert len(candidate['fixed_ranges']) == 2 and candidate['fixed_range'] is None
    assert candidate['after_text'].endswith('灯灭了。\n桌上的手机计时器响了。')


def proposal_scope(db):
    """提案的服务端 scope（客户端看不到、也不能伪造的那部分）。"""
    from app.models import EditProposal
    return db.query(EditProposal).one().scope_json


def test_multi_range_local_apply_is_authorized_only_when_every_changed_paragraph_is_cited():
    db, p, g, run, card, writer, assistant, result, a = multi_repair()
    scope = proposal_scope(db)
    assert scope['local_apply_authorized'] is True
    assert scope['fixed_ranges'] and len(scope['fixed_ranges']) == 2
    state = scope['global_acceptance_state']
    assert state['state'] == 'carried_forward'
    assert state['closed_check_ids'] == ['object_N001']
    assert 'inventory_partial_coverage' in state['unresolved']
    assert state['bound_body_hash'] == result['candidate']['after_hash']
    assert state['source_run_id'] == result['verification']['run_id']
    assert result['verification']['local_semantic_accepted'] is True
    assert result['verification_acceptance']['local_repair_verification'] == 'accepted'


def test_multi_range_local_apply_is_refused_when_one_changed_paragraph_is_uncited():
    from app.models import EditProposal
    def answer(a):
        return {'assessment': 'observed', 'reason': '只看了第一段',
                'findings': [{'dimension': 'object_state', 'assessment': 'supported', 'reason': '第一段一致',
                              'source_ids': [a, 'G1P001']}]}
    db, p, g, run, card, writer, assistant, result, a = multi_repair(assistant_answer=answer)
    scope = proposal_scope(db)
    assert scope['local_apply_authorized'] is False
    assert result['verification_acceptance']['local_repair_verification'] == 'unknown'
    assert g.text == BODY


def test_multi_range_writer_contract_is_a_ordered_replacement_list():
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p, g, run, args, a = authority_fixture()
    card = prepare_body_repair(db, **args)['pending_action']['body']

    class Writer:
        calls = 0

        def chat(self, system, user):
            self.calls += 1
            data = json.loads(user)
            assert data['selected_texts'] == ['她掏出手机拍了两张照片。', '林夏俯身看了那两张照片。']
            assert data['edits'] == {'G1P001': '她掏出手机拍了两张照片。',
                                     'G1P002': '林夏俯身看了那两张照片。'}
            assert 'replacements' in system
            return json.dumps({'replacements': {'G1P001': '她举起登记本记下两项。',
                                                'G1P002': '林夏俯身核对了那两行字。'}})

    writer = Writer()
    assistant = Reply({'assessment': 'observed', 'reason': '一致',
                       'findings': [{'dimension': 'object_state', 'assessment': 'supported', 'reason': '两段一致',
                                     'source_ids': [a, 'G1P001', 'G1P002']}]})
    result = create_body_repair(db, **card, writer=writer, assistant=assistant)
    assert writer.calls == 1
    assert result['candidate']['after_text'].count('登记本') == 1


def test_multi_range_writer_answer_must_cover_every_source_id():
    """按来源ID对槽：少一段、多一段、键名改写 —— 一律整体拒绝，绝不把少改的段当成改好了。"""
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p, g, run, args, a = authority_fixture()
    card = prepare_body_repair(db, **args)['pending_action']['body']
    for answer in ({'replacements': {'G1P001': '只有一段。'}},
                   {'replacements': {'G1P001': 'x', 'G1P002': 'y', 'G1P003': 'z'}},
                   {'replacements': {'G1P001': 'x', 'G1P999': 'y'}},
                   {'replacements': ['x', 'y']}):
        with pytest.raises(ValueError, match='invalid_replacement_response'):
            create_body_repair(db, **card, writer=Reply(answer), assistant=Reply({}))


def test_single_range_writer_contract_is_unchanged():
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p, g, run, args, a = authority_fixture()
    single = dict(args, ranges=None, dependency_note='')
    single['start'] = BODY.index('她掏出手机')
    single['end'] = single['start'] + len('她掏出手机拍了两张照片。')
    card = prepare_body_repair(db, **single)['pending_action']['body']
    assert len(card['ranges']) == 1

    class Writer:
        calls = 0

        def chat(self, system, user):
            self.calls += 1
            data = json.loads(user)
            assert data['selected_text'] == '她掏出手机拍了两张照片。'
            assert '{"replacement":"选定范围的新文本"}' in system
            return json.dumps({'replacement': '她举起登记本记下两项。'})

    writer = Writer()
    assistant = Reply({'assessment': 'observed', 'reason': '一致',
                       'findings': [{'dimension': 'object_state', 'assessment': 'supported', 'reason': '一致',
                                     'source_ids': [a, 'G1P001']}]})
    result = create_body_repair(db, **card, writer=writer, assistant=assistant)
    assert writer.calls == 1
    assert result['candidate']['fixed_range']['start'] == single['start']
    assert result['candidate']['fixed_ranges'] == [result['candidate']['fixed_range']]


# --- 本地判定只看目标维度（修订前的死锁：整段维度保持 unknown） ---


def test_deferred_dimensions_do_not_block_the_local_gate():
    """time/place/participants 未检查即 unknown 属正常，不构成局部失败。"""
    def answer(a):
        return {'assessment': 'observed', 'reason': '只判定目标维度',
                'findings': [
                    {'dimension': 'time', 'assessment': 'unknown', 'reason': '本次未检查', 'source_ids': []},
                    {'dimension': 'place', 'assessment': 'unknown', 'reason': '本次未检查', 'source_ids': []},
                    {'dimension': 'participants', 'assessment': 'unknown', 'reason': '本次未检查', 'source_ids': []},
                    {'dimension': 'object_state', 'assessment': 'supported', 'reason': '两段都不再依赖手机',
                     'source_ids': [a, 'G1P001', 'G1P002']}]}
    db, p, g, run, card, writer, assistant, result, a = multi_repair(assistant_answer=answer)
    verification = result['verification']
    assert verification['local_semantic_accepted'] is True
    assert verification['global_quality_verified'] is False
    assert verification['deferred_dimensions'] == ['participants', 'place', 'time']
    assert 'inventory_partial_coverage' in verification['unresolved']
    assert 'local_semantic_review_required' not in verification['unresolved']
    assert proposal_scope(db)['local_apply_authorized'] is True


@pytest.mark.parametrize('assessment', ['contradicted', 'unknown'])
def test_target_dimension_failure_is_not_locally_accepted(assessment):
    def answer(a):
        return {'assessment': 'observed', 'reason': '目标维度没过',
                'findings': [{'dimension': 'object_state', 'assessment': assessment, 'reason': '仍有矛盾',
                              'source_ids': [a, 'G1P001', 'G1P002']}]}
    db, p, g, run, card, writer, assistant, result, a = multi_repair(assistant_answer=answer)
    assert result['verification']['local_semantic_accepted'] is False
    assert 'local_semantic_review_required' in result['verification']['unresolved']
    assert proposal_scope(db)['local_apply_authorized'] is False


# --- 多个主区间：各自有自己的目标检查 ---


MULTI_BODY = ('她搬来一张小凳子。\n她掏出手机拍了两张照片。\n灯灭了。\n'
              '桌上的手机计时器响了。\n林夏俯身看了那两张照片。')


def multi_primary_fixture():
    """两条互相独立的违例（object_N001 / object_N002）加一个依赖段落。"""
    db, p = fixture()
    from app.models import Generation, AgentRun
    from app.agents.diagnostic import read_capability, digest
    t = dict(p.plot_timeline)
    t['planning'] = {'segment_contracts': {'1': {'authority': {'premise': '清单完整：一本登记本。不得新增物品。'},
                                                      'node_id': 'n1'}}}
    p.plot_timeline = t
    g = Generation(project_id=p.id, segment_no=1, text=MULTI_BODY); db.add(g); db.commit()
    scope = {'character': '', 'cursor': 0, 'limit': 5, 'segment_nos': [1], 'inventory_kinds': ['object']}
    obs = read_capability(db, p.id, 'read_closed_inventory', scope)
    sources = obs['data']['sources']
    a = next(k for k, v in sources.items() if v.get('path') == '/authority/premise')

    def cite(s):
        return dict(sources[s], source_id=s, body_source_id=sources[s]['source_id'], location_verified=True)

    def candidate(cid, body):
        constraint = {'kind': 'object', 'mode': 'closed', 'source_id': a, 'quote': sources[a]['quote'],
                      'segment_no': 1}
        return {'id': cid, 'constraint': constraint, 'mention': {'source_id': body},
                'verdict': {'status': 'violated', 'body_source_id': body, 'authority_source_id': a}}

    report = {'project_id': p.id, 'status': 'completed', 'agent_role': 'diagnostic', 'provider_role': 'assistant',
              'model_participated': True, 'skill': {'id': 'closed_inventory', 'version': 4, 'status': 'experimental'},
              'scope': scope, 'observations': [obs], 'input_fingerprint': digest([obs]),
              'acceptance': [{'id': 'full_scope_coverage', 'requires_evidence': True}],
              'inventory': {'input_fingerprint': obs['input_fingerprint'],
                            'candidates': [candidate('N001', 'G1P001'), candidate('N002', 'G1P002')]},
              'checks': [{'id': 'object_N001', 'status': 'contradicted', 'reason': '小凳子不在清单内',
                          'citations': [cite(a), cite('G1P001')]},
                         {'id': 'object_N002', 'status': 'contradicted', 'reason': '手机不在清单内',
                          'citations': [cite(a), cite('G1P002')]}],
              'unresolved': ['inventory_partial_coverage']}
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='test')
    db.add(run); db.flush(); report['run_id'] = run.id; run.note = json.dumps(report); db.commit()
    first = (0, len('她搬来一张小凳子。'))
    second = (MULTI_BODY.index('她掏出手机'), MULTI_BODY.index('她掏出手机') + len('她掏出手机拍了两张照片。'))
    dependency = (MULTI_BODY.index('林夏俯身'), len(MULTI_BODY))
    ranges = [{'start': first[0], 'end': first[1], 'conflict_source_id': 'G1P001'},
              {'start': second[0], 'end': second[1], 'conflict_source_id': 'G1P002'},
              {'start': dependency[0], 'end': dependency[1], 'dependency': True}]
    args = dict(project_id=p.id, diagnostic_run_id=run.id, generation_id=g.id,
                objective='同时消除小凳子与手机的依赖，不引入清单外物件',
                preserve_source_ids=[a], conflict_source_id='G1P001', ranges=ranges,
                dependency_note='手机的照片在后文被再次引用，依赖段落必须一起改。')
    return db, p, g, run, args, a


def test_multiple_primary_ranges_each_carry_their_own_target_check():
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p, g, run, args, a = multi_primary_fixture()
    card = prepare_body_repair(db, **args)['pending_action']['body']
    assert [r['conflict_source_id'] for r in card['ranges']] == ['G1P001', 'G1P002', None]
    writer = Reply({'replacements': {'G1P001': '她把账册挪到桌上。', 'G1P002': '她举起登记本记下两项。', 'G1P005': '林夏俯身核对了那两行字。'}})
    answer = {'assessment': 'observed', 'reason': '目标维度已核对',
              'findings': [{'dimension': 'object_state', 'assessment': 'supported', 'reason': '三个改动段都看过',
                            'source_ids': [a, 'G1P001', 'G1P002', 'G1P005']}]}
    result = create_body_repair(db, **card, writer=writer, assistant=Reply(answer))
    verification = result['verification']
    assert verification['local_semantic_accepted'] is True
    assert verification['checks'][0]['status'] == 'supported'
    scope = proposal_scope(db)
    assert scope['global_acceptance_state']['closed_check_ids'] == ['object_N001', 'object_N002']
    assert scope['local_apply_authorized'] is True
    assert g.text == MULTI_BODY


def test_changed_body_source_ids_cover_every_declared_range():
    from app.agents.body_repair import prepare_body_repair, create_body_repair
    db, p, g, run, args, a = multi_primary_fixture()
    card = prepare_body_repair(db, **args)['pending_action']['body']
    writer = Reply({'replacements': {'G1P001': '她把账册挪到桌上。', 'G1P002': '她举起登记本记下两项。', 'G1P005': '林夏俯身核对了那两行字。'}})
    answer = {'assessment': 'observed', 'reason': '目标维度已核对',
              'findings': [{'dimension': 'object_state', 'assessment': 'supported', 'reason': '三个改动段都看过',
                            'source_ids': [a, 'G1P001', 'G1P002', 'G1P005']}]}
    assistant = Reply(answer)
    result = create_body_repair(db, **card, writer=writer, assistant=assistant)
    request = json.loads(assistant.calls[0])
    assert request['verification_requirements']['changed_body_source_ids'] == ['G1P001', 'G1P002', 'G1P005']
    assert request['verification_requirements']['scope'] == 'local_only'

