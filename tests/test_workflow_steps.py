"""助理 agent 的工序链：状态评估 + API + 只读工具。

要验的不是「函数返回了个 dict」，而是**判据真的按库里的事实说话**：
  - 没有参考语料 → 语料侧工序不适用（skipped），不是「通过」；
  - 语料切分坏了 → 清洗抽检**阻塞**（error 级），并说清卡在哪；
  - 语料干净 → 清洗抽检通过；
  - 状态是**重新评估**出来的：把分片补上/改坏，状态跟着变；
  - 待批的写回建议 → 修复那一步阻塞（等人批准）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402
from test_causal_timeline import fixture  # noqa: E402


@pytest.fixture
def api_db():
    """API 级测试用的库：内存 sqlite，且**允许跨线程**（TestClient 在别的线程跑请求）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Project
    # 内存 sqlite 每个连接一个独立库；StaticPool 让所有线程共用**同一条**连接，
    # 否则 TestClient 在别的线程里看到的是空库（no such table）。
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    project = Project(title='工序链测试', target_length=9000, mode='quick',
                      generation_length=3000)
    db.add(project)
    db.commit()
    yield db, project
    db.close()


def _seed_corpus(db, p, *, chunks, reference=True):
    """给项目塞一本语料（若干分片）。"""
    from app.models import NovelChunk, RawNovel
    novel = RawNovel(title='测试语料', status='imported')
    db.add(novel)
    db.flush()
    for i, text in enumerate(chunks, start=1):
        db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=i, text=text))
    if reference:
        p.reference_novel_ids = [novel.id]
    db.commit()
    db.refresh(p)
    return novel


@pytest.fixture
def client(api_db):
    """API 级测试的客户端：请求打到测试自己的内存库（不碰真实开发库）。

    **用完必须清掉 dependency_overrides** —— 否则后面的测试模块会接着打到
    这张内存库上（它们的 HTTP 用例会看到空库）。
    """
    from fastapi.testclient import TestClient
    from app.db import get_db
    from app.main import app as fastapi_app
    db, _project = api_db
    fastapi_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(fastapi_app)
    finally:
        fastapi_app.dependency_overrides.clear()


def test_empty_project_has_no_corpus_step_but_is_not_done():
    """没有参考语料 → 语料侧工序**不适用**（skipped），整体不是「完成」。

    自由生成的项目**不该**被提示去蒸馏（没语料就没有文风可提炼）——
    所以蒸馏、档案审查、提示词体检这三步都是 skipped，链直接落到正文轨。
    """
    from app.agents.workflow import evaluate
    db, p = fixture()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['corpus_ready']['state'] == 'skipped'
    assert by_id['clean_check']['state'] == 'skipped'
    assert by_id['distill']['state'] == 'skipped'
    assert by_id['review_narrative']['state'] == 'skipped'
    assert by_id['review_prompt']['state'] == 'skipped'
    assert state['status'] != 'done'
    # 自由生成的项目 → 语料侧不适用，链落到正文轨的「写正文」
    assert state['current_step'] == 'write_body'
    assert state['summary']  # 人话摘要不能为空


def test_healthy_corpus_passes_the_clean_check():
    from app.agents.workflow import evaluate
    db, p = fixture()
    _seed_corpus(db, p, chunks=['这一段有句号。', '这一段也有句号。'])
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['corpus_ready']['state'] == 'done'
    assert by_id['clean_check']['state'] == 'done'
    assert '片' in by_id['corpus_ready']['reason']


def test_broken_chunk_boundaries_block_the_clean_check():
    """分片停在半句话中间（切分没对齐边界）→ error 级 → 阻塞，并说清卡在哪。"""
    from app.agents.workflow import evaluate
    db, p = fixture()
    _seed_corpus(db, p, chunks=[
        '这一段没有句末标点就这样断掉了',
        '另一段也没有句末标点也断掉了',
        '第三段同样断在半句上',
    ])
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['clean_check']['state'] == 'blocked', by_id['clean_check']
    assert state['status'] == 'blocked'
    assert state['current_step'] == 'clean_check'
    assert '清洗抽检' in state['summary']


def test_state_is_re_evaluated_not_cached():
    """状态是**重新评估**出来的：语料变干净，阻塞就消失。"""
    from app.agents.workflow import evaluate
    from app.models import NovelChunk
    db, p = fixture()
    novel = _seed_corpus(db, p, chunks=['没有句号的一段', '也没有句号'])
    assert evaluate(db, p)['status'] == 'blocked'
    for chunk in db.query(NovelChunk).filter_by(novel_id=novel.id).all():
        chunk.text = chunk.text + '。'
    db.commit()
    assert evaluate(db, p)['status'] != 'blocked'


