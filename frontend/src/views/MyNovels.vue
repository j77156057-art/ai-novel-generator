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
  <header class="page-head">
    <div class="eyebrow">03 · 我的</div>
    <h1>我的小说</h1>
    <p>
      一处看全部进度：正在创作（含刚建、构思中、续写中）与已标记完成的小说，点任意一项继续或回看。
    </p>
  </header>

  <button type="button" class="create-card" @click="goCreate">
    新建一部小说
  </button>

  <div
    v-if="loading"
    class="card state-panel loading-panel"
    role="status"
    aria-live="polite"
    aria-busy="true"
  >
    <div class="skeleton-stack" aria-hidden="true">
      <div class="skeleton-line skeleton-heading"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line skeleton-short"></div>
    </div>
    <p class="state-description">正在加载小说列表…</p>
  </div>

  <div v-else-if="error" class="card state-panel" role="alert">
    <h2 class="state-title">小说列表加载失败</h2>
    <p class="state-description">{{ error }}</p>
    <div class="state-actions">
      <button type="button" class="btn primary" @click="refresh">重新加载</button>
    </div>
  </div>

  <template v-else>
    <section class="card">
      <div class="card-title">
        正在创作 <span class="hint">{{ inProgress.length }} 部</span>
      </div>
      <div v-if="!inProgress.length" class="empty">还没有进行中的小说。</div>
      <div v-else class="list project-list">
        <button
          v-for="p in inProgress"
          :key="p.id"
          type="button"
          class="list-item project-item"
          @click="openProject(p.id)"
        >
          <div class="project-copy">
            <div class="project-title-line">
              <strong class="project-title" :title="p.title">{{ p.title }}</strong>
              <span class="project-id faint mono">#{{ p.id }}</span>
            </div>
            <div class="project-meta">
              <span class="tag" :class="p.mode === 'expert' ? 'info' : ''">{{ p.mode }}</span>
              <span v-if="p.genre" class="tag">{{ p.genre }}</span>
              <span v-if="p.style" class="tag accent">{{ p.style }}</span>
              <span class="tag warn">创作中</span>
              <span class="project-count faint">已生成 {{ p.segment_count }} 段</span>
            </div>
          </div>
        </button>
      </div>
    </section>

    <section class="card">
      <div class="card-title">
        已完成 <span class="hint">{{ finished.length }} 部</span>
      </div>
      <div v-if="!finished.length" class="empty">
        还没有标记完成的小说。在工作台点「保存并标记完成」即可归档到这里。
      </div>
      <div v-else class="list project-list">
        <button
          v-for="p in finished"
          :key="p.id"
          type="button"
          class="list-item project-item"
          @click="openProject(p.id)"
        >
          <div class="project-copy">
            <div class="project-title-line">
              <strong class="project-title" :title="p.title">{{ p.title }}</strong>
              <span class="project-id faint mono">#{{ p.id }}</span>
            </div>
            <div class="project-meta">
              <span v-if="p.genre" class="tag">{{ p.genre }}</span>
              <span v-if="p.style" class="tag accent">{{ p.style }}</span>
              <span class="tag ok">已完成</span>
              <span class="project-count faint">共 {{ p.segment_count }} 段</span>
            </div>
          </div>
        </button>
      </div>
    </section>
  </template>
</template>

<style scoped>
.create-card {
  font: inherit;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 15px 18px;
  margin-bottom: var(--gap-lg);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  color: var(--accent-ink);
  font-weight: 600;
  font-size: var(--fs-md);
  cursor: pointer;
}
.create-card:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.create-card:focus-visible,
.project-item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.loading-panel {
  text-align: center;
}
.loading-panel .skeleton-stack {
  max-width: 560px;
  margin: 0 auto var(--gap-md);
}
.skeleton-heading {
  width: 42%;
}
.skeleton-short {
  width: 68%;
}
.project-list {
  gap: var(--gap-sm);
}
.list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap-md);
  padding: 12px 14px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.project-item {
  width: 100%;
  min-width: 0;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.project-item:hover {
  border-color: var(--accent);
}
.project-copy {
  flex: 1;
  min-width: 0;
}
.project-title-line {
  display: flex;
  align-items: flex-start;
  gap: var(--gap-sm);
  min-width: 0;
}
.project-title {
  min-width: 0;
  overflow-wrap: anywhere;
  line-height: 1.5;
}
.project-id {
  flex: none;
  font-size: var(--fs-xs);
  line-height: 1.5;
}
.project-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--gap-sm);
  min-width: 0;
  margin-top: 7px;
}
.project-meta .tag {
  max-width: 100%;
  overflow-wrap: anywhere;
}
.project-count {
  font-size: var(--fs-xs);
}
</style>
