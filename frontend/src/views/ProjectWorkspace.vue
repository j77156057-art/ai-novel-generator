<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api } from "@/api/client";
import type {
  AlertsResponse,
  DistillJob,
  GeneratePlan,
  Graph,
  KgBookkeeping,
  LlmConfig,
  NarrativeTier,
  NovelOut,
  PlotBible,
  ProjectOut,
  StyleDistillSummary,
} from "@/api/types";
import DistillProgress from "@/components/DistillProgress.vue";

const route = useRoute();
const router = useRouter();
const projectId = Number(route.params.id);

const project = ref<ProjectOut | null>(null);
const loadError = ref<string | null>(null);
const novels = ref<NovelOut[]>([]);

// 当前生效的 LLM 渠道（工作台徽标，点击去模型设置）
const llmConfig = ref<LlmConfig | null>(null);

// 向导步骤
const STEPS = [
  "参考语料",
  "类型",
  "风格",
  "大概内容",
  "构思剧情",
  "生成控制台",
  "知识图谱",
] as const;
const step = ref(1);
const maxStep = ref(1);

// 选择态
const selectedRefs = ref<number[]>([]);
const GENRES = ["科幻", "都市", "悬疑", "言情", "奇幻", "仙侠", "历史", "游戏"];
const STYLES = ["番茄", "起点", "晋江", "豆瓣阅读", "知乎盐选"];

// 构思
const proposing = ref(false);
const proposals = ref<string[]>([]);
const proposalNote = ref<string | null>(null);
const proposingErr = ref<string | null>(null);
const asking = ref(false);
const questions = ref<string[]>([]);
const questionNote = ref<string | null>(null);
const answers = reactive<Record<string, string>>({});
const buildingBible = ref(false);
const bible = ref<PlotBible | null>(null);

// 生成前的「让模型出方案」回调后回填
function pickProposal(text: string) {
  premiseDraft.value = text;
  proposals.value = [];
}

// 大概内容草稿（PATCH 后才进 project.premise）
const premiseDraft = ref("");

// —— 标题可编辑（#21）——
const editingTitle = ref(false);
const titleDraft = ref("");
function startEditTitle() {
  if (!project.value) return;
  titleDraft.value = project.value.title;
  editingTitle.value = true;
}
async function saveTitle() {
  if (!project.value) return;
  const t = titleDraft.value.trim();
  editingTitle.value = false;
  if (!t || t === project.value.title) return;
  try {
    project.value = await api.patchProject(projectId, { title: t });
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  }
}

// 生成
const generationLength = ref(3000);
const streaming = ref(false);
const streamedText = ref("");
const liveBox = ref<HTMLElement | null>(null);
const generations = ref<{ id: number; segment_no: number; text: string | null }[]>([]);
const lastText = ref<string | null>(null);

// 阅读区展开/收起（默认给足高度，长文可一键全展开，不再被小框憋住）
const expandLive = ref(false);
const expandedSegs = reactive<Record<number, boolean>>({});
function toggleSeg(id: number) {
  expandedSegs[id] = !expandedSegs[id];
}

// 一次生成段数（#22）
const chaptersPerGen = ref(1);
const batchCount = ref(0);

// 分页（#23）
const pageSize = ref(5);
const page = ref(1);
const totalPages = computed(() =>
  Math.max(1, Math.ceil(generations.value.length / pageSize.value)),
);
const pagedGens = computed(() => {
  const start = (page.value - 1) * pageSize.value;
  return generations.value.slice(start, start + pageSize.value);
});

// 单段重生成（#24）
const regenState = ref<Record<number, { text: string; loading: boolean }>>({});

// 标记完成（#25 保存）
const completed = computed(() => project.value?.status === "completed");
async function toggleComplete() {
  if (!project.value) return;
  const next = completed.value ? "generating" : "completed";
  try {
    project.value = await api.patchProject(projectId, { status: next });
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  }
}

// 图谱
const kgSource = ref<string>("generated");
const graph = ref<Graph | null>(null);
const buildingKg = ref(false);
const kgMsg = ref<string | null>(null);
// 图谱提示条的色级：'' 未设（回退 info）| ok | warn | err。语义色，不写死 hex。
const kgMsgLevel = ref<"" | "ok" | "warn" | "err">("");
// 第 6 步（生成 / 重生成）专用的簿记提示：与第 7 步 kgMsg 分开，
// 否则第 7 步的图加载报错会串到第 6 步，反之亦然。
const genKgMsg = ref<string | null>(null);
const genKgLevel = ref<"" | "ok" | "warn" | "err">("");

const characters = ref<{ id: number; props: any }[]>([]);
const plotholes = ref<{ id: number; props: any }[]>([]);

async function patch(partial: Record<string, unknown>) {
  const updated = await api.patchProject(projectId, partial as any);
  project.value = updated;
  return updated;
}

async function loadProject() {
  try {
    project.value = await api.getProject(projectId);
    selectedRefs.value = (project.value.reference_novel_ids as number[]) || [];
    premiseDraft.value = project.value.premise || "";
    generationLength.value = project.value.generation_length || 3000;
    bible.value = (project.value.plot_bible as PlotBible) || null;
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  }
}

async function loadNovels() {
  try {
    novels.value = await api.listNovels();
  } catch {
    novels.value = [];
  }
}

async function loadGenerations() {
  try {
    const gens = await api.listGenerations(projectId);
    generations.value = gens.map((g) => ({
      id: g.id,
      segment_no: g.segment_no,
      text: g.text,
    }));
    if (page.value > totalPages.value) page.value = totalPages.value;
  } catch {
    generations.value = [];
  }
}

// —— 规则哨兵的历史告警 ——
// 告警是**落库**的，所以刷新页面还在。这不是装饰：只放在单次生成响应里的告警，
// 用户当时没盯着就永远丢了，事后也无从复核「上次那条到底是不是误报」。
// 取不到（老后端 / 网络问题）时静默降级为「无告警」，不打扰主流程 ——
// 哨兵是增强，不是生成的前提。
const alerts = ref<AlertsResponse | null>(null);
const alertsErr = ref<string | null>(null);

async function loadAlerts() {
  try {
    alerts.value = await api.listAlerts(projectId);
    alertsErr.value = null;
  } catch (e) {
    alerts.value = null;
    alertsErr.value = e instanceof Error ? e.message : String(e);
  }
}

/** 告警视图（字段判空都收在这里，模板保持干净）。 */
const alertView = computed(() => {
  const a = alerts.value;
  if (!a || !a.items.length) return null;
  return {
    err: a.by_severity.err,
    warn: a.by_severity.warn,
    items: a.items.map((it) => ({
      ...it,
      // 反证原文是关键：不给原文，用户没法判断这条告警是不是误报
      proof: (it.evidence || [])
        .map((e) => `第 ${e.segment_no} 段「${e.text}」`)
        .join("、"),
      here: (it.spans || []).map((s) => s.text).join("、"),
    })),
  };
});

/** 未生效的规则（必须显式列出来，否则「没告警」会被读成「没问题」）。 */
const inactiveRules = computed(() => {
  const rules = alerts.value?.rules ?? plan.value?.sentinel?.rules;
  if (!rules) return [];
  return Object.entries(rules)
    .filter(([, r]) => !r.active)
    .map(([name, r]) => ({ name, ...r }));
});

// 按项目已有数据推断「上次走到哪一步」，进入工作台即续接，不再每次回到开头。
// 优先级从高到低：已生成/已有剧情圣经 → 构思已完；依次回退到内容/风格/类型。
function inferStep(): number {
  const p = project.value;
  if (!p) return 1;
  if (generations.value.length > 0 || p.plot_bible) return 6;
  if (p.premise) return 5;
  if (p.style) return 4;
  if (p.genre) return 3;
  if (p.reference_novel_ids?.length) return 2;
  return 1;
}

function restoreStep() {
  const n = inferStep();
  step.value = n;
  maxStep.value = Math.max(maxStep.value, n);
}

function goStep(n: number) {
  step.value = n;
  maxStep.value = Math.max(maxStep.value, n);
}

// —— 文风蒸馏档位（完整 / 平衡 / 快速）——
// 档位清单**从后端取**（GET /projects/narrative-tiers），不在前端硬编码三档文案：
// 档位定义（采样/维度/清洗强度）的单一事实来源是后端 TIER_REGISTRY，
// 前端另抄一份必然随档位调整而漂移。
const tiers = ref<NarrativeTier[]>([]);
const tierDefault = ref("full");
const tiersErr = ref<string | null>(null);
const selectedTier = ref<string>("");

/** 档位清单拉取失败时的兜底文案，不静默假装有清单。 */
async function loadTiers() {
  try {
    const r = await api.listNarrativeTiers();
    tiers.value = r.tiers || [];
    tierDefault.value = r.default || "full";
    tiersErr.value = null;
  } catch (err) {
    tiers.value = [];
    tiersErr.value = err instanceof Error ? err.message : String(err);
  }
}

/**
 * 耗时/成本提示**一律取后端 `cost_hint`**，前端不维护第二份耗时表。
 *
 * 之前这里按 key 硬编码过一份耗时表，现已删除：耗时是**会随语料/模型/机器
 * 变化**的实测值，存两份必然漂移——后端更新了、前端还挂着旧数字，用户看到的
 * 承诺就过期了。后端新增档位忘了补提示会响亮失败（KeyError + 测试钉住），
 * 所以这个字段可靠。
 */

/** 伪角色清洗强度 → 人话。强度分三档，别让用户去猜 "partial" 是几成。 */
const CLEANING_LABEL: Record<string, string> = {
  full: "伪角色全量清洗",
  partial: "伪角色部分清洗",
  none: "不做伪角色清洗",
};

/**
 * 采样方式 → 人话。两个分支都要覆盖：percentage 档是「按语料量百分比」，
 * fixed_chapters 档是「固定 N 章」——写死一种会让另一档显示成空。
 */
function samplingText(t: NarrativeTier): string {
  if (t.sampling_mode === "percentage") {
    const pct = t.sample_ratio != null ? Math.round(t.sample_ratio * 1000) / 10 : null;
    return pct != null ? `按语料量 ${pct}% 采样` : "按语料量百分比采样";
  }
  if (t.sampling_mode === "fixed_chapters") {
    return t.fixed_chapters != null ? `固定 ${t.fixed_chapters} 章` : "固定章数采样";
  }
  return `采样方式 ${t.sampling_mode}`;
}

/**
 * 档位 → 「文字标签 + 色标」双编码（沿用 assistantDisplay.ts 的做法）。
 *
 * 色标只做**冗余**提示，不独自承载语义：色弱/单色打印/截图转灰度时，
 * 「最慢·最全 / 居中 / 最快」这行字仍然把三档区分得清清楚楚。
 * 强度标签按清单里的**次序**推导（后端已按完整→平衡→快速排好），
 * 而不是按 key 硬编码，所以后端增删档位时标签不会指错。
 */
function tierMeta(t: NarrativeTier, index: number, total: number) {
  if (total <= 1) return { label: "唯一档位", tag: "accent" };
  if (index === 0) return { label: "最完整 · 也最慢", tag: "accent" };
  if (index === total - 1) return { label: "最快 · 适合冒烟", tag: "info" };
  return { label: "居中 · 试跑用", tag: "warn" };
}

const tierCards = computed(() =>
  tiers.value.map((t, i) => ({
    tier: t,
    meta: tierMeta(t, i, tiers.value.length),
    sampling: samplingText(t),
    cleaning: CLEANING_LABEL[t.fake_character_cleaning] ?? `伪角色清洗 ${t.fake_character_cleaning}`,
    nameNorm: t.normalize_character_names ? "角色名语料自证归一化" : "角色名不归一化",
    cost: t.cost_hint || "",
    isDefault: t.key === tierDefault.value,
  })),
);

