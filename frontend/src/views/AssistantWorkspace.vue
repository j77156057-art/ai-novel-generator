<script setup lang="ts">
/**
 * 助手工作台（Assistant Workspace）—— 独立页面版助手面板。
 *
 * 路由：/projects/:id/assistant（项目级资源，与 /projects/:id 并列）。
 * 本文件是原 components/AssistantPanel.vue 的**整体搬迁**（不是复制）：
 * 四个能力区的全部状态与逻辑原样保留，ProjectWorkspace.vue 里只留入口。
 *
 * 四个能力区与后端端点的对应关系（共 13 个端点）：
 *   问答  → POST /api/assistant/qa
 *   诊断  → POST /api/assistant/diagnose
 *   提案  → POST|GET /api/assistant/proposals + /approve + /reject + /apply
 *   版本  → GET /api/assistant/versions + /versions/diff + /{id}/restore
 *   附    → GET /api/assistant/prompts/active + /prompts/versions
 *
 * 三条产品红线在界面上的落点（不是装饰，是这套流程存在的理由）：
 *   1. 助手不能写：面板里没有任何「直接改正文」的入口。写入只有 apply 与
 *      restore 两条，且都需要人点按钮 + 二次确认。
 *   2. 提案必须由用户显式指定：after_text 永远由人敲 —— **不预填、不生成**；
 *      起草表单里的其它字段只是帮人选对目标。
 *   3. 漂移即失效：apply 撞 409(stale) 时明确说「目标已被改动，请重新起草」，
 *      而不是笼统弹一句「操作失败」。
 */
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError, api } from "@/api/client";
import { useLlmConfig } from "@/composables/useLlmConfig";
import { useModelSettingsDialog } from "@/composables/useModelSettings";
import type {
  AssistantEvidence,
  AssistantIssue,
  AssistantProposal,
  AssistantProposalStatus,
  AssistantProposalType,
  AssistantSeverity,
  AssistantTargetKind,
  AssistantVersion,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const router = useRouter();
const projectId = Number(route.params.id);
const badId = !Number.isInteger(projectId) || projectId <= 0;

// 页头：项目名 + 两个模型的生效徽标（一眼看出生成 / 助手是分开的两件事）
const project = ref<ProjectOut | null>(null);
const { dualReady, roleConfig, load: loadLlm } = useLlmConfig();
const modelSettings = useModelSettingsDialog();

const writerCfg = computed(() => roleConfig("writer"));
const assistantCfg = computed(() => roleConfig("assistant"));

// ==================================================================
// 常量 / 展示映射（后端枚举的界面说法）
// ==================================================================

type TabKey = "qa" | "diagnose" | "proposal" | "version";

const TABS: { key: TabKey; label: string }[] = [
  { key: "qa", label: "① 问答" },
  { key: "diagnose", label: "② 诊断" },
  { key: "proposal", label: "③ 提案审批" },
  { key: "version", label: "④ 版本与回滚" },
];

/** type ↔ target_kind 一一对应（后端 TYPE_TO_TARGET_KIND，错配 = 422）。 */
const TYPE_TO_TARGET: Record<AssistantProposalType, AssistantTargetKind> = {
  text_edit: "generation_text",
  prompt_edit: "prompt_override",
  fact_resolution: "project_plot_bible",
};

const TYPE_OPTIONS: { value: AssistantProposalType; label: string }[] = [
  { value: "text_edit", label: "正文改写（generations.text）" },
  { value: "prompt_edit", label: "提示词覆写（运行时覆写块）" },
  { value: "fact_resolution", label: "设定消解（project.plot_bible）" },
];

const TARGET_LABEL: Record<AssistantTargetKind, string> = {
  generation_text: "generation_text",
  project_plot_bible: "project_plot_bible",
  prompt_override: "prompt_override",
};

/** 可覆写的提示词块键（后端 PromptKey，写错 = 422）。 */
const PROMPT_KEYS: { value: string; label: string }[] = [
  { value: "style_suffix", label: "style_suffix（风格附加）" },
  { value: "constraint_note", label: "constraint_note（额外硬约束）" },
  { value: "directive_suffix", label: "directive_suffix（结尾指令）" },
];

/** severity 同时给文字标签 + 标签样式，不靠颜色单打独斗。 */
const SEVERITY_META: Record<AssistantSeverity, { label: string; tag: string }> = {
  error: { label: "错误 error", tag: "err" },
  warn: { label: "警告 warn", tag: "warn" },
  info: { label: "提示 info", tag: "info" },
};

const STATUS_META: Record<AssistantProposalStatus, { label: string; tag: string }> = {
  pending: { label: "待审批 pending", tag: "warn" },
  approved: { label: "已批准 · 未生效 approved", tag: "info" },
  rejected: { label: "已拒绝 rejected", tag: "" },
  applied: { label: "已生效 applied", tag: "ok" },
  stale: { label: "已失效 stale", tag: "err" },
  rolled_back: { label: "已回滚 rolled_back", tag: "err" },
};

const RISK_META: Record<string, { label: string; tag: string }> = {
  low: { label: "低风险 low", tag: "ok" },
  medium: { label: "中风险 medium", tag: "warn" },
  high: { label: "高风险 high", tag: "err" },
};

const STATUS_OPTIONS: AssistantProposalStatus[] = [
  "pending",
  "approved",
  "rejected",
  "applied",
  "stale",
  "rolled_back",
];

/** 后端可能扩展枚举，取值一律走兜底，避免未知值把界面渲染崩掉。 */
function statusMeta(s: string): { label: string; tag: string } {
  return STATUS_META[s as AssistantProposalStatus] ?? { label: s, tag: "" };
}
function severityMeta(s: string): { label: string; tag: string } {
  return SEVERITY_META[s as AssistantSeverity] ?? { label: s, tag: "" };
}
function riskMeta(r: string): { label: string; tag: string } {
  return RISK_META[r] ?? { label: r, tag: "" };
}

/** evidence.source 的人话说法；原始值一律同时显示，避免「看不出来源」。 */
function sourceLabel(source: string): string {
  const s = (source || "").toLowerCase();
  if (s === "generation") return "本次生成正文";
  if (s === "novel" || s.startsWith("novel:")) return "参考语料";
  if (s === "kg") return "知识图谱 / 提醒";
  if (s === "fact") return "事实账本";
  return "其它来源";
}

// ==================================================================
// 通用工具
// ==================================================================

const tab = ref<TabKey>("qa");

/** 长文本（snippet / 正文 / diff）的展开态，按 key 存，避免小框憋住长文。 */
const expanded = reactive<Record<string, boolean>>({});
function isOpen(key: string): boolean {
  return expanded[key] === true;
}
function toggle(key: string): void {
  expanded[key] = !expanded[key];
}

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

/** 下拉框取值：统一走 select → string，再按调用方的枚举收敛。 */
function selectValue(e: Event): string {
  const el = e.target as HTMLSelectElement;
  return el.value;
}

function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  // 后端给的是 ISO8601（可能带时区），只做「T → 空格」的展示处理，不做时区换算。
  return value.replace("T", " ").slice(0, 19);
}

function shortHash(hash: string | null | undefined): string {
  if (!hash) return "—";
  return hash.length > 20 ? `${hash.slice(0, 12)}…${hash.slice(-4)}` : hash;
}

