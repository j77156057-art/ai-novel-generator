<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import type { AttachmentCleaningPreview } from "@/api/types";
import type { DraftAttachment } from "@/utils/assistantAttachments";

interface PreviewState {
  loading: boolean;
  error: string;
  data: AttachmentCleaningPreview | null;
}

interface RoleMode {
  character: string;
  scene: string;
}

interface MenuItem {
  label: string;
  token: string;
  kind: string;
}

const props = defineProps<{
  query: string;
  sending: boolean;
  canSend: boolean;
  badId: boolean;
  wizardActive: boolean;
  roleMode: RoleMode | null;
  draftItems: DraftAttachment[];
  previewOf: (attachmentId: string) => PreviewState;
  quickPrompts: { label: string; text: string }[];
  quoteItems: MenuItem[];
  commandItems: MenuItem[];
}>();

const emit = defineEmits<{
  (e: "update:query", value: string): void;
  (e: "send"): void;
  (e: "files-selected", files: File[]): void;
  (e: "retry-file", key: number): void;
  (e: "remove-file", key: number): void;
  (e: "toggle-preview", attachmentId: string): void;
  (e: "quick-prompt", text: string): void;
  (e: "insert-token", token: string): void;
  (e: "restart-wizard"): void;
  (e: "exit-role"): void;
}>();

const quoteMenu = ref<"" | "ref" | "cmd">("");
const queryInput = ref<HTMLTextAreaElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);

/** 输入框随内容长高（上限 180px），不出现双滚动条。 */
function autoGrowComposer(): void {
  const el = queryInput.value;
  if (!el) return;
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 180) + "px";
}

watch(
  () => props.query,
  () => nextTick(autoGrowComposer),
);

function onInput(e: Event): void {
  emit("update:query", (e.target as HTMLTextAreaElement).value);
}

/** Enter 发送、Shift+Enter 换行（textarea 默认 Enter 是换行，这里反转）。 */
function onKeydown(e: KeyboardEvent): void {
  // 中文输入法里 Enter 是「选中候选词」，不是发送。少了这个判断，
  // 用户按 Enter 选词会把正在组合的半句话提前发出去，输入框还被清空 ——
  // 表现出来就是「问题没进对话」。
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    emit("send");
  }
}

function onFilesSelected(event: Event): void {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files ?? []);
  if (files.length) emit("files-selected", files);
  input.value = "";
}

function toggleQuoteMenu(kind: "ref" | "cmd"): void {
  quoteMenu.value = quoteMenu.value === kind ? "" : kind;
}

function pickToken(token: string): void {
  emit("insert-token", token);
  quoteMenu.value = "";
  void nextTick(() => {
    autoGrowComposer();
    queryInput.value?.focus();
  });
}

function useQuickPrompt(text: string): void {
  emit("quick-prompt", text);
  void nextTick(() => {
    autoGrowComposer();
    queryInput.value?.focus();
  });
}

/** 供页面在「选了章 / 切了会话 / 快捷提示」后把焦点送回输入框。 */
function focus(): void {
  queryInput.value?.focus();
}

defineExpose({ focus });
</script>

