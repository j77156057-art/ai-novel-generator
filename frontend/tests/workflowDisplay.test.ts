import test from 'node:test';
import assert from 'node:assert/strict';
import {
  actionKindLabel,
  actionProposalState,
  actionRiskLabel,
  pendingProposalTone,
  stepStateLabel,
  stepStateTone,
  workflowTracks,
} from '../src/utils/workflowDisplay.ts';
import type { ActionProposalOut, WorkflowPendingProposal, WorkflowStep } from '../src/api/types.ts';

/** 造一道工序（只填面板用得到的字段）。 */
function step(id: string, track: string, state: WorkflowStep['state'], label?: string): WorkflowStep {
  return {
    id,
    title: id,
    state,
    state_label: label,
    reason: '',
    read_tools: [],
    write_proposal: null,
    track,
    dispatch: '',
    note: '',
  };
}

test('状态词优先用服务端给的，前端只在缺失时兜底', () => {
  assert.equal(stepStateLabel(step('a', 'prompt', 'pending', '下一步')), '下一步');
  assert.equal(stepStateLabel(step('a', 'prompt', 'done')), '已完成');
  assert.equal(stepStateLabel(step('a', 'prompt', 'blocked')), '卡住');
  assert.equal(stepStateLabel(step('a', 'prompt', 'skipped')), '不适用');
});

test('「没有待批项」不会被前端改写成「已完成」', () => {
  // 服务端给的是人话状态词，前端不得用状态映射覆盖它
  assert.equal(stepStateLabel(step('repair', 'body', 'done', '没有待批项')), '没有待批项');
});

test('样式 class 与状态对应', () => {
  assert.equal(stepStateTone(step('a', 'prompt', 'done')), 'wf-done');
  assert.equal(stepStateTone(step('a', 'prompt', 'blocked')), 'wf-blocked');
  assert.equal(stepStateTone(step('a', 'prompt', 'pending')), 'wf-pending');
  assert.equal(stepStateTone(step('a', 'prompt', 'skipped')), 'wf-skip');
});

test('工序按两条轨分组，且当前步被标出来', () => {
  const steps = [
    step('corpus_ready', 'prompt', 'done'),
    step('distill', 'prompt', 'pending'),
    step('write_body', 'body', 'pending'),
    step('review_body', 'body', 'pending'),
  ];
  const tracks = workflowTracks(steps, 'distill');
  assert.equal(tracks.length, 2);
  assert.equal(tracks[0].id, 'prompt');
  assert.equal(tracks[0].steps.length, 2);
  assert.equal(tracks[1].id, 'body');
  assert.equal(tracks[1].steps.length, 2);
  // 当前步只有一处
  const current = tracks.flatMap((t) => t.steps).filter((s) => s.current);
  assert.equal(current.length, 1);
  assert.equal(current[0].step.id, 'distill');
  assert.equal(current[0].label, '待做');
});

test('空工序不产生空轨', () => {
  assert.equal(workflowTracks([], null).length, 0);
  assert.equal(workflowTracks([step('a', 'prompt', 'done')], null).length, 1);
});

test('未登记的轨不丢工序', () => {
  const tracks = workflowTracks([step('x', 'future', 'pending')], null);
  assert.equal(tracks.length, 1);
  assert.equal(tracks[0].steps.length, 1);
});

test('改动大小给色标，文字已经写明大小', () => {
  const base: WorkflowPendingProposal = {
    id: 1,
    target: '正文第 3 段',
    reason: '',
    risk: '改动较大',
    risk_level: 'high',
    status: 'pending',
    created_at: null,
  };
  assert.equal(pendingProposalTone(base), 'err');
  assert.equal(pendingProposalTone({ ...base, risk_level: 'medium' }), 'warn');
  assert.equal(pendingProposalTone({ ...base, risk_level: 'low' }), '');
});

function action(status: ActionProposalOut['status']): ActionProposalOut {
  return {
    id: 1,
    project_id: 1,
    run_id: null,
    kind: 'plan_outline',
    title: '写一份剧情大纲',
    detail: {},
    endpoint: '/api/assistant/action-proposals',
    method: 'POST',
    body: {},
    precondition_fingerprint: 'abc',
    precondition_note: '项目还没有剧情大纲',
    risk_level: 'medium',
    status,
    created_at: null,
    approved_at: null,
    applied_at: null,
    applied_result: null,
  };
}

test('动作提案的种类与风险都有人话说法', () => {
  assert.equal(actionKindLabel('plan_outline'), '规划剧情大纲');
  assert.equal(actionKindLabel('start_distill'), '启动文风蒸馏');
  assert.equal(actionKindLabel('invoke_generation'), '写正文');
  assert.equal(actionRiskLabel('high'), '改动大');
  assert.equal(actionRiskLabel('low'), '改动小');
});

test('动作提案的状态决定该点什么按钮', () => {
  assert.deepEqual(actionProposalState(action('pending')), { label: '等你批', action: 'approve' });
  assert.deepEqual(actionProposalState(action('approved')), { label: '等你执行', action: 'apply' });
  assert.equal(actionProposalState(action('applied')).action, 'none');
  assert.equal(actionProposalState(action('stale')).action, 'none');
  assert.match(actionProposalState(action('stale')).label, /前提变了/);
});