// ==================================================================
// ① 问答
// ==================================================================

const qaQuery = ref("");
const qaScope = ref("");
const qaLoading = ref(false);
const qaError = ref<string | null>(null);
const qaResult = ref<{
  answer: string;
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
} | null>(null);
/** 记下最近一次 AgentRun.id，供起草提案时关联 run_id。 */
const lastRunId = ref<number | null>(null);

async function runQa(): Promise<void> {
  const query = qaQuery.value.trim();
  if (!query) {
    qaError.value = "请先输入要问的问题。";
    return;
  }
  qaLoading.value = true;
  qaError.value = null;
  try {
    const res = await api.assistantQa({
      project_id: projectId,
      query,
      // scope 留空就不传：后端是可选字段，传空串没有意义。
      scope: qaScope.value.trim() || null,
    });
    qaResult.value = {
      answer: res.answer,
      evidence: res.evidence ?? [],
      confidence: res.confidence,
      agent_run_id: res.agent_run_id,
    };
    lastRunId.value = res.agent_run_id;
  } catch (err) {
    qaResult.value = null;
    qaError.value = `问答失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  } finally {
    qaLoading.value = false;
  }
}

const qaConfidenceTag = computed(() => {
  const c = qaResult.value?.confidence ?? 0;
  if (c >= 0.7) return "ok";
  if (c >= 0.4) return "warn";
  return "err";
});

// ==================================================================
// ② 诊断
// ==================================================================

const diagMode = ref<"all" | "segment" | "character">("all");
const diagSegment = ref("");
const diagCharacter = ref("");
const diagQuery = ref("");
const diagLoading = ref(false);
const diagError = ref<string | null>(null);
const diagResult = ref<{
  answer: string;
  issues: AssistantIssue[];
  evidence: AssistantEvidence[];
  confidence: number;
  agent_run_id: number;
  rules_evaluated: string[];
} | null>(null);

/** scope 文本：all / segment:N / character:名字（后端按这个形状解析）。 */
const diagScope = computed(() => {
  if (diagMode.value === "segment") {
    const n = diagSegment.value.trim();
    return n ? `segment:${n}` : "";
  }
  if (diagMode.value === "character") {
    const n = diagCharacter.value.trim();
    return n ? `character:${n}` : "";
  }
  return "all";
});

async function runDiagnose(): Promise<void> {
  if (!diagScope.value) {
    diagError.value = "请先填全诊断范围（段号或角色名不能为空）。";
    return;
  }
  diagLoading.value = true;
  diagError.value = null;
  try {
    const res = await api.assistantDiagnose({
      project_id: projectId,
      scope: diagScope.value,
      query: diagQuery.value.trim() || null,
    });
    diagResult.value = {
      answer: res.answer,
      issues: res.issues ?? [],
      evidence: res.evidence ?? [],
      confidence: res.confidence,
      agent_run_id: res.agent_run_id,
      rules_evaluated: res.rules_evaluated ?? [],
    };
    lastRunId.value = res.agent_run_id;
  } catch (err) {
    diagResult.value = null;
    diagError.value = `诊断失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  } finally {
    diagLoading.value = false;
  }
}

/** 按 severity 分组（error → warn → info），组内保持后端返回顺序。 */
const issuesBySeverity = computed(() => {
  const source = diagResult.value?.issues ?? [];
  const buckets: Record<AssistantSeverity, AssistantIssue[]> = {
    error: [],
    warn: [],
    info: [],
  };
  for (const issue of source) {
    const sev: AssistantSeverity =
      issue.severity === "error" || issue.severity === "warn" ? issue.severity : "info";
    buckets[sev].push(issue);
  }
  const order: AssistantSeverity[] = ["error", "warn", "info"];
  return order
    .filter((sev) => buckets[sev].length > 0)
    .map((sev) => ({ severity: sev, items: buckets[sev] }));
});

// ==================================================================
// ③ 提案：起草 + 列表 + 详情 + 审批 / 应用
// ==================================================================

const draftType = ref<AssistantProposalType>("text_edit");
const draftTargetId = ref("");
const draftPromptKey = ref(PROMPT_KEYS[0].value);
const draftAfterText = ref("");
const draftReason = ref("");
const draftRisk = ref<"" | "low" | "medium" | "high">("");
const draftFromIssue = ref<string | null>(null);
const draftRunId = ref<number | null>(null);

const draftBusy = ref(false);
const draftError = ref<string | null>(null);
const draftNotice = ref<string | null>(null);

/** target_kind 由 type 唯一决定，界面只展示，不允许用户错配。 */
const draftTargetKind = computed<AssistantTargetKind>(() => TYPE_TO_TARGET[draftType.value]);

/** target_id 的实际取值（提示词类走下拉，设定类固定为 plot_bible）。 */
const draftTargetIdValue = computed(() => {
  if (draftType.value === "prompt_edit") return draftPromptKey.value;
  if (draftType.value === "fact_resolution") return "plot_bible";
  return draftTargetId.value.trim();
});

const draftTargetHint = computed(() => {
  if (draftType.value === "text_edit") {
    return "填 generations.id（整数）。生成列表里每段标题的 #号 就是这个 id。";
  }
  if (draftType.value === "fact_resolution") {
    return "固定为 plot_bible（后端强制），after_text 必须是合法 JSON 对象。";
  }
  return "从三个可覆写提示词块里选一个（其它键后端会 422）。";
});

/** after_text 输入框的示例。放在 script 里，避免模板属性里出现转义引号。 */
const draftPlaceholder = computed(() => {
  if (draftType.value === "fact_resolution") {
    return '{"characters": [{"name": "张三", "role": "主角"}]}';
  }
  if (draftType.value === "prompt_edit") {
    return "写进该提示词块的附加文本（会追加在代码默认块之后）";
  }
  return "把改后的完整正文粘贴在这里（正文类是整段替换，不是局部补丁）";
});

/** 本地校验：只校验「能不能发出去」，绝不替用户生成 after_text。 */
const draftProblem = computed<string | null>(() => {
  if (!draftTargetIdValue.value) return "target_id 不能为空。";
  if (draftType.value === "text_edit" && !/^\d+$/.test(draftTargetIdValue.value)) {
    return "text_edit 的 target_id 必须是 generations.id（整数）。";
  }
  if (!draftAfterText.value.trim()) {
    return "after_text 必须由你亲自填写 —— 系统不会替你生成改写后的内容。";
  }
  if (draftType.value === "fact_resolution") {
    let parsed: unknown;
    try {
      parsed = JSON.parse(draftAfterText.value);
    } catch {
      return "fact_resolution 的 after_text 不是合法 JSON。";
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return "fact_resolution 的 after_text 必须是 JSON 对象（plot_bible 是 dict）。";
    }
  }
  return null;
});

function resetDraft(): void {
  draftAfterText.value = "";
  draftFromIssue.value = null;
  draftReason.value = "";
  draftRisk.value = "";
  draftRunId.value = null;
  draftError.value = null;
}

