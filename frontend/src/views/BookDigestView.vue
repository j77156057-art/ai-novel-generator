<script setup lang="ts">
defineOptions({ name: "BookDigestView" });
/**
 * 全书摘要页（/projects/:id/digest）。
 *
 * 后端协议：
 * - `POST /api/projects/{id}/digests` 提交（立即返回 job_id，后台跑）；
 * - `GET  /api/books/{novel_id}/digest` 取最新摘要（没有时 404）；
 * - `GET  /api/books/{novel_id}/digest/verify` 取三项验收。
 *
 * 界面纪律：
 * 1. **不假装有内容**：没有摘要就显示空态，不画占位正文；
 * 2. **验收结果原样展示**：`ok=null` 的项标「不适用」，不能当通过；
 * 3. **进度可续看**：任务行落在 distill_runs，离开页面再回来仍能轮询到状态。
 */
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { ApiError, api } from "@/api/client";
import { errorText as textOf } from "@/utils/errors";
import ProjectNav from "@/components/ProjectNav.vue";
import type {
  BookDigest,
  BookDigestCheck,
  BookDigestSummary,
  BookDigestVerify,
  DistillJob,
  NovelOut,
  ProjectOut,
} from "@/api/types";

const route = useRoute();
const projectId = computed(() => Number(route.params.id));

const project = ref<ProjectOut | null>(null);
const novels = ref<NovelOut[]>([]);
const loading = ref(true);
const loadError = ref("");

const novelId = ref<number | null>(null);
const mode = ref<"auto" | "whole" | "retrieval">("auto");
const targetRatio = ref(10);
const force = ref(false);

const starting = ref(false);
const job = ref<DistillJob | null>(null);
const actionError = ref("");
const actionNote = ref("");

const digest = ref<BookDigest | null>(null);
const verify = ref<BookDigestVerify | null>(null);
const digestLoading = ref(false);
const digestError = ref("");

// —— 历史版本对比 ——
const history = ref<BookDigestSummary[]>([]);
const historyError = ref("");
const compareA = ref<number | null>(null);
const compareB = ref<number | null>(null);
const comparing = ref(false);
const compareError = ref("");
const compareLeft = ref<BookDigest | null>(null);
const compareRight = ref<BookDigest | null>(null);

const MODE_LABELS: Record<string, string> = {
  auto: "自动（按书大小与渠道能力选）",
  whole: "整本直读",
  retrieval: "检索式（取代表性章节）",
};

const CHECK_LABELS: Record<string, string> = {
  compression: "压缩比",
  mainline_coverage: "主线覆盖",
  character_coverage: "角色覆盖",
  evidence: "证据缺失率",
};

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

/** 只列本项目选过的参考语料；没选过时退回全部语料（并给出提示）。 */
const books = computed(() => {
  const ids = new Set(project.value?.reference_novel_ids ?? []);
  return novels.value.filter((n) => ids.size === 0 || ids.has(n.id));
});

const refsEmpty = computed(() => {
  const refs = project.value?.reference_novel_ids ?? [];
  return refs.length === 0;
});

const currentBook = computed(
  () => novels.value.find((n) => n.id === novelId.value) ?? null,
);

const checks = computed<[string, BookDigestCheck][]>(() =>
  Object.entries(verify.value?.checks ?? digest.value?.checks ?? {}),
);

const passed = computed(() => verify.value?.passed ?? digest.value?.verify_passed ?? null);

function fmtDate(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString();
}

function statusLabel(status: string | null | undefined): string {
  if (status === "pending") return "排队中";
  if (status === "running") return "生成中";
  if (status === "success") return "已完成";
  if (status === "failed") return "失败";
  return status || "—";
}

/** 百分比展示：后端存的是 0~1 的比值。 */
function pct(value: unknown): string {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
}

