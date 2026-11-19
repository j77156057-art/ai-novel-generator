<script setup lang="ts">
defineOptions({ name: "AssistantWorkspace" });
/**
 * 助手工作台（Assistant Workspace）—— 只有一条对话流。
 *
 * 路由：/projects/:id/assistant（项目级资源，与 /projects/:id 并列）。
 *
 * 形态（2026-10-14 产品决策）：
 *   助手**只提供问答框**。诊断 / 日志分析 / 三类审核不是六个并排的操作
 *   面板，而是 agent 自己判断「这次需要」时调用的只读能力 —— 让用户去
 *   手动点「跑诊断」等于把调度权交回给人，助手就退化成了遥控器。
 *   所以后端把这些能力注册成 read_only 工具，前端只负责把它们跑出来的
 *   issues 展示在回答里（复用 AssistantIssueList）。
 *
 * 唯一的例外是**写操作**。agent 不得自行 apply 提案或触发生成，但它可以
 * 「提出」：回答里附一张 pending_actions 卡片，由用户点确认才执行。
 * 这是本文件除问答之外唯一的一处交互 —— 没有第二套表单。
 *
 * 三条产品红线在界面上的落点：
 *   1. 写操作必须人确认：卡片只有「确认 / 取消」两个按钮，服务端下发什么
 *      endpoint + body，前端就原样发什么（见 api/client.ts
 *      `assistantRunPendingAction`），前端不参与请求构造。
 *   2. 危险程度必须可见：覆盖型动作（重写已有段）与追加型动作（新建提案、
 *      续写）用**文字 + 色标双编码**区分，不能只靠颜色。
 *   3. 漂移即失效：确认时撞 409(stale) 明确说「目标已被改动」，而不是笼统
 *      弹一句「操作失败」。
 *
 * 与后端的契约：POST /api/assistant/qa。诊断 / 日志 / 审核结果通过响应里的
 * `issues` 回来（agent 自主调的只读工具），待确认写操作通过
 * `pending_actions` 回来。
 */
import { computed, nextTick, onActivated, onDeactivated, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api } from "@/api/client";
import AssistantIssueList from "@/components/AssistantIssueList.vue";
import WizardStepCard from "@/components/WizardStepCard.vue";
import WorkflowStrip from "@/components/WorkflowStrip.vue";
import { useLlmConfig } from "@/composables/useLlmConfig";
import { createAttachmentDraft } from "@/utils/assistantAttachments";
import { pendingActionKindLabel, pendingActionRiskMeta, sourceLabel } from "@/utils/assistantDisplay";
import { useModalResize } from "@/utils/useModalResize";
import type {
  AssistantEvidence,
  AssistantIssue,
  AssistantMemory,
  AssistantPendingAction,
  AssistantToolTraceItem,
  AttachmentCleaningPreview,
  DistillJob,
  ProjectOut,
  WizardActionEcho,
  WizardCommitResult,
  WizardMessage,
  WizardNotice,
  WizardNoticePayload,
  WizardQuestion,
  WizardSayResponse,
  WizardSessionView,
} from "@/api/types";

const route = useRoute();
const router = useRouter();
const projectId = Number(route.params.id);
const badId = !Number.isInteger(projectId) || projectId <= 0;

// 页头：项目名 + 三个模型的生效徽标（一眼看出生成 / 助手 / 蒸馏是三件事）
const project = ref<ProjectOut | null>(null);
// 写作台的「当前作品」切换器：只做导航，不在这里改项目数据。
const bookOptions = ref<ProjectOut[]>([]);
const bookSwitcherVisible = computed(
  () => bookOptions.value.length > 1
    && bookOptions.value.some((b) => b.id === projectId),
);

async function loadBookOptions(): Promise<void> {
  try {
    bookOptions.value = await api.listProjects();
  } catch {
    bookOptions.value = [];
  }
}

function switchBook(event: Event): void {
  const id = Number((event.target as HTMLSelectElement).value);
  if (!Number.isFinite(id) || id === projectId) return;
  router.push(`/projects/${id}/assistant`);
}
const { rolesReady, roleConfig, load: loadLlm } = useLlmConfig();

const writerCfg = computed(() => roleConfig("writer"));
const assistantCfg = computed(() => roleConfig("assistant"));
const distillerCfg = computed(() => roleConfig("distiller"));

/** 「打开设置」直达唯一的模型设置入口（/settings）；此前是开一个全局弹窗。 */
function openModelSettings(): void {
  void router.push("/settings");
}

// ==================================================================
// 对话流的数据形状
// ==================================================================

/** 一张待确认卡片在**本地**的生命周期（服务端只管内容，不管这个）。 */
type CardState = "pending" | "running" | "confirmed" | "failed";

/** 卡片执行轨迹的一行（本地时间轴，不落库）。 */
interface TraceStep {
  at: number;
  text: string;
  tone: "info" | "ok" | "err";
}

interface ActionCard {
  /** 卡片在本次会话内的稳定 key（不用数组下标：删卡片不会让 key 串位）。 */
  key: string;
  action: AssistantPendingAction;
  state: CardState;
  /** detail 摊平后的展示行（收到时就摊好，避免模板里每次渲染重算）。 */
  rows: DetailRow[];
  /** 执行结果 / 失败原因（给人看的一句话）。 */
  notice: string;
  /** 执行成功后拿到的响应体，原样存下便于对账。 */
  result: Record<string, unknown> | null;
  /** 执行轨迹：确认时刻起按真实事件追加（见 pollCardTrace）。 */
  trace: TraceStep[];
  /** 点下确认的时刻（毫秒）；未执行为 null，用于算「已等待」。 */
  startedAt: number | null;
  /** 确认后第一次读到的已落库段数；之后变大 = 本次正文落库。 */
  baseCount: number | null;
  /** 已经记过「正文落库」步，避免每 2 秒重复追加。 */
  sawLanding: boolean;
  /** 后端是否提供进度端点；旧后端 404 时为 false，停止轮询。 */
  traceSupported: boolean;
}

interface Turn {
  /** 消息唯一 key。 */
  key: string;
  role: "user" | "assistant";
  text: string;
  /** user 侧：这条消息带上的附件（只回显文件名，不参与请求构造）。 */
  attachments?: { key: number; filename: string }[];
  /** assistant 侧：本次调用的出参（user 消息为 null）。 */
  answer: string;
  evidence: AssistantEvidence[];
  issues: AssistantIssue[];
  rulesEvaluated: string[];
  confidence: number;
  agentRunId: number;
  cards: ActionCard[];
  /** 本次取证的执行方式（旧后端不返回 → 空串，不显示相关提示）。 */
  toolMode: "model" | "deterministic" | "";
  /** 模型自主调用的工具轨迹（确定性路径为空数组）。 */
  toolTrace: AssistantToolTraceItem[];
  /** 工具名 → 结果体（只用来给人看「拿到了什么」，不参与任何请求构造）。 */
  toolResults: Record<string, unknown>;
  /** 回落原因（仅确定性路径非空）。 */
  toolFallbackReason: string;
  error: string | null;
  loading: boolean;
  /** 创作引导的一步（卡片）；与普通问答互斥。 */
  wizard?: WizardStepState | null;
  /** 引导里的一次「追问/没听懂」：只有解释文字，没有卡片，也不推进流程。 */
  note?: boolean;
  /** 引导里助理真的改了一次配置（mode=action）：回执前后值 + 可撤销。 */
  action?: WizardActionEcho | null;
  /** 撤销这次写操作用的审计 id；null = 不可撤销或已撤销。 */
  undoId?: number | null;
  /** 后端声明的可撤销性（触发蒸馏等为 false）。 */
  undoable?: boolean;
  undoBusy?: boolean;
  undoDone?: boolean;
  /** 引导提交后的执行结果（大纲 / 已就绪 / 已写完 / 角色模式）。 */
  wizardResult?: WizardCommitResult | null;
  /**
   * 一条持久化通知（完成回执 / 疑似卡住提醒）。
   *
   * 与 `wizard` 互斥：通知是后端在任务结束时写进 `wizard_notices` 的，
   * 前端轮询到就变成一条助手气泡，然后 ack 标掉。
   */
  notice?: WizardNotice | null;
  /** 从本地历史恢复的消息：只读文本，不挂任何可交互卡片。 */
  historical?: boolean;
}

interface WizardStepState {
  question: WizardQuestion;
  /** 已作答的选项文案；有值时卡片收起成一行。 */
  answeredLabel: string | null;
  busy: boolean;
  /** 已翻页/已回退的历史卡片：只读，防止重复提交。 */
  disabled: boolean;
}

// ==================================================================
// 会话状态
// ==================================================================

const turns = ref<Turn[]>([]);
const query = ref("");
/** 请求进行中禁止重复提交；卡片执行中的按钮单独由各自 state 控制。 */
const sending = ref(false);
const streamEl = ref<HTMLElement | null>(null);

/**
 * 附件草稿区。
 *
 * 上传只落到项目暂存区（`POST /api/assistant/attachments`），**不写库**；
 * 真正的「投放」必须由问答返回的待确认卡在用户点确认后触发 —— 本组件
 * 从不直接调投放端点，也不替用户决定「剔除混书」。
 */
const fileInput = ref<HTMLInputElement | null>(null);
const draft = reactive(createAttachmentDraft(api.uploadAssistantAttachment, textOf));
const canSend = computed(
  () => !sending.value && !badId && draft.request(query.value) !== null,
);

interface PreviewState {
  loading: boolean;
  error: string;
  data: AttachmentCleaningPreview | null;
}
const EMPTY_PREVIEW: PreviewState = { loading: false, error: "", data: null };
/** 每张草稿的清洗预览（按 attachment_id 缓存；只读，不落库）。 */
const previews = reactive<Record<string, PreviewState>>({});

function previewOf(attachmentId: string): PreviewState {
  return previews[attachmentId] ?? EMPTY_PREVIEW;
}

function addFiles(files: File[]): void {
  if (sending.value || badId) return;
  void draft.add(files);
}

function onFilesSelected(event: Event): void {
  const input = event.target as HTMLInputElement;
  addFiles(Array.from(input.files ?? []));
  input.value = "";
}

/** 展开 / 收起清洗预览：展开时才请求（避免每次上传都跑一遍扫描）。 */
async function togglePreview(attachmentId: string): Promise<void> {
  if (previews[attachmentId]?.loading) return;
  if (previews[attachmentId]?.data) {
    delete previews[attachmentId];
    return;
  }
  previews[attachmentId] = { loading: true, error: "", data: null };
  try {
    const data = await api.previewAssistantAttachment(projectId, attachmentId);
    if (previews[attachmentId]) previews[attachmentId] = { loading: false, error: "", data };
  } catch (err) {
    if (previews[attachmentId]) previews[attachmentId] = { loading: false, error: textOf(err), data: null };
  }
}

function resetAttachments(): void {
  draft.items = [];
  for (const id of Object.keys(previews)) delete previews[id];
}

let seq = 0;
function nextKey(prefix: string): string {
  seq += 1;
  return `${prefix}-${seq}`;
}

function makeUserTurn(text: string): Turn {
  return {
    key: nextKey("u"),
    role: "user",
    text,
    answer: "",
    evidence: [],
    issues: [],
    rulesEvaluated: [],
    confidence: 0,
    agentRunId: 0,
    cards: [],
    toolMode: "",
    toolTrace: [],
    toolResults: {},
    toolFallbackReason: "",
    error: null,
    loading: false,
  };
}

function makeAssistantTurn(): Turn {
  return {
    key: nextKey("a"),
    role: "assistant",
    text: "",
    answer: "",
    evidence: [],
    issues: [],
    rulesEvaluated: [],
    confidence: 0,
    agentRunId: 0,
    cards: [],
    toolMode: "",
    toolTrace: [],
    toolResults: {},
    toolFallbackReason: "",
    error: null,
    loading: true,
  };
}

/** 新内容进来后滚到底部（对话流的基本行为）。 */
async function scrollToBottom(): Promise<void> {
  await nextTick();
  const el = streamEl.value;
  if (el) el.scrollTop = el.scrollHeight;
}

// ==================================================================
// 历史持久化（localStorage，按项目隔离）
// ==================================================================
//
// 只存「能变成一段文字」的内容：问答正文、证据摘要、错误、附件文件名。
// 待确认卡片 / 引导卡 / 通知按钮一律不恢复 —— 它们背后的任务早已结束，
// 恢复成可点状态只会制造一个必然 409 的假按钮；历史里它们只作为文字出现。
//
// 为什么不走后端会话表：普通问答不写库是产品红线（助手可以看，但不能
// 因为聊天就改项目数据）。本地持久化只解决「刷新别丢」，不引入新的写路径。

/** 最近 30 轮（一问一答算一轮）。 */
const HISTORY_ROUNDS = 30;
const HISTORY_MAX_TURNS = HISTORY_ROUNDS * 2;

interface StoredTurn {
  role: "user" | "assistant";
  /** 用户原话。 */
  text: string;
  /** 助手回答（引导/通知类消息的正文也落到这里）。 */
  answer: string;
  error: string | null;
  confidence: number;
  agentRunId: number;
  attachments?: { filename: string }[];
  evidence?: AssistantEvidence[];
}

function historyKey(): string {
  return `assistant:chat:v1:${projectId}`;
}

/** 证据只留开头几段：足够认出「当时引了什么」，又不至于撑爆 5MB 配额。 */
function trimEvidence(list: AssistantEvidence[]): AssistantEvidence[] {
  return list.slice(0, 6).map((ev) => ({
    ...ev,
    snippet: (ev.snippet ?? "").slice(0, 400),
  }));
}

function serializeTurn(turn: Turn): StoredTurn | null {
  if (turn.loading) return null;
  const isUser = turn.role === "user";
  // 引导 / 通知 / 回执类消息没有 answer，正文在 text 里；统一落成文字记录。
  const answer = (turn.answer || turn.text || "").trim();
  if (!isUser && !answer && !turn.error) return null;
  const item: StoredTurn = {
    role: turn.role,
    text: isUser ? turn.text : "",
    answer,
    error: turn.error,
    confidence: turn.confidence,
    agentRunId: turn.agentRunId,
  };
  if (isUser && turn.attachments?.length) {
    item.attachments = turn.attachments.map((a) => ({ filename: a.filename }));
  }
  if (!isUser && turn.evidence.length) {
    item.evidence = trimEvidence(turn.evidence);
  }
  return item;
}

let historyReady = false;
let historyTimer: number | null = null;

function saveHistory(): void {
  if (!historyReady || badId) return;
  let slice = turns.value.slice(-HISTORY_MAX_TURNS);
  // 截断后如果从一条助手消息开场，往前对齐到用户消息，别留下没头没尾的回答。
  while (slice.length && slice[0].role === "assistant") slice = slice.slice(1);
  const payload: StoredTurn[] = [];
  for (const turn of slice) {
    const item = serializeTurn(turn);
    if (item) payload.push(item);
  }
  try {
    localStorage.setItem(historyKey(), JSON.stringify(payload));
  } catch {
    // 配额满 / 隐私模式：历史只是便利功能，写不进去不影响当前会话。
  }
}

function scheduleHistorySave(): void {
  if (!historyReady || badId) return;
  if (historyTimer !== null) window.clearTimeout(historyTimer);
  historyTimer = window.setTimeout(() => {
    historyTimer = null;
    saveHistory();
  }, 400);
}

/**
 * 立即落盘。
 *
 * 刷新/关页时浏览器不会等防抖计时器，也不会跑 onUnmounted —— 不在这里
 * 同步写一次，最后一条回答（尤其是刚失败的那条）就会丢。
 */
