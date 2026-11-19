<script setup lang="ts">
/**
 * 应用外壳：左侧「书架」，右侧工作区。
 *
 * 视觉方向（2026-11-18 定）：像写作软件，而不是工程控制台——
 * - 左侧第一屏先给「当前作品」一张书卡（封面块 + 书名 + 题材），
 *   而不是一串功能入口；
 * - 导航按「写作 / 素材 / 系统」分组，入口文字用产品语言；
 * - 顶部只保留面包屑与状态说明，不放任何操作按钮。
 *
 * 书卡数据来自 `GET /api/projects`（与项目列表同源）。当前路由带项目 id
 * 时高亮对应作品；取不到就整块不渲染，不影响任何页面功能。
 */
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { api } from "@/api/client";
import type { ProjectOut } from "@/api/types";

const route = useRoute();

// 只缓存「重」的工作台页面：项目工作台、助手工作台、知识图谱、章纲。
// 列表页 / 设置页不缓存，保证新建项目、改配置后回来一定是新数据。
const cachedViews = [
  "ProjectWorkspace",
  "AssistantWorkspace",
  "KgWorkspace",
  "ChapterOutlineView",
];

interface NavLink {
  to: string;
  label: string;
  icon: string;
}

interface NavGroup {
  label: string;
  links: NavLink[];
}

const assistantTo = computed(() => {
  const id = route.params.id;
  return typeof id === "string" && /^\d+$/.test(id) ? `/projects/${id}/assistant` : "/assistant";
});

const groups = computed<NavGroup[]>(() => [
  {
    label: "写作",
    links: [
      {
        to: assistantTo.value,
        label: "写作台",
        icon: "M20 15a3 3 0 0 1-3 3H9l-5 3v-6a3 3 0 0 1-1-2V7a3 3 0 0 1 3-3h11a3 3 0 0 1 3 3v8ZM8 9h8M8 13h5",
      },
      {
        to: "/projects",
        label: "创作项目",
        icon: "M4 7.5h6l2-3h8v15H4v-12ZM4 10h16M8 14h8",
      },
      {
        to: "/mine",
        label: "我的小说",
        icon: "M5 3h11a3 3 0 0 1 3 3v15H8a3 3 0 0 1-3-3V3Zm0 15a3 3 0 0 1 3-3h11M9 7h6M9 11h6",
      },
    ],
  },
  {
    label: "素材",
    links: [
      {
        to: "/corpus",
        label: "语料库",
        icon: "M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5v-15ZM4 20.5A2.5 2.5 0 0 1 6.5 18H20M8 7h8M8 11h6",
      },
    ],
  },
  {
    label: "系统",
    links: [
      {
        to: "/settings",
        label: "模型设置",
        icon: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.6-1H3v-4h.09A1.7 1.7 0 0 0 4.6 9a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-1.6V3h4v.09A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9a1.7 1.7 0 0 0 1.6 1h.09v4H21a1.7 1.7 0 0 0-1.6 1Z",
      },
    ],
  },
]);

const crumb = computed(() => route.meta.title ?? "");

// ---- 当前作品书卡 ----
const projects = ref<ProjectOut[]>([]);
const currentBook = computed<ProjectOut | null>(() => {
  const id = Number(route.params.id);
  if (!Number.isInteger(id) || id <= 0) return null;
  return projects.value.find((p) => p.id === id) ?? null;
});

const coverChar = computed(() => (currentBook.value?.title ?? "").trim().slice(0, 1) || "书");
const bookSub = computed(() => {
  const book = currentBook.value;
  if (!book) return "";
  const kind = book.continuation_novel_id ? "续写" : (book.genre || "原创");
  return `${kind} · 已写 ${book.segment_count ?? 0} 段`;
});

onMounted(async () => {
  try {
    projects.value = await api.listProjects();
  } catch {
    /* 书卡是导航点缀：取不到就只显示导航，不报错 */
  }
});
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

      <!-- 当前作品：写作软件的第一屏先回答「我在写哪本」 -->
      <router-link
        v-if="currentBook"
        class="side-book"
        :to="`/projects/${currentBook.id}`"
        :title="`《${currentBook.title}》`"
      >
        <span class="side-book-cover" aria-hidden="true">{{ coverChar }}</span>
        <span class="side-book-meta">
          <span class="side-book-title">{{ currentBook.title }}</span>
          <span class="side-book-sub">{{ bookSub }}</span>
        </span>
      </router-link>

      <nav class="nav" aria-label="主导航">
        <template v-for="g in groups" :key="g.label">
          <div class="nav-group">{{ g.label }}</div>
          <router-link v-for="l in g.links" :key="l.to" :to="l.to">
            <svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path :d="l.icon" /></svg>
            <span>{{ l.label }}</span>
          </router-link>
        </template>
      </nav>

      <div class="sidebar-foot">本地工作区 · 数据仅存本机</div>
    </aside>
    <div class="main-shell">
      <header class="topbar">
        <div class="breadcrumbs">工作台 <span aria-hidden="true">/</span> <strong>{{ crumb || "概览" }}</strong></div>
        <span class="topbar-note">长篇创作与校样</span>
      </header>
      <main class="main">
        <router-view v-slot="{ Component }">
          <keep-alive :include="cachedViews" :max="6">
            <component :is="Component" :key="route.fullPath" />
          </keep-alive>
        </router-view>
      </main>
    </div>
  </div>
</template>
