<script setup lang="ts">
/**
 * 单个模型角色的配置面板（RoleModelPanel）。
 *
 * 设计约束（来自产品需求）：
 *   1. 三个面板（writer / assistant / distiller）**不许共用同一份表单状态**——
 *      本组件以三个独立实例使用，每个实例自带一份 reactive 表单，
 *      改 A 不可能影响 B 的未保存值。
 *   2. 本组件只持有表单与展示状态；保存 / 测试的 API 调用与错误映射
 *      上抛给父级（模型设置页），结果以 props 回流。
 *   3. key 输入框默认脱敏（type=password），可切换显示；留空 = 不改变已存 key。
 *   4. 降级状态（is_fallback）用「文字 + 色标」双编码，不只靠颜色。
 *   5. 角色能力（能否改候选链 / 能否改 max_tokens 与 timeout）由
 *      `utils/llmRoles.ts` 的映射表决定，**不在本组件里写
 *      `role === "writer" ? … : …`** —— 那种二元判断加第三个角色时必漏。
 */
import { computed, reactive, ref, useId, watch } from "vue";
import type {
  LlmProviderMeta,
  LlmRole,
  LlmRoleConfig,
  LlmRoleSavePayload,
  LlmRoleTestPayload,
  LlmTestResult,
} from "@/api/types";
import { needsBaseUrl, roleMeta } from "@/utils/llmRoles";

interface RoleNotice {
  kind: "ok" | "err" | "warn";
  text: string;
}

const props = defineProps<{
  role: LlmRole;
  /** 面板标题，如「生成模型（Writer）」。缺省用角色元数据里的标题。 */
  title?: string;
  /** 这个角色干什么用的一句话说明。缺省用角色元数据里的说明。 */
  purpose?: string;
  /** 当前生效配置（null = 还没取到）。 */
  config: LlmRoleConfig | null;
  providers: Record<string, LlmProviderMeta>;
  /** 父级持有的异步忙态："save" | "test" | null。 */
  busy: "save" | "test" | null;
  /** 父级持有的保存 / 测试结果提示。 */
  notice: RoleNotice | null;
  testResult: LlmTestResult | null;
}>();

const emit = defineEmits<{
  save: [payload: LlmRoleSavePayload];
  test: [payload: LlmRoleTestPayload];
}>();

// 每个实例独立的 useId：同屏三个面板，label/for 与 datalist id 不能撞。
const uid = useId();

/** 本角色的能力开关与文案（单一事实来源在 utils/llmRoles.ts）。 */
const meta = computed(() => roleMeta(props.role));
const supportsChain = computed(() => meta.value.supportsChain);
const supportsLimits = computed(() => meta.value.supportsLimits);
const saveLabel = computed(() => meta.value.saveLabel);
const heading = computed(() => props.title ?? meta.value.title);
const purposeText = computed(() => props.purpose ?? meta.value.purpose);

// ==================================================================
// 表单状态（每实例一份，绝不共享）
// ==================================================================

interface RoleFormState {
  provider: string;
  /** 空 = 保留已存 key（脱敏输入框的约定）。 */
  apiKey: string;
  model: string;
  /** 候选模型链，逗号分隔（限流时按序切换）。 */
  chainText: string;
  /** 自定义端点（仅「通用 OpenAI 接口」等无厂商默认地址的渠道可见）。 */
  baseUrl: string;
  maxTokens: string;
  timeout: string;
  /** 思考档位（取值由渠道决定；空 = 不发这个字段）。 */
  thinking: string;
  showKey: boolean;
}

function createForm(): RoleFormState {
  return {
    provider: "local",
    apiKey: "",
    model: "",
    chainText: "",
    baseUrl: "",
    maxTokens: "",
    timeout: "",
    thinking: "",
    showKey: false,
  };
}

const form = reactive(createForm());
/** 最近一次从 config 同步进来的「基线」，用于脏值比较。 */
const synced = ref<LlmRoleConfig | null>(null);
/** 仅客户端校验错误（API 结果的提示走 props.notice）。 */
const localError = ref<string | null>(null);