function flushHistory(): void {
  if (historyTimer !== null) {
    window.clearTimeout(historyTimer);
    historyTimer = null;
  }
  saveHistory();
}

/** 恢复本地历史：只还原文字，所有可交互字段保持空值。 */
function restoreHistory(): void {
  if (badId) return;
  let parsed: unknown;
  try {
    const raw = localStorage.getItem(historyKey());
    if (!raw) return;
    parsed = JSON.parse(raw);
  } catch {
    return;
  }
  if (!Array.isArray(parsed)) return;
  const restored: Turn[] = [];
  let index = 0;
  for (const raw of parsed) {
    if (!raw || typeof raw !== "object") continue;
    const s = raw as Partial<StoredTurn>;
    if (s.role !== "user" && s.role !== "assistant") continue;
    index += 1;
    const turn = s.role === "user" ? makeUserTurn(String(s.text ?? "")) : makeAssistantTurn();
    turn.key = `h-${index}`;
    turn.loading = false;
    turn.historical = true;
    if (s.role === "user") {
      const names = Array.isArray(s.attachments) ? s.attachments : [];
      const attachments = names
        .filter((a) => a && typeof a.filename === "string")
        .map((a, i) => ({ key: i, filename: a.filename as string }));
      if (attachments.length) turn.attachments = attachments;
    } else {
      turn.answer = typeof s.answer === "string" ? s.answer : "";
      turn.error = typeof s.error === "string" ? s.error : null;
      turn.confidence = Number(s.confidence) || 0;
      turn.agentRunId = Number(s.agentRunId) || 0;
      turn.evidence = Array.isArray(s.evidence)
        ? (s.evidence.filter(
            (ev) => ev && typeof ev === "object",
          ) as AssistantEvidence[])
        : [];
    }
    restored.push(turn);
  }
  turns.value = restored;
}

watch(turns, scheduleHistorySave, { deep: true });

// ==================================================================
// 错误解析（ApiError 的结构化 detail）
// ==================================================================

interface DetailObject {
  error?: string;
  message?: string;
  hint?: string;
  expected_hash?: string;
  current_hash?: string;
}

/** 取出 ApiError 的结构化 detail（漂移 409 带 {error:"stale", ...}）。 */
function detailOf(err: unknown): DetailObject | null {
  if (!(err instanceof ApiError)) return null;
  const d = err.detail;
  return d && typeof d === "object" ? (d as DetailObject) : null;
}

function statusOf(err: unknown): number | null {
  return err instanceof ApiError ? err.status : null;
}

function textOf(err: unknown): string {
  if (err instanceof ApiError) {
    const d = detailOf(err);
    if (d && typeof d.message === "string" && d.message) return d.message;
    if (typeof err.detail === "string" && err.detail) return err.detail;
    return err.message;
  }
  return err instanceof Error ? err.message : String(err);
}

// ==================================================================
// 创作引导（对话式一步步生成）
// ==================================================================

/** 当前引导会话（服务端落库，刷新可续）。 */
const wizard = ref<WizardSessionView | null>(null);
const wizardBusy = ref(false);
const runBusy = ref(false);
/** 角色卡生成中的 key：all / missing / 具体角色名；null = 空闲。 */
const cardsBusyName = ref<string | null>(null);
/** 正在就地编辑的角色名 + 表单草稿。 */
const cardEditName = ref<string | null>(null);
const cardEditTurn = ref<Turn | null>(null);
const cardEditModalEl = ref<HTMLElement | null>(null);
const {
  style: cardEditModalStyle,
  start: startCardEditResize,
  reset: resetCardEditResize,
} = useModalResize(cardEditModalEl);
const cardDraft = ref<Record<string, string>>({});
const cardSaving = ref(false);
const cardEditError = ref<string | null>(null);
/** 待确认删除的角色名；null = 没有待确认。 */
const cardDeleteName = ref<string | null>(null);
const cardDeleting = ref(false);
/** 待确认「整体移除角色」的角色名。 */
const characterRemoveName = ref<string | null>(null);
const characterRemoving = ref(false);
/** 角色对话模式：进入后 composer 的每句话都发给角色，而不是问答 agent。 */
const roleMode = ref<{ character: string; scene: string } | null>(null);

const wizardActive = computed(() => wizard.value?.status === "active");

// ------------------------------------------------------------------
// 蒸馏进度与看护（引导卡片里的一句话进度 + 完成/卡住通知）
// ------------------------------------------------------------------
//
// ## 数据来源
//
// 进度**不靠前端自己算**：`progress_current / progress_total / stage /
// progress_at / health` 全部来自 `DistillRun` 行。前端只做两件事：
//   1. 每 3 秒拉一次 `GET .../notices`（同时返回最近一条任务 + 未消费通知）；
//   2. 把未消费通知渲染成助手气泡并 ack。
//
// 这样刷新、断线重连、甚至服务重启后回来，看到的都是同一份 DB 事实：
// 任务在跑就继续显示实时进度，已经跑完了就把未消费的回执补发出来。

const distillJob = ref<DistillJob | null>(null);
/** 本页已经展示过的通知 id（ack 失败时下一轮会重试，成功后也不会重复弹）。 */
const seenNotices = new Set<number>();
let distillTimer: ReturnType<typeof setTimeout> | null = null;
let pageAlive = true;

/** 「疑似卡住 / 运行中 / 排队」这三种才需要持续轮询。 */
const distillActive = computed(() => {
  const h = distillJob.value?.health;
  return h === "queued" || h === "running" || h === "stalled";
});

/**
 * 百分比只在**总数探明**时才显示；total=0 绝不拿 0 当分母编一个数。
 *
 * 运行中上限 99%：块提炼完之后还有「角色声音 / 文风漂移 / 汇总」几段收尾，
 * 那几段不在块进度里。显示 100% 但状态还是「运行中」就是在撒谎，宁可停在 99%。
 */
const distillPercent = computed<number | null>(() => {
  const j = distillJob.value;
  if (!j || !j.progress_total) return null;
  const raw = Math.floor((j.progress_current / j.progress_total) * 100);
  return Math.max(0, Math.min(distillActive.value ? 99 : 100, raw));
});

const DISTILL_TAG: Record<string, string> = {
  queued: "排队",
  running: "运行中",
  stalled: "疑似卡住",
  success: "已完成",
  failed: "失败",
};

function distillTag(job: DistillJob | null): string {
  if (!job) return "";
  return DISTILL_TAG[job.health ?? job.status] ?? job.status;
}

/** 进度条里那句话：优先用后端给的 health_note（带具体分钟数）。 */
function distillSentence(job: DistillJob | null): string {
  if (!job) return "";
  if (job.health_note) return job.health_note;
  if (job.stage) return job.stage;
  return job.status;
}

function scheduleDistillPoll(delay = 3000): void {
  if (!pageAlive) return;
  if (distillTimer) clearTimeout(distillTimer);
  distillTimer = setTimeout(() => void pollDistill(), delay);
}

/** 把一条通知变成助手气泡（内容由后端渲染好，前端不拼数字）。 */
async function consumeNotice(n: WizardNotice): Promise<void> {
  if (seenNotices.has(n.id)) return;
  seenNotices.add(n.id);
  const t = makeAssistantTurn();
  t.loading = false;
  t.text = n.payload.message || "";
  t.notice = n;
  turns.value.push(t);
  void scrollToBottom();
  try {
    await api.ackWizardNotice(projectId, n.id);
  } catch {
    // ack 失败就下一轮重试：宁可重复补一次，也不能让回执静默消失。
    seenNotices.delete(n.id);
  }
}

async function pollDistill(): Promise<void> {
  if (!pageAlive) return;
  try {
    const res = await api.listWizardNotices(projectId);
    distillJob.value = res.distill;
    for (const n of res.items) await consumeNotice(n);
  } catch {
    /* 网络闪断：保持上一次的进度，下一轮继续 */
  }
  scheduleDistillPoll();
}

/**
 * 中断回执上的「继续蒸馏」：从已落库的块接着跑，不用从头再来。
 *
 * 为什么必须点一下而不是重启自动接上：自动续跑等于无人值守烧模型费用；
 * 若是「某块必炸」的确定性中断，还会变成重启→续跑→再崩的死循环。
 */
const resumeBusy = ref(false);
async function resumeDistill(turn: Turn): Promise<void> {
  if (resumeBusy.value) return;
  resumeBusy.value = true;
  try {
    await api.resumeDistill(projectId);
    // 按钮是一次性入口：提交成功就收回，进度条接手。
    if (turn.notice) turn.notice.payload.resumable = false;
    await pollDistill();
  } catch (err) {
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `继续提炼没提交成功（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    resumeBusy.value = false;
  }
}

onUnmounted(() => {
  pageAlive = false;
  if (distillTimer) clearTimeout(distillTimer);
  if (traceClock !== null) {
    window.clearInterval(traceClock);
    traceClock = null;
  }
  for (const id of traceTimers.values()) window.clearTimeout(id);
  traceTimers.clear();
  // 组件被 keep-alive 淘汰 / 真正卸载时，把最后一次改动落盘。
  window.removeEventListener("pagehide", flushHistory);
  flushHistory();
  draft.reset(0);
});

// keep-alive 停用时停掉轮询/计时器；回来时若蒸馏任务仍在跑，继续轮询。
onDeactivated(() => {
  pageAlive = false;
  railOpen.value = false;
  if (distillTimer) {
    clearTimeout(distillTimer);
    distillTimer = null;
  }
  if (traceClock !== null) {
    window.clearInterval(traceClock);
    traceClock = null;
  }
  for (const id of traceTimers.values()) window.clearTimeout(id);
  traceTimers.clear();
});
onActivated(() => {
  pageAlive = true;
  if (distillActive.value) void pollDistill();
});


/** 「我想生成一本小说」这类说法直接进引导，不用用户去找按钮。 */
const WIZARD_INTENT = /(我想|我要|帮我|想)?\s*(生成|写|创作|开)\s*(一|1)?\s*本?\s*(小说|书)/;

function looksLikeWizardIntent(text: string): boolean {
  return WIZARD_INTENT.test(text);
}

function makeWizardTurn(text: string): Turn {
  const t = makeAssistantTurn();
  t.loading = false;
  t.text = text;
  return t;
}

function pushWizardTurn(question: WizardQuestion, intro = ""): Turn {
  // 一条回复 = 解释正文 + 卡片；正文里可能已经带了上一题的回执。
  const parts = [intro, question.message].filter((s) => s && s.trim());
  const t = makeWizardTurn(parts.join("\n\n"));
  t.wizard = { question, answeredLabel: null, busy: false, disabled: false };
  turns.value.push(t);
  void scrollToBottom();
  return t;
}

function pushResultTurn(result: WizardCommitResult, intro = ""): Turn {
  const t = makeWizardTurn(intro);
  t.wizardResult = result;
  turns.value.push(t);
  void scrollToBottom();
  return t;
}

function applyCommitResult(result: WizardCommitResult): void {
  if (result.kind === "role_chat" && result.character) {
    roleMode.value = {
      character: result.character,
      scene: result.scene || "当前剧情时间线",
    };
  }
  pushResultTurn(result);
}

/** 开始（或恢复）引导；restart=true 放弃旧会话从头问。 */
async function startWizardFlow(intro = "", restart = false): Promise<void> {
  if (wizardBusy.value) return;
  wizardBusy.value = true;
  try {
    const st = await api.startWizard(projectId, restart);
    wizard.value = st.session;
    if (st.question) pushWizardTurn(st.question, intro);
  } catch (err) {
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `无法开始引导（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    wizardBusy.value = false;
  }
}

/**
 * 用后端返回的对话记录重建气泡（跨退出存活）。
 *
 * 历史里的引导卡片一律 `disabled`：它们代表「当时问过、已经答过」，不能再点
 * 一次提交。当前要答的那一题由 `loadWizard` 随后追加/启用在最末尾。
 *
 * 返回历史里是否已经有过「执行结果」气泡——`loadWizard` 据此避免重复画一次。
 */
function restoreTurns(items: WizardMessage[]): boolean {
  let pendingQuestion: Turn | null = null;
  let restoredResult = false;
  for (const m of items) {
    if (m.role === "user") {
      turns.value.push(makeUserTurn(m.text));
      // 用户这句话就是对上一张卡片的作答：把它收成一行已答状态，
      // 而不是让卡片停在「待选择」——历史卡片不该看起来还能点。
      if (pendingQuestion?.wizard) {
        pendingQuestion.wizard.answeredLabel = m.text;
        pendingQuestion.wizard.disabled = true;
      }
      pendingQuestion = null;
      continue;
    }
    const t = makeAssistantTurn();
    t.loading = false;
    if (m.kind === "plain") {
      // 普通问答的正文渲染的是 `answer`（引导类气泡才用 `text`）。
      // 历史回放必须落到同一个字段，否则刷新后每条回答都会显示成
      // 「（助手未返回文字回答，请直接看下方证据）」。
      t.answer = m.text;
    } else {
      t.text = m.text;
    }
    if (m.kind === "wizard_question" && m.payload?.question) {
      t.wizard = {
        question: m.payload.question,
        answeredLabel: null,
        busy: false,
        disabled: true,
      };
      pendingQuestion = t;
    } else if (m.kind === "wizard_result" && m.payload?.result) {
      t.wizardResult = m.payload.result;
      restoredResult = true;
    } else if (m.kind === "notice") {
      const p = (m.payload || {}) as Record<string, unknown>;
      const noticeId = Number(p.notice_id ?? 0);
      // 记进 seenNotices：这条回执已经以历史气泡出现，轮询不要再补一遍。
      if (noticeId) seenNotices.add(noticeId);
      t.notice = {
        id: noticeId,
        kind: (p.notice_kind as WizardNotice["kind"]) || "distill_done",
        job_id: Number(p.job_id ?? 0),
        payload: p as WizardNoticePayload,
        created_at: m.created_at,
      };
      // 这条回执已经在历史里展示过了：顺手标掉，避免它永远以「未消费」的身份
      // 跟着每次历史回放重复出现。标失败无所谓，下次进来还会再标一次。
      if (noticeId) void api.ackWizardNotice(projectId, noticeId).catch(() => {});
    }
    turns.value.push(t);
  }
  void scrollToBottom();
  return restoredResult;
}

/** 进入页面时恢复上次进度：先回放历史对话，再画「现在停在哪一步」。 */
async function loadWizard(): Promise<void> {
  // 历史与当前状态是两次读：历史要「说过什么」，state 要「停在哪一题」。
  // 历史读失败不该挡住当前题——两者互不依赖，失败就只少一段回放。
  let restoredResult = false;
  try {
    const hist = await api.listWizardMessages(projectId);
    if (hist.items.length) restoredResult = restoreTurns(hist.items);
  } catch {
    /* 忽略：没有历史不影响继续问 */
  }
  // 消息里的 bible 是提交那一刻的快照；角色卡/角色删除是之后才写回项目的。
  // 用项目当前圣经刷新最后一张大纲结果，否则刷新页面会看到「角色卡 0 张」。
  const liveBible = project.value?.plot_bible as Record<string, unknown> | null | undefined;
  if (liveBible) {
    const latest = [...turns.value]
      .reverse()
      .find((t) => t.wizardResult?.stage === "outline");
    if (latest?.wizardResult) latest.wizardResult.bible = liveBible;
  }
  try {
    const st = await api.getWizard(projectId);
    wizard.value = st.session;
    // 刷新后立即恢复蒸馏进度（不用等第一次轮询）。
    if (st.distill) distillJob.value = st.distill;
    if (!st.session) return;
    if (st.session.status === "active" && st.question) {
      const last = turns.value[turns.value.length - 1];
      const isCurrent =
        !!last?.wizard &&
        last.wizard.question.step === st.question.step &&
        !last.wizard.answeredLabel;
      if (isCurrent && last.wizard) {
        // 历史最后一条就是当前题：解除只读，让用户接着答，别再画一张重复的。
        last.wizard.disabled = false;
      } else {
        pushWizardTurn(st.question, "上次问到这一步，接着来。");
      }
    } else if (
      st.session.status === "committed" &&
      st.session.result &&
      !restoredResult
    ) {
      applyCommitResult(st.session.result);
    }
  } catch {
    /* 忽略：引导不可用不影响问答 */
  }
}

