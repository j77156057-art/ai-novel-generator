<script setup lang="ts">
/**
 * 写作面板（右侧抽屉）。
 *
 * ## 为什么要有它
 *
 * 此前「调动生成」的卡片点确认后，正文是在助手的同步接口里写完才回来的：
 * 聊天流里既看不到写作过程，生成结果又和对话混在一起。这里把**写作**从
 * **对话**里拆出来：聊天只留一张卡片，真正的正文在独立面板里流式生成、
 * 逐段查看、就地修改。
 *
 * ## 数据从哪来
 *
 * - 正文 / 段列表：`GET /generations/{project_id}`（真实落库的段）
 * - 设定 / 剧情：`GET /projects/{id}` 的 plot_bible 与 outline
 * - 世界线：`GET /projects/{id}/timeline`（引擎存档的节点，只读）
 * - 图谱：`GET /projects/{id}/kg`（只读概览）
 *
 * 面板不编造任何内容：接口取不到就显示可读错误，不画空白占位。
 *
 * ## 修改正文后的级联
 *
 * 保存走 `PATCH /generations/{project_id}/{id}`：后端会重算剧情总结、
 * 重建摘要树与图谱、把 v3 批抽重新入队。面板只如实展示 `cascade` 的
 * 执行结果（成功项 / 失败原因），失败也不掩盖「正文已保存」这个事实。
 */
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { api, planGateStatus } from "@/api/client";
import type { PlanGateStatus } from "@/api/client";
import { errorText as textOf } from "@/utils/errors";
import type {
  GeneratePlan,
  GenerationOut,
  GenerationEditCascade,
  Graph,
  PlotBible,
  PlotTimelineState,
  ProjectOut,
} from "@/api/types";

const props = withDefaults(
  defineProps<{
    projectId: number;
    /** 抽屉是否可见。关闭只是隐藏，不中断正在跑的生成。 */
    open?: boolean;
    /** 打开时定位到哪一段；null = 最新一段。 */
    initialSegmentNo?: number | null;
    /** view = 只看；continue = 打开后立即续写；regenerate = 立即重生成定位段。 */
    mode?: "view" | "continue" | "regenerate";
    /** 单调递增：同一面板再次从卡片打开时用它触发新一轮生成。 */
    runKey?: number;
  }>(),
  {
    open: true,
    initialSegmentNo: null,
    mode: "view",
    runKey: 0,
  },
);

const emit = defineEmits<{
  (e: "close"): void;
  /** 正文 / 摘要 / 图谱可能已变化，调用方应刷新段数与工序链。 */
  (e: "updated"): void;
  (e: "generated", payload: { segment_no: number; chars: number }): void;
  (e: "failed", message: string): void;
  /** 生成进度（聊天卡片画进度条用）：已收到字数 / 已等待秒数 / 目标字数 / 思考字数。 */
  (e: "progress", payload: {
    chars: number;
    seconds: number;
    target: number;
    reasoning: number;
    /** 作者 Agent 当前阶段（旧后端不发 stage 时为 undefined）。 */
    stage?: string;
    /** 当前阶段的人话标签，如「模型思考」。 */
    stageLabel?: string;
  }): void;
}>();

type TabKey = "text" | "setting" | "plot" | "timeline" | "kg" | "context";

const router = useRouter();

const TAB_LABELS: { key: TabKey; label: string }[] = [
  { key: "text", label: "正文" },
  { key: "setting", label: "设定" },
  { key: "plot", label: "剧情" },
  { key: "timeline", label: "世界线" },
  { key: "kg", label: "图谱" },
  { key: "context", label: "上下文" },
];

// ==================================================================
// 基础数据
// ==================================================================

const project = ref<ProjectOut | null>(null);
const segments = ref<GenerationOut[]>([]);
const loading = ref(true);
const loadError = ref("");

const tab = ref<TabKey>("text");

const currentSegmentNo = ref<number | null>(null);
const draftText = ref("");
const savedText = ref("");

const saving = ref(false);
const saveNote = ref("");
const saveError = ref("");

const generating = ref(false);
const genMode = ref<"continue" | "regenerate" | null>(null);

/** 伏笔节奏开关：off=不注入 / auto=按规划器现算 / manual=按台账作者设置。 */
type ForeshadowMode = "auto" | "manual" | "off";
const FORESHADOW_MODE_KEY = "writer.foreshadowMode";
const foreshadowMode = ref<ForeshadowMode>(
  (() => {
    const saved = localStorage.getItem(FORESHADOW_MODE_KEY);
    return saved === "off" || saved === "manual" || saved === "auto" ? saved : "auto";
  })(),
);
function saveForeshadowMode(): void {
  localStorage.setItem(FORESHADOW_MODE_KEY, foreshadowMode.value);
}
const streamText = ref("");
/** 模型思考增量：chars 用于解释「为什么还没出字」，text 供作者查看内容。 */
const reasoningChars = ref(0);
const reasoningText = ref("");

// ---- 装配上下文（只读预览） ------------------------------------------
const contextLoading = ref(false);
const contextError = ref("");
const contextPlan = ref<GeneratePlan | null>(null);

// ---- 作者 Agent 轨迹 -------------------------------------------------
// 后端在生成流的五个节点各发一次 `stage`：装配上下文 / 等待首字 / 模型思考 /
// 流式撰写 / 重算摘要与图谱。这里把它们排成一条可读的执行轨迹，替代旧版
// 「只有一个思考字数」的干等展示；每步都记真实起止时刻，耗时不编造。
type AgentStageKey = "prepare" | "model" | "thinking" | "writing" | "post";

interface AgentStep {
  key: AgentStageKey;
  label: string;
  detail: string;
  state: "idle" | "active" | "done" | "error";
  startedAt: number | null;
  endedAt: number | null;
}

const AGENT_STAGE_ORDER: AgentStageKey[] = [
  "prepare",
  "model",
  "thinking",
  "writing",
  "post",
];

const AGENT_STAGE_LABELS: Record<AgentStageKey, string> = {
  prepare: "装配上下文",
  model: "等待模型首字",
  thinking: "模型思考",
  writing: "流式撰写",
  post: "重算摘要与图谱",
};

const agentSteps = ref<AgentStep[]>([]);
const agentStage = ref<AgentStageKey | null>(null);

const agentHeadline = computed(() => {
  const active = agentSteps.value.find((step) => step.state === "active");
  if (active) return active.label;
  return generating.value ? "准备中" : "已完成";
});

function resetAgentSteps(): void {
  agentSteps.value = AGENT_STAGE_ORDER.map((key) => ({
    key,
    label: AGENT_STAGE_LABELS[key],
    detail: "",
    state: "idle",
    startedAt: null,
    endedAt: null,
  }));
  agentStage.value = null;
}

/** 收到一个阶段事件：之前的阶段收尾，本阶段开始计时。 */
function applyAgentStage(stage: string, label: string): void {
  const key = stage as AgentStageKey;
  const idx = AGENT_STAGE_ORDER.indexOf(key);
  if (idx < 0) return;
  if (!agentSteps.value.length) resetAgentSteps();
  const now = Date.now();
  agentSteps.value = agentSteps.value.map((step, i) => {
    if (i < idx) {
      if (step.state === "active") return { ...step, state: "done", endedAt: now };
      if (step.state === "idle") {
        return { ...step, state: "done", endedAt: now, detail: step.detail || "已跳过" };
      }
      return step;
    }
    if (i > idx) return step;
    if (step.state === "active") return { ...step, label: label || step.label };
    return {
      ...step,
      label: label || step.label,
      state: "active",
      startedAt: now,
      endedAt: null,
    };
  });
  agentStage.value = key;
  emitProgress();
}

/** 流结束（成功 / 失败）：收尾仍在进行中的那一步，不伪造未执行的步骤。 */
function finishAgentSteps(failed: boolean, note: string): void {
  const now = Date.now();
  agentSteps.value = agentSteps.value.map((step) => {
    if (step.state !== "active") return step;
    return {
      ...step,
      state: failed ? "error" : "done",
      endedAt: now,
      detail: step.detail || note,
    };
  });
  if (!failed) agentStage.value = null;
  emitProgress();
}

/** 单步耗时：进行中的用本地时钟实时长，已结束的用真实起止差。 */
function stepSeconds(step: AgentStep): string {
  if (step.startedAt === null) return "";
  const end = step.endedAt ?? (generating.value ? genNow.value : step.startedAt);
  return `${Math.max(0, Math.round((end - step.startedAt) / 1000))}s`;
}

const genNote = ref("");
const genError = ref("");
/** 生成被时间线闸门拒绝时的状态；非空即渲染一键动作。 */
const genFix = ref<PlanGateStatus | null>(null);
const genFixBusy = ref(false);
/** 记住失败的是「续写」还是「重生成」，重规划成功后重试同一个动作。 */
const lastGenAction = ref<"continue" | "regenerate">("continue");
const confirmRegen = ref(false);
/** 本次生成开始时刻 / 每秒刷新，用于「已等待 N 秒」而不是看起来卡死。 */
const genStartedAt = ref<number | null>(null);
const genNow = ref(Date.now());
let genClock: number | null = null;
/** 取消当前流：关闭面板或点「停止等待」时 abort。 */
let genController: AbortController | null = null;

