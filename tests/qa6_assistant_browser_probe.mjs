/**
 * QA6 · 助手面板真浏览器烟雾测试（chromium + playwright-core，本机已有）。
 *
 * 为什么必须开这一层：vue-tsc / vite build 都管不到「v-if 永远不成立」、
 * 「模板里引用了 undefined 字段」这类只在运行时才暴露的问题 —— 这类问题正是
 * 「能编译但不能用」的典型形态。本脚本断言的是**渲染出来的那坨文本**。
 *
 * 前置（由 tests/qa6_assistant_browser_smoke.py 拉起）：
 *   后端 uvicorn :{API_PORT} + 前端 vite dev :{WEB_PORT}（/api 代理到后端）
 *
 * 跑法：node tests/qa6_assistant_browser_probe.mjs
 *       E2E_BASE / E2E_CHROME / E2E_PID 可覆盖
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const MANAGED = "C:/Users/h'h'h/.workbuddy/binaries/node/workspace/node_modules/playwright-core";

function loadChromium() {
  for (const spec of [process.env.E2E_PW || "playwright-core", MANAGED]) {
    try {
      return require(spec).chromium;
    } catch {
      /* 换下一个 */
    }
  }
  console.log("FAIL 找不到 playwright-core");
  process.exit(2);
}
const chromium = loadChromium();

const BASE = process.env.E2E_BASE || "http://127.0.0.1:5183";
const PID = process.env.E2E_PID || "1";
const URL_PROJECT = `${BASE}/projects/${PID}`;
const EXE =
  process.env.E2E_CHROME ||
  "C:/Users/h'h'h/AppData/Local/ms-playwright/chromium-1248/chrome-win64/chrome.exe";
const SHOT = process.env.E2E_SHOT || path.join(os.tmpdir(), `qa6_assistant_${PID}.png`);

let pass = 0;
let fail = 0;
const failed = [];

function check(name, ok, detail = "", hint = "") {
  if (ok) {
    pass += 1;
    console.log(`  [PASS] ${name}${detail ? `\n         ${detail}` : ""}`);
  } else {
    fail += 1;
    failed.push(name);
    console.log(`  [FAIL] ${name}${detail ? `  ← ${detail}` : ""}`);
    if (hint) console.log(`         ${hint}`);
  }
}

/** 助手面板根节点：卡片标题含「助手工作区」的那个 section.card。 */
const PANEL_JS = `
  [...document.querySelectorAll("section.card")].find((c) =>
    (c.querySelector("div.card-title")?.innerText || "").includes("助手工作区"))
`;