async function answerStep(turn: Turn, value: unknown, label: string): Promise<void> {
  const ws = turn.wizard;
  if (!ws || ws.busy) return;
  ws.busy = true;
  const question = ws.question;
  turns.value.push(makeUserTurn(label));
  await scrollToBottom();
  try {
    const res = await api.answerWizard(projectId, {
      step_key: question.step,
      value,
    });
    ws.busy = false;
    ws.disabled = true;
    ws.answeredLabel = label;
    wizard.value = res.session;
    if (res.done && res.result) applyCommitResult(res.result);
    else if (res.question) pushWizardTurn(res.question);
  } catch (err) {
    ws.busy = false;
    turn.error = `这一步没提交成功（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  }
}

async function skipStep(turn: Turn): Promise<void> {
  const ws = turn.wizard;
  if (!ws || ws.busy) return;
  ws.busy = true;
  turns.value.push(makeUserTurn("使用默认值"));
  try {
    const res = await api.answerWizard(projectId, {
      step_key: ws.question.step,
      skip: true,
    });
    ws.busy = false;
    ws.disabled = true;
    ws.answeredLabel = "使用默认值";
    wizard.value = res.session;
    if (res.done && res.result) applyCommitResult(res.result);
    else if (res.question) pushWizardTurn(res.question);
  } catch (err) {
    ws.busy = false;
    turn.error = `跳过失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  }
}

/** 上一步 / 点汇总行跳回：都走同一个「重新画当前题」的路径。 */
async function revisitStep(
  turn: Turn,
  fetcher: () => Promise<{ session: WizardSessionView | null; question: WizardQuestion | null }>,
  intro: string,
): Promise<void> {
  const ws = turn.wizard;
  if (!ws || ws.busy) return;
  ws.busy = true;
  try {
    const st = await fetcher();
    ws.busy = false;
    ws.disabled = true;
    wizard.value = st.session;
    if (st.question) pushWizardTurn(st.question, intro);
  } catch (err) {
    ws.busy = false;
    turn.error = `无法回到上一步（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  }
}

function backStep(turn: Turn): void {
  void revisitStep(turn, () => api.backWizard(projectId), "回到上一步，改完继续。");
}

/**
 * 引导进行中的一句话：交给后端的引导解析器。
 *
 * 四种结局（mode）：
 *   answered —— 真在作答：收起当前卡片，下一条回复开头带回执；
 *   explain / clarify —— 追问或没听懂：**不推进**，当前卡片保持可点，
 *     只追加一条解释气泡。这是「对话流」和「一问一卡」的核心差别；
 *   action —— 助理改了一次配置（档位 / 采样章数 / 字数 / 流程）：不推进
 *     作答语义，回执里带「旧值 → 新值 + 影响」，并给出可撤销 id。
 */
async function sayInWizard(text: string): Promise<void> {
  if (wizardBusy.value) return;
  const current = [...turns.value].reverse().find((t) => t.wizard && !t.wizard.disabled);
  const ws = current?.wizard ?? null;
  if (ws) ws.busy = true;
  wizardBusy.value = true;
  turns.value.push(makeUserTurn(text));
  await scrollToBottom();
  try {
    const res = await api.sayWizard(projectId, text);
    wizard.value = res.session;
    if (res.mode === "answered") {
      if (ws) {
        ws.busy = false;
        ws.disabled = true;
        ws.answeredLabel = res.echo || res.ack || text;
      }
      if (res.done && res.result) applyCommitResult(res.result);
      else if (res.question) pushWizardTurn(res.question);
    } else if (res.mode === "action") {
      if (ws) ws.busy = false;
      const t = makeAssistantTurn();
      t.loading = false;
      t.note = true;
      t.text = res.message || "";
      t.action = res.action ?? null;
      t.undoId = res.undo_id ?? null;
      t.undoable = res.undoable === true;
      turns.value.push(t);
      refreshQuestionAfterAction(ws, res);
    } else {
      if (ws) ws.busy = false;
      const t = makeAssistantTurn();
      t.loading = false;
      t.note = true;
      t.text = res.message || "";
      turns.value.push(t);
    }
  } catch (err) {
    if (ws) ws.busy = false;
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `发送失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    wizardBusy.value = false;
    await scrollToBottom();
  }
}

/**
 * 写操作之后刷新卡片。
 *
 * 动作**不推进作答**，但可能改掉当前题的内容（例如采样章数改了，蒸馏卡的
 * 章数/耗时都要重算），也可能把当前题跳走（skip_step）。所以：
 *   - 还是同一题 → 就地换掉 question（卡片保持可点）；
 *   - 换题了 → 旧卡收起成只读，追加新卡。
 */
function refreshQuestionAfterAction(
  ws: WizardStepState | null,
  res: WizardSayResponse,
): void {
  const nextQ = res.question;
  if (!nextQ) return;
  if (ws && ws.question.step === nextQ.step) {
    ws.question = nextQ;
    ws.answeredLabel = null;
    return;
  }
  if (ws) {
    ws.busy = false;
    ws.disabled = true;
  }
  pushWizardTurn(nextQ);
}

/** 撤销上一次写操作（审计 id 来自 action 回执）。 */
async function undoAction(turn: Turn): Promise<void> {
  if (turn.undoBusy || !turn.undoId) return;
  turn.undoBusy = true;
  try {
    const res = await api.undoWizard(projectId, turn.undoId);
    turn.undoBusy = false;
    turn.undoDone = true;
    turn.undoId = null;
    wizard.value = res.session;
    const t = makeAssistantTurn();
    t.loading = false;
    t.note = true;
    t.text = res.message || "已撤销。";
    turns.value.push(t);
    const current = [...turns.value].reverse().find((x) => x.wizard && !x.wizard.disabled);
    refreshQuestionAfterAction(current?.wizard ?? null, res);
  } catch (err) {
    turn.undoBusy = false;
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `撤销失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    await scrollToBottom();
  }
}

function gotoStep(turn: Turn, step: string): void {
  void revisitStep(
    turn,
    () => api.gotoWizard(projectId, step),
    "跳回这一项，改完继续。",
  );
}

function canRun(result: WizardCommitResult | null | undefined): boolean {
  if (!result) return false;
  if (result.kind === "role_chat") return false;
  return result.stage === "ready" || result.stage === "outline";
}

async function runGeneration(turn: Turn): Promise<void> {
  if (runBusy.value) return;
  runBusy.value = true;
  try {
    const res = await api.runWizard(projectId);
    if (turn.wizardResult) turn.wizardResult = res.result;
    pushResultTurn(res.result, "写好了，下面是本次落笔的段落。");
  } catch (err) {
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `开写失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    runBusy.value = false;
    await scrollToBottom();
  }
}

// 蒸馏完成后的「用档案重建角色卡」：只重建卡，不动剧情圣经/世界观。
const rebuildCardsBusy = ref(false);
async function rebuildCardsWithArchive(): Promise<void> {
  if (rebuildCardsBusy.value) return;
  rebuildCardsBusy.value = true;
  try {
    const res = await api.rebuildWizardCardsWithArchive(projectId);
    const t = makeAssistantTurn();
    t.loading = false;
    t.answer = res.message;
    turns.value.push(t);
    await scrollToBottom();
  } catch (err) {
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `重建角色卡失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    rebuildCardsBusy.value = false;
  }
}

type BibleCharacterView = {
  name: string;
  role: string;
  card: Record<string, unknown> | null;
};

function asText(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

/** 从剧情圣经里取角色（带卡），供角色卡面板渲染。 */
function bibleCharacters(result: WizardCommitResult): BibleCharacterView[] {
  const bible = result.bible as Record<string, unknown> | null | undefined;
  const raw = bible && Array.isArray(bible.characters) ? bible.characters : [];
  const out: BibleCharacterView[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const c = item as Record<string, unknown>;
    const name = asText(c.name).trim();
    if (!name) continue;
    out.push({
      name,
      role: asText(c.role),
      card: c.card && typeof c.card === "object"
        ? (c.card as Record<string, unknown>)
        : null,
    });
  }
  return out;
}

function cardCount(result: WizardCommitResult): number {
  return bibleCharacters(result).filter((c) => !!c.card).length;
}

function missingCards(result: WizardCommitResult): string[] {
  // 以剧情圣经为准算「还没有卡」的角色：单张重生成后仍然准确，
  // 不会被上一批 stats.missing（只描述那一批）覆盖。
  return bibleCharacters(result).filter((c) => !c.card).map((c) => c.name);
}

/** 涉及该角色的关系条数：移除确认里先说清会连带清理多少条。 */
function relationshipCount(result: WizardCommitResult, name: string): number {
  const bible = result.bible as Record<string, unknown> | null | undefined;
  const rels = bible && Array.isArray(bible.relationships) ? bible.relationships : [];
  return rels.filter((r) => {
    if (!r || typeof r !== "object") return false;
    const rel = r as Record<string, unknown>;
    return rel.a === name || rel.b === name;
  }).length;
}

function cardIdentityLine(card: Record<string, unknown>): string {
  const ident = card["身份"];
  if (!ident || typeof ident !== "object") return "";
  const obj = ident as Record<string, unknown>;
  return ["性别", "年龄", "身份职业"]
    .map((key) => asText(obj[key]))
    .filter(Boolean)
    .join(" · ");
}

function cardVoiceLine(card: Record<string, unknown>): string {
  const voice = card["声纹"];
  if (!voice || typeof voice !== "object") return "";
  const v = voice as Record<string, unknown>;
  const tics = Array.isArray(v["口头禅"])
    ? v["口头禅"].map(asText).filter(Boolean).slice(0, 2)
    : [];
  const parts = [
    v["自称"] ? `自称「${asText(v["自称"])}」` : "",
    v["句长"] ? `句长${asText(v["句长"])}` : "",
    asText(v["句末"]),
    tics.length ? `口头禅：${tics.map((x) => `「${x}」`).join("")}` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

async function regenerateCards(
  turn: Turn,
  names: string[] = [],
  key = "all",
): Promise<void> {
  const result = turn.wizardResult;
  if (cardsBusyName.value || !result) return;
  cardsBusyName.value = key;
  try {
    const res = await api.regenerateWizardCards(projectId, names);
    result.bible = res.bible;
    result.character_cards = res.character_cards;
    result.message = [result.message, res.message].filter(Boolean).join("");
  } catch (err) {
    const t = makeAssistantTurn();
    t.loading = false;
    t.error = `角色卡生成失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turns.value.push(t);
  } finally {
    cardsBusyName.value = null;
    await scrollToBottom();
  }
}

/** 就地编辑的字段（与 character_card 的 身份/声纹 分组一一对应）。 */
const CARD_FIELDS: Array<{ key: string; label: string; hint: string }> = [
  { key: "性别", label: "性别", hint: "男 / 女" },
  { key: "年龄", label: "年龄", hint: "27岁" },
  { key: "身份职业", label: "身份职业", hint: "夜班观测员" },
  { key: "外貌", label: "外貌", hint: "标志性细节" },
  { key: "出身背景", label: "出身背景", hint: "从哪来、经历过什么" },
  { key: "自称", label: "自称", hint: "我 / 在下 / 本人" },
  { key: "称呼他人", label: "称呼他人", hint: "顿号分隔：小满、周主管" },
  { key: "句长", label: "句长", hint: "极短 / 短 / 中 / 长" },
  { key: "句末", label: "句末", hint: "直陈收尾 / 常用「呢」「吧」" },
  { key: "口头禅", label: "口头禅", hint: "顿号分隔" },
  { key: "禁用词", label: "禁用词", hint: "顿号分隔" },
  { key: "语气底色", label: "语气底色", hint: "压着说话 / 居高临下" },
];

function cardListText(value: unknown): string {
  return Array.isArray(value)
    ? value.map(asText).filter(Boolean).join("、")
    : asText(value);
}

function splitCardList(value: string): string[] {
  return value
    .split(/[、,，;；\s]+/)
    .map((x) => x.trim())
    .filter(Boolean)
    .slice(0, 6);
}

function compactCardGroup(group: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(group)) {
    if (Array.isArray(value)) {
      if (value.length) out[key] = value;
    } else if (typeof value === "string" && value.trim()) {
      out[key] = value.trim();
    }
  }
  return out;
}

function beginCardEdit(c: BibleCharacterView, turn: Turn): void {
  const card = c.card || {};
  const ident = (card["身份"] as Record<string, unknown>) || {};
  const voice = (card["声纹"] as Record<string, unknown>) || {};
  cardDraft.value = {
    性别: asText(ident["性别"]),
    年龄: asText(ident["年龄"]),
    身份职业: asText(ident["身份职业"]),
    外貌: asText(ident["外貌"]),
    出身背景: asText(ident["出身背景"]),
    自称: asText(voice["自称"]),
    称呼他人: cardListText(voice["称呼他人"]),
    句长: asText(voice["句长"]),
    句末: asText(voice["句末"]),
    口头禅: cardListText(voice["口头禅"]),
    禁用词: cardListText(voice["禁用词"]),
    语气底色: asText(voice["语气底色"]),
  };
  cardEditTurn.value = turn;
  cardEditName.value = c.name;
  cardEditError.value = null;
  resetCardEditResize();
}

function cancelCardEdit(): void {
  cardEditName.value = null;
  cardEditTurn.value = null;
  cardEditError.value = null;
}

async function saveCard(): Promise<void> {
  const turn = cardEditTurn.value;
  const name = cardEditName.value;
  const result = turn?.wizardResult;
  if (!turn || !name || !result || cardSaving.value) return;
  cardSaving.value = true;
  cardEditError.value = null;
  try {
    const d = cardDraft.value;
    const identity = compactCardGroup({
      性别: d.性别, 年龄: d.年龄, 身份职业: d.身份职业,
      外貌: d.外貌, 出身背景: d.出身背景,
    });
    const voice = compactCardGroup({
      自称: d.自称, 称呼他人: splitCardList(d.称呼他人),
      句长: d.句长, 句末: d.句末,
      口头禅: splitCardList(d.口头禅), 禁用词: splitCardList(d.禁用词),
      语气底色: d.语气底色,
    });
    if (!Object.keys(identity).length && !Object.keys(voice).length) {
      cardEditError.value = "至少填写一项再保存。";
      return;
    }
    // 只改身份/声纹两组，能力、关系、量级原样保留（否则编辑一次就丢）。
    const existing = bibleCharacters(result).find((x) => x.name === name)?.card || {};
    const res = await api.updateWizardCard(projectId, name, {
      ...existing,
      身份: identity,
      声纹: voice,
    });
    result.bible = res.bible;
    result.character_cards = res.character_cards;
    result.message = [result.message, res.message].filter(Boolean).join("");
    // 保存成功后关闭弹窗；失败则保留窗口与错误提示。
    cancelCardEdit();
  } catch (err) {
    cardEditError.value = textOf(err);
  } finally {
    cardSaving.value = false;
  }
}