const genWaitSeconds = computed(() =>
  genStartedAt.value === null
    ? 0
    : Math.max(0, Math.round((genNow.value - genStartedAt.value) / 1000)),
);

/** 进度条分母：续写用项目目标字数，重生成用被重写段落的原字数。 */
const genTargetChars = computed(() => {
  if (genMode.value === "regenerate") {
    const old = (currentSegment.value?.text ?? "").length;
    if (old > 0) return old;
  }
  return Math.max(300, Number(project.value?.generation_length) || 3000);
});

function emitProgress(): void {
  // 撰写阶段的步内细节就是实时字数：让轨迹与进度条读同一份数据。
  const writingStep = agentSteps.value.find((step) => step.key === "writing");
  if (writingStep && agentStage.value === "writing") {
    writingStep.detail = `${streamChars.value} 字`;
  }
  emit("progress", {
    chars: streamChars.value,
    seconds: genWaitSeconds.value,
    target: genTargetChars.value,
    reasoning: reasoningChars.value,
    stage: agentStage.value ?? undefined,
    stageLabel: agentHeadline.value,
  });
}

function startGenClock(): void {
  genStartedAt.value = Date.now();
  genNow.value = genStartedAt.value;
  emitProgress();
  if (genClock !== null) window.clearInterval(genClock);
  genClock = window.setInterval(() => {
    if (generating.value) {
      genNow.value = Date.now();
      emitProgress();
    }
  }, 1000);
}

function stopGenClock(): void {
  if (genClock !== null) {
    window.clearInterval(genClock);
    genClock = null;
  }
  genStartedAt.value = null;
}

/** 主动停止等待：连接断开后后端会在下一次 yield 时释放租约。 */
function cancelGeneration(): void {
  genController?.abort();
}

const timeline = ref<PlotTimelineState | null>(null);
const timelineLoading = ref(false);
const timelineError = ref("");
const timelinePlanning = ref(false);
const timelinePlanNote = ref("");

const kg = ref<Graph | null>(null);
const kgLoading = ref(false);
const kgError = ref("");
const kgRebuilding = ref(false);
const kgRebuildConfirm = ref(false);
const kgRebuildNote = ref("");
/**
 * 图谱来源：默认只看**生成正文**这一侧。
 * `/kg` 不带 source 时会把参考语料图谱一起并进来（几千个节点），
 * 看起来就像「抽的是原语料」；写作面板默认不应展示那份。
 */
const kgSource = ref<"generated" | "all">("generated");

const currentSegment = computed(
  () => segments.value.find((s) => s.segment_no === currentSegmentNo.value) ?? null,
);
const dirty = computed(() => draftText.value !== savedText.value);
const currentChars = computed(() => draftText.value.length);
const streamChars = computed(() => streamText.value.length);

function isAbort(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

function segmentChars(seg: GenerationOut): number {
  return (seg.text ?? "").length;
}

function summaryText(summary: unknown): string {
  if (typeof summary === "string") return summary;
  if (summary && typeof summary === "object") {
    const text = (summary as Record<string, unknown>).text;
    if (typeof text === "string") return text;
    return JSON.stringify(summary);
  }
  return "";
}

// ==================================================================
// 读取
// ==================================================================

async function loadProject(): Promise<void> {
  project.value = await api.getProject(props.projectId);
}

async function loadSegments(): Promise<void> {
  const list = await api.listGenerations(props.projectId);
  segments.value = [...list].sort((a, b) => a.segment_no - b.segment_no);
}

function selectSegment(no: number | null): void {
  const target =
    no !== null && segments.value.some((s) => s.segment_no === no)
      ? no
      : segments.value.length
        ? segments.value[segments.value.length - 1].segment_no
        : null;
  currentSegmentNo.value = target;
  const seg = segments.value.find((s) => s.segment_no === target) ?? null;
  draftText.value = seg?.text ?? "";
  savedText.value = seg?.text ?? "";
  saveNote.value = "";
  saveError.value = "";
  confirmRegen.value = false;
}

async function bootstrap(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    await Promise.all([loadProject(), loadSegments()]);
    selectSegment(props.initialSegmentNo);
  } catch (err) {
    loadError.value = `读取失败：${textOf(err)}`;
  } finally {
    loading.value = false;
  }
}

async function loadTimeline(): Promise<void> {
  if (timelineLoading.value) return;
  timelineLoading.value = true;
  timelineError.value = "";
  try {
    timeline.value = await api.getTimeline(props.projectId);
  } catch (err) {
    timelineError.value = `世界线读取失败：${textOf(err)}`;
  } finally {
    timelineLoading.value = false;
  }
}

async function loadKg(): Promise<void> {
  if (kgLoading.value) return;
  kgLoading.value = true;
  kgError.value = "";
  try {
    // source="all" 不是后端的合法来源值；「全部」= 不传 source。
    kg.value = await api.getKg(
      props.projectId,
      "all",
      kgSource.value === "generated" ? "generated" : undefined,
    );
  } catch (err) {
    kgError.value = `图谱读取失败：${textOf(err)}`;
  } finally {
    kgLoading.value = false;
  }
}

/** 切换图谱来源：生成正文 / 全部（含参考语料）。 */
function switchKgSource(source: "generated" | "all"): void {
  if (kgSource.value === source) return;
  kgSource.value = source;
  kg.value = null;
  void loadKg();
}

/**
 * 读「下一次生成实际会发给模型的上下文」（只组装、不调模型）。
 *
 * 每次进入都重读：当前段一变，前情/世界线/检索都会变，缓存只会给出过期答案。
 */
async function loadContext(): Promise<void> {
  if (contextLoading.value) return;
  contextLoading.value = true;
  contextError.value = "";
  try {
    contextPlan.value = await api.generatePlan(props.projectId);
  } catch (err) {
    contextError.value = `上下文读取失败：${textOf(err)}`;
  } finally {
    contextLoading.value = false;
  }
}

function switchTab(key: TabKey): void {
  tab.value = key;
  if (key === "timeline" && timeline.value === null) void loadTimeline();
  if (key === "kg" && kg.value === null) void loadKg();
  if (key === "context") void loadContext();
}

/** 正文改了，图谱就可能变：丢掉旧快照，正停在图谱页时立刻重读。 */
function invalidateKg(): void {
  kg.value = null;
  if (tab.value === "kg") void loadKg();
}

/** 让模型把占位/不完整的世界线规划成真实剧情点。需要先有章纲。 */
async function planTimeline(): Promise<void> {
  if (timelinePlanning.value) return;
  timelinePlanning.value = true;
  timelineError.value = "";
  timelinePlanNote.value = "";
  try {
    const res = await api.planTimeline(props.projectId, true);
    timeline.value = res.timeline;
    timelinePlanNote.value = `已用模型规划 ${res.replaced} 个剧情点；人工追加的节点保留。`;
    emit("updated");
  } catch (err) {
    timelineError.value = `规划失败：${textOf(err)}`;
  } finally {
    timelinePlanning.value = false;
  }
}

const genFixLabel = computed(() => {
  if (genFix.value === "needs_planning") return "重新规划并重试";
  if (genFix.value === "needs_review") return "查看世界线";
  if (genFix.value === "plan_exhausted") return "去扩展章纲";
  return "";
});

/**
 * 时间线闸门的一键动作。
 *
 * - needs_planning：现场重规划，成功后**自动重试**刚才失败的那次生成；
 * - needs_review：切到世界线页，让人看见「预算用尽但退出条件未证实」；
 * - plan_exhausted：规划里的点写完了，去章纲页扩展故事范围。
 */
async function runGenFix(): Promise<void> {
  const fix = genFix.value;
  if (!fix || genFixBusy.value) return;
  if (fix === "needs_review") {
    switchTab("timeline");
    return;
  }
  if (fix === "plan_exhausted") {
    genFix.value = null;
    router.push(`/projects/${props.projectId}/outline`);
    return;
  }
  genFixBusy.value = true;
  try {
    await planTimeline();
    if (timelineError.value) {
      genError.value = timelineError.value;
      return;
    }
    genError.value = "";
    genFix.value = null;
    if (lastGenAction.value === "regenerate") await runRegenerate();
    else await runContinue();
  } finally {
    genFixBusy.value = false;
  }
}