/** 单项验收的一句话明细（不同 check 的字段不同，这里按项分派）。 */
function checkDetail(key: string, c: BookDigestCheck): string {
  if (key === "compression") {
    // 后端 ratio 是「输出/输入」（0.015），而用户理解的压缩比是「输入/输出」（66:1）。
    // 直接显示 ratio.toFixed(1) 会变成「0.0:1」，看起来像压缩失败，这里换算回人话。
    const charsIn = typeof c.chars_in === "number" ? c.chars_in : null;
    const charsOut = typeof c.chars_out === "number" ? c.chars_out : null;
    const folded =
      charsIn != null && charsOut != null && charsOut > 0
        ? (charsIn / charsOut).toFixed(1)
        : "—";
    const target = typeof c.target_ratio === "number" ? c.target_ratio : "—";
    return `${charsIn ?? "—"} → ${charsOut ?? "—"} 字 · ${folded}:1（目标 ≥${target}:1）`;
  }
  if (key === "mainline_coverage" || key === "character_coverage") {
    return `覆盖 ${c.covered ?? 0}/${c.candidates ?? 0} · ${pct(c.value)}`;
  }
  if (key === "evidence") {
    return `保留 ${c.kept ?? 0} · 丢弃 ${c.dropped ?? 0} · 丢弃率 ${pct(c.dropped_ratio)}`;
  }
  return typeof c.value === "number" ? pct(c.value) : "";
}

function checkTone(c: BookDigestCheck): "ok" | "warn" | "muted" {
  if (c.ok === true) return "ok";
  if (c.ok === false) return "warn";
  return "muted";
}

function checkText(c: BookDigestCheck): string {
  if (c.ok === true) return "通过";
  if (c.ok === false) return "未通过";
  return "不适用";
}

function versionLabel(v: BookDigestSummary): string {
  return `#${v.id} · ${fmtDate(v.created_at)} · ${v.status === "archived" ? "旧版" : "当前"}`;
}

function statNum(v: BookDigestSummary, key: string): string {
  const raw = v.stats?.[key];
  return typeof raw === "number" ? String(raw) : "—";
}

const compareSides = computed(() => [
  { key: "A", data: compareLeft.value },
  { key: "B", data: compareRight.value },
]);

async function compareVersions(): Promise<void> {
  if (novelId.value == null || compareA.value == null || compareB.value == null) return;
  comparing.value = true;
  compareError.value = "";
  try {
    const [a, b] = await Promise.all([
      api.getBookDigestVersion(novelId.value, compareA.value),
      api.getBookDigestVersion(novelId.value, compareB.value),
    ]);
    compareLeft.value = a;
    compareRight.value = b;
  } catch (err) {
    compareError.value = `读取版本失败：${textOf(err)}`;
  } finally {
    comparing.value = false;
  }
}

/** 历史列表：两个下拉默认选最新 + 次新，两版以上时自动对比一次。 */
async function loadHistory(): Promise<void> {
  if (novelId.value == null) return;
  historyError.value = "";
  compareError.value = "";
  try {
    const res = await api.listBookDigests(novelId.value, 20);
    history.value = res.items;
    const ids = res.items.map((x) => x.id);
    if (compareA.value == null || !ids.includes(compareA.value)) {
      compareA.value = ids[0] ?? null;
    }
    if (compareB.value == null || !ids.includes(compareB.value)) {
      compareB.value = ids[1] ?? null;
    }
    if (ids.length >= 2 && compareA.value != null && compareB.value != null) {
      await compareVersions();
    } else {
      compareLeft.value = null;
      compareRight.value = null;
    }
  } catch (err) {
    historyError.value = `读取历史版本失败：${textOf(err)}`;
    history.value = [];
    compareLeft.value = null;
    compareRight.value = null;
  }
}

