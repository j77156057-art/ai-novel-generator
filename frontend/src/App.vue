<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { router } from "./router";
import { ACTIVE_THEME } from "./theme";
import ModelSettingsDialog from "./components/ModelSettingsDialog.vue";
import { useModelSettingsDialog } from "./composables/useModelSettings";

const route = useRoute();
// 解构出的顶层 ref 在模板里自动解包；弹窗本体只在 App 挂一份。
const { open: modelSettingsOpen, openDialog, closeDialog } = useModelSettingsDialog();

const links = [
  { to: "/corpus", label: "语料库", idx: "01" },
  { to: "/projects", label: "创作项目", idx: "02" },
  { to: "/mine", label: "我的", idx: "03" },
  { to: "/settings", label: "模型设置", idx: "04" },
];

/**
 * 助手工作台是项目级页面（/projects/:id/assistant）：
 * 正处在某个项目路由上就直达该项目的助手页，否则先回项目列表选一个——
 * 顶部导航没有「当前项目」上下文，不能凭空指一个 projectId。
 */
const assistantTo = computed(() => {
  const id = route.params.id;
  return typeof id === "string" && /^\d+$/.test(id) ? `/projects/${id}/assistant` : "/projects";
});

const crumb = computed(() => route.meta.title ?? "");
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="dot" /> 小说生成器</div>
      <nav class="nav">
        <router-link v-for="l in links" :key="l.to" :to="l.to">
          <span class="idx">{{ l.idx }}</span>{{ l.label }}
        </router-link>
        <router-link :to="assistantTo">
          <span class="idx">05</span>助手工作台
        </router-link>
        <!-- 双模型设置是弹窗不是路由：生成 / 助手两个角色分开配，从任意页面都能开 -->
        <button type="button" class="nav-btn" @click="openDialog">
          <span class="idx">06</span>双模型设置
        </button>
      </nav>
      <div class="sidebar-foot">
        M1–M4 · wayfinder<br />theme: {{ ACTIVE_THEME }}
      </div>
    </aside>
    <main class="main">
      <router-view :key="route.fullPath" />
    </main>
    <ModelSettingsDialog :open="modelSettingsOpen" @close="closeDialog" />
  </div>
</template>

<style scoped>
/* 导航里的弹窗触发按钮：视觉与 router-link 保持一致（base.css 的 .nav a 样式
   只命中 a 元素，button 需要在这里补齐，避免出现两种导航长相）。 */
.nav .nav-btn {
  font: inherit;
  font-size: var(--fs-sm);
  padding: 9px 12px;
  border-radius: var(--radius-md);
  color: var(--fg-muted);
  display: flex;
  align-items: center;
  gap: 10px;
  border: var(--border-w) solid transparent;
  background: none;
  cursor: pointer;
  text-align: left;
  width: 100%;
}
.nav .nav-btn:hover {
  background: var(--bg-sunken);
  color: var(--fg);
}
.nav .nav-btn .idx {
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--fg-faint);
  width: 18px;
}
</style>