function onTypeChange(e: Event): void {
  draftType.value = selectValue(e) as AssistantProposalType;
}
function onRiskChange(e: Event): void {
  const v = selectValue(e);
  draftRisk.value = v === "low" || v === "medium" || v === "high" ? v : "";
}
function onPromptKeyChange(e: Event): void {
  draftPromptKey.value = selectValue(e);
}

/** 从诊断 issue 起草：只带 issue.id 与 suggestion 作为素材，after_text 留空。 */
function draftFrom(issue: AssistantIssue): void {
  draftType.value = "text_edit";
  draftTargetId.value = "";
  draftFromIssue.value = issue.id;
  draftReason.value = issue.suggestion || issue.title;
  // 红线：after_text 一律留空，由用户基于 issue 自己写。
  draftAfterText.value = "";
  draftRisk.value = "";
  draftRunId.value = diagResult.value?.agent_run_id ?? null;
  draftError.value = null;
  draftNotice.value = null;
  tab.value = "proposal";
}

async function submitDraft(): Promise<void> {
  const problem = draftProblem.value;
  if (problem) {
    draftError.value = problem;
    return;
  }
  draftBusy.value = true;
  draftError.value = null;
  draftNotice.value = null;
  try {
    const body: Parameters<typeof api.createProposal>[0] = {
      project_id: projectId,
      type: draftType.value,
      target_kind: draftTargetKind.value,
      target_id: draftTargetIdValue.value,
      // 唯一的「改成什么」：始终来自用户输入框，未经任何加工。
      after_text: draftAfterText.value,
      reason: draftReason.value.trim(),
      // scope 不带服务端独占键（base_content_hash / target_snapshot / diff / impact）。
      scope: null,
    };
    if (draftFromIssue.value) body.from_issue = draftFromIssue.value;
    if (draftRisk.value) body.risk_level = draftRisk.value;
    // 审计关联：优先用「起草来源那次诊断」的 run；没有则退回最近一次问答/诊断
    // 的 run —— 让「这条提案为什么提的」事后能溯源，不让 run_id 空着。
    const runId = draftRunId.value ?? lastRunId.value;
    if (runId !== null) body.run_id = runId;
    const res = await api.createProposal(body);
    draftNotice.value =
      `提案 #${res.proposal.id} 已创建，状态 ${statusMeta(res.proposal.status).label}。${res.note}`;
    resetDraft();
    await refreshProposals();
    await loadProposalDetail(res.proposal.id);
  } catch (err) {
    draftError.value = `创建提案失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  } finally {
    draftBusy.value = false;
  }
}

// ---- 列表 ----
const proposalStatusFilter = ref<AssistantProposalStatus | "">("");
const proposals = ref<AssistantProposal[]>([]);
const listLoading = ref(false);
const listError = ref<string | null>(null);
const selectedId = ref<number | null>(null);
const selected = ref<AssistantProposal | null>(null);
const detailLoading = ref(false);

async function refreshProposals(): Promise<void> {
  listLoading.value = true;
  listError.value = null;
  try {
    const res = await api.listProposals(projectId, proposalStatusFilter.value || null);
    proposals.value = res.proposals ?? [];
  } catch (err) {
    proposals.value = [];
    listError.value = `提案列表加载失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  } finally {
    listLoading.value = false;
  }
}

async function onStatusFilterChange(e: Event): Promise<void> {
  const v = selectValue(e);
  proposalStatusFilter.value = (STATUS_OPTIONS as string[]).includes(v)
    ? (v as AssistantProposalStatus)
    : "";
  await refreshProposals();
}

async function loadProposalDetail(id: number): Promise<void> {
  detailLoading.value = true;
  try {
    const res = await api.getProposal(id);
    selected.value = res.proposal;
    selectedId.value = res.proposal.id;
  } catch (err) {
    selected.value = null;
    selectedId.value = null;
    listError.value = `提案 #${id} 详情加载失败：${textOf(err)}`;
  } finally {
    detailLoading.value = false;
  }
}

async function pickProposal(p: AssistantProposal): Promise<void> {
  if (selectedId.value === p.id && selected.value) return;
  actionError.value = null;
  actionNotice.value = null;
  staleHint.value = null;
  confirmApply.value = false;
  await loadProposalDetail(p.id);
}

// ---- 动作（批准 / 拒绝 / 应用）----
const actionBusy = ref(false);
const actionError = ref<string | null>(null);
const actionNotice = ref<string | null>(null);
const staleHint = ref<{ message: string; expected?: string; current?: string } | null>(null);
const confirmApply = ref(false);
const actorName = ref("");

function actor(): string | null {
  const a = actorName.value.trim();
  return a || null;
}

async function runAction(kind: "approve" | "reject" | "apply"): Promise<void> {
  const p = selected.value;
  if (!p) return;
  actionBusy.value = true;
  actionError.value = null;
  actionNotice.value = null;
  staleHint.value = null;
  try {
    if (kind === "approve") {
      const res = await api.approveProposal(p.id, { action: "approve", actor: actor() });
      selected.value = res.proposal;
      actionNotice.value =
        `已批准提案 #${p.id}。${res.note ?? "内容仍未改动，需再点「应用」才会写入。"}`;
    } else if (kind === "reject") {
      const res = await api.rejectProposal(p.id, { action: "reject", actor: actor() });
      selected.value = res.proposal;
      actionNotice.value = `已拒绝提案 #${p.id}。${res.note ?? ""}`;
    } else {
      const res = await api.applyProposal(p.id, { action: "apply", actor: actor() });
      selected.value = res.proposal ?? null;
      actionNotice.value =
        `已生效：提案 #${res.proposal_id} 已写入目标，并生成版本 Version #${res.version_id}` +
        `（内容哈希 ${shortHash(res.content_hash)}）。`;
      confirmApply.value = false;
    }
    await refreshProposals();
    await refreshVersions();
  } catch (err) {
    const status = statusOf(err);
    const detail = detailOf(err);
    confirmApply.value = false;
    if (status === 409 && detail?.error === "stale") {
      // 漂移：目标内容已被改动，后端已把提案置为 stale。必须说人话 + 给重建入口。
      staleHint.value = {
        message: "目标内容已被改动，提案已失效，请基于最新内容重新起草。",
        expected: detail.expected_hash,
        current: detail.current_hash,
      };
      actionError.value =
        `应用失败：目标内容已被改动，提案已失效，请基于最新内容重新起草。` +
        `（后端提示：${detail.hint ?? detail.message ?? "stale"}）`;
      await refreshProposals();
    } else if (status === 409) {
      actionError.value =
        `应用被状态机拒绝（提案当前不在 approved 状态，或已经生效过）：${textOf(err)}`;
      await refreshProposals();
    } else if (status === 400) {
      actionError.value = `请求被后端拒绝（目标基线缺失或提案数据不完整）：${textOf(err)}`;
    } else {
      actionError.value = `操作失败（HTTP ${status ?? "?"}）：${textOf(err)}`;
    }
  } finally {
    actionBusy.value = false;
  }
}