async function loadDigest(): Promise<void> {
  if (novelId.value == null) return;
  digestLoading.value = true;
  digestError.value = "";
  digest.value = null;
  verify.value = null;
  try {
    digest.value = await api.getBookDigest(novelId.value);
    verify.value = await api.verifyBookDigest(novelId.value).catch(() => null);
  } catch (err) {
    // 404 = 这本书还没有摘要，是正常空态，不是错误。
    if (err instanceof ApiError && err.status === 404) {
      digestError.value = "";
    } else {
      digestError.value = `读取摘要失败：${textOf(err)}`;
    }
  } finally {
    digestLoading.value = false;
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
      actionNote.value = "摘要已生成，正在刷新验收结果。";
      await loadDigest();
      await loadHistory();
    } else {
      actionError.value = `生成失败：${current.error || "后台任务没有给出原因"}`;
    }
  } catch (err) {
    starting.value = false;
    actionError.value = `读取任务进度失败：${textOf(err)}`;
  }
}

async function start(): Promise<void> {
  if (novelId.value == null || starting.value) return;
  stopPoll();
  starting.value = true;
  actionError.value = "";
  actionNote.value = "";
  job.value = null;
  try {
    const res = await api.startBookDigest(projectId.value, {
      novel_id: novelId.value,
      mode: mode.value,
      target_ratio: targetRatio.value,
      force: force.value,
    });
    actionNote.value = res.reused
      ? "这本书已有摘要在跑，已接上那条任务的进度。"
      : "已提交，正在后台生成。";
    await pollJob(res.job_id);
  } catch (err) {
    starting.value = false;
    actionError.value = `提交失败：${textOf(err)}`;
  }
}

async function load(): Promise<void> {
  loading.value = true;
  loadError.value = "";
  try {
    const [proj, list] = await Promise.all([
      api.getProject(projectId.value),
      api.listNovels(),
    ]);
    project.value = proj;
    novels.value = list;
    const refs = proj.reference_novel_ids ?? [];
    const preferred =
      refs.find((id) => list.some((n) => n.id === id)) ?? list[0]?.id ?? null;
    novelId.value = preferred;
    if (preferred != null) {
      await loadDigest();
      await resumeActiveJob();
      await loadHistory();
    }
  } catch (err) {
    loadError.value = `加载失败：${textOf(err)}`;
  } finally {
    loading.value = false;
  }
}

/**
 * 回到页面时接上还在跑的任务：进度落在 distill_runs，关掉页面不该丢。
 * 只认「这本书的摘要任务」，别的书/别的产物的任务不在这里接。
 */
async function resumeActiveJob(): Promise<void> {
  if (novelId.value == null) return;
  try {
    const res = await api.listDistillJobs(projectId.value, 10);
    const active = res.items.find(
      (j) =>
        j.kind === "book_digest" &&
        j.novel_id === novelId.value &&
        (j.status === "pending" || j.status === "running"),
    );
    if (!active) return;
    job.value = active;
    starting.value = true;
    actionNote.value = "检测到这本书的摘要在后台运行，已接上进度。";
    void pollJob(active.id);
  } catch {
    /* 续看只是增强：读不到任务列表不影响本页其他操作 */
  }
}

function onBookChange(): void {
  stopPoll();
  starting.value = false;
  job.value = null;
  actionError.value = "";
  actionNote.value = "";
  history.value = [];
  compareA.value = null;
  compareB.value = null;
  compareLeft.value = null;
  compareRight.value = null;
  void loadDigest();
  void resumeActiveJob();
  void loadHistory();
}

onMounted(load);
</script>