def test_pending_proposal_blocks_the_repair_step():
    """有待批的写回建议 → 修复那一步阻塞（等人批准）。"""
    from app.agents.workflow import evaluate
    from app.models import AgentRun, EditProposal
    db, p = fixture()
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='t')
    db.add(run)
    db.flush()
    db.add(EditProposal(run_id=run.id, project_id=p.id, type='manual',
                        target_kind='generations', target_table='generations',
                        patch_json={}, risk_level='low', status='pending'))
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['repair']['state'] == 'blocked'
    assert '批准' in by_id['repair']['reason']


def test_no_pending_proposal_is_not_reported_as_completed():
    """没有待批项 ≠ 这步「已完成」——它只是没东西可批。

    「已完成」在中文里意味着「该做的都做完了」，用户会以为正文已经改过；
    这两步其实一个字都还没改。
    """
    from app.agents.workflow import evaluate
    db, p = fixture()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['repair']['state'] == 'done'
    assert by_id['repair']['state_label'] == '没有待批项'
    assert state['open_proposals'] == []


def test_pending_proposals_are_listed_in_human_words():
    """待处理的写回建议要看得见、看得懂：改哪儿（人话）、为什么、多大改动。"""
    from app.agents.workflow import evaluate
    from app.models import AgentRun, EditProposal, Generation
    db, p = fixture()
    gen = Generation(project_id=p.id, segment_no=3, text='第三段正文')
    db.add(gen)
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='t')
    db.add(run)
    db.flush()
    db.add(EditProposal(run_id=run.id, project_id=p.id, type='text_edit',
                        target_kind='generation_text', target_table='generations',
                        target_id=str(gen.id), patch_json={}, risk_level='high',
                        status='pending', reason='第 3 段把角色名字写错了'))
    db.commit()
    state = evaluate(db, p)
    items = state['open_proposals']
    assert len(items) == 1, items
    assert items[0]['target'] == '正文第 3 段'
    assert items[0]['risk'] == '改动较大'
    assert '名字写错' in items[0]['reason']
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['repair']['state'] == 'blocked'
    assert by_id['repair']['state_label'] == '等你批'


def test_approved_proposal_stays_visible_until_it_is_applied():
    """批了但还没写进正文的改动，**不能**从清单里消失。

    流程坑：用户点「批准」后提案状态变成 approved —— 如果清单只查 pending，
    这条就从面板上不见了，用户再也点不到「应用」，这件事就卡死了。
    """
    from app.agents.workflow import evaluate
    from app.models import AgentRun, EditProposal
    db, p = fixture()
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='t')
    db.add(run)
    db.flush()
    db.add(EditProposal(run_id=run.id, project_id=p.id, type='prompt_edit',
                        target_kind='prompt_override', target_table='prompt_overrides',
                        target_id='1', patch_json={}, risk_level='low',
                        status='approved', reason='提示词里漏了禁用词'))
    db.commit()
    state = evaluate(db, p)
    items = state['open_proposals']
    assert len(items) == 1, items
    assert items[0]['status'] == 'approved'
    assert items[0]['target'] == '系统提示词'
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['repair']['state'] == 'blocked'
    assert by_id['repair']['state_label'] == '等你应用'


def test_the_current_step_says_next_step_and_queued_ones_say_pending():
    """当前这一步说「下一步」，排队的仍说「待做」——用户一眼知道该动哪一下。"""
    from app.agents.workflow import evaluate
    db, p = fixture()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    current = state['current_step']
    assert current, state
    assert by_id[current]['state_label'] == '下一步'
    queued = [s for s in state['steps']
              if s['state'] == 'pending' and s['id'] not in (current, 'converged')]
    assert queued
    assert all(s['state_label'] == '待做' for s in queued), queued


def test_the_converged_step_says_how_many_steps_are_left():
    """终态那一步说「还差几步」，不说「卡住」。

    说「卡住」会让人以为收敛本身坏了 —— 其实卡点在**前面**那道工序。
    """
    from app.agents.workflow import evaluate
    db, p = fixture()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    conv = by_id['converged']
    assert conv['title'] == '收敛'
    assert conv['state_label'].startswith('还差 '), conv
    # 开放的工序数（含被卡住的那道）就是「还差几步」
    open_steps = [s for s in state['steps']
                  if s['state'] in ('pending', 'blocked') and s['id'] != 'converged']
    assert conv['state_label'] == '还差 %d 步' % len(open_steps), (conv, open_steps)


