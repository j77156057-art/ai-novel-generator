<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { router } from "./router";
import { ACTIVE_THEME } from "./theme";

const route = useRoute();

const links = [
  { to: "/corpus", label: "语料库", idx: "01" },
  { to: "/projects", label: "创作项目", idx: "02" },
  { to: "/mine", label: "我的", idx: "03" },
  { to: "/settings", label: "模型设置", idx: "04" },
];

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
      </nav>
      <div class="sidebar-foot">
        M1–M4 · wayfinder<br />theme: {{ ACTIVE_THEME }}
      </div>
    </aside>
    <main class="main">
      <router-view :key="route.fullPath" />
    </main>
  </div>
</template>
