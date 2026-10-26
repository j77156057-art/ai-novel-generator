"""多段接力 + 规划覆盖率门禁：同一节点跨多段时不能各写各的，
验收也不能对「正文与规划冲突」视而不见。

背景（真实跑出来的）：
  - 一个节点允许三段预算时，三段拿到的都是**节点入口状态**，
    于是第三段重演了一遍「进门」，还断言「登记页仍然是空白的」——
    与第二段已经写下登记的事实冲突（judge 判 n1 的退出条件 contradicted）。
  - 整段验收只核对「正文与权威是否冲突」，不核对「规划里的节点是不是
    都写到了、达成了」→ 一篇正文三段全耗在第一个节点、后面两个节点从未产生，
    验收依然判 accepted。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from test_causal_timeline import fixture, Reply          # noqa: E402
from test_body_acceptance import _acceptance_fixture, _good_answer  # noqa: E402

from app.generation import engine                        # noqa: E402
from app.generation.timeline import build_node_prompt, plan_timeline  # noqa: E402


# —— 多段接力：提示层面 ——

def test_first_segment_has_no_continuation():
    st = plan_timeline(3)
    node = st.nodes['n1']
    node.title = '确认权限'
    node.entry_condition = '两人在值班室，读数未核'
    node.exit_condition = '权限边界已口头确认'
    prompt = build_node_prompt(node, 0, {'main_progress': 0.0})
    assert '接着上一段继续' not in prompt
    assert '不要回到这个状态重演开场' not in prompt


def test_second_segment_carries_previous_segment_state():
    st = plan_timeline(3)
    node = st.nodes['n1']
    node.title = '确认权限'
    node.entry_condition = '两人在值班室，读数未核'
    node.exit_condition = '权限边界已口头确认'
    prompt = build_node_prompt(
        node, 0, {'main_progress': 0.0},
        continuation={'segment_index': 2, 'segment_budget': 3,
                      'prev_state': '上一段摘要：登记页仍然空白\n上一段结尾：…她把铅笔放在登记簿旁。'})
    assert '接着上一段继续' in prompt
    assert '上一段结尾：…她把铅笔放在登记簿旁。' in prompt
    assert '不要回到这个状态重演开场' in prompt
    assert '本节点已经写了 1 段' in prompt
    assert '这是第 2 段' in prompt


# —— 多段接力：契约层面（重生成走的就是冻结的那份）——

def test_second_segment_contract_freezes_continuation():
    from app.models import Generation
    db, p = fixture()
    db.add(Generation(project_id=p.id, segment_no=1,
                      text='她把铅笔放在登记簿旁，登记页仍然是空白的。'))
    db.commit()
    contradicted = {'verdict': 'contradicted', 'reason': '权限还没确认完',
                    'sources': [{'source_id': 'generation:1',
                                 'quote': '她把铅笔放在登记簿旁', 'start': 0, 'end': 10}]}
    engine._judge_and_advance(db, p, Reply(contradicted), '她把铅笔放在登记簿旁。', 0)
    engine._judge_and_advance(db, p, Reply(contradicted), '她继续确认。', 1)
    contracts = p.plot_timeline['planning']['segment_contracts']
    prompt = contracts['2']['prompt']
    assert '接着上一段继续' in prompt, '第二段的契约必须带接力上下文'
    assert '她把铅笔放在登记簿旁' in prompt, '契约必须带上一段真实的收尾现场'


def test_exhausted_node_without_evidence_is_recorded_not_hidden():
    """预算用尽且退出条件未证实 → 记进 node_needs_review（不静默消失）。"""
    db, p = fixture()
    contradicted = {'verdict': 'contradicted', 'reason': '权限还没确认完',
                    'sources': [{'source_id': 'generation:1',
                                 'quote': '她把铅笔放在登记簿旁', 'start': 0, 'end': 10}]}
    engine._judge_and_advance(db, p, Reply(contradicted), '她把铅笔放在登记簿旁。', 0)
    assert p.plot_timeline['planning']['node_needs_review'] == ['n1']
    assert p.plot_timeline['done'] == []


# —— 规划用尽：按**计划里的节点全集**判断 ——

def test_plan_exhausted_looks_at_planned_nodes_not_only_bound_contracts():
    """规划里还有没写的节点 → 不能算「规划写完」（否则后面的节点永远写不到）。"""
    db, p = fixture()
    timeline = dict(p.plot_timeline or {})
    planning = dict(timeline.get('planning') or {})
    planning['nodes'] = [{'id': 'n1'}, {'id': 'n2'}, {'id': 'n3'}]
    planning['segment_contracts'] = {'1': {'node_id': 'n1', 'prompt': 'x'}}
    timeline['planning'] = planning
    timeline['done'] = ['n1']
    p.plot_timeline = timeline
    db.commit()
    assert engine._plan_exhausted(p) is False


# —— 规划覆盖率门禁 ——

def test_planned_node_gaps_separates_conflict_from_unwritten():
    from app.agents.body_acceptance import planned_node_gaps
    timeline = {
        'nodes': {'n1': {'kind': 'main', 'title': '确认权限'},
                  'n2': {'kind': 'main', 'title': '核对读数'},
                  'n3': {'kind': 'main', 'title': '复核'}},
        'seg_counts': {'n1': 3, 'n2': 1},
        'done': ['n1'],
        'completion': {'n1': {'verdict': 'contradicted', 'reason': '正文写了登记页'},
                       'n2': {'verdict': 'unknown', 'reason': '判不出'}},
    }
    blocking, visible = planned_node_gaps(timeline)
    assert [g['node_id'] for g in blocking] == ['n1'], '正文反驳节点退出条件 = 阻塞项'
    visible_ids = [g['node_id'] for g in visible]
    assert 'n2' in visible_ids, '判不出 → 开放项，不阻塞'
    assert any('n3' in gid for gid in visible_ids), '没写到 → 开放项，不阻塞'


def test_node_exit_conflict_blocks_the_acceptance():
    """正文与规划冲突时，即使四维全 supported 也不许放行。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    timeline = dict(p.plot_timeline or {})
    timeline['seg_counts'] = {'n1': 3}
    timeline['completion'] = {'n1': {'verdict': 'contradicted',
                                     'reason': '正文把登记页写了，节点退出条件被反驳'}}
    p.plot_timeline = timeline
    db.commit()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert result['status'] == 'blocked'
    assert 'node_exit_conflict:n1' in result['unresolved']
    assert result['plan_coverage']['blocking'][0]['node_id'] == 'n1'
    db.refresh(p)
    assert p.plot_timeline['pending_acceptance']['state'] == 'carried_forward'


def test_unwritten_nodes_are_visible_but_do_not_block():
    """规划里有没写的节点 → 可见（开放项），但**不阻塞**本版验收。"""
    from app.agents.body_acceptance import run_body_acceptance
    db, p, g, fingerprint = _acceptance_fixture()
    provider = Reply(_good_answer())
    result = run_body_acceptance(db, p.id, base_content_hash=fingerprint, provider=provider)
    assert result['status'] == 'accepted'
    visible = result['plan_coverage']['visible']
    assert visible, '没写到的节点要如实列出来'
    assert 'n2' in json.dumps(visible, ensure_ascii=False)
    assert not result['plan_coverage']['blocking']
