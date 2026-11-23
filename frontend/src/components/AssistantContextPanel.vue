<script setup lang="ts">
import { ref } from "vue";
import type { AssistantEvidence, AssistantMemory } from "@/api/types";
import { sourceLabel } from "@/utils/assistantDisplay";
import WorkflowStrip from "@/components/WorkflowStrip.vue";

/**
 * 助手页右栏「本次上下文」。
 *
 * 纯展示：所有数据由页面传入，面板自己只做两件事 ——
 *   1. 把工序链（WorkflowStrip）挂进来，并对外暴露 reloadWorkflow()，
 *      让页面在卡片确认后能让它重算；
 *   2. 窄屏抽屉的关闭按钮 emit 给页面，抽屉开关仍由页面持有。
 */
const props = defineProps<{
  open: boolean;
  projectId: number;
  badId: boolean;
  projectTitle: string;
  projectMeta: string;
  bookSegments: number | null;
  bookChars: number | null;
  chapterCount: number;
  bibleCharacters: number;
  bibleRelations: number;
  chips: { label: string; accent?: boolean }[];
  evidence: AssistantEvidence[];
  memories: AssistantMemory[];
  models: { label: string; value: string }[];
  writtenChapters: number;
  latestWrittenChapter: number | null;
}>();

const emit = defineEmits<{
  (e: "close"): void;
  (e: "manage-memory"): void;
  (e: "open-model-settings"): void;
  (e: "open-rail"): void;
}>();

const workflowStripRef = ref<{ load: () => Promise<void> } | null>(null);

function reloadWorkflow(): Promise<void> {
  return workflowStripRef.value?.load() ?? Promise.resolve();
}

defineExpose({ reloadWorkflow });
</script>

<template>
  <aside class="ctx-panel" :class="{ open: props.open }" aria-label="本次上下文">
    <div class="ctx-title">
      <span>本次上下文</span>
      <button class="link-btn ctx-close" @click="emit('close')">关闭</button>
    </div>

    <!-- 工序链：全流程走到哪一步、卡在哪、哪条改动等你批（服务端每次重新评估） -->
    <WorkflowStrip
      v-if="!props.badId"
      ref="workflowStripRef"
      class="ctx-workflow"
      :project-id="props.projectId"
    />

    <div class="ctx-card">
      <h3>
        {{ props.projectTitle }}
        <span class="mini">{{ props.projectMeta }}</span>
      </h3>
      <div class="ctx-row">
        <span>已写正文</span>
        <b>{{ props.bookSegments ?? "—" }} 段 · {{ props.bookChars === null ? "—" : props.bookChars.toLocaleString() }} 字</b>
      </div>
      <div class="ctx-row">
        <span>章纲</span><b>{{ props.chapterCount || "—" }} 章</b>
      </div>
      <div class="ctx-row">
        <span>剧情圣经</span>
        <b>{{ props.bibleCharacters }} 角色 · {{ props.bibleRelations }} 关系</b>
      </div>
      <div class="ctx-chips">
        <span
          v-for="chip in props.chips"
          :key="chip.label"
          class="chip"
          :class="{ accent: chip.accent }"
        >{{ chip.label }}</span>
      </div>
    </div>

    <div class="ctx-card">
      <h3>证据 <span class="mini">{{ props.evidence.length }}</span></h3>
      <p v-if="!props.evidence.length" class="ctx-empty">
        这一轮还没有检索证据；问正文或设定类问题时会自动读取。
      </p>
      <div v-for="(ev, i) in props.evidence.slice(0, 6)" :key="i" class="ctx-ev">
        <div class="ctx-ev-head">
          <span class="src">{{ sourceLabel(ev.source) }}</span>
          <span>{{ Number(ev.score).toFixed(2) }}</span>
        </div>
        <p>{{ ev.snippet }}</p>
      </div>
    </div>

    <div class="ctx-card">
      <h3>长期记忆 <span class="mini">{{ props.memories.length }}</span></h3>
      <div v-for="m in props.memories.slice(0, 4)" :key="m.id" class="ctx-mem">{{ m.text }}</div>
      <p v-if="!props.memories.length" class="ctx-empty">
        聊几句后，助手会自动记住你的写作偏好。
      </p>
      <button class="link-btn" @click="emit('manage-memory')">管理记忆</button>
    </div>

    <div class="ctx-card">
      <h3>模型</h3>
      <div v-for="m in props.models" :key="m.label" class="ctx-row">
        <span>{{ m.label }}</span><b>{{ m.value }}</b>
      </div>
      <button class="link-btn" @click="emit('open-model-settings')">模型设置</button>
    </div>

    <div class="ctx-card">
      <h3>章节进度</h3>
      <div class="ctx-row">
        <span>已写</span><b>{{ props.writtenChapters }} / {{ props.chapterCount || "—" }}</b>
      </div>
      <div v-if="props.latestWrittenChapter" class="ctx-row">
        <span>最新</span><b>第 {{ props.latestWrittenChapter }} 章</b>
      </div>
      <button v-if="props.chapterCount" class="link-btn" @click="emit('open-rail')">打开目录</button>
    </div>
  </aside>