def test_distill_step_is_done_when_the_profile_is_already_applied():
    """档案已经在项目上 → 这道工序就是做完了，哪怕本项目没跑过蒸馏。

    实测（《北荒刀歌》真书档案）：项目从蒸馏库**应用**了档案，`distill_runs` 是空的。
    老判据只看 distill_runs → 「文风蒸馏 待做」紧挨着「档案审查 已完成」，
    链条自相矛盾（面板上一眼就能看出不对）。
    """
    from app.agents.workflow import evaluate
    db, p = fixture()
    _seed_corpus(db, p, chunks=['这一段有句号。', '这一段也有句号。'])
    p.style_narrative = {'rules': {'对白风格': [{'rule': '短句为主'}]}}
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['distill']['state'] == 'done', by_id['distill']
    assert '档案已就位' in by_id['distill']['reason']
    # 与档案审查不再自相矛盾
    assert by_id['review_narrative']['state'] in ('done', 'pending', 'blocked')


def test_distill_step_points_at_the_reusable_archive_when_it_is_not_applied():
    """蒸馏库里有这份语料的档案、但还没应用 → 提示**去应用**，不是「还没跑过蒸馏」。"""
    from app.agents.workflow import evaluate
    from app.models import StyleDistill
    from app.style.narrative import _source_ids_hash
    db, p = fixture()
    novel = _seed_corpus(db, p, chunks=['这一段有句号。'])
    db.add(StyleDistill(
        source_ids=[novel.id],
        source_ids_hash=_source_ids_hash([novel.id]),
        tier='full', tier_signature='sig', content_hash='hash',
        narrative_version=1, status='active',
        style_narrative={'rules': {}}, rule_count=42))
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['distill']['state'] == 'pending', by_id['distill']
    assert '蒸馏库' in by_id['distill']['reason']
    assert '42' in by_id['distill']['reason']


def test_write_body_says_plan_the_outline_when_there_is_none():
    """没有剧情大纲 → 这步是**卡住**「先规划大纲」，不是「去写正文」。

    实测（真书全流程）：没有大纲 → 引擎不规划时间线 → 没有段契约 →
    封闭清单审计零候选（missing_saved_authority）→ 整段验收 no_pending_acceptance。
    写出来的正文过不了审查，所以链上不能把人引向「去写正文」。
    """
    from app.agents.workflow import evaluate
    db, p = fixture()
    # 真实形态：没有大纲 → 引擎不规划时间线 → 没有段契约
    p.outline = None
    p.plot_timeline = {}
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    write = by_id['write_body']
    assert write['state'] == 'blocked', write
    assert '大纲' in write['reason']
    assert write['state_label'] == '先规划大纲'


def test_write_body_is_pending_once_there_is_an_outline():
    from app.agents.workflow import evaluate
    db, p = fixture()
    p.outline = ['第一段：少年在渡口摆渡，交代渡口的规矩。']
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['write_body']['state'] == 'pending', by_id['write_body']
    assert '还没有正文' in by_id['write_body']['reason']


def test_written_body_without_a_segment_contract_is_blocked():
    """写了正文但段段没有契约 → 写正文与正文审查都要说清「先补段契约」。"""
    from app.agents.workflow import evaluate
    from app.models import Generation
    db, p = fixture()
    p.outline = ['第一段：少年在渡口摆渡，交代渡口的规矩。']
    db.add(Generation(project_id=p.id, segment_no=1, text='少年把竹篙插进石缝。'))
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['write_body']['state'] == 'blocked', by_id['write_body']
    assert '段契约' in by_id['write_body']['reason']
    assert by_id['review_body']['state'] == 'blocked', by_id['review_body']
    assert 'missing_saved_authority' in by_id['review_body']['reason']


def test_written_body_with_a_segment_contract_is_done():
    """段段都有冻结契约 → 写正文 done。"""
    from app.agents.workflow import evaluate
    from app.models import Generation
    db, p = fixture()
    p.outline = ['第一段：少年在渡口摆渡，交代渡口的规矩。']
    timeline = dict(p.plot_timeline or {})
    timeline['planning'] = {'segment_contracts': {
        '1': {'node_id': 'n1', 'prompt': '……',
              'authority': {'premise': p.premise or '设定', 'causal_node': {'title': '摆渡'}}}}}
    p.plot_timeline = timeline
    db.add(Generation(project_id=p.id, segment_no=1, text='少年把竹篙插进石缝。'))
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['write_body']['state'] == 'done', by_id['write_body']
    # 有契约了，正文审查就回到「还没做整段验收」而不是「没有判据基准」
    assert by_id['review_body']['state'] == 'pending', by_id['review_body']
    assert '整段验收' in by_id['review_body']['reason']


