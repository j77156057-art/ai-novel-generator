import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { distinctChapterTitles } from "../src/utils/corpusReport.ts";

/**
 * 「混进了别的书」在界面上的纪律。
 *
 * ## 为什么要有这组测试
 *
 * 这个功能牵着一个**不可逆的决定**：剔除 = 删掉整章正文。判据万一错了，
 * 丢的是用户真内容。所以界面上必须守住三条：
 *
 *   1. 检出结论**要给人看**（不是只写进数据库）；
 *   2. 剔除默认**不勾** —— 要删得人主动点；
 *   3. 勾选框**只在检出时出现**，没检出时不该有一段"你要不要剔除"的噪音。
 *
 * 这些是「走一步才触发」的类型：单测绿、类型检查绿、构建绿，
 * 但少渲染一个字段 / 把默认值写成 true，界面就静默变成"默默删了用户的书"。
 */
const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");
const assistant = readFileSync(join(SRC, "views", "AssistantWorkspace.vue"), "utf-8");
const corpus = readFileSync(join(SRC, "views", "CorpusView.vue"), "utf-8");
const types = readFileSync(join(SRC, "api", "types.ts"), "utf-8");
const notice = readFileSync(join(SRC, "components", "ForeignBlocksNotice.vue"), "utf-8");

test("投放卡上要有混书结论与剔除勾选框", () => {
  assert.match(assistant, /foreignBlocksOf/, "投放卡没有读混书结论");
  assert.match(assistant, /ForeignBlocksNotice/, "投放卡没有用混书结论组件");
  assert.match(assistant, /selectable/, "投放卡没给剔除勾选框");
  assert.match(assistant, /drop_foreign_blocks/, "勾选结果没有传进请求体");
});

test("剔除默认不勾：勾选状态初始化为 false", () => {
  assert.match(
    assistant,
    /dropForeignBlocks:\s*false/,
    "剔除开关的初始值不是 false —— 默认剔会把用户的正文悄悄删掉",
  );
});

test("勾选框只在检出时出现", () => {
  // 渲染条件必须是"检出了才显示"，而不是"总是显示一个空壳"。
  assert.match(
    assistant,
    /v-if="foreignBlocksOf\(card\)"/,
    "混书块没有按检出条件渲染（没检出时也会出现剔除开关）",
  );
});

test("勾了才改请求体：不勾时保持服务端给的 false", () => {
  // 覆写必须以「用户勾了」为前提，否则前端会替用户做决定。
  assert.match(
    assistant,
    /foreignBlocksOf\(card\)\s*&&\s*card\.dropForeignBlocks/,
    "请求体的剔除位没有以用户的勾选为前提",
  );
});

test("语料页要显示投放后的体检结论", () => {
  assert.match(corpus, /foreignOf/, "语料页没有读体检结论");
  assert.match(corpus, /入库体检/, "语料页没有体检结论区块");
  assert.match(corpus, /ForeignBlocksNotice/, "语料页没有用混书结论组件");
});

test("前端类型与后端契约对齐：NovelOut 带 clean_report", () => {
  assert.match(types, /interface NovelOut/, "NovelOut 类型没了");
  assert.match(types, /clean_report\?:\s*CorpusCleanReport/, "NovelOut 缺 clean_report 字段");
  assert.match(types, /interface ForeignBlocksReport/, "缺 ForeignBlocksReport 类型");
  assert.match(types, /interface CorpusCleanReport/, "缺 CorpusCleanReport 类型");
});

test("章标题去重：盗版站把同一章标题贴两遍，界面上不重复摆", () => {
  // 真数据：十日终焉的体检报告里，第1047章的标题连着出现两次。
  const raw = ["第1047章 符合路子", "第1047章 符合路子", "第1048章 我怎么知道?", "第1048章 我怎么知道?"];
  assert.deepEqual(distinctChapterTitles(raw), ["第1047章 符合路子", "第1048章 我怎么知道?"]);
  // 顺序不变（去重保首次出现的位置），空输入不炸。
  assert.deepEqual(distinctChapterTitles([]), []);
  assert.deepEqual(distinctChapterTitles(undefined), []);
  // 两个界面共用同一个组件 ⇒ 去重逻辑只有一份，不会各写各的。
  assert.match(notice, /distinctChapterTitles/, "共用组件没做标题去重");
});
