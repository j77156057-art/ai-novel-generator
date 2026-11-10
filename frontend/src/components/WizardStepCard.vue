<script setup lang="ts">
// 一步一张卡片：单选 / 多选 / 数字 / 文本 / 确认。
// 卡片只负责收集一个值并 emit，流程推进全部由 AssistantWorkspace 调后端完成。
import { computed, ref, watch } from "vue";
import type { WizardOption, WizardQuestion, WizardValue } from "../api/types";

const props = defineProps<{
  question: WizardQuestion;
  disabled?: boolean;
  busy?: boolean;
  /** 已作答时显示的选项文案，卡片随之收起。 */
  answeredLabel?: string | null;
}>();

const emit = defineEmits<{
  (e: "answer", value: unknown, label: string): void;
  (e: "skip"): void;
  (e: "back"): void;
  (e: "goto", step: string): void;
}>();

const card = computed(() => props.question.card);
const type = computed(() => card.value.type);

const picked = ref<WizardValue | null>(null);
const multi = ref<WizardValue[]>([]);
const numberValue = ref<number | string>("");
const textValue = ref("");

function reset() {
  const d = card.value.default;
  picked.value = d == null ? null : (d as WizardValue);
  multi.value = Array.isArray(d) ? [...(d as WizardValue[])] : [];
  numberValue.value = typeof d === "number" ? d : "";
  textValue.value = typeof d === "string" ? d : "";
}

watch(
  () => [props.question.step, props.question.card] as const,
  reset,
  { immediate: true },
);

const locked = computed(() => Boolean(props.disabled || props.busy));

const numberQuick = computed(() => card.value.quick || []);

function pick(opt: WizardOption) {
  if (locked.value) return;
  picked.value = opt.value;
  emit("answer", opt.value, opt.label);
}

function toggleMulti(opt: WizardOption) {
  if (locked.value) return;
  const exclusive = card.value.exclusive_value;
  const has = multi.value.some((v) => v === opt.value);
  if (!has && opt.value === exclusive) {
    multi.value = [opt.value];
    return;
  }
  if (!has && exclusive !== undefined && multi.value.includes(exclusive)) {
    multi.value = multi.value.filter((v) => v !== exclusive);
  }
  multi.value = has
    ? multi.value.filter((v) => v !== opt.value)
    : [...multi.value, opt.value];
}

function submitMulti() {
  if (locked.value) return;
  const labels = (card.value.options || [])
    .filter((o) => multi.value.some((v) => v === o.value))
    .map((o) => o.label);
  emit(
    "answer",
    multi.value,
    labels.length ? labels.join("、") : "不投喂语料（自由生成）",
  );
}

function submitNumber() {
  if (locked.value) return;
  const n = Number(numberValue.value);
  if (!Number.isFinite(n)) return;
  emit("answer", n, `${n}${card.value.unit || ""}`);
}

function submitText() {
  if (locked.value) return;
  const text = textValue.value.trim();
  if (!text) return;
  emit("answer", text, text);
}

function pickSuggestion(s: string) {
  if (locked.value) return;
  textValue.value = s;
}

const answerLabel = computed(() => {
  if (!picked.value) return "";
  const opt = (card.value.options || []).find((o) => o.value === picked.value);
  return opt?.label || String(picked.value);
});

const confirmSummary = computed(() =>
  type.value === "confirm" && !card.value.options ? card.value.summary || [] : [],
);
</script>