async function deleteCard(turn: Turn, name: string): Promise<void> {
  const result = turn.wizardResult;
  if (!result || cardDeleting.value) return;
  cardDeleting.value = true;
  cardEditError.value = null;
  try {
    const res = await api.deleteWizardCard(projectId, name);
    result.bible = res.bible;
    result.character_cards = res.character_cards;
    result.message = [result.message, res.message].filter(Boolean).join("");
    if (cardEditName.value === name) cardEditName.value = null;
    cardDeleteName.value = null;
  } catch (err) {
    cardEditError.value = textOf(err);
  } finally {
    cardDeleting.value = false;
  }
}

async function removeCharacter(turn: Turn, name: string): Promise<void> {
  const result = turn.wizardResult;
  if (!result || characterRemoving.value) return;
  characterRemoving.value = true;
  cardEditError.value = null;
  try {
    const res = await api.removeWizardCharacter(projectId, name);
    result.bible = res.bible;
    result.message = [result.message, res.message].filter(Boolean).join("");
    if (cardEditName.value === name) cardEditName.value = null;
    if (cardDeleteName.value === name) cardDeleteName.value = null;
    characterRemoveName.value = null;
  } catch (err) {
    cardEditError.value = textOf(err);
  } finally {
    characterRemoving.value = false;
  }
}

function exitRole(): void {
  roleMode.value = null;
  const t = makeAssistantTurn();
  t.loading = false;
  t.answer = "已退出角色对话，回到普通问答。想再聊，点输入框上方的「重新开始创作引导」。";
  turns.value.push(t);
  void scrollToBottom();
}

