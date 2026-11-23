<script setup lang="ts">
/** 助手右侧写作进度：当前章 / 大纲 / 时间线节点。 */
import { computed } from "vue";
import type { WritingProgress } from "@/api/types";

const props = defineProps<{
  progress: WritingProgress | null;
  loading?: boolean;
  generating?: boolean;
  stage?: string;
}>();

const node = computed(() => props.progress?.timeline.current_node || null);
const nextNode = computed(() => props.progress?.timeline.next_node || null);
</script>

<template>
  <aside class="writing-progress">
    <div class="progress-head">
      <strong>写作进度</strong>
      <span v-if="generating" class="tag">生成中</span>
    </div>

    <div v-if="loading && !progress" class="progress-loading">
      <div class="skeleton-line"></div>
      <div class="skeleton-line short"></div>
    </div>

    <template v-else-if="progress">
      <div class="progress-block">
        <div class="progress-row">
          <span class="faint">当前章节</span>
          <span class="mono">
            第 {{ progress.current_chapter || "—" }} 章
            <template v-if="progress.outline_total"> / 共 {{ progress.outline_total }} 章</template>
          </span>
        </div>
        <div class="bar">
          <div class="bar-fill" :style="{ width: `${progress.outline_percent}%` }"></div>
        </div>
        <div class="progress-row faint">
          <span>大纲完成 {{ progress.outline_percent }}%</span>
          <span>还差 {{ progress.to_outline_percent }}%</span>
        </div>
      </div>

      <div v-if="progress.timeline.exists" class="progress-block">
        <div class="progress-row">
          <span class="faint">当前节点</span>
          <span>
            {{ node?.title || "—" }}
            <span v-if="node" class="faint mono">{{ node.index }}/{{ node.total }}</span>
          </span>
        </div>
        <div class="bar node">
          <div
            class="bar-fill"
            :style="{ width: `${Math.round((progress.timeline.node_progress || 0) * 100)}%` }"
          ></div>
        </div>
        <div class="progress-row faint">
          <span>
            节点内
            {{ Math.round((progress.timeline.node_progress || 0) * 100) }}%
          </span>
          <span>距下一节点 {{ progress.timeline.to_next_node_percent }}%</span>
        </div>
        <div v-if="nextNode" class="progress-next">
          下一节点：{{ nextNode.title }}
          <span class="faint mono">第 {{ nextNode.chapter_start }} 章</span>
        </div>
      </div>

      <div v-else class="progress-block faint">
        时间线尚未规划：生成第一章后会自动建立节点。
      </div>

      <div v-if="generating" class="progress-stage">
        <span class="dot"></span>
        {{ stage || `正在生成第 ${progress.next_chapter} 章` }}
      </div>
    </template>
  </aside>
</template>

<style scoped>
.writing-progress {
  display: grid;
  gap: 14px;
  padding: 16px;
  border-left: 1px solid var(--line);
  background: var(--bg);
  min-width: 220px;
  max-width: 280px;
  height: 100%;
  overflow: auto;
}
.progress-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.progress-block {
  display: grid;
  gap: 6px;
}
.progress-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--fs-sm);
}
.bar {
  height: 6px;
  border-radius: 999px;
  background: var(--line);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width 0.35s ease;
}
.bar.node .bar-fill {
  background: var(--ok, #2f9e6e);
}
.progress-next {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  margin-top: 2px;
}
.progress-stage {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: progress-pulse 1s ease-in-out infinite;
}
.progress-loading {
  display: grid;
  gap: 8px;
}
.skeleton-line {
  height: 38px;
  border-radius: 8px;
  background: var(--bg-raised);
  animation: progress-pulse 1.2s ease-in-out infinite;
}
.skeleton-line.short {
  width: 70%;
}
@keyframes progress-pulse {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
}
@media (max-width: 900px) {
  .writing-progress {
    display: none;
  }
}
</style>
