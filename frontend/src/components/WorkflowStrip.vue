<script setup lang="ts">
/**
 * 工序链进度条：助理 agent 编排的全流程走到哪一步、卡在哪儿。
 *
 * 数据来自 `GET /api/projects/{id}/workflow` —— 服务端**重新评估**出来的状态
 * （查库 + 只读规则），不是日志里的一句话：正文改过、蒸馏重跑过、提案批了，
 * 状态都会跟着变。
 *
 * 面板只做两件事：
 *   1. **显示**工序走到哪、卡在哪（两条轨：先备提示词，再写正文）；
 *   2. **处理等着你的改动**（批准 / 写进正文）—— 走的是既有的审批闸门，
 *      刻意**分两步**：先批准，再写进正文。合成一个「一键生效」就等于
 *      取消了「先看清楚改了哪儿再决定」这一步。
 *
 * 需要用户决断的地方（蒸馏用哪一档、一次几章）由对话流里的「选择框」承接，
 * 面板不重复造交互。
 */
import { computed, onMounted, ref, watch } from "vue";
import { ApiError, api } from "@/api/client";
import type {
  ActionProposalOut,
  AssistantProposal,
  WorkflowPendingProposal,
  WorkflowState,
} from "@/api/types";
import {
  actionKindLabel,
  actionProposalState,
  actionRiskLabel,
  pendingProposalTone,
  proposalStateLabel as stateLabel,
  proposalTargetLabel as targetLabel,
  workflowTracks,
} from "@/utils/workflowDisplay";

const props = defineProps<{ projectId: number }>();

const state = ref<WorkflowState | null>(null);
/** 等着你批准/执行的动作（agent 提的「做什么事」）。 */
const actions = ref<ActionProposalOut[]>([]);
/** 已经落定/被否掉的提案（改过什么的历史）。 */
const history = ref<AssistantProposal[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);

/** 两轨分组后的展示模型（纯函数算，见 utils/workflowDisplay.ts）。 */
const tracks = computed(() =>
  state.value ? workflowTracks(state.value.steps, state.value.current_step) : [],
);

/** 等着你处理的改动。 */
const openProposals = computed<WorkflowPendingProposal[]>(
  () => state.value?.open_proposals ?? [],
);

/** 正在处理的提案 id（按钮禁用 + 转圈）。 */
const busyId = ref<number | null>(null);
/** 一句人话反馈（做完之后说什么 / 失败为什么）。 */
const notice = ref<string | null>(null);
/** 展开了改动细节的提案 id。 */
const openDiffId = ref<number | null>(null);
/** 改动细节（按提案 id 存 diff 文本）。 */
const diffs = ref<Record<number, string>>({});

/** 改哪儿 + 什么状态（人话；不出现 target_kind / status 这类内部名）。 */
function proposalTargetLabel(targetKind: string): string {
  return targetLabel(targetKind);
}

function proposalStateLabel(status: string): string {
  return stateLabel(status);
}

/** 展开了哪条历史改动的 diff。 */
const openHistoryDiffId = ref<number | null>(null);

/**
 * 两处默认**收起**。
 *
 * 为什么：这个面板在对话框**上面**，展开成九条会把输入框顶出屏幕。
 * 紧凑条一行就能看完「走到哪、卡在哪」，细节按需展开。
 */
const showSteps = ref(false);
const showHistory = ref(false);

function toggleHistoryDiff(proposal: AssistantProposal): void {
  openHistoryDiffId.value = openHistoryDiffId.value === proposal.id ? null : proposal.id;
}

/** 时间：只到分钟（够人认「什么时候改的」）。 */
function fmtWhen(value: string | null | undefined): string {
  if (!value) return "";
  return value.replace("T", " ").slice(0, 16);
}

async function load(): Promise<void> {
  if (!props.projectId) return;
  loading.value = true;
  try {
    state.value = await api.getWorkflow(props.projectId);
    // 动作提案与文本提案**分两张表**：一个说「改哪儿」，一个说「做什么事」。
    const list = await api.listActionProposals(props.projectId);
    actions.value = list.proposals.filter(
      (p) => p.status === "pending" || p.status === "approved",
    );
    // 改动历史：已落定/被否掉的提案。上面那节只列「还等你处理」的，
    // 批完就没影了 —— 改过什么、为什么改，得留得住。
    const done = await api.listProposals(props.projectId, null, 20);
    history.value = (done.proposals ?? []).filter(
      (p) => p.status !== "pending" && p.status !== "approved",
    );
    error.value = null;
  } catch (err) {
    error.value = errorText(err);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
watch(() => props.projectId, load);
defineExpose({ load });

/** 把后端的报错翻成人话。 */
function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    const detail = err.detail;
    if (err.status === 409) {
      if (detail && typeof detail === "object") {
        const message = (detail as Record<string, unknown>).message;
        if (typeof message === "string" && message) return message;
      }
      return "要改的内容已经被改动过了，这条建议需要重新提一次。";
    }
    if (typeof detail === "string" && detail) return detail;
    return `没办成（${err.status}）。`;
  }
  return err instanceof Error ? err.message : String(err);
}