function applyConfig(c: LlmRoleConfig | null): void {
  localError.value = null;
  if (!c) {
    Object.assign(form, createForm());
    synced.value = null;
    return;
  }
  form.provider = c.provider || "local";
  form.apiKey = "";
  form.model = c.model ?? "";
  form.baseUrl = c.base_url ?? "";
  form.maxTokens = c.max_tokens != null ? String(c.max_tokens) : "";
  form.timeout = c.timeout != null ? String(c.timeout) : "";
  // GET 不返回已存切换链：用该渠道默认链填充（与旧版模型设置页一致，
  // 用户可在高级区改；保存时按文本原样提交）。
  form.chainText = (props.providers[form.provider]?.default_models ?? []).join(", ");
  form.showKey = false;
  synced.value = c;
}

// config 到位后同步表单；内容没变（如同一次 PUT 响应里另一个角色换了
// 对象引用）时不重置——否则保存 A 会清掉 B 未保存的编辑。
watch(
  () => props.config,
  (c) => {
    if (!c) return;
    if (synced.value && sameRole(c)) return;
    applyConfig(c);
  },
  { immediate: true },
);

function sameRole(c: LlmRoleConfig): boolean {
  const s = synced.value;
  if (!s) return false;
  return (
    s.provider === c.provider &&
    s.model === c.model &&
    s.base_url === c.base_url &&
    s.max_tokens === c.max_tokens &&
    s.timeout === c.timeout &&
    s.has_key === c.has_key &&
    s.api_key_masked === c.api_key_masked &&
    s.is_fallback === c.is_fallback &&
    s.label === c.label
  );
}

// ==================================================================
// 交互
// ==================================================================

function switchProvider(p: string): void {
  if (form.provider === p) return;
  form.provider = p;
  // 换渠道即换该渠道的默认模型与默认链；key 不跨渠道复用，清空重填。
  form.apiKey = "";
  form.model = props.providers[p]?.default_models?.[0] ?? "";
  form.chainText = (props.providers[p]?.default_models ?? []).join(", ");
  // 端点也不跨渠道复用：换回需要自定义端点的渠道时让用户重新填，
  // 否则会拿着 A 渠道的地址去打 B 渠道（表现为莫名其妙的 404）。
  form.baseUrl = "";
  localError.value = null;
}

