<script setup lang="ts">
/**
 * 项目内统一导航：所有 `/projects/:id/*` 页面共用同一组入口。
 *
 * 为什么需要：此前项目级页面入口分散在项目工作台头部按钮、向导第⑦步、
 * 助手页与写作面板里，同一个功能换个页面就找不到。现在收敛为一条固定导航：
 * 工作台（概览/设定/写作都在向导里）、章纲、知识图谱、项目资产、助手。
 */
import { computed } from "vue";
import { useRoute } from "vue-router";

const props = defineProps<{ projectId: number }>();
const route = useRoute();

const base = computed(() => `/projects/${props.projectId}`);
const items = computed(() => [
  { key: "workspace", label: "工作台", to: base.value, hint: "概览、设定与生成" },
  { key: "outline", label: "章纲", to: `${base.value}/outline`, hint: "逐章大纲与校验" },
  { key: "kg", label: "知识图谱", to: `${base.value}/kg`, hint: "世界观、时间线与抽取状态" },
  { key: "assets", label: "项目资产", to: `${base.value}/assets`, hint: "摘要、模板、角色卡、力量体系" },
  { key: "assistant", label: "助手", to: `${base.value}/assistant`, hint: "对话式创作与续写" },
]);

const activeKey = computed(() => {
  const path = route.path;
  if (path === base.value) return "workspace";
  if (path.startsWith(`${base.value}/outline`)) return "outline";
  if (path.startsWith(`${base.value}/kg`)) return "kg";
  if (path.startsWith(`${base.value}/assistant`)) return "assistant";
  // 资产下的四个子页也归到「项目资产」，保证导航有明确归属。
  if (
    path.startsWith(`${base.value}/assets`) ||
    ["/digest", "/templates", "/cards", "/power"].some((suffix) =>
      path.startsWith(`${base.value}${suffix}`),
    )
  ) {
    return "assets";
  }
  return "";
});
</script>

<template>
  <nav class="project-nav" aria-label="项目导航">
    <router-link
      v-for="item in items"
      :key="item.key"
      :to="item.to"
      class="project-nav-item"
      :class="{ active: activeKey === item.key }"
      :title="item.hint"
    >
      {{ item.label }}
    </router-link>
  </nav>
</template>

<style scoped>
.project-nav {
  display: flex;
  flex-wrap: nowrap;
  gap: 4px;
  overflow-x: auto;
  padding: 4px;
  margin-bottom: var(--gap);
  border: var(--border-w) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-elev);
  scrollbar-width: thin;
}
.project-nav-item {
  flex: none;
  padding: 7px 13px;
  border: var(--border-w) solid transparent;
  border-radius: var(--radius-sm);
  color: var(--fg-muted);
  font-size: var(--fs-sm);
  font-weight: 600;
  white-space: nowrap;
  transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease;
}
.project-nav-item:hover {
  background: var(--bg-sunken);
  color: var(--fg);
}
.project-nav-item.active {
  background: var(--accent-soft);
  border-color: var(--accent-line);
  color: var(--accent-ink);
}
</style>
