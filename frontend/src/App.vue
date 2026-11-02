<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { isLoggedIn, logout, username } from "./utils/auth";

const route = useRoute();
const router = useRouter();

const links = [
  { to: "/corpus", label: "语料库", icon: "M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5v-15ZM4 20.5A2.5 2.5 0 0 1 6.5 18H20M8 7h8M8 11h6" },
  { to: "/projects", label: "创作项目", icon: "M4 7.5h6l2-3h8v15H4v-12ZM4 10h16M8 14h8" },
  { to: "/mine", label: "我的小说", icon: "M5 3h11a3 3 0 0 1 3 3v15H8a3 3 0 0 1-3-3V3Zm0 15a3 3 0 0 1 3-3h11M9 7h6M9 11h6" },
  { to: "/settings", label: "模型设置", icon: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM19 12a7 7 0 0 1-.1 1.2l2 1.6-2 3.4-2.4-1a7 7 0 0 1-2 1.2L14 21h-4l-.5-2.6a7 7 0 0 1-2-1.2l-2.4 1-2-3.4 2-1.6A7 7 0 0 1 5 12a7 7 0 0 1 .1-1.2l-2-1.6 2-3.4 2.4 1a7 7 0 0 1 2-1.2L10 3h4l.5 2.6a7 7 0 0 1 2 1.2l2.4-1 2 3.4-2 1.6A7 7 0 0 1 19 12Z" },
];

const assistantTo = computed(() => {
  const id = route.params.id;
  return typeof id === "string" && /^\d+$/.test(id) ? `/projects/${id}/assistant` : "/assistant";
});
const crumb = computed(() => route.meta.title ?? "工作台");
const isLogin = computed(() => route.name === "login");

function signOut() {
  logout();
  void router.replace("/login");
}
</script>

<template>
  <main v-if="isLogin" class="login-shell">
    <router-view />
  </main>

  <div v-else class="app-shell">
    <aside class="sidebar">
      <router-link class="brand" to="/projects" aria-label="小说生成器首页">
        <span class="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24"><path d="M5 3h11a3 3 0 0 1 3 3v15H8a3 3 0 0 1-3-3V3Zm0 15a3 3 0 0 1 3-3h11M9 8h6" /></svg>
        </span>
        <span class="brand-copy"><strong>小说生成器</strong><small>NOVEL STUDIO</small></span>
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
      <div class="sidebar-note">
        <span class="sidebar-note-title">本地书稿工作区</span>
        <span>创作、语料与模型配置均连接本机服务。</span>
      </div>
      <div class="sidebar-foot">WAYFINDER · M1—M4</div>
    </aside>

    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumbs"><span>工作区</span><i>/</i><strong>{{ crumb }}</strong></div>
        <div v-if="isLoggedIn" class="user-area">
          <span class="session-state"><i />本地会话</span>
          <span class="user-chip" :title="username">
            <b>{{ username.slice(0, 1).toUpperCase() }}</b><span>{{ username }}</span>
          </span>
          <button class="logout-btn" type="button" @click="signOut">退出</button>
        </div>
      </header>
      <main class="main"><router-view :key="route.fullPath" /></main>
    </div>
  </div>
</template>