/** 从已落库正文全量重建图谱（二次确认，避免误触覆盖现有图谱）。 */
async function rebuildKg(skipConfirm = false): Promise<void> {
  if (kgRebuilding.value) return;
  if (!skipConfirm && !kgRebuildConfirm.value) {
    kgRebuildConfirm.value = true;
    return;
  }
  kgRebuildConfirm.value = false;
  kgRebuilding.value = true;
  kgError.value = "";
  kgRebuildNote.value = "";
  try {
    const res = await api.buildKgFromGenerations(props.projectId);
    const segments = res.segments ?? res.replayed;
    const failed = res.failed?.length ?? 0;
    if (res.rebuilt === false) {
      kgError.value = `重建失败：${res.reason || "后端未返回原因"}`;
    } else {
      kgRebuildNote.value =
        `已从正文重建图谱${segments != null ? `（${segments} 段）` : ""}` +
        (failed ? `，但有 ${failed} 段失败。` : "。");
      await loadKg();
      emit("updated");
    }
  } catch (err) {
    kgError.value = `重建失败：${textOf(err)}`;
  } finally {
    kgRebuilding.value = false;
  }
}

// ==================================================================
// 缺失项体检：助手先看缺什么，再把「补哪几项」交给用户勾选
// ==================================================================

type GapKind = "auto" | "jump" | "info";
type GapAutoAction =
  | "plan_timeline"
  | "rebuild_kg"
  | "sync_story_cards"
  | "import_foreshadows"
  | "import_foreshadows_kg";

interface GapItem {
  key: string;
  group: string;
  title: string;
  detail: string;
  /** auto = 面板内直接执行；jump = 去对应页面；info = 只提示、无自动动作。 */
  kind: GapKind;
  action?: GapAutoAction;
  path?: string;
  selected: boolean;
  disabled: boolean;
}

const gaps = ref<GapItem[]>([]);
const gapDialogOpen = ref(false);
const gapChecking = ref(false);
const gapRunning = ref(false);
const gapResults = ref<string[]>([]);
const gapSelectable = computed(
  () => gaps.value.filter((g) => g.selected && !g.disabled).length,
);

function gapStorageKey(): string {
  return `wp-gap-dismissed-${props.projectId}`;
}

/** 把一段体检结果按固定顺序落进列表；未勾选的 info 不显示复选框。 */
function pushGap(
  out: GapItem[],
  item: Omit<GapItem, "selected" | "disabled"> & { selected?: boolean },
): void {
  out.push({
    ...item,
    selected: item.selected ?? item.kind !== "info",
    disabled: item.kind === "info",
  });
}

/**
 * 全量体检：能查到的项目资产都查一遍，不只章纲/世界线/图谱。
 *
 * 判据只读真实数据；任何一个接口失败都按「未知」跳过该项，不制造假缺失。
 */
async function detectGaps(): Promise<GapItem[]> {
  gapChecking.value = true;
  try {
    const proj = project.value;
    const [t, k, cardsRes, foreRes, jobsRes, workerRes, bgRes, outlineRes] =
      await Promise.all([
        api.getTimeline(props.projectId).catch(() => null),
        // 体检只看生成正文这一侧：参考语料图谱不能用来判断「正文缺图谱」。
        api.getKg(props.projectId, "all", "generated").catch(() => null),
        api.listStoryCards(props.projectId).catch(() => null),
        api.listForeshadows(props.projectId).catch(() => null),
        api.listDistillJobs(props.projectId, 1).catch(() => null),
        api.kgV3WorkerStatus(props.projectId).catch(() => null),
        api.getBackground(props.projectId).catch(() => null),
        api.listChapterOutline(props.projectId).catch(() => null),
      ]);
    if (t) timeline.value = t;
    if (k && (kgSource.value === "generated" || !kg.value)) kg.value = k;

    const bible = proj?.plot_bible ?? null;
    const chars = bible?.characters ?? [];
    const relations = bible?.relationships ?? [];
    const conflicts = bible?.conflicts ?? [];
    const foreshadowing = bible?.foreshadowing ?? [];
    const projectPath = `/projects/${props.projectId}`;
    const outlinePath = `${projectPath}/outline`;
    const kgPath = `${projectPath}/kg`;
    const refs = proj?.reference_novel_ids ?? [];
    const out: GapItem[] = [];

    // ---------- 基础设定 ----------
    if (!proj?.genre) {
      pushGap(out, {
        key: "genre", group: "基础", title: "题材未定",
        detail: "项目没有题材，提示词无法注入题材约束。",
        kind: "jump", path: projectPath,
      });
    }
    if (!proj?.premise && !bible?.synopsis) {
      pushGap(out, {
        key: "premise", group: "基础", title: "故事梗概缺失",
        detail: "一句话设定和剧情梗概都为空；去项目页补一段核心故事。",
        kind: "jump", path: projectPath,
      });
    }
    if (refs.length && !proj?.style_narrative) {
      const job = jobsRes?.items?.[0];
      const running = job && (job.status === "pending" || job.status === "running");
      pushGap(out, {
        key: "style_narrative", group: "文风",
        title: running ? "文风档案提炼中" : "文风档案缺失",
        detail: running
          ? `已选 ${refs.length} 本参考书，提炼任务还在跑；完成前生成会被拦下。`
          : `已选 ${refs.length} 本参考书，但还没有文风档案；直接生成会被拦下。`,
        kind: "jump", path: projectPath,
      });
    }

    // ---------- 人物 ----------
    if (!chars.length) {
      pushGap(out, {
        key: "characters", group: "人物", title: "角色缺失",
        detail: "剧情圣经里没有任何角色，角色约束与关系图谱都无从生成。",
        kind: "jump", path: projectPath,
      });
    } else if (chars.length >= 2 && !relations.length) {
      pushGap(out, {
        key: "relationships", group: "人物", title: "人物关系缺失",
        detail: `已有 ${chars.length} 个角色，但关系清单为空。`,
        kind: "jump", path: projectPath,
      });
    }
    if (chars.length && cardsRes && !cardsRes.cards.length) {
      pushGap(out, {
        key: "story_cards", group: "人物", title: "角色设定卡未生成",
        detail: `剧情圣经有 ${chars.length} 个角色，但设定卡为空；可从圣经/图谱同步一次。`,
        kind: "auto", action: "sync_story_cards",
      });
    }

    // ---------- 世界观 ----------
    if (bgRes && !bgRes.background?.["一句话世界"]) {
      pushGap(out, {
        key: "background", group: "世界观", title: "世界背景缺失",
        detail: "一句话世界、硬规则、势力与地点都为空。",
        kind: "jump", path: projectPath,
      });
    }

    // ---------- 剧情 ----------
    if (!outlineEntries.value.length) {
      pushGap(out, {
        key: "outline", group: "剧情", title: "章纲缺失",
        detail: "世界线规划和按章生成都依赖章纲；先去写至少一章。",
        kind: "jump", path: outlinePath,
      });
    }
    if (!conflicts.length) {
      pushGap(out, {
        key: "conflicts", group: "剧情", title: "主线冲突缺失",
        detail: "剧情圣经的冲突清单为空，生成时缺少核心矛盾约束。",
        kind: "jump", path: projectPath,
      });
    }
    if (foreRes?.legacy?.length) {
      pushGap(out, {
        key: "foreshadow_legacy", group: "剧情", title: "旧伏笔未进台账",
        detail: `剧情圣经里有 ${foreRes.legacy.length} 条伏笔还没登记进台账。`,
        kind: "auto", action: "import_foreshadows",
      });
    } else if (foreRes?.kg?.importable?.length && !foreRes.entries.length) {
      pushGap(out, {
        key: "foreshadow_kg", group: "剧情", title: "图谱伏笔未登记",
        detail: `图谱里有 ${foreRes.kg.importable.length} 个未填坑，可一键登记进台账。`,
        kind: "auto", action: "import_foreshadows_kg",
      });
    }
    const missingOutline = outlineRes?.entries?.filter((e) => e.status === "missing").length ?? 0;
    if (missingOutline) {
      pushGap(out, {
        key: "chapter_outline_missing", group: "剧情",
        title: `已写章缺章纲（${missingOutline} 章）`,
        detail: "这些章已经写了正文却没有章纲，后续章节趋势会失真。",
        kind: "jump", path: outlinePath,
      });
    }
    if (foreshadowing.length === 0 && conflicts.length > 0 && !foreRes?.entries?.length) {
      pushGap(out, {
        key: "foreshadowing", group: "剧情", title: "伏笔缺失",
        detail: "剧情圣经没有伏笔，台账也为空；长线故事容易缺少可回收的钩子。",
        kind: "jump", path: projectPath,
      });
    }

    // ---------- 正文 ----------
    if (!segments.value.length) {
      pushGap(out, {
        key: "segments", group: "正文", title: "还没有正文",
        detail: "在「正文」标签点「续写下一段」开始写第一段。",
        kind: "info",
      });
    } else {
      const noSummary = segments.value.filter((s) => !summaryText(s.summary)).length;
      if (noSummary) {
        pushGap(out, {
          key: "summary_missing", group: "正文",
          title: `有 ${noSummary} 段缺剧情摘要`,
          detail: "摘要缺失会让后续续写接不上；保存该段正文可触发重算。",
          kind: "info",
        });
      }
    }

    // ---------- 派生数据 ----------
    if (outlineEntries.value.length && (!t || !t.exists || t.planning_incomplete)) {
      const stale = !!t?.exists;
      pushGap(out, {
        key: "timeline", group: "派生", title: stale ? "世界线尚未补全" : "世界线未规划",
        detail: stale
          ? "仍是占位节点；可让模型按现有章纲补全剧情点。"
          : "已有章纲，可以直接让模型规划主干剧情点。",
        kind: "auto", action: "plan_timeline",
      });
    }
    if (segments.value.length && k && !k.nodes.length && !k.edges.length) {
      pushGap(out, {
        key: "kg", group: "派生", title: "图谱为空",
        detail: `已有 ${segments.value.length} 段正文，可从正文重建人物/关系/事件图谱。`,
        kind: "auto", action: "rebuild_kg",
      });
    } else if (segments.value.length >= 2 && k && k.nodes.length && !k.edges.length) {
      pushGap(out, {
        key: "kg_edges", group: "派生", title: "图谱没有任何关系边",
        detail: `有 ${k.nodes.length} 个节点但 0 条边，多半是抽取不完整；可重建一次。`,
        kind: "auto", action: "rebuild_kg",
      });
    }
    const failedExtract =
      (workerRes?.summary?.failed ?? 0) + (workerRes?.summary?.stuck ?? 0);
    if (failedExtract > 0) {
      pushGap(out, {
        key: "kg_v3_failed", group: "派生",
        title: `图谱抽取有 ${failedExtract} 段失败/卡住`,
        detail: "去图谱页查看失败明细并重捞；不处理会让后续一致性检查缺数据。",
        kind: "jump", path: kgPath,
      });
    }
    const job = jobsRes?.items?.[0];
    if (job && (job.status === "failed" || job.health === "stalled")) {
      pushGap(out, {
        key: "distill_failed", group: "文风", title: "上一次文风提炼未完成",
        detail: job.health_note || "提炼任务失败或疑似卡住；去项目页重试。",
        kind: "jump", path: projectPath,
      });
    }
    if (refs.length && !(proj?.reference_cutoffs?.length)) {
      pushGap(out, {
        key: "reference_cutoffs", group: "文风", title: "参考书未设阅读边界",
        detail: "角色对话/续写会读到整本参考书，可能剧透；去项目页填写「故事进行到第几章」。",
        kind: "jump", path: projectPath,
      });
    }

    gaps.value = out;
    return out;
  } finally {
    gapChecking.value = false;
  }
}