/** 展开/收起改动细节（只读：拉提案详情，不动任何东西）。 */
async function toggleDiff(proposal: WorkflowPendingProposal): Promise<void> {
  if (openDiffId.value === proposal.id) {
    openDiffId.value = null;
    return;
  }
  openDiffId.value = proposal.id;
  if (diffs.value[proposal.id] !== undefined) return;
  try {
    const detail = await api.getProposal(proposal.id);
    diffs.value = { ...diffs.value, [proposal.id]: detail.proposal.diff || "（没有改动细节）" };
  } catch (err) {
    diffs.value = { ...diffs.value, [proposal.id]: errorText(err) };
  }
}

/**
 * 处理一条改动。三步各自独立，不合并：
 *   approve —— 批准（只改状态，正文一个字不动）
 *   apply   —— 写进正文（唯一会改业务表的一步）
 *   reject  —— 拒绝（终态）
 */
async function act(
  proposal: WorkflowPendingProposal,
  action: "approve" | "apply" | "reject",
): Promise<void> {
  busyId.value = proposal.id;
  notice.value = null;
  try {
    if (action === "approve") {
      const res = await api.approveProposal(proposal.id, { action: "approve" });
      notice.value = res.note ?? "已批准。正文还没改 —— 要写进去再点一次「写进正文」。";
    } else if (action === "apply") {
      const res = await api.applyProposal(proposal.id, { action: "apply" });
      notice.value = `已写进正文（版本 ${res.version_id}）。`;
    } else {
      const res = await api.rejectProposal(proposal.id, { action: "reject" });
      notice.value = res.note ?? "已拒绝，这条不会再动。";
    }
    await load();
  } catch (err) {
    notice.value = errorText(err);
  } finally {
    busyId.value = null;
  }
}

/** 这条改动现在该点什么按钮（由状态决定，不由前端自由发挥）。 */
function actionFor(proposal: WorkflowPendingProposal): "approve" | "apply" {
  return proposal.status === "approved" ? "apply" : "approve";
}

/**
 * 这条动作现在该点什么按钮（由状态决定，不由前端自由发挥）。
 *
 * 单独一个函数是为了类型安全：状态里的 "none"（没有可点的动作）不能直接
 * 传给执行函数 —— 模板里的 v-if 不做类型收窄。
 */
function primaryAction(proposal: ActionProposalOut): "approve" | "apply" {
  return actionProposalState(proposal).action === "apply" ? "apply" : "approve";
}

/**
 * 处理一个**动作**提案（agent 提的「做什么事」）。
 *
 * 同样分两步：批准 → 执行。批准**不执行**那个动作（审批是人的判断，
 * 执行是系统的动作）；合成一步就取消了「先看清楚要做什么再决定」。
 */
async function actAction(
  proposal: ActionProposalOut,
  action: "approve" | "apply" | "reject",
): Promise<void> {
  busyId.value = proposal.id;
  notice.value = null;
  try {
    if (action === "approve") {
      const res = await api.approveActionProposal(proposal.id, { action: "approve" });
      notice.value = res.note ?? "已批准。动作还没执行 —— 要执行再点一次。";
    } else if (action === "apply") {
      const res = await api.applyActionProposal(proposal.id, { action: "apply" });
      notice.value = res.note ?? "已执行。";
    } else {
      const res = await api.rejectActionProposal(proposal.id, { action: "reject" });
      notice.value = res.note ?? "已拒绝，这条不会再动。";
    }
    await load();
  } catch (err) {
    notice.value = errorText(err);
  } finally {
    busyId.value = null;
  }
}
</script>