function prettyJson(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** 角色对话的历史只带最近 8 句，避免提示词无限膨胀。 */
function recentRoleHistory(): { role: string; content: string }[] {
  return turns.value
    .slice(-9, -1)
    .filter((t) => t.role === "user" || t.answer)
    .map((t) => ({
      role: t.role,
      content: t.role === "user" ? t.text : t.answer,
    }));
}

// ==================================================================
// 问答
// ==================================================================

async function send(): Promise<void> {
  const q = query.value.trim();
  if (!q || sending.value) return;

  // 「我想生成一本小说」= 引导入口：不丢给问答 agent，直接开状态机。
  // 已经在引导里时不再抢话，用户可能是想顺便问别的问题。
  if (!wizardActive.value && !roleMode.value && looksLikeWizardIntent(q)) {
    turns.value.push(makeUserTurn(q));
    query.value = "";
    await startWizardFlow("好，我一步一步问你。先定这次要做哪一种作品。");
    await scrollToBottom();
    return;
  }

  // 引导进行中：这句话先交给引导解析——可能是作答，也可能是追问（不推进）。
  if (wizardActive.value && !roleMode.value) {
    if (wizardBusy.value) return;
    query.value = "";
    await sayInWizard(q);
    return;
  }

  // 先把 user 气泡插进去，assistant 占位气泡同时置 loading：
  // 否则「提问 → 等待 → 出结果」之间界面是空的，用户会以为没点上。
  // 附件只随**普通问答**走；上传中/失败的草稿绝不能被静默丢掉。
  const body = draft.request(q);
  if (draft.items.length && !body) {
    const notice = makeAssistantTurn();
    notice.loading = false;
    notice.answer = "附件还在上传中或上传失败，请先处理附件草稿再发送。";
    turns.value.push(notice);
    await scrollToBottom();
    return;
  }
  const userTurn = makeUserTurn(q);
  userTurn.attachments = draft.items.map((item) => ({
    key: item.key,
    filename: item.metadata!.filename,
  }));
  turns.value.push(userTurn);
  const turn = makeAssistantTurn();
  turns.value.push(turn);
  query.value = "";
  sending.value = true;
  await scrollToBottom();

  try {
    if (roleMode.value) {
      // 角色对话：只读角色卡 + 助手模型，不触发问答 agent 的检索/工具。
      const res = await api.wizardRoleChat(projectId, q, recentRoleHistory());
      turn.answer = res.answer;
      turn.loading = false;
      return;
    }
    const res = await api.assistantQa(body ?? { project_id: projectId, query: q });
    turn.answer = res.answer || "";
    turn.evidence = res.evidence ?? [];
    turn.confidence = res.confidence;
    turn.agentRunId = res.agent_run_id;
    // agent 自主调的诊断 / 日志 / 审核结果：新后端会给 issues + rules_evaluated，
    // 旧后端不给 → 空数组 → 整段不渲染（不留空态）。
    turn.issues = res.issues ?? res.findings ?? [];
    turn.rulesEvaluated = res.rules_evaluated ?? [];
    // 工具调用轨迹：旧后端不返回 tool_mode / tool_trace → 空值 → 整块不渲染。
    turn.toolMode = res.tool_mode ?? "";
    turn.toolTrace = res.tool_trace ?? [];
    turn.toolResults = res.tool_results ?? {};
    turn.toolFallbackReason = res.tool_fallback_reason ?? "";
    turn.cards = (res.pending_actions ?? []).map((action) => ({
      key: nextKey("card"),
      action,
      state: "pending" as CardState,
      rows: detailRows(action),
      notice: "",
      result: null,
      trace: [],
      startedAt: null,
      baseCount: null,
      sawLanding: false,
      traceSupported: true,
    }));
    // 说「记住…」/「忘掉…」时后端直接回了处理结果：抽屉与计数跟着刷新。
    if (res.memory_changed) {
      memories.value = res.memories ?? [];
    }
    turn.loading = false;
    // 请求已把附件 id 交给后端（答疑/提卡都在服务端完成），草稿可以清空；
    // 真正的写库仍要用户在上方卡片上点确认。
    resetAttachments();
  } catch (err) {
    turn.error = `问答失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turn.loading = false;
  } finally {
    sending.value = false;
    await scrollToBottom();
  }
}

/** Enter 发送、Shift+Enter 换行（textarea 默认 Enter 是换行，这里反转）。 */
function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void send();
  }
}

// ==================================================================
// 待确认卡片：确认 / 取消
// ==================================================================

// ------------------------------------------------------------------
// 执行轨迹：确认 → 请求发出 → 正文落库 → 完成/失败
// ------------------------------------------------------------------
//
// 为什么要有：卡片执行是**同步 HTTP**，生成一段动辄几分钟，期间只有一句
// 「执行中…」，用户无法判断是在写、卡住了、还是后端根本没跑。
//
// 轨迹只记**可验证的事实**：确认时刻、请求已发出、库里段数有没有长、最后
// 返回什么。不猜「正在组装上下文」这种前端看不见的阶段，也不画假进度条。

/** 每秒走一次，只为刷新「已等待 N 秒」；没有执行中的卡片就不动。 */
const traceNow = ref(Date.now());
const TRACE_POLL_MS = 2000;
const traceTimers = new Map<string, number>();
let traceClock: number | null = null;

function anyCardRunning(): boolean {
  return turns.value.some((t) => t.cards.some((c) => c.state === "running"));
}

function ensureTraceClock(): void {
  if (traceClock !== null) return;
  traceClock = window.setInterval(() => {
    if (anyCardRunning()) traceNow.value = Date.now();
  }, 1000);
}

function pushTrace(card: ActionCard, text: string, tone: TraceStep["tone"] = "info"): void {
  card.trace.push({ at: Date.now(), text, tone });
}

function clockText(at: number): string {
  const d = new Date(at);
  return [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0"))
    .join(":");
}

/** 已等待时长（「42 秒」/「3 分 05 秒」）。 */
function elapsedText(card: ActionCard): string {
  if (card.startedAt === null) return "";
  const total = Math.max(0, Math.round((traceNow.value - card.startedAt) / 1000));
  if (total < 60) return `${total} 秒`;
  return `${Math.floor(total / 60)} 分 ${String(total % 60).padStart(2, "0")} 秒`;
}

function stopTrace(card: ActionCard): void {
  const id = traceTimers.get(card.key);
  if (id !== undefined) {
    window.clearTimeout(id);
    traceTimers.delete(card.key);
  }
}

function scheduleTrace(card: ActionCard): void {
  stopTrace(card);
  traceTimers.set(
    card.key,
    window.setTimeout(() => {
      traceTimers.delete(card.key);
      void pollCardTrace(card);
    }, TRACE_POLL_MS),
  );
}

/**
 * 读一次库里的段数，判断本次生成是否已落库。
 *
 * 只调 `/generation-status`（四个字段），不碰 `/generations/{id}`
 * ——后者连全部正文一起返回，轮询它等于每 2 秒搬一次整本书。
 */
async function pollCardTrace(card: ActionCard): Promise<void> {
  if (card.state !== "running" || !card.traceSupported) return;
  try {
    const st = await api.generationStatus(projectId);
    if (card.state !== "running") return;
    if (card.baseCount === null) {
      // 第一次读到的是**本次生成之前**的基线（确认后立刻读，不等 2 秒）。
      card.baseCount = st.count;
      if (st.last_segment_no !== null) {
        pushTrace(card, `基线：已落库 ${st.count} 段（最新第 ${st.last_segment_no} 段）`);
      }
    } else if (st.count > card.baseCount && !card.sawLanding) {
      card.sawLanding = true;
      const chapter = st.last_chapter ? ` · ${st.last_chapter}` : "";
      pushTrace(card, `正文已落库：第 ${st.last_segment_no} 段${chapter}`, "ok");
    }
  } catch (err) {
    if (statusOf(err) === 404) {
      // 旧后端还没有这个端点：停轮询，但不把「查不到进度」当成执行失败。
      card.traceSupported = false;
      pushTrace(card, "当前后端未提供进度接口，只显示等待时长");
      return;
    }
    // 网络闪断：下一轮继续，不往轨迹里灌噪音。
  }
  if (card.state === "running") scheduleTrace(card);
}

/**
 * 执行卡片。
 *
 * 关键约定：这里**不构造任何请求字段**。endpoint / method / body 全部来自
 * 服务端下发的 action，请求由 `api.assistantRunPendingAction` 原样发出。
 * 前端一旦开始「拼」body（比如补 project_id、填 after_text），契约就变成
 * 两份各改一半，漂移时表现为运行期 422 且极难定位。
 */
async function confirmCard(card: ActionCard): Promise<void> {
  if (card.state !== "pending" && card.state !== "failed") return;
  card.state = "running";
  card.notice = "";
  card.result = null;
  card.trace = [];
  card.startedAt = Date.now();
  card.baseCount = null;
  card.sawLanding = false;
  card.traceSupported = true;
  traceNow.value = card.startedAt;
  ensureTraceClock();
  pushTrace(card, `已确认：${card.action.method} ${card.action.endpoint}`);
  pushTrace(card, "请求已发出，等待生成器返回（同步接口，正文落库后才回话）");
  // 立刻取一次基线，再把「落库」判断留给 2 秒一次的轮询。
  void pollCardTrace(card);
  try {
    const res = await api.assistantRunPendingAction(
      card.action.endpoint,
      card.action.method,
      card.action.body,
    );
    card.state = "confirmed";
    card.result = res;
    card.notice = describeResult(card.action, res);
    pushTrace(card, `完成，总耗时 ${elapsedText(card)}`, "ok");
    // 正文可能刚落库：目录和字数跟着刷新（失败也不影响卡片结论）。
    void loadDesk();
  } catch (err) {
    const status = statusOf(err);
    const detail = detailOf(err);
    card.state = "failed";
    card.result = null;
    if (status === 409 && detail?.error === "stale") {
      card.notice =
        "执行被拒：目标内容已被改动，请基于最新内容重新发起。" +
        `（后端提示：${detail.hint ?? detail.message ?? "stale"}）`;
    } else if (status === 409) {
      card.notice = `执行被状态机拒绝：${textOf(err)}`;
    } else if (status === 400) {
      card.notice = `请求被后端拒绝：${textOf(err)}`;
    } else {
      card.notice = `执行失败（HTTP ${status ?? "?"}）：${textOf(err)}`;
    }
    pushTrace(card, `失败，持续 ${elapsedText(card)}`, "err");
  } finally {
    stopTrace(card);
  }
}

/** 取消：把卡片从这一轮里摘掉。不发任何请求。 */
function cancelCard(turn: Turn, card: ActionCard): void {
  if (card.state === "running" || card.state === "confirmed") return;
  const i = turn.cards.indexOf(card);
  if (i >= 0) turn.cards.splice(i, 1);
}

const RISK_LABEL: Record<string, string> = {
  low: "低风险 low",
  medium: "中风险 medium",
  high: "高风险 high",
};

function riskLabel(r: string): string {
  return RISK_LABEL[r] ?? r;
}

function kindLabelOf(card: ActionCard): string {
  return pendingActionKindLabel(card.action.kind);
}

function riskMetaOf(card: ActionCard): { label: string; tag: string; hint: string } {
  return pendingActionRiskMeta(card.action);
}

/**
 * 把执行结果压成一句话。
 *
 * 只取后端明确回的字段（proposal.id / segment.id / note），**不编造**
 * 「已生效」「已完成」这类界面自造的状态 —— 到底写没写库，数据库说了算。
 */
function describeResult(
  action: AssistantPendingAction,
  res: Record<string, unknown>,
): string {
  const parts: string[] = [];
  const proposal = res.proposal;
  if (proposal && typeof proposal === "object") {
    const p = proposal as Record<string, unknown>;
    const id = p.id;
    parts.push(`提案 #${typeof id === "number" ? id : "?"} 已创建`);
    if (typeof p.status === "string") parts.push(`状态 ${p.status}`);
  }
  const versionId = res.version_id;
  if (typeof versionId === "number") parts.push(`生成版本 Version #${versionId}`);
  const segment = res.segment;
  if (segment && typeof segment === "object") {
    const s = segment as Record<string, unknown>;
    const no = typeof s.segment_no === "number" ? `第 ${s.segment_no} 段` : "";
    const id = typeof s.id === "number" ? ` #${s.id}` : "";
    const chars = typeof res.chars === "number" ? ` ${res.chars} 字` : "";
    parts.push(`已写入${no}${id}${chars}`);
  }
  const note = res.note;
  if (typeof note === "string" && note) parts.push(note);
  if (!parts.length) {
    // 服务端没给可读字段时不编造：原样说明「已受理，但未返回可读摘要」。
    return `请求已成功送达 ${action.endpoint}，但服务端未返回可读摘要。`;
  }
  return `${parts.join("；")}。`;
}

/** 结果响应里值得展开看的字段（正文 / diff），其余已在 notice 里说完。 */
function resultExtras(res: Record<string, unknown> | null): { label: string; text: string }[] {
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

// ==================================================================
// 卡片 detail 的通用渲染（键值对；前端不解释业务语义）
// ==================================================================

/**
 * 把 detail 摊平成若干「标签 / 值」行。
 *
 * 服务端只保证 detail 是渲染用的对象、不保证字段集合（它可以随 agent
 * 版本增加诊断细节），所以这里按 key 排序通用渲染：已知的几个键给中文
 * 标签，其余原样显示 key —— 不认识就如实显示，不丢弃也不编造。
 */
/**
 * detail 摊平后的展示行（收到时就摊好，避免模板里每次渲染重算）。
 */
const DETAIL_LABELS: Record<string, string> = {
  // 调动生成（服务端 pending_actions.propose_invoke_generation 下发）
  action: "动作 action",
  segment_no: "段号 segment_no",
  instruction: "显式指令 instruction",
  destructive: "是否覆盖已有正文 destructive",
  risk_hint: "风险说明 risk_hint",
  // 提案
  type: "提案类型 type",
  target_kind: "目标类型 target_kind",
  target_id: "目标 target_id",
  risk_level: "风险等级 risk_level",
  reason: "理由 reason",
  from_issue: "关联 issue from_issue",
  // 通用
  project_id: "项目 project_id",
  max_chars: "字数上限 max_chars",
};

interface DetailRow {
  key: string;
  label: string;
  value: string;
}

function detailRows(action: AssistantPendingAction): DetailRow[] {
  const detail = action.detail;
  if (!detail || typeof detail !== "object") return [];
  const rows: DetailRow[] = [];
  for (const key of Object.keys(detail).sort()) {
    const raw = (detail as Record<string, unknown>)[key];
    if (raw === null || raw === undefined) continue;
    rows.push({
      key,
      label: DETAIL_LABELS[key] ?? key,
      value: formatDetailValue(raw),
    });
  }
  return rows;
}

/** detail 里的值统一成一行字符串；对象 / 数组走 JSON（不丢信息）。 */
function formatDetailValue(raw: unknown): string {
  if (typeof raw === "string") return raw || "（空）";
  if (typeof raw === "number" || typeof raw === "boolean") return String(raw);
  try {
    return JSON.stringify(raw);
  } catch {
    return String(raw);
  }
}

/** 写操作回执里的旧值/新值：null 明确显示「未设置」，不渲染成空行。 */
function echoValue(raw: unknown): string {
  if (raw === null || raw === undefined || raw === "") return "（未设置）";
  return formatDetailValue(raw);
}

// ==================================================================
// 长文本展开态（回答 / 证据 / detail 复用同一套）
// ==================================================================

const expanded = reactive<Record<string, boolean>>({});
function isOpen(key: string): boolean {
  return expanded[key] === true;
}
function toggle(key: string): void {
  expanded[key] = !expanded[key];
}

// ==================================================================
// 派生展示
// ==================================================================

function confidenceTag(c: number): string {
  if (c >= 0.7) return "ok";
  if (c >= 0.4) return "warn";
  return "err";
}

function confidenceLabel(c: number): string {
  if (c >= 0.7) return "高置信 high";
  if (c >= 0.4) return "中置信 medium";
  return "低置信 low";
}

/** 工具调用状态 → 面向用户的文字 + 色标（文字为主，颜色只做辅助）。 */
function toolStatusMeta(status: string): { label: string; tag: string } {
  if (status === "ok") return { label: "已执行", tag: "ok" };
  if (status === "denied") return { label: "已拒绝（未执行）", tag: "err" };
  return { label: "执行失败", tag: "warn" };
}

/**
 * 工具结果预览：按工具名从 `tool_results` 取结果体，序列化后截断。
 *
 * 只用于「展开轨迹后看一眼拿到了什么」，不参与任何请求构造；
 * 结果体可能很大，这里截到 600 字符并打标记，避免一次渲染几百 KB JSON。
 */
function toolResultText(turn: Turn, name: string): string {
  if (!(name in turn.toolResults)) return "";
  const value = turn.toolResults[name];
  try {
    const text = JSON.stringify(value) ?? "";
    return text.length > 600 ? `${text.slice(0, 600)}…（以下截断）` : text;
  } catch {
    return String(value);
  }
}

const hasHistory = computed(() => turns.value.length > 0);

/**
 * 最后一张「引导卡」的 key：进度条只画在它下面。
 *
 * 不这么限制的话，同一句实时进度会出现在历史里每一张引导卡上——既吵，
 * 又会让用户以为有多个任务在跑。
 */
const lastWizardTurnKey = computed(() => {
  for (let i = turns.value.length - 1; i >= 0; i -= 1) {
    const t = turns.value[i];
    if (t.wizard || t.wizardResult) return t.key;
  }
  return "";
});

// ------------------------------------------------------------------
// 写作台：左侧章节目录 + 顶部字数/进度
// ------------------------------------------------------------------
//
// 数据来自两个只读端点：章节目录用 `chapter-outline`（逐章 status/written，
// 不含正文），字数用 `generation-status` 的 `total_chars`（SQL 求和）。
// 都不碰 `/generations/{id}` —— 那个会把整本书正文搬过来。
// 两块信息都是装饰性辅助：取不到就整块不渲染，不影响问答与生成。

interface ChapterRow {
  chapter: number;
  title: string;
  written: boolean;
}

const chapterRail = ref<ChapterRow[]>([]);
const bookChars = ref<number | null>(null);
const bookSegments = ref<number | null>(null);

/** 已写章数（按章去重，不是段数：一章可能落多段）。 */
const writtenChapterCount = computed(
  () => chapterRail.value.filter((c) => c.written).length,
);

/** 最新已写章：侧栏高亮它，一眼看出「写到哪了」。 */
const latestWrittenChapter = computed(() => {
  const written = chapterRail.value.filter((c) => c.written);
  return written.length ? written[written.length - 1].chapter : null;
});

/** 目录里选中的章（再点一次取消）。选中只影响本地展示与提问模板，不写库。 */
const selectedChapter = ref<number | null>(null);
const queryInput = ref<HTMLTextAreaElement | null>(null);
/** 章节目录抽屉：默认收起，避免常驻占掉正文宽度。 */
const railOpen = ref(false);

const selectedChapterRow = computed<ChapterRow | null>(
  () => chapterRail.value.find((c) => c.chapter === selectedChapter.value) ?? null,
);

function selectChapter(ch: ChapterRow): void {
  selectedChapter.value = selectedChapter.value === ch.chapter ? null : ch.chapter;
  railOpen.value = false;
}

/** 选中章后「就这章提问」：把章号填进输入框并聚焦，不替用户发送。 */
function askAboutChapter(): void {
  const row = selectedChapterRow.value;
  if (!row) return;
  query.value = `第 ${row.chapter} 章《${row.title}》`;
  railOpen.value = false;
  void nextTick(() => queryInput.value?.focus());
}

/**
 * 目录里只放「标题」，不放整条章纲。
 *
 * 章纲条目实际形态是「第N章《标题》：一段几百字的正文大纲」，整条塞进
 * 236px 的侧栏只会变成一列省略号；这里按优先级取一件事：
 * 书名号标题 → 第一个分句 → 截断。
 */
function railTitle(entry: string | null, chapter: number): string {
  const text = (entry ?? "").trim();
  if (!text) return "（无章纲）";
  const stripped = text.replace(/^第\s*\d+\s*章[\s:：、.．-]*/, "");
  const title = stripped.match(/^《[^》]{1,40}》/);
  if (title) return title[0];
  const head = stripped.split(/[。：:，,；;]/)[0].trim();
  if (!head) return `第 ${chapter} 章`;
  return head.length > 18 ? `${head.slice(0, 18)}…` : head;
}

async function loadDesk(): Promise<void> {
  try {
    const [outline, status] = await Promise.all([
      api.listChapterOutline(projectId),
      api.generationStatus(projectId),
    ]);
    chapterRail.value = outline.entries.map((e) => ({
      chapter: e.chapter,
      title: railTitle(e.entry, e.chapter),
      written: e.written,
    }));
    bookChars.value = status.total_chars;
    bookSegments.value = status.count;
  } catch {
    /* 侧栏与字数只是辅助：后端旧版本或缺端点时不显示 */
  }
}

// ------------------------------------------------------------------
// 记忆（项目级长期记忆）
// ------------------------------------------------------------------
//
// 记忆内容由**助手在问答后自动抽取**（陈述句才跑，问句跳过），用户不手写。
// 前端只做两件事：显示条数、给用户一个「删掉抽错了的」的出口。
// 读取是自动的：每次问答后端会把记忆拼进提示词，前端不需要参与。

const memories = ref<AssistantMemory[]>([]);
const memoryLimit = ref(60);
const memoryOpen = ref(false);
const memoryLoading = ref(false);
const memoryError = ref("");
const memoryBusy = ref(false);

async function loadMemories(): Promise<void> {
  if (badId) return;
  memoryLoading.value = true;
  memoryError.value = "";
  try {
    const res = await api.listAssistantMemories(projectId);
    memories.value = res.items;
    memoryLimit.value = res.limit;
  } catch (err) {
    memoryError.value = textOf(err);
  } finally {
    memoryLoading.value = false;
  }
}

function toggleMemory(): void {
  memoryOpen.value = !memoryOpen.value;
  if (memoryOpen.value) void loadMemories();
}

async function removeMemory(item: AssistantMemory): Promise<void> {
  if (memoryBusy.value || badId) return;
  memoryBusy.value = true;
  memoryError.value = "";
  try {
    const res = await api.deleteAssistantMemory(projectId, item.id);
    memories.value = res.items;
    memoryLimit.value = res.limit;
  } catch (err) {
    memoryError.value = textOf(err);
  } finally {
    memoryBusy.value = false;
  }
}

onMounted(async () => {
  if (badId) return;
  // 附件草稿按项目隔离：路由里的项目 id 就是暂存区的 scope。
  draft.reset(projectId);
  // 先恢复本地历史，再让引导卡/通知入列，保证消息顺序自然。
  // （只还原文字；卡片类内容不恢复，见上方 history 小节。）
  restoreHistory();
  historyReady = true;
  // 刷新/关页时同步落盘：pagehide 是浏览器保证会跑的最后时机。
  window.addEventListener("pagehide", flushHistory);
  // 三条读取互相独立：任何一条失败都不能把后面的目录/轮询一起带走
  // （旧写法串行 await，后端重启窗口里一次失败会让整页停在空壳状态）。
  await Promise.all([
    loadLlm().catch(() => {
      /* 模型配置取不到时徽标显示兜底文案 */
    }),
    loadBookOptions(),
    api
      .getProject(projectId)
      .then((p) => {
        project.value = p;
      })
      .catch(() => {
        /* 项目名取不到不阻塞助手功能 */
      }),
  ]);
  try {
    // 恢复未完成的创作引导（会话在服务端，刷新/关页不丢）。
    await loadWizard();
  } catch {
    /* 引导恢复失败不影响问答 */
  }
  // 章节目录 / 字数：装饰性辅助，慢一点不影响首屏可聊。
  void loadDesk();
  // 记忆条数要显示在页头按钮上：先读一次（读不到就显示 0）。
  void loadMemories();
  // 引导卡先入列，再开始轮询：回执/卡住提醒是「之后发生的事」，必须排在卡片后面。
  pageAlive = true;
  void pollDistill();
});
</script>

<template>
  <div class="desk">
    <!-- 左栏目录已收进抽屉：默认不占宽，点抬头「目录」打开。 -->
    <Transition name="rail-fade">
      <div
        v-if="railOpen"
        class="rail-backdrop"
        aria-hidden="true"
        @click="railOpen = false"
      />
    </Transition>
    <aside
      v-if="!badId && chapterRail.length"
      class="desk-rail"
      :class="{ open: railOpen }"
      :inert="!railOpen"
      aria-label="章节目录"
    >
      <div class="rail-head">
        <span class="rail-title">目录</span>
        <span class="rail-count">{{ writtenChapterCount }} / {{ chapterRail.length }} 章</span>
        <button
          type="button"
          class="rail-close"
          aria-label="关闭目录"
          @click="railOpen = false"
        >×</button>
      </div>
      <ol class="rail-list">
        <li v-for="ch in chapterRail" :key="ch.chapter">
          <button
            type="button"
            class="rail-row"
            :class="{
              written: ch.written,
              latest: ch.chapter === latestWrittenChapter,
              selected: ch.chapter === selectedChapter,
            }"
            :aria-pressed="ch.chapter === selectedChapter"
            :title="`第 ${ch.chapter} 章 · ${ch.title}`"
            @click="selectChapter(ch)"
          >
            <span class="rail-no">{{ String(ch.chapter).padStart(2, "0") }}</span>
            <span class="rail-name">{{ ch.title }}</span>
          </button>
        </li>
      </ol>
      <div v-if="selectedChapterRow" class="rail-selected">
        <div class="rail-selected-title">
          第 {{ selectedChapterRow.chapter }} 章
          <span class="faint">{{ selectedChapterRow.written ? "已写" : "未写" }}</span>
        </div>
        <div class="rail-selected-actions">
          <button type="button" class="link-btn" @click="askAboutChapter">
            就这章提问
          </button>
          <router-link class="link-btn" :to="`/projects/${projectId}/outline`">
            编辑章纲
          </router-link>
        </div>
      </div>
      <router-link v-else class="rail-foot" :to="`/projects/${projectId}/outline`">
        编辑章纲 →
      </router-link>
    </aside>

    <div class="desk-body">
      <header class="page-head">
        <div class="eyebrow">助手工作台</div>
        <div class="asst-head">
          <h1>{{ project ? project.title : "未命名作品" }}</h1>
          <select
            v-if="bookSwitcherVisible"
            class="book-switcher"
            :value="projectId"
            aria-label="切换当前小说项目"
            @change="switchBook"
          >
            <option v-for="b in bookOptions" :key="b.id" :value="b.id">
              {{ b.title }}（{{ b.segment_count ?? 0 }} 段）
            </option>
          </select>
          <button
            v-if="chapterRail.length"
            class="link-btn rail-toggle"
            type="button"
            :aria-expanded="railOpen"
            @click="railOpen = true"
          >
            目录
            <span class="rail-toggle-count">{{ writtenChapterCount }}/{{ chapterRail.length }}</span>
          </button>
          <button class="link-btn back-link" @click="router.push(`/projects/${projectId}`)">
            ← 项目工作台
          </button>
          <button
            class="link-btn memory-toggle"
            :class="{ on: memoryOpen }"
            :aria-expanded="memoryOpen"
            @click="toggleMemory"
          >
            记忆 <span class="memory-count">{{ memories.length }}</span>
          </button>
        </div>

        <!-- 写作台抬头：写到哪了、多少字，一眼可见 -->
        <div class="book-stats">
          <span><b>{{ bookSegments ?? "—" }}</b> 段正文</span>
          <span><b>{{ bookChars === null ? "—" : bookChars.toLocaleString() }}</b> 字</span>
          <span><b>{{ writtenChapterCount || "—" }}</b> 章已写</span>
          <span v-if="latestWrittenChapter">写到 <b>第 {{ latestWrittenChapter }} 章</b></span>
        </div>

        <div v-if="badId" class="notice err">路由缺少有效的项目 id（/projects/:id/assistant）。</div>

    <div class="asst-meta">
      <!-- 三个模型徽标：生成 / 助手 / 蒸馏分开显示，一眼看出是三回事 -->
      <span
        class="tag model-badge"
        :class="writerCfg?.is_fallback ? 'warn' : 'info'"
        :title="writerCfg ? `生成模型（Writer）：${writerCfg.label} · ${writerCfg.model}` : '生成模型配置未取到'"
      >
        生成 · {{ writerCfg?.is_fallback ? "本地兜底" : writerCfg?.label }}
      </span>
      <span
        class="tag model-badge"
        :class="assistantCfg?.is_fallback ? 'warn' : 'info'"
        :title="assistantCfg ? `助手模型（Assistant）：${assistantCfg.label} · ${assistantCfg.model}` : '助手模型配置未取到'"
      >
        助手 · {{ assistantCfg?.is_fallback ? "本地兜底" : assistantCfg?.label }}
      </span>
      <span
        class="tag model-badge"
        :class="distillerCfg?.is_fallback ? 'warn' : 'info'"
        :title="distillerCfg ? `提炼模型（Distiller，文风档案抽取用）：${distillerCfg.label} · ${distillerCfg.model}` : '提炼模型配置未取到'"
      >
        提炼 · {{ distillerCfg?.is_fallback ? "本地兜底" : distillerCfg?.label }}
      </span>
      <!-- 模型设置只有一个入口：跳路由页 /settings（弹窗已退役） -->
      <button class="btn sm" @click="openModelSettings">模型设置</button>
      <span
        v-if="!rolesReady"
        class="faint"
        style="font-size: var(--fs-xs)"
        title="后端未返回全部分角色配置，徽标当前显示的是同一份单模型生效值"
      >配置未齐全</span>
    </div>

    <p class="asst-intro">
      想写小说就说一句「我想生成一本小说」，我一步一步问你；
      不想走引导，也可以直接问正文、语料或图谱上的问题。
    </p>
  </header>

  <!-- 工序链：全流程走到哪一步、卡在哪、哪条改动等你批（服务端每次重新评估） -->
  <WorkflowStrip v-if="!badId" :project-id="projectId" />

  <section class="card assistant">
    <!-- ==================== 对话流 ==================== -->
    <div ref="streamEl" class="stream">
      <div v-if="!hasHistory" class="stream-hint">
        <p><strong>要写小说？</strong>我一步步问：参考语料、提炼档位、题材、字数、章节。</p>
        <div class="row tight">
          <button
            class="btn primary sm"
            :disabled="wizardBusy || badId"
            @click="startWizardFlow('好，我一步一步问你。先定这次要做哪一种作品。')"
          >
            {{ wizardBusy ? "准备中…" : "开始创作引导" }}
          </button>
          <span class="faint" style="font-size: var(--fs-xs)">
            或者说一句「我想生成一本小说」
          </span>
        </div>
        <p class="faint">
          也可以直接问：<span class="mono">第 3 段和第 1 段的动机是不是重复了？</span>
        </p>
      </div>

      <div v-for="turn in turns" :key="turn.key" class="turn" :class="turn.role">
        <!-- 用户气泡 -->
        <div v-if="turn.role === 'user'" class="bubble user">
          <div>{{ turn.text }}</div>
          <div v-if="turn.attachments?.length" class="file-list sent">
            <span v-for="file in turn.attachments" :key="file.key" class="file-chip">
              {{ file.filename }}
            </span>
          </div>
        </div>

        <!-- 助手气泡：创作引导（解释正文 + 卡片 / 结果） -->
        <div
          v-else-if="turn.wizard || turn.wizardResult || turn.note || turn.notice"
          class="bubble assistant wizard-bubble"
        >
          <div class="msg-head">
            <span class="msg-avatar">AI</span>
            <span class="msg-name">创作引导</span>
          </div>
          <div v-if="turn.error" class="notice err">{{ turn.error }}</div>
          <p v-if="turn.text" class="wz-prose" :class="{ note: turn.note }">{{ turn.text }}</p>

          <!--
            提炼进度：只一行结论 + 百分比（没有探明总块数就不显示百分比）。
            挂在最后一张引导卡下面，刷新后由 GET /wizard 的 distill 字段恢复。
          -->
          <div
            v-if="turn.key === lastWizardTurnKey && distillJob && distillActive"
            class="wz-distill"
            :class="{ stalled: distillJob.health === 'stalled' }"
          >
            <div class="wz-distill-head">
              <span class="tag" :class="distillJob.health === 'stalled' ? 'warn' : 'accent'">
                {{ distillTag(distillJob) }}
              </span>
              <span class="wz-distill-stage">{{ distillJob.stage || "提炼中" }}</span>
              <span v-if="distillPercent !== null" class="mono wz-distill-pct">
                {{ distillPercent }}%
              </span>
            </div>
            <div v-if="distillPercent !== null" class="wz-distill-bar">
              <span :style="{ width: distillPercent + '%' }" />
            </div>
            <p class="faint wz-distill-note">{{ distillSentence(distillJob) }}</p>
          </div>

          <!-- 完成回执 / 疑似卡住提醒（持久化通知，ack 后不再重复） -->
          <div v-if="turn.notice" class="wz-notice" :class="turn.notice.kind">
            <p v-if="turn.notice.payload.next" class="wz-notice-next">
              {{ turn.notice.payload.next }}
            </p>
            <div class="row tight">
              <button
                v-if="turn.notice.payload.resumable"
                class="btn primary sm"
                :disabled="resumeBusy || distillActive"
                @click="resumeDistill(turn)"
              >
                {{ resumeBusy || distillActive ? "正在接着跑…" : "继续提炼" }}
              </button>
              <button
                v-if="turn.notice.kind === 'distill_done' && canRun(wizard?.result)"
                class="btn primary sm"
                :disabled="runBusy"
                @click="runGeneration(turn)"
              >
                {{ runBusy ? "正在写，别关页面…" : "开始写" }}
              </button>
              <button
                v-if="turn.notice.kind === 'distill_done' && wizard?.result?.bible"
                class="btn sm"
                :disabled="rebuildCardsBusy"
                @click="rebuildCardsWithArchive()"
              >
                {{ rebuildCardsBusy ? "重建中…" : "用档案重建角色卡" }}
              </button>
              <button class="btn ghost sm" @click="router.push(`/projects/${projectId}`)">
                去项目工作台 →
              </button>
            </div>
          </div>

          <!-- 写操作回执：改了什么、从什么改成什么、影响是什么，可撤销 -->
          <div v-if="turn.action" class="wz-action">
            <div class="row tight" style="align-items: center">
              <span class="tag accent mono">{{ turn.action.action }}</span>
              <span class="tag mono">target={{ turn.action.target }}</span>
              <span class="tag mono">审计 #{{ turn.action.id ?? "—" }}</span>
            </div>
            <div class="wz-action-diff">
              <div>
                <span class="blk-head">旧值</span>
                <span class="mono">{{ echoValue(turn.action.old) }}</span>
              </div>
              <div>
                <span class="blk-head">新值</span>
                <span class="mono">{{ echoValue(turn.action.new) }}</span>
              </div>
              <div v-if="turn.action.impact">
                <span class="blk-head">影响</span>
                <span>{{ turn.action.impact }}</span>
              </div>
            </div>
            <div class="row tight" style="align-items: center">
              <button
                v-if="turn.undoId && !turn.undoDone"
                class="btn sm"
                :disabled="turn.undoBusy"
                @click="undoAction(turn)"
              >
                {{ turn.undoBusy ? "撤销中…" : "撤销这次修改" }}
              </button>
              <span v-else-if="turn.undoDone" class="tag ok">已撤销</span>
              <span
                v-else-if="turn.undoable === false"
                class="faint"
                style="font-size: var(--fs-xs)"
              >
                这一步不可撤销（已提交后台任务）
              </span>
            </div>
          </div>

          <WizardStepCard
            v-if="turn.wizard"
            :question="turn.wizard.question"
            :busy="turn.wizard.busy"
            :disabled="turn.wizard.disabled"
            :answered-label="turn.wizard.answeredLabel"
            @answer="(v, l) => answerStep(turn, v, l)"
            @skip="skipStep(turn)"
            @back="backStep(turn)"
            @goto="(step) => gotoStep(turn, step)"
          />

          <template v-if="turn.wizardResult">
            <p class="wz-prose">{{ turn.wizardResult.message }}</p>

            <div v-if="turn.wizardResult.stage === 'outline' && turn.wizardResult.bible" class="blk">
              <button
                class="link-btn"
                @click="toggle(`bible-${turn.key}`)"
              >
                {{ isOpen(`bible-${turn.key}`) ? "收起大纲" : "查看生成的大纲" }}
              </button>
              <pre
                v-if="isOpen(`bible-${turn.key}`)"
                class="diffbox snippet expanded mono"
              >{{ prettyJson(turn.wizardResult.bible) }}</pre>
            </div>

            <!-- 以剧情圣经为准渲染：历史会话没有 character_cards 字段，也要能看到卡/角色。 -->
            <div
              v-if="turn.wizardResult.stage === 'outline' && bibleCharacters(turn.wizardResult).length"
              class="wz-cards"
            >
              <div class="row tight">
                <span class="tag mono">角色卡 {{ cardCount(turn.wizardResult) }} 张</span>
                <span v-if="turn.wizardResult.character_cards?.collisions" class="tag">
                  声纹撞车 {{ turn.wizardResult.character_cards.collisions }}
                </span>
                <button
                  v-if="missingCards(turn.wizardResult).length"
                  class="link-btn"
                  :disabled="!!cardsBusyName"
                  @click="regenerateCards(turn, missingCards(turn.wizardResult), 'missing')"
                >
                  {{ cardsBusyName === "missing"
                    ? "补齐中…"
                    : `补齐缺失（${missingCards(turn.wizardResult).length}）` }}
                </button>
                <button
                  class="link-btn"
                  :disabled="!!cardsBusyName"
                  @click="regenerateCards(turn, [], 'all')"
                >
                  {{ cardsBusyName === "all" ? "生成中…" : "重生成全部" }}
                </button>
              </div>

              <ul class="wz-card-list">
                <li
                  v-for="c in bibleCharacters(turn.wizardResult)"
                  :key="c.name"
                  class="wz-card-item"
                >
                  <div class="wz-card-head">
                    <strong>{{ c.name }}</strong>
                    <span class="tag" :class="{ mono: !!c.card }">
                      {{ c.card ? "已生成" : "未生成" }}
                    </span>
                    <span v-if="c.card && c.card['来源'] === 'user'" class="tag">手写</span>
                    <div class="wz-card-actions">
                      <button
                        v-if="c.card"
                        class="link-btn"
                        :disabled="cardSaving"
                        @click="beginCardEdit(c, turn)"
                      >
                        编辑
                      </button>
                      <button
                        v-if="c.card"
                        class="link-btn wz-danger"
                        :disabled="cardDeleting"
                        @click="cardDeleteName = c.name"
                      >
                        删除卡
                      </button>
                      <button
                        class="link-btn wz-danger"
                        :disabled="characterRemoving"
                        title="从剧情圣经整体移除，不只是删卡"
                        @click="characterRemoveName = c.name"
                      >
                        移除角色
                      </button>
                      <button
                        class="link-btn"
                        :disabled="!!cardsBusyName || cardSaving"
                        @click="regenerateCards(turn, [c.name], c.name)"
                      >
                        {{ cardsBusyName === c.name
                          ? "生成中…"
                          : c.card ? "重生成" : "生成" }}
                      </button>
                    </div>
                  </div>
                  <div v-if="c.card" class="wz-card-meta">{{ cardIdentityLine(c.card) }}</div>
                  <div v-if="c.card" class="wz-card-meta">{{ cardVoiceLine(c.card) }}</div>

                  <div v-if="cardDeleteName === c.name" class="wz-card-confirm">
                    <span>只删除角色卡，角色仍保留在剧情圣经里。</span>
                    <button
                      class="btn ghost sm wz-danger"
                      :disabled="cardDeleting"
                      @click="deleteCard(turn, c.name)"
                    >
                      {{ cardDeleting ? "删除中…" : "确认删除" }}
                    </button>
                    <button class="btn ghost sm" @click="cardDeleteName = null">取消</button>
                  </div>

                  <div v-if="characterRemoveName === c.name" class="wz-card-confirm">
                    <span>
                      会从剧情圣经整体移除「{{ c.name }}」<template
                        v-if="relationshipCount(turn.wizardResult, c.name)"
                      >，并清理 {{ relationshipCount(turn.wizardResult, c.name) }} 条相关关系</template>。
                    </span>
                    <button
                      class="btn ghost sm wz-danger"
                      :disabled="characterRemoving"
                      @click="removeCharacter(turn, c.name)"
                    >
                      {{ characterRemoving ? "移除中…" : "确认移除" }}
                    </button>
                    <button class="btn ghost sm" @click="characterRemoveName = null">取消</button>
                    <span v-if="cardEditError" class="notice err">{{ cardEditError }}</span>
                    <span class="wz-card-confirm-hint">
                      正文与已抽好的图谱保留，只是以后写文不再把 TA 当锁定角色；此操作不可撤销。
                    </span>
                  </div>
                </li>
              </ul>
            </div>

            <div v-if="turn.wizardResult.segments?.length" class="wz-done">
              <div
                v-for="seg in turn.wizardResult.segments"
                :key="String(seg.segment_no)"
                class="wz-seg"
              >
                <span class="tag mono">第 {{ seg.segment_no }} 段 · {{ seg.chars }} 字</span>
                <div class="prose snippet expanded">{{ seg.excerpt }}…</div>
              </div>
            </div>

            <div class="row tight" style="margin-top: var(--sp-2)">
              <button
                v-if="canRun(turn.wizardResult)"
                class="btn primary sm"
                :disabled="runBusy"
                @click="runGeneration(turn)"
              >
                {{ runBusy ? "正在写，别关页面…" : "开始写" }}
              </button>
              <button
                v-if="turn.wizardResult.stage === 'role'"
                class="btn sm"
                @click="exitRole"
              >
                结束角色对话
              </button>
              <button
                class="btn ghost sm"
                @click="router.push(`/projects/${projectId}`)"
              >
                {{ turn.wizardResult.stage === "done" ? "看全文 →" : "去项目工作台 →" }}
              </button>
            </div>
          </template>
        </div>

        <!-- 助手气泡：普通问答 -->
        <div v-else class="bubble assistant">
          <div class="msg-head">
            <span class="msg-avatar">AI</span>
            <span class="msg-name">助手</span>
            <span v-if="turn.historical" class="tag" title="从本机历史恢复的文本记录">
              历史
            </span>
            <div class="row tight bubble-meta" style="align-items: center">
              <span v-if="turn.agentRunId" class="tag" :class="confidenceTag(turn.confidence)">
                {{ confidenceLabel(turn.confidence) }} {{ turn.confidence.toFixed(2) }}
              </span>
              <span v-if="turn.evidence.length" class="tag mono">
                证据 {{ turn.evidence.length }} 条
              </span>
              <span v-if="turn.issues.length" class="tag mono">
                自查 {{ turn.issues.length }}
              </span>
              <details v-if="turn.agentRunId" class="meta-detail">
                <summary>详情</summary>
                <span class="mono">AgentRun #{{ turn.agentRunId }}</span>
              </details>
            </div>
          </div>

          <div v-if="turn.loading" class="loading-line">助手正在查证…</div>

          <div v-if="turn.error" class="notice err">{{ turn.error }}</div>

          <template v-if="!turn.loading">
            <!-- 回答正文 -->
            <div class="blk">
              <div
                class="prose snippet"
                :class="{ expanded: isOpen(`answer-${turn.key}`) }"
              >
                {{ turn.answer || "（助手未返回文字回答，请直接看下方证据）" }}
              </div>
              <button class="link-btn" @click="toggle(`answer-${turn.key}`)">
                {{ isOpen(`answer-${turn.key}`) ? "收起" : "展开全文" }}
              </button>
            </div>

            <!-- agent 自主跑出的 issues：复用统一渲染器（文字 + 色标双编码）。
                 不传 answer：上面刚渲染过同一段回答，重复一遍只会让人以为是两件事。 -->
            <AssistantIssueList
              v-if="turn.issues.length"
              :issues="turn.issues"
              :evidence="turn.evidence"
              :confidence="turn.confidence"
              :rules-evaluated="turn.rulesEvaluated"
              :agent-run-id="turn.agentRunId"
            />

            <!-- 工具调用轨迹：默认折叠；确定性回落时只给一行说明。
                 tool_trace 为空且不是回落 → 整块不渲染（旧后端行为一致）。 -->
            <div
              v-if="turn.toolTrace.length || turn.toolMode === 'deterministic'"
              class="blk"
            >
              <div class="blk-head">
                工具调用{{ turn.toolTrace.length ? `（${turn.toolTrace.length} 次）` : "" }}
              </div>
              <div v-if="turn.toolMode === 'deterministic'" class="tool-mode-hint">
                本次由确定性检索取证（模型未自主调用工具）。
                <span v-if="turn.toolFallbackReason" class="mono">
                  原因：{{ turn.toolFallbackReason }}
                </span>
              </div>
              <button
                v-if="turn.toolTrace.length"
                class="link-btn"
                @click="toggle(`tools-${turn.key}`)"
              >
                {{ isOpen(`tools-${turn.key}`) ? "收起调用轨迹" : "展开调用轨迹" }}
              </button>
              <div
                v-if="turn.toolTrace.length && isOpen(`tools-${turn.key}`)"
                class="ev-list"
              >
                <div
                  v-for="t in turn.toolTrace"
                  :key="`tool-${turn.key}-${t.seq}`"
                  class="ev"
                >
                  <div class="row tight" style="align-items: center">
                    <span class="tag mono">#{{ t.seq }} · 第 {{ t.step }} 轮</span>
                    <span class="tag accent mono">{{ t.name }}</span>
                    <span class="tag" :class="toolStatusMeta(t.status).tag">
                      {{ toolStatusMeta(t.status).label }}
                    </span>
                    <span class="tag mono">{{ t.latency_ms }}ms</span>
                  </div>
                  <div class="tool-args mono">参数：{{ t.arguments }}</div>
                  <div
                    v-if="t.status === 'ok' && toolResultText(turn, t.name)"
                    class="prose snippet mono"
                  >
                    结果：{{ toolResultText(turn, t.name) }}
                  </div>
                </div>
              </div>
            </div>

            <!-- 证据 -->
            <div v-if="turn.evidence.length" class="blk">
              <div class="blk-head">证据（{{ turn.evidence.length }} 条）</div>
              <div class="ev-list">
                <div v-for="(ev, i) in turn.evidence" :key="`ev-${turn.key}-${i}`" class="ev">
                  <div class="row tight" style="align-items: center">
                    <span class="tag accent">{{ sourceLabel(ev.source) }}</span>
                    <span class="tag mono">source={{ ev.source }}</span>
                    <span class="tag mono">kind={{ ev.kind }}</span>
                    <span class="tag mono">score={{ Number(ev.score).toFixed(3) }}</span>
                    <span class="tag mono">ref={{ ev.ref_id }}</span>
                  </div>
                  <div
                    class="prose snippet"
                    :class="{ expanded: isOpen(`ev-${turn.key}-${i}`) }"
                  >
                    {{ ev.snippet }}
                  </div>
                  <button class="link-btn" @click="toggle(`ev-${turn.key}-${i}`)">
                    {{ isOpen(`ev-${turn.key}-${i}`) ? "收起原文" : "展开原文" }}
                  </button>
                </div>
              </div>
            </div>

            <!-- 待确认卡片：pending_actions 为空时这块整个不渲染 -->
            <div v-if="turn.cards.length" class="cards">
              <div class="cards-hint">
                助手想动手改东西，需要你点头（不确认就不会发生）：
              </div>
              <div v-for="card in turn.cards" :key="card.key" class="card-pending">
                <div class="row tight" style="align-items: center">
                  <span class="tag accent">{{ kindLabelOf(card) }}</span>
                  <span class="tag mono">kind={{ card.action.kind }}</span>
                  <span class="tag" :class="riskMetaOf(card).tag">
                    {{ riskMetaOf(card).label }}
                  </span>
                  <span
                    v-if="card.action.body && typeof card.action.body.risk_level === 'string'"
                    class="tag mono"
                  >
                    {{ riskLabel(card.action.body.risk_level) }}
                  </span>
                </div>

                <div class="card-title">{{ card.action.title }}</div>
                <div class="card-hint">{{ riskMetaOf(card).hint }}</div>

                <div v-if="card.rows.length" class="meta-grid">
                  <div v-for="row in card.rows" :key="row.key">
                    <span class="blk-head">{{ row.label }}</span>
                    <div class="mono" style="font-size: var(--fs-xs)">{{ row.value }}</div>
                  </div>
                </div>

                <!-- 待确认时明确告知将调用的端点：用户有权知道按下去会发生什么 -->
                <div v-if="card.state === 'pending'" class="faint mono endpoint">
                  将执行 {{ card.action.method }} {{ card.action.endpoint }}（请求体由服务端下发，
                  前端不参与构造）
                </div>

                <div v-if="card.state === 'running'" class="notice info">
                  执行中 · 已等待 {{ elapsedText(card) }}
                </div>

                <div v-if="card.state === 'confirmed'" class="notice ok confirm-box">
                  <div>{{ card.notice }}</div>
                  <div
                    v-for="(x, i) in resultExtras(card.result)"
                    :key="`x-${card.key}-${i}`"
                    class="blk"
                  >
                    <span class="blk-head">{{ x.label }}</span>
                    <pre
                      class="diffbox snippet"
                      :class="{ expanded: isOpen(`rx-${card.key}-${i}`) }"
                    >{{ x.text }}</pre>
                    <button class="link-btn" @click="toggle(`rx-${card.key}-${i}`)">
                      {{ isOpen(`rx-${card.key}-${i}`) ? "收起" : "展开完整内容" }}
                    </button>
                  </div>
                </div>

                <div v-if="card.state === 'failed'" class="notice err confirm-box">
                  <div>{{ card.notice }}</div>
                </div>

                <!-- 执行轨迹：确认 / 请求发出 / 落库 / 完成，各行都带本地时刻 -->
                <ol v-if="card.trace.length" class="trace">
                  <li
                    v-for="(s, i) in card.trace"
                    :key="`t-${card.key}-${i}`"
                    :class="s.tone"
                  >
                    <span class="trace-time mono">{{ clockText(s.at) }}</span>
                    <span class="trace-text">{{ s.text }}</span>
                  </li>
                </ol>

                <div class="row tight">
                  <button
                    class="btn primary"
                    :disabled="card.state !== 'pending' && card.state !== 'failed'"
                    @click="confirmCard(card)"
                  >
                    {{
                      card.state === "running"
                        ? "执行中…"
                        : card.state === "confirmed"
                          ? "已确认"
                          : card.state === "failed"
                            ? "重试"
                            : "确认执行"
                    }}
                  </button>
                  <button
                    class="btn"
                    :disabled="card.state === 'running' || card.state === 'confirmed'"
                    @click="cancelCard(turn, card)"
                  >
                    取消
                  </button>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>

      <!--
        没有引导卡可挂时的常驻进度（例如用户在项目工作台提交提炼后切回助手）。
        有的卡就不重复画：同一份进度在一屏里出现两次会让人以为有两个任务。
      -->
      <div v-if="distillActive && !lastWizardTurnKey" class="turn assistant">
        <div class="bubble assistant wizard-bubble">
          <div class="wz-distill" :class="{ stalled: distillJob?.health === 'stalled' }">
            <div class="wz-distill-head">
              <span class="tag" :class="distillJob?.health === 'stalled' ? 'warn' : 'accent'">
                {{ distillTag(distillJob) }}
              </span>
              <span class="wz-distill-stage">{{ distillJob?.stage || "提炼中" }}</span>
              <span v-if="distillPercent !== null" class="mono wz-distill-pct">
                {{ distillPercent }}%
              </span>
            </div>
            <div v-if="distillPercent !== null" class="wz-distill-bar">
              <span :style="{ width: distillPercent + '%' }" />
            </div>
            <p class="faint wz-distill-note">{{ distillSentence(distillJob) }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== 输入 ==================== -->
    <form class="composer" @submit.prevent="send">
      <div v-if="wizardActive" class="composer-hint">
        引导进行中：直接打字回答，或问「为什么」我就地解释；缺参数就让我改（如「采样改成 200 章」「跳过风格」），改完可撤销。
        <button type="button" class="link-btn" @click="startWizardFlow('重新开始，先定方向。', true)">
          重新开始
        </button>
      </div>
      <div v-else-if="roleMode" class="composer-hint">
        正在与「{{ roleMode.character }}」对话 · 情境：{{ roleMode.scene }}
        <button type="button" class="link-btn" @click="exitRole">结束对话</button>
      </div>

      <!-- 附件草稿区：上传只进项目暂存区；投放必须由上方卡片确认后触发。 -->
      <div v-if="draft.items.length" class="file-list" aria-live="polite">
        <div
          v-for="file in draft.items"
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
              :disabled="sending"
              @click="togglePreview(file.metadata.attachment_id)"
            >
              {{ previewOf(file.metadata.attachment_id).data ? "收起清洗预览" : "清洗预览" }}
            </button>
            <button
              v-if="file.state === 'failed'"
              class="link-btn"
              type="button"
              :disabled="sending"
              @click="draft.retry(file.key)"
            >
              重试
            </button>
            <button
              class="link-btn"
              type="button"
              :disabled="sending"
              :aria-label="`移除 ${file.file.name}`"
              @click="draft.remove(file.key)"
            >
              移除
            </button>
          </div>
          <span v-if="file.error" class="file-error" role="alert">{{ file.error }}</span>
          <div
            v-if="file.metadata && (previewOf(file.metadata.attachment_id).loading || previewOf(file.metadata.attachment_id).error || previewOf(file.metadata.attachment_id).data)"
            class="file-preview"
          >
            <span v-if="previewOf(file.metadata.attachment_id).loading" class="faint">
              正在读取清洗预览…
            </span>
            <span
              v-else-if="previewOf(file.metadata.attachment_id).error"
              class="file-error"
              role="alert"
            >
              {{ previewOf(file.metadata.attachment_id).error }}
            </span>
            <template v-else>
              <p class="faint">
                清洗后 {{ previewOf(file.metadata.attachment_id).data?.chapters }} 章 ·
                待复核残留 {{ previewOf(file.metadata.attachment_id).data?.unresolved_count }} 处
              </p>
              <ul v-if="previewOf(file.metadata.attachment_id).data?.changes.length" class="file-changes">
                <li v-for="(change, i) in previewOf(file.metadata.attachment_id).data?.changes ?? []" :key="i">
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
        v-model="query"
        class="input"
        rows="2"
        :disabled="badId"
        :placeholder="
          roleMode
            ? `对${roleMode.character}说点什么…`
            : wizardActive
              ? '直接回答这一步，或问我「为什么」；也可以让我改：采样改成 200 章 / 跳过风格'
              : '问助手一个问题，例如：主角第 3 段的动机和第 1 段一致吗？'
        "
        @keydown="onKeydown"
      ></textarea>
      <div class="row tight" style="align-items: center">
        <input
          ref="fileInput"
          type="file"
          accept=".txt,.md,text/plain,text/markdown"
          multiple
          hidden
          :disabled="badId || sending"
          @change="onFilesSelected"
        />
        <button
          class="btn"
          type="button"
          title="支持 .txt / .md；点卡片确认后才投放"
          :disabled="badId || sending"
          @click="fileInput?.click()"
        >
          附件
        </button>
        <button class="btn primary" type="submit" :disabled="!canSend">
          {{ sending ? "思考中…" : "发送" }}
        </button>
        <span
          class="faint"
          style="font-size: var(--fs-xs)"
          title="写操作都会留审计记录，可在提案与版本历史中撤销"
        >
          Enter 发送 · Shift+Enter 换行
        </span>
      </div>
    </form>
  </section>
    </div>
  </div>

  <!-- 记忆抽屉：内容由助手自动抽取，这里只做「看见 / 删除」。
       抽错了要能删掉，而不是让用户只能忍着。 -->
  <aside v-if="memoryOpen" class="memory-drawer" aria-label="助手记忆">
    <div class="memory-head">
      <div>
        <div class="memory-title">助手记忆</div>
        <div class="memory-sub">
          {{ memories.length }} / {{ memoryLimit }} 条 · 自动记住，每次问答都会带上
        </div>
      </div>
      <button class="link-btn" @click="memoryOpen = false">关闭</button>
    </div>

    <div v-if="memoryError" class="notice err">{{ memoryError }}</div>
    <div v-else-if="memoryLoading" class="faint">读取记忆…</div>
    <ol v-else-if="memories.length" class="memory-list">
      <li v-for="m in memories" :key="m.id">
        <span class="memory-text">{{ m.text }}</span>
        <button class="link-btn" :disabled="memoryBusy" @click="removeMemory(m)">
          删除
        </button>
      </li>
    </ol>
    <p v-else class="faint">
      还没有记忆。聊几句之后，助手会自动记住你的写作偏好（人称、称呼、禁忌这类），
      并在这里显示。
    </p>
  </aside>

  <!-- 角色卡编辑：居中模态卡片，保存成功后自动关闭。 -->
  <div v-if="cardEditName" class="card-edit-mask" @click.self="cancelCardEdit">
    <div
      ref="cardEditModalEl"
      class="card-edit-modal"
      role="dialog"
      aria-modal="true"
      aria-label="编辑角色卡"
      :style="cardEditModalStyle"
    >
      <header class="card-edit-head">
        <div>
          <div class="card-edit-kicker">Character card</div>
          <h3>{{ cardEditName }}</h3>
        </div>
        <button class="link-btn" type="button" @click="cancelCardEdit">关闭</button>
      </header>
      <form class="card-edit-form" @submit.prevent="saveCard">
        <div class="card-edit-grid">
          <label v-for="f in CARD_FIELDS" :key="f.key" class="field">
            <span>{{ f.label }}</span>
            <input v-model="cardDraft[f.key]" class="input" :placeholder="f.hint" />
          </label>
        </div>
        <div v-if="cardEditError" class="notice err">{{ cardEditError }}</div>
        <footer class="card-edit-actions">
          <button class="btn ghost" type="button" @click="cancelCardEdit">取消</button>
          <button class="btn primary" type="submit" :disabled="cardSaving">
            {{ cardSaving ? "保存中…" : "保存" }}
          </button>
        </footer>
      </form>
      <span
        class="modal-resize-handle"
        title="拖动调整大小"
        @pointerdown="startCardEditResize"
      />
    </div>
  </div>
</template>

<style scoped>
/* ---- 写作台布局：目录抽屉 + 主区 ---- */
.desk {
  display: block;
}
.desk-body {
  width: 100%;
  max-width: 880px;
  margin: 0 auto;
  min-width: 0;
}
.rail-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.rail-toggle-count {
  color: var(--fg-faint);
  font-family: var(--mono, monospace);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}
.rail-backdrop {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(28, 22, 16, .32);
  opacity: 1;
  /* 离场稍快：关抽屉要跟手，不能拖住下一次点击 */
  transition: opacity .18s cubic-bezier(.4, 0, 1, 1);
}
.rail-fade-enter-active {
  transition: opacity .26s cubic-bezier(.16, 1, .3, 1);
}
.rail-fade-leave-active {
  pointer-events: none;
}
.rail-fade-enter-from,
.rail-fade-leave-to {
  opacity: 0;
}
.desk-rail {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  z-index: 50;
  width: min(86vw, 340px);
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border-strong);
  background: var(--bg-elev);
  box-shadow: 12px 0 40px rgba(28, 22, 16, .18);
  overflow: hidden;
  transform: translateX(-100%);
  /* 关闭：加速离场，不给收抽屉拖泥带水的感觉 */
  transition: transform .2s cubic-bezier(.4, 0, 1, 1);
}
.desk-rail.open {
  transform: translateX(0);
  /* 打开：快起步、慢收尾，滑到位时有轻微「落定」感 */
  transition: transform .3s cubic-bezier(.16, 1, .3, 1);
}
/* 内容比面板晚半拍到位：整体滑动之外多一层层次，不至于像一块板子平移 */
.desk-rail > * {
  transition: opacity .18s linear, transform .26s cubic-bezier(.16, 1, .3, 1);
}
.desk-rail:not(.open) > * {
  opacity: 0;
  transform: translateX(-12px);
}
@media (prefers-reduced-motion: reduce) {
  .desk-rail,
  .desk-rail > *,
  .rail-backdrop {
    transition: none;
  }
}
.rail-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 14px 16px 10px;
  border-bottom: 1px solid var(--border);
}
.rail-close {
  margin-left: 6px;
  padding: 0 2px;
  border: 0;
  background: none;
  color: var(--fg-muted);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}