<template>
  <div class="wizard-card" :class="{ locked }">
    <div class="wz-head">
      <span class="wz-progress">
        第 {{ question.progress.index }}<template v-if="question.progress.total > 1">/{{ question.progress.total }}</template> 步
      </span>
      <h4 class="wz-title">{{ question.title }}</h4>
    </div>

    <!-- 已答：把卡片收成一行，避免历史里堆满按钮 -->
    <div v-if="answeredLabel" class="wz-answered">
      已选：<strong>{{ answeredLabel }}</strong>
    </div>

    <template v-else>
      <!-- 单选 -->
      <div v-if="type === 'single'" class="wz-options">
        <button
          v-for="opt in card.options"
          :key="String(opt.value)"
          type="button"
          class="wz-opt"
          :class="{ on: picked === opt.value }"
          :disabled="locked"
          @click="pick(opt)"
        >
          <span class="wz-opt-label">{{ opt.label }}</span>
          <span v-if="opt.desc" class="wz-opt-desc">{{ opt.desc }}</span>
        </button>
      </div>

      <!-- 多选 -->
      <div v-else-if="type === 'multi'" class="wz-options">
        <button
          v-for="opt in card.options"
          :key="String(opt.value)"
          type="button"
          class="wz-opt check"
          :class="{ on: multi.includes(opt.value) }"
          :disabled="locked"
          @click="toggleMulti(opt)"
        >
          <span class="wz-box" aria-hidden="true">{{ multi.includes(opt.value) ? "✓" : "" }}</span>
          <span class="wz-opt-body">
            <span class="wz-opt-label">{{ opt.label }}</span>
            <span v-if="opt.desc" class="wz-opt-desc">{{ opt.desc }}</span>
          </span>
        </button>
        <p v-if="card.empty_hint" class="wz-hint">{{ card.empty_hint }}</p>
      </div>

      <!-- 数字 -->
      <div v-else-if="type === 'number'" class="wz-number">
        <div class="wz-quick">
          <button
            v-for="q in numberQuick"
            :key="q"
            type="button"
            class="chip"
            :class="{ on: Number(numberValue) === q }"
            :disabled="locked"
            @click="numberValue = q"
          >
            {{ q }}{{ card.unit }}
          </button>
        </div>
        <div class="wz-number-row">
          <input
            v-model="numberValue"
            class="input"
            type="number"
            :min="card.min"
            :max="card.max"
            :step="card.step || 1"
            :disabled="locked"
            @keyup.enter="submitNumber"
          />
          <span class="wz-unit">{{ card.unit }}</span>
          <span class="wz-range">{{ card.min }}–{{ card.max }}</span>
        </div>
      </div>

      <!-- 文本 -->
      <div v-else-if="type === 'text'" class="wz-text">
        <div v-if="card.suggestions?.length" class="wz-quick">
          <button
            v-for="s in card.suggestions"
            :key="s"
            type="button"
            class="chip"
            :class="{ on: textValue === s }"
            :disabled="locked"
            @click="pickSuggestion(s)"
          >
            {{ s }}
          </button>
        </div>
        <textarea
          v-model="textValue"
          class="input"
          rows="2"
          :placeholder="card.placeholder"
          :disabled="locked"
          @keydown.enter.exact.prevent="submitText"
        />
      </div>

      <!-- 确认：带选项（是/否） -->
      <div v-else-if="type === 'confirm' && card.options?.length" class="wz-options">
        <button
          v-for="opt in card.options"
          :key="String(opt.value)"
          type="button"
          class="wz-opt"
          :disabled="locked"
          @click="pick(opt)"
        >
          <span class="wz-opt-label">{{ opt.label }}</span>
          <span v-if="opt.desc" class="wz-opt-desc">{{ opt.desc }}</span>
        </button>
      </div>

      <!-- 确认：汇总 -->
      <div v-else-if="type === 'confirm'" class="wz-summary">
        <button
          v-for="row in confirmSummary"
          :key="row.step"
          type="button"
          class="wz-row"
          :disabled="locked"
          :title="`回到「${row.label}」修改`"
          @click="emit('goto', row.step)"
        >
          <span class="wz-row-label">{{ row.label }}</span>
          <span class="wz-row-value">{{ row.value }}</span>
          <span class="wz-row-edit">修改</span>
        </button>
      </div>
    </template>

    <div class="wz-foot">
      <button
        v-if="question.can_back"
        type="button"
        class="btn ghost sm"
        :disabled="locked"
        @click="emit('back')"
      >
        上一步
      </button>
      <span class="wz-spacer" />
      <button
        v-if="!answeredLabel && question.can_skip"
        type="button"
        class="btn ghost sm"
        :disabled="locked"
        @click="emit('skip')"
      >
        用默认值
      </button>
      <button
        v-if="!answeredLabel && type === 'multi'"
        type="button"
        class="btn primary sm"
        :disabled="locked"
        @click="submitMulti"
      >
        下一步
      </button>
      <button
        v-if="!answeredLabel && type === 'number'"
        type="button"
        class="btn primary sm"
        :disabled="locked"
        @click="submitNumber"
      >
        下一步
      </button>
      <button
        v-if="!answeredLabel && type === 'text'"
        type="button"
        class="btn primary sm"
        :disabled="locked || !textValue.trim()"
        @click="submitText"
      >
        下一步
      </button>
      <button
        v-if="!answeredLabel && confirmSummary.length"
        type="button"
        class="btn primary sm"
        :disabled="locked"
        @click="emit('answer', true, card.action_label || '确认开始')"
      >
        {{ card.action_label || "确认开始" }}
      </button>
      <span v-if="busy" class="wz-busy">处理中…</span>
    </div>

    <!-- 卡片只是快捷入口：不点也能直接在下面打字作答，或追问 -->
    <p v-if="!answeredLabel" class="wz-hint">
      也可以不点卡片，直接在下面打字——说「随便，你定」我按默认走，问「为什么」我就在这一步解释。
    </p>
  </div>
