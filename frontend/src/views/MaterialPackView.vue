<script setup lang="ts">
/**
 * 每本书专属素材包：目录、筛选、手动增删改、后台调研进度。
 *
 * 数据来源：`/api/projects/{id}/materials*`。调研线程跑在后端，本页只轮询任务
 * 状态，不阻塞其它页面。
 */
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import ProjectNav from "@/components/ProjectNav.vue";
import type {
  MaterialResearchJob,
  MaterialToc,
  ProjectMaterial,
} from "@/api/types";

defineOptions({ name: "MaterialPackView" });

const route = useRoute();
const projectId = Number(route.params.id);

const toc = ref<MaterialToc | null>(null);
const items = ref<ProjectMaterial[]>([]);
const total = ref(0);
const page = ref(1);
const pageSize = 20;
const loading = ref(false);
const error = ref("");
const jobs = ref<MaterialResearchJob[]>([]);

const q = ref("");
const status = ref("");
const category = ref("");
const yearFrom = ref<string>("");
const yearUnknown = ref(false);
const notice = ref("");
const showDirectory = ref(false);

const editingId = ref<number | null>(null);
const form = ref({
  title: "",
  summary: "",
  use_case: "",
  category: "",
  published_year: "" as string,
  url: "",
  snippet: "",
});

const activeJob = computed(() =>
  jobs.value.find((j) =>
    ["queued", "running", "consolidating"].includes(j.status),
  ),
);

/** 后台调研推断出的题材内核，展示出来便于确认没有跑偏到载体上。 */
const activeTheme = computed(() => {
  const meta = activeJob.value?.meta as Record<string, unknown> | undefined;
  return meta && meta.theme ? String(meta.theme) : "";
});

const stats = computed(() => toc.value?.stats ?? {
  total: 0,
  usable: 0,
  raw: 0,
  hidden: 0,
  unknown_year: 0,
  used: 0,
});

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)));

/** 目录里 key 为空的年份分组 = 年份未知的可用素材数。 */
const unknownYearCount = computed(
  () => toc.value?.years.find((y) => !y.key)?.count ?? 0,
);

const directorySections = computed(() => toc.value?.directory ?? []);

let timer: number | null = null;

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    const [tocData, listData, jobData] = await Promise.all([
      api.materialToc(projectId),
      api.listMaterials(projectId, {
        q: q.value,
        status: status.value || undefined,
        category: category.value || undefined,
        year_from:
          !yearUnknown.value && yearFrom.value ? Number(yearFrom.value) : null,
        unknown_year: yearUnknown.value || undefined,
        page: page.value,
        page_size: pageSize,
      }),
      api.listMaterialJobs(projectId, 5),
    ]);
    toc.value = tocData;
    items.value = listData.items;
    total.value = listData.total;
    jobs.value = jobData.items;
    // 删除当前页最后一条或换筛选后，页码可能越界：自动夹到最后一页再取一次，
    // 避免「明明还有素材却显示空」。
    const pages = Math.max(1, Math.ceil(listData.total / pageSize));
    if (page.value > pages) {
      page.value = pages;
      return refresh();
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : "素材包加载失败";
  } finally {
    loading.value = false;
  }
}

function resetForm() {
  editingId.value = null;
  form.value = {
    title: "",
    summary: "",
    use_case: "",
    category: "",
    published_year: "",
    url: "",
    snippet: "",
  };
}

function startEdit(row: ProjectMaterial) {
  editingId.value = row.id;
  form.value = {
    title: row.title,
    summary: row.summary,
    use_case: row.use_case,
    category: row.category,
    published_year: row.published_year ? String(row.published_year) : "",
    url: row.url,
    snippet: row.snippet,
  };
}

async function submitForm() {
  if (!form.value.title.trim()) {
    error.value = "标题不能为空";
    return;
  }
  const payload = {
    title: form.value.title.trim(),
    summary: form.value.summary,
    use_case: form.value.use_case,
    category: form.value.category,
    published_year: form.value.published_year
      ? Number(form.value.published_year)
      : null,
    url: form.value.url,
    snippet: form.value.snippet,
  };
  try {
    if (editingId.value) {
      await api.patchMaterial(projectId, editingId.value, payload);
    } else {
      await api.createMaterial(projectId, payload);
    }
    resetForm();
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "保存失败";
  }
}

