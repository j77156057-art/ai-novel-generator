import { computed, ref } from "vue";
import { api } from "@/api/client";
import type { LlmConfig, LlmRole, LlmRoleConfig } from "@/api/types";

/**
 * 旧后端（GET /api/settings/llm 不返回 writer / assistant）时，
 * 用单模型生效值合成该角色的展示配置。
 *
 * 这不是伪造双模型：调用方必须配合 `dualReady` 在界面上标注
 * 「后端未就绪，当前显示的是同一份单模型生效值」。
 */
function synthFromLegacy(c: LlmConfig): LlmRoleConfig {
  const ks = c.keys[c.effective.provider] ?? null;
  return {
    provider: c.effective.provider,
    model: c.effective.model,
    api_key_masked: ks?.masked ?? null,
    has_key: ks?.has ?? false,
    base_url: "",
    max_tokens: null,
    timeout: null,
    is_fallback: c.effective.is_fallback,
    label: c.effective.label,
  };
}

/**
 * 分角色配置的「内容指纹」比较：只比较会影响表单的字段。
 *
 * 用途：PUT 响应会返回整份 config（writer / assistant 两个对象都是新引用），
 * 若按引用变化就重置表单，保存 A 会把 B 未保存的编辑清掉——两个面板的
 * 状态独立性正是靠这个比较守住的。
 */
export function sameRoleConfig(a: LlmRoleConfig | null, b: LlmRoleConfig | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.provider === b.provider &&
    a.model === b.model &&
    a.max_tokens === b.max_tokens &&
    a.timeout === b.timeout &&
    a.has_key === b.has_key &&
    a.api_key_masked === b.api_key_masked &&
    a.is_fallback === b.is_fallback &&
    a.label === b.label
  );
}

/**
 * LLM 配置加载 + 分角色读取（生成 / 助手两个面板与徽标共用）。
 *
 * 每次调用都是独立实例（各自发一次只读 GET），互不影响。
 */
export function useLlmConfig() {
  const config = ref<LlmConfig | null>(null);
  const loading = ref(false);
  const error = ref<string | null>(null);

  /** true = 新后端（返回 writer/assistant 分角色配置）；false = 旧后端。 */
  const dualReady = computed(() => !!config.value?.writer && !!config.value?.assistant);

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      config.value = await api.getLlmConfig();
    } catch (err) {
      config.value = null;
      error.value = err instanceof Error ? err.message : String(err);
    } finally {
      loading.value = false;
    }
  }

  /** 取某角色当前生效配置；旧后端降级为单模型生效值（配合 dualReady 标注）。 */
  function roleConfig(role: LlmRole): LlmRoleConfig | null {
    const c = config.value;
    if (!c) return null;
    const direct = role === "writer" ? c.writer : c.assistant;
    return direct ?? synthFromLegacy(c);
  }

  return { config, loading, error, dualReady, load, roleConfig };
}
