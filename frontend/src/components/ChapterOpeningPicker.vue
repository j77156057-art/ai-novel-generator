<script setup lang="ts">
/**
 * 章前「这一章怎么开头」选择卡。
 *
 * 选项来自后端（模型 3 条 + 规则兜底）；用户也可以：
 * - 直接选一条；
 * - 让 AI 自己决定（不注入额外开场要求）；
 * - 自己写个大概，作为本章开场要求。
 */
import { ref } from "vue";
import type { ChapterOpeningOption } from "@/api/types";

const props = defineProps<{
  chapter: number;
  isFirst: boolean;
  source: "model" | "fallback" | "";
  options: ChapterOpeningOption[];
  loading?: boolean;
  busy?: boolean;
  error?: string;
}>();

const emit = defineEmits<{
  (e: "pick", payload: { opening: string; label: string }): void;
  (e: "refresh"): void;
  (e: "cancel"): void;
}>();

const custom = ref("");
const customOpen = ref(false);

function choose(option: ChapterOpeningOption): void {
  emit("pick", {
    opening: [
      `开场方式：${option.title}`,
      option.summary,
      option.opening,
    ].filter(Boolean).join("\n"),
    label: option.title,
  });
}

function submitCustom(): void {
  const text = custom.value.trim();
  if (!text) return;
  emit("pick", { opening: text, label: "我自己写的开场" });
  custom.value = "";
  customOpen.value = false;
}

function autoPick(): void {
  emit("pick", { opening: "", label: "AI 自动决定" });
}
</script>

<template>
  <div class="opening-card">
    <div class="opening-head">
      <div>
        <strong>{{ isFirst ? "第一章想怎么开头？" : `第 ${chapter} 章想怎么开头？` }}</strong>
        <p class="faint opening-hint">
          <template v-if="source === 'model'">建议由模型根据大纲、时间线、上一章和钩子生成。</template>
          <template v-else-if="source === 'fallback'">模型不可用，下面是根据大纲、时间线和上一章拼出的规则建议。</template>
          <template v-else>正在根据大纲、时间线、上一章和钩子生成建议…</template>
        </p>
      </div>
      <button class="link-btn" :disabled="loading || busy" @click="emit('refresh')">
        {{ loading ? "生成中…" : "换一组" }}
      </button>
    </div>

    <p v-if="error" class="notice err opening-error">{{ error }}</p>

    <div v-if="loading && !options.length" class="opening-skeleton">
      <div class="skeleton-line"></div>
      <div class="skeleton-line short"></div>
    </div>

    <div v-else class="opening-options">
      <button
        v-for="option in options"
        :key="option.id"
        class="opening-option"
        :disabled="busy"
        @click="choose(option)"
      >
        <span class="opening-title">{{ option.title }}</span>
        <span class="opening-summary">{{ option.summary }}</span>
        <span class="opening-detail">{{ option.opening }}</span>
        <span v-if="option.why" class="opening-why">为什么：{{ option.why }}</span>
      </button>
    </div>

    <div class="opening-actions">
      <button class="btn sm" :disabled="busy" @click="autoPick">让 AI 自己决定</button>
      <button class="btn ghost sm" :disabled="busy" @click="customOpen = !customOpen">
        {{ customOpen ? "收起" : "我自己写个大概" }}
      </button>
      <button class="link-btn opening-cancel" :disabled="busy" @click="emit('cancel')">
        先不写
      </button>
    </div>

    <div v-if="customOpen" class="opening-custom">
      <textarea
        v-model="custom"
        class="input"
        rows="3"
        placeholder="例如：从钱多多半夜搬箱子被抓个正着开始，先写凉气，再写他回头加钱。"
      ></textarea>
      <button class="btn primary sm" :disabled="busy || !custom.trim()" @click="submitCustom">
        用这段开场
      </button>
    </div>
  </div>
</template>

<style scoped>
.opening-card {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--bg-raised);
  padding: 14px 16px;
  margin: 10px 0 4px;
}
.opening-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: flex-start;
}
.opening-hint {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
}
.opening-error {
  margin: 8px 0;
}
.opening-options {
  display: grid;
  gap: 8px;
  margin: 12px 0;
}
.opening-option {
  display: grid;
  gap: 4px;
  text-align: left;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg);
  padding: 10px 12px;
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.opening-option:hover:not(:disabled) {
  border-color: var(--accent);
  background: var(--bg-raised);
}
.opening-option:disabled {
  opacity: 0.6;
  cursor: default;
}
.opening-title {
  font-weight: 600;
}
.opening-summary {
  font-size: var(--fs-sm);
}
.opening-detail {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  line-height: 1.7;
}
.opening-why {
  font-size: var(--fs-xs);
  color: var(--fg-faint);
}
.opening-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.opening-cancel {
  margin-left: auto;
}
.opening-custom {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}
.opening-skeleton {
  display: grid;
  gap: 8px;
  margin: 12px 0;
}
.skeleton-line {
  height: 42px;
  border-radius: 10px;
  background: var(--bg);
  animation: opening-pulse 1.2s ease-in-out infinite;
}
.skeleton-line.short {
  width: 70%;
}
@keyframes opening-pulse {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
}
</style>