</template>

<style scoped>
/* ---------- 右：上下文 ---------- */
.ctx-panel {
  min-height: 0;
  overflow-y: auto;
  padding: 16px 13px 20px;
  /* 无框：上下文是一列信息，不是一张侧边卡片 */
  background: transparent;
}
.ctx-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0 0 9px;
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.ctx-close { display: none; }
.ctx-card {
  margin-bottom: 0;
  padding: 12px 0;
  border: 0;
  border-radius: 0;
  background: transparent;
}
.ctx-card + .ctx-card {
  border-top: 1px solid var(--border);
}
.ctx-card h3 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 6px;
  font-family: var(--font-display);
  font-size: var(--fs-xs);
  font-weight: 600;
}
.ctx-card h3 .mini {
  margin-left: auto;
  color: var(--fg-faint);
  font-family: var(--font-sans);
  font-size: var(--fs-3xs);
  font-weight: 400;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 60%;
}
.ctx-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 2px 0;
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
}
.ctx-row b { color: var(--fg-muted); font-weight: 500; text-align: right; }
.ctx-chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 7px; }
.chip {
  padding: 2px 7px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: var(--bg-elev);
  color: var(--fg-muted);
  font-size: var(--fs-3xs);
}
.chip.accent {
  border-color: var(--accent-line);
  background: var(--accent-soft);
  color: var(--accent-ink);
}
.ctx-empty { margin: 0 0 6px; color: var(--fg-faint); font-size: var(--fs-3xs); line-height: 1.6; }
.ctx-ev { margin-top: 7px; padding-top: 7px; border-top: 1px solid var(--border); }
.ctx-ev:first-of-type { margin-top: 0; padding-top: 0; border-top: 0; }
.ctx-ev-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--fg-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-3xs);
}
.ctx-ev p {
  margin: 3px 0 0;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  color: var(--fg-muted);
  font-size: var(--fs-3xs);
  line-height: 1.6;
}
.ctx-mem {
  position: relative;
  padding: 3px 0 3px 10px;
  color: var(--fg-muted);
  font-size: var(--fs-3xs);
  line-height: 1.6;
}
.ctx-mem::before {
  content: "·";
  position: absolute;
  left: 0;
  color: var(--accent);
}

/* 工序链收进右栏：只留进度条 + 一句摘要，别把侧栏撑高 */
.ctx-workflow {
  margin: 0 0 4px;
  padding: 2px 0 12px;
  border: 0;
  border-bottom: 1px solid var(--border);
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.ctx-workflow :deep(.card-title) {
  margin-bottom: 5px;
  font-size: var(--fs-2xs);
}
.ctx-workflow :deep(.wf-progress-note) {
  margin-top: 5px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: var(--fs-3xs);
  line-height: 1.5;
}
.ctx-workflow :deep(.wf-progress-bar) { height: 3px; }

/* ---------- 窄屏：上下文变成右侧抽屉 ---------- */
@media (max-width: 1360px) {
  .ctx-close { display: inline-flex; }
  .ctx-panel {
    position: fixed;
    top: 0;
    bottom: 0;
    right: 0;
    z-index: 60;
    width: 300px;
    transform: translateX(100%);
    transition: transform 0.22s cubic-bezier(0.16, 1, 0.3, 1);
    box-shadow: var(--shadow-md);
  }
  .ctx-panel.open { transform: none; }
}
</style>
