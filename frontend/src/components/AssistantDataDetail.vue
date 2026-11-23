<script setup lang="ts">
import type {
  AssistantEvidence,
  AssistantToolTraceItem,
  AssistantWebSource,
} from "@/api/types";
import { sourceLabel } from "@/utils/assistantDisplay";
import type { DataChipKind, DetailStep } from "./assistantDataTypes";

defineProps<{
  kind: DataChipKind;
  title: string;
  subtitle: string;
  steps: DetailStep[];
  evidence: AssistantEvidence[];
  tools: AssistantToolTraceItem[];
  web: AssistantWebSource[];
  toolResultFor: (name: string) => string;
}>();

const emit = defineEmits<{ (e: "close"): void }>();

/** 工具调用状态 → 面向用户的文字 + 色标（文字为主，颜色只做辅助）。 */
function toolStatusMeta(status: string): { label: string; tag: string } {
  if (status === "ok") return { label: "已执行", tag: "ok" };
  if (status === "denied") return { label: "已拒绝（未执行）", tag: "err" };
  return { label: "执行失败", tag: "warn" };
}
</script>

<template>
  <!--
    本轮数据块的明细小窗口：自带滚动，不占对话流的高度。
    资料 / 证据 / 工具调用 / 联网来源共用这一个窗口，按 kind 换内容。
  -->
  <div class="detail-mask" @click.self="emit('close')">
    <section
      class="detail-window"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
    >
      <header class="detail-head">
        <div class="detail-head-main">
          <div class="detail-kicker">{{ subtitle }}</div>
          <h3>{{ title }}</h3>
        </div>
        <button class="link-btn" type="button" @click="emit('close')">关闭</button>
      </header>

      <div class="detail-body">
        <!-- 读取步骤 -->
        <template v-if="kind === 'steps'">
          <div
            v-for="(step, si) in steps"
            :key="`ds-${si}`"
            class="detail-item"
          >
            <div class="detail-item-head">
              <span class="detail-item-name">{{ step.name }}</span>
              <span v-if="step.meta" class="detail-item-meta">{{ step.meta }}</span>
            </div>
            <pre v-if="step.detail" class="detail-pre">{{ step.detail }}</pre>
          </div>
          <p v-if="!steps.length" class="faint">本轮没有读取资料。</p>
        </template>

        <!-- 证据原文 -->
        <template v-else-if="kind === 'evidence'">
          <div
            v-for="(ev, i) in evidence"
            :key="`de-${i}`"
            class="detail-item"
          >
            <div class="detail-item-head">
              <span class="tag accent">{{ sourceLabel(ev.source) }}</span>
              <span class="detail-item-meta mono">kind={{ ev.kind }}</span>
              <span class="detail-item-meta mono">score={{ Number(ev.score).toFixed(3) }}</span>
              <span class="detail-item-meta mono">ref={{ ev.ref_id }}</span>
            </div>
            <pre class="detail-pre">{{ ev.snippet }}</pre>
          </div>
          <p v-if="!evidence.length" class="faint">本轮没有证据。</p>
        </template>

        <!-- 工具调用轨迹 -->
        <template v-else-if="kind === 'tools'">
          <div
            v-for="t in tools"
            :key="`dt-${t.seq}`"
            class="detail-item"
          >
            <div class="detail-item-head">
              <span class="tag mono">#{{ t.seq }} · 第 {{ t.step }} 轮</span>
              <span class="tag accent mono">{{ t.name }}</span>
              <span class="tag" :class="toolStatusMeta(t.status).tag">
                {{ toolStatusMeta(t.status).label }}
              </span>
              <span class="detail-item-meta mono">{{ t.latency_ms }}ms</span>
            </div>
            <div class="detail-line mono">参数：{{ t.arguments }}</div>
            <div
              v-if="t.status === 'ok' && toolResultFor(t.name)"
              class="detail-line mono"
            >
              结果：{{ toolResultFor(t.name) }}
            </div>
          </div>
          <p v-if="!tools.length" class="faint">本轮没有工具调用。</p>
        </template>

        <!-- 联网检索来源 -->
        <template v-else>
          <a
            v-for="s in web"
            :key="`dw-${s.index}`"
            class="detail-web"
            :href="s.url"
            target="_blank"
            rel="noopener noreferrer"
          >
            <span class="detail-web-index">[{{ s.index }}]</span>
            <span class="detail-web-main">
              <span class="detail-web-title">{{ s.title }}</span>
              <span class="detail-web-url">{{ s.url }}</span>
              <span class="detail-web-snippet">{{ s.snippet }}</span>
            </span>
          </a>
          <p v-if="!web.length" class="faint">本轮没有联网来源。</p>
        </template>
      </div>
    </section>
  </div>
</template>

<style scoped>
.detail-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: color-mix(in srgb, var(--bg) 55%, transparent);
  backdrop-filter: blur(3px);
  animation: detailMaskIn 0.14s ease both;
}
.detail-window {
  width: min(680px, 100%);
  max-height: min(72vh, 640px);
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 14px;
  background: var(--bg-elev);
  box-shadow: var(--shadow-lg);
  overflow: hidden;
  animation: detailWindowIn 0.18s ease both;
}
.detail-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px 12px;
  border-bottom: 1px solid var(--border);
}
.detail-head-main { min-width: 0; flex: 1; }
.detail-kicker {
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  letter-spacing: 0.06em;
}
.detail-head h3 {
  margin: 2px 0 0;
  font-family: var(--font-display);
  font-size: var(--fs-lg);
  font-weight: 600;
}
.detail-body {
  padding: 12px 16px 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.detail-item {
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
}
.detail-item-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.detail-item-name { font-size: var(--fs-xs); font-weight: 600; color: var(--fg); }
.detail-item-meta { color: var(--fg-faint); font-size: var(--fs-3xs); }
.detail-pre {
  margin: 6px 0 0;
  color: var(--fg-muted);
  font-family: var(--font-mono);
  font-size: var(--fs-3xs);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-line {
  margin-top: 6px;
  color: var(--fg-muted);
  font-size: var(--fs-3xs);
  word-break: break-word;
}
.detail-web {
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-sunken);
  text-decoration: none;
}
.detail-web:hover { border-color: var(--accent-line); }
.detail-web-index {
  color: var(--accent-ink);
  font-family: var(--font-mono);
  font-size: var(--fs-3xs);
}
.detail-web-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.detail-web-title { color: var(--fg); font-size: var(--fs-xs); }
.detail-web-url {
  color: var(--fg-faint);
  font-family: var(--font-mono);
  font-size: var(--fs-3xs);
  word-break: break-all;
}
.detail-web-snippet { color: var(--fg-muted); font-size: var(--fs-3xs); line-height: 1.6; }
@keyframes detailMaskIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes detailWindowIn {
  from { opacity: 0; transform: translateY(6px) scale(0.99); }
  to { opacity: 1; transform: none; }
}
</style>
