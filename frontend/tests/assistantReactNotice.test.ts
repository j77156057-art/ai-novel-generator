import { test } from "node:test";
import assert from "node:assert/strict";
import { reactNotice } from "../src/utils/assistantDisplay.ts";

/**
 * 本轮降级标记：三类含义**不能共用文案**。
 *
 * 把「答案无据」说成「模型坏了」，会把用户引到错的地方（去查 key、配额），
 * 而真正要做的是「别信这几句话」—— 这条是这个模块存在的全部理由。
 */
test("没有降级就不显示任何标记", () => {
  assert.equal(reactNotice(null), null);
  assert.equal(reactNotice(undefined), null);
  assert.equal(reactNotice({ degraded: false } as never), null);
  assert.equal(reactNotice({ mode: "react", model_participated: true } as never), null);
});

test("答案无据 → 「本轮未经核对」，不是「模型坏了」", () => {
  const n = reactNotice({
    degraded: true,
    degraded_reason: "unsupported_claims:write_done,consistency",
    unsupported_claims: [{ kind: "write_done", phrase: "已经写入正文了" }],
  } as never);
  assert.ok(n);
  assert.equal(n.label, "本轮未经核对");
  assert.equal(n.tag, "err");
  assert.ok(n.detail.includes("已经写入正文了"), "要指出是哪几句话没据");
  assert.ok(!n.label.includes("模型"), "标题不能把内容问题说成机器问题");
  assert.ok(!n.label.includes("未完成"), "标题不能与模型故障共用");
});

test("没收敛 → 「本轮未能收敛」", () => {
  const n = reactNotice({
    degraded: true,
    degraded_reason: "stagnated:get_workflow_state",
  } as never);
  assert.ok(n);
  assert.equal(n.label, "本轮未能收敛");
  assert.equal(n.tag, "warn");
  assert.ok(!n.label.includes("未经核对"));
});

test("模型侧故障 → 「本轮未完成」，并剥掉机器前缀", () => {
  const n = reactNotice({
    degraded: true,
    degraded_reason: "provider_stub：assistant provider 是本地兜底桩（未配置可用模型）",
  } as never);
  assert.ok(n);
  assert.equal(n.label, "本轮未完成");
  assert.equal(n.tag, "warn");
  assert.ok(!n.detail.includes("provider_stub"), "界面不出现内部术语");
  assert.ok(n.detail.includes("本地兜底桩"));
});

test("超时与模型报错都归到「本轮未完成」", () => {
  for (const reason of [
    "ReAct 墙钟超时（timeout_s=600）",
    "模型调用失败",
    "模型多轮输出无法解析为决策",
  ]) {
    const n = reactNotice({ degraded: true, degraded_reason: reason } as never);
    assert.ok(n, reason);
    assert.equal(n.label, "本轮未完成", reason);
  }
});

test("降级但没给原因 → 也要有话说，不能空白", () => {
  const n = reactNotice({ degraded: true, degraded_reason: "" } as never);
  assert.ok(n);
  assert.equal(n.label, "本轮未完成");
  assert.ok(n.detail.length > 0);
});
