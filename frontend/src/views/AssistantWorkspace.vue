<script setup lang="ts">
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
import { computed, nextTick, onMounted, onBeforeUnmount, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api } from "@/api/client";
import { createAttachmentDraft, pasteAttachmentFiles } from "@/utils/assistantAttachments";
import AssistantIssueList from "@/components/AssistantIssueList.vue";
import ForeignBlocksNotice from "@/components/ForeignBlocksNotice.vue";
import WorkflowStrip from "@/components/WorkflowStrip.vue";
import { useLlmConfig } from "@/composables/useLlmConfig";
import { bodyRepairVerificationText, pendingActionKindLabel, pendingActionRiskMeta, reactNotice, sourceLabel } from "@/utils/assistantDisplay";
import { distinctChapterTitles } from "@/utils/corpusReport";
import { username } from "@/utils/auth";
import type {
  AssistantChoiceOption,
  AssistantEvidence,
  AssistantIssue,
  AssistantPendingAction,
  AssistantReactBlock,
  AssistantRun,
  AssistantRunDetailResponse,
  AssistantVersion,
  AssistantVersionDiffResponse,
  ForeignBlocksReport,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const router = useRouter();
const projectId = computed(() => Number(route.params.id));
const badId = computed(() => !Number.isInteger(projectId.value) || projectId.value <= 0);

// 页头：项目名 + 三个模型的生效徽标（一眼看出生成 / 助手 / 蒸馏是三件事）
const project = ref<ProjectOut | null>(null);
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
  /**
   * 可编辑草稿：agent 起草、用户改或原样确认。
   *
   * 提案的「改后内容」必须由用户定 —— 服务端替用户写正文就是越权。
   * 所以这里存的是**用户正在编辑的那一版**，确认时回传；
   * 落库的是用户确认时看到的那一版，不是 agent 的原版。
   */
  draft: string;
  /** 草稿对应请求体里的哪个字段（服务端下发；空 = 这张卡没有草稿）。 */
  draftField: string;
  /**
   * 「剔除混进来的别的书」的勾选状态（投放语料时才有）。
   *
   * 默认**不勾**：检测照做（结论展示在卡上），删不删由人点。
   * 删的是整章正文，判据万一错了丢的是真内容。
   */
  dropForeignBlocks: boolean;
}

interface Turn {
  /** 消息唯一 key。 */
  key: string;
  role: "user" | "assistant";
  text: string;
  attachments: { key: number; filename: string }[];
  delivery?: "sending" | "sent" | "failed";
  /** assistant 侧：本次调用的出参（user 消息为 null）。 */
  answer: string;
  evidence: AssistantEvidence[];
  issues: AssistantIssue[];
  rulesEvaluated: string[];
  confidence: number;
  agentRunId: number;
  cards: ActionCard[];
  /** 本轮 ReAct 的执行摘要（降级时界面据此给标记）；旧后端不给 → null。 */
  react: AssistantReactBlock | null;
  error: string | null;
  loading: boolean;
}

// ==================================================================
// 会话状态
// ==================================================================

const turns = ref<Turn[]>([]);
const query = ref("");
/** 请求进行中禁止重复提交；卡片执行中的按钮单独由各自 state 控制。 */
const sending = ref(false);
const streamEl = ref<HTMLElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const draft = reactive(createAttachmentDraft(api.uploadAssistantAttachment, textOf));
const canSend = computed(() => !sending.value && !badId.value && draft.request(query.value) !== null);
const dragging = ref(false);
let scopeEpoch = 0;

function addFiles(files: File[]): void {
  if (sending.value || badId.value) return;
  void draft.add(files);
}
function onFilesSelected(event: Event): void {
  const input = event.target as HTMLInputElement;
  addFiles(Array.from(input.files ?? []));
  input.value = "";
}
function onPaste(event: ClipboardEvent): void {
  const target = event.target as HTMLTextAreaElement;
  if (sending.value || badId.value) {
    if (event.clipboardData?.files.length) event.preventDefault();
    return;
  }
  const result = pasteAttachmentFiles(event, query.value, target.selectionStart, target.selectionEnd);
  if (!result) return;
  query.value = result.text;
  addFiles(result.files);
  void nextTick(() => target.setSelectionRange(result.caret, result.caret));
}
function onDragOver(event: DragEvent): void {
  if (!event.dataTransfer?.types.includes("Files")) return;
  event.preventDefault();
  dragging.value = !sending.value && !badId.value;
}
function onDrop(event: DragEvent): void {
  dragging.value = false;
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault();
  addFiles(Array.from(event.dataTransfer.files));
}
// 只读历史：运行记录与内容版本共用一个面板，不提供恢复操作。
// 状态必须先于 immediate watch 初始化，避免切换项目时访问尚未声明的 ref。
const historyOpen = ref(false);
const historyTab = ref<"runs" | "versions">("runs");
const runHistory = ref<AssistantRun[]>([]);
const runHistoryLoading = ref(false);
const runHistoryError = ref<string | null>(null);
const runDetail = ref<AssistantRunDetailResponse | null>(null);
const runDetailLoading = ref(false);
const runDetailError = ref<string | null>(null);
const selectedRunId = ref<number | null>(null);
const versions = ref<AssistantVersion[]>([]);
const versionsLoading = ref(false);
const versionsError = ref<string | null>(null);
const versionFrom = ref<number | null>(null);
const versionTo = ref<number | null>(null);
const versionDiff = ref<AssistantVersionDiffResponse | null>(null);
const versionDiffLoading = ref(false);
const versionDiffError = ref<string | null>(null);
const restorePreviewOpen = ref(false);
const restoreAcknowledged = ref(false);
const restoreBusy = ref(false);
const restoreError = ref<string | null>(null);
const restoreNotice = ref<string | null>(null);
const restoreTarget = computed(() => versions.value.find((item) => item.id === versionFrom.value) ?? null);
const restoreCurrent = computed(() => versions.value.find((item) => item.id === versionTo.value) ?? null);
const restoreDiffStats = computed(() => {
  let added = 0;
  let removed = 0;
  for (const line of (versionDiff.value?.diff || "").split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) added++;
    if (line.startsWith("-") && !line.startsWith("---")) removed++;
  }
  return { added, removed };
});

watch(projectId, (id) => {
  scopeEpoch++;
  resetConversation();
  project.value = null;
  dragging.value = false;
  historyOpen.value = false;
  runHistory.value = [];
  runDetail.value = null;
  selectedRunId.value = null;
  versions.value = [];
  versionDiff.value = null;
  restorePreviewOpen.value = false;
  restoreNotice.value = null;
  restoreError.value = null;
  if (!Number.isInteger(id) || id <= 0) return;
  const epoch = scopeEpoch;
  void api.getProject(id).then((value) => {
    if (scopeEpoch === epoch) project.value = value;
  }).catch(() => { /* Project title is optional. */ });
}, { immediate: true, flush: "sync" });
onBeforeUnmount(() => { scopeEpoch++; draft.reset(0); });

