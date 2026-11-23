<script setup lang="ts">
/**
 * 项目资产：把原先散落在项目工作台头部的四个项目级页面收成一处。
 *
 * 这些页面都是「围绕当前项目」的资料：全书摘要、人物模板、角色卡、力量体系。
 * 入口统一在这里之后，项目导航只需要一个「项目资产」，不再往顶部堆按钮。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import ProjectNav from "@/components/ProjectNav.vue";
import type { ProjectOut } from "@/api/types";

defineOptions({ name: "ProjectAssetsView" });

const route = useRoute();
const projectId = Number(route.params.id);
const project = ref<ProjectOut | null>(null);

const assets = computed(() => [
  {
    key: "digest",
    title: "全书摘要",
    description: "按书生成摘要与历史对比，查看主线覆盖、人物与伏笔密度。",
    to: `/projects/${projectId}/digest`,
    action: "打开摘要",
  },
  {
    key: "templates",
    title: "人物模板",
    description: "按档位提炼、保存人物塑造模板，并应用到当前项目。",
    to: `/projects/${projectId}/templates`,
    action: "管理模板",
  },
  {
    key: "cards",
    title: "角色卡",
    description: "维护身份、能力、声纹与关系；AI 只出草稿，保存由人确认。",
    to: `/projects/${projectId}/cards`,
    action: "打开角色卡",
  },
  {
    key: "power",
    title: "力量体系",
    description: "修炼体系、档位分层与角色修炼档案，供生成时注入。",
    to: `/projects/${projectId}/power`,
    action: "打开体系",
  },
  {
    key: "materials",
    title: "素材包",
    description: "本书专属的外部资料：后台按章纲调研，标注年份与可用处，支持手动增删改。",
    to: `/projects/${projectId}/materials`,
    action: "打开素材包",
  },
]);

onMounted(async () => {
  try {
    project.value = await api.getProject(projectId);
  } catch {
    /* 标题只是点缀：取不到不影响入口使用 */
  }
});
</script>

<template>
  <ProjectNav :project-id="projectId" />

  <header class="page-head">
    <div class="eyebrow">02 · 项目</div>
    <h1>项目资产</h1>
    <p>
      当前项目<span v-if="project">《{{ project.title }}》</span>的写作资料集中在这里：
      摘要、人物模板、角色卡与力量体系。
    </p>
  </header>

  <div class="asset-grid">
    <section v-for="asset in assets" :key="asset.key" class="card asset-card">
      <h2>{{ asset.title }}</h2>
      <p class="muted">{{ asset.description }}</p>
      <router-link class="btn" :to="asset.to">{{ asset.action }} →</router-link>
    </section>
  </div>
</template>

<style scoped>
.asset-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap);
}
.asset-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.asset-card h2 {
  font-family: var(--font-display);
  font-size: var(--fs-xl);
  font-weight: 600;
}
.asset-card p {
  margin: 0;
  flex: 1;
  font-size: var(--fs-sm);
  line-height: 1.7;
}
.asset-card .btn {
  align-self: flex-start;
}
@media (max-width: 880px) {
  .asset-grid {
    grid-template-columns: 1fr;
  }
}
</style>