/** 打开体检弹窗（会重新探测一次，避免用旧数据做决定）。 */
async function openGapDialog(): Promise<void> {
  gapResults.value = [];
  await detectGaps();
  gapDialogOpen.value = true;
}

/** 关闭：本次会话不再自动弹（用户已经看过并选择稍后）。 */
function dismissGapDialog(): void {
  gapDialogOpen.value = false;
  try {
    sessionStorage.setItem(gapStorageKey(), "1");
  } catch {
    /* 隐私模式禁用 sessionStorage 时忽略 */
  }
}

/** 执行勾选项：自动项顺序跑，跳转项在最后导航到对应页面。 */
async function runSelectedGaps(): Promise<void> {
  const picked = gaps.value.filter((g) => g.selected && !g.disabled);
  if (!picked.length || gapRunning.value) return;
  gapRunning.value = true;
  gapResults.value = [];
  try {
    for (const gap of picked) {
      if (gap.kind !== "auto" || !gap.action) continue;
      try {
        if (gap.action === "plan_timeline") {
          await planTimeline();
          gapResults.value.push(timelineError.value || timelinePlanNote.value || "世界线已处理。");
        } else if (gap.action === "rebuild_kg") {
          await rebuildKg(true);
          gapResults.value.push(kgError.value || kgRebuildNote.value || "图谱已处理。");
        } else if (gap.action === "sync_story_cards") {
          const res = await api.syncStoryCards(props.projectId);
          gapResults.value.push(`设定卡已同步：新增 ${res.created} 张。`);
        } else if (gap.action === "import_foreshadows") {
          const res = await api.importForeshadows(props.projectId);
          gapResults.value.push(`伏笔台账已导入：新增 ${res.created} 条。`);
        } else if (gap.action === "import_foreshadows_kg") {
          const res = await api.importForeshadowsFromKg(props.projectId);
          gapResults.value.push(`图谱伏笔已登记：新增 ${res.created} 条。`);
        }
        if (gap.action !== "plan_timeline" && gap.action !== "rebuild_kg") {
          emit("updated");
        }
      } catch (err) {
        gapResults.value.push(`${gap.title}：${textOf(err)}`);
      }
    }
    const jump = picked.find((g) => g.kind === "jump" && g.path);
    if (jump) {
      gapDialogOpen.value = false;
      router.push(jump.path!);
      return;
    }
    await detectGaps();
    if (!gaps.value.filter((g) => g.kind !== "info").length) {
      gapResults.value.push("可处理的缺失项已全部完成。");
    }
  } finally {
    gapRunning.value = false;
  }
}

/** 打开面板后自动体检一次：只在「纯查看」时弹，生成任务中不打断。 */
async function autoCheckGaps(): Promise<void> {
  if (props.mode !== "view") return;
  try {
    if (sessionStorage.getItem(gapStorageKey()) === "1") {
      await detectGaps();
      return;
    }
  } catch {
    /* 忽略 */
  }
  const found = await detectGaps();
  if (found.length) gapDialogOpen.value = true;
}

// ==================================================================
// 导航
// ==================================================================

const segmentIndex = computed(() =>
  segments.value.findIndex((s) => s.segment_no === currentSegmentNo.value),
);

function step(delta: number): void {
  const i = segmentIndex.value;
  if (i < 0) return;
  const next = segments.value[i + delta];
  if (next) selectSegment(next.segment_no);
}

// ==================================================================
// 保存（级联重算由后端完成）
// ==================================================================

function cascadeText(cascade: GenerationEditCascade): string {
  if (cascade.skipped) return "正文没有变化，未触发任何重算。";
  const done: string[] = [];
  if (cascade.summary) done.push("剧情总结");
  if (cascade.tree) done.push("摘要树");
  if (cascade.kg) done.push("图谱");
  if (cascade.v3_enqueued) done.push("v3 批抽（已入队）");
  const base = done.length
    ? `已保存；${done.join(" / ")} 已随之更新。`
    : "已保存；后端未声明配套重算结果。";
  if (cascade.errors?.length) {
    return `${base} 但有 ${cascade.errors.length} 项没跑成：${cascade.errors.join("；")}`;
  }
  return base;
}

async function save(): Promise<void> {
  const seg = currentSegment.value;
  if (!seg || saving.value || !dirty.value) return;
  saving.value = true;
  saveNote.value = "";
  saveError.value = "";
  try {
    const res = await api.updateGenerationText(
      props.projectId,
      seg.id,
      draftText.value,
    );
    savedText.value = draftText.value;
    saveNote.value = cascadeText(res.cascade);
    await loadSegments();
    // 摘要 / 段列表可能刚变：定位保持在同一段。
    selectSegment(seg.segment_no);
    invalidateKg();
    emit("updated");
  } catch (err) {
    saveError.value = `保存失败：${textOf(err)}`;
  } finally {
    saving.value = false;
  }
}

// ==================================================================
// 生成（流式；正文只进本面板）
// ==================================================================

async function runContinue(): Promise<void> {
  if (generating.value) return;
  tab.value = "text";
  generating.value = true;
  genMode.value = "continue";
  streamText.value = "";
  reasoningChars.value = 0;
  reasoningText.value = "";
  genNote.value = "";
  genError.value = "";
  genFix.value = null;
  resetAgentSteps();
  const landedRef = ref<{ segment_no: number; chars: number } | null>(null);
  const controller = new AbortController();
  genController = controller;
  startGenClock();
  try {
    await api.generateStream(
      props.projectId,
      null,
      (text) => {
        streamText.value += text;
      },
      (meta) => {
        landedRef.value = { segment_no: meta.segment_no, chars: meta.chars };
        genNote.value = `第 ${meta.segment_no} 段已落库 · ${meta.chars} 字。`;
      },
      controller.signal,
      (chars, text) => {
        reasoningChars.value = chars;
        if (text) reasoningText.value += text;
      },
      (ev) => {
        applyAgentStage(ev.stage, ev.label);
      },
      foreshadowMode.value,
    );
    finishAgentSteps(false, genNote.value || "生成完成");
    const landed = landedRef.value;
    if (landed) {
      await loadSegments();
      selectSegment(landed.segment_no);
      saveNote.value = genNote.value;
      invalidateKg();
      emit("generated", landed);
      emit("updated");
    } else {
      genNote.value = "生成流已结束，但后端没有回传落库段号。";
    }
  } catch (err) {
    if (isAbort(err)) {
      genError.value = "已停止等待；模型端可能仍在生成，稍后重新打开面板可看到最新段落。";
      finishAgentSteps(true, "已停止等待");
      emit("failed", genError.value);
    } else {
      genError.value = `生成失败：${textOf(err)}`;
      lastGenAction.value = "continue";
      genFix.value = planGateStatus(err);
      finishAgentSteps(true, genError.value);
      emit("failed", genError.value);
    }
  } finally {
    genController = null;
    stopGenClock();
    streamText.value = "";
    generating.value = false;
    genMode.value = null;
  }
}

