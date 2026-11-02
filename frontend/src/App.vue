<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";

const route = useRoute();

const links = [
  { to: "/corpus", label: "语料库", icon: "M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5v-15ZM4 20.5A2.5 2.5 0 0 1 6.5 18H20M8 7h8M8 11h6" },
  { to: "/projects", label: "创作项目", icon: "M4 7.5h6l2-3h8v15H4v-12ZM4 10h16M8 14h8" },
  { to: "/mine", label: "我的小说", icon: "M5 3h11a3 3 0 0 1 3 3v15H8a3 3 0 0 1-3-3V3Zm0 15a3 3 0 0 1 3-3h11M9 7h6M9 11h6" },
  { to: "/settings", label: "模型设置", icon: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.6-1H3v-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6V3h4v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.6 1h.09v4H21a1.7 1.7 0 0 0-1.6 1Z" },
];

const assistantTo = computed(() => {
  const id = route.params.id;
  return typeof id === "string" && /^\d+$/.test(id) ? `/projects/${id}/assistant` : "/assistant";
});

const crumb = computed(() => route.meta.title ?? "");
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <router-link class="brand" to="/projects" aria-label="小说生成器首页">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M5 3h11a3 3 0 0 1 3 3v15H8a3 3 0 0 1-3-3V3Zm0 15a3 3 0 0 1 3-3h11M9 8h6" /></svg>
        </span>
        <span>小说生成器</span>
      </router-link>
      <div class="workspace-label">工作区</div>
      <nav class="nav" aria-label="主导航">
        <router-link v-for="l in links" :key="l.to" :to="l.to">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path :d="l.icon" /></svg>
          <span>{{ l.label }}</span>
        </router-link>
        <router-link :to="assistantTo">
          <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15a3 3 0 0 1-3 3H9l-5 3v-6a3 3 0 0 1-1-2V7a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3v8ZM8 9h8M8 13h5" /></svg>
          <span>助手工作台</span>
        </router-link>
      </nav>
      <div class="sidebar-foot">本地书稿库</div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumbs">工作台 <span aria-hidden="true">/</span> <strong>{{ crumb || "概览" }}</strong></div>
        <span class="topbar-note">长篇创作与校样</span>
      </header>
      <main class="main"><router-view :key="route.fullPath" /></main>
    </div>
  </div>
</template>