function splitChain(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** 当前渠道是否要求自定义端点（通用 OpenAI 接口）。 */
const showBaseUrl = computed(() => needsBaseUrl(form.provider));

/**
 * 这个渠道 + 模型能用的思考档位。
 *
 * 由后端下发（`providers[渠道].thinking`），因为**各家控制方式不同**：
 *   MiniMax M3.1 → 只能调档（关不掉；省略 = 最强最慢）
 *   DeepSeek 类   → 能关
 *   其它          → 未验证，不提供（按了没反应的开关比没有更糟）
 * 拿不到或 mode === "none" → null ⇒ 界面上不显示这一块。
 */
const thinkingOptions = computed(() => {
  const ctl = props.providers[form.provider]?.thinking;
  if (!ctl || ctl.mode === "none" || !ctl.levels?.length) return null;
  return ctl;
});

/** 档位的「人话」标签：不出现 reasoning_effort 这种内部字段名。
 *  措辞对着 2026-10-26 的真跑数据：low 最稳最省、medium 实测更快、high 字数更接近目标。 */
const THINKING_LABELS: Record<string, string> = {
  low: "省思考（最稳，适合写正文）",
  medium: "平衡（实测更快）",
  high: "更细（字数更接近目标）",
  xhigh: "很仔细",
  max: "最强（最慢最贵，省略即此档）",
  off: "关闭（最快）",
  default: "默认（由模型决定）",
};

const thinkingLabel = (level: string): string => THINKING_LABELS[level] ?? level;

/** 解析非负整数输入：空 → null（不改）；非法 → undefined（调用方报错）。 */
function parseCount(raw: string): number | null | undefined {
  const t = raw.trim();
  if (!t) return null;
  const n = Number(t);
  if (!Number.isInteger(n) || n <= 0) return undefined;
  return n;
}

const modelOptions = computed(() => {
  const opts = new Set<string>(props.providers[form.provider]?.default_models ?? []);
  if (form.model.trim()) opts.add(form.model.trim());
  for (const m of splitChain(form.chainText)) opts.add(m);
  return [...opts];
});

/** 有未保存修改：与同步基线比较（key 输入框非空即视为待保存）。 */
const dirty = computed(() => {
  const s = synced.value;
  if (form.provider !== (s?.provider ?? "")) return true;
  if (form.model.trim() !== (s?.model ?? "")) return true;
  if (form.apiKey.trim() !== "") return true;
  // 只比较该角色**可改**的字段：只读字段不存在「未保存」。
  if (supportsChain.value) {
    const chain = splitChain(form.chainText).join(",");
    const baseChain = splitChain(
      (props.providers[form.provider]?.default_models ?? []).join(","),
    ).join(",");
    if (chain !== baseChain) return true;
  }
  // 思考档位：只在该渠道+模型支持时参与脏值比较。
  if (thinkingOptions.value && (form.thinking || "") !== (s?.thinking ?? "")) return true;
  if (supportsLimits.value) {
    if (form.maxTokens.trim() !== (s?.max_tokens != null ? String(s.max_tokens) : "")) return true;
    if (form.timeout.trim() !== (s?.timeout != null ? String(s.timeout) : "")) return true;
  }
  if (showBaseUrl.value && form.baseUrl.trim() !== (s?.base_url ?? "")) return true;
  return false;
});

function submitSave(): void {
  localError.value = null;
  const payload: LlmRoleSavePayload = {
    role: props.role,
    provider: form.provider,
    api_key: form.apiKey.trim() || null,
    model: form.model.trim() || null,
  };
  // base_url 只给「需要自定义端点」的渠道带：其余渠道带上去后端虽然不报错，
  // 但会覆盖掉该渠道的默认地址（留空即覆盖成空 = 静默失效）。
  if (showBaseUrl.value) {
    payload.base_url = form.baseUrl.trim() || null;
  }
  if (supportsChain.value) {
    const chain = splitChain(form.chainText);
    payload.models = chain.length ? chain : null;
  }
  // 思考档位：只在该渠道+模型支持时带上（空 = 不改变）。
  if (thinkingOptions.value) {
    payload.thinking = form.thinking || null;
  }
  if (supportsLimits.value) {
    const maxTokens = parseCount(form.maxTokens);
    if (maxTokens === undefined) {
      localError.value = "max_tokens 必须是正整数（留空 = 不改变）。";
      return;
    }
    const timeout = parseCount(form.timeout);
    if (timeout === undefined) {
      localError.value = "timeout 必须是正整数（秒；留空 = 不改变）。";
      return;
    }
    payload.max_tokens = maxTokens;
    payload.timeout = timeout;
  }
  emit("save", payload);
}

function submitTest(): void {
  localError.value = null;
  emit("test", {
    role: props.role,
    provider: form.provider,
    api_key: form.apiKey.trim() || null,
    model: form.model.trim() || null,
  });
}
</script>

<template>
  <section class="role-panel" :class="role" :aria-label="heading">
    <header class="role-head">
      <div class="role-head-text">
        <h3 class="role-title">{{ heading }}</h3>
        <p class="role-purpose">{{ purposeText }}</p>
      </div>
      <span v-if="dirty" class="tag warn">有未保存修改</span>
    </header>

    <!-- 当前实际生效值（含是否降级为本地兜底：文字 + 色标双编码） -->
    <div class="role-effective">
      <template v-if="config">
        <span class="tag" :class="config.is_fallback ? 'warn' : 'info'">
          {{ config.is_fallback ? "已降级 · 本地兜底" : "生效中" }}
        </span>
        <span class="mono effective-line">{{ config.label }} · {{ config.model }}</span>
        <span class="faint effective-line">
          max_tokens {{ config.max_tokens ?? "—" }} · timeout {{ config.timeout ?? "—" }}s
        </span>
        <span v-if="config.base_url" class="faint effective-line">
          端点 <span class="mono">{{ config.base_url }}</span>
        </span>
      </template>
      <span v-else class="faint">尚未取到该角色配置。</span>
    </div>

    <!-- 渠道 -->
    <div class="field">
      <label>渠道 provider</label>
      <div class="chips">
        <button
          v-for="(m, key) in providers"
          :key="key"
          type="button"
          class="chip"
          :class="{ on: form.provider === key }"
          :aria-pressed="form.provider === key"
          @click="switchProvider(key)"
        >
          {{ m.label }}
        </button>
      </div>
      <p class="faint provider-note">{{ providers[form.provider]?.free_note ?? "" }}</p>
    </div>

    <!-- 自定义端点：只有「通用 OpenAI 接口」这类无厂商默认地址的渠道需要 -->
    <div v-if="showBaseUrl" class="field">
      <label :for="`${uid}-baseurl`">
        Base URL 端点<span class="faint">（留空 = 用后端该渠道的默认地址）</span>
      </label>
      <input
        :id="`${uid}-baseurl`"
        class="input"
        v-model="form.baseUrl"
        autocomplete="off"
        spellcheck="false"
        placeholder="https://your-host/v1"
      />
      <p class="faint chain-note">
        获取方式：{{ providers[form.provider]?.key_hint ?? "" }}
      </p>
    </div>

    <!-- API key（脱敏显示，可切换；空 = 不改） -->
    <div v-if="form.provider !== 'local'" class="field">
      <label :for="`${uid}-key`">API key（留空 = 不改变已存 key）</label>
      <div class="key-row">
        <input
          :id="`${uid}-key`"
          class="input"
          :type="form.showKey ? 'text' : 'password'"
          v-model="form.apiKey"
          autocomplete="off"
          :placeholder="config?.has_key ? `已保存 ${config.api_key_masked}，输入新 key 可替换` : '粘贴 API key'"
        />
        <button type="button" class="btn ghost" @click="form.showKey = !form.showKey">
          {{ form.showKey ? "隐藏" : "显示" }}
        </button>
      </div>
      <span v-if="config?.has_key" class="faint key-saved">
        当前已保存（脱敏）：<span class="mono">{{ config.api_key_masked }}</span>
      </span>
      <p v-if="!showBaseUrl" class="faint chain-note">
        获取方式：{{ providers[form.provider]?.key_hint ?? "" }}
      </p>
    </div>

    <!-- 模型 ID -->
    <div class="field">
      <label :for="`${uid}-model`">模型 ID model</label>
      <input
        :id="`${uid}-model`"
        class="input"
        v-model="form.model"
        :list="`${uid}-models`"
        :placeholder="providers[form.provider]?.default_models?.[0] ?? '模型 id'"
      />
      <datalist :id="`${uid}-models`">
        <option v-for="m in modelOptions" :key="m" :value="m" />
      </datalist>
    </div>

    <!-- 候选模型链：仅 Writer 有（限流切换）；助手 / 蒸馏是单模型配置 -->
    <details v-if="supportsChain" class="advanced">
      <summary>高级：候选模型链（逗号分隔，限流时按序切换）</summary>
      <textarea class="input" v-model="form.chainText" rows="2"></textarea>
    </details>
    <div v-else class="chain-locked">
      <span class="blk-head">候选模型链</span>
      <p class="faint chain-note">
        {{ meta.short }}是<strong>单模型配置</strong>，没有限流切换链
        （后端不接受该字段）——同一个任务只用一个模型跑到底，避免两次问出两个答案。
      </p>
    </div>

    <!-- max_tokens / timeout：助手 / 蒸馏可改；Writer 两项由生成基线固定（只读展示） -->
    <div class="grid-2">
      <div class="field">
        <label :for="`${uid}-maxtok`">max_tokens{{ supportsLimits ? "（留空 = 不改变）" : "" }}</label>
        <input
          v-if="supportsLimits"
          :id="`${uid}-maxtok`"
          class="input"
          v-model="form.maxTokens"
          inputmode="numeric"
          placeholder="如 1024"
        />
        <input v-else :id="`${uid}-maxtok`" class="input" :value="form.maxTokens" disabled />
        <span v-if="!supportsLimits" class="faint limit-note">
          Writer 的 max_tokens / timeout 由生成基线固定，不在此改写；此处显示单例真实值。
        </span>
      </div>
      <div class="field">
        <label :for="`${uid}-timeout`">timeout 秒{{ supportsLimits ? "（留空 = 不改变）" : "" }}</label>
        <input
          v-if="supportsLimits"
          :id="`${uid}-timeout`"
          class="input"
          v-model="form.timeout"
          inputmode="numeric"
          placeholder="如 60"
        />
        <input v-else :id="`${uid}-timeout`" class="input" :value="form.timeout" disabled />
        <span v-if="!supportsLimits" class="faint limit-note">
          基线值（chat 120s / stream 180s），只读。
        </span>
        <span v-else-if="role === 'distiller'" class="faint limit-note">
          蒸馏是长任务：语料越多越慢，建议 ≥180s。
        </span>
      </div>
    </div>

    <!-- 思考档位：只在该渠道+模型支持时出现（后端下发能力，前端不自作主张） -->
    <div v-if="thinkingOptions" class="field thinking-field">
      <label :for="`${uid}-thinking`">思考</label>
      <select :id="`${uid}-thinking`" class="input" v-model="form.thinking">
        <option value="">（不改变）</option>
        <option v-for="lv in thinkingOptions.levels" :key="lv" :value="lv">
          {{ thinkingLabel(lv) }}
        </option>
      </select>
      <span class="faint limit-note">
        {{ thinkingOptions.note }}
        <template v-if="thinkingOptions.mode === 'effort'">
          建议正文续写选「最省」：实测 2400 字从 66.7 秒降到 36 秒，思考少占九成预算。
        </template>
      </span>
    </div>

    <!-- 操作：测试只测这一个角色；保存只保存这一个角色 -->
    <div class="row tight role-actions">
      <button type="button" class="btn" :disabled="busy !== null" @click="submitTest">
        {{ busy === "test" ? "测试中…" : "测试连接（只测此角色）" }}
      </button>
      <button
        type="button"
        class="btn primary"
        :disabled="busy !== null"
        @click="submitSave"
      >
        {{ busy === "save" ? "保存中…" : saveLabel }}
      </button>
    </div>

    <div v-if="localError" class="notice err">{{ localError }}</div>
    <div v-if="testResult" class="notice" :class="testResult.ok ? 'ok' : 'err'">
      <template v-if="testResult.ok">
        连通正常 · 模型 <span class="mono">{{ testResult.model }}</span> ·
        延迟 {{ testResult.latency_ms }}ms · 回复：「{{ testResult.reply }}」
      </template>
      <template v-else>连通失败：{{ testResult.error }}</template>
    </div>
    <div v-if="notice" class="notice" :class="notice.kind">{{ notice.text }}</div>
  </section>
</template>

<style scoped>
.role-panel {
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  padding: var(--gap);
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
}
/* 三个角色用不同的左侧色带 + 标题文案双编码，一眼看出「这是三件事」 */
.role-panel.writer {
  border-left: 4px solid var(--accent);
}
.role-panel.assistant {
  border-left: 4px solid var(--info);
}
.role-panel.distiller {
  border-left: 4px solid var(--warn, #b7791f);
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
  max-width: 46ch;
}
.role-effective {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
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
}
.key-row {
  display: flex;
  gap: 10px;
  align-items: stretch;
}
.key-row .input {
  flex: 1;
}
.key-saved {
  font-size: var(--fs-xs);
}
.advanced {
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
.chain-locked {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: var(--border-w) dashed var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
.chain-note,
.limit-note {
  font-size: var(--fs-xs);
  margin: 0;
  line-height: 1.6;
}
.role-actions {
  margin-top: 2px;
}
/*  chips / chip 在本组件内自含（scoped 样式不跨组件，全局 base.css 也没有） */
.chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.chip {
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  padding: 8px 14px;
  border-radius: var(--radius-full);
  border: var(--border-w) solid var(--border-strong);
  background: var(--bg);
  color: var(--fg);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.chip:hover {
  border-color: var(--accent);
  color: var(--accent-ink);
}
.chip.on {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-fg);
}
</style>