async function runRegenerate(): Promise<void> {
  const seg = currentSegment.value;
  if (!seg || generating.value) {
    if (!seg) genError.value = "没有可重生成的段。";
    return;
  }
  tab.value = "text";
  generating.value = true;
  genMode.value = "regenerate";
  streamText.value = "";
  reasoningChars.value = 0;
  reasoningText.value = "";
  genNote.value = "";
  genError.value = "";
  resetAgentSteps();
  const landedRef = ref<{ segment_no: number; chars: number } | null>(null);
  const controller = new AbortController();
  genController = controller;
  startGenClock();
  try {
    await api.regenerateStream(
      props.projectId,
      seg.segment_no,
      null,
      (text) => {
        streamText.value += text;
      },
      (meta) => {
        landedRef.value = { segment_no: meta.segment_no, chars: meta.chars };
        genNote.value = `第 ${meta.segment_no} 段已重生成并落库 · ${meta.chars} 字。`;
      },
      controller.signal,
      (chars, text) => {
        reasoningChars.value = chars;
        if (text) reasoningText.value += text;
      },
      (ev) => {
        applyAgentStage(ev.stage, ev.label);
      },
      foreshadowMode.value,
    );
    finishAgentSteps(false, genNote.value || "重生成完成");
    const landed = landedRef.value;
    if (landed) {
      await loadSegments();
      selectSegment(landed.segment_no);
      saveNote.value = genNote.value;
      invalidateKg();
      emit("generated", landed);
      emit("updated");
    } else {
      genNote.value = "生成流已结束，但后端没有回传落库段号。";
    }
  } catch (err) {
    if (isAbort(err)) {
      genError.value = "已停止等待；模型端可能仍在生成，稍后重新打开面板可看到最新段落。";
      finishAgentSteps(true, "已停止等待");
      emit("failed", genError.value);
    } else {
      genError.value = `重生成失败：${textOf(err)}`;
      lastGenAction.value = "regenerate";
      genFix.value = planGateStatus(err);
      finishAgentSteps(true, genError.value);
      emit("failed", genError.value);
    }
  } finally {
    genController = null;
    stopGenClock();
    streamText.value = "";
    generating.value = false;
    genMode.value = null;
    confirmRegen.value = false;
  }
}

/** 覆盖式操作二次确认：第一次点击只换文案，第二次才真的发请求。 */
function onRegenerateClick(): void {
  if (generating.value) return;
  if (!confirmRegen.value) {
    confirmRegen.value = true;
    return;
  }
  void runRegenerate();
}

// ==================================================================
// 只读标签页的派生数据
// ==================================================================

const bible = computed<PlotBible | null>(() => project.value?.plot_bible ?? null);
const bibleCharacters = computed(() => bible.value?.characters ?? []);
const bibleRelationships = computed(() => bible.value?.relationships ?? []);
const bibleConflicts = computed(() => bible.value?.conflicts ?? []);
const bibleForeshadowing = computed(() => bible.value?.foreshadowing ?? []);

/** 伏笔可能是老字符串，也可能是分档对象；统一成可读的一行。 */
function bibleForeshadowText(item: unknown): string {
  if (typeof item === "string") return item;
  if (item && typeof item === "object") {
    const row = item as { name?: string; title?: string; tier?: string };
    const label =
      row.tier === "main" ? "主线" : row.tier === "arc" ? "卷级" : "小节级";
    const name = row.name || row.title || "";
    return name ? `[${label}] ${name}` : "";
  }
  return "";
}

const outlineEntries = computed<string[]>(() => {
  const outline = project.value?.outline;
  if (Array.isArray(outline)) return outline;
  if (typeof outline === "string") {
    return outline
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean);
  }
  return [];
});

/** 段列表按章区间去重后的章节下拉项（同章多段时取第一段）。 */
const chapterOptions = computed(() => {
  const seen = new Set<string>();
  const out: { label: string; segment_no: number }[] = [];
  for (const seg of segments.value) {
    const chapter = seg.chapter_range?.trim();
    if (!chapter || seen.has(chapter)) continue;
    seen.add(chapter);
    out.push({ label: chapter, segment_no: seg.segment_no });
  }
  return out;
});

/** 章节下拉的选中值：同一章可能有多段，统一落到该章第一段上。 */
const chapterSelectValue = computed(() => {
  const chapter = currentSegment.value?.chapter_range?.trim();
  if (!chapter) return "";
  return chapterOptions.value.find((c) => c.label === chapter)?.segment_no ?? "";
});

const timelineNodes = computed(() => timeline.value?.nodes ?? []);
const currentTimelineNode = computed(() => {
  const id = timeline.value?.current_node;
  if (!id) return null;
  return timelineNodes.value.find((n) => n.id === id) ?? null;
});

const kgByType = computed<{ type: string; count: number; names: string[] }[]>(() => {
  const graph = kg.value;
  if (!graph) return [];
  const buckets = new Map<string, string[]>();
  for (const node of graph.nodes) {
    const name =
      (typeof node.props?.name === "string" && node.props.name) ||
      (typeof node.props?.title === "string" && node.props.title) ||
      `#${node.id}`;
    const list = buckets.get(node.type) ?? [];
    list.push(name);
    buckets.set(node.type, list);
  }
  return [...buckets.entries()]
    .map(([type, names]) => ({ type, count: names.length, names }))
    .sort((a, b) => b.count - a.count);
});

const kgEdgeCount = computed(() => kg.value?.edges.length ?? 0);

// ==================================================================
// 生命周期
// ==================================================================

function onKeydown(e: KeyboardEvent): void {
  if (!props.open) return;
  if (e.key === "Escape") {
    // 弹窗优先关闭，避免一次 Esc 把整个写作面板也带走。
    if (gapDialogOpen.value) {
      dismissGapDialog();
      return;
    }
    emit("close");
  }
}

function autoRun(): void {
  if (generating.value) {
    // 同一面板上一个任务还没结束：不排队、不假装开始，明确回绝并回写卡片。
    genError.value = "面板正在生成中，请等当前任务结束后再发起新的生成。";
    emit("failed", genError.value);
    return;
  }
  if (props.mode === "continue") void runContinue();
  else if (props.mode === "regenerate") void runRegenerate();
}

onMounted(async () => {
  window.addEventListener("keydown", onKeydown);
  await bootstrap();
  if (props.open) {
    autoRun();
    // 纯查看模式：开面板就体检一次，有缺失才弹选择框。
    void autoCheckGaps();
  }
});

/** 抽屉从隐藏变可见时补一次体检；隐藏期间生成继续，不打断。 */
watch(
  () => props.open,
  (open) => {
    if (open) void autoCheckGaps();
  },
);

onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown);
  // 只有真正卸载（离开路由 / 切项目）才断开流：关闭抽屉只是 v-show 隐藏，
  // 生成会继续跑，卡片进度也继续更新。
  genController?.abort();
  if (genClock !== null) {
    window.clearInterval(genClock);
    genClock = null;
  }
});

/** 同一面板再次从卡片打开：换定位段并执行新的生成任务。 */
watch(
  () => props.runKey,
  (value, old) => {
    if (value === old) return;
    selectSegment(props.initialSegmentNo);
    autoRun();
  },
);
</script>

