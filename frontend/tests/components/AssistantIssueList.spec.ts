import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import AssistantIssueList from "@/components/AssistantIssueList.vue";
import type { AssistantIssue } from "@/api/types";

/**
 * 组件级测试（vitest + @vue/test-utils + jsdom）。
 *
 * 之前前端只有纯逻辑测试（`tests/*.test.ts`，node:test）—— 渲染层**一行没测过**。
 * 这份文件测的是「组件真的渲染成那样吗」：分组顺序、空状态文案、折叠交互。
 *
 * 为什么先测这三个组件：它们是**纯展示**（只吃 props、不发请求），
 * 断言可以钉死「界面不许出现内部术语」「错误在前、提示在后」这类约定，
 * 而且不依赖任何后端 ⇒ 跑得快、不会偶发红。
 */

function issue(p: Partial<AssistantIssue> & { id: string }): AssistantIssue {
  return {
    type: "plothole",
    severity: "info",
    title: "标题",
    claim: "判定说明",
    suggestion: "处理建议",
    rule: "some_rule",
    evidence: [],
    ...p,
  } as AssistantIssue;
}

function mountList(issues: AssistantIssue[], extra: Record<string, unknown> = {}) {
  return mount(AssistantIssueList, {
    props: {
      issues,
      evidence: [],
      confidence: 0.8,
      rulesEvaluated: [],
      agentRunId: 7,
      ...extra,
    },
  });
}

describe("AssistantIssueList", () => {
  it("按 error → warn → info 分组，组内保持后端顺序", () => {
    const w = mountList([
      issue({ id: "i1", severity: "info", title: "提示一条" }),
      issue({ id: "i2", severity: "error", title: "错误一条" }),
      issue({ id: "i3", severity: "warn", title: "警告一条" }),
    ]);
    const heads = w.findAll(".sev-head .tag").map((n) => n.text());
    // 「错误 error」「警告 warn」「提示 info」——文字与色标双编码，标签本身带文字
    expect(heads).toEqual(["错误 error", "警告 warn", "提示 info"]);
    // 组内条数
    expect(w.findAll(".sev-group").map((g) => g.findAll(".issue").length)).toEqual([1, 1, 1]);
  });

  it("空结果给的是「规则都跑过了，不等于没问题」，不是「一切正常」", () => {
    const w = mountList([]);
    expect(w.text()).toContain("未命中任何已知问题模式");
    expect(w.text()).toContain("不等同于「绝对没问题」");
    expect(w.findAll(".sev-group")).toHaveLength(0);
  });

  it("结论默认折叠，点「展开全文」才展开", async () => {
    const w = mountList([], { answer: "这是一段结论。" });
    const prose = w.find(".prose.snippet");
    expect(prose.classes()).not.toContain("expanded");
    const btn = w.findAll("button").find((b) => b.text() === "展开全文");
    expect(btn).toBeTruthy();
    await btn!.trigger("click");
    expect(w.find(".prose.snippet").classes()).toContain("expanded");
    expect(w.text()).toContain("收起");
  });

  it("没有结论文本时整块不出现（调用方自己渲染过就不重复显示）", () => {
    const w = mountList([], { answer: "" });
    expect(w.text()).not.toContain("结论");
  });

  it("issue 的内部字段（rule/type）与人话标签并列显示", () => {
    const w = mountList([issue({ id: "i9", type: "plothole", rule: "unresolved_plothole" })]);
    const text = w.text();
    expect(text).toContain("伏笔"); // 人话标签
    expect(text).toContain("plothole"); // 原始值仍可对账
    expect(text).toContain("rule=unresolved_plothole");
  });

  it("没有证据时明说「该 issue 未附证据」，不假装有", () => {
    const w = mountList([issue({ id: "i0", evidence: [] })]);
    expect(w.text()).toContain("该 issue 未附证据");
  });

  it("证据条目显示来源的人话说法 + 原始来源", () => {
    const w = mountList([
      issue({
        id: "i1",
        evidence: [
          { kind: "quote", source: "generation", ref_id: "g1", snippet: "原文一句", score: 0.5 },
        ],
      }),
    ]);
    const text = w.text();
    expect(text).toContain("本次生成正文");
    expect(text).toContain("source=generation");
    expect(text).toContain("score=0.500");
  });

  it("已执行规则以标签列出（空数组时不占位）", () => {
    const w = mountList([], { rulesEvaluated: ["rule_a", "rule_b"] });
    expect(w.text()).toContain("rule_a");
    expect(w.text()).toContain("rule_b");
    const w2 = mountList([], { rulesEvaluated: [] });
    expect(w2.text()).not.toContain("已执行规则");
  });

  it("换一次响应（issues 换了）清掉上一次的展开态", async () => {
    const w = mountList([issue({ id: "i1" })], { answer: "结论甲" });
    const btn = w.findAll("button").find((b) => b.text() === "展开全文")!;
    await btn.trigger("click");
    expect(w.find(".prose.snippet").classes()).toContain("expanded");

    // 新一轮响应：issues 换了（新数组）⇒ 展开态清空，避免旧展开态串场
    await w.setProps({ issues: [issue({ id: "i2", title: "新问题" })], answer: "结论乙" });
    expect(w.find(".prose.snippet").classes()).not.toContain("expanded");
    expect(w.text()).toContain("新问题");
  });
});
