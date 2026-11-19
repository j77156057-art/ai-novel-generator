import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

import ProjectWorkspace from "@/views/ProjectWorkspace.vue";

/**
 * 工作台关键交互的组件级测试（mock 掉 API 与路由，不连后端）。
 *
 * 覆盖两块最容易被后续改动碰坏的接线：
 * 1. 步骤 6 的「写作执行看板」指标与风险清单；
 * 2. 步骤 5 伏笔台账的「从 KG 登记未填坑」按钮与点击后的回执。
 */

const apiMock = vi.hoisted(() => ({
  getProject: vi.fn(),
  listNovels: vi.fn(),
  listGenerations: vi.fn(),
  listSnapshots: vi.fn(),
  listSnapshotGroups: vi.fn(),
  listProposals: vi.fn(),
  listStoryCards: vi.fn(),
  getBackground: vi.fn(),
  listForeshadows: vi.fn(),
  listAlerts: vi.fn(),
  listNarrativeTiers: vi.fn(),
  listDistillJobs: vi.fn(),
  listStyleDistills: vi.fn(),
  getLlmConfig: vi.fn(),
  importForeshadowsFromKg: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {},
}));

vi.mock("vue-router", () => ({
  useRoute: () => ({ params: { id: "8" }, query: {} }),
  useRouter: () => ({ push: vi.fn() }),
}));

const FORESHADOW_PAYLOAD = {
  project_id: 8,
  entries: [],
  summary: { open: 0, resolved: 0, dropped: 0 },
  legacy: [],
  kg: {
    importable: [
      { question: "铜镜为什么裂了？", status: "open", first_chapter: 3 },
      { question: "师父去了哪？", status: "open", first_chapter: 4 },
    ],
    resolvable: [],
  },
};

function projectFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: 8,
    title: "测试项目",
    target_length: 5000,
    mode: "quick",
    status: "created",
    style_dims: null,
    outline: null,
    created_at: null,
    genre: "悬疑",
    style: "知乎盐选",
    premise: "测试前提",
    reference_novel_ids: [],
    plot_bible: null,
    generation_length: 3000,
    segment_count: 0,
    narrative_tier: null,
    style_narrative: null,
    active_novel_id: null,
    continuation_novel_id: null,
    ...overrides,
  };
}

function storyCardFixture(index: number) {
  return {
    id: index + 1,
    project_id: 8,
    kind: "character",
    title: `角色 ${index + 1}`,
    fields: {},
    enabled: true,
    sort_order: index,
    created_at: null,
    updated_at: null,
  };
}

function applyDefaults() {
  apiMock.getProject.mockResolvedValue(projectFixture());
  apiMock.listNovels.mockResolvedValue([]);
  apiMock.listGenerations.mockResolvedValue([]);
  apiMock.listSnapshots.mockResolvedValue({ items: [] });
  apiMock.listSnapshotGroups.mockResolvedValue({ items: [] });
  apiMock.listProposals.mockResolvedValue({ proposals: [] });
  apiMock.listStoryCards.mockResolvedValue({
    kinds: [],
    seeded: true,
    cards: Array.from({ length: 12 }, (_, index) => storyCardFixture(index)),
  });
  apiMock.getBackground.mockResolvedValue({ background: {} });
  apiMock.listForeshadows.mockResolvedValue(FORESHADOW_PAYLOAD);
  apiMock.listAlerts.mockResolvedValue({
    items: [],
    by_severity: { err: 0, warn: 0 },
    rules: {},
  });
  apiMock.listNarrativeTiers.mockResolvedValue({ tiers: [], default: "full" });
  apiMock.listDistillJobs.mockResolvedValue({ items: [] });
  apiMock.listStyleDistills.mockResolvedValue({ items: [] });
  apiMock.getLlmConfig.mockResolvedValue({
    provider: "test",
    model: "test-model",
    keys: {},
    providers: {},
    effective: {
      provider: "test",
      model: "test-model",
      label: "测试模型",
      is_fallback: false,
    },
    writer: null,
    assistant: null,
    distiller: null,
    kg_extract: null,
  });
  apiMock.importForeshadowsFromKg.mockResolvedValue({
    ...FORESHADOW_PAYLOAD,
    created: 2,
    titles: ["铜镜为什么裂了？", "师父去了哪？"],
  });
}

async function mountWorkspace() {
  const wrapper = mount(ProjectWorkspace, {
    shallow: true,
    global: { stubs: { RouterLink: true } },
  });
  // initialize 里有多次 await（项目 / 正文 / 台账 / 设定卡），多刷两轮。
  await flushPromises();
  await flushPromises();
  return wrapper;
}

describe("ProjectWorkspace", () => {
  beforeEach(() => {
    applyDefaults();
  });

  it("步骤 6 的写作执行看板展示进度与 KG 待登记风险", async () => {
    // plot_bible 非空 → inferStep() = 6。
    apiMock.getProject.mockResolvedValue(projectFixture({ plot_bible: {} }));
    const wrapper = await mountWorkspace();
    const text = wrapper.text();
    expect(wrapper.find('[aria-label="写作执行看板"]').exists()).toBe(true);
    expect(text).toContain("正文进度");
    expect(text).toContain("0 段");
    expect(text).toContain("KG 待登记 2");
    expect(text).toContain("12 / 12");
    expect(text).toContain("KG 还有 2 个未填坑没登记台账");
    expect(text).toContain("可以继续写第 1 段");

    wrapper.unmount();
  });

  it("步骤 5 伏笔台账可从 KG 一键登记未填坑", async () => {
    // 只有 premise → inferStep() = 5。
    const wrapper = await mountWorkspace();
    const button = wrapper
      .findAll("button")
      .find((item) => item.text().includes("从 KG 登记未填坑"));
    expect(button, "KG 登记按钮应出现在伏笔台账面板").toBeTruthy();
    expect(button!.text()).toContain("2 条");

    await button!.trigger("click");
    await flushPromises();

    expect(apiMock.importForeshadowsFromKg).toHaveBeenCalledWith(8);
    expect(wrapper.text()).toContain("已从 KG 登记 2 条未填坑");

    wrapper.unmount();
  });
});
