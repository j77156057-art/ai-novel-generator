<script setup lang="ts">
import type { AssistantPendingAction } from "@/api/types";
import {
  pendingActionKindLabel,
  pendingActionRiskMeta,
} from "@/utils/assistantDisplay";

interface CardRow {
  key: string;
  label: string;
  value: string;
}

interface TraceRow {
  at: number;
  text: string;
  tone: "info" | "ok" | "err";
}

/** 动作卡片只依赖这些字段；父组件的 ActionCard 结构上兼容。 */
interface ActionCardView {
  key: string;
  action: AssistantPendingAction;
  state: "pending" | "running" | "confirmed" | "failed";
  rows: CardRow[];
  notice: string;
  result: Record<string, unknown> | null;
  applied?: boolean;
  trace: TraceRow[];
}

const props = defineProps<{
  card: ActionCardView;
  /** 已等待时长（由父组件每秒重算，避免组件里再养一个时钟）。 */
  elapsed: string;
  /** 「批准并写入」的忙态（父组件按 card.key 维护）。 */
  approving: boolean;
  /** 主按钮文案（父组件按卡片类型/状态统一计算）。 */
  primaryLabel: string;
  /** 结果里待批准提案的 id；null = 无提案。 */
  proposalId: number | null;
  /** 展开态查询（与父组件的明细展开共用一份状态）。 */
  isOpen: (key: string) => boolean;
}>();

const emit = defineEmits<{
  (e: "confirm"): void;
  (e: "cancel"): void;
  (e: "approve"): void;
  (e: "toggle", key: string): void;
}>();

const RISK_LABEL: Record<string, string> = {
  low: "低风险 low",
  medium: "中风险 medium",
  high: "高风险 high",
};

function riskLabel(r: string): string {
  return RISK_LABEL[r] ?? r;
}

function riskMetaOf(card: ActionCardView): { label: string; tag: string; hint: string } {
  return pendingActionRiskMeta(card.action);
}

function clockText(at: number): string {
  const d = new Date(at);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}

/** 结果响应里值得展开看的字段（正文 / diff），其余已在 notice 里说完。 */
function resultExtras(
  res: Record<string, unknown> | null,
): { label: string; text: string }[] {
  if (!res) return [];
  const out: { label: string; text: string }[] = [];
  const proposal = res.proposal;
  if (proposal && typeof proposal === "object") {
    const p = proposal as Record<string, unknown>;
    if (typeof p.diff === "string" && p.diff) out.push({ label: "改动差异 diff", text: p.diff });
    if (typeof p.after_text === "string" && p.after_text) {
      out.push({ label: "改后全文 after_text", text: p.after_text });
    }
  }
  const segment = res.segment;
  if (segment && typeof segment === "object") {
    const s = segment as Record<string, unknown>;
    if (typeof s.text === "string" && s.text) out.push({ label: "生成正文", text: s.text });
  }
  return out;
}

function rowKey(index: number): string {
  return `rx-${props.card.key}-${index}`;
}
</script>

