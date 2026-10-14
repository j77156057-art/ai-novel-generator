<script setup lang="ts">
/**
 * 双模型设置弹窗（ModelSettingsDialog）。
 *
 * 产品硬性要求：
 *   - 生成模型（Writer）与助手模型（Assistant）**视觉上完全独立**的两个面板，
 *     不许共用同一份表单状态（各自是 RoleModelPanel 的独立实例）。
 *   - 分别保存、分别提示：「生成模型已保存」与「助手模型已保存」是两件事。
 *   - 界面上能看出两者当前**实际生效值**（含是否降级为本地兜底）。
 *   - 后端未就绪（GET 不返回 writer/assistant，或 PUT 拒绝 role）时明确标注，
 *     不伪造数据、不把失败说成成功。
 */
import { nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { ApiError, api } from "@/api/client";
import type {
  LlmRole,
  LlmRoleSavePayload,
  LlmRoleTestPayload,
  LlmTestResult,
} from "@/api/types";
import RoleModelPanel from "./RoleModelPanel.vue";
import { useLlmConfig } from "@/composables/useLlmConfig";

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const { config, loading, error, dualReady, load, roleConfig } = useLlmConfig();

interface RoleNotice {
  kind: "ok" | "err" | "warn";
  text: string;
}

/** 每个角色独立的异步忙态 / 提示 / 测试结果（保存 A 不打扰 B 的提示）。 */
const busy = reactive<Record<LlmRole, "save" | "test" | null>>({
  writer: null,
  assistant: null,
});
const notices = reactive<Record<LlmRole, RoleNotice | null>>({
  writer: null,
  assistant: null,
});
const testResults = reactive<Record<LlmRole, LlmTestResult | null>>({
  writer: null,
  assistant: null,
});

// ==================================================================
// 弹窗开关：打开时拉最新配置；Esc / 遮罩 / 关闭按钮退出
// ==================================================================

const dialogEl = ref<HTMLElement | null>(null);

watch(
  () => props.open,
  async (v) => {
    if (typeof document !== "undefined") {
      document.body.style.overflow = v ? "hidden" : "";
    }
    if (!v) return;
    // 每次打开都重新拉一次：另一个页面（如旧设置页）可能刚改过配置。
    await load();
    await nextTick();
    dialogEl.value?.focus();
  },
);

function onKeydown(e: KeyboardEvent): void {
  if (!props.open) return;
  if (e.key === "Escape") {
    emit("close");
    return;
  }
  // 轻量焦点圈闭：Tab 循环留在弹窗内（模态框的基本无障碍要求）。
  if (e.key === "Tab" && dialogEl.value) {
    const nodes = dialogEl.value.querySelectorAll<HTMLElement>(
      'button, input, select, textarea, summary, [href], [tabindex]:not([tabindex="-1"])',
    );
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === dialogEl.value)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  }
}

onMounted(() => {
  window.addEventListener("keydown", onKeydown);
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKeydown);
  if (typeof document !== "undefined") {
    document.body.style.overflow = "";
  }
});

// ==================================================================
// 保存 / 测试（分角色，互不影响）
// ==================================================================

/** 旧后端（StrictRequest extra=forbid）遇到 role/max_tokens/timeout 会 422。 */
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
      models: payload.models,
      role: payload.role,
      max_tokens: payload.max_tokens,
      timeout: payload.timeout,
    });
    notices[role] = {
      kind: "ok",
      text:
        role === "writer"
          ? "生成模型已保存并生效（助手模型配置未改动）。"
          : "助手模型已保存并生效（生成模型配置未改动）。",
    };
  } catch (err) {
    const text = err instanceof Error ? err.message : String(err);
    notices[role] = {
      kind: "err",
      text: roleUnsupported(err)
        ? `保存失败：当前后端暂不支持分角色参数（HTTP 422：${text}）。` +
          "等后端升级后重试；旧版单模型设置仍在「模型设置」页可用。"
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
</script>

<template>
  <div v-if="open" class="overlay" @click.self="emit('close')">
    <div
      ref="dialogEl"
      class="dialog"
      role="dialog"
      aria-modal="true"
      aria-label="模型设置：生成模型与助手模型分开配置"
      tabindex="-1"
    >
      <header class="dialog-head">
        <div>
          <h2 class="dialog-title">模型设置 · 生成与助手分开配置</h2>
          <p class="dialog-sub">
            两个面板各自独立：<strong>左 = 生成模型（Writer，写正文用）</strong>、
            <strong>右 = 助手模型（Assistant，问答 / 诊断 / 审核用）</strong>。
            表单互不共用，分别保存、分别测试，互不影响。
          </p>
        </div>
        <button type="button" class="btn ghost dialog-close" @click="emit('close')">
          关闭 ✕
        </button>
      </header>

      <div v-if="!dualReady" class="notice warn dual-warn">
        <strong>后端未就绪：</strong>
        GET /api/settings/llm 尚未返回 <span class="mono">writer</span> /
        <span class="mono">assistant</span> 分角色配置。下面两个面板回显的是
        <strong>当前同一份单模型生效值</strong>（不是两个角色的真实配置）；
        分别保存需要后端支持 <span class="mono">role</span> 参数——旧后端会以 422
        拒绝，届时请等后端升级。旧版单模型设置仍在「模型设置」页可用。
      </div>
      <div v-if="error" class="notice err">配置加载失败：{{ error }}</div>
      <div v-if="loading" class="empty"><span class="spinner" /> 加载中…</div>

      <div v-else class="panels">
        <RoleModelPanel
          role="writer"
          title="面板 A · 生成模型（Writer）"
          purpose="正文生成用的模型：向导里的续写、重生成、让模型出方案，都走它。"
          :config="roleConfig('writer')"
          :providers="config?.providers ?? {}"
          :busy="busy.writer"
          :notice="notices.writer"
          :test-result="testResults.writer"
          @save="onSave"
          @test="onTest"
        />
        <RoleModelPanel
          role="assistant"
          title="面板 B · 助手模型（Assistant）"
          purpose="助手用的模型：只读问答、结构化诊断、提案与版本审核，都走它。"
          :config="roleConfig('assistant')"
          :providers="config?.providers ?? {}"
          :busy="busy.assistant"
          :notice="notices.assistant"
          :test-result="testResults.assistant"
          @save="onSave"
          @test="onTest"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(28, 26, 23, 0.45);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding: var(--gap-lg) var(--gap);
  overflow: auto;
  z-index: 100;
}
.dialog {
  background: var(--bg);
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  width: 100%;
  max-width: 1120px;
  padding: var(--gap-lg);
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  outline: none;
}
.dialog-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--gap);
}
.dialog-title {
  font-size: var(--fs-lg);
  margin: 0;
}
.dialog-sub {
  margin: 6px 0 0;
  font-size: var(--fs-sm);
  color: var(--fg-muted);
  max-width: 76ch;
}
.dialog-close {
  flex: none;
}
.dual-warn {
  line-height: 1.7;
}
.panels {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap);
  align-items: start;
}
@media (max-width: 960px) {
  .panels {
    grid-template-columns: 1fr;
  }
}
</style>
