<script setup lang="ts">
defineOptions({ name: "KgWorkspace" });
import { computed, nextTick, onActivated, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import ProjectNav from "@/components/ProjectNav.vue";
import type {
  KgStoryTimelineResponse,
  KgV3BookScopeResponse,
  KgV3Character,
  KgV3CorpusWindowsResponse,
  KgV3EventsResponse,
  KgV3JudgeFinding,
  KgV3JudgeFindingsResponse,
  KgV3Relation,
  KgV3Slice,
  KgWorldviewEntity,
  KgWorldviewResponse,
  NovelOut,
  ProjectOut,
  StyleNarrativeConflictsResponse,
} from "@/api/types";
import { buildCorpusStatus } from "@/utils/corpusStatus.js";
import { buildBookScopeStatus, chapterTag } from "@/utils/kgChapter.js";

const route = useRoute();
const projectId = Number(route.params.id);

// ---------- Tabs ----------
const TABS = [
  { key: "slice", label: "切片预览" },
  { key: "intel", label: "人物情报" },
  { key: "events", label: "事件时间线" },
  { key: "story", label: "世界观与时间线" },
  { key: "judge", label: "Judge 面板" },
  { key: "graph", label: "关系与节奏" },
  { key: "worker", label: "Worker 状态" },
  { key: "style", label: "Style 审计" },
] as const;
type TabKey = (typeof TABS)[number]["key"];
// vue-tsc 5.x 推断 ref<TabKey>(literal) 会把 T 缩到字面量类型，下方 v-if 改用 (activeTab as string) === "style" 绕过。
// 保留 `as TabKey` 双重断言确保 ref 真正按 union 处理。
const activeTab = ref<TabKey>("slice" as TabKey);
// 每个 tab 只在该页生命周期内加载一次：来回点标签不再重复打接口。
// 数据变了用各 tab 自带的「刷新」显式重拉，而不是靠切换标签顺手重拉。
const loadedTabs = reactive<Record<string, boolean>>({ slice: true });
const loadingTabs = reactive<Record<string, boolean>>({});
/** 标签内容滚动容器：切换标签时回到顶部，避免新标签从中段开始。 */
const kgBodyEl = ref<HTMLElement | null>(null);

const TAB_LOADERS: Record<TabKey, () => Promise<unknown> | void> = {
  slice: loadSlice,
  intel: loadCharacters,
  events: loadEvents,
  story: loadStory,
  judge: loadFindings,
  graph: loadGraphTab,
  worker: loadWorker,
  style: loadStyleWarnings,
};

function switchTab(k: TabKey) {
  activeTab.value = k;
  // 一屏布局下内容区是独立滚动容器：换标签必须回到顶部。
  void nextTick(() => {
    if (kgBodyEl.value) kgBodyEl.value.scrollTop = 0;
  });
  if (loadedTabs[k] || loadingTabs[k]) return;
  loadingTabs[k] = true;
  Promise.resolve(TAB_LOADERS[k]()).finally(() => {
    loadingTabs[k] = false;
    // 失败也标记为已加载：用户可点「刷新」重试，避免每次切回来都自动重跑。
    loadedTabs[k] = true;
  });
}

// ---------- 语料摄取窗口状态条（只读） ----------
// failed>0 必须显式显示失败，不能退化成「窗口内无实体」；空表说「尚未摄取」。
const corpusWindows = ref<KgV3CorpusWindowsResponse | null>(null);
const corpusWindowsErr = ref<string | null>(null);
const corpusStatusView = computed(() => buildCorpusStatus(corpusWindows.value));

async function loadCorpusWindows() {
  corpusWindowsErr.value = null;
  try {
    corpusWindows.value = await api.kgV3CorpusWindows(projectId);
  } catch (e) {
    corpusWindowsErr.value = e instanceof Error ? e.message : String(e);
  }
}

// ---------- 书维度状态条（只读） ----------
// 「章号未知」与「区间漂移」是两件不同的事实，必须分开显示。
const bookScope = ref<KgV3BookScopeResponse | null>(null);
const bookScopeErr = ref<string | null>(null);
const bookScopeView = computed(() => buildBookScopeStatus(bookScope.value));

async function loadBookScope() {
  bookScopeErr.value = null;
  try {
    bookScope.value = await api.kgV3BookScope(projectId);
  } catch (e) {
    bookScopeErr.value = e instanceof Error ? e.message : String(e);
  }
}

// ---------- Tab 1 切片预览 ----------
const sliceSeg = ref(1);
const sliceWindow = ref(5);
const sliceMaxEnt = ref(20);
const sliceChars = ref("");
const slice = ref<KgV3Slice | null>(null);
const slicePrompt = ref("");
const sliceLoading = ref(false);
const sliceErr = ref<string | null>(null);
const sliceCopied = ref(false);

async function loadSlice() {
  sliceLoading.value = true;
  sliceErr.value = null;
  try {
    const chars = sliceChars.value.trim() || null;
    const opts = {
      window: sliceWindow.value,
      maxEntities: sliceMaxEnt.value,
      characters: chars,
    };
    const [sl, pr] = await Promise.all([
      api.kgV3Slice(projectId, sliceSeg.value, opts),
      api.kgV3SlicePrompt(projectId, sliceSeg.value, opts),
    ]);
    slice.value = sl;
    slicePrompt.value = pr.text;
  } catch (e) {
    sliceErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    sliceLoading.value = false;
  }
}

async function copyPrompt() {
  try {
    await navigator.clipboard.writeText(slicePrompt.value);
    sliceCopied.value = true;
    setTimeout(() => (sliceCopied.value = false), 1500);
  } catch {
    /* 剪贴板不可用时静默 */
  }
}

// ---------- Tab 2 人物情报 ----------
const characters = ref<KgV3Character[]>([]);
const charsLoading = ref(false);
const charsErr = ref<string | null>(null);
const selectedChar = ref<KgV3Character | null>(null);
const charKnows = ref<string[]>([]);
const charKnowsLoading = ref(false);

const knowFact = ref("");
const knowCharacter = ref("");
const knowResult = ref<boolean | null>(null);
const knowErr = ref<string | null>(null);
const knowChecking = ref(false);

async function loadCharacters() {
  charsLoading.value = true;
  charsErr.value = null;
  try {
    characters.value = (await api.kgV3Characters(projectId)).characters;
  } catch (e) {
    charsErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    charsLoading.value = false;
  }
}

async function openChar(c: KgV3Character) {
  selectedChar.value = c;
  charKnowsLoading.value = true;
  try {
    const sl = await api.kgV3Slice(projectId, 999999, { characters: c.name });
    charKnows.value = sl.known_facts[0]?.knows ?? [];
  } catch {
    charKnows.value = [];
  } finally {
    charKnowsLoading.value = false;
  }
}

async function checkKnows() {
  if (!knowCharacter.value.trim() || !knowFact.value.trim()) return;
  knowChecking.value = true;
  knowErr.value = null;
  try {
    const r = await api.kgV3Knows(projectId, knowCharacter.value.trim(), knowFact.value.trim());
    knowResult.value = r.knows;
  } catch (e) {
    knowErr.value = e instanceof Error ? e.message : String(e);
    knowResult.value = null;
  } finally {
    knowChecking.value = false;
  }
}

// ---------- Tab 3 事件时间线 ----------
const EVENT_TYPES = [
  "", "incident", "revelation", "foreshadow_plant", "foreshadow_payoff",
  "death", "romance_milestone", "battle", "decision", "time_marker",
];
const eventTypeFilter = ref("");
const events = ref<KgV3EventsResponse | null>(null);
const eventsLoading = ref(false);
const eventsErr = ref<string | null>(null);

/** event id → index（因果链引用解析用）。 */
const evIndex = computed(() => {
  const m = new Map<number, number>();
  events.value?.events.forEach((e, i) => m.set(e.sequence, i));
  return m;
});

const seqToId = computed(() => {
  // 后端 events 端点的 links 用 event.id，列表项只有 sequence——
  // id 与 sequence 在本项目内是同序自增，但不假设相等：
  // 用 links 两端在列表里的出现位置反解（找不到的链不渲染）。
  return new Map<number, number>();
});
const linksByTarget = computed(() => {
  // 直接用 id→sequence 不可得，退而求其次：按链的 from/to id 在
  // events[].sequence 上做同值匹配（本项目 sequence 连续自增且从 1 起，
  // 与 id 同序——若未来不同序，这里显示会缺失但不报错）。
  const m = new Map<number, { link_type: string; from_seq: number }[]>();
  if (!events.value) return m;
  const seqOf = new Map<number, number>();
  events.value.events.forEach((e) => seqOf.set(e.sequence, e.sequence));
  for (const lk of events.value.links) {
    const fromSeq = seqOf.get(lk.from_event_id);
    if (fromSeq === undefined) continue;
    const arr = m.get(lk.to_event_id) ?? [];
    arr.push({ link_type: lk.link_type, from_seq: fromSeq });
    m.set(lk.to_event_id, arr);
  }
  return m;
});

async function loadEvents() {
  eventsLoading.value = true;
  eventsErr.value = null;
  try {
    events.value = await api.kgV3Events(projectId, 200, eventTypeFilter.value || null);
  } catch (e) {
    eventsErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    eventsLoading.value = false;
  }
}

// ---------- 世界观与剧情时间线（纯读） ----------
const storyProject = ref<ProjectOut | null>(null);
const storyNovels = ref<NovelOut[]>([]);
/** "book" = 本书（生成正文/续写书）；"ref" = 参考书。 */
const storyMode = ref<"book" | "ref">("book");
const storyRefId = ref<number | null>(null);
/** 本书对应的语料 id：续写项目就是 active/continuation 那本；纯生成项目为 null。 */
const storyBookNovelId = computed<number | null>(() =>
  storyProject.value?.active_novel_id
  ?? storyProject.value?.continuation_novel_id
  ?? null,
);
/** 当前实际查询的 novel_id（null = 纯生成项目的本书）。 */
const storyNovelId = computed<number | null>(() =>
  storyMode.value === "book" ? storyBookNovelId.value : storyRefId.value,
);
/** 参考书下拉项：去掉与「本书」同一本，避免同一个选项出现两次。 */
const storyRefOptions = computed(() =>
  storyNovels.value.filter((n) => n.id !== storyBookNovelId.value),
);
const storySelectValue = computed(() =>
  storyMode.value === "book" ? "book" : String(storyRefId.value ?? ""),
);
const storyBookLabel = computed(() => {
  const title = storyNovels.value.find((n) => n.id === storyBookNovelId.value)?.title;
  return title ? `本书（生成正文）· ${title}` : "本书（生成正文）";
});
const worldview = ref<KgWorldviewResponse | null>(null);
const storyTimeline = ref<KgStoryTimelineResponse | null>(null);
const storyLoading = ref(false);
const storyErr = ref<string | null>(null);
const storyFrom = ref(1);
const storyOffset = ref(0);
const STORY_LIMIT = 200;

const worldviewGroups = computed(() => {
  const w = worldview.value;
  if (!w) return [];
  return [
    { key: "factions", label: "势力 / 组织", items: w.factions },
    { key: "locations", label: "地点", items: w.locations },
    { key: "power_systems", label: "体系与境界", items: w.power_systems },
    { key: "resources", label: "资源 / 物品", items: w.resources },
    { key: "rules", label: "世界规则", items: w.rules },
  ];
});

/** 作者定义体系的境界阶梯（来自「力量体系」页，不是 KG）。 */
function definedRealms(it: KgWorldviewEntity): string[] {
  const raw = it.attrs?.境界;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((r) => (typeof r === "string" ? r : String((r as { 名称?: string })?.名称 ?? "")))
    .filter(Boolean);
}

/** 作者定义的档位层（名称/档位/人数）。 */
function definedTiers(it: KgWorldviewEntity): string[] {
  const raw = it.attrs?.档位层;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((t) => {
      const o = t as { 名称?: string; 档位?: string; 人数?: string };
      return [o?.名称, o?.档位, o?.人数].filter(Boolean).join(" / ");
    })
    .filter(Boolean);
}

/** 当前是否在看「本书」（生成正文/续写书）。 */
const storyIsBook = computed(() => storyMode.value === "book");

/** 本书还没有任何 KG 时给一句明确解释，而不是让五组全显示（空）。 */
const storyEmpty = computed(() => {
  if (!worldview.value || !storyTimeline.value) return false;
  const entityCount = worldviewGroups.value.reduce((n, g) => n + g.items.length, 0);
  return storyIsBook.value && entityCount === 0 && (storyTimeline.value.total ?? 0) === 0;
});

function onStoryBookChange(e: Event) {
  const raw = (e.target as HTMLSelectElement).value;
  if (raw === "book") {
    storyMode.value = "book";
    storyRefId.value = null;
  } else {
    storyMode.value = "ref";
    storyRefId.value = Number(raw);
  }
  storyOffset.value = 0;
  void refreshStory();
}

async function loadStoryData() {
  const [w, t] = await Promise.all([
    api.kgV3Worldview(projectId, storyNovelId.value),
    api.kgV3StoryTimeline(projectId, storyNovelId.value, {
      fromChapter: storyFrom.value,
      offset: storyOffset.value,
      limit: STORY_LIMIT,
    }),
  ]);
  worldview.value = w;
  storyTimeline.value = t;
}

async function loadStory() {
  storyLoading.value = true;
  storyErr.value = null;
  try {
    // 需要项目信息才能判断「本书」对应哪本语料（续写项目挂在 active/continuation 上）。
    const [proj, novels] = await Promise.all([
      storyProject.value ?? api.getProject(projectId),
      storyNovels.value.length ? Promise.resolve(storyNovels.value) : api.listNovels(),
    ]);
    storyProject.value = proj;
    storyNovels.value = novels;
    await loadStoryData();
  } catch (e) {
    storyErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    storyLoading.value = false;
  }
}

async function refreshStory() {
  storyLoading.value = true;
  storyErr.value = null;
  try {
    await loadStoryData();
  } catch (e) {
    storyErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    storyLoading.value = false;
  }
}

function storyPage(dir: -1 | 1) {
  const total = storyTimeline.value?.total ?? 0;
  const next = storyOffset.value + dir * STORY_LIMIT;
  storyOffset.value = Math.max(0, Math.min(next, Math.max(0, total - 1)));
  void refreshStory();
}

// ---------- Tab 6 Worker 状态 ----------
// 视图层只关心「线程活没活/有没有积压/failed/stuck/empty」——后端 summary 已聚合，
// 前端只负责展示 + 失败行手动重捞的二次确认。
type WorkerRun = {
  id: number;
  segment_no: number | null;
  status: string;
  stuck: boolean;
  empty: boolean;
  duration_ms: number | null;
  batch_stats: Record<string, unknown> | null;
  error: string | null;
  started_at: string | null;
};
type WorkerStatus = {
  worker_alive: boolean;
  pending: number;
  summary: { success: number; failed: number; running: number; stuck: number; empty: number };
  recent_runs: WorkerRun[];
};
const worker = ref<WorkerStatus | null>(null);
const workerLoading = ref(false);
const workerErr = ref<string | null>(null);
const requeueBusy = ref(false);
const workerMsg = ref<string | null>(null);
const selectedForRequeue = ref<Set<number>>(new Set());

async function loadWorker() {
  workerLoading.value = true;
  workerErr.value = null;
  try {
    worker.value = await api.kgV3WorkerStatus(projectId);
  } catch (e) {
    workerErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    workerLoading.value = false;
  }
}

function toggleRequeuePick(id: number) {
  const s = new Set(selectedForRequeue.value);
  if (s.has(id)) s.delete(id);
  else s.add(id);
  selectedForRequeue.value = s;
}

async function requeueSelected() {
  if (selectedForRequeue.value.size === 0) return;
  requeueBusy.value = true;
  try {
    const r = await api.kgV3Requeue(projectId, [...selectedForRequeue.value]);
    workerMsg.value = `已重捞 ${r.requeued} 条；${r.missing.length ? `缺失 ${r.missing.length} 条（段文本不存在）` : "无缺失"}`;
    selectedForRequeue.value = new Set();
    await loadWorker();
  } catch (e) {
    workerMsg.value = e instanceof Error ? e.message : String(e);
  } finally {
    requeueBusy.value = false;
  }
}

const backfillBusy = ref(false);
const recoverBusy = ref(false);

/** 为「一行入队记录都没有」的已生成段补建任务。 */
async function runBackfill() {
  backfillBusy.value = true;
  workerMsg.value = null;
  try {
    const r = await api.kgV3Backfill(projectId);
    workerMsg.value = `补抽：入队 ${r.enqueued.length} 段；跳过 ${r.skipped.length} 段` +
      (r.skipped.length ? `（${r.skipped.slice(0, 3).map((x) => `段 ${x.segment_no}:${x.reason}`).join("；")}…）` : "");
    await loadWorker();
  } catch (e) {
    workerMsg.value = e instanceof Error ? e.message : String(e);
  } finally {
    backfillBusy.value = false;
  }
}

/** 把停在 running 的超时行重新入队。 */
async function runRecover() {
  recoverBusy.value = true;
  workerMsg.value = null;
  try {
    const r = await api.kgV3Recover(projectId, true);
    workerMsg.value = `恢复：重入队 ${r.requeued} 段；缺失正文 ${r.missing.length} 段`;
    await loadWorker();
  } catch (e) {
    workerMsg.value = e instanceof Error ? e.message : String(e);
  } finally {
    recoverBusy.value = false;
  }
}

function fmtDur(ms: number | null): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// ---------- Tab 4 Judge 面板 ----------
const findings = ref<KgV3JudgeFindingsResponse | null>(null);
const findingsLoading = ref(false);
const findingsErr = ref<string | null>(null);
const exemptions = ref<{ rule: string | null; entity: string | null; note: string | null }[]>([]);
const exemptNote = ref("");
const exemptTarget = ref<KgV3JudgeFinding | null>(null);
const exemptBusy = ref(false);
const judgeMsg = ref<string | null>(null);

async function loadFindings() {
  findingsLoading.value = true;
  findingsErr.value = null;
  try {
    [findings.value, exemptions.value] = await Promise.all([
      api.kgV3JudgeFindings(projectId),
      api.kgV3ListExemptions(projectId).then((r) => r.exemptions),
    ]);
  } catch (e) {
    findingsErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    findingsLoading.value = false;
  }
}

function askExempt(f: KgV3JudgeFinding) {
  exemptTarget.value = f;
  exemptNote.value = "";
  judgeMsg.value = null;
}

async function confirmExempt() {
  const t = exemptTarget.value;
  if (!t) return;
  exemptBusy.value = true;
  try {
    await api.kgV3Exempt(projectId, {
      finding_id: null, rule: t.rule, entity: t.entity, note: exemptNote.value || "（无备注）",
    });
    exemptTarget.value = null;
    judgeMsg.value = "已豁免，Judge 以后不再报同 (规则, 实体) 告警。";
    await loadFindings();
  } catch (e) {
    judgeMsg.value = e instanceof Error ? e.message : String(e);
  } finally {
    exemptBusy.value = false;
  }
}

async function revoke(rule: string | null, entity: string | null) {
  if (rule == null) return;
  try {
    await api.kgV3RevokeExemption(projectId, rule, entity ?? "");
    judgeMsg.value = `已撤销豁免：${rule} / ${entity || "（无实体）"}，Judge 恢复报告。`;
    await loadFindings();
  } catch (e) {
    judgeMsg.value = e instanceof Error ? e.message : String(e);
  }
}

// ---------- Tab 5 关系与节奏 ----------
const relations = ref<KgV3Relation[]>([]);
const relLoading = ref(false);
const relErr = ref<string | null>(null);
const rhythm = ref<{ segments: number[]; top_entities: { name: string; kind: string; mentions: number }[] } | null>(null);
const curveFrom = ref("");
const curveTo = ref("");
const curve = ref<{ rel_type: string; chapter: number | null; strength: number; note: string | null }[]>([]);
const curveLoading = ref(false);
const curveErr = ref<string | null>(null);

/** 环形布局：前 N 节点均匀落圆周。确定性（无物理引擎）。 */
interface RingNode {
  name: string;
  kind: string;
  x: number;
  y: number;
}
const RING_R = 130;
const RING_CX = 240;
const RING_CY = 170;
const ringNodes = computed<RingNode[]>(() => {
  // 只画出现在关系边里的端点，按总强度排序取前 10
  const strength = new Map<string, number>();
  for (const r of relations.value) {
    strength.set(r.from, (strength.get(r.from) ?? 0) + r.strength);
    strength.set(r.to, (strength.get(r.to) ?? 0) + r.strength);
  }
  const names = [...strength.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([n]) => n);
  const kindOf = new Map<string, string>();
  for (const r of relations.value) {
    kindOf.set(r.from, r.from_kind);
    kindOf.set(r.to, r.to_kind);
  }
  return names.map((name, i) => {
    const ang = (2 * Math.PI * i) / names.length - Math.PI / 2;
    return {
      name,
      kind: kindOf.get(name) ?? "character",
      x: RING_CX + RING_R * Math.cos(ang),
      y: RING_CY + RING_R * Math.sin(ang),
    };
  });
});
const posOf = computed(() => new Map(ringNodes.value.map((n) => [n.name, n])));
const ringEdges = computed(() =>
  relations.value
    .map((r) => {
      const a = posOf.value.get(r.from);
      const b = posOf.value.get(r.to);
      if (!a || !b) return null;
      return { ...r, x1: a.x, y1: a.y, x2: b.x, y2: b.y };
    })
    .filter((e): e is NonNullable<typeof e> => e !== null),
);

/** rel_type → 颜色（确定性映射，未知类型走 fallback）。 */
function relColor(t: string): string {
  const map: Record<string, string> = {
    ally: "var(--ok)",
    enemy: "var(--danger)",
    romantic: "var(--accent)",
    mentor: "var(--info)",
    subordinate: "var(--warn)",
    belongs_to: "var(--fg-muted)",
    located_at: "var(--fg-faint)",
    owns: "var(--accent-ink)",
  };
  return map[t] ?? "var(--fg-muted)";
}

const maxMention = computed(() =>
  Math.max(1, ...communityTop.value.map((t) => t.mentions)),
);
const communityTop = computed(() => rhythm.value?.top_entities ?? []);

async function loadGraphTab() {
  relLoading.value = true;
  relErr.value = null;
  try {
    const [relResp, rh] = await Promise.all([
      api.kgV3Relations(projectId),
      api.kgV3Rhythm(projectId, 10),
    ]);
    relations.value = relResp.relations;
    rhythm.value = rh;
  } catch (e) {
    relErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    relLoading.value = false;
  }
}

async function loadCurve() {
  if (!curveFrom.value.trim() || !curveTo.value.trim()) return;
  curveLoading.value = true;
  curveErr.value = null;
  try {
    const r = await api.kgV3RelationCurve(projectId, curveFrom.value.trim(), curveTo.value.trim());
    curve.value = r.curve;
  } catch (e) {
    curveErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    curveLoading.value = false;
  }
}

/** 曲线 SVG 点位：chapter→x，strength→y（0 底 1 顶）。 */
const curvePts = computed(() => {
  if (curve.value.length < 2) return [];
  const chs = curve.value.map((c) => c.chapter ?? 0);
  const lo = Math.min(...chs);
  const hi = Math.max(...chs);
  const span = hi - lo || 1;
  const W = 460;
  const H = 120;
  const pad = 24;
  return curve.value.map((c, i) => ({
    ...c,
    i,
    x: pad + ((c.chapter ?? 0) - lo) / span * (W - 2 * pad),
    y: H - pad - c.strength * (H - 2 * pad),
    W,
    H,
  }));
});

onMounted(() => {
  loadSlice();
  loadCorpusWindows();
  loadBookScope();
});

// 页面被 keep-alive 缓存后，onMounted 不会重跑。状态条是轻量只读聚合，
// 回来时刷新它们；各标签的数据仍按需点「刷新」，不会整页重拉。
let kgActivated = false;
onActivated(() => {
  if (!kgActivated) {
    kgActivated = true;
    return;
  }
  void loadCorpusWindows();
  void loadBookScope();
});

// ---------- Tab 7 Style 审计（v2.1） ----------
import type { KgV3StyleWarning } from "@/api/types";
const styleWarnings = ref<KgV3StyleWarning[]>([]);
const styleTruncated = ref(false);
const styleTotal = ref(0);
const styleLoading = ref(false);
const styleErr = ref<string | null>(null);
const conflicts = ref<StyleNarrativeConflictsResponse | null>(null);
const conflictsErr = ref<string | null>(null);
async function loadStyleWarnings() {
  styleLoading.value = true;
  styleErr.value = null;
  conflictsErr.value = null;
  try {
    const r = await api.kgV3StyleWarnings(projectId);
    styleWarnings.value = r.warnings;
    styleTruncated.value = r.truncated;
    styleTotal.value = r.total;
  } catch (e) {
    styleErr.value = e instanceof Error ? e.message : String(e);
  }
  // 档案冲突与「逐段告警」是两件事：一边失败不能拖垮另一边。
  try {
    conflicts.value = await api.styleNarrativeConflicts(projectId);
  } catch (e) {
    conflictsErr.value = e instanceof Error ? e.message : String(e);
  } finally {
    styleLoading.value = false;
  }
}
function fmtPct(p: number | null | undefined): string {
  if (p === null || p === undefined) return "—";
  return p.toFixed(1) + "th";
}

/** 后端是带时区的 ISO 串；这里只显示到分钟，避免一长串毫秒把列挤爆。 */
function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** v2.14 方案 B：bucket chip 颜色——让用户一眼看出本段走哪档。 */
function bucketChipStyle(bucket: string): Record<string, string> {
  const colorMap: Record<string, string> = {
    short: "#67c23a",   // 绿——段短对齐
    medium: "#909399",  // 灰——主档
    long: "#e6a23c",    // 橙——段长
    v1: "#c0c4cc",      // 淡灰——v1 path（fallback）
  };
  const bg = colorMap[bucket] || "#909399";
  return {
    background: bg,
    color: "#fff",
    padding: "2px 8px",
    borderRadius: "10px",
    fontSize: "0.85em",
  };
}
</script>

<template>
  <div class="kg-page">
  <ProjectNav :project-id="projectId" />
  <header class="page-head">
    <div class="eyebrow">KG v3.0</div>
    <h1>知识图谱工作台</h1>
    <p>查看人物、事件、伏笔与一致性告警；数据来自已写正文的自动抽取。</p>
  </header>

  <!-- 语料摄取窗口状态条：failed>0 必须显式报失败，不能退化成「无实体」 -->
  <div
    v-if="corpusWindowsErr"
    class="corpus-status corpus-status--error"
    role="status"
  >
    <span class="corpus-status__label">语料摄取</span>
    <strong class="corpus-status__text">状态读取失败</strong>
    <span class="corpus-status__err">{{ corpusWindowsErr }}</span>
  </div>
  <div
    v-else-if="corpusWindows"
    class="corpus-status"
    :class="`corpus-status--${corpusStatusView.tone}`"
    role="status"
  >
    <span class="corpus-status__label">语料摄取</span>
    <strong class="corpus-status__text">{{ corpusStatusView.text }}</strong>
    <span v-if="corpusStatusView.detail" class="hint">{{ corpusStatusView.detail }}</span>
    <span v-if="corpusWindows.last_run_at" class="hint">
      最近运行：{{ fmtTime(corpusWindows.last_run_at) }}
    </span>
    <details v-if="corpusWindows.failures?.length" class="corpus-failures">
      <summary>查看失败窗口与原始错误（{{ corpusWindows.failures.length }}）</summary>
      <ul>
        <li v-for="f in corpusWindows.failures" :key="f.window_key">
          <code>{{ f.window_key }}</code>
          <span class="tag err">{{ f.reason }}</span>
          <span>{{ f.error }}</span>
        </li>
      </ul>
    </details>
  </div>

  <!-- 书维度状态条：章号未知 / 区间漂移 / 别书排除 分开显示，不合成「部分数据异常」 -->
  <div
    v-if="bookScopeErr"
    class="corpus-status corpus-status--error"
    role="status"
  >
    <span class="corpus-status__label">书维度</span>
    <strong class="corpus-status__text">状态读取失败</strong>
    <span class="corpus-status__err">{{ bookScopeErr }}</span>
  </div>
  <div
    v-else-if="bookScope"
    class="corpus-status"
    :class="`corpus-status--${bookScopeView.tone}`"
    role="status"
  >
    <span class="corpus-status__label">书维度</span>
    <span class="corpus-status__book">{{ bookScopeView.bookLabel }}</span>
    <span class="tag" :class="bookScopeView.unknown > 0 ? 'warn' : ''">
      {{ bookScopeView.unknownText }}
    </span>
    <span class="tag" :class="bookScopeView.drift > 0 ? 'warn' : ''">
      {{ bookScopeView.driftText }}
    </span>
    <span v-if="bookScopeView.excludedText" class="hint">{{ bookScopeView.excludedText }}</span>
    <details v-if="bookScopeView.detail" class="scope-detail">
      <summary>口径说明</summary>
      <p>{{ bookScopeView.detail }}</p>
    </details>
  </div>

  <nav class="tabs kg-tabs">
    <button
      v-for="t in TABS"
      :key="t.key"
      class="tab"
      :class="{ active: activeTab === t.key }"
      @click="switchTab(t.key)"
    >
      {{ t.label }}
    </button>
  </nav>

  <div ref="kgBodyEl" class="kg-body">
  <!-- ============ Tab 1：切片预览 ============ -->
  <section v-if="activeTab === 'slice'" class="card">
    <div class="card-title">
      生成切片预览
      <span class="hint">LLM 续写这一段时实际注入的 KG 事实</span>
    </div>
    <div class="row" style="margin-bottom: var(--gap)">
      <div class="field" style="width: 110px">
        <label>段号</label>
        <input class="input" type="number" min="1" v-model.number="sliceSeg" />
      </div>
      <div class="field" style="width: 110px">
        <label>回溯窗口</label>
        <input class="input" type="number" min="1" max="50" v-model.number="sliceWindow" />
      </div>
      <div class="field" style="width: 130px">
        <label>实体上限</label>
        <input class="input" type="number" min="1" max="100" v-model.number="sliceMaxEnt" />
      </div>
      <div class="field" style="flex: 1; min-width: 200px">
        <label>角色（逗号分隔，空 = 本段 mention）</label>
        <input class="input" v-model="sliceChars" placeholder="林悠然,顾辰逸" @keyup.enter="loadSlice" />
      </div>
      <button class="btn primary" :disabled="sliceLoading" @click="loadSlice">
        {{ sliceLoading ? "加载中…" : "取切片" }}
      </button>
    </div>
    <div v-if="sliceErr" class="notice err">{{ sliceErr }}</div>

    <template v-if="slice">
      <div class="grid-2">
        <div>
          <div class="card-title" style="font-size: var(--fs-md)">近期出场实体 <span class="hint">{{ slice.recent_entities.length }}</span></div>
          <div class="list">
            <div v-for="e in slice.recent_entities" :key="e.name" class="list-item">
              <strong>{{ e.name }}</strong>
              <span class="tag" style="margin-left: 8px">{{ e.kind }}</span>
              <span class="faint mono" style="font-size: var(--fs-xs)">×{{ e.mention_count }}</span>
              <div v-if="e.context" class="faint" style="font-size: var(--fs-xs); width: 100%">{{ e.context }}</div>
            </div>
            <div v-if="!slice.recent_entities.length" class="empty">窗口内无实体</div>
          </div>

          <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap)">角色当前状态</div>
          <div class="list">
            <div v-for="(s, i) in slice.character_states" :key="i" class="list-item">
              <strong>{{ s.character }}</strong>
              <span class="tag info" style="margin-left: 8px">{{ s.state_type }}</span>
              <span style="font-size: var(--fs-sm)">{{ s.new_value }}</span>
            </div>
            <div v-if="!slice.character_states.length" class="empty">无状态记录</div>
          </div>
        </div>
        <div>
          <div class="card-title" style="font-size: var(--fs-md)">未回收伏笔 <span class="hint">{{ slice.active_foreshadows.length }}</span></div>
          <div class="list">
            <div v-for="f in slice.active_foreshadows" :key="f.name" class="list-item">
              <div style="flex: 1">{{ f.name }}</div>
              <span class="tag warn">{{ f.status }}</span>
              <span class="faint mono" style="font-size: var(--fs-xs)">
                埋@{{ f.planted_chapter ?? "?" }}<template v-if="f.planned_payoff_chapter"> → 计划{{ f.planned_payoff_chapter }}</template>
              </span>
            </div>
            <div v-if="!slice.active_foreshadows.length" class="empty">无未回收伏笔</div>
          </div>

          <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap)">角色已知情报</div>
          <div class="list">
            <div v-for="k in slice.known_facts" :key="k.character" class="list-item" style="align-items: flex-start">
              <div style="flex: 1">
                <strong>{{ k.character }}</strong> 知道：
                <div v-for="(f, i) in k.knows" :key="i" style="font-size: var(--fs-sm)">{{ f }}</div>
              </div>
            </div>
            <div v-if="!slice.known_facts.length" class="empty">无情报记录</div>
          </div>

          <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap)">近期事件 <span class="hint">{{ slice.recent_events.length }}</span></div>
          <div class="list">
            <div v-for="(ev, i) in slice.recent_events" :key="i" class="list-item">
              <span v-if="ev.in_story_date" class="tag info">[{{ ev.in_story_date }}]</span>
              <span style="font-size: var(--fs-sm)">{{ ev.summary }}</span>
            </div>
            <div v-if="!slice.recent_events.length" class="empty">窗口内无事件</div>
          </div>
        </div>
      </div>

      <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap)">
        注入的 prompt 文本
        <button class="btn ghost" style="font-size: var(--fs-xs)" @click="copyPrompt">
          {{ sliceCopied ? "已复制 ✓" : "复制" }}
        </button>
      </div>
      <pre class="promptbox" style="max-height: 300px; overflow: auto">{{ slicePrompt || "（空切片）" }}</pre>
    </template>
  </section>

  <!-- ============ Tab 2：人物情报 ============ -->
  <section v-else-if="activeTab === 'intel'" class="card">
    <div class="card-title">
      人物情报追踪
      <span class="hint">Novelium 卖点：每个角色此刻知道什么（entity / 事件 / 秘密 三类）</span>
    </div>
    <div class="grid-2">
      <div>
        <div class="card-title" style="font-size: var(--fs-md)">角色 <span class="hint">{{ characters.length }}</span></div>
        <div v-if="charsLoading" class="empty"><span class="spinner" /> 加载中…</div>
        <div v-else-if="charsErr" class="notice err">{{ charsErr }}</div>
        <div v-else class="list">
          <div
            v-for="c in characters"
            :key="c.id"
            class="list-item clickable"
            :class="{ active: selectedChar?.id === c.id }"
            @click="openChar(c)"
          >
            <strong>{{ c.name }}</strong>
            <span v-if="c.has_secrets" class="tag warn" style="margin-left: 6px">有秘密</span>
            <span
              class="tag"
              :class="chapterTag(c.chapters).unknown ? 'warn' : ''"
              style="margin-left: 6px"
            >{{ chapterTag(c.chapters).text }}</span>
            <span class="faint mono" style="font-size: var(--fs-xs)">提及 {{ c.mention_count }}</span>
          </div>
          <div v-if="!characters.length" class="empty">
            项目里还没有角色（先生成几段让批抽数据进来）
            <template v-if="bookScopeView.excluded > 0">
              ；另有 {{ bookScopeView.excluded }} 个实体属于别书，读侧不显示
            </template>
          </div>
        </div>
      </div>
      <div>
        <div class="card-title" style="font-size: var(--fs-md)">
          {{ selectedChar ? `${selectedChar.name} 的情报` : "点左侧角色查看情报" }}
        </div>
        <div v-if="charKnowsLoading" class="empty"><span class="spinner" /> 加载中…</div>
        <template v-else-if="selectedChar">
          <div class="list">
            <div v-for="(k, i) in charKnows" :key="i" class="list-item">
              <span
                class="tag"
                :class="k.startsWith('秘密：') ? 'warn' : k.startsWith('事件：') ? 'info' : ''"
              >{{ k.startsWith("秘密：") ? "秘密" : k.startsWith("事件：") ? "事件" : "实体" }}</span>
              <span style="font-size: var(--fs-sm)">{{ k }}</span>
            </div>
            <div v-if="!charKnows.length" class="empty">该角色暂无情报记录</div>
          </div>
          <button class="btn ghost" style="margin-top: var(--gap-md); font-size: var(--fs-xs)" @click="knowCharacter = selectedChar.name; knowFact = charKnows[0] ?? ''">
            带入下方查询
          </button>
        </template>
        <div v-else class="empty">—</div>

        <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap)">情报查询</div>
        <div class="row">
          <div class="field" style="flex: 1; min-width: 140px">
            <label>角色</label>
            <input class="input" v-model="knowCharacter" placeholder="林悠然" />
          </div>
          <div class="field" style="flex: 2; min-width: 220px">
            <label>事实（实体名 / 事件描述 / 秘密文本）</label>
            <input class="input" v-model="knowFact" placeholder="天津港 或 事件：项目危机爆发 或 秘密：父亲并非因病去世" @keyup.enter="checkKnows" />
          </div>
          <button class="btn primary" :disabled="knowChecking || !knowCharacter.trim() || !knowFact.trim()" @click="checkKnows">
            {{ knowChecking ? "查询中…" : "查询" }}
          </button>
        </div>
        <div v-if="knowErr" class="notice err" style="margin-top: 12px">{{ knowErr }}</div>
        <div v-if="knowResult !== null" class="notice" :class="knowResult ? 'ok' : 'err'" style="margin-top: 12px">
          {{ knowCharacter }} {{ knowResult ? "已知此事" : "尚不知道此事" }}
        </div>
      </div>
    </div>
  </section>

  <!-- ============ Tab 3：事件时间线 ============ -->
  <section v-else-if="activeTab === 'events'" class="card">
    <div class="card-title">
      事件时间线
      <span class="hint">{{ events ? `${events.events.length} 事件 · ${events.links.length} 因果链` : "" }}</span>
      <div class="field" style="width: 200px; margin-left: auto">
        <select class="input" v-model="eventTypeFilter" @change="loadEvents">
          <option value="">全部类型</option>
          <option v-for="t in EVENT_TYPES.slice(1)" :key="t" :value="t">{{ t }}</option>
        </select>
      </div>
      <button class="btn" @click="loadEvents">刷新</button>
    </div>
    <div v-if="eventsLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="eventsErr" class="notice err">{{ eventsErr }}</div>
    <div v-else-if="!events?.events.length" class="empty">还没有事件（生成后批抽自动落库）</div>
    <div v-else class="list">
      <div v-for="ev in events.events" :key="ev.sequence" class="list-item" style="align-items: flex-start">
        <div style="flex: 1">
          <div class="row tight" style="margin-bottom: 4px">
            <span class="tag mono">seq{{ ev.sequence }}</span>
            <span class="tag" :class="ev.event_type === 'foreshadow_payoff' ? 'ok' : ev.event_type === 'death' ? 'err' : 'info'">{{ ev.event_type }}</span>
            <span v-if="ev.in_story_date" class="faint mono" style="font-size: var(--fs-xs)">故事内时间 {{ ev.in_story_date }}</span>
            <span v-if="ev.chapter" class="faint mono" style="font-size: var(--fs-xs)">第 {{ ev.chapter }} 章</span>
          </div>
          <div class="prose" style="font-size: var(--fs-sm)">{{ ev.summary }}</div>
          <div v-if="linksByTarget.get(ev.sequence)?.length" class="faint" style="font-size: var(--fs-xs); margin-top: 4px">
            <span v-for="(lk, i) in linksByTarget.get(ev.sequence)" :key="i" style="margin-right: 10px">
              ← {{ lk.link_type }} seq{{ lk.from_seq }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- ============ Tab 4：Judge 面板 ============ -->
  <section v-else-if="activeTab === 'judge'" class="card">
    <div class="card-title">
      Judge 一致性告警
      <span class="hint" v-if="findings">err {{ findings.by_severity.err }} · warn {{ findings.by_severity.warn }}</span>
      <button class="btn" @click="loadFindings">刷新</button>
    </div>
    <div v-if="findingsLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="findingsErr" class="notice err">{{ findingsErr }}</div>
    <template v-else-if="findings">
      <div class="judge-toolbar">
        <div class="judge-stats">
          <span class="judge-stat err"><b>{{ findings.by_severity.err }}</b> err</span>
          <span class="judge-stat warn"><b>{{ findings.by_severity.warn }}</b> warn</span>
        </div>
        <details class="judge-rules">
          <summary>判定规则（{{ Object.keys(findings.rules).length }}）</summary>
          <div class="judge-rule-grid">
            <div v-for="(r, name) in findings.rules" :key="name" class="judge-rule">
              <span class="tag" :class="r.severity === 'err' ? 'err' : 'warn'">{{ r.severity }}</span>
              <code>{{ name }}</code>
              <span class="judge-rule-desc">{{ r.desc }}</span>
            </div>
          </div>
        </details>
      </div>
      <div v-if="judgeMsg" class="notice info" style="margin-bottom: var(--gap)">{{ judgeMsg }}</div>

      <div v-if="!findings.items.length" class="empty">
        没有落库告警。生成后 Judge 会自动判定并落库；没告警 ≠ 规则没跑。
      </div>
      <div v-else class="judge-list">
        <article
          v-for="(f, i) in findings.items"
          :key="i"
          class="judge-item"
          :class="f.severity"
        >
          <header class="judge-head">
            <span class="judge-sev" :class="f.severity">{{ f.severity }}</span>
            <code class="judge-rule-name">{{ f.rule }}</code>
            <strong class="judge-entity">{{ f.entity || "—" }}</strong>
            <span class="judge-seg">段 {{ f.segment_no ?? "?" }}</span>
            <span v-if="f.exempted" class="tag">已豁免</span>
            <button
              v-if="!f.exempted"
              class="btn ghost judge-exempt"
              @click="askExempt(f)"
            >豁免</button>
          </header>
          <p class="judge-msg">{{ f.message }}</p>
          <details v-if="f.evidence.length" class="judge-evidence">
            <summary>证据（{{ f.evidence.length }}）</summary>
            <div v-for="(ev, j) in f.evidence" :key="j">{{ ev }}</div>
          </details>
        </article>
      </div>

      <div v-if="exemptTarget" class="notice info" style="margin-top: var(--gap)">
        <div style="margin-bottom: 8px">
          豁免 <strong>{{ exemptTarget.rule }}</strong> / <strong>{{ exemptTarget.entity }}</strong>（段 {{ exemptTarget.segment_no }}）？
          Judge 以后不再报同 (规则, 实体)。
        </div>
        <div class="row">
          <input class="input" v-model="exemptNote" placeholder="备注：为什么这是故意的（留痕）" style="flex: 1" />
          <button class="btn primary" :disabled="exemptBusy" @click="confirmExempt">确认豁免</button>
          <button class="btn ghost" @click="exemptTarget = null">取消</button>
        </div>
      </div>

      <details class="judge-exemptions">
        <summary>已豁免（{{ exemptions.length }}）</summary>
        <div class="list">
          <div v-for="(x, i) in exemptions" :key="i" class="list-item">
            <div style="flex: 1">
              <strong>{{ x.rule }}</strong> / {{ x.entity || "（无实体）" }}
              <div v-if="x.note" class="faint" style="font-size: var(--fs-xs)">{{ x.note }}</div>
            </div>
            <button class="btn ghost" style="font-size: var(--fs-xs)" @click="revoke(x.rule, x.entity)">撤销</button>
          </div>
          <div v-if="!exemptions.length" class="empty">无豁免</div>
        </div>
      </details>
    </template>
  </section>

  <!-- ============ Tab 5：关系与节奏 ============ -->
  <section v-else-if="activeTab === 'graph'" class="card">
    <div class="card-title">
      关系图与节奏
      <span class="hint">{{ relations.length }} 条关系</span>
      <button class="btn" @click="loadGraphTab">刷新</button>
    </div>
    <div v-if="relLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="relErr" class="notice err">{{ relErr }}</div>
    <template v-else>
      <div v-if="!ringNodes.length" class="empty">还没有关系数据（生成后批抽自动落库）</div>
      <div v-else class="grid-2">
        <div>
          <svg :viewBox="`0 0 480 340`" style="width: 100%; max-width: 520px">
            <g v-for="(e, i) in ringEdges" :key="i">
              <line
                :x1="e.x1" :y1="e.y1" :x2="e.x2" :y2="e.y2"
                :stroke="relColor(e.rel_type)"
                :stroke-width="0.5 + e.strength * 3"
                :stroke-opacity="0.35 + e.strength * 0.5"
              />
              <text
                :x="(e.x1 + e.x2) / 2" :y="(e.y1 + e.y2) / 2 - 3"
                text-anchor="middle"
                style="font-size: var(--fs-3xs)" fill="var(--fg-faint)"
              >{{ e.rel_type }}</text>
            </g>
            <g v-for="n in ringNodes" :key="n.name">
              <circle :cx="n.x" :cy="n.y" r="16" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="1" />
              <text :x="n.x" :y="n.y + 4" text-anchor="middle" style="font-size: var(--fs-3xs); font-weight: 700" fill="var(--accent-ink)">{{
                n.name.length > 4 ? n.name.slice(0, 4) : n.name
              }}</text>
              <text :x="n.x" :y="n.y + 28" text-anchor="middle" style="font-size: var(--fs-3xs)" fill="var(--fg)">{{ n.name }}</text>
            </g>
          </svg>
          <div class="faint" style="font-size: var(--fs-xs)">线宽 = 关系强度；颜色 = 关系类型（ally 绿 / enemy 红 / romantic 朱 / mentor 蓝 / subordinate 黄）</div>
        </div>
        <div>
          <div class="card-title" style="font-size: var(--fs-md)">节奏：最近 {{ rhythm?.segments.length ?? 0 }} 段谁在唱主角</div>
          <div v-for="t in communityTop" :key="t.name" style="margin-bottom: 8px">
            <div class="row tight" style="justify-content: space-between">
              <span style="font-size: var(--fs-sm)">{{ t.name }} <span class="tag">{{ t.kind }}</span></span>
              <span class="faint mono" style="font-size: var(--fs-xs)">{{ t.mentions }}</span>
            </div>
            <div style="background: var(--bg-sunken); border-radius: var(--radius-sm); height: 8px">
              <div
                style="background: var(--accent); height: 8px; border-radius: var(--radius-sm)"
                :style="{ width: (t.mentions / maxMention * 100) + '%' }"
              />
            </div>
          </div>
          <div v-if="!communityTop.length" class="empty">无 mention 数据</div>
        </div>
      </div>

      <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap-lg)">关系演变曲线</div>
      <div class="row">
        <div class="field" style="flex: 1; min-width: 140px">
          <label>角色 A</label>
          <input class="input" v-model="curveFrom" placeholder="林悠然" />
        </div>
        <div class="field" style="flex: 1; min-width: 140px">
          <label>角色 B</label>
          <input class="input" v-model="curveTo" placeholder="顾辰逸" @keyup.enter="loadCurve" />
        </div>
        <button class="btn primary" :disabled="curveLoading || !curveFrom.trim() || !curveTo.trim()" @click="loadCurve">
          {{ curveLoading ? "查询中…" : "查曲线" }}
        </button>
      </div>
      <div v-if="curveErr" class="notice err" style="margin-top: 12px">{{ curveErr }}</div>
      <template v-if="curve.length">
        <svg v-if="curvePts.length >= 2" :viewBox="`0 0 480 120`" style="width: 100%; max-width: 520px; margin-top: 12px">
          <polyline
            :points="curvePts.map((p) => `${p.x},${p.y}`).join(' ')"
            fill="none" stroke="var(--accent)" stroke-width="2"
          />
          <g v-for="p in curvePts" :key="p.i">
            <circle :cx="p.x" :cy="p.y" r="4" fill="var(--accent-ink)" />
            <text :x="p.x" :y="p.y - 8" text-anchor="middle" style="font-size: var(--fs-3xs)" fill="var(--fg-muted)">{{ p.strength.toFixed(1) }}</text>
            <text :x="p.x" :y="p.H - 6" text-anchor="middle" style="font-size: var(--fs-3xs)" fill="var(--fg-faint)">ch{{ p.chapter ?? "?" }}</text>
          </g>
        </svg>
        <div class="list" style="margin-top: 8px">
          <div v-for="(c, i) in curve" :key="i" class="list-item">
            <span class="tag mono">ch{{ c.chapter ?? "?" }}</span>
            <span class="tag">{{ c.rel_type }}</span>
            <span class="tag mono">strength {{ c.strength }}</span>
            <span v-if="c.note" style="font-size: var(--fs-sm)">{{ c.note }}</span>
          </div>
        </div>
      </template>
      <div v-else-if="curveFrom.trim() && curveTo.trim()" class="empty" style="margin-top: 12px">这两位之间暂无关系演变记录</div>
    </template>
  </section>

  <!-- ============ 世界观与剧情时间线 ============ -->
  <section v-else-if="activeTab === 'story'" class="card">
    <div class="card-title">
      世界观与剧情时间线
      <span class="hint">纯读：默认「本书」的 KG（生成正文/续写书），可切换参考书对照</span>
      <button class="btn" :disabled="storyLoading" @click="refreshStory">刷新</button>
    </div>
    <div class="row" style="margin-bottom: var(--gap)">
      <div class="field" style="flex: 1; min-width: 220px">
        <label>书</label>
        <select class="input" :value="storySelectValue" @change="onStoryBookChange">
          <option value="book">{{ storyBookLabel }}</option>
          <option v-for="n in storyRefOptions" :key="n.id" :value="String(n.id)">
            参考书 · {{ n.title || `语料 #${n.id}` }}
          </option>
        </select>
      </div>
      <div class="field" style="width: 130px">
        <label>起始章</label>
        <input class="input" type="number" min="1" v-model.number="storyFrom" @change="refreshStory" />
      </div>
      <span v-if="!storyRefOptions.length" class="faint">没有其他参考书，可先只看本书。</span>
    </div>

    <div v-if="storyLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="storyErr" class="notice err">{{ storyErr }}</div>
    <template v-else>
      <div v-if="storyEmpty" class="notice">
        本书还没有 KG 数据。正文生成后由 KG 抽取自动入库；也可以去「Worker 状态」点「补抽缺失段」。
      </div>
      <div class="world-groups">
        <div v-for="g in worldviewGroups" :key="g.key" class="world-group">
          <div class="world-head">
            <b>{{ g.label }}</b>
            <span class="tag">{{ g.items.length }}</span>
          </div>
          <div v-if="!g.items.length" class="faint">（空）</div>
          <div v-for="(it, i) in g.items.slice(0, 30)" :key="`${g.key}-${i}`" class="world-item">
            <div class="world-name">
              {{ it.name }}
              <span v-if="it.source === 'defined'" class="tag ok">作者定义</span>
              <span v-if="it.mention_count" class="faint">· 提及 {{ it.mention_count }}</span>
              <span v-if="it.first_chapter" class="tag mono">
                第 {{ it.first_chapter }}{{ it.last_chapter && it.last_chapter !== it.first_chapter ? `–${it.last_chapter}` : "" }} 章
              </span>
            </div>
            <template v-if="it.source === 'defined'">
              <div v-if="it.attrs?.类型" class="faint">类型：{{ it.attrs.类型 }}</div>
              <div v-if="definedRealms(it).length" class="faint">
                境界：{{ definedRealms(it).join(" → ") }}
              </div>
              <div v-if="definedTiers(it).length" class="faint">
                档位：{{ definedTiers(it).join(" · ") }}
              </div>
              <div v-if="it.attrs?.说明" class="faint">{{ it.attrs.说明 }}</div>
            </template>
            <div v-if="it.aliases?.length" class="faint">别名：{{ it.aliases.join("、") }}</div>
            <div v-if="it.evidence?.length" class="faint">
              证据：{{ it.evidence[0].summary || it.evidence[0].event_type }}
            </div>
          </div>
          <div v-if="g.items.length > 30" class="faint">… 共 {{ g.items.length }} 条，仅显示前 30</div>
        </div>
      </div>

      <div class="timeline-head">
        <b>剧情时间线</b>
        <span class="faint">
          共 {{ storyTimeline?.total ?? 0 }} 条
          <template v-if="storyTimeline?.total">
            · 当前 {{ storyOffset + 1 }}–{{ Math.min(storyOffset + STORY_LIMIT, storyTimeline?.total ?? 0) }}
          </template>
        </span>
        <button class="btn sm" :disabled="storyOffset <= 0 || storyLoading" @click="storyPage(-1)">上一页</button>
        <button
          class="btn sm"
          :disabled="storyOffset + STORY_LIMIT >= (storyTimeline?.total ?? 0) || storyLoading"
          @click="storyPage(1)"
        >下一页</button>
      </div>
      <div v-if="!(storyTimeline?.events?.length)" class="empty">
        {{ storyIsBook
          ? "本书在这个起点之后还没有事件（可调起始章，或先在 Worker 状态补抽 KG）。"
          : "这个起点之后还没有事件（换本书或调起始章）。" }}
      </div>
      <div v-else class="list">
        <div
          v-for="(ev, i) in (storyTimeline?.events ?? [])"
          :key="`ev-${i}`"
          class="list-item"
          style="align-items: flex-start"
        >
          <span class="tag mono">第 {{ ev.chapter ?? "?" }} 章</span>
          <div style="flex: 1">
            <div class="prose" style="font-size: var(--fs-sm)">
              <span v-if="ev.who" class="tag">{{ ev.who }}</span>
              <span v-if="ev.where" class="faint"> @ {{ ev.where }}</span>
              {{ ev.what || ev.result || "（无描述）" }}
            </div>
            <div v-if="ev.result && ev.what" class="faint" style="font-size: var(--fs-xs)">
              结果：{{ ev.result }}
            </div>
          </div>
        </div>
      </div>
    </template>
  </section>

  <!-- ============ Tab 6：Worker 状态 ============ -->
  <section v-else-if="activeTab === 'worker'" class="card">
    <div class="card-title">
      批抽 Worker 状态
      <span class="hint" v-if="worker">
        success {{ worker.summary.success }} · failed {{ worker.summary.failed }}
        · running {{ worker.summary.running }} · stuck {{ worker.summary.stuck }} · empty {{ worker.summary.empty }}
      </span>
      <button class="btn" @click="loadWorker">刷新</button>
    </div>

    <div v-if="workerLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="workerErr" class="notice err">{{ workerErr }}</div>
    <template v-else-if="worker">
      <!-- 三块状态卡：线程活没活 / 队列积压 / 失败计数 -->
      <div class="grid-2" style="margin-bottom: var(--gap)">
        <div>
          <div class="card-title" style="font-size: var(--fs-md)">线程
            <span class="tag" :class="worker.worker_alive ? 'ok' : 'err'" style="margin-left: 8px">
              {{ worker.worker_alive ? "alive" : "dead" }}
            </span>
          </div>
          <div class="faint" style="font-size: var(--fs-xs)">
            dead ≠ 正常：worker 是 daemon 线程，进程重启后自动拉起；
            显示 dead 通常意味着服务从未启动或进程刚退
          </div>
        </div>
        <div>
          <div class="card-title" style="font-size: var(--fs-md)">
            内存队列积压 <span class="hint">{{ worker.pending }} 段</span>
          </div>
          <div class="faint" style="font-size: var(--fs-xs)">
            进程重启即清零——持久状态看下方 recent_runs
          </div>
        </div>
      </div>

      <div v-if="workerMsg" class="notice info" style="margin-bottom: var(--gap)">{{ workerMsg }}</div>

      <div class="row" style="margin-bottom: var(--gap)">
        <button
          class="btn primary"
          :disabled="requeueBusy || selectedForRequeue.size === 0"
          @click="requeueSelected"
        >
          {{ requeueBusy ? "重捞中…" : `重捞选中 (${selectedForRequeue.size})` }}
        </button>
        <span class="faint" style="font-size: var(--fs-xs)">
          仅 failed/stuck 行可重捞；自动重试只覆盖网络抖动
        </span>
      </div>

      <div class="row" style="margin-bottom: var(--gap)">
        <button class="btn" :disabled="backfillBusy" @click="runBackfill">
          {{ backfillBusy ? "补抽中…" : "补抽缺失段" }}
        </button>
        <button class="btn" :disabled="recoverBusy" @click="runRecover">
          {{ recoverBusy ? "恢复中…" : "恢复卡住的 running 行" }}
        </button>
        <span class="faint" style="font-size: var(--fs-xs)">
          补抽 = 为「一行任务记录都没有」的已生成段补建入队；恢复 = 重捞超时的 running 行
        </span>
      </div>

      <div v-if="!worker.recent_runs.length" class="empty">还没有 AgentRun 记录（先生成几段让批抽落库）</div>
      <div v-else class="list">
        <div
          v-for="r in worker.recent_runs"
          :key="r.id"
          class="list-item"
          style="align-items: flex-start"
        >
          <div style="flex: 1">
            <div class="row tight" style="margin-bottom: 4px">
              <span
                class="tag"
                :class="r.status === 'failed' ? 'err' : r.status === 'running' ? 'warn' : r.stuck ? 'err' : 'ok'"
              >
                {{ r.status }}{{ r.stuck ? " (stuck)" : "" }}
              </span>
              <span class="tag mono">seg {{ r.segment_no ?? "?" }}</span>
              <span v-if="r.empty" class="tag warn">empty</span>
              <span class="faint mono" style="font-size: var(--fs-xs)">{{ fmtDur(r.duration_ms) }}</span>
              <span class="faint mono" style="font-size: var(--fs-xs)">
                {{ r.started_at ? new Date(r.started_at).toLocaleString() : "" }}
              </span>
            </div>
            <div v-if="r.error" class="prose" style="font-size: var(--fs-sm); color: var(--danger)">
              {{ r.error }}
            </div>
            <div v-else-if="r.batch_stats" class="faint" style="font-size: var(--fs-xs)">
              entities {{ r.batch_stats.entities_upserted ?? 0 }} ·
              events {{ r.batch_stats.events_added ?? 0 }} ·
              relations {{ r.batch_stats.relations_upserted ?? 0 }}
            </div>
          </div>
          <input
            v-if="r.status === 'failed' || r.stuck"
            type="checkbox"
            :checked="selectedForRequeue.has(r.id)"
            @change="toggleRequeuePick(r.id)"
            style="margin-top: 6px"
          />
        </div>
      </div>
    </template>
  </section>

  <!-- ============ Tab 7：Style 审计 ============ -->
  <section v-if="(activeTab as string) === 'style'" class="card style-audit">
    <header class="style-head">
      <div>
        <div class="style-kicker">Style audit</div>
        <h2>文风告警</h2>
      </div>
      <div class="style-summary">
        <span><b>{{ styleTotal }}</b> 条</span>
        <span v-if="styleTruncated" class="tag warn">已截断 · 仅显示最新 500 条</span>
        <span v-if="conflicts?.conflicts.length" class="tag err">
          档案冲突 {{ conflicts.conflicts.length }}
        </span>
        <button class="btn sm" :disabled="styleLoading" @click="loadStyleWarnings">刷新</button>
      </div>
    </header>

    <details class="style-note">
      <summary>判定口径</summary>
      <p>
        percentile ≥ 99.5 视为偏离档案。percentile 是 UI 标尺；
        bucket 与 scale 表示这一段按哪种长度档对齐，字数是判断
        「偏离是否只是篇幅差异」的依据。
      </p>
    </details>

    <div v-if="!styleLoading" class="conflict-block">
      <div v-if="conflictsErr" class="notice err">读取档案冲突失败：{{ conflictsErr }}</div>
      <template v-else-if="conflicts">
        <div class="conflict-head">
          <b>档案禁用词冲突</b>
          <span class="tag" :class="conflicts.can_generate ? 'ok' : 'err'">
            {{ conflicts.can_generate ? "不挡生成" : "会挡住生成" }}
          </span>
          <span v-if="!conflicts.has_archive" class="tag">无文风档案</span>
        </div>
        <p class="faint">{{ conflicts.note }}</p>
        <ul v-if="conflicts.conflicts.length" class="conflict-list">
          <li v-for="(c, i) in conflicts.conflicts" :key="`cf-${i}`">
            <span class="tag err">{{ c.dimension }}</span>
            <b>{{ c.word }}</b>
            <span class="faint">规则：{{ c.rule_text }}</span>
            <div class="faint">上下文：{{ c.context }}</div>
          </li>
        </ul>
        <ul v-if="conflicts.attributive.length" class="conflict-list">
          <li v-for="(c, i) in conflicts.attributive" :key="`at-${i}`">
            <span class="tag warn">{{ c.dimension }}</span>
            <b>{{ c.word }}</b>
            <span class="faint">名词性用法 · 不挡生成</span>
            <div class="faint">上下文：{{ c.context }}</div>
          </li>
        </ul>
      </template>
    </div>

    <div v-if="styleLoading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="styleErr" class="notice err">{{ styleErr }}</div>
    <div v-else-if="styleWarnings.length === 0" class="empty">
      暂无告警——所有生成段都在 99.5 分位以内。
    </div>
    <div v-else class="warn-list">
      <article
        v-for="w in styleWarnings"
        :key="w.segment_no + '_' + w.generated_at"
        class="warn-item"
      >
        <header class="warn-head">
          <span class="warn-seg">第 {{ w.segment_no }} 段</span>
          <time class="warn-time">{{ fmtTime(w.generated_at) }}</time>
          <span class="warn-pct">{{ fmtPct(w.percentile) }}</span>
          <span v-if="w.bucket" class="chip" :style="bucketChipStyle(w.bucket)">{{ w.bucket }}</span>
          <span v-if="w.scale_ratio != null" class="warn-meta">口径 {{ w.scale_ratio }}x</span>
          <span v-if="w.chars != null" class="warn-meta">{{ w.chars }} 字</span>
        </header>
        <div v-if="w.worst_features?.length" class="warn-features">
          <span v-for="f in w.worst_features" :key="f" class="chip">{{ f }}</span>
        </div>
        <p class="warn-msg">{{ w.warn_message || "本段文风偏离档案，但没有附带说明。" }}</p>
        <details class="warn-raw">
          <summary>技术指标</summary>
          <span>mean_abs_z {{ w.mean_abs_z?.toFixed(2) ?? "—" }}</span>
          <span>percentile {{ fmtPct(w.percentile) }}</span>
        </details>
      </article>
    </div>
  </section>

  <div class="row" style="margin-top: var(--gap-lg)">
    <router-link class="btn ghost" :to="`/projects/${projectId}`">← 返回项目工作台</router-link>
  </div>
  </div>
  </div>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.kg-tabs {
  margin-bottom: var(--gap-lg);
}
.tab {
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  padding: 8px 15px;
  border-radius: var(--radius-full);
  border: var(--border-w) solid var(--border);
  background: var(--bg-elev);
  color: var(--fg-muted);
  cursor: pointer;
  transition: background .14s ease, border-color .14s ease, color .14s ease;
}
.tab:hover {
  border-color: var(--accent-line);
  color: var(--accent-ink);
}
.tab.active {
  background: var(--accent-soft);
  border-color: var(--accent-line);
  color: var(--accent-ink);
  box-shadow: var(--shadow-sm);
}
.list-item.active {
  border-color: var(--accent);
}
.tag.err {
  background: var(--danger-soft);
  color: var(--danger);
}
.tag.warn {
  background: var(--warn-soft);
  color: var(--warn);
}
.tag.ok {
  background: var(--ok-soft);
  color: var(--ok);
}
.tag.info {
  background: var(--info-soft);
  color: var(--info);
}
/* 语料摄取状态条：失败态必须一眼可见，不能混在空数据里 */
.corpus-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin: 0 0 var(--gap-lg);
  padding: 10px 14px;
  border: var(--border-w) solid var(--border);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  font-size: var(--fs-sm);
}
.corpus-status__label {
  font-size: var(--fs-3xs);
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
  color: var(--fg-muted);
}
.corpus-status__book {
  font-family: var(--mono, monospace);
  font-size: var(--fs-xs);
}
.corpus-status--idle {
  color: var(--fg-muted);
}
.corpus-status--ok {
  background: var(--ok-soft);
  border-color: var(--ok);
  border-left-color: var(--ok);
  color: var(--ok);
}
.corpus-status--warn {
  background: var(--warn-soft);
  border-color: var(--warn);
  border-left-color: var(--warn);
  color: var(--warn);
}
.corpus-status--error {
  background: var(--danger-soft);
  border-color: var(--danger);
  border-left-color: var(--danger);
  color: var(--danger);
}
.corpus-status__err {
  color: var(--danger);
}
.scope-detail {
  width: 100%;
  color: var(--fg-muted);
  font-size: var(--fs-xs);
}
.scope-detail summary { cursor: pointer; }
.scope-detail p { margin: 6px 0 0; line-height: 1.7; }
.corpus-failures {
  width: 100%;
  color: var(--fg-muted);
}
.corpus-failures summary {
  cursor: pointer;
}
.corpus-failures ul {
  margin: 6px 0 0;
  padding-left: 18px;
}
.corpus-failures li {
  margin-bottom: 4px;
  word-break: break-all;
}

