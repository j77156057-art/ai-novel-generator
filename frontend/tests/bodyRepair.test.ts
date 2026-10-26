import test from 'node:test';
import assert from 'node:assert/strict';
import { pendingActionRiskMeta, pendingActionKindLabel, bodyRepairVerificationText } from '../src/utils/assistantDisplay.ts';
test('body repair confirmation describes proposal and audit writes, not body application', () => {
  const meta = pendingActionRiskMeta({kind:'body_repair',detail:{},body:{},title:'修订',endpoint:'/api/assistant/body-repairs',method:'POST'});
  assert.match(meta.label,/候选/);
  assert.match(meta.hint,/不应用正文/);
  assert.equal(pendingActionKindLabel('body_repair'),'局部修订候选');
});
test('local-only acceptance never displays whole-segment approval', () => {
  const text=bodyRepairVerificationText({verification_acceptance:{local_repair_verification:'accepted',uncovered:['missing_saved_contract']}});
  assert.match(text,/局部修复已验收/);
  assert.match(text,/整段计划一致性未知/);
  assert.match(text,/缺少原始契约/);
  assert.match(text,/正文未应用/);
  assert.doesNotMatch(bodyRepairVerificationText({}),/局部修复已验收/);
});
