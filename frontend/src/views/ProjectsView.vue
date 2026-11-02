<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";
import type { ProjectOut } from "@/api/types";

const router = useRouter();
const projects = ref<ProjectOut[]>([]);
const loading = ref(true);
const loadError = ref<string | null>(null);
const formError = ref<string | null>(null);
const submitting = ref(false);
const query = ref("");
const statusFilter = ref<"all" | "active" | "completed">("all");
const titleInput = ref<HTMLInputElement | null>(null);
const form = reactive({ title: "" });

const activeCount = computed(() => projects.value.filter((p) => p.status !== "completed").length);
const segmentCount = computed(() => projects.value.reduce((sum, p) => sum + (p.segment_count || 0), 0));
const filteredProjects = computed(() => {
  const keyword = query.value.trim().toLocaleLowerCase();
  return projects.value.filter((p) => {
    const matchesStatus = statusFilter.value === "all"
      || (statusFilter.value === "completed" ? p.status === "completed" : p.status !== "completed");
    if (!matchesStatus) return false;
    if (!keyword) return true;
    return [p.title, p.genre, p.style].some((value) => value?.toLocaleLowerCase().includes(keyword));
  });
});

function errorText(err: unknown) {
  if (err instanceof TypeError && /fetch/i.test(err.message)) {
    return "无法连接服务，请检查网络连接或后端启动状态。";
  }
  return err instanceof Error ? err.message : String(err);
}

