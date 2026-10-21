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

onMounted(async () => {
  try {
    const projects = await api.listProjects();
    if (!projects.length) {
      router.replace("/projects");
      return;
    }
    // 返回按 id 倒序（最近创建在前）。最近建的往往是还没挂语料的空壳项目，
    // 直接拿 [0] 会一进来就落在空壳上。优先挑「已挂参考语料」的项目，
    // 都没有再退回 [0]，保证助手默认就有可对话的上下文。
    const withCorpus = projects.filter(
      (p) => (p.reference_novel_ids?.length ?? 0) > 0,
    );
    const target = withCorpus[0] ?? projects[0];
    router.replace(`/projects/${target.id}/assistant`);
  } catch {
    loading.value = false;
    error.value = "加载项目失败，无法进入助手。请确认后端已启动后重试。";
  }
});
</script>

<template>
  <div class="assistant-resolver">
    <div v-if="loading" class="hint">正在进入助手工作台…</div>
    <div v-else-if="error" class="hint error">{{ error }}</div>
  </div>
</template>

<style scoped>
.assistant-resolver {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.hint {
  color: var(--fg-muted);
  font-size: var(--fs-sm);
}
.hint.error {
  color: var(--danger, #d9534f);
}
</style>