<template>
  <section class="card wf-strip">
    <div class="card-title">
      全流程进度
      <span class="row tight">
        <button class="link-btn" @click="showSteps = !showSteps">
          {{ showSteps ? "收起" : "展开" }}
        </button>
        <button class="link-btn" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </span>
    </div>

    <div v-if="error" class="notice err">{{ error }}</div>
    <div v-else-if="!state" class="muted">读取工序状态…</div>

    <template v-else>
      <!-- 紧凑条：一行看完「走到哪、卡在哪」。详情默认收起 ——
           展开成九条会把下面的对话框顶出屏幕。 -->
      <div class="wf-compact">
        <span v-for="track in tracks" :key="track.id" class="wf-compact-track">
          <span class="wf-compact-label">{{ track.title }}</span>
          <span
            v-for="item in track.steps"
            :key="item.step.id"
            class="wf-mini"
            :class="item.tone"
            :title="`${item.step.title}｜${item.label}｜${item.step.reason}`"
          />
        </span>
        <span class="wf-compact-now" :title="state.summary">{{ state.summary }}</span>
      </div>

      <template v-if="showSteps">
        <!-- 两条轨：先备提示词，再写正文。分组是为了让顺序一眼可见。 -->
        <div v-for="track in tracks" :key="track.id" class="wf-track-row">
          <div class="wf-track-title">{{ track.title }}</div>
          <div class="row tight wf-steps">
            <span
              v-for="item in track.steps"
              :key="item.step.id"
            class="wf-step"
            :class="[item.tone, { 'wf-current': item.current }]"
            :title="`${item.step.title}｜${item.step.reason}｜派发：${item.step.dispatch}`"
          >
            <span class="wf-dot" />
            {{ item.step.title }}
            <span class="wf-state">{{ item.label }}</span>
          </span>
        </div>
      </div>

      <div v-if="state.needs_decision" class="notice info">
        需要你定：{{ state.needs_decision.title }} —— {{ state.needs_decision.question }}
      </div>
      </template>
    </template>
  </section>

  <!-- 等着你处理的改动：批准与「写进正文」刻意分两步，
       先看清楚改了哪儿，再决定要不要生效。 -->
  <section v-if="openProposals.length" class="card wf-strip">
    <div class="card-title">等着你处理的改动（{{ openProposals.length }}）</div>

    <div v-for="p in openProposals" :key="p.id" class="wf-proposal">
      <div class="wf-proposal-head">
        <span class="wf-proposal-target">{{ p.target }}</span>
        <span class="wf-proposal-risk" :class="pendingProposalTone(p)">{{ p.risk }}</span>
        <span v-if="p.status === 'approved'" class="wf-proposal-risk warn">已批准，待写进正文</span>
      </div>
      <div v-if="p.reason" class="wf-proposal-reason">{{ p.reason }}</div>

      <div class="row tight wf-proposal-actions">
        <button class="link-btn" @click="toggleDiff(p)">
          {{ openDiffId === p.id ? "收起改动" : "看改动" }}
        </button>
        <button
          class="link-btn"
          :disabled="busyId === p.id"
          @click="act(p, actionFor(p))"
        >
          {{ busyId === p.id
            ? "处理中…"
            : actionFor(p) === "approve" ? "批准" : "写进正文" }}
        </button>
        <button
          v-if="p.status === 'pending'"
          class="link-btn"
          :disabled="busyId === p.id"
          @click="act(p, 'reject')"
        >
          拒绝
        </button>
      </div>

      <pre v-if="openDiffId === p.id" class="wf-diff">{{ diffs[p.id] ?? "读取中…" }}</pre>
    </div>

    <div v-if="notice" class="notice info">{{ notice }}</div>
  </section>

  <!-- 等着你批准的动作（agent 提的「做什么事」）。
       与上面那节是**两回事**：那节是「改哪些字」，这节是「做什么事」；
       措辞也不同（这里说「批准 / 执行」，不说「写进正文」）。 -->
  <section v-if="actions.length" class="card wf-strip">
    <div class="card-title">等着你批准的动作（{{ actions.length }}）</div>

    <div v-for="a in actions" :key="a.id" class="wf-proposal">
      <div class="wf-proposal-head">
        <span class="wf-proposal-target">{{ actionKindLabel(a.kind) }}</span>
        <span class="wf-proposal-risk">{{ actionRiskLabel(a.risk_level) }}</span>
        <span class="wf-proposal-risk warn">{{ actionProposalState(a).label }}</span>
      </div>
      <div class="wf-proposal-reason">{{ a.title }}</div>
      <div v-if="a.precondition_note" class="wf-proposal-reason muted">
        前提：{{ a.precondition_note }}
      </div>

      <div class="row tight wf-proposal-actions">
        <button
          v-if="actionProposalState(a).action !== 'none'"
          class="link-btn"
          :disabled="busyId === a.id"
          @click="actAction(a, primaryAction(a))"
        >
          {{ busyId === a.id
            ? "处理中…"
            : actionProposalState(a).action === "approve" ? "批准" : "执行" }}
        </button>
        <button
          v-if="a.status === 'pending'"
          class="link-btn"
          :disabled="busyId === a.id"
          @click="actAction(a, 'reject')"
        >
          拒绝
        </button>
      </div>
    </div>

    <div v-if="notice" class="notice info">{{ notice }}</div>
  </section>

  <!-- 改动历史：批完/否掉的提案留个底 —— 改过什么、为什么改、什么时候改的。
       默认收起：它只是「查账用」的，展开会把下面的对话框顶下去。 -->
  <section v-if="history.length" class="card wf-strip">
    <div class="card-title">
      改动历史（{{ history.length }}）
      <button class="link-btn" @click="showHistory = !showHistory">
        {{ showHistory ? "收起" : "展开" }}
      </button>
    </div>

    <template v-if="showHistory">
    <div v-for="p in history" :key="p.id" class="wf-proposal">
      <div class="wf-proposal-head">
        <span class="wf-proposal-target">{{ proposalTargetLabel(p.target_kind) }}</span>
        <span class="wf-proposal-risk">{{ proposalStateLabel(p.status) }}</span>
        <span v-if="p.created_at" class="faint" style="font-size: var(--fs-xs)">
          {{ fmtWhen(p.created_at) }}
        </span>
      </div>
      <div v-if="p.reason" class="wf-proposal-reason">{{ p.reason }}</div>

      <div class="row tight wf-proposal-actions">
        <button
          v-if="p.diff"
          class="link-btn"
          @click="toggleHistoryDiff(p)"
        >
          {{ openHistoryDiffId === p.id ? "收起改动" : "看改了什么" }}
        </button>
      </div>

      <pre v-if="openHistoryDiffId === p.id" class="wf-diff">{{ p.diff }}</pre>
    </div>
    </template>
  </section>
