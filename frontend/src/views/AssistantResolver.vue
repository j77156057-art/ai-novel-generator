<script setup lang="ts">
/**
 * 顶层助手入口（/assistant）。
 *
 * 助手是项目级资源（/projects/:id/assistant），但产品决策要求「打开应用就
 * 进入对话」—— 不能先弹项目列表。这里负责在顶层自动选一个项目作为对话
 * 上下文：优先挑「已挂参考语料」的项目（最近建的往往是空壳，需要跳过），
 * 然后 replace 到它的助手页。一个项目都没有时退回 /projects 让用户先建一个。
 */
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { api } from "@/api/client";

const router = useRouter();
const loading = ref(true);
const error = ref<string | null>(null);

async function resolveAssistant() {
  loading.value = true;
  error.value = null;
  try {
    const projects = await api.listProjects();
    if (!projects.length) {
      await router.replace("/projects");
      return;
    }
    // 返回按 id 倒序（最近创建在前）。最近建的往往是还没挂语料的空壳项目，
    // 直接拿 [0] 会一进来就落在空壳上。优先挑「已挂参考语料」的项目，
    // 都没有再退回 [0]，保证助手默认就有可对话的上下文。
    const withCorpus = projects.filter(
      (p) => (p.reference_novel_ids?.length ?? 0) > 0,
    );
    const target = withCorpus[0] ?? projects[0];
    await router.replace(`/projects/${target.id}/assistant`);
  } catch {
    loading.value = false;
    error.value = "项目加载或页面跳转失败，暂时无法进入助手。请确认后端已启动后重试。";
  }
}

onMounted(resolveAssistant);
</script>

<template>
  <div class="assistant-resolver">
    <div
      v-if="loading"
      class="state-panel resolver-panel"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div class="skeleton-stack" aria-hidden="true">
        <div class="skeleton-line resolver-skeleton-title"></div>
        <div class="skeleton-line"></div>
      </div>
      <h1 class="state-title">正在进入助手工作台</h1>
      <p class="state-description">正在加载项目并选择合适的对话上下文。</p>
    </div>
    <div v-else-if="error" class="state-panel resolver-panel" role="alert">
      <h1 class="state-title">暂时无法进入助手</h1>
      <p class="state-description">{{ error }}</p>
      <div class="state-actions">
        <button type="button" class="btn primary" @click="resolveAssistant">重试</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.assistant-resolver {
  height: 100%;
  min-height: 240px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
.resolver-panel {
  width: min(440px, 100%);
  text-align: center;
}
.resolver-panel .skeleton-stack {
  margin-bottom: var(--gap-lg);
}
.resolver-panel .state-actions {
  justify-content: center;
}
.resolver-skeleton-title {
  width: 60%;
  margin-right: auto;
  margin-left: auto;
}
</style>
