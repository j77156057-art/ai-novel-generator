import { test } from "node:test";
import assert from "node:assert/strict";
import { issueTypeLabel } from "../src/utils/assistantDisplay.ts";

/**
 * 诊断结论的**类型标签**：界面不出现内部术语。
 *
 * `plothole` / `character_state` / `style` 这种 key 只对写代码的人有意义；
 * 单独摆在界面上用户读不出来。原始值仍然并列显示（界面上是「文风 · style」），
 * 排查时要能对上后端的值。
 */
test("已知类型给人话说法", () => {
  assert.equal(issueTypeLabel("plothole"), "伏笔");
  assert.equal(issueTypeLabel("fact_quality"), "事实质量");
  assert.equal(issueTypeLabel("identity"), "人物身份");
  assert.equal(issueTypeLabel("character_state"), "人物状态");
  assert.equal(issueTypeLabel("style"), "文风");
});

test("未知类型不空着，也不把内部 key 当人话", () => {
  assert.equal(issueTypeLabel("some_new_kind"), "其它");
  assert.equal(issueTypeLabel(""), "其它");
});