/** 当前生效档位：已落库的 > 用户刚点的 > 后端默认档。 */
const effectiveTier = computed(
  () => selectedTier.value || project.value?.narrative_tier || tierDefault.value,
);

// ====================================================================
// 文风蒸馏后台任务（可观测进度）
// ====================================================================
//
// 蒸馏在 833 章语料上跑完整档实测 42 分钟，所以它必须是**后台任务**：
// 提交即返回 job_id，真正的蒸馏跑在服务端，前端按 1.5s 轮询进度。
// 好处是离开页面/刷新/切步骤都不丢 —— 进度存在服务端的 distill_runs 表里。
const distillJob = ref<DistillJob | null>(null);
const distillSubmitting = ref(false);
/** 提交阶段的可见错误（PATCH 失败 / 蒸馏接口报错）。 */
const distillErr = ref<string | null>(null);
/** 提交阶段的可见提示（「已提交」/「未选语料不蒸馏」）。 */
const distillNotice = ref<string | null>(null);
/** 用户对已完成/失败横幅的手动收起；跑起来会自动重新展开。 */
const distillDismissed = ref(false);
/**
 * 强制重新蒸馏开关：勾选后提交会带 force=true，绕过「项目缓存 / 蒸馏库」复用，
 * 真正重新调用模型。默认不勾 —— 命中缓存秒级返回才是预期行为，不该被当成 bug。
 */
const forceRedistill = ref(false);
/** 轮询定时器句柄；组件卸载时必须清掉，否则会在已销毁的组件上写 ref。 */
let distillTimer: ReturnType<typeof setInterval> | null = null;

// ── 文风蒸馏库（跨项目复用）状态 ──
const styleDistills = ref<StyleDistillSummary[]>([]);
const styleDistillsLoading = ref(false);
/** 正在被「应用」到本项目的库条目 id（按钮禁用 + 文案用）。 */
const styleDistillApplying = ref<number | null>(null);
const styleDistillMsg = ref<string | null>(null);
const styleDistillErr = ref<string | null>(null);

const POLL_MS = 1500;

/**
 * 进工作台时，多久之内**刚结束**的任务值得回填展示（毫秒）。
 *
 * 超出窗口的历史任务不再自动弹出：一条上周的旧报错每次进工作台都要先关掉，
 * 那是噪声不是信息。真正想看历史的人走任务列表。
 */
const RESUME_RESULT_WINDOW_MS = 3 * 60 * 1000;

/** pending / running 都算「还在跑」。 */
const distillActive = computed(
  () =>
    distillJob.value?.status === "pending" ||
    distillJob.value?.status === "running",
);

/** 任务档位的可读名（完整 / 平衡 / 快速）；查不到就退化成档位 key。 */
const distillTierName = computed(() => {
  const key = distillJob.value?.tier;
  if (!key) return null;
  return tiers.value.find((t) => t.key === key)?.name ?? key;
});

/**
 * 跨步骤常驻横幅是否显示：跑起来时强制显示，跑完之后等用户手动收起。
 *
 * 「跨步骤常驻」是硬需求：蒸馏期间允许用户继续下一步，若进度条只挂在
 * 步骤①里，一跳步就再也看不到——等于又回到「不知道开始没有」。
 */
const showDistillBanner = computed(
  () => !!distillJob.value && (distillActive.value || !distillDismissed.value),
);

function stopDistillPolling() {
  if (distillTimer !== null) {
    clearInterval(distillTimer);
    distillTimer = null;
  }
}

function startDistillPolling(jobId: number) {
  stopDistillPolling();
  distillTimer = setInterval(() => {
    void pollDistill(jobId);
  }, POLL_MS);
}

async function pollDistill(jobId: number) {
  try {
    const job = await api.getDistillJob(projectId, jobId);
    distillJob.value = job;
    if (job.status !== "pending" && job.status !== "running") {
      stopDistillPolling(); // 终态：停表，不再空转
    }
  } catch (err) {
    // 轮询失败**不**打断主流程、也不弹窗：下一 tick 会再试。
    // 真正的结论由服务端任务状态兜底（它不会永远停在 running，见后端注释）。
    console.warn("蒸馏进度轮询失败，下个周期重试：", err);
  }
}

/**
 * 进工作台时恢复上一次还没跑完的蒸馏。
 *
 * 取不到（老后端没这几个端点 / 网络问题）时静默降级为「无进度条」，
 * 绝不用假数据假装有一条在跑的任务。
 */
async function resumeDistillJob() {
  try {
    const r = await api.listDistillJobs(projectId, 1);
    const last = r.items?.[0];
    if (!last) return;
    if (last.status === "pending" || last.status === "running") {
      distillJob.value = last;
      distillDismissed.value = false;
      startDistillPolling(last.id);
      return;
    }
    // 已终态：**只回填这次会话内刚结束的**。给三分钟窗口，是为了让上一步
    // 点了蒸馏就跳走的用户回来能看到结论；超过这个窗口的历史任务不再弹出来
    // ——否则每次进工作台都要先关掉一条上周的旧报错，那是噪声不是信息。
    const finished = last.finished_at ? Date.parse(last.finished_at) : NaN;
    const fresh =
      !Number.isNaN(finished) && Date.now() - finished < RESUME_RESULT_WINDOW_MS;
    if (fresh) {
      distillJob.value = last;
      distillDismissed.value = false;
    }
  } catch {
    /* 静默：服务端尚未升级到蒸馏端点 */
  }
}

function toggleRef(id: number) {
  const i = selectedRefs.value.indexOf(id);
  if (i >= 0) selectedRefs.value.splice(i, 1);
  else selectedRefs.value.push(id);
}

/**
 * 选语料 + 选档位一起落库。
 *
 * 合成一次 PATCH：这两个是同一步的产物，分两次提交会让「语料存了、档位没存」
 * 这种中间态真实发生（第二次失败时尤其明显）。
 */
async function saveRefs() {
  const body: Record<string, unknown> = { reference_novel_ids: selectedRefs.value };
  // 只在清单真的拿到了、且选中的 key 确实在清单里时才写档位。
  // 否则宁可不写——写进去一个未知 key 会让后端 build-narrative 直接 400。
  const known = tiers.value.some((t) => t.key === effectiveTier.value);
  if (known) body.narrative_tier = effectiveTier.value;
  await patch(body);
  kgSourceOptions.value = buildSourceOptions();
}

// ── 文风蒸馏库（跨项目复用）──

/** 加载蒸馏库列表（active 态），供「应用」面板展示。 */
async function loadStyleDistills() {
  styleDistillsLoading.value = true;
  styleDistillErr.value = null;
  try {
    const r = await api.listStyleDistills("active");
    styleDistills.value = r.items;
  } catch (err) {
    // 蒸馏库是增强项：加载失败不该阻断主流程，静默降级即可。
    styleDistillErr.value = err instanceof Error ? err.message : String(err);
    styleDistills.value = [];
  } finally {
    styleDistillsLoading.value = false;
  }
}

/** 把蒸馏库某条目应用到本项目（复制其 style_narrative 进项目缓存）。 */
async function applyStyleDistill(id: number) {
  styleDistillApplying.value = id;
  styleDistillMsg.value = null;
  styleDistillErr.value = null;
  try {
    const updated = await api.applyStyleDistill(projectId, id);
    // 把应用结果直接写回本地项目态，让界面立刻反映「本项目已有文风档案」。
    if (project.value && updated) {
      project.value.style_narrative = updated.style_narrative ?? null;
      if (updated.narrative_tier != null) {
        project.value.narrative_tier = updated.narrative_tier;
      }
    }
    styleDistillMsg.value =
      "已应用该文风档案到本项目：生成时直接套用，无需重新蒸馏。";
    // 应用成功后清掉强制开关——本次「应用」已把文风档案落进项目缓存，
    // 再点「保存并开始蒸馏」若仍带着 force=true 会无谓地重新烧一遍 LLM。
    forceRedistill.value = false;
    await loadStyleDistills();
  } catch (err) {
    styleDistillErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    styleDistillApplying.value = null;
  }
}

/** 归档一条蒸馏库条目（不再被复用，但保留供查阅）。 */
async function archiveStyleDistill(id: number) {
  styleDistillApplying.value = id;
  styleDistillErr.value = null;
  try {
    await api.archiveStyleDistill(id);
    await loadStyleDistills();
  } catch (err) {
    styleDistillErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    styleDistillApplying.value = null;
  }
}

/**
 * 保存语料 + 档位，然后**提交后台蒸馏**。
 *
 * 这里与旧版的差别是修掉一个真实的竞态：旧版写的是
 * `saveRefs(); goStep(2)` —— `saveRefs()` 返回的 Promise 没人 await，
 * PATCH 还在飞就已经跳到步骤②，PATCH 失败更是变成 unhandled rejection、
 * 界面零提示。于是用户看到的就是「点了没生效就下一步了」。
 *
 * 现在：await 到底 + try/catch 出可见错误态 + 成功后立刻渲染进度条。
 * **不自动跳步** —— 先让人看见「已提交，后台运行中」，跳步交给用户决定。
 */
async function startDistillAndStay() {
  if (distillSubmitting.value) return;
  distillSubmitting.value = true;
  distillErr.value = null;
  distillNotice.value = null;
  try {
    await saveRefs();
    const started = await submitDistill(forceRedistill.value);
    // 未选语料（自由生成）时没有可蒸馏的东西 —— 保持旧版语义：直接往下走；
    // 真的发起了蒸馏才留下这一步，让人先看见「已提交、后台运行中」。
    if (!started) goStep(2);
  } catch (err) {
    distillErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    distillSubmitting.value = false;
  }
}

/**
 * 跳过（自由生成）：**明确不发起蒸馏**。
 *
 * 自由生成没有参考语料，蒸馏也无从下手（后端会 400）。这里同时把
 * 「未选语料 → 不蒸馏」这件事写进文案，让用户清楚两个按钮的区别不只是跳步。
 */
async function skipRefs() {
  if (distillSubmitting.value) return;
  distillSubmitting.value = true;
  distillErr.value = null;
  distillNotice.value = null;
  try {
    selectedRefs.value = [];
    await saveRefs();
    stopDistillPolling();
    distillJob.value = null;
    goStep(2);
  } catch (err) {
    distillErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    distillSubmitting.value = false;
  }
}

/**
 * 提交一次后台蒸馏。
 *
 * @returns true = 真的发起了任务；false = 条件不满足（未选语料），已给出说明。
 */
async function submitDistill(force = false): Promise<boolean> {
  if (!selectedRefs.value.length) {
    distillNotice.value = "未选参考语料 → 本次为自由生成，不发起蒸馏。";
    stopDistillPolling();
    distillJob.value = null;
    return false;
  }
  const r = await api.startDistill(projectId, effectiveTier.value || null, force);
  // 一旦真正发起了任务，立刻清掉强制开关：后台运行已在跑，复选框的使命结束。
  // 否则运行期间再点一次「保存并开始蒸馏」会带着 force=true 又起一条强制任务，
  // 等于把同一份语料烧两遍 LLM。下一轮若还想强制重算，用户重新勾即可。
  forceRedistill.value = false;
  // 立刻造一个乐观快照：**点下去就有反馈**，不等第一个轮询 tick 回来。
  // 少了这一步，用户点击后会有 1.5s 的「什么都没发生」窗口。
  const cacheNote =
    r.cache_source === "library"
      ? "✓ 已复用蒸馏库里已有的文风档案（同语料+同档位），本次未重新调用模型，秒级返回。"
      : "✓ 已复用本项目已缓存的文风档案（上次蒸馏结果），本次未重新调用模型，秒级返回。";
  distillJob.value = {
    id: r.job_id,
    project_id: projectId,
    tier: r.tier ?? effectiveTier.value ?? null,
    status: r.status,
    stage: r.reused
      ? "已在运行中，已切到该任务的进度"
      : r.from_cache
        ? "命中缓存，将直接复用"
        : "已提交，等待后台开始",
    progress_current: 0,
    progress_total: 0,
    n_chapters: null,
    rule_count: null,
    rejected: null,
    error: null,
    created_at: null,
    started_at: null,
    finished_at: null,
  };
  distillDismissed.value = false;
  distillNotice.value = r.reused
    ? "该档位的蒸馏已在运行中，已切到那条任务的进度。"
    : r.from_cache
      ? `${cacheNote} 如需重新提炼，请勾选「强制重新蒸馏」后再点一次。`
      : "已提交，蒸馏后台运行中 —— 可继续下一步，进度不会丢。";
  startDistillPolling(r.job_id);
  void pollDistill(r.job_id); // 立刻拉一次，不等第一个 tick
  return true;
}

