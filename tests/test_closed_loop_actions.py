"""闭环四类动作提案（片 3）：验收 / 修复 / 重建 / 解锁。

要守住的四条：
1. **端点由服务端说了算** —— agent 只给 kind，body 里的指纹由服务端算；
2. **前提漂移能被发现** —— 正文变了、验收过了、重建完了，提案就该 stale；
3. **只提不执行** —— 提卡不落库；执行要人点两次；
4. **工序链上位置对** —— 每类动作只在该步能提，别的步提要说得清为什么。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402
from test_causal_timeline import fixture  # noqa: E402

from app.agents.action_proposals import (  # noqa: E402
    ACTION_ENDPOINTS, ACTION_KINDS, EXECUTORS, PRECONDITIONS,
    build_action_proposal, check_precondition,
)
from app.agents.registry import invoke_tool  # noqa: E402
from app.agents.workflow import (  # noqa: E402
    STEP_BY_ID, STEP_WRITE_KINDS, evaluate,
)
from app.models import Generation  # noqa: E402

CLOSED_LOOP_KINDS = ('close_acceptance', 'apply_body_repair',
                     'derived_rebuild', 'unlock_generation')


def _with_body(db, p, text='少年把竹篙插进石缝，船身晃了晃。'):
    g = Generation(project_id=p.id, segment_no=1, text=text)
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


# ----------------------------------------------------------------------
# 契约：四类动作都齐（前提算法 / 执行器 / 端点）
# ----------------------------------------------------------------------

def test_every_closed_loop_action_has_precondition_executor_and_endpoint():
    for kind in CLOSED_LOOP_KINDS:
        assert kind in ACTION_KINDS
        assert kind in PRECONDITIONS
        assert kind in EXECUTORS
        assert kind in ACTION_ENDPOINTS


def test_the_endpoints_are_the_ones_that_already_exist():
    """不许为闭环另开旁路端点 —— 用的是既有那几条。"""
    assert ACTION_ENDPOINTS['close_acceptance'] == '/api/assistant/body-acceptance'
    assert ACTION_ENDPOINTS['apply_body_repair'] == '/api/assistant/body-repairs'
    assert ACTION_ENDPOINTS['derived_rebuild'] == '/api/assistant/derived-rebuilds'
    assert ACTION_ENDPOINTS['unlock_generation'] == '/api/assistant/derived-rebuilds'


# ----------------------------------------------------------------------
# 提卡：body 里的指纹由服务端算，不由模型填
# ----------------------------------------------------------------------

def test_close_acceptance_body_carries_a_server_computed_fingerprint():
    """指纹必须由服务端算 —— 模型自己填一个就等于「批的是哪一版正文」没人管。"""
    from app.agents.action_proposals import build_action_body
    db, p = fixture()
    _with_body(db, p)
    body = build_action_body('close_acceptance', p.id, db=db, project=p)
    assert body['confirm'] is True
    assert body['project_id'] == p.id
    assert len(body['base_content_hash']) == 64


def test_rebuild_and_unlock_bodies_carry_the_derived_fingerprint():
    from app.agents.action_proposals import build_action_body
    db, p = fixture()
    _with_body(db, p)
    for kind, action in (('derived_rebuild', 'rebuild'), ('unlock_generation', 'unlock')):
        body = build_action_body(kind, p.id, db=db, project=p)
        assert body['action'] == action
        assert len(body['base_fingerprint']) == 64


def test_body_repair_without_a_scope_is_refused_with_a_reason():
    from app.agents.action_proposals import build_action_body
    db, p = fixture()
    _with_body(db, p)
    with pytest.raises(Exception) as exc:
        build_action_body('apply_body_repair', p.id, db=db, project=p,
                          repair={'diagnostic_run_id': None, 'generation_id': None})
    assert '指明' in str(exc.value)


def test_the_chain_keeps_the_agent_from_proposing_at_the_wrong_step():
    """闭环动作只在该步能提 —— 别的步提要说清为什么，不能静默。"""
    db, p = fixture()
    _with_body(db, p)
    out = invoke_tool('propose_action', db=db, project_id=p.id,
                      kind='unlock_generation', title='解除守卫')
    assert out['ok'] is False
    assert out['current_step']
    assert '不能提' in out['note']


def test_each_closed_loop_action_is_allowed_at_exactly_its_own_step():
    # 「正文审查」能提两个：跑验收、或提一个定点修复（审出问题后的两个正当回应）。
    # 只在「修复待批」那步才许提修复是不行的 —— 那步说的是「已经有东西等着批」，
    # 审出问题时它还是「没有待批项」，agent 会被卡住（实测：真书项目 8）。
    assert STEP_WRITE_KINDS['review_body'] == ('close_acceptance', 'apply_body_repair')
    assert STEP_WRITE_KINDS['repair'] == ('apply_body_repair',)
    assert STEP_WRITE_KINDS['derived_rebuild'] == ('derived_rebuild',)
    assert STEP_WRITE_KINDS['unlock'] == ('unlock_generation',)
    for step_id, kinds in STEP_WRITE_KINDS.items():
        assert step_id in STEP_BY_ID, step_id
        for kind in kinds:
            assert kind in ACTION_KINDS


# ----------------------------------------------------------------------
# 前提漂移：正文变了 / 验收过了 / 重建完了 → stale
# ----------------------------------------------------------------------

def test_close_acceptance_goes_stale_once_the_body_changes():
    db, p = fixture()
    g = _with_body(db, p)
    row = build_action_proposal(
        db, p, kind='close_acceptance', title='整段验收',
        endpoint='/api/assistant/body-acceptance',
        body={'project_id': p.id, 'base_content_hash': 'x', 'confirm': True})
    assert check_precondition(db, p, row) is True
    g.text = '少年把竹篙插进石缝，船身晃了晃。他停住了。'
    db.commit()
    assert check_precondition(db, p, row) is False


def test_close_acceptance_goes_stale_once_somebody_else_accepted():
    db, p = fixture()
    _with_body(db, p)
    row = build_action_proposal(
        db, p, kind='close_acceptance', title='整段验收',
        endpoint='/api/assistant/body-acceptance',
        body={'project_id': p.id, 'base_content_hash': 'x', 'confirm': True})
    timeline = dict(p.plot_timeline or {})
    timeline['pending_acceptance'] = {'state': 'accepted'}
    p.plot_timeline = timeline
    db.commit()
    assert check_precondition(db, p, row) is False


def test_derived_rebuild_goes_stale_once_the_derived_data_is_rebuilt():
    db, p = fixture()
    _with_body(db, p)
    row = build_action_proposal(
        db, p, kind='derived_rebuild', title='重建派生数据',
        endpoint='/api/assistant/derived-rebuilds',
        body={'project_id': p.id, 'base_fingerprint': 'x', 'action': 'rebuild'})
    assert check_precondition(db, p, row) is True
    timeline = dict(p.plot_timeline or {})
    timeline['derived_state'] = {'kg': 'rebuilt', 'facts': 'rebuilt'}
    p.plot_timeline = timeline
    db.commit()
    assert check_precondition(db, p, row) is False


def test_body_repair_needs_a_diagnostic_run_of_this_project():
    from app.agents.action_proposals import precondition_now
    db, p = fixture()
    g = _with_body(db, p)
    with pytest.raises(Exception) as exc:
        precondition_now(db, p, 'apply_body_repair',
                         {'diagnostic_run_id': 999, 'generation_id': g.id})
    assert '诊断' in str(exc.value)


# ----------------------------------------------------------------------
# 工序链：闭环那两道工序的判据
# ----------------------------------------------------------------------

def test_the_chain_has_the_rebuild_and_unlock_steps():
    db, p = fixture()
    _with_body(db, p)
    state = evaluate(db, p)
    ids = [s['id'] for s in state['steps']]
    assert 'derived_rebuild' in ids
    assert 'unlock' in ids
    # 收敛永远在最后
    assert ids[-1] == 'converged'


def test_rebuild_and_unlock_are_skipped_when_the_body_was_never_revised():
    """正文没改过 → 没有要重建的东西、也没有守卫 → 这两步**不适用**，
    不是「待做」（说成待做会让人以为还有活没干完）。"""
    db, p = fixture()
    _with_body(db, p)
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['derived_rebuild']['state'] == 'skipped'
    assert by_id['unlock']['state'] == 'skipped'


def test_rebuild_is_pending_when_the_derived_data_is_stale():
    """派生数据待重建 → 重建这步待做；**解锁那步不适用**（没有守卫要解）。

    这两件事是分开的：待重建说明派生数据旧了，待解锁说明生成被挡着。
    """
    db, p = fixture()
    _with_body(db, p)
    timeline = dict(p.plot_timeline or {})
    timeline['derived_state'] = {'kg': 'pending_rebuild', 'facts': 'pending_rebuild'}
    p.plot_timeline = timeline
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['derived_rebuild']['state'] == 'pending'
    assert by_id['unlock']['state'] == 'skipped'


def test_unlock_is_pending_while_the_guard_is_still_on():
    db, p = fixture()
    _with_body(db, p)
    timeline = dict(p.plot_timeline or {})
    timeline['body_revision_requires_review'] = {'generation_id': 1}
    timeline['derived_state'] = {'kg': 'rebuilt', 'facts': 'rebuilt'}
    p.plot_timeline = timeline
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['unlock']['state'] == 'pending'


def test_unlock_is_done_after_the_guard_is_cleared():
    db, p = fixture()
    _with_body(db, p)
    timeline = dict(p.plot_timeline or {})
    timeline['body_revision_requires_review'] = {'generation_id': 1}
    timeline['derived_state'] = {'kg': 'rebuilt', 'facts': 'rebuilt',
                                 'unlocked_at': '2026-10-24T00:00:00Z'}
    p.plot_timeline = timeline
    db.commit()
    state = evaluate(db, p)
    by_id = {s['id']: s for s in state['steps']}
    assert by_id['unlock']['state'] == 'done'
