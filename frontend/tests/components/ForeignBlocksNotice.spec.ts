import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import ForeignBlocksNotice from "@/components/ForeignBlocksNotice.vue";
import type { ForeignBlocksReport } from "@/api/types";

/**
 * 「混进了别的书」结论块的渲染层测试。
 *
 * 这块牵着**一个不可逆的决定**（剔除 = 删掉整章正文），所以界面必须守住：
 *   1. 结论看得见（混了几段、独占词是哪些）；
 *   2. 剔除勾选框**默认不勾**；
 *   3. 已入库的语料页**不给**勾选框（那时已经入库了，剔不剔是另一回事）。
 *
 * 用真实数据：雾港夜行的体检报告（3 段 / 230,674 字 / 5.5%）。
 */
function report(p: Partial<ForeignBlocksReport> = {}): ForeignBlocksReport {
  return {
    detected: true,
    block_count: 3,
    chars: 230674,
    ratio: 0.054927,
    dropped: true,
    blocks: [
      {
        start: 0,
        end: 100,
        chars: 57676,
        ratio: 0.0137,
        chapter_count: 4,
        // 真数据里盗版站把同一章标题贴了两遍 —— 界面上不该重复摆。
        chapter_titles: ["第1047章 符合路子", "第1047章 符合路子", "第1048章 我怎么知道?", "第1048章 我怎么知道?"],
        terms: ["秦昭", "天规", "帝尊", "上苍界", "柳莺"],
        exclusive_ratio: 0.52,
        sample: "灵魂契约，契合灵魂……",
      },
    ],
    ...p,
  } as ForeignBlocksReport;
}

describe("ForeignBlocksNotice", () => {
  it("结论说清混了几段、多少字、占多大比例", () => {
    const w = mount(ForeignBlocksNotice, { props: { report: report() } });
    const text = w.text();
    expect(text).toContain("混进了别的书");
    expect(text).toContain("3 段别的书");
    expect(text).toContain("230,674 字");
    expect(text).toContain("5.5%");
  });

  it("独占词逐个列出（判定依据要给人看）", () => {
    const w = mount(ForeignBlocksNotice, { props: { report: report() } });
    const terms = w.find(".tag.warn").text();
    expect(terms).toContain("独占词");
    expect(terms).toContain("秦昭");
    expect(terms).toContain("天规");
  });

  it("章标题去重：盗版站把同一章贴两遍，界面上只摆一次", () => {
    const w = mount(ForeignBlocksNotice, { props: { report: report() } });
    const text = w.text();
    expect(text).toContain("第1047章 符合路子");
    expect(text).toContain("第1048章 我怎么知道?");
    // 「符合路子」只该出现一次
    expect(text.split("第1047章 符合路子").length - 1).toBe(1);
  });

  it("投放场景给勾选框，且默认不勾", () => {
    const w = mount(ForeignBlocksNotice, {
      props: { report: report(), selectable: true, modelValue: false },
    });
    const box = w.find('input[type="checkbox"]');
    expect(box.exists()).toBe(true);
    expect((box.element as HTMLInputElement).checked).toBe(false);
    expect(w.text()).toContain("剔除这几段别的书再入库");
  });

  it("勾上会向外抛 true（父组件据此改请求体）", async () => {
    const w = mount(ForeignBlocksNotice, {
      props: { report: report(), selectable: true, modelValue: false },
    });
    await w.find('input[type="checkbox"]').setValue(true);
    expect(w.emitted("update:modelValue")?.[0]).toEqual([true]);
  });

  it("已入库的语料页不给勾选框（那时候已经入库了）", () => {
    const w = mount(ForeignBlocksNotice, { props: { report: report() } });
    expect(w.find('input[type="checkbox"]').exists()).toBe(false);
  });
});