async function remove(row: ProjectMaterial) {
  if (!window.confirm(`确定删除素材「${row.title}」？`)) return;
  try {
    await api.deleteMaterial(projectId, row.id);
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "删除失败";
  }
}

async function startResearch() {
  error.value = "";
  try {
    await api.startMaterialResearch(projectId, {});
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "启动调研失败";
  }
}

async function cancelResearch() {
  if (!activeJob.value) return;
  try {
    await api.cancelMaterialJob(projectId, activeJob.value.id);
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "取消失败";
  }
}

function applyFilters() {
  page.value = 1;
  void refresh();
}

function applyCategory(key: string) {
  category.value = category.value === key ? "" : key;
  page.value = 1;
  void refresh();
}

/** 大目录里点条目标题：直接按标题搜索并收起目录。 */
function findTitle(title: string) {
  q.value = title;
  page.value = 1;
  showDirectory.value = false;
  void refresh();
}

function applyYear(key: string) {
  if (!key) {
    // 目录里「未知」chip：切换只看年份未知的素材，并清掉具体年份条件。
    yearUnknown.value = !yearUnknown.value;
    yearFrom.value = "";
  } else {
    yearUnknown.value = false;
    yearFrom.value = yearFrom.value === key ? "" : key;
  }
  page.value = 1;
  void refresh();
}

/** 重算存量素材年份：用于旧数据按宽松规则算错/算漏的情况。 */
async function reparseYears() {
  notice.value = "";
  error.value = "";
  try {
    const result = await api.reparseMaterialYears(projectId);
    notice.value = `已重算 ${result.scanned} 条：更新 ${result.updated} 条，清空无证据年份 ${result.cleared} 条`;
    await refresh();
  } catch (err) {
    error.value = err instanceof Error ? err.message : "重算年份失败";
  }
}

function changePage(next: number) {
  page.value = Math.min(Math.max(1, next), totalPages.value);
  void refresh();
}

function yearLabel(year: number | null) {
  return year ? `${year} 年` : "未知";
}

