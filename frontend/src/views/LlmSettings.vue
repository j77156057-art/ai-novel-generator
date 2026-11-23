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
import { computed, onMounted, reactive, ref } from "vue";
import { ApiError, api } from "@/api/client";
import { errorText } from "@/utils/errors";
import KgExtractPanel from "@/components/KgExtractPanel.vue";
import RoleModelPanel from "@/components/RoleModelPanel.vue";
import { useLlmConfig } from "@/composables/useLlmConfig";
import type {
  LlmKgExtractSavePayload,
  LlmRole,
  LlmRoleSavePayload,
  LlmRoleTestPayload,
  LlmTestResult,
  WebSearchConfig,
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

/**
 * KG 抽取配置（渠道 + 模型链）的忙态与提示。
 *
 * 单独一份：它不属于 writer / assistant / distiller 任何一个角色——
 * 保存它不改写正文的渠道与预算，也不该串到那三个面板的提示里。
 */
const kgBusy = ref(false);
const kgNotice = ref<RoleNotice | null>(null);

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

// ------------------------------------------------------------------
// 联网搜索（多渠道：Tavily / Brave / Serper / 博查 / Exa / SearXNG）
// ------------------------------------------------------------------
type WebSearchNotice = { kind: "ok" | "err" | "warn"; text: string };

const webSearch = ref<WebSearchConfig | null>(null);
const webSearchKey = ref("");
const webSearchBusy = ref(false);
const webSearchTesting = ref(false);
const webSearchNotice = ref<WebSearchNotice | null>(null);

/** 当前渠道的元数据（是否需要 key、默认端点、提示文案）。 */
const searchProviderMeta = computed(() => {
  const cfg = webSearch.value;
  if (!cfg) return null;
  return cfg.providers.find((p) => p.value === cfg.provider) ?? null;
});

/** 当前渠道是否「不需要 key、但必须有实例地址」（SearXNG）。 */
const searchUrlRequired = computed(() => {
  const meta = searchProviderMeta.value;
  return !!meta && !meta.needs_key;
});

/** 必填端点还没填：测试按钮禁用，并给一句可读提示。 */
const searchUrlMissing = computed(
  () => searchUrlRequired.value && !(webSearch.value?.base_url ?? "").trim(),
);

/** 换渠道：同步默认端点并清空未保存的 key，避免拿 A 家的 key 打 B 家。 */
function onSearchProviderChange(event: Event): void {
  const cfg = webSearch.value;
  if (!cfg) return;
  const value = (event.target as HTMLSelectElement).value;
  cfg.provider = value;
  const meta = cfg.providers.find((p) => p.value === value);
  cfg.base_url = meta?.default_base_url ?? "";
  webSearchKey.value = "";
  webSearchNotice.value = null;
}

async function loadWebSearch(): Promise<void> {
  try {
    webSearch.value = await api.getWebSearchConfig();
  } catch (err) {
    webSearchNotice.value = {
      kind: "err",
      text: `联网搜索配置加载失败：${errorText(err)}`,
    };
  }
}

async function saveWebSearch(): Promise<void> {
  const cfg = webSearch.value;
  if (!cfg || webSearchBusy.value) return;
  webSearchBusy.value = true;
  webSearchNotice.value = null;
  try {
    webSearch.value = await api.saveWebSearchConfig({
      enabled: cfg.enabled,
      provider: cfg.provider,
      api_key: webSearchKey.value.trim() || undefined,
      base_url: cfg.base_url,
      max_results: cfg.max_results,
      timeout: cfg.timeout,
      safe_search: cfg.safe_search,
    });
    webSearchKey.value = "";
    webSearchNotice.value = { kind: "ok", text: "联网搜索配置已保存。" };
  } catch (err) {
    webSearchNotice.value = {
      kind: "err",
      text: `保存失败：${errorText(err)}`,
    };
  } finally {
    webSearchBusy.value = false;
  }
}

async function testWebSearch(): Promise<void> {
  if (webSearchTesting.value) return;
  webSearchTesting.value = true;
  webSearchNotice.value = null;
  try {
    const res = await api.testWebSearch();
    const dropped = res.dropped ?? 0;
    const label = searchProviderMeta.value?.label ?? res.provider ?? "搜索";
    webSearchNotice.value = res.ok
      ? {
          kind: "ok",
          text:
            `${label} 可用：${res.latency_ms ?? "—"} ms，返回 ${res.count ?? 0} 条` +
            (dropped ? `（已过滤 ${dropped} 条低质结果）` : "") +
            "。",
        }
      : { kind: "err", text: `测试失败：${res.error || "未知错误"}` };
  } catch (err) {
    webSearchNotice.value = {
      kind: "err",
      text: `测试失败：${errorText(err)}`,
    };
  } finally {
    webSearchTesting.value = false;
  }
}

onMounted(loadWebSearch);

/**
 * 保存 KG 抽取配置：只提交 `kg_extract` 字段。
 *
 * 不夹带 provider / role——后端三种模式互斥（混用 400），而且带 provider
 * 会让人误以为这次顺带改了主渠道。
 */
async function onSaveKgExtract(payload: LlmKgExtractSavePayload): Promise<void> {
  kgBusy.value = true;
  kgNotice.value = null;
  try {
    config.value = await api.saveLlmConfig({ kg_extract: payload });
    kgNotice.value = {
      kind: "ok",
      text: "KG 抽取配置已保存并热生效；生成 / 助手 / 文风提炼与写正文的渠道、预算均未改动。",
    };
  } catch (err) {
    const text = err instanceof Error ? err.message : String(err);
    kgNotice.value = {
      kind: "err",
      text: roleUnsupported(err)
        ? `保存失败：当前后端不支持 KG 抽取独立配置（HTTP 422：${text}）。`
        : `保存失败：${text}`,
    };
  } finally {
    kgBusy.value = false;
  }
}
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">04 · 设置</div>
    <h1>模型设置</h1>
    <p>
      三个角色分开配：生成模型写正文、助手模型做问答与诊断、提炼模型抽文风档案。
      各自的渠道 / key / 模型互不影响，分别保存、分别测试。key 只存在本机数据库
      （.gitignore 保护），页面不显示明文。
      知识图谱抽取可以单独指定渠道与模型链（默认跟随生成渠道），不影响写正文。
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

  <!--
    KG 抽取渠道 + 模型链：放在同一页、三个角色面板之后（不新开第二个设置入口）。
    旧后端不返回 kg_extract（undefined）→ 整块不渲染，不伪造。
  -->
  <KgExtractPanel
    v-if="config?.kg_extract"
    :config="config.kg_extract"
    :providers="config.providers ?? {}"
    :busy="kgBusy"
    :notice="kgNotice"
    @save="onSaveKgExtract"
  />

  <!-- 联网搜索（多渠道）：只把检索词发出去，不发正文/设定。 -->
  <section class="card web-search-card">
    <div class="card-title">
      联网搜索
      <span class="hint">{{ searchProviderMeta?.label || "未选择渠道" }} · 只发检索词</span>
    </div>
    <p class="hint web-search-intro">
      开启后，助手在明确要求联网 / 查外部资料时会检索公开网页，并在回答里用 [1][2]
      标注来源。只发送检索词，不发送正文、设定或记忆；未配置时助手会如实说明不能联网。
      SearXNG 可以自建一个实例同时聚合 Google / Bing / Brave，适合不想逐家申请 key 的情况。
    </p>
    <div v-if="!webSearch" class="empty">读取配置…</div>
    <template v-else>
      <div class="web-search-grid">
        <label class="checkbox web-search-enable">
          <input type="checkbox" v-model="webSearch.enabled" />
          启用联网搜索
        </label>
        <div class="field">
          <label for="web-search-provider">渠道</label>
          <select
            id="web-search-provider"
            class="input"
            :value="webSearch.provider"
            @change="onSearchProviderChange"
          >
            <option v-for="p in webSearch.providers" :key="p.value" :value="p.value">
              {{ p.label }}
            </option>
            <option value="none">不启用</option>
          </select>
        </div>
        <div
          v-if="webSearch.provider !== 'none' && searchProviderMeta?.needs_key !== false"
          class="field web-search-key"
        >
          <label for="web-search-key">{{ searchProviderMeta?.label || "搜索" }} API Key</label>
          <input
            id="web-search-key"
            class="input"
            type="password"
            v-model="webSearchKey"
            :placeholder="
              webSearch.has_key
                ? `已保存 ${webSearch.api_key_masked}，留空不改`
                : '粘贴 API Key'
            "
            autocomplete="off"
          />
        </div>
        <div class="field web-search-base">
          <label for="web-search-base">
            Base URL 端点
            <span class="hint">{{ searchProviderMeta?.needs_key === false ? "（必填实例地址）" : "（留空用默认）" }}</span>
          </label>
          <input
            id="web-search-base"
            class="input"
            v-model="webSearch.base_url"
            :placeholder="searchProviderMeta?.default_base_url || 'http://127.0.0.1:8080'"
            autocomplete="off"
            spellcheck="false"
          />
        </div>
        <div class="field">
          <label for="web-search-max">每次结果数</label>
          <input
            id="web-search-max"
            class="input"
            type="number"
            min="1"
            max="30"
            v-model.number="webSearch.max_results"
          />
        </div>
        <div class="field">
          <label for="web-search-timeout">超时（秒）</label>
          <input
            id="web-search-timeout"
            class="input"
            type="number"
            min="3"
            max="20"
            v-model.number="webSearch.timeout"
          />
        </div>
        <label class="checkbox web-search-safe">
          <input type="checkbox" v-model="webSearch.safe_search" />
          安全搜索
        </label>
      </div>
      <p v-if="searchProviderMeta?.key_hint" class="hint web-search-hint">
        {{ searchProviderMeta.key_hint }}
      </p>
      <p
        v-if="searchUrlMissing"
        class="hint web-search-hint web-search-warn"
      >
        这个渠道不需要 key，但必须先填写实例地址；留空时无法发起搜索。
      </p>
      <div class="row web-search-actions">
        <button class="btn primary" :disabled="webSearchBusy" @click="saveWebSearch">
          {{ webSearchBusy ? "保存中…" : "保存联网搜索配置" }}
        </button>
        <button
          class="btn"
          :disabled="webSearchTesting || searchUrlMissing"
          @click="testWebSearch"
        >
          {{ webSearchTesting ? "测试中…" : `测试 ${searchProviderMeta?.label || "搜索"}` }}
        </button>
      </div>
      <div
        v-if="webSearchNotice"
        class="notice"
        :class="webSearchNotice.kind"
      >{{ webSearchNotice.text }}</div>
    </template>
  </section>
</template>

<style scoped>
.effective-banner {
  margin-bottom: var(--gap-lg);
  border-left: 3px solid var(--info);
  border-radius: var(--radius-md);
  background: var(--info-soft);
  color: var(--fg);
}
.panels {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: var(--gap-lg);
  align-items: start;
  margin-bottom: var(--gap-lg);
}
@media (max-width: 960px) {
  .panels {
    grid-template-columns: 1fr;
  }
}
.web-search-card {
  margin-top: var(--gap-lg);
}
.web-search-intro {
  margin-bottom: var(--gap-md);
  line-height: 1.7;
}
.web-search-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap-md);
  align-items: end;
}
.checkbox {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
  color: var(--fg-muted);
  cursor: pointer;
  user-select: none;
}
.checkbox input {
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
  cursor: pointer;
}
.web-search-key {
  grid-column: 1 / -1;
}
.web-search-enable,
.web-search-safe {
  min-height: 38px;
}
.web-search-actions {
  margin-top: var(--gap-md);
  align-items: center;
}
.web-search-hint {
  margin-top: var(--gap-sm);
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.web-search-warn {
  color: var(--warn, #b7791f);
}
.web-search-card .notice {
  margin-top: var(--gap-md);
}
@media (max-width: 720px) {
  .web-search-grid {
    grid-template-columns: 1fr;
  }
  .web-search-key {
    grid-column: auto;
  }
}
</style>