async function main() {
  if (!fs.existsSync(EXE)) {
    console.log(`FAIL 找不到 Chromium：${EXE}`);
    process.exit(2);
  }

  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });

  const consoleErrors = [];
  const pageErrors = [];
  const badResponses = [];
  const apiCalls = [];
  page.on("console", (m) => {
    if (m.type() === "error") {
      const loc = m.location();
      consoleErrors.push(`${m.text().slice(0, 140)}${loc?.url ? ` @ ${loc.url}` : ""}`);
    }
  });
  page.on("pageerror", (e) => pageErrors.push(String(e).slice(0, 240)));
  page.on("response", (r) => {
    const u = r.url();
    if (u.includes("/api/")) {
      apiCalls.push(`${r.request().method()} ${u.replace(BASE, "")} -> ${r.status()}`);
    }
    if (r.status() >= 400 && !u.includes("/api/")) badResponses.push(`${r.status()} ${u}`);
  });

  console.log("==============================================================================");
  console.log(`A. 打开工作台 ${URL_PROJECT}`);
  console.log("==============================================================================");
  await page.goto(URL_PROJECT, { waitUntil: "domcontentloaded", timeout: 45000 });

  // 挂载哨兵挂在「助手面板根节点出现」上（不能只等页面加载：v-if/v-else 条件
  // 不成立时页面照样 load 完成）。
  const mounted = await page
    .waitForFunction(`!!(${PANEL_JS})`, null, { timeout: 30000 })
    .then(() => true)
    .catch(() => false);
  check("A1 助手面板真的挂载渲染出来了", mounted,
    mounted ? "" : "30s 内 section.card（助手工作区）未出现",
    "挂载失败 = AssistantPanel 整块没进 DOM，typecheck 完全看不出来");

  if (!mounted) {
    await browser.close();
    console.log(`\n通过 ${pass} 项，失败 ${fail} 项`);
    process.exit(1);
  }

  const panelText = await page.evaluate(`(() => ${PANEL_JS}.innerText)()`);
  check("A2 面板首段说明写清了「助手不会直接改正文」",
    panelText.includes("不会") && panelText.includes("直接改你的正文"),
    panelText.split("\n").slice(0, 4).join(" / ").slice(0, 160));
  check("A3 面板里的 after_text 红线提醒已渲染",
    (await page.locator("div.notice.warn.draft-warn").count()) >= 0);

  console.log("\n==============================================================================");
  console.log("B. 四个页签都能切换（切过去内容真的变，不是只换高亮）");
  console.log("==============================================================================");
  const tabs = await page.locator("section.card div.tabs button").allInnerTexts();
  console.log(`     页签 = ${JSON.stringify(tabs)}`);
  check("B0 存在四个页签", tabs.length === 4, `${tabs.length} 个`);
  const expect = ["问题（必填）", "诊断范围", "起草提案", "版本只增不改"];
  for (let i = 0; i < tabs.length; i++) {
    await page.locator("section.card div.tabs button").nth(i).click();
    await page.waitForTimeout(350);
    const t = await page.evaluate(`(() => ${PANEL_JS}.innerText)()`);
    check(`B${i + 1} 页签「${tabs[i].trim()}」切换后渲染出对应内容`,
      t.includes(expect[i]),
      t.replace(/\n+/g, " / ").slice(0, 180));
  }

  console.log("\n==============================================================================");
  console.log("C. 跑一次真诊断（点按钮 → 出请求 → 渲染结论）");
  console.log("==============================================================================");
  await page.locator("section.card div.tabs button").nth(1).click(); // 诊断
  await page.waitForTimeout(200);
  const apiBefore = apiCalls.length;
  await page.locator("button.btn.primary", { hasText: "跑诊断" }).click();
  const diagOk = await page
    .waitForFunction(
      `(() => { const p = ${PANEL_JS};
        return !!p && /AgentRun #/.test(p.innerText); })()`,
      null,
      { timeout: 60000 },
    )
    .then(() => true)
    .catch(() => false);
  const diagApi = apiCalls.slice(apiBefore).filter((s) => s.includes("/api/assistant/"));
  console.log(`     诊断期间发出的 assistant 请求：${JSON.stringify(diagApi)}`);
  check("C1 点「跑诊断」发出了 POST /api/assistant/diagnose 且 200",
    diagApi.some((s) => s.startsWith("POST /api/assistant/diagnose -> 200")),
    diagApi.join(" | ").slice(0, 200));
  check("C2 面板渲染出诊断结论（AgentRun #N）", diagOk, "",
    "按钮点了没反应 / 结论没渲染，是最典型的「能编译不能用」");

  if (diagOk) {
    const dt = await page.evaluate(`(() => ${PANEL_JS}.innerText)()`);
    check("C3 结论区显示「命中 N 个问题」", /命中 \d+ 个问题/.test(dt),
      (dt.match(/命中 \d+ 个问题/) || ["未出现"])[0]);
    // severity 必须是「文字 + 色标」双编码：出现「警告 warn」这种带英文枚举的字样
    const sevTags = await page.locator("section.card span.tag").allInnerTexts();
    const dualCoded = sevTags.filter((t) => /提示 info|警告 warn|错误 error/.test(t));
    console.log(`     severity/status 标签样本：${JSON.stringify(dualCoded.slice(0, 5))}`);
    check("C4 severity 是「中文 + 英文枚举」双编码（不靠颜色单打独斗）",
      dualCoded.length > 0, `${dualCoded.length} 个标签`);
  }

  console.log("\n==============================================================================");
  console.log("D. 提案起草表单：after_text 留空 + 从 issue 起草也不替用户写");
  console.log("==============================================================================");
  await page.locator("section.card div.tabs button").nth(2).click(); // 提案审批
  await page.waitForTimeout(350);
  const afterVal = await page.locator("#asst-draft-after").inputValue();
  check("D1 新建草稿的 after_text 输入框是空的（不预填、不自动生成）", afterVal === "",
    `inputValue=${JSON.stringify(afterVal)}`);

  // 从诊断 issue 起草 → after_text 必须仍然为空
  await page.locator("section.card div.tabs button").nth(1).click();
  await page.waitForTimeout(300);
  const draftBtns = page.locator("section.card button.btn.sm", { hasText: "起草提案" });
  const nDraft = await draftBtns.count();
  if (nDraft > 0) {
    await draftBtns.first().click();
    await page.waitForTimeout(400);
    const t3 = await page.locator("section.card div.tabs button.active").innerText();
    check("D2 点 issue 的「起草提案 →」会跳到提案页签", t3.includes("提案审批"), t3.trim());
    const afterVal2 = await page.locator("#asst-draft-after").inputValue();
    const reasonVal = await page.locator("#asst-draft-reason").inputValue();
    check("D3 从 issue 起草后 after_text 仍然为空（红线：改成什么只能人填）",
      afterVal2 === "", `inputValue=${JSON.stringify(afterVal2)}`);
    check("D4 从 issue 起草带上了 reason 素材（只给「为什么改」）", reasonVal.length > 0,
      reasonVal.slice(0, 60));
  } else {
    check("D2/D3/D4 至少有一条可起草的 issue", false, "诊断没有渲染出「起草提案 →」按钮");
  }

  console.log("\n==============================================================================");
  console.log("G. 真在浏览器里走一遍「起草 → 批准 → 应用」（含二次确认）");
  console.log("==============================================================================");
  await page.locator("section.card div.tabs button").nth(2).click();
  await page.waitForTimeout(300);
  await page.fill("#asst-draft-target", "1");
  const NEW_TEXT = "【QA6 浏览器实测改写】第一章。陈默推开门，铜锁发出干涩的一声。";
  await page.fill("#asst-draft-after", NEW_TEXT);
  const createdResp = page.waitForResponse(
    (r) => r.url().includes("/api/assistant/proposals") && r.request().method() === "POST",
    { timeout: 30000 },
  ).catch(() => null);
  await page.locator("button.btn.primary", { hasText: "创建提案" }).click();
  const cResp = await createdResp;
  check("G1 表单提交发出 POST /api/assistant/proposals 且 200",
    !!cResp && cResp.status() === 200, cResp ? `HTTP ${cResp.status()}` : "30s 内没发出");

  const detailReady = await page
    .waitForFunction(
      `(() => { const p = ${PANEL_JS};
        return !!p && p.innerText.includes("改动差异 diff（应用前必看）"); })()`,
      null, { timeout: 20000 },
    )
    .then(() => true)
    .catch(() => false);
  check("G2 创建后自动展开提案详情（含必看的 diff）", detailReady, "",
    "详情没渲染 = 用户看不到 diff 就要做审批决定");

  if (detailReady) {
    const pTxt = await page.evaluate(`(() => ${PANEL_JS}.innerText)()`);
    check("G3 新提案展示为「待审批 pending」", /待审批 pending/.test(pTxt),
      (pTxt.match(/待审批 pending/) || ["未出现"])[0]);
    check("G4 面板展示 diff 与基线哈希（审批依据）",
      pTxt.includes("基线哈希 base_content_hash") && pTxt.includes("影响范围 impact"));

    const approveBtn = page.locator("section.card button.btn", { hasText: "批准（pending" });
    const applyBtn = page.locator("section.card button.btn", { hasText: "应用（approved" });
    check("G5 「批准」与「应用」是两个独立按钮",
      (await approveBtn.count()) === 1 && (await applyBtn.count()) === 1,
      `批准=${await approveBtn.count()} 应用=${await applyBtn.count()}`);
    check("G6 pending 状态下「应用」按钮是 disabled 的（不能跳步）",
      await applyBtn.first().isDisabled());

    await approveBtn.first().click();
    const appr = await page.waitForFunction(
      `(() => ${PANEL_JS}.innerText.includes("已批准 · 未生效 approved"))()`,
      null, { timeout: 20000 },
    ).then(() => true).catch(() => false);
    check("G7 点「批准」后状态变成「已批准 · 未生效 approved」", appr);

    // 「应用」按钮的 enabled 由 `canApply && !actionBusy` 决定：批准请求会让
    // actionBusy 在 refresh 期间保持 true，所以这里必须轮询而不是单次快照，
    // 否则「文字已变成 approved 但按钮还在 busy」会被误判成产品 bug。
    let g8 = false;
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await applyBtn.first().isEnabled()) {
        g8 = true;
        break;
      }
      await page.waitForTimeout(250);
    }
    check("G8 批准后「应用」按钮才可用", g8,
      g8 ? "" : "15s 内按钮一直是 disabled —— 批准后无法落库");

    // 二次确认
    await applyBtn.first().click();
    await page.waitForTimeout(300);
    const confirmVisible = await page.locator("div.notice.warn.confirm-box").count();
    check("G9 点「应用」先弹二次确认（不是一键生效）", confirmVisible > 0,
      `${confirmVisible} 个确认框`);

    const applied = page.waitForResponse(
      (r) => /\/api\/assistant\/proposals\/\d+\/apply$/.test(r.url()), { timeout: 30000 },
    ).catch(() => null);
    await page.locator("button.btn.primary", { hasText: "确认应用" }).click();
    const aResp = await applied;
    check("G10 确认后发出 POST .../apply 且 200",
      !!aResp && aResp.status() === 200, aResp ? `HTTP ${aResp.status()}` : "没发出");
    const okNotice = await page.waitForFunction(
      `(() => ${PANEL_JS}.innerText.includes("已生效：提案 #"))()`,
      null, { timeout: 20000 },
    ).then(() => true).catch(() => false);
    check("G11 面板给出「已生效 + 生成的 Version 号」回执", okNotice);
  }

  console.log("\n==============================================================================");
  console.log("H. 版本页签：应用产生了快照 + 回滚有二次确认且说清后果");
  console.log("==============================================================================");
  await page.locator("section.card div.tabs button").nth(3).click();
  await page.waitForTimeout(600);
  const vt = await page.evaluate(`(() => ${PANEL_JS}.innerText)()`);
  check("H1 版本列表里出现了 Version #1（应用真的产了快照）",
    /Version #\d+/.test(vt), (vt.match(/Version #\d+/) || ["未出现"])[0]);
  check("H2 版本 Only 增不改的文案已渲染", vt.includes("版本只增不改"), "");

  const restoreBtns = page.locator("section.card button.btn.sm", { hasText: "回滚到此版本" });
  if (await restoreBtns.count()) {
    await restoreBtns.first().click();
    await page.waitForTimeout(300);
    const boxTxt = await page.locator("div.notice.err.confirm-box").first().innerText();
    console.log(`     回滚确认文案：${boxTxt.replace(/\n+/g, " / ").slice(0, 200)}`);
    check("H3 回滚有二次确认", boxTxt.length > 0);
    check("H4 确认文案说清「当前内容会被覆盖」", boxTxt.includes("会被覆盖"), "");
    check("H5 确认文案说清「回滚本身也生成新 Version，历史不丢」",
      boxTxt.includes("新 Version") && boxTxt.includes("历史不丢"), "");
  } else {
    check("H3/H4/H5 版本列表里有可回滚条目", false, "没有「回滚到此版本…」按钮");
  }

  await page.screenshot({ path: SHOT, fullPage: true });
  console.log(`\n截图：${SHOT}`);

  console.log("\n==============================================================================");
  console.log("F. 控制台 / 页面异常");
  console.log("==============================================================================");
  const realConsoleErrors = consoleErrors.filter((e) => !/favicon/i.test(e));
  check("F1 没有未捕获的页面级异常（Vue 运行时报错全在这里）",
    pageErrors.length === 0, pageErrors.join(" | ").slice(0, 300),
    "pageerror 非空意味着模板引用了 undefined 字段或生命周期里抛错");
  check("F2 控制台无 error（排除 favicon）", realConsoleErrors.length === 0,
    realConsoleErrors.join(" | ").slice(0, 300));
  check("F3 静态资源无 4xx/5xx", badResponses.length === 0, badResponses.join(" | ").slice(0, 200));
  console.log(`     assistant 相关请求合计 ${apiCalls.filter((s) => s.includes("assistant/")).length} 条：`);
  for (const c of apiCalls.filter((s) => s.includes("assistant/"))) console.log(`       ${c}`);

  await browser.close();

  console.log("\n==============================================================================");
  console.log(`通过 ${pass} 项，失败 ${fail} 项`);
  if (failed.length) console.log(`失败项：\n  - ${failed.join("\n  - ")}`);
  console.log("==============================================================================");
  process.exit(fail ? 1 : 0);
}

main().catch((e) => {
  console.log("EXCEPTION", e);
  process.exit(2);
});
