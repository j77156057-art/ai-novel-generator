<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";
import type { ProjectOut } from "@/api/types";

const router = useRouter();
const projects = ref<ProjectOut[]>([]);
const loading = ref(false);
const error = ref<string | null>(null);

const inProgress = computed(() =>
  projects.value.filter((p) => p.status !== "completed"),
);
const finished = computed(() =>
  projects.value.filter((p) => p.status === "completed"),
);

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

function openProject(id: number) {
  router.push(`/projects/${id}`);
}
function goCreate() {
  router.push("/projects");
}
</script>

<template>
  <header class="page-head shelf-head">
    <div>
      <div class="eyebrow">03 · MY NOVELS</div>
      <h1>我的小说</h1>
      <p>正在进行的作品与已经完成的书稿，各自归档，随时继续。</p>
    </div>
    <button class="btn primary" type="button" @click="goCreate">新建一部小说 <span aria-hidden="true">＋</span></button>
  </header>

  <div v-if="error" class="notice err error-bar" role="alert">
    <span>{{ error }}</span><button class="btn sm" type="button" @click="refresh">重试</button>
  </div>
  <div v-if="loading" class="empty"><span class="spinner" /> 正在整理书稿…</div>

  <template v-else>
    <section class="novel-section" aria-labelledby="writing-title">
      <div class="section-head">
        <div><span class="section-index">01</span><h2 id="writing-title">正在创作</h2></div>
        <span class="section-count">{{ inProgress.length }} 部</span>
      </div>
      <div v-if="!inProgress.length" class="shelf-empty">
        <strong>当前没有进行中的作品</strong><p>新建一部小说，或把已归档作品重新开启。</p>
      </div>
      <div v-else class="novel-grid">
        <article v-for="p in inProgress" :key="p.id" class="novel-item" tabindex="0" role="button" @click="openProject(p.id)" @keydown.enter="openProject(p.id)">
          <div class="novel-top"><span class="novel-status">创作中</span><span class="mono">#{{ p.id }}</span></div>
          <h3>{{ p.title }}</h3>
          <p>{{ p.premise || "尚未填写故事梗概，可进入工作台继续构思。" }}</p>
          <div class="novel-meta"><span>{{ p.genre || "未分类型" }}</span><span>{{ p.style || "未选风格" }}</span></div>
          <div class="novel-foot"><span>{{ p.segment_count || 0 }} 段正文</span><strong>继续写作 ↗</strong></div>
        </article>
      </div>
    </section>

    <section class="novel-section finished" aria-labelledby="finished-title">
      <div class="section-head">
        <div><span class="section-index">02</span><h2 id="finished-title">已经完成</h2></div>
        <span class="section-count">{{ finished.length }} 部</span>
      </div>
      <div v-if="!finished.length" class="shelf-empty compact">
        完成的作品会在工作台标记完成后归入这里。
      </div>
      <div v-else class="finished-list">
        <article v-for="p in finished" :key="p.id" class="finished-row" tabindex="0" role="button" @click="openProject(p.id)" @keydown.enter="openProject(p.id)">
          <span class="finished-mark" aria-hidden="true">✓</span>
          <div><h3>{{ p.title }}</h3><p>{{ p.genre || "未分类型" }} · {{ p.style || "未选风格" }} · 共 {{ p.segment_count || 0 }} 段</p></div>
          <span class="view-link">查看书稿 ↗</span>
        </article>
      </div>
    </section>
  </template>
</template>

<style scoped>
.shelf-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; padding-top: 8px; }
.error-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.btn.sm { padding: 5px 10px; font-size: var(--fs-xs); }
.novel-section { margin-top: 34px; }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-bottom: 14px; border-bottom: 1px solid var(--border-strong); }
.section-head > div { display: flex; align-items: center; gap: 12px; }
.section-head h2 { font: 22px/1.3 var(--font-serif); }
.section-index { color: var(--accent-ink); font: 10px/1 var(--font-mono); }
.section-count { color: var(--fg-faint); font-size: var(--fs-xs); }
.novel-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }
.novel-item { min-width: 0; min-height: 238px; display: flex; flex-direction: column; padding: 20px; background: var(--bg-elev); border: 1px solid var(--border); border-radius: 9px; cursor: pointer; transition: border-color .15s ease, transform .15s ease; }
.novel-item:hover { border-color: var(--border-strong); transform: translateY(-2px); }
.novel-top { display: flex; align-items: center; justify-content: space-between; color: var(--fg-faint); font-size: 10px; }
.novel-status { display: inline-flex; align-items: center; gap: 5px; color: var(--warn); }
.novel-status::before { content: ""; width: 5px; height: 5px; background: currentColor; border-radius: 50%; }
.novel-item h3 { margin-top: 18px; font: 21px/1.4 var(--font-serif); }
.novel-item > p { display: -webkit-box; overflow: hidden; margin: 10px 0 16px; color: var(--fg-muted); font-size: 12px; line-height: 1.75; -webkit-line-clamp: 3; -webkit-box-orient: vertical; }
.novel-meta { display: flex; gap: 12px; color: var(--fg-faint); font-size: 10px; }
.novel-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: auto; padding-top: 15px; border-top: 1px solid var(--border); color: var(--fg-faint); font-size: 11px; }
.novel-foot strong { color: var(--accent-ink); font-weight: 600; }
.shelf-empty { padding: 48px 20px; text-align: center; color: var(--fg-muted); background: color-mix(in srgb, var(--bg-elev) 55%, transparent); border-bottom: 1px solid var(--border); }
.shelf-empty p { margin: 5px 0 0; color: var(--fg-faint); font-size: var(--fs-sm); }
.shelf-empty.compact { padding: 26px 20px; color: var(--fg-faint); font-size: var(--fs-sm); }
.finished-list { display: flex; flex-direction: column; }
.finished-row { display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; gap: 14px; align-items: center; padding: 18px 8px; border-bottom: 1px solid var(--border); cursor: pointer; }
.finished-row:hover { background: color-mix(in srgb, var(--bg-elev) 65%, transparent); }
.finished-mark { width: 22px; height: 22px; display: grid; place-items: center; color: var(--ok); background: var(--ok-soft); border-radius: 50%; font-size: 11px; }
.finished-row h3 { font: 17px/1.4 var(--font-serif); }
.finished-row p { margin: 4px 0 0; color: var(--fg-faint); font-size: 11px; }
.view-link { color: var(--accent-ink); font-size: 11px; }
@media (max-width: 1120px) { .novel-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 640px) {
  .shelf-head { align-items: flex-start; flex-direction: column; }
  .novel-grid { grid-template-columns: 1fr; }
  .novel-item { min-height: 220px; }
  .finished-row { grid-template-columns: 26px minmax(0, 1fr); }
  .view-link { grid-column: 2; }
}
</style>