/** 从失效提案重建：只回填「改哪儿」，after_text 依旧留给用户。 */
function rebuildFromStale(): void {
  const p = selected.value;
  if (!p) return;
  draftType.value = p.type;
  if (p.type === "prompt_edit") {
    draftPromptKey.value = PROMPT_KEYS.some((k) => k.value === p.target_id)
      ? p.target_id
      : PROMPT_KEYS[0].value;
  } else if (p.type === "text_edit") {
    draftTargetId.value = p.target_id;
  }
  draftReason.value = p.reason ?? "";
  draftRisk.value =
    p.risk_level === "low" || p.risk_level === "medium" || p.risk_level === "high"
      ? p.risk_level
      : "";
  draftRunId.value = p.run_id ?? null;
  draftFromIssue.value = null;
  // 红线：after_text 不回填失效提案里的旧内容 —— 那正是已经过期的东西。
  draftAfterText.value = "";
  draftError.value = null;
  draftNotice.value = "已按失效提案回填「改哪儿」；after_text 需要你基于最新内容重新填写。";
  staleHint.value = null;
  actionError.value = null;
  tab.value = "proposal";
}

const canApprove = computed(() => selected.value?.status === "pending");
const canReject = computed(() => selected.value?.status === "pending");
const canApply = computed(() => selected.value?.status === "approved");

// ==================================================================
// ④ 版本：列表 / diff / 回滚
// ==================================================================

const versions = ref<AssistantVersion[]>([]);
const versionLoading = ref(false);
const versionError = ref<string | null>(null);
const versionNotice = ref<string | null>(null);
const diffFrom = ref<number | null>(null);
const diffTo = ref<number | null>(null);
const diffText = ref<string>("");
const diffLoading = ref(false);
const restoreTarget = ref<AssistantVersion | null>(null);
const restoreBusy = ref(false);

const promptsActive = ref<{
  blocks: { prompt_key: string; source: string; text: string }[];
  has_override: boolean;
  note: string;
} | null>(null);
const promptOverrideCount = ref<number | null>(null);
const promptsError = ref<string | null>(null);

async function refreshVersions(): Promise<void> {
  versionLoading.value = true;
  versionError.value = null;
  try {
    const res = await api.listVersions(projectId);
    versions.value = res.versions ?? [];
    // 默认选中「最早 → 最新」两端，省一次点击。
    if (versions.value.length >= 2) {
      diffFrom.value = versions.value[0].id;
      diffTo.value = versions.value[versions.value.length - 1].id;
    } else if (versions.value.length === 1) {
      diffFrom.value = versions.value[0].id;
      diffTo.value = versions.value[0].id;
    } else {
      diffFrom.value = null;
      diffTo.value = null;
    }
  } catch (err) {
    versions.value = [];
    versionError.value = `版本列表加载失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  } finally {
    versionLoading.value = false;
  }
}

function onDiffFromChange(e: Event): void {
  diffFrom.value = Number(selectValue(e));
}
function onDiffToChange(e: Event): void {
  diffTo.value = Number(selectValue(e));
}

async function runDiff(): Promise<void> {
  if (diffFrom.value === null || diffTo.value === null) return;
  diffLoading.value = true;
  versionError.value = null;
  try {
    const res = await api.diffVersions(diffFrom.value, diffTo.value);
    diffText.value = res.diff || "（两个版本内容完全一致，没有差异）";
  } catch (err) {
    diffText.value = "";
    versionError.value = `版本对比失败：${textOf(err)}`;
  } finally {
    diffLoading.value = false;
  }
}

async function confirmRestore(): Promise<void> {
  const v = restoreTarget.value;
  if (!v) return;
  restoreBusy.value = true;
  versionError.value = null;
  try {
    const res = await api.restoreVersion(v.id, { action: "restore", actor: actor() });
    versionNotice.value =
      `已回滚：用 Version #${res.restored_from_version_id} 的内容覆盖了当前内容，` +
      `并生成新版本 Version #${res.version_id}（历史未丢）。`;
    restoreTarget.value = null;
    await refreshVersions();
    await refreshProposals();
    await runDiff();
  } catch (err) {
    versionError.value = `回滚失败（HTTP ${statusOf(err) ?? "?"}）：${textOf(err)}`;
  } finally {
    restoreBusy.value = false;
  }
}

async function refreshPrompts(): Promise<void> {
  promptsError.value = null;
  try {
    const res = await api.promptsActive(projectId);
    promptsActive.value = {
      blocks: res.blocks ?? [],
      has_override: res.has_override,
      note: res.note,
    };
    const hist = await api.promptVersions(projectId);
    promptOverrideCount.value = hist.count ?? 0;
  } catch (err) {
    promptsActive.value = null;
    promptOverrideCount.value = null;
    promptsError.value = `提示词状态加载失败：${textOf(err)}`;
  }
}

async function loadAll(): Promise<void> {
  await Promise.all([refreshProposals(), refreshVersions(), refreshPrompts()]);
}

