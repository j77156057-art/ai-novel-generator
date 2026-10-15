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

  <div v-if="error" class="notice err">{{ error }}</div>

  <!-- 新建入口 -->
  <button class="create-card" @click="goCreate">
    <span class="plus">＋</span>
    <span>新建一部小说</span>
  </button>

  <!-- 正在创作 -->
  <section class="card">
    <div class="card-title">
      正在创作 <span class="hint">{{ inProgress.length }} 部</span>
    </div>
    <div v-if="loading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="!inProgress.length" class="empty">还没有进行中的小说。</div>
    <div v-else class="list">
      <div
        v-for="p in inProgress"
        :key="p.id"
        class="list-item"
        style="cursor: pointer"
        @click="openProject(p.id)"
      >
        <div class="row tight" style="flex: 1; flex-wrap: wrap">
          <strong>{{ p.title }}</strong>
          <span class="faint mono" style="font-size: var(--fs-xs)">#{{ p.id }}</span>
          <span class="tag" :class="p.mode === 'expert' ? 'info' : ''">{{ p.mode }}</span>
          <span v-if="p.genre" class="tag">{{ p.genre }}</span>
          <span v-if="p.style" class="tag accent">{{ p.style }}</span>
          <span class="tag warn">创作中</span>
          <span class="faint" style="font-size: var(--fs-xs)">已生成 {{ p.segment_count }} 段</span>
        </div>
        <span class="faint" style="font-size: var(--fs-xs)">继续 →</span>
      </div>
    </div>
  </section>

  <!-- 已创建完 -->
  <section class="card">
    <div class="card-title">
      已创建完 <span class="hint">{{ finished.length }} 部</span>
    </div>
    <div v-if="!finished.length" class="empty">
      还没有标记完成的小说。在工作台点「保存并标记完成」即可归档到这里。
    </div>
    <div v-else class="list">
      <div
        v-for="p in finished"
        :key="p.id"
        class="list-item"
        style="cursor: pointer"
        @click="openProject(p.id)"
      >
        <div class="row tight" style="flex: 1; flex-wrap: wrap">
          <strong>{{ p.title }}</strong>
          <span class="faint mono" style="font-size: var(--fs-xs)">#{{ p.id }}</span>
          <span v-if="p.genre" class="tag">{{ p.genre }}</span>
          <span v-if="p.style" class="tag accent">{{ p.style }}</span>
          <span class="tag ok">已完成</span>
          <span class="faint" style="font-size: var(--fs-xs)">共 {{ p.segment_count }} 段</span>
        </div>
        <span class="faint" style="font-size: var(--fs-xs)">查看 →</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.create-card {
  font: inherit;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 20px;
  margin-bottom: var(--gap-lg);
  border: 2px dashed var(--border-strong);
  border-radius: var(--radius-lg);
  background: var(--bg-elev);
  color: var(--accent-ink);
  font-weight: 700;
  font-size: var(--fs-md);
  cursor: pointer;
}
.create-card:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.create-card .plus {
  display: inline-flex;
  width: 28px;
  height: 28px;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-full);
  background: var(--accent);
  color: var(--accent-fg);
  font-size: 20px;
  line-height: 1;
}
.list-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg);
}
.list-item:hover {
  border-color: var(--accent);
}
.row.tight {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
