<script setup lang="ts">
/**
 * KG 抽取配置面板（KgExtractPanel）——挂在现有「模型设置」页，**不是**第二个入口。
 *
 * 它配的是「知识图谱抽取用哪个渠道、哪几个模型」，与写正文的 Writer 无关：
 *   - 渠道：空串 = 跟随主渠道；其余 = 独立渠道（必须属于 allowed_providers）；
 *   - 模型链：留空 / 与渠道默认链一致 = 用渠道默认链；否则是抽取专用自定义链。
 *
 * 语义必须对用户可见（产品要求）：
 *   1. 选「跟随主渠道」时要写清含义（抽取随主渠道一起变，正文生成不受本块影响）；
 *   2. 换渠道后，输入框立刻回填**新渠道**的默认链（链的来源跟着渠道走）；
 *   3. 非法渠道不会静默回退主渠道——前端只渲染 allowed_providers 的按钮，
 *      后端另有 400 兜底；后端返回 valid=false 时直接把 error 显示出来。
 *
 * 表单状态自持；保存 / 错误映射上抛给页面（与 RoleModelPanel 同一套约定）。
 */
import { computed, reactive, ref, useId, watch } from "vue";
import type {
  LlmKgExtractConfig,
  LlmKgExtractSavePayload,
  LlmProviderMeta,
} from "@/api/types";

interface KgNotice {
  kind: "ok" | "err" | "warn";
  text: string;
}

const props = defineProps<{
  config: LlmKgExtractConfig | null;
  providers: Record<string, LlmProviderMeta>;
  busy: boolean;
  notice: KgNotice | null;
}>();

const emit = defineEmits<{
  save: [payload: LlmKgExtractSavePayload];
}>();

const uid = useId();

// ==================================================================
// 表单状态
// ==================================================================

interface KgFormState {
  /** 空串 = 跟随主渠道。 */
  provider: string;
  /** 模型链，逗号分隔；等于渠道默认链时保存为「不自定义」。 */
  chainText: string;
}

const form = reactive<KgFormState>({ provider: "", chainText: "" });
/** 最近一次同步进来的基线，用于「有未保存修改」判定与防误重置。 */
const synced = ref<LlmKgExtractConfig | null>(null);

