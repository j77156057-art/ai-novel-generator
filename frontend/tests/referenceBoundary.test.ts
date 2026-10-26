import { test } from "node:test";
import assert from "node:assert/strict";
import {
  boundaryFromProject,
  boundaryLabel,
  buildBoundary,
} from "../src/utils/referenceBoundary.ts";

/**
 * 参考书的原文边界：界面这一半的纯逻辑。
 *
 * 关键不变量：
 * - 「只学写法」必须**清掉**边界（显式边界永远优先于「写法参考路径整本可见」，
 *   留着旧边界会悄悄限制可用素材）；
 * - 「接着这本书写」必须**逐本**给出章号，缺一个就不许提交；
 * - 错误提示要说清是哪本书、哪个数（提交前拦住，别让用户回头找输入框）。
 */
const titles: Record<number, string> = { 1: "参考甲", 2: "参考乙" };
const titleOf = (id: number) => titles[id] ?? `#${id}`;

test("只学写法：清掉边界，回到自己的故事", () => {
  const draft = buildBoundary("style", [1], { 1: 100 }, { 1: 990 }, titleOf);
  assert.equal(draft.story_authority, "generated_branch");
  assert.equal(draft.reference_cutoffs, null);
  assert.equal(draft.error, null);
});

test("接着写：逐本给出边界", () => {
  const draft = buildBoundary("continue", [1, 2], { 1: 100, 2: 7 }, { 1: 990, 2: 8 }, titleOf);
  assert.equal(draft.story_authority, "reference_anchor");
  assert.equal(draft.error, null);
  assert.deepEqual(draft.reference_cutoffs, [
    { novel_id: 1, chapter: 100 },
    { novel_id: 2, chapter: 7 },
  ]);
});

test("接着写但没选语料：不让提交", () => {
  const draft = buildBoundary("continue", [], {}, {}, titleOf);
  assert.match(draft.error ?? "", /至少一本参考书/);
  assert.equal(draft.reference_cutoffs, null);
});

test("缺一本的边界：说清是哪本书", () => {
  const draft = buildBoundary("continue", [1, 2], { 1: 100 }, { 1: 990, 2: 8 }, titleOf);
  assert.match(draft.error ?? "", /参考乙/);
  assert.match(draft.error ?? "", /故事进行到第几章/);
});

test("章号超过全书章数：报错并给出上界", () => {
  const draft = buildBoundary("continue", [1], { 1: 1000 }, { 1: 990 }, titleOf);
  assert.match(draft.error ?? "", /第 990 章/);
});

test("这本书还没有章节：不能设边界", () => {
  const draft = buildBoundary("continue", [1], { 1: 5 }, { 1: 0 }, titleOf);
  assert.match(draft.error ?? "", /还没有切分出章节/);
});

test("章号必须是正整数", () => {
  assert.match(buildBoundary("continue", [1], { 1: 0 }, { 1: 990 }, titleOf).error ?? "", /正整数/);
  assert.match(buildBoundary("continue", [1], { 1: 3.5 }, { 1: 990 }, titleOf).error ?? "", /正整数/);
  assert.match(buildBoundary("continue", [1], { 1: -2 }, { 1: 990 }, titleOf).error ?? "", /正整数/);
});

test("从项目回显：普通项目是只学写法", () => {
  const state = boundaryFromProject({ story_authority: "generated_branch", reference_cutoffs: null });
  assert.equal(state.usage, "style");
  assert.deepEqual(state.inputs, {});
});

test("从项目回显：锚定项目带边界", () => {
  const state = boundaryFromProject({
    story_authority: "reference_anchor",
    reference_cutoffs: [{ novel_id: 1, chapter: 100 }],
  });
  assert.equal(state.usage, "continue");
  assert.equal(state.inputs[1], 100);
});

test("从项目回显：锚定了却没边界，如实显示成「接着写」", () => {
  const state = boundaryFromProject({ story_authority: "reference_anchor", reference_cutoffs: null });
  assert.equal(state.usage, "continue");
  assert.deepEqual(state.inputs, {});
});

test("边界状态一句话", () => {
  assert.equal(boundaryLabel("style", {}), "只学写法（全书可作写法材料）");
  assert.equal(
    boundaryLabel("continue", { 1: 100 }, titleOf),
    "接着写 · 《参考甲》第 100 章",
  );
  assert.equal(boundaryLabel("continue", {}, titleOf), "接着写（边界未填）");
});