async function nextFromGenre() {
  if (!project.value?.genre) return;
  await patch({ genre: project.value.genre });
  goStep(3);
}
async function nextFromStyle() {
  if (!project.value?.style) return;
  await patch({ style: project.value.style });
  goStep(4);
}

// —— 步骤 4：大概内容 / 让模型出方案 ——
async function runPropose() {
  proposing.value = true;
  proposingErr.value = null;
  proposalNote.value = null;
  try {
    const r = await api.proposePremise(projectId);
    proposals.value = r.proposals || [];
    proposalNote.value = r.note || null;
  } catch (err) {
    proposingErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    proposing.value = false;
  }
}
async function savePremise() {
  await patch({ premise: premiseDraft.value || null });
}

// —— 步骤 5：构思 ——
async function runAsk() {
  asking.value = true;
  questions.value = [];
  questionNote.value = null;
  for (const k of Object.keys(answers)) delete answers[k];
  try {
    const r = await api.expertQuestions(projectId);
    questions.value = r.questions || [];
    questionNote.value = r.note || null;
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  } finally {
    asking.value = false;
  }
}
async function buildBible() {
  buildingBible.value = true;
  try {
    if (project.value?.mode) await patch({ mode: project.value.mode });
    const ans = questions.value.length ? { ...answers } : null;
    const b = await api.buildBible(projectId, ans);
    bible.value = b;
    await patch({ plot_bible: b, status: "planned" });
    goStep(6);
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  } finally {
    buildingBible.value = false;
  }
}

// —— 步骤 6：生成（流式） ——
// 流式正文容器：仅当用户已经停在底部时才自动跟随，避免「页面被拽着往下滚」。
function followLiveBox() {
  const el = liveBox.value;
  if (!el) return;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  if (nearBottom) el.scrollTop = el.scrollHeight;
}

async function runGenerate() {
  streaming.value = true;
  streamedText.value = "";
  lastText.value = "";
  batchCount.value = 0;
  genKgMsg.value = null; // 新一轮开始：清掉上一轮的簿记提示，只反映本轮结果
  genKgLevel.value = "";
  try {
    for (let i = 0; i < chaptersPerGen.value; i++) {
      await api.generateStream(
        projectId,
        generationLength.value,
        (text) => {
          streamedText.value += text;
          followLiveBox(); // 仅在读者本就在底部时跟随，绝不强制下拉页面
        },
        async (meta) => {
          lastText.value = streamedText.value;
          batchCount.value = i + 1;
          // 流式 done 带 KG 簿记：就地（第 6 步）暴露，别藏到第 7 步。
          applyGenKg(meta.kg);
          await loadGenerations();
          refreshPriorIfOpen(); // 每段生成完前情都变了，面板开着就同步刷新
        },
      );
    }
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  } finally {
    streaming.value = false;
  }
}

async function runRegenerate(g: { id: number; segment_no: number }) {
  regenState.value[g.id] = { text: "", loading: true };
  genKgMsg.value = null; // 同上：本轮重生成的簿记结果覆盖上一轮提示
  genKgLevel.value = "";
  try {
    await api.regenerateStream(
      projectId,
      g.segment_no,
      generationLength.value,
      (text) => {
        regenState.value[g.id].text += text;
      },
      async (meta) => {
        // 重生成会全量 replay：簿记结果就地（第 6 步）暴露。
        applyGenKg(meta.kg);
        await loadGenerations();
        refreshPriorIfOpen(); // 重生成会重算受影响的卷摘要与主线，前情报告同步刷新
      },
    );
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  } finally {
    if (regenState.value[g.id]) regenState.value[g.id].loading = false;
  }
}

async function runExport() {
  try {
    await api.exportTxt(projectId, project.value?.title || "novel");
  } catch (err) {
    alert(err instanceof Error ? err.message : String(err));
  }
}

// ===========================================================================
// 前情预检：把「真正下发的那份前情」摆到台面上
// ---------------------------------------------------------------------------
// 为什么必须有这个面板：步骤 2 之后前情不再是「把所有摘要拼起来」，而是一个
// **有预算、分层、会丢东西**的构造物——丢了的卷、退化的档位、超预算的事实，
// 全都发生在用户看不见的地方。看不见的裁剪 = 用户只会觉得「它怎么又忘了」，
// 却永远定位不到原因。
//
// 设计要点：覆盖证明由**前端从渲染文本独立重算**，不转述后端的 report.uncovered。
// 后端若哪天回归（比如又出现信息空洞），这里会先报红——面板是哨兵，不是复读机。
// ===========================================================================
const priorOpen = ref(false);
const priorLoading = ref(false);
const priorErr = ref<string | null>(null);
const plan = ref<GeneratePlan | null>(null);
const priorTab = ref<"blocks" | "system" | "user">("blocks");

type PriorLayer = "spine" | "volume" | "recent" | "other";

interface PriorPart {
  layer: PriorLayer;
  head: string;
  body: string;
  chars: number;
  /** 该块声明覆盖的段区间（主线/卷/近情都有；识别不出的块为 null）。 */
  range: [number, number] | null;
}

const LAYER_META: Record<
  PriorLayer,
  { name: string; tier: string; desc: string }
> = {
  spine: {
    name: "主线骨架",
    tier: "L3",
    desc: "跨卷主线。优先级最高，永不被裁；只覆盖 ≤1 卷时故意不渲染（否则纯开销）。",
  },
  volume: {
    name: "卷情摘要",
    tier: "L2",
    desc: "每 8 段一卷。预算不够时丢最老的卷（它已被主线粗覆盖，丢了不留洞）。",
  },
  recent: {
    name: "近情逐段",
    tier: "L1",
    desc: "最近若干段的摘要，逐段保留。这是覆盖下限，不被裁。",
  },
  other: {
    name: "未识别块",
    tier: "??",
    desc: "前缀格式不符合约定，前情拼装可能出问题了，建议检查后端。",
  },
};

const RE_SPINE = /^\[主线(?:·覆盖至卷\s*(\d+)（段\s*(\d+)\s*-\s*(\d+)）)?\]\s*([\s\S]*)$/;
const RE_VOLUME = /^\[卷\s*(\d+)\s*[｜|]\s*段\s*(\d+)\s*[-–—~]\s*(\d+)\]\s*([\s\S]*)$/;
const RE_RECENT = /^\[近情·段\s*(\d+)\]\s*([\s\S]*)$/;

function parsePriorParts(blocks: string[]): PriorPart[] {
  return blocks.map((raw) => {
    const text = raw.replace(/\n+$/, "");
    let m = RE_VOLUME.exec(text);
    if (m) {
      return {
        layer: "volume",
        head: `卷 ${m[1]}｜段 ${m[2]}-${m[3]}`,
        body: m[4],
        chars: text.length,
        range: [Number(m[2]), Number(m[3])],
      };
    }
    m = RE_RECENT.exec(text);
    if (m) {
      const s = Number(m[1]);
      return {
        layer: "recent",
        head: `段 ${s}`,
        body: m[2],
        chars: text.length,
        range: [s, s],
      };
    }
    m = RE_SPINE.exec(text);
    if (m) {
      return {
        layer: "spine",
        head: m[2] ? `覆盖至卷 ${m[1]}（段 ${m[2]}-${m[3]}）` : "全篇",
        body: m[4],
        chars: text.length,
        range: m[2] ? [Number(m[2]), Number(m[3])] : null,
      };
    }
    return { layer: "other", head: "无法识别", body: text, chars: text.length, range: null };
  });
}

/** 段区间显示：单段写「17」，跨段写「1-8」——近情是逐段块，写成 17-17 是噪音。 */
function fmtRange(r: [number, number] | null): string {
  if (!r) return "—";
  return r[0] === r[1] ? String(r[0]) : `${r[0]}-${r[1]}`;
}

const priorParts = computed(() => parsePriorParts(plan.value?.prior_blocks || []));

/** 按层归组，用于「构成」表。渲染顺序保持前情块的原序。 */
const priorGroups = computed(() => {
  const g: Record<PriorLayer, PriorPart[]> = {
    spine: [],
    volume: [],
    recent: [],
    other: [],
  };
  for (const p of priorParts.value) g[p.layer].push(p);
  return g;
});

/** 分层字数（预算条按层堆叠着色，占比一眼可见）。 */
const priorBar = computed(() => {
  const total = Math.max(1, plan.value?.prior_report.chars || 1);
  const budget = Math.max(1, plan.value?.prior_report.budget_chars || 1);
  const order: PriorLayer[] = ["spine", "volume", "recent", "other"];
  const segs = order
    .map((layer) => {
      const chars = priorGroups.value[layer].reduce((a, p) => a + p.chars + 1, 0);
      return { layer, chars, pctOfBudget: (chars / budget) * 100 };
    })
    .filter((s) => s.chars > 0);
  const usedPct = Math.min(100, ((plan.value?.prior_report.chars || 0) / budget) * 100);
  return { segs, usedPct, total, budget };
});

/**
 * 覆盖证明：**前端独立重算**，只读渲染出来的前情块文本。
 *
 * 后端 report.uncovered 是「自报」；这里把同一件事从文本再算一遍，两者必须一致。
 * 不一致 = 模块的对外承诺破了个口子，面板立刻能看见。
 */
const coverageProof = computed(() => {
  const p = plan.value;
  const n = p?.prior_report.n_segments || 0;
  if (!p || !n) return null;

  const counts = new Array<number>(n + 1).fill(0);
  const coarse = new Set<number>();
  for (const part of priorParts.value) {
    if (!part.range) continue;
    if (part.layer === "spine") {
      for (let s = part.range[0]; s <= part.range[1]; s++) coarse.add(s);
    } else {
      for (let s = part.range[0]; s <= part.range[1]; s++) {
        if (s >= 1 && s <= n) counts[s] += 1;
      }
    }
  }

  const holes: number[] = [];
  for (let s = 1; s <= n; s++) {
    if (counts[s] === 0 && !coarse.has(s)) holes.push(s);
  }
  const multi: number[] = [];
  for (let s = 1; s <= n; s++) if (counts[s] > 1) multi.push(s);

  const contiguous =
    multi.length > 0 && multi[multi.length - 1] - multi[0] + 1 === multi.length;
  const tooDeep = multi.filter((s) => counts[s] > 2);

  // 与后端自报比对（排序后逐项相等才算互证通过）
  const reported = [...(p.prior_report.uncovered || [])].sort((a, b) => a - b);
  const agrees = holes.length === reported.length && holes.every((v, i) => v === reported[i]);

  return {
    n,
    holes,
    overlap: multi,
    overlapContiguous: contiguous,
    overlapTooDeep: tooDeep,
    agrees,
  };
});

