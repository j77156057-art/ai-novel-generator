<script setup lang="ts">
import { ref } from "vue";
import type { WizardCommitResult } from "@/api/types";
import AssistantWizardCards from "./AssistantWizardCards.vue";

interface WizardCharacter {
  name: string;
  role: string;
  card: Record<string, unknown> | null;
}

const props = defineProps<{
  result: WizardCommitResult;
  characters: WizardCharacter[];
  busyName: string | null;
  cardSaving: boolean;
  cardDeleting: boolean;
  characterRemoving: boolean;
  cardEditError: string | null;
  runBusy: boolean;
  canRun: boolean;
}>();

const emit = defineEmits<{
  (e: "regenerate", payload: { names: string[]; mode: string }): void;
  (e: "edit", character: WizardCharacter): void;
  (e: "delete-card", name: string): void;
  (e: "remove-character", name: string): void;
  (e: "run"): void;
  (e: "exit-role"): void;
  (e: "open-project"): void;
}>();

/** 大纲展开是纯本地 UI：只在这个结果块里用，不需要页面级展开表。 */
const bibleOpen = ref(false);

function prettyJson(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
</script>

<template>
  <p v-if="props.result.message" class="wz-prose">{{ props.result.message }}</p>

  <div
    v-if="(props.result.stage === 'outline' || props.result.stage === 'cards') && props.result.bible"
    class="blk"
  >
    <button class="link-btn" @click="bibleOpen = !bibleOpen">
      {{ bibleOpen ? "收起大纲" : "查看生成的大纲" }}
    </button>
    <pre
      v-if="bibleOpen"
      class="diffbox snippet expanded mono"
    >{{ prettyJson(props.result.bible) }}</pre>
  </div>

  <!-- 以剧情圣经为准渲染：历史会话没有 character_cards 字段，也要能看到卡/角色。 -->
  <AssistantWizardCards
    v-if="(props.result.stage === 'outline' || props.result.stage === 'cards') && props.characters.length"
    :result="props.result"
    :characters="props.characters"
    :busy-name="props.busyName"
    :card-saving="props.cardSaving"
    :card-deleting="props.cardDeleting"
    :character-removing="props.characterRemoving"
    :card-edit-error="props.cardEditError"
    @regenerate="(p) => emit('regenerate', p)"
    @edit="(c) => emit('edit', c)"
    @delete-card="(name) => emit('delete-card', name)"
    @remove-character="(name) => emit('remove-character', name)"
  />

  <div v-if="props.result.segments?.length" class="wz-done">
    <div
      v-for="seg in props.result.segments"
      :key="String(seg.segment_no)"
      class="wz-seg"
    >
      <span class="tag mono">第 {{ seg.segment_no }} 段 · {{ seg.chars }} 字</span>
      <div class="prose snippet expanded">{{ seg.excerpt }}…</div>
    </div>
  </div>

  <div class="row tight" style="margin-top: var(--sp-2)">
    <button
      v-if="props.canRun"
      class="btn primary sm"
      :disabled="props.runBusy"
      @click="emit('run')"
    >
      {{ props.runBusy ? "正在写，别关页面…" : "开始写" }}
    </button>
    <button
      v-if="props.result.stage === 'role'"
      class="btn sm"
      @click="emit('exit-role')"
    >
      结束角色对话
    </button>
    <button class="btn ghost sm" @click="emit('open-project')">
      {{ props.result.stage === "done" ? "看全文 →" : "去项目工作台 →" }}
    </button>
  </div>
</template>

<style scoped>
/* 与页面同名工具类保持一致（页面还有 note 等其他地方使用这些规则）。 */
.wz-prose {
  margin: 0 0 var(--gap-sm);
  font-size: var(--fs-sm);
  line-height: 1.78;
  color: var(--fg);
  white-space: pre-wrap;
  max-width: 62ch;
}
.wz-done {
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
  margin-top: var(--gap-sm);
}
.wz-seg .prose {
  margin-top: 4px;
}
.blk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.snippet {
  max-height: 7.5em;
  overflow: hidden;
}
.snippet.expanded {
  max-height: none;
}
.diffbox {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  margin: 0;
  color: var(--fg);
}
</style>
