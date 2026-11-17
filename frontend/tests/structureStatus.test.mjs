/**
 * 结构检查回执文案的回归测试。
 *
 * 核心断言：跳过项的正文必须来自后端 `reason.message`，不能退化成一个
 * 笼统的「跳过」，也不能由前端写死「段模式不跑」。
 *
 * 跑法：cd frontend && npm run test:structure-status
 */
import test from "node:test";
import assert from "node:assert/strict";

import { buildStructureNotices } from "../src/utils/structureStatus.js";

test("没有 structure_check：返回空数组，不编造状态", () => {
  assert.deepEqual(buildStructureNotices(null), []);
  assert.deepEqual(buildStructureNotices(undefined), []);
  // 对象存在但三项都缺：仍然要显示「无结果」，不能当成通过
  const empty = buildStructureNotices({});
  assert.equal(empty.length, 3);
  assert.ok(empty.every((row) => row.code === "missing_result"));
});

test("三类跳过原因原样使用后端 message，不写成静态文案", () => {
  const messages = {
    no_chapter_label: "本段没有章号（标签是「段N」），无法定位章纲条目；请先给本段分配章号。",
    chapter_beyond_outline: "第44章不在章纲范围内（共40条），请先补第44章章纲。",
    outline_entry_empty: "第7章章纲条目为空，请先填写本章章纲。",
  };
  const rows = buildStructureNotices({
    beat: { status: "skipped", reason: { code: "no_chapter_label", message: messages.no_chapter_label } },
    premature: { status: "skipped", reason: { code: "chapter_beyond_outline", message: messages.chapter_beyond_outline } },
    gender: { status: "skipped", reason: { code: "outline_entry_empty", message: messages.outline_entry_empty } },
  });
  assert.equal(rows.length, 3);
  assert.equal(rows[0].text, messages.no_chapter_label);
  assert.equal(rows[1].text, messages.chapter_beyond_outline);
  assert.equal(rows[2].text, messages.outline_entry_empty);
  assert.equal(rows[0].code, "no_chapter_label");
  for (const row of rows) {
    assert.ok(!row.text.includes("段模式不跑"), row.text);
    assert.ok(row.text.length > 0);
  }
});

test("skipped 没有 reason：显式报「没给原因」，不静默", () => {
  const rows = buildStructureNotices({
    beat: { status: "skipped" },
    premature: { status: "skipped", reason: {} },
    gender: { status: "skipped", reason: { code: "no_character_cards" } },
  });
  assert.equal(rows[0].code, "missing_reason");
  assert.ok(rows[0].text.includes("没有给出原因"), rows[0].text);
  assert.equal(rows[1].code, "missing_reason");
  assert.equal(rows[2].code, "no_character_cards");
  assert.ok(rows[2].text.includes("没有给出原因"), rows[2].text);
});

test("checked 三项显示真实数字", () => {
  const rows = buildStructureNotices({
    beat: {
      status: "checked", total: 4, covered: 3, missing: ["他数了数铜牌"],
      basis: "beat_units",
    },
    premature: {
      status: "checked", chapters_hit: 1, max_hits: 3,
      hits: [{ chapter: 2, terms: ["铜牌"] }],
    },
    gender: { status: "checked", count: 0, max_mismatches: 1 },
  });
  assert.equal(rows[0].statusLabel, "已检查");
  assert.ok(rows[0].text.includes("3/4"), rows[0].text);
  assert.ok(rows[0].text.includes("他数了数铜牌"), rows[0].text);
  assert.equal(rows[0].tone, "warn");
  assert.ok(rows[1].text.includes("命中 1 章"), rows[1].text);
  assert.equal(rows[1].tone, "ok");
  assert.ok(rows[2].text.includes("0 处"), rows[2].text);
  assert.equal(rows[2].tone, "ok");
});

test("beat 兜底覆盖率按后端的 passed 决定告警等级", () => {
  const rows = buildStructureNotices({
    beat: {
      status: "checked", basis: "fallback_term_coverage",
      fallback: { coverage: 1.0, passed: true },
    },
    premature: { status: "checked", chapters_hit: 4, max_hits: 3, hits: [] },
    gender: { status: "checked", count: 2, max_mismatches: 1 },
  });
  assert.ok(rows[0].text.includes("100%"), rows[0].text);
  assert.equal(rows[0].tone, "ok");
  assert.equal(rows[1].tone, "warn");
  assert.equal(rows[2].tone, "warn");
});

test("缺项 / 未知状态不会被当成通过", () => {
  const rows = buildStructureNotices({ beat: { status: "weird" } });
  assert.equal(rows[0].code, "unknown_status");
  assert.equal(rows[0].tone, "warn");
  assert.ok(rows[0].text.includes("weird"), rows[0].text);
  assert.equal(rows[1].code, "missing_result");
  assert.equal(rows[1].tone, "warn");
});