// ==================================================================
// 新建对话 / 换语料
//
// 助手工作台以前被**路由里的项目 id**绑死：想换一本语料只能自己改地址，
// 而且没有「重新开一段对话」这个动作。语料换了，之前那段对话的前提
// （这本书的档案、进度）就全错了 —— 所以换语料必须**同时**开新对话。
// ==================================================================

/** 可切换的语料（项目）列表。 */
const projects = ref<ProjectOut[]>([]);
const projectsLoading = ref(false);
const projectsError = ref<string | null>(null);
/** 「换语料」面板是否展开。 */
const showCorpusPicker = ref(false);

/** 清空当前对话（同一本语料，重新开一段）。 */
function resetConversation(): void {
  draft.reset(projectId.value);
  query.value = "";
  turns.value = [];
  sending.value = false;
  dragging.value = false;
}

/** 新建对话：同一本语料，重新开一段。 */
function startNewConversation(): void {
  scopeEpoch++;
  resetConversation();
  void scrollToBottom();
}

/** 换一本语料 = 切到另一个项目的助手页（路由一变，对话自动重开）。 */
async function switchCorpus(id: number): Promise<void> {
  showCorpusPicker.value = false;
  if (id === projectId.value) {
    startNewConversation();
    return;
  }
  await router.push(`/projects/${id}/assistant`);
}

/** 一个项目的语料状况（人话）。 */
function corpusHint(p: ProjectOut): string {
  const refs = p.reference_novel_ids?.length ?? 0;
  const corpus = refs > 0 ? `${refs} 本参考语料` : "没有参考语料";
  const written = p.segment_count ? `已写 ${p.segment_count} 段` : "还没写正文";
  return `${corpus} · ${written}`;
}

async function loadProjects(): Promise<void> {
  projectsLoading.value = true;
  projectsError.value = null;
  try {
    projects.value = await api.listProjects();
  } catch (err) {
    projects.value = [];
    projectsError.value = err instanceof Error ? err.message : String(err);
  } finally {
    projectsLoading.value = false;
  }
}

function formatHistoryTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

function formatRunDuration(value: number | null): string {
  if (value === null) return "进行中";
  if (value < 1000) return `${value} ms`;
  return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)} s`;
}

async function loadRunHistory(): Promise<void> {
  runHistoryLoading.value = true;
  runHistoryError.value = null;
  try {
    const response = await api.assistantRuns(projectId.value, null, 50);
    runHistory.value = response.runs;
  } catch (err) {
    runHistory.value = [];
    runHistoryError.value = err instanceof Error ? err.message : String(err);
  } finally {
    runHistoryLoading.value = false;
  }
}

async function openRunDetail(id: number): Promise<void> {
  selectedRunId.value = id;
  runDetailLoading.value = true;
  runDetailError.value = null;
  try {
    runDetail.value = await api.assistantRunDetail(id, projectId.value);
  } catch (err) {
    runDetail.value = null;
    runDetailError.value = err instanceof Error ? err.message : String(err);
  } finally {
    runDetailLoading.value = false;
  }
}

async function loadVersions(): Promise<void> {
  versionsLoading.value = true;
  versionsError.value = null;
  versionDiffError.value = null;
  try {
    const response = await api.listVersions(projectId.value, null, 100);
    versions.value = response.versions;
    versionFrom.value = response.versions[0]?.id ?? null;
    versionTo.value = response.versions[response.versions.length - 1]?.id ?? null;
    versionDiff.value = null;
  } catch (err) {
    versions.value = [];
    versionFrom.value = null;
    versionTo.value = null;
    versionsError.value = err instanceof Error ? err.message : String(err);
  } finally {
    versionsLoading.value = false;
  }
}

async function compareVersions(): Promise<void> {
  if (!versionFrom.value || !versionTo.value) return;
  versionDiffLoading.value = true;
  versionDiffError.value = null;
  try {
    versionDiff.value = await api.diffVersions(versionFrom.value, versionTo.value);
  } catch (err) {
    versionDiff.value = null;
    versionDiffError.value = err instanceof Error ? err.message : String(err);
  } finally {
    versionDiffLoading.value = false;
  }
}

function openRestorePreview(): void {
  restoreError.value = null;
  restoreNotice.value = null;
  restoreAcknowledged.value = false;
  const target = restoreTarget.value;
  const current = restoreCurrent.value;
  if (!versionDiff.value || !target || !current || target.id === current.id) {
    restoreError.value = "请先比较两个不同版本，再预览恢复影响。";
    return;
  }
  if (target.target_kind !== current.target_kind || target.target_id !== current.target_id) {
    restoreError.value = "只能恢复同一内容对象的版本，请重新选择。";
    return;
  }
  if (!current.after_hash) {
    restoreError.value = "当前版本缺少内容哈希，无法建立漂移保护基线。";
    return;
  }
  restorePreviewOpen.value = true;
}

function closeRestorePreview(): void {
  if (restoreBusy.value) return;
  restorePreviewOpen.value = false;
  restoreAcknowledged.value = false;
  restoreError.value = null;
}

function restoreErrorText(err: unknown): string {
  if (err instanceof ApiError && err.detail && typeof err.detail === "object") {
    const detail = err.detail as { error?: string; expected_hash?: string; current_hash?: string; message?: string };
    if (detail.error === "stale") {
      return `当前内容已偏离预览基线（预期 ${String(detail.expected_hash || "").slice(0, 12)}…，实际 ${String(detail.current_hash || "").slice(0, 12)}…），未执行恢复。请刷新版本后重新预览。`;
    }
    if (detail.message) return detail.message;
  }
  return err instanceof Error ? err.message : String(err);
}

async function runRestoreVersion(): Promise<void> {
  const target = restoreTarget.value;
  const current = restoreCurrent.value;
  if (!target || !current?.after_hash || !restoreAcknowledged.value) return;
  restoreBusy.value = true;
  restoreError.value = null;
  try {
    const result = await api.restoreVersion(target.id, {
      action: "restore",
      actor: username.value || null,
      note: `从版本 #${target.id} 恢复，预览基线为版本 #${current.id}`,
      expected_hash: current.after_hash,
    });
    restorePreviewOpen.value = false;
    restoreNotice.value = `已从版本 #${target.id} 恢复，并生成新版本 #${result.version_id}；历史记录未删除。`;
    await loadVersions();
  } catch (err) {
    restoreError.value = restoreErrorText(err);
  } finally {
    restoreBusy.value = false;
  }
}