</template>

<style scoped>
.wizard-card {
  margin-top: var(--gap-sm);
  padding: var(--gap);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
}

.wizard-card.locked {
  opacity: 0.72;
}

.wz-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: var(--gap-sm);
}

.wz-progress {
  font-size: var(--fs-xs);
  color: var(--fg-faint);
  letter-spacing: 0.02em;
}

.wz-title {
  margin: 0;
  font-size: var(--fs-md);
  font-family: var(--font-display);
  color: var(--fg);
}

.wz-hint {
  margin: var(--gap-sm) 0 0;
  font-size: var(--fs-xs);
  line-height: 1.65;
  color: var(--fg-faint);
}

.wz-options {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.wz-opt {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: 9px 11px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color 0.12s ease, background 0.12s ease;
}

.wz-opt:hover:not(:disabled) {
  border-color: var(--accent-line);
  background: var(--accent-soft);
}

.wz-opt.on {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.wz-opt:disabled {
  cursor: default;
}

.wz-opt.check {
  flex-direction: row;
  align-items: flex-start;
  gap: 8px;
}

.wz-opt-body {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.wz-box {
  flex: none;
  width: 16px;
  height: 16px;
  margin-top: 1px;
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-sm);
  display: grid;
  place-items: center;
  font-size: 11px;
  line-height: 1;
  color: var(--accent-fg);
  background: var(--bg-elev);
}

.wz-opt.on .wz-box {
  background: var(--accent);
  border-color: var(--accent);
}

.wz-opt-label {
  font-size: var(--fs-sm);
  font-weight: 600;
}

.wz-opt-desc {
  font-size: var(--fs-xs);
  line-height: 1.55;
  color: var(--fg-muted);
}

.wz-hint {
  margin: 2px 0 0;
  font-size: var(--fs-xs);
  color: var(--fg-faint);
}

.wz-number {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.wz-quick {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.wz-number-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wz-number-row .input {
  width: 120px;
}

.wz-unit,
.wz-range {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}

.wz-text {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.wz-text .input {
  width: 100%;
  resize: vertical;
}

.wz-summary {
  display: flex;
  flex-direction: column;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.wz-row {
  display: grid;
  grid-template-columns: 78px 1fr auto;
  gap: 8px;
  align-items: center;
  padding: 7px 10px;
  border: 0;
  border-bottom: var(--border-w) solid var(--border);
  background: var(--paper);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.wz-row:last-child {
  border-bottom: 0;
}

.wz-row:hover:not(:disabled) {
  background: var(--accent-soft);
}

.wz-row-label {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}

.wz-row-value {
  font-size: var(--fs-sm);
  color: var(--fg);
}

.wz-row-edit {
  font-size: var(--fs-xs);
  color: var(--accent-ink);
}

.wz-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: var(--gap-sm);
}

.wz-spacer {
  flex: 1;
}

.wz-busy {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}

.wz-answered {
  padding: 7px 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  font-size: var(--fs-sm);
  color: var(--fg-muted);
}
</style>
