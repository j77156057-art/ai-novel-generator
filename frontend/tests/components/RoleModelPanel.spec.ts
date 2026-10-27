import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import RoleModelPanel from "@/components/RoleModelPanel.vue";
import type { LlmProviderMeta, LlmRoleConfig } from "@/api/types";

/**
 * 模型设置面板（三个角色各一个实例）。
 *
 * 这个组件的界面纪律：
 * 1. **保存 / 测试只作用于当前角色**——按钮文案要写清楚（「保存生成模型」、
 *    「测试连接（只测此角色）」），不能让用户以为会连带改掉另外两个角色；
 * 2. **API key 默认脱敏**（input type=password），可切换显示；留空 = 不改已存 key；
 * 3. **候选模型链只有 Writer 有**（后端对 assistant / distiller 拒绝该字段）。
 */

const PROVIDERS: Record<string, LlmProviderMeta> = {
  minimax: {
    label: "MiniMax",
    key_hint: "在 MiniMax 开放平台拿 key",
    free_note: "国内站",
    default_models: ["MiniMax-M3.1-Flash"],
  },
  local: {
    label: "本地兜底",
    key_hint: "不需要 key",
    free_note: "",
    default_models: [],
  },
};

function config(p: Partial<LlmRoleConfig> = {}): LlmRoleConfig {
  return {
    provider: "minimax",
    model: "MiniMax-M3.1-Flash",
    api_key_masked: "sk-***ab",
    has_key: true,
    base_url: "https://api.minimaxi.com/v1",
    max_tokens: null,
    timeout: null,
    is_fallback: false,
    label: "MiniMax",
    ...p,
  } as LlmRoleConfig;
}

function mountPanel(props: Record<string, unknown> = {}) {
  return mount(RoleModelPanel, {
    props: {
      role: "writer",
      config: config(),
      providers: PROVIDERS,
      busy: null,
      notice: null,
      testResult: null,
      ...props,
    } as never,
  });
}

describe("RoleModelPanel", () => {
  it("按钮写明作用范围：保存只存这一个角色、测试只测这一个角色", () => {
    const w = mountPanel();
    expect(w.text()).toContain("测试连接（只测此角色）");
    expect(w.text()).toContain("保存生成模型");
  });

  it("标题与说明来自角色元数据（不是笼统的「模型设置」）", () => {
    const w = mountPanel();
    expect(w.text()).toContain("生成模型（Writer）");
    expect(w.text()).toContain("正文生成用的模型");
  });

  it("点「测试连接」上抛 test 事件，带上角色与当前表单值", async () => {
    const w = mountPanel();
    await w.findAll("button").find((b) => b.text().includes("测试连接"))!.trigger("click");
    const events = w.emitted("test");
    expect(events).toBeTruthy();
    const payload = events![0][0] as Record<string, unknown>;
    expect(payload.role).toBe("writer");
    expect(payload.provider).toBe("minimax");
    expect(payload.model).toBe("MiniMax-M3.1-Flash");
  });

  it("点「保存」上抛 save 事件（组件自己不调 API）", async () => {
    const w = mountPanel();
    await w.findAll("button").find((b) => b.text() === "保存生成模型")!.trigger("click");
    const events = w.emitted("save");
    expect(events).toBeTruthy();
    const payload = events![0][0] as Record<string, unknown>;
    expect(payload.role).toBe("writer");
  });

  it("API key 默认脱敏，可切换显示 / 隐藏", async () => {
    const w = mountPanel();
    const input = w.find('input[type="password"]');
    expect(input.exists()).toBe(true);
    const toggle = w.findAll("button").find((b) => b.text() === "显示")!;
    await toggle.trigger("click");
    expect(w.find('input[type="text"]').exists()).toBe(true);
    expect(w.findAll("button").some((b) => b.text() === "隐藏")).toBe(true);
  });

  it("已存 key 用脱敏串展示，并说清「留空 = 不改」", () => {
    const w = mountPanel();
    const text = w.text();
    expect(text).toContain("当前已保存（脱敏）");
    expect(text).toContain("sk-***ab");
    expect(text).toContain("留空 = 不改变已存 key");
  });

  it("候选模型链只有 Writer 有；助手角色明说自己是单模型配置", () => {
    const w = mountPanel({ role: "writer" });
    expect(w.text()).toContain("高级：候选模型链");

    const w2 = mountPanel({ role: "assistant" });
    expect(w2.text()).not.toContain("高级：候选模型链");
    expect(w2.text()).toContain("单模型配置");
  });

  it("测试结果成功 / 失败都把原文给出来（不只给一个色标）", () => {
    const ok = mountPanel({
      testResult: { ok: true, model: "MiniMax-M3.1-Flash", latency_ms: 1234, reply: "ok" },
    });
    expect(ok.text()).toContain("连通正常");
    expect(ok.text()).toContain("1234ms");

    const bad = mountPanel({ testResult: { ok: false, error: "密钥无效" } });
    expect(bad.text()).toContain("连通失败：密钥无效");
  });

  it("忙态时两个按钮都禁用（防重复提交）", () => {
    const w = mountPanel({ busy: "save" });
    const testBtn = w.findAll("button").find((b) => b.text().includes("测试连接"))!;
    // 忙态下按钮文案会变（不再是「保存生成模型」），所以按忙态文案找
    const saveBtn = w.findAll("button").find((b) => b.text().includes("保存中"))!;
    expect(saveBtn.exists()).toBe(true);
    expect(testBtn.attributes("disabled")).toBeDefined();
    expect(saveBtn.attributes("disabled")).toBeDefined();
    // 按钮文案也要变（按钮不能看着没事干）
    expect(saveBtn.text()).toContain("保存中");
  });
});