<template>
  <div class="card-pending">
    <div class="row tight" style="align-items: center">
      <span class="tag accent">{{ pendingActionKindLabel(props.card.action.kind) }}</span>
      <span class="tag mono">kind={{ props.card.action.kind }}</span>
      <span class="tag" :class="riskMetaOf(props.card).tag">
        {{ riskMetaOf(props.card).label }}
      </span>
      <span
        v-if="props.card.action.body && typeof props.card.action.body.risk_level === 'string'"
        class="tag mono"
      >
        {{ riskLabel(String(props.card.action.body.risk_level)) }}
      </span>
    </div>

    <div class="card-title">{{ props.card.action.title }}</div>
    <div class="card-hint">{{ riskMetaOf(props.card).hint }}</div>

    <div v-if="props.card.rows.length" class="meta-grid">
      <div v-for="row in props.card.rows" :key="row.key">
        <span class="blk-head">{{ row.label }}</span>
        <div class="mono" style="font-size: var(--fs-xs)">{{ row.value }}</div>
      </div>
    </div>

    <!-- 待确认时明确告知将调用的端点：用户有权知道按下去会发生什么 -->
    <div v-if="props.card.state === 'pending'" class="faint mono endpoint">
      将执行 {{ props.card.action.method }} {{ props.card.action.endpoint }}（请求体由服务端下发，
      前端不参与构造）
    </div>

    <div v-if="props.card.state === 'running'" class="notice info">
      执行中 · 已等待 {{ props.elapsed }}
    </div>

    <div v-if="props.card.state === 'confirmed'" class="notice ok confirm-box">
      <div>{{ props.card.notice }}</div>
      <div
        v-for="(x, i) in resultExtras(props.card.result)"
        :key="`x-${props.card.key}-${i}`"
        class="blk"
      >
        <span class="blk-head">{{ x.label }}</span>
        <pre
          class="diffbox snippet"
          :class="{ expanded: props.isOpen(rowKey(i)) }"
        >{{ x.text }}</pre>
        <button class="link-btn" @click="emit('toggle', rowKey(i))">
          {{ props.isOpen(rowKey(i)) ? "收起" : "展开完整内容" }}
        </button>
      </div>

      <!--
        提案类卡片：确认只创建了待批提案，业务表还没动。
        在这里补上「批准 → 写入」，用户不必再跳去项目页找。
      -->
      <!-- 文本提案（kind=proposal）才走 /proposals 的批准+写入；
           动作提案（kind=action_proposal）是另一张表、另一组端点，
           误用这里会拿错 id 去批文本提案，必须在右侧工序链里处理。 -->
      <div
        v-if="props.card.action.kind === 'proposal' && props.proposalId !== null && !props.card.applied"
        class="promote-row"
      >
        <span class="faint">提案已创建，项目数据还没改。</span>
        <button
          class="btn primary sm"
          :disabled="props.approving"
          @click="emit('approve')"
        >
          {{ props.approving ? "写入中…" : "批准并写入" }}
        </button>
      </div>
      <div
        v-else-if="props.card.action.kind === 'action_proposal' && props.proposalId !== null"
        class="faint"
      >
        动作提案已创建，项目数据还没改；请在右侧「等着你批准的动作」里批准并执行。
      </div>
      <div v-else-if="props.card.applied" class="faint">已写入项目。</div>
    </div>

    <div v-if="props.card.state === 'failed'" class="notice err confirm-box">
      <div>{{ props.card.notice }}</div>
    </div>

    <!-- 执行轨迹：确认 / 请求发出 / 落库 / 完成，各行都带本地时刻 -->
    <ol v-if="props.card.trace.length" class="trace">
      <li
        v-for="(s, i) in props.card.trace"
        :key="`t-${props.card.key}-${i}`"
        :class="s.tone"
      >
        <span class="trace-time mono">{{ clockText(s.at) }}</span>
        <span class="trace-text">{{ s.text }}</span>
      </li>
    </ol>

    <div class="row tight">
      <button
        class="btn primary"
        :disabled="props.card.state !== 'pending' && props.card.state !== 'failed'"
        @click="emit('confirm')"
      >
        {{ props.primaryLabel }}
      </button>
      <button
        class="btn"
        :disabled="props.card.state === 'running' || props.card.state === 'confirmed'"
        @click="emit('cancel')"
      >
        取消
      </button>
    </div>
  </div>
</template>

<style scoped>
.card-pending {
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
  padding: 12px 14px;
  border: var(--border-w) solid var(--border-strong);
  /* 左侧色条只是辅助：真正的等级判断靠上面的文字标签。 */
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.card-title {
  font-weight: 700;
  font-size: var(--fs-md);
  line-height: 1.6;
}
.card-hint {
  color: var(--fg-muted);
  font-size: var(--fs-sm);
  line-height: 1.6;
}
.endpoint {
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px 16px;
  font-size: var(--fs-sm);
}
.confirm-box {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
  line-height: 1.7;
}
/* 提案确认后的「批准并写入」行：与上面的回执分开，动作更醒目 */
.promote-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding-top: var(--gap-md);
  border-top: 1px dashed var(--border);
}
.promote-row .btn { flex: none; }
/* ---- 执行轨迹（确认 → 请求发出 → 落库 → 完成） ---- */
.trace {
  list-style: none;
  margin: 0;
  padding: 0 0 0 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  border-left: 2px solid var(--border-strong);
}
.trace li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--fg-muted);
}
.trace-time {
  flex: 0 0 auto;
  color: var(--fg-faint);
}
.trace-text {
  word-break: break-word;
}
.trace li.ok .trace-text {
  color: var(--ok);
}
.trace li.err .trace-text {
  color: var(--danger);
}
/* 与父组件同名工具类保持一致（父组件还有别处用到这些规则）。 */
.blk {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.blk-head {
  color: var(--fg-faint);
  font-weight: 700;
  font-size: var(--fs-xs);
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