async function refresh() {
  loading.value = true;
  loadError.value = null;
  try {
    projects.value = await api.listProjects();
  } catch (err) {
    loadError.value = `项目列表加载失败：${errorText(err)}`;
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);

async function create() {
  const title = form.title.trim();
  if (!title) {
    formError.value = "请输入项目名称。";
    titleInput.value?.focus();
    return;
  }
  submitting.value = true;
  formError.value = null;
  try {
    const p = await api.createProject({ title, mode: "quick", target_length: 5000 });
    form.title = "";
    router.push(`/projects/${p.id}`);
  } catch (err) {
    formError.value = `创建失败：${errorText(err)}`;
  } finally {
    submitting.value = false;
  }
}

function focusCreate() {
  titleInput.value?.focus();
}

function resetFilters() {
  query.value = "";
  statusFilter.value = "all";
}

function formatDate(value: string | null) {
  if (!value) return "创建时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "创建时间未知";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function formatIndex(index: number) {
  return String(index + 1).padStart(2, "0");
}
</script>

<template>
  <header class="page-head project-head">
    <div>
      <div class="eyebrow">Manuscript library</div>
      <h1>书稿工作台</h1>
      <p>每一部小说都是一份持续生长的书稿。回到目录，接着上次的段落继续。</p>
    </div>
    <button class="btn primary head-action" @click="focusCreate">新建书稿</button>
  </header>

  <div class="projects-layout">
    <section class="project-list-card" aria-labelledby="project-list-title">
      <div class="section-heading">
        <div>
          <span class="section-kicker">书稿目录</span>
          <h2 id="project-list-title">全部创作</h2>
        </div>
        <span class="section-count">{{ filteredProjects.length }} / {{ projects.length }} 部</span>
      </div>

      <div v-if="projects.length" class="list-tools">
        <div class="field search-field">
          <label for="project-search">检索目录</label>
          <input id="project-search" v-model="query" class="input" type="search" placeholder="搜索书名、类型或风格" />
        </div>
        <div class="field status-field">
          <label for="project-status">创作状态</label>
          <select id="project-status" v-model="statusFilter" class="input">
            <option value="all">全部状态</option>
            <option value="active">创作中</option>
            <option value="completed">已完成</option>
          </select>
        </div>
      </div>

      <div v-if="loading" class="project-ledger" aria-label="正在加载项目" aria-busy="true">
        <div v-for="i in 4" :key="i" class="project-skeleton">
          <span class="skeleton-line short" />
          <span class="skeleton-line" />
          <span class="skeleton-line medium" />
        </div>
      </div>
      <div v-else-if="loadError" class="state-panel ledger-state" role="alert">
        <span class="state-folio">读取中断</span>
        <strong class="state-title">暂时无法展开书稿目录</strong>
        <span class="state-description">{{ loadError }}</span>
        <div class="state-actions"><button class="btn" @click="refresh">重新加载</button></div>
      </div>
      <div v-else-if="!projects.length" class="state-panel ledger-state">
        <span class="state-folio">第一页</span>
        <strong class="state-title">书稿目录还是空白</strong>
        <span class="state-description">从右侧写下第一部作品的名称，再逐步完善设定与正文。</span>
        <div class="state-actions"><button class="btn primary" @click="focusCreate">新建第一部书稿</button></div>
      </div>
      <div v-else-if="!filteredProjects.length" class="state-panel ledger-state">
        <span class="state-folio">检索完毕</span>
        <strong class="state-title">目录中没有匹配的书稿</strong>
        <span class="state-description">当前检索词或状态筛选没有结果，可清除条件后查看完整目录。</span>
        <div class="state-actions"><button class="btn" @click="resetFilters">清除筛选</button></div>
      </div>
      <div v-else class="project-ledger">
        <article v-for="(p, index) in filteredProjects" :key="p.id" class="project-card" :class="{ featured: index === 0 }">
          <router-link class="project-card-link" :to="`/projects/${p.id}`" :aria-label="`打开书稿 ${p.title}`">
            <span class="project-number">{{ formatIndex(index) }}</span>
            <div class="project-main">
              <div class="project-card-top">
                <span v-if="index === 0" class="lead-label">最近书稿</span>
                <span class="tag" :class="p.status === 'completed' ? 'ok' : 'accent'">{{ p.status === 'completed' ? "已完成" : "创作中" }}</span>
                <time class="project-date" :datetime="p.created_at || undefined">{{ formatDate(p.created_at) }}</time>
              </div>
              <h3 :title="p.title">{{ p.title }}</h3>
              <p class="project-meta"><span>{{ p.genre || "未设置类型" }}</span><span>{{ p.style || "未设置风格" }}</span></p>
            </div>
            <div class="project-progress"><strong>{{ p.segment_count || 0 }}</strong><span>生成段落</span></div>
          </router-link>
          <div class="project-card-foot">
            <span class="project-mode">{{ p.mode === "expert" ? "专家构思" : "快速构思" }}</span>
            <router-link class="assistant-link" :to="`/projects/${p.id}/assistant`">进入助手</router-link>
          </div>
        </article>
      </div>
    </section>

    <aside class="project-rail">
      <section class="create-panel" aria-labelledby="create-title">
        <span class="rail-kicker">起笔</span>
        <h2 id="create-title">开一部新书</h2>
        <p>先写下书名。类型、风格与世界观，留到工作台里慢慢展开。</p>
        <form class="create-form" @submit.prevent="create">
          <div class="field">
            <label for="new-title">书稿名称</label>
            <input
              id="new-title"
              ref="titleInput"
              v-model="form.title"
              class="input"
              maxlength="120"
              placeholder="输入书稿名称"
              :disabled="submitting"
              @input="formError = null"
            />
          </div>
          <button class="btn primary" type="submit" :disabled="submitting">
            <span v-if="submitting" class="spinner" aria-hidden="true" />
            {{ submitting ? "正在创建" : "创建并进入工作台" }}
          </button>
        </form>
        <div v-if="formError" class="notice err" role="alert">{{ formError }}</div>
      </section>

      <section class="project-summary" aria-label="项目概览">
        <span class="rail-kicker">本工作区</span>
        <div><span>全部书稿</span><strong>{{ loading && !projects.length ? "—" : projects.length }}</strong></div>
        <div><span>正在创作</span><strong>{{ loading && !projects.length ? "—" : activeCount }}</strong></div>
        <div><span>已生成段落</span><strong>{{ loading && !projects.length ? "—" : segmentCount }}</strong></div>
      </section>
    </aside>
  </div>
</template>

<style scoped>
.project-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding-bottom: 26px; border-bottom: 1px solid var(--border-strong); }
.project-head h1 { font-size: 44px; }
.project-head p { max-width: 58ch; font-size: 15px; }
.head-action { flex: none; margin-bottom: 4px; }
.projects-layout { display: grid; grid-template-columns: minmax(0, 1fr) 292px; gap: 40px; align-items: start; }
.project-list-card { min-width: 0; border-top: 2px solid var(--fg); }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; padding: 16px 0 14px; border-bottom: 1px solid var(--border); }
.section-kicker, .rail-kicker, .state-folio { display: block; color: var(--accent-ink); font-size: 11px; font-weight: 700; letter-spacing: .14em; }
.section-heading h2 { margin-top: 3px; font-family: var(--font-display); font-size: 22px; font-weight: 600; }
.section-count { color: var(--fg-muted); font-family: var(--font-mono); font-size: 11px; white-space: nowrap; }
.list-tools { display: flex; align-items: flex-end; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--border); }
.search-field { flex: 1; }
.status-field { width: 148px; flex: none; }
.project-ledger { min-width: 0; }
.project-card { min-width: 0; border-bottom: 1px solid var(--border); transition: background 180ms var(--ease-out-quart); }
.project-card:not(.featured):hover { background: var(--bg-elev); }
.project-card-link { min-width: 0; display: grid; grid-template-columns: 54px minmax(0, 1fr) 70px; gap: 16px; align-items: center; padding: 20px 0 12px; }
.project-card-link:focus-visible { outline-offset: 3px; }
.project-number { align-self: start; padding-top: 3px; color: var(--fg-faint); font-family: Georgia, serif; font-size: 25px; line-height: 1; font-variant-numeric: tabular-nums; }
.project-main { min-width: 0; }
.project-card-top { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.project-date { margin-left: auto; color: var(--fg-faint); font-size: 11px; white-space: nowrap; }
.project-card h3 { margin-top: 10px; color: var(--fg); font-family: var(--font-display); font-size: 21px; font-weight: 600; line-height: 1.4; overflow-wrap: anywhere; }
.project-meta { display: flex; flex-wrap: wrap; gap: 5px 16px; margin: 6px 0 0; color: var(--fg-muted); font-size: var(--fs-xs); }
.project-meta span + span { position: relative; }
.project-meta span + span::before { content: ""; position: absolute; left: -9px; top: .7em; width: 3px; height: 1px; background: var(--fg-faint); }
.project-progress { display: flex; flex-direction: column; align-items: flex-end; color: var(--fg-muted); }
.project-progress strong { color: var(--fg); font-family: Georgia, serif; font-size: 28px; font-weight: 400; line-height: 1; font-variant-numeric: tabular-nums; }
.project-progress span { margin-top: 6px; font-size: 10px; white-space: nowrap; }
.project-card-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 0 14px 70px; font-size: var(--fs-xs); }
.project-mode { color: var(--fg-faint); }
.assistant-link { color: var(--accent-ink); font-weight: 650; }
.assistant-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.project-card.featured { margin: 18px 0; border: 0; border-left: 3px solid var(--accent); background: var(--ink); color: var(--paper); }
.project-card.featured .project-card-link { grid-template-columns: 58px minmax(0, 1fr) 76px; padding: 26px 24px 14px 20px; }
.featured .project-number { color: var(--paper-muted); font-size: 34px; }
.project-card.featured h3 { color: var(--paper); font-size: 31px; line-height: 1.3; }
.featured .project-meta, .featured .project-date, .featured .project-mode { color: var(--paper-muted); }
.featured .project-progress strong { color: var(--paper); font-size: 36px; }
.featured .project-progress span { color: var(--paper-muted); }
.featured .project-card-foot { padding: 0 24px 20px 98px; }
.featured .assistant-link { color: var(--paper); }
.featured .tag { border-color: var(--ink-line); background: transparent; color: var(--paper); }
.lead-label { color: var(--paper-muted); font-size: 10px; font-weight: 700; letter-spacing: .14em; }
.project-rail { display: flex; flex-direction: column; gap: 28px; }
.create-panel { padding: 20px 0 24px; border-top: 2px solid var(--accent); border-bottom: 1px solid var(--border-strong); }
.create-panel h2 { margin-top: 6px; font-family: var(--font-display); font-size: 27px; font-weight: 600; }
.create-panel > p { margin: 8px 0 0; color: var(--fg-muted); font-size: var(--fs-sm); line-height: 1.75; }
.create-form { display: flex; flex-direction: column; gap: 12px; margin-top: 20px; }
.create-form .btn { width: 100%; }
.create-panel .notice { margin-top: 12px; }
.project-summary { display: flex; flex-direction: column; }
.project-summary > div { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; padding: 13px 0; border-bottom: 1px solid var(--border); }
.project-summary > div span { color: var(--fg-muted); font-size: var(--fs-sm); }
.project-summary strong { font-family: Georgia, serif; font-size: 28px; font-weight: 400; line-height: 1; font-variant-numeric: tabular-nums; }
.ledger-state { margin: 20px 0; border-width: 1px 0; border-radius: 0; background: transparent; }
.ledger-state .state-title { margin-top: 4px; font-family: var(--font-display); font-size: 25px; }
.project-skeleton { display: grid; grid-template-columns: 54px minmax(0, 1fr) 70px; gap: 16px; align-items: center; min-height: 112px; border-bottom: 1px solid var(--border); }
.project-skeleton .short { width: 32px; height: 26px; }
.project-skeleton .medium { width: 48px; justify-self: end; }
@media (max-width: 1120px) {
  .projects-layout { grid-template-columns: minmax(0, 1fr) 260px; gap: 28px; }
  .project-card.featured h3 { font-size: 26px; }
}
@media (max-width: 940px) {
  .projects-layout { grid-template-columns: 1fr; }
  .project-rail { display: grid; grid-template-columns: 1fr 1fr; gap: 28px; }
}
@media (max-width: 720px) {
  .project-head { align-items: flex-start; flex-direction: column; }
  .project-head h1 { font-size: 36px; }
  .list-tools { align-items: stretch; flex-direction: column; }
  .status-field { width: 100%; }
  .project-rail { grid-template-columns: 1fr; }
  .project-card-link, .project-card.featured .project-card-link { grid-template-columns: 42px minmax(0, 1fr); padding-right: 0; }
  .project-progress { display: none; }
  .project-card-foot, .featured .project-card-foot { padding-left: 58px; }
  .project-card.featured .project-card-link { padding-right: 18px; }
}
</style>
