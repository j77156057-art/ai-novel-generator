/**
 * 书维度状态条与角色章号的回归测试。
 *
 * 核心断言：
 * 1. 「章号未知」与「区间漂移」必须分别成句，不能合成「部分数据异常」这种
 *    模糊表述，也不能互相覆盖；
 * 2. 漂移 > 0 时不能被静默成 0，也不能被当成「已修复」；
 * 3. 角色章号空值一律显示「章号未知」，任何路径不回落成第 1 章。
 *
 * 跑法：cd frontend && npm run test:kg-chapter
 */
import test from "node:test";
import assert from "node:assert/strict";

import { buildBookScopeStatus, chapterTag, CHAPTER_UNKNOWN } from "../src/utils/kgChapter.js";

//: 一旦这些字样出现，用户就会把两个不同的问题混成一个。
const VAGUE_WORDS = ["部分数据异常", "数据异常", "部分异常"];

function assertNoVagueWording(text) {
  for (const word of VAGUE_WORDS) {
    assert.ok(!text.includes(word), `不得出现模糊表述「${word}」：${text}`);
  }
}

test("book-scope 未读取：说「未读取」，不冒充「没有异常」", () => {
  const view = buildBookScopeStatus(null);
  assert.equal(view.tone, "idle");
  assert.ok(view.detail.includes("不等于"), view.detail);
  assert.equal(view.unknown, 0);
  assert.equal(view.drift, 0);
});

test("章号未知与区间漂移分开成句，各自带数", () => {
  const view = buildBookScopeStatus({
    book: { resolved: 7 },
    entities: {
      chapter_unknown: 29,
      chapter_range_drift: 3711,
      other_books_excluded: 12039,
    },
  });
  assert.equal(view.tone, "warn");
  assert.equal(view.unknownText, "章号未知 29");
  assert.equal(view.driftText, "区间漂移 3711");
  assert.notEqual(view.unknownText, view.driftText);
  assert.ok(view.excludedText.includes("12039"), view.excludedText);
  assert.equal(view.bookLabel, "当前书 #7");
  assertNoVagueWording(view.unknownText);
  assertNoVagueWording(view.driftText);
  assertNoVagueWording(view.detail);
  assert.ok(view.detail.includes("不覆盖"), view.detail);
});

test("漂移为 0 时也明确写 0，不能被省略成「无异常」", () => {
  const view = buildBookScopeStatus({
    book: { resolved: 7 },
    entities: { chapter_unknown: 0, chapter_range_drift: 0, other_books_excluded: 0 },
  });
  assert.equal(view.tone, "ok");
  assert.equal(view.unknownText, "章号未知 0");
  assert.equal(view.driftText, "区间漂移 0");
  assert.equal(view.excludedText, "");
});

test("未指定本书：状态条说明只读项目级", () => {
  const view = buildBookScopeStatus({
    book: { resolved: null },
    entities: { chapter_unknown: 29, chapter_range_drift: 0, other_books_excluded: 12039 },
  });
  assert.ok(view.bookLabel.includes("未指定本书"), view.bookLabel);
  assert.equal(view.tone, "warn");
});

test("角色章号：区间原样，空值一律「章号未知」且不回落第 1 章", () => {
  const range = chapterTag("第1–10章");
  assert.equal(range.text, "第1–10章");
  assert.equal(range.unknown, false);

  for (const empty of [null, undefined, "", "   "]) {
    const tag = chapterTag(empty);
    assert.equal(tag.text, CHAPTER_UNKNOWN);
    assert.equal(tag.unknown, true);
    assert.ok(!tag.text.includes("第1"), "空值不得回落成第 1 章");
  }

  const explicit = chapterTag(CHAPTER_UNKNOWN);
  assert.equal(explicit.unknown, true);
});
