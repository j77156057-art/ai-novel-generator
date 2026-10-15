<script setup lang="ts">
/**
 * 模型设置（/settings）——**唯一**的模型配置入口。
 *
 * 2026-10-14 合并：此前侧边栏「04 模型设置」（本路由页，旧版单模型表单）
 * 与「06 双模型设置」（全局弹窗）并存、功能重叠，且弹窗里才是真正能分角色
 * 配置的那套。合并后弹窗退役，本页直接渲染三个 RoleModelPanel
 * （生成 / 助手 / 蒸馏），旧版不带 role 的 legacy 保存表单整段删除。
 *
 * 三个角色的表单状态**各自独立**（RoleModelPanel 每实例一份 reactive），
 * 保存 A 绝不改动 B / C，也不清掉它们未保存的编辑。
 */
import { onMounted, reactive } from "vue";
import { ApiError, api } from "@/api/client";
import RoleModelPanel from "@/components/RoleModelPanel.vue";
import { useLlmConfig } from "@/composables/useLlmConfig";
import type {
  LlmRole,
  LlmRoleSavePayload,
  LlmRoleTestPayload,
  LlmTestResult,
} from "@/api/types";
import { LLM_ROLES, roleMeta } from "@/utils/llmRoles";

const { config, loading, error, rolesReady, missingRoles, load, roleConfig } = useLlmConfig();

interface RoleNotice {
  kind: "ok" | "err" | "warn";
  text: string;
}

/**
 * 每个角色独立的异步忙态 / 提示 / 测试结果（保存 A 不打扰 B / C 的提示）。
 * 用 LLM_ROLES 构造，不手写三个键的字典。
 */
function emptyRoleMap<T>(make: () => T): Record<LlmRole, T> {
  return Object.fromEntries(LLM_ROLES.map((r) => [r, make()])) as Record<LlmRole, T>;
}

const busy = reactive<Record<LlmRole, "save" | "test" | null>>(
  emptyRoleMap<"save" | "test" | null>(() => null),
);
const notices = reactive<Record<LlmRole, RoleNotice | null>>(
  emptyRoleMap<RoleNotice | null>(() => null),
);
const testResults = reactive<Record<LlmRole, LlmTestResult | null>>(
  emptyRoleMap<LlmTestResult | null>(() => null),
);

/** 旧后端（StrictRequest extra=forbid）遇到 role / max_tokens / timeout 会 422。 */
function roleUnsupported(err: unknown): boolean {
  return err instanceof ApiError && err.status === 422;
}

async function onSave(payload: LlmRoleSavePayload): Promise<void> {
  const role = payload.role;
  busy[role] = "save";
  notices[role] = null;
  try {
    config.value = await api.saveLlmConfig({
      provider: payload.provider,
      api_key: payload.api_key,
      model: payload.model,
      models: payload.models ?? null,
      role: payload.role,
      base_url: payload.base_url ?? null,
      max_tokens: payload.max_tokens ?? null,
      timeout: payload.timeout ?? null,
    });
    notices[role] = { kind: "ok", text: roleMeta(role).savedText };
  } catch (err) {
    const text = err instanceof Error ? err.message : String(err);
    notices[role] = {
      kind: "err",
      text: roleUnsupported(err)
        ? `保存失败：当前后端暂不支持分角色参数（HTTP 422：${text}）。`
        : `保存失败：${text}`,
    };
  } finally {
    busy[role] = null;
  }
}

async function onTest(payload: LlmRoleTestPayload): Promise<void> {
  const role = payload.role;
  busy[role] = "test";
  testResults[role] = null;
  try {
    testResults[role] = await api.testLlmConfig({
      provider: payload.provider,
      api_key: payload.api_key,
      model: payload.model,
      role: payload.role,
    });
  } catch (err) {
    const text = err instanceof Error ? err.message : String(err);
    testResults[role] = {
      ok: false,
      error: roleUnsupported(err)
        ? `当前后端暂不支持分角色测试（HTTP 422：${text}）。`
        : text,
    };
  } finally {
    busy[role] = null;
  }
}

onMounted(load);
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">04 · 设置</div>
    <h1>模型设置</h1>
    <p>
      三个角色分开配：生成模型写正文、助手模型做问答与诊断、蒸馏模型抽文风档案。
      各自的渠道 / key / 模型互不影响，分别保存、分别测试。key 只存在本机数据库
      （.gitignore 保护），页面不显示明文。
    </p>
  </header>

  <!-- 后端未就绪：如实标注「下面回显的是同一份单模型生效值」，不伪造三份配置 -->
  <div v-if="!rolesReady && config" class="notice warn" style="margin-bottom: var(--gap-lg); line-height: 1.8">
    <strong>后端未就绪：</strong>
    GET /api/settings/llm 尚未返回全部 <span class="mono">writer</span> /
    <span class="mono">assistant</span> / <span class="mono">distiller</span>
    分角色配置（缺：{{ missingRoles.join("、") }}）。缺的那个面板回显的是
    <strong>当前同一份单模型生效值</strong>（不是该角色的真实配置）；
    分别保存需要后端支持 <span class="mono">role</span> 参数——旧后端会以 422 拒绝。
  </div>
  <div v-if="error" class="notice err">配置加载失败：{{ error }}</div>
  <div v-if="loading" class="empty"><span class="spinner" /> 加载中…</div>

  <!-- 当前生效状态（legacy 单模型视图：Writer 单例的真实标签） -->
  <div v-if="config" class="notice info effective-banner">
    当前生成链路生效：<strong>{{ config.effective.label }}</strong> ·
    <span class="mono">{{ config.effective.model }}</span>
    <span v-if="config.effective.is_fallback" class="tag warn" style="margin-left: 8px">
      本地兜底
    </span>
  </div>

  <div v-if="config" class="panels">
    <RoleModelPanel
      v-for="role in LLM_ROLES"
      :key="role"
      :role="role"
      :title="roleMeta(role).title"
      :purpose="roleMeta(role).purpose"
      :config="roleConfig(role)"
      :providers="config.providers ?? {}"
      :busy="busy[role]"
      :notice="notices[role]"
      :test-result="testResults[role]"
      @save="onSave"
      @test="onTest"
    />
  </div>
</template>

<style scoped>
.effective-banner {
  margin-bottom: var(--gap-lg);
}
.panels {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: var(--gap);
  align-items: start;
  margin-bottom: var(--gap-lg);
}
@media (max-width: 960px) {
  .panels {
    grid-template-columns: 1fr;
  }
}
</style>