<template>
  <div class="composer-wrap">
    <div v-if="!props.wizardActive && !props.roleMode" class="quick-row">
      <button
        v-for="p in props.quickPrompts"
        :key="p.label"
        type="button"
        class="quick-chip"
        @click="useQuickPrompt(p.text)"
      >
        {{ p.label }}
      </button>
    </div>

    <form class="composer" @submit.prevent="emit('send')">
      <div v-if="props.wizardActive" class="composer-hint">
        引导进行中：直接打字回答，或问「为什么」我就地解释；缺参数就让我改（如「采样改成 200 章」「跳过风格」），改完可撤销。
        <button type="button" class="link-btn" @click="emit('restart-wizard')">
          重新开始
        </button>
      </div>
      <div v-else-if="props.roleMode" class="composer-hint">
        正在与「{{ props.roleMode.character }}」对话 · 情境：{{ props.roleMode.scene }}
        <button type="button" class="link-btn" @click="emit('exit-role')">结束对话</button>
      </div>

      <!-- 附件草稿区：上传只进项目暂存区；投放必须由上方卡片确认后触发。 -->
      <div v-if="props.draftItems.length" class="file-list" aria-live="polite">
        <div
          v-for="file in props.draftItems"
          :key="file.key"
          class="file-chip"
          :class="{ failed: file.state === 'failed' }"
        >
          <div class="file-row">
            <span class="file-name">{{ file.file.name }}</span>
            <span class="faint">{{
              file.state === "uploading"
                ? "上传中…"
                : file.state === "ready"
                  ? "已就绪（仅暂存，未投放）"
                  : "上传失败"
            }}</span>
            <button
              v-if="file.state === 'ready' && file.metadata"
              class="link-btn"
              type="button"
              :disabled="props.sending"
              @click="emit('toggle-preview', file.metadata.attachment_id)"
            >
              {{ props.previewOf(file.metadata.attachment_id).data ? "收起清洗预览" : "清洗预览" }}
            </button>
            <button
              v-if="file.state === 'failed'"
              class="link-btn"
              type="button"
              :disabled="props.sending"
              @click="emit('retry-file', file.key)"
            >
              重试
            </button>
            <button
              class="link-btn"
              type="button"
              :disabled="props.sending"
              :aria-label="`移除 ${file.file.name}`"
              @click="emit('remove-file', file.key)"
            >
              移除
            </button>
          </div>
          <span v-if="file.error" class="file-error" role="alert">{{ file.error }}</span>
          <div
            v-if="file.metadata && (props.previewOf(file.metadata.attachment_id).loading || props.previewOf(file.metadata.attachment_id).error || props.previewOf(file.metadata.attachment_id).data)"
            class="file-preview"
          >
            <span v-if="props.previewOf(file.metadata.attachment_id).loading" class="faint">
              正在读取清洗预览…
            </span>
            <span
              v-else-if="props.previewOf(file.metadata.attachment_id).error"
              class="file-error"
              role="alert"
            >
              {{ props.previewOf(file.metadata.attachment_id).error }}
            </span>
            <template v-else>
              <p class="faint">
                清洗后 {{ props.previewOf(file.metadata.attachment_id).data?.chapters }} 章 ·
                待复核残留 {{ props.previewOf(file.metadata.attachment_id).data?.unresolved_count }} 处
              </p>
              <ul v-if="props.previewOf(file.metadata.attachment_id).data?.changes.length" class="file-changes">
                <li v-for="(change, i) in props.previewOf(file.metadata.attachment_id).data?.changes ?? []" :key="i">
                  <span class="mono">{{ change.kind }}</span> 第 {{ change.chapter_no ?? "?" }} 章 ·
                  删 {{ change.removed_chars }} 字
                  <div class="file-diff">
                    <del>{{ change.before || "（空）" }}</del>
                    <ins>{{ change.after || "（空）" }}</ins>
                  </div>
                </li>
              </ul>
              <p v-else class="faint">没有检出需要删除的站点残留。</p>
            </template>
          </div>
        </div>
      </div>

      <textarea
        ref="queryInput"
        :value="props.query"
        class="composer-input"
        rows="1"
        :disabled="props.badId"
        :placeholder="
          props.roleMode
            ? `对${props.roleMode.character}说点什么…`
            : props.wizardActive
              ? '直接回答这一步，或问我「为什么」；也可以让我改：采样改成 200 章 / 跳过风格'
              : '描述你想推进的剧情，或问一个关于正文的问题…'
        "
        @input="onInput"
        @keydown="onKeydown"
      ></textarea>

      <input
        ref="fileInput"
        type="file"
        accept=".txt,.md,text/plain,text/markdown"
        multiple
        hidden
        :disabled="props.badId || props.sending"
        @change="onFilesSelected"
      />

      <div class="composer-bar">
        <button type="button" class="tool-btn" @click.stop="toggleQuoteMenu('ref')">
          @ 引用
        </button>
        <button type="button" class="tool-btn" @click.stop="toggleQuoteMenu('cmd')">
          / 指令
        </button>
        <button
          type="button"
          class="tool-btn"
          title="支持 .txt / .md；点卡片确认后才投放"
          :disabled="props.badId || props.sending"
          @click="fileInput?.click()"
        >
          附件
        </button>
        <span class="composer-hint-text">Enter 发送 · Shift+Enter 换行</span>
        <button class="send-btn" type="submit" :disabled="!props.canSend" aria-label="发送">
          {{ props.sending ? "…" : "↑" }}
        </button>
      </div>

      <div v-if="quoteMenu" class="composer-mask" @click="quoteMenu = ''" />
      <div v-if="quoteMenu" class="composer-pop" @click.stop>
        <div class="composer-pop-head">
          {{ quoteMenu === "ref" ? "引用当前上下文" : "快捷指令" }}
        </div>
        <button
          v-for="item in quoteMenu === 'ref' ? props.quoteItems : props.commandItems"
          :key="item.label"
          type="button"
          class="composer-pop-item"
          @click="pickToken(item.token)"
        >
          <span>{{ item.label }}</span>
          <span class="kind">{{ item.kind }}</span>
        </button>
      </div>
    </form>

    <div class="composer-meta">
      <span class="ok">● 流式输出已开启</span>
      <span class="dot">·</span>
      <span v-if="props.wizardActive">引导进行中，第一处提到「写小说」也会进入引导</span>
      <span v-else-if="props.roleMode">角色对话中，回答不会写入正文</span>
      <span v-else>不会修改正文，写操作需你确认</span>
    </div>
  </div>