/** 需要立刻报红/报黄的问题清单（severity: err 破坏承诺 · warn 值得知道）。 */
const priorIssues = computed(() => {
  const out: { severity: "err" | "warn"; text: string }[] = [];
  const p = plan.value;
  const proof = coverageProof.value;
  if (!p) return out;

  if (proof && proof.holes.length) {
    const head = proof.holes.slice(0, 8).join("、");
    const more = proof.holes.length > 8 ? `…共 ${proof.holes.length} 段` : "";
    out.push({
      severity: "err",
      text: `前情有洞：段 ${head}${more} 没有任何层覆盖。这些段的内容模型看不到，等于凭空失忆。`,
    });
  }
  if (proof && !proof.holes.length && !proof.agrees) {
    out.push({
      severity: "err",
      text: `覆盖自证不一致：前端独立重算是「无洞」，后端却自报 [${(p.prior_report.uncovered || []).join(", ")}]。两边必须一致，否则前情的正确性失去可核性。`,
    });
  }
  if (proof && proof.overlapTooDeep.length) {
    out.push({
      severity: "err",
      text: `重叠过深：段 ${proof.overlapTooDeep.slice(0, 8).join("、")} 被 3 层以上同时覆盖。已知的唯一合法重叠是「跨界卷摘要 × 近情窗口」一条 seam，不该更深。`,
    });
  }
  if (proof && proof.overlap.length && !proof.overlapContiguous) {
    out.push({
      severity: "err",
      text: `重叠区不连续（${proof.overlap.join("、")}）。合法重叠只能是紧贴窗口前沿的一段连续 seam，散开说明拼装逻辑出了问题。`,
    });
  }
  if (p.prior_report.degraded) {
    out.push({
      severity: "warn",
      text: `已退化为「全量逐段拼接」（${p.prior_report.flat_segments} 段）——粗层（卷情/主线）缺失时的兜底，吃预算但不留洞。下次滚上补齐后会自动回到有界档。`,
    });
  }
  if (p.prior_report.over_budget) {
    out.push({
      severity: "warn",
      text: `本次超出前情预算（${p.prior_report.chars} / ${p.prior_report.budget_chars} 字）：为保证覆盖完整，必留层优先于预算。想压回去就调小「近情段数」或等卷情滚上。`,
    });
  }
  if (p.prior_report.missing_l1 > 0) {
    out.push({
      severity: "warn",
      text: `有 ${p.prior_report.missing_l1} 段的摘要是占位符（原文缺失）。这些段在前情里是空白，不会被模型读到。`,
    });
  }
  if (p.prior_report.l2_dropped.length) {
    // 后端丢卷的顺序是「新卷先试」（range(vs, 0, -1)），展示按时间序读才顺
    const dropped = [...p.prior_report.l2_dropped].sort((a, b) => a - b);
    out.push({
      severity: "warn",
      text: `因预算丢了卷 ${dropped.join("、")} 的摘要（它已被主线骨架粗覆盖，不是空洞，只是这几卷的细节变粗了）。`,
    });
  }

  // —— 文风档案 ——
  // 档案的数字直接决定「像不像人类写的」这条线怎么划；它不可信时，
  // 后面所有基于它的判断都白做，所以必须和覆盖证明一样摆在明面上。
  const sp = p.style_profile;
  if (sp && !sp.available && sp.reason?.includes("拟合失败")) {
    out.push({
      severity: "warn",
      text: `文风档案不可用：${sp.reason}。本次 system 提示里没有文风块，生成只能靠【风格】的平台调性。`,
    });
  }
  if (sp?.available && !sp.cleaned && (sp.marks?.length || 0) > 0) {
    const m = sp.marks![0];
    out.push({
      severity: "err",
      text: `参考语料《${m.novel}》检出注入串「${m.text}」${m.count} 处，但并未剔除——下方数字已被污染（实测会让留出离散度虚高、判别力下降），不可作为验收基准。`,
    });
  }
  return out;
});

/** 文风档案摘要（面板专用；字段非空判断都收在这里，模板保持干净）。 */
const styleDigest = computed(() => {
  const sp = plan.value?.style_profile;
  if (!sp || !sp.available) return null;
  return {
    sp,
    scale: `${sp.n_train ?? 0} 训 / ${sp.n_holdout ?? 0} 留出（共 ${sp.n_units ?? 0} 单元）`,
    cleanedLine:
      (sp.removed_chars || 0) > 0
        ? `拟合前已清洗 ${(sp.removed_chars || 0).toLocaleString()} 字`
        : "无需清洗（未检出注入串）",
  };
});

const priorHealth = computed<"ok" | "warn" | "err">(() => {
  const list = priorIssues.value;
  if (list.some((i) => i.severity === "err")) return "err";
  if (list.length) return "warn";
  return "ok";
});

async function loadPlan() {
  priorLoading.value = true;
  priorErr.value = null;
  try {
    plan.value = await api.generatePlan(projectId);
  } catch (err) {
    priorErr.value = err instanceof Error ? err.message : String(err);
  } finally {
    priorLoading.value = false;
  }
}

function togglePrior() {
  priorOpen.value = !priorOpen.value;
  if (priorOpen.value && !plan.value) void loadPlan();
}

/** 生成完成后前情必然变了，面板开着就顺手刷新（没开则不多发一次请求）。 */
function refreshPriorIfOpen() {
  if (priorOpen.value) void loadPlan();
}

// —— 步骤 7：图谱 ——
const kgSourceOptions = ref<{ value: string; label: string }[]>([]);
function buildSourceOptions(): { value: string; label: string }[] {
  const opts: { value: string; label: string }[] = [
    { value: "generated", label: "我的小说（生成后）" },
  ];
  for (const nid of selectedRefs.value) {
    const n = novels.value.find((x) => x.id === nid);
    opts.push({
      value: `novel:${nid}`,
      label: `参考语料·${n?.title || nid}`,
    });
  }
  return opts;
}
// KG 簿记提示：**两组状态，绝不串屏**。
//  - 第 7 步：`kgMsg` / `kgMsgLevel` —— 只管「手动建图」与「图加载报错」。
//  - 第 6 步：`genKgMsg` / `genKgLevel` —— 只管「生成 / 重生成」流式 done 的簿记。
//    必须就地渲染（与生成按钮同屏）：把失败藏到第 7 步 = 藏在交互后面。
// 判据（两组共用）：
//  - `rebuilt === false` → 整体重建抛异常，文案走「簿记重建失败」（含 reason，可缺）。
//  - `complete === false` → 有失败段，文案走「簿记不全」（含段号列表）。
// `complete !== false`（旧后端不返回该键）视为成功，绝不误报。
const KG_WARN_TAIL = "未完成。请重试；若反复失败，请检查数据库是否需要迁移后重试。";
function kgFailedNums(kg?: KgBookkeeping): string {
  return (kg?.failed ?? []).map((f) => f.segment_no).join("、");
}
function kgIncompleteText(kg?: KgBookkeeping): string {
  return `簿记不全：${kg?.replayed ?? 0}/${kg?.segments ?? 0} 段成功，段 ${kgFailedNums(kg)} ${KG_WARN_TAIL}`;
}
function kgRebuildFailText(reason?: string | null): string {
  return `簿记重建失败：本段正文已保存，但图谱/账本未更新${reason ? `（${reason}）` : ""}。请重试。`;
}
function applyKgOk(kg: KgBookkeeping) {
  kgMsg.value = `生成后图谱已重建：${kg.replayed ?? 0}/${kg.segments ?? 0} 段`;
  kgMsgLevel.value = "ok";
}
function applyKgWarn(kg?: KgBookkeeping) {
  kgMsg.value = kgIncompleteText(kg);
  kgMsgLevel.value = "warn";
}
/** 第 6 步的流式 done 簿记：只在「有问题」时落一条 warn，成功后清空（不刷屏）。 */
function applyGenKg(kg?: KgBookkeeping) {
  if (!kg) return;
  if (kg.rebuilt === false) {
    genKgMsg.value = kgRebuildFailText(kg.reason);
    genKgLevel.value = "warn";
  } else if (kg.complete === false) {
    genKgMsg.value = kgIncompleteText(kg);
    genKgLevel.value = "warn";
  }
}
async function buildGeneratedKg() {
  buildingKg.value = true;
  kgMsg.value = null;
  kgMsgLevel.value = "";
  try {
    const r = await api.buildKgFromGenerations(projectId);
    if (r.complete !== false) applyKgOk(r);
    else applyKgWarn(r);
  } catch (err) {
    kgMsg.value = err instanceof Error ? err.message : String(err);
    kgMsgLevel.value = "err";
  } finally {
    buildingKg.value = false;
  }
}
async function loadGraph() {
  try {
    graph.value = await api.getKg(projectId, "all", kgSource.value);
    characters.value = (graph.value.nodes || []).filter(
      (n) => n.type === "character",
    ) as any;
    plotholes.value = (graph.value.nodes || []).filter(
      (n) => n.type === "plothole",
    ) as any;
  } catch (err) {
    kgMsg.value = err instanceof Error ? err.message : String(err);
    kgMsgLevel.value = "err";
  }
}

onMounted(async () => {
  await Promise.all([loadProject(), loadNovels(), loadTiers()]);
  kgSourceOptions.value = buildSourceOptions();
  await loadGenerations();
  restoreStep(); // 续接上次进度，不再每次回到步骤①
  // 前情预检：**进工作台就拉一次**，让折叠头直接带信号。
  // 只在已有段落时才拉——空项目没有前情可看，白跑一次 RAG。
  // 必须提前拉的原因：面板的价值在于「有洞/退化时你还没点开就看得见」；
  // 若要等用户点开才发现，就等于把最该暴露的失败藏在了交互后面。
  // 代价可控：generate-plan 只组装上下文，不调用 LLM。
  if (generations.value.length) void loadPlan();
  // 哨兵告警：进工作台就拉，理由同上 —— 硬冲突要「还没读到那一段就看得见」
  void loadAlerts();
  // 蒸馏进度恢复：离开页面再回来不能丢进度（后台任务在服务端照跑）。
  void resumeDistillJob();
  // 蒸馏库列表：进工作台就拉，应用面板才有东西可选。
  void loadStyleDistills();
  // 模型徽标：失败静默（不影响主流程）
  try {
    llmConfig.value = await api.getLlmConfig();
  } catch {
    /* 忽略：徽标缺失不阻塞工作台 */
  }
});