// 进页面就把列表拉好：切到「提案 / 版本」两个页签时直接有内容，
// 不需要用户先点一次刷新。三个请求都是只读 GET。
onMounted(async () => {
  if (badId) return;
  await Promise.all([loadAll(), loadLlm()]);
  // 项目名用于页头；取不到不阻塞助手功能（徽标与提示里都有兜底文案）。
  try {
    project.value = await api.getProject(projectId);
  } catch {
    /* 忽略：助手能力不依赖项目名 */
  }
});
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">02 · Assistant</div>
    <div class="asst-head">
      <h1>助手工作台</h1>
      <button class="link-btn back-link" @click="router.push(`/projects/${projectId}`)">
        ← 返回项目工作台
      </button>
    </div>

    <div v-if="badId" class="notice err">路由缺少有效的项目 id（/projects/:id/assistant）。</div>

    <div class="asst-meta">
      <span class="tag accent asst-project">
        {{ project ? `《${project.title}》` : "项目 #" + projectId }}
      </span>
      <!-- 两个模型徽标：生成 / 助手分开显示，一眼看出是两回事 -->
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
      <button class="btn sm" @click="modelSettings.openDialog">双模型设置…</button>
      <span v-if="!dualReady" class="faint" style="font-size: var(--fs-xs)">
        后端未返回分角色配置，两个徽标当前显示的是同一份单模型生效值
      </span>
    </div>

    <p>
      助手<strong>不会</strong>直接改你的正文。它只做只读检索与诊断；
      任何改动都要走「起草提案 → 批准 → 应用」三步，且<strong>改成什么永远由你填写</strong>。
    </p>
  </header>

  <section class="card assistant">
    <div class="tabs">
      <button
        v-for="t in TABS"
        :key="t.key"
        :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >
        {{ t.label }}
      </button>
    </div>

    <!-- ==================== ① 问答 ==================== -->
    <div v-if="tab === 'qa'" class="pane">
      <div class="field">
        <label for="asst-qa-query">问题（必填）</label>
        <textarea
          id="asst-qa-query"
          v-model="qaQuery"
          class="input"
          rows="3"
          placeholder="例如：主角第 3 段的动机和第 1 段一致吗？"
        ></textarea>
      </div>
      <div class="field">
        <label for="asst-qa-scope">范围 scope（可选，留空＝不限）</label>
        <input
          id="asst-qa-scope"
          v-model="qaScope"
          class="input"
          placeholder="all / segment:3 / character:张三"
        />
      </div>
      <div class="row tight">
        <button class="btn primary" :disabled="qaLoading" @click="runQa">
          {{ qaLoading ? "检索中…" : "提问（只读检索）" }}
        </button>
        <span class="faint" style="font-size: var(--fs-xs)">
          助手只走只读工具；检索不到证据时会明说，不会编造。
        </span>
      </div>

      <div v-if="qaError" class="notice err">{{ qaError }}</div>

      <div v-if="qaResult" class="qa-out">
        <div class="row tight" style="align-items: center">
          <span class="tag" :class="qaConfidenceTag">
            置信度 {{ qaResult.confidence.toFixed(2) }}
          </span>
          <span class="tag info mono">AgentRun #{{ qaResult.agent_run_id }}</span>
          <span class="tag mono">证据 {{ qaResult.evidence.length }} 条</span>
        </div>

        <div class="blk">
          <div class="blk-head">回答</div>
          <div class="prose snippet" :class="{ expanded: isOpen('qa-answer') }">
            {{ qaResult.answer || "（助手未返回文字回答，请直接看下方证据）" }}
          </div>
          <button class="link-btn" @click="toggle('qa-answer')">
            {{ isOpen("qa-answer") ? "收起" : "展开全文" }}
          </button>
        </div>

        <div class="blk">
          <div class="blk-head">证据（{{ qaResult.evidence.length }} 条）</div>
          <div v-if="!qaResult.evidence.length" class="empty">本次没有检索到证据。</div>
          <div v-else class="ev-list">
            <div v-for="(ev, i) in qaResult.evidence" :key="`qa-ev-${i}`" class="ev">
              <div class="row tight" style="align-items: center">
                <span class="tag accent">{{ sourceLabel(ev.source) }}</span>
                <span class="tag mono">source={{ ev.source }}</span>
                <span class="tag mono">kind={{ ev.kind }}</span>
                <span class="tag mono">score={{ Number(ev.score).toFixed(3) }}</span>
                <span class="tag mono">ref={{ ev.ref_id }}</span>
              </div>
              <div class="prose snippet" :class="{ expanded: isOpen(`qa-ev-${i}`) }">
                {{ ev.snippet }}
              </div>
              <button class="link-btn" @click="toggle(`qa-ev-${i}`)">
                {{ isOpen(`qa-ev-${i}`) ? "收起原文" : "展开原文" }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== ② 诊断 ==================== -->
    <div v-else-if="tab === 'diagnose'" class="pane">
      <div class="field">
        <label>诊断范围 scope（必填）</label>
        <div class="chips">
          <button class="chip" :class="{ on: diagMode === 'all' }" @click="diagMode = 'all'">
            全部 all
          </button>
          <button
            class="chip"
            :class="{ on: diagMode === 'segment' }"
            @click="diagMode = 'segment'"
          >
            指定段 segment:N
          </button>
          <button
            class="chip"
            :class="{ on: diagMode === 'character' }"
            @click="diagMode = 'character'"
          >
            角色 character:名字
          </button>
        </div>
      </div>
      <div v-if="diagMode === 'segment'" class="field">
        <label for="asst-diag-seg">段号 N</label>
        <input id="asst-diag-seg" v-model="diagSegment" class="input" placeholder="例如 3" />
      </div>
      <div v-else-if="diagMode === 'character'" class="field">
        <label for="asst-diag-ch">角色名</label>
        <input id="asst-diag-ch" v-model="diagCharacter" class="input" placeholder="例如 张三" />
      </div>

      <div class="field">
        <label for="asst-diag-q">补充说明 query（可选）</label>
        <input
          id="asst-diag-q"
          v-model="diagQuery"
          class="input"
          placeholder="带上 query 时会额外检索参考语料作为证据"
        />
      </div>

      <div class="row tight">
        <button class="btn primary" :disabled="diagLoading" @click="runDiagnose">
          {{ diagLoading ? "诊断中…" : "跑诊断（只读）" }}
        </button>
        <span class="tag mono">scope = {{ diagScope || "（未填全）" }}</span>
      </div>

      <div v-if="diagError" class="notice err">{{ diagError }}</div>

      <div v-if="diagResult" class="diag-out">
        <div class="row tight" style="align-items: center">
          <span class="tag info mono">AgentRun #{{ diagResult.agent_run_id }}</span>
          <span class="tag mono">命中 {{ diagResult.issues.length }} 个问题</span>
          <span class="tag mono">置信度 {{ diagResult.confidence.toFixed(2) }}</span>
        </div>

        <div class="blk">
          <div class="blk-head">诊断结论</div>
          <div class="prose snippet" :class="{ expanded: isOpen('diag-answer') }">
            {{ diagResult.answer }}
          </div>
          <button class="link-btn" @click="toggle('diag-answer')">
            {{ isOpen("diag-answer") ? "收起" : "展开全文" }}
          </button>
        </div>

        <div v-if="diagResult.rules_evaluated.length" class="rules-line">
          <span class="blk-head">已执行规则：</span>
          <span v-for="r in diagResult.rules_evaluated" :key="r" class="tag mono">{{ r }}</span>
        </div>

        <div v-if="!issuesBySeverity.length" class="empty">
          本次诊断没有命中任何已知问题模式。
          <span class="faint">（规则全部已执行；这不等同于「绝对没问题」）</span>
        </div>

        <div v-else class="sev-groups">
          <div v-for="g in issuesBySeverity" :key="g.severity" class="sev-group">
            <div class="sev-head">
              <span class="tag" :class="severityMeta(g.severity).tag">
                {{ severityMeta(g.severity).label }}
              </span>
              <span class="faint" style="font-size: var(--fs-xs)">{{ g.items.length }} 条</span>
            </div>
            <div v-for="issue in g.items" :key="issue.id" class="issue">
              <div class="row tight" style="align-items: center">
                <span class="tag" :class="severityMeta(issue.severity).tag">
                  {{ severityMeta(issue.severity).label }}
                </span>
                <strong>{{ issue.title }}</strong>
                <span class="tag mono">rule={{ issue.rule }}</span>
                <span class="tag mono">{{ issue.type }}</span>
                <span class="spacer"></span>
                <button class="btn sm" @click="draftFrom(issue)">起草提案 →</button>
              </div>

              <div class="blk">
                <span class="blk-head">判定 claim（库里是什么）</span>
                <div
                  class="prose snippet"
                  :class="{ expanded: isOpen(`issue-claim-${issue.id}`) }"
                >{{ issue.claim }}</div>
                <button class="link-btn" @click="toggle(`issue-claim-${issue.id}`)">
                  {{ isOpen(`issue-claim-${issue.id}`) ? "收起" : "展开" }}
                </button>
              </div>

              <div class="blk">
                <span class="blk-head">建议 suggestion（人该怎么处理）</span>
                <div
                  class="prose snippet"
                  :class="{ expanded: isOpen(`issue-sug-${issue.id}`) }"
                >{{ issue.suggestion }}</div>
                <button class="link-btn" @click="toggle(`issue-sug-${issue.id}`)">
                  {{ isOpen(`issue-sug-${issue.id}`) ? "收起" : "展开" }}
                </button>
              </div>

              <div class="blk">
                <span class="blk-head">证据原文（{{ issue.evidence.length }} 条）</span>
                <div v-if="!issue.evidence.length" class="faint">该 issue 未附证据。</div>
                <div v-else class="ev-list">
                  <div v-for="(ev, i) in issue.evidence" :key="`is-ev-${issue.id}-${i}`" class="ev">
                    <div class="row tight" style="align-items: center">
                      <span class="tag accent">{{ sourceLabel(ev.source) }}</span>
                      <span class="tag mono">source={{ ev.source }}</span>
                      <span class="tag mono">kind={{ ev.kind }}</span>
                      <span class="tag mono">score={{ Number(ev.score).toFixed(3) }}</span>
                    </div>
                    <div
                      class="prose snippet"
                      :class="{ expanded: isOpen(`is-ev-${issue.id}-${i}`) }"
                    >{{ ev.snippet }}</div>
                    <button class="link-btn" @click="toggle(`is-ev-${issue.id}-${i}`)">
                      {{ isOpen(`is-ev-${issue.id}-${i}`) ? "收起原文" : "展开原文" }}
                    </button>
                  </div>
                </div>
              </div>

              <div class="faint mono" style="font-size: var(--fs-xs)">
                issue.id = {{ issue.id }}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== ③ 提案审批 ==================== -->
    <div v-else-if="tab === 'proposal'" class="pane">
      <!-- 起草 -->
      <div class="sub-card">
        <div class="sub-title">起草提案（创建后停在「待审批」，正文不会被动）</div>

        <div class="notice warn draft-warn">
          <strong>after_text 必须由你亲自填写。</strong>
          系统不会替你生成改写后的内容 —— 从诊断 issue 起草时，issue 只提供
          「为什么改」的素材，<strong>改成什么</strong>只能由你决定。
        </div>

        <div class="grid-2">
          <div class="field">
            <label for="asst-draft-type">提案类型 type（必填）</label>
            <select
              id="asst-draft-type"
              class="input"
              :value="draftType"
              @change="onTypeChange"
            >
              <option v-for="o in TYPE_OPTIONS" :key="o.value" :value="o.value">
                {{ o.label }}
              </option>
            </select>
          </div>
          <div class="field">
            <label>目标类型 target_kind（由 type 唯一决定，不可改）</label>
            <div class="row tight" style="align-items: center">
              <span class="tag accent mono">{{ TARGET_LABEL[draftTargetKind] }}</span>
              <span class="faint" style="font-size: var(--fs-xs)">错配会被后端 422 拒绝</span>
            </div>
          </div>
        </div>

        <div class="grid-2">
          <div class="field">
            <label for="asst-draft-target">target_id（必填）</label>
            <select
              v-if="draftType === 'prompt_edit'"
              id="asst-draft-target"
              class="input"
              :value="draftPromptKey"
              @change="onPromptKeyChange"
            >
              <option v-for="k in PROMPT_KEYS" :key="k.value" :value="k.value">{{ k.label }}</option>
            </select>
            <input
              v-else-if="draftType === 'fact_resolution'"
              id="asst-draft-target"
              class="input"
              :value="draftTargetIdValue"
              disabled
            />
            <input
              v-else
              id="asst-draft-target"
              v-model="draftTargetId"
              class="input"
              placeholder="例如 12"
            />
            <span class="faint" style="font-size: var(--fs-xs)">{{ draftTargetHint }}</span>
          </div>
          <div class="field">
            <label for="asst-draft-risk">风险等级 risk_level（可选）</label>
            <select id="asst-draft-risk" class="input" :value="draftRisk" @change="onRiskChange">
              <option value="">（按后端默认）</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </div>
        </div>

        <div class="field">
          <label for="asst-draft-after">after_text（必填 · 改成什么 · 请手动填写）</label>
          <textarea
            id="asst-draft-after"
            v-model="draftAfterText"
            class="input"
            rows="6"
            :placeholder="draftPlaceholder"
          ></textarea>
        </div>

        <div class="field">
          <label for="asst-draft-reason">理由 reason（可选）</label>
          <input
            id="asst-draft-reason"
            v-model="draftReason"
            class="input"
            placeholder="从诊断 issue 起草时会自动带上该 issue 的建议"
          />
        </div>

        <div v-if="draftFromIssue" class="row tight" style="align-items: center">
          <span class="tag accent mono">from_issue = {{ draftFromIssue }}</span>
          <button class="link-btn" @click="draftFromIssue = null">取消关联</button>
        </div>

        <div class="row tight">
          <button class="btn primary" :disabled="draftBusy" @click="submitDraft">
            {{ draftBusy ? "提交中…" : "创建提案（停在待审批）" }}
          </button>
          <button class="btn" @click="resetDraft">清空草稿</button>
          <span v-if="draftProblem" class="tag err">{{ draftProblem }}</span>
        </div>

        <div v-if="draftError" class="notice err">{{ draftError }}</div>
        <div v-if="draftNotice" class="notice ok">{{ draftNotice }}</div>
      </div>

      <!-- 列表 -->
      <div class="sub-card">
        <div class="sub-title">
          提案列表
          <span class="assist-hint">{{ proposals.length }} 条</span>
        </div>
        <div class="row tight" style="align-items: flex-end">
          <div class="field" style="min-width: 200px">
            <label for="asst-filter">按状态过滤</label>
            <select
              id="asst-filter"
              class="input"
              :value="proposalStatusFilter"
              @change="onStatusFilterChange"
            >
              <option value="">（全部）</option>
              <option v-for="s in STATUS_OPTIONS" :key="s" :value="s">{{ s }}</option>
            </select>
          </div>
          <button class="btn" :disabled="listLoading" @click="refreshProposals">
            {{ listLoading ? "刷新中…" : "刷新列表" }}
          </button>
        </div>

        <div v-if="listError" class="notice err">{{ listError }}</div>

        <div v-if="!proposals.length" class="empty">暂无提案。</div>
        <div v-else class="prop-list">
          <div
            v-for="p in proposals"
            :key="p.id"
            class="list-item clickable"
            :class="{ active: selectedId === p.id }"
            @click="pickProposal(p)"
          >
            <div style="min-width: 0; flex: 1">
              <div class="row tight" style="align-items: center">
                <span class="tag mono">#{{ p.id }}</span>
                <span class="tag" :class="statusMeta(p.status).tag">
                  {{ statusMeta(p.status).label }}
                </span>
                <span class="tag" :class="riskMeta(p.risk_level).tag">
                  {{ riskMeta(p.risk_level).label }}
                </span>
                <span class="tag accent mono">{{ p.type }}</span>
              </div>
              <div class="faint mono" style="font-size: var(--fs-xs)">
                {{ p.target_kind }}:{{ p.target_id }} · 创建于 {{ fmtTime(p.created_at) }}
              </div>
            </div>
            <span class="faint" style="font-size: var(--fs-xs)">查看详情 →</span>
          </div>
        </div>
      </div>

      <!-- 详情 + 动作 -->
      <div v-if="detailLoading" class="empty">加载提案详情…</div>
      <div v-else-if="selected" class="sub-card">
        <div class="sub-title">
          提案 #{{ selected.id }} 详情
          <span class="assist-hint">{{ statusMeta(selected.status).label }}</span>
        </div>

        <div class="row tight" style="align-items: center">
          <span class="tag" :class="statusMeta(selected.status).tag">
            {{ statusMeta(selected.status).label }}
          </span>
          <span class="tag" :class="riskMeta(selected.risk_level).tag">
            {{ riskMeta(selected.risk_level).label }}
          </span>
          <span class="tag mono">{{ selected.type }} → {{ selected.target_kind }}</span>
          <span class="tag mono">target_id={{ selected.target_id }}</span>
        </div>

        <div class="meta-grid">
          <div>
            <span class="blk-head">影响范围 impact</span>
            <div>{{ selected.impact || "—" }}</div>
          </div>
          <div>
            <span class="blk-head">基线哈希 base_content_hash</span>
            <div class="mono" style="font-size: var(--fs-xs)">
              {{ selected.base_content_hash || "—" }}
            </div>
          </div>
          <div>
            <span class="blk-head">创建</span>
            <div>{{ fmtTime(selected.created_at) }} · {{ selected.created_by || "—" }}</div>
          </div>
          <div>
            <span class="blk-head">批准</span>
            <div>{{ fmtTime(selected.approved_at) }} · {{ selected.approved_by || "—" }}</div>
          </div>
          <div>
            <span class="blk-head">生效</span>
            <div>{{ fmtTime(selected.applied_at) }}</div>
          </div>
          <div>
            <span class="blk-head">产生版本</span>
            <div>Version #{{ selected.version_id ?? "—" }}</div>
          </div>
        </div>

        <div v-if="selected.reason" class="blk">
          <span class="blk-head">理由 reason</span>
          <div
            class="prose snippet"
            :class="{ expanded: isOpen(`prop-reason-${selected.id}`) }"
          >{{ selected.reason }}</div>
          <button class="link-btn" @click="toggle(`prop-reason-${selected.id}`)">
            {{ isOpen(`prop-reason-${selected.id}`) ? "收起" : "展开" }}
          </button>
        </div>

        <!-- 应用前必看的差异 -->
        <div class="blk">
          <span class="blk-head">改动差异 diff（应用前必看）</span>
          <div v-if="!selected.diff" class="faint">该提案没有 diff 内容。</div>
          <template v-else>
            <pre
              class="diffbox snippet"
              :class="{ expanded: isOpen(`prop-diff-${selected.id}`) }"
            >{{ selected.diff }}</pre>
            <button class="link-btn" @click="toggle(`prop-diff-${selected.id}`)">
              {{ isOpen(`prop-diff-${selected.id}`) ? "收起" : "展开完整 diff" }}
            </button>
          </template>
        </div>

        <div class="blk">
          <span class="blk-head">改前全文 before_text（基线）</span>
          <pre
            class="diffbox snippet"
            :class="{ expanded: isOpen(`prop-before-${selected.id}`) }"
          >{{ selected.before_text }}</pre>
          <button class="link-btn" @click="toggle(`prop-before-${selected.id}`)">
            {{ isOpen(`prop-before-${selected.id}`) ? "收起" : "展开全文" }}
          </button>
        </div>

        <div class="blk">
          <span class="blk-head">改后全文 after_text</span>
          <pre
            class="diffbox snippet"
            :class="{ expanded: isOpen(`prop-after-${selected.id}`) }"
          >{{ selected.after_text }}</pre>
          <button class="link-btn" @click="toggle(`prop-after-${selected.id}`)">
            {{ isOpen(`prop-after-${selected.id}`) ? "收起" : "展开全文" }}
          </button>
        </div>

        <div class="field">
          <label for="asst-actor">操作人 actor（可选，写进审计）</label>
          <input id="asst-actor" v-model="actorName" class="input" placeholder="例如 我" />
        </div>

        <div class="row tight">
          <button
            class="btn primary"
            :disabled="!canApprove || actionBusy"
            @click="runAction('approve')"
          >
            批准（pending → approved）
          </button>
          <button class="btn" :disabled="!canReject || actionBusy" @click="runAction('reject')">
            拒绝（终态）
          </button>
          <button
            class="btn"
            :class="canApply ? 'primary' : ''"
            :disabled="!canApply || actionBusy"
            @click="confirmApply = true"
          >
            应用（approved → applied，会写入内容）
          </button>
          <span class="faint" style="font-size: var(--fs-xs)">
            批准与应用刻意分成两步：批准只是人的判断，应用才真的写库。
          </span>
        </div>

        <!-- 应用的二次确认 -->
        <div v-if="confirmApply" class="notice warn confirm-box">
          <div>
            确认应用提案 #{{ selected.id }}？<br />
            将按上面的 diff 把
            <span class="mono">{{ selected.target_kind }}:{{ selected.target_id }}</span>
            的当前内容替换为 after_text，并生成一条新版本快照。
          </div>
          <div class="row tight">
            <button class="btn primary" :disabled="actionBusy" @click="runAction('apply')">
              {{ actionBusy ? "应用中…" : "确认应用" }}
            </button>
            <button class="btn" :disabled="actionBusy" @click="confirmApply = false">取消</button>
          </div>
        </div>

        <div v-if="actionError" class="notice err confirm-box">
          <div>{{ actionError }}</div>
          <div v-if="staleHint" class="stale-box">
            <div class="faint mono" style="font-size: var(--fs-xs)">
              基线哈希 {{ shortHash(staleHint.expected) }} → 当前哈希
              {{ shortHash(staleHint.current) }}
            </div>
            <button class="btn" @click="rebuildFromStale">基于最新内容重新起草</button>
          </div>
        </div>
        <div v-if="actionNotice" class="notice ok">{{ actionNotice }}</div>
      </div>
    </div>

    <!-- ==================== ④ 版本与回滚 ==================== -->
    <div v-else class="pane">
      <div class="row tight" style="align-items: flex-end">
        <button class="btn" :disabled="versionLoading" @click="refreshVersions">
          {{ versionLoading ? "刷新中…" : "刷新版本列表" }}
        </button>
        <span class="faint" style="font-size: var(--fs-xs)">
          版本只增不改：回滚也会追加一条新版本，历史不会丢。
        </span>
      </div>

      <div v-if="versionError" class="notice err">{{ versionError }}</div>
      <div v-if="versionNotice" class="notice ok">{{ versionNotice }}</div>

      <div v-if="!versions.length" class="empty">还没有任何版本快照（应用提案后才会产生）。</div>
      <div v-else class="ver-list">
        <div v-for="v in versions" :key="v.id" class="ver">
          <div class="row tight" style="align-items: center">
            <span class="tag mono">Version #{{ v.id }}</span>
            <span class="tag accent mono">{{ v.kind }}</span>
            <span v-if="v.is_restore" class="tag warn">回滚产生</span>
            <span class="tag mono">提案 #{{ v.proposal_id ?? "—" }}</span>
            <span class="spacer"></span>
            <button class="btn sm" @click="restoreTarget = v">回滚到此版本…</button>
          </div>
          <div class="ver-meta">
            <span>创建 {{ fmtTime(v.created_at) }}</span>
            <span>来源提案 #{{ v.source_proposal_id ?? "—" }}</span>
            <span>{{ v.target_kind ?? "—" }}:{{ v.target_id ?? "—" }}</span>
            <span class="mono">hash {{ shortHash(v.content_hash) }}</span>
            <span>操作人 {{ v.created_by || "—" }}</span>
          </div>
          <div>
            <button class="link-btn" @click="toggle(`ver-hash-${v.id}`)">
              {{ isOpen(`ver-hash-${v.id}`) ? "收起完整哈希" : "查看完整哈希" }}
            </button>
            <div v-if="isOpen(`ver-hash-${v.id}`)" class="mono ver-hash">{{ v.content_hash }}</div>
          </div>
          <div v-if="v.reason" class="faint" style="font-size: var(--fs-xs)">
            理由：{{ v.reason }}
          </div>
        </div>
      </div>

      <!-- 回滚二次确认 -->
      <div v-if="restoreTarget" class="notice err confirm-box">
        <div>
          确认回滚到 Version #{{ restoreTarget.id }}？<br />
          当前
          <span class="mono">{{ restoreTarget.target_kind ?? "—" }}:{{ restoreTarget.target_id ?? "—" }}</span>
          的内容<strong>会被覆盖</strong>；回滚本身也会生成一条新 Version，所以历史不丢，
          但<strong>当前内容会被替换</strong>。
        </div>
        <div class="row tight">
          <button class="btn primary" :disabled="restoreBusy" @click="confirmRestore">
            {{ restoreBusy ? "回滚中…" : "确认回滚" }}
          </button>
          <button class="btn" :disabled="restoreBusy" @click="restoreTarget = null">取消</button>
        </div>
      </div>

      <!-- 版本对比 -->
      <div class="sub-card">
        <div class="sub-title">版本对比</div>
        <div class="row" style="align-items: flex-end">
          <div class="field" style="min-width: 160px">
            <label for="asst-diff-from">起始版本 from_id</label>
            <select id="asst-diff-from" class="input" :value="diffFrom ?? ''" @change="onDiffFromChange">
              <option v-for="v in versions" :key="v.id" :value="v.id">#{{ v.id }}</option>
            </select>
          </div>
          <div class="field" style="min-width: 160px">
            <label for="asst-diff-to">目标版本 to_id</label>
            <select id="asst-diff-to" class="input" :value="diffTo ?? ''" @change="onDiffToChange">
              <option v-for="v in versions" :key="v.id" :value="v.id">#{{ v.id }}</option>
            </select>
          </div>
          <button
            class="btn"
            :disabled="diffLoading || diffFrom === null || diffTo === null"
            @click="runDiff"
          >
            {{ diffLoading ? "对比中…" : "查看差异" }}
          </button>
        </div>
        <template v-if="diffText">
          <pre class="diffbox snippet" :class="{ expanded: isOpen('ver-diff') }">{{ diffText }}</pre>
          <button class="link-btn" @click="toggle('ver-diff')">
            {{ isOpen("ver-diff") ? "收起" : "展开完整 diff" }}
          </button>
        </template>
      </div>

      <!-- 提示词生效来源（端点 12 / 13） -->
      <div class="sub-card">
        <div class="sub-title">
          提示词覆写状态
          <span class="assist-hint">prompts/active · prompts/versions</span>
        </div>
        <div v-if="promptsError" class="notice err">{{ promptsError }}</div>
        <template v-else-if="promptsActive">
          <div class="row tight" style="align-items: center">
            <span class="tag" :class="promptsActive.has_override ? 'warn' : 'ok'">
              {{ promptsActive.has_override ? "存在运行时覆写" : "全部走代码默认" }}
            </span>
            <span class="tag mono">覆写历史 {{ promptOverrideCount ?? 0 }} 条</span>
          </div>
          <div class="ev-list">
            <div v-for="b in promptsActive.blocks" :key="b.prompt_key" class="ev">
              <div class="row tight" style="align-items: center">
                <span class="tag mono">{{ b.prompt_key }}</span>
                <span class="tag" :class="b.source === 'runtime_override' ? 'warn' : 'ok'">
                  {{ b.source === "runtime_override" ? "运行时覆写" : "代码默认" }}
                </span>
              </div>
              <div class="prose snippet" :class="{ expanded: isOpen(`pk-${b.prompt_key}`) }">
                {{ b.text || "（空）" }}
              </div>
              <button class="link-btn" @click="toggle(`pk-${b.prompt_key}`)">
                {{ isOpen(`pk-${b.prompt_key}`) ? "收起" : "展开内容" }}
              </button>
            </div>
          </div>
          <div class="faint" style="font-size: var(--fs-xs)">{{ promptsActive.note }}</div>
        </template>
      </div>
    </div>

    <!-- ==================== 即将开放（后端未就绪，不接假数据） ==================== -->
    <div class="sub-card soon">
      <div class="sub-title">
        助手运行日志 / 审核 / 调动生成
        <span class="tag warn">后端未就绪 · 即将开放</span>
      </div>
      <p class="faint" style="font-size: var(--fs-sm); margin: 0">
        以下能力已与后端定好契约、前端 api client 与类型也已就绪，但后端端点尚未合入：
        <span class="mono">GET /api/assistant/runs</span>（助手运行日志）、
        <span class="mono">POST /api/assistant/analyze-logs</span>（日志分析）、
        <span class="mono">POST /api/assistant/audit-corpus / audit-kg / audit-generation</span>（三类审核）、
        <span class="mono">POST /api/assistant/invoke-generation</span>（按你的显式指令调动 Writer 生成）。
        端点就绪后直接接线；在当前后端上这里不会发送任何请求，也不会展示模拟数据。
      </p>
    </div>
  </section>
</template>

<style scoped>
.assistant {
  margin-top: var(--gap);
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
.assist-hint {
  font-size: var(--fs-xs);
  font-weight: 400;
  color: var(--fg-faint);
  font-family: var(--font-mono);
}
.pane {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
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
.chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.chip {
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  padding: 8px 14px;
  border-radius: var(--radius-full);
  border: var(--border-w) solid var(--border-strong);
  background: var(--bg);
  color: var(--fg);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.chip:hover {
  border-color: var(--accent);
  color: var(--accent-ink);
}
.chip.on {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-fg);
}
.spacer {
  flex: 1;
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
  background: var(--bg-sunken);
}
.qa-out,
.diag-out {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
/* 长文本默认限高可展开：既不让面板被撑爆，也不把内容憋在小框里。 */
.snippet {
  max-height: 7.5em;
  overflow: hidden;
}
.snippet.expanded {
  max-height: none;
}
.rules-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}
.sev-groups {
  display: flex;
  flex-direction: column;
  gap: var(--gap-lg);
}
.sev-group {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.sev-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.issue {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border: var(--border-w) solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
.sub-card {
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  padding: var(--gap);
  display: flex;
  flex-direction: column;
  gap: var(--gap);
  background: var(--bg);
}
.soon {
  border-style: dashed;
  background: var(--bg-sunken);
}
.sub-title {
  font-size: var(--fs-md);
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap);
  flex-wrap: wrap;
}
.draft-warn {
  line-height: 1.7;
}
.prop-list {
  display: flex;
  flex-direction: column;
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
.stale-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
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
.ver-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ver {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px 14px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
}
.ver-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.ver-hash {
  font-size: var(--fs-xs);
  word-break: break-all;
  color: var(--fg-faint);
}
@media (max-width: 720px) {
  .meta-grid {
    grid-template-columns: 1fr;
  }
}
</style>
