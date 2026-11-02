<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";
import type { ProjectOut } from "@/api/types";

const router = useRouter();
const projects = ref<ProjectOut[]>([]);
const loading = ref(false);
const activeCount = computed(() => projects.value.filter((p) => p.status !== "completed").length);
const segmentTotal = computed(() => projects.value.reduce((sum, p) => sum + (p.segment_count || 0), 0));

const form = reactive({
  title: "",
});
const submitting = ref(false);
const error = ref<string | null>(null);

async function refresh() {
  loading.value = true;
  error.value = null;
  try {
    projects.value = await api.listProjects();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);

async function create() {
  if (!form.title.trim()) {
    error.value = "项目标题必填";
    return;
  }
  submitting.value = true;
  error.value = null;
  try {
    // 构思方式（quick/expert）不在这里定：它只在工作台第 ⑤ 步「构思剧情」才真正起作用，
    // 创建时先给默认 quick，由那一步的切换器统一决定（并即时 PATCH 落库）。
    const p = await api.createProject({
      title: form.title.trim(),
      mode: "quick",
      target_length: 5000,
    });
    form.title = "";
    router.push(`/projects/${p.id}`);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    submitting.value = false;
  }
}

function openProject(id: number) {
  router.push(`/projects/${id}`);
}
</script>

<template>
  <div class="projects-page">
    <header class="page-head projects-hero">
      <div>
        <div class="eyebrow">02 · PROJECT LIBRARY</div>
        <h1>从一句构思，到一部长篇。</h1>
        <p>把参考语料、剧情规划、正文与校样收在同一处。选择作品继续创作，或从一个标题开始。</p>
      </div>
      <dl class="project-stats" aria-label="项目统计">
        <div><dt>作品</dt><dd>{{ projects.length }}</dd></div>
        <div><dt>创作中</dt><dd>{{ activeCount }}</dd></div>
        <div><dt>已写段落</dt><dd>{{ segmentTotal }}</dd></div>
      </dl>
    </header>

    <section class="create-panel" aria-labelledby="create-title">
      <div class="create-index" aria-hidden="true">NEW<br />DRAFT</div>
      <div class="create-copy">
        <span class="section-kicker">新建作品</span>
        <h2 id="create-title">先给故事一个名字。</h2>
        <p>类型、风格与参考语料将在工作台中逐步确认。</p>
      </div>
      <form class="create-form" @submit.prevent="create">
        <label for="project-title">作品标题</label>
        <div class="create-input-row">
          <input id="project-title" class="input" v-model="form.title" placeholder="输入作品标题" maxlength="200" />
          <button class="btn primary" type="submit" :disabled="submitting || !form.title.trim()">
            {{ submitting ? "创建中…" : "创建并进入工作台" }}<span v-if="!submitting" aria-hidden="true">→</span>
          </button>
        </div>
      </form>
    </section>

    <div v-if="error" class="notice err error-bar" role="alert">
      <span>{{ error }}</span><button class="btn sm" type="button" @click="refresh">重新加载</button>
    </div>

    <section class="library-panel" aria-labelledby="library-title">
      <div class="section-head">
        <div><span class="section-kicker">作品架</span><h2 id="library-title">继续创作</h2></div>
        <button class="btn ghost sm" type="button" :disabled="loading" @click="refresh">刷新列表</button>
      </div>
      <div v-if="loading" class="empty"><span class="spinner" /> 正在读取作品…</div>
      <div v-else-if="!projects.length && !error" class="library-empty">
        <span class="empty-mark" aria-hidden="true">01</span>
        <div><strong>作品架还是空的</strong><p>在上方输入标题，创建第一部作品。</p></div>
      </div>
      <div v-else class="project-list">
        <article
          v-for="p in projects"
          :key="p.id"
          class="project-row"
          tabindex="0"
          role="button"
          @click="openProject(p.id)"
          @keydown.enter="openProject(p.id)"
        >
          <div class="project-number">{{ String(p.id).padStart(2, "0") }}</div>
          <div class="project-main">
            <div class="project-title-line">
              <h3>{{ p.title }}</h3>
              <span class="status-dot" :class="{ done: p.status === 'completed' }">
                {{ p.status === "completed" ? "已完成" : "创作中" }}
              </span>
            </div>
            <div class="project-meta">
              <span v-if="p.genre">{{ p.genre }}</span><span v-if="p.style">{{ p.style }}</span>
              <span>{{ p.mode === "expert" ? "追问构思" : "直接构思" }}</span>
              <span>{{ p.segment_count || 0 }} 段正文</span>
            </div>
          </div>
          <div class="project-actions">
            <button class="btn sm" type="button" @click.stop="router.push(`/projects/${p.id}/assistant`)">助手</button>
            <button class="btn ghost sm" type="button" @click.stop="router.push(`/projects/${p.id}/kg`)">图谱</button>
            <span class="enter-mark" aria-hidden="true">↗</span>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>

<style scoped>
.projects-hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 32px; padding: 10px 0 4px; }
.projects-hero h1 { font-size: clamp(32px, 4vw, 46px); }
.project-stats { display: flex; gap: 0; flex: none; margin: 0 0 5px; }
.project-stats div { min-width: 82px; padding: 0 18px; border-left: 1px solid var(--border-strong); }
.project-stats dt { color: var(--fg-faint); font-size: 10px; }
.project-stats dd { margin: 3px 0 0; font: 26px/1.2 var(--font-serif); }
.create-panel { display: grid; grid-template-columns: 76px minmax(220px, .8fr) minmax(340px, 1.2fr); gap: 24px; align-items: center; padding: 24px 26px; color: #f7f2e9; background: #2b2c27; border-radius: 10px; }
.create-index { align-self: stretch; padding-right: 18px; color: #8e8f84; border-right: 1px solid #494a42; font: 10px/1.7 var(--font-mono); letter-spacing: .12em; }
.section-kicker { color: var(--accent-ink); font: 10px/1.4 var(--font-mono); letter-spacing: .12em; }
.create-copy .section-kicker { color: #d8836f; }
.create-copy h2 { margin-top: 6px; font: 22px/1.35 var(--font-serif); }
.create-copy p { margin: 6px 0 0; color: #aaa99f; font-size: 12px; }
.create-form label { display: block; margin-bottom: 7px; color: #aaa99f; font-size: 11px; }
.create-input-row { display: flex; gap: 10px; }
.create-input-row .input { flex: 1; min-width: 0; color: #f7f2e9; background: #22231f; border-color: #515248; }
.create-input-row .input::placeholder { color: #85867c; }
.create-input-row .btn { display: inline-flex; align-items: center; gap: 16px; }
.error-bar { margin-top: 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.library-panel { margin-top: 30px; }
.section-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; padding-bottom: 13px; border-bottom: 1px solid var(--border-strong); }
.section-head h2 { margin-top: 4px; font: 22px/1.3 var(--font-serif); }
.project-list { display: flex; flex-direction: column; }
.project-row { display: grid; grid-template-columns: 48px minmax(0, 1fr) auto; gap: 18px; align-items: center; padding: 20px 12px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background .15s ease; }
.project-row:hover { background: color-mix(in srgb, var(--bg-elev) 65%, transparent); }
.project-number { color: var(--fg-faint); font: 12px/1 var(--font-mono); }
.project-title-line { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.project-title-line h3 { font: 19px/1.4 var(--font-serif); }
.status-dot { display: inline-flex; align-items: center; gap: 5px; color: var(--fg-faint); font-size: 10px; }
.status-dot::before { content: ""; width: 5px; height: 5px; background: var(--warn); border-radius: 50%; }
.status-dot.done::before { background: var(--ok); }
.project-meta { display: flex; gap: 8px 18px; flex-wrap: wrap; margin-top: 7px; color: var(--fg-faint); font-size: 11px; }
.project-actions { display: flex; align-items: center; gap: 6px; }
.btn.sm { padding: 5px 11px; font-size: var(--fs-xs); }
.enter-mark { margin-left: 10px; color: var(--accent-ink); font-size: 18px; }
.library-empty { display: flex; align-items: center; justify-content: center; gap: 18px; min-height: 170px; color: var(--fg-muted); }
.empty-mark { color: var(--border-strong); font: 38px/1 var(--font-serif); }
.library-empty p { margin: 5px 0 0; color: var(--fg-faint); font-size: var(--fs-sm); }
@media (max-width: 1080px) {
  .projects-hero { align-items: flex-start; flex-direction: column; }
  .create-panel { grid-template-columns: 50px 1fr; }
  .create-form { grid-column: 2; }
}
@media (max-width: 680px) {
  .project-stats { width: 100%; }
  .project-stats div { flex: 1; min-width: 0; padding: 0 12px; }
  .create-panel { grid-template-columns: 1fr; padding: 22px 18px; }
  .create-index { display: none; }
  .create-form { grid-column: auto; }
  .create-input-row { align-items: stretch; flex-direction: column; }
  .create-input-row .btn { justify-content: space-between; }
  .project-row { grid-template-columns: 30px minmax(0, 1fr); gap: 10px; padding: 17px 4px; }
  .project-actions { grid-column: 2; }
  .enter-mark { display: none; }
}
</style>