.rail-close:hover { color: var(--fg); }
.rail-title {
  font-family: var(--font-display);
  font-size: var(--fs-lg);
  font-weight: 700;
  letter-spacing: 0.02em;
}
.rail-count {
  margin-left: auto;
  color: var(--fg-faint);
  font-size: var(--fs-xs);
  font-variant-numeric: tabular-nums;
}
.rail-list {
  list-style: none;
  margin: 0;
  padding: 8px;
  overflow-y: auto;
  flex: 1;
}
.rail-list li {
  /* 行内容交给 .rail-row（button），li 只负责排布：整行可点才像目录 */
  padding: 0;
}
.rail-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
  width: 100%;
  padding: 7px 10px;
  border: 0;
  border-radius: var(--radius-md);
  background: none;
  color: var(--fg-faint);
  font: inherit;
  font-size: var(--fs-sm);
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease;
}
.rail-row:hover {
  background: var(--bg-sunken);
  color: var(--fg);
}
/* 已写章节用正文色：目录本身就承载「写到哪」的信息，不另加图标 */
.rail-row.written {
  color: var(--fg);
}
.rail-row.latest .rail-name {
  font-weight: 600;
}
/* 选中态：底色 + 左侧色条，和「最新章」的加粗区分开（一个是位置，一个是选择） */
.rail-row.selected {
  background: var(--accent-soft);
  color: var(--accent-ink);
  box-shadow: inset 2px 0 0 var(--accent);
}
.rail-no {
  flex: none;
  width: 2.2em;
  font-variant-numeric: tabular-nums;
  opacity: 0.65;
}
.rail-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rail-selected {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 16px 12px;
  border-top: 1px solid var(--border);
  background: var(--accent-soft);
}
.rail-selected-title {
  color: var(--accent-ink);
  font-size: var(--fs-xs);
  font-weight: 600;
}
.rail-selected-actions {
  display: flex;
  gap: 12px;
  font-size: var(--fs-xs);
}
.rail-foot {
  display: block;
  padding: 10px 16px 12px;
  border-top: 1px solid var(--border);
  color: var(--fg-muted);
  font-size: var(--fs-xs);
}
.rail-foot:hover {
  color: var(--accent-ink);
}

