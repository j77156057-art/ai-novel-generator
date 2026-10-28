import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import DistillProgress from "@/components/DistillProgress.vue";
import type { DistillJob } from "@/api/types";

/**
 * 蒸馏进度条（纯展示组件）。
 *
 * 这条组件的界面纪律是两条硬约定，组件级测试就是为了把它们钉住：
 * 1. **状态永远是文字**（排队中 / 运行中 / 已完成 / 失败），颜色只是冗余强化；
 * 2. **总数没探明时不能显示百分比**（`progress_total === 0` ⇒ 不确定态），
 *    显示「0 / 0 = 0%」会被用户当成卡死。
 */

function job(p: Partial<DistillJob>): DistillJob {
  return {
    id: 1,
    status: "running",
    stage: "",
    progress_current: 0,
    progress_total: 0,
    n_chapters: null,
    rule_count: null,
    rejected: null,
    error: null,
    started_at: null,
    finished_at: null,
    ...p,
  } as DistillJob;
}

describe("DistillProgress", () => {
  it("运行中：文字状态 + 百分比 + 阶段计数", () => {
    // 后端报的是「已完成阶段数 / 总阶段数」（阶段化进度），不是块数：
    // 块是并发跑的，用块数当分母会 0.6 秒内从 17% 跳到 100%（2026-10-28 实测）。
    const w = mount(DistillProgress, {
      props: { job: job({ status: "running", progress_current: 3, progress_total: 12, stage: "正在切分语料" }) },
    });
    const text = w.text();
    expect(text).toContain("运行中");
    expect(text).toContain("25%");
    expect(text).toContain("第 3 / 12 步");
    expect(text).toContain("正在切分语料");
  });

  it("运行中哪怕 backend 报满也不许显示 100%（早满晚完是最糟的体感）", () => {
    const w = mount(DistillProgress, {
      props: { job: job({ status: "running", progress_current: 6, progress_total: 6 }) },
    });
    expect(w.text()).toContain("99%");
    expect(w.text()).not.toContain("100%");
  });

  it("总数没探明 → 不确定态：不显示百分比，只报已完成项数", () => {
    const w = mount(DistillProgress, {
      props: { job: job({ status: "running", progress_current: 5, progress_total: 0 }) },
    });
    const text = w.text();
    expect(text).toContain("已完成 5 项");
    expect(text).not.toContain("%");
    // 进度条要标成不确定（aria-valuenow 留空）
    expect(w.find('[role="progressbar"]').attributes("aria-valuenow")).toBeUndefined();
  });

  it("排队中：说清楚「在后台跑，进度不会丢」", () => {
    const w = mount(DistillProgress, { props: { job: job({ status: "pending" }) } });
    expect(w.text()).toContain("排队中");
    expect(w.text()).toContain("进度不会丢");
  });

  it("失败：必须给出原因，不能只让进度条转半天", () => {
    const w = mount(DistillProgress, {
      props: { job: job({ status: "failed", error: "模型密钥未配置" }) },
    });
    const text = w.text();
    expect(text).toContain("失败");
    expect(text).toContain("蒸馏失败");
    expect(text).toContain("模型密钥未配置");
  });

  it("失败但服务端没给原因时，也要有一句人话而不是空白", () => {
    const w = mount(DistillProgress, { props: { job: job({ status: "failed", error: null }) } });
    expect(w.text()).toContain("（服务端未返回原因）");
  });

  it("成功：摘要项缺哪个就不显示哪个，不用 0 蒙混", () => {
    const w = mount(DistillProgress, {
      props: { job: job({ status: "success", n_chapters: 30, rule_count: null, rejected: 2 }) },
    });
    const text = w.text();
    expect(text).toContain("已写入叙事档案");
    expect(text).toContain("采样 30 章");
    expect(text).toContain("拒伪规则 2 条");
    expect(text).not.toContain("提炼 0 条规则");
  });

  it("档位名给了才显示（没给就不占位）", () => {
    const w1 = mount(DistillProgress, { props: { job: job({}), tierName: "完整" } });
    expect(w1.text()).toContain("档位：完整");
    const w2 = mount(DistillProgress, { props: { job: job({}) } });
    expect(w2.text()).not.toContain("档位：");
  });

  it("百分比被夹在 0~100（后端估算可能略超）", () => {
    const w = mount(DistillProgress, {
      props: { job: job({ status: "running", progress_current: 13, progress_total: 12 }) },
    });
    // 运行中封顶 99%；完成态才允许 100%（这条只要求「不越界」）
    expect(w.text()).toContain("99%");
    expect(w.text()).not.toContain("108%");
  });
});
