import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 样式块的花括号必须配平。
 *
 * ## 为什么要有这条
 *
 * 少一个 `}`，浏览器不会报错，而是把后面的规则**当成嵌套规则**（Chrome 的原生 CSS 嵌套），
 * 于是 `.bible-modal-mask` 变成 `.bible-row .k .bible-modal-mask` —— 选择器再也匹配不到元素。
 * 后果是：弹窗在 DOM 里（`querySelector` 找得到），但屏幕上根本不画。
 *
 * 实测踩过：`.bible-row .k {` 少了 `}`，一次性把 5 个弹窗样式
 * （编辑剧情圣经 / revision 历史 / 冲突 toast / 角色个性 / 角色卡）全吞掉，
 * 类型检查、构建、单测、CI 全绿，只有真机点开才看得见。
 */
const SRC = join(dirname(fileURLToPath(import.meta.url)), "..", "src");
const AT_RULES = ["@media", "@keyframes", "@supports", "@container", "@layer", "@scope"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else out.push(p);
  }
  return out;
}

/** 返回结构问题列表（空 = 没问题）。 */
export function styleBlockProblems(css: string): string[] {
  const problems: string[] = [];
  let depth = 0;
  let buf = "";
  const atRuleDepths: number[] = [];
  for (const ch of css) {
    buf += ch;
    if (ch === "{") {
      const selector = buf.trim();
      const isAtRule = AT_RULES.some((r) => selector.startsWith(r));
      // @media / @keyframes 里的嵌套是合法的；普通规则里的嵌套通常是漏了 }
      const insideAtRule = atRuleDepths.some((d) => depth > d);
      if (depth >= 1 && !insideAtRule) {
        problems.push(`普通规则里出现嵌套（多半是上面漏了 }）：${selector.slice(-60)}`);
      }
      if (isAtRule) atRuleDepths.push(depth);
      depth += 1;
      buf = "";
    } else if (ch === "}") {
      depth -= 1;
      while (atRuleDepths.length && atRuleDepths[atRuleDepths.length - 1] >= depth) atRuleDepths.pop();
      if (depth < 0) {
        problems.push("多余的 }");
        depth = 0;
      }
      buf = "";
    } else if (ch === ";" && depth === 0) {
      const decl = buf.trim();
      if (decl && !decl.startsWith("@")) problems.push(`顶层散落的声明：${decl.slice(-50)}`);
      buf = "";
    }
  }
  if (depth !== 0) problems.push(`文件结束时 depth=${depth}（花括号不配平）`);
  return problems;
}

test("样式块花括号配平：普通规则里不许出现嵌套", () => {
  const files = walk(SRC).filter((f) => f.endsWith(".vue") || f.endsWith(".css"));
  assert.ok(files.length > 0, "没找到样式文件，测试本身有问题");
  const bad: string[] = [];
  for (const f of files) {
    const text = readFileSync(f, "utf-8");
    const blocks = f.endsWith(".vue")
      ? [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1])
      : [text];
    blocks.forEach((b, i) => {
      for (const p of styleBlockProblems(b)) bad.push(`${relative(SRC, f)} [style#${i}] ${p}`);
    });
  }
  assert.deepEqual(bad, [], `样式块结构有问题（弹窗会画不出来）：\n${bad.join("\n")}`);
});

test("判据本身能认出漏花括号", () => {
  const broken = `.a {\n  color: red;\n.b {\n  position: fixed;\n}\n`;
  const problems = styleBlockProblems(broken);
  assert.ok(problems.length > 0, "漏了 } 却没报出来");
  assert.match(problems.join("\n"), /漏了 \}/);
});
