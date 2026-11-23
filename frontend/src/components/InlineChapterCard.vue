<script setup lang="ts">
/**
 * 对话内联章节卡。
 *
 * 正文按后端 `sentences` 的区间渲染：句子之外的空白（段落分隔）原样保留，
 * 因此和写作面板里的排版一致。点选或长按一句，可以：
 * - 直接改这一句（本地拼接后交给父组件 PATCH 整段）；
 * - 指出问题，让助手只重写这一句（走现有局部 patch）。
 */
import { computed, ref, watch } from "vue";
import type { GenerationSentence } from "@/api/types";

const props = defineProps<{
  chapterRange?: string | null;
  chars?: number;
  text: string;
  sentences: GenerationSentence[];
  busy?: boolean;
  note?: string;
}>();

const emit = defineEmits<{
  (e: "save", text: string): void;
  (e: "rewrite", payload: { index: number; problem: string }): void;
  (e: "open-panel"): void;
}>();

const selected = ref<GenerationSentence | null>(null);
const mode = ref<"view" | "edit" | "problem">("view");
const editText = ref("");
const problemText = ref("");

watch(
  () => props.text,
  () => {
    selected.value = null;
    mode.value = "view";
  },
);

type Part = { gap?: string; sentence?: GenerationSentence };

const parts = computed<Part[]>(() => {
  const out: Part[] = [];
  let cursor = 0;
  for (const s of props.sentences) {
    if (s.start > cursor) out.push({ gap: props.text.slice(cursor, s.start) });
    out.push({ sentence: s });
    cursor = s.end;
  }
  if (cursor < props.text.length) out.push({ gap: props.text.slice(cursor) });
  return out;
});

let pressTimer: number | null = null;

function clearPress(): void {
  if (pressTimer !== null) {
    window.clearTimeout(pressTimer);
    pressTimer = null;
  }
}

function pressStart(sentence: GenerationSentence): void {
  clearPress();
  pressTimer = window.setTimeout(() => {
    pressTimer = null;
    selectSentence(sentence);
  }, 480);
}

function pressEnd(): void {
  clearPress();
}

function selectSentence(sentence: GenerationSentence): void {
  selected.value = sentence;
  mode.value = "view";
  editText.value = sentence.text;
  problemText.value = "";
}

function close(): void {
  selected.value = null;
  mode.value = "view";
}

function beginEdit(): void {
  if (!selected.value) return;
  editText.value = selected.value.text;
  mode.value = "edit";
}

function saveEdit(): void {
  const target = selected.value;
  if (!target) return;
  const next = editText.value;
  if (!next.trim() || next === target.text) {
    mode.value = "view";
    return;
  }
  emit("save", props.text.slice(0, target.start) + next + props.text.slice(target.end));
  close();
}

function submitProblem(): void {
  const target = selected.value;
  const problem = problemText.value.trim();
  if (!target || !problem) return;
  emit("rewrite", { index: target.index, problem });
  mode.value = "view";
}
</script>

<template>
  <div class="inline-chapter">
    <div class="chapter-head">
      <div class="row tight">
        <span class="chapter-title">{{ chapterRange || "本章" }}</span>
        <span v-if="chars" class="tag mono">{{ chars }} 字</span>
        <span v-if="busy" class="tag">处理中…</span>
      </div>
      <button class="link-btn" @click="emit('open-panel')">打开写作面板</button>
    </div>

    <div class="chapter-body">
      <template v-for="(part, i) in parts" :key="i">
        <span v-if="part.gap" class="gap">{{ part.gap }}</span>
        <span
          v-else-if="part.sentence"
          class="sentence"
          :class="{ active: selected?.index === part.sentence.index }"
          @click="selectSentence(part.sentence)"
          @pointerdown="pressStart(part.sentence)"
          @pointerup="pressEnd"
          @pointerleave="pressEnd"
          @pointercancel="pressEnd"
        >{{ part.sentence.text }}</span>
      </template>
    </div>

    <p v-if="note" class="chapter-note">{{ note }}</p>

    <div v-if="selected" class="sentence-actions">
      <div class="row tight">
        <span class="tag mono">第 {{ selected.index }} 句</span>
        <span class="faint sentence-preview">{{ selected.text }}</span>
      </div>

      <template v-if="mode === 'view'">
        <div class="row tight">
          <button class="btn sm" @click="beginEdit">直接改这句</button>
          <button class="btn sm" @click="mode = 'problem'">指出问题让 AI 重写</button>
          <button class="btn ghost sm" @click="close">取消</button>
        </div>
      </template>

      <template v-else-if="mode === 'edit'">
        <textarea v-model="editText" class="input sentence-input" rows="4"></textarea>
        <div class="row tight">
          <button class="btn primary sm" :disabled="busy || !editText.trim()" @click="saveEdit">
            保存这一句
          </button>
          <button class="btn ghost sm" @click="mode = 'view'">返回</button>
        </div>
      </template>

      <template v-else>
        <textarea
          v-model="problemText"
          class="input sentence-input"
          rows="3"
          placeholder="例如：这句太解释，改成动作；或者说话人不像钱多多…"
        ></textarea>
        <div class="row tight">
          <button
            class="btn primary sm"
            :disabled="busy || !problemText.trim()"
            @click="submitProblem"
          >
            只重写这一句
          </button>
          <button class="btn ghost sm" @click="mode = 'view'">返回</button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.inline-chapter {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--bg-raised);
  padding: 14px 16px;
  margin: 10px 0 4px;
}
.chapter-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}
.chapter-title {
  font-weight: 600;
}
.chapter-body {
  white-space: pre-wrap;
  line-height: 1.9;
  font-size: var(--fs-base);
}
.gap {
  white-space: pre-wrap;
}
.sentence {
  cursor: text;
  border-radius: 4px;
  transition: background 0.12s ease;
}
.sentence:hover {
  background: var(--bg-hover, rgba(0, 0, 0, 0.04));
}
.sentence.active {
  background: var(--accent-weak, rgba(120, 90, 255, 0.14));
  box-shadow: 0 0 0 2px var(--accent-weak, rgba(120, 90, 255, 0.14));
}
.chapter-note {
  margin: 8px 0 0;
  font-size: var(--fs-xs);
  color: var(--fg-faint);
}
.sentence-actions {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
  display: grid;
  gap: 8px;
}
.sentence-preview {
  font-size: var(--fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sentence-input {
  width: 100%;
  resize: vertical;
}
</style>
