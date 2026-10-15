<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import type {
  KgV3Character,
  KgV3EventsResponse,
  KgV3JudgeFinding,
  KgV3JudgeFindingsResponse,
  KgV3Relation,
  KgV3Slice,
} from "@/api/types";

const route = useRoute();
const projectId = Number(route.params.id);

// ---------- Tabs ----------
const TABS = [
  { key: "slice", label: "切片预览" },
  { key: "intel", label: "人物情报" },
  { key: "events", label: "事件时间线" },
  { key: "judge", label: "Judge 面板" },
  { key: "graph", label: "关系与节奏" },
  { key: "worker", label: "Worker 状态" },
] as const;
type TabKey = (typeof TABS)[number]["key"];
const activeTab = ref<TabKey>("slice");
function switchTab(k: TabKey) {
  activeTab.value = k;
  // 惰性加载：首次进入某 tab 才拉数据（切片在 onMounted 已拉）
  if (k === "intel") loadCharacters();
  else if (k === "events") loadEvents();
  else if (k === "judge") loadFindings();
  else if (k === "graph") loadGraphTab();
  else if (k === "worker") loadWorker();
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

function sevTag(sev: string): string {
  return sev === "err" ? "tag err" : "tag warn";
}

onMounted(() => {
  loadSlice();
});
</script>

<template>
  <header class="page-head">
    <div class="eyebrow">KG v3.0</div>
    <h1>知识图谱工作台</h1>
    <p>9 表实时读侧：切片预览 · 人物情报 · 事件时间线 · Judge 一致性告警 · 关系与节奏。</p>
  </header>

  <nav class="tabs" style="margin-bottom: var(--gap-lg)">
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
            <span class="faint mono" style="font-size: var(--fs-xs)">提及 {{ c.mention_count }}</span>
          </div>
          <div v-if="!characters.length" class="empty">项目里还没有角色（先生成几段让批抽数据进来）</div>
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
          <button class="btn ghost" style="margin-top: 12px; font-size: var(--fs-xs)" @click="knowCharacter = selectedChar.name; knowFact = charKnows[0] ?? ''">
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
      <div class="row tight" style="margin-bottom: var(--gap); flex-wrap: wrap">
        <span v-for="(r, name) in findings.rules" :key="name" class="tag" :class="r.severity === 'err' ? 'err' : 'warn'">
          {{ name }}: {{ r.desc }}
        </span>
      </div>
      <div v-if="judgeMsg" class="notice info" style="margin-bottom: var(--gap)">{{ judgeMsg }}</div>

      <div v-if="!findings.items.length" class="empty">没有落库告警（生成后 Judge 自动判定并落库；没告警 ≠ 规则没跑，见上方规则清单）</div>
      <div v-else class="list">
        <div v-for="(f, i) in findings.items" :key="i" class="list-item" style="align-items: flex-start">
          <div style="flex: 1">
            <div class="row tight" style="margin-bottom: 4px">
              <span :class="sevTag(f.severity)">{{ f.severity }}</span>
              <span class="tag">{{ f.rule }}</span>
              <strong>{{ f.entity || "—" }}</strong>
              <span class="faint mono" style="font-size: var(--fs-xs)">段 {{ f.segment_no ?? "?" }}</span>
              <span v-if="f.exempted" class="tag">已豁免</span>
            </div>
            <div style="font-size: var(--fs-sm)">{{ f.message }}</div>
            <details v-if="f.evidence.length" style="margin-top: 4px">
              <summary class="faint" style="font-size: var(--fs-xs); cursor: pointer">证据（{{ f.evidence.length }}）</summary>
              <div v-for="(ev, j) in f.evidence" :key="j" class="faint mono" style="font-size: var(--fs-xs)">{{ ev }}</div>
            </details>
            <div v-if="!f.exempted" style="margin-top: 6px">
              <button class="btn ghost" style="font-size: var(--fs-xs)" @click="askExempt(f)">豁免此告警…</button>
            </div>
          </div>
        </div>
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

      <div class="card-title" style="font-size: var(--fs-md); margin-top: var(--gap-lg)">
        已豁免 <span class="hint">{{ exemptions.length }}</span>
      </div>
      <div v-if="!exemptions.length" class="empty">无豁免</div>
      <div v-else class="list">
        <div v-for="(x, i) in exemptions" :key="i" class="list-item">
          <div style="flex: 1">
            <strong>{{ x.rule }}</strong> / {{ x.entity || "（无实体）" }}
            <div v-if="x.note" class="faint" style="font-size: var(--fs-xs)">{{ x.note }}</div>
          </div>
          <button class="btn ghost" style="font-size: var(--fs-xs)" @click="revoke(x.rule, x.entity)">撤销</button>
        </div>
      </div>
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
                style="font-size: 9px" fill="var(--fg-faint)"
              >{{ e.rel_type }}</text>
            </g>
            <g v-for="n in ringNodes" :key="n.name">
              <circle :cx="n.x" :cy="n.y" r="16" fill="var(--accent-soft)" stroke="var(--accent)" stroke-width="1" />
              <text :x="n.x" :y="n.y + 4" text-anchor="middle" style="font-size: 10px; font-weight: 700" fill="var(--accent-ink)">{{
                n.name.length > 4 ? n.name.slice(0, 4) : n.name
              }}</text>
              <text :x="n.x" :y="n.y + 28" text-anchor="middle" style="font-size: 10px" fill="var(--fg)">{{ n.name }}</text>
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
            <text :x="p.x" :y="p.y - 8" text-anchor="middle" style="font-size: 9px" fill="var(--fg-muted)">{{ p.strength.toFixed(1) }}</text>
            <text :x="p.x" :y="p.H - 6" text-anchor="middle" style="font-size: 9px" fill="var(--fg-faint)">ch{{ p.chapter ?? "?" }}</text>
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

  <div class="row" style="margin-top: var(--gap-lg)">
    <router-link class="btn ghost" :to="`/projects/${projectId}`">← 返回项目工作台</router-link>
  </div>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.tab {
  font: inherit;
  font-size: var(--fs-sm);
  font-weight: 600;
  padding: 8px 14px;
  border-radius: var(--radius-full);
  border: var(--border-w) solid var(--border);
  background: var(--bg-elev);
  color: var(--fg-muted);
  cursor: pointer;
}
.tab.active {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-fg);
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
</style>