def test_the_current_step_is_the_first_unfinished_one_not_the_first_blocked_one():
    """当前步 = 链上**第一个还没做完**的工序。

    「写正文」缺段契约时它会被卡住，但前面可能还有没做的工序（蒸馏）。
    说「卡在写正文」会让人以为先解决它 —— 其实先做蒸馏才对。
    """
    from app.agents.workflow import evaluate
    from app.models import NovelChunk, RawNovel
    db, p = fixture()
    novel = RawNovel(title='语料', status='imported')
    db.add(novel)
    db.flush()
    db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=1, text='这一段有句号。'))
    p.reference_novel_ids = [novel.id]
    p.outline = None
    p.plot_timeline = {}
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    # 蒸馏还没做（pending），写正文因为没大纲被卡住（blocked）
    assert by_id['distill']['state'] == 'pending'
    assert by_id['write_body']['state'] == 'blocked'
    # 当前步必须是**蒸馏**：先做蒸馏，不是先解决写正文
    assert state['current_step'] == 'distill', state['current_step']
    assert '蒸馏' in state['summary']
    assert '写正文' in state['summary'], state['summary']
    assert state['status'] == 'blocked'


def test_workflow_api_returns_the_state(api_db, client):
    """GET /api/projects/{id}/workflow → 工序状态（人话 + 机器字段）。"""
    db, p = api_db
    pass
    resp = client.get(f'/api/projects/{p.id}/workflow')
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['project_id'] == p.id
    assert data['status'] in ('active', 'done', 'blocked')
    ids = [s['id'] for s in data['steps']]
    assert ids[:3] == ['corpus_ready', 'clean_check', 'distill']
    assert data['summary']


def test_workflow_log_records_a_step_without_writing_anything_else(api_db, client):
    """POST /api/projects/{id}/workflow/log → 只留痕（不执行写动作）。"""
    db, p = api_db
    pass
    resp = client.post(f'/api/projects/{p.id}/workflow/log', json={
        'step_id': 'clean_check', 'action': '复查了清洗结果', 'detail': '抽样 20 段'})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data['status'] in ('active', 'done', 'blocked')
    from app.models import WorkflowRun
    run = db.query(WorkflowRun).filter_by(project_id=p.id).one()
    assert run.steps[-1]['step_id'] == 'clean_check'
    assert run.steps[-1]['action'] == '复查了清洗结果'


def test_unknown_step_is_rejected(api_db, client):
    db, p = api_db
    pass
    resp = client.post(f'/api/projects/{p.id}/workflow/log', json={
        'step_id': 'not_a_step', 'action': 'x'})
    assert resp.status_code == 422


def test_agent_can_record_the_dispatch():
    """派发留痕：agent 记录「把这一步派给了谁」，链的账里能看到。

    注意它**不能宣布工序完成** —— 工序状态仍是重新评估出来的。
    """
    from app.agents.registry import get_tool_registry
    from app.models import WorkflowRun
    db, p = fixture()
    out = get_tool_registry().invoke(
        'record_workflow_step', db, p.id, step_id='review_body',
        action='派给审查 agent（封闭清单审计）', detail='876 段')
    assert out['ok'] is True, out
    run = db.query(WorkflowRun).filter_by(project_id=p.id).one()
    assert run.steps[-1]['step_id'] == 'review_body'
    assert '审查' in run.steps[-1]['action']
    # 状态仍由重新评估得出（日志没有把它「宣布完成」）
    by_id = {s['id']: s for s in out['workflow']['steps']}
    assert by_id['review_body']['state'] == 'pending'


def test_recording_an_unknown_step_is_refused():
    from app.agents.registry import get_tool_registry
    db, p = fixture()
    out = get_tool_registry().invoke(
        'record_workflow_step', db, p.id, step_id='not_a_step', action='x')
    assert out['ok'] is False
    assert '未知工序' in out['note']


def test_agent_tool_is_read_only():
    """agent 工具 get_workflow_state 只读：调用后不留下任何行。"""
    from app.agents.registry import get_tool_registry
    from app.models import WorkflowRun
    db, p = fixture()
    out = get_tool_registry().invoke('get_workflow_state', db, p.id)
    assert out['found'] is True, out
    assert out['steps']
    # 只读：不该留下工序链行
    assert db.query(WorkflowRun).filter_by(project_id=p.id).count() == 0
