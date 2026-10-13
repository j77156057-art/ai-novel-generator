<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "@/api/client";
import type { LlmConfig, LlmTestResult } from "@/api/types";

const config = ref<LlmConfig | null>(null);
const loading = ref(false);
const saving = ref(false);
const testing = ref(false);

// 表单状态
const provider = ref("zhipu");
const apiKey = ref(""); // 空 = 保留已保存的 key
const showKey = ref(false);
const model = ref("");
const chainText = ref(""); // 高级：限流切换链，逗号分隔

const notice = ref<{ kind: "ok" | "err" | "warn"; text: string } | null>(null);
const testResult = ref<LlmTestResult | null>(null);

const providerKeys = computed(() => Object.keys(config.value?.providers ?? {}));
const meta = computed(() => config.value?.providers[provider.value] ?? null);
const keyStatus = computed(() => config.value?.keys[provider.value] ?? null);
const effective = computed(() => config.value?.effective ?? null);

const modelOptions = computed(() => {
  const opts = new Set<string>(meta.value?.default_models ?? []);
  if (model.value) opts.add(model.value);
  for (const m of chainText.value.split(",")) if (m.trim()) opts.add(m.trim());
  return [...opts];
});

async function refresh() {
  loading.value = true;
  try {
    config.value = await api.getLlmConfig();
    syncFormFromConfig();
  } catch (err) {
    notice.value = { kind: "err", text: err instanceof Error ? err.message : String(err) };
  } finally {
    loading.value = false;
  }
}

function syncFormFromConfig() {
  const c = config.value;
  if (!c) return;
  provider.value = c.provider;
  model.value = c.model;
  // GET 不返回已存切换链，用该渠道默认链填充（用户可在高级区改）
  chainText.value = (c.providers[provider.value]?.default_models ?? []).join(", ");
  apiKey.value = "";
  testResult.value = null;
}

function switchProvider(p: string) {
  provider.value = p;
  apiKey.value = "";
  testResult.value = null;
  notice.value = null;
  const m = config.value?.providers[p];
  model.value = m?.default_models?.[0] ?? "";
  chainText.value = (m?.default_models ?? []).join(", ");
}

async function save() {
  saving.value = true;
  notice.value = null;
  try {
    const models = chainText.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    config.value = await api.saveLlmConfig({
      provider: provider.value,
      api_key: apiKey.value.trim() || null,
      model: model.value.trim() || null,
      models: models.length ? models : null,
    });
    apiKey.value = "";
    notice.value = {
      kind: "ok",
      text: `已保存并切换：${config.value.effective.label} · ${config.value.effective.model}`
        + (config.value.effective.is_fallback ? "（当前为本地兜底：key 为空或无效）" : ""),
    };
  } catch (err) {
    notice.value = { kind: "err", text: err instanceof Error ? err.message : String(err) };
  } finally {
    saving.value = false;
  }
}

async function test() {
  testing.value = true;
  testResult.value = null;
  try {
    testResult.value = await api.testLlmConfig({
      provider: provider.value,
      api_key: apiKey.value.trim() || null,
      model: model.value.trim() || null,
    });
  } catch (err) {
    testResult.value = { ok: false, error: err instanceof Error ? err.message : String(err) };
  } finally {
    testing.value = false;
  }
}

onMounted(refresh);
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">04 · 设置</div>
    <h1>模型设置</h1>
    <p>
      选一个免费大模型渠道、填入 API key，保存后立即生效。key 只存在本机数据库（.gitignore 保护），
      页面不显示明文；限流时自动在切换链里换下一个模型。
    </p>
  </header>

  <div v-if="loading" class="empty"><span class="spinner" /> 加载中…</div>

  <template v-else-if="config">
    <!-- 当前生效状态 -->
    <div class="notice info" style="margin-bottom: var(--gap-lg)">
      当前生效：<strong>{{ effective?.label }}</strong> · <span class="mono">{{ effective?.model }}</span>
      <span v-if="effective?.is_fallback" class="tag warn" style="margin-left: 8px">本地兜底</span>
    </div>

    <!-- 渠道选择 -->
    <section class="card">
      <div class="card-title">① 选择渠道</div>
      <div class="chips">
        <button
          v-for="(m, key) in config.providers"
          :key="key"
          class="chip"
          :class="{ on: provider === key }"
          @click="switchProvider(key)"
        >
          {{ m.label }}
        </button>
      </div>
      <p class="muted" style="font-size: var(--fs-sm); margin-top: 10px">
        {{ meta?.free_note }}
      </p>
    </section>

    <!-- API Key -->
    <section class="card" v-if="provider !== 'local'">
      <div class="card-title">② API Key</div>
      <p class="muted" style="font-size: var(--fs-sm); margin-bottom: 10px">
        获取方式：{{ meta?.key_hint }}
        <template v-if="keyStatus?.has">
          ；已保存 <span class="mono">{{ keyStatus.masked }}</span>（留空保存 = 不改变已有 key）
        </template>
      </p>
      <div class="key-row">
        <input
          class="input"
          :type="showKey ? 'text' : 'password'"
          v-model="apiKey"
          :placeholder="keyStatus?.has ? '已保存，输入新 key 可替换' : '粘贴 API key'"
          autocomplete="off"
        />
        <button class="btn ghost" @click="showKey = !showKey">
          {{ showKey ? "隐藏" : "显示" }}
        </button>
      </div>
    </section>

    <!-- 模型选择 -->
    <section class="card">
      <div class="card-title">③ 模型</div>
      <div class="field">
        <label>默认模型</label>
        <input
          class="input"
          v-model="model"
          list="model-options"
          :placeholder="meta?.default_models?.[0] ?? '模型 id'"
        />
        <datalist id="model-options">
          <option v-for="m in modelOptions" :key="m" :value="m" />
        </datalist>
      </div>
      <details class="advanced">
        <summary>高级：限流切换链（逗号分隔，按序切换）</summary>
        <textarea class="input" v-model="chainText" rows="2" />
      </details>
    </section>

    <!-- 操作 -->
    <div class="row" style="margin-bottom: var(--gap-lg)">
      <button class="btn" :disabled="testing" @click="test">
        {{ testing ? "测试中…" : "测试连通性" }}
      </button>
      <button class="btn primary" :disabled="saving" @click="save">
        {{ saving ? "保存中…" : "保存并应用" }}
      </button>
    </div>

    <div v-if="testResult" :class="['notice', testResult.ok ? 'ok' : 'err']">
      <template v-if="testResult.ok">
        连通正常 · 模型 <span class="mono">{{ testResult.model }}</span> ·
        延迟 {{ testResult.latency_ms }}ms · 回复：「{{ testResult.reply }}」
      </template>
      <template v-else>连通失败：{{ testResult.error }}</template>
    </div>
    <div v-if="notice" :class="['notice', notice.kind]" style="margin-top: 12px">
      {{ notice.text }}
    </div>
  </template>
</template>

<style scoped>
.key-row {
  display: flex;
  gap: 10px;
  align-items: stretch;
}
.key-row .input {
  flex: 1;
}
.advanced {
  margin-top: 12px;
  font-size: var(--fs-sm);
}
.advanced summary {
  cursor: pointer;
  color: var(--accent-ink);
  font-weight: 600;
  margin-bottom: 8px;
}
.advanced summary:hover {
  color: var(--accent);
}
</style>