</template>

<style scoped>
.wf-strip {
  margin-bottom: 16px;
}

/* 紧凑条：一行看完「走到哪、卡在哪」。 */
.wf-compact {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.wf-compact-track {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.wf-compact-label {
  font-size: var(--fs-xs);
  color: var(--muted, rgba(127, 127, 127, 0.95));
  margin-right: 2px;
}
.wf-mini {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--line, rgba(127, 127, 127, 0.5));
}
.wf-mini.wf-done {
  background: var(--ok, #2f9e6b);
}
.wf-mini.wf-pending {
  background: var(--accent, #4a7dff);
}
.wf-mini.wf-blocked {
  background: var(--err, #d4574f);
}
.wf-mini.wf-skip {
  opacity: 0.35;
}
.wf-compact-now {
  flex: 1 1 220px;
  min-width: 0;
  font-size: var(--fs-sm);
  color: var(--muted, rgba(127, 127, 127, 0.95));
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wf-track-row + .wf-track-row {
  margin-top: 10px;
}

.wf-track-title {
  font-size: var(--fs-xs);
  color: var(--muted, rgba(127, 127, 127, 0.95));
  margin-bottom: 4px;
}

.wf-steps {
  flex-wrap: wrap;
  gap: 6px;
}

.wf-step {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--line, rgba(127, 127, 127, 0.25));
  border-radius: 999px;
  font-size: var(--fs-xs);
  line-height: 1.6;
}

.wf-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--line, rgba(127, 127, 127, 0.5));
}

.wf-done .wf-dot {
  background: var(--ok, #2f9e6b);
}

.wf-pending .wf-dot {
  background: var(--accent, #4a7dff);
}

.wf-blocked .wf-dot {
  background: var(--err, #d4574f);
}

.wf-skip {
  opacity: 0.5;
}

/* 当前该动的那一步：描边 + 底色，不只靠颜色区分。 */
.wf-current {
  border-color: var(--accent, #4a7dff);
  background: rgba(74, 125, 255, 0.08);
  font-weight: 600;
}

.wf-state {
  opacity: 0.75;
  font-size: var(--fs-xs);
}

.wf-summary {
  margin-top: 10px;
  font-size: var(--fs-sm);
  color: var(--muted, rgba(127, 127, 127, 0.95));
}

/* ——— 等着你处理的改动 ——— */
.wf-proposal {
  padding: 10px 0;
  border-top: 1px solid var(--line, rgba(127, 127, 127, 0.18));
}

.wf-proposal:first-of-type {
  border-top: none;
  padding-top: 2px;
}

.wf-proposal-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.wf-proposal-target {
  font-weight: 600;
}

.wf-proposal-risk {
  font-size: var(--fs-xs);
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--line, rgba(127, 127, 127, 0.25));
}

.wf-proposal-risk.err {
  border-color: var(--err, #d4574f);
  color: var(--err, #d4574f);
}

.wf-proposal-risk.warn {
  border-color: var(--warn, #c08a2e);
  color: var(--warn, #c08a2e);
}

.wf-proposal-reason {
  margin-top: 4px;
  font-size: var(--fs-sm);
}

.wf-proposal-actions {
  margin-top: 6px;
}

.wf-diff {
  margin-top: 6px;
  padding: 8px 10px;
  max-height: 260px;
  overflow: auto;
  background: rgba(127, 127, 127, 0.08);
  border-radius: 6px;
  font-size: var(--fs-xs);
  white-space: pre-wrap;
}
</style>