</template>

<style scoped>
/* 输入区：一条细分隔线 + 平铺输入，不做成独立聊天框 */
.composer-wrap {
  flex: none;
  padding: 10px 16px 12px;
  border-top: 1px solid var(--border);
  background: transparent;
}
.quick-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-width: 1000px;
  margin: 0 auto 7px;
}
.quick-chip {
  padding: 3px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  cursor: pointer;
  transition: 0.14s;
}
.quick-chip:hover {
  color: var(--fg-muted);
  border-color: var(--accent-line);
  background: var(--accent-soft);
}
.composer {
  position: relative;
  display: block;
  max-width: 1000px;
  margin: 0 auto;
  padding: 0;
  border: 0;
  border-radius: 0;
  background: transparent;
  box-shadow: none;
}
.composer:focus-within {
  box-shadow: none;
}
.composer .file-list { padding: 0 0 8px; }
.composer .composer-input {
  display: block;
  width: 100%;
  min-height: 44px;
  max-height: 180px;
  padding: 9px 0 2px;
  border: 0;
  border-radius: 0;
  outline: none;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.7;
  resize: none;
}
.composer-input::placeholder { color: var(--fg-faint); }
.composer-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 0 8px;
}
.tool-btn {
  padding: 4px 8px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  cursor: pointer;
}
.tool-btn:hover:not(:disabled) { background: var(--bg-sunken); color: var(--fg-muted); }
.tool-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.composer-hint-text { margin-left: 2px; color: var(--fg-faint); font-size: var(--fs-3xs); }
.send-btn {
  margin-left: auto;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border: 0;
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: var(--accent-fg);
  font-size: var(--fs-lg);
  font-weight: 700;
  cursor: pointer;
  box-shadow: var(--shadow-sm);
}
.send-btn:hover:not(:disabled) { background: var(--accent-hover); }
.send-btn:disabled { opacity: 0.4; cursor: default; }
.composer-meta {
  max-width: 1000px;
  margin: 5px auto 0;
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
}
.composer-meta .ok { color: var(--ok); }
.composer-meta .dot { opacity: 0.6; }
.composer-hint {
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
  flex-wrap: wrap;
  padding: 7px 10px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-sunken);
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.composer-mask { position: fixed; inset: 0; z-index: 30; }
.composer-pop {
  position: absolute;
  left: 10px;
  bottom: 54px;
  z-index: 31;
  width: 280px;
  max-height: 260px;
  overflow: auto;
  padding: 5px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  box-shadow: var(--shadow-md);
}
.composer-pop-head {
  padding: 5px 7px;
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  letter-spacing: 0.1em;
}
.composer-pop-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 7px;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg-muted);
  font-size: var(--fs-2xs);
  text-align: left;
  cursor: pointer;
}
.composer-pop-item:hover { background: var(--bg-sunken); color: var(--fg); }
.composer-pop-item .kind {
  margin-left: auto;
  color: var(--fg-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-3xs);
}
/* ---- 附件草稿 / 清洗预览（与页面里「已发送附件」同名工具类保持一致） ---- */
.file-list { display: flex; flex-direction: column; gap: 6px; }
.file-chip {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  font-size: var(--fs-xs);
}
.file-chip.failed { border-color: var(--danger); }
.file-row { display: flex; align-items: center; gap: var(--gap-sm); flex-wrap: wrap; }
.file-name { font-weight: 600; }
.file-error { color: var(--danger); }
.file-preview {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
}
.file-changes { margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px; }
.file-diff {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-family: var(--font-mono);
  white-space: pre-wrap;
  word-break: break-all;
}
.file-diff del { color: var(--danger); }
.file-diff ins { color: var(--ok); text-decoration: none; }
@media (max-width: 880px) {
  .composer-wrap { padding: 6px 12px 10px; }
}
:global(html[data-theme="ink"]) .send-btn {
  box-shadow: 0 6px 18px rgba(224, 101, 74, 0.3);
}
</style>