/* ---- 写作台抬头：书名 + 进度 ---- */
.book-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 20px;
  margin: 2px 0 4px;
  color: var(--fg-muted);
  font-size: var(--fs-sm);
}
.book-stats b {
  color: var(--fg);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.assistant {
  margin-top: 4px;
  padding: 24px;
  border-color: var(--border);
  box-shadow: var(--shadow-md);
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
/* 对话区不再套一层「控制台卡片」：居中一列、无外框，像常规 agent 会话。 */
.card.assistant {
  max-width: 860px;
  margin: 8px auto 0;
  padding: 0 4px;
  border: none;
  box-shadow: none;
  background: transparent;
}
.asst-head {
  display: flex;
  align-items: baseline;
  gap: var(--gap);
  flex-wrap: wrap;
}
.book-switcher {
  max-width: 220px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--fg-muted);
  font: inherit;
  font-size: var(--fs-xs);
  cursor: pointer;
}
.book-switcher:hover {
  color: var(--fg);
  border-color: var(--border-strong);
}
.asst-head h1 {
  font-family: var(--font-display);
  font-size: 30px;
  font-weight: 700;
  letter-spacing: 0.01em;
  margin-top: 6px;
}
.back-link {
  font-size: var(--fs-sm);
}

/* ---- 记忆入口与抽屉 ---- */
.memory-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
}
.memory-toggle.on {
  color: var(--accent-ink);
}
.memory-count {
  display: inline-grid;
  place-items: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: var(--radius-full);
  background: var(--bg-sunken);
  color: var(--fg-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.memory-toggle.on .memory-count {
  background: var(--accent-soft);
  color: var(--accent-ink);
}

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
.asst-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--gap-sm);
  margin-top: 10px;
}
.asst-project {
  font-size: var(--fs-sm);
}
.model-badge {
  cursor: default;
}
.bubble-meta {
  gap: 8px;
  margin-bottom: 6px;
}
.meta-detail summary {
  color: var(--fg-muted);
  font-size: 11px;
  cursor: pointer;
}
.meta-detail span {
  margin-left: 6px;
  color: var(--fg-muted);
  font-size: 11px;
}

