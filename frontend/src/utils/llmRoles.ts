/**
 * 模型角色的单一事实来源（角色清单 + 每个角色的能力开关与文案）。
 *
 * 为什么单独一个文件而不是散在组件里：
 *   - 后端 PUT /api/settings/llm 对每个角色接受的字段是**fail-loud** 的
 *     （非 writer 传 models → 400；writer 传 max_tokens/timeout → 400）。
 *     前端若各写一份 `role === "writer" ? … : …`，加第三个角色时必然漏一处，
 *     漏一处就是运行时 400。这里集中成一张表，组件只读表。
 *   - SFC 的 `<script setup>` 不能 export 具名常量，所以放普通 .ts。
 */
import type { LlmRole } from "@/api/types";

/** 角色顺序 = 界面顺序（生成 → 助手 → 蒸馏）。 */
export const LLM_ROLES: readonly LlmRole[] = ["writer", "assistant", "distiller"] as const;

export interface LlmRoleMeta {
  /** 面板标题（界面上「这是哪一件事」的第一眼依据）。 */
  title: string;
  /** 这个角色干什么用的一句话说明。 */
  purpose: string;
  /** 徽标短名（页头徽标、保存提示复用）。 */
  short: string;
  /** 保存按钮文案（不用笼统的「保存」，要说明保存的是哪个角色）。 */
  saveLabel: string;
  /** 保存成功提示（指明「另外两个角色没被动过」）。 */
  savedText: string;
  /** 是否支持候选模型链（限流切换）——仅 writer。 */
  supportsChain: boolean;
  /** 是否支持改写 max_tokens / timeout——assistant 与 distiller。 */
  supportsLimits: boolean;
}

export const LLM_ROLE_META: Record<LlmRole, LlmRoleMeta> = {
  writer: {
    title: "生成模型（Writer）",
    purpose: "正文生成用的模型：向导里的续写、重生成、让模型出方案，都走它。",
    short: "生成模型",
    saveLabel: "保存生成模型",
    savedText: "生成模型已保存并生效（助手 / 蒸馏模型配置未改动）。",
    supportsChain: true,
    supportsLimits: false,
  },
  assistant: {
    title: "助手模型（Assistant）",
    purpose: "助手用的模型：只读问答、结构化诊断、提案与版本审核，都走它。",
    short: "助手模型",
    saveLabel: "保存助手模型",
    savedText: "助手模型已保存并生效（生成 / 蒸馏模型配置未改动）。",
    supportsChain: false,
    supportsLimits: true,
  },
  distiller: {
    title: "蒸馏模型（Distiller）",
    // 刻意写清「文风档案抽取用（长任务）」：否则会和项目里的「蒸馏档位」
    // （full / balanced / fast，采样与计算档位）混淆成同一件事。
    purpose:
      "文风档案抽取用：把参考语料压成可注入的文风特征。长任务，建议把 timeout 配大。"
      + "（注意：它决定「用哪个模型抽」，与「蒸馏档位」决定「抽多细」不是一回事。）",
    short: "蒸馏模型",
    saveLabel: "保存蒸馏模型",
    savedText: "蒸馏模型已保存并生效（生成 / 助手模型配置未改动）。",
    supportsChain: false,
    supportsLimits: true,
  },
};

/**
 * 需要用户填自定义端点（Base URL）的渠道。
 *
 * 「通用 OpenAI 接口」没有厂商默认地址可依赖——端点本身就是配置，
 * 所以只有它显示 Base URL 输入框；其余渠道有各自的默认地址，隐藏即可。
 */
export const CUSTOM_URL_PROVIDERS: readonly string[] = ["openai"] as const;

export function needsBaseUrl(provider: string): boolean {
  return CUSTOM_URL_PROVIDERS.includes(provider);
}

/** 按角色取能力/文案元数据；未知角色兜底成「只读、不可改」的安全形状。 */
export function roleMeta(role: LlmRole): LlmRoleMeta {
  return LLM_ROLE_META[role] ?? {
    title: role,
    purpose: "",
    short: role,
    saveLabel: "保存",
    savedText: "已保存。",
    supportsChain: false,
    supportsLimits: false,
  };
}
