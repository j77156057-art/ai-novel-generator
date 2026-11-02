import test from "node:test";
import assert from "node:assert/strict";
import type { CharacterCardItem, Reminders } from "../src/api/types.ts";
import {
  formatCharacterCardsMarkdown,
  formatOutlineMarkdown,
  formatRemindersMarkdown,
  projectExportFilename,
  projectExportTypeLabel,
  sanitizeFilename,
} from "../src/utils/projectExports.ts";

test("文件名去掉系统保留字符和首尾空白，保留中文", () => {
  assert.equal(sanitizeFilename("  <星>:\"海/\\|?*\u0000  "), "星海");
  assert.equal(projectExportFilename({ title: " 星:海 " }, "outline"), "星海-大纲.md");
  assert.equal(projectExportFilename({ title: "人物?" }, "characters"), "人物-人物表.md");
  assert.equal(projectExportFilename({ title: "?" }, "reminders"), "未命名作品-坑清单.md");
});

test("导出类型标签统一用于界面和文件名", () => {
  assert.equal(projectExportTypeLabel("outline"), "大纲");
  assert.equal(projectExportTypeLabel("characters"), "人物表");
  assert.equal(projectExportTypeLabel("reminders"), "坑清单");
});

test("数组大纲转成有序 Markdown，字符串大纲保留原有结构", () => {
  assert.equal(formatOutlineMarkdown(["相遇", " 反转 "]), "# 大纲\n\n1. 相遇\n2. 反转\n");
  assert.equal(
    formatOutlineMarkdown("## 第一幕\n\n- 相遇\n- 离开"),
    "# 大纲\n\n## 第一幕\n\n- 相遇\n- 离开\n",
  );
});

test("空大纲明确写暂无，不编造条目", () => {
  for (const outline of [null, "", "  ", [], ["", "  "]]) {
    assert.equal(formatOutlineMarkdown(outline), "# 大纲\n\n暂无大纲。\n");
  }
});

test("人物表保留身份、能力、声纹、关系和来源，数组与对象可直接阅读", () => {
  const character: CharacterCardItem = {
    name: "林晚",
    role: "主角",
    motivation: "找回失散的家人",
    card: {
      身份: { 年龄: "十九", 出身: "临海镇" },
      能力: {
        武器: [{ 名称: "长刀", 说明: "母亲留下的旧刀" }],
        功法: [{ 名称: "听潮诀" }],
        能力: [{ 名称: "辨风", 说明: "能分辨风向" }],
        能力上限: ["不能隔空取物", "雨天失灵"],
      },
      声纹: { 自称: "我", 称呼他人: ["阿姐", "掌柜"], 口头禅: ["先看风向"] },
      关系: { 林舟: "失散的弟弟", 周掌柜: "救命恩人" },
      来源: "第三章角色卡",
    },
    card_source: "ai",
    persona: { 性格: "谨慎" },
    persona_source: "human",
  };
  const output = formatCharacterCardsMarkdown([character]);
  assert.match(output, /^# 人物表\n\n## 林晚/);
  assert.match(output, /- 角色：主角/);
  assert.match(output, /- 动机：找回失散的家人/);
  assert.match(output, /- 卡片来源：ai/);
  assert.match(output, /- 个性来源：human/);
  assert.match(output, /### 个性[\s\S]*- 性格：谨慎/);
  assert.match(output, /### 身份[\s\S]*- 年龄：十九/);
  assert.match(output, /- 出身：临海镇/);
  assert.match(output, /- 武器：名称：长刀，说明：母亲留下的旧刀/);
  assert.match(output, /- 功法：名称：听潮诀/);
  assert.match(output, /- 能力上限：不能隔空取物；雨天失灵/);
  assert.match(output, /- 称呼他人：阿姐；掌柜/);
  assert.match(output, /- 林舟：失散的弟弟/);
  assert.match(output, /### 来源\n\n第三章角色卡/);
  assert.doesNotMatch(output, /\[object Object\]/);
});

test("没有人物时不输出虚构角色", () => {
  assert.equal(formatCharacterCardsMarkdown([]), "# 人物表\n\n暂无人物表。\n");
});

test("坑清单输出伏笔与人物约束的章号、提及信息", () => {
  const reminders: Reminders = {
    unresolved_plotholes: [
      { question: "谁留下了旧刀？", first_chapter: 2 },
      { question: null, first_chapter: null },
    ],
    character_constraints: [
      { name: "林晚", mentions: 8, first_chapter: 1, last_chapter: 5 },
      { name: null, mentions: null, first_chapter: null, last_chapter: null },
    ],
  };
  const output = formatRemindersMarkdown(reminders);
  assert.match(output, /## 未解伏笔/);
  assert.match(output, /- 谁留下了旧刀？（首次提及：第 2 章）/);
  assert.match(output, /- 未填写问题（首次提及：章号未知）/);
  assert.match(output, /## 人物约束/);
  assert.match(output, /- 林晚（提及 8 次；首次提及：第 1 章；最近提及：第 5 章）/);
  assert.match(output, /- 未命名人物（提及 次数未知；首次提及：章号未知；最近提及：章号未知）/);
});

test("坑清单的两个空分区都明确写暂无", () => {
  const output = formatRemindersMarkdown({ unresolved_plotholes: [], character_constraints: [] });
  assert.equal(
    output,
    "# 坑清单\n\n## 未解伏笔\n\n暂无未解伏笔。\n\n## 人物约束\n\n暂无人物约束。\n",
  );
});