/* 对话流：像常规 agent 会话一样——中间一列、消息间留白、滚到底。 */
.stream {
  display: flex;
  flex-direction: column;
  gap: 22px;
  min-height: 20em;
  max-height: 66vh;
  overflow-y: auto;
  padding: 10px 6px 18px 0;
  scroll-behavior: smooth;
}
.stream-hint {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 56px 20px;
  color: var(--fg-muted);
  text-align: center;
  border: none;
  background: transparent;
}
.stream-hint p { margin: 0; max-width: 48ch; line-height: 1.8; }
.stream-hint .row { justify-content: center; }
.turn {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bubble {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 0;
  border-radius: 0;
}
/* 用户靠右 + 主色气泡；助手靠左 + 无边框正文。 */
.bubble.user {
  align-self: flex-end;
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 16px 16px 4px 16px;
  background: var(--accent);
  color: var(--accent-fg);
  white-space: pre-wrap;
  box-shadow: none;
}
.bubble.assistant {
  align-self: stretch;
  padding: 0 4px;
  border: none;
  background: transparent;
}
.msg-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.msg-avatar {
  display: grid;
  place-items: center;
  flex: none;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--accent);
  color: var(--accent-fg);
  font-size: 10px;
  font-weight: 700;
}
.msg-name {
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  font-weight: 700;
}
.bubble-meta { margin-left: auto; gap: 8px; margin-bottom: 0; }
.loading-line {
  color: var(--fg-faint);
  font-size: var(--fs-sm);
}
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
/* .link-btn 基础观感已提到全局 style/base.css，这里只保留本组件需要的对齐修正
   （本组件的 link-btn 都挂在 flex 列里，需要顶对齐）。 */
.link-btn {
  align-self: flex-start;
}
.btn.sm {
  font-size: var(--fs-xs);
  padding: 5px 10px;
}
.ev-list {
  display: flex;
  flex-direction: column;
  gap: var(--gap-md);
}
.ev {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  /* 证据卡是内嵌块，统一走下沉底色（与 AssistantIssueList.vue 的 .ev 一致） */
  background: var(--bg-sunken);
}
/* 长文本默认限高可展开：既不撑爆面板，也不把内容憋在小框里。 */
.snippet {
  max-height: 7.5em;
  overflow: hidden;
}

/* 工具调用轨迹：参数原文与回落说明都走弱化色，避免抢正文的视觉层级。 */
.tool-args {
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  word-break: break-all;
}
.tool-mode-hint {
  color: var(--fg-faint);
  font-size: var(--fs-xs);
}
.snippet.expanded {
  max-height: none;
}

/* ---- 待确认卡片 ---- */
.cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-top: 4px;
  border-top: 1px solid var(--border-strong);
}
.cards-hint {
  color: var(--fg-faint);
  font-size: var(--fs-xs);
}
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

/* ---- 输入区：底部常驻、圆角输入框、发送靠右 ---- */
.composer {
  position: sticky;
  bottom: 0;
  z-index: 2;
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 12px 0 16px;
  border-top: 1px solid var(--border);
  background: var(--bg);
}
.composer .input {
  min-height: 76px;
  max-height: 200px;
  padding: 12px 14px;
  border-color: var(--border-strong);
  border-radius: 14px;
  background: var(--bg-elev);
  line-height: 1.7;
  resize: vertical;
}
.composer .input:focus {
  outline: 2px solid var(--accent-line);
  outline-offset: 0;
  border-color: var(--accent);
}
.composer .input::placeholder { color: var(--fg-faint); }
.composer .btn.primary {
  border-radius: 999px;
  padding: 8px 20px;
}

/* ---- 附件草稿 / 清洗预览 ---- */
.file-list { display: flex; flex-direction: column; gap: 6px; }
.file-list.sent { flex-direction: row; flex-wrap: wrap; margin-top: 6px; }
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
.file-list.sent .file-chip { padding: 2px 8px; }
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

/* ---- 创作引导 ---- */
.asst-intro {
  margin: 0;
  max-width: 720px;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--fg-muted);
}
.wizard-bubble {
  max-width: min(720px, 92%);
}
.wz-intro {
  margin: 0 0 var(--gap-sm);
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--fg);
}
/* 引导正文是「对话」：保留段落换行，别压成一坨 */
.wz-prose {
  margin: 0 0 var(--gap-sm);
  font-size: var(--fs-sm);
  line-height: 1.78;
  color: var(--fg);
  white-space: pre-wrap;
  max-width: 62ch;
}
.wz-prose.note {
  color: var(--fg-muted);
}
/* 蒸馏进度：一句话 + 百分比，不占大屏 */
.wz-distill {
  margin: var(--gap-sm) 0;
  padding: 9px 11px;
  border: var(--border-w) solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
.wz-distill.stalled {
  border-left-color: var(--warn, #c98a1b);
}
.wz-distill-head {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.wz-distill-stage {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--fs-sm);
  color: var(--fg);
}
.wz-distill-pct {
  flex: none;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--fg);
}
.wz-distill-bar {
  margin-top: 6px;
  height: 4px;
  border-radius: 2px;
  background: var(--border-strong);
  overflow: hidden;
}
.wz-distill-bar > span {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.4s ease;
}
.wz-distill-note {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
}
/* 完成回执 / 疑似卡住提醒 */
.wz-notice {
  margin: var(--gap-sm) 0;
  padding: 9px 11px;
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  font-size: var(--fs-sm);
}
.wz-notice.distill_stalled {
  border-left: 3px solid var(--warn, #c98a1b);
}
.wz-notice.distill_done {
  border-left: 3px solid var(--ok, #2f8f5b);
}
.wz-notice.distill_failed {
  border-left: 3px solid var(--danger, #c0392b);
}
.wz-notice-next {
  margin: 0 0 var(--gap-sm);
  color: var(--fg);
}

.wz-action {
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
  margin: var(--gap-sm) 0;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border-strong);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
.wz-action-diff {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.wz-action-diff > div {
  display: flex;
  gap: var(--gap-sm);
  flex-wrap: wrap;
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
.wz-cards {
  margin-top: var(--gap-sm);
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
}
.wz-card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wz-card-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
  background: var(--bg-sunken);
}
.wz-card-head {
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
}
.wz-card-head strong {
  font-size: var(--fs-sm);
}
.wz-card-actions {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
}
.wz-card-form {
  margin-top: 8px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}
.wz-card-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.wz-card-actions-row {
  grid-column: 1 / -1;
  align-items: center;
}
@media (max-width: 720px) {
  .wz-card-form {
    grid-template-columns: 1fr;
  }
}
.wz-card-meta {
  margin-top: 4px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  line-height: 1.5;
}
.wz-danger {
  color: var(--danger);
}
.wz-card-confirm {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: var(--gap-sm);
  flex-wrap: wrap;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
/* 确认块的第二行说明：整行铺开，不和按钮挤在一行。 */
.wz-card-confirm-hint {
  flex-basis: 100%;
  line-height: 1.5;
}
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
@media (max-width: 720px) {
  .meta-grid {
    grid-template-columns: 1fr;
  }
  .stream {
    max-height: 70vh;
  }
}

/*
 * 宽屏一屏布局：抬头 + 对话流 + 输入区正好占满视口，只有对话流内部滚动。
 * 高度 = 视口 − 顶栏 60px − .main 上下内边距（默认 30 + 56）。
 * 窄屏（≤880px）不锁定高度：手机上横向导航会额外占高，锁死反而挤掉对话区。
 *
 * 必须放在样式表末尾：这里覆盖的 .stream/.card.assistant/.asst-head h1
 * 等规则定义在上方，同优先级下后写的才生效。
 */
@media (min-width: 881px) {
  .desk {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 146px);
    height: calc(100dvh - 146px);
    min-height: 520px;
  }
  .desk-body {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .desk-body > .page-head {
    flex: none;
    margin-bottom: 12px;
  }
  /* 工序卡按内容高度占位，不参与对话区的伸缩，避免被压扁 */
  .desk-body :deep(.wf-strip) {
    flex: none;
  }
  .card.assistant {
    flex: 1;
    min-height: 0;
    margin-top: 0;
  }
  .stream {
    flex: 1;
    min-height: 0;
    max-height: none;
    padding-bottom: 10px;
  }
  .composer {
    flex: none;
    gap: 6px;
    padding: 10px 0 12px;
  }
  .composer .input {
    min-height: 56px;
    padding: 10px 12px;
  }
  /* 工序卡只留「进度条 + 一句话」，把高度让给对话；摘要单行省略，完整内容在 title 里 */
  .desk-body :deep(.wf-strip:first-of-type) {
    padding: 12px 16px;
    margin-bottom: 10px;
  }
  .desk-body :deep(.wf-strip:first-of-type .wf-progress) {
    gap: 6px;
  }
  .desk-body :deep(.wf-strip:first-of-type .wf-progress-note) {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  /* 抬头收紧一档：省下的高度全给对话流 */
  .asst-head h1 {
    font-size: 26px;
    margin-top: 2px;
  }
  .book-stats {
    margin: 0;
  }
  .asst-meta {
    margin-top: 6px;
  }
  .asst-intro {
    margin-top: 8px;
  }
}
@media (min-width: 881px) and (max-width: 1100px) {
  /* .main 内边距变成 26 + 48 */
  .desk {
    height: calc(100vh - 134px);
    height: calc(100dvh - 134px);
  }
}
/* 常规笔记本高度优先保对话：说明文字让位，减少一屏里的固定占高 */
@media (min-width: 881px) and (max-height: 900px) {
  .asst-intro {
    display: none;
  }
}

/* ---- 角色卡编辑弹窗：居中卡片，保存后自动关闭 ---- */
.card-edit-mask {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.42);
}
.card-edit-modal {
  position: relative;
  width: min(760px, 100%);
  max-height: 86vh;
  overflow: auto;
  padding: 22px 24px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.28);
}
.card-edit-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.card-edit-kicker {
  color: var(--fg-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.card-edit-head h3 {
  margin-top: 4px;
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
}
.card-edit-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.card-edit-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 18px;
}
@media (max-width: 680px) {
  .card-edit-grid {
    grid-template-columns: 1fr;
  }
}
</style>