/* ---------- Style 审计：告警卡片（替代 9 列表格） ---------- */
.style-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}
.style-head h2 { margin: 0; }
.style-kicker {
  color: var(--accent-ink, var(--accent));
  font-size: var(--fs-3xs);
  font-weight: 700;
  letter-spacing: .15em;
  text-transform: uppercase;
}
.style-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--fg-muted);
  font-size: var(--fs-xs);
}
.style-summary b {
  color: var(--fg);
  font-family: var(--mono, monospace);
  font-size: var(--fs-sm);
}
.style-note { margin: 14px 0 18px; }
.style-note summary {
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.style-note p {
  max-width: 76ch;
  margin: 8px 0 0;
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  line-height: 1.8;
}
.warn-list { display: flex; flex-direction: column; gap: 10px; }
.warn-item {
  padding: 14px 16px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.warn-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.warn-seg {
  font-family: var(--mono, monospace);
  font-size: var(--fs-sm);
  font-weight: 700;
}
.warn-time {
  color: var(--fg-muted);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
}
.warn-pct {
  padding: 2px 8px;
  border: 1px solid var(--warn);
  border-radius: 999px;
  color: var(--warn);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
  font-weight: 600;
}
.warn-meta { color: var(--fg-muted); font-size: var(--fs-xs); }
.warn-features {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 9px;
}
.warn-msg {
  margin: 9px 0 0;
  color: var(--fg);
  font-size: var(--fs-sm);
  line-height: 1.75;
}
.warn-raw { margin-top: 8px; }
.warn-raw summary {
  color: var(--fg-muted);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.warn-raw span {
  margin-right: 16px;
  color: var(--fg-muted);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
}
@media (max-width: 720px) {
  .style-head { flex-direction: column; align-items: flex-start; }
}

/* ---------- Judge：规则折叠 + 告警卡片 ---------- */
.judge-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
}
.judge-stats { display: flex; gap: 8px; }
.judge-stat {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg-elev);
  font-size: var(--fs-xs);
}
.judge-stat b { margin-right: 4px; font-family: var(--mono, monospace); }
.judge-stat.err b { color: var(--danger); }
.judge-stat.warn b { color: var(--warn); }
.judge-rules { flex: 1; min-width: 220px; }
.judge-rules summary {
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  cursor: pointer;
}
.judge-rule-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 6px 18px;
  margin-top: 10px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.judge-rule {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: var(--fs-xs);
}
.judge-rule code {
  color: var(--fg);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
}
.judge-rule-desc { color: var(--fg-muted); }
.judge-list { display: flex; flex-direction: column; gap: 8px; }
.judge-item {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--warn);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.judge-item.err { border-left-color: var(--danger); }
.judge-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.judge-sev {
  padding: 1px 7px;
  border-radius: 999px;
  background: var(--warn-soft);
  color: var(--warn);
  font-size: var(--fs-3xs);
  font-weight: 700;
  text-transform: uppercase;
}
.judge-sev.err { background: var(--danger-soft); color: var(--danger); }
.judge-rule-name {
  color: var(--fg-muted);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
}
.judge-entity { font-size: var(--fs-sm); }
.judge-seg {
  color: var(--fg-muted);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
}
.judge-exempt { margin-left: auto; font-size: var(--fs-2xs); }
.judge-msg { margin: 8px 0 0; font-size: var(--fs-sm); line-height: 1.7; }
.judge-evidence { margin-top: 6px; }
.judge-evidence summary {
  color: var(--fg-muted);
  font-size: var(--fs-2xs);
  cursor: pointer;
}
.judge-evidence div {
  color: var(--fg-muted);
  font-family: var(--mono, monospace);
  font-size: var(--fs-2xs);
}
.judge-exemptions { margin-top: var(--gap-lg); }
.judge-exemptions summary {
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  cursor: pointer;
}
@media (max-width: 720px) {
  .judge-rule-grid { grid-template-columns: 1fr; }
}

/*
 * 宽屏一屏布局：抬头 + 两条状态条 + 标签栏固定，标签内容区内部滚动。
 * 高度 = 视口 − 顶栏 60px − .main 上下内边距（默认 30 + 56）。
 * 窄屏不锁定高度，保持页面自然滚动。
 */
@media (min-width: 881px) {
  .kg-page {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 146px);
    height: calc(100dvh - 146px);
    min-height: 520px;
  }
  .kg-page > .page-head {
    flex: none;
    margin-bottom: 12px;
  }
  .kg-page > .corpus-status {
    flex: none;
  }
  .kg-page > .kg-tabs {
    flex: none;
    margin-bottom: 10px;
  }
  .kg-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding-right: 6px;
    padding-bottom: 8px;
  }
}
@media (min-width: 881px) and (max-width: 1100px) {
  /* .main 内边距变成 26 + 48 */
  .kg-page {
    height: calc(100vh - 134px);
    height: calc(100dvh - 134px);
  }
}
.world-groups {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: var(--gap);
  margin-bottom: var(--gap);
}
.world-group {
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 10px 12px;
}
.world-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.world-item {
  padding: 5px 0;
  border-top: 1px dashed var(--border);
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.world-item:first-of-type { border-top: 0; }
.world-name { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.timeline-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin: var(--gap) 0 8px;
  padding-top: var(--gap);
  border-top: 1px solid var(--border);
}
.conflict-block {
  margin: 10px 0 var(--gap);
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.conflict-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.conflict-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.conflict-list li {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 0;
  border-top: 1px dashed var(--border);
}
.conflict-list li:first-child { border-top: 0; }
</style>