// 轮询定时器必须在组件销毁时清掉，否则会对着已卸载的组件继续写 ref。
onUnmounted(stopDistillPolling);
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">02 · Workspace</div>
    <div class="title-block">
      <template v-if="editingTitle">
        <input
          class="input title-input"
          v-model="titleDraft"
          @keyup.enter="saveTitle"
          @blur="saveTitle"
        />
      </template>
      <template v-else>
        <h1 class="title-click" @click="startEditTitle">
          {{ project?.title ?? "项目 #" + projectId }}
        </h1>
        <button class="link-btn" @click="startEditTitle">重命名</button>
      </template>
      <span v-if="completed" class="tag ok">已完成</span>
      <span v-else class="tag">创作中</span>
      <span
        v-if="llmConfig"
        class="tag model-badge"
        :class="llmConfig.effective.is_fallback ? 'warn' : 'info'"
        title="当前生效的生成模型，点击去设置"
        @click="router.push('/settings')"
      >
        {{ llmConfig.effective.is_fallback ? "本地兜底" : llmConfig.effective.label }} ·
        <span class="mono">{{ llmConfig.effective.model }}</span>
      </span>
    </div>
    <p>
      向导式创作：选参考语料（可跳过→自由生成）→ 类型 → 风格 → 内容 → 构思 → 生成。
      知识图谱只在生成后出现，且按「参考语料 / 生成后」各自成图，绝不混在一起。
    </p>
    <div class="row" style="margin-top: 10px">
      <button class="btn" :class="completed ? '' : 'primary'" @click="toggleComplete">
        {{ completed ? "重新开启创作" : "保存并标记完成" }}
      </button>
      <!-- 助手已独立成页（/projects/:id/assistant）：这里只留入口，七步向导一行不动 -->
      <button class="btn" @click="router.push(`/projects/${projectId}/assistant`)">
        打开助手工作台 →
      </button>
    </div>
  </header>

  <div v-if="loadError" class="notice err">{{ loadError }}</div>

  <!-- 步骤指示器 -->
  <div class="stepper">
    <button
      v-for="(s, i) in STEPS"
      :key="s"
      class="step"
      :class="{ active: step === i + 1, done: i + 1 < step }"
      :disabled="i + 1 > maxStep"
      @click="goStep(i + 1)"
    >
      <span class="num">{{ i + 1 }}</span>{{ s }}
      <!-- 步骤①上的蒸馏徽标：跨步骤常驻横幅之外，这里再冗余一份。
           文字在前（"蒸馏中"），色点只做冗余强化 → 灰度/色弱下照样读得出。 -->
      <span v-if="i === 0 && distillActive" class="step-distill">
        <span class="step-dot" aria-hidden="true"></span>蒸馏中
      </span>
    </button>
  </div>

  <!-- 蒸馏进度（跨步骤常驻）：蒸馏期间允许用户继续下一步，
       所以进度条不能只挂在步骤①里 —— 一跳步就看不见，等于白做。 -->
  <div v-if="showDistillBanner && distillJob" class="distill-banner">
    <DistillProgress
      :job="distillJob"
      :tier-name="distillTierName"
      :compact="step !== 1"
    />
    <button
      v-if="!distillActive"
      class="link-btn distill-dismiss"
      @click="distillDismissed = true"
    >
      收起
    </button>
  </div>

  <!-- ============ 步骤 1：参考语料 ============ -->
  <section v-if="step === 1" class="card">
    <div class="card-title">① 参考语料（可选，可多选）</div>
    <p class="muted" style="font-size: var(--fs-sm); margin-bottom: var(--gap)">
      选中哪些，生成时<strong>只</strong>抽取这些语料的风格；不选则完全自由生成，不会混入任何语料。
      每本语料后续会建成<strong>各自独立</strong>的图谱。
    </p>
    <div v-if="!novels.length" class="empty">语料库为空，可跳过本步。</div>
    <div class="chips">
      <button
        v-for="n in novels"
        :key="n.id"
        class="chip"
        :class="{ on: selectedRefs.includes(n.id) }"
        @click="toggleRef(n.id)"
      >
        {{ n.title || "无题" }}
        <span class="faint mono" style="font-size: var(--fs-xs)">#{{ n.id }}</span>
      </button>
    </div>
    <div class="row" style="margin-top: var(--gap)">
      <button class="btn primary" :disabled="distillSubmitting" @click="startDistillAndStay">
        {{ distillSubmitting ? "正在提交…" : "保存并开始蒸馏" }}
      </button>
      <!-- 语义明确：跳过 = **不发起**蒸馏，直接自由生成。文案写明，不只是换了个跳转。 -->
      <button class="btn" :disabled="distillSubmitting" @click="skipRefs">
        跳过（自由生成，不蒸馏）
      </button>
      <button class="btn ghost" @click="goStep(2)">
        下一步 →<span v-if="distillActive">（蒸馏后台继续）</span>
      </button>
    </div>

    <!-- 提交结果必须**可见**：旧版 `saveRefs(); goStep(2)` 既不 await 也不 catch，
         PATCH 失败时界面毫无反应，这正是「点了也不知道开始没有」的根源。 -->
    <div v-if="distillErr" class="notice err distill-inline">
      保存或提交蒸馏失败：{{ distillErr }}
    </div>
    <div v-else-if="distillNotice" class="notice info distill-inline">
      {{ distillNotice }}
    </div>

    <!-- 蒸馏进度（步骤①内嵌完整版）：与跨步骤常驻横幅共用同一份 job 状态 -->
    <DistillProgress
      v-if="distillJob"
      :job="distillJob"
      :tier-name="distillTierName"
      class="distill-inline"
    />

    <!-- ============ 蒸馏档位（与选语料同一步落库） ============ -->
    <div class="tier-block">
      <div class="card-title" style="font-size: var(--fs-md)">
        蒸馏档位<span class="hint">完整档最准，但最贵</span>
      </div>
      <p class="muted" style="font-size: var(--fs-sm); margin-bottom: var(--gap)">
        档位决定蒸馏时<strong>读多少语料、提炼多少维度</strong>。默认「完整」最准，
        但它是<strong>耗时最长</strong>的一档；只是先看看管线通不通，就选「快速」。
        各档的成本差异见卡片下方。
      </p>

      <div v-if="tiersErr" class="empty">
        档位清单加载失败（{{ tiersErr }}），本次将沿用项目已存的档位。
      </div>

      <div v-else class="tier-list">
        <button
          v-for="c in tierCards"
          :key="c.tier.key"
          class="tier"
          :class="{ on: effectiveTier === c.tier.key }"
          :aria-pressed="effectiveTier === c.tier.key"
          @click="selectedTier = c.tier.key"
        >
          <!-- 文字标签 + 色标双编码：色弱/灰度下靠这行字区分档位，不靠颜色单打独斗 -->
          <span class="tier-head">
            <span class="tier-name">{{ c.tier.name }}</span>
            <span class="tag" :class="c.meta.tag">{{ c.meta.label }}</span>
            <span v-if="c.isDefault" class="tag">默认</span>
          </span>

          <!-- 「什么时候用这一档」——后端 description 原文，不在前端重写 -->
          <span class="tier-desc">{{ c.tier.description }}</span>

          <span class="tier-facts">
            <span class="tier-fact">{{ c.sampling }}</span>
            <span class="tier-fact">{{ c.tier.dimension_count }} 个维度</span>
            <span class="tier-fact">{{ c.nameNorm }}</span>
            <span class="tier-fact">{{ c.cleaning }}</span>
          </span>

          <span v-if="c.cost" class="tier-cost">{{ c.cost }}</span>

          <span class="tier-key mono faint">{{ c.tier.key }}</span>
        </button>
      </div>

      <!-- 强制重新蒸馏：默认不勾。命中缓存秒级复用是预期行为，不该被当成 bug；
           只有用户明确要求重算时才传 force=true，真正重新调模型并走真实进度。 -->
      <div class="row force-redistill" style="margin-top: var(--gap); align-items: center; gap: 12px">
        <label class="checkbox">
          <input type="checkbox" v-model="forceRedistill" />
          强制重新蒸馏（忽略已缓存的文风档案，重新调用模型）
        </label>
        <button
          v-if="forceRedistill"
          class="btn sm ghost"
          type="button"
          @click="forceRedistill = false"
        >取消强制</button>
      </div>
      <p v-if="forceRedistill" class="notice warn distill-inline">
        已开启强制重算：本次会真正重新调用模型蒸馏，耗时与费用按所选档位正常发生。
      </p>
    </div>

    <!-- ============ 文风蒸馏库（跨项目复用） ============ -->
    <div class="library-block">
      <div class="card-title" style="font-size: var(--fs-md)">
        文风蒸馏库<span class="hint">蒸馏一次 · 全局共享 · 其它项目直接复用</span>
      </div>
      <p class="muted" style="font-size: var(--fs-sm); margin-bottom: var(--gap)">
        已经蒸馏过的文风档案会存到这里，<strong>全局共享</strong>：任何项目都能应用，
        不限创建它的项目。给别的项目选了<strong>同样的语料与档位</strong>，
        点「应用」即可秒级套用，不必再跑一遍分钟级蒸馏。每条都标明了<strong>蒸馏程度</strong>（完整/平衡/快速）。
      </p>

      <div v-if="styleDistillsLoading" class="faint">正在加载蒸馏库…</div>
      <div v-else-if="!styleDistills.length" class="empty">
        蒸馏库还是空的。在步骤①点「保存并开始蒸馏」一次（真跑一遍），结果就会进库、之后可复用。
      </div>
      <ul v-else class="lib-list">
        <li v-for="d in styleDistills" :key="d.id" class="lib-item">
          <div class="lib-meta">
            <span class="tag" :class="d.tier">{{ d.tier_name || d.tier }}</span>
            <span class="faint mono">#{{ d.id }}</span>
            <span class="faint">语料 {{ (d.source_ids || []).join("、") || "—" }}</span>
            <span class="faint">蒸馏程度：{{ d.tier_name || d.tier }}档 · {{ d.n_chapters ?? 0 }} 章 · {{ d.rule_count ?? 0 }} 条规则</span>
          </div>
          <div class="lib-actions">
            <button
              class="btn sm"
              :disabled="styleDistillApplying === d.id"
              @click="applyStyleDistill(d.id)"
            >{{ styleDistillApplying === d.id ? "应用中…" : "应用到本项目" }}</button>
            <button
              v-if="d.status === 'active'"
              class="btn sm ghost"
              :disabled="styleDistillApplying === d.id"
              @click="archiveStyleDistill(d.id)"
            >归档</button>
          </div>
        </li>
      </ul>

      <div v-if="styleDistillMsg" class="notice info distill-inline">{{ styleDistillMsg }}</div>
      <div v-if="styleDistillErr" class="notice err distill-inline">{{ styleDistillErr }}</div>
    </div>
  </section>

  <!-- ============ 步骤 2：类型 ============ -->
  <section v-else-if="step === 2" class="card">
    <div class="card-title">② 类型</div>
    <div class="chips">
      <button
        v-for="g in GENRES"
        :key="g"
        class="chip"
        :class="{ on: project?.genre === g }"
        @click="project && (project.genre = g)"
      >
        {{ g }}
      </button>
    </div>
    <div class="row" style="margin-top: var(--gap)">
      <button class="btn ghost" @click="goStep(1)">← 上一步</button>
      <button
        class="btn primary"
        :disabled="!project?.genre"
        @click="nextFromGenre"
      >
        下一步 →
      </button>
    </div>
  </section>

  <!-- ============ 步骤 3：风格 ============ -->
  <section v-else-if="step === 3" class="card">
    <div class="card-title">③ 风格平台调性</div>
    <div class="chips">
      <button
        v-for="s in STYLES"
        :key="s"
        class="chip"
        :class="{ on: project?.style === s }"
        @click="project && (project.style = s)"
      >
        {{ s }}
      </button>
    </div>
    <div class="row" style="margin-top: var(--gap)">
      <button class="btn ghost" @click="goStep(2)">← 上一步</button>
      <button
        class="btn primary"
        :disabled="!project?.style"
        @click="nextFromStyle"
      >
        下一步 →
      </button>
    </div>
  </section>

  <!-- ============ 步骤 4：大概内容 ============ -->
  <section v-else-if="step === 4" class="card">
    <div class="card-title">④ 小说大概内容（可选）</div>
    <p class="muted" style="font-size: var(--fs-sm); margin-bottom: var(--gap)">
      不写也没关系：点「让模型出方案」，会基于前面选的类型/风格给出几个设定供你挑选或改写。
    </p>
    <textarea
      class="input"
      style="min-height: 120px"
      v-model="premiseDraft"
      placeholder="例如：一个没落宗门的少年，在祖祠里唤醒了一缕上古残魂……"
    />
    <div v-if="proposingErr" class="notice err" style="margin-top: 12px">{{ proposingErr }}</div>
    <div v-if="proposals.length" class="proposals">
      <div class="card-title" style="font-size: var(--fs-sm)">方案（点选即填入）</div>
      <div
        v-for="(p, i) in proposals"
        :key="i"
        class="proposal"
        @click="pickProposal(p)"
      >
        {{ p }}
      </div>
    </div>
    <div v-if="proposalNote" class="notice warn" style="margin-top: 12px">{{ proposalNote }}</div>
    <div class="row" style="margin-top: var(--gap)">
      <button class="btn ghost" @click="goStep(3)">← 上一步</button>
      <button class="btn" :disabled="proposing" @click="runPropose">
        {{ proposing ? "生成中…" : "让模型出方案" }}
      </button>
      <button class="btn primary" @click="savePremise(); goStep(5)">保存并构思 →</button>
    </div>
  </section>

  <!-- ============ 步骤 5：构思剧情 ============ -->
  <section v-else-if="step === 5" class="card">
    <div class="card-title">⑤ 构思剧情圣经</div>
    <div class="field" style="margin-bottom: var(--gap)">
      <label>构思方式</label>
      <div class="chips">
        <button
          class="chip"
          :class="{ on: project?.mode === 'quick' }"
          @click="project && (project.mode = 'quick')"
        >
          直接构思
        </button>
        <button
          class="chip"
          :class="{ on: project?.mode === 'expert' }"
          @click="project && (project.mode = 'expert')"
        >
          先追问再构思
        </button>
      </div>
      <p class="faint" style="font-size: var(--fs-xs); margin-top: 8px">
        {{
          project?.mode === "expert"
            ? "先让模型抛出几个关键追问、你来回答，再据此构思——想得更透，适合长线故事。"
            : "跳过追问，直接根据类型 / 风格 / 大概内容构思——最快拿到剧情圣经。"
        }}
      </p>
    </div>

    <div v-if="project?.mode === 'expert'" class="block">
      <button class="btn" :disabled="asking" @click="runAsk">
        {{ asking ? "生成追问中…" : (questions.length ? "重新生成追问" : "获取追问清单") }}
      </button>
      <div v-if="questions.length" class="qa-list">
        <div v-for="(q, i) in questions" :key="i" class="qa">
          <label>Q{{ i + 1 }}. {{ q }}</label>
          <input class="input" v-model="answers[q]" :placeholder="`回答 ${i + 1}`" />
        </div>
      </div>
      <div v-if="questionNote" class="notice warn" style="margin-top: 12px">{{ questionNote }}</div>
    </div>

    <button
      class="btn primary"
      style="margin-top: var(--gap)"
      :disabled="buildingBible"
      @click="buildBible"
    >
      {{ buildingBible ? "构思中…" : "合成剧情圣经 →" }}
    </button>

    <div v-if="bible" class="bible">
      <div class="card-title" style="font-size: var(--fs-md)">剧情圣经（已落库）</div>
      <div class="bible-row"><span class="k">梗概</span><span class="v prose">{{ bible.synopsis }}</span></div>
      <div class="bible-row"><span class="k">走向</span><span class="v prose">{{ bible.direction }}</span></div>
      <div class="bible-row" v-if="bible.characters?.length">
        <span class="k">角色</span>
        <span class="v">
          <span v-for="c in bible.characters" :key="c.name" class="tag accent">{{ c.name }} · {{ c.role }}</span>
        </span>
      </div>
      <div class="bible-row" v-if="bible.relationships?.length">
        <span class="k">关系</span>
        <span class="v">
          <span v-for="(r, i) in bible.relationships" :key="i" class="tag info">{{ r.a }} ↔ {{ r.b }}：{{ r.relation }}</span>
        </span>
      </div>
      <div class="bible-row" v-if="bible.conflicts?.length">
        <span class="k">冲突</span>
        <span class="v"><span v-for="c in bible.conflicts" :key="c" class="tag warn">{{ c }}</span></span>
      </div>
      <div class="bible-row" v-if="bible.foreshadowing?.length">
        <span class="k">伏笔</span>
        <span class="v"><span v-for="f in bible.foreshadowing" :key="f" class="tag">{{ f }}</span></span>
      </div>
      <div v-if="bible.note" class="notice warn" style="margin-top: 12px">{{ bible.note }}</div>
    </div>

    <div class="row" style="margin-top: var(--gap)">
      <button class="btn ghost" @click="goStep(4)">← 上一步</button>
    </div>
  </section>

  <!-- ============ 步骤 6：生成控制台 ============ -->
  <section v-else-if="step === 6" class="card">
    <div class="card-title">⑥ 生成控制台</div>
    <div class="row" style="align-items: flex-start">
      <div class="field" style="width: 200px">
        <label>单次生成字数（长文单段，避免多段压缩）</label>
        <input class="input" type="number" min="500" step="500" v-model.number="generationLength" />
      </div>
      <div class="field" style="width: 160px">
        <label>一次生成段数</label>
        <select class="input" v-model.number="chaptersPerGen">
          <option :value="1">1 段</option>
          <option :value="2">2 段</option>
          <option :value="3">3 段</option>
          <option :value="5">5 段</option>
        </select>
      </div>
      <div class="field" style="flex: 1; min-width: 220px">
        <label>本段参考语料</label>
        <div class="ref-summary">
          <span v-if="!selectedRefs.length" class="tag">自由生成（无参考）</span>
          <span v-for="nid in selectedRefs" :key="nid" class="tag accent">
            {{ novels.find((x) => x.id === nid)?.title || nid }}
          </span>
        </div>
      </div>
    </div>
    <div class="row" style="margin-top: var(--gap)">
      <button class="btn ghost" @click="goStep(5)">← 上一步</button>
      <button class="btn primary" :disabled="streaming" @click="runGenerate">
        {{ streaming ? "续写中…" : `生成 ${chaptersPerGen} 段` }}
      </button>
      <button class="btn" :disabled="!generations.length" @click="runExport">导出 txt</button>
    </div>

    <!-- 生成 / 重生成的 KG 簿记结果：就地在动作发生的那一步暴露，不留到第 7 步 -->
    <div
      v-if="genKgMsg"
      :class="['notice', genKgLevel || 'info']"
      style="margin-top: var(--gap)"
    >{{ genKgMsg }}</div>

    <!-- ---------- 前情预检：真正下发的那份前情长什么样 ---------- -->
    <div class="prior" :class="priorHealth">
      <div class="prior-head">
        <button class="prior-toggle" :aria-expanded="priorOpen" @click="togglePrior">
          <span class="caret" :class="{ open: priorOpen }">▸</span>
          <span class="prior-title">前情注入预检</span>

          <template v-if="plan">
            <span class="prior-stat mono">
              {{ plan.prior_report.chars.toLocaleString() }} /
              {{ plan.prior_report.budget_chars.toLocaleString() }} 字
            </span>
            <span v-if="plan.prior_report.n_segments" class="prior-stat mono faint">
              覆盖 1–{{ plan.prior_report.n_segments }}
            </span>
            <span class="tag" :class="priorHealth">
              {{
                priorHealth === "ok"
                  ? "完整"
                  : priorHealth === "warn"
                    ? `${priorIssues.length} 处提醒`
                    : `${priorIssues.length} 处问题`
              }}
            </span>
          </template>
          <span v-else-if="priorLoading" class="prior-stat faint">读取中…</span>
          <span v-else class="prior-stat faint">点开看看这次到底喂了什么给模型</span>
        </button>
        <button class="prior-refresh" :disabled="priorLoading" @click="loadPlan">刷新</button>
      </div>

      <div v-if="priorOpen" class="prior-body">
        <div v-if="priorErr" class="notice err">{{ priorErr }}</div>

        <template v-else-if="plan">
          <!-- 问题清单：有洞/退化/超预算/丢卷，全部摆明 -->
          <div v-for="(iss, i) in priorIssues" :key="i" class="notice" :class="iss.severity">
            {{ iss.text }}
          </div>
          <div v-if="!priorIssues.length" class="notice ok">
            前情覆盖 1–{{ plan.prior_report.n_segments }} 无洞，分层与预算都正常。
          </div>

          <!-- 预算条：按层堆叠，谁占了多少一眼可见 -->
          <div class="bar-wrap">
            <div class="bar" :class="{ over: plan.prior_report.over_budget }">
              <span
                v-for="s in priorBar.segs"
                :key="s.layer"
                class="bar-seg"
                :class="'l-' + s.layer"
                :style="{ width: s.pctOfBudget + '%' }"
                :title="`${LAYER_META[s.layer].name}：${s.chars} 字（占预算 ${s.pctOfBudget.toFixed(0)}%）`"
              ></span>
              <span
                class="bar-rest"
                :style="{ width: Math.max(0, 100 - priorBar.usedPct) + '%' }"
              ></span>
            </div>
            <div class="bar-legend">
              <span v-for="s in priorBar.segs" :key="s.layer" class="lg">
                <i class="swatch" :class="'l-' + s.layer"></i>
                {{ LAYER_META[s.layer].name }}
                <b class="mono">{{ s.chars }}</b> 字
              </span>
              <span class="lg faint">
                <i class="swatch"></i>未用预算
                <b class="mono">{{ Math.max(0, plan.prior_report.budget_chars - plan.prior_report.chars) }}</b> 字
              </span>
              <span v-if="plan.prior_report.over_budget" class="lg over-mark">
                超出 <b class="mono">{{ plan.prior_report.chars - plan.prior_report.budget_chars }}</b> 字
              </span>
            </div>
          </div>

          <!-- 分层构成表 -->
          <table class="prior-table">
            <thead>
              <tr>
                <th>层</th>
                <th>条数</th>
                <th>覆盖段</th>
                <th>字数</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="layer in ['spine', 'volume', 'recent', 'other'] as PriorLayer[]" :key="layer">
                <template v-if="priorGroups[layer].length">
                  <td>
                    <span class="layer-dot" :class="'l-' + layer"></span>
                    <strong>{{ LAYER_META[layer].name }}</strong>
                    <span class="faint mono" style="font-size: var(--fs-xs)">{{ LAYER_META[layer].tier }}</span>
                  </td>
                  <td class="mono">{{ priorGroups[layer].length }}</td>
                  <td class="mono">
                    {{ priorGroups[layer].map((p) => fmtRange(p.range)).join(" · ") }}
                  </td>
                  <td class="mono">{{ priorGroups[layer].reduce((a, p) => a + p.chars + 1, 0) }}</td>
                  <td class="faint" style="font-size: var(--fs-xs)">{{ LAYER_META[layer].desc }}</td>
                </template>
              </tr>
            </tbody>
          </table>

          <!-- 其它约束层（步骤 1 的账本产物，也在同一个 prompt 里） -->
          <div class="prior-chips">
            <span class="faint" style="font-size: var(--fs-xs)">同时注入的约束</span>
            <span class="tag" :class="plan.constraints.characters.length ? 'accent' : ''">
              锁定角色 {{ plan.constraints.characters.length }}
            </span>
            <span class="tag" :class="plan.constraints.plotholes.length ? 'warn' : ''">
              待填坑 {{ plan.constraints.plotholes.length }}
            </span>
            <span class="tag info">参考语料片段 {{ plan.rag_context.length }}</span>
            <span v-if="plan.constraints.characters.length" class="faint" style="font-size: var(--fs-xs)">
              {{ plan.constraints.characters.join("、") }}
            </span>
          </div>

          <!-- 独立算子：前端自己从渲染文本重算覆盖，跟后端自报互证 -->
          <div v-if="coverageProof" class="proof">
            <span class="faint" style="font-size: var(--fs-xs); font-weight: 700">覆盖自证（前端独立重算，非转述后端）</span>
            <div class="proof-line">
              <span class="tag" :class="coverageProof.holes.length ? 'err' : 'ok'">
                {{ coverageProof.holes.length ? `有洞 ${coverageProof.holes.length} 段` : "1–" + coverageProof.n + " 无洞" }}
              </span>
              <span class="tag" :class="coverageProof.agrees ? 'ok' : 'err'">
                {{ coverageProof.agrees ? "与后端自报一致" : "与后端自报不一致" }}
              </span>
              <span class="tag" :class="coverageProof.overlapTooDeep.length ? 'err' : 'info'">
                重叠 {{ coverageProof.overlap.length }} 段
                {{ coverageProof.overlap.length ? `（段 ${coverageProof.overlap[0]}–${coverageProof.overlap[coverageProof.overlap.length - 1]}，有意 seam）` : "" }}
              </span>
            </div>
          </div>

          <!-- 文风档案：system 里那份「照着数字写」的块从哪来、可不可信 -->
          <div class="style-block">
            <span class="faint" style="font-size: var(--fs-xs); font-weight: 700">
              文风档案（从参考语料实测，整轮固定注入 system）
            </span>
            <template v-if="styleDigest">
              <div class="proof-line">
                <span class="tag" :class="styleDigest.sp.cleaned ? 'ok' : 'warn'">
                  {{ styleDigest.cleanedLine }}
                </span>
                <span class="tag info">粒度 {{ styleDigest.sp.unit_chars }} 字</span>
                <span class="tag info">{{ styleDigest.scale }}</span>
                <span
                  v-if="styleDigest.sp.holdout_p90 != null"
                  class="tag info"
                  title="参考语料里人写的留出单元到档案的距离 p90。生成段报的分位以它为参照，不是随手定的阈值。"
                >
                  留出 p90 {{ styleDigest.sp.holdout_p90.toFixed(3) }}
                </span>
              </div>
              <div
                v-for="m in styleDigest.sp.marks || []"
                :key="m.text"
                class="notice"
                :class="styleDigest.sp.cleaned ? 'warn' : 'err'"
              >
                《{{ m.novel }}》检出注入串「{{ m.text }}」<b class="mono">{{ m.count }}</b> 处
                （句尾比 <b class="mono">{{ m.final_ratio.toFixed(3) }}</b>，
                实际剔除 <b class="mono">{{ m.final }}</b> 处）{{
                  styleDigest.sp.cleaned ? "——已在拟合前剔除。" : "——未剔除，下方数字已被污染。"
                }}
                <span v-if="m.samples?.length" class="faint mono style-sample">{{ m.samples[0] }}</span>
              </div>
              <pre class="style-text mono">{{ styleDigest.sp.text }}</pre>
            </template>
            <div v-else class="notice" :class="plan.style_profile?.reason?.includes('自由生成') ? '' : 'warn'">
              {{ plan.style_profile?.reason || "未取到文风档案。" }}
            </div>
          </div>

          <!-- 硬冲突规则：哪几条**当前真的会响**。
               不摆出来，用户会把「没告警」读成「都检查过了」，而实际有规则是默认沉默的。 -->
          <div v-if="plan.sentinel" class="style-block">
            <span class="faint" style="font-size: var(--fs-xs); font-weight: 700">
              硬冲突规则（生成后自动跑，只标红不改正文）
            </span>
            <div class="proof-line">
              <span
                v-for="(r, name) in plan.sentinel.rules"
                :key="name"
                class="tag"
                :class="r.active ? 'ok' : 'warn'"
                :title="r.desc + (r.note ? '｜' + r.note : '')"
              >
                {{ name }}
              </span>
            </div>
            <div
              v-for="(r, name) in plan.sentinel.rules"
              v-show="!r.active"
              :key="'n-' + name"
              class="notice warn"
            >
              <b class="mono">{{ name }}</b> 当前不参与判定 —— {{ r.note }}
            </div>
            <div class="faint" style="font-size: var(--fs-xs); margin-top: 4px">
              境界阶梯：{{ plan.sentinel.realm_systems }} 套 /
              <b class="mono">{{ plan.sentinel.realm_words }}</b> 个写法（多套之间不可比）。
              {{ plan.sentinel.note }}
            </div>
          </div>

          <!-- prompts 原文 -->
          <div class="tabs" style="margin-bottom: 10px">
            <button :class="{ active: priorTab === 'blocks' }" @click="priorTab = 'blocks'">
              注入的前情（{{ plan.prior_blocks.length }} 块）
            </button>
            <button :class="{ active: priorTab === 'system' }" @click="priorTab = 'system'">
              系统提示词（{{ plan.system_prompt.length }} 字）
            </button>
            <button :class="{ active: priorTab === 'user' }" @click="priorTab = 'user'">
              用户提示词（{{ plan.user_prompt.length }} 字）
            </button>
          </div>
          <div v-if="priorTab === 'blocks'" class="promptbox">
            <template v-for="(part, i) in priorParts" :key="i">
              <span class="blk-head" :class="'l-' + part.layer">[{{ i + 1 }}] {{ LAYER_META[part.layer].name }} · {{ part.head }}</span>
              {{ part.body }}
              <span v-if="i < priorParts.length - 1">&#10;&#10;</span>
            </template>
            <span v-if="!priorParts.length" class="faint">（首段，无前情）</span>
          </div>
          <div v-else-if="priorTab === 'system'" class="promptbox">{{ plan.system_prompt }}</div>
          <div v-else class="promptbox">{{ plan.user_prompt }}</div>
        </template>
      </div>
    </div>

    <div v-if="streaming || lastText" class="card" style="margin-top: var(--gap); box-shadow: none">
      <div class="card-title" style="font-size: var(--fs-md)">
        本次生成内容
        <span v-if="streaming" class="hint">
          正在生成… 已 {{ streamedText.length }} 字（第 {{ batchCount }}/{{ chaptersPerGen }} 段）
        </span>
        <span v-else class="hint">{{ (lastText || "").length }} 字</span>
        <button class="btn sm" style="margin-left: 10px" @click="expandLive = !expandLive">
          {{ expandLive ? "收起" : "展开全文" }}
        </button>
      </div>
      <div
        ref="liveBox"
        class="prose live-box"
        :class="{ expanded: expandLive }"
      >{{ streaming ? streamedText : lastText }}</div>
    </div>

    <!-- 规则哨兵告警：只标红，不改正文。
         放在段落列表上方，是因为它是「读到那一段之前就该知道」的信号：
         死人又说话了、境界倒回去了 —— 这类硬伤埋在正文里读者才不管你写得多好。
         没告警时只留一行状态（含未生效规则），不占版面。 -->
    <div class="card" style="margin-top: var(--gap); box-shadow: none">
      <div class="card-title" style="font-size: var(--fs-md)">
        硬冲突检查
        <span v-if="alertView" class="hint">
          {{ alertView.err }} 条错误<template v-if="alertView.warn"> · {{ alertView.warn }} 条待看</template>
        </span>
        <span v-else class="hint">未发现硬冲突</span>
      </div>

      <div class="proof-line" style="margin-bottom: 6px">
        <span class="tag" :class="alertView?.err ? 'err' : 'ok'">
          {{ alertView ? "需处理" : "通过" }}
        </span>
        <span class="tag info">零 LLM 规则判定（只标红，不改正文）</span>
      </div>

      <div v-if="alertView" class="list">
        <div v-for="(a, i) in alertView.items" :key="i" class="notice err">
          <b>第 {{ a.segment_no }} 段 · <span class="mono">{{ a.entity }}</span> · {{ a.rule }}</b>
          <div style="margin-top: 4px">{{ a.message }}</div>
          <div v-if="a.proof" class="faint" style="margin-top: 4px">
            依据：{{ a.proof }}
          </div>
        </div>
      </div>

      <!-- 未生效的规则必须显式列出来：否则「没告警」会被读成「都检查过了」 -->
      <div v-if="inactiveRules.length" class="notice warn">
        以下规则<b>当前不参与判定</b>，别把「没告警」读成「没问题」：
        <div v-for="r in inactiveRules" :key="r.name" style="margin-top: 4px">
          <b class="mono">{{ r.name }}</b> —— {{ r.note }}
        </div>
      </div>
      <div v-else-if="alertsErr" class="faint" style="font-size: var(--fs-xs)">
        告警未取到（{{ alertsErr }}）——不影响生成。
      </div>
    </div>

    <!-- 已生成段落：分页 + 单段重生成 -->
    <div class="card" style="margin-top: var(--gap); box-shadow: none">
      <div class="card-title" style="font-size: var(--fs-md)">
        已生成段落 <span class="hint">{{ generations.length }} 段</span>
      </div>

      <div class="row" style="margin-bottom: 12px; flex-wrap: wrap; align-items: center">
        <span class="faint" style="font-size: var(--fs-xs)">每页</span>
        <select class="input sm" v-model.number="pageSize" @change="page = 1">
          <option :value="5">5</option>
          <option :value="10">10</option>
          <option :value="20">20</option>
        </select>
        <span class="faint" style="font-size: var(--fs-xs)">条 · 第 {{ page }} / {{ totalPages }} 页</span>
        <button class="btn sm" :disabled="page <= 1" @click="page--">← 上一页</button>
        <button class="btn sm" :disabled="page >= totalPages" @click="page++">下一页 →</button>
      </div>

      <div v-if="!generations.length" class="empty">还没有生成记录，点「生成一段」开始。</div>
      <div class="list">
        <div
          v-for="g in pagedGens"
          :key="g.id"
          class="list-item"
          style="align-items: flex-start; flex-direction: column; gap: 8px"
        >
          <div class="row tight" style="width: 100%">
            <span class="tag accent mono" style="flex: none">第 {{ g.segment_no }} 段</span>
            <span class="faint mono" style="font-size: var(--fs-xs)">#{{ g.id }}</span>
            <span style="flex: 1"></span>
            <button
              class="btn sm"
              :disabled="regenState[g.id]?.loading || streaming"
              @click="runRegenerate(g)"
            >
              {{ regenState[g.id]?.loading ? "重生成中…" : "重新生成" }}
            </button>
            <button class="btn sm" @click="toggleSeg(g.id)">
              {{ expandedSegs[g.id] ? "收起" : "展开" }}
            </button>
          </div>
          <div
            v-if="regenState[g.id]?.loading"
            class="prose live-box"
            style="width: 100%"
          >{{ regenState[g.id]?.text }}</div>
          <div
            v-else
            class="prose seg-text"
            :class="{ expanded: expandedSegs[g.id] }"
          >{{ g.text }}</div>
        </div>
      </div>
    </div>

    <div class="row" style="margin-top: var(--gap)">
      <button class="btn" @click="goStep(7)">下一步：看知识图谱 →</button>
    </div>
  </section>

  <!-- ============ 步骤 7：知识图谱（生成后） ============ -->
  <section v-else class="card">
    <div class="card-title">
      ⑦ 知识图谱（生成后才出现）
      <button class="btn" :disabled="buildingKg" @click="buildGeneratedKg">
        {{ buildingKg ? "建图中…" : "建/重建「我的小说」图谱" }}
      </button>
    </div>
    <div v-if="kgMsg" :class="['notice', kgMsgLevel || 'info']" style="margin-bottom: 12px">{{ kgMsg }}</div>
    <div class="row" style="margin-bottom: var(--gap)">
      <div class="field" style="flex: 1; min-width: 220px">
        <label>查看哪张图（各自独立，不混）</label>
        <select class="input" v-model="kgSource" @change="loadGraph">
          <option v-for="o in kgSourceOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
      <button class="btn" @click="loadGraph">刷新</button>
    </div>

    <div v-if="!graph" class="empty">先点上方「建/重建『我的小说』图谱」。</div>
    <div v-else class="grid-2">
      <div>
        <div class="card-title" style="font-size: var(--fs-md)">角色 <span class="hint">{{ characters.length }}</span></div>
        <div class="list">
          <div v-for="c in characters" :key="c.id" class="list-item">
            <strong>{{ c.props?.name }}</strong>
            <span class="faint mono" style="font-size: var(--fs-xs)">提及 {{ c.props?.mentions }} · 第 {{ c.props?.first_chapter }}–{{ c.props?.last_chapter }} 章</span>
          </div>
          <div v-if="!characters.length" class="empty">无角色节点</div>
        </div>
      </div>
      <div>
        <div class="card-title" style="font-size: var(--fs-md)">未填坑 <span class="hint">{{ plotholes.length }}</span></div>
        <div class="list">
          <div v-for="p in plotholes" :key="p.id" class="list-item" style="align-items: flex-start">
            <div style="flex: 1">
              <div class="prose" style="font-size: var(--fs-sm)">{{ p.props?.question }}</div>
              <span class="faint mono" style="font-size: var(--fs-xs)">首现第 {{ p.props?.first_chapter }} 章</span>
            </div>
            <span class="tag" :class="p.props?.status === 'open' ? 'warn' : 'ok'">{{ p.props?.status }}</span>
          </div>
          <div v-if="!plotholes.length" class="empty">无坑节点</div>
        </div>
      </div>
    </div>

    <div class="row" style="margin-top: var(--gap)">
      <button class="btn ghost" @click="goStep(6)">← 返回生成</button>
    </div>
  </section>
