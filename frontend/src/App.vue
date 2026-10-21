<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { ACTIVE_THEME } from "./theme";

const route = useRoute();

const links = [
  { to: "/corpus", label: "语料库", idx: "01" },
  { to: "/projects", label: "创作项目", idx: "02" },
  { to: "/mine", label: "我的", idx: "03" },
  { to: "/settings", label: "模型设置", idx: "04" },
];

/**
 * 助手工作台是项目级页面（/projects/:id/assistant）：
 * 正处在某个项目路由上就直达该项目的助手页；否则走顶层 /assistant，
 * 由它自动挑一个项目把对话上下文补上，而不是先弹项目列表。
 */
const assistantTo = computed(() => {
  const id = route.params.id;
  return typeof id === "string" && /^\d+$/.test(id) ? `/projects/${id}/assistant` : "/assistant";
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
        <!-- 模型设置只有一个入口：路由页 /settings（生成 / 助手 / 蒸馏三个角色
             都在那一个页面里分开配）。此前还有个「06 双模型设置」弹窗，
             与本页功能重叠，已退役。 -->
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