function jobLabel(job: MaterialResearchJob) {
  const map: Record<string, string> = {
    queued: "排队中",
    running: "检索中",
    consolidating: "整理中",
    done: "已完成",
    partial: "部分完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return map[job.status] ?? job.status;
}

onMounted(async () => {
  await refresh();
  timer = window.setInterval(() => {
    if (activeJob.value) void refresh();
  }, 5000);
});

onUnmounted(() => {
  if (timer !== null) window.clearInterval(timer);
});
</script>

<template>
  <ProjectNav :project-id="projectId" />

  <header class="page-head">
    <div class="eyebrow">02 · 项目</div>
    <h1>素材包</h1>
    <p>
      这本书专属的写作素材：后台按章纲调研、标注年份与可用处，生成时按需注入。
      素材越多越依赖目录检索，助手与写作 agent 都可以调用素材包工具。
    </p>
  </header>

  <section class="stat-row">
    <div class="stat"><b>{{ stats.total }}</b><span>全部素材</span></div>
    <div class="stat"><b>{{ stats.usable }}</b><span>可用</span></div>
    <div class="stat"><b>{{ stats.raw }}</b><span>待整理</span></div>
    <div class="stat"><b>{{ stats.unknown_year }}</b><span>年份未知</span></div>
    <div class="stat"><b>{{ stats.used }}</b><span>已用过</span></div>
  </section>

  <section class="card research-card">
    <div class="research-head">
      <div>
        <h2>后台调研</h2>
        <p class="muted">
          生成前会自动排队；也可手动启动。一次任务目标不少于
          {{ activeJob?.target_raw ?? 40 }} 条原始素材，按目标字数调整可用素材数量。
        </p>
      </div>
      <div class="research-actions">
        <button v-if="!activeJob" class="btn" :disabled="loading" @click="startResearch">
          立即调研
        </button>
        <button v-else class="btn ghost" @click="cancelResearch">取消当前任务</button>
      </div>
    </div>

    <div v-if="activeJob" class="job-progress">
      <div class="job-line">
        <span class="pill">{{ jobLabel(activeJob) }}</span>
        <span>原始 {{ activeJob.raw_count }} / {{ activeJob.target_raw }}</span>
        <span>可用 {{ activeJob.usable_count }} / {{ activeJob.target_usable }}</span>
        <span v-if="activeJob.chapter_no">触发章：第 {{ activeJob.chapter_no }} 章</span>
      </div>
      <div class="bar">
        <div
          class="bar-fill"
          :style="{
            width: Math.min(
              100,
              Math.round((activeJob.raw_count / Math.max(1, activeJob.target_raw)) * 100),
            ) + '%',
          }"
        />
      </div>
      <p v-if="activeTheme" class="muted">识别主题：{{ activeTheme }}</p>
      <p v-if="activeJob.error" class="muted">{{ activeJob.error }}</p>
    </div>

    <div v-if="jobs.length" class="job-history">
      <span
        v-for="job in jobs"
        :key="job.id"
        class="job-chip"
        :class="job.status"
        :title="job.error || job.status"
      >
        #{{ job.id }} · {{ jobLabel(job) }} · {{ job.usable_count }} 条可用
      </span>
    </div>
  </section>

  <section v-if="toc" class="card toc-card">
    <div class="toc-head">
      <h2>目录</h2>
      <button class="link" @click="showDirectory = !showDirectory">
        {{ showDirectory ? "收起大目录" : "展开大目录" }}
      </button>
    </div>
    <div class="toc-row">
      <span class="toc-label">分类</span>
      <button
        v-for="item in toc.categories.filter((c) => c.key)"
        :key="'c-' + item.key"
        class="chip"
        :class="{ active: category === item.key }"
        @click="applyCategory(item.key)"
      >
        {{ item.key }} · {{ item.count }}
      </button>
      <span v-if="!toc.categories.length" class="muted">暂无分类</span>
    </div>
    <div class="toc-row">
      <span class="toc-label">年份</span>
      <button
        v-for="item in toc.years.filter((y) => y.key)"
        :key="'y-' + item.key"
        class="chip"
        :class="{ active: yearFrom === item.key }"
        @click="applyYear(item.key)"
      >
        {{ item.key }} 年 · {{ item.count }}
      </button>
      <button
        v-if="unknownYearCount"
        class="chip"
        :class="{ active: yearUnknown }"
        :title="`只看年份未知的可用素材（${unknownYearCount} 条）`"
        @click="applyYear('')"
      >
        未知 · {{ unknownYearCount }}
      </button>
      <span v-if="!toc.years.filter((y) => y.key).length && !unknownYearCount" class="muted">
        暂无年份
      </span>
    </div>
    <div v-if="toc.use_cases.filter((u) => u.key).length" class="toc-row">
      <span class="toc-label">用途</span>
      <span v-for="item in toc.use_cases.filter((u) => u.key).slice(0, 8)" :key="'u-' + item.key" class="chip static">
        {{ item.key }} · {{ item.count }}
      </span>
    </div>

    <div v-if="showDirectory" class="directory-grid">
      <section
        v-for="section in directorySections"
        :key="'dir-' + section.category"
        class="directory-card"
      >
        <button
          class="directory-title"
          :title="`只看 ${section.category}`"
          @click="applyCategory(section.category === '未分类' ? '__none__' : section.category)"
        >
          {{ section.category }}
          <span class="count">{{ section.count }}</span>
        </button>
        <ul>
          <li
            v-for="item in section.items"
            :key="'di-' + item.id"
            :title="item.use_case || ''"
            @click="findTitle(item.title)"
          >
            <span class="year">{{ item.published_year ? item.published_year + " 年" : "未知" }}</span>
            {{ item.title }}
          </li>
        </ul>
        <p v-if="section.count > section.items.length" class="muted">
          仅显示前 {{ section.items.length }} 条，点分类查看全部
        </p>
      </section>
      <p v-if="!directorySections.length" class="muted">暂无可用素材，先跑一次调研。</p>
    </div>
  </section>

  <section class="card">
    <div class="toolbar">
      <input v-model="q" class="input" placeholder="搜索标题 / 摘要 / 用途" @keyup.enter="applyFilters" />
      <select v-model="status" class="input" @change="applyFilters">
        <option value="">全部状态</option>
        <option value="usable">可用</option>
        <option value="raw">待整理</option>
        <option value="hidden">已隐藏</option>
      </select>
      <button class="btn ghost" :disabled="loading" @click="applyFilters">查询</button>
      <button
        class="btn ghost"
        :disabled="loading"
        title="按新的年份规则重算已有素材（跳过手动添加的素材）"
        @click="reparseYears"
      >
        重新识别年份
      </button>
    </div>

    <p v-if="notice" class="notice">{{ notice }}</p>
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="loading" class="muted">加载中…</div>
    <div v-else-if="!items.length" class="muted empty">
      还没有素材。生成一章会自动触发调研，或点「立即调研」。
    </div>
    <div v-else class="table-wrap">
      <table class="material-table">
        <thead>
          <tr>
            <th>素材</th>
            <th>年份</th>
            <th>分类</th>
            <th>可用在</th>
            <th>使用</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in items" :key="row.id">
            <td>
              <div class="title">
                {{ row.title }}
                <span v-if="row.origin === 'manual'" class="origin">手动</span>
              </div>
              <div class="snippet">{{ row.summary || row.snippet || "（暂无摘要）" }}</div>
            </td>
            <td class="nowrap">
              <span
                :class="{ 'year-unknown': !row.published_year }"
                :title="row.published_at || ''"
              >
                {{ yearLabel(row.published_year) }}
              </span>
            </td>
            <td class="nowrap">{{ row.category || "未分类" }}</td>
            <td>{{ row.use_case || "—" }}</td>
            <td class="nowrap">{{ row.use_count }} 次</td>
            <td class="nowrap actions">
              <button class="link" @click="startEdit(row)">编辑</button>
              <button class="link danger" @click="remove(row)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="totalPages > 1" class="pager">
      <button class="btn ghost" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button>
      <span class="muted">{{ page }} / {{ totalPages }}</span>
      <button class="btn ghost" :disabled="page >= totalPages" @click="changePage(page + 1)">下一页</button>
    </div>
  </section>

  <section class="card form-card">
    <h2>{{ editingId ? "编辑素材" : "手动添加素材" }}</h2>
    <div class="form-grid">
      <label>
        标题
        <input v-model="form.title" class="input" placeholder="例如：旧城改造征收流程" />
      </label>
      <label>
        年份
        <input
          v-model="form.published_year"
          class="input"
          inputmode="numeric"
          placeholder="如 2024，留空 = 未知"
        />
      </label>
      <label>
        分类
        <input v-model="form.category" class="input" placeholder="例如：制度流程" />
      </label>
      <label>
        可用在
        <input v-model="form.use_case" class="input" placeholder="例如：第 12 章 拆迁谈判" />
      </label>
      <label class="wide">
        摘要
        <textarea v-model="form.summary" class="input" rows="2" placeholder="可直接查用的事实摘要" />
      </label>
      <label class="wide">
        原文/摘录
        <textarea v-model="form.snippet" class="input" rows="2" placeholder="可选：原始资料摘录" />
      </label>
      <label class="wide">
        链接
        <input v-model="form.url" class="input" placeholder="可选：来源 URL" />
      </label>
    </div>
    <div class="form-actions">
      <button class="btn" @click="submitForm">{{ editingId ? "保存修改" : "添加素材" }}</button>
      <button v-if="editingId" class="btn ghost" @click="resetForm">取消编辑</button>
    </div>
  </section>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: var(--gap);
}
.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
}
.stat b {
  font-size: var(--fs-xl);
  font-family: var(--font-display);
}
.stat span {
  color: var(--fg-muted);
  font-size: var(--fs-xs);
}
.research-card h2,
.toc-card h2,
.form-card h2 {
  font-size: var(--fs-lg);
  margin-bottom: 6px;
}
.research-head {
  display: flex;
  justify-content: space-between;
  gap: var(--gap);
  align-items: flex-start;
}
.research-actions {
  display: flex;
  gap: 8px;
  flex: none;
}
.job-progress {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.job-line {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: var(--fs-sm);
  color: var(--fg-muted);
}
.pill {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent-ink);
  font-weight: 600;
}
.bar {
  height: 6px;
  border-radius: 999px;
  background: var(--bg-sunken);
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}
.job-history {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.job-chip {
  font-size: var(--fs-xs);
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--bg-sunken);
  color: var(--fg-muted);
}
.job-chip.done {
  background: var(--accent-soft);
  color: var(--accent-ink);
}
.job-chip.failed,
.job-chip.partial {
  background: #fdecec;
  color: #a33;
}
.toc-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-top: 8px;
}
.toc-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.directory-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}
.directory-card {
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
  padding: 10px 12px;
}
.directory-title {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  border: none;
  background: none;
  color: var(--fg);
  font-weight: 600;
  font-size: var(--fs-sm);
  cursor: pointer;
  padding: 0 0 6px;
}
.directory-title .count {
  color: var(--fg-muted);
  font-weight: 500;
}
.directory-card ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.directory-card li {
  font-size: var(--fs-xs);
  line-height: 1.55;
  color: var(--fg-muted);
  cursor: pointer;
}
.directory-card li:hover {
  color: var(--accent-ink);
}
.directory-card li .year {
  margin-right: 4px;
  font-variant-numeric: tabular-nums;
}
.toc-label {
  font-size: var(--fs-xs);
  color: var(--fg-muted);
  min-width: 32px;
}
.chip {
  border: var(--border-w) solid var(--border);
  background: var(--bg);
  color: var(--fg-muted);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: var(--fs-xs);
  cursor: pointer;
}
.chip.static {
  cursor: default;
}
.chip.active {
  background: var(--accent-soft);
  border-color: var(--accent-line);
  color: var(--accent-ink);
}
.toolbar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.input {
  padding: 7px 10px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font-size: var(--fs-sm);
}
.toolbar .input:first-child {
  flex: 1;
  min-width: 180px;
}
.table-wrap {
  overflow-x: auto;
}
.material-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
.material-table th,
.material-table td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: var(--border-w) solid var(--border);
  vertical-align: top;
}
.material-table th {
  color: var(--fg-muted);
  font-weight: 600;
  white-space: nowrap;
}
.title {
  font-weight: 600;
}
.origin {
  margin-left: 6px;
  font-size: var(--fs-xs);
  color: var(--accent-ink);
}
.snippet {
  margin-top: 2px;
  color: var(--fg-muted);
  font-size: var(--fs-xs);
  line-height: 1.6;
  max-width: 42ch;
}
.nowrap {
  white-space: nowrap;
}
.actions {
  display: flex;
  gap: 8px;
}
.link {
  border: none;
  background: none;
  color: var(--accent-ink);
  cursor: pointer;
  padding: 0;
  font-size: var(--fs-sm);
}
.link.danger {
  color: #b44343;
}
.pager {
  display: flex;
  justify-content: center;
  gap: 10px;
  align-items: center;
  margin-top: 10px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: var(--fs-xs);
  color: var(--fg-muted);
}
.form-grid .wide {
  grid-column: 1 / -1;
}
.form-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
}
.error {
  color: #b44343;
  font-size: var(--fs-sm);
}
.notice {
  color: var(--accent-ink);
  font-size: var(--fs-sm);
}
.year-unknown {
  color: var(--fg-muted);
}
.empty {
  padding: 14px 0;
}
@media (max-width: 880px) {
  .stat-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
  .directory-grid {
    grid-template-columns: 1fr;
  }
}
</style>
