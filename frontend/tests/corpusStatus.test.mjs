/**
 * 语料摄取状态条文案的回归测试。
 *
 * 核心断言：failed > 0 时绝不能出现「无数据 / 无实体 / 还没有」这类空态文案；
 * 窗口表为空时必须说「尚未摄取」；本地模板兜底导致的解析失败要标注
 * 「疑似渠道不可用」。
 *
 * 跑法：cd frontend && npm run test:corpus-status
 */
import test from "node:test";
import assert from "node:assert/strict";

import { buildCorpusStatus } from "../src/utils/corpusStatus.js";

//: 一旦这些字样出现在失败态文案里，用户就会把「摄取失败」误读成「没有数据」。
const EMPTY_DATA_WORDS = ["无数据", "无实体", "没有实体", "窗口内无实体", "还没有"];

function assertNoEmptyDataWording(text) {
  for (const word of EMPTY_DATA_WORDS) {
    assert.ok(!text.includes(word), `失败/未摄取态不得出现空数据文案「${word}」：${text}`);
  }
}

test("窗口表为空：说「尚未摄取」，不是「无实体」", () => {
  const view = buildCorpusStatus({
    ingested: false,
    total: 0,
    failed: 0,
    pending: 0,
    running: 0,
    success: 0,
  });
  assert.equal(view.tone, "idle");
  assert.ok(view.text.includes("尚未摄取"), view.text);
  assertNoEmptyDataWording(view.text);
  assert.equal(view.failureCount, 0);
});

test("failed>0（解析失败 + 离线兜底）：必须报失败且标注疑似渠道不可用", () => {
  const view = buildCorpusStatus({
    ingested: true,
    total: 4,
    failed: 4,
    pending: 0,
    running: 0,
    success: 0,
    failed_reasons: { parse_failed: 4, channel_unavailable: 0, other: 0 },
    suspected_local_fallback: 4,
  });
  assert.equal(view.tone, "error");
  assert.ok(view.text.includes("4 个窗口失败"), view.text);
  assert.ok(view.text.includes("解析失败"), view.text);
  assert.ok(view.text.includes("疑似渠道不可用"), view.text);
  assertNoEmptyDataWording(view.text);
  assert.equal(view.failureCount, 4);
});

test("failed>0（明确的渠道不可用 / 其他错误）也保持失败态", () => {
  const channel = buildCorpusStatus({
    ingested: true,
    total: 2,
    failed: 2,
    pending: 0,
    running: 0,
    success: 0,
    failed_reasons: { parse_failed: 0, channel_unavailable: 1, other: 1 },
    suspected_local_fallback: 0,
  });
  assert.equal(channel.tone, "error");
  assert.ok(channel.text.includes("渠道不可用"), channel.text);
  assert.ok(channel.text.includes("其他错误"), channel.text);
  assertNoEmptyDataWording(channel.text);

  const unknown = buildCorpusStatus({
    ingested: true,
    total: 1,
    failed: 1,
    pending: 0,
    running: 0,
    success: 0,
    failed_reasons: { parse_failed: 0, channel_unavailable: 0, other: 0 },
  });
  assert.equal(unknown.tone, "error");
  assert.ok(unknown.text.includes("1 个窗口失败"), unknown.text);
  assertNoEmptyDataWording(unknown.text);
});

test("部分失败也算失败态，不能因为 success>0 就把失败藏起来", () => {
  const view = buildCorpusStatus({
    ingested: true,
    total: 6,
    failed: 2,
    pending: 0,
    running: 0,
    success: 4,
    failed_reasons: { parse_failed: 2, channel_unavailable: 0, other: 0 },
    suspected_local_fallback: 0,
  });
  assert.equal(view.tone, "error");
  assert.ok(view.text.includes("2 个窗口失败"), view.text);
  assertNoEmptyDataWording(view.text);
});

test("没有失败时的正常态：完成 / 运行中 / 待跑都各有明确文案", () => {
  const done = buildCorpusStatus({
    ingested: true, total: 10, failed: 0, pending: 0, running: 0, success: 10,
  });
  assert.equal(done.tone, "ok");
  assert.ok(done.text.includes("10/10 个窗口已完成"), done.text);

  const running = buildCorpusStatus({
    ingested: true, total: 10, failed: 0, pending: 7, running: 3, success: 0,
  });
  assert.equal(running.tone, "warn");
  assert.ok(running.text.includes("3 个窗口摄取中"), running.text);

  const pending = buildCorpusStatus({
    ingested: true, total: 10, failed: 0, pending: 10, running: 0, success: 0,
  });
  assert.equal(pending.tone, "warn");
  assert.ok(pending.text.includes("尚无窗口完成"), pending.text);
  assertNoEmptyDataWording(pending.text);
});

test("summary 为空时按未摄取处理，不崩", () => {
  const view = buildCorpusStatus(null);
  assert.equal(view.tone, "idle");
  assert.ok(view.text.includes("尚未摄取"), view.text);
});
