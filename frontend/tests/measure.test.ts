import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { formatMeasure, measureText, toMeasure } from '../src/utils/measure.ts';

/**
 * 前端纯量层口径测试。
 *
 * 用例来自 `tests/fixtures/measure_cases.json` —— **后端也对着同一份文件断言**
 * （`tests/test_measure_layers.py::test_shared_measure_fixture_matches_backend`）。
 *
 * 为什么要有这份共享 fixture：前端要显示「汉字数 / 标点占比 / 空白占比」，
 * 就得在浏览器里自己数一遍；数出来的数必须与后端段长闸门量的那个数**是同一个数**。
 * 两边各写各的，早晚会漂一处 —— 漂了之后的症状是「界面说 1050 字、闸门说 1200 字」，
 * 没人查得清是界面错了还是闸门错了。
 */

const here = dirname(fileURLToPath(import.meta.url));
const fixturePath = join(here, '..', '..', 'tests', 'fixtures', 'measure_cases.json');

interface Case {
  name: string;
  text: string;
  chars: number;
  cjk: number;
  punct: number;
  blank: number;
}

const fixture = JSON.parse(readFileSync(fixturePath, 'utf8')) as { cases: Case[] };

test('共享 fixture 的每条用例，前端口径与后端一致', () => {
  assert.ok(fixture.cases.length >= 10, '用例太少，挡不住口径漂移');
  for (const c of fixture.cases) {
    const m = measureText(c.text);
    assert.equal(m.chars, c.chars, `${c.name}：字符数 ${m.chars} ≠ ${c.chars}`);
    assert.equal(m.cjk, c.cjk, `${c.name}：汉字数 ${m.cjk} ≠ ${c.cjk}`);
    assert.equal(m.punct, c.punct, `${c.name}：标点数 ${m.punct} ≠ ${c.punct}`);
    assert.equal(m.blank, c.blank, `${c.name}：空白数 ${m.blank} ≠ ${c.blank}`);
  }
});

test('占比以字符数为分母（不是汉字数）', () => {
  const m = measureText('他站着。\n\n她走了。');
  assert.equal(m.chars, 10);
  assert.equal(m.punctPct, 20); // 2 / 10
  assert.equal(m.blankPct, 20); // 2 / 10
});

test('空文本不炸，占比为 0', () => {
  const m = measureText('');
  assert.deepEqual(m, { chars: 0, cjk: 0, punct: 0, blank: 0, punctPct: 0, blankPct: 0 });
});

test('字符数按码点算，不是 UTF-16 长度', () => {
  // "😀他"：JS 的 .length 是 3（代理对），码点数是 2 —— 与 Python len() 对齐。
  const m = measureText('😀他');
  assert.equal(m.chars, 2);
  assert.equal(m.cjk, 1);
});

test('BOM 不算空白（与 Python str.isspace() 一致）', () => {
  const m = measureText('﻿他');
  assert.equal(m.blank, 0);
  assert.equal(m.cjk, 1);
});

test('ASCII 标点不算中文标点（与后端 _PUNCT_ALL 一致）', () => {
  const m = measureText('yes, no.');
  assert.equal(m.punct, 0);
  assert.equal(m.blank, 1); // 中间那个空格算空白
  assert.equal(m.cjk, 0);
});

test('后端 snake_case 读数转成前端的 camelCase', () => {
  const m = toMeasure({
    chars: 100,
    cjk: 80,
    punct: 15,
    blank: 5,
    paras: 4,
    sents: 12,
    punct_pct: 15,
    blank_pct: 5,
  });
  assert.equal(m?.punctPct, 15);
  assert.equal(m?.blankPct, 5);
  assert.equal(toMeasure(null), null);
  assert.equal(toMeasure(undefined), null);
});

test('展示格式：汉字在前，标点/空白占比在后', () => {
  const label = formatMeasure(measureText('他推开门。'));
  assert.equal(label, '汉字 4 · 标点 20.0% · 空白 0.0%');
  assert.equal(formatMeasure(null), '');
});