function selectHistoryTab(tab: "runs" | "versions"): void {
  historyTab.value = tab;
  if (tab === "runs") void loadRunHistory();
  else void loadVersions();
}

function toggleHistory(): void {
  historyOpen.value = !historyOpen.value;
  if (historyOpen.value) void selectHistoryTab(historyTab.value);
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
    attachments: [],
    answer: "",
    evidence: [],
    issues: [],
    rulesEvaluated: [],
    confidence: 0,
    agentRunId: 0,
    cards: [],
    react: null,
    error: null,
    loading: false,
  };
}

function makeAssistantTurn(): Turn {
  return {
    key: nextKey("a"),
    role: "assistant",
    text: "",
    attachments: [],
    answer: "",
    evidence: [],
    issues: [],
    rulesEvaluated: [],
    confidence: 0,
    agentRunId: 0,
    cards: [],
    react: null,
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
// 问答
// ==================================================================

async function send(): Promise<void> {
  if (!canSend.value) return;
  const body = draft.request(query.value);
  if (!body) return;
  const epoch = scopeEpoch;
  const q = body.query;
  const userTurn = makeUserTurn(q);
  userTurn.attachments = draft.items.map((item) => ({ key: item.key, filename: item.metadata!.filename }));
  userTurn.delivery = "sending";

  // 先把 user 气泡插进去，assistant 占位气泡同时置 loading：
  // 否则「提问 → 等待 → 出结果」之间界面是空的，用户会以为没点上。
  turns.value.push(userTurn);
  const turn = makeAssistantTurn();
  turns.value.push(turn);
  query.value = "";
  sending.value = true;
  await scrollToBottom();
  if (epoch !== scopeEpoch) return;

  try {
    const res = await api.assistantQa(body);
    if (epoch !== scopeEpoch) return;
    userTurn.delivery = "sent";
    draft.items = [];
    turn.answer = res.answer || "";
    turn.evidence = res.evidence ?? [];
    turn.confidence = res.confidence;
    turn.agentRunId = res.agent_run_id;
    // agent 自主调的诊断 / 日志 / 审核结果：新后端会给 issues + rules_evaluated，
    // 旧后端不给 → 空数组 → 整段不渲染（不留空态）。
    turn.issues = res.issues ?? res.findings ?? [];
    turn.rulesEvaluated = res.rules_evaluated ?? [];
    // 本轮 ReAct 的降级标记：旧后端不给 → null → 界面不显示任何标记。
    turn.react = res.react ?? null;
    turn.cards = (res.pending_actions ?? []).map((action) => ({
      key: nextKey("card"),
      action,
      state: "pending" as CardState,
      rows: detailRows(action),
      notice: "",
      result: null,
      draft: draftOf(action),
      draftField: draftFieldOf(action),
      dropForeignBlocks: false,
    }));
    turn.loading = false;
  } catch (err) {
    if (epoch !== scopeEpoch) return;
    userTurn.delivery = "failed";
    if (!query.value) query.value = q;
    turn.error = `问答失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
    turn.loading = false;
  } finally {
    if (epoch !== scopeEpoch) return;
    sending.value = false;
    await scrollToBottom();
  }
}

/** Enter 发送、Shift+Enter 换行（textarea 默认 Enter 是换行，这里反转）。 */
function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    void send();
  }
}

// ==================================================================
// 选择框卡片（kind=choice）
//
// 工序链走到需要**用户决断**的岔路口时给的不是「确认 / 取消」，
// 而是**选项**（蒸馏用哪一档、一次生成几章…）：用户点一个即执行。
//
// 纪律与其它卡片一致：**前端不构造任何请求字段** —— body 来自服务端，
// 选项对应的 body_patch 也来自服务端（选项与它的后果是一一对应的）。
// ==================================================================

type ChoiceOption = AssistantChoiceOption;

function isChoiceCard(card: ActionCard): boolean {
  return card.action.kind === "choice";
}

/**
 * 这张卡是不是「**选项即动作**」的那种（不额外给「确认执行」）。
 *
 * 两种：纯选择框（kind='choice'），与**带选项的动作提案** ——
 * 后者把「需要你决断的参数」（蒸馏用哪一档）并进同一张卡，
 * 免得弹两张卡让人自己对应顺序（顺序一错就批错动作）。
 */
function isOptionCard(card: ActionCard): boolean {
  return isChoiceCard(card) || choiceOptions(card).length > 0;
}

function choiceOptions(card: ActionCard): ChoiceOption[] {
  const detail = card.action.detail as Record<string, unknown> | undefined;
  const options = detail?.options;
  return Array.isArray(options) ? (options as ChoiceOption[]) : [];
}

function choiceQuestion(card: ActionCard): string {
  const detail = card.action.detail as Record<string, unknown> | undefined;
  return typeof detail?.question === "string" ? detail.question : "";
}

async function chooseOption(card: ActionCard, option: ChoiceOption): Promise<void> {
  if (card.state !== "pending" && card.state !== "failed") return;
  card.state = "running";
  card.notice = "";
  card.result = null;
  const body = { ...(card.action.body ?? {}), ...(option.body_patch ?? {}) };
  try {
    const res = await api.assistantRunPendingAction(
      card.action.endpoint,
      card.action.method,
      body,
    );
    card.state = "confirmed";
    card.result = res;
    card.notice = `已选择「${option.label}」`;
  } catch (err) {
    card.state = "failed";
    card.result = null;
    const detail = detailOf(err);
    card.notice = typeof detail === "string" ? detail : String(statusOf(err) ?? "执行失败");
  }
}

// ==================================================================
// 待确认卡片：确认 / 取消
// ==================================================================

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
  // 有草稿的卡：回传**用户改过的那一版**（协议字段仍由服务端下发，
  // 前端只覆盖用户输入的那一个字段）。
  let body = card.draftField
    ? { ...(card.action.body ?? {}), [card.draftField]: card.draft }
    : card.action.body;
  // 「剔除混进来的别的书」是**用户在卡上勾的**，所以这一位由前端覆盖。
  // 它不是「前端拼字段」—— 字段名与取值域都由服务端在 body 里给好了，
  // 这里只把用户的勾选状态写进去（不勾时保持服务端给的 false）。
  if (foreignBlocksOf(card) && card.dropForeignBlocks) {
    body = { ...(body ?? {}), drop_foreign_blocks: true };
  }
  try {
    const res = await api.assistantRunPendingAction(
      card.action.endpoint,
      card.action.method,
      body,
    );
    card.state = "confirmed";
    card.result = res;
    card.notice = describeResult(card.action, res);
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

/**
 * 这张卡有没有**可编辑草稿**，以及草稿在请求体的哪个字段。
 *
 * 服务端下发（`detail.draft_field`）—— 前端不自己猜「哪个字段是草稿」。
 */
function draftFieldOf(action: AssistantPendingAction): string {
  const detail = action.detail as Record<string, unknown> | undefined;
  if (!detail || detail.draft_editable !== true) return "";
  const field = detail.draft_field;
  return typeof field === "string" && field ? field : "";
}

/** 草稿的初始值 = 服务端给的草稿（用户可以改）。 */
function draftOf(action: AssistantPendingAction): string {
  const field = draftFieldOf(action);
  if (!field) return "";
  const body = action.body as Record<string, unknown> | undefined;
  const value = body?.[field];
  return typeof value === "string" ? value : "";
}

function riskMetaOf(card: ActionCard): { label: string; tag: string; hint: string } {
  return pendingActionRiskMeta(card.action);
}

// ==================================================================
// 「混进了别的书」的体检结论（投放语料时）
//
// 盗版站的「单本下载」常把多本书拼成一份 txt。这类 txt 进了库，
// 文体档案是混合体的统计、风格样例可能取到外书的段落 ——
// 所以投放前要能看见「里面混了什么」，并由人决定剔不剔。
//
// 判定口径（服务端给，前端不自己判）：
//   人物聚集度 + 排他性 —— 集中出现、且别处见不到的人物名。
// ==================================================================

/** 这张卡带的混书检出结论；没检出（或不是投放卡）→ null。 */
function foreignBlocksOf(card: ActionCard): ForeignBlocksReport | null {
  const detail = card.action.detail as Record<string, unknown> | undefined;
  const raw = detail?.foreign_blocks;
  if (!raw || typeof raw !== "object") return null;
  const report = raw as ForeignBlocksReport;
  return report.detected ? report : null;
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
  if (action.kind === "body_repair") {
    parts.push(bodyRepairVerificationText(res));
    if (typeof res.question === "string") parts.push(res.question);
  }
  if (action.kind === "ingest_attachment" && typeof res.novel_id === "number") {
    parts.push(`参考语料 #${res.novel_id}`);
    if (typeof res.chapters === "number") parts.push(`${res.chapters} 章`);
    if (typeof res.chunks === "number") parts.push(`${res.chunks} 个文本块`);
    if (typeof res.check_status === "string") parts.push(`复核状态 ${res.check_status}`);
  }
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
    // 选择框的题目与选项由专门的一块渲染（可点），别再在这里摊成表格。
    if (action.kind === "choice" && (key === "options" || key === "question")) continue;
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

/** 这一轮的降级标记（没有降级 → null → 不显示任何东西）。 */
function noticeOf(turn: Turn) {
  return reactNotice(turn.react);
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

const hasHistory = computed(() => turns.value.length > 0);

onMounted(async () => {
  await loadLlm();
  await loadProjects();
});
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">05 · WRITING ASSISTANT</div>
    <div class="asst-head">
      <h1>助手工作台</h1>
      <div class="row tight">
        <button class="btn" type="button" @click="toggleHistory">
          {{ historyOpen ? "收起历史" : "历史记录" }}
        </button>
        <button
          class="btn"
          type="button"
          :disabled="sending"
          title="清空当前这段对话，重新开一段（同一本语料）"
          @click="startNewConversation"
        >
          新建对话
        </button>
        <button
          class="btn"
          type="button"
          :disabled="sending"
          title="换一本语料（会同时开一段新的对话）"
          @click="showCorpusPicker = !showCorpusPicker"
        >
          换语料
        </button>
        <button class="link-btn back-link" @click="router.push(`/projects/${projectId}`)">
          ← 返回项目工作台
        </button>
      </div>
    </div>

    <!-- 换语料：换一本就是换一个项目；换了必须开新对话（旧对话的前提是上一本） -->
    <div v-if="showCorpusPicker" class="corpus-picker">
      <div class="faint" style="font-size: var(--fs-xs)">
        换一本语料（会同时开一段新的对话）：
      </div>
      <div v-if="projectsLoading" class="muted">正在读取项目…</div>
      <div v-else-if="projectsError" class="notice err" role="alert">
        项目列表加载失败：{{ projectsError }}<button class="link-btn" type="button" @click="loadProjects">重试</button>
      </div>
      <div v-else-if="!projects.length" class="muted">还没有任何项目。</div>
      <button
        v-for="p in projects"
        :key="p.id"
        type="button"
        class="corpus-item"
        :class="{ current: p.id === projectId }"
        @click="switchCorpus(p.id)"
      >
        <span class="corpus-title">《{{ p.title }}》</span>
        <span class="faint">{{ corpusHint(p) }}</span>
        <span v-if="p.id === projectId" class="tag">当前</span>
      </button>
    </div>

    <section v-if="historyOpen" class="history-panel" aria-labelledby="history-title">
      <div class="history-head">
        <div><span class="history-kicker">READ ONLY</span><h2 id="history-title">历史记录</h2></div>
        <div class="history-tabs" role="tablist" aria-label="历史类型">
          <button type="button" :class="{ active: historyTab === 'runs' }" role="tab" :aria-selected="historyTab === 'runs'" @click="selectHistoryTab('runs')">运行记录</button>
          <button type="button" :class="{ active: historyTab === 'versions' }" role="tab" :aria-selected="historyTab === 'versions'" @click="selectHistoryTab('versions')">内容版本</button>
        </div>
      </div>

      <div v-if="historyTab === 'runs'" class="run-history-layout">
        <div class="run-list">
          <div v-if="runHistoryLoading" class="empty"><span class="spinner" /> 正在读取运行记录…</div>
          <div v-else-if="runHistoryError" class="notice err" role="alert">
            运行记录加载失败：{{ runHistoryError }}<button class="link-btn" type="button" @click="loadRunHistory">重试</button>
          </div>
          <div v-else-if="!runHistory.length" class="empty">当前项目还没有助手运行记录。</div>
          <button
            v-for="run in runHistory"
            v-else
            :key="run.id"
            class="run-row"
            :class="{ active: selectedRunId === run.id }"
            type="button"
            @click="openRunDetail(run.id)"
          >
            <span class="run-row-top"><strong>#{{ run.id }}</strong><span class="tag" :class="run.status === 'success' ? 'ok' : run.status === 'failed' ? 'err' : 'info'">{{ run.status }}</span></span>
            <span class="run-model mono">{{ run.model || "未记录模型" }}</span>
            <span class="run-meta">{{ formatRunDuration(run.duration_ms) }} · 证据 {{ run.evidence_count }} 条</span>
            <span class="run-time">{{ formatHistoryTime(run.started_at) }}</span>
          </button>
        </div>
        <div class="run-detail">
          <div v-if="runDetailLoading" class="empty"><span class="spinner" /> 正在读取运行详情…</div>
          <div v-else-if="runDetailError" class="notice err" role="alert">
            详情加载失败：{{ runDetailError }}<button v-if="selectedRunId" class="link-btn" type="button" @click="openRunDetail(selectedRunId)">重试</button>
          </div>
          <template v-else-if="runDetail">
            <div class="detail-title"><strong>AgentRun #{{ runDetail.run.id }}</strong><span>{{ runDetail.run.note || "无备注" }}</span></div>
            <dl class="run-facts">
              <div><dt>状态</dt><dd>{{ runDetail.run.status }}</dd></div><div><dt>耗时</dt><dd>{{ formatRunDuration(runDetail.run.duration_ms) }}</dd></div>
              <div><dt>模型</dt><dd class="mono">{{ runDetail.run.model || "—" }}</dd></div><div><dt>证据</dt><dd>{{ runDetail.evidence.length }} 条</dd></div>
            </dl>
            <div v-if="!runDetail.evidence.length" class="empty">这次运行没有留存证据。</div>
            <div v-else class="history-evidence-list">
              <article v-for="(evidence, index) in runDetail.evidence" :key="`${evidence.ref_id}-${index}`" class="history-evidence">
                <div><span class="tag accent">{{ sourceLabel(evidence.source) }}</span><span class="mono">{{ evidence.kind }} · score {{ Number(evidence.score).toFixed(3) }}</span></div>
                <p class="prose">{{ evidence.snippet }}</p>
              </article>
            </div>
          </template>
          <div v-else class="history-placeholder">选择左侧一条运行记录，查看模型、耗时与证据。</div>
        </div>
      </div>

      <div v-else class="version-history">
        <div v-if="versionsLoading" class="empty"><span class="spinner" /> 正在读取内容版本…</div>
        <div v-else-if="versionsError" class="notice err" role="alert">
          内容版本加载失败：{{ versionsError }}<button class="link-btn" type="button" @click="loadVersions">重试</button>
        </div>
        <div v-else-if="!versions.length" class="empty">当前项目还没有内容版本。</div>
        <template v-else>
          <div class="version-compare">
            <div class="field"><label for="version-from">旧版本</label><select id="version-from" class="input" v-model.number="versionFrom"><option v-for="version in versions" :key="version.id" :value="version.id">#{{ version.id }} · {{ version.kind }}</option></select></div>
            <div class="field"><label for="version-to">新版本</label><select id="version-to" class="input" v-model.number="versionTo"><option v-for="version in versions" :key="version.id" :value="version.id">#{{ version.id }} · {{ version.kind }}</option></select></div>
            <button class="btn primary" type="button" :disabled="versionDiffLoading || !versionFrom || !versionTo" @click="compareVersions">{{ versionDiffLoading ? "比较中…" : "比较所选版本" }}</button>
          </div>
          <div v-if="versionDiffError" class="notice err" role="alert">版本比较失败：{{ versionDiffError }}<button class="link-btn" type="button" @click="compareVersions">重试</button></div>
          <div v-if="versionDiff" class="version-diff">
            <div class="detail-title"><strong>#{{ versionDiff.from_version.id }} → #{{ versionDiff.to_version.id }}</strong><button class="btn sm" type="button" @click="openRestorePreview">预览恢复影响</button></div>
            <pre>{{ versionDiff.diff || "两个版本内容一致。" }}</pre>
          </div>
          <div v-if="restoreNotice" class="notice ok" role="status">{{ restoreNotice }}</div>
          <div v-if="restoreError && !restorePreviewOpen" class="notice err" role="alert">{{ restoreError }}</div>
          <div class="version-list">
            <div v-for="version in versions" :key="version.id" class="version-row">
              <span class="mono">#{{ version.id }}</span><span class="tag">{{ version.kind }}</span>
              <span class="version-reason">{{ version.reason || "未记录原因" }}</span>
              <span class="faint">{{ formatHistoryTime(version.created_at) }}</span>
            </div>
          </div>
        </template>
      </div>
    </section>

    <div v-if="restorePreviewOpen && restoreTarget && restoreCurrent" class="restore-mask" @click.self="closeRestorePreview">
      <section class="restore-dialog" role="dialog" aria-modal="true" aria-labelledby="restore-title">
        <div class="restore-head">
          <div><span class="restore-kicker">RESTORE REVIEW</span><h2 id="restore-title">恢复版本 #{{ restoreTarget.id }} 的影响</h2></div>
          <button class="btn ghost" type="button" :disabled="restoreBusy" @click="closeRestorePreview">取消</button>
        </div>
        <div class="restore-impact">
          <div><span>将被覆盖</span><strong>版本 #{{ restoreCurrent.id }}</strong><small>{{ restoreCurrent.target_kind }} / {{ restoreCurrent.target_id }}</small></div>
          <div><span>恢复为</span><strong>版本 #{{ restoreTarget.id }}</strong><small>{{ restoreTarget.target_kind }} / {{ restoreTarget.target_id }}</small></div>
          <div><span>差异摘要</span><strong>+{{ restoreDiffStats.added }} / −{{ restoreDiffStats.removed }} 行</strong><small>与上方 diff 面板一致</small></div>
          <div><span>漂移基线</span><strong class="mono">{{ restoreCurrent.after_hash?.slice(0, 12) }}…</strong><small>执行时由后端重新核对</small></div>
        </div>
        <p class="restore-warning">恢复会覆盖上述对象的当前内容，但不会删除历史版本；成功后将追加一条恢复版本。若当前内容已偏离此基线，后端会拒绝执行。</p>
        <label class="restore-confirm">
          <input v-model="restoreAcknowledged" type="checkbox" :disabled="restoreBusy" />
          <span>我已核对覆盖对象与差异，确认继续恢复。</span>
        </label>
        <div v-if="restoreError" class="notice err" role="alert">{{ restoreError }}</div>
        <div class="restore-actions">
          <button class="btn" type="button" :disabled="restoreBusy" @click="closeRestorePreview">返回 diff</button>
          <button class="btn primary" type="button" :disabled="restoreBusy || !restoreAcknowledged" @click="runRestoreVersion">
            {{ restoreBusy ? "正在核对并恢复…" : "二次确认并恢复" }}
          </button>
        </div>
      </section>
    </div>

    <div v-if="badId" class="notice err">路由缺少有效的项目 id（/projects/:id/assistant）。</div>

    <!-- 工序链进度：助理 agent 编排的全流程走到哪一步、卡在哪儿 -->
    <WorkflowStrip v-if="!badId" :project-id="projectId" />

    <div class="asst-meta">
      <span class="tag accent asst-project">
        {{ project ? `《${project.title}》` : "项目 #" + projectId }}
      </span>
      <!-- 三个模型徽标：生成 / 助手 / 蒸馏分开显示，一眼看出是三回事 -->
      <span
        class="tag model-badge"
        :class="writerCfg?.is_fallback ? 'warn' : 'info'"
        :title="writerCfg ? `生成模型（Writer）：${writerCfg.label} · ${writerCfg.model}` : '生成模型配置未取到'"
      >
        生成模型 {{ writerCfg?.is_fallback ? "本地兜底" : writerCfg?.label }} ·
        <span class="mono">{{ writerCfg?.model ?? "—" }}</span>
      </span>
      <span
        class="tag model-badge"
        :class="assistantCfg?.is_fallback ? 'warn' : 'info'"
        :title="assistantCfg ? `助手模型（Assistant）：${assistantCfg.label} · ${assistantCfg.model}` : '助手模型配置未取到'"
      >
        助手模型 {{ assistantCfg?.is_fallback ? "本地兜底" : assistantCfg?.label }} ·
        <span class="mono">{{ assistantCfg?.model ?? "—" }}</span>
      </span>
      <span
        class="tag model-badge"
        :class="distillerCfg?.is_fallback ? 'warn' : 'info'"
        :title="distillerCfg ? `蒸馏模型（Distiller，文风档案抽取用）：${distillerCfg.label} · ${distillerCfg.model}` : '蒸馏模型配置未取到'"
      >
        蒸馏模型 {{ distillerCfg?.is_fallback ? "本地兜底" : distillerCfg?.label }} ·
        <span class="mono">{{ distillerCfg?.model ?? "—" }}</span>
      </span>
      <!-- 模型设置只有一个入口：跳路由页 /settings（弹窗已退役） -->
      <button class="btn sm" @click="openModelSettings">模型设置…</button>
      <span v-if="!rolesReady" class="faint" style="font-size: var(--fs-xs)">
        后端未返回全部分角色配置，徽标当前显示的是同一份单模型生效值
      </span>
    </div>

    <p>在一个对话里查正文、语料与图谱。诊断和审核结果随回答展示；所有写操作都必须经你确认。</p>
  </header>

  <section class="card assistant">
    <!-- ==================== 对话流 ==================== -->
    <div ref="streamEl" class="stream">
      <div v-if="!hasHistory" class="stream-hint">
        <p>
          直接问就行 —— 助手会自己去查正文、参考语料、知识图谱，需要的话也会自己去跑诊断与审核。
        </p>
        <p class="faint">
          提问建议：<span class="mono">第 3 段和第 1 段的动机是不是重复了？</span>
          <span class="mono">主角的名字前后有没有不一致？</span>
        </p>
      </div>

      <div v-for="turn in turns" :key="turn.key" class="turn" :class="turn.role">
        <!-- 用户气泡 -->
        <div v-if="turn.role === 'user'" class="bubble user">
          <div>{{ turn.text }}</div>
          <div v-if="turn.attachments.length" class="file-list">
            <span v-for="file in turn.attachments" :key="file.key" class="file-chip">
              {{ file.filename }} · {{ turn.delivery === "sent" ? "已发送" : turn.delivery === "failed" ? "发送失败，可在下方重试" : "发送中…" }}
            </span>
          </div>
        </div>

        <!-- 助手气泡 -->
        <div v-else class="bubble assistant">
          <div class="row tight" style="align-items: center">
            <span class="tag info mono">AgentRun #{{ turn.agentRunId || "—" }}</span>
            <span v-if="turn.agentRunId" class="tag" :class="confidenceTag(turn.confidence)">
              {{ confidenceLabel(turn.confidence) }} {{ turn.confidence.toFixed(2) }}
            </span>
            <span class="tag mono">证据 {{ turn.evidence.length }} 条</span>
            <span v-if="turn.issues.length" class="tag mono">
              助手自查 {{ turn.issues.length }} 个问题
            </span>
          </div>

          <div v-if="turn.loading" class="loading-line">助手正在查证…</div>

          <div v-if="turn.error" class="notice err">{{ turn.error }}</div>

          <template v-if="!turn.loading">
            <!-- 本轮降级标记：答案无据 / 未能收敛 / 模型侧故障，三种含义不同，
                 不共用文案（把「答案无据」说成「模型坏了」会把人引到错的地方）。 -->
            <div v-if="noticeOf(turn)" class="notice" :class="noticeOf(turn)!.tag">
              <strong>{{ noticeOf(turn)!.label }}</strong>
              <span class="react-notice-detail">{{ noticeOf(turn)!.detail }}</span>
            </div>

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

                <!-- 可编辑草稿：助手起草、用户定稿。
                     确认时回传的是**用户改过的那一版**（协议字段仍由服务端下发）。 -->
                <div v-if="card.draftField" class="draft-box">
                  <div class="faint" style="font-size: var(--fs-xs)">
                    这是助手的草稿，可以直接改；改完点「确认执行」才会存成待批提案。
                  </div>
                  <textarea
                    v-model="card.draft"
                    class="draft-input mono"
                    rows="6"
                    spellcheck="false"
                  />
                </div>

                <!-- 混书体检：投放前告诉用户这份 txt 里混进了别的书，并让人决定剔不剔。
                     勾选框**只在检出时出现**，默认不勾 —— 检测照做，删不删由人点。 -->
                <ForeignBlocksNotice
                  v-if="foreignBlocksOf(card)"
                  :report="foreignBlocksOf(card)!"
                  selectable
                  :model-value="card.dropForeignBlocks"
                  @update:model-value="card.dropForeignBlocks = $event"
                />

                <div v-if="card.rows.length" class="meta-grid">
                  <div v-for="row in card.rows" :key="row.key">
                    <span class="blk-head">{{ row.label }}</span>
                    <div class="mono" style="font-size: var(--fs-xs)">{{ row.value }}</div>
                  </div>
                </div>

                <!-- 待确认时明确告知将调用的端点：用户有权知道按下去会发生什么 -->
                <div
                  v-if="card.state === 'pending' && !isOptionCard(card)"
                  class="faint mono endpoint"
                >
                  将执行 {{ card.action.method }} {{ card.action.endpoint }}（请求体由服务端下发，
                  前端不参与构造）
                </div>

                <div v-if="card.state === 'running'" class="notice info">执行中…</div>

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

                <!-- 选项即动作：选择框，以及带选项的动作提案（不额外给「确认执行」） -->
                <div v-if="isOptionCard(card)" class="choice-box">
                  <div v-if="choiceQuestion(card)" class="card-hint">
                    {{ choiceQuestion(card) }}
                  </div>
                  <div class="row tight choice-options">
                    <button
                      v-for="opt in choiceOptions(card)"
                      :key="opt.id"
                      class="btn"
                      :disabled="card.state !== 'pending' && card.state !== 'failed'"
                      @click="chooseOption(card, opt)"
                    >
                      {{ opt.label }}
                    </button>
                  </div>
                  <div
                    v-for="opt in choiceOptions(card)"
                    :key="`d-${opt.id}`"
                    class="faint"
                    style="font-size: var(--fs-xs)"
                  >
                    {{ opt.description }}
                  </div>
                  <div class="faint mono endpoint">
                    点一个即执行 {{ card.action.method }} {{ card.action.endpoint }}
                    （请求体与选项的对应关系由服务端下发，前端不参与构造）
                  </div>
                </div>

                <div v-else class="row tight">
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
    </div>

    <!-- ==================== 输入 ==================== -->
    <form class="composer" :class="{ dragging }" @submit.prevent="send"
      @dragover="onDragOver" @dragleave.self="dragging = false" @drop="onDrop">
      <div v-if="draft.items.length" class="file-list" aria-live="polite">
        <div v-for="file in draft.items" :key="file.key" class="file-chip" :class="{ failed: file.state === 'failed' }">
          <span class="file-name">{{ file.file.name }}</span>
          <span>{{ file.state === "uploading" ? "上传中…" : file.state === "ready" ? "已就绪" : "上传失败" }}</span>
          <button v-if="file.state === 'failed'" class="link-btn" type="button" :disabled="sending" @click="draft.retry(file.key)">重试</button>
          <button class="link-btn" type="button" :disabled="sending" :aria-label="`移除 ${file.file.name}`" @click="draft.remove(file.key)">移除</button>
          <span v-if="file.error" class="file-error" role="alert">{{ file.error }}</span>
        </div>
      </div>
      <div v-if="dragging" class="faint">松开以添加文本文件</div>
      <textarea
        v-model="query"
        class="input"
        rows="3"
        :disabled="badId"
        placeholder="问助手一个问题，例如：主角第 3 段的动机和第 1 段一致吗？"
        @keydown="onKeydown"
        @paste="onPaste"
      ></textarea>
      <div class="row tight" style="align-items: center">
        <input ref="fileInput" class="file-input" type="file" accept=".txt,.md" multiple :disabled="sending || badId" @change="onFilesSelected" />
        <button class="btn" type="button" :disabled="sending || badId" @click="fileInput?.click()">添加文件</button>
        <button class="btn primary" type="submit" :disabled="!canSend">
          {{ sending ? "思考中…" : "提问" }}
        </button>
        <span class="faint" style="font-size: var(--fs-xs)">
          Enter 发送 · Shift+Enter 换行。支持粘贴或拖入 .txt / .md 文件；上传后只检查，不会自动入库。
        </span>
      </div>
    </form>
  </section>
</template>

<style scoped>
.assistant {
  margin-top: var(--gap);
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  padding: 0;
  overflow: hidden;
  background: var(--bg-elev);
}
.asst-head {
  display: flex;
  align-items: baseline;
  gap: var(--gap);
  flex-wrap: wrap;
}
.asst-head h1 {
  font-family: var(--font-serif);
  font-size: var(--fs-2xl);
  margin-top: 4px;
}

/* 换语料面板：换一本就是换一个项目。 */
.corpus-picker {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0 14px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.corpus-item {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 8px;
  border: var(--border-w) solid transparent;
  border-radius: var(--radius-sm);
  background: none;
  color: inherit;
  text-align: left;
  cursor: pointer;
  font-size: var(--fs-sm);
}
.corpus-item:hover {
  border-color: var(--border-strong);
}
.corpus-item.current {
  border-color: var(--border-strong);
  background: var(--bg-2, var(--bg));
}
.corpus-title {
  font-weight: 600;
}
.back-link {
  font-size: var(--fs-sm);
}
.asst-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}
.asst-project {
  font-size: var(--fs-sm);
}
.model-badge {
  cursor: default;
}

/* 对话流：固定一屏高 + 纵向滚动，滚到底 = 最新一条。 */
.stream {
  display: flex;
  flex-direction: column;
  gap: 18px;
  min-height: 320px;
  max-height: 62vh;
  overflow-y: auto;
  padding: 22px 24px;
  scroll-behavior: smooth;
}
.stream-hint {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: auto 0;
  padding: 34px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 7px;
  color: var(--fg-muted);
  background: var(--bg);
}
.stream-hint p {
  margin: 0;
}
.turn {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bubble {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border-radius: var(--radius-md);
}
/* 用户靠右 + 主色底；助手靠左 + 中性底。位置本身也是一种双编码。 */
.bubble.user {
  align-self: flex-end;
  max-width: 78%;
  background: #2b2c27;
  color: #fffaf1;
  white-space: pre-wrap;
  border-radius: 10px 10px 2px 10px;
}
.bubble.assistant {
  align-self: stretch;
  padding: 18px;
  border: var(--border-w) solid var(--border);
  background: var(--bg);
  border-radius: 2px 10px 10px 10px;
}
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
.link-btn {
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  border: none;
  background: none;
  color: var(--accent-ink);
  cursor: pointer;
  padding: 2px 6px;
  align-self: flex-start;
}
.link-btn:hover {
  text-decoration: underline;
}
.btn.sm {
  font-size: var(--fs-xs);
  padding: 5px 10px;
}
.ev-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ev {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}
/* 长文本默认限高可展开：既不撑爆面板，也不把内容憋在小框里。 */
.snippet {
  max-height: 7.5em;
  overflow: hidden;
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
  gap: 8px;
  padding: 12px 14px;
  border: var(--border-w) solid var(--border-strong);
  /* 左侧色条只是辅助：真正的等级判断靠上面的文字标签。 */
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg);
}

/* 可编辑草稿：助手起草、用户定稿。 */
.draft-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.draft-input {
  width: 100%;
  resize: vertical;
  min-height: 7.5em;
  padding: 8px 10px;
  border: var(--border-w) solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-2, var(--bg));
  color: inherit;
  font-size: var(--fs-xs);
  line-height: 1.7;
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
  gap: 12px;
  line-height: 1.7;
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

/* ---- 输入区 ---- */
.file-input { display: none; }
.file-list { display: flex; flex-wrap: wrap; gap: 6px; }
.file-chip {
  display: flex; align-items: center; flex-wrap: wrap; gap: 6px;
  max-width: 100%; padding: 5px 9px;
  border: 1px solid var(--border-strong); border-radius: var(--radius-md);
  font-size: var(--fs-xs);
}
.file-name { overflow-wrap: anywhere; }
.file-chip.failed { border-color: var(--err, #b84242); }
.file-error { flex-basis: 100%; color: var(--err, #b84242); overflow-wrap: anywhere; }
.composer.dragging { outline: 2px dashed var(--accent); outline-offset: 6px; }
.composer {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px 24px 20px;
  background: var(--bg-elev);
  border-top: 1px solid var(--border-strong);
}
.composer textarea { width: 100%; background: var(--bg); }
.composer .btn.primary { min-width: 88px; }
@media (max-width: 720px) {
  .meta-grid {
    grid-template-columns: 1fr;
  }
  .stream { max-height: 70vh; padding: 16px; }
  .composer { padding: 14px 16px 16px; }
  .bubble.user { max-width: 92%; }
  .stream-hint { padding: 22px 18px; }
}
/* 只读历史 */
.history-panel { margin: 18px 0; padding: 20px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 10px; }
.history-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
.history-kicker { color: var(--accent-ink); font: 10px/1.4 var(--font-mono); letter-spacing: .12em; }
.history-head h2 { margin-top: 4px; font: 21px/1.3 var(--font-serif); }
.history-tabs { display: flex; gap: 4px; padding: 3px; background: var(--bg); border-radius: 7px; }
.history-tabs button { padding: 6px 12px; color: var(--fg-muted); background: none; border: 0; border-radius: 5px; cursor: pointer; font-size: var(--fs-xs); }
.history-tabs button.active { color: #fffaf1; background: #2b2c27; }
.run-history-layout { display: grid; grid-template-columns: minmax(240px, .8fr) minmax(0, 1.4fr); gap: 18px; margin-top: 16px; }
.run-list { max-height: 460px; overflow-y: auto; border-right: 1px solid var(--border); padding-right: 14px; }
.run-row { width: 100%; display: flex; flex-direction: column; gap: 5px; padding: 12px 10px; text-align: left; color: var(--fg); background: none; border: 0; border-bottom: 1px solid var(--border); cursor: pointer; }
.run-row:hover, .run-row.active { background: var(--bg); }
.run-row.active { box-shadow: inset 3px 0 0 var(--accent); }
.run-row-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.run-model { overflow: hidden; text-overflow: ellipsis; color: var(--fg-muted); font-size: 10px; white-space: nowrap; }
.run-meta, .run-time { color: var(--fg-faint); font-size: 10px; }
.run-detail { min-width: 0; max-height: 460px; overflow-y: auto; }
.detail-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.detail-title > span { color: var(--fg-faint); font-size: var(--fs-xs); }
.run-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 14px 0; padding: 12px; background: var(--bg); }
.run-facts dt { color: var(--fg-faint); font-size: 10px; }
.run-facts dd { margin: 4px 0 0; overflow-wrap: anywhere; font-size: var(--fs-xs); }
.history-evidence-list { display: flex; flex-direction: column; gap: 8px; }
.history-evidence { padding: 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 7px; }
.history-evidence > div { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 10px; }
.history-evidence p { max-height: 9em; overflow-y: auto; margin: 8px 0 0; color: var(--fg-muted); font-size: var(--fs-sm); }
.history-placeholder { padding: 42px 20px; color: var(--fg-faint); text-align: center; border: 1px dashed var(--border-strong); }
.version-compare { display: grid; grid-template-columns: 1fr 1fr auto; gap: 12px; align-items: end; margin: 16px 0; }
.version-diff { margin-bottom: 16px; }
.version-diff pre { max-height: 300px; overflow: auto; padding: 14px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--fg-muted); background: var(--bg); border: 1px solid var(--border); border-radius: 7px; font: 11px/1.7 var(--font-mono); }
.version-list { max-height: 250px; overflow-y: auto; border-top: 1px solid var(--border); }
.version-row { display: grid; grid-template-columns: 48px 64px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 10px 4px; border-bottom: 1px solid var(--border); font-size: var(--fs-xs); }
.version-reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.restore-mask { position: fixed; inset: 0; z-index: 100; display: grid; place-items: center; padding: 24px; background: rgba(28, 28, 24, .56); }
.restore-dialog { width: min(100%, 680px); max-height: calc(100vh - 48px); overflow-y: auto; padding: 26px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 20px 70px rgba(20, 20, 17, .22); }
.restore-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.restore-kicker { color: var(--accent-ink); font: 10px/1.4 var(--font-mono); letter-spacing: .12em; }
.restore-head h2 { margin-top: 5px; font: 23px/1.4 var(--font-serif); }
.restore-impact { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin: 20px 0 16px; }
.restore-impact > div { display: flex; flex-direction: column; gap: 5px; padding: 13px; background: var(--bg); border: 1px solid var(--border); border-radius: 7px; }
.restore-impact span { color: var(--fg-faint); font-size: 10px; }
.restore-impact strong { overflow-wrap: anywhere; font-size: var(--fs-sm); }
.restore-impact small { color: var(--fg-faint); font-size: 10px; }
.restore-warning { color: var(--fg-muted); font-size: var(--fs-sm); line-height: 1.8; }
.restore-confirm { display: flex; align-items: flex-start; gap: 9px; margin: 16px 0; padding: 13px; background: var(--warn-soft); cursor: pointer; font-size: var(--fs-sm); }
.restore-confirm input { width: 16px; height: 16px; margin-top: 2px; accent-color: var(--accent); }
.restore-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }
@media (max-width: 620px) { .restore-impact { grid-template-columns: 1fr; } .restore-dialog { padding: 20px 16px; } }
@media (max-width: 800px) {
  .run-history-layout { grid-template-columns: 1fr; }
  .run-list { max-height: 280px; padding-right: 0; border-right: 0; border-bottom: 1px solid var(--border); }
  .run-facts { grid-template-columns: 1fr 1fr; }
  .version-compare { grid-template-columns: 1fr; }
  .version-row { grid-template-columns: 44px 60px minmax(0, 1fr); }
  .version-row > .faint { grid-column: 3; }
}
/* 本轮降级标记：结论与解释分两行 —— 结论用粗体，解释是它的展开说明。 */
.react-notice-detail {
  display: block;
  margin-top: 4px;
  font-weight: 400;
  opacity: 0.9;
}
</style>