function splitChain(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 某渠道的默认链：后端 channel_models → providers 元数据兜底。 */
function channelModels(provider: string): string[] {
  const key = provider || props.config?.main_provider || "";
  const fromServer = props.config?.channel_models?.[key];
  if (fromServer && fromServer.length) return fromServer;
  return props.providers[key]?.default_models ?? [];
}

function chainOf(c: LlmKgExtractConfig): string[] {
  return c.models_override.length ? c.models_override : c.models;
}

function applyConfig(c: LlmKgExtractConfig | null): void {
  if (!c) {
    form.provider = "";
    form.chainText = "";
    synced.value = null;
    return;
  }
  form.provider = c.provider;
  form.chainText = chainOf(c).join(", ");
  synced.value = c;
}

/**
 * 内容指纹：只比会影响本表单的字段。
 *
 * 保存**别的角色**时 PUT 也会返回整份 config（kg_extract 对象是同一份内容的新
 * 引用）；若按引用变化就重置，会把本面板未保存的编辑清掉。
 */
function sameKgExtract(c: LlmKgExtractConfig): boolean {
  const s = synced.value;
  if (!s) return false;
  return (
    s.provider === c.provider &&
    s.models_override.join(",") === c.models_override.join(",") &&
    s.models.join(",") === c.models.join(",") &&
    s.valid === c.valid &&
    s.is_fallback === c.is_fallback
  );
}

watch(
  () => props.config,
  (c) => {
    if (!c) return;
    if (synced.value && sameKgExtract(c)) return;
    applyConfig(c);
  },
  { immediate: true },
);

// ==================================================================
// 交互
// ==================================================================

function switchProvider(p: string): void {
  if (form.provider === p) return;
  form.provider = p;
  // 换渠道即换链的来源：立即回填新渠道的默认链（不保存就不生效）。
  // 否则会拿着 A 渠道的模型名去打 B 渠道（表现为莫名其妙的 404）。
  form.chainText = channelModels(p).join(", ");
}

/** 提交的链：与渠道默认链一致（或留空）→ 空数组 = 不写自定义链。 */
const payloadModels = computed<string[]>(() => {
  const chain = splitChain(form.chainText);
  if (!chain.length) return [];
  const base = channelModels(form.provider);
  return chain.join(",") === base.join(",") ? [] : chain;
});

const dirty = computed(() => {
  const s = synced.value;
  if (!s) return false;
  if (form.provider !== s.provider) return true;
  return payloadModels.value.join(",") !== s.models_override.join(",");
});

const mainLabel = computed(() => {
  const p = props.config?.main_provider ?? "";
  return props.providers[p]?.label ?? p;
});

const selectedLabel = computed(() => {
  const p = form.provider;
  if (!p) return mainLabel.value;
  return props.providers[p]?.label ?? p;
});

/** 渠道语义说明（对用户可见，不许含糊）。 */
const providerSemantics = computed(() => {
  if (!form.provider) {
    return `跟随主渠道（当前：${mainLabel.value}）：抽取沿用主渠道的渠道与模型链，主渠道换渠道时抽取自动跟着换。正文生成本来就与抽取配置无关。`;
  }
  return `独立渠道：只有知识图谱抽取走「${selectedLabel.value}」，写正文仍走主渠道「${mainLabel.value}」，正文渠道与预算不受影响。`;
});

/** 模型链来源说明。 */
const chainSourceNote = computed(() => {
  const c = props.config;
  if (payloadModels.value.length) {
    return "模型链来源：自定义（仅抽取使用，不改变写正文的候选链）。";
  }
  return `模型链来源：${selectedLabel.value} 的默认链。`;
});

function resetChain(): void {
  form.chainText = channelModels(form.provider).join(", ");
}

function submitSave(): void {
  emit("save", { provider: form.provider, models: payloadModels.value });
}
</script>

<template>
  <section class="kg-panel" aria-label="KG 抽取渠道">
    <header class="role-head">
      <div class="role-head-text">
        <h3 class="role-title">KG 抽取渠道（知识图谱抽取）</h3>
        <p class="role-purpose">
          实体 / 关系抽取用哪个渠道、哪几个模型。默认跟随主渠道；
          改成独立渠道后只有抽取走它，写正文的渠道与预算不变。
        </p>
      </div>
      <span v-if="dirty" class="tag warn">有未保存修改</span>
    </header>

    <!-- 当前实际生效值（来自抽取单例，不是表单里的选择） -->
    <div class="role-effective">
      <template v-if="config">
        <span class="tag" :class="!config.valid ? 'err' : config.is_fallback ? 'warn' : 'info'">
          {{ !config.valid ? "配置非法" : config.is_fallback ? "已降级 · 本地兜底" : "生效中" }}
        </span>
        <span class="mono effective-line">
          {{ config.effective_label }} · {{ config.models.join(" → ") || "—" }}
        </span>
        <span class="faint effective-line">
          总预算 {{ config.total_timeout }}s · 首字节 {{ config.first_byte_timeout }}s
        </span>
        <span class="faint effective-line">
          {{ config.follow_main ? "跟随主渠道" : "独立抽取渠道" }}
        </span>
      </template>
      <span v-else class="faint">尚未取到 KG 抽取配置。</span>
    </div>

    <div v-if="config && !config.valid" class="notice err">
      当前配置非法：{{ config.error }}
    </div>

    <!-- 渠道：跟随主渠道 + 后端允许的渠道（非法值没有入口，后端另有 400 兜底） -->
    <div class="field">
      <label>渠道 provider</label>
      <div class="chips">
        <button
          type="button"
          class="chip"
          :class="{ on: form.provider === '' }"
          :aria-pressed="form.provider === ''"
          @click="switchProvider('')"
        >
          跟随主渠道（{{ mainLabel }}）
        </button>
        <button
          v-for="p in config?.allowed_providers ?? []"
          :key="p"
          type="button"
          class="chip"
          :class="{ on: form.provider === p }"
          :aria-pressed="form.provider === p"
          @click="switchProvider(p)"
        >
          {{ providers[p]?.label ?? p }}
        </button>
      </div>
      <p class="faint provider-note">{{ providerSemantics }}</p>
    </div>

    <!-- 模型链：换渠道时已按新渠道默认链回填；留空 = 用渠道默认链 -->
    <div class="field">
      <label :for="`${uid}-chain`">
        模型链<span class="faint">（逗号分隔，首选在首位，其后为回退）</span>
      </label>
      <textarea
        :id="`${uid}-chain`"
        class="input"
        v-model="form.chainText"
        rows="2"
        spellcheck="false"
        :placeholder="channelModels(form.provider).join(', ')"
      ></textarea>
      <div class="chain-tools">
        <button type="button" class="btn ghost" @click="resetChain">填入渠道默认链</button>
        <span class="faint chain-note">{{ chainSourceNote }}</span>
      </div>
    </div>

    <div class="row tight role-actions">
      <button type="button" class="btn primary" :disabled="busy" @click="submitSave">
        {{ busy ? "保存中…" : "保存 KG 抽取配置" }}
      </button>
    </div>

    <div v-if="notice" class="notice" :class="notice.kind">{{ notice.text }}</div>
  </section>
</template>

<style scoped>
.kg-panel {
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  box-shadow: var(--shadow-sm);
  /* 与三个角色面板区分：用 info 色带 + 自己的标题文案双编码 */
  border-left: 3px solid var(--info);
}
.role-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--gap);
}
.role-head-text {
  min-width: 0;
}
.role-title {
  font-size: var(--fs-md);
  margin: 0;
}
.role-purpose {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  max-width: 72ch;
}
.role-effective {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--gap-sm);
  padding: 10px 12px;
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  font-size: var(--fs-sm);
}
.effective-line {
  font-size: var(--fs-xs);
}
.provider-note {
  font-size: var(--fs-xs);
  margin: 2px 0 0;
  line-height: 1.6;
}
.chips {
  display: flex;
  gap: var(--gap-sm);
  flex-wrap: wrap;
}
.chain-tools {
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
  flex-wrap: wrap;
}
.chain-note {
  font-size: var(--fs-xs);
  margin: 0;
  line-height: 1.6;
}
.role-actions {
  margin-top: 2px;
}
</style>
