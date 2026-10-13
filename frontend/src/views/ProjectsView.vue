<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";
import type { ProjectOut } from "@/api/types";

const router = useRouter();
const projects = ref<ProjectOut[]>([]);
const loading = ref(false);

const form = reactive({
  title: "",
});
const submitting = ref(false);
const error = ref<string | null>(null);

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
  <header class="page-head">
    <div class="eyebrow">02 · Projects</div>
    <h1>创作项目</h1>
    <p>
      建项目后进入工作台向导：<strong>选参考语料 → 类型 → 风格 → 内容 → 构思 → 生成</strong>。
      知识图谱在生成后才出现，不抢在前面。
    </p>
  </header>

  <section class="card">
    <div class="card-title">新建项目</div>
    <div class="row" style="align-items: flex-start">
      <div class="field" style="flex: 1; min-width: 220px">
        <label>标题 *</label>
        <input class="input" v-model="form.title" placeholder="例如：夜航船" />
      </div>
    </div>
    <div class="row" style="margin-top: 16px">
      <button class="btn primary" :disabled="submitting" @click="create">
        {{ submitting ? "创建中…" : "创建并进入向导 →" }}
      </button>
      <span v-if="error" class="notice err">{{ error }}</span>
    </div>
    <p class="faint" style="font-size: var(--fs-xs); margin-top: 12px">
      类型 / 风格 / 大概内容 / 参考语料都在向导里选。构思方式（直接构思 / 先追问再构思）
      在工作台第 ⑤ 步决定——那才是它真正起作用的地方。
    </p>
  </section>

  <section class="card">
    <div class="card-title">
      我的项目 <span class="hint">{{ projects.length }} 个（服务端）</span>
    </div>
    <div v-if="loading" class="empty"><span class="spinner" /> 加载中…</div>
    <div v-else-if="!projects.length" class="empty">还没有项目，先建一个。</div>
    <div v-else class="list">
      <div
        v-for="p in projects"
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
          <span v-if="p.status" class="tag">{{ p.status }}</span>
        </div>
        <span class="faint" style="font-size: var(--fs-xs)">进入 →</span>
      </div>
    </div>
  </section>
</template>
