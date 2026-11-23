<script setup lang="ts">
import type { AssistantMemory } from "@/api/types";

/**
 * 助手长期记忆抽屉。
 *
 * 记忆由助手在问答后自动抽取，这里只做「看见 / 删除」：抽错了要能删掉，
 * 而不是让用户只能忍着。数据与删除请求都由页面持有，组件只负责展示。
 */
const props = defineProps<{
  open: boolean;
  memories: AssistantMemory[];
  limit: number;
  loading: boolean;
  error: string;
  busy: boolean;
}>();

const emit = defineEmits<{
  (e: "close"): void;
  (e: "remove", item: AssistantMemory): void;
}>();
</script>

<template>
  <aside v-if="props.open" class="memory-drawer" aria-label="助手记忆">
    <div class="memory-head">
      <div>
        <div class="memory-title">助手记忆</div>
        <div class="memory-sub">
          {{ props.memories.length }} / {{ props.limit }} 条 · 自动记住，每次问答都会带上
        </div>
      </div>
      <button class="link-btn" @click="emit('close')">关闭</button>
    </div>

    <div v-if="props.error" class="notice err">{{ props.error }}</div>
    <div v-else-if="props.loading" class="faint">读取记忆…</div>
    <ol v-else-if="props.memories.length" class="memory-list">
      <li v-for="m in props.memories" :key="m.id">
        <span class="memory-text">{{ m.text }}</span>
        <button class="link-btn" :disabled="props.busy" @click="emit('remove', m)">
          删除
        </button>
      </li>
    </ol>
    <p v-else class="faint">
      还没有记忆。聊几句之后，助手会自动记住你的写作偏好（人称、称呼、禁忌这类），
      并在这里显示。
    </p>
  </aside>
</template>

<style scoped>
.memory-drawer {
  position: fixed;
  top: 76px;
  right: 24px;
  width: 340px;
  max-height: calc(100vh - 108px);
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: var(--bg-elev);
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  z-index: 40;
}
.memory-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 8px;
}
.memory-title {
  font-family: var(--font-display);
  font-size: var(--fs-lg);
  font-weight: 700;
}
.memory-sub {
  margin-top: 2px;
  color: var(--fg-faint);
  font-size: var(--fs-xs);
}
.memory-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.memory-list li {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.memory-text {
  min-width: 0;
  overflow-wrap: anywhere;
}

@media (max-width: 720px) {
  .memory-drawer {
    left: 12px;
    right: 12px;
    width: auto;
  }
}
</style>