</template>

<style scoped>
.page-head .title-block {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.model-badge {
  cursor: pointer;
  transition: border-color 0.15s ease;
}
.model-badge:hover {
  border-color: var(--accent);
}
.title-input {
  font-size: var(--fs-2xl);
  font-weight: 800;
  padding: 4px 10px;
  min-width: 320px;
}
.title-click {
  cursor: pointer;
}
.title-click:hover {
  color: var(--accent-ink);
  text-decoration: underline dotted;
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
}
.link-btn:hover {
  text-decoration: underline;
}
/* 强制重蒸馏勾选项：与 .row 同高、可点击、文字不换行 */
.checkbox {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: var(--fs-sm);
  cursor: pointer;
  user-select: none;
}
.checkbox input {
  width: 16px;
  height: 16px;
  cursor: pointer;
  accent-color: var(--accent, #c0392b);
}
.force-redistill {
  flex-wrap: wrap;
}
.stepper {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: var(--gap-lg);
}
.step {
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 600;
  padding: 7px 12px;
  border-radius: var(--radius-full);
  border: var(--border-w) solid var(--border);
  background: var(--bg-elev);
  color: var(--fg-muted);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.step:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.step .num {
  display: inline-flex;
  width: 18px;
  height: 18px;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-full);
  background: var(--bg-sunken);
  font-family: var(--font-mono);
}
.step.active {
  background: var(--accent);
  color: var(--accent-fg);
  border-color: var(--accent);
}
.step.active .num {
  background: rgba(255, 255, 255, 0.25);
  color: var(--accent-fg);
}
.step.done .num {
  background: var(--accent-soft);
  color: var(--accent-ink);
}
/* ---------- 蒸馏进度（跨步骤常驻） ---------- */
/* 步骤①上的小徽标：蒸馏在后台跑时，用户跳到别的步骤也能一眼看到
   「还在跑」。**文字在前**（"蒸馏中"），圆点只是冗余强化 —— 灰度/色弱下
   照样读得出来，不靠颜色单打独斗。 */
.step-distill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: 2px;
  font-size: var(--fs-xs);
  font-weight: 700;
  color: var(--accent-ink);
  background: var(--accent-soft);
  border: var(--border-w) solid var(--accent-line);
  border-radius: var(--radius-full);
  padding: 0 7px;
}
.step.active .step-distill {
  color: var(--accent-fg);
  background: rgba(255, 255, 255, 0.25);
  border-color: rgba(255, 255, 255, 0.4);
}
.step-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  animation: distill-pulse 1.1s ease-in-out infinite;
}
@keyframes distill-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.25;
  }
}
@media (prefers-reduced-motion: reduce) {
  .step-dot {
    animation: none;
  }
}
.distill-banner {
  position: relative;
  margin-bottom: var(--gap);
}
.distill-dismiss {
  position: absolute;
  top: 8px;
  right: 12px;
  font-size: var(--fs-xs);
}
.distill-inline {
  margin-top: var(--gap);
}
/* ---------- 文风蒸馏库（跨项目复用） ---------- */
.library-block {
  margin-top: calc(var(--gap) * 1.5);
  padding-top: var(--gap);
  border-top: var(--border-w) solid var(--border);
}
.lib-list {
  list-style: none;
  margin: var(--gap) 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.lib-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
}
.lib-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  font-size: var(--fs-sm);
}
.lib-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
/* 复用 distill 的小号按钮观感，避免再定义一套 .btn.sm 样式 */
.btn.sm {
  padding: 6px 12px;
  font-size: var(--fs-sm);
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
/* ---------- 蒸馏档位选择 ---------- */
/* 卡片式而非 chip 式：三档要并排比较的是「什么时候用」和「要等多久」，
   一行 pill 装不下这些说明文字。 */
.tier-block {
  margin-top: var(--gap);
  padding-top: var(--gap);
  border-top: var(--border-w) solid var(--border);
}
.tier-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 10px;
}
.tier {
  font: inherit;
  text-align: left;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 12px 14px;
  border-radius: var(--radius-md);
  border: var(--border-w) solid var(--border-strong);
  background: var(--bg);
  color: var(--fg);
}
.tier:hover {
  border-color: var(--accent);
}
.tier.on {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.tier-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.tier-name {
  font-size: var(--fs-sm);
  font-weight: 700;
}
.tier-desc {
  font-size: var(--fs-xs);
  line-height: 1.5;
  color: var(--fg-muted);
}
.tier-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
}
.tier-fact {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  background: var(--bg-sunken);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-full);
  padding: 1px 8px;
}
.tier-cost {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--fg-muted);
}
.tier-key {
  font-size: var(--fs-xs);
}
.proposals {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.proposal {
  padding: 12px 14px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-sunken);
  font-size: var(--fs-sm);
  cursor: pointer;
  line-height: 1.7;
}
.proposal:hover {
  border-color: var(--accent);
}
.block {
  border-left: 3px solid var(--accent-line);
  padding-left: 14px;
  margin-bottom: var(--gap);
}
.qa-list {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.qa {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.qa label {
  font-size: var(--fs-sm);
  color: var(--fg-muted);
  font-weight: 600;
}
.bible {
  margin-top: var(--gap);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bible-row {
  display: grid;
  grid-template-columns: 64px 1fr;
  gap: 12px;
  align-items: start;
}
.bible-row .k {
  font-size: var(--fs-xs);
  color: var(--fg-faint);
  font-weight: 700;
  padding-top: 2px;
}
.bible-row .v {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.ref-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding-top: 8px;
}
.live-box {
  /* 阅读区默认给足高度（随视口自适应），长文不再被 360px 小框憋住 */
  max-height: min(62vh, 820px);
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg-sunken);
  border-radius: var(--radius-md);
  padding: 18px 20px;
  line-height: 2;
  font-size: var(--fs-md);
}
.live-box.expanded,
.seg-text.expanded {
  max-height: none;
}
.seg-text {
  width: 100%;
  max-height: min(46vh, 560px);
  overflow: auto;
  background: var(--bg-sunken);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  line-height: 2;
  font-size: var(--fs-md);
}
.btn.sm,
.input.sm {
  font-size: var(--fs-xs);
  padding: 5px 10px;
}
.input.sm {
  width: auto;
  padding: 4px 8px;
}
.grid-2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap);
}
/* ---------- 前情预检 ---------- */
/* 分层配色按**优先级**编码：越不会被裁的层用越重的色。
   朱红 = 主线（最高优先，永不被裁）· 蓝 = 近情（覆盖下限，不被裁）
   · 灰 = 卷情（唯一会被预算丢掉的层）——颜色本身就在讲「谁可能消失」。 */
