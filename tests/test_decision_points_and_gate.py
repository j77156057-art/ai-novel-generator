"""选择框（决策点）、生成闸门、关闭直传文本 —— 三件事的测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import pytest  # noqa: E402
from test_causal_timeline import fixture  # noqa: E402


@pytest.fixture
def api_db():
    """API 级测试用的库：内存 sqlite + 跨线程（TestClient 在别的线程跑请求）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Project
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(engine)
    project = Project(title='选择框测试', target_length=9000, mode='quick',
                      generation_length=3000)
    db.add(project)
    db.commit()
    yield db, project
    db.close()


@pytest.fixture
def client(api_db):
    """API 级测试的客户端：请求打到测试自己的内存库（用完必须清掉覆写）。"""
    from fastapi.testclient import TestClient
    from app.db import get_db
    from app.main import app as fastapi_app
    db, _project = api_db
    fastapi_app.dependency_overrides[get_db] = lambda: db
    try:
        yield TestClient(fastapi_app)
    finally:
        fastapi_app.dependency_overrides.clear()


def _seed_corpus(db, p):
    """给项目塞一本语料（否则蒸馏那几步是「不适用」，链落不到决策点）。"""
    from app.models import NovelChunk, RawNovel
    novel = RawNovel(title='测试语料', status='imported')
    db.add(novel)
    db.flush()
    db.add(NovelChunk(novel_id=novel.id, chapter_no=1, idx=1, text='这一段有句号。'))
    p.reference_novel_ids = [novel.id]
    db.commit()


def _add_pending_proposal(db, p):
    from app.models import AgentRun, EditProposal
    run = AgentRun(project_id=p.id, role='assistant', status='success', request_hash='t')
    db.add(run)
    db.flush()
    db.add(EditProposal(run_id=run.id, project_id=p.id, type='manual',
                        target_kind='generations', target_table='generations',
                        patch_json={}, risk_level='low', status='pending'))
    db.commit()


# —— 决策点注册表 ——

def test_decision_points_have_options_and_server_side_endpoints():
    from app.agents.decision_points import DECISION_POINTS
    assert set(DECISION_POINTS) >= {'distill_tier', 'segment_length', 'chapters_per_run'}
    for key, point in DECISION_POINTS.items():
        assert point.options, f'{key} 没有选项'
        assert point.endpoint, f'{key} 没有端点'
        for option in point.options:
            assert option.id and option.label
            assert option.body_patch != {}, f'{key}/{option.id} 没有对应的请求体补丁'


def test_choice_card_shape():
    from app.agents.decision_points import DISTILL_TIER, build_choice_action
    card = build_choice_action(DISTILL_TIER, project_id=7)
    assert card['kind'] == 'choice'
    assert card['title'] and card['detail']['question']
    assert len(card['detail']['options']) == 3
    assert card['endpoint'].startswith('/api/projects/7')
    assert card['method'] == 'POST'
    assert card['body']['project_id'] == 7
    patch = card['detail']['options'][0]['body_patch']
    assert 'tier' in patch


def test_choice_only_shows_when_the_user_asks_about_it():
    """不该每条消息都弹选择框 —— 只有问到那件事（或说「开始」）才给。"""
    from app.agents.decision_points import DISTILL_TIER, point_matches_query
    assert point_matches_query(DISTILL_TIER, '先蒸馏一下，用哪个档位好')
    assert point_matches_query(DISTILL_TIER, '开始')
    assert not point_matches_query(DISTILL_TIER, '第 3 段这里写得怎么样')


def test_workflow_state_says_what_needs_a_decision():
    """链走到蒸馏这一步 → 状态里带上「需要用户决断」。"""
    from app.agents.workflow import evaluate
    db, p = fixture()
    _seed_corpus(db, p)
    state = evaluate(db, p)
    assert state['current_step'] == 'distill'
    assert state['needs_decision']
    assert state['needs_decision']['key'] == 'distill_tier'


def test_workflow_decision_card_respects_the_query():
    from app.agents.workflow import workflow_decision_card
    db, p = fixture()
    _seed_corpus(db, p)
    assert workflow_decision_card(db, p, query='先蒸馏，用哪档') is not None
    assert workflow_decision_card(db, p, query='这段写得怎么样') is None


# —— 生成闸门 ——

def test_generation_is_blocked_while_a_repair_is_pending():
    """有待批的修改建议 → 生成被拒（别绕过流程）。"""
    from app.agents.workflow import generation_block
    db, p = fixture()
    _add_pending_proposal(db, p)
    block = generation_block(db, p)
    assert block is not None
    assert block['status'] == 'workflow_pending_repair'


def test_free_generation_is_not_blocked():
    """没有参考语料、没有待批提案 → 不拦（闸门拦的是流程没走完，不是用户没准备）。"""
    from app.agents.workflow import generation_block
    db, p = fixture()
    assert generation_block(db, p) is None


def test_generation_is_blocked_without_an_outline():
    """没有剧情大纲 → 生成被拒：写出来的正文**过不了正文审查**。

    大纲是段契约的来源（没有大纲 → 没有时间线 → 没有冻结契约），
    没有契约，封闭清单审计就没有判据基准（`missing_saved_authority`、
    零候选、覆盖不完整），整段验收直接 `no_pending_acceptance`。
    实测：真书全流程第一次跑就死在这儿（audit 4 次尝试零模型调用）。
    """
    from app.agents.workflow import generation_block
    db, p = fixture()
    p.outline = None
    p.premise = '少年在渡口摆渡，雾晨船上多了一个不该在册的乘客。'
    db.commit()
    block = generation_block(db, p)
    assert block is not None
    assert block['status'] == 'workflow_needs_outline'
    assert '大纲' in block['message']
    # 有出路：助理能起草一份（动作提案），不是死路
    assert '助理' in block['message']


def test_generation_is_allowed_once_there_is_an_outline():
    from app.agents.workflow import generation_block
    db, p = fixture()
    p.premise = '少年在渡口摆渡。'
    db.commit()
    assert generation_block(db, p) is None


def test_generate_endpoint_refuses_when_the_chain_is_not_ready(api_db, client):
    """端点层：闸门生效时返回 409，并说清差在哪。"""
    db, p = api_db
    _add_pending_proposal(db, p)
    resp = client.post('/api/generate', json={'project_id': p.id})
    assert resp.status_code == 409, resp.text
    assert resp.json()['detail']['status'] == 'workflow_pending_repair'


# —— 直传文本入口已关闭 ——

def test_direct_text_upload_is_closed_with_a_pointer_to_the_assistant(api_db, client):
    """直传文本绕过整条流程 → 明确报错并指向助理 agent（而不是静默 404）。"""
    resp = client.post('/api/novels/upload',
                        files={'file': ('a.txt', '正文内容。', 'text/plain')})
    assert resp.status_code == 409, resp.text
    detail = resp.json()['detail']
    assert detail['status'] == 'text_submission_moved_to_assistant'
    assert detail['assistant_endpoint'] == '/api/assistant/attachments'