<template>
  <Teleport to="body">
    <div class="wp-mask" v-show="open" @click.self="emit('close')">
      <aside class="wp" role="dialog" aria-label="写作面板">
        <header class="wp-head">
          <div class="wp-head-main">
            <div class="wp-kicker">写作面板</div>
            <div class="wp-title">{{ project?.title || "未命名作品" }}</div>
          </div>
          <span class="tag mono">
            {{ segments.length }} 段
          </span>
          <button class="link-btn" type="button" @click="openGapDialog">
            缺失项{{ gaps.length ? ` ${gaps.length}` : "" }}
          </button>
          <button class="link-btn" type="button" @click="emit('close')">关闭</button>
        </header>

        <div v-if="loadError" class="notice err wp-load-error">{{ loadError }}</div>
        <div v-else-if="loading" class="wp-loading faint">读取正文与设定…</div>

        <template v-else>
          <!-- 段 / 章切换 -->
          <div class="wp-segbar">
            <button
              class="wp-step"
              type="button"
              :disabled="segmentIndex <= 0 || generating"
              aria-label="上一段"
              @click="step(-1)"
            >←</button>
            <select
              class="wp-select"
              :value="currentSegmentNo ?? ''"
              aria-label="选择段落"
              @change="selectSegment(Number(($event.target as HTMLSelectElement).value))"
            >
              <option v-if="!segments.length" value="">（还没有正文）</option>
              <option v-for="seg in segments" :key="seg.id" :value="seg.segment_no">
                第 {{ seg.segment_no }} 段{{ seg.chapter_range ? ` · ${seg.chapter_range}` : "" }}
                （{{ segmentChars(seg) }} 字）
              </option>
            </select>
            <button
              class="wp-step"
              type="button"
              :disabled="segmentIndex < 0 || segmentIndex >= segments.length - 1 || generating"
              aria-label="下一段"
              @click="step(1)"
            >→</button>
            <select
              v-if="chapterOptions.length"
              class="wp-select wp-chapter"
              :value="chapterSelectValue"
              aria-label="跳到章节"
              @change="selectSegment(Number(($event.target as HTMLSelectElement).value))"
            >
              <option value="" disabled>跳到章节…</option>
              <option
                v-for="ch in chapterOptions"
                :key="ch.label"
                :value="ch.segment_no"
              >{{ ch.label }}</option>
            </select>
          </div>

          <!-- 操作 -->
          <div class="wp-actions">
            <button
              class="btn primary sm"
              type="button"
              :disabled="!currentSegment || !dirty || saving || generating"
              @click="save"
            >
              {{ saving ? "保存中…" : dirty ? "保存修改" : "无改动" }}
            </button>
            <button
              class="btn sm"
              type="button"
              :class="{ danger: confirmRegen }"
              :disabled="!currentSegment || generating"
              @click="onRegenerateClick"
            >
              {{ confirmRegen ? `确认覆盖第 ${currentSegmentNo} 段` : "重生成本段" }}
            </button>
            <label class="faint wp-foreshadow-mode">
              伏笔
              <select
                class="input"
                v-model="foreshadowMode"
                :disabled="generating"
                @change="saveForeshadowMode"
              >
                <option value="auto">AI 自动</option>
                <option value="manual">按我的节奏</option>
                <option value="off">不需要</option>
              </select>
            </label>
            <button
              class="btn sm"
              type="button"
              :disabled="generating"
              @click="runContinue"
            >
              {{ generating && genMode === "continue" ? "生成中…" : "续写下一段" }}
            </button>
            <span v-if="generating" class="faint wp-gen-timer">
              作者 Agent · {{ agentHeadline }} · {{ genWaitSeconds }} 秒 · {{ streamChars }} 字
            </span>
            <button
              v-if="generating"
              class="btn sm"
              type="button"
              @click="cancelGeneration"
            >
              停止等待
            </button>
          </div>

          <div v-if="saveNote" class="notice ok">{{ saveNote }}</div>
          <div v-if="saveError" class="notice err">{{ saveError }}</div>
          <div v-if="genNote && !generating" class="notice info">{{ genNote }}</div>
          <div v-if="genError" class="notice err">
            <span>{{ genError }}</span>
            <button
              v-if="genFix"
              class="link-btn"
              type="button"
              :disabled="genFixBusy"
              @click="runGenFix"
            >{{ genFixBusy ? "处理中…" : genFixLabel }}</button>
          </div>

          <!-- 标签页 -->
          <nav class="wp-tabs" aria-label="写作面板分类">
            <button
              v-for="t in TAB_LABELS"
              :key="t.key"
              type="button"
              class="wp-tab"
              :class="{ on: tab === t.key }"
              @click="switchTab(t.key)"
            >{{ t.label }}</button>
          </nav>

          <div class="wp-content">
            <!-- 正文 -->
            <section v-if="tab === 'text'" class="wp-pane">
              <div v-if="!segments.length" class="wp-empty">
                还没有正文。点上方「续写下一段」开始写第一段。
              </div>
              <template v-else-if="currentSegment">
                <div class="wp-text-meta">
                  <span class="mono">第 {{ currentSegment.segment_no }} 段</span>
                  <span v-if="currentSegment.chapter_range" class="mono">
                    {{ currentSegment.chapter_range }}
                  </span>
                  <span class="faint">{{ currentChars }} 字</span>
                  <span v-if="dirty" class="tag warn">有未保存修改</span>
                </div>
                <textarea
                  v-model="draftText"
                  class="wp-textarea"
                  spellcheck="false"
                  aria-label="正文编辑区"
                />
                <div v-if="generating" class="wp-stream">
                  <div class="wp-agent">
                    <div class="wp-agent-head">
                      <span class="tag accent">作者 Agent</span>
                      <b class="wp-agent-title">{{ agentHeadline }}</b>
                      <span class="faint mono">{{ genWaitSeconds }}s</span>
                    </div>
                    <ol class="wp-agent-steps" aria-label="作者 Agent 执行轨迹">
                      <li
                        v-for="step in agentSteps"
                        :key="step.key"
                        class="wp-agent-step"
                        :class="step.state"
                      >
                        <span class="wp-agent-dot" aria-hidden="true" />
                        <span class="wp-agent-label">{{ step.label }}</span>
                        <span v-if="stepSeconds(step)" class="wp-agent-time mono">{{ stepSeconds(step) }}</span>
                        <span v-if="step.detail" class="wp-agent-detail">{{ step.detail }}</span>
                      </li>
                    </ol>
                    <div class="wp-agent-metrics">
                      <span>已收到 {{ streamChars }} 字</span>
                      <span v-if="reasoningChars > 0">模型思考 {{ reasoningChars }} 字</span>
                      <span>目标约 {{ genTargetChars }} 字</span>
                      <button class="link-btn" type="button" @click="switchTab('context')">
                        查看装配上下文
                      </button>
                    </div>
                    <details v-if="reasoningText" class="wp-thinking">
                      <summary>模型思考内容（{{ reasoningChars }} 字）</summary>
                      <pre>{{ reasoningText }}</pre>
                    </details>
                  </div>
                  <p v-if="streamChars === 0 && genWaitSeconds >= 30" class="faint wp-stream-hint">
                    首字还没到：不同供应商的首字排队时间差别很大，轨迹会如实停在「等待模型首字 / 模型思考」。超过 3 分钟仍无输出可以点「停止等待」。
                  </p>
                  <pre v-if="streamChars > 0" class="wp-stream-text">{{ streamText }}</pre>
                </div>
              </template>
            </section>

            <!-- 设定 -->
            <section v-else-if="tab === 'setting'" class="wp-pane">
              <div v-if="!bible" class="wp-empty">
                剧情圣经还是空的：先在引导或项目页建立设定。
              </div>
              <template v-else>
                <p v-if="project?.premise" class="wp-prose">{{ project.premise }}</p>
                <p v-if="bible.synopsis" class="wp-prose faint">{{ bible.synopsis }}</p>
                <div class="wp-block">
                  <h3>角色 <span class="mini">{{ bibleCharacters.length }}</span></h3>
                  <div
                    v-for="(c, i) in bibleCharacters"
                    :key="`c-${i}`"
                    class="wp-item"
                  >
                    <div class="wp-item-head">
                      <b>{{ c.name || "未命名" }}</b>
                      <span v-if="c.role" class="tag">{{ c.role }}</span>
                    </div>
                    <p v-if="c.motivation" class="faint">{{ c.motivation }}</p>
                  </div>
                </div>
                <div v-if="bibleRelationships.length" class="wp-block">
                  <h3>关系 <span class="mini">{{ bibleRelationships.length }}</span></h3>
                  <div
                    v-for="(r, i) in bibleRelationships"
                    :key="`r-${i}`"
                    class="wp-item"
                  >
                    <b>{{ r.a || "?" }}</b>
                    <span class="faint"> —{{ r.relation || "关系未定" }}→ </span>
                    <b>{{ r.b || "?" }}</b>
                  </div>
                </div>
                <div v-if="bibleConflicts.length" class="wp-block">
                  <h3>冲突</h3>
                  <ul class="wp-list">
                    <li v-for="(x, i) in bibleConflicts" :key="`cf-${i}`">{{ x }}</li>
                  </ul>
                </div>
                <div v-if="bibleForeshadowing.length" class="wp-block">
                  <h3>伏笔</h3>
                  <ul class="wp-list">
                    <li v-for="(x, i) in bibleForeshadowing" :key="`fs-${i}`">{{ bibleForeshadowText(x) }}</li>
                  </ul>
                </div>
              </template>
            </section>

            <!-- 剧情 -->
            <section v-else-if="tab === 'plot'" class="wp-pane">
              <div class="wp-block">
                <h3>
                  章纲 <span class="mini">{{ outlineEntries.length }}</span>
                  <router-link
                    class="link-btn wp-h3-action"
                    :to="`/projects/${projectId}/outline`"
                  >
                    {{ outlineEntries.length ? "编辑章纲" : "去写章纲" }}
                  </router-link>
                </h3>
                <p v-if="!outlineEntries.length" class="wp-empty">
                  还没有章纲。世界线依赖章纲，先写至少一章，世界线才能被规划。
                </p>
                <ol v-else class="wp-outline">
                  <li v-for="(entry, i) in outlineEntries" :key="`o-${i}`">
                    <span class="mono wp-outline-no">第 {{ i + 1 }} 章</span>
                    <span>{{ entry }}</span>
                  </li>
                </ol>
              </div>
              <div class="wp-block">
                <h3>已写段摘要 <span class="mini">{{ segments.length }}</span></h3>
                <p v-if="!segments.length" class="wp-empty">还没有已写的段。</p>
                <div v-for="seg in segments" :key="`s-${seg.id}`" class="wp-item">
                  <div class="wp-item-head">
                    <b>第 {{ seg.segment_no }} 段</b>
                    <span v-if="seg.chapter_range" class="mono faint">
                      {{ seg.chapter_range }}
                    </span>
                  </div>
                  <p class="faint">
                    {{ summaryText(seg.summary) || "（这一段还没有摘要）" }}
                  </p>
                </div>
              </div>
            </section>

            <!-- 装配上下文（只读预览：下一次生成实际会发给模型的内容） -->
            <section v-else-if="tab === 'context'" class="wp-pane">
              <div class="wp-inline-actions">
                <button
                  class="btn sm"
                  type="button"
                  :disabled="contextLoading"
                  @click="loadContext"
                >
                  {{ contextLoading ? "读取中…" : "重新读取" }}
                </button>
                <span class="faint">
                  只组装、不调模型。前情、世界线、检索与约束都是本次真实下发的内容。
                </span>
              </div>
              <div v-if="contextError" class="notice err">{{ contextError }}</div>
              <div v-if="contextLoading && !contextPlan" class="wp-loading faint">
                正在装配上下文…
              </div>
              <template v-if="contextPlan">
                <div class="wp-text-meta">
                  <span class="mono">前情块 {{ contextPlan.prior_blocks.length }}</span>
                  <span class="mono">摘要 {{ contextPlan.prior_summaries.length }}</span>
                  <span class="mono">检索 {{ contextPlan.rag_context.length }}</span>
                  <span class="mono">
                    约束 {{ contextPlan.constraints.characters.length + contextPlan.constraints.plotholes.length }}
                  </span>
                </div>
                <details class="wp-context-block" open>
                  <summary>用户提示词（本项目上下文）</summary>
                  <pre>{{ contextPlan.user_prompt }}</pre>
                </details>
                <details class="wp-context-block">
                  <summary>系统提示词</summary>
                  <pre>{{ contextPlan.system_prompt }}</pre>
                </details>
                <details v-if="reasoningText" class="wp-context-block">
                  <summary>最近一次模型思考（{{ reasoningChars }} 字）</summary>
                  <pre>{{ reasoningText }}</pre>
                </details>
              </template>
            </section>

            <!-- 世界线 -->
            <section v-else-if="tab === 'timeline'" class="wp-pane">
              <div v-if="timelineLoading" class="wp-loading faint">读取世界线…</div>
              <template v-else>
                <div v-if="timelineError" class="notice err">{{ timelineError }}</div>
                <div v-if="timelinePlanNote" class="notice ok">{{ timelinePlanNote }}</div>
                <div v-if="!timeline || !timeline.exists" class="wp-empty">
                  <p>
                    {{ timeline?.planning_reason || "时间线尚未规划：首次生成或追加节点时才会建立骨架。" }}
                  </p>
                  <p v-if="!outlineEntries.length" class="faint">
                    世界线由「章纲 + 模型规划」生成；当前项目还没有章纲。
                  </p>
                  <router-link
                    v-if="!outlineEntries.length"
                    class="btn sm"
                    :to="`/projects/${projectId}/outline`"
                  >
                    去写章纲
                  </router-link>
                  <button
                    v-else
                    class="btn primary sm"
                    type="button"
                    :disabled="timelinePlanning"
                    @click="planTimeline"
                  >
                    {{ timelinePlanning ? "模型规划中…" : "让模型规划世界线" }}
                  </button>
                </div>
                <template v-else>
                  <div class="wp-text-meta">
                    <span class="mono">{{ timelineNodes.length }} 个节点</span>
                    <span v-if="currentTimelineNode" class="tag accent">
                      当前：{{ currentTimelineNode.title }}
                    </span>
                    <span v-if="timeline.planning_incomplete" class="tag warn">规划未完成</span>
                    <button
                      class="link-btn wp-h3-action"
                      type="button"
                      :disabled="timelinePlanning"
                      @click="planTimeline"
                    >
                      {{ timelinePlanning ? "规划中…" : "重新规划" }}
                    </button>
                  </div>
                  <div
                    v-for="node in timelineNodes"
                    :key="node.id"
                    class="wp-item"
                    :class="{ current: node.id === timeline.current_node }"
                  >
                    <div class="wp-item-head">
                      <b>{{ node.title }}</b>
                      <span class="mono faint">
                        {{ node.chapter_start ? `第 ${node.chapter_start}` : "—" }}–{{
                          node.chapter_end || "—"
                        }} 章
                      </span>
                      <span v-if="node.emotion" class="tag">{{ node.emotion }}</span>
                      <span class="tag mono">{{ node.kind === "branch" ? "支线" : "主线" }}</span>
                    </div>
                    <p v-if="node.beats.length" class="faint">
                      {{ node.beats.join(" · ") }}
                    </p>
                  </div>
                </template>
              </template>
            </section>

            <!-- 图谱 -->
            <section v-else class="wp-pane">
              <div v-if="kgLoading" class="wp-loading faint">读取图谱…</div>
              <template v-else>
                <div v-if="kgError" class="notice err">{{ kgError }}</div>
                <div v-if="kgRebuildNote" class="notice ok">{{ kgRebuildNote }}</div>
                <div class="kg-source">
                  <button
                    type="button"
                    class="wp-tab"
                    :class="{ on: kgSource === 'generated' }"
                    @click="switchKgSource('generated')"
                  >
                    生成正文
                  </button>
                  <button
                    type="button"
                    class="wp-tab"
                    :class="{ on: kgSource === 'all' }"
                    @click="switchKgSource('all')"
                  >
                    全部（含参考语料）
                  </button>
                </div>
                <div class="wp-inline-actions">
                  <button
                    class="btn sm"
                    type="button"
                    :class="{ danger: kgRebuildConfirm }"
                    :disabled="kgRebuilding"
                    @click="rebuildKg()"
                  >
                    {{
                      kgRebuilding
                        ? "重建中…"
                        : kgRebuildConfirm
                          ? "确认重建（会覆盖现有图谱）"
                          : "从正文重建图谱"
                    }}
                  </button>
                  <span v-if="kgRebuildConfirm" class="faint">再点一次确认</span>
                </div>
                <div v-if="!kg || (!kg.nodes.length && !kg.edges.length)" class="wp-empty">
                  图谱还是空的：写几段正文后点上面的「从正文重建图谱」。
                </div>
                <template v-else>
                  <div class="wp-text-meta">
                    <span class="mono">{{ kg.nodes.length }} 个节点</span>
                    <span class="mono">{{ kgEdgeCount }} 条边</span>
                  </div>
                  <div v-for="bucket in kgByType" :key="bucket.type" class="wp-block">
                    <h3>
                      {{ bucket.type }}
                      <span class="mini">{{ bucket.count }}</span>
                    </h3>
                    <div class="wp-chips">
                      <span
                        v-for="(name, i) in bucket.names.slice(0, 40)"
                        :key="`${bucket.type}-${i}`"
                        class="chip"
                      >{{ name }}</span>
                      <span v-if="bucket.names.length > 40" class="faint">
                        还有 {{ bucket.names.length - 40 }} 个…
                      </span>
                    </div>
                  </div>
                </template>
              </template>
            </section>
          </div>
        </template>
      </aside>

      <!-- 缺失项补全：助手先体检，再由用户勾选要补哪几项 -->
      <div
        v-if="gapDialogOpen"
        class="gap-mask"
        @click.self="dismissGapDialog"
      >
        <div class="gap-dialog" role="dialog" aria-label="缺失项补全">
          <header class="gap-head">
            <div>
              <div class="wp-kicker">助手体检</div>
              <h3>发现 {{ gaps.length }} 项缺失</h3>
            </div>
            <button class="link-btn" type="button" @click="dismissGapDialog">关闭</button>
          </header>
          <p class="faint gap-intro">
            勾选要补的项目。可自动执行的本面板内直接做；需要跳转的会带你去对应页面（一次去一个，其余可回来再补）。
          </p>

          <div v-if="gapChecking" class="faint">正在检查…</div>
          <p v-else-if="!gaps.length" class="faint">没有检测到缺失项。</p>
          <template v-else>
            <div
              v-for="gap in gaps"
              :key="gap.key"
              class="gap-row"
              :class="{ off: !gap.selected }"
            >
              <label>
                <input v-model="gap.selected" type="checkbox" :disabled="gapRunning" />
                <span class="gap-body">
                  <span class="gap-title">
                    <span class="gap-group">{{ gap.group }}</span>
                    <b>{{ gap.title }}</b>
                    <span
                      class="tag"
                      :class="
                        gap.kind === 'auto' ? 'accent' : gap.kind === 'jump' ? 'warn' : 'info'
                      "
                    >
                      {{
                        gap.kind === "auto"
                          ? "可自动执行"
                          : gap.kind === "jump"
                            ? "需要跳转"
                            : "提示"
                      }}
                    </span>
                  </span>
                  <span class="gap-detail">{{ gap.detail }}</span>
                </span>
              </label>
            </div>
          </template>

          <div v-if="gapResults.length" class="notice info gap-results">
            <div v-for="(line, i) in gapResults" :key="`gr-${i}`">{{ line }}</div>
          </div>

          <footer class="gap-actions">
            <button class="btn" type="button" :disabled="gapRunning" @click="dismissGapDialog">
              稍后
            </button>
            <button
              class="btn primary"
              type="button"
              :disabled="gapRunning || gapChecking || !gapSelectable"
              @click="runSelectedGaps"
            >
              {{ gapRunning ? "执行中…" : `执行选中项（${gapSelectable}）` }}
            </button>
          </footer>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.wp-mask {
  position: fixed;
  inset: 0;
  z-index: 70;
  display: flex;
  justify-content: flex-end;
  background: rgba(28, 22, 16, 0.34);
}

