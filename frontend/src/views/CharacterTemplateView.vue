<script setup lang="ts">
defineOptions({ name: "CharacterTemplateView" });
/**
 * 人物塑造模板页（/projects/:id/templates）。
 *
 * 后端协议：
 * - `POST /api/projects/{id}/character-template` 提交提炼（后台任务，可复用库里同档产物）；
 * - `GET  /api/projects/character-templates` 模板库列表（跨项目复用）；
 * - `POST /api/projects/{id}/character-templates/apply` 应用到本项目（只记引用，不改正文）。
 *
 * 界面纪律：
 * 1. **提炼与档位绑定**：项目切了档，旧档模板不会被注入（后端已按档位过滤）；
 * 2. **应用是显式动作**：列表默认只读，点「应用」才写项目引用；
 * 3. **复用要说清**：`from_cache=true` 时明确提示「复用库里的产物，没有重新调模型」。
 */
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import { errorText as textOf } from "@/utils/errors";
import ProjectNav from "@/components/ProjectNav.vue";
import type {
  CharacterTemplateDetail,
  CharacterTemplateItem,
  DistillJob,
  NarrativeTier,
  NarrativeTiersResponse,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const projectId = computed(() => Number(route.params.id));

const project = ref<ProjectOut | null>(null);
const tiers = ref<NarrativeTiersResponse | null>(null);
const templates = ref<CharacterTemplateItem[]>([]);
const loading = ref(true);
const loadError = ref("");

const tier = ref("");
const force = ref(false);
const starting = ref(false);
const job = ref<DistillJob | null>(null);
const actionError = ref("");
const actionNote = ref("");

const applyingId = ref<string | null>(null);
const clearingId = ref<string | null>(null);

// —— 规则详情预览（列表只有条数，点开才取规则全文） ——
const expandedId = ref<string | null>(null);
const detailCache = ref<Record<string, CharacterTemplateDetail>>({});
const detailLoadingId = ref<string | null>(null);
const detailError = ref("");

let pollTimer: number | null = null;

onUnmounted(() => {
  if (pollTimer !== null) window.clearTimeout(pollTimer);
});

function stopPoll(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

const tierList = computed<NarrativeTier[]>(() => tiers.value?.tiers ?? []);

const tierName = computed(() => {
  const found = tierList.value.find((t) => t.key === tier.value);
  return found?.name || tier.value || "默认档";
});

const appliedId = computed(() => project.value?.character_template_id ?? null);

function statusLabel(status: string | null | undefined): string {
  if (status === "pending") return "排队中";
  if (status === "running") return "提炼中";
  if (status === "success") return "已完成";
  if (status === "failed") return "失败";
  return status || "—";
}

function tierLabel(item: CharacterTemplateItem): string {
  return tierList.value.find((t) => t.key === item.tier)?.name || item.tier;
}

async function loadTemplates(): Promise<void> {
  try {
    const res = await api.listCharacterTemplates();
    templates.value = res.items;
  } catch (err) {
    actionError.value = `读取模板库失败：${textOf(err)}`;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const [proj, tierRes] = await Promise.all([
      api.getProject(projectId.value),
      api.listNarrativeTiers(),
    ]);
    project.value = proj;
    tiers.value = tierRes;
    tier.value = proj.narrative_tier || tierRes.default || tierList.value[0]?.key || "full";
    await loadTemplates();
    await resumeActiveJob();
  } catch (err) {
    loadError.value = `加载失败：${textOf(err)}`;
  } finally {
    loading.value = false;
  }
}

/**
 * 回到页面时接上还在跑的任务：进度落在 distill_runs，关掉页面不该丢。
 * 只认人物模板任务，别把叙事蒸馏/全书摘要的进度接到这里。
 */
async function resumeActiveJob(): Promise<void> {
  try {
    const res = await api.listDistillJobs(projectId.value, 10);
    const active = res.items.find(
      (j) =>
        j.kind === "character_template" &&
        (j.status === "pending" || j.status === "running"),
    );
    if (!active) return;
    job.value = active;
    starting.value = true;
    actionNote.value = "检测到人物模板提炼在后台运行，已接上进度。";
    void pollJob(active.id);
  } catch {
    /* 续看只是增强：读不到任务列表不影响本页其他操作 */
  }
}

async function pollJob(id: number): Promise<void> {
  stopPoll();
  try {
    const current = await api.getDistillJob(projectId.value, id);
    job.value = current;
    if (current.status === "pending" || current.status === "running") {
      pollTimer = window.setTimeout(() => {
        void pollJob(id);
      }, 1500);
      return;
    }
    starting.value = false;
    if (current.status === "success") {
      actionNote.value = "模板已提炼完成，已刷新模板库。";
      await loadTemplates();
      project.value = await api.getProject(projectId.value).catch(() => project.value);
    } else {
      actionError.value = `提炼失败：${current.error || "后台任务没有给出原因"}`;
    }
  } catch (err) {
    starting.value = false;
    actionError.value = `读取任务进度失败：${textOf(err)}`;
  }
}

async function start(): Promise<void> {
  if (starting.value) return;
  stopPoll();
  starting.value = true;
  actionError.value = "";
  actionNote.value = "";
  job.value = null;
  try {
    const res = await api.startCharacterTemplate(projectId.value, {
      tier: tier.value || null,
      force: force.value,
    });
    if (res.reused) {
      actionNote.value = `这条档位已有任务在跑，已接上进度（#${res.job_id}）。`;
    } else if (res.from_cache) {
      actionNote.value = "库里已有同档同语料的模板，本次直接复用，不会重新调模型。";
    } else {
      actionNote.value = "已提交，正在后台提炼。";
    }
    await pollJob(res.job_id);
  } catch (err) {
    starting.value = false;
    actionError.value = `提交失败：${textOf(err)}`;
  }
}

async function applyTemplate(item: CharacterTemplateItem): Promise<void> {
  if (applyingId.value) return;
  applyingId.value = item.id;
  actionError.value = "";
  actionNote.value = "";
  try {
    project.value = await api.applyCharacterTemplate(projectId.value, item.id);
    actionNote.value = `已应用「${tierLabel(item)}」模板（${item.rule_count} 条规则），只记引用，不改正文。`;
  } catch (err) {
    actionError.value = `应用失败：${textOf(err)}`;
  } finally {
    applyingId.value = null;
  }
}

/** 取消应用：只清项目引用；模板库产物和正文都不动。 */
async function clearTemplate(item: CharacterTemplateItem): Promise<void> {
  if (clearingId.value) return;
  clearingId.value = item.id;
  actionError.value = "";
  actionNote.value = "";
  try {
    project.value = await api.clearCharacterTemplate(projectId.value);
    actionNote.value = `已取消应用「${tierLabel(item)}」模板；模板库里的产物仍保留，可随时再应用。`;
  } catch (err) {
    actionError.value = `取消应用失败：${textOf(err)}`;
  } finally {
    clearingId.value = null;
  }
}

/** 展开/收起规则；首次展开才拉详情，之后走内存缓存。 */
async function toggleDetail(item: CharacterTemplateItem): Promise<void> {
  if (expandedId.value === item.id) {
    expandedId.value = null;
    return;
  }
  expandedId.value = item.id;
  detailError.value = "";
  if (detailCache.value[item.id]) return;
  detailLoadingId.value = item.id;
  try {
    const detail = await api.getCharacterTemplate(item.id);
    detailCache.value = { ...detailCache.value, [item.id]: detail };
  } catch (err) {
    detailError.value = `读取规则失败：${textOf(err)}`;
  } finally {
    detailLoadingId.value = null;
  }
}

onMounted(load);
</script>

<template>
  <section class="tpl-page">
    <ProjectNav :project-id="projectId" />
    <header class="page-head">
      <div>
        <p class="faint kicker">写作素材</p>
        <h1>人物塑造模板</h1>
        <p class="faint">
          从参考语料提炼「怎么把人物写活」的规则（性格 / 行为 / 关系 / 口头禅），
          按档位入库、跨项目复用；应用到项目后才会进入生成提示。
        </p>
      </div>
      <router-link class="btn" :to="`/projects/${projectId}`">返回工作台</router-link>
    </header>

    <div v-if="loading" class="card faint">加载项目与模板库…</div>
    <div v-else-if="loadError" class="notice err" role="alert">
      {{ loadError }}
      <button class="link-btn" type="button" @click="load">重试</button>
    </div>

    <template v-else>
      <div v-if="!(project?.reference_novel_ids ?? []).length" class="card notice warn">
        这个项目还没有选参考语料，无法提炼人物模板。
        <router-link class="link-btn" :to="`/projects/${projectId}`">去工作台选择</router-link>
      </div>

      <div class="card">
        <div class="form-row">
          <label class="field">
            <span>提炼档位</span>
            <select v-model="tier" class="input" :disabled="starting">
              <option v-for="t in tierList" :key="t.key" :value="t.key">
                {{ t.name }} · {{ t.cost_hint }}
              </option>
            </select>
          </label>
          <label class="field check">
            <input v-model="force" type="checkbox" :disabled="starting" />
            <span>同档位也强制重抽</span>
          </label>
        </div>
        <p class="faint tier-note">
          当前档位：{{ tierName }}。换档后旧档模板不会被注入（后端按档位过滤，避免拿 A 档方法论配 B 档档案）。
        </p>

        <div class="actions">
          <button class="btn primary" type="button" :disabled="starting" @click="start">
            {{ starting ? "提炼中…" : "提炼人物模板" }}
          </button>
          <button class="btn" type="button" :disabled="starting" @click="loadTemplates">
            刷新模板库
          </button>
        </div>

        <div v-if="actionNote && !actionError" class="notice info">{{ actionNote }}</div>
        <div v-if="actionError" class="notice err" role="alert">{{ actionError }}</div>

        <div v-if="job && starting" class="job">
          <div class="job-head">
            <span class="tag accent">任务 #{{ job.id }}</span>
            <span class="tag">{{ statusLabel(job.status) }}</span>
            <span class="faint">{{ job.stage || "等待后台开始" }}</span>
          </div>
          <div v-if="job.progress_total" class="job-bar">
            <span
              class="job-fill"
              :style="{ width: `${Math.min(100, (job.progress_current / job.progress_total) * 100)}%` }"
            />
          </div>
          <p v-if="job.progress_total" class="faint">
            {{ job.progress_current }} / {{ job.progress_total }} 块
          </p>
        </div>
      </div>

      <div class="card">
        <div class="list-head">
          <h2>模板库</h2>
          <span class="faint">{{ templates.length }} 条 · 新的在前</span>
        </div>
        <div v-if="!templates.length" class="empty">
          库里还没有人物模板。选好档位后点上方「提炼人物模板」。
        </div>
        <ul v-else class="tpl-list">
          <li v-for="item in templates" :key="item.id" class="tpl-item">
            <div class="tpl-main">
              <div class="tpl-title">
                <b>{{ tierLabel(item) }}</b>
                <span v-if="item.id === appliedId" class="tag ok">已应用</span>
                <button class="link-btn" type="button" @click="toggleDetail(item)">
                  {{ expandedId === item.id ? "收起规则" : "查看规则" }}
                </button>
              </div>
              <p class="faint">
                {{ item.rule_count }} 条规则 · 采样 {{ item.n_chunks }} 块
                <template v-if="item.rejected"> · 证据不足被拒 {{ item.rejected }}</template>
                <template v-if="item.applied_at"> · 最近应用 {{ fmtDate(item.applied_at) }}</template>
              </p>
              <p class="faint mono tpl-id">{{ item.id }}</p>
              <div v-if="expandedId === item.id" class="rules-box">
                <span v-if="detailLoadingId === item.id" class="faint">读取规则…</span>
                <span v-else-if="detailError" class="notice err">{{ detailError }}</span>
                <template v-else-if="detailCache[item.id]">
                  <ul v-if="detailCache[item.id].rules.length" class="rules">
                    <li v-for="(rule, idx) in detailCache[item.id].rules" :key="idx">
                      {{ rule }}
                    </li>
                  </ul>
                  <p v-else class="faint">这条模板没有可展示的规则。</p>
                </template>
              </div>
            </div>
            <div class="tpl-actions">
              <span class="faint tpl-time">{{ fmtDate(item.created_at) }}</span>
              <button
                v-if="item.id === appliedId"
                class="btn sm"
                type="button"
                :disabled="clearingId !== null"
                @click="clearTemplate(item)"
              >
                {{ clearingId === item.id ? "取消中…" : "取消应用" }}
              </button>
              <button
                v-else
                class="btn sm"
                type="button"
                :disabled="applyingId !== null"
                @click="applyTemplate(item)"
              >
                {{ applyingId === item.id ? "应用中…" : "应用到本项目" }}
              </button>
            </div>
          </li>
        </ul>
      </div>
    </template>
  </section>
</template>

<style scoped>
.tpl-page {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--gap);
  flex-wrap: wrap;
}
.page-head h1 {
  margin: 2px 0 4px;
  font-family: var(--font-display);
  font-size: var(--fs-xl);
}
.kicker {
  margin: 0;
  font-size: var(--fs-2xs);
  letter-spacing: 0.08em;
}
.form-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--gap);
  align-items: flex-end;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 240px;
  font-size: var(--fs-xs);
}
.field.check {
  flex-direction: row;
  align-items: center;
  min-width: auto;
  padding-bottom: 6px;
}
.tier-note {
  margin: 8px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
}
.actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: var(--gap);
  flex-wrap: wrap;
}
.job {
  margin-top: var(--gap);
  padding-top: var(--gap);
  border-top: 1px dashed var(--border);
}
.job-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.job-bar {
  margin-top: 8px;
  height: 6px;
  border-radius: 999px;
  background: var(--accent-soft);
  overflow: hidden;
}
.job-fill {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}
.list-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.list-head h2 { margin: 0; }
.empty {
  padding: 14px 0;
  color: var(--fg-faint);
  font-size: var(--fs-sm);
}
.tpl-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
}
.tpl-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--gap);
  padding: 10px 0;
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}
.tpl-item:first-child { border-top: 0; }
.tpl-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.tpl-item p { margin: 2px 0; font-size: var(--fs-xs); }
.tpl-id { font-size: var(--fs-2xs); word-break: break-all; }
.rules-box {
  margin-top: 6px;
  padding: 8px 10px;
  border: 1px dashed var(--border);
  border-radius: var(--radius-md);
}
.rules {
  margin: 0;
  padding-left: 18px;
  font-size: var(--fs-xs);
  line-height: 1.8;
}
.rules li + li { margin-top: 2px; }
.tpl-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}
.tpl-time { font-size: var(--fs-2xs); }
.tag.ok { color: var(--ok); border-color: var(--ok); }
</style>