<template>
  <section class="digest-page">
    <ProjectNav :project-id="projectId" />
    <header class="page-head">
      <div>
        <p class="faint kicker">写作素材</p>
        <h1>全书摘要</h1>
        <p class="faint">
          把整本参考书压成结构化摘要（时间线 / 角色 / 反应），供续写时按需检索；
          产物按书存，换项目也能复用。
        </p>
      </div>
      <router-link class="btn" :to="`/projects/${projectId}`">返回工作台</router-link>
    </header>

    <div v-if="loading" class="card faint">加载项目与语料…</div>
    <div v-else-if="loadError" class="notice err" role="alert">
      {{ loadError }}
      <button class="link-btn" type="button" @click="load">重试</button>
    </div>

    <template v-else>
      <div v-if="!books.length" class="card notice warn">
        这个项目还没有可用的参考语料。
        <span v-if="refsEmpty">先去工作台第 1 步选参考书。</span>
        <span v-else>所选参考书可能已被删除。</span>
        <router-link class="link-btn" :to="`/projects/${projectId}`">去选择</router-link>
      </div>

      <template v-else>
        <div class="card">
          <div class="form-row">
            <label class="field">
              <span>参考书</span>
              <select
                v-model.number="novelId"
                class="input"
                @change="onBookChange"
              >
                <option v-for="b in books" :key="b.id" :value="b.id">
                  {{ b.title || `语料 #${b.id}` }}
                </option>
              </select>
            </label>
            <label class="field">
              <span>抽取方式</span>
              <select v-model="mode" class="input" :disabled="starting">
                <option v-for="(label, key) in MODE_LABELS" :key="key" :value="key">
                  {{ label }}
                </option>
              </select>
            </label>
            <label class="field narrow">
              <span>压缩比目标</span>
              <input v-model.number="targetRatio" class="input" type="number" min="1" max="100" :disabled="starting" />
            </label>
            <label class="field check">
              <input v-model="force" type="checkbox" :disabled="starting" />
              <span>忽略已有摘要重算</span>
            </label>
          </div>

          <div class="actions">
            <button class="btn primary" type="button" :disabled="starting || novelId == null" @click="start">
              {{ starting ? "生成中…" : digest ? "重新生成摘要" : "生成摘要" }}
            </button>
            <button class="btn" type="button" :disabled="digestLoading" @click="loadDigest">
              刷新结果
            </button>
            <span v-if="currentBook" class="faint">
              {{ currentBook.title || `语料 #${currentBook.id}` }}
            </span>
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
          <h2>验收结果</h2>
          <div v-if="digestLoading" class="faint">读取中…</div>
          <div v-else-if="digestError" class="notice err">{{ digestError }}</div>
          <div v-else-if="!digest" class="empty">
            这本书还没有摘要。选好方式后点上方「生成摘要」。
          </div>
          <template v-else>
            <div class="summary-line">
              <span class="tag" :class="passed === false ? 'warn' : passed ? 'ok' : ''">
                {{ passed === false ? "有项未通过" : passed ? "全部通过" : "结论未知" }}
              </span>
              <span class="faint">
                {{ MODE_LABELS[digest.mode] || digest.mode }}
                <template v-if="digest.mode_reason"> · {{ digest.mode_reason }}</template>
              </span>
              <span class="faint">生成于 {{ fmtDate(digest.created_at) }}</span>
            </div>

            <ul class="checks">
              <li v-for="[key, c] in checks" :key="key" class="check-row">
                <span class="tag" :class="checkTone(c)">{{ checkText(c) }}</span>
                <b>{{ CHECK_LABELS[key] || key }}</b>
                <span class="faint">{{ checkDetail(key, c) }}</span>
              </li>
            </ul>

            <p v-if="digest.not_applicable?.length" class="faint">
              不适用（没跑到，不算通过）：{{ digest.not_applicable.map((k) => CHECK_LABELS[k] || k).join("、") }}
            </p>

            <details v-if="digest.evidence_detail" class="evidence">
              <summary>证据丢弃明细</summary>
              <p class="faint">
                保留 {{ digest.evidence_detail.kept }} · 丢弃 {{ digest.evidence_detail.dropped }} ·
                重新归属 {{ digest.evidence_detail.reattributed }} · 丢弃率
                {{ pct(digest.evidence_detail.dropped_ratio) }}（阈值
                {{ pct(digest.evidence_detail.threshold) }}）
              </p>
            </details>

            <h2 class="text-title">摘要正文</h2>
            <pre class="digest-text">{{ digest.text || "（后端没有渲染文本）" }}</pre>
          </template>
        </div>

        <div v-if="history.length || historyError" class="card">
          <div class="list-head">
            <h2>历史版本</h2>
            <span class="faint">{{ history.length }} 条</span>
          </div>
          <div v-if="historyError" class="notice err">{{ historyError }}</div>
          <p v-else-if="history.length < 2" class="faint">
            只有当前版本。勾选「忽略已有摘要重算」再生成一次，旧版本会归档保留，供这里对比。
          </p>
          <template v-else>
            <div class="compare-bar">
              <select v-model.number="compareA" class="input" aria-label="对比版本 A">
                <option v-for="h in history" :key="`a-${h.id}`" :value="h.id">
                  {{ versionLabel(h) }}
                </option>
              </select>
              <span class="faint">对比</span>
              <select v-model.number="compareB" class="input" aria-label="对比版本 B">
                <option v-for="h in history" :key="`b-${h.id}`" :value="h.id">
                  {{ versionLabel(h) }}
                </option>
              </select>
              <button class="btn" type="button" :disabled="comparing" @click="compareVersions">
                {{ comparing ? "读取中…" : "对比" }}
              </button>
            </div>
            <div v-if="compareError" class="notice err">{{ compareError }}</div>
            <div v-if="compareLeft && compareRight" class="compare-grid">
              <article v-for="side in compareSides" :key="side.key" class="compare-col">
                <template v-if="side.data">
                  <div class="compare-head">
                    <span class="tag">{{ side.key }}</span>
                    <span class="faint mono">#{{ side.data.id }}</span>
                    <span class="tag" :class="side.data.verify_passed ? 'ok' : 'warn'">
                      {{ side.data.verify_passed ? "验收通过" : "有项未通过" }}
                    </span>
                  </div>
                  <p class="faint compare-meta">
                    {{ fmtDate(side.data.created_at) }} ·
                    {{ side.data.status === "archived" ? "旧版" : "当前" }} ·
                    输出 {{ statNum(side.data, "chars_out") }} 字 ·
                    保留 {{ statNum(side.data, "kept") }} ·
                    丢弃 {{ statNum(side.data, "dropped") }}
                  </p>
                  <pre class="digest-text compare-text">{{ side.data.text || "（空）" }}</pre>
                </template>
              </article>
            </div>
          </template>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.digest-page {
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
  min-width: 200px;
  font-size: var(--fs-xs);
}
.field.narrow { min-width: 120px; }
.field.check {
  flex-direction: row;
  align-items: center;
  min-width: auto;
  padding-bottom: 6px;
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
.empty {
  padding: 14px 0;
  color: var(--fg-faint);
  font-size: var(--fs-sm);
}
.summary-line {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.checks {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.check-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: var(--fs-sm);
  flex-wrap: wrap;
}
.check-row .tag { flex: 0 0 auto; }
.evidence summary {
  cursor: pointer;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.text-title { margin: var(--gap) 0 6px; }
.digest-text {
  margin: 0;
  max-height: 520px;
  overflow: auto;
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-soft, var(--accent-soft));
  white-space: pre-wrap;
  font-family: var(--font-sans);
  font-size: var(--fs-sm);
  line-height: 1.8;
}
.tag.ok { color: var(--ok); border-color: var(--ok); }
.tag.warn { color: var(--warn); border-color: var(--warn); }
.tag.muted { color: var(--fg-faint); }

.list-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.list-head h2 { margin: 0; }
.compare-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.compare-bar .input { min-width: 220px; }
.compare-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap);
}
.compare-col {
  min-width: 0;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
}
.compare-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.compare-meta {
  margin: 6px 0;
  font-size: var(--fs-2xs);
  line-height: 1.7;
}
.compare-text { max-height: 320px; }
@media (max-width: 900px) {
  .compare-grid { grid-template-columns: 1fr; }
}
</style>