.wp {
  display: flex;
  flex-direction: column;
  width: min(94vw, 620px);
  height: 100%;
  min-height: 0;
  padding: 16px 18px 18px;
  background: var(--bg-elev);
  border-left: 1px solid var(--border-strong);
  box-shadow: -16px 0 48px rgba(28, 22, 16, 0.22);
  animation: wp-in 0.22s cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes wp-in {
  from { transform: translateX(24px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}

.wp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}

.wp-head-main { flex: 1; min-width: 0; }
.wp-kicker {
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.wp-title {
  font-family: var(--font-display);
  font-size: var(--fs-xl);
  font-weight: 700;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wp-loading,
.wp-load-error { margin: 12px 0; }

.wp-segbar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 0 8px;
  flex-wrap: wrap;
}
.wp-step {
  flex: none;
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  cursor: pointer;
}
.wp-step:disabled { opacity: 0.35; cursor: not-allowed; }
.wp-select {
  flex: 1;
  min-width: 160px;
  height: 30px;
  padding: 0 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--fs-xs);
}
.wp-chapter { flex: 0 1 150px; }

.wp-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 10px;
}
.btn.danger {
  border-color: var(--danger-line, var(--accent-line));
  color: var(--danger, var(--accent-ink));
}
.wp-gen-timer { font-size: var(--fs-2xs); }
.wp-foreshadow-mode { display: inline-flex; align-items: center; gap: 4px; font-size: var(--fs-2xs); }
.wp-foreshadow-mode select { padding: 2px 6px; font-size: var(--fs-2xs); }

.wp-tabs {
  display: flex;
  gap: 4px;
  padding: 8px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.wp-tab {
  padding: 4px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--fg-faint);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.wp-tab.on {
  border-color: var(--accent-line);
  background: var(--accent-soft);
  color: var(--accent-ink);
}

.wp-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-top: 10px;
}
.wp-pane { display: flex; flex-direction: column; gap: 8px; }

.wp-text-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  color: var(--fg-faint);
  font-size: var(--fs-2xs);
}

