<script setup lang="ts">
import type { AssistantPendingAction } from "@/api/types";

/** 生成卡片只依赖这些字段；父组件的 ActionCard 结构上兼容。 */
interface GenerationCard {
  action: AssistantPendingAction;
  state: "pending" | "running" | "confirmed" | "failed";
  notice: string;
  restored?: boolean;
  segmentNo?: number | null;
  preview?: string;
  progressChars?: number;
  progressSeconds?: number;
  progressTarget?: number;
  progressReasoning?: number;
  progressStage?: string;
}

const props = defineProps<{ card: GenerationCard }>();
const emit = defineEmits<{ (e: "open"): void }>();

/** 写作卡片的状态文案 / 色标（文字与颜色双编码）。 */
function genStateText(card: GenerationCard): string {
  if (card.restored) return "历史";
  if (card.state === "running") return "生成中";
  if (card.state === "confirmed") return "已生成";
  if (card.state === "failed") return "失败";
  return "待开始";
}

function genStateTag(card: GenerationCard): string {
  if (card.restored) return "info";
  if (card.state === "running") return "info";
  if (card.state === "confirmed") return "ok";
  if (card.state === "failed") return "err";
  return "warn";
}

/** 卡片预览：只展示开头一段；完整正文永远在写作面板里。 */
function genPreview(card: GenerationCard): string {
  if (card.preview) return card.preview;
  if (card.state === "running") return "正在写作面板中流式生成，点开可查看进度。";
  if (card.state === "failed") return card.notice || "生成失败，点开重试。";
  if (card.state === "confirmed") return card.notice || "正文已生成。";
  return card.action.body?.action === "regenerate_segment"
    ? "点开在写作面板重写这一段。"
    : "点开在写作面板续写下一段。";
}

function genFoot(card: GenerationCard): string {
  if (card.restored) return "刷新前记录 · 点开查看最新正文";
  if (card.state === "running") return "正文只在写作面板里流式输出";
  if (card.state === "confirmed") return "点开可查看、编辑或重生成";
  if (card.state === "failed") return "点开重试";
  return "点开开始生成";
}

/** 进度百分比：不知道总量时按目标字数估，封顶 99% 等 done。 */
function genPercent(card: GenerationCard): number {
  const chars = card.progressChars ?? 0;
  const target = Math.max(1, card.progressTarget ?? 3000);
  return Math.min(99, Math.round((chars / target) * 100));
}
</script>

<template>
  <!-- 写作卡片：聊天里只留一张小卡，点开去右侧写作面板看/改正文 -->
  <div
    class="gen-card"
    :class="props.card.state"
    role="button"
    tabindex="0"
    @click="emit('open')"
    @keydown.enter="emit('open')"
  >
    <div class="gen-card-head">
      <span class="gen-card-kicker">写作卡片</span>
      <span class="tag mono">
        {{ props.card.segmentNo ? `第 ${props.card.segmentNo} 段` : "续写下一段" }}
      </span>
      <span class="tag" :class="genStateTag(props.card)">{{ genStateText(props.card) }}</span>
    </div>
    <div class="gen-card-title">{{ props.card.action.title }}</div>
    <p class="gen-card-preview">{{ genPreview(props.card) }}</p>
    <div v-if="props.card.state === 'running'" class="gen-progress">
      <div class="gen-progress-head">
        <span>
          <template v-if="props.card.progressStage">
            作者 Agent · {{ props.card.progressStage }}
            <template v-if="props.card.progressChars"> · {{ props.card.progressChars }} 字</template>
          </template>
          <template v-else-if="props.card.progressChars">
            已生成 {{ props.card.progressChars }} 字
          </template>
          <template v-else-if="props.card.progressReasoning">
            模型思考中 · {{ props.card.progressReasoning }} 字
          </template>
          <template v-else>等待首字…</template>
        </span>
        <span>
          已等待 {{ props.card.progressSeconds ?? 0 }} 秒
          <template v-if="props.card.progressTarget">
            · 目标约 {{ props.card.progressTarget }} 字
          </template>
        </span>
      </div>
      <div class="gen-progress-track">
        <span
          class="gen-progress-fill"
          :class="{ waiting: !props.card.progressChars }"
          :style="props.card.progressChars ? { width: genPercent(props.card) + '%' } : undefined"
        />
      </div>
    </div>
    <div class="gen-card-foot">
      <span class="faint">{{ genFoot(props.card) }}</span>
      <span class="gen-card-open">打开写作面板 →</span>
    </div>
  </div>
</template>

<style scoped>
.gen-card {
  width: 100%;
  align-self: stretch;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--bg);
  cursor: pointer;
  transition: border-color 0.15s ease;
}
.gen-card:hover { border-color: var(--accent-line); }
.gen-card:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.gen-card.running { border-left-color: var(--info); }
.gen-card.failed { border-left-color: var(--err, #c0392b); }
.gen-card-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.gen-card-kicker {
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.gen-card-title {
  font-size: var(--fs-md);
  font-weight: 600;
  line-height: 1.5;
}
.gen-card-preview {
  margin: 0;
  color: var(--fg-muted);
  font-size: var(--fs-sm);
  line-height: 1.7;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.gen-progress {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.gen-progress-head {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--fg-faint);
  font-size: var(--fs-2xs);
}
.gen-progress-track {
  position: relative;
  height: 4px;
  border-radius: var(--radius-full);
  background: var(--bg-sunken);
  overflow: hidden;
}
.gen-progress-fill {
  display: block;
  height: 100%;
  width: 0;
  border-radius: var(--radius-full);
  background: var(--accent);
  transition: width 0.4s ease;
}
.gen-progress-fill.waiting {
  width: 35%;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  animation: gen-wait 1.2s linear infinite;
}
@keyframes gen-wait {
  from { transform: translateX(-100%); }
  to { transform: translateX(300%); }
}
.gen-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--fs-2xs);
}
.gen-card-open {
  color: var(--accent-ink);
  font-weight: 600;
}
</style>
