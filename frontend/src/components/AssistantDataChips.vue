<script setup lang="ts">
import type { DataChip, DataChipKind } from "./assistantDataTypes";

defineProps<{ chips: DataChip[] }>();
const emit = defineEmits<{ (e: "open", kind: DataChipKind): void }>();
</script>

<template>
  <!--
    本轮数据块：只占一行 chip，明细点开小窗口看。
    资料 / 证据 / 工具调用 / 联网来源不再整块铺在正文下面 —— 长回答
    会把这些块推到很远，用户得翻很久才能回到输入框。
  -->
  <div v-if="chips.length" class="turn-chips">
    <button
      v-for="chip in chips"
      :key="chip.kind"
      type="button"
      class="data-chip"
      :class="{ live: chip.live }"
      @click="emit('open', chip.kind)"
    >
      <span v-if="chip.live" class="data-chip-dot" aria-hidden="true" />
      <span v-else class="data-chip-ico" aria-hidden="true">
        <svg
          width="11"
          height="11"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.1"
          stroke-linecap="round"
          stroke-linejoin="round"
        >
          <path d="m5 12 5 5L20 7" />
        </svg>
      </span>
      {{ chip.label }}
    </button>
  </div>
</template>

<style scoped>
@keyframes tracePulse {
  0%, 100% { opacity: 0.4; transform: scale(0.9); }
  50% { opacity: 1; transform: scale(1.12); }
}
.turn-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 2px 0 10px;
}
.data-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px 3px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--bg-elev);
  color: var(--fg-muted);
  font-size: var(--fs-3xs);
  line-height: 1.6;
  cursor: pointer;
  transition: border-color 0.14s ease, background 0.14s ease, color 0.14s ease;
}
.data-chip:hover {
  border-color: var(--accent-line);
  background: var(--accent-soft);
  color: var(--accent-ink);
}
.data-chip-ico {
  display: inline-grid;
  place-items: center;
  width: 13px;
  height: 13px;
  border-radius: 50%;
  background: var(--ok-soft);
  color: var(--ok);
}
.data-chip.live {
  border-color: var(--accent-line);
  background: var(--accent-soft);
  color: var(--accent-ink);
}
.data-chip-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: tracePulse 1.15s ease-in-out infinite;
}
</style>