.prior {
  margin-top: var(--gap);
  border: var(--border-w) solid var(--border);
  border-left: 4px solid var(--border-strong);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
  overflow: hidden;
}
.prior.warn {
  border-left-color: var(--warn);
}
.prior.err {
  border-left-color: var(--danger);
}
.prior-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 10px 4px 4px;
}
.prior-toggle {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font: inherit;
  text-align: left;
  background: none;
  border: none;
  color: var(--fg);
  cursor: pointer;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}
.prior-toggle:hover {
  background: var(--bg-sunken);
}
.prior-toggle .caret {
  display: inline-block;
  transition: transform 0.15s ease;
  color: var(--fg-faint);
  font-size: var(--fs-sm);
}
.prior-toggle .caret.open {
  transform: rotate(90deg);
}
.prior-title {
  font-weight: 800;
  font-size: var(--fs-sm);
}
.prior-stat {
  font-size: var(--fs-xs);
}
.prior-refresh {
  font: inherit;
  font-size: var(--fs-xs);
  font-weight: 600;
  flex: none;
  background: none;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-full);
  color: var(--fg-muted);
  padding: 4px 12px;
  cursor: pointer;
}
.prior-refresh:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent-ink);
}
.prior-refresh:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.prior-body {
  padding: 4px 16px 18px;
  border-top: var(--border-w) solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.prior-body > .notice {
  margin: 0;
}

/* 预算条：按层堆叠 */
.bar-wrap {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar {
  display: flex;
  height: 14px;
  border-radius: var(--radius-full);
  overflow: hidden;
  background: var(--bg-sunken);
  border: var(--border-w) solid var(--border);
}
.bar.over {
  border-color: var(--danger);
}
.bar-seg {
  height: 100%;
  /* flex:none 是必须的——默认的 flex-shrink 会把超预算的条按比例压回 100%，
     于是「超出预算」这件事实在图上被抹平，只剩数字还诚实。 */
  flex: none;
  transition: width 0.2s ease;
}
.bar-rest {
  height: 100%;
  flex: none;
  background: var(--bg-sunken);
}
.over-mark {
  color: var(--danger);
  font-weight: 700;
}
.swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  flex: none;
}
.bar-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.bar-legend .lg {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.bar-legend b {
  color: var(--fg);
}

/* 分层配色（优先级梯度） */
.l-spine {
  background: var(--accent);
}
.l-recent {
  background: var(--info);
}
.l-volume {
  background: var(--fg-faint);
}
.l-other {
  background: var(--border-strong);
}
.layer-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  margin-right: 7px;
  vertical-align: baseline;
}

/* 构成表 */
.prior-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-xs);
}
.prior-table th {
  text-align: left;
  font-weight: 700;
  color: var(--fg-faint);
  padding: 6px 10px 6px 0;
  border-bottom: var(--border-w) solid var(--border);
  white-space: nowrap;
}
.prior-table td {
  padding: 8px 10px 8px 0;
  border-bottom: var(--border-w) solid var(--border);
  vertical-align: top;
}
.prior-table tr:last-child td {
  border-bottom: none;
}
.prior-table td:first-child {
  white-space: nowrap;
}

.prior-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.proof {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 4px;
}
.proof-line {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.blk-head {
  color: var(--fg-faint);
  font-weight: 700;
}

.style-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: 4px;
}
.style-sample {
  display: block;
  margin-top: 4px;
  font-size: var(--fs-xs);
  word-break: break-all;
}
/* 下发正文用 pre：等宽 + 保留换行，一眼看出 system 里块的真实排版。
   限高可滚，避免 20 行统计把面板撑爆。 */
.style-text {
  margin: 0;
  padding: 10px 12px;
  max-height: 220px;
  overflow: auto;
  font-size: var(--fs-xs);
  line-height: 1.65;
  white-space: pre-wrap;
  color: var(--fg);
  background: var(--bg-sunken, rgba(0, 0, 0, 0.03));
  border: 1px solid var(--border);
  border-radius: 6px;
}

@media (max-width: 720px) {
  .grid-2 {
    grid-template-columns: 1fr;
  }
  .prior-table td:last-child,
  .prior-table th:last-child {
    display: none;
  }
}
</style>
