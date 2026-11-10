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
import { computed, nextTick, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api } from "@/api/client";
import AssistantIssueList from "@/components/AssistantIssueList.vue";
import WizardStepCard from "@/components/WizardStepCard.vue";
import { useLlmConfig } from "@/composables/useLlmConfig";
import { pendingActionKindLabel, pendingActionRiskMeta, sourceLabel } from "@/utils/assistantDisplay";
import type {
  AssistantEvidence,
  AssistantIssue,
  AssistantPendingAction,
  ProjectOut,
  WizardActionEcho,
  WizardCommitResult,
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
}

interface Turn {
  /** 消息唯一 key。 */
  key: string;
  role: "user" | "assistant";
  text: string;
  /** assistant 侧：本次调用的出参（user 消息为 null）。 */
  answer: string;
  evidence: AssistantEvidence[];
  issues: AssistantIssue[];
  rulesEvaluated: string[];
  confidence: number;
  agentRunId: number;
  cards: ActionCard[];
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
// 创作引导（对话式一步步生成）
// ==================================================================

/** 当前引导会话（服务端落库，刷新可续）。 */
const wizard = ref<WizardSessionView | null>(null);
const wizardBusy = ref(false);
const runBusy = ref(false);
/** 角色对话模式：进入后 composer 的每句话都发给角色，而不是问答 agent。 */
const roleMode = ref<{ character: string; scene: string } | null>(null);

const wizardActive = computed(() => wizard.value?.status === "active");

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

/** 进入页面时恢复上次进度：停在哪一步就画哪一步。 */
async function loadWizard(): Promise<void> {
  try {
    const st = await api.getWizard(projectId);
    wizard.value = st.session;
    if (!st.session) return;
    if (st.session.status === "active" && st.question) {
      pushWizardTurn(st.question, "上次问到这一步，接着来。");
    } else if (st.session.status === "committed" && st.session.result) {
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
  turns.value.push(makeUserTurn(q));
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
    const res = await api.assistantQa({ project_id: projectId, query: q });
    turn.answer = res.answer || "";
    turn.evidence = res.evidence ?? [];
    turn.confidence = res.confidence;
    turn.agentRunId = res.agent_run_id;
    // agent 自主调的诊断 / 日志 / 审核结果：新后端会给 issues + rules_evaluated，
    // 旧后端不给 → 空数组 → 整段不渲染（不留空态）。
    turn.issues = res.issues ?? res.findings ?? [];
    turn.rulesEvaluated = res.rules_evaluated ?? [];
    turn.cards = (res.pending_actions ?? []).map((action) => ({
      key: nextKey("card"),
      action,
      state: "pending" as CardState,
      rows: detailRows(action),
      notice: "",
      result: null,
    }));
    turn.loading = false;
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
  try {
    const res = await api.assistantRunPendingAction(
      card.action.endpoint,
      card.action.method,
      card.action.body,
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

const hasHistory = computed(() => turns.value.length > 0);

onMounted(async () => {
  if (badId) return;
  await loadLlm();
  // 项目名用于页头；取不到不阻塞助手功能（徽标与提示里都有兜底文案）。
  try {
    project.value = await api.getProject(projectId);
  } catch {
    /* 忽略：助手能力不依赖项目名 */
  }
  // 恢复未完成的创作引导（会话在服务端，刷新/关页不丢）。
  await loadWizard();
});
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">02 · Assistant</div>
    <div class="asst-head">
      <h1>助手工作台</h1>
      <button class="link-btn back-link" @click="router.push(`/projects/${projectId}`)">
        ← 返回项目工作台 · 直接填表（高级）
      </button>
    </div>

    <div v-if="badId" class="notice err">路由缺少有效的项目 id（/projects/:id/assistant）。</div>

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

    <p class="asst-intro">
      想写小说就说一句「我想生成一本小说」，我一步一步问你；
      不想走引导，也可以直接问正文、语料或图谱上的问题。
    </p>
  </header>

  <section class="card assistant">
    <!-- ==================== 对话流 ==================== -->
    <div ref="streamEl" class="stream">
      <div v-if="!hasHistory" class="stream-hint">
        <p><strong>要写小说？</strong>我一步步问：参考语料、蒸馏档位、题材、字数、章节。</p>
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
          {{ turn.text }}
        </div>

        <!-- 助手气泡：创作引导（解释正文 + 卡片 / 结果） -->
        <div
          v-else-if="turn.wizard || turn.wizardResult || turn.note"
          class="bubble assistant wizard-bubble"
        >
          <div v-if="turn.error" class="notice err">{{ turn.error }}</div>
          <p v-if="turn.text" class="wz-prose" :class="{ note: turn.note }">{{ turn.text }}</p>

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
      <textarea
        v-model="query"
        class="input"
        rows="3"
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
        <button class="btn primary" type="submit" :disabled="sending || badId || !query.trim()">
          {{ sending ? "思考中…" : "提问" }}
        </button>
        <span class="faint" style="font-size: var(--fs-xs)">
          Enter 发送 · Shift+Enter 换行。写操作只限本项目配置与会话状态，每次改动都有审计、可撤销。
        </span>
      </div>
    </form>
  </section>
</template>

<style scoped>
.assistant {
  margin-top: 4px;
  padding: 24px;
  border-color: var(--border);
  box-shadow: var(--shadow-md);
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
.asst-head {
  display: flex;
  align-items: baseline;
  gap: var(--gap);
  flex-wrap: wrap;
}
.asst-head h1 {
  font-family: var(--font-sans);
  font-size: 27px;
  letter-spacing: -.04em;
  margin-top: 6px;
}
.back-link {
  font-size: var(--fs-sm);
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

/* 对话流：固定一屏高 + 纵向滚动，滚到底 = 最新一条。 */
.stream {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  min-height: 16em;
  max-height: 62vh;
  overflow-y: auto;
  padding-right: 4px;
}
.stream-hint {
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
  padding: var(--gap);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-md);
  color: var(--fg-muted);
  background: var(--bg-elev);
}
.stream-hint p {
  margin: 0;
}
.turn {
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
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
  max-width: 85%;
  background: var(--accent);
  box-shadow: 0 4px 10px rgba(65,106,117,.17);
  color: var(--accent-fg);
  white-space: pre-wrap;
}
.bubble.assistant {
  align-self: stretch;
  border: var(--border-w) solid var(--border);
  background: var(--bg-elev);
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
.composer {
  display: flex;
  flex-direction: column;
  gap: var(--gap-sm);
  padding-top: 18px;
  border-top: 1px solid var(--border-strong);
}
.composer .input { background: var(--bg-elev); border-color: var(--border-strong); min-height: 84px; }
.composer .input::placeholder { color: var(--fg-faint); }

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
/* 写操作回执：与解释文字区分开，一眼看出「这次真的改了东西」 */
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
</style>