.wp-textarea {
  width: 100%;
  min-height: 340px;
  resize: vertical;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  color: var(--fg);
  font-family: var(--font-sans);
  font-size: var(--fs-md);
  line-height: 1.85;
}

.wp-stream {
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px dashed var(--accent-line);
  border-radius: var(--radius-md);
  background: var(--accent-soft);
}
.wp-agent {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.wp-agent-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-2xs);
}
.wp-agent-title { font-weight: 600; }
.wp-agent-steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.wp-agent-step {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: var(--fs-2xs);
  line-height: 1.9;
  color: var(--fg-faint);
}
.wp-agent-dot {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--border);
  transform: translateY(-1px);
}
.wp-agent-step.done { color: var(--fg-muted); }
.wp-agent-step.done .wp-agent-dot { background: var(--ok); }
.wp-agent-step.active { color: var(--fg); font-weight: 600; }
.wp-agent-step.active .wp-agent-dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
  animation: wp-agent-pulse 1.2s ease-in-out infinite;
}
.wp-agent-step.error { color: var(--danger); font-weight: 600; }
.wp-agent-step.error .wp-agent-dot { background: var(--danger); }
.wp-agent-time { font-size: var(--fs-2xs); }
.wp-agent-detail {
  margin-left: auto;
  font-weight: 400;
  color: var(--fg-faint);
}
.wp-agent-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding-top: 6px;
  border-top: 1px dashed var(--border);
  font-size: var(--fs-2xs);
  color: var(--fg-faint);
}
.wp-agent-metrics span:first-child { color: var(--fg-muted); }
@keyframes wp-agent-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}
.wp-stream-text {
  margin: 8px 0 0;
  max-height: 260px;
  overflow-y: auto;
  white-space: pre-wrap;
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  line-height: 1.8;
}
.wp-stream-hint {
  margin: 0 0 6px;
  font-size: var(--fs-2xs);
  line-height: 1.6;
}
.wp-thinking {
  margin-top: 8px;
  font-size: var(--fs-2xs);
  color: var(--fg-faint);
}
.wp-thinking summary {
  cursor: pointer;
  user-select: none;
}
.wp-thinking pre {
  margin: 6px 0 0;
  max-height: 260px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  line-height: 1.6;
  color: var(--fg-muted);
  background: var(--bg-sunken, rgba(0, 0, 0, 0.03));
  padding: 8px;
  border-radius: 6px;
}
.wp-context-block {
  margin-top: 10px;
  font-size: var(--fs-2xs);
  color: var(--fg-faint);
}
.wp-context-block summary {
  cursor: pointer;
  user-select: none;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.wp-context-block pre {
  margin: 6px 0 0;
  max-height: 360px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: var(--font-mono);
  font-size: var(--fs-2xs);
  line-height: 1.6;
  color: var(--fg-muted);
  background: var(--bg-sunken, rgba(0, 0, 0, 0.03));
  padding: 10px;
  border-radius: 6px;
}

.wp-empty {
  padding: 18px 4px;
  color: var(--fg-faint);
  font-size: var(--fs-sm);
}

.wp-block { padding: 8px 0; }
.wp-block + .wp-block { border-top: 1px solid var(--border); }
.wp-block h3 {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 0 0 6px;
  font-family: var(--font-display);
  font-size: var(--fs-sm);
  font-weight: 600;
}
.wp-block h3 .mini {
  color: var(--fg-faint);
  font-family: var(--font-sans);
  font-size: var(--fs-3xs);
  font-weight: 400;
}

.wp-prose { margin: 0; font-size: var(--fs-sm); line-height: 1.8; }

.wp-item {
  padding: 7px 9px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-sm);
}
.wp-item + .wp-item { margin-top: 4px; }
.wp-item.current {
  background: var(--accent-soft);
  border: 1px solid var(--accent-line);
}
.wp-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.wp-item p { margin: 3px 0 0; font-size: var(--fs-xs); line-height: 1.7; }

.wp-list { margin: 0; padding-left: 18px; font-size: var(--fs-sm); }
.wp-h3-action {
  margin-left: auto;
  font-size: var(--fs-2xs);
  font-weight: 400;
}
.wp-inline-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.kg-source {
  display: flex;
  gap: 4px;
  margin-bottom: 6px;
}
.wp-outline { margin: 0; padding-left: 0; list-style: none; }
.wp-outline li {
  display: flex;
  gap: 8px;
  padding: 5px 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.wp-outline-no { flex: none; color: var(--fg-faint); }

.wp-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.wp-chips .chip {
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-full);
  font-size: var(--fs-2xs);
}

/* ---------- 缺失项补全弹窗 ---------- */
.gap-mask {
  position: fixed;
  inset: 0;
  z-index: 90;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(20, 16, 12, 0.46);
}
.gap-dialog {
  width: min(92vw, 470px);
  max-height: 82vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  box-shadow: 0 24px 64px rgba(20, 16, 12, 0.32);
}
.gap-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.gap-head h3 {
  margin-top: 2px;
  font-family: var(--font-display);
  font-size: var(--fs-xl);
}
.gap-intro {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
}
.gap-row {
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.gap-row.off { opacity: 0.55; }
.gap-row label {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  cursor: pointer;
}
.gap-row input[type="checkbox"] {
  margin-top: 3px;
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  flex: none;
}
.gap-body { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.gap-group {
  color: var(--fg-faint);
  font-size: var(--fs-3xs);
  letter-spacing: 0.08em;
  margin-right: 2px;
}
.gap-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: var(--fs-sm);
}
.gap-detail {
  color: var(--fg-faint);
  font-size: var(--fs-2xs);
  line-height: 1.6;
}
.gap-results {
  margin: 0;
  font-size: var(--fs-xs);
}
.gap-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 4px;
}
</style>
